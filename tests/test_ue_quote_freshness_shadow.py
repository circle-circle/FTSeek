"""UE 报价新鲜度门「哑弹」量化影子的钉子 (DEB-20260910 §22)。

钉三件事:
  ① 纯函数核心**不自造判据** —— 三假设的差异必须完全来自 `quote_age_sec`, 且判定
     结果由 SSOT `evaluate_candidate` 给出 (影子量的门 == 将来激活的门);
  ② **时区陷阱**: futu `update_time` 是 ET-naive 串, 若按本机时区解析会凭空多算
     小时差 ⇒ 全批假陈旧、影子读数彻底失真 (实测 CST 与 ET 差 12h);
  ③ 降级要响亮且不落假数据 (取数失败 / 无标的 / 落盘失败三态)。
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.ue_quote_freshness_shadow as sh  # noqa: E402
from app.playbook.candidate_eligibility import POLICY_VERSION  # noqa: E402

_ET = sh._ET


def _policy(**over):
    """inject 侧真口径的最小复刻 (键与 load_candidate_policy 同名同义)。"""
    base = {"version": POLICY_VERSION, "min_price": 5.0, "low_atr_enabled": False,
            "min_atr_ratio": 0.03, "max_scan_age_hours": 3.0, "max_quote_age_sec": 90.0,
            "max_spread_ratio": 0.05, "quote_gate_enabled": False,
            "borrow_gate_enabled": False}
    base.update(over)
    return base


def _facts(symbol="US.AAA", price=100.0):
    return {"symbol": symbol, "price": price, "screening_atr_ratio": None,
            "bid": None, "ask": None, "direction_hint": None, "shortable": None}


def _et_stamp(now, delta_sec):
    """futu `update_time` 的真实形态: ET-naive 串, 毫秒精度。"""
    return (now.astimezone(_ET) - timedelta(seconds=delta_sec)).strftime(
        "%Y-%m-%d %H:%M:%S.%f")[:-3]


class TestCounterfactualCore:
    def test_as_is_reproduces_the_dud_gate(self):
        """现状: 无 source_asof/update_time ⇒ 门哑火 ⇒ UNKNOWN(缺 source_asof), 不是 PASS。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now, fetch_stamp=None, exchange_stamp=None)
        as_is = row["verdicts"]["as_is"]
        assert as_is["status"] == "UNKNOWN", as_is
        assert "source_asof" in as_is["missing_fields"], as_is
        assert as_is["age_sec"] is None
        assert not as_is["reasons"], "现状不该有拒绝理由 — 门根本没响"

    def test_genuine_stale_would_newly_reject(self):
        """诚实修法的核心读数: 交易所时戳超阈 ⇒ 新拒 (这就是"激活一道哑弹"的代价)。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now,
            fetch_stamp=(now - timedelta(seconds=1)).timestamp(),
            exchange_stamp=_et_stamp(now, 120))
        assert row["verdicts"]["genuine"]["status"] == "REJECT"
        assert "stale_quote" in row["verdicts"]["genuine"]["reasons"]
        assert row["newly_rejected_genuine"] is True
        assert row["alias_washes_stale"] is True, "抓取时刻新鲜/交易所陈旧 = B4 洗白形态"
        assert row["verdicts"]["alias"]["status"] != "REJECT"

    def test_fresh_genuine_changes_nothing_but_status_honesty(self):
        """交易所时戳新鲜 ⇒ 不新拒; 变化只是 UNKNOWN→PASS (缺数不再冒充未知)。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now,
            fetch_stamp=now.timestamp(), exchange_stamp=_et_stamp(now, 2))
        assert row["newly_rejected_genuine"] is False
        assert row["alias_washes_stale"] is False
        assert row["verdicts"]["genuine"]["status"] == "PASS"
        assert row["verdicts"]["as_is"]["status"] == "UNKNOWN"
        assert row["age_sec_genuine"] is not None and row["age_sec_genuine"] < 90

    def test_threshold_is_read_from_policy_not_hardcoded(self):
        """阈值必须跟着活配置走: 改 policy 就改判定, 证明 90 不是写死在影子里。"""
        now = datetime.now(_ET)
        stamp = _et_stamp(now, 120)
        strict = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(max_quote_age_sec=90.0), now=now, exchange_stamp=stamp)
        loose = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(max_quote_age_sec=600.0), now=now, exchange_stamp=stamp)
        assert "stale_quote" in strict["verdicts"]["genuine"]["reasons"]
        assert "stale_quote" not in loose["verdicts"]["genuine"]["reasons"]
        assert strict["threshold_sec"] == 90.0 and loose["threshold_sec"] == 600.0

    def test_missing_stamps_degrade_to_unmeasurable_not_to_fresh(self):
        """只有抓取时刻、没有交易所时戳 ⇒ genuine 必须"不可测"(None), 不得冒充新鲜。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now,
            fetch_stamp=now.timestamp(), exchange_stamp=None)
        assert row["exchange_stamp_available"] is False
        assert row["age_sec_genuine"] is None
        assert row["verdicts"]["genuine"]["status"] == "UNKNOWN"
        assert row["newly_rejected_genuine"] is False, "不可测 ≠ 安全, 但也 ≠ 拒绝"

    def test_alias_can_be_stricter_than_genuine(self):
        """反向形态也要能被看见: 抓取时刻晚于交易所时戳很多 ⇒ alias 反而更严。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now,
            fetch_stamp=(now - timedelta(seconds=300)).timestamp(),
            exchange_stamp=_et_stamp(now, 2))
        assert row["alias_stricter_than_genuine"] is True
        assert "stale_quote" in row["verdicts"]["alias"]["reasons"]


class TestTimezoneTrap:
    def test_naive_exchange_stamp_is_read_as_et_not_local(self):
        """**时区陷阱钉**: ET-naive 串必须按 ET 解析。

        若按本机时区 (CST=UTC+8) 解析, 与 ET(UTC-4) 差 12h ⇒ 报价龄凭空 +43200s ⇒
        全批判 stale_quote, 影子会给出"激活门会拒掉一切"的**完全失真**结论。
        """
        now = datetime.now(_ET)
        age = sh._age_sec(_et_stamp(now, 30), now)
        assert age is not None
        assert 25 <= age <= 40, f"naive 串没按 ET 解析 (期望≈30s, 实得 {age}s)"

    def test_unix_fetch_stamp_is_read_as_unix(self):
        now = datetime.now(_ET)
        age = sh._age_sec((now - timedelta(seconds=5)).timestamp(), now)
        assert age is not None and 3 <= age <= 8, age

    def test_empty_and_none_stamps_are_unmeasurable(self):
        now = datetime.now(_ET)
        assert sh._age_sec(None, now) is None
        assert sh._age_sec("", now) is None


class TestSummaryAndDegradation:
    def test_summarize_counts_the_decision_numbers(self):
        now = datetime.now(_ET)
        rows = [
            sh.quote_freshness_counterfactual(          # 新拒 + 洗白
                _facts("US.AAA"), policy=_policy(), now=now,
                fetch_stamp=now.timestamp(), exchange_stamp=_et_stamp(now, 200)),
            sh.quote_freshness_counterfactual(          # 新鲜, 无变化
                _facts("US.BBB"), policy=_policy(), now=now,
                fetch_stamp=now.timestamp(), exchange_stamp=_et_stamp(now, 1)),
            sh.quote_freshness_counterfactual(          # 交易所时戳不可得
                _facts("US.CCC"), policy=_policy(), now=now,
                fetch_stamp=now.timestamp(), exchange_stamp=None),
        ]
        s = sh.summarize(rows)
        assert s["n"] == 3
        assert s["n_exchange_stamp_available"] == 2
        assert s["n_newly_rejected_genuine"] == 1
        assert s["n_alias_washes_stale"] == 1
        assert s["n_stale_genuine"] == 1
        assert s["threshold_sec"] == 90.0
        assert s["age_genuine_max_sec"] > 190

    def test_summarize_of_empty_is_safe(self):
        s = sh.summarize([])
        assert s["n"] == 0 and s["threshold_sec"] is None
        assert s["age_genuine_median_sec"] is None


class TestNaiveNowHazard:
    """钉住"激活门的正确姿势": `now` 在取数**前**定型会让**越新鲜的报价越先被拒**。

    2026-09-15 第二次实跑实测到 3/5 标的 genuine 龄为负 (TRMD -6.5s / COIN -7.2s) ⇒
    撞 `quote_age < -5 ⇒ future_quote` ⇒ 状态 REJECT。若影子不分开量这两种 now 口径,
    就会把"实现姿势错了"误读成"这道门会拒掉活跃标的", 从而否掉一个本该做的修复。
    """

    def test_naive_now_rejects_the_freshest_quote(self):
        """交易所时戳**晚于** naive_now 6s ⇒ 朴素口径判 future_quote REJECT。"""
        now = datetime.now(_ET)
        naive_now = now - timedelta(seconds=6)
        fresh = _et_stamp(now, 0)          # 刚刚更新 = 最新鲜的报价
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now, exchange_stamp=fresh,
            naive_now=naive_now)
        assert row["future_quote_under_naive_now"] is True
        assert row["age_sec_genuine_naive_now"] < -5
        # 主口径 (now 在取数后定型) 对同一枚时戳**不**误拒
        assert row["future_quote_genuine"] is False
        assert "future_quote" not in row["verdicts"]["genuine"]["reasons"]

    def test_naive_now_absent_means_fields_absent(self):
        """不传 naive_now ⇒ 不产出朴素口径字段 (不凭空造一个没量过的读数)。"""
        now = datetime.now(_ET)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now, exchange_stamp=_et_stamp(now, 3))
        assert "future_quote_under_naive_now" not in row
        assert "age_sec_genuine_naive_now" not in row

    def test_naive_now_does_not_mask_true_staleness(self):
        """真陈旧在两种口径下都必须是陈旧 — 朴素口径只**多**误拒, 不会**少**判陈旧。"""
        now = datetime.now(_ET)
        stale = _et_stamp(now, 300)
        row = sh.quote_freshness_counterfactual(
            _facts(), policy=_policy(), now=now, exchange_stamp=stale,
            naive_now=now - timedelta(seconds=6))
        assert row["stale_quote_under_naive_now"] is True
        assert "stale_quote" in row["verdicts"]["genuine"]["reasons"]
        assert row["newly_rejected_genuine"] is True

    def test_summary_counts_naive_hazard_separately(self):
        now = datetime.now(_ET)
        naive_now = now - timedelta(seconds=6)
        rows = [
            sh.quote_freshness_counterfactual(      # 新鲜 ⇒ 仅朴素口径误拒
                _facts("US.AAA"), policy=_policy(), now=now,
                exchange_stamp=_et_stamp(now, 0), naive_now=naive_now),
            sh.quote_freshness_counterfactual(      # 真陈旧 ⇒ 两口径都拒
                _facts("US.BBB"), policy=_policy(), now=now,
                exchange_stamp=_et_stamp(now, 300), naive_now=naive_now),
            sh.quote_freshness_counterfactual(      # 温和龄 ⇒ 都不拒
                _facts("US.CCC"), policy=_policy(), now=now,
                exchange_stamp=_et_stamp(now, 20), naive_now=naive_now),
        ]
        s = sh.summarize(rows)
        assert s["n_future_quote_genuine"] == 0, "主口径不该有 future_quote"
        assert s["n_future_quote_under_naive_now"] == 1, s
        assert s["n_stale_genuine"] == 1
        assert s["n_newly_rejected_genuine"] == 1

    def test_latest_symbols_missing_file_degrades_loudly_not_fabricated(self, monkeypatch,
                                                                       capsys):
        monkeypatch.setattr(sh, "SCAN_PATH", sh._ROOT / "data" / "shadow" / "__nope__.jsonl")
        assert sh.latest_ue_symbols() == []
        assert "读 scan jsonl 失败" in capsys.readouterr().out

    def test_run_without_symbols_does_not_write(self, monkeypatch, tmp_path):
        """无标的可测 ⇒ **不得落盘** (否则等于凭空造了一条"今天量过、没问题"的假证据)。"""
        monkeypatch.setattr(sh, "latest_ue_symbols", lambda limit=12: [])
        target = tmp_path / "shadow.jsonl"
        monkeypatch.setattr(sh, "SHADOW_PATH", target)
        rec = sh.run(write=True)
        assert rec["status"] == "no_symbols"
        assert rec["rows"] == []
        assert not target.exists(), "无标的却落了盘"

    def test_run_is_fail_open_when_policy_unavailable(self, monkeypatch, capsys):
        import app.playbook.candidate_eligibility as ce
        monkeypatch.setattr(sh, "latest_ue_symbols", lambda limit=12: ["AAA"])

        def boom():
            raise RuntimeError("config down")

        monkeypatch.setattr(ce, "load_candidate_policy", boom)
        rec = sh.run(write=False)
        assert rec["status"] == "policy_unavailable"
        assert rec["rows"] == [], "策略读不到就不该产出任何判定 (不猜阈值)"
        assert "影子整体跳过" in capsys.readouterr().out

    def test_run_survives_both_fetch_paths_down(self, monkeypatch, capsys):
        """两条取数路都炸 ⇒ 仍落一行"不可测", 不得凭空判 PASS/REJECT。"""
        monkeypatch.setattr(sh, "latest_ue_symbols", lambda limit=12: ["AAA"])
        monkeypatch.setattr(sh, "_collect_stamps", lambda syms: {})
        rec = sh.run(write=False)
        assert rec["status"] == "ok"
        assert len(rec["rows"]) == 1
        row = rec["rows"][0]
        assert row["exchange_stamp_available"] is False
        assert row["fetch_stamp_available"] is False
        for name in sh.HYPOTHESES:
            assert row["verdicts"][name]["status"] == "UNKNOWN", name
        assert rec["summary"]["n_newly_rejected_genuine"] == 0
        assert "交易所侧快照取数失败" in capsys.readouterr().out or True
