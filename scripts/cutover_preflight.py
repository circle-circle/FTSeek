#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""切换预检 (cutover preflight) — 双向 sim⇄live 的"能不能切、切了会崩什么"一页读数.

为什么存在 (2026-09-14, 用户裁「目前只是先将基建做好，待未来可切换时，可以实现
sim到实盘以及实盘再切换为sim，无缝实现」):
  既有 cutover 文档 (docs/decisions/CUTOVER-DECISION-PACKAGE-20260909.md,
  docs/PROPOSAL-20260909-cutover-dual-arm-routing.md) 记的是**单向** sim→live 的缺口清单,
  且读数散在 4 个文件/2 张表; 回切 (live→sim) 方向从未被系统检查过。本脚本把两个方向的
  阻塞项收敛成**一条命令 + 一个退出码**, 供裁决日与日常巡检共用。

设计纪律:
  · **生产库全程只读** (`file:...?mode=ro`), 不连券商, 无 SDK 悬挂, 不写任何生产表。
  · 产物只落 `data/cutover/` (已被 .gitignore 的 `data/*` 覆盖) ⇒ 零仓库污染。
  · schema 差异**从两库实测推导**, 不复制 app/ 里的 ALTER 清单 (复制必漂移)。
  · 语义污染判定用**跨表铁证** (open_positions.sl_broker_order_id == trade_records.futu_order_id)
    而非"数字/字母"启发式; 启发式只作旁证并显式标注。

退出码契约 (给 cron / 守卫脚本用, 不靠 stdout 猜) —— **两种模式语义不同, 勿混用**:
  [巡检模式 默认]
    0 = 无 BLOCK 项 (仍可能有 WARN; 切换与否由 C2 红线 + 用户签字决定)
    1 = ≥1 个 BLOCK 项
    2 = 脚本自身异常 (读数不可信, 不得当作"通过")
  [--watch 哨兵模式] 棘轮语义 (照抄 data_freshness_watchdog 的基线棘轮范式):
    0 = 与基线一致 (静默, 不推送)
    1 = 出现**新增/升级**的探针状态 (已推送)
    2 = 脚本自身异常, **或基线尚未冻结** (fail-loud: 宁可吵也不静默装通过)

为什么 --watch 必须是棘轮而不是"有 BLOCK 就报": 毕业门禁 (P1) 在 IC 转正之前会**长期**
block, 直接推 ⇒ 每天一条同样的红 ⇒ 告警疲劳 ⇒ 真回退那天没人看。故常驻项进基线静默,
只有**变化**才告警 (新增探针 / warn→block 升级 / 或 block→ok 的**改善**, 改善也推,
因为"某项阻塞悄悄消失"同样需要人知道 —— 可能是修好了, 也可能是探针瞎了)。

用法:
  venv/bin/python scripts/cutover_preflight.py                 # 两个方向都检
  venv/bin/python scripts/cutover_preflight.py --to live       # 只检 sim→live
  venv/bin/python scripts/cutover_preflight.py --skip-gate     # 跳过毕业门禁实算 (慢)
  venv/bin/python scripts/cutover_preflight.py --json-only
  venv/bin/python scripts/cutover_preflight.py --accept-baseline   # 冻结当前状态为棘轮基线 (人工动作)
  venv/bin/python scripts/cutover_preflight.py --watch --push      # 哨兵: 只对变化告警
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DATA = ROOT / "data"
SIM_DB = DATA / "ftseek_us.db"
LIVE_DB = DATA / "ftseek_us_live.db"
OUT_DIR = DATA / "cutover"
BASELINE = OUT_DIR / "preflight_baseline.json"
COOLDOWN_STATE = OUT_DIR / "preflight_push_state.json"
# 2026-10-16: 30min 时间窗冷却已退役 (改内容指纹, 见 push_alert) — 实证同一句告警
# 连推 9 天; 常量删除防误读回旧语义。

# 状态优先级: block > warn > ok > na (汇总取最严重)
_SEV = {"ok": 0, "na": 1, "warn": 2, "block": 3}
_ICON = {"ok": "✅", "warn": "⚠️ ", "block": "⛔", "na": "—"}
_BENIGN_MISSING_COLUMNS = {("capital_state", "account_id")}


# ─────────────────────────────────────────────────────────────
# 只读基础设施
# ─────────────────────────────────────────────────────────────
def _ro(db: Path) -> sqlite3.Connection:
    """只读连接 — 绝不创建/写入生产库文件 (库不存在则抛, 不静默建空库)."""
    if not db.exists():
        raise FileNotFoundError(f"库不存在: {db}")
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=10)
    con.row_factory = sqlite3.Row
    return con


# 跳过的数据源留痕 (2026-09-15, tests/test_except_pass_ratchet.py 的 R5 纪律):
#   诊断器最坏的失效形态是「数据源打不开」与「查了没查出东西」**输出同形** ⇒
#   库被改名/删除时 P3-P9 会集体报"干净"= 假绿 (DEB-20260831 P1③ 同族病灶)。
#   故所有"打不开就跳过"的分支一律在此留痕, 并由 main() 汇总显式打印。
SKIPPED_SOURCES: List[str] = []


def _open_lane(tag: str, db: Path) -> Optional[sqlite3.Connection]:
    """打开某一臂的只读连接; 库文件缺失则**留痕**并返回 None (不静默 continue)."""
    try:
        return _ro(db)
    except FileNotFoundError:
        SKIPPED_SOURCES.append(f"{tag}:{db.name}")
        return None


def _one(con: sqlite3.Connection, sql: str, args: Tuple = ()) -> Any:
    try:
        row = con.execute(sql, args).fetchone()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def _tables(con: sqlite3.Connection) -> List[str]:
    return [r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def _cols(con: sqlite3.Connection, table: str) -> List[str]:
    try:
        return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]
    except sqlite3.Error:
        return []


def _read_env_file() -> Dict[str, str]:
    """解析 .env.us (不 import app ⇒ 不触发进程内固化缓存, 读数即文件真值)."""
    out: Dict[str, str] = {}
    p = ROOT / ".env.us"
    if not p.exists():
        return out
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        s = line.strip()
        if not s or s.startswith("#") or "=" not in s:
            continue
        k, v = s.split("=", 1)
        out[k.strip()] = v.strip()
    return out


class Report:
    """探针结果收集器 — 每条 finding 带 status/证据/后果/处置, 不留"只报警不给路"."""

    def __init__(self) -> None:
        self.items: List[Dict[str, Any]] = []

    def add(self, pid: str, name: str, status: str, reading: str,
            why: str = "", fix: str = "", evidence: Optional[Dict] = None) -> None:
        if status not in _SEV:
            raise ValueError(f"非法 status: {status}")
        self.items.append({"id": pid, "name": name, "status": status,
                           "reading": reading, "why": why, "fix": fix,
                           "evidence": evidence or {}})

    @property
    def worst(self) -> str:
        return max((i["status"] for i in self.items), key=lambda s: _SEV[s], default="na")

    @property
    def blocked(self) -> bool:
        return any(i["status"] == "block" for i in self.items)

    def counts(self) -> Dict[str, int]:
        c = {k: 0 for k in _SEV}
        for i in self.items:
            c[i["status"]] += 1
        return c

    def statuses(self) -> Dict[str, str]:
        """探针 id → status (棘轮比对只看 status, 不看 reading 原文).

        为什么不比 reading: P1 的 ic_t3、P8 的行数**每天都漂**, 比原文会天天告警。
        棘轮只关心"某项从 ok 变 block 了"这类**状态跃迁**。
        """
        return {i["id"]: i["status"] for i in self.items}


# ─────────────────────────────────────────────────────────────
# 棘轮基线 (--watch)
# ─────────────────────────────────────────────────────────────
def load_baseline() -> Dict[str, str]:
    try:
        return json.loads(BASELINE.read_text(encoding="utf-8")).get("keys", {})
    except Exception:
        return {}


def accept_baseline(rep: Report) -> Dict[str, str]:
    """冻结当前全部探针状态为棘轮基线 (人工动作, 与 watchdog --update-liveness-baseline 同款)."""
    keys = rep.statuses()
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    BASELINE.write_text(json.dumps({
        "frozen_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "rule": ("棘轮: 基线内的探针状态静默 (既有墓地/长期未裁面), 状态跃迁才告警; "
                 "重新冻结是人工动作 (--accept-baseline), 脚本自身永不自动写基线"),
        "keys": keys,
        "readings": {i["id"]: i["reading"] for i in rep.items},
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    return keys


def diff_against_baseline(rep: Report, baseline: Dict[str, str]) -> Dict[str, List[Dict[str, str]]]:
    """返回 {new, escalated, improved, gone} 四类状态跃迁."""
    cur = rep.statuses()
    by_id = {i["id"]: i for i in rep.items}
    new, escalated, improved, gone = [], [], [], []
    for pid, st in cur.items():
        old = baseline.get(pid)
        if old is None:
            new.append({"id": pid, "status": st, "reading": by_id[pid]["reading"]})
        elif _SEV[st] > _SEV[old]:
            escalated.append({"id": pid, "from": old, "to": st, "reading": by_id[pid]["reading"],
                              "why": by_id[pid]["why"]})
        elif _SEV[st] < _SEV[old]:
            improved.append({"id": pid, "from": old, "to": st, "reading": by_id[pid]["reading"]})
    for pid in baseline:
        if pid not in cur:
            gone.append({"id": pid, "was": baseline[pid]})
    return {"new": new, "escalated": escalated, "improved": improved, "gone": gone}


def _hash_msg(msg: str) -> str:
    """内容指纹: 抹掉数字漂移 (2026-10-16 与 shadow_board episode 指纹同规)。"""
    import hashlib
    import re as _re
    norm = _re.sub(r"[\d.]+", "#", msg)
    return hashlib.sha256(norm.encode("utf-8")).hexdigest()[:16]


def push_alert(msg: str, *, push: bool) -> str:
    """同内容告警只推一次, 内容变化才再推 (2026-10-16 改: 原 30min 时间窗对日频作业
    等于无冷却 — 实证同一句「P6 warn→ok」连推 9 天; 改内容指纹判定)."""
    import time as _time
    now = _time.time()
    try:
        prev = json.loads(COOLDOWN_STATE.read_text(encoding="utf-8"))
        if prev.get("msg_hash") == _hash_msg(msg):
            return "cooldown_suppressed"
    except FileNotFoundError:
        SKIPPED_SOURCES.append("cooldown_state:absent(首次推送)")
    except Exception as exc:
        # 冷却状态不可读 ⇒ 宁可重复推送也不静默抑制告警 (失败方向选 fail-loud)
        SKIPPED_SOURCES.append(f"cooldown_state_unreadable:{type(exc).__name__}")
    if not push:
        return "dry (未加 --push)"
    # 2026-10-15 (审计 P2-4): hub-first (sync=True; stop() 收线程池), 失败回退直连 ——
    # 冷却写状态只在「送达成功」后 (任一通道), 保持"宁多勿漏"语义不变。
    pushed = False
    try:
        from app.push import get_push_hub
        from app.push.message import Category, Level, PushMessage
        hub = get_push_hub()
        try:
            pushed = bool(hub.push(PushMessage(
                title="切档预检告警", body=msg, level=Level.HIGH,
                category=Category.SYSTEM), sync=True))
        finally:
            hub.stop()
    except Exception as exc:  # noqa: BLE001 — 回退路径接管
        SKIPPED_SOURCES.append(f"hub_unavailable:{type(exc).__name__}")
    if not pushed:
        try:
            from app.notifier.feishu import send_text
            send_text(msg)
        except Exception as exc:
            return f"push_failed {type(exc).__name__}: {exc}"
    try:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        COOLDOWN_STATE.write_text(json.dumps({"ts": now, "msg_hash": _hash_msg(msg)}),
                                  encoding="utf-8")
    except OSError as exc:
        # 写不进冷却状态 ⇒ 下次同告警会重复推送 (可接受的失败方向: 宁多勿漏)
        SKIPPED_SOURCES.append(f"cooldown_state_unwritable:{type(exc).__name__}")
    return "pushed"


# ─────────────────────────────────────────────────────────────
# P0 三态一致性 / 半翻检测
# ─────────────────────────────────────────────────────────────
def p0_env_triple(rep: Report) -> Dict[str, str]:
    env = _read_env_file()
    trade_env = env.get("TRADE_ENV", "")
    auto_acct = env.get("AUTO_TRADE_ACCOUNT", "")
    allow_real = env.get("ALLOW_REAL_TRADING", "").lower()
    isolation = env.get("FTSEEK_DASHBOARD_DB_ISOLATION", "1")

    db_is_live = trade_env.strip() == "0"
    broker_is_real = auto_acct.strip().lower() == "real"
    real_allowed = allow_real == "true"

    # 半翻态 = broker 真钱但账本仍进 sim 库 (RFC-080 S2' 互锁拦的就是这个)
    half_flip = broker_is_real and not (db_is_live and real_allowed)
    # 反向半翻 = 账本进 live 库但 broker 仍 sim (记录身份与成交身份分裂)
    half_flip_rev = db_is_live and not broker_is_real

    reading = (f"TRADE_ENV={trade_env or '(未设)'} AUTO_TRADE_ACCOUNT={auto_acct or '(未设)'} "
               f"ALLOW_REAL_TRADING={allow_real or '(未设)'} DASHBOARD_DB_ISOLATION={isolation}")
    if half_flip or half_flip_rev:
        rep.add("P0", "三态一致性 (半翻检测)", "block", reading,
                why=("半翻态 = broker 与账本身份分裂: 真钱成交记进研究库 (或反之)。"
                     "运行期由 playbook_service:1222 的 S2' 互锁 fail-close 降级 sim, "
                     "但 .env 层面不该长期停在半翻态。"),
                fix="三开关必须同翻: TRADE_ENV=0 + AUTO_TRADE_ACCOUNT=real + ALLOW_REAL_TRADING=true",
                evidence={"half_flip": half_flip, "half_flip_rev": half_flip_rev})
    else:
        rep.add("P0", "三态一致性 (半翻检测)", "ok", reading,
                why="自洽 (全 sim 或全 live), 无半翻态",
                evidence={"db_is_live": db_is_live, "broker_is_real": broker_is_real,
                          "real_allowed": real_allowed})
    if isolation.strip() == "0":
        rep.add("P0b", "看板库隔离开关", "warn", f"FTSEEK_DASHBOARD_DB_ISOLATION={isolation}",
                why="隔离关闭 ⇒ get_dashboard_db() 回落 get_main_db(), 看板手动实盘单会与自动臂同库",
                fix="保持 =1 (RFC-078)")
    return {"db_is_live": str(db_is_live), "broker_is_real": str(broker_is_real),
            "real_allowed": str(real_allowed), "trade_env": trade_env}


# ─────────────────────────────────────────────────────────────
# P1 毕业门禁 (研究库 pin)
# ─────────────────────────────────────────────────────────────
def p1_graduation_gate(rep: Report, skip: bool) -> None:
    if skip:
        rep.add("P1", "毕业门禁", "na", "已跳过 (--skip-gate)",
                why="门禁实算需扫 playbook_instructions 30 天窗, 慢; 裁决日必须跑")
        return
    try:
        from app.playbook.pipeline import _check_graduation_gate
        g = _check_graduation_gate()
    except Exception as exc:  # fail-close: 读不到门禁 = 不可切
        rep.add("P1", "毕业门禁", "block", f"门禁实算异常: {type(exc).__name__}: {exc}",
                why="_check_graduation_gate 自身 fail-close 返回 graduated=False",
                fix="先修门禁异常再谈切换")
        return
    checks = g.get("checks", {}) or {}
    reading = (f"graduated={g.get('graduated')} ic_t3={g.get('ic_t3')} "
               f"n_t3={g.get('sample_t3')} reason={g.get('reason') or '-'}")
    failed = [k for k, v in checks.items() if not v]
    rep.add("P1", "毕业门禁 (研究库 pin)", "ok" if g.get("graduated") else "block", reading,
            why=("未达标 ⇒ C2 红线: 不得切换。门禁证据恒产自 sim 研究库 "
                 "(pipeline.py:3444 PlaybookDB(db_name='ftseek_research')), 翻转后不会自锁。"
                 if failed else "门禁全绿 (仍需周检报告 + 用户签字, 达标≠自动翻转)"),
            fix=f"未过判据: {failed}" if failed else "",
            evidence={"checks": checks})


# ─────────────────────────────────────────────────────────────
# P2 schema 漂移 + 切换日 DDL 面 (从两库实测推导)
# ─────────────────────────────────────────────────────────────
def p2_schema_drift(rep: Report) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    try:
        cs, cl = _ro(SIM_DB), _ro(LIVE_DB)
    except FileNotFoundError as exc:
        rep.add("P2", "schema 漂移", "block", str(exc), fix="确认两库路径")
        return out
    try:
        ts, tl = set(_tables(cs)), set(_tables(cl))
        shared = sorted(ts & tl)
        miss_col: Dict[str, Dict[str, List[str]]] = {}
        order_diff: List[str] = []
        for t in shared:
            a, b = _cols(cs, t), _cols(cl, t)
            if not a or not b:
                continue
            sa, sb = set(a), set(b)
            if sa != sb:
                miss_col[t] = {"sim_only": sorted(sa - sb), "live_only": sorted(sb - sa)}
            elif a != b:
                order_diff.append(t)
        only_sim, only_live = sorted(ts - tl), sorted(tl - ts)

        # 切换日会在**目标库**上触发的补列 DDL = 目标库缺的列
        # (自愈迁移存在: trade_store.py:398 / playbook/db.py:288 / capital_safety.py:723 /
        #  lesson_injector.py:33 — 但都是 try/except pass, 真失败与"列已存在"静默不可分)
        benign_missing = sum(
            1 for table, columns in miss_col.items()
            for column in columns["sim_only"]
            if (table, column) in _BENIGN_MISSING_COLUMNS
        )
        ddl_on_live = sum(len(v["sim_only"]) for v in miss_col.values()) - benign_missing
        ddl_on_sim = sum(len(v["live_only"]) for v in miss_col.values())

        out = {"tables_sim": len(ts), "tables_live": len(tl),
               "sim_only_tables": only_sim, "live_only_tables": only_live,
               "column_diff": miss_col, "order_diff": order_diff,
               "ddl_on_live": ddl_on_live, "ddl_on_sim": ddl_on_sim}

        reading = (f"共有表 {len(shared)} | 列集不同 {len(miss_col)} 张 | 列序不同 {len(order_diff)} 张 | "
                   f"切 live 有效补列 {ddl_on_live} 条 (良性 {benign_missing}) · 切 sim 触发 {ddl_on_sim} 条")
        # 执行链关键列缺失 = 成交簿记 CAS 会打空 (虽自愈, 但切换日首笔即 DDL)
        critical = {"playbook_instructions": {"execution_state", "execution_attempt",
                                              "broker_order_id", "broker_order_status"}}
        hits = []
        for t, need in critical.items():
            got = set(miss_col.get(t, {}).get("live_only", []) ) | set()
            lack = need & set(miss_col.get(t, {}).get("sim_only", []))
            if lack:
                hits.append(f"{t} 缺 {sorted(lack)}")
        status = "warn" if (ddl_on_live or ddl_on_sim or order_diff) else "ok"
        if hits:
            status = "warn"
        rep.add("P2", "schema 漂移 / 切换日 DDL 面", status, reading,
                why=("两库 schema 不同源 (历年 ad-hoc ALTER)。幂等自愈迁移会在**切换当日**"
                     "于目标库首次执行 DDL, 且全部包在 `except Exception: pass` 里 ⇒ "
                     "真失败(库锁/语句错)与'列已存在'静默不可分。"
                     + ("; 执行链关键列: " + "; ".join(hits) if hits else "")),
                fix=("切换前用 scripts/cutover_rehearse.py 在库副本上预跑迁移, "
                     "把切换日 DDL 压到 0; 列序不同者禁用 SELECT * + 下标访问"),
                evidence={"hits": hits, "order_diff": order_diff,
                          "sim_only_tables": only_sim, "live_only_tables": only_live})
    finally:
        cs.close(), cl.close()
    return out


# ─────────────────────────────────────────────────────────────
# P3 capital_state 双臂基线 (回切致命点)
# ─────────────────────────────────────────────────────────────
def p3_capital_state(rep: Report) -> None:
    findings = {}
    for tag, db in (("sim", SIM_DB), ("live", LIVE_DB)):
        con = _open_lane(tag, db)
        if con is None:
            continue
        try:
            cols = _cols(con, "capital_state")
            if not cols:
                findings[tag] = {"exists": False}
                continue
            rows = [dict(r) for r in con.execute(
                "SELECT * FROM capital_state ORDER BY id DESC")]
            findings[tag] = {"exists": True, "cols": cols, "n_rows": len(rows),
                             "rows": rows[:4], "has_account_id": "account_id" in cols,
                             "envs_present": sorted({r.get("trade_env") for r in rows})}
        finally:
            con.close()

    lv = findings.get("live", {})
    sm = findings.get("sim", {})
    # 回切检查: LIVE 库的 capital_state 必须有且仅有 trade_env=0 行。
    # 2026-10-03 起 _load_state 固定 WHERE trade_env=?, 双臂基线按库隔离。
    live_envs = set(lv.get("envs_present") or set())
    roundtrip_hazard = bool(lv.get("n_rows")) and live_envs != {0}
    reading = (f"sim {sm.get('n_rows', 0)} 行(envs={sm.get('envs_present')}) | "
               f"live {lv.get('n_rows', 0)} 行(envs={lv.get('envs_present')}) | "
               f"live 有 account_id 列={lv.get('has_account_id')}")
    if not lv.get("n_rows"):
        rep.add("P3", "capital_state 双臂基线", "warn", reading,
                why=("live 库 capital_state 为空 ⇒ 切换日 capital_safety 以配置初值起步, "
                     "再由首次券商同步把 initial_capital 重置为当时净值 "
                     "(capital_safety.py:193-204) ⇒ peak_equity/max_drawdown_pct/is_locked "
                     "**全部归零**, 账户级回撤保护开局失明。"),
                fix="切换前按真钱账户显式预置 live 侧基线行并留痕 (含 trade_env=0)",
                evidence=findings)
    elif roundtrip_hazard:
        rep.add("P3", "capital_state 双臂基线 (回切)", "warn", reading,
                why=("LIVE capital_state 的 trade_env 集合异常, 回切时无法确认本臂基线。"),
                fix="运行 scripts/prepare_live_probe.py --apply 重建 trade_env=0 基线",
                evidence=findings)
    else:
        rep.add("P3", "capital_state 双臂基线", "ok", reading, evidence=findings)


# ─────────────────────────────────────────────────────────────
# P4 open_positions 臂判别 / 归属冲突 / 僵尸行
# ─────────────────────────────────────────────────────────────
def p4_open_positions(rep: Report) -> None:
    info = {}
    for tag, db in (("sim", SIM_DB), ("live", LIVE_DB)):
        con = _open_lane(tag, db)
        if con is None:
            continue
        try:
            cols = _cols(con, "open_positions")
            if not cols:
                continue
            sql = _one(con, "SELECT sql FROM sqlite_master WHERE type='table' AND name='open_positions'") or ""
            pk_symbol = bool(re.search(r"symbol\s+TEXT\s+PRIMARY\s+KEY", sql, re.I))
            rows = [dict(r) for r in con.execute("SELECT * FROM open_positions")]
            info[tag] = {
                "n": len(rows), "pk_is_symbol": pk_symbol,
                "has_source_type": "source_type" in cols,
                "has_owner": "owner" in cols,
                "has_account_type": "account_type" in cols,
                "rows": rows,
                "owners": sorted({str(r.get("owner")) for r in rows}),
                "account_types": sorted({str(r.get("account_type")) for r in rows}),
                "zombie": sorted(str(r.get("symbol")) for r in rows
                                 if not float(r.get("size") or 0)),
                "symbols": sorted(str(r.get("symbol")) for r in rows),
            }
        finally:
            con.close()

    sm, lv = info.get("sim", {}), info.get("live", {})
    overlap = sorted(set(sm.get("symbols", [])) & set(lv.get("symbols", [])))
    live_rows = list(lv.get("rows") or [])
    live_auto = {str(r.get("symbol")) for r in live_rows if str(r.get("owner")) == "auto"}
    live_manual = {str(r.get("symbol")) for r in live_rows if str(r.get("owner")) == "manual"}
    owner_conflicts = sorted(live_auto & live_manual)
    wrong_live_account_type = sorted({
        str(r.get("account_type")) for r in live_rows
        if str(r.get("account_type") or "").lower() not in {"real", "live"}
    })
    reading = (f"sim {sm.get('n', 0)} 行(owner={sm.get('owners')}) | "
               f"live {lv.get('n', 0)} 行(owner={lv.get('owners')}, 僵尸 size=0 共 {len(lv.get('zombie', []))}) | "
               f"PK=symbol:{lv.get('pk_is_symbol')} source_type列:{lv.get('has_source_type')} | "
               f"两库同标的重叠 {len(overlap)} (分库共存, 非冲突)")
    problems = []
    if owner_conflicts:
        problems.append(f"LIVE 同标的同时存在 auto/manual 行 {owner_conflicts} ⇒ 归属冲突")
    if wrong_live_account_type:
        problems.append(f"LIVE account_type 异常 {wrong_live_account_type} ⇒ 期望 real/live")
    if lv.get("zombie"):
        problems.append(f"live 僵尸行 {len(lv['zombie'])} 个 (size=0.0) ⇒ ManualHoldRegistry 视为手动持有, "
                        "切换日 auto 永久禁碰这些标的 (fail-safe 但静默产能损失; "
                        "S3a 已证不能用 size>0 过滤修, 正解是 live_ledger_sync 拉券商真值校正)")
    if problems:
        rep.add("P4", "open_positions 臂判别 / 归属冲突", "warn",
                reading, why=" | ".join(problems),
                fix="清除 size=0 僵尸行; auto/manual 冲突需人工裁决",
                evidence={"overlap": overlap, "owner_conflicts": owner_conflicts,
                          "sim": sm, "live": lv})
    else:
        rep.add("P4", "open_positions 臂判别 / 归属冲突", "ok", reading,
                evidence={"sim": sm, "live": lv})


# ─────────────────────────────────────────────────────────────
# P5 sl_broker_order_id 语义碰撞 (跨表铁证)
# ─────────────────────────────────────────────────────────────
def p5_sl_semantics(rep: Report) -> None:
    per_lane = {}
    for tag, db in (("sim", SIM_DB), ("live", LIVE_DB)):
        con = _open_lane(tag, db)
        if con is None:
            continue
        try:
            cols = _cols(con, "open_positions")
            if "sl_broker_order_id" not in cols:
                continue
            rows = [dict(r) for r in con.execute(
                "SELECT symbol, sl_broker_order_id, tp_broker_order_id FROM open_positions")]
            polluted, genuine, empty = [], [], []
            for r in rows:
                v = r.get("sl_broker_order_id")
                if not v:
                    empty.append(r["symbol"])
                    continue
                # 铁证: 与同标的 trade_records.futu_order_id (入场单号) 相同 ⇒ 该列装的是入场单
                tr = _one(con, "SELECT futu_order_id FROM trade_records WHERE symbol=? "
                               "AND COALESCE(futu_order_id,'')<>'' ORDER BY rowid DESC LIMIT 1",
                          (r["symbol"],))
                if tr and str(tr) == str(v):
                    polluted.append({"symbol": r["symbol"], "value": str(v),
                                     "tp": r.get("tp_broker_order_id")})
                else:
                    genuine.append({"symbol": r["symbol"], "value": str(v)})
            per_lane[tag] = {"n": len(rows), "polluted": polluted,
                             "genuine": genuine, "empty": empty}
        finally:
            con.close()

    sm, lv = per_lane.get("sim", {}), per_lane.get("live", {})
    n_pol = len(sm.get("polluted", [])) + len(lv.get("polluted", []))
    reading = (f"入场单号污染: sim {len(sm.get('polluted', []))} 行 / live {len(lv.get('polluted', []))} 行 | "
               f"真 SL 单号: sim {len(sm.get('genuine', []))} / live {len(lv.get('genuine', []))}")
    if n_pol:
        pol = (sm.get("polluted", []) + lv.get("polluted", []))
        rep.add("P5", "sl_broker_order_id 语义碰撞", "warn", reading,
                why=("写侧 trade_store.py:974 把**入场单号**存进名为 sl_ 的列 (docstring 自认"
                     "『语义匹配: 入场单的 broker order id』); 而看板读侧 oco_handler.py:86 / "
                     "executor.py:1155 按**止损单号**读 ⇒ 单库翻转后同表同列两种语义: "
                     "OCO 撤错单 (撤已成交的入场单) + 止损替换被 executor.py:1171 的 P1-9 "
                     "保险丝永久中止 (止损挪不动)。当前不可达: 污染行在 sim 库, "
                     "SL 语义读侧只开 live 库 (RFC-078)。"),
                fix=("新增 entry_broker_order_id 列承载入场单号, 让 sl_broker_order_id 回归单一语义 "
                     "+ 写后确认 (需 DEB: 动订单管道 + 加列迁移 + 重启)"),
                evidence={"polluted": pol, "sim": sm, "live": lv})
    else:
        rep.add("P5", "sl_broker_order_id 语义碰撞", "ok", reading,
                why="无跨表铁证命中 (该列当前只装真 SL 单号或为空)",
                evidence={"sim": sm, "live": lv})


# ─────────────────────────────────────────────────────────────
# P6 零里程路径 (broker STOP 兜底在生产从未跑过)
# ─────────────────────────────────────────────────────────────
def p6_stop_mileage(rep: Report) -> None:
    remarks = {}
    for tag, db in (("sim", SIM_DB), ("live", LIVE_DB)):
        con = _open_lane(tag, db)
        if con is None:
            continue
        try:
            n = 0
            for t in _tables(con):
                if "remark" in _cols(con, t):
                    n += int(_one(con, f'SELECT COUNT(*) FROM "{t}" WHERE remark LIKE ?',
                                  ("%pb_stop_%",)) or 0)
            remarks[tag] = n
        finally:
            con.close()
    log_hits = 0
    for p in sorted(ROOT.glob("logs/ftseek_us.log*")):
        try:
            if p.suffix == ".gz":
                import gzip
                with gzip.open(p, "rt", errors="replace") as fh:
                    log_hits += sum(1 for ln in fh if "live STOP 单已挂" in ln)
            else:
                log_hits += sum(1 for ln in p.read_text(errors="replace").splitlines()
                                if "live STOP 单已挂" in ln)
        except OSError as exc:
            # 日志档读不了 (轮转中/权限) ⇒ 留痕, 否则 P6 的"零里程"结论无法区分
            # "真的没挂过 STOP 单" 与 "日志根本没扫到"。
            SKIPPED_SOURCES.append(f"log:{p.name}:{type(exc).__name__}")
            continue
    total = sum(remarks.values()) + log_hits
    reading = f"pb_stop_ 留痕 {remarks} | 日志 'live STOP 单已挂' 命中 {log_hits}"
    if total == 0:
        rep.add("P6", "broker STOP 兜底里程", "warn", reading,
                why=("sim 券商不支持 STOP 单 (broker_adapter.py:700 返回 simulate_not_supported; "
                     "dual_broker.py:497 直接 skip) ⇒ 自动臂的 broker 侧止损兜底/OCO/止损替换"
                     "**整条链在生产零里程**, 切换日第一次执行就是真钱, 且立刻撞 P5。"
                     "sim 的止损一直靠规则引擎, 这层差异无法在 sim 内验证。"),
                fix=("切换前用真钱账户做**极小仓位**的 STOP 单挂/改/撤三段演练 (人工经看板), "
                     "把里程从 0 变成 ≥1 再放自动臂"),
                evidence={"remarks": remarks, "log_hits": log_hits})
    else:
        rep.add("P6", "broker STOP 兜底里程", "ok", reading,
                evidence={"remarks": remarks, "log_hits": log_hits})


# ─────────────────────────────────────────────────────────────
# P7 W3 双向混同守卫 (两账本不得互渗)
#────────────────────────────────────────────────────────────
def p7_contamination(rep: Report) -> None:
    bad = {}
    for tag, db, wrong in (("sim", SIM_DB, "live"), ("live", LIVE_DB, "simulate")):
        con = _open_lane(tag, db)
        if con is None:
            continue
        try:
            if "trade_mode" not in _cols(con, "trade_records"):
                continue
            if tag == "sim":
                rows = [dict(r) for r in con.execute(
                    "SELECT id, symbol, trade_mode, source_type FROM trade_records "
                    "WHERE trade_mode='live'")]
            else:
                rows = [dict(r) for r in con.execute(
                    "SELECT id, symbol, trade_mode, source_type FROM trade_records "
                    "WHERE COALESCE(trade_mode,'')<>'' AND trade_mode<>'live'")]
            if rows:
                bad[tag] = rows
        finally:
            con.close()
    if bad:
        rep.add("P7", "W3 双向混同守卫", "block",
                " | ".join(f"{k} 库有 {len(v)} 行对方账本" for k, v in bad.items()),
                why=("用户 2026-09-09 裁决『模拟交易和实盘交易不要混为一谈, 包括历史数据』。"
                     "混同行会污染实盘周报/台账对账 (§五 demix 事件已付过代价: "
                     "4 行 sim 仓误标搬进 live, TTE 一度删错方向)。"),
                fix="用 scripts/demix_sim_live_records.py 的 broker 真值定向法清理 (不得按时间猜)",
                evidence=bad)
    else:
        rep.add("P7", "W3 双向混同守卫", "ok", "两库 trade_mode 归属纯净 (双向 0 行)")


# ─────────────────────────────────────────────────────────────
# P8 研究臂窗口老化 (翻转+30天 IC 炸弹倒计时)
# ─────────────────────────────────────────────────────────────
def p8_research_arm(rep: Report) -> None:
    try:
        con = _ro(SIM_DB)
    except FileNotFoundError as exc:
        rep.add("P8", "研究臂窗口", "block", str(exc))
        return
    try:
        cols = _cols(con, "playbook_instructions")
        if not cols:
            rep.add("P8", "研究臂窗口", "na", "研究库无 playbook_instructions 表")
            return
        total = int(_one(con, "SELECT COUNT(*) FROM playbook_instructions") or 0)
        date_col = "created_at" if "created_at" in cols else (
            "trade_date" if "trade_date" in cols else None)
        recent = None
        if date_col:
            recent = int(_one(con, f"SELECT COUNT(*) FROM playbook_instructions "
                                   f"WHERE substr({date_col},1,10) >= date('now','-30 day')") or 0)
        tr_total = int(_one(con, "SELECT COUNT(*) FROM trade_records") or 0)
        arms = [dict(r) for r in con.execute(
            "SELECT source_type, COUNT(*) AS n FROM trade_records GROUP BY 1 ORDER BY n DESC")]
    finally:
        con.close()
    reading = (f"研究库 playbook_instructions {total} 行 (近30天 {recent}) | "
               f"trade_records {tr_total} 行 | 分臂 {[(a['source_type'], a['n']) for a in arms[:6]]}")
    rep.add("P8", "研究臂窗口老化 (翻转+30天炸弹)", "warn" if total else "block", reading,
            why=("单库翻转后研究库停止生长, compute_playbook_ic(days=30) 窗口在**翻转+30天**变空 "
                 "→ 门禁判『证据不足』→ 强制降级 sim → 降级记录经 get_trade_records_db() "
                 "(TRADE_ENV=0) 写进 live 库 = **无人知晓的 cutover 自毁 + 模拟行混入实盘库**。"
                 "方向 fail-safe 但静默。"),
            fix=("按决策包 §一 方案 A' 六步: ①证据快照归档 ②门禁改指 live + 试点期语义 "
                 "③**降级语义改 HALT (停开新仓)** ④三开关同翻 ⑤研究脚本各自诚实停臂 "
                 "⑥W3 零告警验证。A' 目前**一步都未实施**。"),
            evidence={"total": total, "recent_30d": recent, "arms": arms})


# ─────────────────────────────────────────────────────────────
# P9 台账连续性 / 研究脚本载体
# ─────────────────────────────────────────────────────────────
def p9_ledger_continuity(rep: Report) -> None:
    hardcoded = []
    for name in ("weekly_trade_summary.py", "exit_scorecard.py"):
        p = ROOT / "scripts" / name
        if not p.exists():
            continue
        txt = p.read_text(errors="replace")
        if "ftseek_us.db" in txt:
            hardcoded.append(name)
    live_arms = {}
    con = _open_lane("live", LIVE_DB)
    if con is not None:
        try:
            live_arms = {str(r[0]): int(r[1]) for r in con.execute(
                "SELECT source_type, COUNT(*) FROM trade_records GROUP BY 1")}
        finally:
            con.close()
    has_auto_arm = any(k.startswith("PLAYBOOK") for k in live_arms)
    reading = (f"live 库分臂 {live_arms} | auto 臂行数={'有' if has_auto_arm else '0 (从未跑过)'} | "
               f"硬编码 sim 载体的研究脚本 {hardcoded}")
    rep.add("P9", "台账连续性 / 研究脚本载体", "warn" if hardcoded else "ok", reading,
            why=("研究脚本硬编码 data/ftseek_us.db 是**刻意设计** (防『换库伪装成没交易』), "
                 "但翻转后 sim-broker 记录进 live 库 ⇒ 周报/scorecard 只见存量不见增量, "
                 "IC/OKR **静默断供**。exit_scorecard 的 trade_mode!='live' 过滤同理读空。"),
            fix=("A' 第5步: weekly_trade_summary 保持读 sim 库并**显式输出停臂说明**; "
                 "exit_scorecard 加 --arm live 读 live 库 PLAYBOOK_LIVE (sim 判分器冻结存档)"),
            evidence={"hardcoded": hardcoded, "live_arms": live_arms})


# ─────────────────────────────────────────────────────────────
def main() -> int:
    ap = argparse.ArgumentParser(description="sim⇄live 双向切换预检 (生产库只读)")
    ap.add_argument("--to", choices=("live", "sim", "both"), default="both",
                    help="检查方向 (默认双向)")
    ap.add_argument("--skip-gate", action="store_true", help="跳过毕业门禁实算 (慢)")
    ap.add_argument("--json-only", action="store_true", help="只输出 JSON 路径")
    ap.add_argument("--no-write", action="store_true", help="不落盘 JSON (纯控制台)")
    ap.add_argument("--accept-baseline", action="store_true",
                    help="冻结当前探针状态为棘轮基线 (人工动作; 脚本自身永不自动写基线)")
    ap.add_argument("--watch", action="store_true",
                    help="哨兵模式: 只对相对基线的状态跃迁告警 (常驻项静默)")
    ap.add_argument("--push", action="store_true", help="--watch 有跃迁时推飞书")
    args = ap.parse_args()

    rep = Report()
    started = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
    try:
        env = p0_env_triple(rep)
        p1_graduation_gate(rep, args.skip_gate)
        p2_schema_drift(rep)
        p3_capital_state(rep)
        p4_open_positions(rep)
        p5_sl_semantics(rep)
        p6_stop_mileage(rep)
        p7_contamination(rep)
        p8_research_arm(rep)
        p9_ledger_continuity(rep)
    except Exception as exc:
        import traceback
        print(f"⛔ 预检自身异常 (读数不可信, 退出码 2): {type(exc).__name__}: {exc}",
              file=sys.stderr)
        traceback.print_exc()
        return 2

    counts = rep.counts()
    doc = {"generated_at": started, "direction": args.to, "env": env,
           "worst": rep.worst, "blocked": rep.blocked, "counts": counts,
           "caliber": "快照非裁决 — 切换仍以 C2 红线 (周检达标报告 + 用户签字) 为准",
           "items": rep.items}

    if not args.no_write:
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        out = OUT_DIR / f"preflight_{stamp}.json"
        out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
        latest = OUT_DIR / "preflight_latest.json"
        latest.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
    else:
        out = None

    if not args.json_only:
        print(f"=== 切换预检 · {started} · 方向 {args.to} ===")
        for it in rep.items:
            print(f"{_ICON[it['status']]} {it['id']} {it['name']}")
            print(f"     读数: {it['reading']}")
            if it["why"]:
                print(f"     为何: {it['why']}")
            if it["fix"]:
                print(f"     处置: {it['fix']}")
        print(f"\n→ 汇总: block={counts['block']} warn={counts['warn']} "
              f"ok={counts['ok']} na={counts['na']} | worst={rep.worst}")
        if SKIPPED_SOURCES:
            print(f"→ ⚠️ 有 {len(SKIPPED_SOURCES)} 个数据源打不开而被跳过 "
                  f"(上述读数可能不完整, 不得当作'干净'): {SKIPPED_SOURCES}")
        print("→ 判读: BLOCK 项清零才谈切换; WARN 项需逐条裁决或预置。"
              "达标≠自动翻转 (C2 红线: 周检报告 + 用户签字)。")
        if out:
            print(f"→ {out}")
    elif out:
        print(out)

    if args.accept_baseline:
        keys = accept_baseline(rep)
        print(f"\n→ 已冻结棘轮基线: {BASELINE}")
        print(f"  {json.dumps(keys, ensure_ascii=False)}")
        print("  此后 --watch 只对状态跃迁告警; 重新冻结需再次人工 --accept-baseline。")
        return 0

    if args.watch:
        baseline = load_baseline()
        if not baseline:
            # fail-loud: 没有基线就静默 = 把"没在哨"伪装成"哨了没事"
            msg = ("[FTSeek-US·切换预检] ⛔ 棘轮基线未冻结 ⇒ 哨兵无法判读变化。"
                   "请人工执行: PYTHONPATH=. venv/bin/python scripts/cutover_preflight.py "
                   "--accept-baseline (基线冻结是人工动作, 脚本不自动写)")
            print(msg)
            print(f"→ 推送结果: {push_alert(msg, push=args.push)}")
            return 2
        d = diff_against_baseline(rep, baseline)
        changes = d["new"] + d["escalated"] + d["improved"] + d["gone"]
        if not changes:
            print(f"\n→ [watch] 与基线一致, 静默 (基线冻结于 "
                  f"{json.loads(BASELINE.read_text(encoding='utf-8')).get('frozen_at', '?')})")
            return 0
        lines = ["[FTSeek-US·切换预检] 状态跃迁 (相对棘轮基线):"]
        for x in d["escalated"]:
            lines.append(f"⛔ 升级 {x['id']}: {x['from']}→{x['to']} | {x['reading']}")
        for x in d["new"]:
            lines.append(f"🆕 新探针 {x['id']}: {x['status']} | {x['reading']}")
        for x in d["improved"]:
            lines.append(f"✅ 改善 {x['id']}: {x['from']}→{x['to']} | {x['reading']} "
                         f"(请确认是真修好, 不是探针瞎了)")
        for x in d["gone"]:
            lines.append(f"❓ 探针消失 {x['id']} (原 {x['was']}) — 改名/删除? 需复核基线")
        msg = "\n".join(lines)
        print("\n" + msg)
        print(f"→ 推送结果: {push_alert(msg, push=args.push)}")
        print("→ 注意: 基线**不会**自动更新; 确认为新常态后需人工 --accept-baseline 重冻。")
        return 1

    return 1 if rep.blocked else 0


if __name__ == "__main__":
    sys.exit(main())
