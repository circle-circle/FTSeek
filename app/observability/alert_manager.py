"""告警管理器 (统一接口)"""
from typing import Dict, Any, Optional
from .dingtalk_alert import DingTalkAlerter

# P0 2026-04-20: 飞书模块可选导入，未安装时 HAS_FEISHU = False
try:
    from .feishu_alert import FeishuAlerter
    HAS_FEISHU = True
except ImportError:
    HAS_FEISHU = False

# 2026-05-09: 告警防抖器 (观察期 + 升级推送)
try:
    from .alert_debouncer import AlertDebouncer
    HAS_DEBOUNCER = True
except ImportError:
    HAS_DEBOUNCER = False


class AlertManager:
    """告警管理器 (统一接口)"""
    
    def __init__(
        self,
        provider: str = "dingtalk",
        dingtalk_webhook: Optional[str] = None,
        dingtalk_secret: Optional[str] = None,
        feishu_app_id: Optional[str] = None,
        feishu_app_secret: Optional[str] = None,
        debouncer: Optional["AlertDebouncer"] = None,
    ):
        self.provider = provider.lower()
        
        if self.provider == "dingtalk":
            if not dingtalk_webhook:
                raise ValueError("钉钉 webhook URL 必须提供")
            self.alerter = DingTalkAlerter(dingtalk_webhook, dingtalk_secret or "")
        elif self.provider == "feishu":
            if HAS_FEISHU and feishu_app_id and feishu_app_secret:  # noqa: F821 -- HAS_FEISHU defined by try/except below
                self.alerter = FeishuAlerter(feishu_app_id, feishu_app_secret)
            else:
                print("⚠️  飞书模块未安装或配置不完整，降级为钉钉")
                self.alerter = DingTalkAlerter(dingtalk_webhook or "", dingtalk_secret or "")
        else:
            raise ValueError(f"不支持的告警提供商：{provider}")

        # 防抖器 (可选, 默认不开启, 不影响现有行为)
        self.debouncer = debouncer

        # 飞书备用通道 (用于双推)
        self._feishu_alerter = None
        if HAS_FEISHU and feishu_app_id and feishu_app_secret:
            self._feishu_alerter = FeishuAlerter(feishu_app_id, feishu_app_secret)
    
    def format_alert(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> Dict[str, Any]:
        """格式化告警"""
        return self.alerter.format_alert(level, title, content, **kwargs)
    
    def send_alert(
        self,
        level: str,
        title: str,
        content: str,
        alert_type: Optional[str] = None,
        **kwargs
    ) -> bool:
        """发送告警 (支持防抖器)

        如果传入 alert_type 且 debouncer 已启用, 则走防抖逻辑:
        - 首次出现: 记录, 不推送
        - 观察期内重复: 计数
        - 达到阈值: 升级为 CRITICAL 并推送
        否则: 直接推送 (原有行为不变)
        """
        # 防抖器逻辑 (可选)
        if self.debouncer and alert_type:
            result = self.debouncer.record(
                alert_type=alert_type,
                title=title,
                content=content,
                level=level,
                alerter=self,
                **kwargs,
            )
            if result["action"] == "observe":
                return True  # 静默观察, 不算失败
            if result["action"] == "count":
                return True  # 计数中, 不算失败
            # escalate 或 silence 已由 debouncer 内部处理
            return result["action"] == "escalate"

        # 无防抖器 → 原有行为
        return self.alerter.send_alert(level, title, content, **kwargs)

    def send_alert_dual(
        self,
        level: str,
        title: str,
        content: str,
        **kwargs
    ) -> dict:
        """同时推送到钉钉 + 飞书 (用于盘后汇总等关键报告)

        Returns:
            dict: {"dingtalk": bool, "feishu": bool}
        """
        result = {"dingtalk": False, "feishu": False}

        # 主通道 (钉钉)
        try:
            result["dingtalk"] = self.alerter.send_alert(level, title, content, **kwargs)
        except Exception as e:
            print(f"[双推] 钉钉失败: {e}")

        # 备用通道 (飞书)
        if self._feishu_alerter:
            try:
                result["feishu"] = self._feishu_alerter.send_alert(level, title, content, **kwargs)
            except Exception as e:
                print(f"[双推] 飞书失败: {e}")

        return result
    
    def send_info(self, title: str, content: str, **kwargs) -> bool:
        """发送 INFO 级别告警"""
        return self.send_alert("INFO", title, content, **kwargs)
    
    def send_warn(self, title: str, content: str, **kwargs) -> bool:
        """发送 WARN 级别告警"""
        return self.send_alert("WARN", title, content, **kwargs)
    
    def send_critical(self, title: str, content: str, **kwargs) -> bool:
        """发送 CRITICAL 级别告警"""
        return self.send_alert("CRITICAL", title, content, **kwargs)
