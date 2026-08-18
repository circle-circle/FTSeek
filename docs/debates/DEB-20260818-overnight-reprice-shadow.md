# 议题：隔夜仓盘前重定价 shadow（动态 SL/TP 方案 1）— record-only 落地

| 字段 | 值 |
|---|---|
| 状态 | 已落地运行（record-only），攒样中 |
| 创建 | 2026-08-18（用户问：隔夜仓 SL/TP 是否需随行情动态调整 → 批准方案 1 直接实现） |
| 脚本 | scripts/premarket_reprice_shadow.py（cron 北京 21:02 = ET 09:02） |
| 数据 | data/premarket_reprice_shadow.jsonl |
| 纪律 | 零行为变更；唯一键去重 (symbol, trade_date)；结算用权威 ET 口径 |

---

## 一、问题与证据基线（8/18 实弹）

**问题**：隔夜仓的 SL/be 入场后整夜静态。盘前利好不抬 be、盘前利空不收紧，
利润保护要等开盘 9:30 才恢复——行情动态但保护线不动。

**证据基线（89 笔隔夜已了结单）**：
- 隔夜整体正贡献：净 +$18,788，胜率 58%
- 次日开盘 30 分钟内退出的 12 笔：平均 **+0.91%**，最差 **−0.15%**
  → gap 击穿几乎不是问题（broker 硬止损单 + 开盘退出链接得住）
- 结论：**不收紧隔夜政策整体**，只补"盘前重定价"这个具体死角

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

## 关联文档

- docs/debates/DEB-20260808（隔夜 loser_overnight W_WEEK 裁决，本档不推翻）
- app/playbook/overnight_hold_shadow.py（互补 shadow：退出决策侧）
- config/unified_config.yaml L1119 overnight_hold_enabled / L1127 loser_overnight_enabled
- 提交链：7c4b1f7f（净口径费用模型，be 线抬价依据）
