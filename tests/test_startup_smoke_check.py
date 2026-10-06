"""启动冒烟守卫钉子 (2026-09-10, DEB-20260910 §10.2-2, 用户裁「需要」)。

病根 (实测): 断言 5「模拟盘安全确认」原针是**横幅文案字面量** `模拟盘 (TRADE_ENV=1`,
上游把启动横幅 `[安全确认]`→`[StartupMode]` 之后全仓再无任何代码输出该串 ⇒ 这条安全
断言变永久红。**且换针修不好**: 段界取"最后一个 `[PreFlight]`", 而该次启动
`[StartupMode] 模式一致` 在日志第 2917 行、`[PreFlight]` 在第 2918 行 ⇒ 新标记落在段外,
实测把针换成 `[StartupMode] 模式一致` 仍为 False。

修法三件 (本文件逐件钉住): ①段界主标记 → `[StartupMode]`; ②断言 5 → 正则解析语义值,
两个独立证据源 (`[StartupMode]` 三元组 + `[交易环境]` 账户路由横幅) 就位者必须全部为 sim;
③证据源缺席 → exit 2「判定无效」(守卫失明 ≠ 安全), 与 exit 1「确认不安全」分开。

2026-09-14 增补 (跨轮转失明): 原实现只读当前档 `logs/ftseek_us.log`, 而 logrotate 是
daily + copytruncate (实测 03:12-03:48 CST) ⇒ 进程在轮转前启动、轮转后再跑本守卫时,
启动段整段在轮转档里, 当前档只剩尾巴: 实测 2026-09-14 10:17 (进程 00:08:55 启动,
03:31 轮转, 服务健康) 报 exit=2「日志中未找到启动标记」, 七条断言全部无从判定。
修法 = 当前档无段界标记时按 `daily_alert_summary.resolve_log_sources` 的语义升序拼接
回溯窗内的轮转档 (含 .gz)。下方 TestCrossRotation 四例钉住: 拼得到 / 拼接后断言 7
仍咬得住轮转前那半段的 Traceback (假绿面) / 全档无标记时报"拼了几档"而不是静默 /
当前档已有标记时走快路径不读旧档 (重启后立刻跑的常规形态行为不变)。

回滚: 删本文件 + revert `scripts/startup_smoke_check.py` 对应改动 (零生产代码依赖)。
"""
import importlib.util
import gzip
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

_spec = importlib.util.spec_from_file_location(
    "startup_smoke_check", PROJECT_ROOT / "scripts" / "startup_smoke_check.py")

SIM_MODE = "[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=False"
REAL_MODE = "[StartupMode] 模式一致: broker=real db=real ALLOW_REAL_TRADING=True"
SIM_ROUTE = "  账户路由 AUTO_TRADE_ACCOUNT=sim | ALLOW_REAL_TRADING=False"
REAL_ROUTE = "  账户路由 AUTO_TRADE_ACCOUNT=live | ALLOW_REAL_TRADING=True"
LIVE_PROBE_MODE = "[StartupMode] 模式一致: broker=sim db=sim ALLOW_REAL_TRADING=True"
LIVE_PROBE_ROUTE = "  账户路由 AUTO_TRADE_ACCOUNT=sim | ALLOW_REAL_TRADING=True"


def _load():
    mod = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(mod)
    return mod


def _boot_lines(*, mode_line=SIM_MODE, route_line=SIM_ROUTE,
                marker="[PreFlight] OpenD 连通性检查通过 (127.0.0.1:11111)",
                extra="", when=None):
    """合成启动段的行列表. 关键: `[StartupMode]` 行**排在** `[PreFlight]` 之前 (照抄实测顺序)。"""
    stamp = (when or datetime.now()).strftime("%Y-%m-%d %H:%M:%S")
    parts = []
    if mode_line:
        parts.append(f"{stamp} [MainThread] [ftseek_us.main] INFO: {mode_line}")
    if marker:
        parts.append(f"{stamp} [MainThread] [ftseek_us.main] INFO: {marker}")
    parts += [
        f"{stamp} [MainThread] [ftseek_us.main] INFO: [ConfigValidator] 校验结果: ✅ 全部通过",
        f"{stamp} [MainThread] [ftseek_us.main] INFO: [ConfigGov] ✅ 漂移检测已启动",
        f"{stamp} [MainThread] [ftseek_us.main] INFO: [ConfigGov] ✅ 热重载已启动",
        f"{stamp} [MainThread] [ftseek_us.main] INFO: [ConfigAudit] ✅ SSOT 配置一致性审计通过 (0 BLOCKING)",
        f"{stamp} [MainThread] [app.core.scheduler_core] INFO: ==== [交易环境] ====",
    ]
    if route_line:
        parts.append(route_line)
    if extra:
        parts.append(extra)
    parts.append(f"{stamp} [PlaybookExitFastLoop] [app.core.scheduler_core] INFO: "
                 "[PlaybookExit] 快循环已启动 (interval=10s)")
    return parts


def _write_boot_log(tmp_path, **kw):
    """把合成启动段写进当前档 (轮转前启动/未轮转的常规形态)。"""
    log = tmp_path / "ftseek_us.log"
    log.write_text("\n".join(_boot_lines(**kw)) + "\n", encoding="utf-8")
    return log


def _run(tmp_path, **kw):
    ssc = _load()
    ssc.LOG = _write_boot_log(tmp_path, **kw)
    return ssc, ssc.run_once()


def test_healthy_boot_passes_all_seven(tmp_path):
    """①健康启动段全绿 — 证明新段界+语义断言没有把正常启动误判成红。"""
    _ssc, (code, failed, boot_time) = _run(tmp_path)
    assert code == 0, failed
    assert failed == []
    assert boot_time is not None


def test_authorized_live_probe_keeps_global_sim_route(tmp_path):
    """双臂安全形态: 全局 broker/db 仍 sim, 仅显式 LIVE 探针允许 REAL 开关。"""
    ssc = _load()
    ssc.LOG = _write_boot_log(
        tmp_path, mode_line=LIVE_PROBE_MODE, route_line=LIVE_PROBE_ROUTE
    )
    code, failed, _boot_time = ssc.run_once(live_probe=True)
    assert code == 0, failed

    code, failed, _boot_time = ssc.run_once(live_probe=False)
    assert code == 1
    assert "双臂安全确认" in failed


def test_runtime_traceback_after_service_started_is_not_startup_failure(tmp_path):
    """启动段止于「服务已启动」; 运行期已知 pending 回报不得污染启动冒烟。"""
    ssc = _load()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ssc.LOG = tmp_path / "ftseek_us.log"
    ssc.LOG.write_text(
        "\n".join(_boot_lines())
        + f"\n{stamp} [MainThread] [ftseek_us.main] INFO: ✅ FTSeek-US 服务已启动"
        + "\nTraceback (most recent call last):",
        encoding="utf-8",
    )
    code, failed, _boot_time = ssc.run_once()
    assert code == 0, failed


def test_segment_starts_at_startup_mode_even_though_preflight_is_later(tmp_path):
    """①病根钉: `[StartupMode]` 在 `[PreFlight]` **之前**一行时, 必须仍落在启动段内。"""
    ssc, (_code, _failed, _bt) = _run(tmp_path)
    seg, _bt = ssc._extract_boot_segment(ssc.LOG.read_text(encoding="utf-8"))
    assert seg.lstrip().splitlines()[0].count("[StartupMode]") == 1, "段界没吃到 StartupMode 行"
    assert ssc._MODE_RE.search(seg), "模式三元组不在段内 ⇒ 断言 5 会假红"


def test_real_mode_fails_sim_assertion(tmp_path):
    """②真钱模式必须判红 (守卫的存在意义)。"""
    _ssc, (code, failed, _bt) = _run(tmp_path, mode_line=REAL_MODE, route_line=REAL_ROUTE)
    assert code == 1
    assert "双臂安全确认" in failed


def test_disagreeing_sources_fail(tmp_path):
    """②两个证据源互相矛盾 (一个 sim 一个 live) 也必须判红 — 不许"有一个说 sim 就放行"。"""
    _ssc, (code, failed, _bt) = _run(tmp_path, mode_line=SIM_MODE, route_line=REAL_ROUTE)
    assert code == 1
    assert "双臂安全确认" in failed


def test_missing_all_mode_evidence_is_invalid_not_pass(tmp_path):
    """③证据全缺 = 守卫失明 → exit 2 判定无效, 绝不能静默放行 (exit 0) 或误报不安全 (exit 1)。"""
    _ssc, (code, failed, _bt) = _run(tmp_path, mode_line="", route_line="")
    assert code == 2
    assert "模式证据缺失" in failed[0]
    assert "StartupMode" in failed[0] and "交易环境横幅" in failed[0]


def test_missing_one_source_is_invalid(tmp_path):
    """③只缺一个源同样判无效 — 少一个源就是少一半覆盖, 不得当成"另一半说 sim 就够了"。"""
    _ssc, (code, failed, _bt) = _run(tmp_path, route_line="")
    assert code == 2
    assert "交易环境横幅" in failed[0]


def test_traceback_in_segment_still_fails(tmp_path):
    """断言 7 未被本次修改削弱: 段内出现 Traceback 仍判红。"""
    _ssc, (code, failed, _bt) = _run(tmp_path, extra="Traceback (most recent call last):")
    assert code == 1
    assert "零 Traceback/CRITICAL" in failed


def test_fallback_marker_still_finds_segment(tmp_path):
    """上游若再改名致 `[StartupMode]` 消失, 段界回退 `[PreFlight]` 仍取到段 → 报 exit 2 而非"无启动段"。"""
    _ssc, (code, failed, boot_time) = _run(tmp_path, mode_line="")
    assert code == 2
    assert boot_time is not None
    assert "未找到启动标记" not in failed[0]


def test_stale_segment_is_invalid(tmp_path):
    """段龄超过 MAX_BOOT_AGE_HOURS → exit 2 (本次重启没产生新段)。"""
    old = datetime.now() - timedelta(hours=7)
    _ssc, (code, failed, _bt) = _run(tmp_path, when=old)
    assert code == 2
    assert "过旧" in failed[0]


def test_dead_literal_needle_not_reintroduced():
    """④文案字面量针不得回潮: **代码里**不许再用 TRADE_ENV=1 / 旧横幅串做判定。

    只查 AST 的字符串常量并排除模块 docstring —— 文档字符串里讲这段历史是应该的,
    被禁的是把文案字面量当断言针 (那正是本次永久红的病根)。
    """
    import ast

    src = (PROJECT_ROOT / "scripts" / "startup_smoke_check.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    # 按**节点身份**排除模块 docstring (get_docstring 会 cleandoc, 字面比较对不上)
    _first = tree.body[0].value if tree.body and isinstance(tree.body[0], ast.Expr) else None
    doc_node = _first if isinstance(_first, ast.Constant) and isinstance(_first.value, str) else None
    code_consts = [n.value for n in ast.walk(tree)
                   if isinstance(n, ast.Constant) and isinstance(n.value, str) and n is not doc_node]
    for dead in ("TRADE_ENV=1", "模拟盘 (TRADE_ENV", "[安全确认]"):
        offenders = [c for c in code_consts if dead in c]
        assert not offenders, f"文案字面量针回潮 ({dead!r}): {offenders}"


# ══════════════ 2026-09-14 增补: 跨轮转拼接 (copytruncate 失明面) ══════════════

def _split_logs(tmp_path, boot_lines, tail_lines, *, stamp=None, gz=True):
    """模拟 logrotate copytruncate: 启动段落进轮转档, 当前档只剩轮转后的尾巴。

    dateext 用**轮转当日**日期命名 (实测 ftseek_us.log-20260914 末行是 09-14 03:31),
    所以默认用今天的戳; 两档内容不重叠, 升序拼接即完整时间线。
    """
    stamp = stamp or datetime.now().strftime("%Y%m%d")
    arch = tmp_path / f"ftseek_us.log-{stamp}"
    arch.write_text("\n".join(boot_lines) + "\n", encoding="utf-8")
    if gz:
        with open(arch, "rb") as raw, gzip.open(arch.with_suffix(arch.suffix + ".gz"), "wb") as z:
            z.write(raw.read())
        arch.unlink()
        arch = arch.with_suffix(arch.suffix + ".gz")
    log = tmp_path / "ftseek_us.log"
    log.write_text("\n".join(tail_lines) + "\n", encoding="utf-8")
    return log, arch


def _run_split(tmp_path, **kw):
    ssc = _load()
    ssc.LOG = _split_logs(tmp_path, **kw)[0]
    return ssc, ssc.run_once()


class TestCrossRotation:
    def test_boot_segment_in_rotated_archive_is_found(self, tmp_path):
        """病根钉: 启动段整段在轮转档里 (当前档只有尾巴) → 仍须找到并七条全判。

        旧实现只读当前档 ⇒ 报 exit 2「日志中未找到启动标记」, 七条断言全部无从判定
        (2026-09-14 10:17 实测形态: 进程 00:08:55 启动, 03:31 轮转, 服务其实健康)。
        """
        ssc, (code, failed, boot_time) = _run_split(
            tmp_path, boot_lines=_boot_lines(), tail_lines=[
                f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} [VIXMonitorLoop] "
                "[app.core.scheduler_core] INFO: [VIX] 当前值=14.8 | 级别=NORMAL"])
        assert code == 0, failed
        assert boot_time is not None
        assert ssc.LAST_COVERAGE["spliced"] is True and ssc.LAST_COVERAGE["sources"] == 2

    def test_traceback_after_rotation_still_fails_assertion_seven(self, tmp_path):
        """假绿面钉: 启动段被轮转劈成两半时, **后半段**的 Traceback 必须照样咬住。

        旧实现在这一形态下走 exit 2 (找不到段界) ⇒ 断言 7 根本没跑, 崩了也报不出。
        """
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        _ssc, (code, failed, _bt) = _run_split(
            tmp_path, boot_lines=_boot_lines(extra="Traceback (most recent call last):"), tail_lines=[
                f"{now} [VIXMonitorLoop] [app.core.scheduler_core] INFO: "
                "[VIX] 当前值=14.8 | 级别=NORMAL",
            ])
        assert code == 1, failed
        assert "零 Traceback/CRITICAL" in failed

    def test_no_marker_anywhere_reports_splice_count(self, tmp_path):
        """全档都没有段界标记 → 仍 exit 2, 但必须自陈"拼了几档"(证明慢路径真跑过, 不是静默跳过)。"""
        _ssc, (code, failed, boot_time) = _run_split(
            tmp_path, boot_lines=["2026-09-14 00:08:59 [MainThread] INFO: 无标记的一档"],
            tail_lines=["2026-09-14 05:00:00 [MainThread] INFO: 尾巴"])
        assert code == 2 and boot_time is None
        assert "未找到启动标记" in failed[0] and "已跨轮转拼接 2 档" in failed[0]

    def test_fast_path_ignores_archives_when_current_has_marker(self, tmp_path):
        """快路径不变钉: 当前档已有段界标记 → 只读当前档, 不把轮转档拼进来。

        钉的是**读取面**而不是判定结果: 轮转档里放一段带 Traceback 的旧启动段 (上一个
        进程的残骸), 当前档放轮转后那次重启的干净启动段。若实现无脑拼接, sources 会变 2
        (与 test_boot_segment_in_rotated_archive_is_found 的断言互为反证 ⇒ 本例非空转)。
        """
        ssc = _load()
        _log, _arch = _split_logs(
            tmp_path,
            boot_lines=_boot_lines(extra="Traceback (most recent call last):"),
            tail_lines=["2026-09-14 05:00:00 [MainThread] INFO: 尾巴"])
        # 轮转后又重启一次: 新的干净启动段写进当前档 (尾巴保留, 模拟 copytruncate 后继续写)
        cur = tmp_path / "ftseek_us.log"
        cur.write_text(cur.read_text(encoding="utf-8") + "\n".join(_boot_lines()) + "\n",
                       encoding="utf-8")
        ssc.LOG = cur
        code, failed, boot_time = ssc.run_once()
        assert code == 0, failed
        assert boot_time is not None
        assert ssc.LAST_COVERAGE == {"sources": 1, "spliced": False}, (
            "当前档已有段界标记却去拼了轮转档 ⇒ 读取面变宽, 上一个进程的崩栈会污染本次判定")
