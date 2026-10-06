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
    # 2026-09-12 (DEB-20260910 / proud-slope-falcon P1): DA/UE 候选漏斗新载体 —
    #   candidate_funnel.jsonl (批次/候选级审计) 与 inject 额度进程间锁文件。
    #   两者都在 data/shadow/ 下且由 inject 链无条件写入 → 登记为缝, 单测自动落沙箱。
    ("app.playbook.candidate_eligibility", "FUNNEL_PATH", "path",
     "candidate_funnel.jsonl"),
    ("scripts.llm_universe_scan", "_INJECT_LOCK_PATH", "path",
     "ue_inject_quota.lock"),
    # 2026-09-12 (同批补缝): cmd_scan 现在**无条件**先落 batch_start, 失败/空扫描再落
    #   batch_end — 批次纪律要求"失败批次必须落盘并与无机会区分", 于是原本零写入的
    #   失败分支 (tests/test_ue_scan_fail_alert.py) 也开始写主 jsonl。这两个路径同时是
    #   判分样本载体 (cmd_score/cmd_report 读它), 测试写它 = 直接污染尺子, 故登记为缝。
    ("scripts.llm_universe_scan", "_SCAN_PATH", "path",
     "llm_universe_scan.jsonl"),
    ("scripts.llm_universe_scan", "_MIDDAY_PATH", "path",
     "llm_universe_scan_midday.jsonl"),
    # 2026-09-12 (同批补缝②): inject 批次级消费留痕 (_log_inject_batch_decision) 复用
    #   llm_universe_inject.jsonl 载体, 与既有 _log_inject_reject 同文件。它现在是
    #   cmd_inject 的无条件写入点 (消费了哪一批/为何不消费), 未登记前被写者自己的
    #   except 吞掉硬闸 = 隔离形同虚设, 故按 (b) 方案登记为缝。
    ("scripts.llm_universe_scan", "_INJECT_REJECT_PATH", "path",
     "llm_universe_inject.jsonl"),
    ("scripts.llm_universe_scan", "_LONG_GATE_LOG_PATH", "path",
     "ue_long_momentum_gate.jsonl"),
    # 2026-10-01 (RFC-125 阶段0): 写库边界冲突影子 — save_instruction 无条件调用
    #   (有冲突才落盘), 未登记时写者自身 except 会吞掉下方硬闸 = 隔离形同虚设。
    ("app.playbook.conflict_shadow", "_CONFLICT_SHADOW_PATH", "path",
     "instruction_conflict.jsonl"),
    # 2026-10-01 (RFC-127 阶段0): ETH出场HELD→RTH升级影子 — 退出恢复循环调用,
    #   按同纪律登记, 防测试写生产影子台账。
    ("app.playbook.eth_held_exit_shadow", "_ETH_HELD_EXIT_SHADOW_PATH", "path",
     "eth_held_exit_escalation.jsonl"),
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
    _orig_os_replace = os.replace
    _orig_os_rename = os.rename

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

    def _os_replace_guarded(src, dst, *a, **kw):
        _guard(_resolve_maybe(dst), "os.replace")
        return _orig_os_replace(src, dst, *a, **kw)

    def _os_rename_guarded(src, dst, *a, **kw):
        _guard(_resolve_maybe(dst), "os.rename")
        return _orig_os_rename(src, dst, *a, **kw)

    monkeypatch.setattr(builtins, "open", _open_guarded)
    monkeypatch.setattr(Path, "open", _path_open_guarded)
    monkeypatch.setattr(Path, "write_text", _write_text_guarded)
    monkeypatch.setattr(Path, "write_bytes", _write_bytes_guarded)
    monkeypatch.setattr(os, "replace", _os_replace_guarded)
    monkeypatch.setattr(os, "rename", _os_rename_guarded)
    yield


@pytest.fixture(autouse=True)
def _block_live_llm_http(monkeypatch):
    """测试默认禁真实 LLM API 出口 (2026-09-09): 网关与 DA skill 都走 urllib,
    "假设功能默认关"的用例在 yaml 常开后会静默真调付费 API (实证: test_l3
    thesis_inferer 以 trace_id=test 真调 qwen3.7-max 落生产审计库, 与上方
    生产数据写入硬闸同纪律)。确需真调的会话: FTSEEK_LIVE_LLM=1 pytest ..."""
    if os.environ.get("FTSEEK_LIVE_LLM") == "1":
        yield
        return
    import urllib.request
    _real_urlopen = urllib.request.urlopen
    _LLM_HOSTS = ("dashscope.aliyuncs.com", "api.openai.com", "api.anthropic.com",
                  "api.deepseek.com", "open.bigmodel.cn")

    def _guarded_urlopen(req, *a, **kw):
        url = getattr(req, "full_url", None) or (req if isinstance(req, str) else "")
        if any(h in str(url) for h in _LLM_HOSTS):
            raise RuntimeError(
                f"测试发起真实 LLM API 请求被拦: {url} — 测试应 monkeypatch 关闭/打桩 "
                f"(先例: test_llm_features.py L1 news_classifier 强制 is_enabled=False)。"
                f"确需真调: FTSEEK_LIVE_LLM=1 显式放行")
        return _real_urlopen(req, *a, **kw)

    monkeypatch.setattr(urllib.request, "urlopen", _guarded_urlopen)
    yield


@pytest.fixture(autouse=True)
def _block_outbound_notifications(request, monkeypatch):
    """测试**永不**触达生产通知通道 (2026-09-18, 用户实测反馈)。

    病灶: 个别用例 (如 test_execute_rejects_real_environment) 用 FUTU_TRD_ENV=REAL 调 _execute
    且**未** mock 推送 ⇒ 每跑一次 pytest 就给生产飞书真发一条
    "🚫 延长时段开仓拦截: US.FAKE REAL未授权"。同类污染先例: 钉钉通知器被真调用致环境污染。
    处置: 在 conftest 层封死**具体网络出口**，而不是替换 PushHub.push：
    PushHub.push 是路由/去重/聚合/广播的核心被测行为，整体替换会让既有单测
    全部变成"恒成功但零送达"的假红。这里保留 Hub 逻辑，只拦截钉钉/飞书/flag
    网络与磁盘出口；需要断言 Hub 行为的用例照常使用本地 mock channel。
    并默认把 FUTU_TRD_ENV 钉在 SIMULATE —— 测试环境不与 REAL 混淆 (显式要 REAL 的用例自行 setenv)。

    2026-09-19: 两个桩改为 `staticmethod` 装到类上。原写法把普通函数赋给类属性 ⇒ 描述符协议会把
    实例自身当第一个位置参数塞进来 (`notifier.send_text("x")` 记录到的是**对象 repr**, 渠道桩更会
    因 channel 实例上没有 `render_text()` 而抛错) ⇒ 留痕内容不可读、"出站留痕报红"没法点名发了什么。
    """
    recorded = []

    def _note(text):
        recorded.append((_outbound_origin(), str(text)))
        return True

    def _record(text, *args, **kwargs):
        return _note(text)

    for module_path, class_name in (("app.notifier.feishu", "FeishuNotifier"),
                                    ("app.notifier.dingtalk", "DingTalkNotifier")):
        try:
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name, None)
            if cls is not None and hasattr(cls, "send_text"):
                monkeypatch.setattr(cls, "send_text", staticmethod(_record), raising=False)
        except Exception:
            pass
    try:
        hub_module = importlib.import_module("app.push.hub")
        from app.push.channels import DingTalkChannel, FeishuChannel, FlagFileChannel

        def _record_channel(msg, *args, **kwargs):
            try:
                _note(msg.render_text())
            except Exception:                       # 非 PushMessage: 记 repr 也不许让桩抛错
                _note(repr(msg))
            return True

        for cls_name in ("DingTalkChannel", "FeishuChannel", "FlagFileChannel"):
            cls = getattr(hub_module, cls_name, None)
            if cls is None:
                cls = {"DingTalkChannel": DingTalkChannel,
                       "FeishuChannel": FeishuChannel,
                       "FlagFileChannel": FlagFileChannel}[cls_name]
            monkeypatch.setattr(cls, "send", staticmethod(_record_channel), raising=False)
    except Exception:
        pass
    monkeypatch.setenv("FUTU_TRD_ENV", "SIMULATE")
    yield recorded
    # ── 出站留痕 → 会话结束报红 (2026-09-19) ────────────────────────────────────
    # 上述封锁保证"测试真发不到网络", 但对**绕过封锁的出口/跑法**是哑的: 2026-09-19 实测一条建仓
    # 成交富卡把单测 fixture (`US.FAKE` 100@10.0, tests/test_extended_hours_order_execution.py:136)
    # 真发到生产 IM, 而单测**全绿**(用户的 IM 才是唯一告警面)。机制化: 用例跑到渠道 send 层留痕
    # 就记账, 会话结束置红并点名。故意触达渠道层的用例用 `@pytest.mark.allow_outbound_notification` 豁免。
    #
    # 只算 `test`/`hub` 两类来源: 后台 daemon 自发的那条 (PushHub 单例的 HealthMonitor 心跳) 会落在
    # **恰好当时在跑**的用例头上 —— 不分类就会把无辜用例算成泄漏 (假红), 而假红会把这条纪律废掉。
    mine = [t for o, t in recorded if o in ("test", "hub")]
    _OUTBOUND_BACKGROUND.extend(t for o, t in recorded if o == "background")
    if mine and not request.node.get_closest_marker(_ALLOW_OUTBOUND_MARKER):
        _OUTBOUND_LEAKS.append((request.node.nodeid, mine[0][:160], len(mine)))


# ── 券商写口封锁 (2026-09-22) ────────────────────────────────────────────────
# 实锤: tests/test_ext_entry_test_symbol_guard.py::test_real_env_notice_is_env_level_and_deduped
#   用 legacy 键 FUTU_TRD_ENV=REAL 驱动 REAL 守卫, 而守卫判据已于 2026-09-20 迁到
#   TradingMode(SSOT) ⇒ is_live=False ⇒ 守卫跳过 ⇒ **代码直落 place_order**, 每跑一次该文件
#   就往 SIM 账户真下 1 笔 US.NVDA BUY qty=1 @$100 remark=ext_entry (券商流水:
#   9/20 8191435/8191447/8192065, 9/21 8195391/8195420/8195497, 全 CANCELLED_ALL; 9/22 又见 8197138),
#   随后 wait_order_fill 还会真 modify_order 同一张单。
#   代价不止噪声: 9/21 19:25 ET 那轮生产 afterhours 执行器读到活单 ⇒ skip=active_ext_entry_order
#   ⇒ **压制了该轮真实入场** (在途单门按 symbol-无关的全局口径拦), 且每轮对账刷"跨日旧单"日志。
#   与"生产 data/ 写入硬闸""真 LLM 出口封锁""出站通知封锁"同纪律: 把"测试不该有的副作用"
#   从"事后翻券商流水才发现"变成"当场红"。**测试必须打桩券商, 不许打真券商。**
#   只拦写口 (place_order/modify_order/cancel_order), 不拦查询 (查持仓/订单是对账类用例的正常输入);
#   确需真发单的用例加 @pytest.mark.allow_live_broker_order 并注明理由。
_BROKER_WRITE_METHODS = ("place_order", "modify_order", "cancel_order")


@pytest.fixture(autouse=True)
def _block_live_broker_orders(request, monkeypatch):
    if request.node.get_closest_marker("allow_live_broker_order"):
        yield
        return
    try:
        import futu
        cls = futu.OpenSecTradeContext
    except Exception:                     # futu 不可用时无该出口可封 (与其余封锁同容错)
        yield
        return

    def _blocked(self, *args, **kwargs):
        raise RuntimeError(
            f"测试触达券商写口被拦 ({self.__class__.__name__}): 参数 {kwargs!r} — "
            f"单测必须打桩券商 (先例: tests/test_extended_hours_order_execution.py 的 "
            f"FakeTradeContext + monkeypatch _open_trade_context), 而不是打真券商。"
            f"2026-09-22 实锤: 一个用例因判据迁移而静默直落 place_order, 每次 pytest 都往 SIM "
            f"账户真下一笔 NVDA qty=1 @$100, 并压制了生产延长时段的一轮真实入场。"
            f"确需真发单: @pytest.mark.allow_live_broker_order 并注明理由。"
        )

    for name in _BROKER_WRITE_METHODS:
        if hasattr(cls, name):
            monkeypatch.setattr(cls, name, _blocked, raising=False)
    yield


def _outbound_origin():
    """留痕来源分类 (2026-09-19): `test` / `hub` / `background`。

    结构性判据 (不写死发送者名字): 看**调用栈**里有没有 `tests/` 帧 (conftest 自身除外) 或
    `app/push/hub.py` 帧。两类都算"测试驱动": 前者=测试代码直接打渠道/通知器, 后者=经 PushHub
    路由 (含 hub 线程池里的异步投递 —— 那种栈里没有 tests 帧, 靠 hub 帧兜住)。
    都不占 = 生产后台周期线程自发 (如 `app/push/health.py` 每小时的心跳), 只记录不报红。
    """
    try:
        import traceback
        paths = [f.filename for f in traceback.extract_stack()
                 if not f.filename.endswith("conftest.py")]
    except Exception:
        return "unknown"
    if any(f"{os.sep}tests{os.sep}" in p for p in paths):
        return "test"
    if any(p.endswith(os.path.join("app", "push", "hub.py")) for p in paths):
        return "hub"
    return "background"


# 会话级出站留痕账 (只在 conftest 进程内, 不落盘)
_OUTBOUND_LEAKS = []          # [(nodeid, 首条正文, 条数)] —— 测试驱动, 置红
_OUTBOUND_BACKGROUND = []     # 后台周期线程自发 (HealthMonitor 心跳等) —— 只报不红
_ALLOW_OUTBOUND_MARKER = "allow_outbound_notification"


def pytest_sessionfinish(session, exitstatus):
    if _OUTBOUND_LEAKS or _OUTBOUND_BACKGROUND:
        _report_outbound_traces(session)
    if _OUTBOUND_LEAKS:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
    _futu_thread_exit_bailout()


def _report_outbound_traces(session):
    """把"出口没封住"从"用户看 IM 才知道"变成"测试红" (明细打到终端摘要)。"""
    try:
        tr = session.config.pluginmanager.getplugin("terminalreporter")
        if tr is None:
            return
        tr.write_line("")
        tr.write_line("=" * 72)
        if _OUTBOUND_LEAKS:
            tr.write_line(f"🚨 出站通知泄漏: {len(_OUTBOUND_LEAKS)} 个用例触达了渠道 send 层")
            tr.write_line("   (渠道若未被 conftest 封住 = 已真外发到生产 IM)")
            for nodeid, sample, n in _OUTBOUND_LEAKS[:20]:
                tr.write_line(f"   · {nodeid}  ({n} 条) 首条: {sample!r}")
            if len(_OUTBOUND_LEAKS) > 20:
                tr.write_line(f"   · … 其余 {len(_OUTBOUND_LEAKS) - 20} 个同理")
            tr.write_line("   处置: 测试自身故意打渠道 → @pytest.mark.allow_outbound_notification;"
                          " 否则 = 有出口绕过封锁, 必须堵住而不是豁免")
        if _OUTBOUND_BACKGROUND:
            tr.write_line(f"ℹ️ 另 {len(_OUTBOUND_BACKGROUND)} 条为后台周期线程自发 (如 PushHub 心跳),"
                          f" 不计红; 首条: {_OUTBOUND_BACKGROUND[0][:100]!r}")
        tr.write_line("=" * 72)
    except Exception:
        pass


def _futu_thread_exit_bailout():
    """futu SDK 线程退出兜底 (2026-08-27 修正): 事后置 t._daemonic 对 3.8+ 无效 —
    threading._shutdown 按线程【启动时】注册的 _shutdown_locks 逐锁 acquire 等
    线程退出, 不复查 t.daemon (机制实证: 旧 hack 挂死/清锁干净退出)。
    清空 _shutdown_locks = 把已启动的非 daemon 线程事后 daemon 化: 解释器
    finalize 阶段照常击杀这些空闲循环线程, 正常退出路径不受影响。
    """
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


# ── LIVE 臂生产运维态文件隔离 (全测试面) ─────────────────────────────
# 2026-10-06 两次实证: ①10-05 运维落 HALT 止血 ⇒ 走 LIVE gate 的 14 条用例恒红
# (红因与代码无关); ②RFC-135 waiver 身份绑定上线后, tests/ 根目录的
# test_rfc080_half_flip_interlock.py 读到生产 waiver (auth id 不匹配合成配置) 转红。
# 同型病灶: 生产运维可写的态文件 (HALT/waiver) 不得泄漏进测试。fixture 原在
# tests/playbook/conftest.py (作用域限该目录), 根目录同型缺口上提到此单点收口 —
# 语义与默认值与原版逐字一致 (playbook 目录内层 fixture 已移除, 以此为准)。
import json as _json_live_iso
from datetime import timedelta as _td_live_iso

from app.utils.timezone import now_et as _now_et_live_iso


@pytest.fixture(autouse=True)
def _isolate_live_arm_halt_file(monkeypatch, tmp_path):
    """把 LIVE 臂 HALT 态文件指向空 tmp_path, 使测试与生产运维态解耦."""
    monkeypatch.setattr(
        "app.playbook.live_arm_gate._HALT_FILE",
        tmp_path / "live_arm_halt.json",
    )


@pytest.fixture(autouse=True)
def _isolate_live_probe_waiver_file(monkeypatch, tmp_path):
    """把四锁破例预注册载体指向 tmp_path, 并默认写成**有效**破例 (TEST-AUTH).

    防两坑: (a) 读生产载体 ⇒ 到期日 2026-10-12 后全场面转红 (硬编码日期定时炸弹
    同型); (b) 指向不存在文件 (fail-close) ⇒ 走 LIVE 臂的用例在下游断言炸,
    红因与用例意图无关。到期/缺失/坏文件语义由
    tests/playbook/test_live_probe_lock_waiver.py 显式覆盖 (那里自写文件)。
    到期日相对当前时间 +3650 天 ⇒ 永不过期。
    """
    waiver_path = tmp_path / "live_probe_lock_waiver.json"
    expires = (_now_et_live_iso() + _td_live_iso(days=3650)).isoformat(timespec="seconds")
    waiver_path.write_text(_json_live_iso.dumps({
        "waiver_id": "TEST-WAIVER",
        "authorization_id": "TEST-AUTH",
        "granted": "2026-10-06",
        "granted_by": "tests/conftest.py (测试夹具, 非生产授权)",
        "locks_waived": {"ALLOW_REAL_TRADING": "true"},
        "expires": expires,
    }, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr("app.playbook.live_arm_gate._WAIVER_FILE", waiver_path)
