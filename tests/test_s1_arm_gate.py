"""S1 臂闸契约测试 (判据 B23 / 注册表 s1_short_after_open).

钉住七件:
  ① enabled=False 时全放行、不查库 (零行为变更语义);
  ② 方向 (P10, 用户 2026-11-16 裁选项 1): 非臂方向**放行且不施加任何 S1 约束**;
     臂方向 (SELL_SHORT) 才进五道门;
  ③ 时段: ET<585 分钟拒 (黑窗虽有独立闸, 臂自身必须再守一次);
  ④ 帽: 单日笔数 / 在仓敞口 / 单笔缩股 (缩股不放大, 整股向下);
  ⑤ kill switch: 累计 < -300 或 n>=15 且净 bps<0 即拒 (S1/S2 两条停止条件);
  ⑥ 账本按臂别解析 (P8): target_env 无默认值 ⇒ 漏传即 TypeError, 不静默回落 sim;
  ⑦ 读库失败 fail-close (P9): degraded ⇒ 臂方向拒 + 缩股 0, 非臂方向不受影响。
另钉 cohort 口径与判据字典 B23 一致 (n=88 / +30.75bps 的历史参照), 防谓词漂移。
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.playbook import s1_arm as arm  # noqa: E402


class _LockedError(Exception):
    """替身: 只为触发 except 分支, 不依赖真库锁。"""


def _st(enabled=True, **kw):
    base = {"enabled": enabled, "today": "2026-11-16", "today_count": 0,
            "exposure": 0.0, "killed": None, "cohort": {"n": 0, "net": 0.0},
            "target_env": "sim", "degraded": False}
    base.update(kw)
    return base


# ① 关闸零行为变更
def test_disabled_arm_allows_everything():
    st = _st(enabled=False)
    assert arm.check_entry(st, direction="BUY", entry_minute=560) == (True, "")
    assert arm.check_entry(st, direction="SELL_SHORT", entry_minute=560) == (True, "")
    assert arm.cap_size(st, 1000, 10.0, direction="SELL_SHORT") == 1000


def test_disabled_cycle_state_skips_db(monkeypatch):
    def _boom(*a, **k):
        raise AssertionError("enabled=False 时不得查库")

    monkeypatch.setattr(arm, "_connect", _boom)
    st = arm.cycle_state("2026-11-15", "sim")
    assert st["enabled"] is False and st["exposure"] == 0.0 and st["killed"] is None
    assert st["degraded"] is False and st["target_env"] == "sim"


# ② 方向 (P10)
# 指令表方向词汇实测 (2026-11-16, 双库): playbook_instructions.direction ∈
# {BUY, SELL_SHORT, SKIP}; trade_records.direction ∈ {LONG, SHORT} 是另一套词汇,
# 只用于 S1_WHERE 人群谓词, 不会到达 check_entry。
@pytest.mark.parametrize("direction", ["BUY", "LONG", "SHORT", "", None])
def test_non_arm_direction_passes_unconstrained(direction):
    """P10: 非臂方向放行, 且**不施加任何 S1 约束** (时段/笔数/敞口/kill 全不适用)。

    原实现 return False ⇒ 开闸即把整条 playbook 自动执行流变成只做空, 会抽走同日
    起跑的 RFC-209 前瞻 cohort 七成人群 (实测 LONG 145 / SHORT 58)。
    """
    worst = _st(today_count=99, exposure=1e9, killed="S2_ECON(cum_net=-9999.00)")
    assert arm.check_entry(worst, direction=direction, entry_minute=1) == (True, "")


@pytest.mark.parametrize("direction", ["BUY", "LONG", "", None])
def test_non_arm_direction_not_capped(direction):
    """P10 连带面: cap_size 也必须认方向, 否则多头会被空头臂的 $4,200 帽砍掉。"""
    assert arm.cap_size(_st(), 1000, 10.0, direction=direction) == 1000


def test_arm_direction_still_gated():
    """防过修: P10 只放行非臂方向, SELL_SHORT 的五道门一条都不能松。"""
    assert arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=600) == (True, "")
    assert arm.cap_size(_st(), 1000, 10.0, direction="SELL_SHORT") == 420


# ③ 时段
@pytest.mark.parametrize("minute", [570, 584, None])
def test_before_0945_rejected(minute):
    ok, why = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=minute)
    assert ok is False and why.startswith("s1_arm_before_open")


def test_exactly_0945_allowed():
    ok, _ = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=585)
    assert ok is True


# ④ 帽
def test_daily_count_cap():
    ok, why = arm.check_entry(_st(today_count=2), direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_daily_count")


def test_exposure_cap():
    ok, why = arm.check_entry(_st(exposure=8400.0), direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_exposure")


def test_cap_size_shrinks_never_grows():
    st = _st(exposure=0.0)
    assert arm.cap_size(st, 1000, 10.0, direction="SELL_SHORT") == 420          # 4200/10
    assert arm.cap_size(st, 100, 10.0, direction="SELL_SHORT") == 100           # 不放大
    assert arm.cap_size(_st(exposure=6000.0), 1000, 10.0,
                        direction="SELL_SHORT") == 240                           # 余量 2400/10


def test_cap_size_zero_room_blocks():
    assert arm.cap_size(_st(exposure=8400.0), 1000, 10.0, direction="SELL_SHORT") == 0


# ⑤ kill switch
def test_kill_switch_cum_loss():
    assert arm.kill_reason({"n": 3, "net": -300.0, "net_bps": -50.0}).startswith("S2_ECON")
    assert arm.kill_reason({"n": 3, "net": -299.9, "net_bps": -50.0}) is None


def test_kill_switch_direction_after_n():
    assert arm.kill_reason({"n": 15, "net": -5.0, "net_bps": -1.0}).startswith("S1_DIR")
    assert arm.kill_reason({"n": 14, "net": -5.0, "net_bps": -1.0}) is None


def test_killed_state_blocks_entry():
    ok, why = arm.check_entry(_st(killed="S2_ECON(cum_net=-350.00)"),
                              direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_killed")


# ⑥ 账本按臂别解析 (P8)
def test_target_env_is_required_not_defaulted():
    """P8: 漏传 target_env 必须 TypeError, 不静默回落 sim 账本。"""
    with pytest.raises(TypeError):
        arm.cycle_state("2026-11-16")
    with pytest.raises(TypeError):
        arm.open_exposure_usd()
    with pytest.raises(TypeError):
        arm.cohort_state("2026-11-16")
    with pytest.raises(TypeError):
        arm.cap_size(_st(), 100, 10.0)


def test_ledger_follows_arm():
    """P8: sim → 研究库, real/live → 隔离真钱库 (SSOT app/core/paths.py)。"""
    sim, real = arm._ledger("sim"), arm._ledger("real")
    assert sim.name == "ftseek_us.db"
    assert real.name == "ftseek_us_live.db"
    assert arm._ledger("live") == real
    assert sim != real


def test_live_ledger_has_no_outbox_and_no_short_cohort_yet():
    """P8 实证: 真钱库当前无 S1 人群 ⇒ 若臂闸仍读 sim, kill switch 恒不触发。"""
    assert arm.cohort_state("real", "2026-11-16")["n"] == 0
    assert arm.open_exposure_usd("real", "2026-11-16") == 0.0


# ⑦ 读库失败 fail-close (P9)
def test_ledger_failure_degrades_and_blocks_arm_direction(monkeypatch):
    monkeypatch.setattr(arm, "enabled", lambda: True)

    def _boom(*a, **k):
        raise _LockedError("database is locked")

    monkeypatch.setattr(arm, "_connect", _boom)
    st = arm.cycle_state("2026-11-16", "sim")
    assert st["degraded"] is True and st["enabled"] is True
    ok, why = arm.check_entry(st, direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_degraded")
    assert arm.cap_size(st, 100, 10.0, direction="SELL_SHORT") == 0


def test_degraded_does_not_leak_to_non_arm_direction(monkeypatch):
    """P9 × P10 交叉: 降级只关本臂, 不得连带停掉无关方向 (否则又回到 P10 的病灶)。"""
    monkeypatch.setattr(arm, "enabled", lambda: True)

    def _boom(*a, **k):
        raise _LockedError("database is locked")

    monkeypatch.setattr(arm, "_connect", _boom)
    st = arm.cycle_state("2026-11-16", "sim")
    assert st["degraded"] is True
    assert arm.check_entry(st, direction="BUY", entry_minute=1) == (True, "")
    assert arm.cap_size(st, 1000, 10.0, direction="BUY") == 1000



# cohort 口径 (生产库, 只读) — 钉住判据字典 B23 的历史参照, 防谓词漂移。
# 尾界 2026-11-16 = 排除前瞻窗 (前瞻自该日起, 盘中会持续新增平仓行, 无尾界则参照漂移)。
def test_cohort_matches_prereg_reference():
    h = arm.cohort_state("sim", "2026-07-01", end_date="2026-11-16")
    assert h["n"] == 88, h
    assert abs(h["net"] - 1472.02) < 1.0, h
    assert abs(h["net_bps"] - 30.75) < 0.05, h


def test_forward_cohort_arm_attribution_consistent():
    """前瞻窗 (2026-11-16 起) 已产生真实平仓 (如 COIN SHORT): 全口径与臂属口径
    必须一致 —— 前瞻窗内的 SHORT 应全部来自本臂 (PLAYBOOK_SIM), 无外来源混入。"""
    full = arm.cohort_state("sim", "2026-11-16")
    scoped = arm.arm_cohort_state("sim", "2026-11-16")
    assert full["n"] >= 1                     # 前瞻窗已有样本 (不再恒 0)
    assert scoped["n"] == full["n"]
    assert scoped["net"] == full["net"]


# S3 未实现留痕 (P11): 只实现退出侧会低估滑点 ⇒ 弱化 kill switch, 宁缺勿半。
def test_kill_reason_implements_only_s1_s2():
    """S3 (实盘滑点中位>15bps) 今天无数据可算: 入场追价 bps 全库未持久化。"""
    src = Path(arm.__file__).read_text(encoding="utf-8")
    assert "S3_SLIPPAGE" not in src.split("def kill_reason")[1].split("def ")[0]
    assert "入场追价 bps" in src and "全库无持久化字段" in src


# ⑧ 臂属 cohort 过滤 (DEB-20261116 C6) + 真钱停止条件 record-only 影子 (C5)
import json  # noqa: E402
import sqlite3  # noqa: E402


def _mk_ledger(tmp_path, rows):
    db = tmp_path / "fake_live.db"
    conn = sqlite3.connect(db)
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT,"
        " source_type TEXT, trade_date TEXT, entry_time TEXT, exit_time TEXT,"
        " quantity REAL, entry_price REAL, realized_pnl REAL, fees_est REAL,"
        " pnl_net REAL, data_quality TEXT)"
    )
    for r in rows:
        conn.execute(
            "INSERT INTO trade_records (symbol, direction, source_type, trade_date,"
            " entry_time, exit_time, quantity, entry_price, realized_pnl, fees_est,"
            " pnl_net, data_quality) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()
    conn.close()
    return db


def _row(source, direction="SHORT", pnl=-100.0, entry="2026-11-16T10:00:00-05:00",
         quality="clean", date="2026-11-16"):
    return ("US.TEST", direction, source, date, entry, "2026-11-16T15:00:00-05:00",
            10, 20.0, pnl, 1.0, pnl, quality)


def test_arm_cohort_filters_non_arm_sources(tmp_path, monkeypatch):
    """C6: kill switch 人群只收本臂来源; S1_WHERE 本身 (B23 冻结面) 不动。"""
    db = _mk_ledger(tmp_path, [
        _row("PLAYBOOK_LIVE", pnl=-100.0),     # 本臂: 亏 $100, 不足以触发 S2(-300)
        _row("MAIN_PIPELINE", pnl=-5000.0),    # 非臂: 全口径会误触发 S2
        _row("SCALPING", pnl=-5000.0),         # 非臂
        _row("MANUAL", pnl=-9999.0, quality="manual"),  # S1_WHERE 本就排除
        _row("PLAYBOOK_LIVE", direction="LONG"),        # 非空头
        _row("PLAYBOOK_LIVE", entry="2026-11-16T09:35:00-05:00"),  # 09:45 前
    ])
    monkeypatch.setattr(arm, "_ledger", lambda env: db)
    full = arm.cohort_state("real", "2026-11-16")
    scoped = arm.arm_cohort_state("real", "2026-11-16")
    assert full["n"] == 3 and full["net"] == -10100.0
    assert arm.kill_reason(full).startswith("S2_ECON")      # 混入非臂 ⇒ 误停
    assert scoped["n"] == 1 and scoped["net"] == -100.0
    assert arm.kill_reason(scoped) is None                  # 臂属口径不误停
    # sim 臂映射到 PLAYBOOK_SIM (同一谓词, 不同来源词)
    assert arm._ARM_SOURCE["sim"] == "PLAYBOOK_SIM"
    assert arm._ARM_SOURCE["live"] == "PLAYBOOK_LIVE"


def test_arm_cohort_target_env_required():
    with pytest.raises(TypeError):
        arm.arm_cohort_state("2026-11-16")


def test_real_arm_cohort_empty_on_prod_ledger():
    """实证: 真钱库当前无本臂人群 (SHORT 全为 MANUAL) ⇒ 影子起步 n=0。"""
    assert arm.arm_cohort_state("real", "2026-11-16")["n"] == 0


def test_real_kill_shadow_record_only(tmp_path, monkeypatch):
    """C5 影子: 落一行观测, 不 halt、不写交易库、不拦截。"""
    db = _mk_ledger(tmp_path, [_row("PLAYBOOK_LIVE", pnl=-400.0)])
    out = tmp_path / "shadow" / "s1_real_kill_shadow.jsonl"
    monkeypatch.setattr(arm, "_ledger", lambda env: db)
    monkeypatch.setattr(arm, "_REAL_KILL_SHADOW_PATH", out)
    from app.playbook import live_arm_gate as lag
    halt_before = lag._HALT_FILE.exists()
    arm.record_real_kill_shadow("2026-11-16")
    lines = out.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    row = json.loads(lines[0])
    assert row["mode"] == "record_only" and row["degraded"] is False
    assert row["cohort"]["n"] == 1
    assert row["kill_reason"].startswith("S2_ECON")   # 观测到"会停", 但只是记录
    assert row["exposure_usd"] == 0.0                 # 已平仓不计敞口
    assert lag._HALT_FILE.exists() == halt_before     # 绝不持久化 halt
    # 影子函数体不得含执行语义 (防未来顺手把 halt/check_entry 接进 record-only 函数)
    src = Path(arm.__file__).read_text(encoding="utf-8")
    body = src.split("def record_real_kill_shadow")[1].split("\ndef ")[0]
    code = body.split('"""', 2)[2]                     # 跳过 docstring (其内提及接线属说明)
    assert "halt_live_new_positions" not in code and "check_entry" not in code


def test_real_kill_shadow_independent_of_enabled(tmp_path, monkeypatch):
    """影子独立于 s1_arm_enabled: 关闸期也观测真钱账本可读性。"""
    db = _mk_ledger(tmp_path, [])
    out = tmp_path / "s.jsonl"
    monkeypatch.setattr(arm, "enabled", lambda: False)
    monkeypatch.setattr(arm, "_ledger", lambda env: db)
    monkeypatch.setattr(arm, "_REAL_KILL_SHADOW_PATH", out)
    arm.record_real_kill_shadow("2026-11-16")
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["degraded"] is False and row["cohort"]["n"] == 0


def test_real_kill_shadow_failure_loud_not_fatal(tmp_path, monkeypatch):
    """读库失败 ⇒ degraded 行 + 不抛进交易路径 (影子的失败方向也是留痕不是静默)。"""
    out = tmp_path / "s.jsonl"
    monkeypatch.setattr(arm, "_ledger", lambda env: tmp_path / "nonexistent.db")
    monkeypatch.setattr(arm, "_REAL_KILL_SHADOW_PATH", out)
    arm.record_real_kill_shadow("2026-11-16")          # 不得 raise
    row = json.loads(out.read_text(encoding="utf-8").strip())
    assert row["degraded"] is True and row["error"]


def test_shadow_wiring_not_gated_by_enabled():
    """接线契约: 影子调用在 auto_execute_flow 每轮路径上, 不被 enabled 包裹,
    且路径从 module_file 派生 (hermetic 沙箱可改写, 同 sizing_delta 范式)。"""
    flow = Path(arm.__file__).resolve().parents[1] / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
    src = flow.read_text(encoding="utf-8")
    assert "_s1_gate.record_real_kill_shadow(" in src
    assert 'shadow_path=Path(module_file).resolve().parents[2] / "data" / "shadow" / "s1_real_kill_shadow.jsonl"' in src
    call_line = next(l for l in src.splitlines() if "_s1_gate.record_real_kill_shadow(" in l)
    assert not call_line.lstrip().startswith("#")


# ⑨ 无价 fail-closed (DEB-20261116 C8, P9 族): 价格≤0 ⇒ 名义不可算 ⇒ 0 股
@pytest.mark.parametrize("price", [0.0, -1.0])
def test_cap_size_no_price_fail_closed_for_arm(price):
    """原实现 entry_price<=0 混在 enabled/size guard 里 return size = fail-open。"""
    assert arm.cap_size(_st(), 100, price, direction="SELL_SHORT") == 0


@pytest.mark.parametrize("direction", ["BUY", "LONG", "", None])
def test_cap_size_no_price_does_not_touch_non_arm(direction):
    """P10 交叉: 无价 fail-closed 只关本臂, 非臂方向原样放行。"""
    assert arm.cap_size(_st(), 100, 0.0, direction=direction) == 100


def test_cap_size_no_price_disabled_still_passthrough():
    """关闸零行为变更语义不因 C8 改变。"""
    assert arm.cap_size(_st(enabled=False), 100, 0.0, direction="SELL_SHORT") == 100


def test_no_price_skip_reason_is_distinguishable():
    """调用点落痕区分 s1_arm_no_price 与 s1_arm_cap_zero_room (C14 可观测性前置)。"""
    flow = Path(arm.__file__).resolve().parents[1] / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
    src = flow.read_text(encoding="utf-8")
    assert '"s1_arm_no_price" if _entry <= 0 else "s1_arm_cap_zero_room"' in src


# ⑩ P13 重设计 (用户 2026-11-16 裁): 一条账本、一个锚点、一个增量
def test_cycle_state_daily_count_from_arm_ledger(tmp_path, monkeypatch):
    """锚点 = 臂账本今日 SHORT 成交数: 多头/非臂来源/昨日都不计 (谓词即语义)。"""
    db = _mk_ledger(tmp_path, [
        _row("PLAYBOOK_SIM", pnl=10.0),                      # 今日臂属 SHORT ✓
        _row("PLAYBOOK_SIM", pnl=10.0),                      # 今日臂属 SHORT ✓
        _row("MAIN_PIPELINE", pnl=10.0),                     # 非臂来源 ✗
        _row("PLAYBOOK_SIM", direction="LONG", pnl=10.0),    # 多头 ✗
        _row("PLAYBOOK_SIM", pnl=10.0, date="2026-11-15",
             entry="2026-11-15T10:00:00-05:00"),             # 昨日 ✗
    ])
    monkeypatch.setattr(arm, "enabled", lambda: True)
    monkeypatch.setattr(arm, "_ledger", lambda env: db)
    st = arm.cycle_state("2026-11-16", "sim")
    assert st["today_count"] == 2
    ok, why = arm.check_entry(st, direction="SELL_SHORT", entry_minute=600)
    assert ok is False and why.startswith("s1_arm_daily_count")   # 2>=2 拒第三笔
    # 多头不受影响 (P10): 即使额度满也放行
    assert arm.check_entry(st, direction="BUY", entry_minute=600) == (True, "")


def test_cycle_state_rejects_legacy_budget_counter_call():
    """旧三参签名 (today, today_opened_count, target_env) 必须 TypeError ——
    防任何调用点把全流向计数器重新喂进臂闸 (P13 回退形态)。"""
    with pytest.raises(TypeError):
        arm.cycle_state("2026-11-16", 0, "sim")


def test_daily_count_wiring_single_arm_increment():
    """接线契约: 调用点两参; 每指令重置行已删; 轮内唯一增量只计臂方向,
    方向取臂态载值 (C11 后 flow 不再直接读 cfg)。"""
    flow = Path(arm.__file__).resolve().parents[1] / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
    src = flow.read_text(encoding="utf-8")
    assert '_s1_gate.cycle_state(today, "sim")' in src
    assert '_s1_st["today_count"] = _today_opened_count' not in src   # 全流向同步已删
    assert '_s1_st["today_count"] = (_s1_st.get("today_count") or 0) + 1' in src
    assert '_s1_st.get("allowed_direction")' in src
    assert "_s1_gate.cfg(" not in src                                 # flow 零直接 cfg 调用
    inc_line = next(l for l in src.splitlines()
                    if '_s1_st["today_count"] = (_s1_st.get("today_count") or 0) + 1' in l)
    assert inc_line.startswith("                        ")            # 在方向守卫 if 之内


# ⑪ X2 缩股物化 (DEB-20261116 §10 第 4 步): 一律写 custom_params, 不写 inst 内存 dict
def test_x2_shrink_always_materialized_to_custom_params():
    """原 else 分支写 inst 内存 dict, 但 execute_instruction 重查库 ⇒ 写回被丢,
    订单按库中原股数提交 (fail-open)。修复后缩股无条件物化到 _reprice_params。"""
    flow = Path(arm.__file__).resolve().parents[1] / "playbook" / "pipeline_parts" / "auto_execute_flow.py"
    src = flow.read_text(encoding="utf-8")
    assert 'inst["position_size"] = _size' not in src        # 内存写回已删
    assert "if _reprice_params is None:\n                        _reprice_params = {}" in src
    assert '_reprice_params["position_size"] = _size' in src
    # 物化必须无条件 (不再以 _reprice_params.get("position_size") 为前提)
    assert 'if _reprice_params and _reprice_params.get("position_size"):\n                        _reprice_params["position_size"] = _size' not in src


def test_x2_partial_dict_premise_per_key_fallback():
    """部分 dict 安全性前提: execute_instruction 逐键回退 (DEB-20260903 #90) ——
    新建的 {"position_size": N} 不得把 entry/SL/TP 兑底成 None/1股。"""
    svc = Path(arm.__file__).resolve().parents[1] / "playbook" / "playbook_service.py"
    src = svc.read_text(encoding="utf-8")
    for field in ("entry_price", "stop_loss", "take_profit", "position_size"):
        assert f'_cp["{field}"] if _cp.get("{field}") is not None else inst.get("{field}", 0)' in src


# ⑫ C11/C12 授权值单源 + TTL 缓存 (DEB-20261116 §10 第 6 步)
def test_c11_no_defaults_single_source():
    """DEFAULTS 同值双写已删; 授权值唯一真源 = YAML; 缺键抛 S1ConfigError 不回落。"""
    src = Path(arm.__file__).read_text(encoding="utf-8")
    assert "DEFAULTS =" not in src and "DEFAULTS[" not in src   # 代码级断言 (注释提及不算)
    with pytest.raises(arm.S1ConfigError):
        arm.cfg("s1_arm_no_such_key_exists")


def test_c11_yaml_carries_all_nine_authorized_keys():
    """单源完整性: 九个授权键必须全部在 YAML (缺任何一个 = 部署错误, 运行时 fail-close)。"""
    from app.playbook.config import PlaybookConfig
    pc = PlaybookConfig()
    for key in ("s1_arm_enabled", "s1_arm_start_date", "s1_arm_allowed_direction",
                "s1_arm_min_entry_minute_et", "s1_arm_single_notional_cap_usd",
                "s1_arm_max_entries_per_day", "s1_arm_max_open_exposure_usd",
                "s1_arm_kill_cum_pnl_usd", "s1_arm_kill_after_n"):
        assert pc.get(key) is not None, f"YAML 缺授权键 {key}"


def test_c11_config_unreadable_cycle_state_fail_close(monkeypatch):
    """连 enabled 都判不出 ⇒ 视为开闸但降级: 拒本臂、放非臂 (P10 不连带)。"""
    def _boom(key):
        raise arm.S1ConfigError(key)
    monkeypatch.setattr(arm, "cfg", _boom)
    st = arm.cycle_state("2026-11-16", "sim")
    assert st["enabled"] is True and st["degraded"] is True
    assert arm.check_entry(st, direction="SELL_SHORT", entry_minute=600)[0] is False
    assert arm.check_entry(st, direction="BUY", entry_minute=1) == (True, "")


def test_c11_missing_key_gate_fail_close(monkeypatch):
    """运行中授权键丢失: check_entry 拒 / cap_size 0 / kill_reason 按停。"""
    real_cfg = arm.cfg
    broken = {"s1_arm_max_entries_per_day",        # check_entry 读
              "s1_arm_single_notional_cap_usd",    # cap_size 读
              "s1_arm_kill_cum_pnl_usd"}           # kill_reason 读
    def _half(key):
        if key in broken:
            raise arm.S1ConfigError(key)
        return real_cfg(key)
    monkeypatch.setattr(arm, "cfg", _half)
    ok, why = arm.check_entry(_st(), direction="SELL_SHORT", entry_minute=600)
    assert ok is False and "config_missing" in why
    assert arm.cap_size(_st(), 100, 10.0, direction="SELL_SHORT") == 0
    assert arm.kill_reason({"n": 0, "net": 0.0}) == "CONFIG_UNREADABLE(fail-close)"
    # 非臂方向不受配置故障连带 (P10)
    assert arm.check_entry(_st(), direction="BUY", entry_minute=1) == (True, "")
    assert arm.cap_size(_st(), 100, 10.0, direction="BUY") == 100


def test_c12_ttl_cache_reduces_constructions(monkeypatch):
    """TTL 窗内多次 cfg 只构造一次 PlaybookConfig (原实现每次调用都构造)。"""
    import app.playbook.config as cfgmod
    n = {"count": 0}
    real_pc = cfgmod.PlaybookConfig
    class _CountingPC(real_pc):
        def __init__(self):
            n["count"] += 1
            super().__init__()
    monkeypatch.setattr(cfgmod, "PlaybookConfig", _CountingPC)
    monkeypatch.setattr(arm, "_cfg_cache", (0.0, None))
    for _ in range(6):
        arm.cfg("s1_arm_enabled")
    assert n["count"] == 1


def test_c12_not_blind_singleton_hot_reload_preserved():
    """不得改用 R-4 盲单例: 它只在 horizon mtime 变更时重建, unified 键
    (s1_arm_enabled) 热载不穿透 ⇒ 会丢 ~60s 热载回滚。TTL 机制必须在位。"""
    src = Path(arm.__file__).read_text(encoding="utf-8")
    assert "get_playbook_config()" not in src
    assert "_CFG_TTL_SEC" in src and "time.monotonic()" in src
