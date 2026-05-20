#!/usr/bin/env python3
"""
配置漂移检测器 (Config Drift Detector)

检测 config_defaults.py 与 unified_config.yaml 之间的配置漂移。
集成到盘后审计链路，每次审计时自动检查配置一致性。

用法:
    python scripts/config_drift_detector.py
    python scripts/config_drift_detector.py --json  # JSON 输出
"""

import json
import sys
import os
from datetime import datetime
from pathlib import Path

# 添加项目根目录到 path
PROJECT_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import yaml

# ============================================================
# 配置映射定义 (SSOT)
# ============================================================
# 格式: (CFG_ATTR, YAML_PATH, DESCRIPTION)
CONFIG_MAPPING = [
    # 风控 - 置信度
    ("MIN_CONFIDENCE", "risk_limits.min_confidence", "信号最低置信度门槛"),
    ("DECISION_BUFFER_MIN_CONFIDENCE", "strategy.decision_buffer.min_confidence", "决策缓冲最低置信度"),
    ("CONFIDENCE_ADJUSTER_MIN_CONFIDENCE", "strategy.confidence_adjuster.min_confidence", "置信度调整器最低门槛"),
    
    # 决策缓冲层
    ("DECISION_BUFFER_SIGNAL_COOLDOWN", "strategy.decision_buffer.signal_cooldown", "信号冷却秒数"),
    ("DECISION_BUFFER_MAX_PENDING", "strategy.decision_buffer.max_pending", "最大待处理信号数"),
    ("DECISION_BUFFER_MAX_POSITIONS", "strategy.decision_buffer.max_positions", "最大持仓数 (null=动态)"),
    
    # 置信度调整器
    ("CONFIDENCE_ADJUSTER_MAX_SINGLE_PENALTY", "strategy.confidence_adjuster.max_single_penalty", "单因子最大惩罚"),
    ("CONFIDENCE_ADJUSTER_MAX_TOTAL_PENALTY", "strategy.confidence_adjuster.max_total_penalty", "总惩罚上限"),
    ("CONFIDENCE_ADJUSTER_MAX_SINGLE_BOOST", "strategy.confidence_adjuster.max_single_boost", "单因子最大增强"),
    ("CONFIDENCE_ADJUSTER_MAX_TOTAL_BOOST", "strategy.confidence_adjuster.max_total_boost", "总增强上限"),
    
    # 仓位 & 亏损限制
    ("MAX_SINGLE_POSITION", "risk_limits.max_single_position", "单仓位最大占比"),
    ("MAX_TOTAL_POSITION", "risk_limits.max_total_position", "总仓位最大占比"),
    ("MAX_DAILY_TRADES", "risk_limits.max_daily_trades", "每日最大交易次数"),
    ("MAX_DAILY_LOSS", "risk_limits.max_daily_loss", "日亏损上限"),
    ("MAX_WEEKLY_LOSS", "risk_limits.max_weekly_loss", "周亏损上限"),
    ("MAX_MONTHLY_LOSS", "risk_limits.max_monthly_loss", "月亏损上限"),
    ("MAX_DRAWDOWN", "risk_limits.max_drawdown", "最大回撤"),
    ("CONSECUTIVE_LOSS_LIMIT", "risk_limits.consecutive_loss_limit", "连续亏损次数"),
    ("STRATEGY_MIN_WINRATE", "risk_limits.strategy_min_winrate", "策略最低胜率"),
    
    # 止损 / 止盈
    ("DEFAULT_STOP_LOSS_PCT", "stop_loss.default_percent", "默认止损百分比"),
    ("DEFAULT_TAKE_PROFIT_PCT", "take_profit.default_percent", "默认止盈百分比"),
    ("TRAILING_STOP_PCT", "stop_loss.trailing_percent", "移动止损百分比"),
    ("TRAILING_TP_PCT", "take_profit.trailing_percent", "移动止盈百分比"),
    ("MAX_HOLDING_HOURS", "stop_loss.max_holding_hours", "最大持仓小时"),
    ("ATR_MULTIPLIER", "stop_loss.atr_multiplier", "ATR 止损乘数"),
    ("BREAKEVEN_TRIGGER", "take_profit.breakeven.trigger_profit", "保本触发利润"),
    ("BREAKEVEN_LEVEL", "take_profit.breakeven.stop_level", "保本止损位"),
    
    # 共识引擎
    ("CONSENSUS_WINDOW", "strategy.consensus.window_seconds", "共识窗口秒数"),
    ("CONSENSUS_THRESHOLD", "strategy.consensus.threshold", "共识阈值"),
    ("CONSENSUS_ADX_TREND", "strategy.consensus.adx_trend", "ADX 趋势判定阈值"),
    ("CONSENSUS_ADX_RANGE", "strategy.consensus.adx_range", "ADX 震荡判定阈值"),
    
    # 信号冷却
    ("SIGNAL_COOLDOWN", "strategy.cooldown.signal_cooldown", "信号冷却秒数"),
    
    # 趋势过滤
    ("TREND_FILTER_MIN_ADX", "strategy.trend_filter.adx_range_gate", "ADX 趋势过滤阈值"),
    ("TREND_FILTER_USE_MULTI_TIMEFRAME", "strategy.trend_filter.use_multi_timeframe", "启用多时间框架"),
    ("TREND_FILTER_PREVENT_BUY_IN_DOWNTREND", "strategy.trend_filter.prevent_buy_in_downtrend", "下跌趋势拦截买入"),
    ("TREND_FILTER_PREVENT_SHORT_IN_UPTREND", "strategy.trend_filter.prevent_short_in_uptrend", "上涨趋势拦截做空"),
    
    # 入场价格检查
    ("ENTRY_PRICE_HIGH_BUY_THRESHOLD", "entry_price_checker.high_buy_warn", "追高警告阈值"),
    ("ENTRY_PRICE_CHASE_WARNING_THRESHOLD", "entry_price_checker.chase_threshold", "追高警告阈值"),
    ("ENTRY_PRICE_CHASE_BLOCK_THRESHOLD", "entry_price_checker.panic_threshold", "恐慌阈值"),
    
    # 熔断器
    ("CIRCUIT_BREAKER_REJECT_LIMIT", "risk_limits.circuit_breaker.max_reject_count", "最大拒绝次数"),
    ("CIRCUIT_BREAKER_PAUSE_DURATION", "risk_limits.circuit_breaker.pause_duration_sec", "熔断暂停秒数"),
    ("CIRCUIT_BREAKER_DRAWDOWN_LIMIT", "risk_limits.circuit_breaker.daily_loss_halt", "日内回撤熔断"),
    
    # 硬编码熔断守卫
    ("HARD_STOP_DRAWDOWN_LIMIT", "circuit_breaker.daily_loss_pct", "日内回撤硬止损"),
    ("HARD_STOP_CONSECUTIVE_LOSSES", "loss_pause.consecutive_losses", "连续亏损次数"),
    
    # LLM 交易
    ("LLM_TRADING_MIN_CONFIDENCE", "smart_push.auto_execute_min_confidence", "LLM 自动执行最低置信度"),
    ("LLM_TRADING_MANUAL_CONFIRM_MIN_CONFIDENCE", "smart_push.manual_confirm_min_confidence", "人工确认最低置信度"),
]

# 已知漂移项 (语义不同，允许存在差异)
KNOWN_DRIFTS = {
    "CIRCUIT_BREAKER_DRAWDOWN_LIMIT": "CFG=总回撤 (15%), YAML=日内回撤 (3%)，语义不同",
}


def get_nested_value(data: dict, path: str, default=None):
    """获取嵌套字典的值"""
    keys = path.split(".")
    value = data
    for key in keys:
        if isinstance(value, dict):
            value = value.get(key, default)
        else:
            return default
    return value


def load_config_defaults():
    """加载 config_defaults.py 的值"""
    try:
        from app.core.config_defaults import CFG
        result = {}
        for cfg_attr, _, _ in CONFIG_MAPPING:
            result[cfg_attr] = getattr(CFG, cfg_attr, None)
        return result
    except ImportError as e:
        print(f"⚠️  无法加载 config_defaults.py: {e}", file=sys.stderr)
        return {}


def load_unified_config():
    """加载 unified_config.yaml 的值"""
    config_path = PROJECT_ROOT / "config" / "unified_config.yaml"
    if not config_path.exists():
        print(f"⚠️  配置文件不存在：{config_path}", file=sys.stderr)
        return {}
    
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f)
        
        result = {}
        for _, yaml_path, _ in CONFIG_MAPPING:
            result[yaml_path] = get_nested_value(config, yaml_path)
        return result
    except Exception as e:
        print(f"⚠️  无法加载 unified_config.yaml: {e}", file=sys.stderr)
        return {}


def detect_drift():
    """检测配置漂移"""
    cfg_values = load_config_defaults()
    yaml_values = load_unified_config()
    
    drifts = []
    matches = []
    missing = []
    
    for cfg_attr, yaml_path, description in CONFIG_MAPPING:
        cfg_val = cfg_values.get(cfg_attr)
        yaml_val = yaml_values.get(yaml_path)
        
        # 处理 None 值
        if cfg_val is None and yaml_val is None:
            missing.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "status": "MISSING_BOTH",
            })
            continue
        
        if cfg_val is None:
            missing.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "status": "MISSING_CFG",
                "yaml_value": yaml_val,
            })
            continue
        
        if yaml_val is None:
            missing.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "status": "MISSING_YAML",
                "cfg_value": cfg_val,
            })
            continue
        
        # 比较值 (处理类型差异)
        cfg_normalized = cfg_val
        yaml_normalized = yaml_val
        
        # 数字比较 (int vs float)
        if isinstance(cfg_val, (int, float)) and isinstance(yaml_val, (int, float)):
            if abs(cfg_val - yaml_val) < 1e-9:
                matches.append({
                    "cfg_attr": cfg_attr,
                    "yaml_path": yaml_path,
                    "description": description,
                    "value": cfg_val,
                })
                continue
        
        # 字符串/布尔值比较
        if str(cfg_val) == str(yaml_val):
            matches.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "value": cfg_val,
            })
            continue
        
        # 检查是否为已知漂移
        if cfg_attr in KNOWN_DRIFTS:
            drifts.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "cfg_value": cfg_val,
                "yaml_value": yaml_val,
                "status": "KNOWN_DRIFT",
                "reason": KNOWN_DRIFTS[cfg_attr],
            })
        else:
            drifts.append({
                "cfg_attr": cfg_attr,
                "yaml_path": yaml_path,
                "description": description,
                "cfg_value": cfg_val,
                "yaml_value": yaml_val,
                "status": "DRIFT",
            })
    
    return {
        "timestamp": datetime.now().isoformat(),
        "summary": {
            "total": len(CONFIG_MAPPING),
            "matches": len(matches),
            "drifts": len(drifts),
            "missing": len(missing),
        },
        "drifts": drifts,
        "missing": missing,
        "matches": matches,
    }


def print_report(result: dict, json_output: bool = False):
    """打印报告"""
    if json_output:
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return
    
    summary = result["summary"]
    print("=" * 60)
    print("📊 FTSeek 配置漂移检测报告")
    print("=" * 60)
    print(f"检测时间：{result['timestamp']}")
    print(f"\n📈 统计:")
    print(f"  总配置项：{summary['total']}")
    print(f"  ✅ 一致：{summary['matches']}")
    print(f"  ⚠️  漂移：{summary['drifts']}")
    print(f"  ❓ 缺失：{summary['missing']}")
    
    if result["drifts"]:
        print(f"\n⚠️  配置漂移项:")
        for drift in result["drifts"]:
            status = "已知差异" if drift["status"] == "KNOWN_DRIFT" else "需修复"
            print(f"  [{status}] {drift['cfg_attr']}")
            print(f"    CFG: {drift['cfg_value']}")
            print(f"    YAML: {drift['yaml_value']}")
            if drift["status"] == "KNOWN_DRIFT":
                print(f"    原因：{drift['reason']}")
    
    if result["missing"]:
        print(f"\n❓ 缺失配置项:")
        for miss in result["missing"]:
            print(f"  [{miss['status']}] {miss['cfg_attr']} -> {miss['yaml_path']}")
    
    if not result["drifts"] and not result["missing"]:
        print("\n✅ 所有配置项一致，无漂移")
    
    print("\n" + "=" * 60)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="配置漂移检测器")
    parser.add_argument("--json", action="store_true", help="JSON 格式输出")
    args = parser.parse_args()
    
    result = detect_drift()
    print_report(result, json_output=args.json)
    
    # 返回退出码
    if result["summary"]["drifts"] > 0:
        return 1  # 有漂移，返回非 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
