# 新闻价值衰减模型 (News Value Decay Model)

## 核心理念

新闻缓存不应按简单时间过期，而应基于：
1. **交易时段** (盘前/盘中/盘后) - 决定新闻的"消化窗口"
2. **信息质量评分** - 决定新闻的"保留优先级"
3. **市场影响度** - 决定新闻的"衰减速度"

## 质量评分模型 (0-100 分)

| 维度 | 权重 | 评分标准 |
|------|------|----------|
| 时效性 | 25 分 | <1h=25, <3h=20, <6h=15, <12h=10, >12h=5 |
| 市场影响度 | 25 分 | 财报/并购/监管=25, 利率/CPI/政策=18, 分析/评级=10 |
| 相关度 | 25 分 | 含 watchlist 股票=25, 宏观指标=18, 无关=10 |
| 来源可信度 | 15 分 | 富途/彭博=15, 金十=13, 华尔街见闻=12/11, 财联社=10 |
| 信息独特性 | 10 分 | 内容>300 字=10, >150 字=8, >50 字=6, <50 字=4 |

### 质量等级

| 等级 | 分数范围 | 说明 |
|------|----------|------|
| S | ≥80 | 重大新闻（财报、并购、监管） |
| A | 65-79 | 重要新闻（政策、利率、宏观） |
| B | 50-64 | 一般新闻（分析、评级、预测） |
| C | 35-49 | 低价值新闻 |
| D | <35 | 垃圾信息 |

## 交易时段保留策略

### 港股交易时段

| 时段 | 时间 | 说明 |
|------|------|------|
| 盘前竞价 | 09:00-09:30 | 盘前准备 |
| 上午交易 | 09:30-12:00 | 正常交易 |
| 午间休市 | 12:00-13:00 | 休市 |
| 下午交易 | 13:00-16:00 | 正常交易 |
| 盘后竞价 | 16:00-16:10 | 盘后准备 |

### TTL 矩阵 (保留时长)

| 交易时段 | S 级 | A 级 | B 级 | C 级 | D 级 |
|----------|------|------|------|------|------|
| 盘前 (09:00-09:30) | 4h | 3h | 2h | 30min | 15min |
| 上午 (09:30-12:00) | 3h | 2h | 1h | 30min | 15min |
| 午间 (12:00-13:00) | 4h | 3h | 2h | 30min | 15min |
| 下午 (13:00-16:00) | 3h | 2h | 1h | 30min | 15min |
| 盘后 (16:00-16:10) | 16h | 14h | 12h | 2h | 30min |
| 非交易时段 | 16h | 14h | 12h | 2h | 30min |

### 消化窗口

- **定义**: 开盘后 2 小时内为新闻消化窗口
- **作用**: 此期间的重要新闻会被优先展示给交易策略参考
- **判断**: `is_digest_window()` 方法返回 true/false

## 智能缓存管理流程

```
[新闻采集] → [质量评分] → [标签过期时间] → [合并去重] → [清理过期] → [优先级排序] → [保存]
```

### 1. 质量评分
- 对每条新闻进行 5 维度评分
- 计算总分并分配等级 (S/A/B/C/D)

### 2. 标签过期时间
- 根据当前交易时段和新闻等级计算 TTL
- 设置 `_expires_at` 时间戳

### 3. 清理过期
- 检查每条新闻的 `_expires_at`
- 移除已过期的新闻
- 更新元数据

### 4. 优先级排序
- 排序公式：`(等级权重 + 质量分数) × 时间衰减`
- 等级权重：S=1000, A=800, B=600, C=400, D=200
- 时间衰减：`max(0.1, 1.0 - 年龄小时/24)`

## 使用示例

```python
from scripts.news_value_decay import SmartNewsCacheManager

# 初始化缓存管理器
cache_manager = SmartNewsCacheManager("/opt/ftseek/FTSeek/data/rsshub_multi_source")

# 对新闻进行评分
watchlist = {"HK.00700", "HK.09988"}
scored_news = cache_manager.score_and_tag_news(news_list, watchlist)

# 清理过期新闻
kept_news, removed_count = cache_manager.cleanup_expired(all_news)

# 按优先级排序
sorted_news = cache_manager.get_priority_sorted(kept_news)

# 获取缓存统计
stats = cache_manager.get_cache_stats(sorted_news)
print(f"S 级新闻: {stats['by_grade']['S']}")
print(f"平均分数: {stats['avg_score']:.1f}")
print(f"消化窗口内: {stats['digest_window_count']}")
```

## 缓存元数据

保存在 `data/rsshub_multi_source/cache_metadata.json`:

```json
{
  "news_items": {
    "news_id_1": {
      "grade": "S",
      "score": 85.0,
      "session": "pre_market",
      "expires_at": "2026-04-18T12:00:00",
      "created_at": "2026-04-17T09:15:00"
    }
  },
  "last_cleanup": "2026-04-17T10:00:00"
}
```

## 集成到 RSSHub 采集器

已在 `scripts/rsshub_multi_source_cron.py` 中集成：

1. **导入模块**: `from scripts.news_value_decay import SmartNewsCacheManager`
2. **初始化**: `self.cache_manager = SmartNewsCacheManager(str(DATA_DIR))`
3. **保存时评分**: `scored_news = self.cache_manager.score_and_tag_news(news_list, watchlist)`
4. **清理过期**: `unique_data, removed_count = self.cache_manager.cleanup_expired(unique_data)`
5. **优先级排序**: `unique_data = self.cache_manager.get_priority_sorted(unique_data)`

## 日志输出示例

```
💾 保存 137 条快讯到 /opt/ftseek/FTSeek/data/rsshub_multi_source/multi_source_20260417.json
   📊 缓存统计：S 级=3 A 级=2 B 级=5
   ⏱️ 平均分数: 5.1 | 消化窗口内: 0
```

## 优势

1. **智能保留**: 重要新闻保留更久，低价值新闻快速清理
2. **交易感知**: 盘前/盘后新闻保留至开盘后消化完成
3. **优先级排序**: 高质量新闻始终排在前面
4. **自动清理**: 基于质量和时段的动态过期机制
5. **可观测性**: 详细的缓存统计和质量分布

## 后续优化方向

1. **LLM 辅助评分**: 使用 LLM 对新闻影响度进行更精准的评估
2. **用户反馈学习**: 根据交易策略对新闻的点击/使用情况调整权重
3. **板块联动**: 识别新闻影响的板块，保留相关板块的所有新闻
4. **情绪衰减**: 结合市场情绪指标调整新闻保留时长
