"""生产 data/ 写入隔离钉子 (2026-09-03 复盘批, REVIEW-20260903 §四.8).

钉住三件事不得复发:
  ① 埋点/持久化路径必须有**可重定向的缝** — 原先三处都在函数体内就地拼字符串
     (`_EDP("data/early_dead_signal_shadow.jsonl")` / 两处 `_Pab(__file__)…/vwap_gate_ab.jsonl`
     / `audit._default_audit_path()` 无人接管), 测试无从隔离。
  ② `tests/conftest.py` 自动把已登记缝指向 tmp 沙箱, 并对未登记的 data/ 写入硬闸报错。
  ③ 生产判分样本与审计留痕载体不得含测试伪造行 (棘轮) — 实测曾累积
     2307 行 `symbol=TEST` (占死单影子 90%, shadow_board 段10 分母虚增 44×)、
     363 行虚构审计 (100%, 全是对 tmp 库的 void 留痕)、40 个合成推荐 JSON
     (payload 恒 AAPL/180.00-185.00), 跨 8/11→9/3 共 23 天无人发现。

清除物隔离留痕于 `data/_quarantine/20260903_test_pollution/` (含 README 与 .orig 副本)。
回滚: 删本文件 + revert 对应提交即可 — 缝常量的值与解析语义逐字未变, 生产行为零变更。
"""
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

DATA = REPO / "data"
_SANDBOX_MARKER = "production_data_sandbox"


# ── ① 缝必须存在 (源码契约: 防止有人把路径又拼回函数体内) ──────────────

def test_pipeline_shadow_paths_are_module_seams():
    src = (REPO / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    assert "_VWAP_AB_LEDGER_PATH = Path(__file__).resolve().parents[2]" in src
    assert 'open(_VWAP_AB_LEDGER_PATH, "a", encoding="utf-8")' in src
    # 就地拼接的旧写法不得回来 (它们正是测试无法隔离的根因)
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert body.count('"data" / "vwap_gate_ab.jsonl"') == 1, (
        "台账路径只应在模块常量处出现一次 (原两处就地拼接)")


def test_exit_engine_shadow_path_is_module_seam():
    """P2a (2026-09-06): 死单影子缝随 _check_playbook_exit_rules 迁入 exit_engine."""
    src = (REPO / "app" / "playbook" / "exit_engine.py").read_text(encoding="utf-8")
    assert '_EARLY_DEAD_SHADOW_PATH = Path("data/early_dead_signal_shadow.jsonl")' in src, (
        "死单影子埋点路径必须是模块常量 (值=原相对路径, 解析语义不变)")
    assert 'open(_EARLY_DEAD_SHADOW_PATH, "a")' in src
    body = "\n".join(l for l in src.splitlines() if not l.lstrip().startswith("#"))
    assert '_EDP("data/early_dead_signal_shadow.jsonl")' not in body


def test_seams_are_redirected_away_from_production_data():
    import app.playbook.exit_engine as exit_engine
    import app.playbook.pipeline as pipeline
    import app.playbook.audit as audit

    for val in (exit_engine._EARLY_DEAD_SHADOW_PATH, pipeline._VWAP_AB_LEDGER_PATH,
                audit._default_audit_path()):
        resolved = str(Path(val).resolve())
        assert not resolved.startswith(str(DATA) + "/"), (
            f"缝未被 conftest 重定向到 tmp 沙箱, 仍指向生产 data/: {resolved}")
        assert _SANDBOX_MARKER in resolved, f"应落在会话级沙箱内: {resolved}"


# ── ② 硬闸: 未登记的 data/ 写入当场红 ─────────────────────────────────

def test_hard_guard_blocks_unregistered_open_write():
    with pytest.raises(RuntimeError, match="污染尺子"):
        open(DATA / "_guard_probe_should_never_exist.jsonl", "a", encoding="utf-8")
    assert not (DATA / "_guard_probe_should_never_exist.jsonl").exists()


def test_hard_guard_blocks_path_write_text_and_bytes():
    probe = DATA / "_guard_probe_write_text.tmp"
    with pytest.raises(RuntimeError, match="_PRODUCTION_DATA_SEAMS"):
        probe.write_text("x", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Path.write_bytes"):
        probe.write_bytes(b"x")
    assert not probe.exists()


def test_hard_guard_allows_reads_tmp_writes_and_relative_outside_data(tmp_path):
    # 读生产数据放行 (判分/对账测试大量需要)
    if (DATA / "early_dead_signal_shadow.jsonl").exists():
        (DATA / "early_dead_signal_shadow.jsonl").read_text(encoding="utf-8")
    # 写 tmp 放行
    (tmp_path / "ok.jsonl").write_text("{}", encoding="utf-8")
    with open(tmp_path / "ok2.jsonl", "w", encoding="utf-8") as fh:
        fh.write("{}")
    # 沙箱内 (缝重定向后) 放行
    import app.playbook.exit_engine as exit_engine
    seam = Path(exit_engine._EARLY_DEAD_SHADOW_PATH)
    seam.parent.mkdir(parents=True, exist_ok=True)
    with open(seam, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"symbol": "US.GUARDPROBE"}) + "\n")
    assert json.loads(seam.read_text(encoding="utf-8").strip().splitlines()[-1])[
        "symbol"] == "US.GUARDPROBE"


@pytest.mark.allow_production_data_write
def test_opt_out_marker_disables_guard_for_declared_tests():
    """豁免阀必须可用 (未被测试的豁免阀等于没有 — 将来会被"直接改弱硬闸"取代)。

    本例即豁免阀的唯一合法用法样板: 写真路径 + try/finally 自清 + 注明理由。
    """
    probe = DATA / "_guard_optout_probe.tmp"
    try:
        probe.write_text("opt-out 生效\n", encoding="utf-8")
        assert probe.read_text(encoding="utf-8").startswith("opt-out")
    finally:
        probe.unlink(missing_ok=True)
    assert not probe.exists()


# ── ③ 棘轮: 生产载体不得含测试伪造行 ──────────────────────────────────

_TEST_SYMBOLS = {"TEST", "US.TEST", "US.LOSS", "US.AUDIT", "ZZTEST_VWAPAB"}


def test_early_dead_shadow_has_no_fixture_rows():
    path = DATA / "early_dead_signal_shadow.jsonl"
    if not path.exists():
        return
    bad = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        sym = str(json.loads(line).get("symbol"))
        if sym in _TEST_SYMBOLS or not sym.startswith("US."):
            bad.append(sym)
    assert not bad, (
        f"死单早期影子 (DEB-20260811 预注册裁决样本) 含 {len(bad)} 行测试伪造: "
        f"{sorted(set(bad))[:5]} — 隔离区 data/_quarantine/20260903_test_pollution/")


def test_audit_trail_has_no_fixture_rows():
    path = DATA / "audit" / "playbook_reconciliation.jsonl"
    if not path.exists():
        return
    bad = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        blob = json.dumps(row, ensure_ascii=False)
        if any(sym in blob for sym in _TEST_SYMBOLS):
            bad += 1
    assert bad == 0, (
        f"审计留痕含 {bad} 行测试虚构 void 记录 (曾有 363 行 = 100%); "
        f"审计文件的意义就是只记真发生过的事")


def test_recommendations_have_no_synthetic_fixture_payload():
    synth = []
    for path in sorted((DATA / "recommendations").glob("daily_*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        recs = payload.get("recommendations") or []
        # tests/test_recommendations.py 的合成 payload 指纹 (曾有 40 个文件, 跨 6/20→9/3)
        if (len(recs) == 1 and recs[0].get("symbol") == "AAPL"
                and recs[0].get("entry_zone") == "180.00-185.00"):
            synth.append(path.name)
    assert not synth, (
        f"生产推荐目录含 {len(synth)} 个测试合成文件 (虚构 AAPL 180-185 建议): {synth[:5]}")
