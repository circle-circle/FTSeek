#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek 盘前新闻联动策略推送 (Pre-Market News-Linked Strategy Push)

功能:
1. 采集最新新闻 (富途快讯 + 本地缓存 + RSSHub)
2. 获取美股隔夜行情 + 港股盘前数据
3. LLM 深度分析新闻联动行情的买卖策略
4. 格式化推送至钉钉 (含完整推理链、止盈止损、仓位建议)

触发时机:
- crontab 08:00 交易日自动执行
- 也可手动触发测试

集成:
- 复用 llm_news_strategy.py 策略引擎
- 复用 DingTalkNotifier 推送
- 独立脚本，不依赖主进程

作者: FTSeek Team
日期: 2026-04-15
"""

import asyncio
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# ============================================================
# 路径与配置
# ============================================================
BASE_DIR = Path("/opt/ftseek/FTSeek")
sys.path.insert(0, str(BASE_DIR))

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv(BASE_DIR / ".env")
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s: %(message)s",
    handlers=[
        logging.StreamHandler(),
        logging.FileHandler(BASE_DIR / "logs" / "pre_market_strategy_push.log"),
    ]
)
logger = logging.getLogger("pre_market_strategy_push")

# ============================================================
# 配置加载
# ============================================================

def load_config() -> dict:
    """加载推送配置"""
    return {
        "bailian_api_key": os.getenv("BAILIAN_API_KEY", ""),
        "bailian_model": os.getenv("BAILIAN_MODEL", "qwen-plus"),
        "bailian_timeout": int(os.getenv("BAILIAN_TIMEOUT", "90")),
        "dingtalk_webhook": os.getenv("DINGTALK_WEBHOOK_URL", ""),
        "dingtalk_secret": os.getenv("DINGTALK_SECRET", ""),
        "futu_host": os.getenv("FUTU_HOST", "127.0.0.1"),
        "futu_port": int(os.getenv("FUTU_PORT", "11111")),
        "max_news_items": 15,        # 最多分析的新闻数
        "llm_temperature": 0.1,
        "push_to_dingtalk": True,
    }


# ============================================================
# 新闻采集
# ============================================================

def collect_latest_news(max_items: int = 15) -> List[dict]:
    """
    采集最新新闻 (多源合并 + 去重)
    
    来源优先级:
    1. RSSHub 本地缓存 (最近72h, 扩展窗口)
    2. 金十数据 API (备用)
    3. 富途新闻缓存
    4. AkShare 财经快讯 (兜底)
    """
    all_news = []
    
    # 源1: RSSHub 本地缓存 (最近72h, 扩展窗口)
    try:
        data_dir = BASE_DIR / "data" / "rsshub_multi_source"
        if data_dir.exists():
            # 读取所有可用的缓存文件 (不只是最新)
            json_files = sorted(data_dir.glob("multi_source_*.json"))
            for cache_file in json_files[-3:]:  # 最多读最近3个文件
                with open(cache_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                logger.info(f"RSSHub缓存: {cache_file.name} ({len(data)}条)")
                
                # 过滤最近72h (扩展窗口, 防止数据断层)
                cutoff = datetime.now() - timedelta(hours=72)
                for item in data:
                    pub = item.get("publish_time", "")
                    if pub:
                        try:
                            pub_dt = datetime.fromisoformat(pub.replace("Z", "+00:00").split("+")[0])
                            if pub_dt < cutoff:
                                continue
                        except:
                            pass
                    
                    all_news.append({
                        "title": item.get("title", ""),
                        "content": item.get("content", item.get("summary", "")),
                        "source": item.get("source_name", "rsshub"),
                        "publish_time": item.get("publish_time", ""),
                        "is_major": item.get("is_major", False),
                        "markets": item.get("detected_markets", []),
                        "stocks": item.get("stocks", []),
                    })
    except Exception as e:
        logger.warning(f"RSSHub缓存读取失败: {e}")
    
    # 源2: 金十快讯 API (备用源, 不依赖RSSHub)
    if len(all_news) < 3:
        logger.info("RSSHub数据不足, 尝试金十API...")
        try:
            import requests
            resp = requests.get(
                "https://flash-api.jin10.com/get_flash_list",
                params={"channel": "-8200", "vip": "1"},
                timeout=10,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.jin10.com/"},
            )
            if resp.status_code == 200:
                jin10_data = resp.json().get("data", [])
                for item in jin10_data[:20]:
                    ts = item.get("time", 0)
                    pub_time = datetime.fromtimestamp(ts).isoformat() if ts else ""
                    all_news.append({
                        "title": item.get("content", "")[:100],
                        "content": item.get("content", ""),
                        "source": "jin10",
                        "publish_time": pub_time,
                        "is_major": item.get("important", False),
                        "markets": ["HK", "US"],
                        "stocks": [],
                    })
                logger.info(f"金十API: 获取 {len(jin10_data[:20])} 条")
        except Exception as e:
            logger.warning(f"金十API获取失败: {e}")
    
    # 源3: 富途新闻缓存
    try:
        futu_cache = BASE_DIR / "data" / "futu_news_cache.json"
        if futu_cache.exists():
            with open(futu_cache, "r", encoding="utf-8") as f:
                futu_data = json.load(f)
            for item in futu_data.get("news", [])[:10]:
                all_news.append({
                    "title": item.get("title", ""),
                    "content": item.get("content", ""),
                    "source": "futu",
                    "publish_time": item.get("publish_time", ""),
                    "is_major": item.get("importance", "normal") == "major",
                    "markets": [],
                    "stocks": item.get("stocks", []),
                })
    except Exception as e:
        logger.warning(f"富途缓存读取失败: {e}")
    
    # 去重 (按标题)
    seen = set()
    unique_news = []
    for news in all_news:
        title_hash = hash(news.get("title", "")[:50])
        if title_hash not in seen:
            seen.add(title_hash)
            unique_news.append(news)
    
    # 排序: 重要新闻优先 + 时间倒序
    unique_news.sort(key=lambda x: (x.get("is_major", False), x.get("publish_time", "")), reverse=True)
    
    # 截断
    result = unique_news[:max_items]
    logger.info(f"新闻采集完成: {len(result)}条 (总{len(all_news)}条, 去重前{len(unique_news)}条)")
    return result


# ============================================================
# 行情数据获取
# ============================================================

def get_market_snapshot(codes: List[str]) -> Dict[str, dict]:
    """
    通过 Futu OpenD 获取行情快照
    
    Returns:
        {code: {price, change_pct, volume, turnover, ...}}
    """
    market_data = {}
    if not codes:
        return market_data
    
    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        ret, data = ctx.get_market_snapshot(codes)
        ctx.close()
        
        if ret == 0:
            for _, row in data.iterrows():
                code = row.get("code", "")
                market_data[code] = {
                    "price": float(row.get("last_price", 0)),
                    "change_pct": float(row.get("change_rate", 0)),
                    "volume": int(row.get("volume", 0)),
                    "turnover": float(row.get("turnover", 0)),
                    "high": float(row.get("high_price", 0)),
                    "low": float(row.get("low_price", 0)),
                    "open": float(row.get("open_price", 0)),
                    "prev_close": float(row.get("prev_close_price", 0)),
                }
            logger.info(f"行情快照: {len(market_data)}只股票")
    except Exception as e:
        logger.warning(f"行情快照获取失败: {e} (使用模拟数据)")
        # 模拟数据 fallback
        for code in codes:
            market_data[code] = {
                "price": 0,
                "change_pct": 0,
                "volume": 0,
                "note": "数据不可用 (Futu未连接)",
            }
    
    return market_data


def get_us_overnight_summary() -> str:
    """获取美股隔夜市场摘要"""
    try:
        from futu import OpenQuoteContext
        ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
        
        # 美股主要指数
        us_codes = ["US.SPX", "US.NDX", "US.DJI", "US.VIX"]
        ret, data = ctx.get_market_snapshot(us_codes)
        ctx.close()
        
        if ret == 0:
            lines = []
            for _, row in data.iterrows():
                code = row.get("code", "")
                name_map = {"US.SPX": "标普500", "US.NDX": "纳指100", "US.DJI": "道指", "US.VIX": "VIX恐慌指数"}
                name = name_map.get(code, code)
                change = float(row.get("change_rate", 0))
                price = float(row.get("last_price", 0))
                emoji = "📈" if change > 0 else ("📉" if change < 0 else "➡️")
                lines.append(f"{emoji} {name}: {price:.2f} ({change:+.2f}%)")
            return "\n".join(lines)
    except Exception as e:
        logger.debug(f"美股指数获取失败: {e}")
    
    return "美股数据暂不可用"


# ============================================================
# LLM 盘前策略分析
# ============================================================

PREMARKET_STRATEGY_PROMPT = """# Role
你是 FTSeek 首席盘前策略分析师。每天开盘前，你需要综合隔夜新闻、美股收盘行情、板块轮动，生成今日港股/美股的可执行交易策略。

# Input Data

## 一、最新重大新闻
{news_items}

## 二、美股隔夜行情
{us_market}

## 三、富途行情快照 (如可用)
{futu_snapshot}

## 四、监控池 (watchlist)
{watchlist}

# Analysis Framework (必须逐步执行)

## 第一步: 新闻定性
- 每条新闻属于: 宏观政策 / 地缘政治 / 行业周期 / 财报 / 并购重组 / 突发事件?
- 影响方向: 利好 / 利空 / 中性?
- 影响级别: 一级(重大) / 二级(中等) / 三级(轻微)?

## 第二步: 传导路径
对每条重要新闻，画出完整的传导链:
"新闻事件 → 行业/宏观指标变化 → 具体标的受益/受损逻辑"

## 第三步: 二阶效应
- 替代效应: 利空A是否间接利好B?
- 情绪传染: 美股隔夜表现如何映射到港股?
- 资金流向: 南向/北向可能的方向?

## 第四步: 技术面配合
- 结合行情快照的价格、涨跌幅判断技术面是否支持基本面逻辑
- RSI 是否超买/超卖? 成交量是否配合?

## 第五步: 生成策略
为每个监控池中的相关标的生成具体策略

# Output Format (严格 JSON)
{{
  "market_overview": "今日市场整体判断 (50字以内)",
  "key_themes": ["主题1", "主题2", "主题3"],
  "overall_bias": "偏多 / 偏空 / 震荡",
  "risk_level": "低 / 中 / 高",
  "strategies": [
    {{
      "code": "HK.00700",
      "name": "腾讯控股",
      "direction": "买入 / 卖出 / 观望",
      "confidence": "高 / 中 / 低",
      "position_suggestion": "重仓(12%) / 标准(8%) / 轻仓(4%) / 试探(2%) / 不建仓",
      "entry_logic": "详细的入场逻辑 (100字以内)",
      "exit_logic": "出场/止损逻辑 (50字以内)",
      "stop_loss_pct": -3.0,
      "take_profit_pct": 8.0,
      "news_trigger": "触发该策略的新闻标题",
      "second_order": ["二阶效应1", "二阶效应2"],
      "risk_warning": "风险提示"
    }}
  ],
  "watch_list": [
    {{
      "code": "US.NVDA",
      "name": "英伟达",
      "reason": "为什么值得关注但暂不操作",
      "trigger_condition": "什么条件下转为操作"
    }}
  ],
  "action_plan": "今日行动计划总结 (100字以内)"
}}

# Constraints
- 必须输出严格 JSON，不要添加任何其他文本
- strategies 至少包含3只相关标的 (如无则留空数组)
- 每只标的的 reasoning 必须包含具体的新闻关联和行情数据引用
- 止盈止损必须合理 (止损不超过-8%, 止盈不低于+3%)
"""


async def call_llm_strategy_analysis(
    news_items: List[dict],
    us_market: str,
    futu_snapshot: str,
    watchlist: str,
    config: dict,
) -> Optional[dict]:
    """调用 LLM 进行盘前策略分析"""
    
    # 格式化新闻
    news_text = ""
    for i, news in enumerate(news_items, 1):
        major_tag = "🔴重要" if news.get("is_major") else "普通"
        markets = "/".join(news.get("markets", ["ALL"]))
        news_text += f"""
{i}. [{major_tag}] [{markets}] {news['title']}
   来源: {news['source']} | 时间: {news['publish_time'][:19] if news.get('publish_time') else 'N/A'}
   内容: {news['content'][:200]}
"""
    
    prompt = PREMARKET_STRATEGY_PROMPT.format(
        news_items=news_text,
        us_market=us_market,
        futu_snapshot=futu_snapshot or "暂无实时行情",
        watchlist=watchlist,
    )
    
    logger.info(f"调用 LLM 盘前策略分析: model={config['bailian_model']}, news={len(news_items)}条")
    
    try:
        from openai import AsyncOpenAI
        client = AsyncOpenAI(
            api_key=config["bailian_api_key"],
            base_url="https://dashscope.aliyuncs.com/compatible-mode/v1",
        )
        
        model_name = config["bailian_model"].replace("bailian/", "")
        
        response = await asyncio.wait_for(
            client.chat.completions.create(
                model=model_name,
                messages=[
                    {"role": "system", "content": "你是 FTSeek 首席盘前策略分析师，输出严格 JSON。"},
                    {"role": "user", "content": prompt},
                ],
                temperature=config["llm_temperature"],
                max_tokens=3000,
            ),
            timeout=config["bailian_timeout"],
        )
        
        raw = response.choices[0].message.content
        logger.info(f"LLM 响应长度: {len(raw)} chars")
        
        # 清理控制字符 (LLM 可能在 JSON 中输出 \n \t 等)
        import re
        cleaned = re.sub(r'[\x00-\x1f\x7f-\x9f]', '', raw)
        
        # 提取 JSON
        json_match = re.search(r'\{[\s\S]*\}', cleaned)
        if json_match:
            result = json.loads(json_match.group())
            return result
        else:
            logger.error(f"LLM 响应无 JSON: {raw[:200]}")
            return None
            
    except Exception as e:
        logger.error(f"LLM 策略分析失败: {e}")
        return None


# ============================================================
# 钉钉推送
# ============================================================

def build_dingtalk_message(analysis: dict, timestamp: str) -> str:
    """构建钉钉推送消息 (Markdown 格式)"""
    
    # 市场概览
    overview = analysis.get("market_overview", "暂无")
    bias = analysis.get("overall_bias", "震荡")
    risk = analysis.get("risk_level", "中")
    themes = analysis.get("key_themes", [])
    
    bias_icon = {"偏多": "🐂", "偏空": "🐻", "震荡": "⚖️"}.get(bias, "📊")
    risk_icon = {"低": "🟢", "中": "🟡", "高": "🔴"}.get(risk, "🟡")
    
    themes_text = "\n".join([f"  • {t}" for t in themes]) if themes else "  暂无"
    
    # 策略详情
    strategies = analysis.get("strategies", [])
    strategies_text = ""
    for s in strategies:
        dir_emoji = {"买入": "📈", "卖出": "📉", "观望": "👀"}.get(s.get("direction", "观望"), "📊")
        
        strategies_text += f"""---

### {dir_emoji} {s.get('code')} ({s.get('name', '')})

| 项目 | 内容 |
|------|------|
| **方向** | {s.get('direction')} |
| **置信度** | {s.get('confidence')} |
| **仓位建议** | {s.get('position_suggestion')} |
| **止盈** | +{s.get('take_profit_pct', 0)}% |
| **止损** | {s.get('stop_loss_pct', 0)}% |

**入场逻辑**: {s.get('entry_logic', '暂无')}

**出场逻辑**: {s.get('exit_logic', '暂无')}

**触发新闻**: {s.get('news_trigger', '暂无')}

**二阶效应**: {', '.join(s.get('second_order', [])) or '暂无'}

⚠️ **风险**: {s.get('risk_warning', '暂无')}"""
    
    # 关注列表
    watch_list = analysis.get("watch_list", [])
    watch_text = ""
    if watch_list:
        watch_text = "\n\n---\n\n### 👀 重点关注 (暂不操作)\n\n"
        for w in watch_list:
            watch_text += f"- **{w.get('code')} ({w.get('name', '')})**: {w.get('reason', '')} → {w.get('trigger_condition', '')}\n"
    
    # 行动计划
    action = analysis.get("action_plan", "暂无")
    
    msg = f"""### 🌅 FTSeek 盘前策略推送 | {timestamp}

**市场判读**: {bias_icon} {bias} | {risk_icon} 风险等级: {risk}

**整体判断**: {overview}

**今日主题**:
{themes_text}

---

## 📋 交易策略

{strategies_text or "*暂无具体策略*"}

{watch_text}

---

### 📌 今日行动计划
{action}

---
⚡ LLM 驱动 · 多源数据融合 · 仅供参考不构成投资建议
🕐 生成时间: {timestamp}
"""
    
    return msg


def send_to_dingtalk(message: str, config: dict) -> bool:
    """发送消息到钉钉"""
    if not config.get("dingtalk_webhook"):
        logger.error("钉钉 Webhook 未配置")
        return False
    
    if not config.get("push_to_dingtalk"):
        logger.info("钉钉推送已禁用")
        return False
    
    import requests
    
    try:
        import hmac
        import hashlib
        import base64
        import urllib.parse
        
        webhook = config["dingtalk_webhook"]
        secret = config.get("dingtalk_secret", "")
        
        # 加签
        if secret:
            timestamp = str(round(time.time() * 1000))
            string_to_sign = f"{timestamp}\n{secret}"
            hmac_code = hmac.new(
                secret.encode("utf-8"),
                string_to_sign.encode("utf-8"),
                digestmod=hashlib.sha256,
            ).digest()
            sign = urllib.parse.quote_plus(base64.b64encode(hmac_code))
            url = f"{webhook}&timestamp={timestamp}&sign={sign}"
        else:
            url = webhook
        
        payload = {
            "msgtype": "markdown",
            "markdown": {
                "title": "FTSeek 盘前策略推送",
                "text": message,
            },
        }
        
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            result = resp.json()
            if result.get("errcode") == 0:
                logger.info("✅ 钉钉推送成功")
                return True
            else:
                logger.error(f"❌ 钉钉返回错误: {result}")
                return False
        else:
            logger.error(f"❌ 钉钉 HTTP {resp.status_code}: {resp.text[:200]}")
            return False
            
    except Exception as e:
        logger.error(f"钉钉推送异常: {e}")
        return False


# ============================================================
# 主流程
# ============================================================

async def run_pre_market_strategy_push(test_mode: bool = False):
    """
    盘前策略推送主流程
    
    Args:
        test_mode: 测试模式 (使用模拟数据)
    """
    config = load_config()
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    
    logger.info("=" * 70)
    logger.info(f"FTSeek 盘前新闻联动策略推送 | {timestamp}")
    logger.info("=" * 70)
    
    if test_mode:
        logger.info("🔧 测试模式: 使用指定新闻")
    
    # Step 1: 采集新闻
    if test_mode:
        # 测试用新闻
        news_items = [
            {
                "title": "博通与Meta官宣长期战略合作，联合扩产MTIA定制AI芯片至数吉瓦级",
                "content": "博通(Broadcom, AVGO)与Meta Platforms正式宣布达成长期战略合作伙伴关系，双方将联合扩产MTIA定制AI芯片，产能规划达数吉瓦级。瑞银同步上调博通2027年TPU出货量至700万颗。此举标志着AI芯片定制市场进入爆发阶段，ASIC需求持续高速增长。",
                "source": "futu",
                "publish_time": "2026-04-14T21:09:00",
                "is_major": True,
                "markets": ["US", "HK"],
                "stocks": ["US.AVGO", "US.MRVL", "US.NVDA"],
            },
            {
                "title": "亚马逊逾百亿美元收购卫星公司 直面星链竞争",
                "content": "亚马逊宣布以逾百亿美元价格收购一家低轨卫星互联网公司，加速推进Kuiper卫星星座项目，直接挑战SpaceX的Starlink。此举将大幅加速全球卫星互联网赛道竞争，地面设备制造商和发射服务提供商将受益。",
                "source": "futu",
                "publish_time": "2026-04-14T20:43:45",
                "is_major": True,
                "markets": ["US"],
                "stocks": ["US.AMZN", "US.AAPL"],
            },
            {
                "title": "美国最大电网PJM发布紧急提案：拟增15吉瓦电力，以应对AI数据中心浪潮",
                "content": "美国最大电网运营商PJM Interconnection发布紧急提案，计划新增15吉瓦电力产能，以应对AI数据中心带来的电力需求激增。提案涉及多个州的电网扩容，预计将带动电力设备、变压器、储能等相关产业链。",
                "source": "rsshub",
                "publish_time": "2026-04-14T18:30:00",
                "is_major": False,
                "markets": ["US"],
                "stocks": [],
            },
            {
                "title": "瑞银上调博通2027年TPU出货量至700万颗",
                "content": "瑞银分析师发布最新研报，将博通(Broadcom)2027年TPU出货量预测从此前的400万颗上调至700万颗，主要基于AI芯片定制需求的超预期增长。维持买入评级，目标价上调至250美元。",
                "source": "rsshub",
                "publish_time": "2026-04-14T17:45:00",
                "is_major": False,
                "markets": ["US"],
                "stocks": ["US.AVGO"],
            },
            {
                "title": "办公巨头一季度业绩预告出炉，两连板算力芯片概念股提示交易风险",
                "content": "多家算力芯片概念股连续涨停后发布风险提示公告。一季度业绩预告显示行业整体景气度提升，但部分公司估值已明显高于行业平均，提示投资者注意交易风险。",
                "source": "rsshub",
                "publish_time": "2026-04-14T16:00:00",
                "is_major": True,
                "markets": ["HK"],
                "stocks": [],
            },
        ]
        logger.info(f"测试新闻: {len(news_items)}条")
    else:
        news_items = collect_latest_news(max_items=config["max_news_items"])
    
    # Step 2: 获取美股隔夜行情
    logger.info("获取美股隔夜行情...")
    us_market = get_us_overnight_summary()
    logger.info(f"美股行情:\n{us_market}")
    
    # Step 3: 获取富途行情快照
    all_stock_codes = set()
    for news in news_items:
        for stock in news.get("stocks", []):
            all_stock_codes.add(stock)
    # 加入主要watchlist
    all_stock_codes.update(["HK.00700", "HK.09988", "HK.03690", "HK.00981", "HK.01211"])
    
    logger.info(f"行情快照: {len(all_stock_codes)}只股票")
    market_data = get_market_snapshot(list(all_stock_codes))
    
    futu_snapshot_text = ""
    for code, data in market_data.items():
        if data.get("price", 0) > 0:
            futu_snapshot_text += f"{code}: 现价={data['price']}, 涨跌={data['change_pct']:+.2f}%\n"
    
    # Step 4: 构建 watchlist 字符串
    watchlist_text = ", ".join(sorted(all_stock_codes))
    
    # [NEW] Step 4.5: 数据充分性检查
    data_sufficiency = {
        "news_count": len(news_items),
        "has_us_market": "暂不可用" not in us_market and len(us_market) > 10,
        "has_hk_snapshot": len(futu_snapshot_text) > 10,
    }
    data_score = sum([
        data_sufficiency["news_count"] >= 3,  # 至少3条新闻
        data_sufficiency["has_us_market"],     # 有美股数据
        data_sufficiency["has_hk_snapshot"],   # 有港股快照
    ])
    
    logger.info(f"数据充分性检查: 新闻={data_sufficiency['news_count']}条, "
                f"美股={'✅' if data_sufficiency['has_us_market'] else '❌'}, "
                f"港股={'✅' if data_sufficiency['has_hk_snapshot'] else '❌'}, "
                f"得分={data_score}/3")
    
    if data_score == 0 and not test_mode:
        # 数据严重不足, 不推送
        warning_msg = (
            f"### ⚠️ 盘前策略推送 — 数据不足跳过\n\n"
            f"**时间**: {timestamp}\n\n"
            f"数据严重不足, 暂不推送策略:\n"
            f"- 新闻: {data_sufficiency['news_count']}条 (需≥3)\n"
            f"- 美股行情: {'可用' if data_sufficiency['has_us_market'] else '不可用'}\n"
            f"- 港股快照: {'可用' if data_sufficiency['has_hk_snapshot'] else '不可用'}\n\n"
            f"可能原因: RSSHub 服务异常 / Futu OpenD 未连接 / 非交易日\n\n"
            f"建议: 检查数据源后手动运行 `python3 scripts/pre_market_strategy_push.py`"
        )
        logger.warning("⚠️ 数据不足, 跳过推送")
        return False, {"error": "insufficient_data", "details": data_sufficiency}
    
    if data_score == 1:
        logger.warning("⚠️ 数据不足 (1/3), 推送质量可能较低")
    
    # Step 5: LLM 策略分析
    logger.info("LLM 盘前策略分析中...")
    analysis = await call_llm_strategy_analysis(
        news_items=news_items,
        us_market=us_market,
        futu_snapshot=futu_snapshot_text,
        watchlist=watchlist_text,
        config=config,
    )
    
    if not analysis:
        logger.error("LLM 策略分析返回空，尝试降级...")
        # 降级: 使用规则引擎 (内联实现，避免 app.factors numpy 依赖)
        FALLBACK_RULES = {
            "收购": {"direction": "买入", "confidence": "中", "position": "轻仓(4%)", "stop": -3.0, "tp": 8.0},
            "并购": {"direction": "买入", "confidence": "中", "position": "轻仓(4%)", "stop": -3.0, "tp": 8.0},
            "合作": {"direction": "买入", "confidence": "中", "position": "轻仓(4%)", "stop": -3.0, "tp": 8.0},
            "扩产": {"direction": "买入", "confidence": "中", "position": "轻仓(4%)", "stop": -3.0, "tp": 8.0},
            "超预期": {"direction": "买入", "confidence": "中", "position": "标准(8%)", "stop": -4.0, "tp": 10.0},
            "不及预期": {"direction": "卖出", "confidence": "中", "position": "轻仓(4%)", "stop": 3.0, "tp": -8.0},
            "制裁": {"direction": "卖出", "confidence": "中", "position": "轻仓(4%)", "stop": 3.0, "tp": -8.0},
            "暴跌": {"direction": "卖出", "confidence": "中", "position": "轻仓(4%)", "stop": 3.0, "tp": -8.0},
        }
        
        strategies = []
        for news in news_items[:5]:
            text = f"{news['title']} {news['content']}"
            matched = None
            for kw, rule in FALLBACK_RULES.items():
                if kw in text:
                    matched = (kw, rule)
                    break
            if not matched:
                continue
            
            kw, rule = matched
            for stock in news.get("stocks", [])[:3]:
                strategies.append({
                    "code": stock,
                    "name": stock,
                    "direction": rule["direction"],
                    "confidence": rule["confidence"],
                    "position_suggestion": rule["position"],
                    "entry_logic": f"规则引擎命中关键词 '{kw}'",
                    "exit_logic": f"止损{rule['stop']}%, 止盈+{rule['tp']}%",
                    "stop_loss_pct": rule["stop"],
                    "take_profit_pct": rule["tp"],
                    "news_trigger": news["title"],
                    "second_order_effects": [],
                    "risk_warning": "降级规则引擎生成",
                })
        
        analysis = {
            "market_overview": "LLM降级: 基于规则引擎分析",
            "key_themes": ["新闻驱动策略"],
            "overall_bias": "震荡",
            "risk_level": "中",
            "strategies": strategies,
            "watch_list": [],
            "action_plan": "建议关注新闻驱动标的，严格控制仓位",
        }
    
    # Step 6: 构建推送消息
    message = build_dingtalk_message(analysis, timestamp)
    
    # Step 7: 推送到钉钉
    logger.info("推送至钉钉...")
    success = send_to_dingtalk(message, config)
    
    if success:
        logger.info("✅ 盘前策略推送完成")
    else:
        logger.warning("⚠️ 钉钉推送失败，消息已记录到日志")
    
    # Step 8: 输出到日志文件
    output_file = BASE_DIR / "logs" / f"pre_market_strategy_{datetime.now().strftime('%Y%m%d')}.md"
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"# FTSeek 盘前策略推送 | {timestamp}\n\n")
        f.write(message)
    logger.info(f"策略报告已保存: {output_file}")
    
    return success, analysis


# ============================================================
# 入口
# ============================================================

if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="FTSeek 盘前新闻联动策略推送")
    parser.add_argument("--test", action="store_true", help="测试模式 (使用模拟新闻数据)")
    parser.add_argument("--no-push", action="store_true", help="仅分析，不推送到钉钉")
    args = parser.parse_args()
    
    if args.no_push:
        config = load_config()
        config["push_to_dingtalk"] = False
    
    success, analysis = asyncio.get_event_loop().run_until_complete(
        run_pre_market_strategy_push(test_mode=args.test)
    )
    
    if success:
        print(f"\n✅ 盘前策略推送成功")
        sys.exit(0)
    else:
        print(f"\n⚠️ 推送失败，请检查日志")
        sys.exit(1)
