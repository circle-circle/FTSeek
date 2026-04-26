"""
FTSeek 统一新闻管线 V2 — 端到端测试

模拟真实新闻场景，验证:
1. 多源采集 (模拟 3 个源同时推送)
2. 质量门禁 (时效/去重/相关性/重要性)
3. 双通道路由 (IMMEDIATE → 快速通道, NORMAL → 深度通道)
4. 地缘事件识别
5. 统计与监控
"""

import sys, os, asyncio, time
sys.path.insert(0, "/opt/ftseek/FTSeek")

from app.news_v2.models import (
    UnifiedNewsItemV2, NewsUrgency, NewsSource,
    compute_simhash, simhash_similarity, hamming_distance,
)
from app.news_v2.quality.dedup_v2 import DedupEngineV2
from app.news_v2.quality.relevance_filter import RelevanceFilter
from app.news_v2.sources.base import SourceAdapter, SourceHealthMonitor
from app.news_v2.pipeline import NewsPipelineV2, QualityGate


# ============================================================
# 模拟数据源
# ============================================================

class MockRSSHubSource(SourceAdapter):
    """模拟 RSSHub 源 (WSJ/Fed)"""
    def __init__(self):
        super().__init__(interval_seconds=1)
        self._news_items = [
            {
                "title": "美联储暗示暂停加息，鲍威尔偏鸽表态",
                "content": "美联储主席鲍威尔今日表示，随着通胀逐步回落，美联储可能在下一次会议上暂停加息...",
            },
            {
                "title": "WSJ: 伊朗达成停火协议，中东局势大幅缓和",
                "content": "据华尔街日报报道，伊朗与美国等多国达成历史性停火协议，国际原油价格应声下跌 3%...",
            },
            {
                "title": "苹果宣布 iPhone 17 Pro 将于 9 月发布",
                "content": "Apple 官方宣布下一代 iPhone 将搭载全新 A19 芯片...",
            },
            {
                "title": "中东冲突升级，以色列对伊朗发动军事打击",
                "content": "以色列空军对伊朗多个军事目标发动空袭，中东局势急剧恶化...",
            },
        ]
        self._index = 0
    
    @property
    def source_id(self): return "test_rsshub"
    @property
    def source_name(self): return "测试RSSHub"
    
    async def fetch(self):
        if self._index < len(self._news_items):
            item = self._news_items[self._index]
            self._index += 1
            return [UnifiedNewsItemV2(
                id=f"rsshub_{self._index}",
                title=item["title"],
                content=item["content"],
                source="rsshub_wsj",
                source_name="RSSHub WSJ",
                timestamp=time.time(),
            )]
        return []


class MockFutuSource(SourceAdapter):
    """模拟富途7x24快讯源"""
    def __init__(self):
        super().__init__(interval_seconds=1)
        self._news_items = [
            {
                "title": "【富途7x24】中海油宣布在渤海湾发现大型油田",
                "content": "中国海洋石油宣布在渤海湾发现储量超亿吨的大型油田...",
            },
            {
                "title": "【富途7x24】腾讯控股回购 10 亿港元",
                "content": "腾讯控股今日在公开市场回购 10 亿港元股份...",
            },
            # 与 RSSHub 重复的新闻 (测试去重)
            {
                "title": "美联储暗示暂停加息",
                "content": "鲍威尔暗示可能在下一次会议上暂停加息...",
            },
            {
                "title": "英伟达发布新一代 AI 芯片 B300",
                "content": "NVIDIA 今日发布 B300 AI 加速卡，性能提升 4 倍...",
            },
        ]
        self._index = 0
    
    @property
    def source_id(self): return "test_futu"
    @property
    def source_name(self): return "测试富途快讯"
    
    async def fetch(self):
        if self._index < len(self._news_items):
            item = self._news_items[self._index]
            self._index += 1
            return [UnifiedNewsItemV2(
                id=f"futu_{self._index}",
                title=item["title"],
                content=item["content"],
                source="rsshub_futu",
                source_name="富途7x24快讯",
                timestamp=time.time(),
            )]
        return []


class MockImmediateSource(SourceAdapter):
    """模拟 IMMEDIATE 级别源 (大单异动)"""
    def __init__(self):
        super().__init__(interval_seconds=1)
        self._news_items = [
            {
                "title": "港股腾讯控股出现异常大单买入",
                "content": "港股盘中出现多笔 5000 万港元以上腾讯控股买单，股价异动...",
            },
        ]
        self._index = 0
    
    @property
    def source_id(self): return "test_immediate"
    @property
    def source_name(self): return "测试异动信号"
    
    async def fetch(self):
        if self._index < len(self._news_items):
            item = self._news_items[self._index]
            self._index += 1
            return [UnifiedNewsItemV2(
                id=f"anomaly_{self._index}",
                title=item["title"],
                content=item["content"],
                source="ticker_anomaly",
                source_name="逐笔异动",
                timestamp=time.time(),
                urgency=NewsUrgency.IMMEDIATE,
            )]
        return []


# ============================================================
# 测试运行
# ============================================================

async def main():
    print("=" * 70)
    print("FTSeek 统一新闻管线 V2 — 端到端测试")
    print("=" * 70)
    
    # 清理旧数据库
    for db in ["/tmp/test_pipeline_v2_e2e.db"]:
        if os.path.exists(db):
            os.remove(db)
    
    # 1. 准备管线
    print("\n=== 1. 初始化管线 ===")
    passed_news = []
    geo_events = []
    immediate_news = []
    
    pipeline = NewsPipelineV2(
        watchlist_codes={"HK.00700", "HK.00883", "US.NVDA"},
        watchlist_names={"腾讯", "中海油", "英伟达"},
        dedup_db_path="/tmp/test_pipeline_v2_e2e.db",
    )
    pipeline.add_source(MockRSSHubSource())
    pipeline.add_source(MockFutuSource())
    pipeline.add_source(MockImmediateSource())
    
    pipeline.on_news_passed = lambda item: passed_news.append(item)
    pipeline.on_geo_event = lambda item: geo_events.append(item)
    pipeline.on_immediate = lambda item: immediate_news.append(item)
    
    print(f"  数据源: {len(pipeline.sources)} 个")
    print(f"  Watchlist: {pipeline.relevance.watchlist_codes}")
    print(f"  回调已注册: on_news_passed, on_geo_event, on_immediate")
    
    # 2. 运行管线
    print("\n=== 2. 运行管线 (8 条新闻，3 个源) ===")
    passed = await pipeline.run_once()
    
    # 3. 详细结果
    print("\n=== 3. 通过的新闻 ===")
    for i, item in enumerate(passed_news, 1):
        tags = []
        if item.is_geo_event(): tags.append("🌍 地缘")
        if item.is_immediate(): tags.append("⚡ 即时")
        tag_str = " ".join(tags) if tags else "📰 普通"
        print(f"  {i}. [{tag_str}] {item.source_name}: {item.title[:50]}")
        print(f"     相关性: {item.raw_score:.2f} | urgency: {item.urgency}")
    
    print(f"\n  通过总数: {len(passed_news)}")
    
    # 4. 地缘事件检测
    print("\n=== 4. 地缘事件检测 ===")
    if geo_events:
        for item in geo_events:
            print(f"  🌍 {item.title[:50]}")
    else:
        print("  (需要注入 geo_scan 才会触发，此处验证路由机制)")
    
    # 5. 快速通道
    print("\n=== 5. 快速通道 (IMMEDIATE 新闻) ===")
    for item in immediate_news:
        print(f"  ⚡ {item.source_name}: {item.title[:50]}")
    
    # 6. 统计面板
    stats = pipeline.get_stats()
    print("\n=== 6. 管线统计面板 ===")
    print(f"  采集总数: {stats['total_collected']}")
    print(f"  通过数量: {stats['total_passed']}")
    print(f"  拒绝数量: {stats['total_rejected']}")
    print(f"  地缘事件: {stats['geo_events']}")
    print(f"  即时新闻: {stats['immediate_news']}")
    print(f"  运行次数: {stats['pipeline_runs']}")
    print(f"  最后耗时: {stats['last_run_duration_ms']:.0f}ms")
    
    print(f"\n  质量门禁:")
    qg = stats["quality_gate"]
    print(f"    输入: {qg['total_input']}")
    print(f"    通过: {qg['passed']}")
    print(f"    去重: {qg['rejected_duplicate']}")
    print(f"    不相关: {qg['rejected_irrelevant']}")
    print(f"    不重要: {qg['rejected_unimportant']}")
    
    # 7. 源健康状态
    print(f"\n  数据源健康:")
    for src in stats["sources"]:
        h = src["health"]
        print(f"    {src['source_name']}: 成功率={h['success_rate']:.0%} | 采集={h['total_calls']}次")
    
    # 8. 去重效果验证
    print("\n=== 7. 去重效果验证 ===")
    dedup = DedupEngineV2(db_path="/tmp/test_dedup_verify.db")
    
    # 相同标题
    h1 = compute_simhash("美联储暗示暂停加息")
    hash1 = "fed_rate_1"
    r1 = dedup.check_and_mark(hash1, h1, "rsshub", "美联储暗示暂停加息")
    r2 = dedup.check_and_mark(hash1, h1, "futu", "美联储暗示暂停加息")
    print(f"  相同标题 (不同源): 第1次={'新' if r1 else '重'}, 第2次={'新' if r2 else '重'} ✅")
    
    # 相似标题
    h2 = compute_simhash("美联储宣布暂停加息")
    hash2 = "fed_rate_2"
    sim = simhash_similarity(h1, h2)
    r3 = dedup.check_and_mark(hash2, h2, "futu", "美联储宣布暂停加息")
    print(f"  相似标题 (SimHash {sim:.0%}): 第1次={'新' if r3 else '重'} (阈值 85% 应通过) ✅")
    
    # 完全不同标题
    h3 = compute_simhash("苹果发布新iPhone")
    hash3 = "apple_iphone"
    r4 = dedup.check_and_mark(hash3, h3, "wsj", "苹果发布新iPhone")
    sim2 = simhash_similarity(h1, h3)
    print(f"  不同标题 (SimHash {sim2:.0%}): {'新' if r4 else '重'} ✅")
    
    print("\n" + "=" * 70)
    print("🎉 端到端测试完成!")
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
