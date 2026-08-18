# 议题：隔夜仓盘前重定价 shadow（动态 SL/TP 方案 1）— record-only 落地

| 字段 | 值 |
|---|---|
| 状态 | 已落地运行（record-only），攒样中；8/18 Qoder 独立核查：§一基线已勘误（ET 口径）+ settle OPEN 冻结待修（§七） |
| 创建 | 2026-08-18（用户问：隔夜仓 SL/TP 是否需随行情动态调整 → 批准方案 1 直接实现） |
| 脚本 | scripts/premarket_reprice_shadow.py（cron 北京 21:02 = ET 09:02） |
| 数据 | data/premarket_reprice_shadow.jsonl |
| 纪律 | 零行为变更；唯一键去重 (symbol, trade_date)；结算用权威 ET 口径 |

---

## 一、问题与证据基线（8/18 实弹）

**问题**：隔夜仓的 SL/be 入场后整夜静态。盘前利好不抬 be、盘前利空不收紧，
利润保护要等开盘 9:30 才恢复——行情动态但保护线不动。

**证据基线**（~~89 笔隔夜已了结单~~ → **8/18 Qoder 核查勘误，详见 §七**：初版数字系混合时区
字符串日期比较产物，ET 规范化真值如下）：
- 隔夜整体正贡献：~~净 +$18,788，胜率 58%~~ → **净 +$11,974，胜率 57.4%（47 笔）**。
  字符串口径复现 84–86 笔/+$18.5–18.6k，与声称 89 同族——均把 CST 午夜跨越的日内单
  误计为隔夜，笔数虚增 ~2x；"隔夜正贡献"两口径方向均成立
- 次日开盘 30 分钟内退出：~~12 笔：平均 +0.91%，最差 −0.15%~~ → **ET 真值 22 笔：
  平均 +0.65%，最差 −5.04%，净 −$3,193**（GOOG −$6,035 / XLK −$3,594 / TSM −$633）。
  初版 12 笔是字符串口径漏计 10 笔后的幸存子集（漏计行净 −$6,549，7 亏 3 赢）
  → ~~gap 击穿几乎不是问题~~ → **修正表述**：全史 gap 击穿真实存在（大仓时代 4–6 月为主，
  XLK −$3,594 即 6 月 suffix 时代）；7–8 月现行小仓纪律样本仅 3 笔且最差 −0.15%
  （n 过小不足下结论）。"broker 硬止损 + 开盘退出链接得住"只在现行纪律口径内成立且样本不足
- 结论：**不收紧隔夜政策整体 → 维持**（隔夜正贡献方向两口径均成立），
  但只补"盘前重定价"这个具体死角——本判断不受勘误影响

**现存保护（UBER 1288 为例）**：
1. broker 硬止损单（单号 8104560，跳空也执行，不依赖进程活着）
2. OVERNIGHT_GAP_STOP 规则（隔夜仓跌超 3% 开盘市价砍，`overnight_gap_stop_pct` 默认 3.0）
3. time_exit_only_if_loss（次日 EOD 只砍亏损仓）

**与既有 shadow 的分工**（防重复建设，已核实）：
- `overnight_hold_shadow.py`（8/10）：问"benign 退出该不该改持仓过夜"（退出决策反事实）
- 本 shadow：问"继续持有的仓，盘前该不该重定价"（持有中仓的保护线动态化）

## 二、决策规则（常量带注释，攒样后裁决可配置化）

盘前收益 `pre_ret_signed = (pre_price − prev_close)/prev_close × 方向符号`：

| 条件 | action | 含义 |
|---|---|---|
| ≥ +1.0% | `be_raise` | 建议抬保本线至 入场价±净费用线（净口径，对齐 7c4b1f7f 费用模型） |
| ≤ −2.0% | `tighten` | 建议开盘观察收紧 SL（只记录，不定义幅度） |
| 其余 | `hold` | 维持现 SL（broker 硬止损是主保护） |
| 盘前无成交 | `no_data` | 不算信号 |

## 三、结算设计

- 同脚本每轮先 settle 再 collect；trade_date < 今日 ET 的未结算行，用该日**完整日 K**
  算 gap / 全日收益 / 路径下界（×方向），be_raise 行另算 be 线是否被触及（be_touched）
- 实际结局 join trade_records（exit_reason/pnl_net），权威键 = symbol + entry_date_et
- 毕业检验：按 action 分组对比"建议动作 vs 实际结局"，be_raise 组看 be_touched 率与
  避免的亏损，tighten 组看 gap/路径下界——**增量视角**（在现有保护链基线上），禁裸边际

## 四、踩坑记录（实弹发现，接线者必读）

1. **open_positions.entry_time 是 CST 本地时间**（无时区后缀）：UBER 10:50 ET 入场
   显示为 22:50 → CST 日期=ET+1，用它做隔夜判定会漏判盘后入场仓。
   **修复：隔夜仓清单改从 trade_records 未平仓行取（权威 ET 源），SL 再 join open_positions。**
2. **SDK 10.9 `request_history_kline` 返回 3 元组**（ret, df, page_req_key），
   2 元组解包报 "too many values to unpack"。
3. **结算测试日期必须选真实交易日**（8/15 是周六，日 K 为空属正常非 bug）。
4. **trade_records 时间戳三种写法混存，同一行可混用**（naive=CST / `-04:00`=ET /
   `+00:00`=UTC；XLK 6 月案例 entry=naive + exit=suffix）。任何 `date()` 字符串比较
   都会同时**虚增**（CST 午夜跨越的日内单误计隔夜）与**漏计**（entry 为 naive 的真隔夜单
   被判当日）。本档 §一初版基线即栽在此坑——与坑 1 同源，但对历史统计生效。
   **修复：历史统计先规范化到 ET**（复现脚本 `scripts/deb_overnight_baseline_et.py`）。
   现行写入路径已统一 ET 后缀（新行不受影响），受影响的只是历史基线数字。

## 五、预注册判据与裁决

- **攒样目标**：n≥30 条 settled（含 be_raise/tighten 各 ≥5 条），或 8/29 验证期结束
- **毕业条件**（转正门槛，对齐 8/9 纪律）：be_raise 或 tighten 组相对 hold 基线
  有可复现的净改善（避免亏损或锁定利润，费用扣除后），双窗口方向一致
- **裁决三选项**：转正（接退出链，届时才动 broker 单）/ 续采 / 关门
- **显式不做**：盘前自动改 broker 止损单（SIM 期改单复杂度高、收益未验证）；
  复活死配置 `overnight_threshold_atr`（gap 数据不支持紧迫性）

## 六、验收（8/18 实弹）

- dry-run：UBER pre −1.38% → hold ✅（阈值逻辑正确）
- 结算路径：合成行 8/14 MU be_raise → settled=True，settle 字段齐全 ✅
- 首次正式记录：data/premarket_reprice_shadow.jsonl 1 条（UBER 8/18，hold，
  entry_date_et=2026-08-17，decision_id/SL/净费率 0.00079 全齐）✅
- 唯一键去重：重跑同日零新增 ✅
- 交易日 gate：trading_day_check.py 复用 ✅

## 七、独立核查（Qoder，2026-08-18）

**方法**：脚本逐行审读 + cron/DB/jsonl 实弹复测（含 dry 跑）+ 基线 SQL 独立重算。
复现：`venv/bin/python scripts/deb_overnight_baseline_et.py`（两种口径对照 + cohort 时代分布）。

**通过项**：
1. record-only 安全：DB 两处 `mode=ro`，唯一写路径 = jsonl append，零 broker/交易路径 ✅
2. cron 挂载：`2 21 * * 1-5` = 北京 21:02（ET 09:02），与声明一致；交易日 gate 复用 ✅
3. 幂等去重实弹验证：dry 跑 (US.UBER, 2026-08-18) 命中 seen key 跳过，jsonl 零新增 ✅
4. 首条记录字段全齐（decision_id / SL 72.65 / fee_pct 0.00079 / entry_date_et=2026-08-17）✅
5. §四.1 CST 踩坑铁证：UBER 同仓 open_positions `22:50:54.727120`（naive）vs
   trade_records `10:50:54.658127-04:00`——同一时刻差 12h ✅
6. 决策阈值与 §二一致；settle 的 be_touched / path_worst 多空方向逻辑正确
   （多看 low / 空看 high）✅

**材料性发现**：
1. **§一基线数字系混合时区字符串比较产物（已就地勘误）**：真值 47 笔 / +$11,974 / 57.4%
   （声称 89 / +$18,788 / 58%，虚增 ~2x）；首 30 分钟 cohort 真值 22 笔 avg +0.65%
   worst −5.04% 净 −$3,193（声称 12 笔 worst −0.15% 系漏计幸存子集，漏计行净 −$6,549）。
   方向性结论（隔夜正贡献 / 不收紧政策 / shadow 立项）两口径均成立，落地无需回滚
2. **settle 对未平仓行也置 settled=True**（L272）：仓未退出时 actual_exit_reason 冻结为
   'OPEN' / pnl=null 且永不再刷新 → W_WEEK 多日持有仓最终结局丢失，污染毕业检验
   "建议 vs 实际结局"对照。UBER 8/18 行预计 8/19 结算时仍是 OPEN。
   **待修**：exit_time 非空才 finalize，或每轮对 settled-but-OPEN 行重查 join
3. **`--collect-only` 语义反转**（L298）：实际效果 = settle-only（collect 被跳过），命名误导

**小注**（不阻塞）：
- 首条记录系 05:16 ET 手动跑采的早期薄盘前价（pre −1.33%），当晚 09:02 cron 因去重
  不再采 UBER——一次性数据质量注记
- 交易日 gate fail-open + cron 1-5：美股假日落在工作日时会写 no_data 行（不算信号，噪音）
- 初版 89 与复现 84–86 的 3–5 笔残差未精确对上（微秒边界解析差异），不影响定性

## 关联文档

- docs/debates/DEB-20260808（隔夜 loser_overnight W_WEEK 裁决，本档不推翻）
- app/playbook/overnight_hold_shadow.py（互补 shadow：退出决策侧）
- config/unified_config.yaml L1119 overnight_hold_enabled / L1127 loser_overnight_enabled
- scripts/deb_overnight_baseline_et.py（基线 ET 口径复现脚本，§七）
- 提交链：7c4b1f7f（净口径费用模型，be 线抬价依据）；1b400813（本 shadow 落地）
