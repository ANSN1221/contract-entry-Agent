"""生成一份合成合同 PDF，用于本地联调（不含任何真实主体信息）。

为什么要有这个脚本：项目一需要 `data/raw/*.pdf` 才能建索引，而真实合同
既敏感又不该进仓库。合成样例覆盖 12 个通用字段，主链路就能端到端跑通。

用法::

    uv run python scripts/make_sample_contract.py --out data/samples
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pymupdf as fitz

PAGE_WIDTH, PAGE_HEIGHT = 595, 842  # A4，单位 point
MARGIN = 64
BODY_SIZE = 10.5
LINE_HEIGHT = 18
CJK_FONT = "china-s"  # PyMuPDF 内置简体中文字体，无需外部字体文件

TITLE = "软件服务采购合同"

SECTIONS: list[tuple[str, str]] = [
    ("合同编号", "HT-2026-0418"),
    (
        "合同主体",
        "甲方（采购方）：江南智造科技有限公司，住所地上海市浦东新区张江路 88 号。\n"
        "乙方（服务方）：北辰云软件服务有限公司，住所地杭州市滨江区网商路 66 号。",
    ),
    (
        "第一条 服务内容",
        "乙方向甲方提供智能制造执行系统的定制开发与部署服务，包括需求调研、系统设计、"
        "开发实施、上线培训及一年期运维支持。交付物清单以附件一《服务范围说明书》为准。",
    ),
    (
        "第二条 合同总价",
        "本合同总价为人民币壹佰贰拾捌万元整（¥1,280,000.00 元），已包含税费、差旅及"
        "实施费用。除双方书面确认的变更外，总价不作调整。",
    ),
    (
        "第三条 付款方式",
        "甲方按三阶段付款：合同生效后十个工作日内支付合同总价的百分之三十作为预付款；"
        "系统上线试运行验收合格后十五个工作日内支付百分之五十；质保期届满且无质量异议后"
        "十个工作日内支付剩余百分之二十。乙方应在收款前开具等额增值税专用发票。",
    ),
    (
        "第四条 履行期限",
        "本合同履行期限自 2026 年 4 月 1 日起至 2027 年 3 月 31 日止。乙方应于 2026 年 "
        "9 月 30 日前完成系统上线；因甲方原因导致的延期，交付日期相应顺延。",
    ),
    (
        "第五条 双方权利义务",
        "甲方应按约定提供业务数据、测试环境及对接人员，并保证所提供资料真实合法。"
        "乙方应按项目计划投入具备相应资质的技术人员，未经甲方书面同意不得将主要工作"
        "分包给第三方。",
    ),
    (
        "第六条 验收标准",
        "系统应满足附件二《功能验收清单》所列全部强制项，并在连续十个工作日内稳定运行、"
        "故障次数不超过两次。验收由双方共同组织，验收合格以双方签署的验收报告为准。",
    ),
    (
        "第七条 履约保证金",
        "本合同签订后五个工作日内，乙方应向甲方提交合同总价百分之五的履约保证金，"
        "形式为银行保函。保证金在系统验收合格后十个工作日内无息退还。",
    ),
    (
        "第八条 质保与运维",
        "质保期为系统验收合格之日起十二个月。质保期内乙方提供七乘二十四小时响应服务，"
        "严重故障两小时内到场、二十四小时内修复。质保期满后可另行签订运维服务协议。",
    ),
    (
        "第九条 知识产权",
        "乙方交付的定制软件源代码及文档著作权归甲方所有；乙方保留其既有通用组件的权利，"
        "并授予甲方永久、免费、可转让的使用许可。",
    ),
    (
        "第十条 保密条款",
        "双方对履行合同过程中知悉的对方商业秘密承担保密义务，保密期限自知悉之日起五年。"
        "保密义务不因合同终止而免除。",
    ),
    (
        "第十一条 违约责任",
        "乙方逾期交付的，每逾期一日按合同总价的百分之零点零五向甲方支付违约金，"
        "累计不超过合同总价的百分之十；逾期超过三十日的，甲方有权解除合同并要求赔偿损失。"
        "甲方逾期付款的，参照同一标准承担违约责任。",
    ),
    (
        "第十二条 争议解决",
        "因本合同引起的争议，双方应友好协商解决；协商不成的，提交上海仲裁委员会"
        "按其届时有效的仲裁规则进行仲裁，仲裁裁决为终局裁决，对双方均有约束力。",
    ),
    (
        "第十三条 其他",
        "本合同自双方授权代表签字并加盖公章之日起生效，一式肆份，双方各执贰份，"
        "具有同等法律效力。附件为本合同不可分割的组成部分。",
    ),
    (
        "签署",
        "甲方（盖章）：江南智造科技有限公司        乙方（盖章）：北辰云软件服务有限公司\n"
        "授权代表：____________________            授权代表：____________________\n"
        "签订日期：2026 年 3 月 12 日              签订日期：2026 年 3 月 12 日",
    ),
]


def _wrap(text: str, max_chars: int) -> list[str]:
    """按字符数硬折行：中文没有空格，只能估宽度。"""
    lines: list[str] = []
    for raw_line in text.split("\n"):
        while len(raw_line) > max_chars:
            lines.append(raw_line[:max_chars])
            raw_line = raw_line[max_chars:]
        lines.append(raw_line)
    return lines


def build_pdf(out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "合同样例-软件服务采购.pdf"

    doc = fitz.open()
    page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
    y = MARGIN

    def new_page() -> None:
        nonlocal page, y
        page = doc.new_page(width=PAGE_WIDTH, height=PAGE_HEIGHT)
        y = MARGIN

    # 标题
    page.insert_textbox(
        fitz.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + 40),
        TITLE,
        fontname=CJK_FONT,
        fontsize=18,
        align=fitz.TEXT_ALIGN_CENTER,
    )
    y += 56

    max_chars = int((PAGE_WIDTH - 2 * MARGIN) / (BODY_SIZE * 1.02))

    for heading, body in SECTIONS:
        needed = LINE_HEIGHT * (2 + body.count("\n"))
        if y + needed > PAGE_HEIGHT - MARGIN:
            new_page()

        page.insert_text(
            (MARGIN, y + 12),
            heading,
            fontname=CJK_FONT,
            fontsize=11.5,
        )
        y += LINE_HEIGHT + 2

        for line in _wrap(body, max_chars):
            if y + LINE_HEIGHT > PAGE_HEIGHT - MARGIN:
                new_page()
            page.insert_textbox(
                fitz.Rect(MARGIN, y, PAGE_WIDTH - MARGIN, y + LINE_HEIGHT),
                line,
                fontname=CJK_FONT,
                fontsize=BODY_SIZE,
            )
            y += LINE_HEIGHT
        y += 10

    doc.save(str(path))
    doc.close()
    return path


def main() -> int:
    parser = argparse.ArgumentParser(description="生成合成合同 PDF（联调用）")
    parser.add_argument("--out", type=Path, default=Path("data/samples"))
    args = parser.parse_args()

    path = build_pdf(args.out)
    with fitz.open(str(path)) as doc:
        print(f"已生成：{path}")
        print(f"页数：{doc.page_count}  字符数：{sum(len(p.get_text()) for p in doc)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
