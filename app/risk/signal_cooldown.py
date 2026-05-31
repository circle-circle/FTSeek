"""
信号冷却机制
防止同一标的短时间内重复下单
"""
import logging
from datetime import datetime, timedelta
from app.utils.timezone import now_et
from typing import Dict, Optional
from pathlib import Path
import json
import threading

logger = logging.getLogger(__name__)


class SignalCooldown:
    """信号冷却管理器"""
    
    def __init__(self, cooldown_seconds: int = 300, cache_file: Optional[str] = None):
        """
        Args:
            cooldown_seconds: 冷却时间（秒），默认 5 分钟
            cache_file: 持久化文件路径
        """
        self.cooldown_seconds = cooldown_seconds
        self.cache_file = cache_file
        self._cache: Dict[str, datetime] = {}
        self._lock = threading.Lock()
        
        if cache_file:
            self._load_cache()
    
    def _load_cache(self):
        """加载缓存"""
        try:
            if Path(self.cache_file).exists():
                with open(self.cache_file, 'r') as f:
                    data = json.load(f)
                    for symbol, ts in data.items():
                        self._cache[symbol] = datetime.fromisoformat(ts)
        except Exception as e:
            logger.warning(f"加载冷却缓存失败: {e}")
    
    def _save_cache(self):
        """保存缓存"""
        if not self.cache_file:
            return
        try:
            data = {symbol: ts.isoformat() for symbol, ts in self._cache.items()}
            with open(self.cache_file, 'w') as f:
                json.dump(data, f)
        except Exception as e:
            logger.warning(f"保存冷却缓存失败: {e}")
    
    def check(self, symbol: str) -> bool:
        """
        检查是否在冷却期
        
        Args:
            symbol: 标的代码
        
        Returns:
            True = 可以下单，False = 在冷却期
        """
        with self._lock:
            now = now_et()
            
            if symbol in self._cache:
                last_signal = self._cache[symbol]
                elapsed = (now - last_signal).total_seconds()
                
                if elapsed < self.cooldown_seconds:
                    remaining = self.cooldown_seconds - elapsed
                    logger.info(
                        f"[信号冷却] {symbol} 在冷却期，剩余 {remaining:.0f}s"
                    )
                    return False
            
            return True
    
    def record(self, symbol: str):
        """
        记录信号时间
        
        Args:
            symbol: 标的代码
        """
        with self._lock:
            self._cache[symbol] = now_et()
            self._save_cache()
            logger.debug(f"[信号冷却] 记录 {symbol} 信号时间")
    
    def can_trade(self, symbol: str) -> tuple:
        """
        检查是否可以交易
        
        Returns:
            (can_trade: bool, remaining_seconds: float)
        """
        with self._lock:
            now = now_et()
            
            if symbol not in self._cache:
                return True, 0.0
            
            last_signal = self._cache[symbol]
            elapsed = (now - last_signal).total_seconds()
            
            if elapsed >= self.cooldown_seconds:
                return True, 0.0
            
            return False, self.cooldown_seconds - elapsed
    
    def clear(self, symbol: Optional[str] = None):
        """清除冷却记录"""
        with self._lock:
            if symbol:
                self._cache.pop(symbol, None)
            else:
                self._cache.clear()
            self._save_cache()
    
    def get_status(self) -> Dict[str, float]:
        """获取所有冷却状态"""
        now = now_et()
        status = {}
        
        with self._lock:
            for symbol, last_signal in self._cache.items():
                elapsed = (now - last_signal).total_seconds()
                remaining = max(0, self.cooldown_seconds - elapsed)
                if remaining > 0:
                    status[symbol] = remaining
        
        return status


# 全局实例
_cooldown_instance: Optional[SignalCooldown] = None


def get_cooldown(cooldown_seconds: int = 300) -> SignalCooldown:
    """获取全局冷却实例"""
    global _cooldown_instance
    if _cooldown_instance is None:
        cache_file = Path(__file__).parent.parent.parent / "data" / "signal_cooldown.json"
        _cooldown_instance = SignalCooldown(
            cooldown_seconds=cooldown_seconds,
            cache_file=str(cache_file)
        )
    return _cooldown_instance


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser()
    parser.add_argument("--status", action="store_true", help="查看冷却状态")
    parser.add_argument("--clear", type=str, help="清除指定标的冷却")
    parser.add_argument("--check", type=str, help="检查指定标的")
    args = parser.parse_args()
    
    cooldown = get_cooldown()
    
    if args.status:
        status = cooldown.get_status()
        print("当前冷却状态:")
        for symbol, remaining in status.items():
            print(f"  {symbol}: {remaining:.0f}s")
        if not status:
            print("  无冷却中的标的")
    
    elif args.clear:
        cooldown.clear(args.clear)
        print(f"已清除 {args.clear} 的冷却")
    
    elif args.check:
        can_trade, remaining = cooldown.can_trade(args.check)
        print(f"{args.check}: {'可交易' if can_trade else f'冷却中 (剩余 {remaining:.0f}s)'}")
    
    else:
        parser.print_help()
