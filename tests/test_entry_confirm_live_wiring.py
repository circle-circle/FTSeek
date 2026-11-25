"""RFC-141 入场延迟确认门 — 端到端接线活体验证 (非合成替身)。

为什么单独一个文件 (而不是塞进 test_entry_confirm_gate.py):
  契约测试用 _FakeDB/_FakeSched 验**门自身语义**; 本文件用真 PlaybookDB (真 schema +
  真 save_instruction 准入门) + 真 PlaybookConfig (生产 YAML) + 真结算器, 验**接线活**。
  「fixture 全绿 ≠ 接线活」是本项目反复踩的坑 (2026-10-10 同批: _FakeDB 缺
  mark_instruction_guardrail_skip ⇒ ABANDONED 落库静默走 except 分支, 36 例全绿却
  从未验到 skip_reason 真写)。本文件就是那道防线。

覆盖 (对应用户目标口径「SIM 盈亏在富途 app 可见」):
  A. defer 期间不写 skip_reason / 不标 executed / 不碰 execution_state ⇒ 下轮仍被取到
     (若这里漏, 指令会永久卡死, 富途 app 上永远看不到成交)
  B. CONFIRMED 后 gate 返回 execute ⇒ 真能走到 execute_instruction (通往富途 SIM 唯一出口)
  C. ABANDONED 落 guardrail SKIP + 前瞻行两臂齐全 (裁决样本完整)
  D. 生产配置真值 / 浅合并不覆盖原 metadata / 孤儿自愈留痕 / holdout 分流
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook import entry_confirm_gate as ecg  # noqa: E402
from app.playbook.db import PlaybookDB  # noqa: E402

_ET = timezone(timedelta(hours=-4))
_T0 = datetime(2026, 10, 12, 9, 40, 0, tzinfo=_ET)
_DATE = "2026-10-12"


class _Router:
    """按 symbol 发价的假行情源 (真 Futu push 不可在测试里驱动)。

    值为标量 → 仅 last (无 bid/ask ⇒ mid=None, v2 回落 legacy 混尺留痕);
    值为 (last, bid, ask) 元组 → 带双边报价 (v2 mid 尺生效)。
    """

    def __init__(self, prices):
        self._p = prices

    def get_full_market_context(self, symbol):
        q = self._p.get(symbol)
        if isinstance(q, tuple):
            return {"price": q[0], "bid": q[1], "ask": q[2]}
        return {"price": q}


class _Sched:
    def __init__(self, prices):
        self.data_router = _Router(prices)


@pytest.fixture()
def env(tmp_path, monkeypatch):
    """真 PlaybookDB (临时文件, 真 ensure_tables schema) + 产物重定向。

    ⚠️ check_same_thread=False: 与生产连接池 db_pool.py:172 同策略
    (thread-local cache handles safety)。漏掉它会在跨线程读库时抛
    ProgrammingError, 而生产不会 ⇒ 探针假红。
    """
    from app.risk import capital_safety

    capital = capital_safety.CapitalSafetySystem(db_path=str(tmp_path / "capital.sqlite"))
    monkeypatch.setattr(capital_safety, "_capital_instance", capital)
    conn = sqlite3.connect(str(tmp_path / "probe.sqlite"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    db = PlaybookDB(conn=conn)
    db.ensure_tables()
    monkeypatch.setattr(ecg, "_FORWARD_PATH", tmp_path / "entry_confirm_forward.jsonl")
    ecg._PENDING.clear()
    ecg._POST_WATCH.clear()
    yield db, tmp_path
    ecg._PENDING.clear()
    ecg._POST_WATCH.clear()
    conn.close()


def _save(db, sym, direction, entry, size=50, did=None):
    """走真 save_instruction (含真准入门) 并核验方向未被改写。

    2026-11-03: 名义额须过**活**地板 (min_notional_usd 2500→5500, 2026-10-31 P3) ——
    夹具写死 size=50 时, 低价标的的名义额 ($4,965 等) 低于新地板 ⇒ 落库即被准入闸改成
    SKIP ⇒ `get_instructions_by_date` 虽能查到, 但方向已被改写 ⇒ 本文件 10 条守卫全红。
    修法: size 取"调用方给的量"与"活地板足额"二者较大 (不改变用例本意, 只别被地板误伤)。
    """
    from tests.helpers.notional import qty_above_min_notional
    size = max(int(size), qty_above_min_notional(entry))
    db.save_instruction({
        "date": _DATE, "symbol": sym, "direction": direction, "entry_price": entry,
        "stop_loss": entry * 0.97, "take_profit": entry * 1.06,
        "position_size": size, "confidence": 80,
        "decision_id": did or f"DA-{sym}",
        "metadata_json": json.dumps({"original_instruction": {"keep": True}}),
    })
    row = [r for r in db.get_instructions_by_date(_DATE) if r["symbol"] == sym]
    assert row, f"{sym} 未入库"
    r = row[0]
    assert r["direction"] == direction, (
        f"{sym} 被准入门改写 direction={r['direction']} (skip={r['skip_reason']}) "
        f"⇒ 探针人群不对, 需提高 confidence 或另选标的")
    return r


def _cfg_t():
    """生产 cfg 但 holdout 显式置 0 (验 treatment 路径); holdout 臂另有专测。

    ⚠️ 2026-10-31: `entry_confirm_enabled` 生产真值已按用户裁**关门** (RFC-141 §5 案例3)。
    本文件验的是**门机制**是否接线正确, 不该随治理开关一起失效 ⇒ `enabled` 显式强制 True。
    (若跟着生产关 ⇒ 13 例集体假红, 且某天门被重开时这些用例又会"自己变绿" = 覆盖不可信。)
    """
    from app.playbook.config import PlaybookConfig
    return {**ecg.confirm_gate_config(PlaybookConfig()), "holdout_pct": 0, "enabled": True}


# ══════════════════════════════════════════════════════════════════
# A. defer 不改变任何「可执行性」标志 ⇒ 指令不会永久卡死
# ══════════════════════════════════════════════════════════════════
def test_defer_keeps_instruction_pickable_next_cycle(env):
    """用户目标口径: SIM 盈亏须在富途 app 可见 ⇒ defer 绝不能让指令消失。

    三个标志任一被写坏, auto_execute 下轮就取不到它 (executed 过滤 /
    direction=='SKIP' 分支 / INVALIDATED_SKIP_REASONS), 单子永远到不了富途。
    """
    db, _ = env
    r = _save(db, "US.DEFER", "BUY", 100.0)
    assert ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9, "position_size": 55},
                              _T0, _cfg_t()) == "defer"
    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.DEFER"][0]
    assert not r2["skip_reason"], f"defer 写了 skip_reason={r2['skip_reason']!r} ⇒ 下轮被过滤"
    assert int(r2["executed"] or 0) == 0, "defer 标了 executed ⇒ 下轮被过滤"
    assert str(r2["execution_state"]) == "PENDING", (
        f"execution_state 被改成 {r2['execution_state']} ⇒ 违反 C3 (broker 镜像状态机)")
    assert [x for x in db.get_instructions_by_date(_DATE) if x["id"] == r2["id"]], \
        "下轮取数捞不到该指令"
    assert ecg.defer_pending(dict(r2), _T0) is True, "pending 期间未拦住 ⇒ 锚会被 reprice 漂"


def test_anchor_is_reprice_execution_anchor_not_plan_anchor(env):
    """C1: 锚必须是 arm 时刻 reprice 执行锚。

    dual_anchor 影子 573 行实测: 计划锚 vs 执行锚分歧中位 2.10%、60.4% 行分歧>2%、
    trigger_divergent 38% ⇒ 用盘前计划锚 = 静默换尺子, 翻转超三分之一判定。
    """
    db, _ = env
    r = _save(db, "US.ANCHOR", "BUY", 100.0)
    ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9}, _T0, _cfg_t())
    iid = int(r["id"])
    assert ecg._PENDING[iid]["anchor"] == 105.9, (
        f"锚={ecg._PENDING[iid]['anchor']} 不是 reprice 执行锚 105.9 (盘前计划锚是 100.0)")


def test_flag_lands_in_metadata_without_clobbering(env):
    """旗标走 metadata_json.entry_confirm (零 schema 迁移) + 浅合并不覆盖原有键。

    DEB-20260910 §18.3 教训: 整块覆盖 metadata 会静默丢掉别人写的字段。
    """
    db, _ = env
    r = _save(db, "US.FLAG", "BUY", 100.0)
    ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9}, _T0, _cfg_t())
    md = json.loads([x for x in db.get_instructions_by_date(_DATE)
                     if x["symbol"] == "US.FLAG"][0]["metadata_json"])
    assert md["entry_confirm"]["status"] == ecg.ST_PENDING
    assert md["original_instruction"] == {"keep": True}, "浅合并覆盖了原有 metadata 键"


# ══════════════════════════════════════════════════════════════════
# B. CONFIRMED ⇒ 真能走到 execute_instruction (通往富途 SIM 的唯一出口)
# ══════════════════════════════════════════════════════════════════
def test_confirmed_reaches_execute_instruction_path(env):
    """端到端: arm → 120s 后仍站锚同侧 → CONFIRMED → 下轮 gate 放行 → 调 service。

    本测试不真下单 (周末休市且不该真触发 broker), 但验「gate 返回 execute」这个
    唯一分支条件 —— auto_execute_flow 里 act=='execute' 才会调 execute_instruction。
    """
    db, _ = env
    r = _save(db, "US.FUTU", "BUY", 100.0)
    iid = int(r["id"])
    assert ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9, "position_size": 55},
                              _T0, _cfg_t()) == "defer"

    sw = ecg.sweep_entry_confirmations(_Sched({"US.FUTU": 107.2}), db,
                                       _T0 + timedelta(seconds=122))
    assert sw["confirmed"] == 1 and sw["abandoned"] == 0, f"sweep 判定不对: {sw}"
    assert ecg.pending_count() == 0, "CONFIRMED 后未清 pending"

    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.FUTU"][0]
    ec = json.loads(r2["metadata_json"])["entry_confirm"]
    assert ec["status"] == ecg.ST_CONFIRMED
    assert ec["px_at_confirm"] == 107.2, "复核价未留痕 ⇒ 事后无法审计判定依据"
    assert ec["delay_actual_sec"] == 122, "实际延迟未留痕 (契约: ≤10s 快循环误差)"
    assert ec["opp_cost_pct"] == pytest.approx((107.2 - 105.9) / 105.9 * 100, abs=1e-4), \
        "机会成本与影子判据公式不一致"
    assert ecg.defer_pending(dict(r2), _T0) is False, "CONFIRMED 后仍被拦 ⇒ 永远无法执行"

    act = ecg.gate_at_execute(dict(r2), db, {"entry_price": 107.2},
                              _T0 + timedelta(seconds=180), _cfg_t())
    assert act == "execute", f"CONFIRMED 后 gate 未放行 (返回 {act}) ⇒ 单子到不了富途 SIM"

    # 真出口: auto_execute_flow 在 act=='execute' 时调的就是这个签名
    calls = []

    class _Svc:
        def execute_instruction(self, **kw):
            calls.append(kw)
            return {"success": True}

    _Svc().execute_instruction(instruction_id=iid, acknowledge_risk=True,
                              custom_params={"entry_price": 107.2, "position_size": 55})
    assert calls and calls[0]["instruction_id"] == iid


def test_confirmed_never_marked_skip(env):
    """⛔ 红线: CONFIRMED 绝不能落 SKIP (落了单子就到不了富途, app 上什么都看不见)。"""
    db, _ = env
    r = _save(db, "US.NOSKIP", "BUY", 100.0)
    ecg.gate_at_execute(dict(r), db, {"entry_price": 100.0}, _T0, _cfg_t())
    ecg.sweep_entry_confirmations(_Sched({"US.NOSKIP": 101.0}), db,
                                 _T0 + timedelta(seconds=125))
    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.NOSKIP"][0]
    assert r2["direction"] == "BUY", f"确认单被改成 SKIP: {r2['direction']}"
    assert not r2["skip_reason"]
    assert json.loads(r2["metadata_json"])["entry_confirm"].get("skip_marked") in (None, False)


# ══════════════════════════════════════════════════════════════════
# C. ABANDONED: 唯一被拦路径, 且必须可追溯
# ══════════════════════════════════════════════════════════════════
def test_abandoned_marks_skip_and_keeps_forward_row(env):
    """MDB 9/28 秒杀族靶形态: SHORT @306.46 → 318.43 (2min 内反抽)。

    放弃单必须落 guardrail SKIP (reason=entry_confirm_reversed), 否则库里它仍是
    「活跃待执行」却永不执行 ⇒ 复盘/app 分不清「没触发」与「被门放弃」, 误导性
    比看不出来更糟。
    """
    db, tmp = env
    _save(db, "US.KILL", "SELL_SHORT", 306.46, size=19)
    _save(db, "US.LIVE", "BUY", 100.0, size=55)
    cfg = _cfg_t()
    r_kill = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.KILL"][0]
    r_live = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.LIVE"][0]
    ecg.gate_at_execute(dict(r_kill), db, {"entry_price": 306.46, "position_size": 19}, _T0, cfg)
    ecg.gate_at_execute(dict(r_live), db, {"entry_price": 105.9, "position_size": 55}, _T0, cfg)

    # v2 (RFC-141 §5.10): 首见逆向进 grace, 持续 ≥2 票才弃 — 连扫 3 轮走完 grace 流
    n_ab, n_cf = 0, 0
    for i in range(3):
        sw = ecg.sweep_entry_confirmations(
            _Sched({"US.KILL": 318.43, "US.LIVE": 107.2}), db,
            _T0 + timedelta(seconds=122 + i * 10))
        n_ab += sw["abandoned"]
        n_cf += sw["confirmed"]
    assert n_ab == 1 and n_cf == 1, f"grace 流合计: abandoned={n_ab} confirmed={n_cf}"

    rk = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.KILL"][0]
    ec = json.loads(rk["metadata_json"])["entry_confirm"]
    assert ec["status"] == ecg.ST_ABANDONED
    assert ec["grace"]["outcome"] == "persisted" and ec["grace"]["adverse_votes"] >= 2
    assert rk["direction"] == "SKIP", "放弃单未落 guardrail SKIP ⇒ 不可追溯"
    assert rk["skip_reason"] == ecg.SKIP_REASON_ABANDONED == "entry_confirm_reversed"
    assert ec["skip_marked"] is True, "落 SKIP 成败未留痕"
    assert ecg.defer_pending(dict(rk), _T0) is True, "ABANDONED 未终态拦住"

    # 前瞻产物两臂齐全 (裁决样本完整性: 放弃与确认都留痕)
    rows = [json.loads(x) for x in
            (tmp / "entry_confirm_forward.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 2
    by_sym = {r["symbol"]: r for r in rows}
    assert by_sym["US.KILL"]["still_triggers"] is False
    assert by_sym["US.LIVE"]["still_triggers"] is True
    assert all(r["schema"] == "entry_confirm_forward_v2" for r in rows)
    assert all(r["arm"] == "treatment" for r in rows), "holdout_pct=0 却出现 holdout 臂行"


def test_gate_and_settler_share_one_artifact_path():
    """⛔ 路径同一性契约: 门写 ecg._FORWARD_PATH, 结算器读 st.FWD。

    两者若漂移 ⇒ 结算器读空文件、输出 INSUFFICIENT, 而门其实天天在拦
    ⇒ 假「样本不够」(裁决永久卡住, 且看不出来是路径错)。
    本测试不用 env fixture (它会把门的路径重定向到 tmp), 必须在生产真值上比。
    """
    from scripts import settle_entry_confirm_forward as st
    from app.playbook.entry_confirm_gate import _FORWARD_PATH as _GATE_FWD
    assert Path(st.FWD).resolve() == Path(_GATE_FWD).resolve(), (
        f"结算器读 {st.FWD} 而门写 {_GATE_FWD} ⇒ 两把尺子 (结算恒读空)")
    assert Path(st.OUT).parent == Path(st.FWD).parent, "结算产物与样本不同目录"
    assert Path(st.DB).name == "ftseek_us.db", f"结算器读错库: {st.DB}"


def test_settler_consumes_gate_artifact_without_change(env, monkeypatch):
    """结算器与门同尺: 产物直接可吃, 不需任何适配 (否则=两把尺子)。"""
    from scripts import settle_entry_confirm_forward as st
    db, tmp = env
    monkeypatch.setattr(st, "FWD", tmp / "entry_confirm_forward.jsonl")
    _save(db, "US.SET", "BUY", 100.0, size=55)
    r = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.SET"][0]
    ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9, "position_size": 55}, _T0, _cfg_t())
    ecg.sweep_entry_confirmations(_Sched({"US.SET": 107.2}), db, _T0 + timedelta(seconds=122))

    agg = st._agg(st.load_rows(), {})
    assert agg["n"] == 1 and agg["n_confirmed"] == 1 and agg["n_abandoned"] == 0
    assert agg["avoidance_rate"] == 0.0
    # notional / session_min 供 R1 分层 (SHORT×开盘窗)
    row = json.loads((tmp / "entry_confirm_forward.jsonl").read_text().splitlines()[0])
    assert row["notional"] == round(105.9 * 55, 2), "notional 未用 reprice 后锚×仓位"
    assert row["session_min"] == 10, "session_min 应为 09:40 − 09:30 = 10 分钟"


# ══════════════════════════════════════════════════════════════════
# D. 配置 / 孤儿自愈 / holdout 分流
# ══════════════════════════════════════════════════════════════════
def test_production_config_end_to_end():
    """生产 YAML 真值 (不经替身) — RFC-158 用户批准重开 v2 主门。

    20% 同期对照臂用于归因; 未预注册的 FADE_SHORT 追价帽保持关闭。
    """
    from app.playbook.config import PlaybookConfig
    cfg = ecg.confirm_gate_config(PlaybookConfig())
    assert cfg == {"enabled": True, "delay_sec": 120, "holdout_pct": 20, "ttl_sec": 600,
                   "ruler": "mid", "grace_sweeps": 3, "grace_votes": 2,
                   "fade_short_chase_cap_enabled": False,
                   "fade_short_max_opp_pct": 1.0, "fade_short_window_min": 15}, cfg


def test_orphan_self_heals_after_restart_with_trace(env):
    """C6: pending 是内存态 (~120s), 重启后旗标残留 PENDING 而内存空 ⇒
    按旗标重建窗口续上门控 (不得跳过确认立即放行), 窗口走完照常执行 ——
    同时不得永久卡死 (富途 app 上永远看不到它), 那是本门最坏的失效模式。
    """
    db, _ = env
    _save(db, "US.ORPH", "BUY", 50.0)
    r = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.ORPH"][0]
    assert ecg.gate_at_execute(dict(r), db, {"entry_price": 50.0}, _T0, _cfg_t()) == "defer"
    ecg._PENDING.clear()                      # 模拟进程重启
    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.ORPH"][0]
    assert json.loads(r2["metadata_json"])["entry_confirm"]["status"] == ecg.ST_PENDING
    assert ecg.defer_pending(dict(r2), _T0) is False, (
        "auto_execute 的第一道 defer_pending 已把孤儿拦死, gate_at_execute 自愈不可达")
    assert ecg.gate_at_execute(dict(r2), db, {"entry_price": 50.0}, _T0, _cfg_t()) == "defer", \
        "重启后必须按旗标重建窗口, 不得把 120s 确认降级为立即执行"
    assert ecg._PENDING[int(r2["id"])]["anchor"] == 50.0
    ecg.sweep_entry_confirmations(_Sched({"US.ORPH": 50.5}), db, _T0 + timedelta(seconds=121))
    r3 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.ORPH"][0]
    confirmed = json.loads(r3["metadata_json"])["entry_confirm"]
    assert confirmed["status"] == ecg.ST_CONFIRMED and confirmed.get("confirmed_at"), \
        f"重建窗口未走完判定: {confirmed}"
    assert ecg.gate_at_execute(dict(r3), db, {"entry_price": 50.0}, _T0 + timedelta(seconds=121),
                               _cfg_t()) == "execute"


def test_holdout_arm_split_is_deterministic_and_zero_in_production():
    """holdout_pct=0 必须=全量走门, 绝不能反过来变成门整体失效。

    边界陷阱: 若 _holdout_bucket 漏掉 pct<=0 短路 (或写成 `<= pct`), 0% 会命中全部
    ⇒ 全部照常执行不设防 ⇒ 门看着开着 (enabled=true) 却零作用, 而 SIM 结果与基线
    无差异会被误读成「门无效」而不是「门没生效」。
    """
    assert sum(1 for i in range(1, 501) if ecg._holdout_bucket(i, 0)) == 0
    assert sum(1 for i in range(1, 501) if ecg._holdout_bucket(i, 100)) == 500
    # 反向钉: 分流函数本身在工作 (不是恒 False 僵尸, 否则上面的 0% 断言无意义)
    assert 60 < sum(1 for i in range(1, 501) if ecg._holdout_bucket(i, 20)) < 140
    # 确定性: 同 inst_id 恒定同臂 (跨轮/跨重启一致, 否则人群会漂)
    assert ecg._holdout_bucket(7, 20) == ecg._holdout_bucket(7, 20)


def test_holdout_arm_executes_normally_and_logs_control_row(env):
    """holdout 100% 臂: 照常执行 + 落对照行 (机制 inert 保留, 改配置值即恢复)。"""
    db, tmp = env
    _save(db, "US.HOLD", "BUY", 70.0)
    r = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.HOLD"][0]
    cfg = {**_cfg_t(), "holdout_pct": 100}
    assert ecg.gate_at_execute(dict(r), db, {"entry_price": 70.0}, _T0, cfg) == "execute"
    assert ecg.pending_count() == 0, "holdout 臂不应落 pending"
    rows = [json.loads(x) for x in
            (tmp / "entry_confirm_forward.jsonl").read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 1 and rows[0]["arm"] == "holdout"
    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.HOLD"][0]
    assert json.loads(r2["metadata_json"])["entry_confirm"]["status"] == ecg.ST_HOLDOUT


def test_gate_off_is_true_noop(env):
    """门关闭 ⇒ 恒 execute + 零产物 (回滚路径必须真是零行为变更)。"""
    db, tmp = env
    _save(db, "US.OFF", "BUY", 100.0)
    r = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.OFF"][0]
    cfg = {**ecg.confirm_gate_config(), "enabled": False}
    assert ecg.gate_at_execute(dict(r), db, {"entry_price": 105.9}, _T0, cfg) == "execute"
    assert ecg.pending_count() == 0
    assert not (tmp / "entry_confirm_forward.jsonl").exists()
    assert ecg.defer_pending(dict(r), _T0) is False


# ══════════════════════════════════════════════════════════════════
# E. v2 (RFC-141 §5.10): mid-mid 同尺端到端 + 弃单后观测补记
# ══════════════════════════════════════════════════════════════════
def test_v2_mid_ruler_spread_bounce_survives_end_to_end(env):
    """INTC 误杀形态端到端: 锚=bid, 复核 last 微高于锚但 mid 未反向 ⇒ CONFIRMED。

    v1 混尺下这笔必死 (last 92.33 > 锚 92.32 即反转); v2 mid-mid 下同尺对比,
    价差跳动不再误判 — 2026-10-15 生产日志实证病灶的接线级回归。
    """
    db, tmp = env
    r = _save(db, "US.INTCLKE", "SELL_SHORT", 92.30, size=30)
    cfg = _cfg_t()
    assert cfg["ruler"] == "mid", "生产配置应为 mid 尺 (否则本端到端测的是空气)"
    # arm: 带双边报价的 scheduler ⇒ mid_arm=(92.32+92.34)/2=92.33, 锚=92.32 (reprice bid)
    assert ecg.gate_at_execute(
        dict(r), db, {"entry_price": 92.32, "position_size": 30}, _T0, cfg,
        scheduler=_Sched({"US.INTCLKE": (92.32, 92.32, 92.34)})) == "defer"
    iid = int(r["id"])
    assert ecg._PENDING[iid]["mid_arm"] == pytest.approx(92.33), "arm 未快照 mid"
    # 复核: last=92.33 微高于锚 (v1 杀) 但 mid=92.32 ≤ mid_arm=92.33 (v2 放)
    sw = ecg.sweep_entry_confirmations(
        _Sched({"US.INTCLKE": (92.33, 92.31, 92.33)}), db, _T0 + timedelta(seconds=122))
    assert sw["confirmed"] == 1 and sw["abandoned"] == 0
    r2 = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.INTCLKE"][0]
    ec = json.loads(r2["metadata_json"])["entry_confirm"]
    assert ec["status"] == ecg.ST_CONFIRMED and ec["ruler_used"] == "mid"
    assert ec["mid_move_pct"] is not None
    assert not r2["skip_reason"], "mid 尺放行单不得落 SKIP"


def test_v2_abandoned_post_observation_rows_land(env):
    """弃单后 +5m/+10m 补记落到同一 jsonl (type=entry_confirm_post), 供裁决日分解
    「真避免 vs 误杀」— 观测行不撞 _agg (结算器按 type 过滤, 另有专测)。"""
    db, tmp = env
    _save(db, "US.POST", "BUY", 100.0, size=40)
    r = [x for x in db.get_instructions_by_date(_DATE) if x["symbol"] == "US.POST"][0]
    cfg = {**_cfg_t(), "grace_sweeps": 0}   # 单点即弃, 缩短测试路径
    ecg.gate_at_execute(dict(r), db, {"entry_price": 100.0}, _T0, cfg)
    ecg.sweep_entry_confirmations(_Sched({"US.POST": 99.0}), db, _T0 + timedelta(seconds=122))
    assert ecg.pending_count() == 1, "watch 未计入 pending_count ⇒ scheduler 不再调 sweep"
    t_ab = _T0 + timedelta(seconds=122)
    ecg.sweep_entry_confirmations(_Sched({"US.POST": 99.3}), db, t_ab + timedelta(seconds=301))
    ecg.sweep_entry_confirmations(_Sched({"US.POST": 99.4}), db, t_ab + timedelta(seconds=601))
    rows = [json.loads(x) for x in
            (tmp / "entry_confirm_forward.jsonl").read_text(encoding="utf-8").splitlines()]
    posts = [x for x in rows if x.get("type") == "entry_confirm_post"]
    assert [p["horizon"] for p in posts] == ["+5m", "+10m"]
    assert all("last_from_confirm_pct" in p and "mid_from_confirm_pct" in p for p in posts)
    assert all("px_at_confirm" in p and "mid_at_confirm" in p for p in posts)
    assert all(p["symbol"] == "US.POST" and p["direction"] == "LONG" for p in posts)
    # 逆向幅度方向归一: LONG 100→99.3 = −0.7%
    assert posts[0]["last_move_pct"] == pytest.approx(-0.7, abs=1e-3)
    assert ecg.pending_count() == 0


# ══════════════════════════════════════════════════════════════════
# E. RFC-153 禁追门 进度读数 (只印不判) 的人群/窗口契约
# ══════════════════════════════════════════════════════════════════
def test_rfc153_progress_cohort_and_window():
    """人群 = FADE_SHORT ∧ 开盘 window ∧ date ≥ 生效首日; 判据用样本门, 不是"有行就算"。

    三条否定样本 (非 FADE_SHORT / 开盘窗外 / 生效首日之前) 必须被挡掉 —— 否则进度行会
    虚高, 30 笔门提前到达。
    """
    from scripts import settle_entry_confirm_forward as st

    def row(**kw):
        base = {"type": "entry_confirm", "entry_archetype": "FADE_SHORT",
                "session_min": 5, "date": st.RFC153_SINCE, "still_triggers": False,
                "chase_cap_hit": True, "opp_cost_pct": 1.4, "decision_id": "d1"}
        base.update(kw)
        return base

    rows = [
        row(),                                             # ✅ 计入
        row(entry_archetype="PULLBACK_LIMIT"),             # ✗ 非 FADE_SHORT
        row(session_min=30),                               # ✗ 开盘窗外
        row(date="2026-10-20"),                            # ✗ 生效首日之前
        row(still_triggers=True, chase_cap_hit=False,       # ✅ 确认成交, 计入
            opp_cost_pct=0.1, decision_id="d2"),
    ]
    joined = {"d2": {"net": -50.0, "exit_reason": "SL_INITIAL"}}
    blk = st.rfc153_progress(rows, joined=joined)
    assert blk["n_samples"] == 2, blk
    assert blk["n_chase_cap_abandoned"] == 1
    assert blk["n_confirmed"] == 1
    assert blk["confirmed_net_mean"] == -50.0
    assert blk["confirmed_sl_initial_n"] == 1
    assert blk["gate"]["pass"] is False          # 2 笔远未达 30 笔门
