"""四锁破例预注册契约 (RFC-135, 2026-10-06 用户裁 A).

钉住破例机制的边界。**核心安全属性**: 破例只能消音"被精确授权覆盖的松动锁",
且到期自动失效 — 不得变成永久消音器, 否则会掩盖下一次真松动。

覆盖:
  ① read_live_probe_waiver 的 fail-close 五态 (缺失/坏JSON/缺字段/过期/有效)
  ② gate: allow_real_trading=True 时破例失效 ⇒ 拒 (到期即停)
  ③ 四锁 measure_locks: closed 记实测真相, 破例只影响 alarm_suppressed
  ④ kr4_hard_gate: alarm 只在未覆盖松动时响
  ⑤ 精确覆盖: 授权不得当万能消音器 (别的锁松了必须响)
  ⑥ 周检 check_locks 与四锁同判定 (不分叉)
"""

import json
import sys
from datetime import timedelta
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import app.playbook.live_arm_gate as G
from app.utils.timezone import now_et
from scripts import okr_2026_09_scoring as sc

_ARM = {
    "enabled": True, "mode": "engineering_probe",
    "max_order_qty": 1, "max_open_positions": 3, "session": "RTH",
    "probe": {"authorized": True, "authorization_id": "TEST-AUTH", "direction": "LONG"},
}
_GATE_KW = dict(
    allow_real_trading=True, real_account_id="123456789", lane_db_name="ftseek_us_live",
    instruction_direction="BUY", market_phase="TRADING", open_live_positions=0)


def _write_waiver(path, **over):
    """写一个结构完整的破例文件; 默认永不过期 (相对时间, 无定时炸弹)."""
    doc = {
        "waiver_id": "TEST-WAIVER",
        "authorization_id": "TEST-AUTH",
        "granted": "2026-10-06",
        "granted_by": "pytest",
        "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": (now_et() + timedelta(days=3650)).isoformat(timespec="seconds"),
    }
    doc.update(over)
    Path(path).write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")


# ══════════════ ① read_live_probe_waiver fail-close 五态 ══════════════
def test_waiver_absent_is_inactive(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_WAIVER_FILE", tmp_path / "missing.json")
    w = G.read_live_probe_waiver()
    assert w["active"] is False and w["reason"] == "waiver_file_absent"


def test_waiver_corrupt_json_is_inactive(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is False and w["reason"] == "waiver_unreadable"
    assert "raw_error" in w


def test_waiver_non_object_is_inactive(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text("[1,2,3]", encoding="utf-8")
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    assert G.read_live_probe_waiver()["reason"] == "waiver_not_an_object"


@pytest.mark.parametrize("drop,reason", [
    ("authorization_id", "waiver_missing_authorization_id"),
    ("locks_waived", "waiver_missing_locks_waived"),
    ("expires", "waiver_missing_expires"),
])
def test_waiver_missing_required_field_is_inactive(tmp_path, monkeypatch, drop, reason):
    p = tmp_path / "w.json"
    doc = {"authorization_id": "A", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
           "expires": (now_et() + timedelta(days=1)).isoformat(timespec="seconds")}
    doc.pop(drop)
    p.write_text(json.dumps(doc), encoding="utf-8")
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is False and w["reason"] == reason


def test_waiver_unparsable_expires_is_inactive(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    p.write_text(json.dumps({"locks_waived": {"X": "y"}, "expires": "not-a-date"}),
                 encoding="utf-8")
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is False and w["reason"] == "waiver_expires_unparsable"


def test_waiver_expired_is_inactive(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    _write_waiver(p, expires=(now_et() - timedelta(hours=1)).isoformat(timespec="seconds"))
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is False and w["reason"] == "waiver_expired"
    assert w["days_left"] < 0


def test_waiver_valid_is_active_with_days_left(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    # +6天1小时 (非整 6 天): days_left 是 floor, 跨两次 now_et() 调用会差几微秒,
    # 用整 6 天断言 ==6 是脆的 (实测得 5)。到期判定本身用精确 now >= expires, 不受影响。
    _write_waiver(p, expires=(now_et() + timedelta(days=6, hours=1)).isoformat(timespec="seconds"))
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is True and w["reason"] == "waiver_active"
    assert w["days_left"] == 6
    assert w["locks_waived"] == {"ALLOW_REAL_TRADING": "true"}
    assert w["authorization_id"] == "TEST-AUTH"


def test_waiver_naive_expires_is_treated_as_et(tmp_path, monkeypatch):
    """无时区的 expires 不得被当 UTC 而误判提前 4/5 小时到期.

    ET = UTC-4。取 +1天2小时 的裸时间戳: 按 ET 读 ⇒ floor=1;
    若被误当 UTC 读 ⇒ 瞬间等价于 ET 墙钟 +1天6小时, floor 会变成 1 但边界偏移,
    故同时钉"到期前 3 小时的裸戳仍 active"(UTC 误读会把它算成已过期)。
    """
    p = tmp_path / "w.json"
    naive = (now_et() + timedelta(days=1, hours=2)).replace(tzinfo=None)
    _write_waiver(p, expires=naive.isoformat(timespec="seconds"))
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    w = G.read_live_probe_waiver()
    assert w["active"] is True and w["days_left"] == 1

    # 关键边界: 距到期只剩 3 小时的裸戳必须仍 active
    p2 = tmp_path / "w2.json"
    naive2 = (now_et() + timedelta(hours=3)).replace(tzinfo=None)
    _write_waiver(p2, expires=naive2.isoformat(timespec="seconds"))
    monkeypatch.setattr(G, "_WAIVER_FILE", p2)
    w2 = G.read_live_probe_waiver()
    assert w2["active"] is True, "裸时间戳被当 UTC ⇒ 提前 4 小时误判过期"
    assert w2["days_left"] == 0


# ══════════════ ② gate 到期即停 ══════════════
def test_gate_allows_probe_when_waiver_active(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    _write_waiver(p)
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    d = G.evaluate_live_arm_gate(_ARM, halted=False, **_GATE_KW)
    assert d["allowed"] is True, [r["code"] for r in d["reasons"]]


def test_gate_rejects_when_waiver_expired(tmp_path, monkeypatch):
    """到期自动失效 — 不需要人工记得关 ALLOW_REAL_TRADING."""
    p = tmp_path / "w.json"
    _write_waiver(p, expires=(now_et() - timedelta(days=1)).isoformat(timespec="seconds"))
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    d = G.evaluate_live_arm_gate(_ARM, halted=False, **_GATE_KW)
    assert d["allowed"] is False
    codes = [r["code"] for r in d["reasons"]]
    assert "LIVE_PROBE_WAIVER_INACTIVE" in codes


def test_gate_rejects_when_waiver_absent_but_real_trading_on(tmp_path, monkeypatch):
    monkeypatch.setattr(G, "_WAIVER_FILE", tmp_path / "missing.json")
    d = G.evaluate_live_arm_gate(_ARM, halted=False, **_GATE_KW)
    assert d["allowed"] is False
    assert "LIVE_PROBE_WAIVER_INACTIVE" in [r["code"] for r in d["reasons"]]


def test_gate_rejects_waiver_scope_or_authorization_mismatch(tmp_path, monkeypatch):
    p = tmp_path / "w.json"
    _write_waiver(p, locks_waived={"OTHER_LOCK": "true"})
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    d = G.evaluate_live_arm_gate(_ARM, halted=False, **_GATE_KW)
    assert "LIVE_PROBE_WAIVER_SCOPE_INVALID" in [r["code"] for r in d["reasons"]]

    _write_waiver(p, authorization_id="DIFFERENT-AUTH")
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    d = G.evaluate_live_arm_gate(_ARM, halted=False, **_GATE_KW)
    assert "LIVE_PROBE_WAIVER_AUTHORIZATION_MISMATCH" in [r["code"] for r in d["reasons"]]


def test_waiver_does_not_loosen_other_probe_gates(tmp_path, monkeypatch):
    """破例只解锁 ALLOW_REAL_TRADING 那把锁, 不放宽方向/时段/持仓上限."""
    p = tmp_path / "w.json"
    _write_waiver(p)
    monkeypatch.setattr(G, "_WAIVER_FILE", p)
    for label, over in (("SHORT 方向", dict(instruction_direction="SELL_SHORT")),
                        ("盘后", dict(market_phase="AFTER_HOURS")),
                        ("已持仓到上限", dict(open_live_positions=3)),
                        ("持仓数不可得", dict(open_live_positions=None))):
        kw = {**_GATE_KW, **over}
        d = G.evaluate_live_arm_gate(_ARM, halted=False, **kw)
        assert d["allowed"] is False, f"{label}: 竟被破例放行"
        assert "LIVE_PROBE_WAIVER_INACTIVE" not in [r["code"] for r in d["reasons"]]


# ══════════════ ③④⑤ 四锁侧 ══════════════
def _repo(tmp_path, env, waiver_doc=None, cfg="  execute_mode: futu_simulate\n"):
    root = tmp_path
    (root / "data").mkdir(exist_ok=True)
    (root / sc.ENV_FILE).write_text(env, encoding="utf-8")
    (root / sc.CONFIG_FILE).parent.mkdir(exist_ok=True)
    (root / sc.CONFIG_FILE).write_text(cfg, encoding="utf-8")
    if waiver_doc is not None:
        (root / sc.WAIVER_FILE).write_text(json.dumps(waiver_doc, ensure_ascii=False),
                                           encoding="utf-8")
    return root


_SIM_ENV = "AUTO_TRADE_ACCOUNT=sim\nALLOW_REAL_TRADING=false\n"
_REAL_ON_ENV = "AUTO_TRADE_ACCOUNT=sim\nALLOW_REAL_TRADING=true\n"


def test_locks_all_closed_needs_no_waiver(tmp_path):
    lk = sc.measure_locks(_repo(tmp_path, _SIM_ENV), graduation=None)
    assert lk["all_closed"] is True
    assert lk["alarm_suppressed"] is False
    assert lk["loose_locks"] == []
    assert sc.kr4_hard_gate(locks=lk)["alarm"] is False


def test_locks_closed_records_truth_even_when_waived(tmp_path):
    """③ 核心: closed 不得因授权被改写为 True (那是撒谎)."""
    root = _repo(tmp_path, _REAL_ON_ENV, {
        "authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": (now_et() + timedelta(days=6, hours=1)).isoformat(timespec="seconds")})
    lk = sc.measure_locks(root, graduation=None)
    assert lk["observed"]["ALLOW_REAL_TRADING"] == "true"
    assert lk["closed"]["ALLOW_REAL_TRADING"] is False, "锁松着必须记 False"
    assert lk["all_closed"] is False, "有破例也不得声称全闭"
    assert lk["alarm_suppressed"] is True
    assert lk["loose_locks"] == []
    assert lk["waiver"]["active"] is True and lk["waiver"]["days_left"] == 6


def test_kr4_alarm_suppressed_only_when_fully_covered(tmp_path):
    root = _repo(tmp_path, _REAL_ON_ENV, {
        "authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": (now_et() + timedelta(days=6)).isoformat(timespec="seconds")})
    b = sc.kr4_hard_gate(locks=sc.measure_locks(root, graduation=None))
    assert b["alarm"] is False, "破例期内不得 alarm (否则每周狼来了)"
    # verdict 仍 NOT_MET: 门禁重定义未完成, 破例不改判 KR4 达成
    assert b["verdict"] == sc.VERDICT_NOT_MET


def test_kr4_alarm_fires_when_waiver_expired(tmp_path):
    """② 对称性: 到期 ⇒ 告警自动复响, 无需人工."""
    root = _repo(tmp_path, _REAL_ON_ENV, {
        "authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": (now_et() - timedelta(days=1)).isoformat(timespec="seconds")})
    lk = sc.measure_locks(root, graduation=None)
    assert lk["waiver"]["active"] is False and lk["waiver"]["reason"] == "waiver_expired"
    assert lk["alarm_suppressed"] is False
    assert lk["loose_locks"] == ["ALLOW_REAL_TRADING"]
    assert sc.kr4_hard_gate(locks=lk)["alarm"] is True


def test_kr4_alarm_fires_when_waiver_file_absent(tmp_path):
    lk = sc.measure_locks(_repo(tmp_path, _REAL_ON_ENV), graduation=None)
    assert lk["waiver"]["reason"] == "waiver_file_absent"
    assert lk["alarm_suppressed"] is False
    assert sc.kr4_hard_gate(locks=lk)["alarm"] is True


def test_waiver_cannot_silence_uncovered_lock(tmp_path):
    """⑤ 授权不是万能消音器: 别的锁松了必须响."""
    root = _repo(tmp_path, "AUTO_TRADE_ACCOUNT=live\nALLOW_REAL_TRADING=true\n", {
        "authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": (now_et() + timedelta(days=6)).isoformat(timespec="seconds")})
    lk = sc.measure_locks(root, graduation=None)
    assert "AUTO_TRADE_ACCOUNT" in lk["loose_locks"]
    assert lk["alarm_suppressed"] is False
    assert sc.kr4_hard_gate(locks=lk)["alarm"] is True


def test_waiver_value_mismatch_does_not_silence(tmp_path):
    """授权写 true 但实测是别的值 ⇒ 不算覆盖 (防止授权被曲解)."""
    root = _repo(tmp_path, _REAL_ON_ENV, {
        "authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "1"},
        "expires": (now_et() + timedelta(days=6)).isoformat(timespec="seconds")})
    lk = sc.measure_locks(root, graduation=None)
    assert lk["loose_locks"] == ["ALLOW_REAL_TRADING"]
    assert lk["alarm_suppressed"] is False


def test_execute_mode_lock_not_waivable_by_probe_doc(tmp_path):
    """探针破例文档只授权 ALLOW_REAL_TRADING; execute_mode 松动必须响."""
    root = _repo(tmp_path, _SIM_ENV,
                 {"authorization_id": "A1", "locks_waived": {"ALLOW_REAL_TRADING": "true"},
                  "expires": (now_et() + timedelta(days=6)).isoformat(timespec="seconds")},
                 cfg="  execute_mode: futu_real\n")
    lk = sc.measure_locks(root, graduation=None)
    assert lk["loose_locks"] == ["execute_mode"]
    assert lk["alarm_suppressed"] is False
    assert sc.kr4_hard_gate(locks=lk)["alarm"] is True


# ══════════════ ⑥ 周检与四锁同判定 ══════════════
def test_weekly_checkpoint_consumes_same_waiver_reader(tmp_path):
    src = (PROJECT_ROOT / "ops" / "hermes" / "ftseek_weekly_checkpoint.py").read_text(
        encoding="utf-8")
    assert "read_live_probe_waiver" in src, "周检必须复用同一判定源 (不得另写日期解析)"
    assert sc.WAIVER_FILE in src, "周检读的破例文件路径必须与四锁一致"


def test_waiver_file_is_same_path_in_both_auditors(tmp_path):
    src = (PROJECT_ROOT / "ops" / "hermes" / "ftseek_weekly_checkpoint.py").read_text(
        encoding="utf-8")
    assert sc.WAIVER_FILE == "data/live_probe_lock_waiver.json"
    assert f'WAIVER_FILE = "{sc.WAIVER_FILE}"' in src
