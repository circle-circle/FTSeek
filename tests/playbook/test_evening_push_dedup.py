"""盘后追踪推送持久去重的契约测试 (2026-10-15, 审计 P2-6).

钉死: ①同指纹同日不重复推 ②内容变化(结算/改价)后允许重推 ③状态文件损坏=当作未推。
"""
import app.playbook.pipeline_parts.tracking_flow as tf


class _DB:
    def __init__(self, rows):
        self._rows = rows

    def get_instructions_by_date(self, d):
        return self._rows


_R1 = [{"id": 1, "outcome": "win", "exit_price": 10, "pnl_net": 5}]
_R2 = [{"id": 1, "outcome": "loss", "exit_price": 9, "pnl_net": -5}]


def test_same_fingerprint_not_repushed(tmp_path, monkeypatch):
    monkeypatch.setattr(tf, "_TRACK_PUSH_STATE", tmp_path / "s.json")
    db = _DB(_R1)
    fp = tf._evening_push_fingerprint(db, "2026-10-15")
    assert not tf._evening_push_already_sent(db, "2026-10-15", fp)   # 首推前
    tf._evening_push_mark_sent("2026-10-15", fp)
    assert tf._evening_push_already_sent(db, "2026-10-15", fp)       # 同指纹跳过


def test_changed_content_allows_repush(tmp_path, monkeypatch):
    monkeypatch.setattr(tf, "_TRACK_PUSH_STATE", tmp_path / "s.json")
    fp1 = tf._evening_push_fingerprint(_DB(_R1), "2026-10-15")
    tf._evening_push_mark_sent("2026-10-15", fp1)
    fp2 = tf._evening_push_fingerprint(_DB(_R2), "2026-10-15")       # 结局变化
    assert fp2 != fp1
    assert not tf._evening_push_already_sent(_DB(_R2), "2026-10-15", fp2)


def test_corrupt_state_treated_as_unsent(tmp_path, monkeypatch):
    p = tmp_path / "s.json"
    p.write_text("not-json", encoding="utf-8")
    monkeypatch.setattr(tf, "_TRACK_PUSH_STATE", p)
    db = _DB(_R1)
    fp = tf._evening_push_fingerprint(db, "2026-10-15")
    assert not tf._evening_push_already_sent(db, "2026-10-15", fp)   # 宁可多推不丢推
