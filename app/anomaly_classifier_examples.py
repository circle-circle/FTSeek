"""
异常分类器集成示例

展示如何在现有模块中接入 AnomalyClassifier。
不修改现有代码，只做增强。

用法:
    在各模块的异常处理中，添加一行 report_anomaly() 调用即可。
"""

# ============================================================
#  示例 1: OpenD 连接异常 (data_subscriber.py)
# ============================================================

def example_opend_connection():
    """
    在 data_subscriber.py 中集成
    
    原有代码:
        try:
            self._connect_to_opend()
        except Exception as e:
            logger.error(f"OpenD 连接失败: {e}")
    
    增强后:
    """
    from app.anomaly_classifier import report_anomaly
    
    try:
        # self._connect_to_opend()  # 原有代码
        pass
    except Exception as e:
        logger.error(f"OpenD 连接失败: {e}")
        
        # 新增: 报告异常
        report_anomaly(
            source="opend",
            anomaly_type="connection_lost",
            message=str(e),
            impact_scope="all_trading",
        )


# ============================================================
#  示例 2: 碎股拦截 (lot_size_manager.py)
# ============================================================

def example_lot_size_error():
    """
    在 lot_size_manager.py 中集成
    
    原有代码:
        if quantity % lot_size != 0:
            logger.warning(f"碎股拦截: {quantity} 不是 {lot_size} 的倍数")
            return False
    
    增强后:
    """
    from app.anomaly_classifier import report_anomaly
    
    quantity = 150
    lot_size = 200
    
    if quantity % lot_size != 0:
        logger.warning(f"碎股拦截: {quantity} 不是 {lot_size} 的倍数")
        
        # 新增: 报告异常 (P1)
        report_anomaly(
            source="lot_size",
            anomaly_type="lot_size_mismatch",
            message=f"碎股拦截: {quantity} 不是 {lot_size} 的倍数",
            impact_scope="single_symbol",
            affected_symbols=["HK.00700"],  # 实际从上下文获取
        )
        return False


# ============================================================
#  示例 3: 订阅超限 (data_subscriber.py)
# ============================================================

def example_subscription_limit():
    """
    在 data_subscriber.py 中集成
    
    原有代码:
        if self._subscription_count >= 100:
            logger.warning("订阅数达到上限，无法订阅新标的")
            return
    
    增强后:
    """
    from app.anomaly_classifier import report_anomaly
    
    subscription_count = 100
    max_subscriptions = 100
    
    if subscription_count >= max_subscriptions:
        logger.warning("订阅数达到上限，无法订阅新标的")
        
        # 新增: 报告异常 (P1)
        report_anomaly(
            source="subscription",
            anomaly_type="subscription_limit_reached",
            message=f"订阅数 {subscription_count}/{max_subscriptions} 达到上限",
            impact_scope="data_only",
        )


# ============================================================
#  示例 4: 风控突破 (risk_manager.py)
# ============================================================

def example_risk_breach():
    """
    在 risk_manager.py 中集成
    
    原有代码:
        if drawdown > self.max_drawdown:
            logger.critical(f"回撤突破: {drawdown} > {self.max_drawdown}")
            self._halt_trading()
    
    增强后:
    """
    from app.anomaly_classifier import report_anomaly
    
    drawdown = 0.15
    max_drawdown = 0.10
    
    if drawdown > max_drawdown:
        logger.critical(f"回撤突破: {drawdown} > {max_drawdown}")
        self._halt_trading()
        
        # 新增: 报告异常 (P0)
        report_anomaly(
            source="risk",
            anomaly_type="risk_limit_breached",
            message=f"回撤突破: {drawdown:.2%} > {max_drawdown:.2%}",
            impact_scope="all_trading",
            metadata={
                "drawdown": drawdown,
                "max_drawdown": max_drawdown,
            },
        )


# ============================================================
#  示例 5: 配置回退 (config_center.py)
# ============================================================

def example_config_fallback():
    """
    在 config_center.py 中集成
    
    原有代码:
        if not config_value:
            logger.warning(f"配置 {key} 未设置，使用默认值")
            return default_value
    
    增强后:
    """
    from app.anomaly_classifier import report_anomaly
    
    key = "some_config"
    config_value = None
    default_value = "default"
    
    if not config_value:
        logger.warning(f"配置 {key} 未设置，使用默认值")
        
        # 新增: 报告异常 (P2)
        report_anomaly(
            source="config",
            anomaly_type="config_fallback",
            message=f"配置 {key} 未设置，使用默认值",
            impact_scope="system",
        )
        return default_value


# ============================================================
#  使用模式总结
# ============================================================

"""
集成模式:
1. 在现有 except 或 if 分支中，添加 report_anomaly() 调用
2. 不需要修改原有逻辑，只做增强
3. 异常分类器自动分类并记录

分类规则映射:
- opend_connection_lost → P0 (阻断交易)
- risk_limit_breached → P0 (阻断交易)
- lot_size_mismatch → P1 (影响质量)
- subscription_limit_reached → P1 (影响质量)
- config_fallback → P2 (信息记录)

新增异常类型:
在 CLASSIFICATION_RULES 中添加即可，无需修改调用方。

Agent 处理:
- P0: 自动修复 或 升级给人工
- P1: 自动修复 或 确认影响 或 忽略
- P2: 仅记录，不触发 Agent 处理

通知渠道:
- P0: 钉钉 + 飞书 (实时)
- P1: 飞书 (盘前/午间/盘后汇总)
- P2: 仅记录到数据库
"""


if __name__ == "__main__":
    print("异常分类器集成示例")
    print("运行: python3 app/anomaly_classifier_examples.py")
    print("\n示例函数:")
    print("- example_opend_connection()")
    print("- example_lot_size_error()")
    print("- example_subscription_limit()")
    print("- example_risk_breach()")
    print("- example_config_fallback()")
