#!/usr/bin/env python3
"""[P1 Step4 2026-09-12, proud-slope-falcon] DA/UE 离线全链驱动 (冻结材料 + stub LLM).

计划要求: "第一批先使用冻结材料和 stub LLM 跑完整 scan→select→generate→inject→execute,
不调用真实账户, 不发送通知"。本驱动覆盖 **UE scan → inject** 两站 (真实代码路径,
只把外部边界换成冻结替身), execute 站由 tests/playbook/test_offline_chain_execute.py
在隔离环境里消费本驱动写出的**真实 DB 行**。DA select 站走既有
tests/playbook/da_selection_driver.py (仓外脚本, 独立进程)。

隔离 (三重, 因为子进程里 tests/conftest.py 的生产 data/ 硬闸不生效):
  1. 全部埋点路径重定向到 --workdir (scan/midday jsonl、inject 留痕、额度锁、漏斗);
  2. 写守卫: builtins.open / Path.open / Path.write_text / os.open / sqlite3.connect
     对 workdir 之外的写一律抛错并计入 violations (含生产 DB 路径显式改道);
  3. socket / subprocess / os.system / futu 全部 forbidden — 断网断进程断真实券商。

用法:
  venv/bin/python tests/playbook/offline_chain_driver.py --workdir /tmp/chain --out /tmp/chain.json
"""
from __future__ import annotations

import argparse
import builtins
import copy
import importlib
import io
import json
import logging
import os
import socket
import sqlite3
import subprocess
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

ET = timezone(timedelta(hours=-4))
PRODUCTION_DB = str((ROOT / "data" / "ftseek_us.db").resolve())
LIVE_DB = str((ROOT / "data" / "ftseek_us_live.db").resolve())
# 证据文件写在 workdir 之外 (--out 由调用方指定) ⇒ 用**未加守卫**的原始 open,
#   守卫只约束被测代码, 不约束驱动器自己的产出。(Path.write_text 内部仍走
#   Path.open, 光存 write_text 的原始引用不够 — 第一轮就是这么被自己拦下的。)
_RAW: dict = {}
# 供应商 SDK 自己的日志目录: `import futu` 时 TimedRotatingFileHandler 立刻开写
#   ~/.com.futunn.FutuOpenD/Log/py_*.log。它不是本仓判分/审计载体, 但也不能被写守卫
#   误伤成"越界写" (否则整条链一步都跑不动)。只放行**这一目录**, 其余照拦。
VENDOR_LOG_ALLOW = (Path.home() / ".com.futunn.FutuOpenD" / "Log",)

# ── 冻结材料 (LLM 输出替身) ────────────────────────────────────────────────
#   设计意图 = 让漏斗每一格都有样本, 且**最高 confidence 的候选被资格门拒掉**
#   (TESTB 0.90 报价过期 / TESTG 0.95 无报价), 用来钉"先过滤后预算":
#   名额只在合格集合内发放, 被拒者不占额度也不回流, 由剩余合格队列补位。
FROZEN_SURFACED = [
    {"symbol": "TESTG", "direction": "LONG", "confidence": 0.95,
     "thesis": "frozen: no quote → UNKNOWN (缺数≠拒绝)",
     "evidence_ref": "ev-g",
     "causal_chain": "事实: chain-g; 传导假设: chain-g; 市场确认: chain-g",
     "invalidation": "invalid-g", "counter_evidence": "none-g",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
    {"symbol": "TESTB", "direction": "LONG", "confidence": 0.90,
     "thesis": "frozen: stale quote → REJECT",
     "evidence_ref": "ev-b",
     "causal_chain": "事实: chain-b; 传导假设: chain-b; 市场确认: chain-b",
     "invalidation": "invalid-b", "counter_evidence": "none-b",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
    {"symbol": "TESTA", "direction": "LONG", "confidence": 0.80,
     "thesis": "frozen: healthy → PASS",
     "evidence_ref": "ev-a",
     "causal_chain": "事实: chain-a; 传导假设: chain-a; 市场确认: chain-a",
     "invalidation": "invalid-a", "counter_evidence": "none-a",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
    {"symbol": "TESTC", "direction": "LONG", "confidence": 0.75,
     "thesis": "frozen: $3 below floor → REJECT",
     "evidence_ref": "ev-c",
     "causal_chain": "事实: chain-c; 传导假设: chain-c; 市场确认: chain-c",
     "invalidation": "invalid-c", "counter_evidence": "none-c",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
    {"symbol": "TESTD", "direction": "SHORT", "confidence": 0.70,
     "thesis": "frozen: ATR 0.5% → REJECT low_atr",
     "evidence_ref": "ev-d",
     "causal_chain": "事实: chain-d; 传导假设: chain-d; 市场确认: chain-d",
     "invalidation": "invalid-d", "counter_evidence": "none-d",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED",
     "short_check": {"provided": True, "borrow_feasible": True,
                     "borrow_data_basis": "UNKNOWN",
                     "downside_catalyst_expiry_hours": 24, "squeeze_risk": 2},
     "sector": "TEST"},
    {"symbol": "TESTE", "direction": "LONG", "confidence": 0.68,
     "thesis": "frozen: healthy → PASS",
     "evidence_ref": "ev-e",
     "causal_chain": "事实: chain-e; 传导假设: chain-e; 市场确认: chain-e",
     "invalidation": "invalid-e", "counter_evidence": "none-e",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
    {"symbol": "TESTF", "direction": "LONG", "confidence": 0.66,
     "thesis": "frozen: healthy but budget_cut",
     "evidence_ref": "ev-f",
     "causal_chain": "事实: chain-f; 传导假设: chain-f; 市场确认: chain-f",
     "invalidation": "invalid-f", "counter_evidence": "none-f",
     "confidence_basis": "THESIS_ONLY", "event_phase": "ONE_SHOT",
     "move_basis": "UNQUALIFIED", "sector": "TEST"},
]

# 冻结配置: 只改本链必需的旋钮, 阈值仍走活 yaml (load_candidate_policy 单一真源)。
FROZEN_CFG = {
    "universe_expansion.entry_enabled": True,       # 非 dry-run → 真写临时库
    "universe_expansion.min_confidence": 0.5,
    "universe_expansion.max_targets_per_day": 3,    # 名额绑定 (合格 4 > 3) → 验证补位/截断
    "universe_expansion.max_emergence_streak": 99,
    "universe_expansion.min_price_floor": 5,
    "universe_expansion.premarket_anchor": True,
    "universe_expansion.same_symbol_cooldown_days": 0,
    # 2026-10-08 修: 对齐生产 yaml (v2)。冻结在 v1 时 _normalize_surfaced 不透传
    # 契约字段 (invalidation/counter_evidence/...) ⇒ 7/7 结构性违约 → repair
    # 二次 LLM 调用 → "llm exactly once" 契约假红。
    "universe_expansion.prompt_version": "v2",
    "universe_expansion.midday_shadow_enabled": False,
    "universe_expansion.da_candidate_feed": False,
    "universe_expansion.inject_reject_log": True,
    "universe_expansion.short_boost_shadow": False,
}

# 每只标的的日线波动档 (决定筛选 ATR 比例): normal ≈ 4%, flat ≈ 0.5%
VOL = {"TESTA": 0.02, "TESTB": 0.02, "TESTC": 0.02, "TESTD": 0.0025,
       "TESTE": 0.02, "TESTF": 0.02, "TESTG": 0.02}
PRICE = {"TESTA": 100.0, "TESTB": 100.0, "TESTC": 3.0, "TESTD": 50.0,
         "TESTE": 80.0, "TESTF": 60.0, "TESTG": 70.0}
STALE = {"TESTB"}          # 报价过期 (source_asof = now-10min) → stale_quote
NO_QUOTE = {"TESTG"}       # 完全无报价 → price 缺失 → UNKNOWN


def bare(symbol: str) -> str:
    """冻结表按裸符号建键; 真实链路里筛选 ATR 用 canonical (US.X), 报价可能用裸符号。

    离线全链第一轮就是栽在这里: 假路由只认裸符号 ⇒ 所有标的都落到默认档
    (价 100 / 波 2%), 七只标的的 screening ATR 全等 4.03, 低ATR门形同未测。
    """
    return str(symbol or "").strip().upper().removeprefix("US.")


def daily_bars(symbol: str, n: int = 22) -> list:
    """n 根**已完成**日线, 末根 = asof 前一交易日 (周六跑批则到周五)。"""
    key = bare(symbol)
    vol = VOL.get(key, 0.02)
    close = PRICE.get(key, 100.0)
    day = date.today() - timedelta(days=1)
    days = []
    while len(days) < n:
        if day.weekday() < 5:
            days.append(day)
        day -= timedelta(days=1)
    out = []
    for d in reversed(days):
        out.append({"time": d.isoformat(), "open": close, "high": close * (1 + vol),
                    "low": close * (1 - vol), "close": close, "volume": 1_000_000})
        close *= 1.0005
    return out


class FrozenRouter:
    """伪造行情路由: 只有 get_batch_quotes / get_daily_bars / get_full_market_context。"""

    def __init__(self, now: datetime):
        self.now = now
        self.quote_calls: list = []
        self.bar_calls: list = []

    def _quote(self, symbol: str) -> dict:
        key = bare(symbol)
        px = PRICE.get(key)
        if key in NO_QUOTE or px is None:
            return {}
        asof = self.now - timedelta(minutes=10) if key in STALE else self.now
        return {"price": px, "last_price": px, "source": "frozen_stub",
                "source_asof": asof.isoformat(), "timestamp": asof.timestamp(),
                "bid_price": round(px * 0.999, 4), "ask_price": round(px * 1.001, 4),
                "day_high": px * 1.01, "day_low": px * 0.99, "volume_ratio": 1.5}

    def get_batch_quotes(self, symbols):
        self.quote_calls.append(list(symbols))
        return {s: self._quote(s) for s in symbols}

    def get_quote(self, symbol):
        return self._quote(symbol)

    def get_daily_bars(self, symbol, count=40):
        self.bar_calls.append(symbol)
        return daily_bars(symbol)[-int(count):]

    def get_full_market_context(self, symbol):
        q = self._quote(symbol)
        return {**q, "price": q.get("price"), "atr": (q.get("price") or 0) * 0.04}


def install_isolation(workdir: Path, violations: list, db_path: Path):
    """三重隔离: 写守卫 + 断网 + 断进程/真实券商。生产 DB 路径显式改道到临时库。"""
    workdir = workdir.resolve()

    def forbidden(kind):
        def block(*args, **kwargs):
            violations.append(kind)
            raise AssertionError(f"forbidden external boundary: {kind}")
        return block

    for name in ("connect", "connect_ex", "send", "sendall", "sendto", "sendmsg"):
        if hasattr(socket.socket, name):
            setattr(socket.socket, name, forbidden(f"socket.{name}"))
    socket.create_connection = forbidden("socket.create_connection")
    socket.getaddrinfo = forbidden("socket.getaddrinfo")
    subprocess.Popen = forbidden("subprocess.Popen")
    os.system = forbidden("os.system")

    def in_tmp(path) -> bool:
        try:
            return Path(os.fsdecode(path)).resolve().is_relative_to(workdir)
        except (TypeError, ValueError, OSError):
            return False

    def guard(resolved, how):
        try:
            probe = Path(os.fsdecode(resolved)).resolve()
            if any(probe.is_relative_to(allow) for allow in VENDOR_LOG_ALLOW):
                return
        except (TypeError, ValueError, OSError):
            pass
        if str(resolved) in (PRODUCTION_DB, LIVE_DB):
            violations.append(f"{how} → production db {resolved}")
            raise AssertionError(f"production DB write attempted via {how}: {resolved}")
        if not in_tmp(resolved):
            violations.append(f"{how} → {resolved}")
            raise AssertionError(f"write outside workdir via {how}: {resolved}")

    raw_open = builtins.open
    _RAW["open"] = raw_open

    def open_guarded(file, mode="r", *a, **kw):
        if any(c in mode for c in "wax+"):
            guard(file if isinstance(file, (str, os.PathLike)) else getattr(file, "name", file),
                  "open")
        return raw_open(file, mode, *a, **kw)

    builtins.open = open_guarded
    io.open = open_guarded
    raw_path_open = Path.open
    raw_write_text = Path.write_text

    def path_open_guarded(self, mode="r", *a, **kw):
        if any(c in mode for c in "wax+"):
            guard(self, "Path.open")
        return raw_path_open(self, mode, *a, **kw)

    def write_text_guarded(self, *a, **kw):
        guard(self, "Path.write_text")
        return raw_write_text(self, *a, **kw)

    Path.open = path_open_guarded
    Path.write_text = write_text_guarded
    raw_os_open = os.open

    def os_open_guarded(path, flags, *a, **kw):
        if flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND):
            guard(path, "os.open")
        return raw_os_open(path, flags, *a, **kw)

    os.open = os_open_guarded
    raw_connect = sqlite3.connect

    def connect_guarded(database, *a, **kw):
        name = os.fspath(database)
        if name in (PRODUCTION_DB, LIVE_DB):
            return raw_connect(str(db_path), *a, **kw)      # 显式改道 (不改语义)
        if name != ":memory:" and not in_tmp(name):
            guard(name, "sqlite3.connect")
        return raw_connect(database, *a, **kw)

    sqlite3.connect = connect_guarded
    sqlite3.dbapi2.connect = connect_guarded
    return raw_connect


def patch(name: str, value):
    """按点号路径替换属性 (记录原值以便还原)。"""
    mod_name, _, attr = name.rpartition(".")
    mod = __import__(mod_name, fromlist=[attr])
    old = getattr(mod, attr, None)
    setattr(mod, attr, value)
    return (mod, attr, old)


def run(workdir: Path, db_path: Path) -> dict:
    violations: list = []
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "data" / "shadow").mkdir(parents=True, exist_ok=True)
    # 相对路径埋点 (如 data/ue_short_boost_shadow.jsonl) 按 CWD 解析 ⇒ 把 CWD 挪进
    #   沙箱, 而不是放行它写生产 data/ (写守卫会拦, 但拦下来的是"链路跑不完")。
    os.chdir(workdir)

    notifications: list = []
    llm_calls: list = []
    now = datetime.now(ET)
    router = FrozenRouter(now)

    import scripts.llm_universe_scan as ue
    from app.core.atr_provider import ATRProvider
    from app.utils import daily_active_pool as daily_active_pool_module

    undo = []
    # ── 埋点路径全部改道 ──
    undo += [patch("scripts.llm_universe_scan._SCAN_PATH", workdir / "scan.jsonl"),
             patch("scripts.llm_universe_scan._MIDDAY_PATH", workdir / "midday.jsonl"),
             patch("scripts.llm_universe_scan._INJECT_REJECT_PATH",
                   workdir / "inject_log.jsonl"),
             patch("scripts.llm_universe_scan._INJECT_LOCK_PATH", workdir / "quota.lock"),
             # S0/tier 影子埋点同样是生产 data/shadow 载体 (不在 conftest 缝表里,
             #   既有单测靠 mock 掉整个函数规避) — 本链要**真跑**这些埋点, 故改道沙箱。
             patch("scripts.llm_universe_scan._DIRECTION_STRUCTURE_PATH",
                   workdir / "data" / "shadow" / "ue_direction_structure.jsonl"),
             patch("scripts.llm_universe_scan._TIER_GATE_SHADOW_PATH",
                   workdir / "data" / "shadow" / "ue_tier_gate_shadow.jsonl"),
             # [RFC-117] LONG 动量闸三态埋点 (conftest 缝表已登记, 但本驱动自带
             #   改道清单不读缝表 — 漏改道 = 真跑 inject 时直写生产 jsonl)。
             patch("scripts.llm_universe_scan._LONG_GATE_LOG_PATH",
                   workdir / "data" / "shadow" / "ue_long_momentum_gate.jsonl"),
             patch("app.playbook.candidate_eligibility.FUNNEL_PATH",
                   workdir / "candidate_funnel.jsonl"),
             # [RFC-125] 写库边界冲突影子 (conftest 缝表已登记, 本驱动清单独立维护
             #   — 漏改道 = 链内 save_instruction 触发冲突时直写生产 jsonl)。
             patch("app.playbook.conflict_shadow._CONFLICT_SHADOW_PATH",
                   workdir / "data" / "shadow" / "instruction_conflict.jsonl")]
    daily_active_pool_module._active_pool_instance = None
    original_pool_init = daily_active_pool_module.DailyActivePool.__init__

    def sandbox_pool_init(self, *args, **kwargs):
        original_pool_init(self, *args, **kwargs)
        self._manual_path = str(workdir / "data" / "daily_active_manual.json")
        self._pool_path = str(workdir / "data" / "daily_active_pool.json")

    daily_active_pool_module.DailyActivePool.__init__ = sandbox_pool_init
    undo.append((daily_active_pool_module.DailyActivePool, "__init__", original_pool_init))
    # app/data/rsshub_us_news.py 在 **import 时**执行 logging.basicConfig(
    #   handlers=[FileHandler(logs/rsshub_us.log)]) — 它会替换根 handler, 于是此后
    #   任何一条日志都追加进生产 logs/ (第一轮离线链就是被这个写守卫拦下的)。
    #   断法: basicConfig 变 no-op + 摘掉根上已存在的 FileHandler (只影响本进程)。
    undo.append(patch("logging.basicConfig", lambda *a, **kw: None))
    _root = logging.getLogger()
    _saved_handlers = list(_root.handlers)
    for _h in _saved_handlers:
        if isinstance(_h, logging.FileHandler):
            _root.removeHandler(_h)
    # ── 时间/交易日/时段 ──
    undo += [patch("scripts.llm_universe_scan._is_us_trading_day_today", lambda: True),
             patch("scripts.llm_universe_scan._is_us_premarket", lambda: False),
             patch("scripts.llm_universe_scan._cfg",
                   lambda k, d=None: FROZEN_CFG.get(k, d))]
    # ── 冻结材料 + stub LLM ──
    undo += [patch("scripts.llm_universe_scan._build_clue_brief",
                   lambda manual="": "FROZEN CLUE BRIEF (offline chain driver)"),
             patch("scripts.llm_universe_scan._gather_news", lambda hours=48: []),
             patch("scripts.llm_universe_scan._build_router", lambda: router),
             patch("scripts.llm_universe_scan._batch_quotes",
                   lambda r, syms: r.get_batch_quotes(syms))]
    # 先真实导入这些模块, patch() 才能按点号路径改到活对象 (而不是造一个假的替身)。
    for _mod in ("app.llm.model_call", "app.analysis.signal_ic_tracker",
                 "app.playbook.market_regime", "app.core.di_container",
                 "app.playbook.db", "app.notifier.feishu",
                 # rsshub 抓取器在 import 时就构造 FileHandler(logs/rsshub_us.log)
                 #   (构造即 open) ⇒ 必须在装写守卫**之前**导入完, 否则链路里任何
                 #   懒导入它的新闻路径都会当场被守卫拦死。
                 "app.data.rsshub_us_news"):
        importlib.import_module(_mod)

    # 隔离在导入之后装: `import futu` 自身要开供应商日志 (见 VENDOR_LOG_ALLOW)。
    install_isolation(workdir, violations, db_path)

    def fake_call_llm(*args, **kwargs):
        # 2026-10-08 修: call_llm 现存两种调用形 — (task, brief) 位置形与
        # (feature=..., prompt=...) 关键字形 (合同修复臂) — 存根须两形通吃,
        # 否则合同修复路径 TypeError 被观测模式吞掉, 扫描带病继续。
        _task = args[0] if args else kwargs.get("feature")
        _brief = args[1] if len(args) > 1 else kwargs.get("prompt")
        llm_calls.append({"task": _task, "brief_len": len(str(_brief)),
                          "max_tokens": kwargs.get("max_tokens"),
                          "json_mode": kwargs.get("json_mode")})
        return SimpleNamespace(success=True, model_used="frozen-stub-llm",
                               response={"surfaced": copy.deepcopy(FROZEN_SURFACED)})

    undo += [patch("app.llm.model_call.call_llm", fake_call_llm),
             # R5 候选快照写生产 signal_snapshots.db → 本链不落 (只验选股/指令链)
             patch("app.analysis.signal_ic_tracker.record_signal_snapshot",
                   lambda **kw: notifications.append(("ic_snapshot", kw.get("decision_id")))),
             patch("app.notifier.feishu.send_text",
                   lambda text: notifications.append(("feishu", str(text)[:120])) or True)]

    regime = SimpleNamespace(regime=SimpleNamespace(value="neutral"), vix=15.0,
                             spy_ma20=1.0, spy_ma50=1.0)
    undo.append(patch("app.playbook.market_regime.MarketRegimeGate",
                      lambda *a, **kw: SimpleNamespace(assess=lambda: regime)))
    atr_provider = ATRProvider(router)
    undo.append(patch("app.core.di_container.get_di_container",
                      lambda: SimpleNamespace(atr_provider=lambda: atr_provider)))

    # ── 真实 PlaybookDB 代码 + 临时 sqlite (走真 schema/真仲裁/真回读) ──
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    # 主库 trade_records 的两个真实读方都要能跑, 否则门只是"查询失败 fail-open/fail-close":
    #   ① inject 同标的冷却门 (symbol/direction/trade_date/source_type);
    #   ② auto_execute 的 R1b 已部署敞口预算门 (entry_price*quantity/source_type/exit_time)
    #      — 缺列时 R1b fail-close 把 _max_deploy 置 +inf, 执行站会全数"购买力上限"跳过
    #      (第一轮离线链实测就是这样, 修表后 3 笔才真走到委托)。
    #   零行 = 无在场持仓 (与"新开一批"的场景一致)。
    conn.execute("CREATE TABLE IF NOT EXISTS trade_records ("
                 "symbol TEXT, direction TEXT, trade_date TEXT, source_type TEXT, "
                 "entry_price REAL, quantity INTEGER, entry_time TEXT, exit_time TEXT)")
    conn.commit()
    # 2026-10-08 修: db._conn 现以 connection(db_name) 位置参调用 — 存根须两形兼容
    # (旧形 connection() 零参), 否则 ensure_tables 当场 TypeError。
    undo.append(patch("app.playbook.db.get_db",
                      lambda name: SimpleNamespace(connection=lambda *a, **kw: conn)))

    # ── SIM 身份前置条件 (计划 P0.2): 非 SIM 直接失败, 不降级 ──
    # 2026-10-08 修 (DEB-20261008-pipeline-refactor §9.2 前置债): 生产 .env.us 是
    # LIVE-capable (ALLOW_REAL_TRADING=true 探针授权态) — 本离线链的身份判定须读
    # workdir 冻结 SIM 副本, 而非生产 .env.us。只设子进程 env 不够:
    # read_startup_mode_snapshot 的**文件读取优先于 env** (app/core/trading_mode.py:59),
    # 故必须把 _ENV_FILE 改道到 workdir; 同时把三个身份键**显式置 SIM 值**
    # (不能只 pop — config 包的 load_dotenv(override=False) 会在 scan 深层
    # 懒导入时把生产 .env.us 的 ALLOW_REAL_TRADING=true 重新灌进 os.environ,
    # 第二个 require_auto_sim (cmd_inject) 又会红)。
    _env_frozen = workdir / ".env.us"
    _env_frozen.write_text(
        "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\nALLOW_REAL_TRADING=false\n",
        encoding="utf-8")
    os.environ["AUTO_TRADE_ACCOUNT"] = "sim"
    os.environ["TRADE_ENV"] = "1"
    os.environ["ALLOW_REAL_TRADING"] = "false"
    undo.append(patch("app.core.trading_mode._ENV_FILE", _env_frozen))
    from app.playbook.candidate_eligibility import require_auto_sim
    sim_mode = require_auto_sim()

    out = {"workdir": str(workdir), "db": str(db_path), "sim_mode": sim_mode,
           "llm_calls": llm_calls, "notifications": notifications,
           "violations": violations, "stages": {}}

    # ══ Stage 1: UE scan (冻结材料 + stub LLM) ══
    # force_window: 离线链设计=冻结材料+随时可跑, 不绑真实墙钟窗口
    # (2026-10-08 修: 修完 require_auto_sim 后暴露的时间依赖第二层)。
    rc_scan = ue.cmd_scan(force_window=True)
    scan_rows = [json.loads(x) for x in
                 (workdir / "scan.jsonl").read_text(encoding="utf-8").splitlines() if x.strip()]
    out["stages"]["ue_scan"] = {
        "rc": rc_scan,
        "records": [{"type": r.get("type"), "batch_status": r.get("batch_status"),
                     "scan_window": r.get("scan_window"), "batch_id": r.get("batch_id"),
                     "surfaced_n": len(r.get("surfaced") or []),
                     "scanned_at": r.get("scanned_at"),
                     "policy_version": r.get("policy_version")} for r in scan_rows],
    }

    # ══ Stage 2: UE inject (资格初筛 → 名额 → 补位 → 写库 → 写后确认) ══
    rc_inject = ue.cmd_inject()
    rows = [dict(r) for r in conn.execute(
        "SELECT * FROM playbook_instructions ORDER BY id").fetchall()]
    funnel = [json.loads(x) for x in
              (workdir / "candidate_funnel.jsonl").read_text(encoding="utf-8").splitlines()
              if x.strip()] if (workdir / "candidate_funnel.jsonl").exists() else []
    inject_log = [json.loads(x) for x in
                  (workdir / "inject_log.jsonl").read_text(encoding="utf-8").splitlines()
                  if x.strip()] if (workdir / "inject_log.jsonl").exists() else []
    out["stages"]["ue_inject"] = {
        "rc": rc_inject,
        "rows": [{"id": r["id"], "symbol": r["symbol"], "direction": r["direction"],
                  "entry_price": r["entry_price"], "stop_loss": r["stop_loss"],
                  "take_profit": r["take_profit"], "confidence": r["confidence"],
                  "skip_reason": r["skip_reason"], "decision_id": r["decision_id"],
                  "position_size": r["position_size"], "horizon": r["horizon"],
                  "raw_instruction_json": r["raw_instruction_json"]} for r in rows],
        "funnel": funnel,
        "inject_log": inject_log,
        "router_quote_calls": router.quote_calls,
        "router_bar_calls": sorted(set(router.bar_calls)),
    }
    out["llm_calls"] = llm_calls
    out["notifications"] = notifications
    out["violations"] = violations

    for mod, attr, old in reversed(undo):
        setattr(mod, attr, old)
    for _h in _saved_handlers:
        if isinstance(_h, logging.FileHandler) and _h not in _root.handlers:
            _root.addHandler(_h)
    conn.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--workdir", required=True)
    ap.add_argument("--out", required=True)
    args = ap.parse_args()
    workdir = Path(args.workdir).resolve()
    db_path = workdir / "chain.sqlite"
    result = run(workdir, db_path)
    with _RAW["open"](args.out, "w", encoding="utf-8") as fh:
        fh.write(json.dumps(result, ensure_ascii=False, indent=2, default=str))
    bad = result["violations"]
    print(json.dumps({"stages": {k: v.get("rc") for k, v in result["stages"].items()},
                      "rows": len(result["stages"]["ue_inject"]["rows"]),
                      "llm_calls": len(result["llm_calls"]),
                      "notifications": len(result["notifications"]),
                      "violations": bad}, ensure_ascii=False))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
