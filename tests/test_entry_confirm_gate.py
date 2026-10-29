"""RFC-141 入场延迟确认门 (D2' forward) 契约测试。

钉住的是**纪律**, 不是实现细节 — 每条都对应一个已发生过的事故形态:
  1. 判据逐字对齐影子 _eval_delay (换尺子=静默改语义, 须重新裁决)
  2. 门关闭 ⇒ 零行为变更 (default off 契约)
  3. defer_pending 必须在 reprice **之前** (锚漂=120s 后判的不是 arm 那把尺)
  4. arm 必须在预算 guardrail **之后**、execute 之前 (否则污染判据人群)
  5. 不碰 execution_state (broker 镜像状态机/幽灵仓根因修复面)
  6. 孤儿自愈 (重启不得永久卡死指令)
  7. holdout 分流确定性 + 跨进程稳定
  8. sweep 三态 (CONFIRMED/ABANDONED/EXPIRED) + 取价失败不误判
  9. fail-open 到照常执行 (拦普通单不是安全方向)
 10. 写盘锚定模块路径 (相对路径影子污染教训 2026-09-21)
 11. 注册表 config_key 真门控写方 (RFC-109⑤-C 陷阱: 照假开关停采)
 12. 结算器判据常量 = RFC-141 预注册值 (防挪门柱)
 13. v2 判尺/持续性 (§5.10): mid-mid 同尺救价差噪声 + grace 2-of-3 + 弃单后观测 +
     legacy 回滚臂逐字保真 + 结算器双代分窗 (两源不混算)
"""
import json
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from app.playbook import entry_confirm_gate as ecg  # noqa: E402

_SRC_GATE = Path(__file__).resolve().parents[1] / "app" / "playbook" / "entry_confirm_gate.py"
_SRC_FLOW = (Path(__file__).resolve().parents[1] / "app" / "playbook"
             / "pipeline_parts" / "auto_execute_flow.py")
_SRC_SCHED = Path(__file__).resolve().parents[1] / "app" / "core" / "scheduler_core.py"
_ET = timezone(timedelta(hours=-4))


def _now():
    return datetime(2026, 10, 12, 9, 40, 0, tzinfo=_ET)


class _FakeDB:
    """最小 db 替身: 记录 merge_instruction_metadata 调用。"""

    def __init__(self):
        self.merged = []
        self.skips = []
        self.fail = False

    def merge_instruction_metadata(self, inst_id, patch):
        if self.fail:
            raise RuntimeError("db down")
        self.merged.append((inst_id, patch))
        return True

    def mark_instruction_guardrail_skip(self, inst_id, reason):
        """真 PlaybookDB 同名方法: SET direction='SKIP', skip_reason=?
        WHERE executed=0 AND execution_state='PENDING' (竞态保护)。

        ⚠️ 缺这个方法会让 ABANDONED 的落库调用静默走 except 分支 ⇒ 契约测试全绿
        却从未验到 skip_reason 真写 (fixture 全绿 ≠ 接线活, 2026-10-10 实测踩过)。
        """
        if self.fail:
            raise RuntimeError("db down")
        self.skips.append((inst_id, reason))
        return True


class _FakeRouter:
    """价格源替身: 标量 → 仅 last (无 bid/ask ⇒ mid=None, v2 回落 legacy 混尺);
    (last, bid, ask) 元组 → 带双边报价 (mid 尺生效); 列表 → 按调用次序推进
    (grace 多轮/弃单后补记用); dict → 原样返回 router context (含哨兵键)。"""

    def __init__(self, quote):
        self._q = quote
        self._i = 0

    def get_full_market_context(self, symbol):
        q = self._q
        if isinstance(q, list):                     # 列表 = 逐调用推进的序列
            q = q[min(self._i, len(q) - 1)]
            self._i += 1
        if q is None:
            return None
        if isinstance(q, tuple):                    # 元组 = 单帧 (last, bid, ask)
            return {"price": q[0], "bid": q[1], "ask": q[2]}
        if isinstance(q, dict):                     # dict = 完整 context
            return dict(q)
        return {"price": q}                         # 标量 = 仅 last


class _FakeSched:
    def __init__(self, quote):
        self.data_router = _FakeRouter(quote)


def _inst(i=1, sym="US.TEST", direction="BUY", meta=None):
    return {"id": i, "symbol": sym, "direction": direction,
            "entry_price": 100.0, "position_size": 10,
            "decision_id": f"DA-{i}", "metadata_json": meta or {}}


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    """每例隔离: 清内存 pending + 弃单后观测表 + 产物重定向到 tmp (绝不写生产影子)。"""
    ecg._PENDING.clear()
    ecg._POST_WATCH.clear()
    monkeypatch.setattr(ecg, "_FORWARD_PATH", tmp_path / "entry_confirm_forward.jsonl")
    yield
    ecg._PENDING.clear()
    ecg._POST_WATCH.clear()


CFG_ON = {"enabled": True, "delay_sec": 120, "holdout_pct": 0, "ttl_sec": 600}
CFG_OFF = {"enabled": False, "delay_sec": 120, "holdout_pct": 0, "ttl_sec": 600}


# ══════════════════════════════════════════════════════════════
# 1. 判据不漂移 — 与影子 _eval_delay 逐字同尺
# ══════════════════════════════════════════════════════════════
def test_eval_confirm_matches_shadow_eval_delay_exactly():
    """换判据公式 = 换尺子 = 静默改语义。必须与影子裁决用的函数逐点相同。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import entry_delay_confirm_shadow as shadow

    for direction in ("LONG", "SHORT"):
        for anchor in (100.0, 0.072, 306.46):
            for px in (anchor * 0.9, anchor, anchor * 1.001, anchor * 1.1):
                assert ecg.eval_confirm(direction, anchor, px) == \
                    shadow._eval_delay(direction, anchor, px), \
                    f"判据漂移: {direction} anchor={anchor} px={px}"


def test_eval_confirm_direction_semantics():
    """LONG 需 px≥锚; SHORT 需 px≤锚 (方向反了 = 门恒放行/恒拦)。"""
    assert ecg.eval_confirm("LONG", 100.0, 101.0) == (True, pytest.approx(1.0))
    assert ecg.eval_confirm("LONG", 100.0, 99.0) == (False, pytest.approx(-1.0))
    assert ecg.eval_confirm("SHORT", 100.0, 99.0) == (True, pytest.approx(1.0))
    assert ecg.eval_confirm("SHORT", 100.0, 101.0) == (False, pytest.approx(-1.0))


# ══════════════════════════════════════════════════════════════
# 2. 门关闭 ⇒ 零行为变更
# ══════════════════════════════════════════════════════════════
def test_disabled_gate_is_noop():
    db = _FakeDB()
    inst = _inst()
    assert ecg.gate_at_execute(inst, db, {"entry_price": 101.0}, _now(), CFG_OFF) == "execute"
    assert ecg.defer_pending(inst, _now()) is False
    assert db.merged == []          # 关闭时连旗标都不写
    assert ecg.pending_count() == 0


def test_config_read_failure_fails_to_disabled(monkeypatch):
    """配置不可读 ⇒ enabled=False (不静默改交易行为)。"""
    def _boom(*a, **k):
        raise RuntimeError("config container down")
    monkeypatch.setattr("app.playbook.config.PlaybookConfig", _boom, raising=False)
    assert ecg.confirm_gate_config()["enabled"] is False


def test_production_yaml_keys_are_readable_end_to_end():
    """端到端接线验证 (Claim-Complete 反模式防线): 生产 YAML 真值必须被读到,
    且键必须落在 PlaybookConfig 拍平面 (execution_parameters), 否则 = 装饰性配置。

    2026-10-10 实锤: 首版把键嵌成 execution_parameters.entry_confirm.{enabled,...}
    三层 + 用 get_dotted("playbook.entry_confirm.*") 读 ⇒ 拍平面读不到, 门恒关闭
    而测试全绿 (测试传 cfg dict 绕过了读取路径)。本例钉真实读取路径。
    """
    from app.playbook.config import PlaybookConfig
    pb = PlaybookConfig()
    # 键存在且非默认兜底 (证明真从 YAML 读到, 不是 DEFAULTS 回填)
    assert pb.get("entry_confirm_enabled") is True
    assert pb.get("entry_confirm_delay_sec") == 120
    # 2026-10-10 用户裁「直接走 sim 交易链路, 不用影子攒样看 sim 交易结果」
    # ⇒ holdout 归零 (留对照臂会稀释 SIM 结果), 全量走门
    assert pb.get("entry_confirm_holdout_pct") == 0
    assert pb.get("entry_confirm_ttl_sec") == 600
    # v2 (2026-10-15 用户裁, RFC-141 §5.10): mid-mid 同尺 + grace 2-of-3
    assert pb.get("entry_confirm_ruler") == "mid"
    assert pb.get("entry_confirm_grace_sweeps") == 3
    assert pb.get("entry_confirm_grace_votes") == 2
    # RFC-153: FADE_SHORT 开盘快跌禁追
    assert pb.get("entry_confirm_fade_short_chase_cap_enabled") is True
    assert pb.get("entry_confirm_fade_short_max_opp_pct") == 1.0
    assert pb.get("entry_confirm_fade_short_window_min") == 15
    # 嵌套路径必须读不到 (钉住「不可用 get_dotted 嵌套路径」这条约束)
    from app.core.config_container import get_config
    assert get_config().get_dotted("playbook.entry_confirm.enabled", None) is None
    assert get_config().get_dotted(
        "playbook.execution_parameters.entry_confirm_enabled", None) is True
    # 门自身读到的 = YAML 真值 (不是保守默认)
    assert ecg.confirm_gate_config() == {
        "enabled": True, "delay_sec": 120, "holdout_pct": 0, "ttl_sec": 600,
        "ruler": "mid", "grace_sweeps": 3, "grace_votes": 2,
        "fade_short_chase_cap_enabled": True,
        "fade_short_max_opp_pct": 1.0, "fade_short_window_min": 15}


def test_production_holdout_zero_means_gate_covers_every_order():
    """⛔ holdout_pct=0 的语义必须是「全量走门」, 绝不能反过来变成「门整体失效」。

    边界陷阱: 若 _holdout_bucket 写成 `hash % 100 <= pct` 或漏掉 pct<=0 短路,
    0% 会命中全部指令 ⇒ 全部照常执行不设防 ⇒ 门看着开着 (enabled=true) 却零作用,
    而 SIM 结果与基线无差异会被误读成「门无效」而不是「门没生效」。
    """
    from app.playbook.config import PlaybookConfig
    prod_pct = PlaybookConfig().get("entry_confirm_holdout_pct")
    assert prod_pct == 0, f"生产 holdout 应为 0, 实得 {prod_pct}"
    hits = [i for i in range(1, 501) if ecg._holdout_bucket(i, prod_pct)]
    assert hits == [], f"holdout_pct=0 却有 {len(hits)} 笔绕过门: {hits[:5]}"
    # 反向钉: 100% 必须全命中 (证明分流函数本身在工作, 不是恒 False 的僵尸)
    assert all(ecg._holdout_bucket(i, 100) for i in range(1, 21)), \
        "pct=100 未全命中 ⇒ _holdout_bucket 是恒 False 僵尸, 上面的 0% 断言无意义"
    # 20% 应约 20% (确定性哈希, 防分流函数退化成全 0 或全 1)
    h20 = sum(1 for i in range(1, 501) if ecg._holdout_bucket(i, 20))
    assert 60 < h20 < 140, f"20% 分流严重偏离: {h20}/500"


def test_config_zero_values_are_not_swallowed_by_falsy_default():
    """⛔ 配置读取禁 `get(k, default) or default` — 0 是 falsy 会被吃掉。

    2026-10-10 实锤: 用户裁 holdout_pct=0 (全量走门), 但
    `int(pb_cfg.get(...) or DEFAULT_HOLDOUT_PCT)` 里 `0 or 20` → 20
    ⇒ YAML 写着 0、生产实际 20% 单子绕过门, **静默违背用户意图**且看起来配置已生效。
    同型陷阱见 memory: dict.get None→or default。
    """
    class _ZeroCfg:
        def get(self, k, d=None):
            # 模拟真 PlaybookConfig: 键存在且值为 0
            return {"entry_confirm_enabled": True, "entry_confirm_delay_sec": 0,
                    "entry_confirm_holdout_pct": 0, "entry_confirm_ttl_sec": 0,
                    "entry_confirm_fade_short_chase_cap_enabled": True,
                    "entry_confirm_fade_short_max_opp_pct": 0,
                    "entry_confirm_fade_short_window_min": 0}.get(k, d)

    cfg = ecg.confirm_gate_config(_ZeroCfg())
    assert cfg["holdout_pct"] == 0, f"0 被 falsy 吃掉 → {cfg['holdout_pct']} (用户意图失效)"
    assert cfg["delay_sec"] == 0, f"delay_sec=0 被吃掉 → {cfg['delay_sec']}"
    assert cfg["ttl_sec"] == 0, f"ttl_sec=0 被吃掉 → {cfg['ttl_sec']}"
    assert cfg["fade_short_max_opp_pct"] == 0, "0% 禁追阈值被 falsy 吃掉"
    assert cfg["fade_short_window_min"] == 0, "0 分钟窗口被 falsy 吃掉"

    class _NoneCfg:
        """缺键 ⇒ 必须回落默认 (只认 None 为缺, 不认 0)。"""

        def get(self, k, d=None):
            return d

    cfg2 = ecg.confirm_gate_config(_NoneCfg())
    assert cfg2["holdout_pct"] == ecg.DEFAULT_HOLDOUT_PCT, "缺键未回落默认"
    assert cfg2["delay_sec"] == ecg.DEFAULT_DELAY_SEC, "缺键未回落默认"
    assert cfg2["ttl_sec"] == ecg.DEFAULT_TTL_SEC, "缺键未回落默认"
    assert cfg2["fade_short_max_opp_pct"] == ecg.DEFAULT_FADE_SHORT_MAX_OPP_PCT
    assert cfg2["fade_short_window_min"] == ecg.DEFAULT_FADE_SHORT_WINDOW_MIN

    # 生产真实值复核 (端到端, 不经替身)
    from app.playbook.config import PlaybookConfig
    prod = ecg.confirm_gate_config(PlaybookConfig())
    assert prod["holdout_pct"] == 0, f"生产门实际 holdout={prod['holdout_pct']} ≠ YAML 的 0"
    assert prod["enabled"] is True and prod["delay_sec"] == 120 and prod["ttl_sec"] == 600


# ══════════════════════════════════════════════════════════════
# 3/4. 接线位置 (源级契约 — 位置错了语义就错了)
# ══════════════════════════════════════════════════════════════
def test_defer_pending_is_wired_before_reprice_block():
    """锚冻结契约: pending 拦截必须在 reprice 之前, 否则 DB entry_price 被逐轮覆盖
    ⇒ 120s 后判定的锚不是 arm 时刻那把尺子 (语义不可审计)。"""
    src = _SRC_FLOW.read_text(encoding="utf-8")
    i_defer = src.index("defer_pending as _ec_defer")
    i_reprice = src.index("触发后重新定价 (re-price at trigger)")
    assert i_defer < i_reprice, "defer_pending 接在 reprice 之后 = 锚会漂移"


def test_arm_is_wired_after_budget_guardrails_before_execute():
    """arm 点契约: 在 BP/notional/单轮上限之后 (不污染判据人群), execute 之前 (真会拦)。"""
    src = _SRC_FLOW.read_text(encoding="utf-8")
    i_arm = src.index("gate_at_execute as _ec_gate")
    i_bp = src.index("skipped_bp_cap")
    i_notional = src.index("skipped_notional_cap")
    i_cycle = src.index("deferred_cycle_cap")
    i_exec = src.index("result = service.execute_instruction(")
    assert i_bp < i_arm and i_notional < i_arm and i_cycle < i_arm, \
        "arm 在预算 guardrail 之前 ⇒ 预算不足的单也会被记成延迟确认样本"
    assert i_arm < i_exec, "arm 在 execute 之后 ⇒ 门根本拦不住"


def test_sweep_is_wired_into_fast_loop_not_main_loop():
    """载体节奏契约: sweep 必须挂 10s 快循环。挂 180s 主循环 = 静默落地
    3–5min 未预注册变体 (影子只裁过 2/5min 腿)。"""
    src = _SRC_SCHED.read_text(encoding="utf-8")
    assert "sweep_entry_confirmations" in src
    i_sweep = src.index("sweep_entry_confirmations")
    i_fastloop = src.index("def _playbook_exit_fast_loop")
    # sweep 必须在快循环函数体内 (其后、下一个 def 之前)
    i_next_def = src.index("\n    def ", i_sweep)
    assert i_fastloop < i_sweep < i_next_def, "sweep 不在 _playbook_exit_fast_loop 内"


# ══════════════════════════════════════════════════════════════
# 5. 不碰 execution_state (broker 镜像状态机)
# ══════════════════════════════════════════════════════════════
def test_never_writes_execution_state():
    """execution_state 是 broker 镜像状态机 (position_sync 按它账实对账,
    幽灵仓根因修复面)。本门只许用 metadata_json 旗标。

    按纪律剥 docstring 后比 AST (docstring 里说明「为什么不用」是合法的,
    真代码里出现才是违规 — AST 判定必须剥 docstring, 2026-09-09 误判教训)。
    """
    import ast
    src = _SRC_GATE.read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 剥 docstring
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            b = node.body
            if b and isinstance(b[0], ast.Expr) and isinstance(b[0].value, ast.Constant) \
               and isinstance(b[0].value.value, str):
                node.body = b[1:] or [ast.Pass()]
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    strs = {n.value for n in ast.walk(tree) if isinstance(n, ast.Constant) and isinstance(n.value, str)}
    assert "execution_state" not in names and "execution_state" not in attrs, \
        "门代码触碰 execution_state (broker 镜像状态机/幽灵仓对账面)"
    # 也不许通过字符串键写它 (update_instruction_tracking 的白名单键)
    assert "execution_state" not in strs
    assert "update_instruction_tracking" not in names and "update_instruction_tracking" not in attrs


# ══════════════════════════════════════════════════════════════
# 6. arm → defer → sweep 全流程
# ══════════════════════════════════════════════════════════════
def test_arm_defers_and_freezes_anchor():
    db = _FakeDB()
    inst = _inst()
    t0 = _now()
    act = ecg.gate_at_execute(inst, db, {"entry_price": 105.9, "position_size": 55}, t0, CFG_ON)
    assert act == "defer"
    assert ecg.pending_count() == 1
    # 锚 = reprice 执行锚, 不是 DB 里的盘前计划锚 (实测两锚分歧中位 2.10%)
    assert ecg._PENDING[1]["anchor"] == 105.9
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_PENDING and st["anchor"] == 105.9
    # pending 期间 defer_pending 拦住 (防 reprice 覆盖锚)
    inst_with_flag = _inst(meta={"entry_confirm": st})
    assert ecg.defer_pending(inst_with_flag, t0) is True


def test_anchor_falls_back_to_db_entry_when_no_reprice():
    db = _FakeDB()
    assert ecg.gate_at_execute(_inst(), db, None, _now(), CFG_ON) == "defer"
    assert ecg._PENDING[1]["anchor"] == 100.0


def test_no_valid_anchor_fails_open():
    db = _FakeDB()
    inst = _inst()
    inst["entry_price"] = 0
    assert ecg.gate_at_execute(inst, db, {"entry_price": 0}, _now(), CFG_ON) == "execute"
    assert ecg.pending_count() == 0


def test_sweep_confirms_when_price_still_on_side():
    """LONG 价格站上锚 ⇒ CONFIRMED, 放行下轮执行。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(101.5), db, t0 + timedelta(seconds=125))
    assert r["confirmed"] == 1 and ecg.pending_count() == 0
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_CONFIRMED
    assert st["opp_cost_pct"] == pytest.approx(1.5)
    assert st["delay_actual_sec"] == 125          # 实测节奏可审计 (≤10s 误差契约)
    # CONFIRMED 后 defer_pending 不再拦
    assert ecg.defer_pending(_inst(meta={"entry_confirm": st}), t0) is False


def _sweep_series(sched, db, t0, start_sec=125, steps=4, step_sec=10):
    """按 10s 节奏连扫若干轮, 返回逐轮 out 列表 (v2 grace 流驱动用)。"""
    return [ecg.sweep_entry_confirmations(
        sched, db, t0 + timedelta(seconds=start_sec + i * step_sec)) for i in range(steps)]


def test_sweep_abandons_when_price_reversed():
    """SHORT 价格反抽上穿锚且**持续** ⇒ ABANDONED (MDB 9/28 秒杀族靶形态:
    SHORT @306.46 → 318.43), 终态跨轮拦住。

    v2 (§5.10): 首见逆向不即弃 ⇒ 进 grace; 连续逆向 ≥2 票才弃 (防单点噪声一枪毙命)。
    """
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(direction="SELL_SHORT"), db, {"entry_price": 306.46}, t0, CFG_ON)
    r1 = ecg.sweep_entry_confirmations(_FakeSched(318.43), db, t0 + timedelta(seconds=122))
    assert r1["abandoned"] == 0 and r1["pending"] == 1, "v2: 首见逆向应进 grace 而非即弃"
    rs = _sweep_series(_FakeSched(318.43), db, t0, start_sec=132, steps=2)
    assert rs[-1]["abandoned"] == 1 and ecg.pending_count() - len(ecg._POST_WATCH) == 0
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_ABANDONED
    assert st["grace"]["adverse_votes"] >= 2 and st["grace"]["outcome"] == "persisted"
    assert ecg.defer_pending(_inst(meta={"entry_confirm": st}), t0) is True


def test_sweep_abandons_long_side_too():
    """LONG 跌破锚且持续同样 ABANDONED (方向对称, 防只测一侧)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    rs = _sweep_series(_FakeSched(98.0), db, t0, start_sec=125, steps=3)
    assert rs[0]["abandoned"] == 0 and rs[-1]["abandoned"] == 1


def test_sweep_short_side_symmetry():
    """SHORT 价格下穿锚 = 仍触发 (方向对称, 防只测 LONG)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(i=7, direction="SELL_SHORT"), db, {"entry_price": 180.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(178.0), db, t0 + timedelta(seconds=130))
    assert r["confirmed"] == 1


def test_abandoned_marks_guardrail_skip_for_traceability():
    """ABANDONED 必须落 guardrail SKIP + 专用 reason (可追溯)。

    为什么不能只写 metadata 旗标: defer_pending 已拦住 (双保险), 但库里该指令仍是
    「活跃待执行」方向且永不执行 ⇒ 复盘/dashboard 分不清「没触发」与「被门放弃」,
    误导性比看不出来更糟。落 SKIP 后走 auto_execute_flow.py:160 的 direction=='SKIP'
    分支 + dashboard「已跳过」区显示 reason。
    """
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(i=21, direction="SELL_SHORT"), db,
                        {"entry_price": 306.46}, t0, CFG_ON)
    rs = _sweep_series(_FakeSched(318.43), db, t0, start_sec=122, steps=3)
    assert rs[-1]["abandoned"] == 1
    assert db.skips == [(21, ecg.SKIP_REASON_ABANDONED)], f"未落 guardrail SKIP: {db.skips}"
    assert ecg.SKIP_REASON_ABANDONED == "entry_confirm_reversed"   # reason 是稳定契约
    assert db.merged[-1][1]["entry_confirm"]["skip_marked"] is True  # 落库成败留痕


def test_confirmed_never_marks_skip_so_order_reaches_sim_broker():
    """⛔ 红线: CONFIRMED 绝不能被标 SKIP。

    用户目标口径 (2026-10-10): SIM 盈亏要在**富途 app** 可见 ⇒ 确认门只准拦反转单,
    确认通过的单必须照常走到 execute_instruction (通往富途 SIM 的唯一出口)。
    若哪天误把确认单也标了 SKIP, auto_execute_flow.py:160 会在源头 continue,
    单子永远到不了富途 ⇒ app 上什么都看不见, 门等于把交易掐死。
    """
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(i=22), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(101.5), db, t0 + timedelta(seconds=125))
    assert r["confirmed"] == 1
    assert db.skips == [], f"确认单被误标 SKIP ⇒ 单子到不了富途 SIM: {db.skips}"
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_CONFIRMED
    assert st.get("skip_marked") in (None, False), "CONFIRMED 不得带 skip_marked=True"
    # 下轮 gate 必须返回 execute (不是 defer) ⇒ 真会调 execute_instruction
    assert ecg.gate_at_execute(_inst(i=22, meta={"entry_confirm": st}), db,
                               {"entry_price": 101.5}, t0 + timedelta(seconds=180),
                               CFG_ON) == "execute"
    assert db.skips == [], "放行轮次也不得标 SKIP"


def test_sweep_not_due_yet_stays_pending():
    """未到 delay_sec 不判 (否则又变成更细粒度的未预注册变体)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(50.0), db, t0 + timedelta(seconds=60))
    assert r == {"confirmed": 0, "abandoned": 0, "expired": 0, "pending": 1}
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_PENDING


def test_price_unavailable_keeps_pending_not_misjudged():
    """取价失败 ⇒ 留 pending 下轮再试, 不得当成 ABANDONED (会凭空造亏损样本)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(None), db, t0 + timedelta(seconds=130))
    assert r["abandoned"] == 0 and r["confirmed"] == 0 and r["pending"] == 1


def test_ttl_expiry_marks_expired_and_not_counted():
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(50.0), db, t0 + timedelta(seconds=700))
    assert r["expired"] == 1 and ecg.pending_count() == 0
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_EXPIRED
    # EXPIRED 是终态 ⇒ 不再执行也不再 arm
    st = db.merged[-1][1]["entry_confirm"]
    assert ecg.defer_pending(_inst(meta={"entry_confirm": st}), t0) is True


# ══════════════════════════════════════════════════════════════
# 7. 孤儿自愈 (重启不得永久卡死指令)
# ══════════════════════════════════════════════════════════════
def test_orphan_flag_self_heals_to_execute():
    """旗标 PENDING 但无可信锚/窗口 (旧旗标) ⇒ 自愈留痕且照常执行, 绝不永久卡死。"""
    db = _FakeDB()
    stale = {"status": ecg.ST_PENDING, "armed_at": "2026-10-12T09:40:00-04:00"}
    inst = _inst(meta={"entry_confirm": stale})
    assert ecg.pending_count() == 0
    assert ecg.defer_pending(inst, _now()) is False, (
        "auto_execute 先调 defer_pending; 孤儿若在此被拦, gate_at_execute 的自愈永远不可达")
    assert ecg.gate_at_execute(inst, db, {"entry_price": 100.0}, _now(), CFG_ON) == "execute"
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_HEALED
    assert db.merged[-1][1]["entry_confirm"]["healed_at"]
    healed = db.merged[-1][1]["entry_confirm"]
    assert ecg.defer_pending(_inst(meta={"entry_confirm": healed}), _now()) is False
    assert ecg.gate_at_execute(
        _inst(meta={"entry_confirm": healed}), db, {"entry_price": 100.0},
        _now(), CFG_ON) == "execute"


def test_orphan_with_window_restores_pending_instead_of_executing():
    """重启时窗口未过 ⇒ 按旗标重建 pending 续上门控, 不得把 120s 确认降级为立即执行。"""
    db = _FakeDB()
    t0 = _now()
    stale = {"status": ecg.ST_PENDING,
             "armed_at": (t0 - timedelta(seconds=60)).isoformat(),
             "anchor": 100.0, "direction": "LONG", "delay_sec": 120,
             "ttl_sec": 600, "ruler": "legacy", "mid_arm": None}
    inst = _inst(meta={"entry_confirm": stale})
    assert ecg.gate_at_execute(inst, db, {"entry_price": 101.0}, t0, CFG_ON) == "defer"
    arm = ecg._PENDING[1]
    assert arm["anchor"] == 100.0, "重建必须沿用 arm 时锚, 不得用重启后 reprice 值"
    assert arm["delay_sec"] == 120 and arm.get("restored") is True
    # 已进入 pending ⇒ defer_pending 在 reprice 前拦截
    assert ecg.defer_pending(_inst(meta={"entry_confirm": stale}), t0) is True
    # 窗口剩余 60s: 到点后由 sweep 正常判定 (同向 ⇒ CONFIRMED), 而非直接放行
    r = ecg.sweep_entry_confirmations(_FakeSched(101.0), db, t0 + timedelta(seconds=61))
    assert r["confirmed"] == 1
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_CONFIRMED


def test_orphan_without_window_rearms_instead_of_executing():
    """[RFC-178] 窗口不可重建但有现价 ⇒ 不立即放行, 按现价补一轮确认窗口 → defer。"""
    db = _FakeDB()
    t0 = _now()
    stale = {"status": ecg.ST_PENDING}          # 旧旗标: 无锚/无方向/无 armed_at
    inst = _inst(meta={"entry_confirm": stale})
    act = ecg.gate_at_execute(inst, db, {"entry_price": 100.0}, t0, CFG_ON,
                              scheduler=_FakeSched(103.5))
    assert act == "defer", "没经过确认不得因为'窗口不可重建'就放行"
    assert ecg.pending_count() == 1
    arm = ecg._PENDING[1]
    assert arm["anchor"] == 103.5, "补 arm 必须用补 arm 时刻的现价, 不得用计划锚充数"
    assert arm["delay_sec"] == 120
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_PENDING and st["orphan_rearms"] == 1
    assert st["rearmed_from_orphan_at"]
    # 补出来的窗口照常走 sweep 判定 (不绕过确认门)
    assert ecg.defer_pending(_inst(meta={"entry_confirm": st}), t0) is True
    r = ecg.sweep_entry_confirmations(_FakeSched(103.6), db, t0 + timedelta(seconds=121))
    assert r["confirmed"] == 1


def test_orphan_rearm_exhausted_falls_back_to_execute():
    """[RFC-178] 已补过 1 次 arm 仍不可重建 ⇒ 放行 (兜底不摘, 防永久卡死)。"""
    db = _FakeDB()
    t0 = _now()
    stale = {"status": ecg.ST_PENDING, "orphan_rearms": 1,
             "armed_at": (t0 - timedelta(seconds=900)).isoformat(),
             "anchor": 100.0, "direction": "LONG", "delay_sec": 120, "ttl_sec": 600}
    act = ecg.gate_at_execute(_inst(meta={"entry_confirm": stale}), db,
                              {"entry_price": 100.0}, t0, CFG_ON,
                              scheduler=_FakeSched(103.5))
    assert act == "execute"
    assert ecg.pending_count() == 0
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_HEALED


def test_orphan_rearm_without_quote_still_executes():
    """[RFC-178] 补 arm 需要现价: 取不到价 ⇒ 维持放行 (无报价不得卡死指令)。"""
    db = _FakeDB()
    t0 = _now()
    stale = {"status": ecg.ST_PENDING}
    act = ecg.gate_at_execute(_inst(meta={"entry_confirm": stale}), db,
                              {"entry_price": 100.0}, t0, CFG_ON,
                              scheduler=_FakeSched(None))
    assert act == "execute"
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_HEALED


def test_orphan_with_expired_window_heals_to_execute():
    """重启时窗口已整体超 TTL ⇒ 不在过期锚上补判定, 转 HEALED 照常执行。"""
    db = _FakeDB()
    t0 = _now()
    stale = {"status": ecg.ST_PENDING,
             "armed_at": (t0 - timedelta(seconds=700)).isoformat(),
             "anchor": 100.0, "direction": "LONG", "delay_sec": 120,
             "ttl_sec": 600, "ruler": "legacy", "mid_arm": None}
    assert ecg.gate_at_execute(_inst(meta={"entry_confirm": stale}), db,
                               {"entry_price": 100.0}, t0, CFG_ON) == "execute"
    assert ecg.pending_count() == 0
    assert db.merged[-1][1]["entry_confirm"]["status"] == ecg.ST_HEALED


def test_orphan_heal_survives_db_failure():
    """自愈写旗标失败也不能卡单 (放行优先于留痕)。"""
    db = _FakeDB()
    db.fail = True
    inst = _inst(meta={"entry_confirm": {"status": ecg.ST_PENDING}})
    assert ecg.gate_at_execute(inst, db, {"entry_price": 100.0}, _now(), CFG_ON) == "execute"


# ══════════════════════════════════════════════════════════════
# 8. holdout 对照臂
# ══════════════════════════════════════════════════════════════
def test_holdout_bucket_is_deterministic_and_stable():
    """同 inst_id 恒定同臂 (跨轮/跨重启一致), 否则对照臂被污染。"""
    for i in (1, 7, 42, 999):
        assert ecg._holdout_bucket(i, 20) == ecg._holdout_bucket(i, 20)


def test_holdout_ratio_approximates_config():
    hits = sum(1 for i in range(1000) if ecg._holdout_bucket(i, 20))
    assert 140 < hits < 260, f"holdout 分流严重偏离 20%: {hits}/1000"


def test_holdout_arm_executes_immediately_and_logs_row(tmp_path):
    """对照臂照常执行 (不设防) + 落一行供配对读数。"""
    db = _FakeDB()
    cfg = {**CFG_ON, "holdout_pct": 100}
    assert ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, _now(), cfg) == "execute"
    assert ecg.pending_count() == 0
    rows = [json.loads(x) for x in ecg._FORWARD_PATH.read_text().splitlines()]
    assert len(rows) == 1 and rows[0]["arm"] == "holdout"
    assert rows[0]["status"] == "ok" and rows[0]["still_triggers"] is True
    # holdout 重入不重复落行 (旗标已在)
    st = db.merged[-1][1]["entry_confirm"]
    assert ecg.gate_at_execute(_inst(meta={"entry_confirm": st}), db,
                              {"entry_price": 100.0}, _now(), cfg) == "execute"
    assert len(ecg._FORWARD_PATH.read_text().splitlines()) == 1


def test_holdout_zero_means_all_treatment():
    db = _FakeDB()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, _now(), CFG_ON)
    assert ecg.pending_count() == 1


# ══════════════════════════════════════════════════════════════
# 9. fail-open + 逐笔隔离
# ══════════════════════════════════════════════════════════════
def test_gate_exception_fails_open(monkeypatch):
    """门自身异常 ⇒ 照常执行 (拦普通单不是安全方向; 静默减样=改基线)。"""
    monkeypatch.setattr(ecg, "_holdout_bucket", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    db = _FakeDB()
    assert ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, _now(), CFG_ON) == "execute"


def test_sweep_isolates_per_instruction(monkeypatch):
    """单笔判定异常不得影响其他 pending (逐笔隔离), 且异常笔弹出等自愈。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(i=1), db, {"entry_price": 100.0}, t0, CFG_ON)
    ecg.gate_at_execute(_inst(i=2, sym="US.OTHER"), db, {"entry_price": 50.0}, t0, CFG_ON)
    assert ecg.pending_count() == 2

    orig = ecg._current_quotes
    def _flaky(sched, sym):
        if sym == "US.TEST":
            raise RuntimeError("quote blew up")
        return orig(sched, sym)
    monkeypatch.setattr(ecg, "_current_quotes", _flaky)
    r = ecg.sweep_entry_confirmations(_FakeSched(51.0), db, t0 + timedelta(seconds=130))
    assert r["confirmed"] == 1              # 健康那笔照常判定
    assert ecg.pending_count() == 0         # 异常笔已弹出 → 下轮 auto_execute 自愈放行


def test_db_write_failure_does_not_block_execution():
    db = _FakeDB()
    db.fail = True
    assert ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, _now(), CFG_ON) == "defer"


# ══════════════════════════════════════════════════════════════
# 10. 产物路径锚定模块 (相对路径污染教训)
# ══════════════════════════════════════════════════════════════
def test_forward_path_is_module_anchored_absolute():
    src = _SRC_GATE.read_text(encoding="utf-8")
    assert 'Path(__file__).resolve().parents[2]' in src
    assert 'Path("data/' not in src, "相对路径影子文件 = 生产污染入口 (2026-09-21 实锤)"
    assert ecg._FORWARD_PATH.is_absolute()


def test_forward_row_schema_is_stable_and_agg_compatible():
    """字段必须满足结算器/影子 _agg_block 契约 (status/still_triggers/opp_cost_pct/
    notional/date), 否则攒了样也裁不了 (断头传感器教训)。

    v2 (§5.10): schema 升 v2 = v1 全字段 + 判尺/grace/双边报价新字段 (结算器双代通吃)。
    """
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0, "position_size": 55}, t0, CFG_ON)
    ecg.sweep_entry_confirmations(_FakeSched(101.0), db, t0 + timedelta(seconds=125))
    row = json.loads(ecg._FORWARD_PATH.read_text().splitlines()[0])
    for k in ("schema", "ts", "date", "inst_id", "decision_id", "symbol", "direction",
              "arm", "status", "still_triggers", "opp_cost_pct", "anchor",
              "px_at_confirm", "notional", "session_min", "outcome",
              # v2 新增 (缺 bid/ask 的 price-only 源 ⇒ mid 系 None + ruler_used=legacy)
              "ruler_used", "mid_arm", "mid_at_confirm", "mid_move_pct", "grace"):
        assert k in row, f"前瞻行缺字段 {k}"
    assert row["schema"] == "entry_confirm_forward_v2"
    assert row["ruler_used"] == "legacy"      # price-only 源 ⇒ mid 两端不全 ⇒ 回落留痕
    assert row["notional"] == 5500.0
    assert row["session_min"] == 10          # 09:40 − 09:30 = 10min (R1 分层用)
    # 影子 _agg_block 能直接吃这行 (同尺子零改动契约)
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
    import entry_delay_confirm_shadow as shadow
    blk = shadow._agg_block([{**row, "pnl_net": -231.73}])
    assert blk["n"] == 1 and blk["n_delayed"] == 1


def test_session_min_marks_open_window_for_r1_stratum():
    db = _FakeDB()
    t_open = datetime(2026, 10, 12, 9, 36, 0, tzinfo=_ET)
    ecg.gate_at_execute(_inst(i=5, direction="SELL_SHORT"), db, {"entry_price": 300.0}, t_open, CFG_ON)
    assert ecg._PENDING[5]["session_min"] == 6      # 开盘窗 0-15 内


# ══════════════════════════════════════════════════════════════
# 11. 注册表登记 + config_key 真门控写方
# ══════════════════════════════════════════════════════════════
def test_registered_in_shadow_registry_with_real_gate_key():
    """RFC-109⑤-C 陷阱: config_key 必须真门控写方, 否则照它「停采」是假开关。"""
    from app.playbook.shadow_registry import REGISTRY
    item = next((s for s in REGISTRY if s.id == "entry_confirm_forward"), None)
    assert item is not None, "新影子未入注册表 = 无人跟踪 = 等于没做"
    assert item.config_key == "playbook.execution_parameters.entry_confirm_enabled"
    # min_n 必须与结算器样本门一致 (2026-10-10 口径变更后从 20→60): 否则周检会用
    # 错的 n 驱动「到期未达标=治理缺口」告警 ⇒ 钉跨文件一致性, 不硬编码字面量
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as _st
    assert item.min_n == _st.MIN_SIM_N, (
        f"注册表 min_n={item.min_n} 与结算器样本门 MIN_SIM_N={_st.MIN_SIM_N} 漂移 "
        f"⇒ 周检告警会用错阈值")
    assert item.min_n == 60, f"样本门应为 60 (SIM 真实成交笔数), 实得 {item.min_n}"
    assert item.eval_script == "scripts/settle_entry_confirm_forward.py"
    # 2026-10-22 (全链路复查 R3 修复): 计数锚从「机制台帐行」改为「裁决轴 = v2 起 SIM 真实
    # 成交笔数」(结算器 latest.json 的 adjudication_source.all.n) — 原 where:arm=treatment
    # 把台帐行 105 计成 n, 是裁决轴 (20) 的 5 倍 ⇒ 假 READY。钉子同步换钉新轴。
    assert item.count_mode == "json_key:adjudication_source.all.n"
    assert item.data_path == "data/shadow/entry_confirm_forward_latest.json"
    assert "entry_delay_confirm" in (item.cross_ref or ())
    # 口径变更后注册表不得残留旧的「配对净差/holdout 对照」裁决语义
    assert "配对净差" not in (item.graduation_criteria or ""), \
        "注册表仍写旧判据 (影子配对净差) ⇒ 与结算器实现漂移, 裁决会用错尺子"
    assert "holdout 真成交净均" not in (item.graduation_criteria or "")
    # 判据里的数字本体须真出现在注册表 (防文档与实现两张皮)
    # ⚠️ needle 必须是 **v2 现行判据**的数字。v1 值 (−$4.1115/+$2.79/−$157.9069) 已从
    #   注册表移除, 故此处不会假绿; 但 RFC 里 v1 值以「留档」形式仍在 ⇒ RFC 侧 needle
    #   另有防假绿断言 (见 test_rfc141_preregistration_exists_and_pins_criteria)。
    for needle in ("−$40", "−$2.2632", "+$7.79", "+$8.79", "n≥60"):
        assert needle in item.graduation_criteria, f"注册表判据缺 {needle}"
    # v1 判据数字若出现, 必须**就近标注 v1** (否则=现行判据仍在用旧基线尺子)
    # 注: 不能简单断言 "v1 值 not in text" —— 留档对比 ("比 v1 地板 +$2.79 抬高 2.8×")
    #     是合法的且有价值 (证明门槛只收紧)。故改为: 每个 v1 值出现的 40 字符窗口内须有 "v1"。
    gc = item.graduation_criteria or ""
    for v1 in ("−$4.1115", "+$2.79", "−$157.9069", "+$3.4852"):
        idx = gc.find(v1)
        while idx != -1:
            ctx = gc[max(0, idx - 40):idx + len(v1)]
            assert "v1" in ctx, (
                f"注册表出现 v1 判据 {v1} 但就近未标注 v1 ⇒ 疑似当作现行判据: ...{ctx}...")
            idx = gc.find(v1, idx + 1)
    # 现行判据必须是 v2 值 (上面 needle 已验), 且 v2 地板严于 v1 (门槛只准收紧)
    assert _st.WINNER_MEAN_FLOOR > 2.79 and _st.WINNER_MEAN_FLOOR_CLEAN > 3.47, (
        "v2 地板必须严于 v1 (v1: 2.79/3.47) — 若更松 = 挪门柱")
    # config_key 真的门控写方: 门关闭 ⇒ 不 arm ⇒ 不落行
    db = _FakeDB()
    assert ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, _now(), CFG_OFF) == "execute"
    assert not ecg._FORWARD_PATH.exists()


def test_rfc141_preregistration_exists_and_pins_criteria():
    """判据必须在读数前写死在 RFC (防阈值购物/挪门柱)。

    2026-10-10 口径变更 (用户裁「直接走 sim 交易链路, 不用影子攒样看 sim 交易结果」):
      裁决源 = 开门后 SIM 真实盈亏 vs 开门前预注册基线; jsonl 降级为机制留痕。
    ⇒ needle 换成新判据的**数字本体** (不只是关键词), 钉死防事后挪门柱。
    """
    rfc = (Path(__file__).resolve().parents[1] / "docs" / "decisions"
           / "RFC-141-entry-confirm-forward-live.md")
    assert rfc.exists()
    txt = rfc.read_text(encoding="utf-8")

    # 📌 防假绿: RFC 里 v1 值以「留档」形式仍全文存在 ⇒ 只断言 "v1 数字 in txt" 会命中
    #   留档行而非现行判据 (假绿)。故现行判据 needle 全部取 **v2 值**, 并要求留档段
    #   显式自标 (v1 数字只能出现在标注了留档/变更依据的段落里)。
    for needle in (
        # 裁决源改口径的用户原话留痕 (禁悄悄改回影子攒样)
        "直接走 sim 交易链路",
        # v2 窗口变更的用户原话 (禁悄悄把窗口改回去 / 禁不留依据地挑窗口)
        "从8月份开始统计",
        # 判据1 病灶族收敛: v2 基线 + 阈值本体 (阈值刻意不随 v2 重算, 取严)
        "SL_INITIAL", "−$162.3363", "−$40/笔", "−10.2418",
        # 判据2 开盘窗改善: v2 基线净均 + 钉住的 v1 严 t 门槛
        "−$2.2632", "−0.3218", "OPEN_WIN_T_FLOOR",
        # 判据3 机制量
        "≥30%", "<0.3%",
        # 判据4 不伤赢家: v2 基线 + 80% 地板 (全量/剔费两端同尺)
        "+$9.7353", "+$7.79", "+$10.9854", "+$8.79",
        # 样本门
        "n≥60", "≥10 交易日",
        # 基线冻结声明 (禁事后挑窗口)
        "开门前定, 禁事后挑窗口",
        # 基线生成器 (基线可复现, 非手抄)
        "deb20261009_rfc141_baseline_gen.py",
    ):
        assert needle in txt, f"RFC-141 预注册缺关键判据: {needle}"

    # v1 值只能出现在明确标注留档的上下文里 (出现即须有留档标签, 否则=两套判据并存)
    for v1 in ("−$4.1115", "+$3.4852", "−$157.9069"):
        assert v1 in txt, f"{v1} 应作为 v1 留档保留 (禁事后挑窗口 ⇒ 两版并存可查)"
        # 留档段必须自标: 全文出现 "v1 值留档" 与 "不构成挪门柱" 说明
        assert "v1 值留档" in txt, "RFC 缺 v1 留档标签 ⇒ v1/v2 数字并存但无归属说明"
    assert "不构成挪门柱" in txt, "RFC 必须写明改窗为何不算挪门柱 (零读数 + 收紧方向)"
    assert "verdict=TOO_EARLY" in txt, "RFC 必须留痕「改窗时门尚无读数」这一前置证据"


def test_rfc141_discloses_replay_arm_main_caliber_fail():
    """§5.7 对照环主判读口径 fail 的披露不得被删 (裁决日须并陈, shadow_registry:1827)。

    钉数字本体而非关键词: 前向 B_only 是**主判读口径**且已 fail
    (机会成本 1.0253% ≥ 0.3% 门), 而回放 SL_INITIAL t=4.92 很好看 ⇒ 单引回放给门背书
    是这条披露要防的具体误用。删掉披露不会让任何其它测试变红, 故须单独钉住。
    """
    rfc = (Path(__file__).resolve().parents[1] / "docs" / "decisions"
           / "RFC-141-entry-confirm-forward-live.md")
    txt = rfc.read_text(encoding="utf-8")

    for needle in (
        # 主判读口径及其 fail 状态 (数字本体)
        "forward_B_only_pass: false",
        "1.0253%",                      # 机会成本率 (超 0.3% 门 3.42 倍)
        "−$769.28",                     # 前向 B_only 净价值
        # 回放臂的漂亮读数 (必须与上面并陈, 不得只引这一条)
        "+$2,938.73", "+4.92",
        # 成本/收益结构反转 (同量纲美元口径)
        "$3,037.14", "$98.41", "$122.25", "$891.53", "7.29×",
        # 量纲陷阱批注 (防后人重犯: 成本率 vs 命中率不可相减)
        "量纲不同",
        # 并陈义务与不得回滚的界定
        "两边读数须并陈", "这不是要求回滚本门",
        # 载体卡点更正 (§27 担忧成立但已被 10s sweep 载体绕开)
        "sweep_entry_confirmations", "five_min_confirm_exceeds_ttl=true",
    ):
        assert needle in txt, f"RFC-141 §5.7 披露缺关键读数: {needle}"

    # 📌 防假绿: 该披露必须是**编号条目 7** 且落在 §5 内 (而非塞进留档段当装饰)
    assert "7. **对照环主判读口径已 fail" in txt, \
        "披露须作为 §5 强制披露第 7 条存在 (编号缺失=可能被挪进非强制段)"
    sec5 = txt.split("## 5. 强制披露", 1)[1].split("## 6. 回滚", 1)[0]
    assert "forward_B_only_pass: false" in sec5, "披露必须在 §5 强制披露节内"


def test_rfc141_discloses_precise_coverage_is_pseudo_goal():
    """§5.8 精准覆盖伪目标的披露不得被删 (防裁决日重提"收窄作用域就行")。

    钉数字本体: 三条收窄路各被一个**实值**堵死 — 路A=预注册纪律+forward-B已FAIL、
    路B=ADR-035路由影子 fill_rate 0.323<0.50 且 sign_test p=1.0、路C=T19红线。
    外加隔夜地平线错配的 52% 实测量。删掉披露不会让其它测试变红, 故单独钉住。
    """
    rfc = (Path(__file__).resolve().parents[1] / "docs" / "decisions"
           / "RFC-141-entry-confirm-forward-live.md")
    txt = rfc.read_text(encoding="utf-8")

    for needle in (
        # 三圈错配 (live 作用域 ⊋ 裁决人群)
        "圈3=100%", "entry_archetype_routing_enabled=false",
        # 路 B 堵死的实值 (ADR-035 路由影子自身 FAIL)
        "fill_rate=0.323", "sign_test_p_one_sided=1.0", "paired_diff_median=−10.77",
        # 路 C 堵死的红线
        "T19", "t=1.94",
        # 隔夜地平线错配的实测量 (2min 门只接住一半)
        "2min 门只接到隔夜跳空亏损的 52%", "−$1,315.72", "−$1,203.37",
        # 结论: 精准杠杆在入场质量不在本门收窄
        "candidate C 生成锚", "+$137.20",
    ):
        assert needle in txt, f"RFC-141 §5.8 披露缺关键读数: {needle}"

    # 📌 防假绿: 必须是**编号条目 8** 且落在 §5 内 (而非塞进留档段当装饰)
    assert "8. **精准覆盖是伪目标" in txt, \
        "披露须作为 §5 强制披露第 8 条存在 (编号缺失=可能被挪进非强制段)"
    sec5 = txt.split("## 5. 强制披露", 1)[1].split("## 6. 回滚", 1)[0]
    assert "精准覆盖是伪目标" in sec5, "披露必须在 §5 强制披露节内"
    # 三条路必须都在 (防只删一条让披露失去"全堵死"力度)
    for path in ("路 A", "路 B", "路 C"):
        assert path in sec5, f"§5.8 缺 {path} — 三条收窄路须全部登记堵死依据"



# ══════════════════════════════════════════════════════════════
# 12. 结算器常量 = 预注册值
# ══════════════════════════════════════════════════════════════
def test_settler_criteria_constants_match_rfc():
    """结算器常量 = RFC 预注册值 (两边漂移即判红, 防改一边不改另一边)。

    2026-10-10 口径变更后: 旧的 MIN_N/MIN_T/pair_delta 已废 (裁决源不再是影子配对),
    改为 SIM 真实盈亏对比基线的四条判据常量。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st
    # 样本门
    assert (st.MIN_SIM_N, st.MIN_SIM_DAYS) == (60, 10)
    # 判据1 病灶族收敛
    assert st.SL_INIT_MAX_LOSS_PER_TRADE == -40.0
    assert st.SL_INIT_MAX_N == 1
    # 判据3 机制量
    assert (st.MIN_AVOID, st.MAX_OPP) == (0.30, 0.3)
    # 判据4 不伤赢家地板 — 两端同尺 (RFC-141 §5.6 / DEB §33.3)
    # v2 窗口 (8/1 起): rest.net_mean +9.7353 ⇒ 全量地板 7.79; rest_clean +10.9854 ⇒ 剔费地板 8.79
    assert st.WINNER_MEAN_FLOOR == 7.79, "全量地板必须逐字等于预注册文本 (禁 4 位漂移)"
    assert st.WINNER_MEAN_FLOOR_CLEAN == 8.79, "剔费地板 (裁决口径)"
    assert st.WINNER_MEAN_FLOOR_CLEAN > st.WINNER_MEAN_FLOOR, (
        "剔费地板必须**更高** (基线含费用异常单被拖低) — 若反向 ⇒ 剔费口径失效, "
        "门会用松地板白过")
    # 地板必须从 BASELINE 派生 (单一真源), 不是硬编码字面量
    assert st.winner_floor(False) == round(st.BASELINE["rest"]["net_mean"] * 0.8, 2)
    assert st.winner_floor(True) == round(st.BASELINE["rest_clean"]["net_mean"] * 0.8, 2)
    # 4 位精度会把地板降到预注册文本以下 = 放松门槛方向 ⇒ 钉住 2 位
    assert round(st.BASELINE["rest"]["net_mean"] * 0.8, 4) < st.WINNER_MEAN_FLOOR, (
        "4 位派生值低于预注册门槛 ⇒ 若有人改成 4 位就是挪门柱, 本断言会红")
    # 费用异常单阈值沿用 §27/W8 同口径, 不另造
    assert st.FEE_BPS_ANOMALY == 100.0
    assert st.BASELINE["rest_clean"]["fee_bps_anomaly_dropped"] == 2, (
        "v2 窗口 [8/1,10/10) 内 fee_bps>100 的非SL笔数 = 2 (CTNT/GIPR, 均 9/17); "
        "v1 是 3 (含 NVDA 7/9, 已落在窗口外)")
    # 开盘窗口径 (与 time_bucket_analysis 分桶一致)
    assert (st.OPEN_WIN_START, st.OPEN_WIN_END) == ("09:35", "09:45")
    # 旧常量必须已删除 (防有人拿废判据继续裁决)
    assert not hasattr(st, "MIN_N") and not hasattr(st, "MIN_T")
    # forward 产物只作机制留痕 (裁决源是 SIM 真实盈亏)
    assert "entry_confirm_forward.jsonl" in st.FWD.name


def test_settler_excludes_research_arm_from_pnl_cohort(tmp_path, monkeypatch):
    """研究臂 (gate_mode=research_momentum) 不得混入 RFC-141 裁决队列。

    10-19 实证: NVDA -75.50 被计入 SL_INITIAL 族, 使判据①假性不过; PEP +106.64
    抬高非 SL 族。研究臂 10-19 才启用, 不在 8/1–10/9 冻结基线人群内, 且不经过
    入场确认门 ⇒ 必须排除。strict EXT 在基线窗内已存在, 必须保留 (同尺)。
    """
    import sqlite3
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st

    db_path = tmp_path / "settle.db"
    con = sqlite3.connect(db_path)
    con.execute(
        "CREATE TABLE trade_records ("
        "symbol TEXT, trade_date TEXT, entry_time TEXT, exit_time TEXT, "
        "exit_reason TEXT, fees_est REAL, quantity INTEGER, entry_price REAL, "
        "pnl_net REAL, realized_pnl REAL, source_type TEXT, exit_price REAL, "
        "metadata_json TEXT)"
    )
    rows = [
        ("US.DA", "2026-10-19", "SL_BREAKEVEN", 5.0, "PLAYBOOK_SIM", '{}'),
        ("US.STRICT", "2026-10-19", "SL_BREAKEVEN", 5.0, "PLAYBOOK_SIM",
         '{"gate_mode": "strict"}'),
        ("US.RESEARCH", "2026-10-19", "SL_INITIAL", -75.5, "PLAYBOOK_SIM",
         '{"gate_mode": "research_momentum", "research_source": "research_watchlist"}'),
    ]
    for symbol, date, reason, net, source, meta in rows:
        con.execute(
            "INSERT INTO trade_records (symbol, trade_date, entry_time, exit_time, "
            "exit_reason, fees_est, quantity, entry_price, pnl_net, realized_pnl, "
            "source_type, exit_price, metadata_json) "
            "VALUES (?, ?, ?, ?, ?, 4.0, 10, 100.0, ?, ?, ?, 101.0, ?)",
            (symbol, date, f"{date}T09:40:00-04:00", f"{date}T10:00:00-04:00",
             reason, net, net, source, meta),
        )
    con.commit()
    con.close()
    monkeypatch.setattr(st, "DB", db_path)

    symbols = {r["symbol"] for r in st.load_sim_pnl("2026-10-19")}
    assert symbols == {"US.DA", "US.STRICT"}, (
        "研究臂必须排除; strict EXT 必须保留 (基线同尺)")
    fills = st.load_fills("2026-10-19")
    assert set(fills) == {("US.DA", "2026-10-19"), ("US.STRICT", "2026-10-19")}
    assert st.RESEARCH_ARM_GATE_MODE == "research_momentum"
    assert "gate_mode" in st.EXCLUDE_RESEARCH_ARM_SQL


def test_fee_bps_is_pre_computable_not_post_hoc():
    """fee_bps 必须是**事前可算**量 (下单前就知道), 不是事后标签。

    这是它比 MFE 门更适合做准入闸的原因 (MFE 要持仓走完才知道 ⇒ 只能事后归因)。
    公式 = fees_est / (entry_price × quantity) × 10000, 三个输入全在下单时刻已知。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st
    # CTNT 实锤形态: 68,493 股 @ $0.072, 费用 $451.26 ⇒ 915.1bps (§27 缺陷3)
    ctnt = {"fees_est": 451.26, "qty": 68493, "entry_price": 0.072}
    assert st.fee_bps(ctnt) == pytest.approx(915.1, abs=0.2), st.fee_bps(ctnt)
    assert st.is_fee_anomaly(ctnt) is True
    # 正常单 (NVDA 形态): 费用 $4.01 / 大 notional ⇒ 低 bps, 不得误剔
    normal = {"fees_est": 4.01, "qty": 10, "entry_price": 202.5}
    assert st.is_fee_anomaly(normal) is False
    # 边界: 恰好 100bps 不算异常 (阈值是 > 不是 >=), 与 W8 口径一致
    edge = {"fees_est": 1.0, "qty": 100, "entry_price": 1.0}   # 1/100 → 100bps
    assert st.fee_bps(edge) == pytest.approx(100.0)
    assert st.is_fee_anomaly(edge) is False
    # 数据缺失 ⇒ None / 不剔 (不因数据缺失误杀样本 = 保守方向)
    assert st.fee_bps({"fees_est": None, "qty": 10, "entry_price": 1.0}) is None
    assert st.is_fee_anomaly({"qty": 10, "entry_price": 1.0}) is False
    assert st.is_fee_anomaly({"fees_est": 5.0, "qty": 0, "entry_price": 1.0}) is False


def test_settler_baseline_reproducible_from_live_db():
    """⛔ 预注册基线必须能从真 DB 逐字复现 (防基线被口算/事后篡改)。

    这是本 RFC 最重要的契约: 判据 1/2/4 全挂在基线上, 基线错 = 判据全废。
    冻结于 2026-10-10; 重算须逐字一致 (含 n / 净均 / 日聚类 t)。
    """
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st

    if not st.DB.exists():
        pytest.skip("生产 DB 不在本机 (基线窗口见 st.BASELINE_SINCE/UNTIL)")

    # 基线窗口从**结算器常量**读 (不硬编码日期) ⇒ 换窗只改一处, 本测试自动跟随。
    # v2 (2026-10-10 用户裁「从8月份开始统计, 早期的策略和逻辑不够成熟」): [2026-08-01, 2026-10-10)
    assert st.BASELINE_SINCE == "2026-08-01", (
        f"基线窗口起点应为 2026-08-01 (用户裁 8 月起统计), 实得 {st.BASELINE_SINCE}")
    # 2026-10-15 修复右删失: 装载口径按 trade_date(入场日) 取行, 窗内入场但 10-10 后
    # 才结算的行会晚熟进窗 (实测 n 441→443, 净额漂 −650) ⇒ 复现端补「结算日 < 冻结日」
    # 过滤, 与 10-10 冻结时刻的语义逐字一致 (彼时 exit_price>0 即已结算)。
    # (E 表教训: 只钉冻窗/冻结行; 生产装载器不动。)
    pre = [r for r in st.load_sim_pnl(st.BASELINE_SINCE)
           if r["trade_date"] < st.BASELINE_UNTIL
           and (r.get("exit_time") or "")[:10] < st.BASELINE_UNTIL]
    assert pre, "基线窗口无样本 — DB 口径变了?"

    b_all = st._pnl_block(pre)
    b_sl = st._pnl_block(pre, lambda r: (r.get("exit_reason") or "") == "SL_INITIAL")
    b_rest = st._pnl_block(pre, lambda r: (r.get("exit_reason") or "") != "SL_INITIAL")
    b_win = st._pnl_block(pre, lambda r: st._in_open_win(r.get("entry_time")))

    assert (b_all["n"], b_all["net_sum"], b_all["net_mean"], b_all["t"]) == (
        st.BASELINE["all"]["n"], st.BASELINE["all"]["net_sum"],
        st.BASELINE["all"]["net_mean"], st.BASELINE["all"]["t"]), \
        f"BASE_ALL 漂移: 重算 {b_all} vs 冻结 {st.BASELINE['all']}"
    assert (b_sl["n"], b_sl["net_sum"], b_sl["net_mean"], b_sl["t"]) == (
        st.BASELINE["sl_initial"]["n"], st.BASELINE["sl_initial"]["net_sum"],
        st.BASELINE["sl_initial"]["net_mean"], st.BASELINE["sl_initial"]["t"]), \
        f"BASE_SL_INIT 漂移 (判据1 基准!): 重算 {b_sl} vs 冻结 {st.BASELINE['sl_initial']}"
    assert (b_rest["n"], b_rest["net_sum"], b_rest["net_mean"]) == (
        st.BASELINE["rest"]["n"], st.BASELINE["rest"]["net_sum"],
        st.BASELINE["rest"]["net_mean"]), \
        f"BASE_REST 漂移 (判据4 基准!): 重算 {b_rest} vs 冻结 {st.BASELINE['rest']}"
    # rest_clean = 判据④ **裁决口径**基准 (剔 fee_bps>100, RFC-141 §5.6)
    b_rest_clean = st._pnl_block(
        pre, lambda r: (r.get("exit_reason") or "") != "SL_INITIAL"
        and not st.is_fee_anomaly(r))
    assert (b_rest_clean["n"], b_rest_clean["net_sum"], b_rest_clean["net_mean"]) == (
        st.BASELINE["rest_clean"]["n"], st.BASELINE["rest_clean"]["net_sum"],
        st.BASELINE["rest_clean"]["net_mean"]), \
        (f"BASE_REST_CLEAN 漂移 (判据4 裁决基准!): 重算 {b_rest_clean} "
         f"vs 冻结 {st.BASELINE['rest_clean']}")
    assert b_rest_clean["n"] == b_rest["n"] - st.BASELINE["rest_clean"]["fee_bps_anomaly_dropped"], \
        "剔费笔数必须等于 rest 与 rest_clean 的 n 差 (防静默多剔/少剔)"
    assert (b_win["n"], b_win["net_sum"], b_win["net_mean"], b_win["t"]) == (
        st.BASELINE["open_win"]["n"], st.BASELINE["open_win"]["net_sum"],
        st.BASELINE["open_win"]["net_mean"], st.BASELINE["open_win"]["t"]), \
        f"BASE_OPEN_WIN 漂移 (判据2 基准!): 重算 {b_win} vs 冻结 {st.BASELINE['open_win']}"

    # 判据阈值与基线自洽 (阈值不是凭空挑的, 是基线的固定比例)
    assert abs(st.BASELINE["sl_initial"]["net_mean"] * 0.25
               - st.SL_INIT_MAX_LOSS_PER_TRADE) < 1.0, "判据1 阈值与基线 75% 收敛不自洽"
    assert abs(st.BASELINE["rest"]["net_mean"] * 0.8
               - st.WINNER_MEAN_FLOOR) < 0.02, "判据4 地板与基线 80% 不自洽"
    # 病灶集中度: SL_INITIAL 族亏掉的是全样本净亏的 N 倍 (门存在的理由)
    # ⚠️ v2 窗口下 all.net_sum=−352.67 已接近 0 ⇒ 比值 12.4283 **不稳定** (分母趋零会爆/翻符号),
    #   故不钉死数值, 只钉「方向 + 量级下界」: SL_INITIAL 净亏必须 ≥ 全样本净亏的 5×。
    #   v1 是 1.8482× (分母 −2477.74 远离 0, 当时可钉死); v2 病灶更集中但比值失去可比性。
    ratio = st.BASELINE["sl_initial"]["net_sum"] / st.BASELINE["all"]["net_sum"]
    assert ratio >= 5.0, f"病灶倍数漂移: {ratio} (应 ≥5×; v2 实测 12.4283)"
    assert st.BASELINE["sl_initial"]["net_sum"] < 0 and st.BASELINE["all"]["net_sum"] < 0, \
        "比值同号前提 (两者都亏) 不成立 ⇒ ratio 失去意义, 须重定判据"
    assert st.BASELINE["sl_initial"]["win_rate"] == 0.0, "SL_INITIAL 族胜率应为 0/n"
    # 判据② 的 t 门槛必须**取严** (v2 基线 t 更负, 跟基线走会放松门槛 = 挪门柱)
    assert st.OPEN_WIN_T_FLOOR == -0.3218, "v1 t 严值须钉住, 不得随 v2 基线放松"
    assert st.OPEN_WIN_T_FLOOR > st.BASELINE["open_win"]["t"], (
        f"OPEN_WIN_T_FLOOR ({st.OPEN_WIN_T_FLOOR}) 必须严于 v2 基线 t "
        f"({st.BASELINE['open_win']['t']}), 否则取严逻辑失效")


def test_settler_day_clustered_t_requires_three_days():
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st
    assert st._day_clustered_t([1.0, 2.0]) is None       # n_days<3 不可判 ≠ 0
    assert st._day_clustered_t([1.0, 1.0, 1.0]) is None  # 零方差不可判


def test_venv_interpreter_matches_documented_constraint():
    """解释器版本事实钉: skill/文档曾记 venv=Python 3.8.17, 2026-10-10 实测 3.11.15
    (且 shadow_registry.py:618 已用 PEP585 `list[ShadowItem]` ⇒ 3.8 约束早已失效)。
    钉真实下界, 防再按过期约束写代码或反过来引入更高版本语法。"""
    assert sys.version_info >= (3, 11), f"venv 解释器低于实测基线: {sys.version}"


# ══════════════════════════════════════════════════════════════
# 13. v2 (RFC-141 §5.10, 2026-10-15 用户裁): mid-mid 同尺 + grace 持续性
#     + 弃单后观测 — 缘起 v1 实证: INTC +1.6bp 纯价差噪声杀 / 弃单率 61%
# ══════════════════════════════════════════════════════════════
def test_mid_ruler_ignores_spread_bounce_intc_case():
    """⛔ v2 主病灶回归钉: 锚=bid 92.32, 复核 last=92.33 (+1.6bp 价差跳动)。

    v1 混尺 (锚 bid × last) ⇒ 判反转弃单; v2 mid-mid: last 打在 ask 侧 (92.33) 但
    mid 92.33→92.32 实际持平微降 (SHORT 顺向) ⇒ CONFIRMED。
    这正是 2026-10-15 日志里 INTC 被误杀的形态。
    """
    db = _FakeDB()
    t0 = _now()
    cfg = {**CFG_ON}   # 缺省即 v2: ruler=mid, grace 3/2
    # arm 时刻: bid 92.32 ask 92.34 ⇒ mid_arm=92.33 (锚=bid=92.32 来自 reprice)
    ecg.gate_at_execute(_inst(direction="SELL_SHORT"), db, {"entry_price": 92.32}, t0, cfg,
                        scheduler=_FakeSched((92.32, 92.32, 92.34)))
    assert ecg._PENDING[1]["mid_arm"] == pytest.approx(92.33)
    # 复核: last=92.33 (>锚 ⇒ legacy 会判反转) 但 bid/ask=92.31/92.33 ⇒ mid=92.32 ≤ 92.33
    r = ecg.sweep_entry_confirmations(
        _FakeSched((92.33, 92.31, 92.33)), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and r["abandoned"] == 0, \
        "mid 尺下价差跳动不得判反转 (INTC 1.6bp 误杀回归)"
    row = json.loads(ecg._FORWARD_PATH.read_text().splitlines()[0])
    assert row["ruler_used"] == "mid" and row["mid_move_pct"] is not None


def test_mid_ruler_real_reversal_still_abandons():
    """mid 尺不是放宽: mid 真反向持续 ⇒ 照样 ABANDONED (防「换尺=门变松」误读)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(direction="SELL_SHORT"), db, {"entry_price": 92.32}, t0,
                        dict(CFG_ON), scheduler=_FakeSched((92.32, 92.32, 92.34)))
    # mid 92.33 → 92.60 (真反抽 29bp) 且持续
    rs = _sweep_series(_FakeSched((92.62, 92.58, 92.62)), db, t0, start_sec=122, steps=3)
    assert rs[0]["abandoned"] == 0 and rs[-1]["abandoned"] == 1
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_ABANDONED and st["ruler_used"] == "mid"


def test_mid_ruler_falls_back_to_legacy_when_quotes_incomplete():
    """bid/ask 缺失/交叉 ⇒ 该笔回落 legacy 混尺 + ruler_used 留痕 (不阻断, 不误用)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, dict(CFG_ON),
                        scheduler=_FakeSched(100.0))     # price-only ⇒ mid_arm=None
    assert ecg._PENDING[1]["mid_arm"] is None
    r = ecg.sweep_entry_confirmations(_FakeSched(101.0), db, t0 + timedelta(seconds=125))
    assert r["confirmed"] == 1
    assert db.merged[-1][1]["entry_confirm"]["ruler_used"] == "legacy"
    # 交叉报价同样回落 (bid>ask 是坏数据, 不得拿来算 mid)
    db2 = _FakeDB()
    ecg.gate_at_execute(_inst(i=9), db2, {"entry_price": 100.0}, t0, dict(CFG_ON),
                        scheduler=_FakeSched((100.0, 100.05, 99.95)))
    assert ecg._PENDING[9]["mid_arm"] is None, "交叉报价不得算 mid"


def test_mid_ruler_falls_back_when_book_is_proxy():
    """Futu 无真实盘口时 bid/ask=last 兜底 ⇒ 不得伪装成 mid 尺。

    生产路径: push/get_stock_quote 会用 last 回填双边价并带 proxy 哨兵;
    若直接按 b/a>0 判有效, 会把 legacy 行标成 ruler_used=mid, 污染 v2 分代。
    """
    db = _FakeDB()
    t0 = _now()
    proxy_ctx = {"price": 101.0, "bid": 101.0, "ask": 101.0,
                 "lv3_book_is_proxy": True}
    ecg.gate_at_execute(_inst(i=11), db, {"entry_price": 100.0}, t0, dict(CFG_ON),
                        scheduler=_FakeSched(proxy_ctx))
    assert ecg._PENDING[11]["mid_arm"] is None

    ecg.gate_at_execute(_inst(i=12), db, {"entry_price": 100.0}, t0, dict(CFG_ON),
                        scheduler=_FakeSched((100.0, 99.95, 100.05)))
    assert ecg._PENDING[12]["mid_arm"] == pytest.approx(100.0)

    r = ecg.sweep_entry_confirmations(_FakeSched(proxy_ctx), db,
                                      t0 + timedelta(seconds=125))
    assert r["confirmed"] == 2 and r["abandoned"] == 0
    states = {inst_id: patch["entry_confirm"]
              for inst_id, patch in db.merged if inst_id in (11, 12)}
    assert all(st["ruler_used"] == "legacy" for st in states.values())
    assert all(st["mid_move_pct"] is None for st in states.values())


def test_grace_recovers_to_confirmed():
    """grace 内任一扫回同侧 ⇒ CONFIRMED (一次性噪声不再杀死信号 — v2 用户核心诉求)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, dict(CFG_ON))
    r1 = ecg.sweep_entry_confirmations(_FakeSched(99.5), db, t0 + timedelta(seconds=125))
    assert r1["confirmed"] == 0 and r1["abandoned"] == 0 and r1["pending"] == 1, \
        "首见逆向应进 grace"
    r2 = ecg.sweep_entry_confirmations(_FakeSched(100.5), db, t0 + timedelta(seconds=135))
    assert r2["confirmed"] == 1, "grace 内回正必须确认放行"
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_CONFIRMED and st["delay_actual_sec"] == 135
    assert st["grace"]["adverse_votes"] == 0 and st["grace"]["outcome"] == "recovered"


def test_grace_unavailable_price_does_not_consume_votes():
    """取价失败 ≠ 逆向证据: grace 次数/票数都不消耗 (断价 30s 不得白送 2 票)。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, dict(CFG_ON))
    ecg.sweep_entry_confirmations(_FakeSched(99.5), db, t0 + timedelta(seconds=125))  # grace 起
    r_none = ecg.sweep_entry_confirmations(_FakeSched(None), db, t0 + timedelta(seconds=135))
    assert r_none["pending"] == 1
    assert ecg._PENDING[1]["grace"] == {"left": 3, "adv": 0}, "断价消耗了 grace 计数"
    rs = _sweep_series(_FakeSched(99.4), db, t0, start_sec=145, steps=2)
    assert rs[-1]["abandoned"] == 1


def test_grace_exhausted_without_enough_votes_confirms():
    """grace 耗尽仍不足票 ⇒ CONFIRMED (fail toward trade, 与 fail-open 同向)。

    正常参数 (2/3) 下此分支不可达 (任一非同侧即票、同侧即确认); 用 votes=3/sweeps=2
    的错配参数钉住这条防御路径的语义, 防有人改成「耗尽即弃」(那是 fail-close 方向)。
    """
    db = _FakeDB()
    t0 = _now()
    cfg = {**CFG_ON, "grace_sweeps": 2, "grace_votes": 3}
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, cfg)
    rs = _sweep_series(_FakeSched(99.5), db, t0, start_sec=125, steps=3)
    assert rs[-1]["confirmed"] == 1 and rs[-1]["abandoned"] == 0
    assert db.merged[-1][1]["entry_confirm"]["grace"]["outcome"] == "unpersisted"


def test_legacy_escape_hatch_is_v1_verbatim():
    """回滚契约: ruler=legacy + grace_sweeps=0 ⇒ v1 逐字行为 (单点混尺即弃)。"""
    db = _FakeDB()
    t0 = _now()
    cfg = {**CFG_ON, "ruler": "legacy", "grace_sweeps": 0}
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0, cfg)
    r = ecg.sweep_entry_confirmations(_FakeSched(99.0), db, t0 + timedelta(seconds=125))
    assert r["abandoned"] == 1, "回滚臂首见逆向必须即弃 (v1 逐字)"
    row = json.loads(ecg._FORWARD_PATH.read_text().splitlines()[0])
    assert row["ruler_used"] == "legacy" and row["grace"] is None


def test_post_abandon_watch_writes_m5_m10_rows():
    """v2 弃单后观测: +5m/+10m 各补记一行 post (纯观测), 补齐后出表。

    硬接线理由: scheduler 只在 pending_count()>0 时调 sweep ⇒ pending_count 必须含
    watch, 否则全量判完后补记永不落 (钉住这个回归)。
    """
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_inst(), db, {"entry_price": 100.0}, t0,
                        {**CFG_ON, "grace_sweeps": 0})   # 单点即弃, 缩短测试
    ecg.sweep_entry_confirmations(_FakeSched(99.0), db, t0 + timedelta(seconds=125))
    assert len(ecg._POST_WATCH) == 1
    assert ecg.pending_count() == 1, "pending_count 不含 watch ⇒ scheduler 不再调 sweep ⇒ 补记丢失"
    t_ab = t0 + timedelta(seconds=125)
    # +4min 未到点 ⇒ 无补记; +5m ⇒ m5; +10m ⇒ m10 且出表
    ecg.sweep_entry_confirmations(_FakeSched(99.2), db, t_ab + timedelta(seconds=240))
    assert not ecg._FORWARD_PATH.read_text().count("entry_confirm_post")
    ecg.sweep_entry_confirmations(_FakeSched(99.2), db, t_ab + timedelta(seconds=301))
    rows = [json.loads(x) for x in ecg._FORWARD_PATH.read_text().splitlines()]
    posts = [r for r in rows if r.get("type") == "entry_confirm_post"]
    assert len(posts) == 1 and posts[0]["horizon"] == "+5m"
    assert posts[0]["last_move_pct"] == pytest.approx(-0.8, abs=1e-3)  # LONG 100→99.2 逆向
    ecg.sweep_entry_confirmations(_FakeSched(99.1), db, t_ab + timedelta(seconds=601))
    rows = [json.loads(x) for x in ecg._FORWARD_PATH.read_text().splitlines()]
    posts = [r for r in rows if r.get("type") == "entry_confirm_post"]
    assert len(posts) == 2 and posts[-1]["horizon"] == "+10m"
    assert len(ecg._POST_WATCH) == 0 and ecg.pending_count() == 0


def test_settler_load_rows_accepts_both_gens_and_skips_post(tmp_path, monkeypatch):
    """结算器双代通吃: v1/v2 判据行都收并打 _gen 标签; post 观测行计数但不进人群。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
    from scripts import settle_entry_confirm_forward as st
    p = tmp_path / "fwd.jsonl"
    lines = [
        {"type": "entry_confirm", "schema": "entry_confirm_forward_v1", "date": "2026-10-12",
         "arm": "treatment", "status": "ok"},
        {"type": "entry_confirm", "schema": "entry_confirm_forward_v2", "date": "2026-10-15",
         "arm": "treatment", "status": "ok"},
        {"type": "entry_confirm_post", "schema": "entry_confirm_forward_v2",
         "date": "2026-10-15", "horizon": "+5m"},
        {"type": "entry_confirm", "schema": "bogus_schema", "date": "2026-10-15"},
    ]
    p.write_text("\n".join(json.dumps(x) for x in lines), encoding="utf-8")
    monkeypatch.setattr(st, "FWD", p)
    rows = st.load_rows()
    assert [r["_gen"] for r in rows] == ["v1", "v2"]
    assert st.load_rows.post_rows == 1
    # 双窗推导: 有 v2 ⇒ 裁决窗 = v2 首行 date; 无 v2 ⇒ 退回 v1 开门日
    assert st.verdict_window(rows) == ("2026-10-12", "2026-10-15", "v2")
    assert st.verdict_window(rows[:1]) == ("2026-10-12", "2026-10-12", "v1")


# ══════════════════════════════════════════════════════════════
# RFC-153: FADE_SHORT 开盘快跌禁追 (2026-10-20 用户批准 SIM 启用)
# ══════════════════════════════════════════════════════════════
def _fade_inst(i=31):
    inst = _inst(i=i, direction="SELL_SHORT")
    inst["raw_instruction_json"] = json.dumps({"entry_archetype": "FADE_SHORT"})
    return inst


def test_fade_short_opening_fast_drop_abandons_without_grace():
    """SHORT 锚 100, 确认时 98.9 ⇒ opp=+1.1% ≥1% ⇒ 直接弃, 不进 grace。"""
    db = _FakeDB()
    t0 = _now()  # 09:40 ET → session_min=10
    ecg.gate_at_execute(_fade_inst(), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(98.9), db, t0 + timedelta(seconds=122))
    assert r["abandoned"] == 1 and r["pending"] == 0
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_ABANDONED
    assert st["abandon_cause"] == "fade_short_chase_cap"
    assert st["chase_cap_hit"] is True
    assert st["grace"] is None, "禁追门不应进入 grace"
    assert db.skips == [(31, ecg.SKIP_REASON_ABANDONED)]
    rows = [json.loads(x) for x in ecg._FORWARD_PATH.read_text().splitlines()]
    assert rows[-1]["chase_cap_hit"] is True
    assert rows[-1]["abandon_cause"] == "fade_short_chase_cap"
    assert rows[-1]["entry_archetype"] == "FADE_SHORT"


def test_fade_short_below_threshold_still_confirms():
    """SHORT 锚 100, 确认时 99.2 ⇒ opp=+0.8% <1% ⇒ 原确认路径不变。"""
    db = _FakeDB()
    t0 = _now()
    ecg.gate_at_execute(_fade_inst(i=32), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(99.2), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and db.skips == []
    st = db.merged[-1][1]["entry_confirm"]
    assert st["status"] == ecg.ST_CONFIRMED
    assert st.get("chase_cap_hit") is None


def test_fade_short_outside_opening_window_not_capped():
    """09:50 开盘 (session_min=20) 不在 15 分钟窗口 ⇒ 不启用禁追。"""
    db = _FakeDB()
    t0 = datetime(2026, 10, 12, 9, 50, tzinfo=_ET)
    ecg.gate_at_execute(_fade_inst(i=33), db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(98.9), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and db.skips == []


def test_pullback_limit_not_capped():
    """PULLBACK_LIMIT 不受 RFC-153 影响 (只作用于 FADE_SHORT)。"""
    db = _FakeDB()
    t0 = _now()
    inst = _inst(i=34, direction="BUY")
    inst["raw_instruction_json"] = json.dumps({"entry_archetype": "PULLBACK_LIMIT"})
    ecg.gate_at_execute(inst, db, {"entry_price": 100.0}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(101.1), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and db.skips == []


def test_fade_short_cap_disabled_restores_confirm():
    """一行配置回滚 (enabled=false) ⇒ 恢复 RFC-153 前的确认行为。"""
    db = _FakeDB()
    t0 = _now()
    cfg = {**CFG_ON, "fade_short_chase_cap_enabled": False}
    ecg.gate_at_execute(_fade_inst(i=35), db, {"entry_price": 100.0}, t0, cfg)
    r = ecg.sweep_entry_confirmations(_FakeSched(98.9), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and db.skips == []


def test_plan_gap_fields_are_recorded_without_changing_gate():
    """计划锚与执行锚的差值只记录, 不改确认门判定 (2026-10-21 冲突审计)。"""
    db = _FakeDB()
    t0 = _now()
    inst = _inst(i=36, direction="SELL_SHORT")
    inst["raw_instruction_json"] = json.dumps({
        "entry_archetype": "FADE_SHORT",
        "planned_entry": 100.0,
        "confirmation": {"price_level": 100.0, "side": "above"},
    })
    ecg.gate_at_execute(inst, db, {"entry_price": 98.9}, t0, CFG_ON)
    r = ecg.sweep_entry_confirmations(_FakeSched(98.9), db, t0 + timedelta(seconds=122))
    assert r["confirmed"] == 1 and db.skips == []
    rows = [json.loads(x) for x in ecg._FORWARD_PATH.read_text().splitlines()]
    assert rows[-1]["planned_entry"] == 100.0
    assert rows[-1]["plan_gap_pct"] == -1.1
    assert rows[-1]["plan_gap_shadow"] is True
    assert rows[-1]["plan_gap_threshold_pct"] == 1.0
    st = db.merged[-1][1]["entry_confirm"]
    assert st["planned_entry"] == 100.0
    assert st["plan_gap_pct"] == -1.1
