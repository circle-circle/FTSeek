#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切换彩排 (cutover rehearsal) — 在**库副本沙箱**里把切换日的 DDL 与状态迁移先跑一遍.

为什么存在 (2026-09-14, 用户裁「目前只是先将基建做好，待未来可切换时…无缝实现」):
  预检 (scripts/cutover_preflight.py) 实测: 切 live 会在**真钱库**上首次触发 33 条补列
  DDL (含 playbook_instructions 的 execution_state/execution_attempt/broker_order_id/
  broker_order_status —— 成交簿记 CAS 依赖列)。这些迁移本身幂等自愈
  (trade_store.py:398 / playbook/db.py:288 / capital_safety.py:723 / lesson_injector.py:39),
  但**全部包在 `except Exception: pass` 里** ⇒ 真失败(库锁/语句错)与"列已存在"静默不可分,
  而首跑时刻恰好是切换日第一笔真钱成交。把首跑挪到副本上, 切换日 DDL 即可压到 0。

本脚本额外**实证**一个只靠读代码看不出来的回切缺陷:
  capital_safety._load_state (:755) 用 `ORDER BY id DESC LIMIT 1` **不按 trade_env 过滤**,
  表上却有 per-env 唯一索引 (:741) 让两臂基线各自留存 ⇒ live→sim 回切时, 只要 live 臂那行
  id 更大, sim 臂自己那行就被判"环境切换"整份丢弃 (peak/max_drawdown/is_locked 归零)。
  在沙箱里种入两臂行后调**真实** _load_state, 直接看它捞到哪一行。

纪律:
  · **生产库零接触** — 只读复制, 所有写入落在沙箱; 启动即断言沙箱不在 data/ 下。
  · 不连券商 (四个迁移入口都可只给 conn/db_path), 无 SDK 悬挂。
  · 沙箱默认跑完即删, --keep 留档取证。

退出码: 0 = 彩排干净 (残留缺口 0 且回切基线可恢复) | 1 = 有残留缺口/回切丢基线 | 2 = 自身异常
用法: venv/bin/python scripts/cutover_rehearse.py [--to live|sim] [--keep] [--sandbox DIR]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
SIM_DB = DATA / "ftseek_us.db"
LIVE_DB = DATA / "ftseek_us_live.db"


# ── 已知良性残留 (逐条带证据, 不是"看着没事就放过") ────────────────────
# 计入这些会让退出码恒红 ⇒ 判读价值归零。故显式白名单 + 理由锚点:
BENIGN_MISSING_COLUMNS = {
    # capital_safety.py 全文无 account_id 引用 (rg 实测 0 命中) ⇒ sim 侧死列;
    # 臂判别已由 trade_env 列 + idx_capital_state_trade_env 唯一索引 (capital_safety.py:741) 承担。
    ("capital_state", "account_id"),
}
BENIGN_MISSING_TABLES = {
    # scripts/intraday_loss_breaker_shadow.py:153/159 CREATE IF NOT EXISTS 首次使用即自建;
    # 影子统计载体, 不在下单/退出/簿记链上。
    "cb_shadow_blocked", "cb_shadow_daily",
    # 全仓无 CREATE TABLE close_retry_state (rg 实测 0 命中) ⇒ 遗留死表;
    # 真实平仓重试态是进程内 dict (app/pipeline/order_executor.py:87 _close_retry_state)。
    "close_retry_state",
    # ── 以下 11 张: live 库有而 sim 库无 (即 --to sim 回切方向的"缺表"), 逐类实证良性 ──
    # (a) 被 app/storage/trade_store.py **主动 DROP** 的退役死表 ⇒ sim 库没有它们才是对的:
    #     option_positions    trade_store.py:251 「2026-07-01 表已删除 (自动交易不做期权)」
    #     daemon_heartbeat    trade_store.py:350 DROP TABLE IF EXISTS
    #     entry_context_snapshot trade_store.py:365 「死表退役 (2026-09-13, 用户裁"删除")」
    #     ⚠️ 反向含义: --to live 彩排/切换日会在**真钱库**上执行这 2 条 DROP (见 DEB §一.2)。
    "option_positions", "daemon_heartbeat", "entry_context_snapshot",
    # (b) CREATE TABLE IF NOT EXISTS 自建 (首次使用即建):
    #     live_deal_records / live_position_snapshots / live_round_trips → app/web_dashboard/live_ledger.py
    #     thesis_challenges → app/llm/thesis_challenge.py
    "live_deal_records", "live_position_snapshots", "live_round_trips", "thesis_challenges",
    # (c) 全仓 app/+scripts/ **零引用**的孤儿表 (rg 实测 0 命中) ⇒ 无人读, 缺了不影响任何链路:
    # playbook_factor_stats_bak_rfc112 是 SIM 侧历史备份表, 无运行时消费者, 不迁移到 LIVE.
    "option_peak_profits", "performance_stats", "shadow_daily_stats",
    "shadow_schema_version", "playbook_factor_stats_bak_rfc112",
}


def _cols(db: Path, table: str) -> List[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []
    finally:
        con.close()


def _tables(db: Path) -> List[str]:
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[0] for r in con.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
    finally:
        con.close()


def _diff(a: Path, b: Path) -> Dict[str, Any]:
    """a 相对 b 的缺口: b 缺哪些表/列 (即"从 a 视角看 b 需要补什么")."""
    ta, tb = set(_tables(a)), set(_tables(b))
    miss_tables = sorted(ta - tb)
    miss_cols: Dict[str, List[str]] = {}
    for t in sorted(ta & tb):
        ca, cb = _cols(a, t), _cols(b, t)
        if not ca or not cb:
            continue
        gap = [c for c in ca if c not in set(cb)]
        if gap:
            miss_cols[t] = gap
    blocking_tables = [t for t in miss_tables if t not in BENIGN_MISSING_TABLES]
    blocking_cols = {t: [c for c in cs if (t, c) not in BENIGN_MISSING_COLUMNS]
                     for t, cs in miss_cols.items()}
    blocking_cols = {t: cs for t, cs in blocking_cols.items() if cs}
    return {"missing_tables": miss_tables, "missing_columns": miss_cols,
            "n_missing_columns": sum(len(v) for v in miss_cols.values()),
            "blocking_tables": blocking_tables, "blocking_columns": blocking_cols,
            "n_blocking_columns": sum(len(v) for v in blocking_cols.values())}


def _copy_db(src: Path, dst: Path) -> None:
    """连同 -wal/-shm 一起复制 (WAL 里有未 checkpoint 的真数据, 只拷主库会丢行)."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)
    for suffix in ("-wal", "-shm"):
        s = Path(str(src) + suffix)
        if s.exists():
            shutil.copy2(s, Path(str(dst) + suffix))
    # 落盘 WAL → 让副本自洽 (只对副本做, 生产绝不动)
    con = sqlite3.connect(dst)
    try:
        con.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        con.close()


# ─────────────────────────────────────────────────────────────
# 迁移入口 (全部走 app 真实代码路径, 不复制 SQL 清单 ⇒ 不会与生产漂移)
# ─────────────────────────────────────────────────────────────
def apply_migrations(db: Path) -> Dict[str, str]:
    """对副本执行 app 的四条真实迁移路径, 返回每条的结果标签."""
    results: Dict[str, str] = {}

    # 1) TradeStore._init_db — trade_records/open_positions 补列 (trade_store.py:398)
    try:
        from app.storage.trade_store import TradeStore
        TradeStore(str(db))
        results["trade_store"] = "ok"
    except Exception as exc:
        results["trade_store"] = f"FAIL {type(exc).__name__}: {exc}"

    # 2) PlaybookDB.ensure_tables(conn=副本) — playbook_instructions 补列 (db.py:288)
    try:
        from app.playbook.db import PlaybookDB
        con = sqlite3.connect(db, timeout=15)
        try:
            PlaybookDB(conn=con).ensure_tables()
            con.commit()
        finally:
            con.close()
        results["playbook_db"] = "ok"
    except Exception as exc:
        results["playbook_db"] = f"FAIL {type(exc).__name__}: {exc}"

    # 3) CapitalSafetySystem._create_db_table — capital_state 补 trade_env (capital_safety.py:723)
    try:
        from app.risk.capital_safety import CapitalSafetySystem
        CapitalSafetySystem(db_path=str(db), initial_capital=100_000.0, trade_env=1)
        results["capital_safety"] = "ok"
    except Exception as exc:
        results["capital_safety"] = f"FAIL {type(exc).__name__}: {exc}"

    # 4) lesson_injector.ensure_lessons_lifecycle_columns — lessons 补 3 列
    try:
        from app.review.lesson_injector import ensure_lessons_lifecycle_columns
        con = sqlite3.connect(db, timeout=15)
        try:
            ensure_lessons_lifecycle_columns(con)
            con.commit()
        finally:
            con.close()
        results["lessons"] = "ok"
    except Exception as exc:
        results["lessons"] = f"FAIL {type(exc).__name__}: {exc}"

    return results


# ─────────────────────────────────────────────────────────────
# 回切实证: capital_state 的 _load_state 是否按臂取行
# ─────────────────────────────────────────────────────────────
def probe_capital_roundtrip(db: Path) -> Dict[str, Any]:
    """在副本里种两臂基线行, 调真实 _load_state 看它捞到哪一臂.

    模拟场景: sim 臂(trade_env=1)先有基线 → 切 live(trade_env=0)写入新行(id 更大)
              → 回切 sim(trade_env=1) 时, _load_state 应恢复 sim 自己的 peak/lock。
    """
    con = sqlite3.connect(db, timeout=15)
    try:
        con.execute("CREATE TABLE IF NOT EXISTS capital_state ("
                    "id INTEGER PRIMARY KEY, initial_capital REAL, current_equity REAL,"
                    "peak_equity REAL, cash REAL, market_value REAL, buying_power REAL,"
                    "drawdown_pct REAL, max_drawdown_pct REAL, water_level TEXT,"
                    "is_locked INTEGER, lock_reason TEXT, last_update TEXT,"
                    "trade_env INTEGER DEFAULT 1)")
        con.execute("DELETE FROM capital_state")
        # sim 臂: 已触发锁定 + 有回撤历史 (回切后理应原样恢复)
        con.execute("INSERT INTO capital_state (id, initial_capital, current_equity, peak_equity,"
                    " cash, market_value, buying_power, drawdown_pct, max_drawdown_pct,"
                    " water_level, is_locked, lock_reason, last_update, trade_env)"
                    " VALUES (1, 77806.99, 91000.0, 99127.14, 90000.0, 1000.0, 180000.0,"
                    " -0.082, -0.082, 'red', 1, 'rehearse_sim_lock', '2026-09-14T00:00:00', 1)")
        # live 臂: 后写入 ⇒ id 更大
        con.execute("INSERT INTO capital_state (id, initial_capital, current_equity, peak_equity,"
                    " cash, market_value, buying_power, drawdown_pct, max_drawdown_pct,"
                    " water_level, is_locked, lock_reason, last_update, trade_env)"
                    " VALUES (2, 500000.0, 500000.0, 500000.0, 500000.0, 0.0, 500000.0,"
                    " 0.0, 0.0, 'green', 0, '', '2026-09-14T01:00:00', 0)")
        con.commit()
        raw = con.execute("SELECT id, trade_env, peak_equity, is_locked, lock_reason"
                          " FROM capital_state ORDER BY id DESC").fetchall()
    finally:
        con.close()

    picked, state = None, None
    try:
        from app.risk.capital_safety import CapitalSafetySystem
        cs = CapitalSafetySystem(db_path=str(db), initial_capital=100_000.0, trade_env=1)
        cs._load_state()
        st = cs._state
        picked = {"initial_capital": st.initial_capital, "current_equity": st.current_equity,
                  "peak_equity": st.peak_equity, "water_level": str(getattr(st, "water_level", "")),
                  "is_locked": bool(st.is_locked), "lock_reason": st.lock_reason}
        state = "loaded"
    except Exception as exc:
        state = f"probe_error {type(exc).__name__}: {exc}"

    # 期望 (无缝回切): 恢复 sim 臂那行 → peak=99127.14 且 is_locked=True
    expected_peak, expected_lock = 99127.14, True
    ok = bool(picked) and abs(float(picked.get("peak_equity") or 0) - expected_peak) < 1e-6 \
        and bool(picked.get("is_locked")) is expected_lock
    return {"rows_in_sandbox": [dict(zip(("id", "trade_env", "peak_equity", "is_locked",
                                          "lock_reason"), r)) for r in raw],
            "load_state_result": picked, "probe_state": state,
            "seamless_roundtrip": ok,
            "expected": {"peak_equity": expected_peak, "is_locked": expected_lock}}


def main() -> int:
    ap = argparse.ArgumentParser(description="切换彩排 (库副本沙箱, 生产零接触)")
    ap.add_argument("--to", choices=("live", "sim"), default="live",
                    help="彩排哪个方向的切换 (默认 sim→live)")
    ap.add_argument("--sandbox", default="", help="沙箱目录 (默认临时目录, 跑完即删)")
    ap.add_argument("--keep", action="store_true", help="保留沙箱供取证")
    ap.add_argument("--json", default="", help="另存 JSON 报告路径")
    args = ap.parse_args()

    # 方向语义: --to live ⇒ **目标库 = live** (切换后代码实际读写的那个),
    #            参照库 = sim (代码期望的 schema 由 sim 侧历年 ALTER 塑造);
    #            缺口 = 参照有而目标无 = 切换日会在目标库上触发的 DDL。
    if args.to == "live":
        src, other = LIVE_DB, SIM_DB
    else:
        src, other = SIM_DB, LIVE_DB
    dst_name = "target.db"
    if not src.exists():
        print(f"⛔ 源库不存在: {src}", file=sys.stderr)
        return 2

    sandbox = Path(args.sandbox) if args.sandbox else Path(tempfile.mkdtemp(prefix="cutover_rehearse_"))
    # 硬断言: 沙箱绝不能在 data/ 下 (防把副本当生产库写)。
    # 用 parents 判定而不是 try/relative_to + `except ValueError: pass` —— 后者把
    # "守卫通过"与"守卫异常"塞进同一条 except 通道, 且会被
    # tests/test_except_pass_ratchet.py 记为静默吞错 (R5 纪律)。语义等价:
    # relative_to 成功 ⟺ 目标是自身或自身的祖先。
    _sbx, _dat = sandbox.resolve(), DATA.resolve()
    if _sbx == _dat or _dat in _sbx.parents:
        print(f"⛔ 沙箱不得位于 {DATA} 下: {sandbox}", file=sys.stderr)
        return 2

    started = datetime.now().astimezone().isoformat(timespec="seconds")
    print(f"=== 切换彩排 · {started} · 方向 →{args.to} ===")
    print(f"沙箱: {sandbox}")
    try:
        sandbox.mkdir(parents=True, exist_ok=True)
        tgt = sandbox / dst_name
        oth = sandbox / "other.db"
        _copy_db(src, tgt)
        _copy_db(other, oth)
        print(f"副本: 目标库={tgt.name} ({tgt.stat().st_size/1e6:.1f}MB) "
              f"对照库={oth.name} ({oth.stat().st_size/1e6:.1f}MB)")

        # 切换前: 目标库相对参照库缺什么 = 切换日会在目标库上触发的 DDL 面
        before = _diff(other, tgt)
        print(f"\n[1] 迁移前缺口: 缺表 {len(before['missing_tables'])} · "
              f"缺列 {before['n_missing_columns']}")
        for t, cs in list(before["missing_columns"].items())[:8]:
            print(f"      {t}: {cs if len(cs) <= 6 else str(cs[:6]) + f' …共{len(cs)}列'}")

        print("\n[2] 执行 app 真实迁移路径 (对副本) …")
        mig = apply_migrations(tgt)
        for k, v in mig.items():
            print(f"      {'✅' if v == 'ok' else '⛔'} {k}: {v}")

        after = _diff(other, tgt)
        residual = after["n_missing_columns"]
        healed = before["n_missing_columns"] - residual
        print(f"\n[3] 迁移后残留缺列: {residual} (已自愈 {healed}) | "
              f"其中阻塞性 {after['n_blocking_columns']}")
        for t, cs in after["missing_columns"].items():
            benign = [c for c in cs if (t, c) in BENIGN_MISSING_COLUMNS]
            hard = [c for c in cs if c not in benign]
            if hard:
                print(f"      ⛔ {t} 仍缺 (阻塞): {hard}")
            if benign:
                print(f"      ℹ️  {t} 仍缺 (已知良性): {benign}")
        if after["blocking_tables"]:
            print(f"      ⛔ 仍缺表 (阻塞): {after['blocking_tables'][:10]}")
        benign_tabs = [t for t in after["missing_tables"] if t in BENIGN_MISSING_TABLES]
        if benign_tabs:
            print(f"      ℹ️  仍缺表 (已知良性 — 首次使用即自建/遗留死表): {benign_tabs}")

        print("\n[4] capital_state 双臂回切实证 (调真实 _load_state) …")
        rt = probe_capital_roundtrip(tgt)
        for r in rt["rows_in_sandbox"]:
            print(f"      沙箱行: {r}")
        print(f"      _load_state 实捞: {rt['load_state_result']}")
        icon = "✅" if rt["seamless_roundtrip"] else "⛔"
        print(f"      {icon} 回切能否恢复本臂基线(peak={rt['expected']['peak_equity']}, "
              f"is_locked={rt['expected']['is_locked']}): {rt['seamless_roundtrip']}")
        if not rt["seamless_roundtrip"]:
            print("      ↳ 病灶: capital_safety.py:755 `ORDER BY id DESC LIMIT 1` 未按 trade_env 过滤;"
                  " 另一臂行 id 更大 ⇒ 本臂基线被判『环境切换』整份丢弃 (含 is_locked 锁定状态)")

        ddl_clean = (after["n_blocking_columns"] == 0 and not after["blocking_tables"]
                     and all(v == "ok" for v in mig.values()))
        clean = ddl_clean and rt["seamless_roundtrip"]
        print(f"\n→ DDL 面: {'干净 (阻塞性残留 0 ⇒ 切换日零未知 DDL)' if ddl_clean else '⛔ 有阻塞性残留 (见上)'}")
        print(f"→ 回切面: {'无缝 (本臂基线可恢复)' if rt['seamless_roundtrip'] else '⛔ 会丢本臂基线 (含 is_locked 风险锁定被静默解除)'}")
        print(f"→ 彩排结论: {'干净 (切换日零 DDL 且双向无缝)' if clean else '未达无缝 (见上两面)'}")
        doc = {"generated_at": started, "direction": args.to, "sandbox": str(sandbox),
               "before": before, "after": after, "migrations": mig,
               "capital_roundtrip": rt, "clean": clean}
        if args.json:
            Path(args.json).write_text(json.dumps(doc, ensure_ascii=False, indent=1),
                                       encoding="utf-8")
            print(f"→ {args.json}")
        return 0 if clean else 1
    except Exception as exc:
        import traceback
        print(f"⛔ 彩排自身异常: {type(exc).__name__}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return 2
    finally:
        if not args.keep and not args.sandbox:
            shutil.rmtree(sandbox, ignore_errors=True)
            print(f"(沙箱已清理: {sandbox})")


if __name__ == "__main__":
    sys.exit(main())
