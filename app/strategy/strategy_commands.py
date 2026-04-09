"""
策略命令处理器
桥接钉钉命令到 FTSeek 策略管理 API
"""
import logging
import requests
from typing import Optional

logger = logging.getLogger("openclaw-dingtalk-bridge.strategy_cmd")
logger.propagate = False  # 防止日志重复

FTSEEK_API_BASE = "http://127.0.0.1:8899"
FTSEEK_API_TOKEN = "ftseek_secret_token_change_me"


def handle_strategy_command(text: str) -> Optional[str]:
    """
    解析策略相关命令，调用 FTSeek API，返回响应文本
    如果不是策略命令则返回 None
    """
    text = text.strip()
    if not text.startswith("/"):
        return None

    parts = text.split()
    cmd = parts[0].lower()

    # /策略列表
    if cmd in ("/策略列表", "/strategies", "/sl"):
        return _call_strategy_list()

    # /策略详情 <name>
    if cmd in ("/策略详情", "/strategy", "/sd") and len(parts) >= 2:
        return _call_strategy_detail(parts[1])

    # /启用 <name>
    if cmd in ("/启用", "/enable") and len(parts) >= 2:
        return _call_strategy_update(parts[1], {"enabled": True})

    # /禁用 <name>
    if cmd in ("/禁用", "/disable") and len(parts) >= 2:
        return _call_strategy_update(parts[1], {"enabled": False})

    # /调参 <name> key=val ...
    if cmd in ("/调参", "/set", "/param") and len(parts) >= 3:
        name = parts[1]
        params = {}
        for kv in parts[2:]:
            if "=" in kv:
                k, v = kv.split("=", 1)
                try:
                    # 尝试数值转换
                    if "." in v:
                        v = float(v)
                    elif v.lower() in ("true", "false"):
                        v = v.lower() == "true"
                    else:
                        v = int(v)
                except ValueError:
                    pass
                params[k] = v
        if params:
            return _call_strategy_update(name, params)
        return "参数格式: `/调参 策略名 key=value key=value`"

    # /help 扩展
    if cmd in ("/help", "帮助", "/命令"):
        return (
            "**OpenClaw + FTSeek 命令**\n\n"
            "**AI 对话**\n"
            "- 直接发送消息即可和 OpenClaw 对话\n\n"
            "**策略管理**\n"
            "- `/策略列表` — 查看所有策略启停状态\n"
            "- `/策略详情 <名称>` — 查看策略完整参数\n"
            "- `/启用 <名称>` — 启用策略\n"
            "- `/禁用 <名称>` — 禁用策略\n"
            "- `/调参 <名称> key=value` — 动态调整参数\n\n"
            "**系统**\n"
            "- `/status` — 系统状态\n"
            "- `/clear` — 清除对话历史\n\n"
            "**示例**\n"
            "- `/调参 rsi period=21 overbought=75`\n"
            "- `/启用 confluence`\n"
            "- `/策略详情 macd`\n"
        )

    return None


def _call_strategy_list() -> str:
    """查询策略列表"""
    try:
        resp = requests.get(
            f"{FTSEEK_API_BASE}/api/strategies",
            params={"token": FTSEEK_API_TOKEN},
            timeout=5,
        )
        if resp.status_code != 200:
            return f"查询失败: HTTP {resp.status_code}"

        strategies = resp.json()
        if isinstance(strategies, dict) and "error" in strategies:
            return f"错误: {strategies['error']}"

        lines = ["**当前策略状态**\n"]
        for s in strategies:
            status = "✅" if s.get("enabled") else "❌"
            lines.append(f"- {status} **{s.get('name', '?')}**")
        lines.append(f"\n共 {len(strategies)} 个策略，{sum(1 for s in strategies if s.get('enabled'))} 个已启用")
        return "\n".join(lines)
    except requests.ConnectionError:
        return "FTSeek API 服务未运行"
    except Exception as e:
        return f"查询异常: {e}"


def _call_strategy_detail(name: str) -> str:
    """查询策略详情"""
    try:
        resp = requests.get(
            f"{FTSEEK_API_BASE}/api/strategies",
            params={"token": FTSEEK_API_TOKEN},
            timeout=5,
        )
        if resp.status_code != 200:
            return f"查询失败: HTTP {resp.status_code}"

        strategies = resp.json()
        for s in strategies:
            if s.get("name") == name:
                lines = [f"**策略: {name}**\n"]
                for k, v in s.items():
                    if isinstance(v, dict):
                        lines.append(f"- **{k}**:")
                        for sk, sv in v.items():
                            lines.append(f"  - `{sk}`: {sv}")
                    else:
                        lines.append(f"- `{k}`: {v}")
                return "\n".join(lines)

        available = ", ".join(s.get("name", "?") for s in strategies)
        return f"策略 `{name}` 不存在。可用: {available}"
    except Exception as e:
        return f"查询异常: {e}"


def _call_strategy_update(name: str, params: dict) -> str:
    """更新策略参数"""
    try:
        resp = requests.post(
            f"{FTSEEK_API_BASE}/api/strategy/update",
            json={"token": FTSEEK_API_TOKEN, "strategy": name, "params": params},
            timeout=5,
        )
        if resp.status_code != 200:
            return f"更新失败: HTTP {resp.status_code}"

        result = resp.json()
        if result.get("status") == "ok":
            p = result.get("params", {})
            lines = [f"✅ 策略 `{name}` 已更新\n"]
            for k, v in p.items():
                if isinstance(v, dict):
                    lines.append(f"- **{k}**:")
                    for sk, sv in v.items():
                        lines.append(f"  - `{sk}`: {sv}")
                else:
                    lines.append(f"- `{k}`: {v}")
            return "\n".join(lines)
        return f"更新失败: {result.get('message', '未知错误')}"
    except requests.ConnectionError:
        return "FTSeek API 服务未运行"
    except Exception as e:
        return f"更新异常: {e}"
