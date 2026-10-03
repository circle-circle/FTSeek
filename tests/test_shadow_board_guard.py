"""shadow_board 注册面 + 生产者停摆守卫契约 (2026-09-26 RFC-109③).

钉三条:
  ① 每段必须带注册面行 (status/n/min_n 取自 shadow_registry SSOT) — 未登记 id
     不得静默, 必须显式告警 (治「看板绕过注册表硬编码」根因);
  ② 生产者停摆 (artifact mtime 超阈) 必须显式可见, 与「正常无触发」可区分;
  ③ 终态/已停采项豁免陈旧警告 (停更属预期, 否则退役项天天误报)。
"""
import importlib.util
import os
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "shadow_board.py"


def _load():
    spec = importlib.util.spec_from_file_location("shadow_board_t", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _FakeItem:
    def __init__(self, iid, status="OBSERVING", n=5, min_n=30,
                 retired=False, noadj=""):
        self.id = iid
        self.status = status
        self.current_n = n
        self.min_n = min_n
        self.collection_retired = retired
        self.no_adjudication_reason = noadj


def test_guard_shows_registry_face_and_missing_registration(monkeypatch):
    m = _load()
    items = {"known": _FakeItem("known", n=12, min_n=30)}
    monkeypatch.setattr(m, "_reg_item", lambda rid: items.get(rid))
    art = m.ROOT / "data" / "shadow" / "sltp_verify_shadow.jsonl"
    lines = m._guard_lines([("known", "data/shadow/sltp_verify_shadow.jsonl", 4),
                            ("ghost", "data/shadow/sltp_verify_shadow.jsonl", 4)])
    assert any("注册[known] OBSERVING n=12/30" in l for l in lines)
    # 未登记 id 必须显式告警, 不得静默
    assert any("注册面缺失[ghost]" in l for l in lines)
    assert art.exists()  # 前置: 产物真实存在 (本测试不造假数据)


def test_guard_flags_stale_producer(monkeypatch, tmp_path):
    m = _load()
    monkeypatch.setattr(m, "_reg_item", lambda rid: _FakeItem(rid))
    monkeypatch.setattr(m, "ROOT", tmp_path)
    p = tmp_path / "data" / "stale.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{}\n")
    old = time.time() - 10 * 86400
    os.utime(p, (old, old))
    lines = m._guard_lines([("x", "data/stale.jsonl", 4)])
    assert any("生产者停摆" in l and "10" in l for l in lines)


def test_guard_flags_missing_artifact(monkeypatch, tmp_path):
    m = _load()
    monkeypatch.setattr(m, "_reg_item", lambda rid: _FakeItem(rid))
    monkeypatch.setattr(m, "ROOT", tmp_path)
    lines = m._guard_lines([("x", "data/nope.jsonl", 4)])
    assert any("生产者缺产物" in l for l in lines)


def test_guard_exempts_terminal_and_retired(monkeypatch, tmp_path):
    """终态 (KILLED/SUPERSEDED/GRADUATED) 与 collection_retired 项停更属预期, 不告警。"""
    m = _load()
    monkeypatch.setattr(m, "ROOT", tmp_path)
    p = tmp_path / "data" / "retired.jsonl"
    p.parent.mkdir(parents=True)
    p.write_text("{}\n")
    old = time.time() - 30 * 86400
    os.utime(p, (old, old))
    for st, retired in (("KILLED", False), ("OBSERVING", True), ("SUPERSEDED", False)):
        monkeypatch.setattr(m, "_reg_item",
                            lambda rid, st=st, r=retired: _FakeItem(rid, status=st, retired=r))
        lines = m._guard_lines([("x", "data/retired.jsonl", 4)])
        assert not any("生产者停摆" in l for l in lines), f"{st}/retired={retired} 不应告警"


def test_every_board_section_is_registered():
    """看板每一段挂的注册 id 必须真实存在于 shadow_registry (治「段绕过 SSOT」)。"""
    m = _load()
    m._init_table()
    sys.path.insert(0, str(ROOT))
    from app.playbook.shadow_registry import REGISTRY
    known = {s.id for s in REGISTRY}
    missing = []
    for _sec, entries in m._SECTION_TABLE:
        for reg_id, _art, _thr in entries:
            if reg_id not in known:
                missing.append(reg_id)
    assert not missing, f"看板段引用了未登记的注册 id: {missing}"


# ── 推送门 (2026-10-03 清理批): 无料不推, 有料必推, 尺子坏了 fail-open ──────────
def test_push_gate_silent_when_nothing_actionable(monkeypatch):
    """无待裁 + 无守卫告警 ⇒ 不推送 (全文仍进日志)。"""
    m = _load()
    import app.review.criteria_status as cs
    monkeypatch.setattr(cs, "pending_adjudications", lambda: [])
    ok, reason = m._board_actionable("🌓 看板正常, 无任何 ⚠️")
    assert ok is False
    assert "无待裁" in reason


def test_push_gate_pushes_on_pending_adjudication(monkeypatch):
    """待裁面非空 ⇒ 必推 (含 id 清单)。"""
    m = _load()
    import app.review.criteria_status as cs
    monkeypatch.setattr(cs, "pending_adjudications", lambda: [
        {"id": "some_gate", "needs_adjudication": True}])
    ok, reason = m._board_actionable("看板正常")
    assert ok is True
    assert "some_gate" in reason


def test_push_gate_pushes_on_guard_marker():
    """守卫告警 (生产者停摆等) 在看板正文 ⇒ 必推, 不依赖待裁面。"""
    m = _load()
    ok, reason = m._board_actionable("某段: ⚠️ 生产者停摆 data/x.jsonl 已 20 天未更新")
    assert ok is True
    assert "生产者停摆" in reason


def test_push_gate_fails_open_when_query_breaks(monkeypatch):
    """待裁面查询异常 ⇒ fail-open 推送 (尺子坏了必须可见, 不许静默)。"""
    m = _load()
    import app.review.criteria_status as cs
    def _boom():
        raise RuntimeError("sensor broken")
    monkeypatch.setattr(cs, "pending_adjudications", _boom)
    ok, reason = m._board_actionable("看板正常")
    assert ok is True
    assert "fail-open" in reason


def test_main_push_suppresses_feishu_when_quiet(monkeypatch, capsys):
    """main(push=True) 静默日: 不调 send_text, stdout 落 [push-gate] 抑制行。"""
    m = _load()
    import app.review.criteria_status as cs
    monkeypatch.setattr(cs, "pending_adjudications", lambda: [])
    sent = []
    monkeypatch.setattr(m, "build_board", lambda: "quiet board")
    class _FakeFeishu:
        @staticmethod
        def send_text(msg):
            sent.append(msg)
    import types
    fake_mod = types.ModuleType("app.notifier.feishu")
    fake_mod.send_text = _FakeFeishu.send_text
    monkeypatch.setitem(__import__("sys").modules, "app.notifier.feishu", fake_mod)
    m.main(push=True)
    out = capsys.readouterr().out
    assert sent == []
    assert "[push-gate]" in out and "不推送" in out
