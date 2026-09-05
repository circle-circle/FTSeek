"""G-01/A9 playbook 组合级敞口兜底 — 行为 + 结构契约 (2026-09-05 审计修复批).

被测: app/playbook/pipeline.py `_portfolio_backstop_evaluate` (纯函数) 与其在
`monitor_playbook_sim_exits` 内的唯一调用点。

为何必须锁数字 (而非只锁结构): 阈值口径 = max(min_abs_loss_usd, Σ逐仓SL距离 × 倍数)。
Σ SL 距离是"所有逐仓 SL 同时按设计触发"的合计损失, 乘倍数后本兜底只在逐仓 SL 集体失效
时才可能开火 — 2026-08-21 AMZN 误杀 (水深上限被开盘噪声打穿即清仓, 30min 后价回 +1.1%,
-5% SL 从未参与, 用户裁撤销该上限) 是直接教训。阈值若不可单测, 就会像 G-03 (小时熔断退化为
每日仅一次) 那样长期带病存活。

方向符号是本批修掉的真实缺陷: 原实现用毛市值差 (mkt - cost) 聚合浮亏, 只对 LONG 成立。
账本实测 direction 仅 LONG/SHORT 两值 (388/85), SHORT 占 18% → 空头盈利被算成亏损
(误开火) / 空头亏损被算成盈利 (永不开火), 与 H-06 "非 LONG 一律按 SHORT" 同族。

v1 动作边界 (刻意偏离 DEB-20260905 A9 的"全仓逐笔市价平"): 检测 + critical 告警 + 推送,
不在函数内强平。理由: 宿主 monitor_playbook_sim_exits 自带 TRADING 相位门, 进程死亡/休市时
它与兜底一起不跑 —— 那才是 G-01 的真实空洞, 函数内堵不到; 另建平仓回路会与逐仓
close+簿记+tombstone 重复, 引入双平/账实不符 (G-04/G-09 同族)。存活保护交外部监督层
(systemd Restart=always + freeze_sentinel 存活检查)。test_v1_alert_only_no_force_close
锁住该决策 — 若将来要接强平, 必须显式改本测试并复用逐仓平仓路径。

回滚: 删本文件 (不影响被测代码); 或 playbook.execution_parameters.portfolio_backstop.enabled=false。
"""
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402
import yaml  # noqa: E402

from app.playbook.pipeline import _portfolio_backstop_evaluate  # noqa: E402

_PIPE_SRC = (PROJECT_ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
_CFG_PATH = PROJECT_ROOT / "config" / "unified_config.yaml"

# 保守档 (绝对下限主导): max(500, 100×1.5=150) = 500
CFG_FLOOR = {"enabled": True, "sl_loss_multiple": 1.5, "min_abs_loss_usd": 500.0,
             "cooldown_sec": 300.0}
# ΣSL 主导档: max(10, 100×1.5) = 150 — 用于验证阈值取 max 的另一支
CFG_SL = {"enabled": True, "sl_loss_multiple": 1.5, "min_abs_loss_usd": 10.0,
          "cooldown_sec": 300.0}


def _row(symbol, direction, entry, qty, sl=None, meta_raw=None):
    """构造 trade_records 未平行 (只带纯函数消费的列)."""
    if meta_raw is not None:
        meta = meta_raw
    else:
        import json as _json
        meta = _json.dumps({"stop_loss": sl} if sl else {})
    return {"id": 1, "symbol": symbol, "direction": direction, "entry_price": entry,
            "quantity": qty, "metadata_json": meta}


def _q(*pairs):
    return {s: {"last": p} for s, p in pairs}


# ── 阈值口径 ──────────────────────────────────────────────────────────────

class TestThresholdDerivation:
    """阈值 = max(绝对下限, Σ逐仓SL距离 × 倍数) — 两支都要覆盖."""

    # L1: LONG 100×10 SL95 → 成本1000, SL距离 (100-95)×10 = 50
    # L2: LONG  50×20 SL47.5 → 成本1000, SL距离 (50-47.5)×20 = 50   ΣSL = 100
    ROWS = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0),
            _row("US.BBB", "LONG", 50.0, 20, sl=47.5)]
    QUOTES = _q(("US.AAA", 90.0), ("US.BBB", 45.0))   # 浮亏 -100 + -100 = -200

    def test_sl_sum_is_design_loss_of_all_positions(self):
        agg = _portfolio_backstop_evaluate(self.ROWS, self.QUOTES, CFG_FLOOR)
        assert agg["sl_loss_sum_usd"] == pytest.approx(100.0)

    def test_abs_floor_dominates_when_sl_sum_small(self):
        agg = _portfolio_backstop_evaluate(self.ROWS, self.QUOTES, CFG_FLOOR)
        assert agg["threshold_usd"] == pytest.approx(500.0)
        assert agg["unrealized_usd"] == pytest.approx(-200.0)
        assert agg["breached"] is False, "-200 未达 500 下限 → 不得开火 (防 AMZN 式误杀)"

    def test_sl_multiple_dominates_when_floor_low(self):
        agg = _portfolio_backstop_evaluate(self.ROWS, self.QUOTES, CFG_SL)
        assert agg["threshold_usd"] == pytest.approx(150.0)
        assert agg["breached"] is True

    def test_breach_boundary_is_inclusive(self):
        """>= 语义: 浮亏恰好等于阈值即算越线 (不得留 1 分钱的缝)."""
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0)]
        cfg = {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 100.0}
        agg = _portfolio_backstop_evaluate(rows, _q(("US.AAA", 90.0)), cfg)
        assert agg["unrealized_usd"] == pytest.approx(-100.0)
        assert agg["threshold_usd"] == pytest.approx(100.0)
        assert agg["breached"] is True

    def test_cost_market_pct_terms(self):
        rows = [_row("US.AAA", "LONG", 100.0, 10), _row("US.BBB", "LONG", 50.0, 20)]
        deep = _q(("US.AAA", 60.0), ("US.BBB", 40.0))   # -400 + -200 = -600
        agg = _portfolio_backstop_evaluate(rows, deep, CFG_FLOOR)
        assert agg["cost_usd"] == pytest.approx(2000.0)
        assert agg["market_usd"] == pytest.approx(1400.0)
        assert agg["unrealized_usd"] == pytest.approx(-600.0)
        assert agg["unrealized_pct"] == pytest.approx(-30.0)
        assert agg["n_priced"] == 2
        assert agg["breached"] is True


# ── 方向符号 (本批实修缺陷) ────────────────────────────────────────────────

class TestDirectionSign:
    def test_short_profit_is_not_a_loss(self):
        """空头 price<entry = 盈利。旧毛市值差实现会算成 -200 亏损 → 误开火."""
        rows = [_row("US.SHT", "SHORT", 100.0, 10, sl=105.0)]
        cfg = {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 10.0}
        agg = _portfolio_backstop_evaluate(rows, _q(("US.SHT", 80.0)), cfg)
        assert agg["unrealized_usd"] == pytest.approx(200.0)
        assert agg["breached"] is False

    def test_short_loss_counts_as_loss(self):
        """空头 price>entry = 亏损。旧实现算成 +300 盈利 → 兜底永不开火."""
        rows = [_row("US.SHT", "SHORT", 100.0, 10, sl=105.0)]
        cfg = {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 10.0}
        agg = _portfolio_backstop_evaluate(rows, _q(("US.SHT", 130.0)), cfg)
        assert agg["unrealized_usd"] == pytest.approx(-300.0)
        assert agg["breached"] is True

    def test_short_sl_distance_is_above_entry(self):
        rows = [_row("US.SHT", "SHORT", 100.0, 10, sl=105.0)]
        agg = _portfolio_backstop_evaluate(rows, _q(("US.SHT", 100.0)), CFG_FLOOR)
        assert agg["sl_loss_sum_usd"] == pytest.approx(50.0)

    def test_mixed_long_short_net(self):
        """LONG -100 与 SHORT -50 求和 = -150 (逐行定号后再聚合)."""
        rows = [_row("US.LNG", "LONG", 100.0, 10), _row("US.SHT", "SHORT", 50.0, 10)]
        agg = _portfolio_backstop_evaluate(
            rows, _q(("US.LNG", 90.0), ("US.SHT", 55.0)),
            {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 1000.0})
        assert agg["unrealized_usd"] == pytest.approx(-150.0)
        assert agg["cost_usd"] == pytest.approx(1500.0)

    def test_long_and_short_offset(self):
        """LONG -100 被 SHORT +100 完全对冲 → 浮亏 0, 不开火."""
        rows = [_row("US.LNG", "LONG", 100.0, 10), _row("US.SHT", "SHORT", 100.0, 10)]
        agg = _portfolio_backstop_evaluate(
            rows, _q(("US.LNG", 90.0), ("US.SHT", 90.0)),
            {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 10.0})
        assert agg["unrealized_usd"] == pytest.approx(0.0)
        assert agg["breached"] is False

    @pytest.mark.parametrize("direction", ["SHORT", "short", "SELL", " SHORT "])
    def test_short_aliases(self, direction):
        rows = [_row("US.SHT", direction, 100.0, 10)]
        agg = _portfolio_backstop_evaluate(
            rows, _q(("US.SHT", 130.0)),
            {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 10.0})
        assert agg["unrealized_usd"] == pytest.approx(-300.0)

    @pytest.mark.parametrize("direction", ["LONG", "BUY", None, "", "WEIRD"])
    def test_unknown_direction_defaults_to_long(self, direction):
        """未知/缺失按 LONG 兜底 = position_exit_monitor 同款默认 (不反向猜 SHORT)."""
        rows = [_row("US.XXX", direction, 100.0, 10)]
        agg = _portfolio_backstop_evaluate(
            rows, _q(("US.XXX", 90.0)),
            {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 10.0})
        assert agg["unrealized_usd"] == pytest.approx(-100.0)


# ── 保守口径 (缺价/脏行) ───────────────────────────────────────────────────

class TestConservativeInputs:
    def test_missing_quote_excluded_from_all_terms(self):
        """缺价标的既不计入浮亏也不计入成本与 ΣSL — 宁少算敞口, 不用陈旧价触发."""
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0),
                _row("US.NAQ", "LONG", 200.0, 10, sl=150.0)]
        agg = _portfolio_backstop_evaluate(rows, _q(("US.AAA", 90.0)), CFG_FLOOR)
        assert agg["n_priced"] == 1
        assert agg["cost_usd"] == pytest.approx(1000.0)
        assert agg["sl_loss_sum_usd"] == pytest.approx(50.0)

    @pytest.mark.parametrize("bad_quote", [None, {}, {"last": 0}, {"last": None},
                                           {"last": "abc"}, "not-a-dict", {"price": 0}])
    def test_unusable_quote_excluded(self, bad_quote):
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0)]
        agg = _portfolio_backstop_evaluate(rows, {"US.AAA": bad_quote}, CFG_FLOOR)
        assert agg is None, "唯一标的不可定价 → 无可评估敞口, 返回 None 不猜"

    def test_price_key_fallback(self):
        """get_batch_quotes 两种键位 (last/price) 都要吃."""
        rows = [_row("US.AAA", "LONG", 100.0, 10)]
        agg = _portfolio_backstop_evaluate(
            rows, {"US.AAA": {"price": 90.0}},
            {"sl_loss_multiple": 0.0, "min_abs_loss_usd": 50.0})
        assert agg["unrealized_usd"] == pytest.approx(-100.0)

    @pytest.mark.parametrize("bad_row", [
        _row("US.Z1", "LONG", 0.0, 10),          # entry_price=0
        _row("US.Z2", "LONG", 100.0, 0),         # qty=0
        _row("US.Z3", "LONG", None, 10),         # entry_price NULL
        _row("US.Z4", "LONG", 100.0, 10, meta_raw="{not json"),   # metadata 脏
    ])
    def test_bad_row_does_not_poison_aggregate(self, bad_row):
        good = _row("US.AAA", "LONG", 100.0, 10, sl=95.0)
        agg = _portfolio_backstop_evaluate(
            [good, bad_row], _q(("US.AAA", 90.0), (bad_row["symbol"], 50.0)), CFG_FLOOR)
        if bad_row["symbol"] == "US.Z4":
            # metadata 脏 → 仍计入成本/浮亏, 只是 ΣSL 不含它
            assert agg["n_priced"] == 2
            assert agg["sl_loss_sum_usd"] == pytest.approx(50.0)
        else:
            assert agg["n_priced"] == 1
            assert agg["cost_usd"] == pytest.approx(1000.0)
            assert agg["unrealized_usd"] == pytest.approx(-100.0)

    def test_returns_none_when_no_rows(self):
        assert _portfolio_backstop_evaluate([], {}, CFG_FLOOR) is None
        assert _portfolio_backstop_evaluate([], {}, CFG_FLOOR) is None

    def test_empty_cfg_falls_back_to_safe_defaults(self):
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0)]
        agg = _portfolio_backstop_evaluate(rows, _q(("US.AAA", 90.0)), {})
        assert agg["threshold_usd"] == pytest.approx(500.0), "cfg 缺键 → 内置默认 (下限500/倍数1.5)"

    def test_unusable_rows_are_counted_not_swallowed(self):
        """except:pass 棘轮纪律: 单行异常必须计数+带错误上交, 由调用方 warn 留痕。
        否则"敞口被少算"与"本来就没敞口"不可区分 (守卫自己坏了却报零违例)。"""
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0),
                _row("US.BAD", "LONG", 100.0, 10, sl=95.0)]
        agg = _portfolio_backstop_evaluate(
            rows, {"US.AAA": {"last": 90.0}, "US.BAD": {"last": "abc"}}, CFG_FLOOR)
        assert agg["n_priced"] == 1
        assert agg["n_unusable_rows"] == 1
        assert "ValueError" in agg["unusable_last_err"]

    def test_clean_book_reports_zero_unusable(self):
        rows = [_row("US.AAA", "LONG", 100.0, 10, sl=95.0)]
        agg = _portfolio_backstop_evaluate(rows, _q(("US.AAA", 90.0)), CFG_FLOOR)
        assert agg["n_unusable_rows"] == 0
        assert agg["unusable_last_err"] == ""


# ── 接线与 v1 动作边界 (结构契约) ──────────────────────────────────────────

def _monitor_fn_span():
    start = _PIPE_SRC.index("def monitor_playbook_sim_exits(")
    nxt = _PIPE_SRC.index("\ndef ", start + 10)
    return start, nxt


class TestWiringContract:
    def test_call_site_lives_in_exit_fast_loop_after_batch_quotes(self):
        """兜底必须在 20s 快环内、批量取价之后 (否则吃不到 _batch_quotes)."""
        start, end = _monitor_fn_span()
        body = _PIPE_SRC[start:end]
        assert "_batch_quotes: dict = {}" in body
        assert "_portfolio_backstop_evaluate(rows, _batch_quotes, _bs_cfg)" in body
        assert body.index("_batch_quotes: dict = {}") < body.index(
            "_portfolio_backstop_evaluate(rows, _batch_quotes, _bs_cfg)")

    def test_backstop_gated_by_enabled_flag_and_quotes(self):
        """enabled=false 或本轮无报价 → 整段不跑 (热载回滚开关必须在场)."""
        assert 'if _bs_cfg.get("enabled") and _batch_quotes:' in _PIPE_SRC

    def test_breach_path_alerts_critical_and_pushes(self):
        start, end = _monitor_fn_span()
        body = _PIPE_SRC[start:end]
        idx = body.index("_portfolio_backstop_evaluate")
        block = body[idx:idx + 4000]
        assert "logger.critical" in block, "越线必须 critical (G-13 同族: 静默失效不可接受)"
        assert "push_text" in block, "越线必须外推 (进程内 logger 不够 — 需人看到)"
        assert "cooldown_sec" in _PIPE_SRC[start:end], "告警去重窗须在 (20s 快环防刷屏)"

    def test_v1_alert_only_no_force_close(self):
        """v1 刻意不在兜底内强平 — 锁住该决策 (改强平必须显式改本测试).

        理由见文件头: 宿主有 TRADING 相位门, 进程死亡窗口堵不到; 另建平仓回路与逐仓
        close+簿记+tombstone 重复 → 双平/账实不符 (G-04/G-09 同族)。
        """
        start, end = _monitor_fn_span()
        body = _PIPE_SRC[start:end]
        idx = body.index("# ── G-01/A9")
        block = body[idx:body.index("exited = 0", idx)]
        for forbidden in ("close_position", "_close_trade_record", "execute_close_fn",
                          "get_dual_broker", "place_order"):
            assert forbidden not in block, f"v1 兜底不得含平仓动作 {forbidden}"

    def test_config_key_present_and_parsable(self):
        """配置键路径 = playbook.execution_parameters.portfolio_backstop (热载可回滚)."""
        cfg = yaml.safe_load(_CFG_PATH.read_text(encoding="utf-8"))
        bs = cfg["playbook"]["execution_parameters"]["portfolio_backstop"]
        assert bs["enabled"] is True
        assert float(bs["sl_loss_multiple"]) >= 1.0, (
            "倍数 <1 会让阈值低于 ΣSL 设计损失 → 盘中噪声即可打穿 (AMZN 教训)")
        assert float(bs["min_abs_loss_usd"]) > 0
        assert float(bs["cooldown_sec"]) > 0

    def test_runtime_config_resolves_backstop(self):
        from app.playbook.config import get_playbook_config
        bs = get_playbook_config().get("portfolio_backstop")
        assert isinstance(bs, dict) and bs.get("enabled") is True
        assert set(bs) >= {"sl_loss_multiple", "min_abs_loss_usd", "cooldown_sec"}


# ── 当前存量仓实况快照 (回归锚: 三仓全触发 SL 也不得越组合阈值) ─────────────

def test_current_open_book_does_not_breach():
    """2026-09-05 存量 3 仓 (UBER/RIVN/HOOD, 全 LONG) 按其 metadata SL 复算.

    三仓 SL 同时打穿 ≈ -$601 而 ΣSL×1.5 ≈ -$902 → 逐仓 SL 集体按设计触发时兜底**不该**
    开火 (它只该在 SL 失效、亏得比设计更深时开火)。这是阈值"宽于设计损失"的实证锚。
    """
    rows = [
        _row("US.UBER", "LONG", 78.09, 70, sl=74.57),
        _row("US.RIVN", "LONG", 15.94, 312, sl=15.12),
        _row("US.HOOD", "LONG", 123.42, 23, sl=119.13),
    ]
    at_sl = _q(("US.UBER", 74.57), ("US.RIVN", 15.12), ("US.HOOD", 119.13))
    agg = _portfolio_backstop_evaluate(rows, at_sl, CFG_FLOOR)
    assert agg["sl_loss_sum_usd"] == pytest.approx(601.19, abs=0.5)
    assert agg["threshold_usd"] == pytest.approx(901.79, abs=0.5)
    assert agg["unrealized_usd"] == pytest.approx(-601.19, abs=0.5)
    assert agg["breached"] is False

    # 缺口场景 (SL 未兜住): 三仓各再多跌 5% (周末跳空量级) → 合计 -$1,236 越过 $902 → 开火
    gapped = _q(("US.UBER", 74.57 * 0.95), ("US.RIVN", 15.12 * 0.95),
                ("US.HOOD", 119.13 * 0.95))
    agg2 = _portfolio_backstop_evaluate(rows, gapped, CFG_FLOOR)
    assert agg2["unrealized_usd"] < -agg2["threshold_usd"]
    assert agg2["breached"] is True
