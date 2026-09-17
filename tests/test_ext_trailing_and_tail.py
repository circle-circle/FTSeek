#!/usr/bin/env python3
"""F3 (adaptive trailing 上限可 env 放开) + F5 (previous_session_volume 只读尾部) 回归测试.

F3 背景: clamp 的 max 在 MFE ≥ min/max 比例处饱和 ⇒ MFE 3% 与 16% 共用 1.2% 回撤
        (高 MFE 反而被锁更紧)。放开走 env, 不动冻结窗 config (KR2 协议)。
F5 背景: 原实现每次全量 read_text + 倒读, 分钟级 cron × 每标的 会随样本线性放大 I/O。
"""
import importlib.util
import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))


def _load(name, rel):
    spec = importlib.util.spec_from_file_location(name, ROOT / rel)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


AT = _load("adaptive_trailing_t", "app/playbook/adaptive_trailing.py")
MS = _load("market_session_t", "scripts/extended_hours_market_session.py")

CFG = {"adaptive_trailing_enabled": True, "adaptive_trailing_target_capture_pct": 60.0,
       "adaptive_trailing_min_drawdown_pct": 0.4, "adaptive_trailing_max_drawdown_pct": 1.2}


def test_trailing_default_cap_unchanged_without_env(monkeypatch):
    monkeypatch.delenv("EXT_ADAPTIVE_TRAILING_MAX_DD", raising=False)
    assert AT.adaptive_trailing_drawdown_pct(1.0, CFG, "PLAYBOOK_SIM") == 0.4     # min 地板
    assert AT.adaptive_trailing_drawdown_pct(3.0, CFG, "PLAYBOOK_SIM") == 1.2
    assert AT.adaptive_trailing_drawdown_pct(16.0, CFG, "PLAYBOOK_SIM") == 1.2    # 饱和(问题所在)
    assert AT.adaptive_trailing_drawdown_pct(3.0, CFG, "LIVE") is None            # 非 SIM 不生效


def test_trailing_env_raises_cap_and_gives_space(monkeypatch):
    monkeypatch.setenv("EXT_ADAPTIVE_TRAILING_MAX_DD", "2.5")
    assert AT.adaptive_trailing_drawdown_pct(1.0, CFG, "PLAYBOOK_SIM") == 0.4     # 低 MFE 仍地板
    assert AT.adaptive_trailing_drawdown_pct(3.0, CFG, "PLAYBOOK_SIM") == 1.2     # 公式段
    assert AT.adaptive_trailing_drawdown_pct(6.25, CFG, "PLAYBOOK_SIM") == 2.5    # 公式到上限
    assert AT.adaptive_trailing_drawdown_pct(16.0, CFG, "PLAYBOOK_SIM") == 2.5    # 之上有界封顶
    snap = AT.adaptive_trailing_snapshot(10.0, CFG, "PLAYBOOK_SIM")
    assert snap["drawdown_pct"] == 2.5 and snap["mfe_pct"] == 10.0


def test_trailing_env_invalid_falls_back_to_config(monkeypatch):
    for bad in ("abc", "", "0.1"):          # 非法 / 空 / 小于地板
        monkeypatch.setenv("EXT_ADAPTIVE_TRAILING_MAX_DD", bad)
        assert AT.adaptive_trailing_drawdown_pct(16.0, CFG, "PLAYBOOK_SIM") == 1.2


def test_tail_lines_reads_only_tail(tmp_path):
    path = tmp_path / "big.jsonl"
    rows = [json.dumps({"i": i, "pad": "x" * 200}) for i in range(2000)]
    path.write_text("\n".join(rows) + "\n")
    lines = MS._tail_lines(path, max_bytes=65536)
    assert lines and len(lines) < len(rows)                 # 没有全读
    assert json.loads(lines[-1])["i"] == 1999                # 末尾完整
    assert all(json.loads(l).get("i") is not None for l in lines)   # 首行截断已丢弃


def test_previous_session_volume_uses_tail_and_returns_none_when_absent(tmp_path):
    path = tmp_path / "shadow.jsonl"
    old = [json.dumps({"symbol": "US.OLD", "trade_date": "2026-01-01", "session": "premarket",
                       "session_volume": 1}) for _ in range(1500)]
    recent = [json.dumps({"symbol": "US.NEW", "trade_date": "2026-09-18", "session": "afterhours",
                          "session_volume": 5000})]
    path.write_text("\n".join(old + recent) + "\n")
    assert MS.previous_session_volume(str(path), "US.NEW", "2026-09-18", "afterhours") == 5000
    assert MS.previous_session_volume(str(path), "US.NOPE", "2026-09-18", "afterhours") is None
    delta = MS.session_volume_delta(str(path), "US.NEW", "2026-09-18", "afterhours", 5200)
    assert delta == 200