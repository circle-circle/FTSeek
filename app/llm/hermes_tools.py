"""
Hermes Agent 工具集 — Agent 可调用的 5 个工具。

每个工具提供 OpenAI function-calling 兼容的 JSON Schema，
Agent 通过 tool-calling 自主决定调用哪个工具来获取数据。

工具通过 ToolDependencies 注入外部依赖 (DB, Futu, Notifier, Config)，
实现纯函数式的工具执行，与 Agent 处理器解耦。
"""

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Any, Callable

logger = logging.getLogger("ftseek.llm.hermes_tools")


@dataclass
class ToolDependencies:
    """工具执行所需的依赖注入容器"""
    db_path: str = "data/ftseek.db"
    memory_dir: str = os.path.expanduser("~/.hermes/ftseek/memory")
    quote_ctx: Optional[Any] = None       # Futu OpenD quote context
    notifier: Optional[Any] = None         # Unified notifier
    config_center: Optional[Any] = None    # ConfigCenter instance


# ═══════════════════════════════════════════════════════════════════
#  工具定义 (OpenAI function-calling 格式)
# ═══════════════════════════════════════════════════════════════════

TOOL_DEFINITIONS: List[Dict] = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "查询 FTSeek 数据库获取交易记录、持仓、信号或新闻。"
                "用于获取今日/历史交易数据、当前持仓状态、信号质量等。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query_type": {
                        "type": "string",
                        "enum": ["trades", "positions", "signals", "news", "lessons"],
                        "description": "查询类型: trades=交易记录, positions=持仓, signals=信号, news=新闻, lessons=教训",
                    },
                    "market": {
                        "type": "string",
                        "enum": ["hk", "us"],
                        "description": "市场: hk=港股, us=美股",
                    },
                    "date": {
                        "type": "string",
                        "description": "日期 YYYY-MM-DD，默认今天",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返回条数上限，默认 30",
                    },
                },
                "required": ["query_type", "market", "date"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_market_data",
            "description": (
                "获取实时市场行情快照。返回价格、涨跌幅、量比、换手率等。"
                "用于了解当前市场状态和标的实时表现。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "symbols": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "标的代码列表，如 ['HK.00700', 'HK.800000']，最多 20 个",
                    },
                },
                "required": ["symbols"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_or_update_memory",
            "description": (
                "读写 Agent 的持久化市场记忆。包含历史教训、标的规律、策略调整记录。"
                "盘前应读取记忆避免重复犯错，盘后应将新教训写入记忆。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "append"],
                        "description": "read=读取记忆, append=追加新教训",
                    },
                    "market": {
                        "type": "string",
                        "enum": ["hk", "us"],
                        "description": "市场: hk=港股, us=美股",
                    },
                    "lesson": {
                        "type": "string",
                        "description": "append 时必填: 新教训文本",
                    },
                    "symbol": {
                        "type": "string",
                        "description": "append 时可选: 关联标的代码",
                    },
                },
                "required": ["action", "market"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_notification",
            "description": (
                "推送通知到钉钉/飞书。"
                "当分析发现重要风险、异常交易或需要人工关注时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "text": {
                        "type": "string",
                        "description": "通知内容 (支持 markdown 格式)",
                    },
                    "channel": {
                        "type": "string",
                        "enum": ["dingtalk", "feishu", "both"],
                        "description": "推送渠道，默认 both",
                    },
                },
                "required": ["text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_config",
            "description": (
                "读取系统配置。获取风控参数、策略阈值、仓位限制等。"
                "用于验证交易计划是否符合系统规则。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "keys": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "配置项列表，如 ['risk_limits', 'strategy', 'watchlist_hk']，空数组返回所有",
                    },
                },
                "required": ["keys"],
            },
        },
    },
]


def get_openai_tools() -> List[Dict]:
    """返回 OpenAI function-calling 格式的工具列表"""
    return TOOL_DEFINITIONS


# ═══════════════════════════════════════════════════════════════════
#  工具执行函数
# ═══════════════════════════════════════════════════════════════════

async def execute_tool(name: str, arguments: Dict, deps: ToolDependencies) -> str:
    """执行指定工具，返回 JSON 字符串结果。

    Args:
        name: 工具名称
        arguments: 工具参数 (已从 LLM function_call 解析)
        deps: 依赖注入

    Returns:
        JSON 字符串，包含 result 或 error
    """
    try:
        if name == "query_database":
            return await _query_database(arguments, deps)
        elif name == "get_market_data":
            return await _get_market_data(arguments, deps)
        elif name == "get_or_update_memory":
            return await _get_or_update_memory(arguments, deps)
        elif name == "send_notification":
            return await _send_notification(arguments, deps)
        elif name == "get_config":
            return await _get_config(arguments, deps)
        else:
            return json.dumps({"error": f"未知工具: {name}"}, ensure_ascii=False)
    except Exception as e:
        logger.warning(f"[HermesTools] 工具执行失败 {name}: {e}")
        return json.dumps({"error": str(e)}, ensure_ascii=False)


async def _query_database(args: Dict, deps: ToolDependencies) -> str:
    query_type = args["query_type"]
    market = args["market"]
    date = args.get("date", datetime.now().strftime("%Y-%m-%d"))
    limit = int(args.get("limit", 30))

    conn = sqlite3.connect(deps.db_path)
    conn.row_factory = sqlite3.Row

    try:
        if query_type == "trades":
            prefix = "HK." if market == "hk" else "US."
            rows = conn.execute(
                """SELECT code, action, entry_price, exit_price, quantity,
                   pnl, pnl_percent, entry_time, exit_time, exit_reason, strategy
                   FROM trades WHERE code LIKE ? AND entry_time >= ? AND entry_time < ?
                   ORDER BY entry_time DESC LIMIT ?""",
                (f"{prefix}%", f"{date} 00:00:00", f"{date} 23:59:59", limit),
            ).fetchall()
        elif query_type == "positions":
            prefix = "HK." if market == "hk" else "US."
            rows = conn.execute(
                """SELECT code, action, entry_price, quantity,
                   unrealized_pnl, unrealized_pnl_pct, entry_time
                   FROM positions WHERE code LIKE ? AND status='open'
                   ORDER BY entry_time DESC LIMIT ?""",
                (f"{prefix}%", limit),
            ).fetchall()
        elif query_type == "signals":
            prefix = "HK." if market == "hk" else "US."
            rows = conn.execute(
                """SELECT id, code, direction, technical_score AS signal_score, confidence,
                   created_at, status
                   FROM signals WHERE code LIKE ? AND created_at >= ?
                   ORDER BY created_at DESC LIMIT ?""",
                (f"{prefix}%", f"{date} 00:00:00", limit),
            ).fetchall()
        elif query_type == "news":
            rows = conn.execute(
                """SELECT headline AS title, COALESCE(content, '') AS summary, source, fetched_at
                   FROM news_cache WHERE fetched_at >= ? AND fetched_at < ?
                   ORDER BY fetched_at DESC LIMIT ?""",
                (f"{date} 00:00:00", f"{date} 23:59:59", limit),
            ).fetchall()
        elif query_type == "lessons":
            # ftseek.db 无 lessons 表，改查 knowledge.db
            conn.close()
            import os
            kb_path = os.path.join(os.path.dirname(deps.db_path), "knowledge.db")
            if os.path.exists(kb_path):
                conn2 = sqlite3.connect(kb_path)
                conn2.row_factory = sqlite3.Row
                rows = conn2.execute(
                    """SELECT date AS trade_date, stock_code AS symbol, title AS cause,
                          content AS lesson, confidence AS confidence
                       FROM lessons WHERE date >= ? AND date < ?
                       ORDER BY date DESC LIMIT ?""",
                    (date, date, limit),
                ).fetchall()
                results = [dict(r) for r in rows]
                conn2.close()
                return json.dumps(results, ensure_ascii=False, default=str)
            return json.dumps([], ensure_ascii=False)
        else:
            conn.close()
            return json.dumps({"error": f"未知查询类型: {query_type}"}, ensure_ascii=False)

        conn.close()
        results = [dict(r) for r in rows]

        # 截断过大的结果
        result_json = json.dumps(results, ensure_ascii=False, default=str)
        if len(result_json) > 3000:
            result_json = json.dumps(results[:limit//2], ensure_ascii=False, default=str)
        return result_json

    except Exception as e:
        conn.close()
        return json.dumps({"error": f"数据库查询失败: {e}"}, ensure_ascii=False)


async def _get_market_data(args: Dict, deps: ToolDependencies) -> str:
    symbols = args.get("symbols", [])[:20]

    if not deps.quote_ctx:
        return json.dumps(
            {"error": "行情上下文不可用 (Futu OpenD 未连接)", "symbols": symbols},
            ensure_ascii=False,
        )

    try:
        ret, data = deps.quote_ctx.get_market_snapshot(symbols)
        if ret != 0 or data is None or data.empty:
            return json.dumps({"error": f"获取行情失败 (ret={ret})", "symbols": symbols}, ensure_ascii=False)

        results = []
        for _, row in data.iterrows():
            results.append({
                "code": row.get("code", ""),
                "last_price": float(row.get("last_price", 0)),
                "change_rate": float(row.get("change_rate", 0) or 0) / 100.0,
                "volume_ratio": float(row.get("volume_ratio", 0) or 0),
                "turnover": float(row.get("turnover", 0) or 0),
                "amplitude": float(row.get("amplitude", 0) or 0) / 100.0,
            })
        return json.dumps(results, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"error": f"行情获取异常: {e}", "symbols": symbols}, ensure_ascii=False)


async def _get_or_update_memory(args: Dict, deps: ToolDependencies) -> str:
    action = args["action"]
    market = args.get("market", "hk")
    memory_file = os.path.join(deps.memory_dir, f"{market}.json")

    os.makedirs(deps.memory_dir, exist_ok=True)

    if action == "read":
        if os.path.exists(memory_file):
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    memory = json.load(f)
                # 仅返回 Agent 需要的字段，截断过长内容
                summary = {
                    "market": memory.get("market", ""),
                    "core_principle": memory.get("core_principle", ""),
                    "rules": memory.get("market_specific_rules", []),
                    "recent_lessons": memory.get("historical_lessons", [])[-10:],
                    "symbol_lessons": memory.get("symbol_lessons", {}),
                    "performance": memory.get("performance_tracking", {}),
                }
                return json.dumps(summary, ensure_ascii=False, indent=2)
            except Exception as e:
                return json.dumps({"error": f"读取记忆失败: {e}"}, ensure_ascii=False)
        else:
            return json.dumps({"market": market, "message": "无历史记忆", "lessons": []}, ensure_ascii=False)

    elif action == "append":
        lesson_text = args.get("lesson", "")
        if not lesson_text:
            return json.dumps({"error": "append 操作需要提供 lesson"}, ensure_ascii=False)

        memory = {}
        if os.path.exists(memory_file):
            try:
                with open(memory_file, "r", encoding="utf-8") as f:
                    memory = json.load(f)
            except Exception:
                memory = {"historical_lessons": [], "symbol_lessons": {}}

        memory.setdefault("historical_lessons", [])
        entry = f"{datetime.now().strftime('%Y-%m-%d')}: {lesson_text}"
        if entry not in memory["historical_lessons"]:
            memory["historical_lessons"].append(entry)
        memory["historical_lessons"] = memory["historical_lessons"][-20:]
        memory["updated_at"] = datetime.now().isoformat()

        symbol = args.get("symbol", "")
        if symbol:
            memory.setdefault("symbol_lessons", {})
            memory["symbol_lessons"][symbol] = lesson_text

        try:
            with open(memory_file, "w", encoding="utf-8") as f:
                json.dump(memory, f, ensure_ascii=False, indent=2)
            return json.dumps({"status": "ok", "message": "教训已写入记忆"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"写入记忆失败: {e}"}, ensure_ascii=False)


async def _send_notification(args: Dict, deps: ToolDependencies) -> str:
    text = args.get("text", "")
    if not text:
        return json.dumps({"error": "通知内容为空"}, ensure_ascii=False)

    if deps.notifier:
        try:
            deps.notifier.send_text(text)
            return json.dumps({"status": "ok", "message": "通知已推送"}, ensure_ascii=False)
        except Exception as e:
            return json.dumps({"error": f"推送失败: {e}"}, ensure_ascii=False)
    else:
        return json.dumps({"status": "skipped", "message": "notifier 不可用，跳过推送"}, ensure_ascii=False)


async def _get_config(args: Dict, deps: ToolDependencies) -> str:
    keys = args.get("keys", [])

    if not deps.config_center:
        return json.dumps({"error": "ConfigCenter 不可用"}, ensure_ascii=False)

    try:
        if not keys:
            # 返回关键配置摘要
            keys = ["risk_limits", "strategy", "watchlist_hk", "trading_hours"]

        result = {}
        for key in keys[:10]:
            try:
                val = deps.config_center.get(key)
                if val is not None:
                    # 截断过长的配置值
                    val_str = json.dumps(val, ensure_ascii=False, default=str)
                    if len(val_str) > 1000:
                        val = f"(配置过长，{len(val_str)} 字符，已截断)"
                    result[key] = val
            except Exception:
                result[key] = "(不可用)"

        return json.dumps(result, ensure_ascii=False, default=str)
    except Exception as e:
        return json.dumps({"error": f"读取配置失败: {e}"}, ensure_ascii=False)
