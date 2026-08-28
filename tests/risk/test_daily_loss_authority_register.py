"""日亏损防线六套权威登记契约 (2026-08-28 冲突显性化).

"当日亏损"事实上存在六套独立防线 (阈值/口径/触点各异, 互不感知) — 合并/裁剪
属人工风险裁决, 已在每套防线模块头注写入同一份全景登记块。本测试钉住两件事:
  ① 登记不烂: 六个模块的头注都必须仍带登记块 (防止被误删或新增第七套后旧
     登记静默过时);
  ② 登记全量: 每份登记必须点名全部六个模块文件名 (防止登记被截断成残表).

回滚: 删本文件即可 (登记块本身是注释, 无运行时行为).
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]

AUTHORITIES = [
    "app/risk/account_circuit_breaker.py",
    "app/risk/absolute_circuit_breaker.py",
    "app/risk/symbol_day_loss.py",
    "app/risk/symbol_daily_loss_guard.py",
    "app/playbook/risk_circuit_breaker.py",
    "app/risk/drawdown_control.py",
]


def test_every_daily_loss_authority_carries_the_full_register():
    missing = []
    for rel in AUTHORITIES:
        text = (ROOT / rel).read_text(encoding="utf-8")
        if "日亏损防线登记" not in text:
            missing.append(f"{rel}: 头注缺登记块")
            continue
        for peer in AUTHORITIES:
            if peer != rel and Path(peer).name not in text:
                missing.append(f"{rel}: 登记缺 {Path(peer).name} 条目")
    assert not missing, f"日亏损防线登记残缺: {missing}"


def test_register_lists_exactly_the_live_authorities():
    """登记的六套必须是当前活体消费方实存的模块 (改名/退役需同步改登记)."""
    for rel in AUTHORITIES:
        assert (ROOT / rel).exists(), f"{rel} 不存在 — 登记与实际防线漂移"
