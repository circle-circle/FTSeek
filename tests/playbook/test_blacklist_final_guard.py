# -*- coding: utf-8 -*-
"""RFC-218 §19 P0: Playbook 黑名单写入/执行最终闭环契约 (默认 OFF = 逐字节旧行为).

病灶 (§18.3 确认): §16 守卫只在新候选三边界 (runtime_context/topup/合并); DA 正式管线
(仓外 pipeline.py:2529) / UE / generate 直写 save_instruction 绕过 → 被禁标的
(QCOM/LCID/TME) 仍可形成可执行指令; 执行层亦无全局黑名单复核。

本钉锁死独立开关 PLAYBOOK_BLACKLIST_FINAL_GUARD (与 §16 PLAYBOOK_BLACKLIST_GUARD 解耦):
  - 默认 OFF / 未知值 fail-closed → 逐字节旧行为;
  - 写入侧 save_instruction: 被禁新入场转 SKIP + 保留 original_direction/原因 (全写入方收口);
  - 执行侧 run_playbook_auto_execute: 被禁 PENDING 转 guardrail SKIP (同源谓词, 兜底历史 PENDING);
  - 只消费 global_blacklist, 不动已 SKIP/已执行/booked/持仓/退出链;
  - 空黑名单/读取失败 fail-open; 异常 warning+exc_info 可见 (不添 W4 棘轮)。

注意: 本测试只验「禁止语义闭环」正确性, 不证收益 (风控/候选正确性 ≠ 盈利补丁)。
"""
import sqlite3
from pathlib import Path

import pytest

from app.playbook.db import PlaybookDB
from app.utils.watchlist_manager import (
    _blacklist_final_guard_enabled,
    get_watchlist_manager,
    is_global_blacklisted,
)

REPO = Path(__file__).resolve().parents[2]
EXEC_FLOW = REPO / "app" / "playbook" / "pipeline_parts" / "auto_execute_flow.py"

BANNED = ["QCOM", "LCID", "TME", "HK.00700"]


@pytest.fixture
def banned(monkeypatch):
    """单一真源桩: 与生产 risk.global_blacklist 同归一化形态 (已剥 US.)."""
    wm = get_watchlist_manager()
    monkeypatch.setattr(wm, "_get_global_blacklist", lambda: list(BANNED))
    return set(BANNED)


@pytest.fixture
def db(tmp_path):
    conn = sqlite3.connect(tmp_path / "bl-final.db")
    conn.row_factory = sqlite3.Row
    d = PlaybookDB(conn=conn)
    d.ensure_tables()
    yield d
    conn.close()


def _flag(monkeypatch, value):
    if value is None:
        monkeypatch.delenv("PLAYBOOK_BLACKLIST_FINAL_GUARD", raising=False)
    else:
        monkeypatch.setenv("PLAYBOOK_BLACKLIST_FINAL_GUARD", value)


def _inst(symbol="US.QCOM", direction="BUY", **kw):
    base = {
        "date": "2026-11-27", "symbol": symbol, "direction": direction,
        "entry_price": 100.0, "stop_loss": 95.0, "take_profit": 110.0,
        "position_size": 100, "confidence": 90,
        "decision_id": f"DA-20261127-{symbol}", "thesis": "test",
        "raw_instruction": {"source": "da"},
    }
    base.update(kw)
    return base


def _row(db, iid):
    return db._conn().execute(
        "SELECT direction, skip_reason, raw_instruction_json "
        "FROM playbook_instructions WHERE id=?", (iid,)).fetchone()


# ── 开关语义 (独立于 §16) ──────────────────────────────────────────────

def test_flag_unset_default_off(monkeypatch):
    _flag(monkeypatch, None)
    assert _blacklist_final_guard_enabled() is False


@pytest.mark.parametrize("v", ["1", "true", "YES", " on ", "TRUE", "On"])
def test_flag_truthy(monkeypatch, v):
    _flag(monkeypatch, v)
    assert _blacklist_final_guard_enabled() is True


@pytest.mark.parametrize("v", ["0", "2", "maybe", "", "off", "false"])
def test_flag_unknown_fail_closed(monkeypatch, v):
    _flag(monkeypatch, v)
    assert _blacklist_final_guard_enabled() is False


def test_final_flag_independent_from_16(monkeypatch):
    """§16 开关 ON 不得连带激活 §19 最终闭环 (两批 enforcement 独立回滚)."""
    monkeypatch.setenv("PLAYBOOK_BLACKLIST_GUARD", "1")
    _flag(monkeypatch, None)
    assert _blacklist_final_guard_enabled() is False


# ── 单标的谓词 (与 §16/§17 同源同口径) ─────────────────────────────────

def test_predicate_banned_true(banned):
    assert is_global_blacklisted("US.QCOM") is True
    assert is_global_blacklisted("QCOM") is True
    assert is_global_blacklisted("US.US.LCID") is True   # 剥全部 US. 前缀
    assert is_global_blacklisted("US.HK.00700") is True


def test_predicate_non_banned_false(banned):
    assert is_global_blacklisted("US.NVDA") is False
    assert is_global_blacklisted("US.AAPL") is False


def test_predicate_empty_fail_open(monkeypatch):
    wm = get_watchlist_manager()
    monkeypatch.setattr(wm, "_get_global_blacklist", lambda: [])
    assert is_global_blacklisted("US.QCOM") is False


def test_predicate_failure_fail_open(monkeypatch):
    wm = get_watchlist_manager()

    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr(wm, "_get_global_blacklist", _boom)
    assert is_global_blacklisted("US.QCOM") is False   # fail-open 放行, 不瘫痪写入


# ── 写入侧 save_instruction 闭环 (全写入方唯一收口点) ──────────────────

def test_write_flag_off_banned_unchanged(db, banned, monkeypatch):
    """OFF: 被禁标的 BUY 逐字节旧行为入库 (病灶复现 — 直写绕过)."""
    _flag(monkeypatch, None)
    iid = db.save_instruction(_inst("US.QCOM", "BUY"))
    r = _row(db, iid)
    assert r["direction"] == "BUY"
    assert r["skip_reason"] != "GLOBAL_BLACKLIST_FINAL"


def test_write_flag_on_banned_buy_to_skip(db, banned, monkeypatch):
    """ON: 被禁 BUY 转 SKIP + 原因 + 原方向留存 (与静态侧 SKIP 同形态)."""
    _flag(monkeypatch, "1")
    iid = db.save_instruction(_inst("US.QCOM", "BUY"))
    r = _row(db, iid)
    assert r["direction"] == "SKIP"
    assert r["skip_reason"] == "GLOBAL_BLACKLIST_FINAL"
    import json
    raw = json.loads(r["raw_instruction_json"])
    assert raw.get("original_direction") == "BUY"
    assert raw.get("blacklist_final_guard", {}).get("blocked") is True
    assert raw.get("blacklist_final_guard", {}).get("original_direction") == "BUY"


def test_write_flag_on_banned_sell_short_to_skip(db, banned, monkeypatch):
    """ON: 被禁 SELL_SHORT 同样转 SKIP (禁止语义不分多空)."""
    _flag(monkeypatch, "1")
    iid = db.save_instruction(_inst("US.LCID", "SELL_SHORT"))
    r = _row(db, iid)
    assert r["direction"] == "SKIP"
    assert r["skip_reason"] == "GLOBAL_BLACKLIST_FINAL"


def test_write_flag_on_non_banned_unchanged(db, banned, monkeypatch):
    """ON: 非黑名单标的零误伤 (NVDA BUY 照常)."""
    _flag(monkeypatch, "1")
    iid = db.save_instruction(_inst("US.NVDA", "BUY"))
    r = _row(db, iid)
    assert r["direction"] == "BUY"
    assert r["skip_reason"] != "GLOBAL_BLACKLIST_FINAL"


def test_write_flag_on_already_skip_untouched(db, banned, monkeypatch):
    """ON: 已 SKIP 票不被改写 (只转新入场 direction!=SKIP), 原 skip_reason 保留."""
    _flag(monkeypatch, "1")
    iid = db.save_instruction(_inst(
        "US.QCOM", "SKIP", skip_reason="非当前通道候选",
        raw_instruction={"source": "supply_placeholder"}))
    r = _row(db, iid)
    assert r["direction"] == "SKIP"
    assert r["skip_reason"] != "GLOBAL_BLACKLIST_FINAL"


def test_write_fail_open_on_config_error(db, monkeypatch):
    """ON + 黑名单读取异常: fail-open 照常入库 (审计降级优于丢指令), 不抛."""
    _flag(monkeypatch, "1")
    wm = get_watchlist_manager()

    def _boom():
        raise RuntimeError("config down")
    monkeypatch.setattr(wm, "_get_global_blacklist", _boom)
    iid = db.save_instruction(_inst("US.QCOM", "BUY"))
    r = _row(db, iid)
    assert r["direction"] == "BUY"   # fail-open 放行


def test_write_flag_on_prefix_variants_normalized(db, banned, monkeypatch):
    """ON: 双前缀/裸代码变体经 normalize_symbol 后仍命中谓词."""
    _flag(monkeypatch, "1")
    iid = db.save_instruction(_inst("US.US.TME", "BUY"))
    r = _row(db, iid)
    assert r["direction"] == "SKIP"
    assert r["skip_reason"] == "GLOBAL_BLACKLIST_FINAL"


# ── 执行侧 run_playbook_auto_execute 兜底 (源结构 + DB 闩, 同仓约定) ───

def test_executor_guard_wiring_and_placement():
    """执行入口守卫接线: 读独立开关 + 同源黑名单 + guardrail SKIP, 且位于 SKIP/executed
    检查之后 (绝不触已执行/已 SKIP 行); 读取失败 fail-open 空集。"""
    src = EXEC_FLOW.read_text(encoding="utf-8")
    assert "_blacklist_final_guard_enabled" in src
    assert '_final_bl_set = set(get_watchlist_manager()._get_global_blacklist() or [])' in src
    assert 'db.mark_instruction_guardrail_skip(inst["id"], "GLOBAL_BLACKLIST_FINAL")' in src
    # 谓词与写入侧同源: 剥 US. 前缀
    assert 'str(inst.get("symbol", "")).replace("US.", "") in _final_bl_set' in src
    # 放置: 守卫在「跳过已执行/SKIP」之后
    i_skip = src.index('if inst.get("direction") == "SKIP":')
    i_guard = src.index('if _final_bl_set and str(inst.get("symbol"')
    assert i_guard > i_skip
    # fail-open: 异常分支重置空集
    i_except = src.index("except Exception as _fbl_err:")
    assert "_final_bl_set = set()" in src[i_except:i_except + 400]


def test_executor_guard_default_off_empty_set():
    """OFF = 空集 = 逐字节旧行为 (守卫短路, 不触任何行)."""
    src = EXEC_FLOW.read_text(encoding="utf-8")
    assert "_final_bl_set = set()" in src
    assert "if _final_bl_set and " in src   # 空集短路


def test_guardrail_skip_latch_pending_only(db):
    """mark_instruction_guardrail_skip 闩: 只转 PENDING∧executed=0 行;
    已执行/booked/非 PENDING 态绝不触碰 (兜底不得伤在途/已成交)。"""
    # 三种行: PENDING(可转) / executed=1(已认领) / booked(有单号)
    iid_pending = db.save_instruction(_inst("US.QCOM", "BUY",
                                            decision_id="DA-20261127-PEND"))
    iid_exec = db.save_instruction(_inst("US.LCID", "BUY",
                                         decision_id="DA-20261127-EXEC"))
    iid_booked = db.save_instruction(_inst("US.TME", "BUY",
                                           decision_id="DA-20261127-BOOK"))
    conn = db._conn()
    conn.execute("UPDATE playbook_instructions SET executed=1 WHERE id=?", (iid_exec,))
    conn.execute(
        "UPDATE playbook_instructions SET executed=1, execution_state='BOOKED', "
        "broker_order_id='ORD-1' WHERE id=?", (iid_booked,))
    conn.commit()

    assert db.mark_instruction_guardrail_skip(iid_pending, "GLOBAL_BLACKLIST_FINAL") is True
    assert db.mark_instruction_guardrail_skip(iid_exec, "GLOBAL_BLACKLIST_FINAL") is False
    assert db.mark_instruction_guardrail_skip(iid_booked, "GLOBAL_BLACKLIST_FINAL") is False

    assert _row(db, iid_pending)["direction"] == "SKIP"
    assert _row(db, iid_exec)["direction"] == "BUY"      # 已执行不改
    assert _row(db, iid_booked)["direction"] == "BUY"    # booked 不改


def test_guardrail_skip_latch_non_pending_state(db):
    """非 PENDING 态 (如 SKIPPED_EXISTING_POSITION) 不被本闩改写."""
    iid = db.save_instruction(_inst("US.QCOM", "BUY", decision_id="DA-20261127-SEP"))
    conn = db._conn()
    conn.execute(
        "UPDATE playbook_instructions SET execution_state='SKIPPED_EXISTING_POSITION' "
        "WHERE id=?", (iid,))
    conn.commit()
    assert db.mark_instruction_guardrail_skip(iid, "GLOBAL_BLACKLIST_FINAL") is False
    assert _row(db, iid)["direction"] == "BUY"
