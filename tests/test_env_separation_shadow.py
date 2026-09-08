"""库级分离影子核对 + diagnostic_results 列名笔误 的契约测试 (DEB-20260905 §13.17).

红绿纪律 (每条都做过双向证伪, 见 DEB §13.17.5):
  · 探测器必须**能**抓到缺列 (绿), 且补列后**必须**放行 (红→绿翻转), 否则
    READY_TO_SWITCH 这个判定没有任何证伪力;
  · 分类器必须把「幂等 DDL 撞已存在列」「f-string 标识符位插值」判成**探测器局限**,
    而不是缺陷 —— 否则假阳性会把真缺口淹掉 (本批第一版就淹了: 93 条失败里只有 3 列真缺);
  · live 库补齐的两列用**生产库实测**钉住, 防止恢复旧备份后静默退回缺口态;
  · `get_diagnostic_data` 的列名笔误用「真 DDL + 真 SQL 字面量」在内存库上跑通,
    而不是只断言源码字符串 —— 后者对"改了字符串但仍不对"零证伪力。
"""
import re
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import scripts.env_separation_shadow as ess  # noqa: E402

DATA_SERVICE = ROOT / "app" / "web_dashboard" / "data_service.py"
DIAG_BASE = ROOT / "app" / "diagnostics" / "base.py"


# ══════════════════════════════════════════════════════════════════
#  1. EXPLAIN 探测器: 双向证伪 (缺列必抓 / 补列必放)
# ══════════════════════════════════════════════════════════════════
def _mkdb(path: Path, with_col: bool) -> None:
    conn = sqlite3.connect(str(path))
    try:
        ddl = "CREATE TABLE open_positions (symbol TEXT PRIMARY KEY, size REAL)"
        if with_col:
            ddl += ""
        conn.execute(ddl)
        if with_col:
            conn.execute("ALTER TABLE open_positions ADD COLUMN account_type TEXT DEFAULT 'real'")
        conn.execute("INSERT INTO open_positions (symbol, size) VALUES ('US.TEST', 10)")
        conn.commit()
    finally:
        conn.close()


def test_explain_probe_flags_missing_column(tmp_path):
    db = tmp_path / "gap.db"
    _mkdb(db, with_col=False)
    ok, errors = ess.explain_probe(
        db, ["SELECT symbol, account_type FROM open_positions WHERE size > 0"])
    assert ok is False, "缺 account_type 列却判通过 ⇒ 探测器无证伪力"
    assert any("no such column: account_type" in e for e in errors), errors


def test_explain_probe_passes_after_column_added(tmp_path):
    db = tmp_path / "fixed.db"
    _mkdb(db, with_col=True)
    ok, errors = ess.explain_probe(
        db, ["SELECT symbol, account_type FROM open_positions WHERE size > 0"])
    assert ok is True, f"补列后仍判失败 ⇒ 缺口永远补不平: {errors}"
    assert errors == []


def test_explain_probe_binds_placeholders(tmp_path):
    """占位符数量必须自动补齐, 否则 `LIMIT ?` 类语句全部假阳性 (第一版踩过)."""
    db = tmp_path / "ph.db"
    _mkdb(db, with_col=True)
    ok, errors = ess.explain_probe(
        db, ["SELECT symbol FROM open_positions WHERE symbol = ? ORDER BY rowid DESC LIMIT ?"])
    assert ok is True, f"占位符未绑定导致假阳性: {errors}"


def test_explain_probe_catches_insert_column_gap(tmp_path):
    """INSERT 的缺口报法与 SELECT 不同 ("has no column named"), 必须同样抓到."""
    db = tmp_path / "ins.db"
    _mkdb(db, with_col=False)
    ok, errors = ess.explain_probe(
        db, ["INSERT INTO open_positions (symbol, size, account_type) VALUES (?, 0, 'real')"])
    assert ok is False
    assert ess.classify(errors) == "SCHEMA_GAP", errors
    assert ess.MISSING_COL_INS.findall(" | ".join(errors)) == ["account_type"]


# ══════════════════════════════════════════════════════════════════
#  2. 分类器: 真缺口 vs 探测器局限
# ══════════════════════════════════════════════════════════════════
def test_classify_idempotent_ddl_is_not_a_defect():
    err = "duplicate column name: time_in_force"
    assert ess.classify([err], is_ddl=True) == "IDEMPOTENT_DDL_OK"
    # 同样的文本若出现在非 DDL 语句里, 不得被洗成 OK
    assert ess.classify([err], is_ddl=False) != "IDEMPOTENT_DDL_OK"


def test_classify_template_artifact_is_unverifiable_not_defect():
    err = 'near "1": syntax error'
    assert ess.classify([err], is_ddl=True, is_template=False) == "UNVERIFIABLE_TEMPLATE"
    assert ess.classify([err], is_ddl=False, is_template=True) == "UNVERIFIABLE_TEMPLATE"


def test_classify_real_schema_gap_wins_over_template_excuse():
    """缺列错误即使来自模板语句, 也必须判 SCHEMA_GAP —— 否则真缺口会被模板借口吃掉."""
    assert ess.classify(["no such column: pnl_net"], is_ddl=False,
                        is_template=True) == "SCHEMA_GAP"


# ══════════════════════════════════════════════════════════════════
#  3. SQL 提取: 三个假阳性坑各自的钉子
# ══════════════════════════════════════════════════════════════════
def test_variants_cover_expression_and_statement_interpolation():
    """`{date_filter}` 展开成整段 WHERE 追加或空串 ⇒ 单一替身必然误报, 需 3 变体."""
    import ast
    node = ast.parse(
        "f\"SELECT a FROM t WHERE b > 0 {date_filter}\"").body[0].value
    variants = ess._variants(node)
    assert len(variants) == len(ess.INTERP_VARIANTS) == 3
    assert any(v.rstrip().endswith("WHERE b > 0 1") for v in variants)
    assert any(v.rstrip().endswith("WHERE b > 0") for v in variants), \
        "缺空串变体 ⇒ 语句位插值 (整段 WHERE 追加) 必假阳性"
    assert any("WHERE b > 0 ''" in v for v in variants), "缺字符串位变体"


def test_joinedstr_children_are_not_extracted_as_fragments():
    """`ast.walk` 会重复访问 JoinedStr 的子 Constant ⇒ 半截片段假阳性 (第一版踩过)."""
    import ast
    src = 'x = f"SELECT a FROM t WHERE {col} = 1"\n'
    tree = ast.parse(src)
    consumed = ess._mark_joined_descendants(tree)
    pieces = []
    for node in ast.walk(tree):
        if id(node) in consumed:
            continue
        pieces.extend(ess._variants(node))
    pieces = [x for x in pieces if x.strip()]
    # JoinedStr 本身产出 3 个变体 (设计如此); 关键是**子 Constant 不得再被单独提取**,
    # 否则会出现 "SELECT a FROM t WHERE " 这种半截片段并被 EXPLAIN 判成 incomplete input。
    assert len(pieces) == len(ess.INTERP_VARIANTS), pieces
    assert all(x.strip().startswith("SELECT a FROM t WHERE") and "=" in x for x in pieces), pieces
    assert not any(x.rstrip().endswith("WHERE") for x in pieces), \
        f"出现了跨行拼接的半截片段: {pieces}"


def test_dangling_tail_marks_concat_fragment():
    """`"... WHERE " + " AND ".join(where)` 的左半截必须以片段论, 不得计为缺陷."""
    assert ess.DANGLING_TAIL.search("SELECT a FROM t WHERE")
    assert ess.DANGLING_TAIL.search("SELECT a FROM t ORDER BY")
    assert ess.DANGLING_TAIL.search("INSERT INTO t (a, b,")
    assert not ess.DANGLING_TAIL.search("SELECT a FROM t WHERE b = 1")


def test_docstring_is_not_mistaken_for_sql():
    """SQL 动词出现在中文文档串里不得纳入 (第一版把 data_service 文档串当语句)."""
    assert not ess.STATEMENT_HEAD.match("判断 SQLite updated_at 是否早于 SELECT 阈值")
    assert ess.STATEMENT_HEAD.match("SELECT a FROM t")


# ══════════════════════════════════════════════════════════════════
#  4. 脚本自身必须是只读的
# ══════════════════════════════════════════════════════════════════
SCRIPT = ROOT / "scripts" / "env_separation_shadow.py"


def _script_body() -> str:
    """去掉模块文档串后的脚本主体 (文档串里合法地提到 ALTER/immutable 作为说明)."""
    src = SCRIPT.read_text(encoding="utf-8")
    import ast
    tree = ast.parse(src)
    doc = ast.get_docstring(tree, clean=False)
    return src.replace(doc, "", 1) if doc else src


def test_shadow_script_never_opens_a_writable_connection():
    body = _script_body()
    connects = re.findall(r"sqlite3\.connect\(([^\n]*)\)", body)
    assert connects, "探测器不再连库了? 提取正则失效"
    for call in connects:
        assert "mode=ro" in call, f"存在可写连接: sqlite3.connect({call})"
    assert "commit(" not in body, "只读脚本不得提交事务"
    # 执行的语句只允许 EXPLAIN / SELECT / PRAGMA (脚本会**生成** DDL 文本供人工执行,
    # 但绝不能自己执行 —— 故检查 execute() 的实参, 而不是全文关键词)
    pattern = r"""\.execute\(\s*f?['"]([^'"]{0,40})"""
    for stmt in re.findall(pattern, body):
        head = stmt.strip().split()[0].upper() if stmt.strip() else ""
        assert head in ("EXPLAIN", "SELECT", "PRAGMA", ""), f"执行了非只读语句: {stmt}"


def test_shadow_script_uses_wal_correct_readonly_mode():
    """`immutable=1` 会忽略 -wal; sim 库正被主服务写入 ⇒ 用它会读到滞后 schema."""
    body = _script_body()
    for call in re.findall(r"sqlite3\.connect\(([^\n]*)\)", body):
        assert "immutable=1" not in call, \
            f"immutable=1 对活跃 WAL 库会读到滞后 schema: {call}"


# ══════════════════════════════════════════════════════════════════
#  5. 生产库状态钉桩: live 库阻塞缺口已补齐
# ══════════════════════════════════════════════════════════════════
def _cols(db: Path, table: str):
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    try:
        return [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
    finally:
        conn.close()


@pytest.mark.skipif(not ess.LIVE_DB.exists(), reason="live 库不存在 (非本机环境)")
def test_live_db_blocking_columns_are_present():
    """影子核对判定的 2 个阻塞缺口必须已补; 恢复旧备份会让本例转红 (这是意图)."""
    assert "account_type" in _cols(ess.LIVE_DB, "open_positions"), \
        "live 库 open_positions 缺 account_type ⇒ 切换后看板 3 处写/读点全抛错"
    assert "pnl_net" in _cols(ess.LIVE_DB, "trade_records"), \
        "live 库 trade_records 缺 pnl_net ⇒ dashboard.py:191/:830 抛错"


@pytest.mark.skipif(not (ess.LIVE_DB.exists() and ess.SIM_DB.exists()),
                    reason="两库需同时存在")
def test_live_db_account_type_does_not_mislabel_real_rows_as_sim():
    """补列时若照抄 sim 的 DEFAULT 'sim', 8 行 owner='manual' 的实盘墓碑会被反向污染."""
    conn = sqlite3.connect(f"file:{ess.LIVE_DB}?mode=ro", uri=True)
    try:
        bad = conn.execute(
            "SELECT COUNT(*) FROM open_positions "
            "WHERE LOWER(COALESCE(account_type,'')) IN ('sim','simulate')").fetchone()[0]
        manual = conn.execute(
            "SELECT COUNT(*) FROM open_positions WHERE owner='manual'").fetchone()[0]
    finally:
        conn.close()
    assert bad == 0, f"live 库有 {bad} 行被标成 sim (实盘墓碑反向污染)"
    assert manual > 0, "live 库应仍有 owner='manual' 的实盘行 (口径未变)"


@pytest.mark.skipif(not ess.LIVE_DB.exists(), reason="live 库不存在")
def test_live_db_legacy_pnl_rows_still_fall_back_to_realized_pnl():
    """新增 pnl_net 必须留 NULL, 让 COALESCE(pnl_net, realized_pnl) 正确回落.

    RFC-078 (2026-09-08) 收紧而非放宽: 一次性迁移器
    `scripts/migrate_live_records_to_live_db.py` 把 3 行**误落在 sim 库的实盘行**
    (US.TTE 2026-07-09 / US.PDD 2026-07-10 / US.BULL 2026-08-20) 逐字搬进 live 库,
    其中 2 行的 pnl_net 是 sim 侧当期费用净额管线算出的**真值** ⇒ 它们带着非 NULL 的
    pnl_net 落进了"历史窗口"。这不是改写历史数值, 而是**插入**一行本就带值的行。
    故本例不再用"日期 < 2026-09-01 ⇒ pnl_net 必须 NULL"这个会被合法迁移证伪的代理判据,
    改成要求例外行**自证**: 必须在 sim 库找到同 symbol + 同 entry_time 且
    realized_pnl / pnl_net 完全相同、trade_mode='live' 的源行 (= 逐字搬运的证据)。
    找不到 ⇒ 有人真改写了历史数值 ⇒ 转红 (证伪力比原判据更强: 原判据只挡"写非 NULL",
    新判据还要求非 NULL 的值与源库逐字一致)。
    """
    conn = sqlite3.connect(f"file:{ess.LIVE_DB}?mode=ro", uri=True)
    try:
        legacy = conn.execute(
            "SELECT symbol, entry_time, trade_mode, realized_pnl, pnl_net "
            "FROM trade_records WHERE trade_date < '2026-09-01'").fetchall()
    finally:
        conn.close()
    assert legacy, "live 库无历史成交, 本例失去意义"
    dirty = [r for r in legacy if r[4] is not None]
    if not dirty:
        return  # 主路径: 历史行全为 NULL, COALESCE 回落成立

    assert all(r[2] == "live" for r in dirty), (
        f"非实盘行被写入 pnl_net: {dirty} — RFC-078 迁移只搬 trade_mode='live'")
    if not ess.SIM_DB.exists():
        pytest.skip("sim 库不在, 无法核验例外行的逐字来源")
    sconn = sqlite3.connect(f"file:{ess.SIM_DB}?mode=ro", uri=True)
    try:
        for symbol, entry_time, _mode, realized_pnl, pnl_net in dirty:
            n = sconn.execute(
                "SELECT COUNT(*) FROM trade_records WHERE trade_mode='live' "
                "AND symbol=? AND entry_time=? AND realized_pnl IS ? AND pnl_net IS ?",
                (symbol, entry_time, realized_pnl, pnl_net)).fetchone()[0]
            assert n > 0, (
                f"live 库历史行 {symbol} {entry_time} 的 pnl_net={pnl_net} 在 sim 库找不到"
                f"逐字源行 (realized_pnl={realized_pnl}) ⇒ 不是 RFC-078 迁移, 而是改写了"
                f"既有历史数值 (COALESCE(pnl_net, realized_pnl) 的回落语义已被破坏)")
    finally:
        sconn.close()


# ══════════════════════════════════════════════════════════════════
#  6. diagnostic_results 列名笔误: 真 DDL + 真 SQL 字面量跑通
# ══════════════════════════════════════════════════════════════════
def _extract_diag_ddl() -> str:
    src = DIAG_BASE.read_text(encoding="utf-8")
    match = re.search(r"(CREATE TABLE IF NOT EXISTS diagnostic_results\s*\(.*?\)\s*\n\s*\"\"\")",
                      src, re.DOTALL)
    assert match, "写方 app/diagnostics/base.py 的建表语句挪位了 — 请同步本测试"
    return match.group(1).replace('"""', "")


def _extract_diag_sql() -> str:
    src = DATA_SERVICE.read_text(encoding="utf-8")
    match = re.search(r'"(SELECT data FROM diagnostic_results WHERE [^"]+)"', src)
    assert match, "get_diagnostic_data 的 SQL 字面量挪位了 — 请同步本测试"
    return match.group(1)


def test_get_diagnostic_data_query_runs_against_real_writer_schema():
    """旧写法 `WHERE key = ?` 在两库都无 key 列 ⇒ 必抛 no such column, 且被
    `_db_query_sync` 的 `except → return []` 吞成静默空数据。本例用写方的真 DDL
    + 读方的真 SQL 在内存库上对跑, 证明修后确实取回数据。"""
    sql = _extract_diag_sql()
    assert "module_name" in sql, f"列名笔误未修: {sql}"
    assert not re.search(r"\bkey\b\s*=", sql), f"仍在用不存在的 key 列: {sql}"

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(_extract_diag_ddl())
        conn.execute(
            "INSERT INTO diagnostic_results (module_name, status, data, summary, timestamp) "
            "VALUES (?, ?, ?, ?, ?)",
            ("shadow_account_v2", "success", '{"behavioral_score": 1}', "s", "2026-09-01T00:00:00"),
        )
        row = conn.execute(sql, ("shadow_account_v2",)).fetchone()
    finally:
        conn.close()
    assert row is not None, "修后仍取不到行 ⇒ 修复无效"
    assert row[0] == '{"behavioral_score": 1}'


def test_old_diagnostic_query_would_have_failed():
    """证伪力自检: 把 SQL 退回旧写法, 必须真的抛错 (否则上一条测试是空转)."""
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute(_extract_diag_ddl())
        with pytest.raises(sqlite3.OperationalError, match="no such column: key"):
            conn.execute(
                "SELECT data FROM diagnostic_results WHERE key = ? "
                "ORDER BY rowid DESC LIMIT 1", ("shadow_account_v2",))
    finally:
        conn.close()


# ══════════════════════════════════════════════════════════════════
#  7. 切换面钉桩: 看板不得硬编码主库路径
# ══════════════════════════════════════════════════════════════════
def test_dashboard_has_no_hardcoded_main_db_path():
    """库级分离要靠**解析器**切换, 不是靠再硬编码一个路径 (§13.15 抓过 day_score 硬编码).

    唯一豁免: mode_switch_service._clear_live_db() —— 它的语义就是"针对 live 库文件
    归档并清空", 与运行模式无关, 必须写死。
    """
    import ast

    offenders = []
    for path in sorted((ROOT / "app" / "web_dashboard").rglob("*.py")):
        if path.name == "mode_switch_service.py":
            continue
        src = path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        # 文档串合法地叙述历史 (「原硬编码 data/ftseek_us_live.db」) ⇒ 只查非文档串字面量
        docstrings = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                doc = ast.get_docstring(node, clean=False)
                if doc:
                    docstrings.add(doc)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if node.value in docstrings:
                continue
            if "ftseek_us.db" in node.value or "ftseek_us_live.db" in node.value:
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.value[:80]}")
    assert not offenders, "看板内出现硬编码主库路径:\n" + "\n".join(offenders)


def test_sim_open_positions_primary_key_still_symbol():
    """分离方案的根据: PK=symbol ⇒ 两 env 无法共存于一表 ⇒ 只能库文件级分离.

    若哪天 PK 变成 (symbol, account_type), 本批的库级分离结论就要重议。
    """
    if not ess.SIM_DB.exists():
        pytest.skip("sim 库不存在")
    conn = sqlite3.connect(f"file:{ess.SIM_DB}?mode=ro", uri=True)
    try:
        pk = [r[1] for r in conn.execute("PRAGMA table_info(open_positions)") if r[5]]
    finally:
        conn.close()
    assert pk == ["symbol"], f"open_positions PK 变了 ({pk}) ⇒ 分离方案需重议"
