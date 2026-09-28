# DEB-20260928 · SL_INITIAL 止损几何归因（只读，冻结期合规）

- **立案日**: 2026-09-28
- **发起人**: 用户（"从提升收益的目标出发给出优化建议"→ 同意立案）
- **状态**: **提案（待用户裁 F1–F2）**；零行为变更——冻结期内**只做归因，不提参数**
- **类型**: 只读归因 → 若假设成立，9/30 冻结结束后另立"stop ≥ k×ATR"影子
- **来源**: DEB-20260928 §五/§十四 + DA 笔记挑战者行（止损几何问题）+ loss_exit_recovery
- **关联**: 判据字典 C1（退出参数冻结至 9/30）；DEB-20260928 底牌7（回翻分层）

---

## 一、问题界定

日内 `SL_INITIAL` 是第二大出血口（16 笔 −$2,081），且日期/价格层分散、无单点集中——
不是个别坏票，可能是**止损几何与日内噪音不匹配**的结构问题。DA 笔记挑战者已多次点名
"止损距离 < 单日振幅"（例：AMD 止损 3.24% vs 单日振幅 5.135%）。本 DEB 只做一件事：
**用逐仓 ATR 比（|entry−stop|/atr）量化"多少 SL_INITIAL 是被日内噪音打穿的"**，
并预注册"若过半是噪音触发"的后续影子入口。

## 二、底牌清单

| # | 事实 | 读数 | 证据锚 |
|---|---|---|---|
| 1 | 日内 SL_INITIAL 16 笔 **−$2,080.86**（−$130.05/笔）；另隔夜 1 笔（UBER 8/26）−$307.13 | 日期分散 8/28→9/24；层分布 <$20 3 笔 −$325 / $20–100 3 笔 −$598 / ≥$100 10 笔 −$1,158 | trade_records SQL（§附） |
| 2 | 回翻分层：SL_INITIAL ≤5 日触碰 **59%**（13/22）→ ≤20 日衰减 **38%**（3/8） | 快回的是浅亏、死的是深亏（deep>3% 桶仅 20%）——**回≠该扛**，但"过半快回"提示部分触发在噪音区 | scripts/loss_exit_recovery_check.py 实跑 |
| 3 | 逐仓 ATR 已落库 | metadata `atr`（**绝对价**）+ `atr_basis`（=proxy）+ `stop_loss`——量纲可声明、无需新拉行情 | trade_records.metadata_json |
| 4 | 全量 16 笔 ratio 已读出（本会话附录命令实测） | 14/16 聚在 **0.93–1.04**（止损系统性贴 **1×ATR** 放置）；1 离群 LLY **16.17**（ATR 3.13 vs 止损距 50.61）；**noise（ratio<1）=10/16=62.5%**；7/16 行 `atr_basis` 缺失（旧行）须分层 | §附 SQL（已实跑） |

## 三、影子/归因设计（裁决后落地，只读）

1. **人群**：8/25 起日内 `SL_INITIAL` 已平仓（含后续增量），逐笔计算
   `ratio = |entry − stop_loss| / atr`（**同基绝对价，无量纲**；atr_basis≠proxy 的行
   如实标注并单独分层，防量纲混装）。
2. **分类**：`noise`（ratio < 1.0，止损距离小于一个 ATR）vs `breakdown`（ratio ≥ 1.0）。
3. **结局 join**：接 `loss_exit_recovery` 的 ttr_touch/close 口径，比两桶的 ≤5 日回翻率。
4. **报告铁律**：ratio 单位（无量纲）、ATR 单位（绝对价）、分母（atr）三项每报必带
   （军规"单位声明+契约"纪律）。
5. **⚠️读数已部分先行披露**：ratio 半场（§二底牌 4）在附录命令复现检查中已跑出
   （noise 62.5%）——正式归因报告须原样复核该数并补齐**恢复率差**半场与 basis
   分层，判据不因先行读数移动任何阈值。

## 四、反方观点

| # | 反方论据 | 回应 | 残余风险 |
|---|---|---|---|
| R1 | 放宽止损=错时亏更深（deep 桶 20% 回翻支持砍深亏是对的） | 归因不自动等于放宽——只测"噪音触发占比"；k×ATR 影子届时独立预注册 | 高（方向反噬风险） |
| R2 | 回翻≠该扛（DEB-20260928 Q4 幸存偏差已登记） | 归因判据用"噪音占比+回翻差"双条件，不用单一回翻率 | 中 |
| R3 | C1 冻结：止损参数 9/30 前禁改 | 本 DEB 冻结期只读；任何参数影子排 10 月后 | 低（设计内） |
| R4 | ATR basis=proxy 的质量/量纲风险 | §三.1 按 basis 分层 + 单位必报；proxy 行单独统计 | 中 |
| R5 | n=16 小样本 | 归因只作方向参考；后续影子另设 n 门 | 中 |

## 五、预注册判据（归因读出后，冻结结束后才可触发下一步）

```
sl_initial_stop_geometry 归因门 (一次读数, 无攒样):
  noise 桶占比 ≥ 50% (日内 SL_INITIAL)
  ∧ noise 桶 ≤5日触碰率 ≥ 70% 且显著高于 breakdown 桶
  → 10 月冻结结束后另立 "stop ≥ k×ATR" 影子 DEB
    (k 预注册单值, 禁网格; 影子先行; 用户裁)
  否则: 止损几何假设不成立, 结题 (出血归 SL_INITIAL 正常功能)。
禁改: ratio 边界 1.0 / noise 定义 — 改=重裁。
```

## 六、边界 / 验证 / 回滚

- 零行为零配置零 cron；归因脚本只读 DB + 复用 loss_exit_recovery 产物。
- 回滚：整文件 revert。
- 自曝 1：建议稿口头版直接指向"k×ATR 参数影子"——自查 C1 冻结 + 一次一变量纪律后
  收敛为"归因→条件触发→另立影子"三段式。
- 自曝 2：附录命令复现检查把全量 16 笔 ratio 先跑了出来（本想只验 SQL 语法）——
  按"轮内自我修正光荣"就地披露：noise 62.5% 已知，判据**未因看到读数而移动**
  （ratio 边界 1.0 是预注册后才跑的全量）；剩余决定性条件=恢复率差。
- 自曝 3：全量读数显示止损已系统性贴 1×ATR（14/16 在 0.93–1.04）——原假设表述
  "止损几何不匹配"应精化为"**1×ATR 对日内噪音是否过紧**"，本节就地更正。

## 附：可复现命令

```bash
sqlite3 -header -column "file:data/ftseek_us.db?mode=ro" "
SELECT symbol, entry_price, json_extract(metadata_json,'$.atr') atr,
json_extract(metadata_json,'$.atr_basis') basis,
json_extract(metadata_json,'$.stop_loss') stop,
ROUND(ABS(entry_price-json_extract(metadata_json,'$.stop_loss'))
      / json_extract(metadata_json,'$.atr'),2) ratio
FROM trade_records WHERE date(entry_time)>='2026-08-25' AND exit_time IS NOT NULL
AND exit_reason='SL_INITIAL'
AND CAST(julianday(date(exit_time))-julianday(date(entry_time)) AS INT)=0
ORDER BY date(entry_time);"
```
