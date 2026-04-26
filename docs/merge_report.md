# FTSeek 统一新闻管线合并报告

**日期**: 2026-04-17  
**状态**: ✅ 已完成  
**合并范围**: `futu_flash_cron.py` + `rsshub_multi_source_cron.py`

---

## 📊 合并前后对比

| 维度 | 合并前 | 合并后 |
|------|--------|--------|
| **采集脚本** | 2 个 (futu_flash + rsshub_multi_source) | 1 个 (rsshub_multi_source) |
| **数据源** | 富途 (独立) + 6 源 (RSSHub) | 6 源统一 (含富途) |
| **crontab 任务** | 2 个 | 1 个 |
| **健康检查** | 2 个独立检查 | 1 个统一检查 |
| **告警数量** | 2 倍 (重复) | 1 倍 (统一) |
| **推送逻辑** | futu_flash 独立推送 | 统一推送引擎 |
| **去重系统** | 2 套独立去重 | 1 套全局去重 |
| **质量评分** | futu_flash 无评分 | 规则+LLM 统一评分 |

---

## 🏗️ 架构变更

### 新增文件

| 文件 | 功能 |
|------|------|
| `scripts/unified_news_push.py` | 统一新闻推送引擎 (合并自 futu_flash) |

### 修改文件

| 文件 | 修改内容 |
|------|---------|
| `scripts/rsshub_multi_source_cron.py` | 集成推送引擎、统一统计 |
| `scripts/health_guardian.py` | 合并健康检查、提高错误阈值 |
| `crontab` | 移除 futu_flash 独立任务 |

### 保留文件 (兼容)

| 文件 | 状态 |
|------|------|
| `scripts/futu_flash_cron.py` | 保留但不运行 (可手动执行) |
| `data/futu_flash/` | 保留历史数据 |

---

## 🔧 统一推送引擎功能

### 核心功能 (继承自 futu_flash)

1. **重要新闻判断**: 基于关键词匹配 (财报、业绩、监管、宏观等)
2. **股票关联提取**: 通过名称映射和 watchlist 代码匹配
3. **情感分析**: 正面/负面/中性三分类
4. **钉钉推送**: 带全局去重，最多 5 条/次
5. **事件驱动注入**: 调用 FTSeek API 注入新闻事件

### 新增功能

1. **多源支持**: 支持所有 RSSHub 源的推送
2. **统一去重**: 跨源全局去重 (2 小时 TTL)
3. **质量评分集成**: 与 LLM 评分联动
4. **推送统计持久化**: `data/unified_push_stats.json`

---

## 📋 Crontab 变更

### 合并前

```bash
# futu_flash 独立任务 (已移除)
*/3 9-16 * * 1-5 flock -n /tmp/futu_flash_cron.lock -c "cd /opt/ftseek/FTSeek && python3 scripts/futu_flash_cron.py"

# RSSHub 多源任务 (已移除)
*/3 9-16 * * 1-5 flock -n /tmp/rsshub_multi_source_cron.lock -c "cd /opt/ftseek/FTSeek && python3 scripts/rsshub_multi_source_cron.py"
```

### 合并后

```bash
# 统一新闻采集任务
*/3 9-16 * * 1-5 flock -n /tmp/rsshub_unified.lock -c "cd /opt/ftseek/FTSeek && python3 scripts/rsshub_multi_source_cron.py"
```

---

## 🩺 健康检查变更

### RSSHub 检查优化

| 项目 | 旧阈值 | 新阈值 | 说明 |
|------|--------|--------|------|
| 缓存过期 (critical) | 12h | 12h | 不变 |
| 缓存过期 (warning) | 6h | 6h | 不变 |
| 错误数 (critical) | >20 | >15 | 降低阈值 |
| 错误数 (warning) | >5 | >8 | 提高阈值，减少误报 |

### futu_flash 检查

- **标记为已弃用**: `check_futu_flash_health()` 现在调用 `check_rsshub_health()`
- **保留兼容**: 避免其他模块调用失败

---

## ✅ 验证结果

| 测试项 | 结果 |
|--------|------|
| 推送引擎初始化 | ✅ 成功 |
| 重要性判断 | ✅ 财报=重要，普通=不重要 |
| 股票提取 | ✅ 腾讯→HK.00700, 阿里→HK.09988 |
| 情感分析 | ✅ 正面新闻识别正确 |
| RSSHub 集成 | ✅ 推送引擎和缓存管理器均已集成 |
| 健康检查 | ✅ RSSHub 和 futu_flash 检查一致 |
| crontab 配置 | ✅ 统一任务已配置 |

---

## 📈 预期效果

1. **资源节省**: 减少 50% 采集任务 (富途不再重复采集)
2. **告警减少**: 从 2 个独立告警 → 1 个统一告警
3. **去重优化**: 跨源统一去重，避免重复推送
4. **质量提升**: 所有新闻经过同一质量评分标准
5. **维护简化**: 单一 crontab，单一健康检查

---

## 🔄 回滚方案

如需回滚到合并前状态:

```bash
# 1. 恢复旧 crontab
crontab /opt/ftseek/FTSeek/config/ftseek_crontab.txt

# 2. 重启服务 (如需要)
sudo systemctl restart ftseek

# 3. 删除统一推送引擎 (可选)
rm /opt/ftseek/FTSeek/scripts/unified_news_push.py
```

---

## 📝 后续优化建议

1. **废弃 futu_flash_cron.py**: 当确认合并稳定后，可完全移除
2. **统一数据目录**: 将 `data/futu_flash/` 合并到 `data/rsshub_multi_source/`
3. **推送策略优化**: 基于 LLM 评分的差异化推送 (S 级立即推送，A 级批量推送)
4. **推送统计面板**: 可视化推送成功率、去重率等指标
