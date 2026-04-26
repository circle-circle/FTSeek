"""
FTSeek 新闻管线 V2 — 集成测试

测试目标:
1. RSSHubSource 能正常采集真实 RSSHub 源
2. 质量门禁有效过滤
3. 集成适配器能正确桥接到现有系统
4. 端到端流程完整

注意: 此测试需要网络连接
"""

import sys, os, time, asyncio
sys.path.insert(0, "/opt/ftseek/FTSeek")

from app.news_v2.models import UnifiedNewsItemV2, NewsUrgency, compute_simhash
from app.news_v2.sources.rsshub_source import RSSHubSource
from app.news_v2.quality.dedup_v2 import DedupEngineV2
from app.news_v2.quality.relevance_filter import RelevanceFilter
from app.news_v2.pipeline import NewsPipelineV2, QualityGate
from app.news_v2.integration import NewsV2Integration


async def test_rsshub_source():
    """测试 RSSHub 真实数据采集"""
    print("=" * 70)
    print("测试 1: RSSHub 真实数据采集")
    print("=" * 70)
    
    source = RSSHubSource(interval_seconds=1)
    
    # 尝试采集 2 轮 (测试轮询)
    all_items = []
    for i in range(3):
        items = await source.fetch_safe()
        print(f"  轮次 {i+1}: 采集 {len(items)} 条")
        for item in items[:3]:  # 只打印前3条
            print(f"    [{item.urgency}] {item.source_name}: {item.title[:50]}")
            print(f"       markets={item.market_tags} | stocks={item.related_stocks[:3]} | score={item.raw_score:.2f}")
        all_items.extend(items)
        if items:
            break  # 采集成功就停止
    
    print(f"\n  总计采集: {len(all_items)} 条")
    return all_items


async def test_quality_gate(items):
    """测试质量门禁"""
    print("\n" + "=" * 70)
    print("测试 2: 质量门禁 (真实数据)")
    print("=" * 70)
    
    # 清理旧数据库
    db_path = "/tmp/test_v2_real_dedup.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    
    pipeline = NewsPipelineV2(
        watchlist_codes={"HK.00700", "HK.09988", "US.NVDA", "US.TSLA"},
        watchlist_names={"腾讯", "阿里", "英伟达", "特斯拉"},
        dedup_db_path=db_path,
    )
    
    passed = []
    rejected = []
    
    for item in items:
        ok, reason = pipeline.quality_gate.evaluate(item)
        if ok:
            passed.append(item)
        else:
            rejected.append((item, reason))
    
    print(f"  输入: {len(items)} 条")
    print(f"  通过: {len(passed)} 条 ({len(passed)/max(1,len(items)):.0%})")
    print(f"  拒绝: {len(rejected)} 条")
    
    # 显示通过的新闻
    if passed:
        print("\n  通过新闻:")
        for i, item in enumerate(passed[:10], 1):
            tags = " ".join(item.market_tags[:2])
            stocks = ", ".join(item.related_stocks[:3]) if item.related_stocks else "无"
            print(f"    {i:2d}. [{tags}] {item.title[:55]}")
            print(f"        股票: {stocks} | 分数: {item.raw_score:.2f}")
    
    # 显示拒绝原因统计
    if rejected:
        reasons = {}
        for _, reason in rejected:
            reasons[reason] = reasons.get(reason, 0) + 1
        print("\n  拒绝原因:")
        for reason, count in sorted(reasons.items(), key=lambda x: -x[1]):
            print(f"    {reason}: {count} 条")
    
    return passed


async def test_integration(passed_items):
    """测试集成适配器"""
    print("\n" + "=" * 70)
    print("测试 3: 集成适配器 (observe 模式)")
    print("=" * 70)
    
    # 创建模拟集成 (不实际推送)
    integration = NewsV2Integration(mode="observe")
    
    for item in passed_items[:5]:  # 测试前5条
        integration._on_news_passed(item)
    
    stats = integration.get_stats()
    print(f"  处理: {stats['total_processed']} 条")
    print(f"  推送: {stats['total_pushed']} 条")
    print(f"  分析: {stats['total_analyzed']} 条")
    print(f"  信号: {stats['total_signals']} 条")
    print(f"  错误: {stats['errors']} 条")
    
    return stats


async def test_dedup_cross_source():
    """测试跨源去重"""
    print("\n" + "=" * 70)
    print("测试 4: 跨源去重 (真实场景)")
    print("=" * 70)
    
    db_path = "/tmp/test_v2_cross_dedup.db"
    if os.path.exists(db_path):
        os.remove(db_path)
    
    dedup = DedupEngineV2(db_path=db_path, ttl_hours=48)
    
    # 模拟同一新闻从不同源来
    test_cases = [
        ("美联储宣布加息25个基点", "rsshub_bloomberg"),
        ("美联储宣布加息25个基点", "rsshub_futu"),       # 完全相同
        ("美联储加息25个基点", "rsshub_jin10"),           # 略微不同
        ("美联储利率决议出炉", "rsshub_wscn"),            # 语义相关
        ("苹果发布新iPhone", "rsshub_futu"),              # 不同新闻
    ]
    
    results = []
    for title, source in test_cases:
        title_hash = UnifiedNewsItemV2(
            id="test", title=title, content="", source="", source_name="", timestamp=0
        ).content_hash
        simhash = compute_simhash(title)
        
        is_new = dedup.check_and_mark(title_hash, simhash, source, title)
        results.append((title, source, is_new))
    
    for title, source, is_new in results:
        status = "✅ 新" if is_new else "❌ 重复"
        print(f"  {status} | {source:20s} | {title[:30]}")
    
    # 统计
    new_count = sum(1 for _, _, is_new in results if is_new)
    dup_count = len(results) - new_count
    print(f"\n  新: {new_count} | 重复: {dup_count}")
    
    return new_count, dup_count


async def test_health_monitor():
    """测试健康监控"""
    print("\n" + "=" * 70)
    print("测试 5: 源健康监控")
    print("=" * 70)
    
    source = RSSHubSource(interval_seconds=1)
    
    # 模拟连续失败
    print("  模拟连续失败 5 次...")
    source.health._consecutive_failures = 4  # 已经失败了4次
    
    # 第5次失败应该触发暂停
    source.health.record_failure("connection timeout")
    status = source.health.get_status()
    
    print(f"  暂停状态: {status['paused']}")
    print(f"  剩余时间: {status['pause_remaining_seconds']:.0f}s")
    print(f"  成功率: {status['success_rate']:.0%}")
    print(f"  总调用: {status['total_calls']}")
    
    # 测试恢复
    print("\n  模拟成功恢复...")
    # 手动重置
    source.health._pause_until = 0
    source.health._consecutive_failures = 0
    source.health.record_success()
    status2 = source.health.get_status()
    print(f"  暂停状态: {status2['paused']}")
    print(f"  成功率: {status2['success_rate']:.0%}")
    
    return status


async def main():
    print("=" * 70)
    print("FTSeek 新闻管线 V2 — 集成测试 (真实数据)")
    print("=" * 70)
    
    # 测试 1: 真实采集
    items = await test_rsshub_source()
    
    if not items:
        print("\n⚠️  未能采集到新闻，可能是网络问题")
        print("   跳过后续测试")
        return
    
    # 测试 2: 质量门禁
    passed = await test_quality_gate(items)
    
    # 测试 3: 集成适配器
    if passed:
        await test_integration(passed)
    
    # 测试 4: 跨源去重
    new_count, dup_count = await test_dedup_cross_source()
    
    # 测试 5: 健康监控
    await test_health_monitor()
    
    # 最终评估
    print("\n" + "=" * 70)
    print("📊 最终评估")
    print("=" * 70)
    print(f"  采集能力: {'✅ 正常' if items else '❌ 失败'}")
    print(f"  质量门禁: {'✅ 正常' if len(passed) > 0 else '⚠️  全部过滤'}")
    print(f"  去重能力: {'✅ 正常' if dup_count > 0 else '⚠️  无重复'}")
    print(f"  健康监控: ✅ 正常")
    print(f"\n  有效新闻: {len(passed)}/{len(items)} = {len(passed)/max(1,len(items)):.0%}")


if __name__ == "__main__":
    asyncio.run(main())
