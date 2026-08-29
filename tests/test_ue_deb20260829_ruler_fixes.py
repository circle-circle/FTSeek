"""DEB-20260829 修尺子批次测试 (UE-B2 拒绝落盘 / UE-A3 clue 噪声过滤 / UE-B1 财报影子)。

背景: 第4轮执行 (520133a3) 落地时测试未提交 ("8用例单测过" 不可复现); 本文件补齐钉住。
另含复核补漏: 入库循环 (stage=save) DA优先/冷却 两分支拒绝落盘 (第4轮遗漏)。

判据锚: DEB-20260829 §五 — UE-A3 过滤后 ETF/权证行占比<10% 且真股权事件不减少;
UE-B2 每笔 reject 可归因; UE-B1 只记不喂。
"""
import json
import os
import sys
from unittest import mock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scripts import llm_universe_scan as ue  # noqa: E402


@pytest.fixture(autouse=True)
def _cfg_on():
    """特性开关钉 True (代码默认路径); kill-switch 用例在测试内再 patch 覆盖。"""
    with mock.patch.object(ue, "_cfg", lambda k, d=None: True):
        yield


# ── UE-A3: 8-K/13DG clue 噪声过滤 ──────────────────────────────

def _8k(ticker, company, items):
    return {"ticker": ticker, "company": company, "items": items}


class TestSecNoiseFilter:
    def test_commodity_trust_filtered(self):
        rows = [_8k("CPER", "UNITED STATES COPPER INDEX FD", ["7.01"]),
                _8k("USO", "UNITED STATES OIL FUND LP", ["7.01", "9.01"]),
                _8k("SAIC", "SCIENCE APPLICATIONS INTL", ["2.02"])]
        kept = ue._filter_8k_noise(rows)
        assert [r["ticker"] for r in kept] == ["SAIC"]

    def test_warrant_by_name_and_hyphen_filtered(self):
        rows = [_8k("OSRHW", "OSR HOLDINGS WARRANT", ["8.01"]),
                _8k("IONQ-WT", "IONQ INC", ["8.01"]),
                _8k("LH", "LABCORP HOLDINGS INC", ["2.02"])]
        kept = ue._filter_8k_noise(rows)
        assert [r["ticker"] for r in kept] == ["LH"]

    def test_bare_w_suffix_real_company_kept(self):
        """FWFW=Flywheel 真实公司: 裸 W 后缀不杀 (8/29 实弹误杀教训)。"""
        rows = [_8k("FWFW", "FLYWHEEL INC", ["8.01"])]
        kept = ue._filter_8k_noise(rows)
        assert [r["ticker"] for r in kept] == ["FWFW"]

    def test_cross_batch_base_ticker_marks_warrant(self):
        """同批有同公司名的基础 ticker → W 后缀者判权证 (MOBX/MOBXW 式)。"""
        rows = [_8k("MOBX", "MOBIX LABS INC", ["8.01"]),
                _8k("MOBXW", "MOBIX LABS INC", ["8.01"])]
        kept = ue._filter_8k_noise(rows)
        assert [r["ticker"] for r in kept] == ["MOBX"]

    def test_etf_routine_701_filtered_but_multi_item_kept(self):
        """ETF 仅 7.01 例行文件剔除; 同 ETF 带实质 item (如2.02) 保留。"""
        rows = [_8k("SPY", "SPDR S&P 500 ETF TRUST", ["7.01"]),
                _8k("SPY", "SPDR S&P 500 ETF TRUST", ["7.01", "8.01"])]
        kept = ue._filter_8k_noise(rows)
        assert len(kept) == 1 and kept[0]["items"] == ["7.01", "8.01"]

    def test_13dg_preferred_and_warrant_filtered(self):
        assert ue._is_13dg_noise("PERFF", "PERFECT PREFERRED SHARES") is True
        assert ue._is_13dg_noise("ABCW", "ABC CORP WARRANT") is True
        assert ue._is_13dg_noise("ABC", "ABC CORP") is False

    def test_kill_switch_disables_filter(self):
        with mock.patch.object(ue, "_cfg", lambda k, d=None: False):
            rows = [_8k("CPER", "UNITED STATES COPPER INDEX FD", ["7.01"])]
            assert ue._filter_8k_noise(rows) == rows
            assert ue._is_13dg_noise("PERFF", "X PREFERRED") is False


# ── UE-B2: inject 拒绝落盘 ──────────────────────────────

def _surf(sym, direction="LONG", conf=0.8, streak=1, tier="A"):
    return {"symbol": sym, "direction": direction, "confidence": conf,
            "emergence_streak_days": streak, "tier": tier}


class TestInjectRejectLog:
    def _read(self, path):
        return [json.loads(l) for l in path.read_text().splitlines()]

    def test_filter_chain_attribution(self, tmp_path):
        p = tmp_path / "inject.jsonl"
        with mock.patch.object(ue, "_INJECT_REJECT_PATH", p):
            surfaced = [
                _surf("KEEP1"), _surf("KEEP2"), _surf("KEEP3"),
                _surf("SKIPDIR", direction="SKIP"),
                _surf("LOWCONF", conf=0.5),
                _surf("INPOS"),
                _surf("DAWRITTEN"),
                _surf("STREAKY", streak=3),
            ]
            ue._log_inject_reject(surfaced, min_conf=0.7,
                                  positions={"INPOS"}, existing_pb={"DAWRITTEN"},
                                  max_streak=2, max_n=2)
        rows = self._read(p)
        reasons = {r["symbol"]: r["reject_reason"] for r in rows}
        assert reasons["SKIPDIR"].startswith("direction(")
        assert reasons["LOWCONF"].startswith("conf(")
        assert reasons["INPOS"] == "already_in_position"
        assert reasons["DAWRITTEN"] == "da_or_other_already_written"
        assert reasons["STREAKY"].startswith("streak_gate(")
        # max_n=2: 3 个合格候选 (KEEP1/2/3) 置信同分, 第3个被配额截断
        assert reasons["KEEP3"] == "quota_cap(max_targets_per_day)"
        assert all(r["stage"] == "filter" for r in rows)
        assert all(set(("date", "symbol", "direction", "reject_reason",
                        "raw_conf", "tier", "streak", "stage")) <= set(r)
                   for r in rows)

    def test_loop_reject_build_stage(self, tmp_path):
        p = tmp_path / "inject.jsonl"
        with mock.patch.object(ue, "_INJECT_REJECT_PATH", p):
            ue._log_inject_loop_reject(_surf("NOPX"), "no_quote(无现价)")
        rows = self._read(p)
        assert rows[0]["reject_reason"] == "no_quote(无现价)"
        assert rows[0]["stage"] == "build"

    def test_save_reject_stage_from_instruction(self, tmp_path):
        """复核补漏: 入库循环 DA优先/冷却 reject 落盘, tier/streak 从 origin 回填。"""
        p = tmp_path / "inject.jsonl"
        it = {"date": "2026-08-30", "symbol": "US.BABA", "direction": "SELL_SHORT",
              "confidence": 50,
              "raw_instruction": {"origin": {"raw_confidence": 85,
                                             "self_tier": "MARKET_CORROBORATED",
                                             "emergence_streak_days": 2}}}
        with mock.patch.object(ue, "_INJECT_REJECT_PATH", p):
            ue._log_inject_save_reject(it, "da_priority_skip(DA-20260830-001)")
            ue._log_inject_save_reject(it, "cooldown(3d同方向已执行)")
        rows = self._read(p)
        assert len(rows) == 2
        assert rows[0]["reject_reason"].startswith("da_priority_skip(")
        assert rows[1]["reject_reason"].startswith("cooldown(")
        assert all(r["stage"] == "save" for r in rows)
        assert rows[0]["symbol"] == "BABA"  # US. 前缀剥掉
        assert rows[0]["raw_conf"] == 85 and rows[0]["tier"] == "MARKET_CORROBORATED"
        assert rows[0]["streak"] == 2

    def test_kill_switch_no_write(self, tmp_path):
        p = tmp_path / "inject.jsonl"
        with mock.patch.object(ue, "_INJECT_REJECT_PATH", p), \
                mock.patch.object(ue, "_cfg", lambda k, d=None: False):
            ue._log_inject_reject([_surf("X")], 0.7, set(), set(), 2, 5)
            ue._log_inject_loop_reject(_surf("X"), "no_quote")
            ue._log_inject_save_reject({"symbol": "US.X"}, "cooldown")
        assert not p.exists()


# ── UE-B1: 全市场财报影子 (只记不喂) ──────────────────────────────

class FakeEC:
    def __init__(self, rows):
        self._rows = rows

    def get_earnings_calendar(self):
        return self._rows


class TestEarningsShadow:
    def _run(self, tmp_path, cal_rows, corro=frozenset(), today="2026-08-31"):
        p = tmp_path / "shadow.jsonl"
        with mock.patch.object(ue, "_EARNINGS_SHADOW_PATH", p), \
                mock.patch.object(ue, "_today", lambda: today), \
                mock.patch("app.data.earnings_calendar.EarningsCalendar",
                           lambda: FakeEC(cal_rows)):
            ret = ue._gather_earnings_shadow(set(corro))
        rows = ([json.loads(l) for l in p.read_text().splitlines()]
                if p.exists() else [])
        return ret, rows

    def test_window_and_cross_hit_sort(self, tmp_path):
        cal = [
            {"symbol": "TODAY_A", "name": "A", "reportDate": "2026-08-31"},
            {"symbol": "TMRW_B", "name": "B", "reportDate": "2026-09-01"},
            {"symbol": "YEST_C", "name": "C", "reportDate": "2026-08-30"},  # 窗口外
            {"symbol": "FAR_D", "name": "D", "reportDate": "2026-09-05"},   # 窗口外
            {"symbol": "HIT_E", "name": "E", "reportDate": "2026-09-01"},
        ]
        ret, rows = self._run(tmp_path, cal, corro={"HIT_E"})
        assert ret is None  # 只记不喂: 不返回任何 clue 行
        syms = [r["symbol"] for r in rows]
        assert "YEST_C" not in syms and "FAR_D" not in syms  # 只取当日+次日
        assert syms[0] == "HIT_E"  # 交叉命中优先
        assert rows[0]["cross_hit"] is True
        assert set(syms) == {"TODAY_A", "TMRW_B", "HIT_E"}

    def test_kill_switch_and_empty_calendar_no_write(self, tmp_path):
        p = tmp_path / "shadow.jsonl"
        with mock.patch.object(ue, "_EARNINGS_SHADOW_PATH", p), \
                mock.patch.object(ue, "_cfg", lambda k, d=None: False):
            ue._gather_earnings_shadow(set())
        assert not p.exists()
        with mock.patch.object(ue, "_EARNINGS_SHADOW_PATH", p), \
                mock.patch("app.data.earnings_calendar.EarningsCalendar",
                           lambda: FakeEC([])):
            ue._gather_earnings_shadow(set())
        assert not p.exists()
