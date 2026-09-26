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
