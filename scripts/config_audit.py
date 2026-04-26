#!/usr/bin/env python3
"""
配置一致性审计 (只读，零风险)
================================
检查 config.yaml vs 环境变量 vs 代码默认值 的差异
输出差异报告，不修改任何文件

运行方式:
  python3 scripts/config_audit.py [FTSeek|FTSeek-US]
"""
import os
import sys
import json
import yaml
from pathlib import Path
from datetime import datetime

# 项目路径
PROJECTS = {
    "FTSeek": "/opt/ftseek/FTSeek",
    "FTSeek-US": "/opt/ftseek/FTSeek-US",
}

# 需要审计的关键配置项映射 (yaml_key, env_var, code_default, description)
AUDIT_ITEMS = {
    "FTSeek": [
        # 信号/策略配置
        ("strategy.cooldown.signal_cooldown", "SIGNAL_COOLDOWN", 120, "信号冷却时间(秒)"),
        ("strategy.consensus.window_seconds", "CONSENSUS_WINDOW", 60, "共识窗口(秒)"),
        ("strategy.consensus.threshold", "CONSENSUS_THRESHOLD", 1.5, "共识阈值"),
        ("strategy.consensus.adx_trend", "CONSENSUS_ADX_TREND", 25, "ADX趋势阈值"),
        ("strategy.consensus.adx_range", "CONSENSUS_ADX_RANGE", 20, "ADX范围阈值"),
        # 交易配置
        ("trading.max_open_positions", "MAX_OPEN_POSITIONS", 4, "最大持仓数"),
        ("trading.trade_env", "TRADE_ENV", 1, "交易环境(0=实盘,1=模拟)"),
        # 风控配置
        ("risk_limits.min_confidence", "MIN_CONFIDENCE", 0.55, "最小置信度"),
        ("risk_limits.max_single_position", "MAX_SINGLE_POSITION", 0.08, "单标的最大仓位"),
        ("risk_limits.max_total_position", "MAX_TOTAL_POSITION", 0.75, "总仓位上限"),
        ("risk_limits.max_daily_loss", "MAX_DAILY_LOSS", 0.03, "日最大亏损"),
        ("risk_limits.max_drawdown", "MAX_DRAWDOWN", 0.12, "最大回撤"),
        # 止损/止盈
        ("stop_loss.default_percent", "DEFAULT_STOP_LOSS", 0.05, "默认止损比例"),
        ("take_profit.default_percent", "DEFAULT_TAKE_PROFIT", 0.10, "默认止盈比例"),
        # OpenD 配置
        ("opend.host", "OPEND_HOST", "127.0.0.1", "OpenD 主机"),
        ("opend.port", "OPEND_PORT", 11111, "OpenD 端口"),
        # 钉钉配置
        ("notifier.dingtalk_webhook", "DINGTALK_WEBHOOK_URL", "", "钉钉 Webhook URL"),
        ("notifier.dingtalk_secret", "DINGTALK_SECRET", "", "钉钉 Secret"),
        # 回调服务
        ("server.callback_host", "CALLBACK_HOST", "0.0.0.0", "回调主机"),
        ("server.callback_port", "CALLBACK_PORT", 8899, "回调端口"),
    ],
    "FTSeek-US": [
        # 美股策略配置
        ("strategy.cooldown.signal_cooldown", "SIGNAL_COOLDOWN", 120, "信号冷却时间(秒)"),
        ("strategy.consensus_threshold", "US_CONSENSUS_THRESHOLD", 0.6, "共识阈值"),
        # 交易配置
        ("trading.max_open_positions", "US_MAX_OPEN_POSITIONS", 4, "最大持仓数"),
        ("trading.trade_env", "TRADE_ENV", 1, "交易环境(0=实盘,1=模拟)"),
        # 风控配置
        ("risk_limits.min_confidence", "US_MIN_CONFIDENCE", 0.55, "最小置信度"),
        ("risk_limits.max_single_position", "US_MAX_SINGLE_POSITION", 0.10, "单标的最大仓位"),
        ("risk_limits.max_total_position", "US_MAX_TOTAL_POSITION", 0.80, "总仓位上限"),
        ("risk_limits.max_daily_loss", "US_MAX_DAILY_LOSS", 0.03, "日最大亏损"),
        # 止损/止盈
        ("stop_loss.default_percent", "US_DEFAULT_STOP_LOSS", 0.05, "默认止损比例"),
        ("take_profit.default_percent", "US_DEFAULT_TAKE_PROFIT", 0.10, "默认止盈比例"),
        # OpenD 配置
        ("opend.host", "OPEND_HOST", "127.0.0.1", "OpenD 主机"),
        ("opend.port", "OPEND_PORT", 11111, "OpenD 端口"),
        # 通知配置
        ("notifier.dingtalk_webhook", "DINGTALK_WEBHOOK_URL_US", "", "钉钉 Webhook URL"),
        ("notifier.dingtalk_secret", "DINGTALK_SECRET_US", "", "钉钉 Secret"),
    ]
}


def get_nested(d: dict, key_path: str, default=None):
    """从嵌套字典获取值，如 'strategy.signal_cooldown.signal_cooldown'"""
    keys = key_path.split(".")
    current = d
    for key in keys:
        if isinstance(current, dict) and key in current:
            current = current[key]
        else:
            return default
    return current


def parse_env_value(env_name: str, value: str):
    """解析环境变量值为正确类型"""
    if value is None:
        return None
    
    # 尝试转换为数字
    try:
        if "." in value:
            return float(value)
        return int(value)
    except (ValueError, TypeError):
        pass
    
    # 布尔值
    if value.lower() in ("true", "yes", "1"):
        return True
    if value.lower() in ("false", "no", "0"):
        return False
    
    return value


def mask_sensitive(key: str, value: str) -> str:
    """脱敏敏感信息"""
    sensitive_keywords = ["SECRET", "WEBHOOK", "PASSWORD", "PWD", "TOKEN", "KEY"]
    if any(k in key.upper() for k in sensitive_keywords):
        if not value:
            return "(未设置)"
        if value.startswith("http"):
            return value[:30] + "..."
        return value[:5] + "***" + value[-3:] if len(value) > 8 else "***"
    return str(value)


def audit_project(project_name: str, project_path: str) -> dict:
    """审计单个项目的配置一致性"""
    results = {
        "project": project_name,
        "timestamp": datetime.now().isoformat(),
        "issues": [],
        "summary": {"total_checked": 0, "consistent": 0, "drifted": 0, "missing": 0},
    }
    
    base_path = Path(project_path)
    
    # 1. 加载 YAML 配置
    yaml_config = {}
    yaml_paths = ["config/config.yaml", "config/unified_config.yaml", "config.yaml"]
    yaml_file = None
    for yp in yaml_paths:
        p = base_path / yp
        if p.exists():
            yaml_file = p
            try:
                with open(p, "r", encoding="utf-8") as f:
                    yaml_config = yaml.safe_load(f) or {}
                break
            except Exception as e:
                results["issues"].append({
                    "severity": "error",
                    "message": f"YAML 加载失败: {yp} - {e}"
                })
    
    if not yaml_file:
        results["issues"].append({
            "severity": "warning",
            "message": "未找到 YAML 配置文件"
        })
    
    # 2. 加载 .env 文件
    env_file_values = {}
    env_paths = [".env", ".env.us", ".env.ai"]
    for ep in env_paths:
        p = base_path / ep
        if p.exists():
            try:
                with open(p, "r", encoding="utf-8") as f:
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith("#") and "=" in line:
                            k, _, v = line.partition("=")
                            env_file_values[k.strip()] = v.strip().strip('"').strip("'")
            except Exception as e:
                results["issues"].append({
                    "severity": "warning",
                    "message": f".env 加载失败: {ep} - {e}"
                })
    
    # 3. 逐项审计
    audit_items = AUDIT_ITEMS.get(project_name, [])
    for yaml_key, env_var, code_default, description in audit_items:
        results["summary"]["total_checked"] += 1
        
        # 从各来源获取值
        yaml_value = get_nested(yaml_config, yaml_key)
        env_value = os.environ.get(env_var)
        env_file_value = env_file_values.get(env_var)
        code_value = code_default
        
        # 解析类型
        env_parsed = parse_env_value(env_var, env_value) if env_value else None
        env_file_parsed = parse_env_value(env_var, env_file_value) if env_file_value else None
        
        # 确定最终值 (ENV > ENV_FILE > YAML > CODE)
        final_value = env_parsed or env_file_parsed or yaml_value or code_value
        
        # 检查差异
        drifted = False
        drift_details = []
        
        # 检查 YAML vs ENV
        if yaml_value is not None and env_parsed is not None:
            # 类型安全比较
            try:
                yaml_num = float(yaml_value) if not isinstance(yaml_value, bool) else yaml_value
                env_num = float(env_parsed) if not isinstance(env_parsed, bool) else env_parsed
                if yaml_num != env_num:
                    drifted = True
                    drift_details.append(f"YAML({yaml_value}) ≠ ENV({env_parsed})")
            except (ValueError, TypeError):
                if str(yaml_value) != str(env_parsed):
                    drifted = True
                    drift_details.append(f"YAML({yaml_value}) ≠ ENV({env_parsed})")
        
        # 检查 YAML vs ENV_FILE
        if yaml_value is not None and env_file_parsed is not None:
            try:
                yaml_num = float(yaml_value) if not isinstance(yaml_value, bool) else yaml_value
                env_file_num = float(env_file_parsed) if not isinstance(env_file_parsed, bool) else env_file_parsed
                if yaml_num != env_file_num:
                    drifted = True
                    drift_details.append(f"YAML({yaml_value}) ≠ .env({env_file_parsed})")
            except (ValueError, TypeError):
                if str(yaml_value) != str(env_file_parsed):
                    drifted = True
                    drift_details.append(f"YAML({yaml_value}) ≠ .env({env_file_parsed})")
        
        # 检查 YAML vs CODE
        if yaml_value is not None and yaml_value != code_default:
            # 这可能是正常的 (YAML 覆盖了代码默认值)
            # 但如果 ENV 也存在，需要检查三层是否一致
            if env_parsed is not None and yaml_value != env_parsed:
                pass  # 已在上面处理
            elif env_parsed is None:
                # 只有 YAML 和 CODE，YAML 覆盖了 CODE — 这是预期行为
                pass
        
        # 检查缺失
        missing = False
        if yaml_value is None and env_parsed is None and env_file_parsed is None:
            # 所有来源都没有值，使用代码默认值
            missing = True
            results["summary"]["missing"] += 1
            drifted = True
            drift_details.append("所有配置源均未设置，使用代码默认值")
        
        if drifted:
            results["summary"]["drifted"] += 1
            results["issues"].append({
                "severity": "warning" if missing else "info",
                "key": yaml_key,
                "env_var": env_var,
                "description": description,
                "yaml_value": mask_sensitive(yaml_key, yaml_value) if yaml_value is not None else "(未设置)",
                "env_value": mask_sensitive(env_var, env_value) if env_value is not None else "(未设置)",
                "env_file_value": mask_sensitive(env_var, env_file_value) if env_file_value is not None else "(未设置)",
                "code_default": mask_sensitive(yaml_key, code_default),
                "final_value": mask_sensitive(yaml_key, final_value),
                "drift_details": drift_details,
            })
        else:
            results["summary"]["consistent"] += 1
    
    return results


def print_report(results: dict):
    """打印审计报告"""
    print(f"\n{'='*70}")
    print(f"  {results['project']} 配置一致性审计报告")
    print(f"  时间: {results['timestamp']}")
    print(f"{'='*70}")
    
    s = results["summary"]
    print(f"\n📊 审计统计:")
    print(f"  总检查项: {s['total_checked']}")
    print(f"  ✅ 一致: {s['consistent']}")
    print(f"  ⚠️  差异: {s['drifted']}")
    print(f"  ❓ 缺失: {s['missing']}")
    
    if results["issues"]:
        print(f"\n📋 差异详情:")
        for i, issue in enumerate(results["issues"], 1):
            severity_icon = {"error": "🔴", "warning": "🟡", "info": "🔵"}.get(issue.get("severity", "info"), "⚪")
            print(f"\n  {severity_icon} [{i}] {issue.get('description', issue.get('message', 'Unknown'))}")
            
            if "key" in issue:
                print(f"      配置键: {issue['key']}")
                print(f"      环境变量: {issue['env_var']}")
                print(f"      YAML: {issue['yaml_value']}")
                print(f"      ENV: {issue['env_value']}")
                print(f"      .env: {issue['env_file_value']}")
                print(f"      代码默认: {issue['code_default']}")
                print(f"      最终值: {issue['final_value']}")
                for detail in issue.get("drift_details", []):
                    print(f"      → {detail}")
            else:
                print(f"      {issue.get('message', '')}")
    else:
        print(f"\n✅ 所有配置项一致!")
    
    print(f"\n{'='*70}")


def main():
    # 确定要审计的项目
    if len(sys.argv) > 1:
        projects_to_audit = [sys.argv[1]]
    else:
        projects_to_audit = list(PROJECTS.keys())
    
    all_results = []
    
    for project_name in projects_to_audit:
        if project_name not in PROJECTS:
            print(f"❌ 未知项目: {project_name}")
            print(f"可用项目: {', '.join(PROJECTS.keys())}")
            sys.exit(1)
        
        project_path = PROJECTS[project_name]
        if not os.path.exists(project_path):
            print(f"❌ 项目路径不存在: {project_path}")
            sys.exit(1)
        
        result = audit_project(project_name, project_path)
        all_results.append(result)
        print_report(result)
    
    # 保存报告
    report_path = Path("/tmp/config_audit_report.json")
    with open(report_path, "w", encoding="utf-8") as f:
        json.dump(all_results, f, ensure_ascii=False, indent=2, default=str)
    
    print(f"\n💾 完整报告已保存: {report_path}")
    
    # 退出码: 0=无差异, 1=有差异
    has_drift = any(r["summary"]["drifted"] > 0 for r in all_results)
    sys.exit(1 if has_drift else 0)


if __name__ == "__main__":
    main()
