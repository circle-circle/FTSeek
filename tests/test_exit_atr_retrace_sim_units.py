"""`scripts/exit_atr_retrace_sim.py` 的 ATR 量纲钉 (2026-09-14)。

立案: DEB-20260910 §16.10(b) 断言该脚本 `atr_pct >= 3.0` 是"单位误用 ⇒ 判据恒假",
并给出一条危险的推广:"任何把 atr_pct 当百分数用的代码都是错的"。实测该断言**不成立**:

  · 该脚本自己算 `metadata_json.$.atr / entry_price * 100` —— `$.atr` 是**绝对价差**
    (n=502, min 0 / max 109.43 / avg 13.21 美元), 故结果是**百分点** (min 0 /
    max 6.29 / avg 3.75);
  · §16.4 量的 728 行 max 0.1538 是 `playbook_instructions.atr_pct` —— **比例**尺,
    与本脚本同名不同尺, 两者互不相干;
  · 运行时输出 `低中波 n=36 / 高波 n=372`, 两桶皆非空 ⇒ 判据有区分度。

为何要钉: 若有人照 §16.10(b) 去"修"(把值除以 100 或把阈值改成 0.03), 各臂的
`clamp(k×ATR, 0.5, 1.5)` 会因 k×0.0375≈0.009 **全部钳到下限 0.5** ⇒ 所有臂退化成
同一条, 这把退出参数尺子会**静默失效**且输出看不出异常 (与本仓"尺子被污染 →
参数裁决基于错样本"的既有事故同源)。别名已由 `atr_pct` 改为 `atr_pp` 根治同名。

回滚: 删本文件即可 (纯只读契约钉, 不依赖生产库内容)。
"""
import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SIM = PROJECT_ROOT / "scripts" / "exit_atr_retrace_sim.py"


@pytest.fixture(scope="module")
def src():
    return SIM.read_text(encoding="utf-8")


def test_script_declares_its_own_unit(src):
    """量纲声明必须在场 (本仓纪律: 无量纲声明 = 迟早用错尺子)。"""
    assert "量纲声明" in src, "文件头的量纲声明被删了 → 同名不同尺会再次被误读"
    assert "atr_pp" in src and "百分点" in src
    assert "playbook_instructions.atr_pct" in src, "必须写明'另一个 atr_pct 是比例尺'以断歧义"


def test_sql_alias_is_percentage_scaled(src):
    """SQL 别名必须带 `*100` (绝对价差 → 百分点)。删掉 *100 = 整把尺子缩 100 倍。"""
    m = re.search(
        r"CAST\(json_extract\(metadata_json,'\$\.atr'\) AS REAL\)\s*/\s*entry_price\s*\*\s*100\s+AS\s+atr_pp",
        src)
    assert m, "ATR 口径被改 (应为 atr/entry_price*100 AS atr_pp) → 各臂 clamp 会退化"


def test_no_ratio_scaled_identifier_left(src):
    """同名不同尺已根治: 文件内不得再出现被当数值用的 `atr_pct` 标识符。

    注释里提到它 (用于断歧义) 是允许且必要的, 故只查代码形态 `d["atr_pct"]` / `AS atr_pct`。
    """
    assert 'd["atr_pct"]' not in src, "比例尺标识符回流 → §16.10(b) 的误读会重演"
    assert not re.search(r"AS\s+atr_pct\b", src), "SQL 别名退回 atr_pct = 与比例尺列同名"


def test_bucket_threshold_stays_in_percentage_points(src):
    """分桶阈值 3.0 是**百分点**; 改成 0.03 (比例尺) 会让'高波'桶吞掉全部样本。"""
    m = re.search(r'"high"\s+if\s+d\["atr_pp"\]\s*>=\s*([0-9.]+)', src)
    assert m, "分桶判据被改写, 需重新核量纲"
    assert float(m.group(1)) == pytest.approx(3.0), (
        f"阈值 {m.group(1)} 不是 3.0 百分点 —— 若为 0.03 一类则已按错误量纲'修'过")


def test_ratio_scale_would_degenerate_every_arm():
    """退化数学钉: 同一批 k 值下, 百分点尺有展开, 比例尺全部钳到下限 (即 §16.10(b) 的'修法'后果)。

    不读生产库: 只用两把尺子的实测均值 (百分点 3.75 / 比例 0.0399) 做纯数值对照,
    故本钉不随库内容漂移。
    """
    clamp = lambda v, lo, hi: min(max(v, lo), hi)
    ks = (0.20, 0.25, 0.30, 0.35, 0.40)          # 臂A 的 k 取值 (源码同步)
    pp_scale = [clamp(k * 3.75, 0.5, 1.5) for k in ks]      # 本文件的百分点尺
    ratio_scale = [clamp(k * 0.0399, 0.5, 1.5) for k in ks]  # §16.10(b) 主张的比例尺
    assert len(set(round(v, 6) for v in pp_scale)) > 1, "百分点尺必须有区分度 (否则臂A 白跑)"
    assert len(set(ratio_scale)) == 1 and ratio_scale[0] == 0.5, (
        "比例尺应使全部臂钳到下限 0.5 —— 若这条不再成立, 说明 clamp 边界被改, 需重核")
