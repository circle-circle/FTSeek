"""
FTSeek Agent 请求处理器

运行在 Hermes Agent 中，定期处理 FTSeek 的请求:
1. 扫描 ~/.hermes/ftseek/requests/ 目录
2. 读取请求文件
3. 调用 LLM 生成响应
4. 写入 ~/.hermes/ftseek/responses/ 目录

可通过 cron 每 5 分钟运行一次
"""

import json
import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# 设置日志
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("ftseek.agent_processor")

HERMES_HOME = os.path.expanduser("~/.hermes")
FTSEEK_DIR = os.path.join(HERMES_HOME, "ftseek")
REQUESTS_DIR = os.path.join(FTSEEK_DIR, "requests")
RESPONSES_DIR = os.path.join(FTSEEK_DIR, "responses")
MEMORY_DIR = os.path.join(FTSEEK_DIR, "memory")

# 确保目录存在
for d in [REQUESTS_DIR, RESPONSES_DIR, MEMORY_DIR]:
    os.makedirs(d, exist_ok=True)


def process_requests():
    """处理所有待处理的请求"""
    if not os.path.exists(REQUESTS_DIR):
        logger.info("请求目录不存在，跳过")
        return
    
    request_files = [f for f in os.listdir(REQUESTS_DIR) if f.endswith('.json')]
    
    if not request_files:
        logger.info("无待处理请求")
        return
    
    logger.info(f"发现 {len(request_files)} 个待处理请求")
    
    for filename in request_files:
        request_file = os.path.join(REQUESTS_DIR, filename)
        try:
            with open(request_file, 'r', encoding='utf-8') as f:
                request = json.load(f)
            
            request_id = request.get("id", filename.replace('.json', ''))
            request_type = request.get("type", "unknown")
            
            logger.info(f"处理请求: {request_id} ({request_type})")
            
            # 根据类型处理
            if request_type == "generate_battle_plan":
                result = handle_battle_plan(request)
            elif request_type == "generate_eod_review":
                result = handle_eod_review(request)
            else:
                result = {"error": f"未知请求类型: {request_type}"}
            
            # 写入响应
            response = {
                "request_id": request_id,
                "timestamp": datetime.now().isoformat(),
                "status": "completed",
                "result": result
            }
            
            response_file = os.path.join(RESPONSES_DIR, filename)
            with open(response_file, 'w', encoding='utf-8') as f:
                json.dump(response, f, ensure_ascii=False, indent=2)
            
            logger.info(f"✅ 响应已写入: {response_file}")
            
        except Exception as e:
            logger.error(f"处理请求失败 {filename}: {e}")


def handle_battle_plan(request: Dict) -> Dict:
    """处理盘前计划生成请求"""
    context = request.get("context", {})
    memory = context.get("memory", "")
    yesterday_review = context.get("yesterday_review", "")
    overnight_news = context.get("overnight_news", "")
    
    # 这里应该调用 LLM API
    # 当前返回模拟响应
    return {
        "status": "success",
        "message": "盘前计划已生成",
        "plan": {
            "date": datetime.now().strftime('%Y-%m-%d'),
            "market_regime": "NEUTRAL",
            "targets": [],
            "risk_warnings": ["等待 LLM 集成"],
            "max_positions": 3,
            "notes": "此响应为占位符，等待 LLM 集成"
        }
    }


def handle_eod_review(request: Dict) -> Dict:
    """处理盘后归因报告请求"""
    context = request.get("context", {})
    trades = context.get("trades", [])
    
    return {
        "status": "success",
        "message": "盘后报告已生成",
        "summary": {
            "total_trades": len(trades),
            "win_rate": 0.0,
            "total_pnl": 0.0
        },
        "memory_updates": {
            "historical_lessons": [],
            "symbol_lessons": {}
        }
    }


if __name__ == "__main__":
    process_requests()
