"""Pytest 配置文件"""
import importlib
import os
import sys
from pathlib import Path

# 添加项目根目录到 Python 路径
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))


# futu SDK 线程退出挂死修复 (2026-08-27, 根因定位): callback_executor 线程
# 默认非 daemon (SysConfig.get_all_thread_daemon()=False) 且 queue.get() 永久
# 等待; 测试触达 lazy 连接路径后, 解释器退出时 threading._shutdown 逐锁等它
# — 全量套件 141s 跑完但退出挂死 1h+ 实证 (07-30/31 同症的最久 11h)。
# 必须在任何测试创建 futu 连接之前设置 (network_manager 主循环本就 setDaemon)。
try:
    from futu.common.sys_config import SysConfig
    SysConfig.set_all_thread_daemon(True)
except Exception:
    pass


import pytest


@pytest.fixture(autouse=True)
def _reset_market_regime_gate_cache():
    # MarketRegimeGate._assess_cache 是进程级类属性 (2026-07-31 为省 AlphaVantage
    # 付费调用加的 TTL 缓存) — 测试间不清会串上下文 (全量跑 test_risk_on_normal_vix
    # 挂、单跑过的实证)。
    try:
        from app.playbook.market_regime import MarketRegimeGate
        MarketRegimeGate._assess_cache = None
    except Exception:
        pass
    yield


# ── 生产数据写入隔离 (2026-09-03 复盘批, REVIEW-20260903 §四.8) ────────────────
# 取证: 全量套件挂 open/Path.write_text 探针后实测 33 次写入落在生产 data/ 下 —
#   ①data/early_dead_signal_shadow.jsonl 被灌 2307 行 symbol=TEST (占该文件 90%),
#     shadow_board 段10 分母虚增 44× (近7天 "30min行1366" vs 真实 31);
#   ②data/audit/playbook_reconciliation.jsonl 363 行 100% 虚构 (US.TEST/US.LOSS),
#     生产审计留痕从未有过一条真记录;
#   ③data/recommendations/daily_<今日>.json 连续 8 天被合成 AAPL 180-185 覆盖;
#   ④data/vwap_gate_ab.jsonl 测试自清无残留, 但与生产写者竞争同一文件。
# 生产 data/ 是判分样本与审计留痕的载体 — 测试写它 = 直接污染尺子 (AGENT.md 军规
#   "基数自检/量纲声明"同源: 尺子说谎比没有尺子更危险)。两层防护:
#   1. 已登记的埋点缝自动重定向到 tmp 沙箱 (测试无需逐个改, 新增埋点在 pipeline 侧
#      提模块常量后在此登记一行);
#   2. 硬闸 — 任何未登记缝对 data/ 的写入当场报错并指路, 使"新增埋点忘记隔离"
#      在 CI 即刻红, 而不是几周后在判分数据里发现 (对照: 上述污染累积 8/11→9/3 共 23 天)。
#   确需写真路径的测试 (如自清型实验台账) 用 @pytest.mark.allow_production_data_write
#   显式豁免并在测试内注明理由 — 同 docs/references/crontab-loop-baseline.txt 的棘轮纪律。
_DATA_DIR = (project_root / "data").resolve()

_PRODUCTION_DATA_SEAMS = (
    # (模块, 属性, 缝类型, 沙箱内相对路径)
    # P2a (2026-09-06): 死单影子缝随退出引擎迁入 exit_engine (常量/写入点同迁)
    ("app.playbook.exit_engine", "_EARLY_DEAD_SHADOW_PATH", "path",
     "early_dead_signal_shadow.jsonl"),
    ("app.playbook.pipeline", "_VWAP_AB_LEDGER_PATH", "path", "vwap_gate_ab.jsonl"),
    ("app.playbook.audit", "_default_audit_path", "callable",
     "audit/playbook_reconciliation.jsonl"),
    # 2026-09-03 盘中链路体检补缝: 下方硬闸只拦 open/Path.write_text/write_bytes,
    #   **sqlite3.connect 直写是盲区** — tests/strategy/test_regime_strategy_integration.py
    #   每跑一次经 regime_filter.record_regime_change 往生产 signal_snapshots.db 的
    #   regime_history 灌 8 行夹具 (SPY=550/450, MA200=500, VIX=15/22/27/28/40)。
    #   实测: 表内 3044 行中 2504 行 (82.3%) 是该签名夹具, 生产自 2026-07-29 起零真行;
    #   唯一读取端 ops/hermes/intraday_diagnose_late.py:206 取"最后一行"当现行 regime
    #   → 盘中诊断长期播报测试夹具 (今日会报 BULL→BEAR trigger=safety VIX=28)。
    ("app.strategy.regime_filter", "_REGIME_HISTORY_DB", "path",
     "signal_snapshots.db"),
)


@pytest.fixture(scope="session")
def production_data_sandbox(tmp_path_factory):
    """生产 data/ 的 tmp 替身根目录 (会话级复用, 逐测试由下方缝重定向指向其中)."""
    return tmp_path_factory.mktemp("production_data_sandbox")


def _resolve_maybe(path_like):
    """把写入目标解析成绝对路径; 非路径对象/解析失败 → None (放行, 不误伤)."""
    try:
        raw = path_like if isinstance(path_like, str) else os.fspath(path_like)
    except TypeError:
        return None
    # 快筛: 仓内 data/ 下的路径必含 "/data/" (绝对) 或以 "data/" 起头 (相对)
    if "/data/" not in raw and not raw.startswith("data/"):
        return None
    try:
        cand = Path(raw)
        return str(cand.resolve() if cand.is_absolute() else (Path.cwd() / cand).resolve())
    except OSError:
        return None


@pytest.fixture(autouse=True)
def _isolate_production_data_writes(monkeypatch, request, production_data_sandbox):
    """①重定向已登记缝 ②对未登记的 data/ 写入硬闸。每个测试自动生效。"""
    for mod_name, attr, kind, rel in _PRODUCTION_DATA_SEAMS:
        target = production_data_sandbox / rel
        try:
            mod = importlib.import_module(mod_name)
        except Exception:
            continue                      # 模块不可导入 (可选依赖) → 无该缝可隔离
        if not hasattr(mod, attr):
            continue
        if kind == "path":
            monkeypatch.setattr(mod, attr, target)
        else:                             # callable 缝 (如 audit._default_audit_path)
            def _resolver(_t=target):
                _t.parent.mkdir(parents=True, exist_ok=True)
                return _t
            monkeypatch.setattr(mod, attr, _resolver)

    if request.node.get_closest_marker("allow_production_data_write"):
        yield
        return

    nodeid = request.node.nodeid
    import builtins

    def _guard(resolved, how):
        if resolved and resolved.startswith(str(_DATA_DIR) + os.sep):
            raise RuntimeError(
                f"测试写入生产数据被拦: {resolved} (via {how}, 测试 {nodeid})。"
                f" 生产 data/ 承载判分样本与审计留痕, 测试写入=污染尺子 "
                f"(REVIEW-20260903 §四.8: 死单影子曾被灌 2307 行 TEST / 审计留痕 363 行全虚构)。"
                f" 修法三选一: (a) 把写入路径参数化, 测试传 tmp_path;"
                f" (b) 在 app 侧把埋点路径提为模块常量并在 tests/conftest.py 的"
                f" _PRODUCTION_DATA_SEAMS 登记一行; (c) 确需写真路径才加"
                f" @pytest.mark.allow_production_data_write 并注明理由。")

    _orig_open = builtins.open

    def _open_guarded(file, mode="r", *a, **kw):
        if any(c in mode for c in "wax+"):
            _guard(_resolve_maybe(file), "open")
        return _orig_open(file, mode, *a, **kw)

    _orig_path_open = Path.open
    _orig_write_text = Path.write_text
    _orig_write_bytes = Path.write_bytes

    def _path_open_guarded(self, mode="r", *a, **kw):
        if any(c in mode for c in "wax+"):
            _guard(_resolve_maybe(self), "Path.open")
        return _orig_path_open(self, mode, *a, **kw)

    def _write_text_guarded(self, *a, **kw):
        _guard(_resolve_maybe(self), "Path.write_text")
        return _orig_write_text(self, *a, **kw)

    def _write_bytes_guarded(self, *a, **kw):
        _guard(_resolve_maybe(self), "Path.write_bytes")
        return _orig_write_bytes(self, *a, **kw)

    monkeypatch.setattr(builtins, "open", _open_guarded)
    monkeypatch.setattr(Path, "open", _path_open_guarded)
    monkeypatch.setattr(Path, "write_text", _write_text_guarded)
    monkeypatch.setattr(Path, "write_bytes", _write_bytes_guarded)
    yield


def pytest_sessionfinish(session, exitstatus):
    # futu SDK 线程退出兜底 (2026-08-27 修正): 事后置 t._daemonic 对 3.8+ 无效 —
    # threading._shutdown 按线程【启动时】注册的 _shutdown_locks 逐锁 acquire 等
    # 线程退出, 不复查 t.daemon (机制实证: 旧 hack 挂死/清锁干净退出)。
    # 清空 _shutdown_locks = 把已启动的非 daemon 线程事后 daemon 化: 解释器
    # finalize 阶段照常击杀这些空闲循环线程, 正常退出路径不受影响。
    import threading
    for t in threading.enumerate():
        if t is not threading.main_thread():
            try:
                t._daemonic = True
            except Exception:
                pass
    try:
        with threading._shutdown_locks_lock:
            threading._shutdown_locks.clear()
    except Exception:
        pass
