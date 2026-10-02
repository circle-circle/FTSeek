"""horizon_confidence 量纲/消费方契约钉子 (2026-09-09 盘后复盘立案)。

立案事实 (data/ftseek_us.db::playbook_instructions, date>=2026-08-20, n≈840):
同一列里并存 5 个数量级的取值 —— 0.0 (698 行/83%)、0.7 (103 行)、0.64/0.5/1.0、
以及 1e-5..1e-4 量级 (0.000136 / 7.4e-05 / 3.7e-05)，且**出现过 >1 的 2.0 (9/1) 与
1.5 (9/2)**。列名叫 confidence，实际语义是 `|short_score - long_score|` 得分差
(`app/playbook/horizon_selector.py::HorizonDecision`)，既不是概率也没有 0-1 上界。

四条生成路径不同基 (量纲声明已写进 HorizonDecision 的 SSOT 注释):
  1. _score_with_model  → 历史收益贡献均值之差, 近平局时 ~1e-5..1e-2
  2. _fallback_score    → 0.5/0.2 步长规则累加之差, ~0.1..2.0 (常见 0.7)
  3. select() 禁用分支  → 硬编码 1.0
  4. pipeline.py 异常兜底 → 硬编码 0.0 (与"真近平局的 0.0"不可区分)

危害: 对它设任何绝对阈值 (如 >0.3 才算"有信心") 会把路径 1 的**全部**行判成低置信，
与证据强度无关 —— 属军规「单位混装」第四案的同型病灶 (前三案: ATR 百分比直落绝对槽 /
da_glm_shadow 异基 taint 误杀 / KR2 快照漏键假绿)。

为何只钉不改: horizon 的**选择**走 _choose() 的比值判据与 fallback 的 *1.1 判据，
**都不读 confidence** ⇒ 归一化本字段不会改变 horizon 选择，也就不触碰
playbook_horizon.yaml 的退出参数；但反过来，在 OKR-2026-09 C1 退出参数冻结窗
(→2026-09-30) 内动 horizon 链路的任何计算都需先证明 KR2 cohort 唯一值数不受影响，
故本批只做量纲声明 + 钉子，不改计算 (改法与重开条件见
docs/reviews/REVIEW-20260909-postclose-recap.md §C)。

回滚: 删本文件 + 还原 app/playbook/horizon_selector.py 的 HorizonDecision 注释块
      (备份 /tmp/horizon_selector.py.bak-20260909)。
"""
import ast
import inspect
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.playbook.horizon_selector import HorizonDecision, HorizonSelector


SELECTOR_SRC = PROJECT_ROOT / "app" / "playbook" / "horizon_selector.py"

# 消费面登记 (军规 2026-09-06「prompt 字段消费方登记制」同源): 新增消费方必须显式改这里,
# 改的人被强制先读 HorizonDecision 的量纲声明。
ALLOWED_CONSUMERS = {
    "app/playbook/horizon_selector.py",    # 生成方本体 + 量纲声明 SSOT (声明里点名了落库列名)
    "app/playbook/db.py",                  # 建表 + 写入/更新 (schema 列)
    "app/playbook/instruction_generator.py",  # Instruction dataclass 字段, 默认 0.0
    "app/playbook/pipeline.py",            # 取 decision.confidence 落库; 异常兜底写 0.0
    "app/playbook/playbook_service.py",    # 看板透传 (只呈现, 不判定)
    "app/playbook/shadow_tracker.py",      # DEBUG 日志 (只呈现, 不判定)
}


def _bare_selector() -> HorizonSelector:
    """绕过 __init__ (会连生产 PlaybookDB); 被测方法均不读 self.db。"""
    sel = HorizonSelector.__new__(HorizonSelector)
    sel.config = {}
    return sel


# ══════════════════════ 一、量纲: 是得分差, 不是概率 ══════════════════════

def test_confidence_is_score_margin_not_probability():
    """confidence 必须恒等于 |short_score - long_score| (得分差定义)。"""
    d = HorizonDecision(horizon="short", short_score=1.3, long_score=-0.7, confidence=2.0)
    assert d.confidence == pytest.approx(abs(d.short_score - d.long_score))


def test_fallback_margin_may_exceed_one_and_is_not_clamped():
    """路径 2 实测能产出 >1 (9/1 落库 2.0 / 9/2 落库 1.5) ⇒ 禁止任何 0-1 截断。

    构造: 财报临近 (short +1.5) + 高波动 (short +1.0) 而 long 侧零加分。
    """
    sel = _bare_selector()
    features = {
        "symbol": "US.TEST", "direction": "BUY", "factors": [], "factor_count": 0,
        "confidence": 0, "price_vs_ma20": 0.0, "ma20": None, "ma50": None,
        "trend_5d": None, "atr_pct": 6.0, "volume_ratio": 0.5, "rsi_14": 50,
        "macd_signal": None, "vix": 26.0, "market_regime": "CAUTIOUS",
        "spy_trend": "neutral", "earnings_days": 2,
    }
    d = sel._fallback_score(features)

    assert d.confidence == pytest.approx(abs(d.short_score - d.long_score))
    assert d.confidence > 1.0, (
        "fallback 得分差本就无上界; 若某天开始恒 <=1, 说明有人把它偷偷改成了概率口径 "
        "—— 那是改判据, 必须先落档 ADR/DEB 并同步 HorizonDecision 的量纲声明"
    )
    assert d.horizon in ("short", "long")


def test_confidence_near_tie_is_tiny_but_legal():
    """路径 1 近平局 → 1e-5 量级是**合法**读数, 不是缺测/退化, 不得被当成 0 或当成低置信。"""
    # 取值照抄 2026-09-08 US.SNDK 落库实值 (horizon_confidence=0.000136)
    short_score, long_score = 0.700136, 0.700000
    d = HorizonDecision(horizon="short", short_score=short_score, long_score=long_score,
                        confidence=round(abs(short_score - long_score), 6))
    assert d.confidence == pytest.approx(0.000136)
    assert 0 < d.confidence < 1e-3, "近平局得分差是合法读数, 不是缺测/退化"


def test_disabled_path_hardcodes_one_and_is_a_different_basis():
    """路径 3: horizon_strategy 禁用时 confidence 恒 1.0 (与得分差不同基, 不可与路径1/2 混算)。"""
    sel = _bare_selector()
    sel.config = {"horizon_selector": {"enabled": False}}

    d = sel.select({"symbol": "US.TEST", "direction": "BUY"}, None)

    assert d.horizon == "short"
    assert d.confidence == 1.0
    assert d.reasoning.get("source") == "disabled"


# ══════════════════ 二、C1 冻结安全: horizon 选择不读本字段 ══════════════════

def test_choose_does_not_take_confidence():
    """_choose 的入参里没有 confidence ⇒ 本字段量级变化不可能改变 horizon 选择。"""
    params = list(inspect.signature(HorizonSelector._choose).parameters)
    assert params == ["short_score", "long_score", "margin"], params


def test_choose_body_never_reads_confidence():
    """钉子到源码层: _choose 函数体内不得出现 confidence (防将来顺手加个置信度门)。"""
    src = inspect.getsource(HorizonSelector._choose)
    assert "confidence" not in src


def test_fallback_decision_rule_is_ratio_based_not_confidence_based():
    """fallback 的判定是 long_score > short_score * 1.1 (比值), 与 confidence 无关。"""
    src = SELECTOR_SRC.read_text(encoding="utf-8")
    body = src.split("def _fallback_score", 1)[1].split("\n    def ", 1)[0]
    assert "long_score > short_score * 1.1" in body
    assert not re.search(r"if\s+confidence|confidence\s*[<>]", body), (
        "fallback 判定链里出现了 confidence 比较 ⇒ horizon 选择开始依赖一个异基字段, "
        "会间接改退出参数 (违反 OKR-2026-09 C1 冻结), 必须先落档再动"
    )


# ══════════════════════ 三、消费面登记 + 阈值禁令 ══════════════════════

# 一次性 DEB 审计家族 (`deb<YYYYMMDD>_*.py`) **不参与**生产消费方登记。
# 理由 (2026-10-02, R3 提交 deb20261002_da_ue_{discrim,preflight}.py 时漏跑全量 ⇒ 本守卫红了一轮):
#   ① 这类脚本**只读归因**, 不喂任何门/不改任何行为 ⇒ 不是「消费方」, 登记进白名单是语义错配;
#   ② 它们**寿命有限**(DEB 裁决后按纪律删除), 登记进 ALLOWED_CONSUMERS 会触发本用例「少下去」侧
#      的腐烂红 —— 白名单跟着一次性脚本生死, 等于把临时产物钉进长期契约;
#   ③ 本文件的 `test_no_numeric_threshold_on_horizon_confidence_anywhere` **只扫 ALLOWED_CONSUMERS**,
#      而审计脚本合法地要对该字段做**数值分桶**(算判别力, 只读分析非门控) ⇒ 登记会把分析口径判成违规。
# 代价 (明写, 不留隐患): 审计脚本误读量纲的风险改由 DEB 纪律覆盖 —— 参见
#   skill `data-claim-verification` 规则 61 + 该脚本头部 v1 自曝 (按字段名猜分桶阈值, 实际是 0-1 小数)。
# 排除范围已核: `app/` 下零同名文件; `scripts/` 下引用该字段的**只有** DEB 家族两个脚本 ⇒ 无误伤。
_AUDIT_FAMILY = re.compile(r"^deb\d{8}_.*\.py$")


def _referencing_files() -> set:
    hits = set()
    for root in ("app", "scripts"):
        for p in (PROJECT_ROOT / root).rglob("*.py"):
            if "__pycache__" in p.parts or "archive" in p.parts:
                continue
            if _AUDIT_FAMILY.match(p.name):
                continue
            try:
                if "horizon_confidence" in p.read_text(encoding="utf-8", errors="ignore"):
                    hits.add(str(p.relative_to(PROJECT_ROOT)))
            except OSError:
                continue
    main_py = PROJECT_ROOT / "main.py"
    if main_py.exists() and "horizon_confidence" in main_py.read_text(encoding="utf-8", errors="ignore"):
        hits.add("main.py")
    return hits


def test_consumer_whitelist_is_closed():
    """消费面登记制: 引用方集合必须与白名单逐字相等。

    多出来 = 有人新接了消费方却没读量纲声明 (异基字段最危险的接入方式);
    少下去 = 消费方被删/改名, 白名单跟着腐烂 (静默失效比崩溃更难发现)。
    """
    assert _referencing_files() == ALLOWED_CONSUMERS


def test_no_numeric_threshold_on_horizon_confidence_anywhere():
    """全仓禁止对 horizon_confidence 做数值比较 (>0.3 之类) —— 异基字段设绝对阈值必然误判。

    只扫消费方文件, 允许 `is None` / `is not None` 与赋值/传参。
    """
    bad = []
    for rel in sorted(ALLOWED_CONSUMERS):
        text = (PROJECT_ROOT / rel).read_text(encoding="utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), 1):
            if "horizon_confidence" not in line:
                continue
            if re.search(r"horizon_confidence\D{0,12}[<>]=?\s*-?\d", line):
                bad.append(f"{rel}:{lineno}: {line.strip()}")
    assert not bad, "发现对 horizon_confidence 的数值阈值判定:\n" + "\n".join(bad)


# ══════════════════════ 四、量纲声明不得被顺手删掉 ══════════════════════

def test_ssot_unit_declaration_present_in_horizon_decision():
    """军规「新增落库字段必带单位/量纲声明」的存量补课钉子: 声明被删即红。"""
    src = SELECTOR_SRC.read_text(encoding="utf-8")
    block = src.split("class HorizonDecision", 1)[1].split("class HorizonSelector", 1)[0]
    for needle in ("量纲声明", "得分差", "不是概率", "禁止跨路径比大小", "_choose", "消费方登记"):
        assert needle in block, f"HorizonDecision 量纲声明缺失关键段: {needle}"


def test_declaration_names_all_four_generators():
    """四条生成路径必须逐条在案 —— 漏一条 = 下一个读数据的人按三条推断量级, 照样误判。"""
    src = SELECTOR_SRC.read_text(encoding="utf-8")
    block = src.split("class HorizonDecision", 1)[1].split("class HorizonSelector", 1)[0]
    for needle in ("_score_with_model", "_fallback_score", "禁用时硬编码", "pipeline.py"):
        assert needle in block, f"量纲声明漏登生成路径: {needle}"


def test_declaration_is_a_comment_not_runtime_code():
    """声明必须是注释 (零行为变化): 块内不得引入可执行语句。"""
    src = SELECTOR_SRC.read_text(encoding="utf-8")
    block = src.split("class HorizonDecision", 1)[1].split("class HorizonSelector", 1)[0]
    docstrings_and_fields = [
        ln for ln in block.splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    # 只允许 docstring + 6 个 dataclass 字段声明 (+ 可能的 field(default_factory=...))
    assert len(docstrings_and_fields) <= 9, docstrings_and_fields
    assert not any(ln.strip().startswith(("import ", "from ", "def ", "if ", "for "))
                   for ln in docstrings_and_fields)
