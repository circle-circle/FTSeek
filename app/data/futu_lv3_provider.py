"""
富途 LV3 美股实时行情提供商 (Futu LV3 US Quote Provider)

Phase 1 实现: 通过 Futu OpenD 获取美股实时行情 (LV3)
- Nasdaq Basic + Nasdaq TotalView + NYSE Arcabook (推广期免费)
- 实时报价 (毫秒级延迟, 替代 Finnhub 15 分钟延迟)
- 自动订阅/取消订阅管理
- 降级到 Finnhub (LV3 不可用时)

架构:
  Futu OpenD (同一实例, 港股+美股共享)
    → OpenQuoteContext (美股专用连接)
    → subscribe(QUOTE, K_1M) for watchlist
    → get_market_snapshot() for real-time prices

降级策略:
  Level 1: Futu LV3 实时行情 (主)
  Level 2: Finnhub HTTP API (备, 15 分钟延迟)
  Level 3: Alpha Vantage (终备)

使用:
  provider = FutuLV3Provider(host="127.0.0.1", port=11111)
  quote = provider.get_quote("US.NVDA")
  context = provider.get_full_market_context("US.NVDA")
"""
import atexit
import logging
import os
import socket
import threading
import time
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List

from futu import CurKlineHandlerBase

from app.utils.symbol_utils import to_dotted_class_share

logger = logging.getLogger("ftseek_us.futu_lv3")

# ── [RFC-171] 日K磁盘缓存 + 会话感知新鲜度 ────────────────────────────────
# 病灶 (2026-10-26 实测): 日K缓存只在进程内存 (24h TTL) ⇒ 进程重建后 09:0x 的
#   62 只候选批次必然全部冷启动 → 瞬间打满 OpenD 账户级 60次/30s 窗口 →
#   末位标的 ret=-1 / 配额等待 10s 超时 → Router 回退 TwelveData (更慢、口径不同)。
#   playbook 生成轮 09:01-09:06 的 TwelveData 回退即此路径 (日志 US.ARM ×2)。
# 修法: ①缓存落盘 (跨进程重启/多实例复用); ②新鲜度判据从"24h TTL"改成
#   "相位感知" — 盘中/盘前只需要到上一交易日的**已收盘**bar, 收盘后 (>=16:15 ET)
#   才要求含当日 bar。⇒ 盘前批次 0 次历史K请求。
_DAILY_DISK_PATH = (
    __file__.rsplit("/app/data/", 1)[0] + "/data/cache/daily_kline_v1.json"
)
_DAILY_DISK_FLUSH_SEC = 30.0     # 落盘节流 (62 只批次只写 2-3 次)
_DAILY_ACCEPT_HOLIDAY_SEC = 20 * 3600.0   # 假日错位: 20h 内拉过即信任
_DAILY_ACCEPT_POSTCLOSE_SEC = 15 * 60.0   # 收盘后刚拉过 → 信任 15min, 防重拉风暴


def _session_final_bar(last_bar_date: str, fetched_ts: float, now_et: datetime) -> bool:
    """该缓存条目是否为"已定稿的日线" (可跨进程落盘复用)。

    盘中拉到的当日 bar 是半截 bar (close=当时价)。若落盘并在次日沿用, ATR/MA/RSI 与
    prev_close 会整天吃一个错误收盘价 ⇒ 只有两类算定稿:
      ① 末端 bar 早于今日 (历史交易日已收盘, 不再变);
      ② 末端 bar = 今日 且 拉取时刻在今日 16:15 ET 之后 (收盘定稿后才拉的)。
    """
    try:
        last = datetime.strptime(str(last_bar_date)[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    if last < now_et.date():
        return True
    if last > now_et.date():
        return False
    close_ts = now_et.replace(hour=16, minute=15, second=0, microsecond=0).timestamp()
    return fetched_ts >= close_ts


def daily_cache_acceptable(last_bar_date: str, fetched_ts: float,
                           now_et: datetime, now_epoch: float) -> bool:
    """日K缓存是否可直接复用 (相位感知, [RFC-171])。

    - 盘前/盘中 (ET < 16:15): 只要求含**上一交易日**的已收盘 bar
      (假日导致缓存末端落后时, 20h 内拉过仍信任, 避免假日晨反复重拉);
    - 收盘后 (ET >= 16:15): 要求含**当日** bar (IC/影子回填需要当日行情);
      刚拉过但当日 bar 尚未发布时 15min 内不重复请求。
    - 硬底: 末端 bar 超过 10 天 → 一律不接受 (既有反幻觉守卫)。
    """
    if not last_bar_date or fetched_ts <= 0:
        return False
    try:
        last = datetime.strptime(str(last_bar_date)[:10], "%Y-%m-%d").date()
    except Exception:
        return False
    today = now_et.date()
    if (today - last).days > 10:
        return False
    age = now_epoch - fetched_ts
    if (now_et.hour, now_et.minute) >= (16, 15):
        # 收盘后: 必须是"收盘后拉取且含当日 bar"的数据。盘中拉到的当日 bar 是
        #   半截 bar (close=盘中价), 直接给 IC/影子回填会污染当日收益 ⇒ 必须重拉。
        close_ts = now_et.replace(hour=16, minute=15, second=0, microsecond=0).timestamp()
        if last >= today and fetched_ts >= close_ts:
            return True
        return age < _DAILY_ACCEPT_POSTCLOSE_SEC
    prev = today - timedelta(days=1)
    while prev.weekday() >= 5:          # 跳过周末 (假日由 20h 信任窗兜住)
        prev -= timedelta(days=1)
    if last >= prev:
        return True
    return age < _DAILY_ACCEPT_HOLIDAY_SEC


def silence_futu_sdk_logs() -> None:
    """SSOT 唯一入口: 静默 Futu SDK 的 DEBUG/INFO 日志 (2026-08-12 提取).

    背景: SDK 用进程级单例 FTLog (logger 名 'FTFileLog'/'FTConsoleLog'),
    默认 file_level=DEBUG 会把每个行情推送/协议包写盘并做昂贵的 findCaller
    堆栈回溯, 直接吃满回调线程 CPU + 日志膨胀。调高单例 file_level/
    console_level 到 WARNING 才能真正止血。
    (2026-08-12 SDK 10.9.6908 升级后实测: file_level property setter 机制
    与旧版一致, 本方法继续适用。)

    历史: 此前 3 处 inline 拷贝 (provider connect 时 / playbook / dashboard),
    provider 拷贝挂在连接事件上 → 盘后无 LV3 连接时抑制从未应用 →
    trade context keepalive (proto=1004 心跳) 持续写盘 (实测 28min 1566 行)。
    现统一调本函数; main.py import 本模块即触发模块级调用, 覆盖盘后场景。
    """
    try:
        import futu.common.ft_logger as _ft_logger
        _ft_logger.logger.file_level = logging.WARNING
        _ft_logger.logger.console_level = logging.WARNING
    except Exception as _e:
        logger.warning("[FutuLog] 调整 Futu SDK 日志级别失败: %s", _e, exc_info=True)
    _futu_logger = logging.getLogger("futu")
    _futu_logger.setLevel(logging.CRITICAL)
    _futu_logger.handlers = []
    _futu_logger.propagate = False


# import 即抑制: 任何 import 本模块的进程 (main / dashboard / cron 脚本)
# 在建立第一个 Futu 连接前即完成静默, 不再依赖"连接时"时机。
silence_futu_sdk_logs()


def enable_daemon_threads() -> None:
    """SSOT 唯一入口: 让 futu SDK 内部线程全部 daemon (2026-09-16 提取, 生产侧落点).

    与 `tests/conftest.py` 2026-08-27 那段是**同一根因**, 本函数把测试侧已验证的
    修法落到生产侧: SDK 的 `CallbackExecutor` 是**非 daemon** `threading.Thread`,
    永久阻塞在 `self._queue.get()`; 只要有一个 `OpenQuoteContext` 未 close, 解释器
    收尾时 `threading._shutdown()` 的 join 就**永不返回** ⇒ 进程干完活却不退出。

    实证 (2026-09-16): DA 盘前 pipeline 20:21 活已干完, 25 分钟不退出 → cron agent
    判为挂起 → SIGTERM(`PIPELINE_EXIT=143`) → **整条管线全量重跑** (白烧一倍 LLM
    预算); 2026-09-11 更留下存活 **2 天 16 小时**的孤儿进程 (持 `ftseek_us.db` 三
    句柄 + 一条 OpenD ESTAB)。daemon 化后, 这类"收尾拖住不退"在**任何 import 本
    模块的进程**里都不再可能 (main / dashboard / cron 脚本 / DA skill pipeline)。

    对主服务无行为变更: `_graceful_shutdown()` 第 4 步已 `data_router.close()` 释放
    LV3, 末尾 `os._exit(0)` 本就绕过 SDK 线程 —— 本函数只是让"暴力退出"不再是必需。
    注意: 线程的 daemon 标志在**建线程时**读取, 事后设置无效 ⇒ 必须早于本进程建立
    第一条 futu 连接, 故与 `silence_futu_sdk_logs()` 一样在模块级 import 即调用。
    """
    try:
        from futu.common.sys_config import SysConfig
        SysConfig.set_all_thread_daemon(True)
    except Exception as _e:
        logger.warning("[FutuThreads] futu 线程 daemon 化失败 (非致命): %s", _e, exc_info=True)


enable_daemon_threads()


def _configured_stock_sub_types() -> List[str]:
    """LV3 股票推送订阅类型 (单一可配置源, 默认精简集 QUOTE+K_1M).

    [ADR-013] K_5M/K_15M/K_30M/ORDER_BOOK/TICKER 无有效交易链路消费者:
      日线指标走 request_history_kline, 1分钟走 K_1M; K_15M 下单时 get_kline 按需 HTTP 拉取(不需订阅);
      TICKER/ORDER_BOOK 实测 100% 未被使用 (router 恒 N/A).
    过度订阅 → Futu SDK 对每条推送构造 DataFrame → 接收线程持续占满一核.
    如需恢复, 在 config data_sources.futu_lv3.stock_sub_types 追加类型即可.
    """
    try:
        from app.core.config_container import get_config
        v = get_config().get_dotted("data_sources.futu_lv3.stock_sub_types", None)
        if v:
            return list(v)
    except Exception:
        pass
    return ['QUOTE', 'K_1M']


def _configured_active_set_sub_types() -> List[str]:
    """动态活跃集 (指令/持仓/基准) 推送订阅类型 = 精简集 + ORDER_BOOK + TICKER.

    2026-08-10 (用户批准启用): 消费者已就位 — ORDER_BOOK 走微观结构快速路径
    (spread/深度实时化, 替代 300ms pull), TICKER 走 record-only 订单流证据链
    (order_flow_imbalance 恒 null → 真值, 攒样供 Phase2 裁决, 零行为变更);
    障碍均已清除: SDK DataFrame 性能单点 (接收线程 ~40% 核 → ~10%) 与
    ticker_direction 字符串映射 bug 已于同日修复。
    仅作用于动态活跃集 (subscription_manager.ensure_subscribed 默认路径);
    44 只扫描池仍走 _configured_stock_sub_types() 不受影响。
    回滚: data_sources.futu_lv3.active_set_orderflow_enabled=false + 重启。
    """
    base = _configured_stock_sub_types()
    try:
        from app.core.config_container import get_config
        enabled = bool(get_config().get_dotted(
            "data_sources.futu_lv3.active_set_orderflow_enabled", True))
    except Exception:
        enabled = True
    if not enabled:
        return base
    return base + [t for t in ('ORDER_BOOK', 'TICKER') if t not in base]


def _map_ticker_direction(raw) -> str:
    """逐笔方向归一: SDK 实发字符串枚举 (TickerDirect BUY/SELL/NEUTRAL),
    兼容历史 int 约定 (1=BUY 2=SELL 4=NEUTRAL), 其余一律 NEUTRAL.

    2026-08-10 修复: handler 曾拿字符串与 int 1/2 比较 → 恒 NEUTRAL;
    若 TICKER 订阅开启, buy/sell 量全 0 → buy_sell_ratio 兜底 2.0 →
    order_flow_imbalance 假多头 +0.333 注入择时上下文。

    DEB-20260903 #17: 本函数为方向映射 SSOT — get_recent_tickers (pull 路径)
    也必须调用本函数, 禁止另写内联映射 (两路径曾 1/2 互反 → buy/sell 反转)。
    """
    if raw in ("BUY", 1):
        return "BUY"
    if raw in ("SELL", 2):
        return "SELL"
    return "NEUTRAL"


def _futu_sub_type_map() -> dict:
    """DEB-20260903 #16: 订阅类型映射 SSOT — subscribe 主路径/重连重试/退订共用.

    原重连重试路径仅含 QUOTE/K_1M/K_15M/K_30M 四项, 丢失 ORDER_BOOK/TICKER/
    K_5M/K_60M/K_DAY → 断线恢复后盘口/逐笔推送静默丢失且标的已标记已订阅。
    """
    from futu import SubType
    return {
        'QUOTE': SubType.QUOTE,
        'K_1M': SubType.K_1M,
        'K_5M': SubType.K_5M,
        'K_15M': SubType.K_15M,
        'K_30M': SubType.K_30M,
        'K_60M': SubType.K_60M,
        'K_DAY': SubType.K_DAY,
        'ORDER_BOOK': SubType.ORDER_BOOK,  # 订单流深度 (Push Handler 已注册)
        'TICKER': SubType.TICKER,          # 逐笔成交 (Push Handler 已注册)
    }


# ── [DEB-20261002-input-validity P0-2 2026-10-02] 及时性: 从「自我声明」改为「真实观测」 ──
#   病灶: `delay_ms` 在三条取数路径上被硬编码成字面量 "<100"/"<10" (:966/:1008/:1063),
#   下游 `market_data_router.py:271` 渲染进日志、`:589` 落 `ts_ctx["lv3_delay_ms"]`
#   ⇒ 「及时性」这一层此前**零真实观测**, 只有未经核实的声明。全仓 grep 确认
#   `delay_ms` **无任何门消费方** (只渲染 + 落 ts_ctx, 且 `lv3_delay_ms` 不进 prompt)
#   ⇒ 改值是纯遥测, 零行为变更。
#   ⚠️ 语义区分 (勿混为一谈, 这正是本层此前的错):
#     · `delay_ms`     = 链路投递延迟 — 需要交易所**发送**时戳, 我方拿不到 ⇒ 不可测;
#     · `quote_age_ms` = 报价陈旧度 (交易所 `update_time` → now) — **可测**, 且这才是
#       SSOT 新鲜度门 (`candidate_eligibility._source_time` → `stale_quote`) 量的那个量。
#   处置: 不可测的诚实标 `unmeasured` (缺数据不编数据), 可测的**新增**字段承载 (additive)。
#   纪律: 时戳解析**复用 SSOT `_source_time`**, 绝不自写 — 影子
#   `ue_quote_freshness_shadow._age_sec` docstring 逐字写明「抄一份就是造第二个真源」,
#   因为 `_source_time` 里藏着 naive 串按 ET 解析的约定 (按本机 CST 解析会凭空多算 12h
#   ⇒ 全批假陈旧)。fail-open: 取不到/解析失败 → None, 不抛异常、不编数字。
def _measured_quote_age_ms(exchange_stamp: Any) -> Optional[int]:
    """交易所 `update_time` → 报价龄 (毫秒 int)。不可得/不可解析 ⇒ None (fail-open)。

    `now` 用 UTC-aware: `_source_time` 内部把 naive 串补成 ET-aware, 两个 aware 相减
    与所选时区无关 ⇒ 不必私有导入该模块的 `_ET` (降低耦合)。
    """
    if exchange_stamp in (None, ""):
        return None
    try:
        from datetime import timezone as _tz
        from app.playbook.candidate_eligibility import _source_time
        _, age = _source_time({"source_asof": exchange_stamp}, datetime.now(_tz.utc))
        if age is None:
            return None
        return int(round(age * 1000))
    except Exception:
        return None


def _push_exchange_stamp(row: Dict[str, Any]) -> str:
    """Futu push 行的交易所时戳: data_date + data_time → 单一 source_asof 串。"""
    data_date = str(row.get("data_date", "") or "").strip()
    data_time = str(row.get("data_time", "") or "").strip()
    if data_date and data_time:
        return f"{data_date} {data_time}"
    return str(row.get("update_time", "") or "")


# 2026-10-22 (P0 冻结族): K 线生病窗时长 — 时敏调用方在窗内跳过 HTTP 兜底。
_KLINE_SICK_WINDOW_SEC = 120.0


class FutuLV3Provider:
    """
    富途 LV3 美股实时行情提供商

    特性:
    - 共享已有 Futu OpenD 实例 (港股+美股)
    - 实时报价 (QUOTE) + 1分钟K线 (K_1M)
    - 自动订阅管理 (watchlist 自动订阅/取消)
    - 连接健康监控 + 自动重连
    - 降级到 Finnhub (当 Futu 不可用时)
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 11111,
        fallback_provider=None,
        cache_ttl_seconds: int = 5,
    ):
        """
        初始化富途 LV3 提供商

        Args:
            host: Futu OpenD 主机地址
            port: Futu OpenD 端口
            fallback_provider: 降级数据源 (FinnhubProvider 实例)
            cache_ttl_seconds: 缓存 TTL (秒), 默认 5 秒 (实时行情)
        """
        self.host = host
        self.port = port
        self.fallback = fallback_provider
        self.cache_ttl = cache_ttl_seconds
        self.enabled = False

        # 连接状态
        self._quote_ctx = None
        self._connected = False
        self._lock = threading.Lock()
        self._cache: Dict[str, Any] = {}
        self._cache_time: Dict[str, float] = {}

        # 健康统计
        self._success_count = 0
        self._fail_count = 0
        self._last_error: Optional[str] = None
        self._last_connect_time: Optional[float] = None

        # 订阅状态
        self._subscribed_symbols: set = set()
        # DEB-20260903 #16: 独立锁保护 _subscribed_symbols 读-改-写竞态
        # (subscribe 与 disconnect/close/force_reconnect 并发时可能重复订阅/漏订阅)
        self._sub_lock = threading.Lock()
        # 类型级订阅失败追踪: futu_symbol → 失败 sub_type 集 (下次 subscribe 自动补订)
        self._failed_sub_types: Dict[str, set] = {}

        # Push 行情缓存 (由 Handler 异步更新，扫描循环主动读取)
        self._push_quote_cache: Dict[str, Any] = {}  # symbol -> quote dict
        self._push_kline_cache: Dict[str, Any] = {}   # symbol -> latest kline
        self._push_ticker_cache: Dict[str, List] = {} # symbol -> recent tickers (ring buffer)
        # B4 (2026-09-05, DEB-20260905 §十三): 逐笔缓存的**到达时刻** (epoch, 单调钟) —
        # 与 orderbook 的 cached["timestamp"] 同一套 freshness gate 机制。用 time.time()
        # 而非 ticker["time"] 字符串, 是为了绕开 Futu 时间串的尺度/格式不确定性。
        self._push_ticker_seen_at: Dict[str, float] = {}  # symbol -> last push arrival epoch
        self._push_orderbook_cache: Dict[str, Any] = {}  # symbol -> latest orderbook
        self._push_cache_lock = threading.Lock()
        self._kline_handler = None
        self._push_kline_cache_lock = threading.Lock()

        self._push_quote_count: int = 0  # 统计接收到的 Push 报价数量
        self._push_ticker_count: int = 0
        self._push_orderbook_count: int = 0

        # Push Handler 异常观测: 原为 except:pass 静默吞错导致行情丢失不可诊断。
        # 累计计数 + 30s 节流日志, 既可见又不刷屏。
        self._push_error_count: int = 0
        self._last_push_error_log: float = 0.0

        # 延迟初始化 (首次调用时连接)
        self._initialized = False

        # K线请求限速: Futu OpenD 硬限制 60次/30秒 (2026-05-06 优化)
        self._kline_req_times: list = []  # 最近30秒内的请求时间戳
        self._kline_req_lock = threading.Lock()
        self._kline_max_per_30s = 60  # Futu 硬限制，不可突破
        
        # P0 修复: K线数据持久化缓存 + TTL，避免重复请求消耗配额
        self._kline_data_cache: dict = {}       # symbol -> {"data": [...], "ts": time.time()}
        self._kline_cache_lock = threading.Lock()
        self._kline_refresh_lock = threading.Lock()  # 每 provider 最多一个后台刷新, 不排队
        self._kline_sick_until = 0.0
        self._kline_cache_ttl = 60              # M6 (2026-06-03): 300→60s, K线缓存缩短到1分钟 (技术指标滞后从5min降到1min)
        # B4: 逐笔缓存 TTL = 300s, 对齐 ticker_analyzer.MEDIUM_WINDOW (中窗口 ~250 笔 ≈ 5min)。
        #   依据: analyze_tickers 的头注明确写了"Push 数据没有 Unix 时间戳, 用最近 N 笔近似"
        #   ⇒ 它**假定缓存是新鲜的**。缓存停更后 (断线/收盘/未订阅) 缓冲里仍是几小时前的
        #   500 笔, 旧实现 `if cached: return list(cached)` 会把它当实时数据返回, 于是
        #   ticker_velocity(笔/分钟) 与 buy_sell_ratio 全在描述一个远古窗口, 却被
        #   market_data_router 注入 ts_ctx 的 ticker_* 与 order_flow_imbalance = 信号污染。
        #   超过中窗口即视为不可用 ⇒ 返回 None, 走既有"无逐笔数据"降级分支 (不注入因子,
        #   不阻断下单)。
        self._ticker_cache_ttl = 300
        self._kline_symbol_cooldown: dict = {}  # symbol -> last_request_time (防同一标的重复请求)
        self._kline_symbol_cooldown_sec = 30    # 同一标的最小请求间隔30秒 (10→30 降低配额压力)

        # Daily K线缓存 (24hr TTL, 日线每天只变一次)
        self._daily_kline_cache: Dict[str, List[Dict]] = {}   # futu_symbol -> kline list
        self._daily_kline_cache_time: Dict[str, float] = {}   # futu_symbol -> cache timestamp
        # [RFC-171] 日K落盘 (跨进程重启复用; 盘前批次零历史K请求)
        self._daily_disk_loaded = False
        self._daily_disk_dirty = False
        self._daily_disk_last_flush = 0.0
        self._daily_disk_lock = threading.Lock()
        # 2026-06-30: 过期K线标记缓存 — 避免反复fallback到TwelveData导致429
        self._stale_kline_notified: Dict[str, float] = {}  # futu_symbol -> last_notified_ts

        # ── 连接健康监控 (2026-06-02 新增, Task #7) ──
        # 跟踪最后一次收到任意 push 的时间，用于检测 OpenD 断线 / 数据陈旧
        self._last_push_time: Optional[float] = None
        self._stale_threshold_sec: float = 30.0  # 数据超过 30s 未更新视为陈旧
        self._initial_grace_sec: float = 60.0    # 连接成功后给 60s 宽容期等待首批 push
        # 重连指数退避: 2,4,8,16,32,60 秒
        self._reconnect_attempts: int = 0
        self._last_reconnect_time: float = 0.0
        self._reconnect_min_backoff: float = 2.0
        self._reconnect_max_backoff: float = 60.0
        # 健康状态变化日志 + 断线统计
        self._last_health_state: Optional[bool] = None
        self._disconnect_start_time: Optional[float] = None
        self._total_disconnect_count: int = 0
        self._last_disconnect_duration_sec: float = 0.0

    def _is_us_market_open(self) -> bool:
        """判断当前是否为美股交易时段 (使用美东时间)

        美股交易: 美东 09:30-16:00，周一至周五（不含节假日）
        修复 (2026-05-09): 原用北京时间判断，周六凌晨 0-4 点被错误拦截，
        但此时对应美东周五下午，仍在交易时段内。
        修复 (2026-07-03): 接入 holiday_calendar SSOT，节假日返回 False。
        根因: 7/3 独立日补休，_is_us_market_open() 仍返回 True →
        watchdog 每 30s 判定不健康（无推送）→ 重连风暴 287 次。
        """
        import pytz
        from datetime import datetime

        ET = pytz.timezone("America/New_York")
        now_et = datetime.now(ET)
        hour = now_et.hour
        minute = now_et.minute
        weekday = now_et.weekday()

        # 周一至周五
        if weekday >= 5:
            return False

        # 节假日 (SSOT: holiday_calendar)
        from app.utils.holiday_calendar import get_holiday_calendar
        if get_holiday_calendar().is_us_holiday(now_et.date()):
            return False

        # 美东 09:30-16:00 (570-960 分钟)
        t = hour * 60 + minute
        return 570 <= t < 960

    def _is_us_pre_market(self) -> bool:
        """判断当前是否为美股盘前时段 (使用美东时间)

        盘前交易: 美东 04:00-09:30，周一至周五
        用于允许 _ensure_connected() 在盘前建立连接，
        避免系统重启后 subscribe 被阻塞导致 watchdog 无法启动。
        """
        import pytz
        from datetime import datetime

        ET = pytz.timezone("America/New_York")
        now_et = datetime.now(ET)
        hour = now_et.hour
        minute = now_et.minute
        weekday = now_et.weekday()

        # 周一至周五
        if weekday >= 5:
            return False

        # 美东 04:00-09:30 (240-570 分钟)
        t = hour * 60 + minute
        return 240 <= t < 570

    def _verify_connection_alive(self) -> bool:
        """轻量级 TCP 存活探测 — 验证 Futu OpenD 连接实际可用。

        根因: Futu SDK CallClose 后 _quote_ctx 对象仍在、_connected=True，
        但底层 TCP 已断。需要实际调用 API 验证。

        使用 get_global_state() — 最轻量的 Futu API 调用，
        仅查询 OpenD 内部状态，无网络 I/O 开销。
        """
        try:
            if self._quote_ctx is None:
                return False
            ret, data = self._quote_ctx.get_global_state()
            return ret == 0
        except Exception:
            return False

    def _start_health_watchdog(self):
        """启动后台健康守护线程 — 交易时段每 30s 自动检测断连并重连。

        根因: ensure_healthy() 仅被 position_monitor 调用，
        ORB 引擎等其他模块订阅时断连无人检测。
        独立守护线程确保任何断连都能被发现和修复。
        """
        if getattr(self, '_watchdog_started', False):
            return
        self._watchdog_started = True

        import threading

        def _watchdog_loop():
            while True:
                try:
                    time.sleep(30)
                    # DEB-20260904 #143: 盘前已连接/已订阅的会话纳入守护 —
                    # 原仅常规时段检查, 盘前断连要等 09:30 后才重连, 盘前锚定/信号
                    # 长时间用陈旧行情。盘前无 push 期待, is_healthy 只看连接标志
                    # 识别不了假活 → 用 TCP 探测判定; 盘前无会话时不主动建连 (不占槽位)。
                    if not self._is_us_market_open():
                        if not (self._is_us_pre_market() and self._connected
                                and self._subscribed_symbols):
                            continue
                        if self._verify_connection_alive():
                            continue
                        logger.warning("[FutuLV3][Watchdog] 盘前连接断开/假活，触发重连")
                        self.force_reconnect()
                        continue
                    # 只在有订阅时才检查（无订阅=无 push=正常）
                    if not self._subscribed_symbols:
                        continue
                    if not self.is_healthy(log_change=True):
                        logger.warning("[FutuLV3][Watchdog] 检测到不健康，触发重连")
                        self.force_reconnect()
                except Exception as e:
                    logger.debug(f"[FutuLV3][Watchdog] 异常: {e}")

        t = threading.Thread(target=_watchdog_loop, daemon=True, name="FutuLV3-HealthWatchdog")
        t.start()
        logger.info("[FutuLV3][Watchdog] 后台健康守护已启动 (30s 间隔)")

    def _ensure_connected(self) -> bool:
        """确保已连接到 Futu OpenD

        非美股交易时段拒绝连接，不消耗 OpenD 槽位。
        2026-06-17 修复: 盘前时段 (4:00-9:30 ET) 也允许连接，
        避免系统重启后 subscribe() 被阻塞导致 watchdog 无法启动。
        """
        if not self._is_us_market_open() and not self._is_us_pre_market():
            return False

        if self._connected and self._quote_ctx is not None:
            # 根因修复 (2026-06-05): 验证 TCP 连接实际存活
            # Futu SDK CallClose 后 _quote_ctx 对象仍在但不可用
            if self._verify_connection_alive():
                # 确保 watchdog 已启动（即使 subscribe 尚未调用）
                self._start_health_watchdog()
                return True
            # 假活状态 → 标记断连，走重连路径
            logger.warning("[FutuLV3] 连接假活 (对象存在但 TCP 已断)，触发重连")
            self._connected = False
            return self.force_reconnect()

        with self._lock:
            if not self._is_us_market_open() and not self._is_us_pre_market():
                return False
            if self._connected and self._quote_ctx is not None:
                if self._verify_connection_alive():
                    return True
                self._connected = False

            try:
                from futu import OpenQuoteContext, SysConfig, SubType

                # 2026-06-26 修复: 连接 OpenD 前先做端口探测, 不可达时快速失败,
                # 避免 OpenQuoteContext 在 OpenD 不可达时无限阻塞。
                sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                sock.settimeout(2.0)
                port_result = sock.connect_ex((self.host, self.port))
                sock.close()
                if port_result != 0:
                    logger.warning(f"[FutuLV3] OpenD {self.host}:{self.port} 端口不可达，跳过连接")
                    self._connected = False
                    return False

                # 禁用 Futu SDK 日志 (SSOT: 模块级已调用一次, 此处幂等再确认,
                # 覆盖本模块被绕过直接建连的边缘场景; 历史与原理见函数 docstring)
                silence_futu_sdk_logs()

                SysConfig.enable_proto_encrypt(False)

                logger.info(f"[FutuLV3] 正在连接 OpenD {self.host}:{self.port}...")
                self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)

                self._connected = True
                self._last_connect_time = time.time()
                self.enabled = True

                # 注册轻量 Push Handler (报价 + K 线累积)
                self._setup_push_handlers()

                # 启动健康守护线程 — 连接建立后立即运行
                # 即使 subscribe() 尚未调用（如 pre-market 重连场景）
                self._start_health_watchdog()

                # 验证 LV3 权限 (获取美股快照)
                test_ret, test_data = self._quote_ctx.get_market_snapshot(["US.AAPL"])
                if test_ret == 0 and test_data is not None and len(test_data) > 0:
                    price = test_data.iloc[0].get("last_price", 0)
                    logger.info(
                        f"[FutuLV3] ✅ 连接成功 | LV3 行情可用 | "
                        f"US.AAPL=${price:.2f} | 延迟 < 1s"
                    )
                else:
                    logger.warning(
                        f"[FutuLV3] ⚠️ 连接成功但 LV3 快照获取失败 (ret={test_ret}), "
                        f"可能无美股行情权限, 将使用降级模式"
                    )

                return True

            except ImportError:
                logger.error("[FutuLV3] ❌ futu SDK 未安装, 使用降级模式")
                self._last_error = "futu SDK not installed"
                return False
            except Exception as e:
                self._connected = False
                self._last_error = str(e)
                self._fail_count += 1
                logger.error(f"[FutuLV3] ❌ 连接失败: {e}, 使用降级模式")
                return False

    def _to_futu_symbol(self, symbol: str) -> str:
        """US.AAPL → US.AAPL (Futu 格式), 兼容无 US. 前缀。

        类股转点号: US.BRK-B → US.BRK.B。Futu OpenD 只认点号写法
        (实测 2026-09-18: US.BRK-B → ret=-1 未知股票; US.BRK.B → ret=0 快照/K线均通)。
        """
        s = symbol if symbol.startswith("US.") else f"US.{symbol}"
        return s[:3] + to_dotted_class_share(s[3:])

    def _strip_prefix(self, symbol: str) -> str:
        """US.AAPL → AAPL"""
        return symbol.replace("US.", "")

    def _record_push_error(self, handler: str, exc: Exception):
        """记录 Push Handler 异常 (累计计数 + 30s 节流日志), 替代原静默 except:pass。"""
        self._push_error_count += 1
        now = time.time()
        if now - self._last_push_error_log >= 30:
            self._last_push_error_log = now
            logger.debug(
                f"[FutuLV3] Push Handler[{handler}] 异常 (累计 {self._push_error_count} 次): "
                f"{type(exc).__name__}: {exc}"
            )

    def _setup_push_handlers(self):
        """
        设置轻量 Push Handler (2026-04-23 新增)

        Handler 只做一件事：收到推送后更新本地缓存。
        不做任何策略计算/交易逻辑，确保回调线程毫秒级返回。
        """
        from futu import StockQuoteHandlerBase, CurKlineHandlerBase, TickerHandlerBase, OrderBookHandlerBase

        provider = self  # 闭包引用

        class PushQuoteHandler(StockQuoteHandlerBase):
            """轻量报价 Handler — 只更新缓存"""
            def on_recv_rsp(self, rsp_pb):
                try:
                    # 2026-08-10 性能: parse_rsp_pb 直接返回 dict 列表, 跳过 SDK 的
                    # pd.DataFrame 构造 (占 push 线程 CPU ~75%) 与 iterrows (~12%).
                    ret, data = StockQuoteHandlerBase.parse_rsp_pb(rsp_pb)
                    if ret == 0 and data:
                        updated = 0
                        with provider._push_cache_lock:
                            for row in data:
                                sym = row.get("code", "")
                                provider._push_quote_cache[sym] = {
                                    "price": row.get("last_price", 0),
                                    "open": row.get("open_price", 0),
                                    "high": row.get("high_price", 0),
                                    "low": row.get("low_price", 0),
                                    "prev_close": row.get("prev_close_price", 0),
                                    "volume": row.get("volume", 0),
                                    "turnover": row.get("turnover", 0),
                                    # 2026-06-26: StockQuote push 不含 bid/ask, 用 last_price 兜底
                                    # (与 snapshot 路径 line 743-744 一致, 避免 health check 误拦)
                                    "bid": float(row.get("bid_price", 0) or 0) or float(row.get("last_price", 0) or 0),
                                    "ask": float(row.get("ask_price", 0) or 0) or float(row.get("last_price", 0) or 0),
                                    "bid_vol": row.get("bid_vol", 0),
                                    "ask_vol": row.get("ask_vol", 0),
                                    # DEB-20260903 #18: 标记 bid/ask 是否来自 last_price 兜底 —
                                    # 真实 bid/ask 均缺失时上两行退化为 last_price, 下游
                                    # get_order_book_quality 若直接采用会得 spread=0/liquidity=100
                                    "book_is_proxy": not (
                                        float(row.get("bid_price", 0) or 0) > 0
                                        and float(row.get("ask_price", 0) or 0) > 0
                                    ),
                                    "update_time": _push_exchange_stamp(row),
                                    "timestamp": time.time(),
                                }
                                updated += 1
                        if updated > 0:
                            provider._push_quote_count += updated
                            provider._last_push_time = time.time()
                            if provider._push_quote_count <= len(data):  # 首次接收
                                syms = [r.get("code", "") for r in data][:5]
                                logger.info(f"[FutuLV3] ✅ Push 数据到达: {len(data)} 条, 首批: {syms}")
                    return ret, data
                except Exception as e:
                    provider._record_push_error("quote", e)
                return -1, None

        class PushKlineHandler(CurKlineHandlerBase):
            """轻量 K 线 Handler — 累积 K 线列表 (修复：之前存单条 dict 导致 get_kline 永远走 HTTP)"""
            def on_recv_rsp(self, rsp_pb):
                try:
                    # 2026-08-10 性能: parse_rsp_pb 直接返回 dict 列表, 跳过 DataFrame 构造.
                    ret, data = CurKlineHandlerBase.parse_rsp_pb(rsp_pb)
                    if ret == 0 and data:
                        with provider._push_cache_lock:
                            for row in data:
                                sym = row.get("code", "")
                                kline_entry = {
                                    "time_key": row.get("time_key", ""),
                                    "open": float(row.get("open", 0)),
                                    "high": float(row.get("high", 0)),
                                    "low": float(row.get("low", 0)),
                                    "close": float(row.get("close", 0)),
                                    "volume": float(row.get("volume", 0)),
                                    "timestamp": time.time(),
                                }
                                # 累积到列表，而非覆盖单条
                                if sym not in provider._push_kline_cache:
                                    provider._push_kline_cache[sym] = []
                                cache_list = provider._push_kline_cache[sym]
                                # 如果最新 bar 时间相同，更新；否则追加
                                if cache_list and cache_list[-1].get("time_key", "") == kline_entry["time_key"]:
                                    cache_list[-1] = kline_entry
                                else:
                                    cache_list.append(kline_entry)
                                    # 保持合理大小 (最近 300 条 ≈ 5 小时 1M K 线)
                                    if len(cache_list) > 300:
                                        provider._push_kline_cache[sym] = cache_list[-300:]
                            provider._last_push_time = time.time()
                    return ret, data
                except Exception as e:
                    provider._record_push_error("kline", e)
                return -1, None

        class PushTickerHandler(TickerHandlerBase):
            """轻量逐笔 Handler — 累积最近 ticker 用于量能分析"""
            MAX_TICKERS_PER_SYMBOL = 500  # 最近 500 笔 ≈ 几分钟高频数据

            def on_recv_rsp(self, rsp_pb):
                try:
                    # 2026-08-10 性能: parse_rsp_pb 直接返回 dict 列表, 跳过 DataFrame 构造.
                    ret, data = TickerHandlerBase.parse_rsp_pb(rsp_pb)
                    if ret == 0 and data:
                        with provider._push_cache_lock:
                            for row in data:
                                sym = row.get("code", "")
                                ticker_entry = {
                                    "price": float(row.get("price", 0)),
                                    "volume": float(row.get("volume", 0)),
                                    "turnover": float(row.get("turnover", 0)),
                                    "direction": _map_ticker_direction(row.get("ticker_direction")),
                                    "time": row.get("time", ""),
                                    "sequence": row.get("sequence", 0),
                                }
                                if sym not in provider._push_ticker_cache:
                                    provider._push_ticker_cache[sym] = []
                                buf = provider._push_ticker_cache[sym]
                                buf.append(ticker_entry)
                                # Ring buffer: 只保留最近 MAX_TICKERS_PER_SYMBOL 条
                                if len(buf) > self.MAX_TICKERS_PER_SYMBOL:
                                    provider._push_ticker_cache[sym] = buf[-self.MAX_TICKERS_PER_SYMBOL:]
                                provider._push_ticker_seen_at[sym] = time.time()
                        provider._push_ticker_count += len(data)
                        provider._last_push_time = time.time()
                        if provider._push_ticker_count <= len(data):  # 首次接收
                            syms = [r.get("code", "") for r in data][:5]
                            logger.info(f"[FutuLV3] ✅ Ticker 数据到达: {len(data)} 笔, 首批: {syms}")
                    return ret, data
                except Exception as e:
                    provider._record_push_error("ticker", e)
                return -1, None

        class PushOrderBookHandler(OrderBookHandlerBase):
            """轻量买卖盘 Handler — 更新最新 orderbook 快照"""

            def on_recv_rsp(self, rsp_pb):
                try:
                    # 2026-08-10 修复: SDK 该基类本就返回 dict (无 DataFrame),
                    # 旧代码按 DataFrame 用 (.empty/.iterrows) 每次推送必抛 AttributeError,
                    # 推送缓存从未写入 → 消费端永远降级 pull. 现按 dict 解析最优买卖一档.
                    # unpack 格式: {'code', 'Bid': [(price, vol, order_cnt, detail)], 'Ask': [...]}
                    ret, data = OrderBookHandlerBase.parse_rsp_pb(rsp_pb)
                    if ret == 0 and data:
                        sym = data.get("code", "")
                        bids = data.get("Bid") or []
                        asks = data.get("Ask") or []
                        best_bid = bids[0] if bids else (0, 0, 0, {})
                        best_ask = asks[0] if asks else (0, 0, 0, {})
                        with provider._push_cache_lock:
                            provider._push_orderbook_cache[sym] = {
                                "BidPrice": float(best_bid[0]),
                                "BidVol": float(best_bid[1]),
                                "AskPrice": float(best_ask[0]),
                                "AskVol": float(best_ask[1]),
                                "BidNum": float(len(bids)),
                                "AskNum": float(len(asks)),
                                "BidOrderNum": float(best_bid[2]),
                                "AskOrderNum": float(best_ask[2]),
                                "timestamp": time.time(),
                            }
                        provider._push_orderbook_count += 1
                        provider._last_push_time = time.time()
                        if provider._push_orderbook_count <= 1:  # 首次接收
                            logger.info(f"[FutuLV3] ✅ OrderBook 数据到达: 首批: ['{sym}']")
                    return ret, data
                except Exception as e:
                    provider._record_push_error("orderbook", e)
                return -1, None

        self._push_quote_handler = PushQuoteHandler()
        self._push_kline_handler = PushKlineHandler()
        self._push_ticker_handler = PushTickerHandler()
        self._push_orderbook_handler = PushOrderBookHandler()
        self._quote_ctx.set_handler(self._push_quote_handler)
        self._quote_ctx.set_handler(self._push_kline_handler)
        self._quote_ctx.set_handler(self._push_ticker_handler)
        self._quote_ctx.set_handler(self._push_orderbook_handler)
        logger.info("[FutuLV3] ✅ Push Handler 已注册 (Quote+Kline+Ticker+OrderBook)")

    def get_push_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 Push 缓存获取实时报价 (2026-04-23 新增)

        由 Push Handler 异步更新，延迟 <100ms。
        如果缓存过期或不存在，返回 None (调用者应 fallback 到 snapshot)。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_quote_cache.get(futu_symbol)
            if cached and (time.time() - cached.get("timestamp", 0)) < self.cache_ttl:
                return cached
        return None

    def get_push_tickers(self, symbol: str) -> Optional[List[Dict]]:
        """
        从 Push 缓存获取最近逐笔成交 (2026-05-20 新增)

        由 PushTickerHandler 异步更新，实时推送延迟 <100ms。
        返回最近 500 笔逐笔数据，供量能分析因子使用。

        B4 freshness gate (2026-09-05): 缓存停更超过 `_ticker_cache_ttl` (300s) 返回 None。
        与同类 `get_push_orderbook` (:707) 的 `(time.time() - cached["timestamp"]) < cache_ttl`
        对齐 — 原实现两个方法一个有闸一个没有, 是同一个类里的自相矛盾。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_ticker_cache.get(futu_symbol)
            if cached:
                age = time.time() - self._push_ticker_seen_at.get(futu_symbol, 0)
                if age < self._ticker_cache_ttl:
                    return list(cached)  # 返回副本防止外部修改
                logger.debug(
                    f"[FutuLV3] {futu_symbol} 逐笔缓存已停更 {age:.0f}s "
                    f"(> {self._ticker_cache_ttl}s TTL), 按无数据处理 (不注入 ticker_* 因子)"
                )
        return None

    def get_push_orderbook(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        从 Push 缓存获取最新买卖盘 (2026-05-20 新增)

        由 PushOrderBookHandler 异步更新，实时推送延迟 <100ms。
        返回最新一档 + 订单数信息，供深度分析因子使用。
        """
        futu_symbol = self._to_futu_symbol(symbol)
        with self._push_cache_lock:
            cached = self._push_orderbook_cache.get(futu_symbol)
            if cached and (time.time() - cached.get("timestamp", 0)) < self.cache_ttl:
                return dict(cached)  # 返回副本
        return None

    def subscribe(self, symbols: List[str], sub_types: List[str] = None) -> bool:
        """
        订阅美股实时行情 (Push 模式)

        Args:
            symbols: 股票代码列表 (US.AAPL 格式)
            sub_types: 订阅类型列表 (默认 ['QUOTE', 'K_1M'])

        Returns:
            是否成功
        """
        if not self._ensure_connected():
            return False

        # 竞态修复 (2026-06-25): 捕获 _quote_ctx 局部引用，防止 watchdog 的 force_reconnect
        # 在 _ensure_connected() 返回后、subscribe 调用前将 _quote_ctx 置 None → NoneType 报错
        quote_ctx = self._quote_ctx
        if quote_ctx is None:
            return False

        futu_symbols = [self._to_futu_symbol(s) for s in symbols]
        # DEB-20260903 #16: 读-过滤在锁内 (与 unsubscribe/clear 竞态)
        with self._sub_lock:
            new_symbols = [s for s in futu_symbols if s not in self._subscribed_symbols]
            # 已订阅但有类型级失败记录的标的 → 本次补订其失败类型
            retry_symbols = [s for s in futu_symbols
                             if s not in new_symbols and self._failed_sub_types.get(s)]

        if not new_symbols and not retry_symbols:
            logger.debug(f"[FutuLV3] 已订阅 {len(futu_symbols)} 只美股, 无需重复订阅")
            return True

        try:
            from futu import SubType

            # 默认订阅类型 ([ADR-013] 改为可配置精简集, 默认 QUOTE+K_1M; 覆盖重连恢复路径)
            if sub_types is None:
                sub_types = _configured_stock_sub_types()

            # DEB-20260903 #16: 复用模块级 SSOT 映射 (与重试/退订路径一致)
            sub_type_map = _futu_sub_type_map()

            # DEB-20260903 #16: 按类型记录成功/失败 — 原实现部分/全部失败仍
            # 无条件标记已订阅并返回 True, 造成假订阅状态 (无推送但显示健康)
            failed_this_round: Dict[str, set] = {}
            any_success = False
            for st_name in sub_types:
                sub_type = sub_type_map.get(st_name)
                if sub_type is None:
                    logger.warning(f"[FutuLV3] 未知订阅类型: {st_name}")
                    continue
                # 新标的全类型订阅; 老标的仅补订其失败类型
                targets = list(new_symbols)
                for _rs in retry_symbols:
                    if st_name in self._failed_sub_types.get(_rs, set()):
                        targets.append(_rs)
                if not targets:
                    continue
                ret, data = quote_ctx.subscribe(targets, [sub_type], subscribe_push=True)
                if ret != 0:
                    logger.warning(
                        f"[FutuLV3] 订阅 {st_name} 部分失败: {data}, "
                        f"symbols={targets} — 已记录待补订"
                    )
                    for _s in targets:
                        failed_this_round.setdefault(_s, set()).add(st_name)
                else:
                    any_success = True
                    logger.info(
                        f"[FutuLV3] ✅ 已订阅 {len(targets)} 只美股 {st_name}: "
                        f"{[self._strip_prefix(s) for s in targets]}"
                    )

            with self._sub_lock:
                # 仅当至少一个类型成功才标记已订阅; 全失败 → 不标记, 下轮完整重试
                if any_success:
                    self._subscribed_symbols.update(new_symbols)
                for _s in list(new_symbols) + list(retry_symbols):
                    _fails = failed_this_round.get(_s, set())
                    if _fails:
                        self._failed_sub_types[_s] = set(_fails)
                    else:
                        self._failed_sub_types.pop(_s, None)  # 全部成功, 清除失败记录
            logger.info(
                f"[FutuLV3] 总订阅: {len(self._subscribed_symbols)} 只美股"
            )
            # 全部类型失败 → False (上层可感知降级); 部分失败 → True + 失败类型待补订
            return any_success

        except Exception as e:
            # 根因修复 (2026-06-05): 订阅失败时自动重连并重试
            # 典型场景: Futu SDK CallClose 后 _quote_ctx 假活
            logger.error(f"[FutuLV3] 订阅失败: {e} — 尝试重连后重试", exc_info=True)
            if self.force_reconnect():
                try:
                    # 重连后 _subscribed_symbols 已清空，全部视为 new
                    futu_symbols_retry = [self._to_futu_symbol(s) for s in symbols]
                    # DEB-20260903 #16: 复用完整 SSOT 映射 (原仅 QUOTE/K_1M/K_15M/K_30M 四项,
                    # 丢失 ORDER_BOOK/TICKER/K_5M/K_60M/K_DAY → 重连后盘口/逐笔推送静默丢失)
                    retry_map = _futu_sub_type_map()
                    retry_failed: Dict[str, set] = {}
                    retry_any_ok = False
                    for st_name in (sub_types or _configured_stock_sub_types()):
                        _sub = retry_map.get(st_name)
                        if _sub is None or self._quote_ctx is None:
                            continue
                        _r, _d = self._quote_ctx.subscribe(futu_symbols_retry, [_sub], subscribe_push=True)
                        if _r != 0:
                            logger.warning(f"[FutuLV3] 重连重订阅 {st_name} 失败: {_d}")
                            for _s in futu_symbols_retry:
                                retry_failed.setdefault(_s, set()).add(st_name)
                        else:
                            retry_any_ok = True
                    with self._sub_lock:
                        if retry_any_ok:
                            self._subscribed_symbols.update(futu_symbols_retry)
                        for _s in futu_symbols_retry:
                            if retry_failed.get(_s):
                                self._failed_sub_types[_s] = set(retry_failed[_s])
                            else:
                                self._failed_sub_types.pop(_s, None)
                    if not retry_any_ok:
                        logger.error("[FutuLV3] 重连后重订阅全部类型失败")
                        return False
                    logger.info(f"[FutuLV3] ✅ 重连后重订阅成功: {len(futu_symbols_retry)} 只")
                    return True
                except Exception as e2:
                    logger.error(f"[FutuLV3] 重连后重订阅仍失败: {e2}")
            return False

    # ── 期权代码检测 ────────────────────────────────────────────────
    @staticmethod
    def _is_option_code(symbol: str) -> bool:
        """检测是否为期权合约代码
        
        格式: US.XXX26XXXXP/C (标的 + 日期YYMMDD + P/C + 行权价)
        例如: US.QQQ260602P710000, US.SPY260602P730000
        """
        s = symbol.replace("US.", "") if symbol.startswith("US.") else symbol
        # 期权代码特征: 包含6位日期(26/27/28开头) + P或C + 数字
        if len(s) > 10 and any(x in s for x in ['26', '27', '28', '29', '30']):
            for i, c in enumerate(s):
                if c in ('P', 'C') and i > 6 and s[i+1:].isdigit():
                    return True
        return False

    def get_quote(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取实时报价 (LV3)

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            {
                "price": float,          # 最新价
                "open": float,           # 开盘价
                "high": float,           # 最高价
                "low": float,            # 最低价
                "prev_close": float,     # 昨收价
                "volume": float,         # 成交量
                "turnover": float,       # 成交额
                "change": float,         # 涨跌额
                "change_percent": float, # 涨跌幅 (%)
                "bid": float,            # 买价
                "ask": float,            # 卖价
                "bid_vol": float,        # 买量
                "ask_vol": float,        # 卖量
                "data_source": str,      # "futu_lv3" | "finnhub" | "alphavantage"
                "timestamp": float,      # Unix 时间戳
            }
        """
        # 2026-06-11: 跳过非美股标的，避免无效 Futu 订阅和错误日志
        if (not symbol.startswith('US.') or symbol.count('.') > 1) and not self._is_option_code(symbol):
            return None

        # P0 修复 (2026-05-21): 期权代码仅通过 Futu 快照获取，跳过 Finnhub/TwelveData
        is_option = self._is_option_code(symbol)
        futu_symbol = self._to_futu_symbol(symbol)

        # 1. Push 缓存检查 (实时推送，延迟 <100ms)
        push_data = self.get_push_quote(symbol)
        if push_data:
            # DEB-20260904 #144: 返回副本 — 原原地改共享缓存 dict, 各调用者互相污染;
            # 并补齐契约字段: change/change_percent 与 subscribed/snapshot 路径同口径,
            # pre_price/pre_change_rate push 通道无此数据, 置 0 (与快照路径缺省口径一致)
            quote = dict(push_data)
            prev_close = float(quote.get("prev_close") or 0)
            price = float(quote.get("price") or 0)
            change = price - prev_close
            quote["change"] = change
            quote["change_percent"] = (change / prev_close * 100) if prev_close > 0 else 0.0
            quote.setdefault("pre_price", 0.0)        # push 无盘前字段, 占位对齐契约
            quote.setdefault("pre_change_rate", 0.0)  # 同上
            quote["data_source"] = "futu_lv3_push"
            # [P0-2] 原 "<100" 是**自我声明**非观测值; 链路投递延迟我方不可测 ⇒ 诚实标注。
            #   及时性改由下一行 additive 字段 `quote_age_ms` 承载 (可测且是门量的量)。
            quote["delay_ms"] = "unmeasured"
            quote["quote_age_ms"] = _measured_quote_age_ms(quote.get("update_time"))
            quote["symbol"] = symbol
            return quote

        # 2. 旧缓存检查 (快照模式 fallback)
        cache_key = f"quote:{futu_symbol}"
        if self._is_cache_valid(cache_key):
            return self._cache[cache_key]

        # 2.5 订阅报价读取 (2026-07-16 核心修复): get_stock_quote 读 OpenD 本地订阅缓存。
        #     实时 (由 QUOTE/TICKER push 持续维护), 且不做服务端拉取 → 不消耗 get_market_snapshot
        #     的频率配额。仅对已订阅标的可用 (监控池 32 只 + 持仓均已订阅)。
        #     根因: 原来 push 缓存 (5s TTL) 一 miss 就落 rate-limited snapshot; 止损/止盈/入场
        #     高频取价打爆 snapshot 配额 → 级联降级到 TD (429)。实时价是核心场景, 不能靠兜底。
        if futu_symbol in self._subscribed_symbols and self._ensure_connected():
            try:
                ret, data = self._quote_ctx.get_stock_quote([futu_symbol])
                if ret == 0 and data is not None and len(data) > 0:
                    row = data.iloc[0]
                    last_price = row.get("last_price", 0)
                    if last_price > 0:
                        prev_close = row.get("prev_close_price", last_price) or last_price
                        change = last_price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close > 0 else 0
                        result = {
                            "price": float(last_price),
                            "open": float(row.get("open_price", last_price) or last_price),
                            "high": float(row.get("high_price", last_price) or last_price),
                            "low": float(row.get("low_price", last_price) or last_price),
                            "prev_close": float(prev_close),
                            "volume": float(row.get("volume", 0) or 0),
                            "turnover": float(row.get("turnover", 0) or 0),
                            "change": float(change),
                            "change_percent": float(change_pct),
                            # get_stock_quote 不含盘口, 用 last_price 兜底 (与 push handler L403-404 一致)
                            "bid": float(last_price),
                            "ask": float(last_price),
                            "bid_vol": 0.0,
                            "ask_vol": 0.0,
                            "data_source": "futu_lv3_subscribed",
                            "update_time": str(row.get("update_time", "") or ""),
                            "timestamp": time.time(),
                            # [P0-2] 原 "<100" 是自我声明; 投递延迟不可测 ⇒ 诚实标注,
                            #   及时性由 additive `quote_age_ms` 承载 (真观测, fail-open)。
                            "delay_ms": "unmeasured",
                            "quote_age_ms": _measured_quote_age_ms(row.get("update_time")),
                        }
                        self._cache[cache_key] = result
                        self._cache_time[cache_key] = time.time()
                        self._success_count += 1
                        return result
            except Exception as e:
                logger.debug(f"[FutuLV3] {symbol} get_stock_quote 失败, 尝试 snapshot: {e}")

        # 3. 尝试 Futu LV3 快照
        if self._ensure_connected():
            try:
                ret, data = self._quote_ctx.get_market_snapshot([futu_symbol])
                if ret == 0 and data is not None and len(data) > 0:
                    row = data.iloc[0]
                    last_price = row.get("last_price", 0)

                    if last_price > 0:
                        prev_close = row.get("prev_close_price", last_price)
                        open_price = row.get("open_price", last_price)
                        high = row.get("high_price", last_price)
                        low = row.get("low_price", last_price)
                        volume = row.get("volume", 0)
                        turnover = row.get("turnover", 0)
                        bid = row.get("bid_price", 0)
                        ask = row.get("ask_price", 0)
                        bid_vol = row.get("bid_vol", 0)
                        ask_vol = row.get("ask_vol", 0)

                        change = last_price - prev_close
                        change_pct = (change / prev_close * 100) if prev_close > 0 else 0

                        result = {
                            "price": float(last_price),
                            "open": float(open_price),
                            "high": float(high),
                            "low": float(low),
                            "prev_close": float(prev_close),
                            "volume": float(volume),
                            "turnover": float(turnover),
                            "change": float(change),
                            "change_percent": float(change_pct),
                            "bid": float(bid) if bid > 0 else float(last_price),
                            "ask": float(ask) if ask > 0 else float(last_price),
                            "bid_vol": float(bid_vol),
                            "ask_vol": float(ask_vol),
                            # 2026-08-25 (DEB-ue-audit r5): 单只快照路径补盘前字段,
                            #   与批量快照路径 (get_batch_quote, 8/11 修复) 同源同口径。
                            #   此前漏补 → 批量失败逐只降级后盘前锚定门拿不到 pre_price,
                            #   UE inject 盘前仍全判"无现价"。加法式补字段, 现有消费方零感知。
                            "pre_price": float(row.get("pre_price", 0) or 0),
                            "pre_change_rate": float(row.get("pre_change_rate", 0) or 0),
                            "data_source": "futu_lv3",
                            "update_time": str(row.get("update_time", "") or ""),
                            "timestamp": time.time(),
                            # [P0-2] 原 "<10" + 注释「LV3 延迟 < 10ms」是**未核实的声明**
                            #   (无任何观测支撑); 投递延迟不可测 ⇒ 诚实标注, 及时性由
                            #   additive `quote_age_ms` 承载 (真观测, fail-open → None)。
                            "delay_ms": "unmeasured",
                            "quote_age_ms": _measured_quote_age_ms(row.get("update_time")),
                        }

                        self._cache[cache_key] = result
                        self._cache_time[cache_key] = time.time()
                        self._success_count += 1

                        return result
                    else:
                        logger.debug(
                            f"[FutuLV3] {symbol} 快照返回 price=0, 可能非交易时段"
                        )

            except Exception as e:
                self._fail_count += 1
                self._last_error = str(e)
                logger.debug(f"[FutuLV3] 获取 {symbol} 快照失败: {e}")

        # P0 修复 (2026-05-21): 期权代码不走 Finnhub/TwelveData 降级（它们不支持期权格式）
        if is_option:
            logger.debug(f"[FutuLV3] 期权代码 {symbol} Futu 快照失败，跳过降级源")
            return None

        # 降级: 使用 fallback provider
        if self.fallback:
            try:
                quote = self.fallback.get_quote(symbol)
                if quote and quote.get("price", 0) > 0:
                    quote["data_source"] = "finnhub_fallback"
                    # B4 (2026-07-20): 不再覆写 timestamp — finnhub C3 修复已回传上游成交时间,
                    #   覆写为抓取时间会把 15min 延迟报价"洗白"成新鲜 → 新鲜度门失效. 仅缺失时兜底.
                    if not quote.get("timestamp"):
                        quote["timestamp"] = time.time()
                    self._cache[cache_key] = quote
                    self._cache_time[cache_key] = time.time()
                    logger.info(
                        f"[FutuLV3] 🔄 降级: {symbol} 从 Finnhub 获取报价 "
                        f"${quote['price']:.2f}"
                    )
                    return quote
            except Exception as e:
                logger.warning(f"[FutuLV3] Finnhub 降级也失败: {e}")

        logger.error(f"[FutuLV3] ❌ {symbol} 所有数据源均失败")
        return None

    def get_full_market_context(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取完整市场上下文 (兼容 MarketDataRouter 接口)

        Args:
            symbol: 股票代码 (US.AAPL 格式)

        Returns:
            市场上下文字典
        """
        quote = self.get_quote(symbol)
        if not quote or quote.get("price", 0) <= 0:
            return None

        # 构建与 MarketDataRouter 兼容的上下文
        context = {
            "price": quote["price"],
            "open": quote.get("open", quote["price"]),
            "high": quote.get("high", quote["price"]),
            "low": quote.get("low", quote["price"]),
            "prev_close": quote.get("prev_close", quote["price"]),
            "volume": quote.get("volume", 0),
            "change_percent": quote.get("change_percent", 0),
            "change": quote.get("change", 0),
            "bid": quote.get("bid", quote["price"]),
            "ask": quote.get("ask", quote["price"]),
            "data_source": quote.get("data_source", "futu_lv3"),
            "timestamp": quote.get("timestamp", time.time()),
        }

        return context

    def get_batch_quote(self, symbols: List[str]) -> Dict[str, Dict[str, Any]]:
        """
        批量获取报价 (2026-04-23 升级: Push 缓存优先)

        策略:
        1. 先从 Push 缓存读取所有有实时数据的标的
        2. 仅对缺失/过期的标的调用快照
        3. 合并返回

        Args:
            symbols: 股票代码列表 (US.AAPL 格式)

        Returns:
            {symbol: quote_dict}
        """
        results = {}
        missing = []

        # 1. Push 缓存检查 (实时推送)
        for sym in symbols:
            push_data = self.get_push_quote(sym)
            if push_data:
                futu_sym = self._to_futu_symbol(sym)
                push_data["data_source"] = "futu_lv3_push"
                push_data["symbol"] = sym
                results[futu_sym] = push_data
            else:
                missing.append(self._to_futu_symbol(sym))

        if not missing:
            return results  # 全部从 Push 缓存获取

        # 2. 对缺失的标的调用快照
        if not self._ensure_connected():
            return results

        try:
            ret, data = self._quote_ctx.get_market_snapshot(missing)
            snapshot_results = {}

            if ret == 0 and data is not None and len(data) > 0:
                for _, row in data.iterrows():
                    symbol_code = row.get("code", "")
                    last_price = row.get("last_price", 0)

                    if last_price > 0:
                        prev_close = row.get("prev_close_price", last_price)
                        change_pct = (
                            (last_price - prev_close) / prev_close * 100
                            if prev_close > 0 else 0
                        )

                        snapshot_results[symbol_code] = {
                            "price": float(last_price),
                            "open": float(row.get("open_price", last_price)),
                            "high": float(row.get("high_price", last_price)),
                            "low": float(row.get("low_price", last_price)),
                            "prev_close": float(prev_close),
                            "volume": float(row.get("volume", 0)),
                            "turnover": float(row.get("turnover", 0)),
                            "change_percent": float(change_pct),
                            # 2026-08-11 (LIF/UE 同款盘前盲点修复): 加法式补盘前字段 —
                            #   盘前时段 last_price=昨收, 真实可交易价在 pre_price。
                            #   现有读 price/last_price 的调用方零感知; 消费方按需读取。
                            "pre_price": float(row.get("pre_price", 0) or 0),
                            "pre_change_rate": float(row.get("pre_change_rate", 0) or 0),
                            "data_source": "futu_lv3",
                            "update_time": str(row.get("update_time", "") or ""),
                            "timestamp": time.time(),
                        }

                self._success_count += 1
                logger.debug(
                    f"[FutuLV3] 批量快照: {len(snapshot_results)}/{len(missing)} 成功, "
                    f"Push 缓存: {len(results)} 只"
                )
                results.update(snapshot_results)
                return results
            else:
                # 2026-08-25 根因修复 (DEB-ue-audit r5): 原实现在此直接返回 Push 缓存,
                #   但 Futu get_market_snapshot 对"批内任一标的不支持"整批拒绝 (ret=-1):
                #   8/25 21:10 UE inject 批内 2 只 OTC 票 (NUMD/LGMK, "暂不提供美股 OTC 市场行情")
                #   毒死整批 8 只 → 主板票失去 LV3 pre_price → 盘前锚定门全判"无现价" → 0 条注入。
                #   修复: 不再提前返回, 落入下方逐只降级 (与 except 路径同语义),
                #   毒票只杀自己, 主板票逐只快照仍带 pre_price。
                logger.warning(
                    f"[FutuLV3] 批量快照失败: ret={ret}, msg={getattr(data, 'msg', None)} "
                    f"→ 逐只降级 (防单票毒死整批)"
                )

        except Exception as e:
            self._fail_count += 1
            self._last_error = str(e)
            logger.error(f"[FutuLV3] 批量快照异常: {e}")

        # 降级: 逐个获取 (get_quote 内部会自动检查 Push 缓存)
        for sym in symbols:
            q = self.get_quote(sym)
            if q:
                results[self._to_futu_symbol(sym)] = q
        return results

    def _wait_kline_quota(self, symbol: str = None) -> bool:
        """
        非阻塞 K 线配额检查 (P0 修复 - 2026-05-06)
        
        双重门控: 1) 全局配额 60次/30s  2) 单标的冷却 10s
        调用方应处理 False 返回值（使用缓存数据）。
        """
        now = time.time()
        
        # ① 单标的冷却: 同一标的10秒内不重复请求
        if symbol:
            with self._kline_req_lock:
                last = self._kline_symbol_cooldown.get(symbol, 0)
                if now - last < self._kline_symbol_cooldown_sec:
                    return False  # 冷却中
        
        # ② 全局配额检查
        with self._kline_req_lock:
            self._kline_req_times = [t for t in self._kline_req_times if t > (now - 30.0)]
            if len(self._kline_req_times) >= self._kline_max_per_30s:
                return False  # 配额已满
            
            # ③ 同时记录
            self._kline_req_times.append(now)
            if symbol:
                self._kline_symbol_cooldown[symbol] = now
                # 清理过期的冷却记录
                expired = [s for s, t in self._kline_symbol_cooldown.items() if now - t > 60]
                for s in expired:
                    del self._kline_symbol_cooldown[s]
            return True

    def kline_sick(self) -> bool:
        """K 线失败后 120s 内不启动后台刷新; 普通调用仍可探测恢复。"""
        return time.time() < self._kline_sick_until

    def _refresh_kline_async(self, symbol: str, ktype: str, count: int, autype: str):
        """非阻塞抢占一个刷新槽; SDK 卡住时不再创建线程或排队。"""
        if self.kline_sick() or not self._kline_refresh_lock.acquire(blocking=False):
            return

        def refresh():
            try:
                if not self.kline_sick():
                    self.get_kline(symbol, ktype=ktype, count=count, autype=autype)
            except Exception:
                self._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC
                # W4 纪律 (2026-10-23): 后台刷新失败必须 warning 可见, debug 会在
                # INFO 级别下隐形 = 「影子停采」与「链路断了」不可区分。
                logger.warning("[FutuLV3] %s K 线后台刷新异常", symbol, exc_info=True)
            finally:
                self._kline_refresh_lock.release()

        try:
            threading.Thread(target=refresh, name="FutuKlineRefresh", daemon=True).start()
        except Exception:
            self._kline_refresh_lock.release()
            logger.warning("[FutuLV3] %s K 线后台刷新启动失败", symbol, exc_info=True)

    def _daily_disk_load(self) -> None:
        """[RFC-171] 一次性把落盘日K装载进内存缓存 (失败静默, 退化为纯内存行为)。"""
        if self._daily_disk_loaded:
            return
        with self._daily_disk_lock:
            if self._daily_disk_loaded:
                return
            self._daily_disk_loaded = True
            try:
                import json as _json
                with open(_DAILY_DISK_PATH, "r", encoding="utf-8") as fh:
                    payload = _json.load(fh)
            except FileNotFoundError:
                return
            except Exception as exc:
                logger.warning(f"[FutuLV3·DailyK] 落盘缓存读取失败 (忽略): {exc}", exc_info=True)
                return
            loaded = 0
            for sym, entry in (payload.get("symbols") or {}).items():
                bars = entry.get("bars") or []
                ts = float(entry.get("fetched_at") or 0)
                if not bars or ts <= 0 or sym in self._daily_kline_cache:
                    continue
                # 半截当日 bar 不装载 (旧文件可能残留; 见 _session_final_bar 注释)
                from app.utils.timezone import now_et as _now_et_ld
                if not _session_final_bar(str(bars[-1].get("time", ""))[:10],
                                          ts, _now_et_ld()):
                    continue
                self._daily_kline_cache[sym] = bars
                self._daily_kline_cache_time[sym] = ts
                loaded += 1
            if loaded:
                logger.info(f"[FutuLV3·DailyK] 落盘日K缓存装载 {loaded} 只 "
                            f"({_DAILY_DISK_PATH})")

    def _daily_disk_save(self, force: bool = False) -> None:
        """[RFC-171] 节流落盘 (30s); 原子写 (tmp+rename), 失败不影响取数。"""
        now = time.time()
        if not force and (now - self._daily_disk_last_flush) < _DAILY_DISK_FLUSH_SEC:
            return
        with self._daily_disk_lock:
            if not self._daily_disk_dirty and not force:
                return
            self._daily_disk_last_flush = now
            self._daily_disk_dirty = False
            try:
                import json as _json
                import os as _os
                # 合并写 (2026-10-27 实测: MarketDataRouter 每个实例自建 FutuLV3Provider,
                #   多个实例共享同一落盘文件 ⇒ 直接覆盖会互相丢条目)。按 symbol 取
                #   fetched_at 较新者, 保证只在"本实例数据更新"时才替换。
                merged = {}
                try:
                    with open(_DAILY_DISK_PATH, "r", encoding="utf-8") as fh:
                        merged = dict((_json.load(fh) or {}).get("symbols") or {})
                except FileNotFoundError:
                    merged = {}
                except Exception:
                    merged = {}
                for sym, bars in self._daily_kline_cache.items():
                    if not bars:
                        continue
                    mine_ts = self._daily_kline_cache_time.get(sym, 0.0)
                    # 半截当日 bar 不落盘 (否则次日整天沿用错误收盘价)
                    from app.utils.timezone import now_et as _now_et_sv
                    if not _session_final_bar(str(bars[-1].get("time", ""))[:10],
                                              mine_ts, _now_et_sv()):
                        continue
                    prev = merged.get(sym) or {}
                    if float(prev.get("fetched_at") or 0) > mine_ts:
                        continue
                    merged[sym] = {"bars": bars, "fetched_at": mine_ts}
                payload = {"version": 1, "symbols": merged}
                _os.makedirs(_os.path.dirname(_DAILY_DISK_PATH), exist_ok=True)
                tmp = _DAILY_DISK_PATH + ".tmp"
                with open(tmp, "w", encoding="utf-8") as fh:
                    _json.dump(payload, fh, ensure_ascii=False, separators=(",", ":"))
                _os.replace(tmp, _DAILY_DISK_PATH)
            except Exception as exc:
                logger.warning(f"[FutuLV3·DailyK] 落盘缓存写入失败 (忽略): {exc}", exc_info=True)

    def _acquire_daily_kline_quota(self, symbol_key: str, max_wait: float = 30.0) -> bool:
        """日K配额门闸 (2026-08-27 ret=-1根因修): 复用 _wait_kline_quota 的全局60/30s窗口+单标的冷却.

        get_kline (分钟K) 走非阻塞拒绝, 但日线主要消费方是 EOD 批量回填 (ic_tracker/shadow_tracker/
        factor_analyzer) — 有界等待比拒绝更合理: 让富途新数据在批次内自然流完, 而不是成批溢到
        TwelveData 打爆免费版 8次/分钟 → 10分钟熔断 → "所有数据源均失败".
        symbol_key 带 "D::" 前缀, 与分钟K冷却分键空间, 避免日线拉取把同标的分钟K挡进30s冷却.
        """
        deadline = time.time() + max_wait
        while True:
            if self._wait_kline_quota(symbol_key):
                return True
            if time.time() >= deadline:
                return False
            time.sleep(0.5)

    def _get_cached_kline(self, symbol: str, *, blocking: bool = True) -> Optional[list]:
        """从持久化缓存获取K线数据 (不消耗配额)"""
        futu_sym = self._to_futu_symbol(symbol)
        now = time.time()
        if not self._kline_cache_lock.acquire(blocking=blocking):
            return None
        try:
            entry = self._kline_data_cache.get(futu_sym)
            if entry and (now - entry["ts"]) < self._kline_cache_ttl:
                return entry["data"]
        finally:
            self._kline_cache_lock.release()
        return None

    def _set_cached_kline(self, symbol: str, data: list):
        """将K线数据写入持久化缓存"""
        futu_sym = self._to_futu_symbol(symbol)
        with self._kline_cache_lock:
            self._kline_data_cache[futu_sym] = {"data": data, "ts": time.time()}

    def get_kline(self, symbol: str, ktype: str = "K_1M", count: int = 100,
                  autype: str = "QFQ", latency_sensitive: bool = False) -> Optional[List[Dict]]:
        """
        获取 K 线数据 (P0 修复 - 2026-05-06)

        三级降级:
          1. 持久化缓存 (60s TTL, 不消耗配额)
          2. Push 缓存 (由 PushKlineHandler 累积 K 线列表，实时更新)
          3. HTTP 请求 (配额允许时)

        2026-07-28 (DEB-bar-ruler F2): autype 显式化 — 缺省 QFQ (前复权, 与旧隐式行为
        一致, 技术指标消费方零变化); bar 遥测等与原始成交价对比的消费方传 "NONE"。
        注: NONE 请求与 QFQ 缓存分 key 存储, 避免口径串缓存。

        latency_sensitive=True 只读缓存并尝试单 worker 后台刷新, 不在调用线程连接或请求 SDK。
        """
        if not latency_sensitive and not self._ensure_connected():
            self._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC
            return None

        futu_symbol = self._to_futu_symbol(symbol)
        # 缓存 key 必须含 ktype (2026-10-17 审计修复): 原 key 只有 symbol/autype ⇒
        # 60s 内 K_DAY 请求会命中 K_1M 缓存 (反之亦然), 技术指标静默拿到错周期序列。
        # 非缺省复权口径同样分 key (防 QFQ/NONE 串缓存)。
        _cache_sym = f"{symbol}|{autype}|{ktype}"

        # ① 优先读持久化缓存 (60s TTL) — 按口径+周期 key
        cached = self._get_cached_kline(_cache_sym, blocking=not latency_sensitive)
        if cached and len(cached) >= min(count, 10):
            return cached[-count:]

        # ② Push ring 仅含 QFQ 的 1M bar, 不得供其它周期/复权使用。
        push_data = None
        if autype == "QFQ" and ktype == "K_1M":
            if self._push_kline_cache_lock.acquire(blocking=not latency_sensitive):
                try:
                    push_data = self._push_kline_cache.get(futu_symbol)
                    if push_data:
                        push_data = push_data[-count:]
                finally:
                    self._push_kline_cache_lock.release()
                if push_data and len(push_data) >= min(count, 10):
                    if not latency_sensitive:
                        self._set_cached_kline(_cache_sym, push_data)
                    return push_data

        # 首次连接/SDK 请求也移出时敏线程; 生病或刷新槽忙时不排队。
        if latency_sensitive:
            self._refresh_kline_async(symbol, ktype, count, autype)
            return cached[-count:] if cached else push_data

        # ③ 配额允许才发 HTTP 请求
        if not self._wait_kline_quota(symbol):
            # 配额满/冷却中 → 返回已有缓存(哪怕不足)
            all_cached = self._get_cached_kline(_cache_sym)
            if all_cached:
                return all_cached[-count:]
            return None

        try:
            from futu import KLType

            kl_type_map = {
                "K_1M": KLType.K_1M,
                "K_3M": KLType.K_3M,
                "K_5M": KLType.K_5M,
                "K_15M": KLType.K_15M,
                "K_30M": KLType.K_30M,
                "K_60M": KLType.K_60M,
                "K_DAY": KLType.K_DAY,
            }

            kl_type = kl_type_map.get(ktype, KLType.K_1M)

            # P0 fix (2026-06-09): 指定 start 防止 OpenD 返回去年数据
            # P0 fix (2026-06-17): 放宽至 T-1 交易日, 解决开盘前 30 分钟 K 线不足 30 条
            #   根因: start=today 在 09:30-10:00 只能获取 ≤30 条 1min K 线,
            #   导致技术指标无法计算 → quote_only → IC 死锁
            from datetime import datetime as _dk, timedelta as _td
            import pytz as _pz
            _now_et = _dk.now(_pz.timezone('US/Eastern'))
            today_et = _now_et.strftime("%Y-%m-%d")
            # 计算前一交易日: Mon→Fri(-3), Tue-Fri→前一日(-1)
            _weekday = _now_et.weekday()  # 0=Mon, 6=Sun
            _back_days = 3 if _weekday == 0 else (2 if _weekday == 6 else 1)
            prev_trading_day = (_now_et - _td(days=_back_days)).strftime("%Y-%m-%d")

            # 2026-06-30 根因修复: max_count太小 → OpenD返回范围内最旧的N根(2025年)
            # max_count要 >= 范围内交易日总数, 否则只拿最旧的。320覆盖~1.5年交易日。
            _end_date = _now_et.strftime("%Y-%m-%d")
            _start_date = (_now_et - _td(days=400)).strftime("%Y-%m-%d")
            hist_kwargs = {
                "code": futu_symbol,
                "ktype": kl_type,
                "max_count": max(count + 10, 320) if ktype == "K_DAY" else count,
                "start": _start_date,
                "end": _end_date,
            }
            # 2026-07-28 (F2): autype 显式透传 (缺省 QFQ = futu 默认, 旧行为零变化)
            try:
                from futu import AuType as _AuT
                hist_kwargs["autype"] = {"NONE": _AuT.NONE, "HFQ": _AuT.HFQ}.get(autype, _AuT.QFQ)
            except Exception:
                pass
            # 日内 K 线: start=T-1 获取前一交易日尾盘 K 线补充缓冲区, end=today 防止未来数据
            if ktype != "K_DAY":
                hist_kwargs["start"] = prev_trading_day
                hist_kwargs["end"] = today_et

            ret, data, _err = self._quote_ctx.request_history_kline(**hist_kwargs)

            if ret == 0 and data is not None and len(data) > 0:
                klines = []
                for _, row in data.iterrows():
                    klines.append({
                        "time": str(row.get("time_key", "")),
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0)),
                        "turnover": float(row.get("turnover", 0)),
                    })
                # 日期过滤:
                # 日内 K 线: start=T-1 会包含前一交易日尾盘, 保留全部用于技术指标计算
                #   (ORB 引擎有独立的 today 校验; 技术指标需要 ≥30 条连续 K 线)
                # 日 K 线: end=today 已由请求参数约束, 不再按当日过滤 (2026-10-17
                #   修复: 原实现把 400 天日线砍到 0-1 根, pre_market_ranker 等
                #   get_kline(K_DAY) 消费方恒拿 None); 按升序取最近 count 根。
                if ktype == "K_DAY":
                    klines = [k for k in klines if k["time"]][-count:]
                elif klines:
                    # 日内 K 线: 仅排除非当日和非前一交易日的异常旧数据
                    _valid_prefixes = (today_et, prev_trading_day)
                    klines = [k for k in klines
                              if k["time"][:10] in _valid_prefixes]
                if not klines:
                    logger.warning(f"[FutuLV3] {symbol} history kline 无当日数据 (today={today_et}, ktype={ktype}), 返回 None")
                    return None
                # Push ring 只收 QFQ K_1M; 持久化缓存按 symbol|autype|ktype 分 key。
                if autype == "QFQ" and ktype == "K_1M":
                    with self._push_kline_cache_lock:
                        self._push_kline_cache[futu_symbol] = klines
                self._set_cached_kline(_cache_sym, klines)
                self._kline_sick_until = 0.0  # 2026-10-22: 成功即出生病窗
                return klines
            else:
                logger.warning(f"[FutuLV3] {symbol} K 线获取失败: ret={ret} msg={_err}")
                self._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC
                # ── P0 (2026-06-04): K 线失败触发重连 — 检测 Push 断连 ──
                # 根因: K_1M Push 订阅可能在盘中断连(Disconnect), 历史 K 线请求也失败
                # 修复: 连续失败时触发 force_reconnect 恢复 Push 订阅
                if not hasattr(self, '_kline_fail_count'):
                    self._kline_fail_count = 0
                    self._kline_last_reconnect = 0.0
                self._kline_fail_count += 1
                if self._kline_fail_count >= 3:
                    import time as _t
                    if _t.time() - self._kline_last_reconnect > 120:  # 2min 冷却
                        logger.warning(
                            f"[FutuLV3][K线] 连续 {self._kline_fail_count} 次 K 线失败, "
                            f"触发 force_reconnect 恢复 Push 订阅"
                        )
                        self._kline_last_reconnect = _t.time()
                        self._kline_fail_count = 0
                        self.force_reconnect()
                return self._get_cached_kline(_cache_sym)  # 降级返回缓存

        except Exception as e:
            self._kline_sick_until = time.time() + _KLINE_SICK_WINDOW_SEC  # 2026-10-22 冻结族
            logger.debug(f"[FutuLV3] {symbol} K 线异常: {e}")
            return self._get_cached_kline(_cache_sym)  # 降级返回缓存

    def get_daily_kline(self, symbol: str, count: int = 60) -> Optional[List[Dict]]:
        """获取日K线历史数据 (不做当日过滤，返回完整历史日线)

        与 get_kline(ktype="K_DAY") 的区别:
          - get_kline 对 K_DAY 做了当日过滤，只返回今天的1根bar
          - 本方法直接调用 request_history_kline，返回最近 count 根日K线

        Args:
            symbol: 股票代码 (如 "NVDA" 或 "US.NVDA")
            count:  返回的日K线数量，默认60根

        Returns:
            List of dicts: [{time, open, high, low, close, volume, turnover}, ...]
            按时间升序排列；失败时返回缓存数据或 None
        """
        # ponytail: request_history_kline 是API调用, 不需要市场开盘。
        # _ensure_connected 的时段检查只对实时订阅有意义, 历史K线随时可查。
        if not self._connected or self._quote_ctx is None or not self._verify_connection_alive():
            self._connect_for_historical()  # 盘后也能连接
        if not self._connected or self._quote_ctx is None:
            # 仍然连不上 → 降级返回缓存
            futu_symbol = self._to_futu_symbol(symbol)
            cached = self._daily_kline_cache.get(futu_symbol)
            if cached:
                logger.debug(f"[FutuLV3·DailyK] {symbol} 未连接，返回缓存 ({len(cached)} bars)")
                return cached[-count:]
            return None

        futu_symbol = self._to_futu_symbol(symbol)

        # 检查缓存 + 数据新鲜度 (2026-06-27 P0: 缓存里可能有过期数据/2025年bar)
        # [RFC-171] 判据从"24h TTL + 10天宽限"改为相位感知 (daily_cache_acceptable):
        #   盘前/盘中只要含上一交易日已收盘 bar 即命中 (跨进程重启落盘复用);
        #   收盘后要求含当日 bar。⇒ 盘前 62 只批次零历史K请求, 消除配额打穿/回退。
        self._daily_disk_load()
        cache_ts = self._daily_kline_cache_time.get(futu_symbol, 0)
        cached = self._daily_kline_cache.get(futu_symbol)
        if cached and len(cached) >= min(count, 5):
            from app.utils.timezone import now_et as _now_et_dk
            if daily_cache_acceptable(str(cached[-1].get("time", ""))[:10],
                                      cache_ts, _now_et_dk(), time.time()):
                return cached[-count:]
            logger.debug(f"[FutuLV3·DailyK] {symbol} 缓存相位过期 "
                         f"(latest={str(cached[-1].get('time',''))[:10]}), 重新查询")

        # 2026-08-27 ret=-1 根因修: DailyK 路径此前未接 K 线配额门闸 — EOD 批量恰在 24h 整点
        # 撞上日K缓存 TTL 集体过期, 45 symbol 在 6s 内连发打穿 OpenD 账户级频控 (60次/30s),
        # 其后全部 ret=-1 ("获取历史K线频率太高") → TwelveData fallback 风暴 → 429 熔断.
        # 现在有界等待配额窗口; 超时才降级 (缓存 → None 触发 Router fallback).
        if not self._acquire_daily_kline_quota(f"D::{futu_symbol}"):
            cached = self._daily_kline_cache.get(futu_symbol)
            logger.warning(
                f"[FutuLV3·DailyK] {symbol} K线配额窗口满 (60次/30s) 等待超时, "
                + (f"降级返回缓存 ({len(cached)} bars)" if cached else "返回 None 触发 Router fallback")
            )
            return cached[-count:] if cached else None

        try:
            from futu import KLType
            from datetime import date as _date, timedelta as _td

            # 2026-06-30 根因修复: max_count太小 → OpenD返回范围内最旧的N根(2025年)
            # max_count要 >= 范围内交易日总数, 否则只拿最旧的。320覆盖~1.5年交易日。
            _end = _date.today().isoformat()
            _start = (_date.today() - _td(days=400)).isoformat()

            ret, data, _err = self._quote_ctx.request_history_kline(
                code=futu_symbol,
                ktype=KLType.K_DAY,
                max_count=max(count + 10, 320),
                start=_start,
                end=_end,
            )
            # [RFC-171] ret=-1 = OpenD 账户级频控 (与分钟K/其它进程共享配额, 应用侧
            #   60/30s 窗口只是近似 ⇒ 应用侧放行也可能被 OpenD 拒)。单次退避重试,
            #   避免一次抖动就把日K永久推到 TwelveData (口径不同 + 更慢)。
            if ret != 0:
                time.sleep(2.0)
                ret, data, _err = self._quote_ctx.request_history_kline(
                    code=futu_symbol,
                    ktype=KLType.K_DAY,
                    max_count=max(count + 10, 320),
                    start=_start,
                    end=_end,
                )
                if ret == 0:
                    logger.info(f"[FutuLV3·DailyK] {symbol} 退避重试成功")

            if ret == 0 and data is not None and len(data) > 0:
                klines = []
                for _, row in data.iterrows():
                    klines.append({
                        "time": str(row.get("time_key", "")),
                        "open": float(row.get("open", 0)),
                        "high": float(row.get("high", 0)),
                        "low": float(row.get("low", 0)),
                        "close": float(row.get("close", 0)),
                        "volume": float(row.get("volume", 0)),
                        "turnover": float(row.get("turnover", 0)),
                    })

                if not klines:
                    logger.warning(f"[FutuLV3·DailyK] {symbol} 日K线解析为空, 返回 None")
                    return None

                logger.debug(f"[FutuLV3·DailyK] {symbol} 获取 {len(klines)} 根日K线成功")

                # 2026-06-28 P0修复: 富途OpenD模拟环境的历史K线可能停滞在旧日期
                # (request_history_kline 返回2025年数据, snapshot正常返回2026年)
                # 根因: OpenD登录账号无LV2行情权限 → 历史K线不更新
                # 修复: 检测最新bar日期, 超过10天则丢弃, 让上层Router fallback到TwelveData
                try:
                    from datetime import date, timedelta
                    latest_bar_date = str(klines[-1].get("time", ""))[:10]
                    if latest_bar_date:
                        latest = date.fromisoformat(latest_bar_date)
                        if date.today() - latest > timedelta(days=10):
                            # 2026-06-30: 10分钟冷却 — 避免每tick都fallback到TwelveData导致429
                            _now_ts = time.time()
                            _last_notified = self._stale_kline_notified.get(futu_symbol, 0)
                            if _now_ts - _last_notified > 600:  # 10分钟
                                self._stale_kline_notified[futu_symbol] = _now_ts
                                logger.warning(
                                    f"[FutuLV3·DailyK] {symbol} 富途历史K线过期 "
                                    f"(latest={latest_bar_date}, >10天前) → 返回None触发TwelveData fallback"
                                )
                            # 不缓存过期数据, 直接返回None
                            return None
                except Exception:
                    pass  # 日期解析失败不阻断

                # 写入缓存 (只在数据新鲜时写入)
                self._daily_kline_cache[futu_symbol] = klines
                self._daily_kline_cache_time[futu_symbol] = time.time()
                # [RFC-171] 落盘 (节流 30s) — 供下次进程重建后盘前批次直接复用
                self._daily_disk_dirty = True
                self._daily_disk_save()
                return klines[-count:]
            else:
                # 2026-08-27: 透出 OpenD retMsg (此前 `_` 吞掉 → 频控/权限/无数据都只见 "ret=-1", 病灶查了10天)
                logger.warning(f"[FutuLV3·DailyK] {symbol} 日K线获取失败: ret={ret} msg={_err}")
                # 降级返回缓存
                cached = self._daily_kline_cache.get(futu_symbol)
                if cached:
                    logger.debug(f"[FutuLV3·DailyK] {symbol} 降级返回缓存 ({len(cached)} bars)")
                    return cached[-count:]
                return None

        except Exception as e:
            logger.warning(f"[FutuLV3·DailyK] {symbol} 日K线异常: {e}")
            # 降级返回缓存
            cached = self._daily_kline_cache.get(futu_symbol)
            if cached:
                logger.debug(f"[FutuLV3·DailyK] {symbol} 异常降级返回缓存 ({len(cached)} bars)")
                return cached[-count:]
            return None

    @property
    def is_connected(self) -> bool:
        """连接状态 (2026-04-20 新增)"""
        return self._connected and self._quote_ctx is not None

    def disconnect(self):
        """主动断开 Futu 连接 (2026-04-20 新增)

        港股交易时段主动断开，释放 OpenD 槽位给 FTSeek 港股系统。
        """
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception as e:
                    logger.debug(f"[FutuLV3] 断开连接异常: {e}")
                self._quote_ctx = None
            self._connected = False
            # DEB-20260903 #16: clear 加 _sub_lock (与 subscribe 的读-过滤-写互斥)
            with self._sub_lock:
                self._subscribed_symbols.clear()
                self._failed_sub_types.clear()
            logger.info("[FutuLV3] 已断开 OpenD 连接")

    # ═════════════════════════════════════════════════════════════
    # Task #7 (2026-06-02): 连接健康检查 + 指数退避重连
    # ═════════════════════════════════════════════════════════════

    def get_last_push_age(self) -> Optional[float]:
        """距上次收到任意 push 的秒数，从未收过返回 None"""
        if self._last_push_time is None:
            return None
        return time.time() - self._last_push_time

    def is_healthy(self, log_change: bool = True) -> bool:
        """连接健康检查 — 验证 OpenD 连接与数据新鲜度。

        判定规则:
            1. 未连接 → 不健康
            2. 非美股交易时段 / 无活跃订阅 → 仅看连接状态 (健康)
            3. 连接后超过宽容期 (60s) 仍未收到 push → 不健康
            4. 最后 push 超过 stale_threshold (30s) → 不健康

        Args:
            log_change: 是否在状态变化时输出 INFO/WARNING 日志

        Returns:
            True: 连接活跃且数据新鲜
            False: 连接断开 / 数据陈旧 / 长期无推送
        """
        healthy = self._compute_health()
        if log_change:
            self._log_health_change(healthy)
        return healthy

    def _compute_health(self) -> bool:
        """纯函数：计算当前健康状态 (不产生副作用)。"""
        if not self._connected or self._quote_ctx is None:
            return False

        # 非美股交易时段: 不应期待推送，仅看连接
        if not self._is_us_market_open():
            return True

        # 无有效订阅: 不会有 push，仅看连接
        if not self._subscribed_symbols:
            return True

        now = time.time()
        if self._last_push_time is None:
            # 连接后从未收到 push — 给宽容期
            if self._last_connect_time and (now - self._last_connect_time) > self._initial_grace_sec:
                return False
            return True

        age = now - self._last_push_time
        return age <= self._stale_threshold_sec

    def _log_health_change(self, healthy: bool):
        """健康状态变化时输出日志 + 累计统计。"""
        prev = self._last_health_state
        if prev is None:
            self._last_health_state = healthy
            return
        if healthy == prev:
            return

        if healthy:
            duration = (
                time.time() - self._disconnect_start_time
                if self._disconnect_start_time else 0.0
            )
            self._last_disconnect_duration_sec = duration
            logger.info(
                f"[FutuLV3][HEALTH] ✅ 连接恢复健康 | 本次中断 {duration:.1f}s | "
                f"累计断线 {self._total_disconnect_count} 次"
            )
            self._disconnect_start_time = None
        else:
            self._total_disconnect_count += 1
            self._disconnect_start_time = time.time()
            age = self.get_last_push_age()
            age_str = f"{age:.1f}s" if age is not None else "never"
            logger.warning(
                f"[FutuLV3][HEALTH] ⚠️ 连接不健康 | connected={self._connected} | "
                f"last_push_age={age_str} | subs={len(self._subscribed_symbols)} | "
                f"累计断线 {self._total_disconnect_count} 次"
            )
        self._last_health_state = healthy

    def _connect_for_historical(self) -> bool:
        """盘后/非交易时段连接OpenD (仅用于request_history_kline等API调用, 不订阅实时行情)."""
        if self._connected and self._quote_ctx is not None and self._verify_connection_alive():
            return True
        try:
            # 2026-06-26 修复: OpenQuoteContext 在 OpenD 不可达时会无限阻塞,
            # 先用 socket 探测端口, 不可达时快速失败。
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.settimeout(2.0)
            result = sock.connect_ex((self.host, self.port))
            sock.close()
            if result != 0:
                logger.debug(f"[FutuLV3·历史] OpenD {self.host}:{self.port} 不可达, 跳过连接")
                return False

            from futu import OpenQuoteContext, SysConfig
            SysConfig.enable_proto_encrypt(False)
            self._quote_ctx = OpenQuoteContext(host=self.host, port=self.port)
            self._connected = True
            self._last_connect_time = time.time()
            logger.info(f"[FutuLV3·历史] 盘后连接 OpenD 成功 (历史K线查询)")
            return True
        except Exception as e:
            self._connected = False
            logger.debug(f"[FutuLV3·历史] 盘后连接失败: {e}")
            return False

    def get_snapshot(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """通用快照观测 (仅观测, 不服务任何交易/退出链路).

        2026-08-05 (DEB-20260805(二) B1/C1): 走历史连接路径, 不占交易时段槽位。
        返回原始快照行 (含 update_time — 判断成交/报价时点的依据)。
        """
        if not symbols:
            return []
        if not self._connect_for_historical():
            return []
        try:
            futu_symbols = [self._to_futu_symbol(s) for s in symbols]
            ret, data = self._quote_ctx.get_market_snapshot(futu_symbols)
            if ret != 0 or data is None:
                logger.debug(f"[FutuLV3·ETH] 快照查询失败: {data}")
                return []
            rows = []
            for _, row in data.iterrows():
                rows.append({
                    "code": str(row.get("code", "")),
                    "last_price": float(row.get("last_price", 0) or 0),
                    "prev_close": float(row.get("prev_close_price", 0) or 0),
                    "bid": float(row.get("bid_price", 0) or 0),
                    "ask": float(row.get("ask_price", 0) or 0),
                    "volume": float(row.get("volume", 0) or 0),
                    "turnover": float(row.get("turnover", 0) or 0),
                    "update_time": str(row.get("update_time", "")),
                })
            return rows
        except Exception as e:
            logger.debug(f"[FutuLV3·ETH] 快照查询异常: {e}")
            return []

    def get_snapshot_after_hours(self, symbols: List[str]) -> List[Dict[str, Any]]:
        """盘后快照观测 (DEB-20260805 门E 数据基础). 通用快照的别名, 保持旧调用方兼容."""
        return self.get_snapshot(symbols)

    def force_reconnect(self) -> bool:
        """带指数退避的强制重连，重连后会恢复之前的订阅集合。

        Returns:
            True: 重连成功 (并尝试恢复订阅)
            False: 退避中 / 重连失败 / 非交易时段
        """
        # DEB-20260904 #143: 盘前也允许重连 (与 _ensure_connected 会话口径一致),
        # 否则 watchdog 盘前发现断连也会被此处拦截, 永远重连不上
        if not self._is_us_market_open() and not self._is_us_pre_market():
            logger.debug("[FutuLV3][RECONNECT] 非美股交易/盘前时段，跳过重连")
            return False

        now = time.time()
        # 指数退避: 2,4,8,16,32,60 (封顶)
        backoff = min(
            self._reconnect_max_backoff,
            self._reconnect_min_backoff * (2 ** min(self._reconnect_attempts, 6)),
        )
        if self._last_reconnect_time and (now - self._last_reconnect_time) < backoff:
            logger.debug(
                f"[FutuLV3][RECONNECT] 退避中 (剩余 {backoff - (now - self._last_reconnect_time):.1f}s)"
            )
            return False

        self._last_reconnect_time = now
        self._reconnect_attempts += 1
        logger.warning(
            f"[FutuLV3][RECONNECT] 触发强制重连 (第 {self._reconnect_attempts} 次, 退避间隔 {backoff:.0f}s)"
        )

        # 保存当前订阅，重连后恢复
        # DEB-20260903 #16: 读集合加 _sub_lock (与 subscribe 并发互斥)
        with self._sub_lock:
            saved_subs = list(self._subscribed_symbols)

        # 强制关闭旧连接
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    self._quote_ctx.close()
                except Exception as e:
                    logger.debug(f"[FutuLV3][RECONNECT] 关闭旧连接异常: {e}")
                self._quote_ctx = None
            self._connected = False
            # DEB-20260903 #16: clear 加 _sub_lock
            with self._sub_lock:
                self._subscribed_symbols.clear()
                self._failed_sub_types.clear()
            self._last_push_time = None
            with self._push_cache_lock:
                self._push_quote_cache.clear()
                self._push_kline_cache.clear()
                self._push_ticker_cache.clear()
                self._push_ticker_seen_at.clear()
                self._push_orderbook_cache.clear()

        # 重新连接
        if not self._ensure_connected():
            logger.error(f"[FutuLV3][RECONNECT] ❌ 第 {self._reconnect_attempts} 次重连失败")
            return False

        # 重新订阅之前的 symbols
        if saved_subs:
            try:
                ok = self.subscribe(saved_subs)
                if ok:
                    logger.info(
                        f"[FutuLV3][RECONNECT] ✅ 重连成功，已恢复订阅 {len(saved_subs)} 只"
                    )
                else:
                    logger.warning(f"[FutuLV3][RECONNECT] 重连成功但重订阅返回 False")
            except Exception as e:
                logger.error(f"[FutuLV3][RECONNECT] 重订阅异常: {e}")
        else:
            logger.info("[FutuLV3][RECONNECT] ✅ 重连成功 (无需恢复订阅)")

        # 重连成功 — 重置计数器
        self._reconnect_attempts = 0
        return True

    def ensure_healthy(self) -> bool:
        """例行健康保障: 检测不健康时自动触发 force_reconnect。

        调用者可在交易时段周期性调用，返回值表示当前是否健康。
        不会阻塞数据流 — 退避期间只记录状态、不重返。
        """
        if self.is_healthy(log_change=True):
            return True
        # 负载低时才调用；force_reconnect 内部有退避保护
        self.force_reconnect()
        # 重连后重新评估（不重复写变化日志）
        return self._compute_health()

    # ═══════════════════════════════════════════════════════════
    # P0 新增 (2026-05-09): 订单流深度数据 (ORDER_BOOK)
    # ═══════════════════════════════════════════════════════════

    def get_order_book(self, symbol: str) -> Optional[Dict]:
        """
        获取实时买卖盘深度数据

        Returns:
            {
                "bid_prices": [买1价, 买2价, ...],
                "bid_volumes": [买1量, 买2量, ...],
                "ask_prices": [卖1价, 卖2价, ...],
                "ask_volumes": [卖1量, 卖2量, ...],
                "bid_total_volume": 买盘总量,
                "ask_total_volume": 卖盘总量,
                "bid_ask_ratio": 买卖盘深度比,
            } or None
        """
        if not self._ensure_connected():
            return None

        try:
            from futu import OrderBookQuery
            futu_symbol = self._to_futu_symbol(symbol)

            ret, data = self._quote_ctx.get_order_book(
                futu_symbol, num=10  # 获取 10 档深度
            )

            if ret != 0 or data is None or data.empty:
                logger.debug(f"[FutuLV3] ORDER_BOOK 获取失败: {symbol}")
                return None

            row = data.iloc[0]

            # 提取买卖盘数据
            bid_prices = [row.get(f'BidPrice{i}', 0) for i in range(1, 11)]
            bid_volumes = [row.get(f'BidVol{i}', 0) for i in range(1, 11)]
            ask_prices = [row.get(f'AskPrice{i}', 0) for i in range(1, 11)]
            ask_volumes = [row.get(f'AskVol{i}', 0) for i in range(1, 11)]

            bid_total = sum(v for v in bid_volumes if v > 0)
            ask_total = sum(v for v in ask_volumes if v > 0)

            ratio = bid_total / ask_total if ask_total > 0 else 0

            return {
                "bid_prices": bid_prices,
                "bid_volumes": bid_volumes,
                "ask_prices": ask_prices,
                "ask_volumes": ask_volumes,
                "bid_total_volume": bid_total,
                "ask_total_volume": ask_total,
                "bid_ask_ratio": ratio,
            }

        except Exception as e:
            logger.debug(f"[FutuLV3] ORDER_BOOK 异常: {symbol}: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # P1 新增 (2026-05-09): 逐笔成交数据 (TICKER)
    # ═══════════════════════════════════════════════════════════

    def get_recent_tickers(self, symbol: str, count: int = 50) -> Optional[List[Dict]]:
        """
        获取最近逐笔成交数据

        Returns:
            [
                {
                    "price": 成交价,
                    "volume": 成交量,
                    "direction": "BUY"/"SELL"/"NEUTRAL",
                    "timestamp": 时间戳,
                    "turnover": 成交额,
                },
                ...
            ] or None
        """
        if not self._ensure_connected():
            return None

        try:
            from futu import SysNotifyHandler
            futu_symbol = self._to_futu_symbol(symbol)

            ret, data = self._quote_ctx.get_recent_tickers(futu_symbol, count=count)

            if ret != 0 or data is None or data.empty:
                logger.debug(f"[FutuLV3] TICKER 获取失败: {symbol}")
                return None

            tickers = []
            for _, row in data.iterrows():
                # DEB-20260903 #17: 统一调用 _map_ticker_direction (SSOT) —
                # 原内联映射 2=BUY/1=SELL 与 push 路径 (1=BUY/2=SELL) 互反,
                # 降级到 API 拉取时买卖方向反转, 污染 buy_sell_ratio/order_flow_imbalance
                direction = _map_ticker_direction(row.get('Direction', 4))

                tickers.append({
                    "price": float(row.get('Price', 0)),
                    "volume": float(row.get('Volume', 0)),
                    "direction": direction,
                    "timestamp": row.get('Time', ''),
                    "turnover": float(row.get('Turnover', 0)),
                })

            return tickers

        except Exception as e:
            logger.debug(f"[FutuLV3] TICKER 异常: {symbol}: {e}")
            return None

    # ═══════════════════════════════════════════════════════════
    # P1 新增 (2026-05-30): 盘口流动性 + 成交流向辅助决策
    # ═══════════════════════════════════════════════════════════

    def get_order_book_quality(self, symbol: str) -> Dict[str, Any]:
        """评估标的实时流动性质量

        优先级: Push OrderBook 缓存 → Push Quote 缓存 → 快照

        Returns:
            {
                'spread_pct': float,       # bid-ask spread 百分比
                'balance_ratio': float,    # ask_vol / bid_vol 比值
                'liquidity_score': float,  # 0-100 综合评分
                'tradeable': bool,         # 是否适合交易 (spread < 0.5%)
            }
        """
        default = {
            'spread_pct': 0.0,
            'balance_ratio': 1.0,
            'liquidity_score': 50.0,
            'tradeable': True,
        }
        try:
            bid = ask = 0.0
            bid_vol = ask_vol = 0.0

            # ① 优先 Push OrderBook 缓存 (实时推送，<100ms 延迟)
            ob = self.get_push_orderbook(symbol)
            if ob:
                bid = float(ob.get('BidPrice', 0) or 0)
                ask = float(ob.get('AskPrice', 0) or 0)
                bid_vol = float(ob.get('BidVol', 0) or 0)
                ask_vol = float(ob.get('AskVol', 0) or 0)

            # ② Push Quote 缓存
            if bid <= 0 or ask <= 0:
                pq = self.get_push_quote(symbol)
                if pq:
                    # DEB-20260903 #18: push quote 的 bid/ask 在真实盘口缺失时
                    # 兜底为 last_price (book_is_proxy=True), 直接采用会得 spread=0/
                    # liquidity_score=100/tradeable=True, 把无真实盘口标的误判为高流动性。
                    # proxy 时跳过, 交由快照降级/默认值处理
                    if not pq.get("book_is_proxy"):
                        bid = float(pq.get('bid', 0) or 0)
                        ask = float(pq.get('ask', 0) or 0)
                        bid_vol = float(pq.get('bid_vol', 0) or 0)
                        ask_vol = float(pq.get('ask_vol', 0) or 0)

            # ③ 快照降级
            if (bid <= 0 or ask <= 0) and self._ensure_connected():
                futu_symbol = self._to_futu_symbol(symbol)
                ret, data = self._quote_ctx.get_market_snapshot([futu_symbol])
                if ret != 0 or data is None or data.empty:
                    return dict(default)
                row = data.iloc[0]
                bid = float(row.get('bid_price', 0) or 0)
                ask = float(row.get('ask_price', 0) or 0)
                bid_vol = float(row.get('bid_vol', 0) or 0)
                ask_vol = float(row.get('ask_vol', 0) or 0)

            # 数据有效性
            if bid <= 0 or ask <= 0 or ask < bid:
                return dict(default)

            bid_vol_safe = bid_vol if bid_vol > 0 else 1.0
            ask_vol_safe = ask_vol if ask_vol > 0 else 1.0

            mid = (bid + ask) / 2
            spread_pct = (ask - bid) / mid * 100  # 百分比
            balance_ratio = ask_vol_safe / bid_vol_safe if bid_vol > 0 else 999.0

            # 流动性评分: spread 越小越好, 0.1% → 80, 0.3% → 40, 0.5% → 0
            liquidity_score = max(0.0, min(100.0, 100.0 - spread_pct * 200))
            tradeable = spread_pct < 0.5

            return {
                'spread_pct': round(spread_pct, 4),
                'balance_ratio': round(balance_ratio, 2),
                'liquidity_score': round(liquidity_score, 1),
                'tradeable': bool(tradeable),
            }
        except Exception as e:
            logger.warning(f"[LIQUIDITY] {symbol} 流动性检查异常: {e}")
            return dict(default)

    def get_ticker_sentiment(self, symbol: str, window: int = 30) -> Dict[str, Any]:
        """分析最近 N 笔成交的主动买卖情绪

        通过富途 Ticker 推送 (PushTickerHandler 缓存) 或 get_recent_tickers API
        获取成交方向。direction == 'BUY' 表示主动买入 (做多力量)，
        direction == 'SELL' 表示主动卖出 (做空力量)。

        Returns:
            {
                'sentiment': float,       # [-1, 1], 正=多头力量, 负=空头力量
                'buy_ratio': float,       # 主动买入笔数占比 [0, 1]
                'volume_weighted': float, # 量加权情绪 (大单权重更高)
                'confidence': float,      # 数据质量 [0, 1]
            }
        """
        default = {
            'sentiment': 0.0,
            'buy_ratio': 0.5,
            'volume_weighted': 0.0,
            'confidence': 0.0,
        }
        try:
            # ① 优先 Push 缓存 (PushTickerHandler 实时累积)
            tickers = self.get_push_tickers(symbol) or []

            # ② 缓存不足则降级到 API
            if len(tickers) < 5 and self._ensure_connected():
                try:
                    fetched = self.get_recent_tickers(symbol, count=max(window, 50))
                    if fetched:
                        tickers = fetched
                except Exception:
                    pass

            if not tickers or len(tickers) < 5:
                return dict(default)

            recent = tickers[-window:] if len(tickers) > window else tickers

            buy_count = sell_count = 0
            buy_volume = sell_volume = 0.0

            for t in recent:
                direction = t.get('direction', 'NEUTRAL')
                try:
                    volume = float(t.get('volume', 0) or 0)
                except (TypeError, ValueError):
                    volume = 0.0
                if direction == 'BUY':
                    buy_count += 1
                    buy_volume += volume
                elif direction == 'SELL':
                    sell_count += 1
                    sell_volume += volume

            total = buy_count + sell_count
            if total == 0:
                return dict(default)

            buy_ratio = buy_count / total
            sentiment = (buy_count - sell_count) / total  # [-1, 1]

            total_vol = buy_volume + sell_volume
            volume_weighted = (
                (buy_volume - sell_volume) / total_vol if total_vol > 0 else 0.0
            )

            confidence = min(1.0, len(recent) / max(window, 1))  # 数据充足度

            return {
                'sentiment': round(sentiment, 3),
                'buy_ratio': round(buy_ratio, 3),
                'volume_weighted': round(volume_weighted, 3),
                'confidence': round(confidence, 2),
            }
        except Exception as e:
            logger.warning(f"[TICKER_SENTIMENT] {symbol} 成交流向分析异常: {e}")
            return dict(default)

    def get_health_status(self) -> dict:
        """获取健康状态"""
        total = self._success_count + self._fail_count
        success_rate = (
            self._success_count / total * 100 if total > 0 else 0
        )

        return {
            "provider": "futu_lv3",
            "connected": self._connected,
            "enabled": self.enabled,
            "is_healthy": self._compute_health(),
            "last_push_age_sec": self.get_last_push_age(),
            "reconnect_attempts": self._reconnect_attempts,
            "total_disconnect_count": self._total_disconnect_count,
            "last_disconnect_duration_sec": round(self._last_disconnect_duration_sec, 2),
            "subscribed_symbols": len(self._subscribed_symbols),
            "success_rate": f"{success_rate:.1f}%",
            "total_requests": total,
            "success_count": self._success_count,
            "fail_count": self._fail_count,
            "push_quote_count": self._push_quote_count,
            "push_ticker_count": self._push_ticker_count,
            "push_orderbook_count": self._push_orderbook_count,
            "push_error_count": self._push_error_count,
            "ticker_cache_symbols": len(self._push_ticker_cache),
            "ticker_cache_stale_symbols": self._count_stale_ticker_symbols(),
            "orderbook_cache_symbols": len(self._push_orderbook_cache),
            "last_error": self._last_error,
            "last_connect_time": (
                datetime.fromtimestamp(self._last_connect_time).isoformat()
                if self._last_connect_time
                else None
            ),
        }

    def _is_cache_valid(self, key: str) -> bool:
        """检查缓存是否有效"""
        if key not in self._cache:
            return False
        age = time.time() - self._cache_time.get(key, 0)
        return age < self.cache_ttl

    def _count_stale_ticker_symbols(self) -> int:
        """B4: 统计"有缓存但已停更超 TTL"的标的数 — 让 freshness gate 的触发面可观测。

        get_push_tickers 在热路径上只打 debug (避免刷屏), 故把命中面汇总到本计数,
        经 get_stats()/健康端点暴露, 免得重演 B5 那种"debug 全吞 ⇒ 故障不可见"。
        """
        now = time.time()
        with self._push_cache_lock:
            return sum(
                1 for sym in self._push_ticker_cache
                if (now - self._push_ticker_seen_at.get(sym, 0)) >= self._ticker_cache_ttl
            )

    def close(self):
        """关闭连接"""
        with self._lock:
            if self._quote_ctx is not None:
                try:
                    # 取消所有订阅 (2026-05-20: 覆盖所有已订阅类型)
                    if self._subscribed_symbols:
                        from futu import SubType
                        symbols_list = list(self._subscribed_symbols)
                        # DEB-20260904 #145: 默认退订列表补 K_DAY —
                        # _futu_sub_type_map/stock_sub_types 均可订阅 K_DAY,
                        # 原列表遗漏 → close 时 K_DAY 残留 OpenD 侧, 槽位泄漏
                        all_sub_types = [
                            SubType.QUOTE, SubType.K_1M, SubType.K_5M,
                            SubType.K_15M, SubType.K_30M, SubType.K_60M,
                            SubType.K_DAY, SubType.ORDER_BOOK, SubType.TICKER,
                        ]
                        for sub_type in all_sub_types:
                            try:
                                self._quote_ctx.unsubscribe(symbols_list, [sub_type])
                            except Exception:
                                pass

                    self._quote_ctx.close()
                    self._quote_ctx = None
                    self._connected = False
                    # DEB-20260903 #16: clear 加 _sub_lock
                    with self._sub_lock:
                        self._subscribed_symbols.clear()
                        self._failed_sub_types.clear()
                    with self._push_cache_lock:
                        self._push_quote_cache.clear()
                        self._push_kline_cache.clear()
                        self._push_ticker_cache.clear()
                        self._push_ticker_seen_at.clear()
                        self._push_orderbook_cache.clear()
                    logger.info("[FutuLV3] 连接已关闭")
                except Exception as e:
                    logger.warning(f"[FutuLV3] 关闭连接异常: {e}")

    def unsubscribe_all(self, symbols: List[str] = None, sub_types: List[str] = None):
        """
        取消所有美股行情订阅 (2026-04-23 新增, 2026-05-20 增强)
        用于非交易时段释放富途 OpenD 槽位。

        Args:
            symbols: 股票代码列表 (为 None 时取消所有已订阅)
            sub_types: 订阅类型列表 (默认取消所有已订阅类型)
        """
        if not self._quote_ctx or not self._subscribed_symbols:
            logger.debug("[FutuLV3] 无活跃订阅，无需退订")
            return

        from futu import SubType

        # DEB-20260903 #16: 读集合加 _sub_lock
        with self._sub_lock:
            symbols_list = symbols or list(self._subscribed_symbols)
        if sub_types is None:
            # DEB-20260904 #145: 默认退订补 K_DAY — 原列表遗漏, 订阅过 K_DAY
            # 的配置永远无法释放 (sub_type_map 已有该映射)
            sub_types = ['QUOTE', 'K_1M', 'K_5M', 'K_15M', 'K_30M', 'K_60M',
                         'K_DAY', 'ORDER_BOOK', 'TICKER']

        sub_type_map = {
            'QUOTE': SubType.QUOTE,
            'K_1M': SubType.K_1M,
            'K_5M': SubType.K_5M,
            'K_15M': SubType.K_15M,
            'K_30M': SubType.K_30M,
            'K_60M': SubType.K_60M,
            'K_DAY': SubType.K_DAY,
            'ORDER_BOOK': SubType.ORDER_BOOK,
            'TICKER': SubType.TICKER,
        }

        logger.info(f"[FutuLV3] 开始退订 {len(symbols_list)} 只美股 (释放槽位)...")
        success = 0
        failed = 0
        # 2026-09-22: 区分"良性拒退"与"真退订失败"。SDK 客户端侧会直接拒绝两类无意义的退订:
        #   ① 未订阅过 <sym> 的 <type> ② <sym>的<type>订阅时间过短 (ORDER_BOOK 硬规则 ≥1min)。
        #   两者都**不占 OpenD 槽位** (前者从未订阅, 后者由 OpenD 自行结束) = 不是故障,
        #   却在聚合行里被计成 ERROR, 污染 ERROR 计数 (9/22 停机窗口实测: ORDER_BOOK+TICKER
        #   报此两类, 而 QUOTE/K_1M 均退订成功)。故仅"真失败"才 ERROR, 否则 WARNING。
        _benign_marks = ("未订阅过", "订阅时间过短")
        benign_failed = 0

        for st_name in sub_types:
            sub_type = sub_type_map.get(st_name)
            if sub_type is None:
                continue
            try:
                ret, msg = self._quote_ctx.unsubscribe(symbols_list, [sub_type])
                if ret == 0:
                    success += 1
                else:
                    failed += 1
                    if any(_m in str(msg) for _m in _benign_marks):
                        benign_failed += 1
                    logger.warning(f"[FutuLV3] 退订 {st_name} 失败: {msg}")
            except Exception as e:
                failed += 1
                logger.warning(f"[FutuLV3] 退订 {st_name} 异常: {e}")

        # DEB-20260903 #19: 部分退订失败时保留本地订阅状态 —
        # 原无条件 clear, 失败类型在 OpenD 侧仍活跃推送但本地已遗忘 →
        # close() (依赖 _subscribed_symbols) 不再补退订 = 槽位泄漏, 且
        # watchdog `if not _subscribed_symbols: continue` 使健康检查被跳过。
        # 仅全部成功才清空; 有失败时保留集合供后续重试/补退订
        if failed == 0:
            # DEB-20260903 #16: clear 加 _sub_lock
            with self._sub_lock:
                self._subscribed_symbols.clear()
                self._failed_sub_types.clear()
            with self._push_cache_lock:
                self._push_quote_cache.clear()
                self._push_kline_cache.clear()
                self._push_ticker_cache.clear()
                self._push_ticker_seen_at.clear()
                self._push_orderbook_cache.clear()
            logger.info(f"[FutuLV3] 退订完成: 成功={success}, 失败={failed} | 槽位已释放")
        else:
            _real_failed = failed - benign_failed
            _agg_msg = (
                f"[FutuLV3] 退订部分失败: 成功={success}, 失败={failed}"
                f"(其中良性拒退={benign_failed}, 真失败={_real_failed}) | "
                f"保留 _subscribed_symbols 与 push 缓存供后续补退订"
            )
            if _real_failed > 0:
                logger.error(_agg_msg + ", OpenD 侧槽位可能仍被占用 (需人工/重试)")
            else:
                # 全部失败项都是 SDK 客户端侧拒退 (未订阅过/订阅过短) = 无槽位泄漏, 非故障
                logger.warning(_agg_msg + ", 无槽位泄漏 (良性拒退, 无需人工介入)")


# ── 全局单例 ──────────────────────────────────────────────────────
_futu_lv3_instance: Optional[FutuLV3Provider] = None
_futu_lv3_lock = threading.Lock()


def get_futu_lv3_provider(
    host: str = "127.0.0.1",
    port: int = 11111,
    fallback_provider=None,
    cache_ttl_seconds: int = 5,
) -> FutuLV3Provider:
    """
    获取 Futu LV3 提供商单例

    Args:
        host: OpenD 主机
        port: OpenD 端口
        fallback_provider: 降级数据源
        cache_ttl_seconds: 缓存 TTL

    Returns:
        FutuLV3Provider 实例
    """
    global _futu_lv3_instance
    if _futu_lv3_instance is None:
        with _futu_lv3_lock:
            if _futu_lv3_instance is None:
                _futu_lv3_instance = FutuLV3Provider(
                    host=host,
                    port=port,
                    fallback_provider=fallback_provider,
                    cache_ttl_seconds=cache_ttl_seconds,
                )
    return _futu_lv3_instance


def reset_futu_lv3_provider():
    """重置单例 (用于测试或重新初始化)"""
    global _futu_lv3_instance
    with _futu_lv3_lock:
        if _futu_lv3_instance is not None:
            _futu_lv3_instance.close()
            _futu_lv3_instance = None
        logger.info("[FutuLV3] 单例已重置")


def _atexit_release_futu_lv3(timeout: float = 5.0) -> None:
    """进程正常退出时释放单例连接 —— atexit 钩子 (**有界, 绝不拖住退出**).

    为什么需要: `reset_futu_lv3_provider()` 只有显式调用才会跑; 短命 CLI/cron 进程
    (DA skill `pipeline.py`、UE `scripts/llm_universe_scan.py`、审计脚本) 走完
    `get_futu_lv3_provider()` 就退出, 单例的行情订阅会留在 OpenD 侧直到 TCP 超时
    才回收 —— 而 OpenD 槽位是稀缺资源 (盘前多进程并发时尤其明显)。

    为什么"有界": `close()` 内含一轮 unsubscribe, OpenD 不可达时可能阻塞 (SDK 已知
    问题) —— 无界的话, 一个**清理**钩子会把退出重新变成挂死。这里把 close 丢进
    daemon 线程 + `join(timeout)`: 超时即放弃清理, 进程照常退出, 只留一条告警。

    为什么不影响主服务: `main.py::_graceful_shutdown()` 末尾走 `os._exit(0)`,
    **atexit 在 os._exit 下不执行** ⇒ 本钩子对常驻服务是 no-op (其 LV3 释放由
    `data_router.close()` 第 4 步负责, 语义不变)。

    回滚: 删掉本函数与下面的 `atexit.register(...)` 一行即可 (纯增量, 无调用方)。
    """
    inst = _futu_lv3_instance
    if inst is None:
        return
    _t = threading.Thread(
        target=reset_futu_lv3_provider, name="futu-lv3-atexit-release", daemon=True
    )
    _t.start()
    _t.join(timeout)
    if _t.is_alive():
        logger.warning(
            f"[FutuLV3] atexit 释放未在 {timeout:.0f}s 内完成 → 放弃清理直接退出 "
            f"(OpenD 侧槽位由 TCP 断连回收)"
        )


# 2026-09-16: 短命进程的兜底释放 (常驻服务不受影响, 见 docstring)
atexit.register(_atexit_release_futu_lv3)
