#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 配置一致性自动巡检 (Config Audit Sentinel)
- 每日定时运行，检测 config.yaml 与代码默认值的一致性
- 不一致时推送钉钉告警
- 记录审计日志到 data/config_audit_history.json

作者: FTSeek Team
日期: 2026-04-22
"""
import sys
import os
import json
import logging
from datetime import datetime
from pathlib import Path

# 确保路径正确
BASE_DIR = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BASE_DIR))

from config.config_validator import ConfigValidator, ValidationResult

logger = logging.getLogger("ftseek.config_audit")

# 审计历史文件
AUDIT_HISTORY = BASE_DIR / "data" / "config_audit_history.json"

# 关键参数列表 (需要 100% 一致的核心参数)
CRITICAL_PARAMS = [
    "最小置信度",
    "决策缓冲队列",
    "决策缓冲冷却",
    "止损百分比",
    "止盈百分比",
    "最大回撤",
    "最大日亏损",
]


def run_audit() -> dict:
    """
    执行配置审计
    
    Returns:
        审计结果字典
    """
    logger.info("=" * 60)
    logger.info("配置一致性巡检开始")
    logger.info("=" * 60)
    
    # 1. 运行 validator
    from config.config_validator import validate_configs
    result = validate_configs()
    
    audit_result = {
        "timestamp": datetime.now().isoformat(),
        "is_valid": result.is_valid,
        "errors": len(result.errors),
        "warnings": len(result.warnings),
        "infos": len(result.infos),
        "error_details": [],
        "warning_details": [],
        "critical_param_status": {},
    }
    
    # 2. 记录错误和警告
    for err in result.errors:
        audit_result["error_details"].append({
            "category": err.category,
            "message": err.message,
            "field": err.field,
            "suggestion": err.suggestion,
        })
        logger.error(f"  ❌ [{err.category}] {err.message}")
    
    for warn in result.warnings:
        audit_result["warning_details"].append({
            "category": warn.category,
            "message": warn.message,
            "field": warn.field,
        })
        logger.warning(f"  ⚠️ [{warn.category}] {warn.message}")
    
    # 3. 检查关键参数状态
    validator = ConfigValidator()
    validator.load_all_configs()
    config = validator._configs.get("config.yaml", {})
    
    # 读取关键参数值
    risk_limits = config.get("risk_limits", {})
    decision_buffer = config.get("strategy", {}).get("decision_buffer", {})
    
    audit_result["critical_param_status"] = {
        "min_confidence": risk_limits.get("min_confidence", "N/A"),
        "max_pending": decision_buffer.get("max_pending", "N/A"),
        "stop_loss_default": config.get("stop_loss", {}).get("default_percent", "N/A"),
        "take_profit_default": config.get("take_profit", {}).get("default_percent", "N/A"),
        "max_drawdown": risk_limits.get("max_drawdown", "N/A"),
    }
    
    # 4. 保存到审计历史
    save_audit_history(audit_result)
    
    # 5. 输出摘要
    logger.info("-" * 60)
    logger.info(f"审计完成: {'✅ 通过' if result.is_valid else '❌ 失败'}")
    logger.info(f"  错误: {len(result.errors)} | 警告: {len(result.warnings)}")
    logger.info(f"  关键参数:")
    for param, value in audit_result["critical_param_status"].items():
        logger.info(f"    {param}: {value}")
    logger.info("=" * 60)
    
    return audit_result


def save_audit_history(result: dict):
    """保存审计结果到历史文件"""
    history = []
    
    # 加载现有历史
    if AUDIT_HISTORY.exists():
        try:
            with open(AUDIT_HISTORY, "r") as f:
                history = json.load(f)
        except:
            history = []
    
    # 保留最近 30 条记录
    history.append(result)
    history = history[-30:]
    
    AUDIT_HISTORY.parent.mkdir(parents=True, exist_ok=True)
    with open(AUDIT_HISTORY, "w") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def format_dingtalk_message(result: dict) -> str:
    """格式化为钉钉 Markdown 消息"""
    is_valid = result["is_valid"]
    icon = "✅" if is_valid else "🚨"
    
    lines = [
        f"## {icon} FTSeek 配置一致性审计",
        f"**时间**: {result['timestamp']}",
        f"**状态**: {'✅ 通过' if is_valid else '❌ 发现不一致'}",
        f"**错误**: {result['errors']} | **警告**: {result['warnings']}",
    ]
    
    if not is_valid:
        lines.append("")
        lines.append("### ❌ 错误详情")
        for err in result["error_details"][:5]:
            lines.append(f"- **{err['category']}**: {err['message']}")
            if err.get("suggestion"):
                lines.append(f"  - 建议: {err['suggestion']}")
    
    lines.append("")
    lines.append("### 📊 关键参数")
    for param, value in result["critical_param_status"].items():
        lines.append(f"- {param}: `{value}`")
    
    # 检查历史趋势
    if AUDIT_HISTORY.exists():
        try:
            with open(AUDIT_HISTORY) as f:
                history = json.load(f)
            if len(history) >= 2:
                last_valid = sum(1 for r in history[-5:] if r["is_valid"])
                lines.append("")
                lines.append(f"### 📈 趋势 (最近5次)")
                lines.append(f"- 通过: {last_valid}/5 | 失败: {5-last_valid}/5")
        except:
            pass
    
    lines.append("")
    lines.append(f"_生成时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}_")
    
    return "\n".join(lines)


def send_dingtalk_alert(message: str):
    """发送钉钉告警"""
    try:
        from app.notifier.dingtalk import get_dingtalk_notifier
        notifier = get_dingtalk_notifier()
        if notifier:
            notifier.send_text(message)
            logger.info("钉钉告警已发送")
        else:
            logger.warning("钉钉通知器未初始化")
    except Exception as e:
        logger.error(f"钉钉告警发送失败: {e}")


if __name__ == "__main__":
    # 配置日志
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(BASE_DIR / "logs" / "config_audit.log"),
        ]
    )
    
    # 运行审计
    result = run_audit()
    
    # 如果有错误，发送钉钉告警
    if not result["is_valid"]:
        message = format_dingtalk_message(result)
        send_dingtalk_alert(message)
    else:
        logger.info("配置一致性正常，无需告警")
    
    # 退出码: 0=通过, 1=有错误
    sys.exit(0 if result["is_valid"] else 1)
