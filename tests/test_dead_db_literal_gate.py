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
    "app/core/db_utils.py": "mode SSOT 解析器本体 (同上)",
    "app/core/config_container.py": "TRADE_ENV 条件式解析, 非无条件读",
    "app/storage/db_pool.py": "TRADE_ENV 条件式 _DB_PATHS 映射",
    "app/plan/position_intelligence.py": "解析器 except 兜底, 且兜底本身按 TRADE_ENV 判",
    "scripts/update_signal_lifecycle.py": "TRADE_ENV 条件式 DB_PATH",
    "scripts/daily_ic_analysis.py": "解析器兜底按 TRADE_ENV 判 (2026-08-28 由无条件硬编码改)",
    "app/web_dashboard/mode_switch_service.py": "模式切换器职责所在: 切实盘前清空/校验该库",
    "scripts/data_health_check.py": "健康检查故意同时统计两个载体 (比对而非消费)",
    "app/plan/plan_feedback.py": "模块已冻结 (scheduler_triggers 调用点 2026-08-28 摘除), "
                                "复活前须先按 docstring 4 项缺陷清单改解析",
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
