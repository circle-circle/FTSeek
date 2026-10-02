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
    # 2026-09-09 (RFC-080 demix): 以下三处同样是**先被本门抓红、后逐个人工核实补登记**
    # (与 env_separation_shadow / migrate_live_records_to_live_db 同例), 记档以证
    # 该门证伪力 —— 三者全是双载体比对/搬运工具, 非消费方:
    "scripts/demix_sim_live_records.py":
        "RFC-080 demix 一阶段清理器: 同时只读两库普查互污行 (sim 库 trade_mode='live' "
        "真钱行 / live 库模拟行), 字段级验证孪生后删错放副本。**必须**双载体 (比对而非"
        "消费), mode-aware 解析器只给一个载体无法做差集。默认 dry-run, --apply 前强制备份。",
    "scripts/demix2_broker_verified.py":
        "RFC-080 demix 二阶段定向修正器: 以 broker REAL 成交史 (唯一权威) 核验后对两库"
        "做恢复/移动/删除 (TTE 恢复回 sim, AVGO 移回 sim, MSFT/BULL 删 live 重复行)。"
        "与 migrate_live_records_to_live_db 同类双载体搬运。默认 dry-run, --apply 前备份。",
    "scripts/daily_invariant_check.py":
        "W3 防复发守卫: 只读统计两库互污行数 (sim 库 live 行 / live 库 sim 行), "
        "比对而非消费, 与 data_health_check 同类。单库翻转日后 sim 侧检测自动空转、"
        "live 侧继续有效, 改用 mode-aware 解析器反而丢掉守卫意义。",
    # 2026-10-02 (INCIDENT-002 P2-5): 同样是**先被本门抓红、后补登记**的 (与
    # env_separation_shadow / daily_invariant_check 同例), 记档以证该门证伪力 ——
    # 双载体健康检查类, 比对而非消费, 只读不写。
    "scripts/exit_closekey_health.py":
        "close_key 死锁体检探针: **必须**同时只读 sim + live 两个仓位载体, 因为 "
        "exit_intents.target_env 分 sim/real ⇒ 死锁可能落在任一载体; mode-aware "
        "解析器只返回当前一个载体, 会让探针对另一载体的死锁**完全失明** (切盘后"
        "尤其致命 — 恰是静默零保护形态)。产物是两载体各自的死锁清单, 即「差集/普查」"
        "而非消费, 与 daily_invariant_check / data_health_check 同类。",
    # 2026-09-15 (DEB-20260914-cutover §十): 以下两条同样是**先被本门抓红、后补登记**
    # (与 env_separation_shadow / daily_invariant_check 同例), 记档以证该门证伪力 ——
    # 二者是 sim⇄live **双向**切换预检/彩排工具, 产物本身就是"两载体之差"。
    "scripts/cutover_preflight.py":
        "切换预检哨兵 (10 探针 P0-P9, cron 每日 06:38 `--watch --push`): **必须**同时"
        "只读两库才能产出它的判据 —— P2 两库 schema 差集 (实测切 live 触发补列 33 条)、"
        "P4 两库同标的重叠、P5 `sl_broker_order_id` 语义分臂、P7 `trade_mode` 双向混同、"
        "P9 live 库分臂台账连续性。改用 mode-aware 解析器只能拿到**一个**载体 ⇒ "
        "差集恒为空、探针集体假绿 (这正是本工具要防的失效形态)。全程只读: 由 "
        "`tests/test_cutover_infra.py` 的 AST 只读钉 (只扫真正传给 execute*() 的字面量 SQL) "
        "+ `_ro()` 强制 `mode=ro` URI (库不存在则抛, 绝不建空库) 双向锁死; "
        "库缺失不再静默跳臂, 改 `_open_lane()` 留痕并在汇总显式打印。",
    "scripts/cutover_rehearse.py":
        "切换彩排器: 把两库 `copy2` 到沙箱后走 app **真实**迁移路径跑一遍切换, "
        "再比对迁移前后缺口 —— 与 migrate_live_records_to_live_db / demix_* 同类"
        "双载体工具, 产物正是两载体之差。**生产库零接触**由三重钉锁死: `_copy_db` 只 "
        "`copy2`+对副本 checkpoint (钉子钉住)、沙箱硬断言不得位于 `data/` 下、"
        "`tests/test_cutover_infra.py::test_rehearse_touches_production_only_by_copy`。"
        "实证价值已兑现: 查出切 live 当天会在真钱库执行 2 条 `DROP TABLE` "
        "(`daemon_heartbeat`/`entry_context_snapshot`), 以及 D-A 回切丢本臂基线。",
    "scripts/exit_closekey_health.py":
        "close_key 死锁健康检查: 只读扫描 sim/live 两个仓位载体, 检测任一环境"
        "活仓被 BOOKED intent 锁死; mode-aware 解析器只返回当前环境, 会漏另一"
        "载体死锁, 故必须双载体豁免。",
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
