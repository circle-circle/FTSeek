#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
测试: 美伊和谈新闻 → 跨市场映射 → 时效管理 → 快速获利策略

验证 LLM 是否能正确:
1. 解析宏观地缘新闻 (美伊和谈)
2. 映射到多资产影响 (油价/美股/黄金/美元/比特币)
3. 推断港股关联标的 (航空/科技/能源/黄金/加密)
4. 评估情绪有效期和衰减逻辑
5. 生成可执行的交易策略

作者: FTSeek Team
日期: 2026-04-15
"""

import asyncio
import json
import sys
import os
sys.path.insert(0, '/opt/ftseek/FTSeek')

# 加载 .env
try:
    from dotenv import load_dotenv
    load_dotenv('/opt/ftseek/FTSeek/.env')
except ImportError:
    pass

# 测试新闻
TEST_NEWS = {
    "title": "美伊和谈重启预期驱动全面风险偏好反弹",
    "content": """油价下挫，美股美债齐涨，其中纳指连涨十天、创2021年以来最长连涨纪录。
周二美股指普涨，美股存储芯片指数收涨超2.4%，继续创收盘历史新高。美光科技涨9.17%，闪迪则跌0.84%。
美国航空大涨8%，美联航涨2.07%。美元连续第七日走低，黄金涨超2%、升破每盎司4800美元，
比特币则涨至冲突爆发以来最高水平。""",
    "source": "综合",
    "pub_time": "2026-04-15T06:30:00",
    "event_name": "地缘冲突与能源",
    "event_sector": "宏观地缘/多资产",
    "static_logic": "和谈预期 → 风险偏好反弹 (Risk-On) → 油价跌/美股涨/黄金涨/美元跌",
    "related_stocks": {
        # 航空 (油价下跌利好)
        "HK.00753": "中国国航",
        "HK.01055": "中国南方航空",
        # 科技 (纳指上涨传导)
        "HK.00700": "腾讯控股",
        "HK.09988": "阿里巴巴",
        # 半导体 (美光+9%映射)
        "HK.00981": "中芯国际",
        "HK.01347": "华虹半导体",
        # 能源 (油价下跌利空)
        "HK.00883": "中国海洋石油",
        # 黄金 (金价新高)
        "HK.02899": "紫金矿业",
        "HK.01818": "招金矿业",
        # 加密 (比特币新高)
        "HK.00863": "OSL集团",
        "HK.01849": "新火科技控股",
    },
    # 美股隔夜行情 (模拟)
    "market_data": {
        "US.NDX": {"price": 18500, "change_pct": 1.2, "note": "纳指10连涨"},
        "US.MU": {"price": 145.0, "change_pct": 9.17, "note": "美光科技"},
        "US.AAL": {"price": 16.5, "change_pct": 8.0, "note": "美国航空"},
        "US.UAL": {"price": 52.0, "change_pct": 2.07, "note": "美联航"},
        "DXY": {"price": 103.5, "change_pct": -0.5, "note": "美元连跌7天"},
        "GC": {"price": 4800, "change_pct": 2.0, "note": "黄金突破$4800"},
        "BTC": {"price": 78000, "change_pct": 3.5, "note": "比特币新高"},
        "CL": {"price": 75.0, "change_pct": -2.5, "note": "油价下跌"},
    }
}


async def main():
    print("=" * 80)
    print("🧪 测试: 美伊和谈 → 跨市场映射 → 时效管理 → 快速获利")
    print("=" * 80)
    
    # 1. 初始化 LLM 策略引擎
    print("\n[1] 初始化 LLM 新闻策略引擎...")
    # 直接导入 (绕过 app.factors.__init__.py 的 numpy 依赖)
    import importlib.util
    spec = importlib.util.spec_from_file_location("llm_news_strategy", "app/factors/llm_news_strategy.py")
    llm_mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(llm_mod)
    LLMNewsStrategyEngine = llm_mod.LLMNewsStrategyEngine
    
    engine = LLMNewsStrategyEngine({
        'enabled': True,
        'model': 'qwen-plus',
        'timeout': 90,
        'fallback_enabled': True,
    })
    
    # 2. 调用策略分析
    print("\n[2] 调用 LLM 策略分析...")
    print(f"    新闻: {TEST_NEWS['title']}")
    print(f"    标的: {len(TEST_NEWS['related_stocks'])} 只")
    print(f"    美股行情: {len(TEST_NEWS['market_data'])} 项")
    
    signal = await engine.analyze_news(
        news_title=TEST_NEWS['title'],
        news_content=TEST_NEWS['content'],
        news_source=TEST_NEWS['source'],
        pub_time=TEST_NEWS['pub_time'],
        event_name=TEST_NEWS['event_name'],
        event_sector=TEST_NEWS['event_sector'],
        static_logic=TEST_NEWS['static_logic'],
        related_stocks=TEST_NEWS['related_stocks'],
        market_data=TEST_NEWS['market_data'],
    )
    
    if not signal:
        print("  ❌ 策略分析返回空")
        return False
    
    print(f"\n✅ 策略分析完成!")
    print(f"  风险偏好: {signal.risk_bias}")
    print(f"  市场判断: {signal.market_overview[:60]}...")
    print(f"  情绪衰减: {signal.decay_logic[:60]}...")
    print(f"  生成策略: {len(signal.stock_strategies)} 只")
    
    # 3. 展示策略详情
    print("\n" + "=" * 80)
    print("📋 生成的交易策略:")
    print("=" * 80)
    
    actionable = []
    for s in signal.stock_strategies:
        if s.direction not in ('NEUTRAL', 'WATCH_ONLY'):
            actionable.append(s)
            print(f"\n{ '📈' if 'BUY' in s.direction else '📉'} {s.code} ({s.name})")
            print(f"  方向: {s.direction} | 置信度: {s.confidence} | 仓位: {s.position_tier}")
            print(f"  止盈: +{s.take_profit}% | 止损: {s.stop_loss}%")
            print(f"  情绪有效: {s.sentiment_expiry_minutes}分钟")
            print(f"  逻辑: {s.reasoning}")
            if s.us_hk_mapping:
                print(f"  美股映射: {s.us_hk_mapping}")
            if s.second_order_effects:
                print(f"  二阶效应: {', '.join(s.second_order_effects[:2])}")
    
    # 4. 桥接测试
    print("\n" + "=" * 80)
    print("🔗 信号桥接测试:")
    print("=" * 80)
    
    spec2 = importlib.util.spec_from_file_location("news_signal_bridge", "app/factors/news_signal_bridge.py")
    bridge_mod = importlib.util.module_from_spec(spec2)
    spec2.loader.exec_module(bridge_mod)
    NewsSignalBridge = bridge_mod.NewsSignalBridge
    
    bridge = NewsSignalBridge(signal_store=None, dingtalk_notifier=None)
    injected, rejected = bridge.bridge_signal(
        news_strategy_signal=signal,
        auto_inject=False,  # 测试不注入
        auto_alert=False,
    )
    
    print(f"  可执行信号: {len(injected)}")
    for sig in injected:
        extra = sig.extra
        print(f"    ✅ {sig.code} | {sig.action.value} | "
              f"情绪有效={extra.get('sentiment_expiry_minutes', 'N/A')}min | "
              f"映射={extra.get('us_hk_mapping', 'N/A')[:40]}")
    
    # 5. 时效性验证
    print("\n" + "=" * 80)
    print("⏱️  时效性验证:")
    print("=" * 80)
    
    short_expiry = [s for s in actionable if s.sentiment_expiry_minutes <= 120]
    medium_expiry = [s for s in actionable if 120 < s.sentiment_expiry_minutes <= 240]
    long_expiry = [s for s in actionable if s.sentiment_expiry_minutes > 240]
    
    print(f"  极强 (≤2h): {len(short_expiry)} 只 → 开盘立即执行")
    for s in short_expiry:
        print(f"    - {s.code} ({s.name})")
    
    print(f"  强 (2-4h): {len(medium_expiry)} 只 → 开盘半天有效")
    for s in medium_expiry:
        print(f"    - {s.code} ({s.name})")
    
    print(f"  中 (>4h): {len(long_expiry)} 只 → 持续1-2天")
    for s in long_expiry:
        print(f"    - {s.code} ({s.name})")
    
    print("\n" + "=" * 80)
    print("✅ 测试完成!")
    print("=" * 80)
    
    return True


if __name__ == "__main__":
    success = asyncio.get_event_loop().run_until_complete(main())
    sys.exit(0 if success else 1)
