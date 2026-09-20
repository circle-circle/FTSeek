# -*- coding: utf-8 -*-
"""`scripts/meta_audit_loop.py` 契约钉 (2026-09-20, RFC-091 试点第 1 个 loop).

钉的是 Loop Engineering 里最容易退化成"仪式"的四条, 每条都对应一个具体失败模式:
  1. **不自动关闭**: 源产物不可用/不新鲜时, 消失的 finding 只能落 UNVERIFIABLE —
     否则"探测器坏了"会被读成"问题修好了"(= 把尺子坏掉当成零违反)。
  2. **关闭必须有源侧回读证据**: CLOSED 项 close_evidence 非空, 且是"当日产物不再报该项"。
  3. **探测器缺失/产物缺失必须产 finding**: 严禁静默返回空清单 (元环规则 5)。
  4. **静默契约只认状态变化**: 摘要正文含时间戳 ⇒ 每轮 hash 都变 ⇒ 每轮都推 = 契约形同虚设。
另钉两条物理边界: L1 只读 (只写自己的 loop 目录) + 不引入交易侧依赖 (不 import app/futu/sqlite3)。
"""
import importlib.util
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "meta_audit_loop.py"
ENV_KEY = "FTSEEK_REPO"

GATE_DOC_WITH_DEAD = {
    "generated_at_et": "2026-09-20T00:00:00-04:00",
    "gates": {
        "ok_gate": {"status": "ok", "detail": "fine"},
        "dead_gate": {"status": "dead", "detail": "17 交易日零命中"},
    },
}
GATE_DOC_CLEAN = {"generated_at_et": "2026-09-20T00:00:00-04:00",
                  "gates": {"ok_gate": {"status": "ok", "detail": "fine"}}}


@pytest.fixture
def repo(tmp_path):
    """临时仓: data/ 下放四个源的产物, 其余缺席 (缺席源自身会产 detector finding)。"""
    (tmp_path / "data").mkdir(parents=True, exist_ok=True)
    (tmp_path / "scripts").mkdir(parents=True, exist_ok=True)
    old = os.environ.get(ENV_KEY)
    os.environ[ENV_KEY] = str(tmp_path)
    yield tmp_path
    if old is None:
        os.environ.pop(ENV_KEY, None)
    else:
        os.environ[ENV_KEY] = old


def load_mod():
    """每次按当前 FTSEEK_REPO 重新加载 (模块级 BASE 在 import 时取自环境)。"""
    spec = importlib.util.spec_from_file_location("meta_audit_loop_under_test", SCRIPT)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def put(repo, rel, doc, age_h=0.0):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    if age_h:
        ts = (datetime.now() - timedelta(hours=age_h)).timestamp()
        os.utime(str(p), (ts, ts))
    return p


def seed_fresh_sources(repo, gate_doc=GATE_DOC_WITH_DEAD, consumer=True):
    put(repo, "data/gate_liveness_latest.json", gate_doc)
    put(repo, "data/watchdog_liveness_latest.json", {"alarms": []})
    put(repo, "data/invariant_check_latest.json", {"violations": {}, "examples": {}})
    put(repo, "data/config_key_reverse_census_latest.json",
        {"undeclared_literal": [], "undeclared_second_surface": []})
    # 反向配置普查是**按需源**: 判定由这个测试文件承载 (消费者), 不是周期传感器
    stub = repo / "tests" / "test_config_key_reverse_census.py"
    stub.parent.mkdir(parents=True, exist_ok=True)
    if consumer:
        stub.write_text("# consumer stub\n", encoding="utf-8")
    elif stub.exists():
        stub.unlink()


def run(repo, *args, capsys=None):
    mod = load_mod()
    rc = mod.main(list(args))
    out = capsys.readouterr().out if capsys else ""
    return rc, out, mod


def findings(repo):
    return json.loads((repo / "data/loops/meta_audit/findings.json").read_text(encoding="utf-8"))


def state_md(repo):
    return (repo / "data/loops/meta_audit/STATE.md").read_text(encoding="utf-8")


# ── 1/2: 生命周期与回读证据 ───────────────────────────────────────────────

def test_dead_gate_opens_then_closes_only_with_source_readback(repo, capsys):
    seed_fresh_sources(repo)
    rc, out, _ = run(repo, "--force-print", capsys=capsys)
    assert rc == 0
    key = "gate_liveness:dead_gate"
    assert findings(repo)["findings"][key]["status"] == "OPEN"
    assert "🆕 %s" % key in out

    # 源侧回读: 当日产物不再报该项 ⇒ 允许关闭, 且必须带证据
    put(repo, "data/gate_liveness_latest.json", GATE_DOC_CLEAN)
    rc, out, _ = run(repo, "--force-print", capsys=capsys)
    f = findings(repo)["findings"][key]
    assert f["status"] == "CLOSED"
    assert f["closed_at"]
    assert "源侧回读" in f["close_evidence"]
    assert "✅ 关闭 %s" % key in out


def test_finding_absent_from_stale_source_is_never_autoclosed(repo, capsys):
    """核心反退化钉: 产物停更 + 该项消失 ⇒ UNVERIFIABLE, 绝不 CLOSED。"""
    seed_fresh_sources(repo)
    run(repo, capsys=capsys)
    key = "gate_liveness:dead_gate"
    assert findings(repo)["findings"][key]["status"] == "OPEN"

    put(repo, "data/gate_liveness_latest.json", GATE_DOC_CLEAN, age_h=240)  # 停更 10 天
    run(repo, capsys=capsys)
    f = findings(repo)["findings"][key]
    assert f["status"] == "UNVERIFIABLE"
    assert not f.get("closed_at") and not f.get("close_evidence")
    assert "detector:gate_liveness" in findings(repo)["findings"]
    assert "UNVERIFIABLE" in state_md(repo)
    # 判据标签与取值不得错位 (首版 ②/③ 配错值 = 说反)
    assert "②无逾期未裁=False" in state_md(repo) or "②无逾期未裁=True" in state_md(repo)


def test_recurrence_reopens_finding(repo, capsys):
    seed_fresh_sources(repo)
    run(repo, capsys=capsys)
    put(repo, "data/gate_liveness_latest.json", GATE_DOC_CLEAN)
    run(repo, capsys=capsys)
    key = "gate_liveness:dead_gate"
    assert findings(repo)["findings"][key]["status"] == "CLOSED"

    put(repo, "data/gate_liveness_latest.json", GATE_DOC_WITH_DEAD)
    _, out, _ = run(repo, "--force-print", capsys=capsys)
    f = findings(repo)["findings"][key]
    assert f["status"] == "OPEN"
    assert f["closed_at"] is None and f["close_evidence"] is None
    assert "🆕 %s" % key in out
    hist = (repo / "data/loops/meta_audit/findings_history.jsonl").read_text(encoding="utf-8")
    assert '"REOPEN"' in hist and '"CLOSED"' in hist and '"OPEN"' in hist


# ── 3: 探测器缺失不得静默 ─────────────────────────────────────────────────

def test_missing_source_produces_finding_not_silence(repo, capsys):
    (repo / "data").mkdir(parents=True, exist_ok=True)
    rc, out, _ = run(repo, "--force-print", capsys=capsys)
    assert rc == 0
    fs = findings(repo)["findings"]
    for src in ("gate_liveness", "watchdog", "invariant", "config_census"):
        assert "detector:%s" % src in fs, "缺失源 %s 未产 finding = 静默失明" % src
    assert "LOOP_DONE=False" in out


def test_source_doc_unparsable_produces_finding(repo, capsys):
    (repo / "data").mkdir(parents=True, exist_ok=True)
    (repo / "data/gate_liveness_latest.json").write_text("{not json", encoding="utf-8")
    rc, out, _ = run(repo, "--force-print", capsys=capsys)
    assert rc == 0
    assert "detector:gate_liveness" in findings(repo)["findings"]
    assert "不可解析" in findings(repo)["findings"]["detector:gate_liveness"]["title"]


# ── 4: 静默契约 ───────────────────────────────────────────────────────────

def test_second_run_is_silent_and_third_too(repo, capsys):
    seed_fresh_sources(repo)
    run(repo, "--force-print", capsys=capsys)
    _, out2, _ = run(repo, capsys=capsys)
    _, out3, _ = run(repo, capsys=capsys)
    assert out2 == "" and out3 == "", "无状态变化必须静默 (时间戳不得进摘要 hash)"


def test_state_change_breaks_silence(repo, capsys):
    seed_fresh_sources(repo)
    run(repo, capsys=capsys)
    put(repo, "data/watchdog_liveness_latest.json",
        {"alarms": [{"kind": "D1", "key": "D1:new_table:never_fed",
                     "detail": "写入方零行", "action": "修接线"}]})
    _, out, _ = run(repo, capsys=capsys)
    assert out and "watchdog:D1:new_table:never_fed" in out


# ── 逾期: 可见 + 不自动动作 ───────────────────────────────────────────────

def test_overdue_is_surfaced_but_never_acted_on(repo, capsys):
    seed_fresh_sources(repo)
    run(repo, capsys=capsys)
    p = repo / "data/loops/meta_audit/findings.json"
    doc = json.loads(p.read_text(encoding="utf-8"))
    doc["findings"]["gate_liveness:dead_gate"]["decide_by"] = "2026-01-01"
    doc["findings"]["gate_liveness:dead_gate"]["owner"] = "用户(裁决)"
    p.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")

    _, out, _ = run(repo, capsys=capsys)
    assert "⏰ 逾期未裁 gate_liveness:dead_gate" in out
    f = findings(repo)["findings"]["gate_liveness:dead_gate"]
    assert f["status"] == "OPEN" and f["owner"] == "用户(裁决)"
    assert "⏰逾期" in state_md(repo)
    assert "LOOP_DONE=False" in out


# ── 停止条件自身可复算 ────────────────────────────────────────────────────

def test_stop_condition_all_four_true_when_clean(repo, capsys):
    seed_fresh_sources(repo, gate_doc=GATE_DOC_CLEAN)
    _, out, _ = run(repo, "--force-print", capsys=capsys)
    assert "LOOP_DONE=True" in out
    assert "①owner+decide_by 齐备=True ②无逾期未裁=True ③CLOSED 全带回读证据=True ④探测器产物全新鲜=True" in out


# ── 物理边界: L1 只读 + 无交易侧依赖 ─────────────────────────────────────

def test_writes_only_inside_own_loop_dir(repo, capsys):
    seed_fresh_sources(repo)
    before = {str(p): p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    run(repo, "--force-print", capsys=capsys)
    after = {str(p): p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    loop_dir = str(repo / "data/loops/meta_audit")
    touched = {k for k in after if k not in before or after[k] != before.get(k)}
    assert touched, "本环应当写自己的 STATE/findings"
    assert all(t.startswith(loop_dir) for t in touched), "越界写入: %s" % sorted(
        t for t in touched if not t.startswith(loop_dir))


def test_no_trading_side_imports():
    src = SCRIPT.read_text(encoding="utf-8")
    for banned in ("import futu", "from futu", "import sqlite3", "from app", "import app."):
        assert banned not in src, "L1 只读环不得引入交易侧依赖: %s" % banned

# ── 按需源语义 (2026-09-20 用户令「如果无用就退役」实测后新增) ──────────────
# 判据: 反向配置普查无周期载体, 但判定**有活消费者** (回归测试) ⇒ 判定不退役,
# 退役的是"把它当周期传感器"这个错误假设: 产物陈旧 ≠ 失明; 只有消费者消失才算失明。

def test_on_demand_source_stale_artifact_is_not_a_sensor_failure(repo, capsys):
    seed_fresh_sources(repo)
    put(repo, "data/config_key_reverse_census_latest.json",
        {"undeclared_literal": ["some.new.key"], "undeclared_second_surface": []}, age_h=600)
    _, out, _ = run(repo, "--force-print", capsys=capsys)
    fs = findings(repo)["findings"]
    assert "detector:config_census" not in fs, "按需源的产物陈旧被误判成探测器失明"
    assert "config_census:some.new.key" not in fs, "22 天前的读点不得当 live finding 报出"
    assert "④探测器产物全新鲜=True" in out, "按需源消费者在册 ⇒ 该条判据不应为假"


def test_on_demand_source_lost_consumer_is_a_finding(repo, capsys):
    seed_fresh_sources(repo, consumer=False)
    _, out, _ = run(repo, "--force-print", capsys=capsys)
    fs = findings(repo)["findings"]
    assert "detector:config_census" in fs, "消费者(判定载体)消失必须报失明"
    assert "消费者消失" in fs["detector:config_census"]["title"]
    assert "④探测器产物全新鲜=False" in out


def test_ondemand_detector_finding_closes_when_consumer_alive(repo, capsys):
    """回归钉: 首版只认"产物恢复新鲜"才能关 detector:* ⇒ 按需源的 detector finding 永久 OPEN
    (一个永不收敛的 finding)。消费者复活必须能关闭它, 且关闭后不得自行复发。"""
    seed_fresh_sources(repo, consumer=False)
    run(repo, capsys=capsys)
    key = "detector:config_census"
    assert findings(repo)["findings"][key]["status"] == "OPEN"

    (repo / "tests" / "test_config_key_reverse_census.py").write_text("# back\n", encoding="utf-8")
    _, out, _ = run(repo, "--force-print", capsys=capsys)
    f = findings(repo)["findings"][key]
    assert f["status"] == "CLOSED", "消费者复活未能关闭该 finding = 永不收敛"
    assert "按需源语义" in f["close_evidence"]
    assert "✅ 关闭 %s" % key in out

    _, out2, _ = run(repo, capsys=capsys)
    assert out2 == ""
    assert findings(repo)["findings"][key]["status"] == "CLOSED"
