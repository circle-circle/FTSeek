#!/usr/bin/env python3
"""库级分离切换前的**只读**双侧影子核对 (2026-09-05, DEB-20260905 §13.16, record-only, 零写入).

背景 (用户裁「webdashboard 是我用来手动操作实盘, 和 SIM 模拟账号数据不要混乱了」):
  看板券商侧恒 REAL (`manual_order_service._is_live()` 硬编码 True), 而 DB 侧由 TRADE_ENV
  路由 ⇒ TRADE_ENV=1 时 `get_main_db()` = **sim 库**, 手动实盘单的账本写进了 sim 数据收集仓。
  §13.15 已落地行级收敛层 (`app/web_dashboard/env_scope.py`) 止血, 但 `open_positions`
  的 PRIMARY KEY 是 `symbol` ⇒ 同标的的实盘仓与 sim 收集仓**物理上无法共存于一表**,
  终局修法只能是**库文件级分离**: 看板走 `data/ftseek_us_live.db`, sim 主管线保持 sim 库。

  切换的硬前提是「切过去之后看板不能整条查询抛错」。live 库自 2026-07-19 零写入,
  schema 已落后 sim 库若干列 ⇒ 必须先量化缺口, 再谈切换。

本脚本做什么 (纯静态 + 只读 EXPLAIN, 不连券商、不改配置、不写任何库):
  1. AST 提取 `app/web_dashboard/**/*.py` 里的全部 SQL 字面量。三个提取坑已处理:
     · `ast.walk` 会**重复**访问 JoinedStr 的子 Constant ⇒ 产生半截片段假阳性, 故先把
       JoinedStr 的全部后代标记为 consumed 再跳过;
     · f-string 插值分**表达式位**与**语句位**两种 (`LIMIT {n}` vs `{date_filter}` 展开成
       `AND trade_date >= '...'` 或空串), 单一替换值必然误报 ⇒ 每个模板生成 3 个变体
       (插值→`1` / →`''` / →空), **任一变体编译通过即判通过**;
     · 文档串会被 SQL_VERB 误命中 ⇒ 要求字符串**以 SQL 动词开头**才纳入。
  2. 按引用表把语句分类: 主库语句 (表 ⊆ sim 主库表集) / 他库语句 (signal_snapshots.db、
     social_sentiment.db、rejections.db 等独立库文件, 不受主库路由影响 ⇒ 出范围)。
  3. **双侧** EXPLAIN: EXPLAIN 只编译不执行 (既不需真参数、也不可能改数据), 却能精确抓
     "no such table/column"。
     · **sim 侧 (= 看板今天的实际库)**: 这里失败的与 env 分离无关, 是**现役生产缺陷**
       (`_db_query_sync` 吞异常返回 `[]` ⇒ 表现为看板静默空数据, 只留一行 ERROR 日志);
     · **live 侧 (= 切换后的目标库)**: 这里失败而 sim 侧通过的, 才是**分离切换的缺口**。
  4. 把 live 侧缺口聚合成可直接执行的 `ALTER TABLE ... ADD COLUMN` DDL 清单 (只打印, 不执行)。
  5. 数据面对比: 看板主要消费的 5 张表在两库的行数/时间跨度/现役仓/来源分布, 让用户在
     切换前就看见「数据会变少到什么程度」, 而不是切完才发现。
  6. 前后双录安全指纹并**只钉可归因的一侧**: live 库无其它写方 ⇒ 它的内容指纹/文件大小
     一旦变化就是本脚本干的 ⇒ 退出码 2 + CRITICAL; sim 库正被主服务合法写入 (数据收集
     不停), 其漂移只作 `sim_drift` 上报, **不参与判据** —— 否则会把正常交易读成事故。
     连接一律 `mode=ro` (WAL 语义正确; `immutable=1` 会忽略 -wal, 对活跃 sim 库会读到
     滞后 schema)。代价: 对原本没有 -wal/-shm 的库, 只读连接会留下 0 字节 -wal 与 32KB
     -shm 空壳 —— 不含任何帧、不改数据, SQLite 在最后一个连接关闭时自行回收。

判据 (预注册):
  · 「可切换」= live 侧缺口 0 **且** 安全指纹前后一致;
  · sim 侧失败数**不参与**切换判据 (那是另一类缺陷, 单独修), 但必须逐条列清。
幂等: 只读 + 覆写同一份 json (纯函数; `generated_at` 单列以免污染 diff)。
挂载: 人工执行 (`PYTHONPATH=. venv/bin/python scripts/env_separation_shadow.py`), 不排程。
回滚: 无需回滚 —— 本脚本不产生任何副作用; 删除本文件与 json 即完全复原。
"""
from __future__ import annotations

import ast
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

ROOT = Path(__file__).resolve().parents[1]
DASH_ROOT = ROOT / "app" / "web_dashboard"
SIM_DB = ROOT / "data" / "ftseek_us.db"
LIVE_DB = ROOT / "data" / "ftseek_us_live.db"
OUT = ROOT / "data" / "env_separation_shadow.json"

# f-string 插值的三种替身: 表达式位要 `1`, 字符串位要 `''`, 语句位 (整段 WHERE 追加) 要空
INTERP_VARIANTS: Tuple[str, ...] = ("1", "''", "")

STATEMENT_HEAD = re.compile(
    r"^\s*\(?\s*(SELECT|INSERT|UPDATE|DELETE|CREATE|ALTER|WITH|PRAGMA|REPLACE|VALUES)\b",
    re.IGNORECASE,
)
TABLE_REF = re.compile(
    r"\b(?:FROM|JOIN|UPDATE|INTO|TABLE(?:\s+IF\s+NOT\s+EXISTS)?)\s+[`\"']?([a-zA-Z_][a-zA-Z0-9_]*)[`\"']?",
    re.IGNORECASE,
)
DDL_HEAD = re.compile(r"^\s*\(?\s*(CREATE|ALTER|PRAGMA|DROP)\b", re.IGNORECASE)
MISSING_COL = re.compile(r"no such column:\s*([a-zA-Z_][a-zA-Z0-9_.]*)", re.IGNORECASE)
MISSING_TBL = re.compile(r"no such table:\s*([a-zA-Z_][a-zA-Z0-9_.]*)", re.IGNORECASE)
# INSERT 的列缺口报法与 SELECT 不同: "table X has no column named Y"
MISSING_COL_INS = re.compile(r"table\s+\S+\s+has no column named\s*([a-zA-Z_][a-zA-Z0-9_]*)",
                             re.IGNORECASE)
# 悬空尾巴 = 该字面量是跨行拼接的**左半截** (右半截是 `.join(...)` 之类的方法调用,
# 静态拿不到) ⇒ 片段, 不是缺陷
DANGLING_TAIL = re.compile(
    r"(\b(?:WHERE|AND|OR|SET|SELECT|FROM|JOIN|ON|BY|IN|VALUES|HAVING|LIMIT|NOT|AS)\b|[,(])\s*$",
    re.IGNORECASE)

DATA_PROBE_TABLES = ("open_positions", "trade_records", "capital_state",
                     "manual_order_audit", "trade_attribution")

SQL_KEYWORDS = {
    "select", "from", "where", "and", "or", "not", "in", "is", "null", "as", "on",
    "join", "left", "right", "inner", "outer", "cross", "group", "by", "order",
    "limit", "offset", "having", "union", "all", "distinct", "case", "when", "then",
    "else", "end", "asc", "desc", "like", "between", "exists", "values", "into",
    "insert", "update", "set", "delete", "create", "table", "if", "index", "drop",
    "alter", "add", "column", "coalesce", "cast", "count", "sum", "avg", "min",
    "max", "abs", "round", "upper", "lower", "trim", "substr", "replace", "date",
    "time", "datetime", "strftime", "json_extract", "ifnull", "nullif", "typeof",
    "length", "rowid", "with", "recursive", "over", "partition", "row_number",
    "rank", "pragma", "explain", "query", "plan", "temp", "temporary", "view",
    "trigger", "default", "primary", "key", "unique", "check", "references",
    "constraint", "integer", "text", "real", "blob", "numeric", "boolean",
    "autoincrement", "true", "false", "collate", "escape", "glob", "isnull",
    "notnull", "using", "natural", "except", "intersect", "cast", "filter",
}


# ══════════════════════════════════════════════════════════════════
#  安全指纹 (前后双录, 证明本脚本零副作用)
# ══════════════════════════════════════════════════════════════════
def _file_state(path: Path) -> Dict[str, object]:
    st = path.stat()
    return {"size": st.st_size, "mtime_ns": st.st_mtime_ns}


def _table_md5(db_path: Path, table: str) -> str:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = [list(r) for r in conn.execute(f'SELECT * FROM "{table}" ORDER BY 1')]
    finally:
        conn.close()
    payload = json.dumps(rows, default=str, sort_keys=True)
    return hashlib.md5(payload.encode("utf-8")).hexdigest()


def safety_snapshot() -> Dict[str, object]:
    return {
        "sim_db": _file_state(SIM_DB),
        "live_db": _file_state(LIVE_DB),
        "sim_open_positions_md5": _table_md5(SIM_DB, "open_positions"),
        "live_open_positions_md5": _table_md5(LIVE_DB, "open_positions"),
    }


def evaluate_safety(before: Dict[str, object], after: Dict[str, object]) -> Dict[str, object]:
    """篡改判据只钉**可归因于本脚本**的一侧.

    · live 库无其它写方 (自 2026-07-19 死库, `lsof` 实测零持有者) ⇒ 运行期间它的
      内容指纹或文件大小若变化, 只可能是本脚本干的 ⇒ `tampered=True`, 退出码 2;
    · sim 库正被主服务合法写入 (数据收集不停) ⇒ 它的内容漂移**不是**本脚本的副作用,
      只作 `sim_drift` 如实上报, 不参与判据, 否则会把正常交易读成事故。
    """
    live_changed = (before["live_open_positions_md5"] != after["live_open_positions_md5"]
                    or before["live_db"] != after["live_db"])
    return {
        "tampered": live_changed,
        "sim_drift": before["sim_open_positions_md5"] != after["sim_open_positions_md5"],
        "sim_db_file_delta": before["sim_db"] != after["sim_db"],
    }


# ══════════════════════════════════════════════════════════════════
#  AST 提取 SQL 字面量 (含三个假阳性坑的处理)
# ══════════════════════════════════════════════════════════════════
def _mark_joined_descendants(tree: ast.AST) -> Set[int]:
    consumed: Set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            for child in ast.walk(node):
                if child is not node:
                    consumed.add(id(child))
    return consumed


def _variants(node: ast.AST) -> List[str]:
    """返回该字符串节点的全部候选 SQL 文本 (普通常量 1 个, f-string 3 个变体)."""
    if isinstance(node, ast.JoinedStr):
        constants: List[str] = []
        slots: List[int] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                constants.append(value.value)
            else:
                constants.append("\x00")
                slots.append(len(constants) - 1)
        if not slots:
            return ["".join(constants)]
        out: List[str] = []
        for fill in INTERP_VARIANTS:
            parts = list(constants)
            for idx in slots:
                parts[idx] = fill
            out.append("".join(parts))
        return out
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left_variants = _variants(node.left)
        right_variants = _variants(node.right)
        if left_variants and right_variants:
            return [lv + rv for lv in left_variants for rv in right_variants]
    return []


def extract_sql_literals() -> List[Dict[str, object]]:
    found: List[Dict[str, object]] = []
    for path in sorted(DASH_ROOT.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError as exc:
            print(f"  ⚠️ 解析失败 {path}: {exc}", file=sys.stderr)
            continue
        consumed = _mark_joined_descendants(tree)
        for node in ast.walk(tree):
            if id(node) in consumed:
                continue
            variants = [v.strip() for v in _variants(node) if v and v.strip()]
            variants = [v for v in variants if STATEMENT_HEAD.match(v)]
            if not variants:
                continue
            found.append({
                "variants": variants,
                "file": str(path.relative_to(ROOT)),
                "line": getattr(node, "lineno", 0),
            })
    return found


def split_statements(sql: str) -> List[str]:
    return [chunk.strip() for chunk in sql.split(";") if chunk.strip()]


def referenced_tables(sql: str) -> Set[str]:
    tables = set()
    for match in TABLE_REF.finditer(sql):
        name = match.group(1).lower()
        if name not in SQL_KEYWORDS:
            tables.add(name)
    return tables


def read_schema(db_path: Path) -> Dict[str, List[str]]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        names = [r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        schema: Dict[str, List[str]] = {}
        for name in names:
            schema[name] = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        return schema
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
#  EXPLAIN 影子编译 (只编译, 不执行, 不改数据)
# ══════════════════════════════════════════════════════════════════
def _explain_one(conn: sqlite3.Connection, sql: str) -> Optional[str]:
    try:
        bindings = (1,) * sql.count("?")
        conn.execute(f"EXPLAIN {sql}", bindings)
        return None
    except sqlite3.Error as exc:
        return str(exc)


def explain_probe(db_path: Path, variants: Sequence[str]) -> Tuple[bool, List[str]]:
    """任一变体编译通过即通过; 全败时返回全部错误文本供分类."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    errors: List[str] = []
    try:
        for sql in variants:
            for statement in split_statements(sql):
                err = _explain_one(conn, statement)
                if err is not None:
                    errors.append(err)
            if not errors:
                return True, []
    finally:
        conn.close()
    return False, errors


def classify(errors: Sequence[str], is_ddl: bool = False, is_template: bool = False) -> str:
    """把 EXPLAIN 失败分成「真缺口」与「探测器自身局限」两类, 避免假阳性冒充缺陷.

    · SCHEMA_GAP            —— no such table/column, 真缺口 (切换阻塞项 / 现役缺陷);
    · IDEMPOTENT_DDL_OK     —— `ALTER TABLE ADD COLUMN` 撞已存在列 (duplicate column name)。
                               代码用 `except sqlite3.OperationalError: pass` **显式容忍**,
                               这是设计意图, 不是缺陷;
    · UNVERIFIABLE_TEMPLATE —— f-string 的插值落在**标识符位** (`ADD COLUMN {col_name} {col_type}`)
                               或**语句位** (`SET {', '.join(sets)}`), 任何替身都编译不过。
                               静态不可判定 ⇒ 交给 token 兜底扫描, 不计入缺陷;
    · BINDING               —— 占位符数量对不上 (探测器局限), 不计入缺陷。
    """
    joined = " | ".join(errors)
    if MISSING_COL.search(joined) or MISSING_TBL.search(joined) or MISSING_COL_INS.search(joined):
        if is_ddl and "duplicate column name" in joined:
            return "IDEMPOTENT_DDL_OK"
        return "SCHEMA_GAP"
    if is_ddl and "duplicate column name" in joined:
        return "IDEMPOTENT_DDL_OK"
    if is_ddl or is_template:
        return "UNVERIFIABLE_TEMPLATE"
    if "bindings" in joined:
        return "BINDING"
    if "incomplete input" in joined:
        return "FRAGMENT"
    return "TEMPLATE_ARTIFACT"


def column_gap_audit(main_db: Sequence[Dict[str, object]],
                     sim_schema: Dict[str, List[str]],
                     live_schema: Dict[str, List[str]]) -> Dict[str, Dict[str, List[str]]]:
    """看板触碰的每张表: live 库缺哪些列, 并按「看板 SQL 是否真引用」分阻塞/休眠.

    阻塞列 = 缺 **且** 出现在看板 SQL 的 token 里 ⇒ 切换后该语句直接抛错;
    休眠列 = 缺但看板不引用 ⇒ 两库 schema 的历史分叉, 切换不受影响, 只登记不补
             (补它会掩盖真实缺口, 也违反最小改动)。
    """
    touched: Dict[str, Set[str]] = {}
    for record in main_db:
        tokens = {t.lower() for t in re.findall(
            r"[A-Za-z_][A-Za-z0-9_]*", " ".join(str(v) for v in record["variants"]))}  # type: ignore[index]
        tokens -= SQL_KEYWORDS
        for table in record["tables"]:  # type: ignore[index]
            touched.setdefault(str(table), set()).update(tokens)
    audit: Dict[str, Dict[str, List[str]]] = {}
    for table in sorted(touched):
        if table not in sim_schema or table not in live_schema:
            continue
        gap = [c for c in sim_schema[table] if c not in live_schema[table]]
        if not gap:
            continue
        tokens = touched[table]
        audit[table] = {
            "blocking": sorted(c for c in gap if c in tokens),
            "dormant": sorted(c for c in gap if c not in tokens),
        }
    return audit


def token_column_scan(sql: str, tables: Iterable[str],
                      sim_schema: Dict[str, List[str]],
                      live_schema: Dict[str, List[str]]) -> Set[str]:
    """EXPLAIN 跑不动的模板语句的兜底: 找「sim 有、live 没有」且出现在语句里的列名."""
    tokens = {t.lower() for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", sql)}
    tokens -= SQL_KEYWORDS
    hits: Set[str] = set()
    for table in tables:
        sim_cols = set(sim_schema.get(table, []))
        live_cols = set(live_schema.get(table, []))
        for col in (sim_cols - live_cols) & tokens:
            hits.add(f"{table}.{col}")
    return hits


# ══════════════════════════════════════════════════════════════════
#  数据面对比
# ══════════════════════════════════════════════════════════════════
def _probe_table(db_path: Path, table: str) -> Dict[str, object]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    out: Dict[str, object] = {}
    try:
        out["rows"] = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        out["cols"] = len(cols)
        if table == "open_positions":
            if "size" in cols:
                out["active_rows(size>0)"] = conn.execute(
                    f'SELECT COUNT(*) FROM "{table}" WHERE COALESCE(size,0) > 0').fetchone()[0]
            if "updated_at" in cols:
                row = conn.execute(
                    f'SELECT MIN(updated_at), MAX(updated_at) FROM "{table}"').fetchone()
                out["updated_at_span"] = [row[0], row[1]]
            if "account_type" in cols:
                out["account_type_hist"] = {
                    (r[0] or "<NULL>"): r[1] for r in conn.execute(
                        f'SELECT account_type, COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 2 DESC')}
            else:
                out["account_type_hist"] = "<列不存在>"
        if table == "trade_records":
            if "trade_date" in cols:
                row = conn.execute(
                    f'SELECT MIN(trade_date), MAX(trade_date) FROM "{table}"').fetchone()
                out["date_span"] = [row[0], row[1]]
            if "realized_pnl" in cols:
                out["realized_pnl_sum"] = conn.execute(
                    f'SELECT ROUND(SUM(COALESCE(realized_pnl,0)),2) FROM "{table}"').fetchone()[0]
            if "source_type" in cols:
                out["source_type_hist"] = {
                    (r[0] or "<NULL>"): r[1] for r in conn.execute(
                        f'SELECT source_type, COUNT(*) FROM "{table}" GROUP BY 1 ORDER BY 2 DESC')}
    finally:
        conn.close()
    return out


def data_probe() -> Dict[str, Dict[str, object]]:
    probe: Dict[str, Dict[str, object]] = {}
    for table in DATA_PROBE_TABLES:
        entry: Dict[str, object] = {}
        for label, db_path in (("sim", SIM_DB), ("live", LIVE_DB)):
            try:
                entry[label] = _probe_table(db_path, table)
            except sqlite3.Error as exc:
                entry[label] = {"error": str(exc)}
        probe[table] = entry
    return probe


# ══════════════════════════════════════════════════════════════════
#  主流程
# ══════════════════════════════════════════════════════════════════
def main() -> int:
    before = safety_snapshot()
    sim_schema = read_schema(SIM_DB)
    live_schema = read_schema(LIVE_DB)
    sim_tables = set(sim_schema)

    literals = extract_sql_literals()

    main_db: List[Dict[str, object]] = []
    other_db: List[Dict[str, object]] = []
    for item in literals:
        tables: Set[str] = set()
        for variant in item["variants"]:  # type: ignore[index]
            for statement in split_statements(variant):
                tables |= referenced_tables(statement)
        if not tables:
            continue
        head = split_statements(str(item["variants"][0]))[0]  # type: ignore[index]
        record: Dict[str, object] = {
            "file": item["file"],
            "line": item["line"],
            "tables": sorted(tables),
            "sql_head": head[:120],
            "is_ddl": bool(DDL_HEAD.match(head)),
            "is_template": len(item["variants"]) > 1,  # type: ignore[index]
            "variants": item["variants"],
        }
        if tables <= sim_tables:
            main_db.append(record)
        else:
            record["out_of_scope_tables"] = sorted(tables - sim_tables)
            other_db.append(record)

    sim_fail: List[Dict[str, object]] = []
    live_fail: List[Dict[str, object]] = []
    for record in main_db:
        variants = [str(v) for v in record["variants"]]  # type: ignore[index]
        sim_ok, sim_errs = explain_probe(SIM_DB, variants)
        live_ok, live_errs = explain_probe(LIVE_DB, variants)
        is_ddl = bool(record["is_ddl"])
        is_template = bool(record["is_template"])
        is_fragment = any(DANGLING_TAIL.search(v.rstrip().rstrip(";").rstrip())
                          for v in variants)
        if not sim_ok:
            entry = dict(record)
            entry.pop("variants", None)
            entry["errors"] = sim_errs
            entry["class"] = ("FRAGMENT_CONCAT" if is_fragment
                              else classify(sim_errs, is_ddl, is_template))
            sim_fail.append(entry)
        if not live_ok:
            entry = dict(record)
            entry.pop("variants", None)
            entry["errors"] = live_errs
            entry["class"] = ("FRAGMENT_CONCAT" if is_fragment and not (
                                  MISSING_COL.search(" | ".join(live_errs))
                                  or MISSING_COL_INS.search(" | ".join(live_errs))
                                  or MISSING_TBL.search(" | ".join(live_errs)))
                              else classify(live_errs, is_ddl, is_template))
            entry["sim_also_fails"] = not sim_ok
            entry["token_scan_gaps"] = sorted(token_column_scan(
                " ".join(variants), [str(t) for t in record["tables"]],  # type: ignore[index]
                sim_schema, live_schema))
            live_fail.append(entry)

    missing_cols: Dict[str, Set[str]] = {}
    missing_tbls: Set[str] = set()
    for entry in live_fail:
        if entry.get("sim_also_fails"):
            continue
        for err in entry["errors"]:  # type: ignore[index]
            for col in MISSING_COL.findall(str(err)) + MISSING_COL_INS.findall(str(err)):
                col = col.split(".")[-1]
                for table in entry["tables"]:  # type: ignore[index]
                    if table in live_schema and col not in live_schema[table]:
                        missing_cols.setdefault(str(table), set()).add(col)
            for table in MISSING_TBL.findall(str(err)):
                if table.lower() not in live_schema:
                    missing_tbls.add(table.lower())
        for gap in entry.get("token_scan_gaps", []):  # type: ignore[union-attr]
            table, col = str(gap).split(".", 1)
            missing_cols.setdefault(table, set()).add(col)

    ddl: List[str] = []
    for table in sorted(missing_cols):
        sim_types = {c: "TEXT" for c in missing_cols[table]}
        for col in sorted(sim_types):
            ddl.append(f'ALTER TABLE "{table}" ADD COLUMN "{col}" {sim_types[col]};')
    for table in sorted(missing_tbls):
        conn = sqlite3.connect(f"file:{SIM_DB}?mode=ro", uri=True)
        try:
            row = conn.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        finally:
            conn.close()
        ddl.append(row[0] + ";" if row and row[0]
                   else f"-- ⚠️ sim 库也查不到 {table} 的建表语句, 需人工判定")

    after = safety_snapshot()
    safety = evaluate_safety(before, after)
    tampered = bool(safety["tampered"])

    # 只统计**真正阻塞切换**的一类: live 侧失败而 sim 侧通过 (SCHEMA_GAP)
    blocking = [e for e in live_fail
                if not e.get("sim_also_fails") and e.get("class") == "SCHEMA_GAP"]
    sim_defects = [e for e in sim_fail if e.get("class") == "SCHEMA_GAP"]
    unverifiable = [e for e in live_fail if e.get("class") == "UNVERIFIABLE_TEMPLATE"]
    verdict = "READY_TO_SWITCH" if (not blocking and not tampered) else "NOT_READY"

    report = {
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "verdict": verdict,
        "scope": {
            "dashboard_sql_literals": len(literals),
            "main_db_statements": len(main_db),
            "other_db_statements_out_of_scope": len(other_db),
            "other_db_tables": sorted({t for r in other_db
                                       for t in r["out_of_scope_tables"]}),  # type: ignore[index]
        },
        "live_side": {
            "failures": live_fail,
            "blocking_schema_gaps": blocking,
            "missing_columns": {t: sorted(c) for t, c in sorted(missing_cols.items())},
            "missing_tables": sorted(missing_tbls),
            "required_ddl": ddl,
        },
        "sim_side": {
            "explain_failures": sim_fail,
            "production_defects": sim_defects,
            "unverifiable_templates": unverifiable,
        },
        "column_gap_audit": column_gap_audit(main_db, sim_schema, live_schema),
        "table_set_diff": {
            "sim_only": sorted(sim_tables - set(live_schema)),
            "live_only": sorted(set(live_schema) - sim_tables),
        },
        "data_probe": data_probe(),
        "safety": {"before": before, "after": after, **safety},
    }

    OUT.write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str), encoding="utf-8")

    print("=" * 78)
    print("库级分离影子核对 (只读·双侧) — 看板 DB 从 sim 库切到 live 库的前置体检")
    print("=" * 78)
    print(f"SQL 字面量 {len(literals)} 条 | 主库语句 {len(main_db)} 条 | "
          f"他库语句(出范围) {len(other_db)} 条")
    print(f"他库表: {report['scope']['other_db_tables']}")

    print(f"\n--- sim 侧 (看板今天的实际库) EXPLAIN 失败 {len(sim_fail)} 条, "
          f"其中真缺陷 {len(sim_defects)} 条 ---")
    for entry in sim_fail:
        print(f"  [{entry['class']}] {entry['file']}:{entry['line']} "
              f"tables={entry['tables']} :: {entry['errors'][0][:110]}")

    print(f"\n--- live 侧失败 {len(live_fail)} 条 | 阻塞切换 {len(blocking)} 条 | "
          f"静态不可判定模板 {len(unverifiable)} 条 ---")
    for entry in live_fail:
        if entry in blocking:
            tag = "BLOCKING"
        elif entry.get("sim_also_fails"):
            tag = "sim也败"
        else:
            tag = str(entry["class"])
        print(f"  [{tag}] {entry['file']}:{entry['line']} :: {entry['errors'][0][:110]}")
        if entry.get("token_scan_gaps"):
            print(f"      token兜底缺口: {entry['token_scan_gaps']}")

    if missing_cols or missing_tbls:
        print("\n--- live 库 schema 缺口 (切换前必须补) ---")
        for table, cols in sorted(missing_cols.items()):
            print(f"  {table}: 缺列 {sorted(cols)}")
        for table in sorted(missing_tbls):
            print(f"  缺表: {table}")
        print("\n--- 可直接执行的 DDL (本脚本不执行) ---")
        for line in ddl:
            print(f"  {line}")

    print("\n--- 全列差审计 (看板触碰的表 · live 库缺列) ---")
    gap_audit = report["column_gap_audit"]
    if not gap_audit:
        print("  ✅ 无缺列 — 看板触碰的表在 live 库列集是 sim 库的超集/等集")
    for table, entry in gap_audit.items():
        print(f"  {table}:")
        print(f"    阻塞列 ({len(entry['blocking'])}): {entry['blocking'] or '无'}")
        print(f"    休眠列 ({len(entry['dormant'])}): {entry['dormant'] or '无'}")

    print("\n--- 数据面对比 (切换后看板会看到什么) ---")
    for table, entry in report["data_probe"].items():
        sim_side = entry.get("sim", {})
        live_side = entry.get("live", {})
        print(f"  {table:20} rows sim={sim_side.get('rows', '?'):>5} live={live_side.get('rows', '?'):>5}"
              f" | cols sim={sim_side.get('cols', '?')} live={live_side.get('cols', '?')}")
        for key in ("active_rows(size>0)", "date_span", "updated_at_span",
                    "realized_pnl_sum", "account_type_hist", "source_type_hist"):
            if key in sim_side or key in live_side:
                print(f"  {'':20} {key}: sim={sim_side.get(key, '<无>')} live={live_side.get(key, '<无>')}")

    print("\n--- 安全指纹 (本脚本必须零副作用) ---")
    print(f"  sim  open_positions md5: {before['sim_open_positions_md5']} → "
          f"{after['sim_open_positions_md5']}")
    print(f"  live open_positions md5: {before['live_open_positions_md5']} → "
          f"{after['live_open_positions_md5']}")
    print(f"  tampered (只钉 live 侧, 本脚本唯一可归因的库): {tampered}")
    print(f"  sim_drift (主服务合法写入, 不计入判据): {safety['sim_drift']} | "
          f"sim 库文件态变化: {safety['sim_db_file_delta']}")
    print(f"\n判定: {verdict}   报告: {OUT}")
    if tampered:
        print("🚨 CRITICAL: 本只读脚本改动了库文件 — 立即停止切换并排查", file=sys.stderr)
        return 2
    return 0 if verdict == "READY_TO_SWITCH" else 1


if __name__ == "__main__":
    sys.exit(main())
