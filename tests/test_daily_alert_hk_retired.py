"""日报「港股段退役」钉子 (DEB-20260910-logging §11.15, 2026-09-11)

病灶 (实测, 非推演): `scripts/daily_alert_summary.py` 的港股段读的是**仓外已停用项目**
的两个文件, 二者都已冻结:

| 数据源 | 实测状态 |
|---|---|
| `/opt/ftseek/FTSeek/logs/ftseek.log` | 末行 `2026-06-14 13:20:48 [Shutdown] 优雅关闭完成`, 89 天零写入 |
| `/opt/ftseek/FTSeek/data/ftseek.db` | `signal_outcomes` 最新 `2026-06-01`, 全表 26 行; `positions` open = 0 |

⇒ 报告里那段恒渲染「0 笔 / +0.00% / 0 只 / 0 错误 + 0 警告」(原件
`data/reports/daily_alert_summary_2026-09-10.md` 可验), 是一个**永不变化的假绿灯**,
还被 Hermes cron `7a40dd7c9e94` (`0 4 * * 2-6`) 的 LLM 读走转述进推送正文;
覆盖自检里那条「HK 日志源 1 个 (ftseek.log=0)」更像在声称港股日志仍被监视。

第二条污染: `sys.path.insert(1, HK_ROOT)` 把已停用仓库挂上导入路径, 而它有同名
`app/ config/ scripts/` 包 ⇒ 任何 US 侧缺失的模块都会**静默从死仓解析**
(`scripts/error_alert.py:112` 正是为躲这个副作用才自带 `resolve_log_sources` 同构实现)。

退役口径 (刻意收窄): 只断**本脚本对 HK 文件的读取与渲染**。港股**手动交易**链
(`app/web_dashboard/routes/hk_trade.py` + `hk_order_service.py`, 富途综合账户实盘直通,
`app/web_dashboard/app.py:363` 在册, 9/8–9/9 仍在迭代) 与 `app/shared/market_hours.py`
的 HK 相位**不在本批范围** —— 那两处是活的, 删了会断实盘手动下单。

钉住五件事:
  ① 源级: 脚本里不得再出现仓外港股根 (`/opt/ftseek/FTSeek` 且非 `-US`) 与 `HK_ROOT`
  ② 签名级: `build_summary_report` 无 `hk_*` 形参; 三个解析函数无 `market` 形参
     (只剩一个合法取值时, 形参就是摆设 —— 摆设形参是下一次"顺手传 HK"的入口)
  ③ **运行时级 (最强)**: 子进程装 `sys.addaudithook` 拦截任何对港股根的 open,
     再跑一遍完整读取链 ⇒ 即使将来用**计算出来的路径**重新读 HK, 这里也炸
     (源级 grep 抓不到 f-string 拼路径)。钩子自带**负对照**证明它真的会响,
     否则本钉是空转常绿。
  ④ 渲染级: 正文无港股段/无 HK 覆盖行, 但**必须显式声明口径** —— 静默消失会被
     读报告的 LLM 判成"港股数据缺失"再造一条假告警 (旧污染换新污染)
  ⑤ 判据级: 总结区的错误/警告数**逐字等于**美股区的数 (港股曾加进 total_*,
     重新引入第二个市场却不同步两处读数时, 这里就碎)
"""
from __future__ import annotations

import ast
import importlib.util
import inspect
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SCRIPT = PROJECT_ROOT / "scripts" / "daily_alert_summary.py"
_SRC = SCRIPT.read_text(encoding="utf-8")
_TREE = ast.parse(_SRC)

# 港股根 = /opt/ftseek/FTSeek 但**不是** /opt/ftseek/FTSeek-US
_HK_ROOT_RE = re.compile(r"/opt/ftseek/FTSeek(?!-US)")


def _load_module():
    spec = importlib.util.spec_from_file_location("das_hk_retired", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _func(name: str) -> ast.FunctionDef:
    for node in ast.walk(_TREE):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"脚本里找不到函数 {name}")


def _param_names(fn: ast.FunctionDef) -> list[str]:
    a = fn.args
    return [x.arg for x in (*a.posonlyargs, *a.args, *a.kwonlyargs)]


# ── ① 源级 ────────────────────────────────────────────────────────────────

def _docstring_nodes(tree: ast.AST) -> set[int]:
    """所有 docstring 节点的 id — 说明文字里点名死路径是**留证**, 不算代码引用。"""
    out: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = getattr(node, "body", None)
            if body and isinstance(body[0], ast.Expr) \
                    and isinstance(body[0].value, ast.Constant) \
                    and isinstance(body[0].value.value, str):
                out.add(id(body[0].value))
    return out


def test_no_hk_root_path_literal_in_source():
    """**代码**里不得再出现仓外港股根 (docstring 里的实证路径是留证, 显式排除)。"""
    docs = _docstring_nodes(_TREE)
    offenders = [
        n.value for n in ast.walk(_TREE)
        if isinstance(n, ast.Constant) and isinstance(n.value, str)
        and id(n) not in docs and _HK_ROOT_RE.search(n.value)
    ]
    assert not offenders, (
        f"脚本又出现仓外港股根字面量 {offenders} — 该目录 2026-06-14 已停用 "
        f"(.DECOMMISSIONED / ftseek.service masked), 读它只会产出恒零假绿灯"
    )
    # 留证归留证: 文件头必须仍记着退役理由, 否则下一个人会把港股段加回来
    assert _HK_ROOT_RE.search(ast.get_docstring(_TREE) or ""), (
        "文件头 docstring 丢了港股根实证 — 退役理由必须留痕, 防止被当作误删而复活"
    )
    names = {n.id for n in ast.walk(_TREE) if isinstance(n, ast.Name)}
    assert "HK_ROOT" not in names, "HK_ROOT 仍作为变量名在代码里被引用"


def test_single_sys_path_root():
    """只允许挂本仓一个导入根 (第二根 = 死仓同名 app/config/scripts 包的遮蔽面)。"""
    inserts = [
        n for n in ast.walk(_TREE)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        and n.func.attr == "insert"
        and isinstance(n.func.value, ast.Attribute)
        and n.func.value.attr == "path"
    ]
    assert len(inserts) == 1, f"sys.path.insert 出现 {len(inserts)} 次, 应只有 1 次 (本仓)"
    src = ast.unparse(inserts[0])
    assert "US_ROOT" in src, f"唯一导入根不是本仓常量 US_ROOT: {src}"
    # US_ROOT 本身必须指向本仓 (否则"唯一根"也可以是别处)
    assign = next(
        (n for n in ast.walk(_TREE) if isinstance(n, ast.Assign)
         and any(isinstance(t, ast.Name) and t.id == "US_ROOT" for t in n.targets)), None)
    assert assign is not None and "FTSeek-US" in ast.unparse(assign), (
        f"US_ROOT 定义异常: {ast.unparse(assign) if assign else None}")


# ── ② 签名级 ──────────────────────────────────────────────────────────────

def test_build_summary_report_has_no_hk_params():
    params = _param_names(_func("build_summary_report"))
    hk = [p for p in params if "hk" in p.lower()]
    assert not hk, f"build_summary_report 仍有港股形参 {hk} — 港股段应已整段退役"
    assert params[:3] == ["us_alerts", "us_summary", "report_date"], params


@pytest.mark.parametrize("fname", ["parse_log_alerts", "parse_log_alerts_window", "_is_noise_alert"])
def test_market_param_retired(fname):
    """`market` 形参随港股段退役: 只剩一个合法取值时它就是"顺手传 HK"的入口。"""
    params = _param_names(_func(fname))
    assert "market" not in params, f"{fname} 仍带 market 形参 {params}"


def test_hk_helpers_gone_from_module():
    das = _load_module()
    for name in ("is_hk_trading_hours", "get_hk_trade_summary", "HK_ROOT"):
        assert not hasattr(das, name), f"{name} 仍在模块命名空间里 — 港股段未退干净"
    assert not any("FTSeek-US" not in p and _HK_ROOT_RE.search(p) for p in sys.path), (
        f"导入本脚本后 sys.path 里出现港股根: {[p for p in sys.path if _HK_ROOT_RE.search(p)]}"
    )


# ── ③ 运行时级 (最强钉子: 审计钩子 + 负对照) ───────────────────────────────

_AUDIT_PROBE = r'''
import sys, importlib.util
from datetime import datetime
from pathlib import Path

HK_PREFIX = "/opt/ftseek/FTSeek/"
violations = []

def _hook(event, args):
    if event == "open":
        p = str(args[0])
        if p.startswith(HK_PREFIX):
            violations.append(p)
            raise RuntimeError("BLOCKED: 读取已停用港股根 " + p)
sys.addaudithook(_hook)

# 负对照: 钩子必须真的会响, 否则本探针是空转常绿
try:
    open(HK_PREFIX + "logs/ftseek.log", "r")
except RuntimeError as e:
    assert "BLOCKED" in str(e)
except FileNotFoundError:
    print("NEGCTL_FAIL: 钩子未拦截 (文件不存在先于钩子命中)", file=sys.stderr)
    sys.exit(3)
else:
    print("NEGCTL_FAIL: 港股根被成功打开, 钩子形同虚设", file=sys.stderr)
    sys.exit(3)
print("NEGCTL_OK")
violations.clear()   # 负对照自己那次 open 必须清掉, 否则读数恒 >=1 (钉子自我打脸)

ROOT = Path(sys.argv[2])   # 被测仓根由测试传入 — 写死生产仓会让本探针测到别的文件 (实测漏网过一次)
sys.path.insert(0, str(ROOT))
spec = importlib.util.spec_from_file_location("das_probe", ROOT / "scripts" / "daily_alert_summary.py")
das = importlib.util.module_from_spec(spec)
spec.loader.exec_module(das)

# 完整读取链: 日志解析 (真跑一遍 tmp 档) + 报告渲染
tmp = Path(sys.argv[1])
alerts, cover = das.parse_log_alerts_window(str(tmp / "ftseek_us.log"),
                                            datetime(2026, 9, 10, 21, 0),
                                            datetime(2026, 9, 11, 6, 0))
report = das.build_summary_report(alerts, {"trades": 1, "pnl": -2.0, "positions": 1},
                                  "2026-09-10", log_coverage=cover)
assert "港股" not in report or "停用" in report
print("READCHAIN_OK alerts=%d violations=%d" % (len(alerts), len(violations)))
'''


def test_read_chain_never_touches_decommissioned_hk_root(tmp_path):
    """装审计钩子跑完整读取链: 任何对港股根的 open 都会炸 (f-string 拼路径也躲不过)。"""
    logdir = tmp_path
    (logdir / "ftseek_us.log").write_text(
        "2026-09-10 22:00:00 [MainThread] [app.playbook.pipeline] WARNING: [playbook] 探针告警\n",
        encoding="utf-8",
    )
    r = subprocess.run(
        [sys.executable, "-c", _AUDIT_PROBE, str(logdir), str(PROJECT_ROOT)],
        capture_output=True, text=True, timeout=180, cwd=str(PROJECT_ROOT),
    )
    assert r.returncode == 0, (
        f"读取链触碰了已停用的港股根 (或探针自身失败):\n"
        f"--- stdout ---\n{r.stdout}\n--- stderr ---\n{r.stderr[-2000:]}"
    )
    assert "NEGCTL_OK" in r.stdout, "负对照未通过 ⇒ 审计钩子不可信, 本钉子等于没钉"
    assert "READCHAIN_OK" in r.stdout and "violations=0" in r.stdout, r.stdout


# ── ④ 渲染级 ──────────────────────────────────────────────────────────────

def _report(das, alerts=None, coverage=None):
    return das.build_summary_report(
        alerts or [], {"trades": 9, "pnl": -165.53, "positions": 3},
        "2026-09-10", log_coverage=coverage)


def test_report_has_no_hk_segment():
    das = _load_module()
    report = _report(das, coverage=["US 日志源 1 个 (ftseek_us.log=3) | 告警跨度 x→y | 窗 a→b"])
    assert "🇭🇰" not in report, "报告里又出现港股段"
    assert "港股 (FTSeek)" not in report
    assert "HK 日志源" not in report, "覆盖自检里仍在声称监视港股日志 (该文件已冻结 89 天)"


def test_report_declares_scope_instead_of_vanishing_silently():
    """口径必须显式: 港股段静默消失会被读报告的 LLM 判成"数据缺失"再造一条假告警。"""
    das = _load_module()
    report = _report(das)
    assert das._REPORT_SCOPE_NOTE in report, "总结区缺少口径声明行"
    note_line = next(ln for ln in report.splitlines() if das._REPORT_SCOPE_NOTE in ln)
    assert "2026-06-14" in note_line and "停用" in note_line, note_line


# ── ⑤ 判据级 ──────────────────────────────────────────────────────────────

def test_summary_totals_equal_us_section_verbatim():
    """总结区读数必须逐字等于美股区读数 (港股曾加进 total_*, 二者分叉即碎)。"""
    das = _load_module()
    alerts = [
        {"market": "US", "time": "2026-09-10 22:00:00", "level": "ERROR",
         "content": "[app.x] ERROR: 错误一", "is_trading_hours": True},
        {"market": "US", "time": "2026-09-10 22:01:00", "level": "ERROR",
         "content": "[app.x] ERROR: 错误二", "is_trading_hours": True},
        {"market": "US", "time": "2026-09-10 22:02:00", "level": "WARN",
         "content": "[app.x] WARNING: 警告一", "is_trading_hours": True},
        {"market": "US", "time": "2026-09-10 03:00:00", "level": "WARN",
         "content": "[app.x] WARNING: 非盘中", "is_trading_hours": False},
    ]
    report = das.build_summary_report(alerts, {"trades": 9, "pnl": -165.53, "positions": 3},
                                      "2026-09-10")
    us_line = next(ln for ln in report.splitlines() if ln.startswith("- **盘中告警**:"))
    total_line = next(ln for ln in report.splitlines() if ln.startswith("- 盘中告警:"))
    assert "2 错误 + 1 警告 (共 3 条)" in us_line, us_line
    assert total_line == "- 盘中告警: 2 错误 + 1 警告", total_line
    assert "- 🔇 非盘中静默: 1 条" in report


def test_report_still_ends_with_verdict_when_no_coverage():
    """口径行插在计数之前, 不得顶掉"结论是最后一句"这个既有契约。"""
    das = _load_module()
    report = _report(das)
    assert report.rstrip().endswith("- ✅ 盘中系统运行正常，无异常告警"), report.splitlines()[-3:]
