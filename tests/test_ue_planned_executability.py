"""[P3.3-3.5 2026-09-13] 生成期估算股数 / 费用覆盖 / 可执行性 — proud-slope-falcon.

被测对象: `app/playbook/planned_executability.py` (新) + `scripts/llm_universe_scan.py`
的批级上下文读取与 `_planned_exec_block()` 接线。

立项依据 (计划 P3 第二批 + DEB-20260910 §20 只读取证):
  ① **估算股数的口径必须先定**。计划明禁"逐候选调用会读取资本状态的完整
     compute_instruction_values()" ⇒ 本批口径 = 每批读一次权益 + 逐候选跑纯函数
     `compute_position_size()`。实测 (生产库 mode=ro, 近 8 条已触发 UE 行) 预览股数与
     真实委托 **7/8 逐位吻合**, 第 8 条差 1 股 (US.M 278 vs 279, 逐日净值漂移:
     该行需要 equity≥$98,720, 今日 capital_state 是 $98,657.999)。
  ② **UE 股数由名义上限绑定, 不是由风险预算绑定**: 8/8 行 `size_basis =
     max_position_pct_cap`。因为 SL 宽度 (BUY 4.5% / SHORT 5.5%) 使预算名义
     = equity×0.005/0.045 = 11.1% > 6% 帽 ⇒ `risk_pct=0.5%` 在当前几何下**不生效**。
     不记 size_basis, 库里的 risk_amount 派生列会被误读成风险预算。
  ③ **`fees_source='est'` 不覆盖借券**: `pipeline.py:6596` 写的 fees_est 来自
     `estimate_round_trip` (佣金/交收/SEC/TAF/CAT 五项), 借券是独立函数
     `accrue_borrow_fee`, 生产账路径不调它 (全仓只有 `scripts/e1_shadow_settle.py` 调)。
     实测量级: 41 条实际隔夜 SHORT (ET 本地日口径) 名义 $1,718,710, 借券 @$3% 中位
     = $162.97 (0.95bps) = fees_est 的 28.7%, 使 pnl_net 高估 1.49%。
  ④ **252/365 三重不一致**: CostModel 是 `fraction年率 × 分数持有交易日/252`
     (`cost_model.py:177`), fee_model 是 `percent年率 × int(自然日)/365`
     (`fee_model.py:130-140`) ⇒ 同 2%/5天 两边差 365/252=1.4484 倍。CostModel 活在
     **退出路径** (`trailing_stop.py:151`), 计划明禁改退出参数 ⇒ 本批两套数都记、谁也不改谁。

回滚: 删本文件 + 删 app/playbook/planned_executability.py + 按块删 UE 侧四处插入
(见 DEB §20 回滚段)。
"""
import ast
import hashlib
import inspect
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timedelta

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook import planned_executability as pe  # noqa: E402
from scripts import llm_universe_scan as ue  # noqa: E402

# ── 生产实测夹具 (data/ftseek_us.db mode=ro, 2026-09-13 取证, DEB §20.3) ──
# capital_state 最新行: current_equity=98657.999 / water_level=green / trade_env=1
EQUITY = 98657.999
CAP_NOTIONAL = EQUITY * 0.06          # = 5919.47994
PROD_ROWS = [
    # (symbol, direction, entry, stop_loss, 真实 position_size, 真实 risk_amount)
    ("US.UBER", "BUY", 72.96, 69.68, 81, 265.68),
    ("US.ADBE", "SELL_SHORT", 245.96, 259.49, 24, 324.72),
    ("US.QCOM", "BUY", 181.27, 173.11, 32, 260.99),
    ("US.STX", "BUY", 896.80, 851.96, 6, 269.04),
    ("US.BIDU", "SELL_SHORT", 91.85, 96.90, 64, 323.20),
    ("US.TTD", "SELL_SHORT", 15.05, 15.88, 393, 326.19),
]

DDL = """CREATE TABLE capital_state (
    id INTEGER PRIMARY KEY, initial_capital REAL, current_equity REAL, peak_equity REAL,
    cash REAL, market_value REAL, buying_power REAL, drawdown_pct REAL,
    max_drawdown_pct REAL, water_level TEXT, is_locked INTEGER, lock_reason TEXT,
    last_update TEXT, trade_env INTEGER)"""


def _capital_db(tmp_path, equity=EQUITY, water="green", env=1, stamp=None):
    path = tmp_path / "cap.db"
    conn = sqlite3.connect(str(path))
    conn.execute(DDL)
    conn.execute(
        "INSERT INTO capital_state (initial_capital, current_equity, peak_equity, cash,"
        " market_value, buying_power, drawdown_pct, max_drawdown_pct, water_level,"
        " is_locked, lock_reason, last_update, trade_env) VALUES (?,?,?,?,?,?,?,?,?,0,'',?,?)",
        (77806.99, equity, equity, 90123.64, 1256.79, 186052.55, -0.0047, -0.0333,
         water, stamp if stamp is not None else datetime.now().isoformat(), env))
    conn.commit()
    conn.close()
    return str(path)


def _ctx(**over):
    base = {
        "state": "ok", "equity": EQUITY, "equity_origin": "capital_state_db",
        "equity_stale": False, "water_level": "green", "water_mult": 1.0,
        "risk_pct": 0.5, "max_position_pct": 0.06, "risk_base_capital": 82000.0,
        "cautious_mult": 1.0, "confidence_sizing_enabled": False,
        "model_disagreement_downsize_factor": 1.0, "min_confidence": 60.0,
        "regime": "RISK_ON", "notes": [],
    }
    base.update(over)
    return base


def _borrow_file(tmp_path, rows):
    path = tmp_path / "borrow.jsonl"
    with open(path, "w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")
    return str(path)


# ══ A. 权益上下文: 每批一次、只读、带 provenance ══════════════════════════

class TestSizingContext:
    def test_reads_equity_from_capital_state(self, tmp_path):
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path))
        assert ctx["state"] == "ok"
        assert ctx["equity"] == EQUITY
        assert ctx["equity_origin"] == "capital_state_db"
        assert ctx["water_level"] == "green" and ctx["water_mult"] == 1.0

    def test_config_values_come_from_live_ssot(self, tmp_path):
        """反分叉钉: 阈值必须与执行侧同一 accessor 读出的活值一致, mock 不可满足。

        执行侧 sizing 用的是 `pb_cfg.get("risk_pct"/"max_position_pct")`
        (`pipeline.py:4128-4130`)。这里断言**活配置**的值, 而不是我抄一份常量 —
        配置一改, 预览口径自动跟随 (与 test_ue_low_atr_precheck.py::TestNoForkNail 同型)。
        """
        from app.playbook.config import get_playbook_config
        cfg = get_playbook_config()
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path))
        assert ctx["risk_pct"] == float(cfg.get("risk_pct", 0.5))
        assert ctx["max_position_pct"] == float(cfg.get("max_position_pct", 0.06))
        assert ctx["max_position_pct"] == 0.06 and ctx["risk_pct"] == 0.5

    def test_trade_env_mismatch_refuses_saved_equity(self, tmp_path):
        """与 SSOT 同形 (capital_safety.py:761-767): 环境切换过 ⇒ 旧净值不可用。"""
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path, env=0),
                                     expected_trade_env=1)
        assert ctx["equity_origin"] == "config_risk_base_capital"
        assert ctx["equity"] == 82000.0
        assert any("trade_env_mismatch" in n for n in ctx["notes"])
        assert ctx["state"] == "degraded"

    def test_empty_table_falls_back_with_note(self, tmp_path):
        path = tmp_path / "empty.db"
        conn = sqlite3.connect(str(path))
        conn.execute(DDL)
        conn.commit()
        conn.close()
        ctx = pe.read_sizing_context(db_path=str(path))
        assert "capital_state_empty" in ctx["notes"]
        assert ctx["equity_origin"] == "config_risk_base_capital"

    def test_unreadable_db_degrades_without_raising(self, tmp_path):
        ctx = pe.read_sizing_context(db_path=str(tmp_path / "nope.db"))
        assert ctx["state"] == "degraded"
        assert any(n.startswith("capital_state_unreadable") for n in ctx["notes"])
        assert ctx["equity"] is not None      # 退化到配置兜底, 不是 None

    def test_non_positive_equity_falls_back(self, tmp_path):
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path, equity=0.0))
        assert ctx["equity_origin"] == "config_risk_base_capital"
        assert any("non_positive" in n for n in ctx["notes"])

    def test_stale_equity_is_flagged_not_hidden(self, tmp_path):
        old = (datetime.now() - timedelta(hours=pe.EQUITY_STALE_HOURS + 5)).isoformat()
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path, stamp=old))
        assert ctx["equity_stale"] is True
        assert ctx["state"] == "degraded"
        assert ctx["equity_origin"] == "capital_state_db"   # 仍记来源, 只是标陈旧

    def test_future_stamp_is_stale(self, tmp_path):
        """未来时间戳 = 时钟/口径错位, 不能当"很新鲜"。"""
        future = (datetime.now() + timedelta(hours=3)).isoformat()
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path, stamp=future))
        assert ctx["equity_stale"] is True

    def test_age_uses_writer_clock_not_et(self, tmp_path):
        """钉住 CST/ET 陷阱: `last_update` 由 `datetime.now()` 写 (capital_safety.py:180)
        = 服务器本地朴素时间, 而本仓其余口径用 now_et()。若拿 now_et() 算年龄会凭空多
        12 小时 ⇒ 每条快照都被误判 stale。故刚写下的时间戳年龄必须≈0。
        """
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path))
        assert ctx["equity_age_hours"] is not None
        assert abs(ctx["equity_age_hours"]) < 0.5

    def test_never_instantiates_capital_safety_system(self):
        """ CapitalSafetySystem.__init__ 会 `_ensure_db_table()` 开**写**连接
        (capital_safety.py:139/699-705)。cron fork 的 UE 进程不该往生产库引入新写连接
        ⇒ 本模块必须只读 URI 直取, 这条钉住该决定 (改回去就红)。
        """
        src = inspect.getsource(pe)
        # 用 AST 判定 (docstring 里**必须**解释这个决定, 故不能靠字符串搜类名):
        #   ① 不 import capital_safety ⇒ 结构上不可能实例化;
        #   ② 代码里没有对 get_capital_safety_system / _ensure_db_table 的调用。
        tree = ast.parse(src)
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any("capital_safety" in mod for mod in imported), imported
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    called.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    called.add(node.func.attr)
        assert "get_capital_safety_system" not in called
        assert "_ensure_db_table" not in called
        assert "mode=ro" in src

    def test_module_writes_no_sql(self):
        src = inspect.getsource(pe)
        for verb in ("INSERT", "UPDATE ", "DELETE ", "CREATE TABLE", "ALTER TABLE"):
            assert verb not in src, f"record-only 模块不得含 {verb}"


# ══ B. 乘数 parity (不许自己复刻一张表) ════════════════════════════════════

class TestMultiplierParity:
    @staticmethod
    def _ssot_mult(tmp_path, level):
        from app.risk.capital_safety import CapitalSafetySystem, WaterLevel
        system = CapitalSafetySystem(db_path=str(tmp_path / f"cs_{level}.db"), trade_env=1)
        system._state.water_level = WaterLevel(level)   # 枚举 value 是小写
        return min(system.get_position_multiplier(), system.get_risk_multiplier())

    @pytest.mark.parametrize("level", ["green", "yellow", "orange", "red", "blackout"])
    def test_water_multiplier_matches_capital_safety_via_yaml(self, tmp_path, level):
        """对 5 个水位逐个比对 SSOT 的 min(position_multiplier, risk_multiplier)。

        执行侧写的是 `min(cs.get_position_multiplier(), cs.get_risk_multiplier())`
        (`pipeline.py:4141`); SSOT 的两个映射来自 `risk.capital_safety.water_levels.<level>`
        (`capital_safety.py:655-692`)。本模块读**同一 yaml 节点**, 这条钉住两者不漂移 —
        漂移会让预览股数与真实委托系统性偏差, 且从账上看不出来。
        """
        from app.core.config_container import get_config
        cs_cfg = get_config().get_dotted("risk.capital_safety", {}) or {}
        assert cs_cfg.get("water_levels"), "活配置里读不到 water_levels ⇒ 本条失去意义"
        assert pe.water_multiplier(level, cs_cfg) == self._ssot_mult(tmp_path, level)

    @pytest.mark.parametrize("level", ["green", "yellow", "orange", "red", "blackout"])
    def test_builtin_defaults_also_match(self, tmp_path, level):
        """缺省表也要对得上: yaml 节点缺失时本模块退回 `_WATER_DEFAULTS`,
        若有人只改 yaml 不改缺省表, 这条会红 (两侧今天同值, 故都能过)。
        """
        assert pe.water_multiplier(level, None) == self._ssot_mult(tmp_path, level)

    def test_water_multiplier_unknown_level_is_zero(self):
        """与 SSOT 同形: `multipliers.get(level, 0.0)` — 不认识的水位不给仓位。"""
        assert pe.water_multiplier("purple") == 0.0
        assert pe.water_multiplier(None) == 0.0
        assert pe.water_multiplier("") == 0.0

    @pytest.mark.parametrize("name,expected", [
        ("RISK_ON", 1.0), ("risk_on", 1.0), ("RISK_OFF", 0.3), ("NO_TRADE", 0.0),
        ("SOMETHING_ELSE", 0.5), (None, 0.5), ("", 0.5),
    ])
    def test_regime_multiplier_table(self, name, expected):
        assert pe.regime_multiplier(name, {"cautious_mult": 1.0}) == expected

    def test_cautious_uses_configured_mult(self):
        """2026-08-06 用户指示 sim 采集期 CAUTIOUS 不降仓 (现值 1.0); 改回 0.5 也跟随。"""
        assert pe.regime_multiplier("CAUTIOUS", {"cautious_mult": 1.0}) == 1.0
        assert pe.regime_multiplier("CAUTIOUS", {"cautious_mult": 0.5}) == 0.5


# ══ C. 估算股数 ═══════════════════════════════════════════════════════════

class TestEstimateSize:
    @pytest.mark.parametrize("symbol,direction,entry,sl,real_size,real_risk", PROD_ROWS)
    def test_reproduces_production_order_size(self, symbol, direction, entry, sl,
                                              real_size, real_risk):
        """预览股数必须复现真实委托股数 (7/8 逐位吻合的那 6 条 + QCOM/TTD 等)。

        夹具是生产库里已触发 UE 行的 (entry, stop_loss, position_size) 三元组 —
        即**重定价后**的值。用今日 capital_state 净值复算: 上限绑定时预览与真实一致。
        """
        out = pe.estimate_size(entry=entry, stop_loss=sl, direction=direction,
                               ctx=_ctx(), confidence=70)
        assert out["state"] == "computed"
        assert out["size"] == real_size, f"{symbol} 预览 {out['size']} ≠ 真实 {real_size}"

    @pytest.mark.parametrize("symbol,direction,entry,sl,real_size,real_risk", PROD_ROWS)
    def test_risk_amount_derived_matches_db_column(self, symbol, direction, entry, sl,
                                                   real_size, real_risk):
        """`risk_amount` 是**派生列** = |entry−SL|×size (`pipeline.py:4253`), 不是风险预算。

        预览记同定义的 `risk_amount_derived` ⇒ 可直接与 DB 列对账。DB 列用未四舍五入的
        SL 算, 夹具里的 SL 是落库的 2dp 值 ⇒ 允许 1 分位以内差 (QCOM 实测 260.99 vs 261.12)。
        """
        out = pe.estimate_size(entry=entry, stop_loss=sl, direction=direction,
                               ctx=_ctx(), confidence=70)
        assert out["risk_amount_derived"] == pytest.approx(out["size"] * abs(entry - sl), abs=0.01)
        assert out["risk_amount_derived"] == pytest.approx(real_risk, abs=0.15)

    @pytest.mark.parametrize("symbol,direction,entry,sl,real_size,real_risk", PROD_ROWS)
    def test_size_basis_is_cap_not_risk_budget(self, symbol, direction, entry, sl,
                                               real_size, real_risk):
        """DEB §20.3 的实测结论: UE 股数由名义上限绑定, risk_pct 预算项不生效。

        预算名义 = equity×0.005/sl_pct。SL 宽 4.5%⇒11.1%equity, 5.5%⇒9.1%equity, 都 > 6% 帽。
        不记这个归因, 读账的人会以为 0.5% 风险预算在起作用 (它没有)。
        """
        out = pe.estimate_size(entry=entry, stop_loss=sl, direction=direction,
                               ctx=_ctx(), confidence=70)
        assert out["size_basis"] == "max_position_pct_cap"
        assert out["truncated_by_cap"] is True
        assert out["notional"] <= CAP_NOTIONAL + 1.0
        assert out["risk_size_uncapped"] > out["size"]

    def test_risk_budget_binds_when_regime_shrinks(self):
        """RISK_OFF (0.3) 把预算名义压到 3.3%equity < 6% 帽 ⇒ 换 risk_budget 绑定。

        这条钉住 size_basis 不是常量: 乘数一变, 归因必须跟着变。
        """
        out = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY",
                               ctx=_ctx(regime="RISK_OFF"), confidence=70)
        assert out["size_basis"] == "risk_budget"
        assert out["truncated_by_cap"] is False
        assert out["size"] == out["risk_size_uncapped"]

    @pytest.mark.parametrize("entry,sl,direction,mults", [
        (72.96, 69.68, "BUY", {}), (245.96, 259.49, "SELL_SHORT", {}),
        (10.0, 9.0, "BUY", {"regime": "CAUTIOUS", "cautious_mult": 0.5}),
        (10.0, 11.0, "SELL_SHORT", {"water_mult": 0.25, "water_level": "orange"}),
        (896.80, 851.96, "BUY", {"regime": "NO_TRADE"}),
        (5.0, 4.5, "BUY", {"max_position_pct": 0.0}),
    ])
    def test_ssot_parity_with_compute_position_size(self, entry, sl, direction, mults):
        """归因可以和 SSOT 分开算, **股数不行** — 必须逐位等于 compute_position_size。"""
        from app.playbook.instruction_generator import compute_position_size
        ctx = _ctx(**mults)
        out = pe.estimate_size(entry=entry, stop_loss=sl, direction=direction,
                               ctx=ctx, confidence=70)
        regime_mult = pe.regime_multiplier(ctx.get("regime"), ctx)
        expected = compute_position_size(
            ctx["equity"], ctx["risk_pct"], entry, sl, ctx["max_position_pct"],
            regime_mult=regime_mult, water_mult=ctx["water_mult"], conf_factor=1.0)
        assert out["size"] == expected

    def test_is_never_a_real_position(self):
        """计划 P3「预估股数不写成真实仓位」的钉子。"""
        out = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY", ctx=_ctx())
        assert out["is_real_position"] is False
        assert "position_size" not in out

    @pytest.mark.parametrize("bad", [0, -1.0, None, float("nan"), float("inf"), "x", True])
    def test_invalid_entry_is_not_computed(self, bad):
        """P3.1 抓到的同类缺陷 (NaN 穿过 `e<=0`) 在 sizing 侧也必须挡住。"""
        out = pe.estimate_size(entry=bad, stop_loss=69.68, direction="BUY", ctx=_ctx())
        assert out["state"] == "not_computed_invalid_price"
        assert out["size"] is None

    @pytest.mark.parametrize("bad", [0, None, float("nan"), float("inf")])
    def test_invalid_stop_is_not_computed(self, bad):
        out = pe.estimate_size(entry=72.96, stop_loss=bad, direction="BUY", ctx=_ctx())
        assert out["state"] == "not_computed_invalid_price"

    def test_zero_risk_distance_is_not_computed(self):
        out = pe.estimate_size(entry=72.96, stop_loss=72.96, direction="BUY", ctx=_ctx())
        assert out["state"] == "not_computed_zero_risk_distance"

    def test_missing_equity_is_not_computed(self):
        out = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY",
                               ctx=_ctx(equity=None))
        assert out["state"] == "not_computed_no_equity"

    def test_geometry_recorded_but_not_enforced(self):
        """BUY 的 SL 在入场**上方** = 几何非法 (§18.5 实测 6 行)。本批只记不拒 —
        拒绝闸是 P3 第三批 (计划要求影子先行, 不与审计字段同批)。
        """
        out = pe.estimate_size(entry=72.96, stop_loss=80.0, direction="BUY", ctx=_ctx())
        assert out["geometry_ok"] is False
        assert out["state"] == "computed" and out["size"] is not None

    def test_short_geometry_ok(self):
        out = pe.estimate_size(entry=245.96, stop_loss=259.49, direction="SELL_SHORT", ctx=_ctx())
        assert out["geometry_ok"] is True

    def test_confidence_sizing_off_keeps_factor_neutral(self):
        """2026-07-12 起 confidence 加权默认关闭 (conf 未校准且反向) ⇒ 低 conf 不缩仓。"""
        low = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY",
                               ctx=_ctx(), confidence=10)
        high = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY",
                                ctx=_ctx(), confidence=90)
        assert low["multipliers"]["conf_factor"] == 1.0
        assert low["size"] == high["size"]

    def test_disagreement_downsize_applies_to_buy_only(self):
        """执行侧同形 (pipeline.py:4181-4195): BUY 且 conf<50 才乘降仓因子, 现值 1.0。"""
        ctx = _ctx(model_disagreement_downsize_factor=0.5)
        buy = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY",
                               ctx=ctx, confidence=35)
        short = pe.estimate_size(entry=245.96, stop_loss=259.49, direction="SELL_SHORT",
                                 ctx=ctx, confidence=35)
        assert buy["multipliers"]["conf_factor"] == 0.5
        assert short["multipliers"]["conf_factor"] == 1.0


# ══ D. 借券: 365 vs 252 显式转换, 未知不编造 ═════════════════════════════

class TestBorrow:
    def test_long_is_not_applicable(self):
        out = pe.estimate_borrow(size=81, entry=72.96, direction="BUY", symbol="US.UBER",
                                 horizon_hours=6, borrow_rates={})
        assert out["state"] == "not_applicable_long" and out["usd"] == 0.0

    def test_unknown_rate_is_not_assumed(self):
        """计划「借券未知不能当可借」: 快照没这个标的 (实测 US.XLF 就不在 258 个里)
        ⇒ 记 unknown, **不套用 3% 中位数当已知**。
        """
        out = pe.estimate_borrow(size=100, entry=50.0, direction="SHORT", symbol="US.XLF",
                                 horizon_hours=48, borrow_rates={})
        assert out["state"] == "unknown_rate"
        assert out["usd"] == 0.0 and out["rate_pct"] is None

    def test_planned_intraday_horizon_is_zero(self):
        """UE 声明的 horizon 以 4–8h 为主 (157/183 有值) ⇒ int(hold_days)=0 ⇒ 计划内 $0。"""
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11",
                             "shortable": True, "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=24, entry=245.96, direction="SHORT", symbol="US.ADBE",
                                 horizon_hours=6, borrow_rates=rates)
        assert out["state"] == "zero_planned_intraday"
        assert out["usd"] == 0.0
        assert out["hold_days"] == 0.25 and out["hold_days_origin"] == "model_horizon_hours"

    def test_overnight_uses_365_basis(self):
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11",
                             "shortable": True, "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=100, entry=50.0, direction="SHORT", symbol="US.ADBE",
                                 horizon_hours=24, borrow_rates=rates)
        assert out["state"] == "known"
        assert out["basis_days"] == 365
        assert out["usd"] == pytest.approx(100 * 50.0 * 0.03 * 1 / 365.0, abs=1e-4)

    def test_cost_model_basis_conversion_is_explicit(self):
        """计划要求「252/365 天口径必须显式转换」: 两套数都记, 且换算比钉死。

        CostModel 口径 = fraction年率 × **分数**持有交易日/252 (`cost_model.py:177`),
        fee_model 口径 = percent年率 × int(自然日)/365。整数天数下两者之比恰为 365/252。
        """
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11",
                             "shortable": True, "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=100, entry=50.0, direction="SHORT", symbol="US.ADBE",
                                 horizon_hours=48, borrow_rates=rates)
        assert pe.BORROW_BASIS_RATIO == pytest.approx(365.0 / 252.0)
        assert out["cost_model_basis_days"] == 252
        # 两个数都按 4dp 落盘 ⇒ 比值只在舍入精度内成立 (0.8219 vs 1.1905)。
        expected_cm = 100 * 50.0 * (3.0 / 100.0) * 2.0 / 252.0
        assert out["usd_if_cost_model_basis"] == pytest.approx(expected_cm, abs=1e-4)
        assert out["usd_if_cost_model_basis"] / out["usd"] == pytest.approx(
            pe.BORROW_BASIS_RATIO, rel=1e-3)

    def test_cost_model_overstates_typical_one_day_short(self):
        """DEB §20.5 的量级: 退出路径 CostModel 用 (2% × 5交易日/252) 常量借券项,
        而实测典型 UE SHORT 是 1 自然日 @3% ⇒ 3.968bps vs 0.822bps, 高估 ~4.8 倍。
        本批不改 CostModel (它在退出路径), 只把两个数都留档。
        """
        cost_model_bps = 0.02 * (5.0 / 252.0) * 1e4
        rates = {"US.X": {"rate_pct": 3.0, "rate_asof": "2026-09-11", "shortable": True,
                          "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=100, entry=100.0, direction="SHORT", symbol="US.X",
                                 horizon_hours=24, borrow_rates=rates)
        fee_model_bps = out["usd"] / (100 * 100.0) * 1e4
        assert cost_model_bps == pytest.approx(3.968, abs=0.001)
        assert fee_model_bps == pytest.approx(0.822, abs=0.001)
        assert cost_model_bps / fee_model_bps > 4.0

    def test_missing_horizon_is_unknown_hold(self):
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11", "shortable": True,
                             "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=24, entry=245.96, direction="SHORT", symbol="US.ADBE",
                                 horizon_hours=None, borrow_rates=rates)
        assert out["state"] == "unknown_hold"
        assert out["hold_days"] is None
        assert out["overnight_risk"] is True      # 不知道就当可能隔夜, 不当日内

    def test_stale_rate_is_labelled(self):
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-08-01", "shortable": True,
                             "stale": True, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=100, entry=50.0, direction="SHORT", symbol="US.ADBE",
                                 horizon_hours=24, borrow_rates=rates)
        assert out["state"] == "known_stale_rate" and out["rate_stale"] is True

    def test_shortable_propagated(self):
        rates = {"US.A": {"rate_pct": 3.0, "rate_asof": "2026-09-11", "shortable": False,
                          "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_borrow(size=10, entry=10.0, direction="SHORT", symbol="US.A",
                                 horizon_hours=24, borrow_rates=rates)
        assert out["shortable"] is False


class TestBorrowRateLoading:
    def test_latest_snapshot_per_symbol_wins(self, tmp_path):
        path = _borrow_file(tmp_path, [
            {"date": "2026-09-01", "symbol": "US.ADBE", "fee_rate": 3.0, "shortable": True},
            {"date": "2026-09-11", "symbol": "US.ADBE", "fee_rate": 4.5, "shortable": True},
            {"date": "2026-09-05", "symbol": "US.ADBE", "fee_rate": 9.0, "shortable": True},
        ])
        rates = pe.load_borrow_rates(path=path, asof=datetime(2026, 9, 13))
        assert rates["US.ADBE"]["rate_pct"] == 4.5
        assert rates["US.ADBE"]["rate_asof"] == "2026-09-11"

    def test_stale_flag_by_age(self, tmp_path):
        path = _borrow_file(tmp_path, [
            {"date": "2026-08-01", "symbol": "US.OLD", "fee_rate": 3.0, "shortable": True}])
        rates = pe.load_borrow_rates(path=path, asof=datetime(2026, 9, 13), max_age_days=7.0)
        assert rates["US.OLD"]["stale"] is True and rates["US.OLD"]["age_days"] == 43

    def test_malformed_lines_skipped(self, tmp_path):
        path = tmp_path / "bad.jsonl"
        path.write_text('{"date":"2026-09-11","symbol":"US.A","fee_rate":3.0}\nnot json\n\n',
                        encoding="utf-8")
        rates = pe.load_borrow_rates(path=str(path), asof=datetime(2026, 9, 13))
        assert list(rates) == ["US.A"]

    def test_missing_file_returns_empty(self, tmp_path):
        assert pe.load_borrow_rates(path=str(tmp_path / "none.jsonl")) == {}


# ══ E. 费用覆盖: 逐项 + 未知项, 不算净期望 ═══════════════════════════════

class TestCostCoverage:
    def test_items_match_fee_model_ssot(self):
        """反平行层钉: 分项必须逐值等于 fee_model.estimate_round_trip, 不自建费率公式。"""
        from app.execution.fee_model import estimate_round_trip
        out = pe.estimate_cost(entry=245.96, take_profit=225.67, size=24,
                               direction="SELL_SHORT", symbol="US.ADBE", horizon_hours=6,
                               trade_date="2026-09-11")
        rt = estimate_round_trip(qty=24, entry_price=245.96, exit_price=245.96,
                                 direction="SHORT", trade_date="2026-09-11")
        for key in ("commission", "settlement", "sec", "taf", "cat"):
            expected = rt["entry"][key] + rt["exit"][key]
            assert out["items_usd"][key] == pytest.approx(expected, abs=1e-4)
        assert out["total_cost_usd"] == pytest.approx(rt["total"], abs=1e-4)

    def test_total_includes_borrow_exactly_once(self):
        rates = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11", "shortable": True,
                             "stale": False, "origin": "borrow_rate_series_jsonl"}}
        out = pe.estimate_cost(entry=245.96, take_profit=225.67, size=24,
                               direction="SELL_SHORT", symbol="US.ADBE", horizon_hours=48,
                               borrow_rates=rates)
        fee_only = pe.estimate_cost(entry=245.96, take_profit=225.67, size=24,
                                    direction="SELL_SHORT", symbol="US.ADBE",
                                    horizon_hours=48, borrow_rates={})
        assert out["borrow"]["usd"] > 0
        assert out["total_cost_usd"] == pytest.approx(
            sum(out["items_usd"].values()) + out["borrow"]["usd"], abs=1e-3)
        assert out["total_cost_usd"] > fee_only["total_cost_usd"]
        assert "cost_model_not_used" in out["double_count_guard"]

    def test_unknown_items_make_cost_incomplete(self):
        """无 bid/ask ⇒ 点差未知; 无 ADV ⇒ 冲击未知。计划: 缺 bid/ask 不能按零点差算。"""
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                               symbol="US.UBER", horizon_hours=6)
        assert "spread" in out["unknown_items"] and "impact" in out["unknown_items"]
        assert out["cost_complete"] is False
        assert out["all_in_cost_usd"] is None
        assert out["spread_bps"] is None

    def test_spread_counted_when_quote_known(self):
        screening = {"bid": 72.90, "ask": 73.02}
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                               symbol="US.UBER", horizon_hours=6, screening=screening)
        assert "spread" not in out["unknown_items"]
        assert out["spread_bps"] == pytest.approx((73.02 - 72.90) / 72.96 * 1e4, abs=0.5)
        assert out["all_in_cost_usd"] == pytest.approx(
            out["total_cost_usd"] + out["liquidity_usd_equiv"], abs=1e-4)
        assert "impact" in out["unknown_items"]      # ADV 仍缺 ⇒ 还是不完整
        assert out["cost_complete"] is False

    def test_crossed_quote_is_not_a_spread(self):
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                               symbol="US.UBER", horizon_hours=6,
                               screening={"bid": 73.50, "ask": 72.00})
        assert "spread" in out["unknown_items"] and out["spread_bps"] is None

    def test_no_net_expectation_fabricated(self):
        """计划「缺成本数据不产生伪精确净期望」: 不许出现任何净期望/EV 键。"""
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                               symbol="US.UBER", horizon_hours=6)
        for banned in ("net_expectation", "expected_value", "ev_usd", "net_edge_bps",
                       "expected_net"):
            assert banned not in out

    def test_admission_ratio_parity_when_borrow_zero(self):
        """admission 的 profit_fee_ratio (口径=仅费率表) 与本模块 coverage_ratio 在
        借券为 0 时必须同值 ⇒ 证明两边同源, 不是两套费率。
        """
        from app.playbook import admission
        cost = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                                symbol="US.UBER", horizon_hours=6, trade_date="2026-09-11")
        res = admission.evaluate({"symbol": "US.UBER", "direction": "BUY",
                                  "date": "2026-09-11", "entry_price": 72.96,
                                  "stop_loss": 69.68, "take_profit": 81.2,
                                  "position_size": 81, "confidence": 70})
        assert cost["borrow"]["usd"] == 0.0
        assert cost["coverage_ratio"] == pytest.approx(res["metrics"]["profit_fee_ratio"], rel=1e-3)
        assert cost["total_cost_usd"] == pytest.approx(res["metrics"]["rt_fee"], abs=0.01)

    def test_no_size_is_not_computed(self):
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=None, direction="BUY",
                               symbol="US.UBER", horizon_hours=6)
        assert out["state"] == "not_computed_no_size_or_price"
        assert out["total_cost_usd"] is None

    def test_fee_model_error_degrades(self, monkeypatch):
        import app.execution.fee_model as fm

        def boom(*args, **kwargs):
            raise RuntimeError("fee table unavailable")

        monkeypatch.setattr(fm, "estimate_round_trip", boom)
        out = pe.estimate_cost(entry=72.96, take_profit=81.2, size=81, direction="BUY",
                               symbol="US.UBER", horizon_hours=6)
        assert out["state"] == "not_computed_fee_model_error"
        assert "fee table unavailable" in out["error"]


# ══ F. 可执行性: 复用纯函数 admission.evaluate, 生成期不执法 ═══════════════

class TestExecutability:
    """⚠ 本类**自带配置隔离**, 不读环境配置 —— 全量套件里实测会被污染:
    `admission.get_admission_config()` 在配置容器不可用时把 `_DEFAULTS`
    (`min_entry_price=0.0` / `min_notional_usd=0.0` / `enforce=False`) **写进模块级
    `_cfg_cache`**, 且容器恢复后不自愈 (只有 `refresh=True` 才拿回真值) ⇒ 依赖活配置的
    断言会在全量跑时静默变绿/变红。这正是 DEB §17.6 待裁⑤ 的形态, 也是
    `tests/test_ue_evidence_index.py::test_debt_marker_admission_cache_can_silently_disable_price_floor`
    钉住的那条债。生产阈值不漂移由本文件末尾的 yaml 直读钉单独保证。
    """

    # 与生产 yaml `playbook.admission` 同值 (下方 yaml 直读钉负责保证它没漂)
    PINNED = {"enabled": True, "enforce": True, "min_notional_usd": 2500.0,
              "min_entry_price": 5.0, "max_shares_per_order": 0,
              "min_profit_fee_ratio": 0.0, "max_sl_pct": 0.0}

    @pytest.fixture(autouse=True)
    def _isolate_admission_cfg(self):
        from app.playbook import admission as A
        saved = A._cfg_cache
        A._cfg_cache = dict(A._DEFAULTS, **self.PINNED)
        yield A._cfg_cache
        A._cfg_cache = saved

    def test_healthy_instruction_passes(self):
        out = pe.assess_executability(symbol="US.UBER", direction="BUY", entry=72.96,
                                      stop_loss=69.68, take_profit=81.2, size=81,
                                      confidence=70, trade_date="2026-09-11")
        assert out["state"] == "assessed" and out["verdict"] == "PASS"
        assert out["reasons"] == []

    def test_never_applies_the_gate(self):
        """计划 P3: 生成期不执法。真门是执行侧 admission.apply() (enforce=true 改 SKIP)。"""
        out = pe.assess_executability(symbol="US.CHEAP", direction="BUY", entry=1.0,
                                      stop_loss=0.9, take_profit=1.2, size=10,
                                      confidence=70, trade_date="2026-09-11")
        assert out["applied_here"] is False
        assert out["verdict"] == "REJECT"          # 判据照跑
        assert out["would_block_if_enforced"] is True

    def test_price_floor_surfaces(self):
        """min_entry_price=5.0 (yaml 直读钉保证与生产同值)。"""
        out = pe.assess_executability(symbol="US.Penny", direction="BUY", entry=2.5,
                                      stop_loss=2.3, take_profit=2.9, size=1000,
                                      confidence=70, trade_date="2026-09-11")
        assert any(r.startswith("PRICE_TOO_LOW") for r in out["reasons"])

    def test_min_notional_surfaces(self):
        """min_notional_usd=2500 且 enforce=true (yaml 直读钉保证与生产同值)。"""
        out = pe.assess_executability(symbol="US.TINY", direction="BUY", entry=10.0,
                                      stop_loss=9.5, take_profit=11.0, size=10,
                                      confidence=70, trade_date="2026-09-11")
        assert any(r.startswith("NOTIONAL_TOO_SMALL") for r in out["reasons"])
        assert out["enforce_at_execution"] is True

    def test_cap_bound_ue_size_clears_min_notional(self):
        """DEB §20.6: 上限绑定的 UE 名义 ≈ $5.9k, 远高于 $2500 地板 ⇒ 该判据对 UE 不绑定。
        这条把"为什么 UE 从没被 NOTIONAL_TOO_SMALL 拦过"钉成可复算的事实。
        """
        size = pe.estimate_size(entry=72.96, stop_loss=69.68, direction="BUY", ctx=_ctx())
        out = pe.assess_executability(symbol="US.UBER", direction="BUY", entry=72.96,
                                      stop_loss=69.68, take_profit=81.2,
                                      size=size["size"], confidence=70,
                                      trade_date="2026-09-11")
        assert out["metrics"]["notional"] > 2500
        assert not any(r.startswith("NOTIONAL_TOO_SMALL") for r in out["reasons"])

    def test_unknowns_merge_cost_and_screening(self):
        cost = {"unknown_items": ["spread", "impact", "borrow"]}
        screening = {"status": "UNKNOWN", "missing_fields": ["bid_ask", "spread"],
                     "reasons": []}
        out = pe.assess_executability(symbol="US.UBER", direction="BUY", entry=72.96,
                                      stop_loss=69.68, take_profit=81.2, size=81,
                                      confidence=70, trade_date="2026-09-11",
                                      screening=screening, cost=cost)
        assert out["unknowns"] == ["spread", "impact", "borrow", "bid_ask"]   # 去重且保序
        assert out["screening_status"] == "UNKNOWN"

    def test_admission_error_degrades(self, monkeypatch):
        from app.playbook import admission

        def boom(inst):
            raise ValueError("cfg broken")

        monkeypatch.setattr(admission, "evaluate", boom)
        out = pe.assess_executability(symbol="US.UBER", direction="BUY", entry=72.96,
                                      stop_loss=69.68, take_profit=81.2, size=81,
                                      confidence=70, trade_date="2026-09-11")
        assert out["state"] == "not_assessed_admission_error"
        assert "cfg broken" in out["error"]

    def test_module_has_no_private_thresholds(self):
        """反平行层钉: 阈值只能来自 admission, 本模块不得自带一份。

        一旦这里出现 `min_entry_price`/`min_notional_usd` 之类的字面量, 就有了第二套门 —
        计划 P3 要求"复用 admission.evaluate()", 不是"再实现一个 admission"。
        """
        src = inspect.getsource(pe)
        for banned in ("min_entry_price", "min_notional_usd", "max_shares_per_order",
                       "min_profit_fee_ratio", "PRICE_TOO_LOW", "NOTIONAL_TOO_SMALL"):
            assert banned not in src, f"模块内出现私有阈值/判据名: {banned}"

    def test_verdict_and_reasons_are_admissions_own(self, _isolate_admission_cfg):
        """判定必须**逐值**等于 admission.evaluate 的输出 (不是本模块另算一份)。"""
        from app.playbook import admission
        inst = {"symbol": "US.TINY", "direction": "BUY", "date": "2026-09-11",
                "entry_price": 10.0, "stop_loss": 9.5, "take_profit": 11.0,
                "position_size": 10, "confidence": 70}
        expected = admission.evaluate(inst)
        out = pe.assess_executability(symbol="US.TINY", direction="BUY", entry=10.0,
                                      stop_loss=9.5, take_profit=11.0, size=10,
                                      confidence=70, trade_date="2026-09-11")
        assert out["verdict"] == expected["verdict"]
        assert out["reasons"] == list(expected["reasons"])
        assert out["metrics"] == expected["metrics"]


class TestProductionConfigNotDrifted:
    """直读 yaml (不经 `_cfg_cache`) ⇒ 与测试执行顺序无关, 污染不到。"""

    @pytest.fixture(scope="class")
    def admission_yaml(self):
        import yaml
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "config", "unified_config.yaml"), encoding="utf-8") as handle:
            return yaml.safe_load(handle)["playbook"]["admission"]

    def test_gate_is_enforcing(self, admission_yaml):
        assert admission_yaml["enabled"] is True
        assert admission_yaml["enforce"] is True

    def test_thresholds_match_pinned_values(self, admission_yaml):
        """本文件 TestExecutability.PINNED 与生产 yaml 同值 — 漂了这里先红。"""
        assert float(admission_yaml["min_notional_usd"]) == 2500.0
        assert float(admission_yaml["min_entry_price"]) == 5.0
        assert int(admission_yaml["max_shares_per_order"]) == 0        # 关
        assert float(admission_yaml["min_profit_fee_ratio"]) == 0.0    # 关
        assert float(admission_yaml["max_sl_pct"]) == 0.0              # 关
        for key, value in TestExecutability.PINNED.items():
            assert key not in admission_yaml or admission_yaml[key] == value, key

    def test_sizing_config_matches_context_reader(self, tmp_path):
        """sizing 侧同理: 预览读的 risk_pct/max_position_pct 必须等于生产 yaml。"""
        import yaml
        root = os.path.join(os.path.dirname(__file__), "..")
        with open(os.path.join(root, "config", "unified_config.yaml"), encoding="utf-8") as handle:
            cfg = yaml.safe_load(handle)
        pb = cfg["playbook"]["execution_parameters"] if "execution_parameters" in cfg["playbook"] else cfg["playbook"]
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path))
        assert ctx["max_position_pct"] == 0.06
        assert ctx["risk_pct"] == 0.5
        assert float(pb.get("max_position_pct", ctx["max_position_pct"])) == ctx["max_position_pct"]


class TestNoBareLocalNow:
    """`tests/test_naive_datetime_now_ratchet.py` 的 BASELINE_NOW 只降不升, 且**纯文本**计数
    (连注释/docstring 里的字面量都算)。本模块的权益年龄必须与写入方同尺 (naive CST),
    做法是两边化 epoch, 而不是引入裸 now ⇒ 这里钉住零出现, 防日后"顺手加一个"。
    """

    def test_module_has_no_bare_now(self):
        src = inspect.getsource(pe)
        assert not re.search(r"(?<!\w)datetime\.now\(\)", src), "裸 now 会抬高 BASELINE_NOW"
        assert not re.search(r"\b(?:_dt|_datetime|_DT|dt)\.now\(\)", src), "别名裸 now 同样被棘轮咬"
        assert not re.search(r"(?<!\w)datetime\.utcnow\(\)", src)

    def test_equity_age_is_same_scale_as_writer(self, tmp_path):
        """写入方 `capital_safety.py:180` 用裸 now (= 服务器本地 CST 朴素串)。
        刚写下的快照年龄必须≈0; 若读侧改用 now_et() 相减会凭空 +12h ⇒ 恒判陈旧。
        """
        ctx = pe.read_sizing_context(db_path=_capital_db(tmp_path))
        assert abs(ctx["equity_age_hours"]) < 0.5
        assert ctx["equity_stale"] is False


# ══ G. 合成 + 接线契约 ═══════════════════════════════════════════════════

class TestBuildPlannedExecutability:
    P31_KEYS = ("state", "version", "value_origin", "direction", "entry", "entry_zone",
                "stop_loss", "take_profit", "risk_distance", "sl_pct_cfg", "tp_pct_cfg",
                "sl_pct_from_entry", "tp_pct_from_entry", "rr", "generated_at")

    def _preview(self, direction="LONG", entry=167.326):
        return ue._compute_numbers(entry, direction)["planned_preview"]

    def test_no_clobber_of_p31_keys(self):
        """P3.3-3.5 的附加段只能**加键**, 不能改 P3.1 那 15 个键的任何一个值。"""
        preview = self._preview()
        merged = {**preview, **pe.build_planned_executability(
            preview=preview, sizing_ctx=_ctx(), symbol="US.XOM", direction="BUY",
            entry=preview["entry"], stop_loss=preview["stop_loss"],
            take_profit=preview["take_profit"], confidence=70, horizon_hours=6,
            trade_date="2026-09-14", regime="RISK_ON")}
        for key in self.P31_KEYS:
            assert merged[key] == preview[key], f"P3.1 键 {key} 被改动"

    def test_three_sections_present(self):
        preview = self._preview()
        out = pe.build_planned_executability(
            preview=preview, sizing_ctx=_ctx(), symbol="US.XOM", direction="BUY",
            entry=preview["entry"], stop_loss=preview["stop_loss"],
            take_profit=preview["take_profit"], confidence=70, horizon_hours=6,
            trade_date="2026-09-14", regime="RISK_ON")
        assert out["exec_state"] == "generated"
        assert out["exec_basis_version"] == pe.PLANNED_EXEC_BASIS_VERSION
        assert out["size_estimate"]["size"] > 0
        assert out["cost_coverage"]["state"] == "computed"
        assert out["executability"]["state"] == "assessed"
        assert out["sizing_context"]["equity"] == round(EQUITY, 2)

    def test_no_preview_state_is_explicit(self):
        """§17.3-② 同型纪律: 不许靠"键不存在"表态。"""
        out = pe.build_planned_executability(
            preview={"state": "not_generated_invalid_band", "version": "ue_planned_v2"},
            sizing_ctx=_ctx(), symbol="US.X", direction="BUY", entry=1.0, stop_loss=0.9,
            take_profit=1.1, confidence=70, trade_date="2026-09-14")
        assert out["exec_state"] == "not_generated_no_preview"
        assert "size_estimate" not in out

    def test_unavailable_context_degrades_but_still_explicit(self):
        out = pe.build_planned_executability(
            preview=self._preview(), sizing_ctx={"state": "unavailable", "notes": ["x"]},
            symbol="US.XOM", direction="BUY", entry=167.326, stop_loss=157.437,
            take_profit=186.2338, confidence=70, trade_date="2026-09-14")
        assert out["exec_state"] == "generated"
        assert out["size_estimate"]["state"] == "not_computed_no_equity"
        assert out["cost_coverage"]["state"] == "not_computed_no_size_or_price"

    def test_never_raises_on_garbage(self):
        out = pe.build_planned_executability(
            preview=self._preview(), sizing_ctx=None, symbol=None, direction=None,
            entry=object(), stop_loss=None, take_profit=None, confidence=None,
            horizon_hours="six", trade_date=None, screening="not-a-dict")
        assert out["exec_state"] in ("generated", "not_generated_error")

    def test_json_serialisable_and_no_nan(self):
        """planned_preview 是要 json.dumps 进 raw_instruction_json 的: NaN 会吐出非法 JSON。"""
        preview = self._preview()
        out = pe.build_planned_executability(
            preview=preview, sizing_ctx=_ctx(), symbol="US.XOM", direction="BUY",
            entry=preview["entry"], stop_loss=preview["stop_loss"],
            take_profit=preview["take_profit"], confidence=70, horizon_hours=6,
            trade_date="2026-09-14")
        text = json.dumps({**preview, **out}, allow_nan=False)
        assert "NaN" not in text and "Infinity" not in text

    def test_blob_version_bumped_and_separate_from_prompt_ruler(self):
        """v1→v2 是 blob 口径升版; 与 prompt 尺子 (PROMPT_VERSION/BRIEF_TEMPLATE_VERSION)
        必须继续分离 —— 改预览口径不等于改模型输入。
        """
        assert ue.PLANNED_PREVIEW_VERSION == "ue_planned_v3"
        assert ue.PLANNED_PREVIEW_VERSION != ue.PROMPT_VERSION
        assert ue.PLANNED_PREVIEW_VERSION != ue.BRIEF_TEMPLATE_VERSION


class TestInjectSiteWiring:
    @pytest.fixture(scope="class")
    def inject_src(self):
        return inspect.getsource(ue._cmd_inject_impl)

    @pytest.fixture(scope="class")
    def block_src(self):
        return inspect.getsource(ue._planned_exec_block)

    def test_context_read_once_per_batch(self, inject_src):
        """计划明禁逐候选读资本状态 ⇒ read_sizing_context/load_borrow_rates 各出现一次,
        且在候选循环之外 (与 _regime_fields 同一段批级准备)。
        """
        assert inject_src.count("read_sizing_context()") == 1
        assert inject_src.count("load_borrow_rates()") == 1
        head = inject_src.split("for _row in _eligible:")[0]
        assert "read_sizing_context()" in head and "load_borrow_rates()" in head

    def test_per_candidate_helper_does_no_io(self, block_src):
        """逐候选那一层只许跑纯函数: 不得再读权益/借券/配置。"""
        for banned in ("read_sizing_context", "load_borrow_rates", "capital_state",
                       "get_capital_safety_system", "sqlite3"):
            assert banned not in block_src, f"逐候选层出现 IO: {banned}"

    def test_batch_context_failure_does_not_block_injection(self, inject_src):
        """批级上下文读取失败只能降级预览, 不能让注入挂掉 ⇒ 必须有兜底默认 + except。"""
        assert re.search(r"except Exception as _pe_ctx_e:", inject_src)
        assert '"state": "unavailable"' in inject_src      # 退化默认值存在
        assert "_sizing_ctx" in inject_src and "_borrow_rates" in inject_src

    def test_merged_blob_is_what_gets_written(self, inject_src):
        assert re.search(r'_preview_full\s*=\s*_planned_exec_block\(', inject_src)
        assert re.search(r'"planned_preview":\s*_preview_full', inject_src)
        # P3.1 的三个平级键仍取生成值, 未被 P3.3-3.5 顶掉
        assert re.search(r'"planned_entry":\s*round\(float\(px\),\s*4\)', inject_src)
        assert re.search(r'"planned_stop_loss":\s*nums\["stop_loss"\]', inject_src)

    def test_symbol_form_matches_borrow_snapshot(self, block_src):
        """借券快照的键是 `US.XXX`, surfaced 的 symbol 是裸码 ⇒ 必须补前缀, 否则
        覆盖率恒 0 而看起来像"没有借券数据"。
        """
        assert 'f"US.{surfaced[\'symbol\']}"' in block_src

    def test_skip_rows_do_not_carry_exec_block(self, inject_src):
        """SKIP 行不调 _compute_numbers ⇒ 也不该有 size/cost/exec 三段, 只留显式状态。"""
        skip_branch = inject_src.split('"not_generated_ineligible"')[0].rsplit("instructions.append", 1)[-1]
        assert "size_estimate" not in skip_branch
        assert "_planned_exec_block" not in skip_branch

    def test_exec_block_never_writes_a_db_column(self):
        """零迁移钉: 本批不加列、不进 `_TRACKING_FIELDS` (加列=动生产 schema, 需批准)。"""
        from app.playbook import db
        assert len(db._TRACKING_FIELDS) == 42
        for field in ("size_estimate", "cost_coverage", "executability", "planned_preview"):
            assert field not in db._TRACKING_FIELDS


class TestPlannedExecBlockHelper:
    """UE 侧真正的接缝: `_planned_exec_block()` 把 P3.1 的 preview 与 P3.3-3.5 三段合起来。

    源级契约只能验文本, 这里验行为 —— 尤其是 `US.` 前缀 (借券快照的键空间) 与
    horizon_hours 的管道, 两者错一个就会让借券覆盖率恒 0 而看起来像"没有借券数据"。
    """

    RATES = {"US.ADBE": {"rate_pct": 3.0, "rate_asof": "2026-09-11", "shortable": True,
                         "pool": 1000000.0, "age_days": 2, "stale": False,
                         "origin": "borrow_rate_series_jsonl"}}

    def _call(self, symbol="ADBE", direction="SHORT", horizon=48, screening=None, ctx=None):
        nums = ue._compute_numbers(245.96, direction)
        surfaced = {"symbol": symbol, "confidence": 0.72, "horizon_hours": horizon}
        return nums, ue._planned_exec_block(
            nums["planned_preview"], ctx if ctx is not None else _ctx(), self.RATES,
            surfaced, screening if screening is not None else {"status": "PASS", "reasons": [],
                                                               "missing_fields": []},
            245.96, nums, "SELL_SHORT" if direction == "SHORT" else "BUY", 72,
            "2026-09-14", "RISK_ON")

    def test_merged_blob_shape_and_serialisability(self):
        nums, merged = self._call()
        for key in ("state", "version", "value_origin", "entry", "entry_zone", "stop_loss",
                    "take_profit", "risk_distance", "rr"):
            assert merged[key] == nums["planned_preview"][key]
        assert merged["exec_state"] == "generated"
        assert merged["size_estimate"]["size"] > 0
        assert merged["cost_coverage"]["state"] == "computed"
        assert merged["executability"]["verdict"] in ("PASS", "REJECT")
        json.dumps(merged, allow_nan=False)

    def test_symbol_prefix_reaches_borrow_snapshot(self):
        """surfaced['symbol'] 是裸码 ADBE, 快照键是 US.ADBE ⇒ 补前缀后必须命中。"""
        _, merged = self._call(symbol="ADBE", direction="SHORT", horizon=48)
        borrow = merged["cost_coverage"]["borrow"]
        assert borrow["rate_pct"] == 3.0
        assert borrow["state"] == "known"
        assert borrow["usd"] > 0

    def test_uncovered_symbol_stays_unknown(self):
        """实测覆盖缺口: US.XLF 不在 258 个快照标的里 ⇒ 记 unknown, 不套 3% 中位数。"""
        _, merged = self._call(symbol="XLF", direction="SHORT", horizon=48)
        borrow = merged["cost_coverage"]["borrow"]
        assert borrow["state"] == "unknown_rate" and borrow["rate_pct"] is None
        assert "borrow" in merged["cost_coverage"]["unknown_items"]
        assert merged["cost_coverage"]["cost_complete"] is False

    def test_intraday_horizon_zero_borrow(self):
        _, merged = self._call(symbol="ADBE", direction="SHORT", horizon=6)
        assert merged["cost_coverage"]["borrow"]["state"] == "zero_planned_intraday"
        assert merged["cost_coverage"]["borrow"]["overnight_risk"] is False

    def test_screening_unknowns_flow_through(self):
        _, merged = self._call(screening={"status": "UNKNOWN", "reasons": [],
                                          "missing_fields": ["bid_ask", "screening_atr_ratio"]})
        exec_block = merged["executability"]
        assert exec_block["screening_status"] == "UNKNOWN"
        assert "bid_ask" in exec_block["unknowns"]
        assert "spread" in exec_block["unknowns"]

    def test_helper_never_raises(self):
        nums = ue._compute_numbers(245.96, "SHORT")
        out = ue._planned_exec_block(nums["planned_preview"], None, None,
                                     {"symbol": None}, None, object(), nums, None, None,
                                     None, None)
        assert out["exec_state"] in ("generated", "not_generated_error")
        assert "exec_state" in out

    def test_unavailable_context_yields_explicit_not_computed(self):
        _, merged = self._call(ctx={"state": "unavailable", "notes": ["context_not_read"]})
        assert merged["exec_state"] == "generated"
        assert merged["size_estimate"]["state"] == "not_computed_no_equity"
        assert merged["sizing_context"]["state"] == "unavailable"


class TestNoModelInputDrift:
    """P3.3-3.5 不碰模型输入 — 四项指纹必须原样 (与 P3.1 收口时逐值相同)。"""

    def test_prompt_rulers_unchanged(self):
        assert ue.PROMPT_VERSION == "v2.14"
        assert ue.PROMPT_HASH_V2 == hashlib.sha1(
            ue.SYSTEM_PROMPT_V2.encode("utf-8")).hexdigest()[:12]
        # 2026-10-15 重钉: v5→v6 系 RFC-136 (c162df515, 用户裁决 10-14) 借券快照注入
        # 修空头断供的有意升级, 测试当时未跟 (10-12~14 SSOT 断档期)。
        assert ue.BRIEF_TEMPLATE_VERSION == "ue_brief_v6"

    def test_compute_numbers_return_shape_unchanged(self):
        """P3.3-3.5 没动 _compute_numbers ⇒ 返回面仍是 P3.1 的 4 键。"""
        nums = ue._compute_numbers(167.326, "LONG")
        assert set(nums) == {"entry_zone", "stop_loss", "take_profit", "planned_preview"}
