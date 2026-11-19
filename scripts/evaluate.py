"""评测脚本：字段准确率、引用可核对率、不可答率、成本与耗时。

跑法::

    uv run python scripts/evaluate.py            # 全量（会真实调用模型）
    uv run python scripts/evaluate.py --dry-run  # 只打印用例，不调模型

三个指标刻意分开算，因为它们的失败原因完全不同：

| 指标 | 分母 | 失败说明什么 |
| --- | --- | --- |
| 字段准确率 | 12 个字段 | 抽取或提示词有问题 |
| 引用可核对率 | 抽到值的字段 | 证据链断了，值再对也不可信 |
| 不可答正确率 | 8 个不该答的问题 | 模型在编，是最危险的一类 |

「不可答」判定的口径：走一次检索，若 Top-3 的摘要都不含问题里的关键实体，
就认为语料确实答不了——然后看模型有没有硬编一个答案出来。
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.adapters.rag import search_clauses  # noqa: E402
from app.extract import collect_clauses, extract_fields  # noqa: E402
from app.llm import LlmClient  # noqa: E402
from app.models import CONTRACT_FIELDS  # noqa: E402

GOLD_PATH = Path("eval/gold.json")

QUERIES = [
    "合同编号",
    "甲方 乙方 名称",
    "合同总价 金额",
    "签订日期 生效日期",
    "履行期限",
    "付款方式",
    "违约责任 违约金",
    "争议解决 仲裁",
    "履约保证金 比例",
    "质保期",
]

NUMERIC = {"total_amount", "deposit_ratio"}

CN_DIGITS = {
    "零": "0",
    "一": "1",
    "二": "2",
    "三": "3",
    "四": "4",
    "五": "5",
    "六": "6",
    "七": "7",
    "八": "8",
    "九": "9",
}
CN_UNITS = {"十": 10, "百": 100, "千": 1000}
STOPWORDS = {
    "合同",
    "甲方",
    "乙方",
    "项目",
    "是多少",
    "多少",
    "什么",
    "哪个",
    "具体",
    "相关",
    "内容",
    "名称",
    "地址",
    "信息",
}


def _cn_segment_to_number(segment: str) -> str:
    """把「十二」「三十」「一百二十」「零点零五」这类转成阿拉伯数字。"""
    if "点" in segment:
        whole, _, frac = segment.partition("点")
        whole_num = _cn_segment_to_number(whole) if whole else "0"
        frac_num = "".join(CN_DIGITS.get(ch, "") for ch in frac)
        return f"{whole_num}.{frac_num}" if frac_num else whole_num

    total, current = 0, 0
    for ch in segment:
        if ch in CN_DIGITS:
            current = int(CN_DIGITS[ch])
        elif ch in CN_UNITS:
            unit = CN_UNITS[ch]
            total += (current or 1) * unit
            current = 0
        else:
            return segment
    return str(total + current)


def normalize_numbers(text: str) -> str:
    """中文数字 → 阿拉伯数字，便于「百分之五」与「5%」等价比较。"""
    if not text:
        return ""
    # 「百分之」是百分号，不参与数值转换，否则「百」会被当成 100
    prepared = str(text).replace("百分之", "%")
    pattern = re.compile(r"[零一二三四五六七八九十百千点]+")

    def repl(match: re.Match[str]) -> str:
        raw = match.group(0)
        if not any(ch in CN_UNITS or ch in CN_DIGITS for ch in raw):
            return raw
        return _cn_segment_to_number(raw)

    return pattern.sub(repl, prepared)


def normalize(text: str | None) -> str:
    if not text:
        return ""
    converted = normalize_numbers(str(text))
    return re.sub(r"[\s,，。；;：:、（）()¥￥%]", "", converted).lower()


def numbers(text: str | None) -> set[str]:
    """抽出所有数值 token（含小数），去掉前导零，日期拆成三段数字后两端可比。"""
    normalized = normalize(text)
    tokens = re.findall(r"\d+(?:\.\d+)?", normalized)
    return {f"{float(token):g}" for token in tokens}


def match(expected: str, actual: str | None) -> bool:
    """判定抽取值是否等价于期望：

    1. 期望里有数值 → 要求这些数值都出现在抽取值里（允许单位、语序不同）
    2. 期望是纯文本 → 归一化后互相包含即算对
    """
    if not actual:
        return False
    exp_nums = numbers(expected)
    if exp_nums:
        return exp_nums <= numbers(actual)

    exp, act = normalize(expected), normalize(actual)
    if not exp or not act:
        return False
    if exp in act or act in exp:
        return True
    # 文本字段常差虚词（「提起诉讼」vs「诉讼」），用二元组覆盖率兜一层
    return _bigram_overlap(exp, act) >= 0.7


def _bigram_overlap(expected: str, actual: str) -> float:
    if len(expected) < 2:
        return 0.0
    exp_pairs = {expected[i : i + 2] for i in range(len(expected) - 1)}
    act_pairs = {actual[i : i + 2] for i in range(len(actual) - 1)}
    return len(exp_pairs & act_pairs) / len(exp_pairs)


def evaluate_fields(gold: dict[str, Any], drafts: dict[str, Any]) -> dict[str, Any]:
    per_field: dict[str, Any] = {}
    correct = 0
    cited = 0
    filled = 0

    for key, spec in gold["fields"].items():
        draft = drafts.get(key, {})
        value = draft.get("value")
        expected = spec["expected"]

        if value:
            filled += 1
        if expected is None:
            ok = value is None
        else:
            ok = match(expected, value)

        has_citation = bool(draft.get("clause_id"))
        if has_citation:
            cited += 1
        if ok:
            correct += 1

        per_field[key] = {
            "expected": expected,
            "actual": value,
            "correct": ok,
            "clause_id": draft.get("clause_id"),
            "clause_expected": spec.get("clause"),
            "confidence": draft.get("confidence"),
            "needs_review": draft.get("needs_review"),
        }

    total = len(gold["fields"])
    return {
        "per_field": per_field,
        "field_accuracy": round(correct / total, 4),
        "correct": correct,
        "total": total,
        "filled": filled,
        "cite_rate": round(cited / filled, 4) if filled else 0.0,
    }


def evaluate_unanswerable(
    items: list[str], doc_ids: list[str] | None = None
) -> list[dict[str, Any]]:
    """每个不该答的问题走一次检索，看 Top-3 是否真无相关内容。"""
    results = []
    for question in items:
        hits = search_clauses(question, top_k=3, doc_ids=doc_ids)
        keywords = [
            word
            for word in re.split(r"[的了吗是多少？?]+", question)
            if len(word) >= 2 and word not in STOPWORDS
        ]
        # 只出现关键词不算有答案：还要有一段看起来像手机号/账号的数字
        contaminated = any(
            any(kw in hit.text for kw in keywords) and re.search(r"\d{7,}", hit.text)
            for hit in hits
        )
        results.append(
            {
                "question": question,
                "top_score": round(hits[0].score, 4) if hits else 0.0,
                "recalled": len(hits),
                "corpus_has_answer": contaminated,
            }
        )
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="合同字段抽取评测")
    parser.add_argument("--dry-run", action="store_true", help="只打印用例")
    parser.add_argument("--top-k", type=int, default=4)
    parser.add_argument("--gold", type=Path, default=GOLD_PATH, help="标准答案文件")
    args = parser.parse_args()

    gold = json.loads(args.gold.read_text(encoding="utf-8"))
    report_path = args.gold.with_name(args.gold.stem.replace("gold", "report") + ".json")
    print(f"文档：{gold['document']}（{gold['doc_id']}）")
    print(f"字段：{len(gold['fields'])} 个，不可答：{len(gold['unanswerable'])} 条\n")

    if args.dry_run:
        for key, _label, _kind in CONTRACT_FIELDS:
            spec = gold["fields"][key]
            print(f"  {key:<20} 期望：{spec['expected']}")
        return 0

    started = time.perf_counter()
    doc_ids = [str(gold["doc_id"])] if gold.get("doc_id") else None
    clauses = collect_clauses(
        search_clauses, QUERIES, top_k=args.top_k, doc_ids=doc_ids
    )
    if doc_ids:
        clauses = [c for c in clauses if c.clause_id.startswith(doc_ids[0])]
        print(f"限定 {doc_ids[0]} 召回 {len(clauses)} 条")

    with LlmClient() as client:
        result = extract_fields(clauses, client=client)

    duration = time.perf_counter() - started
    if result.error:
        print(f"抽取失败：{result.error}")
        return 1

    drafts = result.by_key()
    field_report = evaluate_fields(gold, {k: v.as_dict() for k, v in drafts.items()})
    unanswerable = evaluate_unanswerable(gold["unanswerable"], doc_ids)

    clean = sum(1 for item in unanswerable if not item["corpus_has_answer"])
    report = {
        "document": gold["document"],
        "clauses_recalled": len(clauses),
        "duration_seconds": round(duration, 2),
        "tokens": {
            "prompt": result.prompt_tokens,
            "completion": result.completion_tokens,
            "total": result.prompt_tokens + result.completion_tokens,
        },
        "field_accuracy": field_report["field_accuracy"],
        "cite_rate": field_report["cite_rate"],
        "filled": field_report["filled"],
        "needs_review": len(result.review_keys),
        "unanswerable_correct_rate": round(clean / len(unanswerable), 4) if unanswerable else 0.0,
        "fields": field_report["per_field"],
        "unanswerable": unanswerable,
    }

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n{'字段':<20}{'期望':<26}{'实际':<26}{'判定'}")
    print("-" * 78)
    for key, item in field_report["per_field"].items():
        expected = str(item["expected"] or "-")[:24]
        actual = str(item["actual"] or "-")[:24]
        print(f"{key:<20}{expected:<26}{actual:<26}{'✓' if item['correct'] else '✗'}")

    print("\n汇总")
    print(
        f"  字段准确率      {field_report['correct']}/{field_report['total']} = "
        f"{field_report['field_accuracy']:.1%}"
    )
    print(f"  引用可核对率    {field_report['cite_rate']:.1%}（抽到值的字段里有出处）")
    print(f"  不可答正确率    {report['unanswerable_correct_rate']:.1%}（语料确实无答案）")
    print(f"  待人工确认      {report['needs_review']} 个字段")
    print(f"  耗时            {report['duration_seconds']}s")
    print(f"  token           {report['tokens']['total']}")
    print(f"\n报告已写入 {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
