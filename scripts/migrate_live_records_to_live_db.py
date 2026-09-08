#!/usr/bin/env python3
"""RFC-078 批A · 把误落在 sim 库的**实盘**账本行迁进 live 库 (一次性, 默认 dry-run).

为什么存在 (2026-09-08 取证):
  看板券商侧恒 REAL (`manual_order_service._is_live()` 硬编码 return True), 而 DB 侧由
  `TRADE_ENV` 路由; 现网 `TRADE_ENV=1` ⇒ 真钱手动实盘单的账本一直写进 sim 数据收集仓
  `data/ftseek_us.db`。批A 已把看板路由改指 live 库 (`get_dashboard_db()`), 但**历史行
  仍留在 sim 库** ⇒ 不迁则实盘周报/绩效会漏掉 2026-06-10 ~ 2026-09-04 的全部手动实盘留痕。

迁移范围 (只迁**实属实盘**的行, 按 env 标记列筛, 绝不按时间猜):
  · `trade_records`      WHERE trade_mode = 'live'          (实测 3 行)
  · `manual_order_audit` WHERE mode LIKE 'live%'            (实测 64 行)
  刻意**不迁**:
  · `open_positions` — sim 库现役 3 行全是 account_type='simulate' 的数据收集仓, 属实盘侧 0 行;
  · `trade_attribution` — 该表**无任何 env 标记列** (实测 81 列里只有 owner_strategy/
    model_scores/llm_model), 无法可靠区分实盘/模拟行 ⇒ 按纪律**不猜不迁**, 登记为已知缺口。

三条安全纪律:
  1. **默认 dry-run**: 不带 `--apply` 只打印计划, 零写入 (两库都以 `mode=ro` 打开)。
  2. **先备份**: `--apply` 前把两库物理复制到 `data/backups/<ts>-rfc078-migrate/`, 备份失败即中止。
  3. **不丢账**: 待迁行在目标库**缺列**且该列有非空值时, 先幂等 `ALTER TABLE ADD COLUMN`
     再迁 —— 这是对 DEB-20260905 §13.17「休眠列刻意不补」的**有意偏离**, 理由: 该裁决的
     目的是不让补列掩盖切换缺口, 而影子核对已判 `READY_TO_SWITCH`(阻塞 0), 缺口量化任务
     已完成; 此时 `trade_records.fees_est`(实测 4.01 / 4.78, 真钱手续费) 若不补列就会被
     **静默丢弃** = 实盘账本丢账, 比"掩盖缺口"严重。补了哪些列会逐条打印并写进报告。

去重: 按自然键判存在, 已存在则跳过 (幂等, 可反复跑)。
  · trade_records      (symbol, entry_time, source_type, quantity)
  · manual_order_audit (timestamp, symbol, order_id, action, quantity)
主键: **一律不搬 id**, 由目标库自增分配 —— 实测 `manual_order_audit` 两侧 id 空间重叠
  (sim 侧 17..83 vs live 侧 1..96), 搬 id 必然撞主键; `trade_records` 虽不重叠
  (sim 1036/1039/1327 vs live max 901) 也统一不搬, 避免两表规则分裂。

用法:
  PYTHONPATH=. venv/bin/python scripts/migrate_live_records_to_live_db.py            # dry-run
  PYTHONPATH=. venv/bin/python scripts/migrate_live_records_to_live_db.py --apply    # 实跑
回滚: cp data/backups/<ts>-rfc078-migrate/ftseek_us_live.db data/ftseek_us_live.db
"""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Sequence, Tuple

ROOT = Path(__file__).resolve().parents[1]
SIM_DB = ROOT / "data" / "ftseek_us.db"
LIVE_DB = ROOT / "data" / "ftseek_us_live.db"
REPORT = ROOT / "data" / "rfc078_migration_report.json"

# 表名 → (实盘筛选谓词, 自然键列)
PLAN: Dict[str, Tuple[str, Sequence[str]]] = {
    "trade_records": ("trade_mode = 'live'", ("symbol", "entry_time", "source_type", "quantity")),
    "manual_order_audit": ("mode LIKE 'live%'", ("timestamp", "symbol", "order_id", "action", "quantity")),
}


def _cols(conn: sqlite3.Connection, table: str) -> List[str]:
    return [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]


def _table_md5(path: Path, table: str) -> str:
    """全表内容指纹 (证明本脚本对 sim 库零写入)."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
        finally:
            conn.close()
        return hashlib.md5(repr(rows).encode()).hexdigest()
    except Exception as exc:  # noqa: BLE001 — 指纹失败不得阻断迁移计划输出
        return f"<unfingerprintable:{exc}>"


def _existing_keys(conn: sqlite3.Connection, table: str, keys: Sequence[str]) -> set:
    sel = ", ".join(f"COALESCE(CAST({k} AS TEXT),'') " for k in keys)
    try:
        return {tuple(r) for r in conn.execute(f"SELECT {sel} FROM {table}")}
    except sqlite3.OperationalError:
        return set()


def _sqlite_type(sample) -> str:
    if isinstance(sample, bool):
        return "INTEGER"
    if isinstance(sample, int):
        return "INTEGER"
    if isinstance(sample, float):
        return "REAL"
    return "TEXT"


def build_plan() -> Dict[str, object]:
    """纯只读地算出迁移计划 (dry-run 与 --apply 共用同一份计划, 避免"说的和做的不一样")."""
    sim = sqlite3.connect(f"file:{SIM_DB}?mode=ro", uri=True)
    live = sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True)
    out: Dict[str, object] = {"tables": {}, "fingerprints_before": {}}
    try:
        for table, (where, keys) in PLAN.items():
            sim_cols, live_cols = _cols(sim, table), _cols(live, table)
            out["fingerprints_before"][f"sim.{table}"] = _table_md5(SIM_DB, table)
            out["fingerprints_before"][f"live.{table}"] = _table_md5(LIVE_DB, table)
            rows = sim.execute(f"SELECT * FROM {table} WHERE {where}").fetchall()
            have = _existing_keys(live, table, keys)
            key_idx = [sim_cols.index(k) for k in keys if k in sim_cols]

            pending, skipped = [], []
            need_cols: Dict[str, str] = {}
            for row in rows:
                k = tuple("" if row[i] is None else str(row[i]) for i in key_idx)
                (skipped if k in have else pending).append(row)
                if k not in have:
                    for name, val in zip(sim_cols, row):
                        if name in live_cols or val is None or name == "id":
                            continue
                        # 目标库缺列且源值非空 ⇒ 必须补列, 否则静默丢账
                        need_cols.setdefault(name, _sqlite_type(val))

            out["tables"][table] = {
                "where": where,
                "natural_key": list(keys),
                "source_rows": len(rows),
                "to_migrate": len(pending),
                "already_present_skipped": len(skipped),
                "target_cols_missing_in_live": sorted(set(sim_cols) - set(live_cols) - {"id"}),
                "cols_to_add_for_nonnull_payload": need_cols,
                "common_cols": [c for c in sim_cols if c in live_cols and c != "id"],
                "_pending": pending,
                "_sim_cols": sim_cols,
            }
    finally:
        sim.close()
        live.close()
    return out


def apply_plan(plan: Dict[str, object], backup_dir: Path) -> Dict[str, object]:
    backup_dir.mkdir(parents=True, exist_ok=True)
    for src in (SIM_DB, LIVE_DB):
        dst = backup_dir / src.name
        shutil.copy2(src, dst)
        if not dst.exists() or dst.stat().st_size == 0:
            raise RuntimeError(f"备份失败, 中止迁移: {dst}")
    for sidecar in SIM_DB.parent.glob("ftseek_us*.db-wal"):
        shutil.copy2(sidecar, backup_dir / sidecar.name)

    added, inserted = {}, {}
    live = sqlite3.connect(str(LIVE_DB), timeout=30)
    try:
        for table, spec in plan["tables"].items():
            need: Dict[str, str] = spec["cols_to_add_for_nonnull_payload"]
            live_cols = set(_cols(live, table))
            added[table] = []
            for col, typ in sorted(need.items()):
                if col in live_cols:
                    continue
                live.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                added[table].append(f"{col} {typ}")
            if need:
                live_cols = set(_cols(live, table))

            use_cols = [c for c in spec["_sim_cols"] if c in live_cols and c != "id"]
            idx = {c: spec["_sim_cols"].index(c) for c in use_cols}
            ph = ", ".join("?" for _ in use_cols)
            sql = f"INSERT INTO {table} ({', '.join(use_cols)}) VALUES ({ph})"
            n = 0
            for row in spec["_pending"]:
                live.execute(sql, tuple(row[idx[c]] for c in use_cols))
                n += 1
            inserted[table] = n
        live.commit()
    finally:
        live.close()
    return {"backup_dir": str(backup_dir), "columns_added": added, "rows_inserted": inserted}


def main() -> int:
    ap = argparse.ArgumentParser(description="RFC-078 批A: sim 库实盘行迁往 live 库")
    ap.add_argument("--apply", action="store_true", help="实跑 (默认 dry-run, 零写入)")
    ap.add_argument("--no-backup", action="store_true", help="跳过备份 (仅测试用, 生产禁用)")
    args = ap.parse_args()

    for p in (SIM_DB, LIVE_DB):
        if not p.exists():
            print(f"❌ 库不存在: {p}", file=sys.stderr)
            return 2

    plan = build_plan()
    total = sum(t["to_migrate"] for t in plan["tables"].values())
    print(f"{'🔧 APPLY' if args.apply else '👁  DRY-RUN'} · RFC-078 实盘账本迁移 "
          f"({SIM_DB.name} → {LIVE_DB.name})")
    for table, spec in plan["tables"].items():
        print(f"\n  {table}  [{spec['where']}]  自然键={spec['natural_key']}")
        print(f"    源行 {spec['source_rows']} | 待迁 {spec['to_migrate']} | "
              f"已存在跳过 {spec['already_present_skipped']}")
        gap = spec["target_cols_missing_in_live"]
        if gap:
            print(f"    live 库缺列 {len(gap)}: {gap}")
        need = spec["cols_to_add_for_nonnull_payload"]
        if need:
            print(f"    ⚠ 待迁行携带非空值但 live 库缺列 ⇒ 必须补列 (否则静默丢账): "
                  f"{', '.join(f'{k} {v}' for k, v in sorted(need.items()))}")

    result: Dict[str, object] = {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "mode": "apply" if args.apply else "dry_run",
        "planned_rows": total,
        "plan": {k: {kk: vv for kk, vv in v.items() if not kk.startswith("_")}
                 for k, v in plan["tables"].items()},
    }

    if total == 0:
        print("\n✅ 无可迁行 (幂等: 已迁过或 sim 库无实盘行)")
        result["outcome"] = "NOOP"
    elif not args.apply:
        print(f"\n👁  dry-run 结束, 零写入。实跑加 --apply (计划迁移 {total} 行)")
        result["outcome"] = "DRY_RUN"
    else:
        ts = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup_dir = ROOT / "data" / "backups" / f"{ts}-rfc078-migrate"
        if args.no_backup:
            print("\n⚠ --no-backup: 生产禁用, 仅测试", file=sys.stderr)
            backup_dir = ROOT / "data" / "backups" / f"{ts}-rfc078-migrate-NOBACKUP"
            backup_dir.mkdir(parents=True, exist_ok=True)
            applied = {"backup_dir": str(backup_dir), "columns_added": {}, "rows_inserted": {}}
            live = sqlite3.connect(str(LIVE_DB), timeout=30)
            try:
                for table, spec in plan["tables"].items():
                    for col, typ in sorted(spec["cols_to_add_for_nonnull_payload"].items()):
                        if col not in _cols(live, table):
                            live.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typ}")
                            applied["columns_added"].setdefault(table, []).append(f"{col} {typ}")
                    live_cols = set(_cols(live, table))
                    use_cols = [c for c in spec["_sim_cols"] if c in live_cols and c != "id"]
                    idx = {c: spec["_sim_cols"].index(c) for c in use_cols}
                    sql = f"INSERT INTO {table} ({', '.join(use_cols)}) VALUES ({', '.join('?' for _ in use_cols)})"
                    n = 0
                    for row in spec["_pending"]:
                        live.execute(sql, tuple(row[idx[c]] for c in use_cols))
                        n += 1
                    applied["rows_inserted"][table] = n
                live.commit()
            finally:
                live.close()
        else:
            applied = apply_plan(plan, backup_dir)
        result.update(applied)

        after = {f"sim.{t}": _table_md5(SIM_DB, t) for t in PLAN}
        polluted = [k for k, v in after.items()
                    if v != plan["fingerprints_before"][k]]
        result["fingerprints_after_sim"] = after
        result["sim_polluted"] = polluted
        print(f"\n  备份: {applied['backup_dir']}")
        for t, cols in applied["columns_added"].items():
            if cols:
                print(f"  补列 {t}: {cols}")
        for t, n in applied["rows_inserted"].items():
            print(f"  写入 {t}: {n} 行")
        if polluted:
            print(f"\n🚨 sim 库指纹变化 (本脚本只以 mode=ro 读它, 变化必来自外部写方): {polluted}",
                  file=sys.stderr)
            result["outcome"] = "APPLIED_WITH_SIM_DRIFT"
        else:
            print("\n✅ sim 库指纹未变 = 本脚本对 sim 库零写入")
            result["outcome"] = "APPLIED"

    REPORT.write_text(json.dumps(result, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    print(f"报告: {REPORT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
