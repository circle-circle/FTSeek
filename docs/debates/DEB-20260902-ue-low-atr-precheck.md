# DEB-20260902: UE inject 低ATR源头预检 (结构性死指令止损)

- **日期**: 2026-09-02 晚 (交易时段内裁定, 改动骑次日 21:10 UE cron 生效)
- **裁决**: 用户裁"同意" (方案 1: inject 阶段 atr_pct 预检, 阈值与执行门同源)
- **性质**: 缺陷类修复 (资源浪费/遥测噪音), 非行为假设检验 — 无预注册判据、无影子窗
  (依据: 预检拦截的指令在执行门下本来就永不可能成交, 理论零交易行为变更)

---

## 一、缘起

用户令"对标的股预检查做分析评估"。当晚链路复盘发现 UE 注入的 16 条指令中
5 条 (XLY/XLI/XLU/AAPL/LLYVK) 的 atr_pct 均低于执行门阈值 3%
(yaml `playbook.low_atr_gate_min_atr_pct: 0.03`) — **结构性死指令**: 注入即注定
每轮被 low_atr_block 拦截, 全天 12 轮重复。

## 二、证据

| 证据 | 数值 |
|---|---|
| 今晚 UE 死指令 | XLY atr=1.31% / XLI 1.33% / XLU 1.43% / AAPL 2.05% / LLYVK 2.51%, 全 <3% |
| 历史拦截量 | trigger_miss 遥测自 7/24 起 5,240 条 low_atr_block; top: XOM 676 / CVX 479 / KO 457 / MRK 355 / JNJ 215 (不只 ETF, DA 慢性低波大盘票同病) |
| ETF 全历史成绩 | 420 笔个股成交 vs **ETF 仅 1 笔 (-$43.23)** |
| 立门证据 (7月) | atr<2.5% 桶 13 笔 15% WR -$244, MFE<0.5% 占 85% — 波动能力决定 MFE 天花板 |
| 死指令日成本 | 常驻订阅槽位 (今晚总订阅 27 只 ×4 推送类型) + 每轮 re-price + trigger_miss 遥测噪音 (稀释真实 miss 信号) + 推送"可交易"假象 |

## 三、方案对比

1. **UE inject 阶段加 atr_pct 预检 (定案)** — 与执行门同键同阈值同 provider 源头拦截。
   关键论点: 执行门已判定这些指令永不可能成交 → 预检不改任何交易结果, 纯止损
   死指令开销, 无需影子验证期。唯一边界: inject 用前收 ATR, 隔夜跳空日
   2.8%→3.5% 的票会被误拒一条 (可接受, 单日单标的指令槽位损失)。
2. ETF 类直接排除 — 效果≈方案 1 (3 只 ETF 均 <1.5%), 但类目规则无证据支持
   ETF 专用低阈值 (只会更差), 多一个特例。弃。
3. DA 慢性低波票 (XOM/CVX/KO/MRK/JNJ) 同治 — 暂不动: DA 是深析轨道, 指令对
   用户有信息价值; 若未来嫌遥测噪音, 先给 trigger_miss 加分类标签 (纯遥测改动)。

## 四、实现 (scripts/llm_universe_scan.py)

- 位置: cmd_inject 候选循环, price_floor 检查之后 (先便宜后贵: 本地比较 → provider 调用)。
- Provider: `get_di_container().atr_provider()` — 与盘前定桶任务
  (pipeline.py `_tag_volatility_buckets`, 即执行门读的 atr_pct 列的写入方) 同源:
  日线 ATR, 盘前稳定不依赖 OpenD; 传 inject 现价 px 避免 provider 内部二次取价。
- 配置: 读执行门**同键** `playbook.low_atr_gate_enabled` / `playbook.low_atr_gate_min_atr_pct`
  (回退默认 0.025 与执行门一致; 活性值 yaml 0.03 SSOT)。单旋钮同开同关 —
  关执行门同时关预检, 无双门失联。
- 拒绝形态: 与 price_floor 同轨 — 落 SKIP 行 (skip_reason 自带数值
  `low_atr(0.0131<0.03, 低波票过不了执行门)`, decision_id 保持 UE 源
  `universe_expansion:date:symbol`) + `_log_inject_loop_reject` jsonl 归因。
  SKIP 行不占 ADR-019 排重 (次日波动恢复可再浮现), save_instruction 不映射
  atr_pct 列 (数值进 skip_reason, 不动 db.py)。
- fail-open 纪律 (与执行门同): provider 构建失败 → 预检整体旁路; 单标的 ATR
  取不到 (None) / 异常 → 放行, 执行门兜底。

## 五、测试 (tests/test_ue_low_atr_precheck.py, 9 例)

功能级 (mock 驱动 cmd_inject dry-run): 低ATR→SKIP行+jsonl归因+不生成正常指令 /
高ATR照常生成 / 开关关→provider 不构建 / ATR None→fail-open / provider 异常→
fail-open 不炸 inject。源级契约: 同键同默认 (与 pipeline.py 对齐防漂移) /
同 provider / fail-open 文案在。

## 六、回滚

`playbook.low_atr_gate_enabled: false` — 执行门与预检一起关 (单键)。
代码级回滚: revert 本提交。

## 七、go-forward 观察点 (明晚 21:10 首跑)

1. logs/universe_scan.log inject 段应出现 `低ATR预检 (x.xxxx < 0.03) → SKIP行`,
   且 DB 当日 uni 行中 XLY/XLI/XLU 一类标的 direction=SKIP、skip_reason=low_atr。
2. 推送"实际 N 条"应排除这些 SKIP 行 (与 price_floor 同口径)。
3. 若某低波标的次日 ATR 回升 ≥3% (隔夜跳空) 被误拒 → 预期内的边界情形, 记录
   频率; 若高频出现 (>1 条/日常态) 再议阈值分离 (inject 2.5% vs 执行 3%)。

## 八、未做与诚实边界

1. **DA 慢性低波票未治** (方案 3 搁置): XOM/CVX/KO/MRK/JNJ 仍每日生成注定被拦的
   指令, 代价仅 re-price 循环与遥测噪音, 收益是 DA 深析信息保真。待遥测噪音成为
   实际痛点再立提案。
2. **方向偏斜 guardrail 全同向退化** (当晚同场发现, 另立议题): 9/2 生成批 10 条
   有效指令全 BUY → 偏斜 100% → trim-to-1 (RIVN 存活), 9 条翻 SKIP; 4 条
   (GTLB/DELL/BWXT/UBER) 仅因"空 did 写入不覆盖有源行"仲裁保留 BUY 并全部成交。
   少数方为 0 时比例 trim 永不达标是机制固有, 且 14 条 DA conf 全=70 无区分度,
   幸存者近乎随机。当晚 5 条成交盈亏混合, 无证据表明被杀 9 条更好 — 记录在案,
   若再现 (连续 all-one-direction 日) 建议提案"全同向时按仓位预算截断"。
