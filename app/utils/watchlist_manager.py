"""
股票名单管理器 - 统一读取watchlist_us.json

所有模块都应该使用这个接口获取股票名单,避免硬编码
"""
import json
import logging
import os
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
            # RFC-218 §22 (P2): debug→warning+栈 — 内部吞异常必须对外层守卫/日志可见,
            #   否则配置异常静默变空名单, 外层 warning 接不到真实异常。
            #   行为保持 fail-open 返回空列表 (异常即停新仓须另行裁决, 本批不改)。
            logger.warning(f"[BLACKLIST] watchlist_manager 读取全局黑名单失败 (fail-open 空名单): {e}",
                           exc_info=True)
            return []

    def _apply_exclusions(self, items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """DEB-20260904 #136: 黑名单+自管过滤抽公共 — 股票与 ETF 同一口径.

        此前 get_etfs 只过 enabled, 黑名单/自管 ETF 经 get_all_enabled 漏进扫描池.
        """
        blacklist = self._get_global_blacklist()
        filtered = [
            s for s in items
            if s.get("code", "").replace("US.", "") not in blacklist
        ] if blacklist else items
        blocked = [s.get("code") for s in items if s not in filtered]
        if blocked:
            logger.info(f"[BLACKLIST] watchlist 过滤拦截 {len(blocked)} 个黑名单标的: {blocked}")

        try:
            from app.core.config_container import get_config
            self_managed = get_config().get_dotted("risk.self_managed_symbols", []) or []
            if self_managed:
                before = len(filtered)
                # DEB-20260903 #9: 剥 US. 前缀再比对 — 与上方黑名单过滤口径一致,
                # 原直接比对带前缀 code 与裸名 self_managed 可能永不命中
                _sm_bare = {m.replace("US.", "") for m in self_managed}
                filtered = [
                    s for s in filtered
                    if s.get("code", "").replace("US.", "") not in _sm_bare
                ]
                skipped = before - len(filtered)
                if skipped:
                    logger.info(f"[SELF_MANAGED] watchlist 过滤 {skipped} 个自管标的")
        except Exception:
            pass

        return filtered

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
        return self._apply_exclusions(enabled)
    
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
        """获取启用的ETF列表 (DEB-20260904 #136: 与股票同一黑名单/自管口径)"""
        etfs = self._watchlist_data.get("etfs", [])
        enabled = [e for e in etfs if e.get("enabled", False)]
        return self._apply_exclusions(enabled)

    def get_symbols_by_role(self, role: str) -> List[str]:
        """按角色标签取标的 (P-1 名单合一 2026-08-01, DEB-20260724 §7).

        topup 补量池 / 订阅集合 / 基准 从 watchlist 块同一真源派生, 防双名单漂移.
        角色: benchmark | liquidity_whitelist | topup_eligible | subscribe.
        不过滤 enabled — NFLX/QCOM 暂停扫描但仍 topup_eligible; SPY/QQQ enabled=false 但是基准.
        """
        out = []
        for entry in (self._watchlist_data.get("us_stocks", [])
                      + self._watchlist_data.get("etfs", [])):
            if role in (entry.get("roles") or []):
                code = entry.get("code")
                if code:
                    out.append(code)
        return out
    
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


def get_symbols_by_role(role: str) -> List[str]:
    """快捷函数: 按角色标签取标的 (P-1 名单合一, 真源派生用)"""
    return get_watchlist_manager().get_symbols_by_role(role)


def _blacklist_guard_enabled() -> bool:
    """RFC-218 §16 独立开关: PLAYBOOK_BLACKLIST_GUARD (进程 env, systemd 注入 .env.us)。

    默认 OFF; 仅显式 truthy 值开启, 未知值 fail-closed 回到旧行为 (与 s1_arm.enabled
    同一 truthy 口径)。每次实读, 不缓存。
    """
    v = os.environ.get("PLAYBOOK_BLACKLIST_GUARD", "0")
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def filter_global_blacklist(symbols: List[str]) -> List[str]:
    """RFC-218 §16: Playbook 新候选边界全局黑名单守卫 (默认 OFF)。

    病灶 (§13.2-B/§14.4 确认): 静态侧 get_stock_codes 已滤 risk.global_blacklist,
    但 scheduler.watchlist 经 daily_active_pool 合并、runtime_context 纯加法合并、
    topup roles.topup_eligible 派生三条路径均无复核 → 被禁标的可回入新候选。

    语义:
      - 开关 OFF/未知值 → 原样返回输入 (同一对象, 逐字节旧行为);
      - 开关 ON → 复用 _get_global_blacklist() 谓词 (剥 US. 前缀, 与 _apply_exclusions
        同口径) 过滤被禁标的;
      - 只消费 risk.global_blacklist (明确禁止语义); 不动 enabled=false 普通暂停,
        不动 self_managed (后者仍由静态侧 _apply_exclusions 负责);
      - 黑名单读取失败/为空 → fail-open 原样返回 (与 _get_global_blacklist 同口径,
        守卫故障不瘫痪候选面)。

    不触碰持仓/退出链: 本函数只用于新候选名单, 持仓管理仍读原名单/DB。
    """
    if not _blacklist_guard_enabled():
        return symbols
    blacklist = get_watchlist_manager()._get_global_blacklist()
    if not blacklist:
        return symbols
    bl = set(blacklist)
    out = [s for s in symbols if str(s).replace("US.", "") not in bl]
    if len(out) != len(symbols):
        blocked = [s for s in symbols if str(s).replace("US.", "") in bl]
        logger.info(
            f"[BLACKLIST] playbook 候选守卫拦截 {len(blocked)} 个被禁标的: {blocked}"
        )
    return out


def _blacklist_final_guard_enabled() -> bool:
    """RFC-218 §19 独立开关: PLAYBOOK_BLACKLIST_FINAL_GUARD (进程 env, systemd 注入 .env.us)。

    与 §16 PLAYBOOK_BLACKLIST_GUARD 相互独立: §16 门控「新候选名单三边界」, 本开关门控
    「写入侧 save_instruction + 最终执行入口」闭环 (覆盖 DA 直写/UE/topup/generate 全写入方
    与历史 PENDING)。默认 OFF; 仅显式 truthy 开启, 未知值 fail-closed 回旧行为。每次实读不缓存。
    刻意不复用 §16 开关: 两批 enforcement 点独立回滚, 避免把已激活的候选边界与新增写入/执行
    闭环耦合 (同 §17 EXT 不复用 filter_global_blacklist 包装的纪律)。
    """
    v = os.environ.get("PLAYBOOK_BLACKLIST_FINAL_GUARD", "0")
    return str(v).strip().lower() in ("1", "true", "yes", "on")


def is_global_blacklisted(symbol: Any) -> bool:
    """RFC-218 §19: 单标的黑名单谓词 — 与 filter_global_blacklist 同源同口径。

    只消费 risk.global_blacklist (明确禁止语义), 剥 US. 前缀比对 (与 §16/§17 同一行谓词,
    parity 契约钉死)。读取失败/空黑名单 → False (fail-open, 守卫故障不瘫痪写入/执行)。
    不触碰 enabled=false 普通暂停 / self_managed / 持仓与退出链。
    """
    try:
        blacklist = get_watchlist_manager()._get_global_blacklist()
        if not blacklist:
            return False
        return str(symbol).replace("US.", "") in set(blacklist)
    except Exception as e:
        logger.warning(f"[BLACKLIST] 单标的谓词读取失败 (fail-open 放行): {e}", exc_info=True)
        return False


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
