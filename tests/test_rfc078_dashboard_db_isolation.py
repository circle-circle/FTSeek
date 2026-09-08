r"""RFC-078 批A · 看板(手动实盘台)与 SIM 数据收集仓的**库级**隔离契约 (2026-09-08).

病灶 (取证见 docs/decisions/RFC-078-*.md §1): 看板券商侧恒 REAL
(`manual_order_service._is_live()` 硬编码 return True), DB 侧却由 `TRADE_ENV` 路由;
现网 `TRADE_ENV=1` ⇒ 真钱手动实盘单的账本写进 sim 数据收集仓 `data/ftseek_us.db`
(实证: sim 库 `manual_order_audit` 有 64 行 `mode='live_conservative'` 写到 2026-09-04,
而 live 库同 mode 止于 2026-06-22)。

与 `tests/test_dashboard_sim_isolation.py` 的分工: 那批是**行级收敛层**
(`env_scope` 谓词, 分离落地前的止血), 本批是它 docstring 里预告的**终局 = 库文件级分离**
(「看板 DB 路由切到 ftseek_us_live.db」)。两者并存: 谓词层不删, 作缺列容错 + 双保险。

证伪力自检: 每条钉桩都配一条"退回旧写法必须转红"的说明; 若某条断言在
`get_dashboard_db()` 被改回 `get_main_db()` 后仍绿 ⇒ 该条是空转, 应删。
"""
from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DASH = ROOT / "app" / "web_dashboard"
LIVE_NAME = "ftseek_us_live.db"
SIM_NAME = "ftseek_us.db"


def _reload_db_utils(monkeypatch, **env):
    """在受控环境变量下取得干净的 db_utils 视图 (两个开关都是**进程内固化**的).

    **刻意不用 importlib.reload**: db_utils 在 import 期就给 `sqlite3.connect` 打全局
    猴补丁 (`_safe_connect` 包住上一层), reload 一次就多包一层 ⇒ 数层后
    `sqlite3.connect()` 直接 RecursionError (本测试首版就是这样炸的)。改为直接重置
    两个固化缓存 + 替换两个 `.env.us` 解析函数, 效果等价且零副作用。
    """
    for k in ("FTSEEK_DASHBOARD_DB_ISOLATION", "FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH",
              "TRADE_ENV", "AUTO_TRADE_ACCOUNT"):
        monkeypatch.delenv(k, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    import app.core.db_utils as du
    # .env.us 优先于环境变量 ⇒ 测试必须绕过它, 否则测的是生产配置而不是被测逻辑。
    # 替身只读 os.environ (monkeypatch.setenv 会更新它) ⇒ 测试中途改环境变量也能生效。
    monkeypatch.setattr(du, "_parse_dashboard_isolation_from_file",
                        lambda: os.getenv("FTSEEK_DASHBOARD_DB_ISOLATION", "1") != "0")
    monkeypatch.setattr(du, "_parse_trade_env_from_file",
                        lambda: os.getenv("TRADE_ENV", "1") == "0")
    monkeypatch.setattr(du, "_DASHBOARD_ISOLATION_CACHE", None)
    monkeypatch.setattr(du, "_LIVE_MODE_CACHE", None)
    return du


# ══════════════════════════════════════════════════════════════════
#  1. 解析器语义
# ══════════════════════════════════════════════════════════════════
def test_dashboard_db_points_to_live_regardless_of_trade_env(monkeypatch):
    """核心命题: 看板库路由**与 TRADE_ENV 解耦** (TRADE_ENV=1 时仍指 live 库).

    证伪: 把 get_dashboard_db 改回 get_main_db() ⇒ TRADE_ENV=1 下返回 sim 库 ⇒ 本条转红。
    """
    du = _reload_db_utils(monkeypatch, TRADE_ENV="1")
    assert du.get_dashboard_db().name == LIVE_NAME
    assert du.get_dashboard_trade_records_db().name == LIVE_NAME
    # 解耦的**意义**正在于两者不同: 自动 sim 管线继续写 sim 库, 看板写 live 库
    assert du.get_main_db().name == SIM_NAME
    assert du.get_dashboard_db() != du.get_main_db()


def test_dashboard_db_still_live_when_trade_env_is_zero(monkeypatch):
    """TRADE_ENV=0 (全局实盘) 时看板路由不变 ⇒ 两种模式下看板恒 live, 无分裂。"""
    du = _reload_db_utils(monkeypatch, TRADE_ENV="0", AUTO_TRADE_ACCOUNT="real")
    assert du.get_dashboard_db().name == LIVE_NAME
    assert du.get_main_db().name == LIVE_NAME


def test_kill_switch_falls_back_to_main_db(monkeypatch):
    """一键回滚: FTSEEK_DASHBOARD_DB_ISOLATION=0 ⇒ 回落 get_main_db() (RFC-078 §5).

    没有这条, "回滚" 就只能改代码 + 重启, 而不是改一个环境变量。
    """
    du = _reload_db_utils(monkeypatch, TRADE_ENV="1", FTSEEK_DASHBOARD_DB_ISOLATION="0")
    assert du.dashboard_db_isolation_enabled() is False
    assert du.get_dashboard_db() == du.get_main_db()
    assert du.get_dashboard_trade_records_db() == du.get_trade_records_db()


def test_isolation_switch_is_process_cached(monkeypatch):
    """开关**进程内固化** — 运行期半路改路由会撕出"半本账在 sim 半本在 live"的账本.

    与 R-3 固化 TRADE_ENV 同理 (db_utils.py:78 注释记过那条自毁路径)。
    证伪: 去掉 _DASHBOARD_ISOLATION_CACHE ⇒ 本条转红。
    """
    du = _reload_db_utils(monkeypatch, TRADE_ENV="1")
    assert du.dashboard_db_isolation_enabled() is True
    # 运行期改环境变量不得生效 (显式豁免开关除外)
    monkeypatch.setenv("FTSEEK_DASHBOARD_DB_ISOLATION", "0")
    assert du.dashboard_db_isolation_enabled() is True
    monkeypatch.setenv("FTSEEK_ALLOW_RUNTIME_TRADE_ENV_SWITCH", "1")
    assert du.dashboard_db_isolation_enabled() is False


# ══════════════════════════════════════════════════════════════════
#  2. 切换面钉桩: 看板不得再走 TRADE_ENV 路由
# ══════════════════════════════════════════════════════════════════
def _dash_py_files():
    return sorted(p for p in DASH.rglob("*.py") if "__pycache__" not in p.parts)


def test_dashboard_has_no_trade_env_routed_resolution_point():
    """看板内不得再出现 `get_main_db()` / `get_trade_records_db()` **调用**.

    这是本批最容易腐烂的地方: 新加一个看板路由时顺手 `from app.core.db_utils import
    get_main_db` 就把实盘账本重新写回 sim 库, 而且**不报任何错** (静默串味)。
    注释/docstring 里的提及不计 (那正是解释性记录)。
    证伪: 任一看板文件把 get_dashboard_db 改回 get_main_db ⇒ 本条转红。
    """
    import ast

    offenders = []
    for path in _dash_py_files():
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            offenders.append(f"{path.relative_to(ROOT)}: 解析失败 (门不得因语法错误失明)")
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id in ("get_main_db", "get_trade_records_db"):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: {node.func.id}()")
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) \
                    and node.func.attr in ("get_main_db", "get_trade_records_db"):
                offenders.append(f"{path.relative_to(ROOT)}:{node.lineno}: .{node.func.attr}()")
    assert not offenders, (
        "看板内出现随 TRADE_ENV 漂移的主库解析点 (应改用 get_dashboard_db / "
        "get_dashboard_trade_records_db, RFC-078):\n" + "\n".join(offenders)
    )


def test_dashboard_db_route_points_are_all_isolated():
    """正向普查: 看板确实**在用**新解析器 (防"改了个没人调用的函数"式空转).

    实测基线 = 11 个解析点; 少于该数说明有站点被回退或漏改。
    """
    hits = []
    for path in _dash_py_files():
        src = path.read_text(encoding="utf-8", errors="replace")
        hits += [f"{path.relative_to(ROOT)}:{i + 1}"
                 for i, line in enumerate(src.splitlines())
                 if "get_dashboard_db()" in line or "get_dashboard_trade_records_db()" in line
                 and not line.strip().startswith("#")]
    assert len(hits) >= 11, f"看板隔离解析点只剩 {len(hits)} 处 (基线 11): {hits}"


# ══════════════════════════════════════════════════════════════════
#  3. 迁移脚本: 零副作用 / 幂等 / 不丢账 / 不搬主键
# ══════════════════════════════════════════════════════════════════
MIG = ROOT / "scripts" / "migrate_live_records_to_live_db.py"


def _table_md5(path: Path, table: str) -> str:
    import hashlib
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = conn.execute(f"SELECT * FROM {table} ORDER BY 1").fetchall()
    finally:
        conn.close()
    return hashlib.md5(repr(rows).encode()).hexdigest()


@pytest.mark.skipif(not (ROOT / "data" / "ftseek_us.db").exists(), reason="生产库不在")
def test_migration_dry_run_has_zero_side_effect():
    """dry-run 必须对**两库**零写入 (它会被反复跑来核对计划).

    证伪: 把 build_plan 里的 live 连接改成可写并插一行 ⇒ 本条转红。
    """
    tables = ("trade_records", "manual_order_audit", "open_positions")
    before = {(db.name, t): _table_md5(db, t)
              for db in (ROOT / "data" / SIM_NAME, ROOT / "data" / LIVE_NAME)
              for t in tables if db.exists()}
    sizes = {p.name: p.stat().st_size for p in (ROOT / "data" / SIM_NAME, ROOT / "data" / LIVE_NAME)}

    r = subprocess.run([sys.executable, str(MIG)], cwd=str(ROOT),
                       capture_output=True, text=True, timeout=180,
                       env={**os.environ, "PYTHONPATH": str(ROOT)})
    assert r.returncode == 0, r.stderr
    assert "DRY-RUN" in r.stdout

    for (name, t), md5 in before.items():
        assert _table_md5(ROOT / "data" / name, t) == md5, f"dry-run 改写了 {name}:{t}"
    for name, size in sizes.items():
        assert (ROOT / "data" / name).stat().st_size == size, f"dry-run 改变了 {name} 文件大小"


def test_migration_plan_excludes_tables_without_env_marker():
    """`trade_attribution` **刻意不迁** — 该表 81 列里没有任何 env 标记列, 无法区分实盘/模拟行.

    按纪律"不猜不迁": 宁可登记为已知缺口, 也不按时间/标的猜着搬 (搬错 = 把模拟归因
    混进实盘账本, 比不搬更坏)。证伪: 把 trade_attribution 加进 PLAN ⇒ 本条转红。
    """
    src = MIG.read_text(encoding="utf-8")
    assert "trade_attribution" not in src.split("PLAN:")[1].split("}")[0], \
        "trade_attribution 无 env 标记列, 不得进入迁移 PLAN"
    assert "open_positions" not in src.split("PLAN:")[1].split("}")[0], \
        "open_positions sim 侧现役行全是 account_type='simulate', 属实盘侧 0 行, 不得迁"


def test_migration_does_not_carry_source_primary_key():
    """迁移**不搬 id** — 实测 manual_order_audit 两侧 id 空间重叠 (sim 17..83 vs live 1..96).

    搬 id 必然撞主键; 两表统一不搬, 避免规则分裂。
    证伪: 把 use_cols 的 `!= 'id'` 去掉 ⇒ 本条转红。
    """
    src = MIG.read_text(encoding="utf-8")
    assert src.count('!= "id"') >= 2, "插入列集合必须排除 id (两侧 id 空间重叠)"
