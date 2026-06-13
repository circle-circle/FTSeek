"""
股票名单管理器 - 统一读取watchlist_us.json

所有模块都应该使用这个接口获取股票名单,避免硬编码
"""
import json
import logging
from pathlib import Path
from typing import List, Dict, Any, Optional

logger = logging.getLogger(__name__)


class WatchlistManager:
    """股票名单管理器"""
    
    def __init__(self, config_path: Optional[str] = None):
        """
        初始化
        
        Args:
            config_path: 配置文件路径,默认使用config/watchlist_us.json
        """
        if config_path is None:
            # 默认路径: FTSeek-US/config/watchlist_us.json
            config_path = Path(__file__).parent.parent.parent / "config" / "watchlist_us.json"
        
        self.config_path = Path(config_path)
        self._watchlist_data = None
        self._load_watchlist()
    
    def _load_watchlist(self):
        """加载股票名单配置 (SSOT: 统一从 unified_config.yaml 读取)"""
        try:
            # P0 修复 (2026-05-03): 弃用 watchlist_us.json，统一从 unified_config.yaml 读取
            try:
                from app.core.config_container import get_config
                cfg = get_config()
                
                # 尝试从 YAML 加载
                self._watchlist_data = cfg.get_dotted("watchlist", {"us_stocks": [], "etfs": []})
                
                # 确保结构兼容性 (YAML 结构可能略有不同)
                if "us_stocks" not in self._watchlist_data:
                    self._watchlist_data["us_stocks"] = []
                if "etfs" not in self._watchlist_data:
                    self._watchlist_data["etfs"] = []
                
                logger.info(f"✅ 股票名单已从 unified_config.yaml 加载")
                
            except Exception as yaml_err:
                # Fallback: 如果 YAML 加载失败，尝试加载旧 JSON (向后兼容)
                logger.warning(f"从 YAML 加载 watchlist 失败 ({yaml_err})，回退到 JSON")
                
                if not self.config_path.exists():
                    logger.warning(f"股票名单配置文件不存在: {self.config_path}")
                    self._watchlist_data = {"us_stocks": [], "etfs": []}
                    return
                
                with open(self.config_path, 'r', encoding='utf-8') as f:
                    self._watchlist_data = json.load(f)
                
                logger.info(f"✅ 股票名单已从 JSON 回退加载: {self.config_path}")

        except Exception as e:
            logger.error(f"加载股票名单最终失败: {e}")
            self._watchlist_data = {"us_stocks": [], "etfs": []}
    
    def reload(self):
        """重新加载配置 (支持热更新)"""
        logger.info("重新加载股票名单...")
        self._load_watchlist()
    
    def _get_global_blacklist(self) -> List[str]:
        """读取全局黑名单 (P0 2026-05-30)

        从 unified_config.yaml 的 risk.global_blacklist 读取并去除 US. 前缀，
        返回仅含纯代码的黑名单集合。
        """
        try:
            from app.core.config_container import get_config
            raw = get_config().get_dotted("risk.global_blacklist", []) or []
            return [str(s).replace("US.", "") for s in raw]
        except Exception as e:
            logger.debug(f"[BLACKLIST] watchlist_manager 读取全局黑名单失败: {e}")
            return []

    def get_enabled_stocks(self) -> List[Dict[str, Any]]:
        """
        获取启用的股票列表 (三重过滤: enabled + 不在全局黑名单 + 不在自管列表)
        
        Returns:
            [
                {"code": "US.TSLA", "name": "Tesla Inc", "order_qty": 50, ...},
                ...
            ]
        """
        stocks = self._watchlist_data.get("us_stocks", [])
        enabled = [s for s in stocks if s.get("enabled", False)]
        # P0 (2026-05-30): 双重过滤 — enabled 且不在全局黑名单
        blacklist = self._get_global_blacklist()
        filtered = [
            s for s in enabled
            if s.get("code", "").replace("US.", "") not in blacklist
        ] if blacklist else enabled
        blocked = [s.get("code") for s in enabled if s not in filtered]
        if blocked:
            logger.info(f"[BLACKLIST] watchlist 过滤拦截 {len(blocked)} 个黑名单标的: {blocked}")
        
        # 2026-06-13: 三重过滤 — 排除 self_managed_symbols (AMD/AAPL等用户自管)
        # 防止这些标的进入扫描池浪费API预算，或绕过信号管道的self_managed检查
        try:
            from app.core.config_container import get_config
            self_managed = get_config().get_dotted("risk.self_managed_symbols", []) or []
            if self_managed:
                before = len(filtered)
                filtered = [
                    s for s in filtered
                    if s.get("code", "") not in self_managed
                ]
                skipped = before - len(filtered)
                if skipped:
                    logger.info(f"[SELF_MANAGED] watchlist 过滤 {skipped} 个自管标的")
        except Exception:
            pass
        
        return filtered
    
    def get_stock_codes(self) -> List[str]:
        """
        获取所有启用标的代码列表 (股票+ETF)
        
        P0 (2026-06-04): 原实现仅返回股票, 遗漏8只ETF未进入信号扫描
        修复: 使用 get_all_enabled() 返回股票+ETF 合计36只
        
        Returns:
            ["US.TSLA", "US.SPY", "US.XLF", ...]
        """
        return [s["code"] for s in self.get_all_enabled()]
    
    def get_stock_symbols(self) -> List[str]:
        """
        获取纯股票代码 (去掉US.前缀)
        
        Returns:
            ["TSLA", "NVDA", ...]
        """
        codes = self.get_stock_codes()
        return [code.replace("US.", "") for code in codes]
    
    def get_etfs(self) -> List[Dict[str, Any]]:
        """获取启用的ETF列表"""
        etfs = self._watchlist_data.get("etfs", [])
        return [e for e in etfs if e.get("enabled", False)]
    
    def get_stock_config(self, symbol: str) -> Optional[Dict[str, Any]]:
        """
        获取单只股票的配置
        
        Args:
            symbol: 股票代码 (如 "US.TSLA" 或 "TSLA")
        
        Returns:
            股票配置字典,如果未找到返回None
        """
        # 标准化代码
        if not symbol.startswith("US."):
            symbol = f"US.{symbol}"
        
        stocks = self._watchlist_data.get("us_stocks", [])
        for stock in stocks:
            if stock.get("code") == symbol:
                return stock
        
        return None
    
    def get_order_qty(self, symbol: str, default_qty: int = 50) -> int:
        """
        获取下单数量
        
        Args:
            symbol: 股票代码
            default_qty: 默认数量
        
        Returns:
            下单数量
        """
        config = self.get_stock_config(symbol)
        if config:
            return config.get("order_qty", default_qty)
        return default_qty
    
    def get_stop_loss(self, symbol: str, default_sl: float = -0.10) -> float:
        """获取止损比例"""
        config = self.get_stock_config(symbol)
        if config:
            return config.get("stop_loss", default_sl)
        return default_sl
    
    def get_take_profit(self, symbol: str, default_tp: float = 0.20) -> float:
        """获取止盈比例"""
        config = self.get_stock_config(symbol)
        if config:
            return config.get("take_profit", default_tp)
        return default_tp
    
    def get_max_position_ratio(self, symbol: str, default_ratio: float = 0.20) -> float:
        """获取最大仓位比例"""
        config = self.get_stock_config(symbol)
        if config:
            return config.get("max_position_ratio", default_ratio)
        return default_ratio
    
    def get_all_enabled(self) -> List[Dict[str, Any]]:
        """获取所有启用的标的 (股票+ETF)"""
        stocks = self.get_enabled_stocks()
        etfs = self.get_etfs()
        return stocks + etfs
    
    def summary(self) -> str:
        """生成配置摘要 (SSOT: 动态仓位/止损，不再显示硬编码值)"""
        stocks = self.get_enabled_stocks()
        etfs = self.get_etfs()
        
        lines = [
            "📊 股票名单配置摘要",
            f"数据源: unified_config.yaml (SSOT)",
            f"",
            f"股票: {len(stocks)} 只",
        ]
        
        for stock in stocks:
            code = stock.get("code", "?")
            name = stock.get("name", "?")
            tier = stock.get("tier", "?")
            max_pos = stock.get("max_position_ratio", "?")
            
            lines.append(f"  • {code} ({name})")
            lines.append(f"    分层: {tier} | 最大仓位: {max_pos:.0%} | 止损/数量: 动态计算")
        
        if etfs:
            lines.append(f"\nETF: {len(etfs)} 只")
            for etf in etfs:
                lines.append(f"  • {etf.get('code', '?')} ({etf.get('name', '?')})")
        
        return "\n".join(lines)


# ============================================================================
# 全局单例
# ============================================================================

_instance: Optional[WatchlistManager] = None


def get_watchlist_manager() -> WatchlistManager:
    """获取全局股票名单管理器实例"""
    global _instance
    if _instance is None:
        _instance = WatchlistManager()
    return _instance


def get_enabled_stocks() -> List[Dict[str, Any]]:
    """快捷函数: 获取启用的股票列表"""
    return get_watchlist_manager().get_enabled_stocks()


def get_stock_codes() -> List[str]:
    """快捷函数: 获取股票代码列表"""
    return get_watchlist_manager().get_stock_codes()


def get_stock_symbols() -> List[str]:
    """快捷函数: 获取纯股票代码"""
    return get_watchlist_manager().get_stock_symbols()


def get_order_qty(symbol: str, default_qty: int = 50) -> int:
    """快捷函数: 获取下单数量"""
    return get_watchlist_manager().get_order_qty(symbol, default_qty)


def get_stop_loss(symbol: str, default_sl: float = -0.10) -> float:
    """快捷函数: 获取止损比例"""
    return get_watchlist_manager().get_stop_loss(symbol, default_sl)


def get_take_profit(symbol: str, default_tp: float = 0.20) -> float:
    """快捷函数: 获取止盈比例"""
    return get_watchlist_manager().get_take_profit(symbol, default_tp)


def get_watchlist_with_tiers() -> Dict[str, List[Dict[str, Any]]]:
    """
    Livermore 融合 (2026-04-16): 获取带分层信息的完整 watchlist
    
    P2 扩展 (2026-04-19): 支持从龙头股票池动态获取
    """
    manager = get_watchlist_manager()
    result = {
        'us_stocks': manager.get_enabled_stocks(),
        'etfs': manager.get_etfs(),
        'sectors': manager._watchlist_data.get('sectors', []),
        'tiers': manager._watchlist_data.get('tiers', {}),
    }

    # P2 扩展: 如果龙头池管理器可用，合并龙头标的
    try:
        from app.utils.dragonhead_pool import get_dragonhead_pool, MarketRegime
        dragon_pool = get_dragonhead_pool()
        existing_codes = set(s["code"] for s in result["us_stocks"])

        # 添加龙头池中不在现有 watchlist 中的标的 (去重)
        added_codes = set()
        for stock in dragon_pool.get_active_pool("US"):
            if stock.code not in existing_codes and stock.code not in added_codes:
                added_codes.add(stock.code)
                result["us_stocks"].append({
                    "code": stock.code,
                    "name": stock.name,
                    "sector": stock.sector_name,
                    "tier": f"TIER{stock.tier}",
                    "enabled": True,
                    "order_qty": 50,
                    "max_position_ratio": 0.10,
                    "stop_loss": -0.08,
                    "take_profit": 0.20,
                    "notes": f"龙头池 ({stock.sector_name}) - {stock.notes}",
                    "_source": "dragonhead_pool",
                })
                logger.info(f"[龙头池] 新增动态标的: {stock.code} {stock.name} ({stock.sector_name})")

        if added_codes:
            logger.info(f"[龙头池] 共新增 {len(added_codes)} 只龙头标的")
    except ImportError:
        logger.debug("[龙头池] 模块未加载，使用静态 watchlist")
    except Exception as e:
        logger.warning(f"[龙头池] 合并失败: {e}")

    return result


def reload_watchlist():
    """快捷函数: 重新加载配置"""
    get_watchlist_manager().reload()


if __name__ == "__main__":
    # 测试
    manager = WatchlistManager()
    print(manager.summary())
    
    print("\n📋 股票代码列表:")
    print(manager.get_stock_codes())
    
    print("\n📋 纯代码列表:")
    print(manager.get_stock_symbols())
    
    print("\n📋 TSLA配置:")
    print(manager.get_stock_config("TSLA"))
