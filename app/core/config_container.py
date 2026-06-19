"""
配置容器类 - 集中管理所有全局配置和常量

目标:
1. 替代分散在各模块的全局变量
2. 提供依赖注入支持
3. 添加访问控制和类型注解
4. 支持配置热重载
5. 统一 TRADE_ENV 判断 (模拟盘/实盘切换 SSOT)

使用示例:
    from app.core.config_container import ConfigContainer
    
    config = ConfigContainer()
    paths = config.paths
    risk = config.risk
    is_live = config.is_live()
    trade_env = config.trade_env  # TrdEnv enum
    value = config.get("risk", "MIN_CONFIDENCE", 0.70)
"""
import os
import logging
from enum import IntEnum
from pathlib import Path
from typing import Dict, List, Any, Optional
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


# ============================================================
#  交易模式抽象 (模拟盘/实盘切换 SSOT)
# ============================================================
class TradeMode(IntEnum):
    """交易模式枚举 — 模拟盘/实盘切换的唯一判定点"""
    SIMULATE = 1
    REAL = 0


def _resolve_trade_mode() -> TradeMode:
    """从环境变量解析交易模式（带安全拦截）

    D-DAY 修 (2026-05-17): TRADE_ENV 解析失败 → fallback SIMULATE (最安全默认).
    """
    try:
        _raw = int(os.getenv("TRADE_ENV", "1"))
    except (ValueError, TypeError):
        import logging as _lg
        _lg.getLogger(__name__).warning(
            "[TradeMode] TRADE_ENV 解析失败 (raw=%r), 强制 fallback SIMULATE (最安全)",
            os.getenv("TRADE_ENV"),
        )
        return TradeMode.SIMULATE
    _allow_real = os.getenv("ALLOW_REAL_TRADING", "false").lower() == "true"
    # 安全拦截：不允许未确认的实盘
    if _raw == 0 and not _allow_real:
        import warnings
        warnings.warn(
            "[安全拦截] TRADE_ENV=0 被忽略，当前强制使用模拟环境。",
            RuntimeWarning, stacklevel=3
        )
        return TradeMode.SIMULATE
    return TradeMode(_raw) if _raw in (0, 1) else TradeMode.SIMULATE


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
    
    # 数据库
    db_path: Path
    
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
            news_cache_dir=data_dir / "news_cache",
            db_path=data_dir / ("ftseek_us_live.db" if os.getenv("TRADE_ENV", "1").strip() == "0" else "ftseek_us.db"),
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
        """从环境变量创建 OpenD 配置.

        D-DAY 修 (2026-05-17): port 解析失败时 fallback 默认值, 不阻断 ConfigContainer.
        否则 .env 任一字段异常 → 整个 yaml 读取失效 → 所有依赖 get_config() 的模块全挂.
        """
        try:
            port = int(os.getenv("FUTU_OPEND_PORT", "11111"))
        except (ValueError, TypeError):
            import logging as _lg
            _lg.getLogger(__name__).warning(
                "[OpenDConfig] FUTU_OPEND_PORT 解析失败 (raw=%r), 用默认 11111",
                os.getenv("FUTU_OPEND_PORT"),
            )
            port = 11111
        return cls(
            host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
            port=port,
            dir=os.getenv("OPEND_DIR", "/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7"),
            login_account=os.getenv("FUTU_LOGIN_ACCOUNT", ""),
            login_pwd=os.getenv("FUTU_LOGIN_PWD", ""),
            trade_env=_resolve_trade_mode(),
            simulate_account_id=os.getenv("SIMULATE_ACCOUNT_ID", ""),
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
    vix_cache_ttl_seconds: int = 180  # 3 分钟
    
    # 冷却时间
    signal_cooldown_seconds: int = 300  # 5 分钟
    
    # 仓位限制
    max_position_pct: float = 0.25  # 单只股票最大仓位 25%
    max_total_position_pct: float = 0.80  # 总仓位上限 80%
    
    # 止损
    stop_loss_pct: float = -0.08  # -8% 止损
    
    @classmethod
    def from_env(cls) -> 'RiskConfig':
        """从环境变量创建风控配置"""
        return cls(
            drop_warning_threshold=float(os.getenv("DROP_WARNING_THRESHOLD", "-0.05")),
            drop_critical_threshold=float(os.getenv("DROP_CRITICAL_THRESHOLD", "-0.08")),
            vix_warning_threshold=float(os.getenv("VIX_WARNING_THRESHOLD", "30.0")),
            vix_critical_threshold=float(os.getenv("VIX_CRITICAL_THRESHOLD", "40.0")),
            vix_cache_ttl_seconds=int(os.getenv("VIX_CACHE_TTL_SECONDS", "180")),
            signal_cooldown_seconds=int(os.getenv("SIGNAL_COOLDOWN_SECONDS", "300")),
            max_position_pct=float(os.getenv("MAX_POSITION_PCT", "0.25")),
            max_total_position_pct=float(os.getenv("MAX_TOTAL_POSITION_PCT", "0.80")),
            stop_loss_pct=float(os.getenv("STOP_LOSS_PCT", "-0.08")),
        )


# ============================================================
#  数据源配置容器
# ============================================================
@dataclass
class DataSourceConfig:
    """数据源配置"""
    # AlphaVantage
    alphavantage_api_key: str = ""
    alphavantage_base_url: str = "https://www.alphavantage.co/query"
    alphavantage_calls_per_minute: int = 5
    alphavantage_calls_per_day: int = 500
    
    # Finnhub
    finnhub_api_key: str = ""
    finnhub_base_url: str = "https://finnhub.io/api/v1"
    finnhub_calls_per_minute: int = 60
    
    # TwelveData
    twelvedata_api_key: str = ""
    twelvedata_base_url: str = "https://api.twelvedata.com"
    twelvedata_credits_per_minute: int = 8
    
    @classmethod
    def from_env(cls) -> 'DataSourceConfig':
        """从环境变量创建数据源配置"""
        return cls(
            alphavantage_api_key=os.getenv("ALPHAVANTAGE_API_KEY", ""),
            finnhub_api_key=os.getenv("FINNHUB_API_KEY", ""),
            twelvedata_api_key=os.getenv("TWELVEDATA_API_KEY", ""),
        )


# ============================================================
#  Agent 配置容器
# ============================================================
@dataclass
class AgentConfig:
    """多 Agent 配置"""
    # Agent 权重
    coordinator_weight: float = 1.0
    analyst_weight: float = 0.8
    strategist_weight: float = 0.9
    risk_officer_weight: float = 1.0
    executor_weight: float = 0.7
    
    # 共识阈值
    consensus_threshold: float = 0.60
    min_strategies: int = 3
    
    # 超时设置
    agent_timeout_seconds: int = 30
    
    @classmethod
    def from_env(cls) -> 'AgentConfig':
        """从环境变量创建 Agent 配置"""
        return cls(
            consensus_threshold=float(os.getenv("CONSENSUS_THRESHOLD", "0.60")),
            min_strategies=int(os.getenv("MIN_STRATEGIES", "3")),
            agent_timeout_seconds=int(os.getenv("AGENT_TIMEOUT_SECONDS", "30")),
        )


# ============================================================
#  通知配置容器
# ============================================================
@dataclass
class NotificationConfig:
    """通知配置"""
    # 钉钉
    dingtalk_webhook: str = ""
    dingtalk_enabled: bool = True
    
    # 飞书
    feishu_webhook: str = ""
    feishu_enabled: bool = True
    
    # 告警级别
    alert_levels: List[str] = field(default_factory=lambda: ["warning", "critical"])
    
    @classmethod
    def from_env(cls) -> 'NotificationConfig':
        """从环境变量创建通知配置"""
        return cls(
            dingtalk_enabled=os.getenv("DINGTALK_ENABLED", "true").lower() == "true",
            feishu_enabled=os.getenv("FEISHU_ENABLED", "true").lower() == "true",
        )


# ============================================================
#  配置容器主类
# ============================================================
class ConfigContainer:
    """
    配置容器
    
    集中管理所有配置，提供依赖注入支持
    新增: TradeMode 抽象层 (模拟盘/实盘切换 SSOT)
    """
    
    _instance: Optional['ConfigContainer'] = None
    _initialized: bool = False
    
    def __new__(cls) -> 'ConfigContainer':
        """单例模式"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance
    
    def __init__(self):
        """初始化配置容器"""
        if self._initialized:
            return
        
        self.paths = PathConfig.from_env()
        self.opend = OpenDConfig.from_env()
        self.risk = RiskConfig.from_env()
        self.data_source = DataSourceConfig.from_env()
        self.agent = AgentConfig.from_env()
        self.notification = NotificationConfig.from_env()
        
        # ── TradeMode 抽象层 (模拟盘/实盘切换 SSOT) ──
        self._trade_mode = _resolve_trade_mode()
        
        # ── 统一配置字典 (从 unified_config.yaml 加载) ──
        self._config_dict: Dict[str, Any] = {}
        self._yaml_raw: Dict[str, Any] = {}  # 原始 YAML 结构 (保留嵌套)
        self._load_unified_config()
        
        self._initialized = True
    
    # ============================================================
    #  TradeMode 抽象层 — 模拟盘/实盘切换的唯一判定点
    # ============================================================
    @property
    def trade_env(self) -> TradeMode:
        """交易模式: TradeMode.SIMULATE (1) 或 TradeMode.REAL (0)"""
        return self._trade_mode
    
    @property
    def trade_env_int(self) -> int:
        """交易模式整数值 (兼容 Futu SDK: 0=实盘, 1=模拟)"""
        return int(self._trade_mode)
    
    def is_live(self) -> bool:
        """是否为实盘模式"""
        return self._trade_mode == TradeMode.REAL
    
    def is_simulated(self) -> bool:
        """是否为模拟盘模式"""
        return self._trade_mode == TradeMode.SIMULATE
    
    # ============================================================
    #  统一配置访问 — 替代 sys.modules.get("config_us")
    # ============================================================
    def get(self, path_or_section: str, key_or_default: Any = None, default: Any = None) -> Any:
        """
        统一配置访问接口 — 兼容两种调用模式

        模式 1 (推荐, 兼容 ConfigLoader): config.get("risk.signal_quality.min_confidence", 0.50)
        模式 2 (历史兼容): config.get("risk", "MIN_CONFIDENCE", 0.5)

        Args:
            path_or_section: 点分隔路径 (模式1) 或配置节名 (模式2)
            key_or_default: 默认值 (模式1) 或配置键名 (模式2)
            default: 默认值 (仅模式2使用)

        Returns:
            配置值
        """
        # 检测调用模式: 模式1只有一个默认参数, 模式2有key+default两个参数
        if key_or_default is not None and default is not None:
            # 模式2: get(section, key, default)
            section_data = self._config_dict.get(path_or_section)
            if section_data is None:
                section_data = self._config_dict.get(path_or_section + "_config", {})
            if isinstance(section_data, dict):
                return section_data.get(key_or_default, default)
            return default
        # 模式1: get(dotted_path, default)
        return self.get_dotted(path_or_section, key_or_default)

    def get_dotted(self, path: str, default: Any = None) -> Any:
        """
        点号分隔的嵌套路径访问 unified_config.yaml 中的任意深度配置

        直接读取 YAML 原始结构，支持任意嵌套层级。

        Args:
            path: 点号分隔的路径，如 "decision.signal_thresholds.min_score"
            default: 路径不存在时返回的默认值

        Returns:
            配置值

        示例:
            config.get_dotted("decision.signal_thresholds.min_score")    # 50
            config.get_dotted("risk.account.max_daily_drawdown")          # 0.03
            config.get_dotted("execution.slippage.max_ratio")             # 0.005
            config.get_dotted("scheduler.scan_interval_minutes", 3)       # 默认值 3
        """
        parts = path.lower().split(".")
        # 直接从 YAML 原始结构中查找 (保留嵌套层级)
        value = self._yaml_raw
        for i, part in enumerate(parts):
            if isinstance(value, dict):
                # 先精确匹配
                if part in value:
                    value = value[part]
                else:
                    # 尝试 _config 后缀 (兼容 config_loader 导出键)
                    alt_key = part + "_config"
                    if alt_key in value:
                        value = value[alt_key]
                    else:
                        return default
            else:
                return default
        return value if value is not None else default

    def get_section(self, section: str, default: Optional[Dict] = None) -> Dict[str, Any]:
        """获取完整配置节（优先从 YAML 原始结构读取）"""
        # 优先从 YAML 读取（保留嵌套）
        yaml_val = self._yaml_raw.get(section.lower())
        if isinstance(yaml_val, dict):
            return yaml_val
        # 退回到 config_loader 导出的扁平字典
        return self._config_dict.get(section, default or {})
    
    @staticmethod
    def _expand_env_vars(value: Any) -> Any:
        """递归展开配置中的 ${VAR} 环境变量占位符"""
        if isinstance(value, str):
            return os.path.expandvars(value)
        elif isinstance(value, dict):
            return {k: ConfigContainer._expand_env_vars(v) for k, v in value.items()}
        elif isinstance(value, list):
            return [ConfigContainer._expand_env_vars(item) for item in value]
        return value

    def _load_unified_config(self) -> None:
        """从 unified_config.yaml 加载全部配置到内存字典"""
        try:
            from config.config_loader import get_config
            loader = get_config()
            # 将 loader 的所有大写属性导出为配置节
            for attr_name in dir(loader):
                if attr_name.startswith("_"):
                    continue
                attr = getattr(loader, attr_name)
                if isinstance(attr, (dict, list)):
                    # 统一用小写名访问: RISK_CONFIG -> risk_config, WATCH_LIST_US -> watchlist_us
                    self._config_dict[attr_name.lower()] = attr
            # 同时加载 unified_config.yaml 全量配置 (补充 loader 未导出的节)
            try:
                import yaml
                yaml_path = Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"
                if yaml_path.exists():
                    with open(yaml_path) as f:
                        full_cfg = yaml.safe_load(f) or {}
                    # 展开 ${VAR} 环境变量占位符 (修复 DingTalk webhook URL 等)
                    full_cfg = ConfigContainer._expand_env_vars(full_cfg)
                    # 保存原始 YAML 结构 (供 get_dotted 嵌套访问)
                    self._yaml_raw = {k.lower(): v for k, v in full_cfg.items()}
                    # 合并全量配置 (loader 已导出的优先)
                    for key, val in full_cfg.items():
                        k = key.lower()
                        if k not in self._config_dict and isinstance(val, (dict, list)):
                            self._config_dict[k] = val
            except Exception as e:
                logger.debug(f"[ConfigContainer] YAML 补充加载失败: {e}")
            logger.info(
                "[ConfigContainer] 已加载 %d 个配置节",
                len(self._config_dict)
            )
        except Exception as e:
            logger.warning(f"[ConfigContainer] unified_config 加载失败: {e}")
    
    @classmethod
    def get_instance(cls) -> 'ConfigContainer':
        """获取单例实例"""
        if cls._instance is None:
            cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset(cls):
        """重置配置（用于测试）"""
        cls._instance = None
    
    def reload(self):
        """重新加载配置"""
        self._initialized = False
        self.__init__()

    # ── 兼容 config_mgmt 模块 (期望 ConfigLoader 接口) ──
    @property
    def _raw_config(self) -> Dict[str, Any]:
        """兼容 config_mgmt: 返回原始配置字典"""
        return self._yaml_raw

    @property
    def _config_path(self) -> Path:
        """兼容 config_mgmt: 返回配置文件路径"""
        return Path(__file__).resolve().parent.parent.parent / "config" / "unified_config.yaml"

    def _load(self):
        """兼容 config_mgmt: 重新加载配置"""
        self.reload()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典"""
        return {
            'paths': {
                'base_dir': str(self.paths.base_dir),
                'data_dir': str(self.paths.data_dir),
                'log_dir': str(self.paths.log_dir),
                'db_path': str(self.paths.db_path),
            },
            'risk': {
                'drop_warning_threshold': self.risk.drop_warning_threshold,
                'drop_critical_threshold': self.risk.drop_critical_threshold,
                'vix_warning_threshold': self.risk.vix_warning_threshold,
                'max_position_pct': self.risk.max_position_pct,
            },
            'agent': {
                'consensus_threshold': self.agent.consensus_threshold,
                'min_strategies': self.agent.min_strategies,
            },
        }
    
    def validate(self) -> List[str]:
        """
        验证配置
        
        Returns:
            错误信息列表，空列表表示配置有效
        """
        errors = []
        
        # 验证路径
        if not self.paths.base_dir.exists():
            errors.append(f"Base directory does not exist: {self.paths.base_dir}")
        
        # 验证数据源配置
        if not self.data_source.alphavantage_api_key:
            errors.append("AlphaVantage API key is not set")
        
        # 验证风控配置
        if self.risk.max_position_pct > 1.0 or self.risk.max_position_pct < 0:
            errors.append(f"Invalid max_position_pct: {self.risk.max_position_pct}")
        
        if self.risk.stop_loss_pct > 0:
            errors.append(f"Stop loss should be negative: {self.risk.stop_loss_pct}")
        
        return errors


# ============================================================
#  全局配置实例 (向后兼容)
# ============================================================
_config_instance: Optional[ConfigContainer] = None


def get_config(config_path: str = None) -> ConfigContainer:
    """
    获取全局配置实例 (兼容 ConfigLoader 接口)

    Args:
        config_path: 配置文件路径 (忽略，始终使用默认路径)

    Returns:
        ConfigContainer 实例
    """
    global _config_instance
    if _config_instance is None:
        _config_instance = ConfigContainer()
    return _config_instance


def reload_config():
    """重新加载全局配置"""
    global _config_instance
    _config_instance = None
    return get_config()


# ============================================================
#  手动/自管标的合并查询 (yaml + DB owner='manual')
#  SSOT: 所有子系统统一调用此函数，不再各自只读 yaml
# ============================================================

_manual_symbols_cache: Optional[set] = None
_manual_symbols_cache_ts: float = 0.0
_MANUAL_SYMBOLS_TTL = 60.0  # 60s cache


def get_all_manual_symbols() -> set:
    """
    返回当前所有手动/自管标的集合 (yaml self_managed_symbols ∪ DB owner='manual')。
    60s TTL cache，避免每次 sync 循环都查 DB。
    """
    global _manual_symbols_cache, _manual_symbols_cache_ts
    import time
    now = time.time()
    if _manual_symbols_cache is not None and (now - _manual_symbols_cache_ts) < _MANUAL_SYMBOLS_TTL:
        return _manual_symbols_cache

    result = set()

    # 1. yaml 配置 (静态列表)
    try:
        cfg = get_config()
        _sm = cfg.get("risk.self_managed_symbols", [])
        if _sm:
            result.update(_sm)
    except Exception:
        pass

    # 2. DB owner='manual' (动态: web 手动下单的新标的)
    try:
        import sqlite3
        from app.core.db_utils import get_main_db
        db_path = str(get_main_db())
        conn = sqlite3.connect(db_path, timeout=5)
        rows = conn.execute(
            "SELECT DISTINCT symbol FROM open_positions WHERE owner='manual'"
        ).fetchall()
        conn.close()
        for row in rows:
            result.add(row[0])
    except Exception:
        pass

    _manual_symbols_cache = result
    _manual_symbols_cache_ts = now
    return result
