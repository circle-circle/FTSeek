# -*- coding: utf-8 -*-
"""借券拥挤度信号行为测试 (2026-08-29, RFC-062).

钉住预注册判据: 费率分档/小池升级/无借券市场隔离/扫描与渲染。
"""
import json
import sys
import tempfile
import unittest
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT))

from app.data.borrow_crowding import (  # noqa: E402
    compute_signal, load_series, scan, format_for_context)


def row(symbol="US.X", date="2026-08-28", pool=1_000_000, fee=3.0, shortable=True):
    return {"symbol": symbol, "date": date, "pool": pool,
            "fee_rate": fee, "shortable": shortable}


class TestFeeTiers(unittest.TestCase):
    """主维度: 费率分档 (预注册 3.0/5.0/8.0)."""

    def test_baseline_fee_no_signal(self):
        self.assertEqual(compute_signal(row(fee=3.0))["level"], "NONE")

    def test_boundary_exactly_3_is_none(self):
        # fee=3.0 是众数基线, 必须不触发
        self.assertEqual(compute_signal(row(fee=3.0))["level"], "NONE")

    def test_watch_tier(self):
        sig = compute_signal(row(fee=3.5))
        self.assertEqual(sig["level"], "WATCH")

    def test_warning_tier(self):
        self.assertEqual(compute_signal(row(fee=5.5))["level"], "WARNING")

    def test_critical_tier(self):
        # BIRK 实弹 12.7% 事件
        sig = compute_signal(row(fee=12.7278, pool=2600))
        self.assertEqual(sig["level"], "CRITICAL")

    def test_no_fee_data_returns_none(self):
        self.assertEqual(compute_signal(row(fee=None))["level"], "NONE")


class TestPoolUpgrade(unittest.TestCase):
    """辅维度: 小池升级只在已有信号时生效."""

    def test_tiny_pool_alone_no_signal(self):
        # 小池 + 基线费率 = 不拥挤 (大盘股报价噪声防护)
        self.assertEqual(compute_signal(row(fee=3.0, pool=5000))["level"], "NONE")

    def test_tiny_pool_upgrades_watch_to_warning(self):
        sig = compute_signal(row(fee=3.5, pool=2600))
        self.assertEqual(sig["level"], "WARNING")

    def test_tiny_pool_upgrades_warning_to_critical(self):
        sig = compute_signal(row(fee=6.0, pool=5000))
        self.assertEqual(sig["level"], "CRITICAL")


class TestNoBorrowMarketIsolation(unittest.TestCase):
    """无借券市场 ≠ 拥挤 (首评实弹纠错: 55/60 噪声事件)."""

    def test_no_borrow_market_is_none(self):
        # shortable=False + pool=0 + 基线费率 = 无借券市场, 不是拥挤
        sig = compute_signal(row(fee=0.0, pool=0, shortable=False))
        self.assertEqual(sig["level"], "NONE")
        self.assertTrue(sig.get("no_borrow_market"))

    def test_no_borrow_market_null_pool(self):
        sig = compute_signal(row(fee=3.0, pool=None, shortable=False))
        self.assertEqual(sig["level"], "NONE")

    def test_unborrowable_with_pool_still_warns(self):
        # 有池但券商标不可卖空: 仍预警 (罕见但语义属拥挤/可借性)
        sig = compute_signal(row(fee=3.0, pool=500_000, shortable=False))
        self.assertEqual(sig["level"], "WARNING")


class TestScanAndFormat(unittest.TestCase):
    def _tmp_series(self, rows):
        f = tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False)
        for r in rows:
            f.write(json.dumps(r) + "\n")
        f.close()
        return Path(f.name)

    def test_scan_flags_and_dedupes_daily(self):
        p = self._tmp_series([
            row(symbol="US.BIRK", date="2026-08-14", fee=12.7, pool=2600),
            row(symbol="US.AAPL", date="2026-08-14", fee=3.0, pool=999_999),
            row(symbol="US.SAN", date="2026-08-19", fee=11.5, pool=120_400),
        ])
        summary = scan(series=load_series(p))
        self.assertEqual(summary["date"], "2026-08-19")
        # 只看最新日期 (8/19): 只有 SAN 在 8/19 有行
        syms = [f["symbol"] for f in summary["flagged"]]
        self.assertEqual(syms, ["US.SAN"])

    def test_scan_specific_date(self):
        p = self._tmp_series([
            row(symbol="US.BIRK", date="2026-08-14", fee=12.7, pool=2600),
        ])
        summary = scan(series=load_series(p), date="2026-08-14")
        self.assertEqual(len(summary["flagged"]), 1)
        self.assertEqual(summary["flagged"][0]["level"], "CRITICAL")

    def test_scan_symbol_filter(self):
        p = self._tmp_series([
            row(symbol="US.BIRK", date="2026-08-14", fee=12.7, pool=2600),
            row(symbol="US.SAN", date="2026-08-14", fee=11.5, pool=120_400),
        ])
        summary = scan(series=load_series(p), symbols=["US.BIRK"])
        syms = [f["symbol"] for f in summary["flagged"]]
        self.assertEqual(syms, ["US.BIRK"])

    def test_format_empty_when_no_flags(self):
        self.assertEqual(format_for_context({"flagged": []}), "")

    def test_format_renders_record_only_banner(self):
        summary = {"flagged": [{
            "symbol": "US.BIRK", "level": "CRITICAL", "score": 3.0,
            "fee_rate": 12.7, "pool": 2600,
            "reasons": ["费率 12.7% > 8.0% (借券极贵, 做空高度拥挤)"]}]}
        txt = format_for_context(summary)
        self.assertIn("BIRK", txt)
        self.assertIn("record-only", txt)
        self.assertIn("不构成方向指令", txt)


if __name__ == "__main__":
    unittest.main()
