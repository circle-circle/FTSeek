"""RFC-218 §28 挑战者事实一致性契约测试 (DA_CHALLENGER_FACT_CONSISTENCY 独立开关).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护, 不让 CI 因外部依赖缺失而红.

病灶 (§15.7): `_challenger_data_view` 的 `_FULL` 白名单含「资金面」却不含「数据源状态」
→ 「## 数据源状态」段被丢弃, 挑战者看不到源 FAILED/EMPTY/RENDER_DROPPED 状态, 与分析师
信息不对称: 无法核验分析师弃权(SKIP)是否因缺材料, 无法据源失败反证"没有催化剂"类论断.
修复以独立开关 DA_CHALLENGER_FACT_CONSISTENCY 控制 (纯呈现层可见性, 不动采集/计算):
  =0(默认): 逐字节旧挑战者视图 (丢弃「## 数据源状态」, 当前生产行为不变);
  =1: 白名单补「数据源状态」, 挑战者与分析师同见该段 (共享同一 data_text, 同事件版本/
      同时点价格); 仅放行该段, _TRUNC/其余白名单/回退[:4000] 逐字节不动.

覆盖点:
  1. OFF 逐字节 == 改前备份 (合成视图网格 + 67 份真实冻结 checkpoint)
  2. ON 只增「## 数据源状态」段, 其余段落逐字节不动 (隔离: ON 去该段 == OFF)
  3. ON 该段文本 == 分析师 prompt 中该段文本 (两角色同事件版本/同时点价格的一致性)
  4. _TRUNC(新闻/研报/社区) 仍截断 600+标记; 未识别段仍丢弃; 回退[:4000] 两态一致
  5. DA_CHALLENGER_FULL_DATA=0 → [:4000] 旧截断, 与开关正交
  6. 与资金/短K/财报三态/源状态(§27)/DA_INPUT_FULL 开关互不牵连
  7. 回滚态: 删除开关属性 → getattr 默认 False → 旧行为

注意: 本测试只验呈现层可见性正确, 不证收益. 激活正式输入须按 RFC-218 §10.4 单变量 A/B
(n≥120 且 ≥20 独立 ET 日, 预注册 MDE), 不得以本测试代替经济裁决.
"""
import importlib.machinery
import importlib.util
import json
import re
import sys
from pathlib import Path

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)
try:
    import llm_synth as L
    import config as C
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    L = None
    _IMPORT_ERR = str(_e)

pytestmark = pytest.mark.skipif(L is None, reason="deep-stock-analysis skill 不可用")

_BAK_PATH = Path(_SKILL) / "llm_synth.py.bak-20261128-rfc218-challenger"
_CKPT_DIRS = [Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-25"),
              Path("/opt/ftseek/FTSeek-US/data/da_checkpoint/2026-11-27")]

_OTHER_FLAGS = ("DA_SOURCE_STATUS_FULL", "DA_CAPITAL_CODE_FIX", "DA_SHORTK_NA",
                "DA_EARNINGS_TRISTATE", "DA_INPUT_FULL")


def _load_bak():
    loader = importlib.machinery.SourceFileLoader("llm_synth_s28_bak", str(_BAK_PATH))
    spec = importlib.util.spec_from_loader("llm_synth_s28_bak", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def _view(text, flag, full_data_env="1", monkeypatch=None):
    """调用被测 _challenger_data_view, 临时设开关与 DA_CHALLENGER_FULL_DATA env, 用后还原."""
    import os
    old_flag = getattr(C, "DA_CHALLENGER_FACT_CONSISTENCY", None)
    old_env = os.environ.get("DA_CHALLENGER_FULL_DATA")
    C.DA_CHALLENGER_FACT_CONSISTENCY = flag
    os.environ["DA_CHALLENGER_FULL_DATA"] = full_data_env
    try:
        return L._challenger_data_view(text)
    finally:
        if old_flag is None:
            if hasattr(C, "DA_CHALLENGER_FACT_CONSISTENCY"):
                delattr(C, "DA_CHALLENGER_FACT_CONSISTENCY")
        else:
            C.DA_CHALLENGER_FACT_CONSISTENCY = old_flag
        if old_env is None:
            os.environ.pop("DA_CHALLENGER_FULL_DATA", None)
        else:
            os.environ["DA_CHALLENGER_FULL_DATA"] = old_env


def _parts(text):
    return re.split(r'(?=^## )', text, flags=re.M)


def _status_part(text):
    for p in _parts(text):
        if p.lstrip().startswith("## 数据源状态"):
            return p
    return ""


def _mk_text(status=True, long_trunc=True, extra_unknown=True):
    """合成多段 prompt: 含数据源状态/技术面/资金面/质量审计/覆盖统计 + 三截断段 + 未识别段."""
    news_body = ("新闻标题事实流数字 " * 80) if long_trunc else "短新闻"
    res_body = ("研报摘要内容数字 " * 80) if long_trunc else "短研报"
    sent_body = ("社区讨论帖子内容 " * 80) if long_trunc else "短社区"
    secs = ["## 数据覆盖统计: 19/19 维度有效\n- score_band 锚点③【数据充分性】: 充分 ✓\n"]
    if status:
        secs.append("## 数据源状态\n"
                    "- Futu新闻: FAILED(采集失败; 不等于无新闻/无情绪, 禁止写成'没有催化剂')\n"
                    "- 研报: EMPTY(源成功但无结果)\n"
                    "- 资金分布: RENDER_DROPPED(采集有数据但正式块未呈现)\n")
    secs.append("## 技术面\nMA5: 100.00 MA20: 99.50 chg5: +1.2%\n")
    secs.append("## 资金面\n主力净流入: +1.2亿\n")
    secs.append("## 数据质量审计\n完整19/降级0/缺失0\n")
    secs.append("## 最新新闻\n" + news_body + "\n")
    secs.append("## 研报\n" + res_body + "\n")
    secs.append("## 社区讨论\n" + sent_body + "\n")
    if extra_unknown:
        secs.append("## 杂项未识别段\n应当被丢弃\n")
    return "".join(secs)


def _real_collected():
    out = []
    for d in _CKPT_DIRS:
        if not d.is_dir():
            continue
        for p in sorted(d.glob("US.*.raw.json")):
            try:
                col = json.load(open(p, encoding="utf-8")).get("collected")
            except Exception:
                continue
            if isinstance(col, dict):
                out.append((p.name, col))
    return out


# ── 1. 开关形态 + 默认 OFF ─────────────────────────────────────────

def test_flag_is_bool():
    assert isinstance(getattr(C, "DA_CHALLENGER_FACT_CONSISTENCY", None), bool)


def test_flag_default_off_fresh_import(monkeypatch):
    monkeypatch.delenv("DA_CHALLENGER_FACT_CONSISTENCY", raising=False)
    loader = importlib.machinery.SourceFileLoader(
        "config_s28_fresh", str(Path(_SKILL) / "config.py"))
    spec = importlib.util.spec_from_loader("config_s28_fresh", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.DA_CHALLENGER_FACT_CONSISTENCY is False


def test_flag_on_via_env(monkeypatch):
    monkeypatch.setenv("DA_CHALLENGER_FACT_CONSISTENCY", "1")
    loader = importlib.machinery.SourceFileLoader(
        "config_s28_on", str(Path(_SKILL) / "config.py"))
    spec = importlib.util.spec_from_loader("config_s28_on", loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    assert mod.DA_CHALLENGER_FACT_CONSISTENCY is True


# ── 2. OFF 逐字节 == 备份 (合成网格) ───────────────────────────────

@pytest.mark.parametrize("status", [True, False])
@pytest.mark.parametrize("long_trunc", [True, False])
@pytest.mark.parametrize("extra_unknown", [True, False])
def test_off_byte_identical_to_backup_synthetic(status, long_trunc, extra_unknown):
    bak = _load_bak()
    text = _mk_text(status=status, long_trunc=long_trunc, extra_unknown=extra_unknown)
    import os
    old = os.environ.get("DA_CHALLENGER_FULL_DATA")
    os.environ["DA_CHALLENGER_FULL_DATA"] = "1"
    try:
        expect = bak._challenger_data_view(text)
    finally:
        if old is None:
            os.environ.pop("DA_CHALLENGER_FULL_DATA", None)
        else:
            os.environ["DA_CHALLENGER_FULL_DATA"] = old
    assert _view(text, False) == expect


def test_off_drops_status_section():
    text = _mk_text(status=True)
    assert "## 数据源状态" in text
    assert "## 数据源状态" not in _view(text, False)


def test_off_keeps_other_full_sections():
    text = _mk_text(status=True)
    v = _view(text, False)
    for marker in ("## 技术面", "## 资金面", "## 数据质量审计", "## 数据覆盖统计"):
        assert marker in v


# ── 3. ON 只增数据源状态 (隔离) ────────────────────────────────────

def test_on_retains_status_section():
    text = _mk_text(status=True)
    v = _view(text, True)
    assert "## 数据源状态" in v
    assert "FAILED(采集失败" in v
    assert "RENDER_DROPPED" in v


def test_on_isolation_remove_status_equals_off():
    text = _mk_text(status=True)
    on = _view(text, True)
    off = _view(text, False)
    sp = _status_part(text)
    assert sp and sp in on
    # OFF 可能丢弃该段(无 _TRUNC 关键词碰撞)或截断至600(碰撞); ON 恒全量.
    # 隔离不变量: ON 与 OFF 唯一差异＝数据源状态段 (全量 vs OFF 形态).
    off_sp = _status_part(off)
    assert on.replace(sp, off_sp, 1) == off


def test_off_status_truncated_on_trunc_keyword_collision():
    # 真实病灶精化: 数据源状态段首40字含「研报/社区讨论」(如 "- 研报: FAILED") →
    # 与 _TRUNC 关键词子串碰撞, OFF 不是丢弃而是截断至600+标记; ON 走 _FULL(先判)恒全量.
    text = ("## 数据覆盖统计: 19/19 维度有效\n- 充分 ✓\n"
            "## 数据源状态\n- 研报: FAILED(采集失败; 不等于无新闻/无情绪, 禁止写成'没有催化剂')\n"
            + ("- 社区情绪: EMPTY(源成功但无结果)\n" * 60)
            + "## 技术面\nMA5: 100.0\n")
    off = _view(text, False)
    on = _view(text, True)
    off_sp = _status_part(off)
    on_sp = _status_part(on)
    assert off_sp and "原文截断, 挑战者仅需主旨" in off_sp  # OFF 截断
    assert len(off_sp) <= 600 + len("\n…(原文截断, 挑战者仅需主旨)\n")
    assert on_sp == _status_part(text)  # ON 全量 == 分析师所见
    assert "原文截断" not in on_sp
    assert on.replace(on_sp, off_sp, 1) == off


def test_off_status_dropped_when_no_keyword_collision():
    # 首40字无 _TRUNC 关键词 (Futu新闻 在前) → OFF 整段丢弃; ON 全量.
    text = _mk_text(status=True)
    assert _status_part(_view(text, False)) == ""
    assert _status_part(_view(text, True)) == _status_part(text)


def test_on_status_text_identical_to_analyst_prompt():
    # 两角色共享同一 data_text → ON 挑战者所见数据源状态段 == 分析师 prompt 中该段 (同事件版本/同时点价格)
    text = _mk_text(status=True)
    analyst_sp = _status_part(text)
    challenger_sp = _status_part(_view(text, True))
    assert analyst_sp == challenger_sp


def test_on_absent_status_no_spurious_addition():
    text = _mk_text(status=False)
    assert "## 数据源状态" not in text
    assert _view(text, True) == _view(text, False)


@pytest.mark.parametrize("marker", ["## 技术面", "## 资金面", "## 数据质量审计", "## 数据覆盖统计"])
def test_on_other_full_sections_unchanged(marker):
    text = _mk_text(status=True)
    on = _view(text, True)
    off = _view(text, False)
    # 该段在 ON/OFF 中均全量且逐字一致
    def sec(t):
        for p in _parts(t):
            if p.lstrip().startswith(marker):
                return p
        return ""
    assert sec(on) == sec(off) != ""


@pytest.mark.parametrize("marker", ["## 最新新闻", "## 研报", "## 社区讨论"])
def test_on_trunc_sections_still_truncated(marker):
    text = _mk_text(status=True, long_trunc=True)
    on = _view(text, True)
    off = _view(text, False)
    def sec(t):
        for p in _parts(t):
            if p.lstrip().startswith(marker):
                return p
        return ""
    s_on, s_off = sec(on), sec(off)
    assert s_on == s_off
    assert "原文截断, 挑战者仅需主旨" in s_on
    assert len(s_on) <= 600 + len("\n…(原文截断, 挑战者仅需主旨)\n")


def test_on_unknown_section_still_dropped():
    text = _mk_text(status=True, extra_unknown=True)
    assert "## 杂项未识别段" not in _view(text, True)
    assert "## 杂项未识别段" not in _view(text, False)


# ── 4. 回退 [:4000] / FULL_DATA=0 正交 ─────────────────────────────

def test_full_data_off_fallback_identical_both_states():
    bak = _load_bak()
    text = _mk_text(status=True)
    import os
    old = os.environ.get("DA_CHALLENGER_FULL_DATA")
    os.environ["DA_CHALLENGER_FULL_DATA"] = "0"
    try:
        expect = bak._challenger_data_view(text)
    finally:
        if old is None:
            os.environ.pop("DA_CHALLENGER_FULL_DATA", None)
        else:
            os.environ["DA_CHALLENGER_FULL_DATA"] = old
    assert expect == text[:4000]
    assert _view(text, False, full_data_env="0") == expect
    assert _view(text, True, full_data_env="0") == expect


def test_empty_view_fallback_identical():
    text = "## 杂项未识别段\n仅未识别段, 全丢弃 → 回退[:4000]\n"
    assert _view(text, True) == text[:4000]
    assert _view(text, False) == text[:4000]


def test_exception_path_fallback(monkeypatch):
    # re.split 抛异常 → except 回退 [:4000], 两态一致
    text = _mk_text(status=True)
    import re as _re
    def _boom(*a, **k):
        raise RuntimeError("synthetic split failure")
    monkeypatch.setattr(_re, "split", _boom)
    assert _view(text, True) == text[:4000]
    assert _view(text, False) == text[:4000]


# ── 5. 与其他开关正交 ──────────────────────────────────────────────

@pytest.mark.parametrize("flag_name", _OTHER_FLAGS)
@pytest.mark.parametrize("flag_val", [True, False])
def test_orthogonal_to_other_flags(flag_name, flag_val):
    text = _mk_text(status=True)
    old = getattr(C, flag_name, None)
    base_off = _view(text, False)
    base_on = _view(text, True)
    setattr(C, flag_name, flag_val)
    try:
        assert _view(text, False) == base_off
        assert _view(text, True) == base_on
    finally:
        if old is None:
            if hasattr(C, flag_name):
                delattr(C, flag_name)
        else:
            setattr(C, flag_name, old)


# ── 6. 回滚态: 删属性 → getattr 默认 False ─────────────────────────

def test_rollback_missing_attr_defaults_off():
    text = _mk_text(status=True)
    old = getattr(C, "DA_CHALLENGER_FACT_CONSISTENCY", None)
    if hasattr(C, "DA_CHALLENGER_FACT_CONSISTENCY"):
        delattr(C, "DA_CHALLENGER_FACT_CONSISTENCY")
    try:
        assert _view(text, False) == L._challenger_data_view(text)
        assert "## 数据源状态" not in L._challenger_data_view(text)
    finally:
        if old is not None:
            C.DA_CHALLENGER_FACT_CONSISTENCY = old


# ── 7. 真实冻结 checkpoint (67 份) ─────────────────────────────────

def test_real_checkpoints_present():
    assert len(_real_collected()) >= 60


def test_off_byte_identical_real_checkpoints():
    bak = _load_bak()
    import os
    old = os.environ.get("DA_CHALLENGER_FULL_DATA")
    os.environ["DA_CHALLENGER_FULL_DATA"] = "1"
    n = 0
    try:
        for name, col in _real_collected():
            text = L._format_data_for_prompt(col)
            assert _view(text, False) == bak._challenger_data_view(text), name
            n += 1
    finally:
        if old is None:
            os.environ.pop("DA_CHALLENGER_FULL_DATA", None)
        else:
            os.environ["DA_CHALLENGER_FULL_DATA"] = old
    assert n >= 60


def test_on_isolation_real_checkpoints():
    n = 0
    with_status = 0
    for name, col in _real_collected():
        text = L._format_data_for_prompt(col)
        on = _view(text, True)
        off = _view(text, False)
        sp = _status_part(text)
        if sp:
            with_status += 1
            assert sp in on, name
            # OFF 形态: 丢弃("")或截断(碰撞); ON 恒全量. 唯一差异＝该段.
            off_sp = _status_part(off)
            assert on.replace(sp, off_sp, 1) == off, name
        else:
            assert on == off, name
        n += 1
    assert n >= 60
    # 至少部分真实 checkpoint 含数据源状态段 (4 list 源 FAILED/EMPTY), 证明真实路径被 exercised
    assert with_status >= 1


def test_on_status_matches_analyst_real_checkpoints():
    for name, col in _real_collected():
        text = L._format_data_for_prompt(col)
        sp = _status_part(text)
        if sp:
            assert _status_part(_view(text, True)) == sp, name
