# FTSeek 量化交易辅助系统改造方案

**版本**: v2.0  
**日期**: 2026-02-27  
**目标**: 零资金风险下验证策略有效性，模拟盘连续两个月胜率>60%

---

## 📋 需求对照表

| 需求 | 现有状态 | 改造方案 | 优先级 |
|------|----------|----------|--------|
| 富途 L2 行情 | ✅ 已有 | 优化订阅逻辑 | P0 |
| 模拟交易 | ✅ 已有 | 默认模拟环境 | P0 |
| 新闻情绪分析 | ❌ 缺失 | 新增新闻模块 | P0 |
| 5 大技术指标 | ✅ 已有 | 整合到融合引擎 | P0 |
| 信号融合 (60% 量化 +40% 新闻) | ❌ 缺失 | 新增融合引擎 | P0 |
| 钉钉三按钮 (确认/忽略/延期) | ⚠️ 两按钮 | 增加延期按钮 | P0 |
| 频率限制 (单股 3 次/日，总 20 次/日) | ❌ 缺失 | 新增限频器 | P0 |
| 仓位限制 (单股 20%, 单日 10 次) | ✅ 已有 | 配置对齐 | P0 |
| 信号 15 分钟失效 | ✅ 已有 | 配置调整 | P1 |
| 数据存储 (信号永久，新闻 90 天，日志 30 天) | ⚠️ 内存 | 新增 SQLite 存储 | P0 |
| 绩效统计 (周/月维度) | ⚠️ 基础 | 增强统计模块 | P0 |
| 回测报告 (可配置周期) | ✅ 已有 | 增加预设选项 | P1 |
| 自定义股票列表 | ✅ 已有 | 优化配置界面 | P1 |

---

## 🏗️ 系统架构

```
┌─────────────────────────────────────────────────────────────────┐
│                        FTSeek v2.0                               │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  数据采集层  │  │  信号生成层  │  │  交易执行层  │             │
│  ├─────────────┤  ├─────────────┤  ├─────────────┤             │
│  │ • 富途 L2   │  │ • 技术指标  │  │ • 钉钉推送  │             │
│  │ • 新闻抓取  │  │ • 情绪分析  │  │ • 人工确认  │             │
│  │ • 宏观数据  │  │ • 融合引擎  │  │ • 模拟下单  │             │
│  └─────────────┘  └─────────────┘  └─────────────┘             │
│         │                │                │                      │
│         └────────────────┼────────────────┘                      │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    数据存储层                            │    │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────┐ │    │
│  │  │ 信号记录  │  │ 交易记录  │  │ 新闻数据  │  │ 日志文件  │ │    │
│  │  │ (永久)   │  │ (永久)   │  │ (90 天)   │  │ (30 天)   │ │    │
│  │  └──────────┘  └──────────┘  └──────────┘  └──────────┘ │    │
│  └─────────────────────────────────────────────────────────┘    │
│                          │                                       │
│                          ▼                                       │
│  ┌─────────────────────────────────────────────────────────┐    │
│  │                    绩效统计层                            │    │
│  │  • 周度报告  • 月度报告  • 回测分析  • 胜率追踪         │    │
│  └─────────────────────────────────────────────────────────┘    │
└─────────────────────────────────────────────────────────────────┘
```

---

## 📁 新增文件结构

```
/opt/ftseek/FTSeek/
├── app/
│   ├── data/                    # 数据采集模块 [新增]
│   │   ├── __init__.py
│   │   ├── futu_data.py         # 富途行情数据
│   │   ├── news_fetcher.py      # 新闻抓取 [新增]
│   │   └── sentiment.py         # 情绪分析 [新增]
│   ├── signal/                  # 信号生成模块 [新增]
│   │   ├── __init__.py
│   │   ├── fusion_engine.py     # 信号融合引擎 [新增]
│   │   └── indicators.py        # 技术指标计算 (复用现有)
│   ├── storage/                 # 数据存储模块 [新增]
│   │   ├── __init__.py
│   │   ├── database.py          # SQLite 数据库 [新增]
│   │   └── cleanup.py           # 数据清理任务 [新增]
│   ├── performance/             # 绩效统计模块 [新增]
│   │   ├── __init__.py
│   │   ├── stats.py             # 统计计算 [新增]
│   │   └── report.py            # 报告生成 [新增]
│   ├── limiter/                 # 频率限制模块 [新增]
│   │   ├── __init__.py
│   │   └── rate_limiter.py      # 限频器 [新增]
│   ├── strategy/                # 策略模块 [改造]
│   │   └── confluence_v2.py     # 融合策略升级
│   ├── notifier/                # 通知模块 [改造]
│   │   └── dingtalk.py          # 增加延期按钮
│   └── config.py                # 配置升级
├── config/                      # 配置文件目录 [新增]
│   ├── watchlist.json           # 监控股票列表
│   ├── backtest_presets.json    # 回测预设配置
│   └── system_config.yaml       # 系统配置
├── scripts/                     # 运维脚本
│   ├── daily_report.py          # 日报生成 [新增]
│   └── cleanupOldData.sh        # 数据清理 [新增]
└── docs/
    └── UPGRADE_GUIDE.md         # 升级指南 [新增]
```

---

## 🔧 核心改造点

### 1. 新闻情绪分析模块

**数据源** (免费):
- 新浪财经 API
- 腾讯财经 API
- RSS  feeds (可选)

**情绪评分**:
```python
SentimentScore:
  - score: -1.0 ~ 1.0  (负向到正向)
  - confidence: 0.0 ~ 1.0
  - keywords: ["盈利", "增长", "风险", ...]
  - source: "sina" | "tencent" | "rss"
  - timestamp: datetime
```

### 2. 信号融合引擎

**融合公式**:
```
FinalScore = TechnicalScore × 0.6 + SentimentScore × 0.4
Confidence = min(1.0, FinalScore × weight_factor)
```

**信号等级**:
| 置信度 | 等级 | 操作建议 |
|--------|------|----------|
| >0.8 | 强烈推荐 | 高优先级推送 |
| 0.6-0.8 | 推荐 | 正常推送 |
| 0.4-0.6 | 中性 | 仅记录不推送 |
| <0.4 | 弱信号 | 忽略 |

### 3. 钉钉推送升级

**三按钮设计**:
```json
{
  "btns": [
    {"title": "✅ 确认执行", "actionURL": "/api/confirm"},
    {"title": "⏰ 延期 15 分钟", "actionURL": "/api/defer"},
    {"title": "❌ 忽略", "actionURL": "/api/reject"}
  ]
}
```

### 4. 频率限制器

```python
RateLimiter:
  - per_stock_daily_limit: 3      # 单股票每日最多 3 次
  - global_daily_limit: 20        # 全局每日最多 20 次
  - window_reset: "00:00:00"      # 每日零点重置
```

### 5. SQLite 数据存储

**表结构**:
```sql
-- 信号记录 (永久)
CREATE TABLE signals (
    id TEXT PRIMARY KEY,
    code TEXT,
    action TEXT,
    price REAL,
    quantity INTEGER,
    strategy TEXT,
    technical_score REAL,
    sentiment_score REAL,
    confidence REAL,
    created_at DATETIME,
    expire_at DATETIME,
    confirmed_at DATETIME,
    executed_at DATETIME,
    status TEXT
);

-- 交易记录 (永久)
CREATE TABLE trades (
    id TEXT PRIMARY KEY,
    signal_id TEXT,
    code TEXT,
    action TEXT,
    entry_price REAL,
    exit_price REAL,
    quantity INTEGER,
    pnl REAL,
    pnl_percent REAL,
    entry_time DATETIME,
    exit_time DATETIME,
    strategy TEXT
);

-- 新闻数据 (90 天)
CREATE TABLE news (
    id TEXT PRIMARY KEY,
    code TEXT,
    title TEXT,
    content TEXT,
    sentiment_score REAL,
    source TEXT,
    url TEXT,
    published_at DATETIME,
    fetched_at DATETIME
);

-- 绩效快照 (永久)
CREATE TABLE performance_snapshots (
    id TEXT PRIMARY KEY,
    period TEXT,  -- "weekly" | "monthly"
    start_date DATE,
    end_date DATE,
    total_trades INTEGER,
    win_rate REAL,
    profit_factor REAL,
    max_drawdown REAL,
    sharpe_ratio REAL,
    created_at DATETIME
);
```

### 6. 绩效统计模块

**周度/月度指标**:
```python
PerformanceMetrics:
  # 胜率指标
  - signal_win_rate: 信号胜率
  - execution_win_rate: 执行胜率
  - profit_factor: 盈亏比
  
  # 风险指标
  - max_drawdown: 最大回撤
  - sharpe_ratio: 夏普比率
  - sortino_ratio: 索提诺比率
  
  # 交易统计
  - total_trades: 总交易数
  - winning_trades: 盈利交易
  - losing_trades: 亏损交易
  - avg_win: 平均盈利
  - avg_loss: 平均亏损
  
  # 目标追踪
  - target_win_rate: 60% 目标
  - current_streak: 当前连续月数
  - months_above_target: 连续达标月数
```

---

## 📊 胜率 60% 目标实现路径

### 阶段一：基础验证 (第 1-2 周)
- [ ] 完成新闻情绪分析模块
- [ ] 完成信号融合引擎
- [ ] 完成 SQLite 存储
- [ ] 完成钉钉三按钮
- [ ] 完成频率限制器

### 阶段二：策略优化 (第 3-4 周)
- [ ] 回测历史数据 (3 个月/6 个月/1 年)
- [ ] 参数优化 (网格搜索/随机搜索)
- [ ] 融合权重调优 (60/40 → 动态调整)
- [ ] 建立基准策略对比

### 阶段三：模拟盘验证 (第 5-12 周)
- [ ] 每日信号记录
- [ ] 每周绩效报告
- [ ] 每月胜率统计
- [ ] 连续两月>60% 目标验证

### 阶段四：持续改进 (第 13 周+)
- [ ] 根据实盘反馈调整
- [ ] 增加新数据源
- [ ] 优化情绪分析模型
- [ ] 扩展策略库

---

## 🔐 安全与风控

| 限制项 | 配置值 | 说明 |
|--------|--------|------|
| 交易环境 | 模拟盘 | 零资金风险 |
| 单股仓位 | ≤20% | 分散风险 |
| 单日交易 | ≤10 次 | 防止过度交易 |
| 单股日信号 | ≤3 次 | 防止重复信号 |
| 全局日信号 | ≤20 次 | 总体频率控制 |
| 信号有效期 | 15 分钟 | 过期自动失效 |
| 人工确认 | 必须 | 所有订单需确认 |

---

## 📈 监控与告警

**钉钉通知类型**:
1. 交易信号推送 (带三按钮)
2. 交易执行结果
3. 频率限制警告
4. 系统异常告警
5. 日报/周报/月报

**绩效告警阈值**:
- 胜率<50% 持续 5 天 → 警告
- 回撤>10% → 警告
- 连续亏损>5 次 → 警告

---

## 🚀 部署步骤

1. **备份现有配置**
   ```bash
   cp -r /opt/ftseek/FTSeek /opt/ftseek/FTSeek.backup
   ```

2. **安装新增依赖**
   ```bash
   pip install aiohttp beautifulsoup4 lxml
   ```

3. **运行数据库初始化**
   ```bash
   python -m app.storage.database --init
   ```

4. **配置监控股票列表**
   ```bash
   vim config/watchlist.json
   ```

5. **启动系统**
   ```bash
   ./run.sh
   ```

---

## 📝 验收标准

| 验收项 | 标准 | 验证方法 |
|--------|------|----------|
| 数据采集延迟 | <5 秒 | 日志时间戳对比 |
| 信号融合 | 60% 技术 +40% 情绪 | 检查信号记录 |
| 钉钉推送 | 三按钮可用 | 手动测试 |
| 频率限制 | 单股 3 次/日 | 触发测试 |
| 数据存储 | 信号/交易永久 | 查询数据库 |
| 绩效统计 | 周/月报告 | 查看报告 |
| 回测配置 | 3/6/12 月可选 | UI 测试 |

---

**下一步**: 开始实施阶段一开发任务
