# RFC-216：Market Regime VIX 20–22 连续计分边界

- 日期：2026-11-24
- 状态：**已批准 · 已部署并完成自然链路验证（收益未证）**
- 用户裁决：「按照建议修复调整」
- 关联：RFC-190（VIX 代理口径与硬门）、`DEB-20261124-entry-data-stale-cache-starvation.md`

## 1. 问题与范围

旧 VIX 阶梯在 20 附近为 `<=20:+1`、`>20:-2`，单点变化带来 3 分跳变。若其他趋势项合计 -5，则 VIX 19.99 得总分 -4（RISK_OFF），VIX 20.01 得总分 -7（NO_TRADE）。而生产使用 VIXY×0.85 代理，RFC-190 已证明该代理存在系数漂移，硬边界没有测量缓冲。

本 RFC 只处理这一不连续性问题，**一次一变量**：

- 不改方向权限，不区分 LONG/SHORT；
- 不改 SPY/MA 分项、regime 分档、仓位倍率、其他 VIX 区间；
- 不改 VIXY 代理、官方 VIX 源、auto_execute、价格/确认/预算/退出规则；
- 不承诺增加盈利或成交，仅消除不必要的阈值跳变。

## 2. 实施公式

新增 `vix_score(vix, smooth_20_22=False)`。开关关闭时逐值恢复旧阶梯；开启时仅在 `20<VIX<22` 内使用：

```text
vix_score = 1 - 1.5 × (VIX - 20)
```

边界：

| VIX | 旧规则 | 连续规则 |
|---:|---:|---:|
| 20.00 | +1 | +1 |
| 20.26 | -2 | +0.61 |
| 21.00 | -2 | -0.50 |
| 22.00 | -2 | -2 |
| >=22 的其他值 | 原值 | 原值 |

两端连续，区间外逐值相同。配置读取失败或缺键=旧阶梯（保守回滚）。

当前实例的其他分项为 -5，因此 VIX=20.26 时：旧总分=-7/NO_TRADE；新总分=-4.39/RISK_OFF。`RegimeContext.score` 允许小数；关闭开关的整数分数与日志格式保持不变。

## 3. 独立开关与回滚

配置：`playbook.execution_parameters.market_regime_vix_20_22_smoothing_enabled`。

- 本批 YAML=true 启用连续计分；
- **回滚=改 false**，热载恢复旧阶梯，无需改代码；
- 初始 Python 修改按项目规定重启生效。

## 4. 只读回放与证据边界

解析现有 `logs/ftseek_us.log*` 的 regime 评估行，`20<VIX<22` 共 7 条，全部为当前 11-24 边界实例：旧规则均 `no_trade`，连续规则均 `risk_off`。未发现其他历史日被改变。

这证明影响面窄，**不证明新规则有正收益，也不证明 VIX 阈值型门有 edge**。RFC-190 的代理失真、历史高 VIX 日样本不足等限制原样保留。不得把本 RFC 当成放宽全部风控或重新证明 regime 策略。

## 5. 测试

新增 `tests/test_market_regime_vix_neutral_band.py`：

- 旧阶梯边界与回滚；
- 20/22 两端连续、区间外不变；
- 当前实例 -7/NO_TRADE → -4.39/RISK_OFF；
- VIX=22 仍为 -7/NO_TRADE；
- 回滚恢复整数 -7/NO_TRADE。

实际运行 regime + 配置测试（带项目 conftest，确保类级 regime cache 每例清理）：

```text
87 passed, 1 warning
```

配置普查组合另有 **1 failed / 34 passed**：`test_gate_no_new_dead_keys` 只列既存 `default_method`、`simulated_mode_force_options`、`reasoning_effort`，没有本 RFC 新键；未修改无关基线，未宣称该组合全绿。

## 6. Cohort 与前向验证

这是入场环境行为变更，部署时点之后必须作为新执行 regime 子 cohort，不能与 11-17 qwen4-max post 窗混合宣称改善。现有 OKR whole/pre/post 披露不因此改写；后续若比较收益，须另按部署时点切分并保留 A9/A10、日聚类与 MDE。

验收顺序：

1. 启动冒烟与四锁/持仓/在途不变；
2. 自然 regime 日志在当前边界显示连续分数与 RISK_OFF；
3. auto_execute 恢复评估后，观察日 K 热加载是否消除 BABA starvation；
4. 最终是否成交由其他既有门决定，不把新开仓当本 RFC 的必要成功条件；
5. 若出现非预期放大、数据异常或回撤归因不清，配置改 false 回滚，不继续叠加方向调整。

## 7. 部署与自然验证（2026-11-25 00:01 CST / 11-24 11:01 ET）

- 重启前复核：SIM LLY/BA 两仓、LIVE 无仓、入场在途 0、两出场账本未决 0；进程启动后只有本批 `market_regime.py` 新改。
- `systemctl restart ftseek-us` 后新 PID **4064204**，启动冒烟 **7/7 / exit 0**，日志 `/tmp/rfc216-startup-smoke-20261125.log`。
- 自然 regime：`VIX=20.34 → score=-4.51 / risk_off`，auto_execute 恢复 18 条候选评估；`VIX=20.39 → -4.585`，连续计分与日志格式符合预期。
- 日 K 热加载生产实证：`落盘日K缓存热加载 701 只`。BABA 不再 trigger timeout，完成 `85.30→83.62` reprice 并进入 120s 确认，随后确认通过；CAT/UBER 同样通过确认门。
- MSFT 先按设计出现 `SUBMITTED` 待确认，随后正常回执收口：指令 `5752` **BOOKED / FILLED_ALL**，SHORT 8 股、成交 **494.15**；trade record `1943` 落库，`LateFill audit=True tca=True`，退出监控接管。随后 BABA 指令 `5757` 也 **BOOKED / FILLED_ALL**，SHORT 45 股、成交 **83.525**，trade record `1944`。最终入场在途 0，SIM 未平为 LLY/BA/MSFT/BABA，LIVE 无仓。

因此本 RFC 的**结构修复与自然链路验证成立**；盈利、命中率、死单率和方向偏斜仍未验证，不能用两笔成交宣称策略改善。

## 8. 未决事项

- LONG/SHORT 是否应有不同 NO_TRADE 权限：本 RFC 不处理，需另案与独立证据；
- VIXY 代理准确性与官方 VIX 真值：沿用 RFC-190，不借本批换源；
- 新规则的收益、死单率、方向偏斜与被拦机会成本：前向样本不足，当前不作结论。
