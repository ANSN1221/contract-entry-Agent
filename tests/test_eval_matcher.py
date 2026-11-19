"""评测脚本的匹配逻辑验收。

匹配规则错了，评测分数就是假的——这比模型抽错更危险，所以规则本身要有测试：
中文数字、百分比、日期格式差异都必须判对，真实错配必须判错。
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from evaluate import match, normalize, numbers  # noqa: E402


@pytest.mark.parametrize(
    ("expected", "actual", "ok"),
    [
        # 中文数字与阿拉伯数字等价
        ("12 个月", "系统验收合格之日起十二个月", True),
        ("5", "合同总价的百分之五", True),
        ("每日 0.05% 上限 10%", "按合同总价的百分之零点零五……不超过合同总价的百分之十", True),
        # 日期两种写法等价
        ("2026-04-01 至 2027-03-31", "自 2026 年 4 月 1 日起至 2027 年 3 月 31 日止", True),
        ("2026-03-12", "2026 年 3 月 12 日", True),
        # 金额千分位无关
        ("1280000.00", "¥1,280,000.00 元", True),
        # 纯文本字段
        ("上海仲裁委员会", "提交上海仲裁委员会按其规则仲裁", True),
        ("江南智造科技有限公司", "甲方（采购方）：江南智造科技有限公司", True),
        # 真错必须判错
        ("12 个月", "质保期为二十四个月", False),
        ("5", "履约保证金比例为百分之十", False),
        ("上海仲裁委员会", "提交北京仲裁委员会", False),
        ("1280000.00", "12800000.00", False),
    ],
)
def test_match(expected: str, actual: str, ok: bool) -> None:
    assert match(expected, actual) is ok


def test_match_with_empty_actual() -> None:
    assert match("12 个月", None) is False
    assert match("12 个月", "") is False


def test_numbers_normalizes_leading_zero() -> None:
    assert numbers("2026-04-01") == numbers("2026 年 4 月 1 日")


def test_numbers_excludes_percent_keyword() -> None:
    """「百分之五」里的「百」不能被当成 100。"""
    assert numbers("百分之五") == {"5"}
    assert numbers("百分之零点零五") == {"0.05"}


def test_numbers_picks_decimals_and_integers() -> None:
    assert numbers("上限为 10%，每日 0.05%") == {"10", "0.05"}


def test_normalize_strips_punctuation() -> None:
    assert normalize("甲方：某某 公司（上海）") == "甲方某某公司上海"
