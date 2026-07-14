# ADR-014: Playbook 退出原因精细化标签 + 退出影子模块存档

- 状态: Accepted (存档现状 / archive of running code)
- 日期: 2026-07-14
- 分支: rfc-060-phase1-hotfixes

## 背景

Playbook 退出侧已在运行的一批改动此前未提交（服务重启时从磁盘加载，已生效多日）。
本 ADR 将这批改动**原样存档**，防止误 checkout/reset 丢失。**零运行时行为变化**——
代码即当前生产在跑的版本，本次仅做版本化留痕。

## 决策

将 playbook 退出重构作为一个自洽提交存档，范围严格限定为退出侧相关文件：

### 退出原因标签精细化 (行为不变, 仅归因语义)
`SL_FIXED_PCT` 拆分为 `SL_INITIAL` / `SL_BREAKEVEN` / `SL_TRAIL_LOCK`：
- 触发时机、止损价计算**完全不变**；仅按 breakeven ratchet / trailing 状态给出更精确的退出原因标签。
- 保留 `SL_` 前缀以兼容下游 `LIKE 'SL%'` 过滤。
- 下游消费端（chain.py 看板、db.py、trade_attribution.py、feishu_push.py）同步兼容新旧标签。

### 退出/校验影子 (record-only, 自带 try/except, 不影响主路径)
- `app/playbook/exit_shadow.py`: 退出影子, 记录各退出变体首次触发 + peak/trough, 落 JSONL。
- `app/playbook/sltp_verify_shadow.py`: 止损止盈校验影子, record-only。

### 入场 band 单一真源 (SSOT)
- `app/playbook/entry_band.py`: CHASE_ZONE_PCT / KNIFE_FLOOR_PCT 单一定义, 执行端与 DA 直写共用, 防百分比漂移。

## 提交范围

- M: app/playbook/pipeline.py, app/playbook/db.py, app/analysis/trade_attribution.py,
     app/playbook/feishu_push.py, app/web_dashboard/routes/chain.py
- New: app/playbook/entry_band.py, app/playbook/exit_shadow.py, app/playbook/sltp_verify_shadow.py

工作区其它在途改动（voting 重构、position_monitor、entry_reference、thesis_recheck 等）
**不在本提交范围**, 属独立关注点, 另行处理。

## 后果

- 正向: 退出重构版本化, 防丢失; 退出归因语义更精确; 影子链路可离线复盘。
- 中性: 无运行时行为变化 (存档现状)。
- 待办: 通知来源标注修正 (MAIN_PIPELINE→owner_strategy) 与历史归因去重, 另行处理。
