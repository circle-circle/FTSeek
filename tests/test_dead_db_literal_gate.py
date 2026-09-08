"""死库文件名 (ftseek_us_live.db) 单向声明门 (2026-08-28, C类"死库伪装"制度化)。

背景: data/ftseek_us_live.db 是 TRADE_ENV=0 (实盘) 时代的同名文件, 现网 sim 模式下
零写入 (实测冻结在 2026-07-19, 而权威库 ftseek_us.db 每日在写)。它最坏的性质不是"读不到",
而是"读得到且长得像真库": thesis_challenge / battle_plan / watchlist / plan_feedback
都硬编码过它, 读到的全是陈旧影子行却无任何报错。8/28 已逐一改为 mode-aware 解析,
本门把"再写一处无条件读它"变成测试红而不是下周的排查。

双向 (与 test_ops_scripts_sync 同法, 单向普查本身是缺陷):
  方向1: 代码级出现该文件名的 .py 必须出现在 ALLOWLIST (新站点必须先声明理由)。
  方向2: ALLOWLIST 里的文件若已不再出现该文件名 = 陈旧豁免, 同样报警 (防名单腐烂)。
注释与 docstring 里的提及不计 (解释性文字正是本门要留下的记录形态)。
"""
import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
NECROSIS_FILE = "ftseek_us_live.db"
SKIP_DIRS = {"venv", ".venv", "archive", "docs", "tests", "node_modules", ".git", "data"}

# 现存站点 → 为何允许 (2026-08-28 逐个人工核实)
ALLOWLIST = {
    "app/core/paths.py": "mode SSOT 解析器本体 (TRADE_ENV=0 才返回此名)",
    # 2026-09-08 (RFC-078): 理由**扩充** —— 除 TRADE_ENV 条件式解析外, 新增的
    # get_dashboard_db() / get_dashboard_trade_records_db() 在 FTSEEK_DASHBOARD_DB_ISOLATION=1
    # (默认) 时**无条件**返回此名: 看板券商侧恒 REAL (_is_live() 硬编码 True), 其账本必须
    # 落 live 库, 与 TRADE_ENV 解耦, 否则真钱实盘单写进 sim 数据收集仓。仍是解析器本体。
    "app/core/db_utils.py": "mode SSOT 解析器本体 (TRADE_ENV 条件式 + RFC-078 看板专属路由)",
    "app/core/config_container.py": "TRADE_ENV 条件式解析, 非无条件读",
    "app/storage/db_pool.py": "TRADE_ENV 条件式 _DB_PATHS 映射",
    "app/plan/position_intelligence.py": "解析器 except 兜底, 且兜底本身按 TRADE_ENV 判",
    "scripts/update_signal_lifecycle.py": "TRADE_ENV 条件式 DB_PATH",
    "scripts/daily_ic_analysis.py": "解析器兜底按 TRADE_ENV 判 (2026-08-28 由无条件硬编码改)",
    "app/web_dashboard/mode_switch_service.py": "模式切换器职责所在: 切实盘前清空/校验该库",
    "scripts/data_health_check.py": "健康检查故意同时统计两个载体 (比对而非消费)",
    # 2026-09-06 (DEB-20260905 §13.17): 本条目由该门**自己抓出来后**补登记的, 不是
    # 先写代码再放行 —— 门确实咬人了, 记档以证其证伪力。
    "scripts/env_separation_shadow.py": "库级分离切换前的**双载体**影子核对: 故意同时"
                                        "只读两个载体做 schema/EXPLAIN/数据面比对"
                                        "(比对而非消费), 与 data_health_check 同类。"
                                        "不得改用 mode-aware 解析器 —— 解析器只返回"
                                        "一个载体, 而本脚本的产出正是两载体之差"
                                        "(判定 READY_TO_SWITCH 的依据)",
    "app/plan/plan_feedback.py": "模块已冻结 (scheduler_triggers 调用点 2026-08-28 摘除), "
                                "复活前须先按 docstring 4 项缺陷清单改解析",
    # 2026-09-08 (RFC-078 批A): 本条目同样是**先被本门抓红、后补登记**的 (与
    # env_separation_shadow 同例), 记档以证该门的证伪力。
    "scripts/migrate_live_records_to_live_db.py":
        "RFC-078 批A 一次性账本迁移器: 把误落在 sim 库的**实盘**行 (trade_records "
        "trade_mode='live' / manual_order_audit mode LIKE 'live%') 搬进 live 库。"
        "**必须**同时只读源库+读写目标库 (双载体搬运), 改用 mode-aware 解析器只能拿到"
        "一个载体、无法完成迁移; 且本脚本正是为了终结「该库零写入=死库」这一状态 —— "
        "迁移+看板路由切换后 live 库重新成为实盘账本的活载体。默认 dry-run, --apply 前强制备份。",
}


def _rel(p: Path) -> str:
    return str(p.relative_to(REPO))


def _code_string_literals(path: Path):
    """文件里出现在"代码"中的字符串常量 (排除注释与 docstring)。"""
    src = path.read_text(encoding="utf-8")
    if NECROSIS_FILE not in src:
        return []
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [f"{_rel(path)}: 解析失败 (门不得因语法错误失明)"]
    doc_nodes = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
                doc_nodes.add(id(body[0].value))
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) \
                and NECROSIS_FILE in node.value and id(node) not in doc_nodes:
            hits.append(f"{_rel(path)}:{node.lineno}")
    return hits


def _all_hits():
    hits = []
    for p in REPO.rglob("*.py"):
        if set(p.relative_to(REPO).parts) & SKIP_DIRS:
            continue
        hits.extend(_code_string_literals(p))
    return hits


def test_new_unconditional_live_db_site_must_be_declared():
    undeclared = [h for h in _all_hits() if h.split(":")[0] not in ALLOWLIST]
    assert not undeclared, (
        "新增无条件引用废弃同名单库 " + NECROSIS_FILE + " 的代码站点: "
        + "; ".join(undeclared)
        + " — 该库在 sim 模式下零写入, 读它=读陈旧影子。正解是 app.core.paths."
          "get_trade_records_db() / app.core.db_utils.get_main_db(); 若确有理由 "
          "(解析器/模式切换器/双载体健康检查), 加进 ALLOWLIST 并写明理由")


def test_allowlist_has_no_stale_entries():
    live_files = {h.split(":")[0] for h in _all_hits()}
    stale = sorted(set(ALLOWLIST) - live_files)
    assert not stale, "豁免名单陈旧 (文件已不再引用该库却仍挂在名单上): " + "; ".join(stale) \
        + " — 单向普查本身即缺陷, 请同步删除条目"


def test_resolvers_do_return_mode_aware_paths():
    """名单里的"正解"必须真的存在且可解析 — 否则本门指向一条走不通的路。"""
    import importlib
    paths = importlib.import_module("app.core.paths")
    db_utils = importlib.import_module("app.core.db_utils")
    main = str(paths.get_trade_records_db())
    assert main.endswith(".db")
    assert main == str(db_utils.get_main_db()), \
        "两个解析器指向不同库 = mode 判定分叉"
