# ADR-017: Universe-Scan clue brief 注入结构化硬事件 (复用 CatalystClassifier)

- 状态: Accepted
- 日期: 2026-07-14
- 关联: ADR-016 (live prompt V2), `app/catalyst/event_classifier.py` (CatalystClassifier)

## 背景

ADR-016 把 live scan 切到 V2 后, scan-eval / 实盘扫描的 tier 分布长期是
`HARD_EVENT 0 / MARKET_CORROBORATED N / THESIS_ONLY 0`。

根因: [`_build_clue_brief`](../../scripts/llm_universe_scan.py) 只喂三样 —— 人工线索、
板块轮动/异动、**原始新闻头条**。头条是未分类的裸标题, 而 V2 的 `HARD_EVENT` tier 要求
"因果链第一环锚定 evidence_ref 里的**具体事件**(封锁/战争/财报/监管/事故)"。输入里没有
结构化事件可引用 → LLM 只能落到 `MARKET_CORROBORATED`。**不是 prompt 问题, 是输入缺结构化事件。**

系统内已有成熟事件分类能力 `CatalystClassifier` (关键词→event_type/severity/tickers,
O(n), 可 `use_llm=False` 零成本), 但未接入这条扫描线。

## 决策

在 `_build_clue_brief` 增加一个 **config 驱动**的 `[硬事件]` 段:

- 新增 `_gather_hard_events(news)`: 把 scan 已拉取的新闻 (RSSHub, `_gather_news`)
  经轻量 shim → `UnifiedNewsItemV2` → `CatalystClassifier().classify(use_llm=False)`,
  取结构化硬事件, 按 severity(CRITICAL>HIGH>MEDIUM) 排序, 格式化为
  `[SEVERITY/EVENT_TYPE] headline → 关联: TICKERS`。
- 该段插在市场佐证之上 (传导优先级最高)。
- 开关 `universe_expansion.clue_hard_events`; 代码默认 `false` (安全回退), yaml 显式 `true`。
- prompt 不改 —— V2 已定义 HARD_EVENT tier, 喂进可引用的结构化事件后 LLM 自然自评。
- 复用来源: 不新建事件检测, 完全复用 `CatalystClassifier` 关键词库; 分类器/新闻拉取
  任一失败均降级返回 `[]`, 不影响原有 clue brief。

## 影响范围

- 仅**丰富 clue brief 输入**; 经 V2 → `min_confidence`/`max_targets_per_day` 闸门后
  进富途模拟盘。不碰下单/退出链路; 仅 sim。
- 零 LLM 成本 (`use_llm=False`, 纯关键词匹配)。
- 完全可回退: `clue_hard_events: false` 即恢复原 clue brief。

## 预期与边界 (诚实)

- **安静日仍会稀疏**: 无爆点新闻的日子命中 CRITICAL/HIGH 关键词很少, HARD_EVENT 可能仍为 0
  —— 这是"宁缺毋滥"的正确行为, 非缺陷。
- ticker 映射: 分类器 `_extract_tickers`(related_stocks + 公司名表) + 板块传导兜底。
- 后续增量 (本 ADR 不含): 财报日历 `earnings_calendar.py`、内部人集群
  `insider_daily_*.json` 等"日历型/结构化"硬事件源, 验证本步有效后再一次一变量地加。

## 验证

- `py_compile scripts/llm_universe_scan.py` 通过。
- 重跑 `scan`: clue brief 出现 `[硬事件]` 段 (事件日); 落库 surfaced 的 `self_tier`
  在事件日应出现 `HARD_EVENT`。
- `eval-report` 对照前后 tier 分布 / 克制比。

## 回退

`config/unified_config.yaml` → `universe_expansion.clue_hard_events: false`, 重启服务。
