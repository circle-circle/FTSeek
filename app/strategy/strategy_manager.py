"""
策略动态管理器
支持运行时通过 API / 钉钉命令 查询和调整策略参数
"""
import json
import logging
from typing import Dict, List, Optional
from .base import BaseStrategy

logger = logging.getLogger("ftseek.strategy.manager")
logger.propagate = False  # 防止日志重复


class StrategyManager:
    """策略生命周期管理: 注册、查询、动态调参、启停"""

    def __init__(self):
        self._strategies: Dict[str, BaseStrategy] = {}

    def register(self, strategy: BaseStrategy):
        self._strategies[strategy.name] = strategy
        logger.info(f"策略已注册: {strategy.name} (enabled={strategy.enabled})")

    def get(self, name: str) -> Optional[BaseStrategy]:
        return self._strategies.get(name)

    def all(self) -> List[BaseStrategy]:
        return list(self._strategies.values())

    def enabled(self) -> List[BaseStrategy]:
        return [s for s in self._strategies.values() if s.enabled]

    def list_status(self) -> List[dict]:
        """返回所有策略状态摘要"""
        result = []
        for s in self._strategies.values():
            info = {"name": s.name, "enabled": s.enabled}
            if hasattr(s, "get_params"):
                info.update(s.get_params())
            result.append(info)
        return result

    def update_strategy(self, name: str, params: dict) -> bool:
        """动态更新策略参数"""
        strategy = self._strategies.get(name)
        if strategy is None:
            logger.warning(f"策略不存在: {name}")
            return False
        if hasattr(strategy, "update_params"):
            strategy.update_params(params)
            return True
        # fallback: 直接设置属性
        for k, v in params.items():
            if k == "enabled":
                strategy.enabled = bool(v)
            elif hasattr(strategy, k):
                setattr(strategy, k, v)
        return True

    def enable(self, name: str) -> bool:
        return self.update_strategy(name, {"enabled": True})

    def disable(self, name: str) -> bool:
        return self.update_strategy(name, {"enabled": False})

    def parse_dingtalk_command(self, text: str) -> Optional[str]:
        """
        解析钉钉群命令，返回响应文本

        支持的命令:
          /策略列表 | /strategies
          /策略详情 <name> | /strategy <name>
          /启用 <name> | /enable <name>
          /禁用 <name> | /disable <name>
          /调参 <name> key=value key=value ...
          /set <name> key=value key=value ...
        """
        text = text.strip()
        if not text.startswith("/"):
            return None

        parts = text.split()
        cmd = parts[0].lower()

        # 策略列表
        if cmd in ("/策略列表", "/strategies", "/sl"):
            lines = ["**当前策略状态**\n"]
            for s in self.list_status():
                status = "✅" if s["enabled"] else "❌"
                lines.append(f"- {status} **{s['name']}**")
            return "\n".join(lines)

        # 策略详情
        if cmd in ("/策略详情", "/strategy", "/sd") and len(parts) >= 2:
            name = parts[1]
            s = self.get(name)
            if not s:
                return f"策略 `{name}` 不存在。可用: {', '.join(self._strategies.keys())}"
            if hasattr(s, "get_params"):
                params = s.get_params()
                lines = [f"**策略: {name}**\n"]
                for k, v in params.items():
                    lines.append(f"- `{k}`: {v}")
                return "\n".join(lines)
            return f"**{name}**: enabled={s.enabled}"

        # 启用
        if cmd in ("/启用", "/enable") and len(parts) >= 2:
            name = parts[1]
            if self.enable(name):
                return f"✅ 策略 `{name}` 已启用"
            return f"❌ 策略 `{name}` 不存在"

        # 禁用
        if cmd in ("/禁用", "/disable") and len(parts) >= 2:
            name = parts[1]
            if self.disable(name):
                return f"🚫 策略 `{name}` 已禁用"
            return f"❌ 策略 `{name}` 不存在"

        # 动态调参
        if cmd in ("/调参", "/set", "/param") and len(parts) >= 3:
            name = parts[1]
            params = {}
            for kv in parts[2:]:
                if "=" in kv:
                    k, v = kv.split("=", 1)
                    # 尝试类型转换
                    try:
                        v = json.loads(v)
                    except (json.JSONDecodeError, ValueError) as e:
                        logger.debug(f"解析参数值 {v} 失败：{e}")
                        pass
                    params[k] = v
            if params and self.update_strategy(name, params):
                s = self.get(name)
                result = s.get_params() if hasattr(s, "get_params") else {"updated": True}
                return f"✅ 策略 `{name}` 参数已更新:\n```\n{json.dumps(result, indent=2, ensure_ascii=False)}\n```"
            return f"❌ 更新失败: 策略不存在或参数无效"

        return None


# 全局单例
strategy_manager = StrategyManager()
