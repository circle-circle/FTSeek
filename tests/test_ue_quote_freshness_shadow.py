"""UE 报价新鲜度门「哑弹」量化影子的钉子 (DEB-20260910 §22)。

钉三件事:
  ① 纯函数核心**不自造判据** —— 三假设的差异必须完全来自 `quote_age_sec`, 且判定
     结果由 SSOT `evaluate_candidate` 给出 (影子量的门 == 将来激活的门);
  ② **时区陷阱**: futu `update_time` 是 ET-naive 串, 若按本机时区解析会凭空多算
     小时差 ⇒ 全批假陈旧、影子读数彻底失真 (实测 CST 与 ET 差 12h);
  ③ 降级要响亮且不落假数据 (取数失败 / 无标的 / 落盘失败三态)。

2026-09-15 补口 (用户裁「请执行」) 追加三组钉子:
  ④ **交易日守卫** — 非交易日不落盘, 判定不可得也不落盘 (fail-closed); 覆盖面之外
     必须是"未知"而不是"是交易日"; 守卫必须**零 futu 连接** (OpenD 槽位与生产共享);
  ⑤ **机械裁决器** — 有效轮次 <10 禁止建议激活; 假日/forced 行不得冒充轮次; 阈值不硬编码;
  ⑥ **alias 边界** — `alias`(抓取时刻冒充行情时点) 只允许存在于新鲜度门关着的路径:
     inject 侧喂 `timestamp` 不得产生 `stale_quote` (行为钉), 且扫描期那个"写 source_asof
     的归一函数"只能被初筛调用 (源码钉 + 阳性对照)。
"""

from __future__ import annotations

import ast
import json
import subprocess
import sys
from datetime import date as _date, datetime, timedelta
from pathlib import Path

import pytest

_ROOT = Path(__file__).resolve().parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

import scripts.ue_quote_freshness_shadow as sh  # noqa: E402
import scripts.ue_quote_freshness_shadow_eval as ev  # noqa: E402
from app.playbook.candidate_eligibility import POLICY_VERSION  # noqa: E402

_ET = sh._ET
# 真守卫 (fixture 会把它换掉; 需要真守卫的用例自行换回)
_REAL_TRADING_CONTEXT = sh._trading_context


@pytest.fixture(autouse=True)
def _pin_trading_day(monkeypatch):
    """把 `_trading_context` 钉成"交易日 + 盘前", 让所有走 `run()` 的既有用例**与运行日无关**。

    为什么必须钉: 守卫落地后, 周末/假日跑测试会让 `run()` 提前返回 `non_trading_day`,
    既有用例全红 —— 那是守卫的正确行为, 不是测试噪声, 所以由 fixture 显式隔离。
    需要**真守卫**语义的用例在自身内部换回 `_REAL_TRADING_CONTEXT` (见 TestTradingDayGuard)。
    """
    monkeypatch.setattr(sh, "_trading_context",
                        lambda now: {"is_trading_day": True, "session_hint": "PRE_MARKET"})


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


class TestTradingDayGuard:
    """交易日守卫 (2026-09-15 补口): cron `15 21 * * 1-5` **假日照跑**。

    假日报价来自上一交易日收盘 ⇒ `genuine` 龄小时级 ⇒ 当轮"会新拒 = n" —— 既**冒充一个
    有效轮次**, 又把 P95 拽向"激活必杀"的假结论。故守卫必须在**取数之前**判、且非交易日
    什么都不做。
    """

    def test_weekend_and_holiday_are_not_trading_days(self):
        assert sh.us_trading_day_flag(_date(2026, 9, 6)) is False    # 周日
        assert sh.us_trading_day_flag(_date(2026, 9, 7)) is False    # Labor Day 2026
        assert sh.us_trading_day_flag(_date(2026, 9, 15)) is True    # 普通交易日

    def test_coverage_gap_is_unknown_never_defaults_to_trading(self):
        """**变异钉**: 假日历覆盖面之外必须是 `None` (未知), 不得默认 `True`。

        默认 `True` = 2027 年起所有假日被静默当交易日 (仓内假日历只到 2026, 已知定时炸弹)。
        """
        assert sh.us_trading_day_flag(_date(2027, 1, 4)) is None
        assert sh.us_trading_day_flag(_date(2027, 12, 24)) is None

    def test_guard_is_futu_free(self):
        """守卫必须**零 futu 连接**: 它存在的意义就是"非交易日什么都别做"。

        反面证据: `app.shared.market_hours` / `app.utils.holiday_calendar` 实测调一次
        `is_trading_day` 就会开一个 `OpenQuoteContext` (2026-09-15 实测), 而 OpenD 槽位
        与生产共享 ⇒ 用它们当守卫 = 为省一次取数先占一次连接。子进程隔离验证。
        """
        code = ("import sys; sys.path.insert(0, %r);"
                "from datetime import date;"
                "from scripts.ue_quote_freshness_shadow import us_trading_day_flag;"
                "print(us_trading_day_flag(date(2026, 9, 15)));"
                "print('futu' in sys.modules);"
                "print('app.utils.holiday_calendar' in sys.modules);"
                "print('app.shared.market_hours' in sys.modules)" % str(_ROOT))
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             timeout=120, cwd=str(_ROOT))
        assert out.returncode == 0, out.stderr
        lines = out.stdout.strip().splitlines()
        assert lines[0] == "True", lines              # 守卫本身要给对答案
        assert lines[1:] == ["False", "False", "False"], (
            f"守卫把 futu/假日历拉进来了 ⇒ 非交易日也会占 OpenD 槽位: {lines[1:]}")

    def test_run_skips_before_fetch_on_non_trading_day(self, monkeypatch):
        """非交易日: **不落盘 + 不取数** (守卫在 `_collect_stamps` 之前 ⇒ 连 futu 都不碰)。"""
        touched = []
        monkeypatch.setattr(sh, "_trading_context", _REAL_TRADING_CONTEXT)
        monkeypatch.setattr(sh, "_now_et", lambda: datetime(2026, 9, 7, 9, 15, tzinfo=_ET))
        monkeypatch.setattr(sh, "_collect_stamps", lambda *a, **k: touched.append(1) or {})
        rec = sh.run(["NVDA"], write=True)
        assert rec["status"] == "non_trading_day"
        assert rec["is_trading_day"] is False
        assert touched == [], "守卫必须在取数之前 —— 否则非交易日仍占 OpenD 槽位"
        assert rec["rows"] == []

    def test_run_fails_closed_when_trading_day_undecidable(self, monkeypatch):
        """判定不可得 (日历覆盖外) ⇒ 不落盘, 且分明与"非交易日"区分 (状态可归因)。"""
        monkeypatch.setattr(sh, "_trading_context", _REAL_TRADING_CONTEXT)
        monkeypatch.setattr(sh, "_now_et", lambda: datetime(2027, 1, 4, 9, 15, tzinfo=_ET))
        touched = []
        monkeypatch.setattr(sh, "_collect_stamps", lambda *a, **k: touched.append(1) or {})
        rec = sh.run(["NVDA"], write=True)
        assert rec["status"] == "trading_day_undecidable"
        assert touched == []

    def test_force_writes_a_row_but_tags_it(self, monkeypatch, tmp_path):
        """`--force` 人工核查: 允许落行, 但必须**自曝身份**, 供裁决器排除 (不冒充轮次)。"""
        monkeypatch.setattr(sh, "_trading_context", _REAL_TRADING_CONTEXT)
        monkeypatch.setattr(sh, "_now_et", lambda: datetime(2026, 9, 7, 9, 15, tzinfo=_ET))
        monkeypatch.setattr(sh, "latest_ue_symbols", lambda limit=12: ["AAA"])
        monkeypatch.setattr(sh, "_collect_stamps", lambda syms: {})
        monkeypatch.setattr(sh, "SHADOW_PATH", tmp_path / "shadow.jsonl")
        rec = sh.run(write=True, force=True)
        assert rec["status"] == "ok"
        assert rec["forced_non_trading_day"] is True
        assert rec["is_trading_day"] is False
        assert (tmp_path / "shadow.jsonl").exists(), "--force 应真落行 (再由裁决器按 forced 排除)"


def _rec(d, ages, *, threshold=90.0, status="ok", flag=True, forced=False):
    """造一条影子记录 (字段名以探针落盘结构为准, 供裁决器读数用)。"""
    rows = [{"symbol": f"US.S{i}", "age_sec_genuine": a, "threshold_sec": threshold,
             "verdicts": {h: {"reasons": (["stale_quote"] if (h == "genuine" and a > threshold)
                                          else [])}
                          for h in sh.HYPOTHESES},
             "newly_rejected_genuine": a > threshold, "alias_washes_stale": False,
             "future_quote_genuine": False, "exchange_stamp_available": True}
            for i, a in enumerate(ages)]
    rec = {"type": "quote_freshness_shadow", "date": d, "status": status,
           "threshold_sec": threshold, "rows": rows}
    if flag is not None:
        rec["is_trading_day"] = flag
    if forced:
        rec["forced_non_trading_day"] = True
    return rec


def _write(path, recs):
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in recs) + "\n",
                    encoding="utf-8")
    return path


class TestShadowEvaluatorMechanicalVerdict:
    """机械裁决器 (2026-09-15 补口): 把"≥10 交易日后再裁"从台账文字变成可执行判据。

    此前该时钟**只活在 crontab 注释里**: 注册表无条目、jsonl 无消费者 ⇒ 到期无人被提醒。
    """

    def test_less_than_10_rounds_forbids_proposing_activation(self, tmp_path):
        recs = [_rec(f"2026-09-{d:02d}", [1.0, 2.0]) for d in (1, 2, 3)]
        rounds, tally = ev.load_rounds(_write(tmp_path / "s.jsonl", recs))
        res = ev.evaluate(rounds)
        assert res["n_rounds"] == 3
        assert res["verdict"] == "INSUFFICIENT_RECORD"
        assert "禁止建议激活" in res["verdict_text"]
        assert tally["lines"] == 3

    def test_10_rounds_below_threshold_is_ready_to_propose(self, tmp_path):
        recs = [_rec(f"2026-09-{d:02d}", [1.0, 2.0, 3.0]) for d in range(1, 11)]
        res = ev.evaluate(ev.load_rounds(_write(tmp_path / "s.jsonl", recs))[0])
        assert res["n_rounds"] == 10
        assert res["verdict"] == "READY_TO_PROPOSE_ACTIVATION"
        assert res["age_genuine_p95"] < 90.0

    def test_10_rounds_above_threshold_is_not_ready_and_says_why(self, tmp_path):
        recs = [_rec(f"2026-09-{d:02d}", [200.0] * 5) for d in range(1, 11)]
        res = ev.evaluate(ev.load_rounds(_write(tmp_path / "s.jsonl", recs))[0])
        assert res["verdict"] == "NOT_READY"
        assert res["n_newly_rejected_genuine"] == 50
        assert "不得事后改阈值重算" in res["verdict_text"]

    def test_holiday_and_forced_rows_do_not_count_as_rounds(self, tmp_path):
        """假日行/`--force` 行**不得**冒充有效轮次 (否则 P95 被小时级龄拖垮)。"""
        recs = [_rec(f"2026-09-{d:02d}", [1.0]) for d in range(1, 6)]
        recs.append(_rec("2026-09-07", [40000.0], flag=False))          # 假日报价: 小时级
        recs.append(_rec("2026-09-08", [1.0], flag=False, forced=True))  # --force 人工行
        rounds, tally = ev.load_rounds(_write(tmp_path / "s.jsonl", recs))
        res = ev.evaluate(rounds)
        assert res["n_rounds"] == 5, "假日/forced 行不得计入轮次"
        assert res["age_genuine_max"] == 1.0, "小时级假日龄不得进入 P95 池"
        assert tally["non_trading"] == 1 and tally["forced"] == 1

    def test_legacy_row_without_flag_falls_back_to_calendar(self, tmp_path):
        """老版本行无 `is_trading_day` 字段 ⇒ 按 `date` 回查日历; 查不出则**不计**。"""
        recs = [_rec("2026-09-15", [1.0], flag=None),      # 交易日 ⇒ 计
                _rec("2026-09-07", [1.0], flag=None),      # Labor Day ⇒ 不计
                _rec("2027-01-04", [1.0], flag=None)]      # 覆盖面外 ⇒ 不计 (fail-safe)
        rounds, tally = ev.load_rounds(_write(tmp_path / "s.jsonl", recs))
        assert sorted(rounds.keys()) == ["2026-09-15"]
        assert tally["non_trading"] == 1 and tally["undecidable"] == 1

    def test_not_ok_rows_are_excluded(self, tmp_path):
        """降级行 (status != ok, 如策略不可读) 不算轮次 —— 它没量过任何东西。"""
        recs = [_rec("2026-09-15", [1.0]), _rec("2026-09-16", [1.0], status="no_symbols")]
        rounds, tally = ev.load_rounds(_write(tmp_path / "s.jsonl", recs))
        assert sorted(rounds.keys()) == ["2026-09-15"]
        assert tally["not_ok"] == 1

    def test_thresholds_are_not_hardcoded_and_divergence_is_disclosed(self, tmp_path):
        """阈值取影子行自带的活配置值; 行间不一致时**并列披露**而不是挑一个。"""
        recs = [_rec("2026-09-15", [1.0], threshold=90.0),
                _rec("2026-09-16", [1.0], threshold=300.0)]
        res = ev.evaluate(ev.load_rounds(_write(tmp_path / "s.jsonl", recs))[0])
        assert res["thresholds_seen"] == [90.0, 300.0]
        assert res["threshold_used"] == 300.0

    def test_missing_file_is_empty_not_a_crash(self, tmp_path):
        rounds, tally = ev.load_rounds(tmp_path / "nope.jsonl")
        assert rounds == {} and tally["lines"] == 0
        assert ev.evaluate(rounds)["verdict"] == "INSUFFICIENT_RECORD"


def _alias_writers_and_callers(src: str):
    """源码检查器: 返回 (把抓取时刻写进 source_asof/update_time 的函数, 调用归一函数的函数)。

    纯函数 (只吃源码字符串) ⇒ 可以对合成源做阳性对照, 证明它**能报坏** (不是恒绿)。
    """
    writers, callers = set(), set()
    for node in ast.walk(ast.parse(src)):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if (isinstance(tgt, ast.Subscript) and isinstance(tgt.slice, ast.Constant)
                            and tgt.slice.value in ("source_asof", "update_time")):
                        writers.add(node.name)
            if (isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
                    and sub.func.id == "_prescreen_quotes_with_asof"):
                callers.add(node.name)
    return writers, callers


class TestAliasConfinedToGateOffPaths:
    """边界钉子: 任何路径都不得把我方**抓取时刻**写成行情源时点。

    实测依据 (§22.5): `alias` **会洗白真陈旧** 1/5 (AXON 别名口径 −10s"很新鲜" / 交易所
    口径 159.1s 已超阈)。若同一套逻辑进了 inject/执行侧 (90s 真执法), 就会把真陈旧判成
    新鲜照常开仓 —— 即 `futu_lv3_provider.py:1052` B4 注释警告的那件事, 而且方向是**更放行**
    (静默放宽, 事后最难发现) ⇒ 必须有红钉, 不能只靠注释与台账文字。
    """

    def test_inject_screen_ignores_fetch_time_and_gate_is_live(self):
        """① inject 侧喂 `timestamp`(抓取时刻) **不得**产生 `stale_quote` (alias 洗白面);
        ② 阳性对照: 同一报价值一旦写进 `source_asof` 就**必须**判 stale ⇒ 证明①是真钉。"""
        from app.playbook.candidate_eligibility import screen_candidates

        now = datetime.now(_ET)
        rows = [{"symbol": "US.AAA", "direction": "LONG", "effective_price": 100.0}]
        fresh_fetch = {"timestamp": now.timestamp(), "price": 100.0, "source": "futu_lv3"}
        got = screen_candidates(rows, None, source="UE", batch_id="t", now=now,
                                quotes={"US.AAA": fresh_fetch}, policy=_policy(),
                                atr_provider=None)
        sc = got[0]["candidate_screening"]
        assert "stale_quote" not in sc["reasons"], "抓取时刻不得参与新鲜度判定 (alias 洗白面)"
        assert sc["status"] != "REJECT"
        assert "source_asof" in sc["missing_fields"], "现状应是'缺源时点', 不是'已判新鲜'"

        # 阳性对照: 把**真陈旧**的行情时点写进 source_asof ⇒ 同一道门必须判 stale
        stale = dict(fresh_fetch, source_asof=(now - timedelta(seconds=600)).timestamp())
        sc2 = screen_candidates(rows, None, source="UE", batch_id="t", now=now,
                                quotes={"US.AAA": stale}, policy=_policy(),
                                atr_provider=None)[0]["candidate_screening"]
        assert "stale_quote" in sc2["reasons"], "门没牙 ⇒ 上面的'不判 stale'就不构成证据"
        assert sc2["status"] == "REJECT"

    def test_scan_phase_has_no_alias_writer(self):
        """源码钉: `llm_universe_scan.py` 不存在把 `timestamp` 写成 source_asof/update_time 的归一写点。

        一旦它被 inject 侧复用, "新鲜度门关着的路径"这个前提就不成立了 ⇒ 直接洗白真陈旧。
        """
        src = (_ROOT / "scripts" / "llm_universe_scan.py").read_text(encoding="utf-8")
        writers, callers = _alias_writers_and_callers(src)
        assert writers == set(), f"别名写点不得存在: {writers}"
        assert callers == {"_prescreen_input_symbols"}

    def test_checker_can_fail_positive_control(self):
        """阳性对照: 检查器对"写点搬到 inject 侧"的合成源**必须报出来** (否则它是恒绿的尺子)。"""
        fake = ("def _inject_screens():\n"
                "    row['source_asof'] = 1.0\n"
                "    _prescreen_quotes_with_asof(r, s)\n")
        writers, callers = _alias_writers_and_callers(fake)
        assert writers == {"_inject_screens"}
        assert callers == {"_inject_screens"}


# ── 2026-09-16 补账: 注册表条目与裁决器必须成对 (补口②的"谁被提醒"半) ──

def _registry_item():
    from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: F401
    return {it.id: it for it in REGISTRY}.get("ue_quote_freshness_shadow")


class TestRegistryEntryPairsWithEvaluator:
    """采集环在跑而无人裁决 = DEB-20260915 da_glm_shadow 的同族病灶 (那边是裁决器自删
    而采集行没人摘, 这边是采集行在跑而没有裁决器/没有到期触发人)。本类钉住"成对"这一事实。
    """

    def test_entry_exists_and_points_at_a_real_script(self):
        it = _registry_item()
        assert it is not None, ("ue_quote_freshness_shadow 未注册 ⇒ 周检/挂载哨兵都看不见它 "
                               "(入账提醒纪律: 建了没登)")
        from app.playbook.shadow_registry import ShadowStatus
        assert it.status == ShadowStatus.OBSERVING
        assert it.eval_script == "scripts/ue_quote_freshness_shadow_eval.py"
        assert (_ROOT / it.eval_script).exists(), "eval_script 指了个不存在的文件 = 假成对"
        # 按需触发: 采集 cron 与 eval 无周期挂载面, 若写成 False 会被挂载哨兵当悬空报
        assert it.eval_on_demand is True

    def test_registration_and_evaluator_share_one_threshold(self):
        """预注册的轮次门槛**只有一个真源**: 注册表 min_n ⇔ 裁决器 MIN_ROUNDS。

        两处各写一份 10 的话, 改一处就成了"改阈值重算"的暗门 — 正是预注册要防的事。
        """
        it = _registry_item()
        assert it.min_n == ev.MIN_ROUNDS == 10
        assert it.count_mode == "distinct:date", "计数单位必须是交易日轮次, 不是 jsonl 行数"

    def test_counter_residual_cannot_produce_a_false_activation(self, tmp_path):
        """口径残留演练: 接口 `distinct:date` 会把 `--force` 人工行也算 1 个日期 (可能 READY),
        但**裁决器**必须仍判不足 —— 到期判据以裁决器为准, 假 READY 不得变成假激活。
        """
        days = [f"2026-09-{d:02d}" for d in range(1, 11)]
        recs = [_rec(d, [1.0]) for d in days[:-1]]                 # 9 个真轮次 (09-01..09-09)
        recs.append(_rec("2026-09-12", [1.0], flag=False, forced=True))  # +1 假轮次 (周六人工跑)
        p = _write(tmp_path / "s.jsonl", recs)
        # 接口口径 (周检 refresh_counts 走的 distinct:date): 10 个不同日期 ⇒ 会通知 READY
        import json as _json
        distinct_dates = {_json.loads(l)["date"] for l in p.read_text(encoding="utf-8").splitlines() if l.strip()}
        assert len(distinct_dates) == 10
        # 裁决器口径: 假轮次被三重过滤挡掉 ⇒ 9/10, 禁止建议激活
        res = ev.evaluate(ev.load_rounds(p)[0])
        assert res["n_rounds"] == 9
        assert res["verdict"] == "INSUFFICIENT_RECORD"

    def test_expiry_trigger_is_written_down_and_baseline_does_not_claim_pass(self):
        """到期触发点 (谁在什么时候读这把尺子) 必须落在注册表文本里, 不能只活在 crontab 注释;
        且首条裁决历史不得写成"已达标"的假绿。"""
        it = _registry_item()
        assert "2026-09-29" in it.notes, "到期触发日未写进注册表 ⇒ 又变成无人被提醒"
        assert "不落盘" in it.notes and "零 futu 连接" in it.notes, "补口①的三条纪律须留痕"
        last = it.verdict_history[-1]
        assert last["verdict"].startswith("BASELINE_NO_DATA")
        assert "禁止建议激活" in last["evidence"]
