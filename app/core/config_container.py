"""
配置容器类 - 集中管理所有全局配置和常量

目标：
1. 替代分散在各模块的全局变量
2. 提供依赖注入支持
3. 添加访问控制和类型注解
4. 与 ConfigCenter 集成 (config/unified_config.yaml 为唯一配置源)

使用示例:
    from app.core.config_container import ConfigContainer

    config = ConfigContainer()
    paths = config.paths
    thresholds = config.risk.thresholds

架构说明 (2026-04-09 收敛后):
  - .env → 敏感环境变量
  - config/unified_config.yaml → 唯一配置源 (通过 ConfigCenter 加载)
  - ConfigContainer → 依赖注入容器 (包装 ConfigCenter 提供点式访问)

SSOT v4.1 (2026-05-06):
  - 所有 from_env() 必须通过 _cc_get() 从 ConfigCenter 读取
  - 所有 fallback 必须来自 config_defaults.CFG
  - 禁止直接 os.getenv() + 硬编码数值作为业务配置默认值
"""
import os
import json
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field

# 导入 ConfigCenter 作为配置数据源
from config.config_center import get_config_center

# 导入唯一默认值源
try:
    from app.core.config_defaults import CFG
except ImportError:
    CFG = None


def _cc_get(cc, key: str, cfg_attr: str, fallback: Any) -> Any:
    """从 ConfigCenter 获取配置值，逐级回退：ConfigCenter → CFG → 硬回退"""
    val = cc.get(key)
    if val is not None:
        return val
    if CFG is not None:
        cfg_val = getattr(CFG, cfg_attr, None)
        if cfg_val is not None:
            return cfg_val
    return fallback


# ============================================================
#  路径配置容器
# ============================================================
@dataclass
class PathConfig:
    """路径配置"""
    base_dir: Path
    data_dir: Path
    log_dir: Path
    config_dir: Path
    
    # 子目录
    trade_records_dir: Path
    signal_records_dir: Path
    order_log_dir: Path
    failure_stats_dir: Path
    prediction_tracking_dir: Path
    reports_dir: Path
    vix_cache_dir: Path
    news_cache_dir: Path
    strategy_versions_file: Path
    strategy_params_file: Path
    config_backups_dir: Path
    
    # 数据库
    db_path: Path
    
    # 外部数据
    futu_cp2_trade_journal: Path
    
    @classmethod
    def from_env(cls) -> 'PathConfig':
        """从环境变量或默认值创建路径配置"""
        base_dir = Path(__file__).resolve().parent.parent.parent
        data_dir = base_dir / "data"
        log_dir = base_dir / "logs"
        config_dir = base_dir / "config"
        
        # 确保目录存在
        data_dir.mkdir(exist_ok=True)
        log_dir.mkdir(exist_ok=True)
        config_dir.mkdir(exist_ok=True)
        
        return cls(
            base_dir=base_dir,
            data_dir=data_dir,
            log_dir=log_dir,
            config_dir=config_dir,
            trade_records_dir=data_dir / "trade_records",
            signal_records_dir=data_dir / "signal_records",
            order_log_dir=data_dir / "order_logs",
            failure_stats_dir=data_dir / "failure_stats",
            prediction_tracking_dir=data_dir / "prediction_tracking",
            reports_dir=data_dir / "reports",
            vix_cache_dir=data_dir / "vix_cache",
            news_cache_dir=base_dir / "data" / "news_cache",
            strategy_versions_file=data_dir / "strategy_versions.json",
            strategy_params_file=config_dir / "strategy_params.json",
            config_backups_dir=config_dir / "backups",
            db_path=data_dir / "ftseek.db",
            futu_cp2_trade_journal=Path("/opt/futu_cp2/data/trade_journal.json"),
        )


# ============================================================
#  OpenD 配置容器
# ============================================================
@dataclass
class OpenDConfig:
    """OpenD 连接配置"""
    host: str
    port: int
    dir: str
    login_account: str
    login_pwd: str
    trade_env: int
    simulate_account_id: str
    
    @classmethod
    def from_env(cls) -> 'OpenDConfig':
        """从环境变量创建 OpenD 配置"""
        _raw_trade_env = int(os.getenv("TRADE_ENV", "1"))
        _allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
        trade_env = _raw_trade_env if (_allow_real and _raw_trade_env == 0) else 1
        
        if _raw_trade_env == 0 and not _allow_real:
            import warnings
            warnings.warn(
                "[安全拦截] TRADE_ENV=0 被忽略，当前强制使用模拟环境。",
                RuntimeWarning, stacklevel=2
            )
        
        return cls(
            host=os.getenv("OPEND_HOST", "127.0.0.1"),
            port=int(os.getenv("OPEND_PORT", "11111")),
            dir=os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_10.3.6308_Centos7/Futu_OpenD_10.3.6308_Centos7"),
            login_account=os.getenv("FUTU_LOGIN_ACCOUNT", ""),
            login_pwd=os.getenv("FUTU_LOGIN_PWD", ""),
            trade_env=trade_env,
            simulate_account_id=os.getenv("SIMULATE_ACCOUNT_ID", "18820895"),
        )


# ============================================================
#  风控配置容器
# ============================================================
@dataclass
class RiskConfig:
    """风控相关配置"""
    # 持仓跌幅预警
    drop_warning_threshold: float = -0.05
    drop_critical_threshold: float = -0.08
    
    # VIX 监控
    vix_warning_threshold: float = 30.0
    vix_critical_threshold: float = 40.0
    vix_cache_ttl_seconds: int = 600
    
    # 动态退出
    dynamic_exit_config_path: str = ""
    
    # 阈值配置
    threshold_config_path: str = ""
    threshold_history_path: str = ""
    
    # 市场体制
    params_config_path: str = ""
    runtime_params_path: str = ""
    
    # 默认费率
    default_fee_rate: float = 0.0003
    default_min_fee: float = 15.0
    default_stamp_duty: float = 0.001
    
    @classmethod
    def from_env(cls, paths: 'PathConfig') -> 'RiskConfig':
        """从 ConfigCenter 创建风控配置 (SSOT v4.1)"""
        cc = get_config_center()
        return cls(
            drop_warning_threshold=float(_cc_get(cc, 'risk_limits.drop_warning_threshold', 'DROP_WARNING_THRESHOLD', -0.05)),
            drop_critical_threshold=float(_cc_get(cc, 'risk_limits.drop_critical_threshold', 'DROP_CRITICAL_THRESHOLD', -0.08)),
            vix_warning_threshold=float(_cc_get(cc, 'risk_limits.vix_warning_threshold', 'VIX_WARNING_THRESHOLD', 30.0)),
            vix_critical_threshold=float(_cc_get(cc, 'risk_limits.vix_critical_threshold', 'VIX_CRITICAL_THRESHOLD', 40.0)),
            vix_cache_ttl_seconds=int(_cc_get(cc, 'risk_limits.vix_cache_ttl_seconds', 'VIX_CACHE_TTL_SECONDS', 600)),
            dynamic_exit_config_path=str(paths.config_dir / "unified_config.yaml"),
            threshold_config_path=str(paths.config_dir / "dynamic_threshold.json"),
            threshold_history_path=str(paths.data_dir / "threshold_history.json"),
            params_config_path=str(paths.config_dir / "market_regime_params.yaml"),
            runtime_params_path=str(paths.config_dir / "runtime_params.json"),
        )


# ============================================================
#  共识引擎配置容器
# ============================================================
@dataclass
class ConsensusConfig:
    """共识引擎配置"""
    signal_cooldown: int = 120              # 2026-04-16 对齐 unified_config.yaml (120s)
    consensus_window: int = 90              # 2026-04-15: 60→90, 对齐 unified_config.yaml
    consensus_threshold: float = 1.0        # 2026-05-06: 1.5→1.0, 对齐 unified_config.yaml
    adx_period: int = 14
    adx_trend: float = 25.0                 # ADX 高于此值 = 趋势市
    adx_range: float = 20.0                 # ADX 低于此值 = 震荡市
    max_open_positions: int = 20            # 对齐 YAML trading.max_open_positions (20)
    max_positions_per_sector: int = 4       # 2026-04-15 新增: 单行业最大持仓数
    max_add_per_position: int = 2          # 2026-04-15 新增: 单股最大补仓次数
    max_add_multiplier: list = field(default_factory=lambda: [1.0, 0.5, 0.25])  # 金字塔补仓比例
    add_position_profit_threshold: float = 5.0   # 2026-04-15 新增: 盈利补仓阈值 (%)
    add_position_loss_threshold: float = -2.0    # 2026-04-15 新增: 亏损补仓阈值 (%)
    sector_alert_threshold: float = 30.0         # 2026-04-15 新增: 行业集中度告警 (%)
    sector_block_threshold: float = 40.0         # 2026-04-15 新增: 行业集中度拦截 (%)
    position_idle_hours: int = 48                # 2026-04-15 新增: 持仓超时巡检阈值
    
    # 2026-04-15 新增: 资金安全水位配置 (基准: 不破产)
    max_position_pct: float = 10.0          # 单股最大占总资金 % (黑天鹅 -50% 时仅损 5%)
    max_sector_pct: float = 25.0            # 单行业最大占总资金 % (行业 -40% 时仅损 10%)
    min_cash_reserve_pct: float = 10.0      # 最低现金缓冲 % (防极端行情)
    max_portfolio_drawdown: float = 0.12    # 2026-04-16 对齐 unified_config.yaml (0.12 = 12%)
    max_daily_loss_pct: float = 3.0         # 每日最大亏损 % (触发当日停交易)
    stop_loss_pct: float = 0.03             # 2026-04-16 对齐 unified_config.yaml stop_loss.default_percent (3%)
    take_profit_pct: float = 0.10           # 2026-04-16 对齐 unified_config.yaml take_profit.default_percent (10%)
    
    # 策略分类
    trend_strategies: set = field(default_factory=lambda: {"supertrend", "macd", "ma_cross"})
    oscillator_strategies: set = field(default_factory=lambda: {"rsi", "bollinger"})
    hybrid_strategies: set = field(default_factory=lambda: {"confluence", "fusion_v2", "fusion_v3"})
    sentiment_strategies: set = field(default_factory=lambda: {"sentiment", "sentiment_ai", "news_sentiment"})
    mtf_strategies: set = field(default_factory=lambda: {"mtf_15m", "mtf_30m", "mtf_60m", "mtf_day"})
    
    # 基础权重
    base_weights: Dict[str, float] = field(default_factory=lambda: {
        "trend": 0.3,
        "oscillator": 0.2,
        "hybrid": 0.25,
        "sentiment": 0.15,
        "mtf": 0.1,
    })
    
    @classmethod
    def from_env(cls) -> 'ConsensusConfig':
        """从 ConfigCenter 创建共识配置 (SSOT v4.1)"""
        cc = get_config_center()
        return cls(
            signal_cooldown=int(_cc_get(cc, 'strategy.cooldown.signal_cooldown', 'SIGNAL_COOLDOWN', 120)),
            consensus_window=int(_cc_get(cc, 'strategy.consensus.window_seconds', 'CONSENSUS_WINDOW', 90)),
            consensus_threshold=float(_cc_get(cc, 'strategy.consensus.threshold', 'CONSENSUS_THRESHOLD', 1.0)),
            adx_trend=float(_cc_get(cc, 'strategy.consensus.adx_trend', 'CONSENSUS_ADX_TREND', 25)),
            adx_range=float(_cc_get(cc, 'strategy.consensus.adx_range', 'CONSENSUS_ADX_RANGE', 20)),
            adx_period=int(_cc_get(cc, 'strategy.consensus.adx_period', 'ADX_PERIOD', 14)),
            max_open_positions=int(_cc_get(cc, 'trading.max_open_positions', 'MAX_OPEN_POSITIONS', 20)),
            max_positions_per_sector=int(_cc_get(cc, 'strategy.consensus.max_positions_per_sector', 'MAX_POSITIONS_PER_SECTOR', 4)),
            max_add_per_position=int(_cc_get(cc, 'strategy.consensus.max_add_per_position', 'MAX_ADD_PER_POSITION', 2)),
            add_position_profit_threshold=float(_cc_get(cc, 'strategy.consensus.add_position_profit_threshold', 'ADD_POSITION_PROFIT_THRESHOLD', 5.0)),
            add_position_loss_threshold=float(_cc_get(cc, 'strategy.consensus.add_position_loss_threshold', 'ADD_POSITION_LOSS_THRESHOLD', -2.0)),
            sector_alert_threshold=float(_cc_get(cc, 'strategy.consensus.sector_alert_threshold', 'SECTOR_ALERT_THRESHOLD', 30.0)),
            sector_block_threshold=float(_cc_get(cc, 'strategy.consensus.sector_block_threshold', 'SECTOR_BLOCK_THRESHOLD', 40.0)),
            position_idle_hours=int(_cc_get(cc, 'strategy.consensus.position_idle_hours', 'POSITION_IDLE_HOURS', 48)),
            max_position_pct=float(_cc_get(cc, 'strategy.consensus.max_position_pct', 'MAX_POSITION_PCT', 10.0)),
            max_sector_pct=float(_cc_get(cc, 'strategy.consensus.max_sector_pct', 'MAX_SECTOR_PCT', 25.0)),
            min_cash_reserve_pct=float(_cc_get(cc, 'strategy.consensus.min_cash_reserve_pct', 'MIN_CASH_RESERVE_PCT', 10.0)),
            max_portfolio_drawdown=float(_cc_get(cc, 'risk_limits.max_drawdown', 'MAX_DRAWDOWN', 0.12)),
            max_daily_loss_pct=float(_cc_get(cc, 'risk_limits.max_daily_loss_pct', 'MAX_DAILY_LOSS_PCT', 3.0)),
            stop_loss_pct=float(_cc_get(cc, 'stop_loss.default_percent', 'DEFAULT_STOP_LOSS_PCT', 0.03)),
            take_profit_pct=float(_cc_get(cc, 'take_profit.default_percent', 'DEFAULT_TAKE_PROFIT_PCT', 0.10)),
        )


# ============================================================
#  交易配置容器
# ============================================================
@dataclass
class TradeConfig:
    """交易相关配置"""
    # 订单配置
    default_order_qty_hk: int = 100
    default_order_qty_us: int = 1
    default_order_qty: int = 100
    default_order_type: str = "MARKET"
    max_order_amount: float = None  # SSOT: Derived from capital + max_order_pct
    
    # 港股每手股数 (P0 修复 2026-04-26: 从 LotSizeManager 动态获取)
    # 注: 这里保留空字典作为 fallback，实际值由 LotSizeManager 提供
    # LotSizeManager 优先级: Futu API → 缓存文件 → 配置文件 → 默认值
    _lot_size_manager: Optional['LotSizeManager'] = field(default=None, repr=False)
    
    # 默认下单数量 (按股票配置)
    default_order_qty_by_code: Dict[str, int] = field(default_factory=lambda: {
        'HK.00700': 100,
        'HK.09988': 100,
        'HK.03690': 100,
        'HK.09992': 200,
        'HK.00981': 500,
        'HK.01810': 200,
    })
    
    # 监控标的
    watch_list_hk: List[str] = field(default_factory=lambda: [
        "HK.00700", "HK.09988", "HK.03690", "HK.00981", "HK.01347",
        "HK.01810", "HK.01211", "HK.00388", "HK.02269", "HK.09992",
        "HK.02020", "HK.00941"
    ])
    watch_list_us: List[str] = field(default_factory=list)
    
    # 订阅类型
    subscribe_types: List[str] = field(default_factory=lambda: [
        "QUOTE", "K_1M", "K_3M", "K_5M", "K_15M", "K_30M", "K_60M", "K_DAY"
    ])
    
    # 卖空支持
    short_selling_enabled: bool = False
    
    @classmethod
    def from_env(cls) -> 'TradeConfig':
        """从 ConfigCenter 创建交易配置 (SSOT v4.1)"""
        cc = get_config_center()
        # watch_list 优先从 ConfigCenter / YAML 读取
        yaml_wl = cc.get('trading.watch_list_hk')
        if yaml_wl and isinstance(yaml_wl, list):
            watch_list_hk = yaml_wl
        else:
            env_wl = os.getenv("WATCH_LIST_HK")
            if env_wl:
                watch_list_hk = json.loads(env_wl)
            else:
                watch_list_hk = [
                    "HK.00700", "HK.09988", "HK.03690", "HK.00981", "HK.01347",
                    "HK.01810", "HK.01211", "HK.00388", "HK.02269", "HK.09992",
                    "HK.02020", "HK.00941"
                ]

        watch_list_us_raw = cc.get('trading.watch_list_us')
        watch_list_us = watch_list_us_raw if isinstance(watch_list_us_raw, list) else json.loads(os.getenv("WATCH_LIST_US", '[]'))

        sub_types_raw = cc.get('futu.subscribe_types')
        if sub_types_raw and isinstance(sub_types_raw, list):
            subscribe_types = sub_types_raw
        else:
            _subscribe_types_str = os.getenv("SUBSCRIBE_TYPES", 'QUOTE,K_1M,K_3M,K_5M,K_15M,K_30M,K_60M,K_DAY')
            if _subscribe_types_str.startswith('['):
                subscribe_types = json.loads(_subscribe_types_str)
            else:
                subscribe_types = [s.strip() for s in _subscribe_types_str.split(',')]

        return cls(
            default_order_qty_hk=int(_cc_get(cc, 'trading.order.default_order_qty_hk', 'DEFAULT_ORDER_QTY_HK', 100)),
            default_order_qty_us=int(_cc_get(cc, 'trading.order.default_order_qty_us', 'DEFAULT_ORDER_QTY_US', 1)),
            default_order_qty=int(_cc_get(cc, 'trading.order.default_order_qty', 'DEFAULT_ORDER_QTY', 100)),
            default_order_type=str(_cc_get(cc, 'trading.order.default_order_type', 'DEFAULT_ORDER_TYPE', "MARKET")),
            max_order_amount=None,
            watch_list_hk=watch_list_hk,
            watch_list_us=watch_list_us,
            subscribe_types=subscribe_types,
            short_selling_enabled=os.getenv('SHORT_SELLING_ENABLED', 'false').lower() in ('true', '1', 'yes'),
        )
    
    def get_lot_size(self, code: str) -> int:
        """获取股票每手股数 (P0 修复: 使用 LotSizeManager)"""
        try:
            from app.execution.lot_size_manager import get_lot_size_manager
            lsm = get_lot_size_manager()
            return lsm.get_lot_size(code)
        except Exception:
            # 终极降级
            return 100
    
    def get_order_qty(self, code: str) -> int:
        """获取股票默认下单数量"""
        if code in self.default_order_qty_by_code:
            return self.default_order_qty_by_code[code]
        return self.default_order_qty_hk if code.startswith("HK.") else self.default_order_qty_us


# ============================================================
#  API 配置容器
# ============================================================
@dataclass
class APIConfig:
    """API 相关配置"""
    api_token: str
    callback_host: str
    callback_port: int
    callback_external_url: str
    signal_expire_seconds: int
    tradingview_webhook_token: str
    ftseek_api_base: str
    ftseek_api_token: str
    
    @classmethod
    def from_env(cls) -> 'APIConfig':
        """从环境变量创建 API 配置"""
        import secrets
        
        api_token = os.getenv("API_TOKEN")
        if not api_token:
            if os.getenv("FTSEEK_DEV_MODE", "").lower() in ("1", "true", "yes"):
                api_token = secrets.token_hex(16)
                print(f"⚠️  警告：API_TOKEN 未设置，已生成临时 Token: {api_token}")
            else:
                raise RuntimeError(
                    "🔐 安全错误：API_TOKEN 环境变量未设置！\n"
                    "请执行：export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')"
                )
        
        callback_port = int(os.getenv("CALLBACK_PORT", "8899"))
        
        return cls(
            api_token=api_token,
            callback_host=os.getenv("CALLBACK_HOST", "0.0.0.0"),
            callback_port=callback_port,
            callback_external_url=os.getenv("CALLBACK_EXTERNAL_URL", f"http://39.97.58.4:{callback_port}"),
            signal_expire_seconds=int(os.getenv("SIGNAL_EXPIRE_SECONDS", "300")),
            tradingview_webhook_token=os.getenv("TRADINGVIEW_WEBHOOK_TOKEN", api_token),
            ftseek_api_base=os.getenv("FTSEEK_API_BASE", "http://127.0.0.1:8899"),
            ftseek_api_token=os.getenv("FTSEEK_API_TOKEN", "ftseek_secret_token_change_me"),
        )


# ============================================================
#  钉钉推送配置容器
# ============================================================
@dataclass
class DingTalkConfig:
    """钉钉推送配置"""
    webhook_url: str
    secret: str
    
    # 动作标签和颜色
    action_labels: Dict[str, str] = field(default_factory=lambda: {
        "confirm": "确认交易",
        "defer": "暂缓交易",
        "ignore": "忽略信号",
        "reject": "拒绝交易",
    })
    
    action_colors: Dict[str, str] = field(default_factory=lambda: {
        "confirm": "#32CD32",
        "defer": "#FFA500",
        "ignore": "#808080",
        "reject": "#DC143C",
    })
    
    @classmethod
    def from_env(cls) -> 'DingTalkConfig':
        """从环境变量创建钉钉配置"""
        webhook_url = os.getenv("DINGTALK_WEBHOOK_URL") or os.getenv("DINGTALK_WEBHOOK_URL_DEFAULT", "")
        secret = os.getenv("DINGTALK_SECRET") or os.getenv("DINGTALK_SECRET_DEFAULT", "")
        
        return cls(webhook_url=webhook_url, secret=secret)


# ============================================================
#  日志配置容器
# ============================================================
@dataclass
class LogConfig:
    """日志配置"""
    level: str
    log_file: Optional[Path] = None
    
    @classmethod
    def from_env(cls, paths: PathConfig) -> 'LogConfig':
        """从环境变量创建日志配置"""
        return cls(
            level=os.getenv("LOG_LEVEL", "INFO"),
            log_file=paths.log_dir / "ftseek.log",
        )


# ============================================================
#  新闻推送配置容器
# ============================================================
@dataclass
class NewsConfig:
    """新闻推送配置"""
    rss_feeds: Dict[str, str]
    source_priority: Dict[str, int]
    category_enabled: Dict[str, bool]
    push_keywords: Dict[str, List[str]]
    watch_stocks: List[str]
    stock_aliases: Dict[str, str]
    rate_limit: Dict[str, int]
    qwenv_config: Dict[str, Any]
    push_time_window: Dict[str, str]
    cache_config: Dict[str, Any]
    collection_interval: Dict[str, int]
    log_level: str
    log_file: Path
    storage_keywords: List[str]
    storage_stocks: List[str]
    storage_news_push: bool
    
    @classmethod
    def from_env(cls, paths: PathConfig) -> 'NewsConfig':
        """从环境变量创建新闻配置"""
        return cls(
            rss_feeds={},  # 从配置文件加载
            source_priority={},
            category_enabled={},
            push_keywords={},
            watch_stocks=json.loads(os.getenv("NEWS_WATCH_STOCKS", "[]")),
            stock_aliases={},
            rate_limit={"requests_per_minute": 60},
            qwenv_config={"model": "qwen-plus"},
            push_time_window={"start": "09:00", "end": "16:00"},
            cache_config={"max_size_mb": 100},
            collection_interval={"rss": 300, "api": 60},
            log_level=os.getenv("NEWS_LOG_LEVEL", "INFO"),
            log_file=paths.log_dir / "news_push.log",
            storage_keywords=[],
            storage_stocks=[],
            storage_news_push=True,
        )


# ============================================================
#  主配置容器
# ============================================================
class ConfigContainer:
    """
    配置容器 - 集中管理所有配置
    
    使用示例:
        config = ConfigContainer()
        
        # 访问路径配置
        data_dir = config.paths.data_dir
        
        # 访问风控配置
        stop_loss_threshold = config.risk.drop_warning_threshold
        
        # 访问交易配置
        lot_size = config.trade.get_lot_size("HK.09992")
        
        # 依赖注入
        def my_function(config: ConfigContainer):
            ...
    """
    
    def __init__(self, load_from_env: bool = True):
        """
        初始化配置容器
        
        Args:
            load_from_env: 是否从环境变量加载配置
        """
        self.paths = PathConfig.from_env() if load_from_env else PathConfig(
            base_dir=Path(__file__).resolve().parent.parent.parent,
            data_dir=Path("data"),
            log_dir=Path("logs"),
            config_dir=Path("config"),
            trade_records_dir=Path("data/trade_records"),
            signal_records_dir=Path("data/signal_records"),
            order_log_dir=Path("data/order_logs"),
            failure_stats_dir=Path("data/failure_stats"),
            prediction_tracking_dir=Path("data/prediction_tracking"),
            reports_dir=Path("data/reports"),
            vix_cache_dir=Path("data/vix_cache"),
            news_cache_dir=Path("data/news_cache"),
            strategy_versions_file=Path("data/strategy_versions.json"),
            strategy_params_file=Path("config/strategy_params.json"),
            config_backups_dir=Path("config/backups"),
            db_path=Path("data/ftseek.db"),
            futu_cp2_trade_journal=Path("/opt/futu_cp2/data/trade_journal.json"),
        )
        
        self.opend = OpenDConfig.from_env() if load_from_env else OpenDConfig(
            host="127.0.0.1", port=11111, dir="", login_account="", login_pwd="",
            trade_env=1, simulate_account_id="18820895",
        )
        
        self.risk = RiskConfig.from_env(self.paths) if load_from_env else RiskConfig()
        self.consensus = ConsensusConfig.from_env() if load_from_env else ConsensusConfig()
        self.trade = TradeConfig.from_env() if load_from_env else TradeConfig()
        self.api = APIConfig.from_env() if load_from_env else APIConfig(
            api_token="dev-token", callback_host="0.0.0.0", callback_port=8899,
            callback_external_url="http://localhost:8899", signal_expire_seconds=300,
            tradingview_webhook_token="", ftseek_api_base="http://127.0.0.1:8899",
            ftseek_api_token="dev-token",
        )
        self.dingtalk = DingTalkConfig.from_env() if load_from_env else DingTalkConfig(
            webhook_url="", secret="",
        )
        self.log = LogConfig.from_env(self.paths) if load_from_env else LogConfig(level="INFO")
        self.news = NewsConfig.from_env(self.paths) if load_from_env else NewsConfig(
            rss_feeds={}, source_priority={}, category_enabled={}, push_keywords={},
            watch_stocks=[], stock_aliases={}, rate_limit={}, qwenv_config={},
            push_time_window={}, cache_config={}, collection_interval={},
            log_level="INFO", log_file=self.paths.log_dir / "news_push.log",
            storage_keywords=[], storage_stocks=[], storage_news_push=True,
        )
        
        # 缓存已加载的配置路径
        self._loaded_paths: set = set()

        # 配置中心 (2026-04-09 收敛后的唯一配置源)
        self._config_center = None

    def _get_config_center(self):
        """延迟加载配置中心"""
        if self._config_center is None:
            from config.config_center import get_config_center
            self._config_center = get_config_center()
        return self._config_center

    def get(self, key: str, default: Any = None) -> Any:
        """从配置中心获取配置值"""
        config_center = self._get_config_center()
        return config_center.get(key, default)
    
    def get_path(self, name: str) -> Path:
        """
        获取路径配置
        
        Args:
            name: 路径名称
            
        Returns:
            对应的 Path 对象
            
        Raises:
            AttributeError: 如果路径不存在
        """
        if not hasattr(self.paths, name):
            raise AttributeError(f"路径配置 '{name}' 不存在")
        return getattr(self.paths, name)
    
    def validate(self) -> List[str]:
        """
        验证配置完整性
        
        Returns:
            错误消息列表，空列表表示配置有效
        """
        errors = []
        
        # 验证 API Token
        if not self.api.api_token or self.api.api_token.startswith("dev-"):
            errors.append("API_TOKEN 未设置或使用默认值")
        
        # 验证 OpenD 配置
        if not self.opend.host:
            errors.append("OPEND_HOST 未设置")
        
        # 验证路径
        required_dirs = [
            self.paths.data_dir,
            self.paths.log_dir,
            self.paths.trade_records_dir,
        ]
        for dir_path in required_dirs:
            try:
                dir_path.mkdir(exist_ok=True)
            except Exception as e:
                errors.append(f"无法创建目录 {dir_path}: {e}")
        
        return errors
    
    def to_dict(self) -> Dict[str, Any]:
        """将配置转换为字典（用于序列化）"""
        from dataclasses import asdict
        
        return {
            "paths": {k: str(v) for k, v in asdict(self.paths).items()},
            "opend": asdict(self.opend),
            "risk": asdict(self.risk),
            "consensus": {
                **asdict(self.consensus),
                "trend_strategies": list(self.consensus.trend_strategies),
                "oscillator_strategies": list(self.consensus.oscillator_strategies),
                "hybrid_strategies": list(self.consensus.hybrid_strategies),
                "sentiment_strategies": list(self.consensus.sentiment_strategies),
                "mtf_strategies": list(self.consensus.mtf_strategies),
            },
            "trade": asdict(self.trade),
            "api": {**asdict(self.api), "api_token": "***"},  # 隐藏 token
            "dingtalk": {**asdict(self.dingtalk), "secret": "***"},  # 隐藏 secret
            "log": asdict(self.log),
        }
    
    def __repr__(self) -> str:
        return f"ConfigContainer(paths={self.paths.base_dir}, trade_env={self.opend.trade_env})"


# ============================================================
#  全局配置实例 (向后兼容)
# ============================================================
# 注意：新代码应使用依赖注入，此全局实例仅用于向后兼容
_global_config: Optional[ConfigContainer] = None


def get_config() -> ConfigContainer:
    """获取全局配置实例（单例模式）"""
    global _global_config
    if _global_config is None:
        _global_config = ConfigContainer()
    return _global_config


def reset_config() -> None:
    """重置全局配置（用于测试）"""
    global _global_config
    _global_config = None
