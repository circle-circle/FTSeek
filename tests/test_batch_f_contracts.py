"""DEB-20260816-fullchain 批次F 契约测试: P1-13 (解锁密码
去硬编码) / P1-6cap (fallback 陈旧度上限) / 低优 (ATR带因返回, DTE 用 ET).

2026-09-06: #25 (pkill 精确匹配) 两条契约随 ftseek_us.sh 一并删除 — 脚本既删,
全仓 live 代码已无 pkill 面, 契约主体不存在 (治理审计批②, 用户批准删脚本)."""
import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))


# ── P1-13: 期权解锁密码不再硬编码 ────────────────────────────────────────

def test_p1_13_no_hardcoded_md5():
    src = (PROJECT_ROOT / "scripts" / "archive" / "close_all_options.py").read_text(encoding="utf-8")
    assert "21eab4e033c379d31c61a4e64419028c" not in src
    assert "FUTU_TRADE_UNLOCK_MD5" in src


def test_p1_13_missing_env_refuses():
    # 按路径加载归档模块 (archive 零活 import 军规: 不得 from scripts.archive import)
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "close_all_options_archived",
        PROJECT_ROOT / "scripts" / "archive" / "close_all_options.py")
    cao = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cao)
    called = []
    cao.unlock_trade(SimpleNamespace(unlock_trade=lambda **kw: called.append(kw)),
                     acc_id=123, )
    assert called == []  # 未设环境变量 → 拒绝解锁, 不调用 ctx


# ── P1-6 cap: fallback 陈旧度上限 (prev_scan_ts) ─────────────────────────

def test_p1_6_cap_trail_state_records_timestamp():
    from app.risk.position_exit_monitor import ExitMonitor
    position = SimpleNamespace(trail_state={}, symbol="US.TEST")
    ExitMonitor._update_trail_state(
        object(), position, 105.0, {}, "LONG", entry_price=100.0, atr=0)
    ts = position.trail_state["prev_scan_ts"]
    assert ts > 0
    assert position.trail_state["prev_scan_price"] == 105.0


def test_p1_6_cap_freshness_gate_in_source():
    src = (PROJECT_ROOT / "app" / "risk" / "position_exit_monitor.py").read_text(encoding="utf-8")
    assert "prev_scan_ts" in src
    # 陈旧度门: 无时间戳(含重启/旧state)或 >5min → 跳过本轮 (不再假装监控)
    assert "_prev_ts <= 0 or (time.time() - _prev_ts) > 300" in src


# ── 低优: ATR=0 带因返回 + 调用方透传 ───────────────────────────────────

def test_low_atr_skip_reason_propagates():
    gen = (PROJECT_ROOT / "app" / "playbook" / "instruction_generator.py").read_text(encoding="utf-8")
    pipe = (PROJECT_ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    assert "ATR数据缺失" in gen
    assert '(values or {}).get("skip_reason")' in pipe  # 透传, 不再统一标"价格缺失"


def test_low_dte_uses_et_date(monkeypatch):
    from app.utils import timezone as tz
    from app.risk.option_risk_monitor import OptionRiskMonitor, OptionRiskConfig
    monkeypatch.setattr(tz, "now_et", lambda: SimpleNamespace(
        date=lambda: date(2026, 8, 14)))
    mon = OptionRiskMonitor(OptionRiskConfig())
    res = mon.check_dte("2026-08-21")  # 无 current_date → ET 今天
    assert res["dte"] == 7
    # 显式 current_date 仍优先 (确定性入口)
    res2 = mon.check_dte("2026-08-21", current_date="2026-08-13")
    assert res2["dte"] == 8
