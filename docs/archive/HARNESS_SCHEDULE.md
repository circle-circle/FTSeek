# FTSeek Harness 三阶段巡检调度

> 盘前预检 → 午间诊断 → 盘后审计 的持续迭代闭环

## 调度时间表

```
交易日时间线 (CST)
────────────────────────────────────────────────────────────────────
08:30  阶段1: 盘前预检 v2
       └─ 系统状态检查 + OpenD连接 + 配置漂移 + 根因诊断
       └─ P0问题自动修复(Phase 2) + 就绪状态评估
       
09:30  港股开盘 ──────────────────────────────┐
                                            │
12:00  阶段2: 午间休市检查                    │ 上 午
       └─ 上午交易执行审计                    │ 交
       └─ 上午日志问题分析                    │ 易
       └─ 暴露问题根因诊断                    │
       └─ 下午交易建议                        │
                                            │
13:00  港股下午开盘 ─────────────────────────┘
                                            │
16:00  港股收盘                              │ 下 午
                                            │ 交
16:30  阶段3: 盘后审计 v2                    │ 易
       └─ 全天交易统计 + 盈亏分析            │
       └─ 全天日志审计                       │
       └─ 完整诊断报告                       │
       └─ 趋势分析报告                       │
       └─ 次日交易建议                       │
                                            │
22:00  深度分析 (每天) ──────────────────────┘
       └─ 6维度深度分析 + 诊断 + 趋势
       └─ 代码质量 + 性能 + 可靠性 + 配置 + 回归 + 安全

非交易日
       所有脚本自动跳过，仅记录"非交易日"状态
```

## 三阶段差异化设计

### 阶段1: 盘前预检 v2 (08:30)

**目标**: 确保系统准备好开盘

**检查项**:
- ✅ 服务状态 (systemd + 进程 + 健康端点)
- ✅ OpenD 连接 (进程 + 端口 + 重连频率)
- ✅ 配置一致性 (systemd/.env/yaml 三向对比)
- ✅ 系统资源 (内存 + 磁盘)
- ✅ Harness 诊断 (匹配已知问题模式)

**输出**: 就绪状态评估 (就绪/基本就绪/未就绪)

**关键特征**: 快速(5秒内完成)，只检查不深入分析

---

### 阶段2: 午间休市检查 (12:00)

**目标**: 利用午休时间，诊断上午暴露的问题

**检查项**:
- ✅ 上午交易执行统计 (交易笔数 + 最近交易)
- ✅ 信号质量分析 (信号数 + 策略分布)
- ✅ 上午日志分析 (错误模式 + 策略耗时 + 重连频率)
- ✅ Harness 诊断 (基于上午日志的根因定位)
- ✅ 下午交易建议 (基于诊断结果)

**输出**: 上午交易审计 + 问题诊断 + 下午建议

**关键特征**: 基于实际交易数据，不是空跑检查

---

### 阶段3: 盘后审计 v2 (16:30)

**目标**: 全天综合审计，生成次日建议

**检查项**:
- ✅ 全天交易统计 (盈亏 + 执行 + 决策)
- ✅ 全天日志审计 (错误模式 + 性能分析)
- ✅ Harness 完整诊断 (7条诊断规则匹配)
- ✅ 趋势分析 (对比历史数据)
- ✅ 次日交易建议

**输出**: 全天综合报告 + 次日建议 + 历史趋势对比

**关键特征**: 最全面的分析，包含趋势预测

---

### 附加: 深度分析 (22:00)

**目标**: 6维度深度扫描 + 完整Harness分析

**检查项**:
- ✅ 代码质量 (长函数 + 未使用import + TODO/FIXME)
- ✅ 性能分析 (策略耗时 P50/P95 + 内存趋势)
- ✅ 可靠性分析 (错误模式 + 服务稳定性 + DB健康)
- ✅ 配置一致性 (深度交叉对比)
- ✅ 历史回归 (已知问题复发检测)
- ✅ 安全审计 (敏感信息 + 文件权限)
- ✅ Harness 诊断 + 趋势分析

**输出**: 完整深度报告

**关键特征**: 最重度的分析，适合非交易时段运行

---

## 持续迭代闭环

```
上午暴露问题
      ↓
午间检查诊断 → 生成下午建议 → 下午验证效果
      ↓
盘后审计确认 → 次日建议 → 盘前预检验证
      ↓
深度分析汇总 → 趋势更新 → 技能库进化
      ↓
下一轮循环...
```

## Crontab 配置

```bash
# 阶段1: 盘前预检 (08:30 交易日)
30 8 * * 1-5 cd /opt/ftseek/FTSeek && python3.8 scripts/auto_audit/pre_market_check_v2.py >> logs/audit/pre_market_cron.log 2>&1

# 阶段2: 午间检查 (12:00 交易日)
0 12 * * 1-5 cd /opt/ftseek/FTSeek && python3.8 scripts/auto_audit/midday_check.py >> logs/audit/midday_cron.log 2>&1

# 阶段3: 盘后审计 (16:30 交易日)
30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3.8 scripts/auto_audit/post_market_audit_v2.py >> logs/audit/post_market_cron.log 2>&1

# 深度分析 (22:00 每天)
0 22 * * * cd /opt/ftseek/FTSeek && python3.8 scripts/auto_audit/deep_analysis.py >> logs/audit/deep_analysis_cron.log 2>&1
```

## 报告输出路径

```
logs/audit/
├── pre_market_check_YYYY-MM-DD.json      # 盘前预检
├── midday_check_YYYY-MM-DD.json          # 午间检查
├── post_market_audit_YYYY-MM-DD.json     # 盘后审计
├── deep_analysis_YYYY-MM-DD_HHMM.json    # 深度分析
├── config_drift_YYYY-MM-DD_HHMM.json     # 配置漂移
├── pre_market_cron.log                   # 盘前 cron 日志
├── midday_cron.log                       # 午间 cron 日志
├── post_market_cron.log                  # 盘后 cron 日志
└── deep_analysis_cron.log                # 深度 cron 日志

data/
├── diagnosis_results.json                # 诊断历史
├── trend_analysis_history.json           # 趋势历史
├── audit_history.json                    # 审计历史
└── deep_analysis_history.json            # 深度分析历史
```

## 文件清单

```
scripts/auto_audit/
├── utils.py                              # 公共工具
├── diagnosis_rules.yaml                  # 诊断规则库 (配置)
├── diagnosis_engine.py                   # 诊断引擎
├── trend_analyzer.py                     # 趋势分析器
├── pre_market_check_v2.py                # 盘前预检 v2 (Harness)
├── midday_check.py                       # 午间检查 (新增)
├── post_market_audit_v2.py              # 盘后审计 v2 (Harness)
├── deep_analysis.py                      # 深度分析 (已增强)
├── pre_market_check.py                   # 旧版 (保留兼容)
├── post_market_audit.py                  # 旧版 (保留兼容)
├── config_drift_detector.py              # 配置漂移检测
└── run_all_audits.py                     # 一键运行器
```

## 实际运行示例 (2026-04-14)

### 盘前预检输出
```
盘前就绪状态评估
  ⚠️  基本就绪 - 有 2 个警告建议关注
  
  诊断发现:
  - [P1] strategy_timeout: LLM同步调用阻塞策略计算 (置信度: 80%)
  - [P1] zero_trade: 置信度阈值过高 (置信度: 70%)
```

### 午间检查输出
```
上午交易统计: 46 笔交易, 29 次信号
上午错误: ERROR=0, WARNING=1261
策略耗时: 平均=230ms, 最大=843ms

下午交易建议:
1. ℹ️ 上午交易频繁，注意下午的仓位管理
```

### 盘后审计输出
```
全天交易: 46 笔, 总PnL=0.00
诊断发现:
- [P1] strategy_timeout: LLM同步调用阻塞策略计算 (80%)
- [P1] zero_trade: 置信度阈值过高 (70%)

趋势分析:
- strategy_time_ms: 逐渐改善 ↓ (224→133ms, -40.7%)
- error_frequency: 稳定 (0次)

次日建议:
1. ✅ 系统运行正常，次日继续保持
```
