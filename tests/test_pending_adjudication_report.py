"""待裁决触发通知 (`scripts/pending_adjudication_report.py`) 契约测试 (2026-09-21).

钉住三件事 (载体静默契约的前提):
  1. 待裁面**无变化 ⇒ 零输出** (否则每周重复推送 = 通知变噪声, 正是本通知要治的病);
  2. **首次运行输出全量** (用户必须先看到整块板, 才知道有哪些试验项在跑);
  3. 白话清单必须含"背景"与"需要你决定"两段 (通俗易懂是需求本身, 不是包装)。

不依赖线上注册表真值: 打桩 `criteria_status.pending_adjudications` 与 STATE 路径。
"""
import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "pending_adjudication_report.py"


def _load():
    spec = importlib.util.spec_from_file_location("pending_adjudication_report", SCRIPT)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def _rows():
    return [
        {   # 需用户裁 (人工分类为 NEEDS_USER)
            "id": "futu_channel_top_movers",
            "status": "READY_TO_EVALUATE",
            "n": 3291, "min_n": 540, "progress": "3291/540", "ready": True,
            "criteria": "IC 显著", "artifact": "data/shadow/top_movers_eval_latest.json",
            "eval_script": "scripts/top_movers_eval.py", "count_mode": None, "evidence": {},
        },
        {   # 自动续观
            "id": "gap_chase_gate",
            "status": "READY_TO_EVALUATE",
            "n": 31, "min_n": 30, "progress": "31/30", "ready": True,
            "criteria": "n≥30 ∧ 笔均<0 ∧ p≤0.05", "artifact": "data/shadow/gap_chase_gate_latest.json",
            "eval_script": "scripts/gap_chase_gate_eval.py", "count_mode": None, "evidence": {},
        },
        {   # 未收录的新条目 ⇒ 必须抬到待裁面, 不许静默吞掉
            "id": "brand_new_shadow",
            "status": "READY_TO_EVALUATE",
            "n": 40, "min_n": 30, "progress": "40/30", "ready": True,
            "criteria": "新判据", "artifact": "data/shadow/nope_latest.json",
            "eval_script": "scripts/nope.py", "count_mode": None, "evidence": {},
        },
    ]


def _prep(monkeypatch, tmp_path, rows):
    from app.review import criteria_status
    monkeypatch.setattr(criteria_status, "pending_adjudications", lambda: rows)
    m = _load()
    monkeypatch.setattr(m, "STATE", tmp_path / "state.json")
    return m


def test_first_run_prints_full_plain_report(monkeypatch, tmp_path):
    m = _prep(monkeypatch, tmp_path, _rows())
    out = m.build_report()
    assert "背景 30 秒" in out, "缺背景说明 = 不满足'通俗易懂解释背景'"
    assert "需要你决定" in out, "缺待裁段"
    assert "涨幅榜前 100" in out, "白话名未命中 (NEEDS_USER 项必须抬出)"
    assert "brand_new_shadow" in out, "未收录的新条目被静默吞掉"
    assert m.STATE.exists(), "state 未落盘 ⇒ 下一轮无法判'有无变化'"
    assert json.loads(m.STATE.read_text(encoding="utf-8"))["brand_new_shadow"].startswith("READY")


def test_second_run_is_silent_when_nothing_changed(monkeypatch, tmp_path):
    m = _prep(monkeypatch, tmp_path, _rows())
    m.build_report()
    assert m.build_report() == "", "待裁面无变化却输出 ⇒ 违反静默契约 (会变成周报噪声)"


def test_status_change_raises_again(monkeypatch, tmp_path):
    m = _prep(monkeypatch, tmp_path, _rows())
    m.build_report()
    rows = _rows()
    rows[1]["status"] = "KILLED"
    monkeypatch.setattr(m, "_prep_rows", None, raising=False)
    from app.review import criteria_status
    monkeypatch.setattr(criteria_status, "pending_adjudications", lambda: rows)
    out = m.build_report()
    assert out and "gap_chase_gate" in out, "条目落终态后必须再次抬出 (结论出来了要让人看见)"


def test_ssot_adjudicated_item_is_not_raised_until_recheck(monkeypatch, tmp_path):
    """已读判据 + 重看闹钟未到 ⇒ 不算待裁 (SSOT 路径, 即 RFC-092 §8.6-2 的字段级修法)。"""
    rows = _rows()
    for r in rows:
        r["needs_adjudication"] = r["id"] == "futu_channel_top_movers"
        r["recheck_after"] = "" if r["id"] == "futu_channel_top_movers" else "2099-01-01"
        r["adjudicated_at"] = "2026-09-21"
    m = _prep(monkeypatch, tmp_path, rows)
    out = m.build_report()
    assert "涨幅榜前 100" in out, "needs_adjudication=True 的项必须抬到待裁面"
    head = out.split("②")[0]
    assert "gap_chase_gate" not in head, "已读判据且闹钟未到 ⇒ 不算待裁, 不该出现在 ① 段"


def test_overdue_recheck_raises_again(monkeypatch, tmp_path):
    rows = _rows()
    for r in rows:
        r["needs_adjudication"] = False
        r["recheck_after"] = "2000-01-01"      # 闹钟早已过 ⇒ SSOT 会置 needs_adjudication=True
        r["adjudicated_at"] = "2026-09-21"
    rows[1]["needs_adjudication"] = True        # 模拟 SSOT 的 overdue 判定结果
    m = _prep(monkeypatch, tmp_path, rows)
    out = m.build_report()
    assert "gap_chase_gate" in out.split("②")[0], "重看闹钟到期后必须重新抬到待裁面"


def test_adjudicated_registry_ids_are_real_and_typed():
    """已读判据登记的 id 必须真实存在 (防打字错的"幽灵已裁"把真待裁项静默吞掉)。"""
    import sys
    from pathlib import Path as _P
    sys.path.insert(0, str(_P(__file__).resolve().parent.parent))
    from app.playbook.shadow_registry import ADJUDICATED, REGISTRY, adjudication_state
    ids = {i.id for i in REGISTRY}
    ghosts = sorted(set(ADJUDICATED) - ids)
    assert not ghosts, f"ADJUDICATED 引用了不存在的注册表 id (幽灵已裁): {ghosts}"
    for i in REGISTRY:
        st = adjudication_state(i)
        if i.id in ADJUDICATED:
            assert st["adjudicated"] is True and st["read_at"], f"{i.id} 登记缺 read_at"
    # 未登记的条目一律 adjudicated=False ⇒ 仍会被抬到待裁面 (不静默)
    assert adjudication_state(type("X", (), {"id": "__nope__"})())["adjudicated"] is False


def test_dry_does_not_write_state(monkeypatch, tmp_path):
    m = _prep(monkeypatch, tmp_path, _rows())
    assert m.build_report(dry=True)
    assert not m.STATE.exists(), "--dry 不该写 state (供人工查看时不得吞掉下次真实推送)"