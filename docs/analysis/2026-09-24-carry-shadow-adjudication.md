# 隔夜财报 carry 影子裁决留档 (2026-09-24)

**裁决**: ⛔ **彻底停采，不重启**（用户明示裁决，2026-09-24）。若 10 月财报季需重启，**须重新预注册**（判据/口径/采集字段修复一并重写），不得直接恢复 cron。

## 1. 预注册判据（写死于 scripts/overnight_earnings_carry_shadow.py 头部，2026-09-22 立）

> n≥3 交易日 ∧ 过闸上界 ≥2 只/日 ⇒ 值得立 DEB 提请放宽门的"同日"语义；
> 过闸上界持续 <0.5 只/日 ⇒ 该路不成立，停采（删 cron 行）。

## 2. 实测读数（9/22–9/24，3 个交易日）

- 逐日 cohort summary：**n_passes_floors = 0 / 0 / 0**（<0.5 只/日停采判据达成）。
- 根因与 9/22 零成交归因一致：9 月非财报淡季，EXT 候选池 ∩ 财报门 = ∅ 结构性零供给（回测上界 2.7 只/日系 9/01–9/16 财报季尾声数据，淡季不成立）。
- **数据质量披露（随裁决一并留档）**：台账 data/overnight_earnings_carry_shadow.jsonl 190 行中明细键（symbol/verdict/carry_eligible 等）全 null，仅 cohort/quoted/reject 汇总字段可用 —— 即使供给恢复，明细面也不可裁决；重启预注册时须先修采集字段。

## 3. 裁决时点说明

预定裁决日 2026-09-25（台账 ext-research-arm-20260922）。实际于 9/24 裁决：停采判据（n≥3 日 ∧ <0.5/日）已在 9/24 收盘采集轮后达成，且用户在场明示「彻底停采不重启」。提前一日不改变判据本身，判据文字零改动。

## 4. 执行动作

1. crontab 删除 2 条采集行（原文见附录；备份 /tmp/crontab_backup_20260924_carry.txt）；
2. 脚本头部加退役标（保留代码与预注册原文供考古）；
3. shadow_board.py 展示段加「⛔已退役9/24」诚实标签（看板不替死影子背书，同 RFC-106 退出桶标签诚实化纪律）；
4. 台账 jsonl 保留归档不删。

## 附录：被删 cron 行原文

```cron
*/5 16-22 * * 1-5 cd /opt/ftseek/FTSeek-US && PYTHONPATH=. timeout 120 venv/bin/python3.11 scripts/overnight_earnings_carry_shadow.py >> logs/overnight_earnings_carry_shadow.log 2>&1 # OVERNIGHT-CARRY-SHADOW (2026-09-22 用户裁「按建议」): 「T-1盘后财报票→T盘前承接」口径记录面, 测"若门放宽到 T-1 盘后当日能过几只闸"(回测上界 24/95≈2.7只/日, 缺口=盘前量, 本影子逐轮补测); 零下单/零落库/零改配置; 假日由 get_extended_hours_session 自门控; 回滚=删本行+删脚本; 2026-09-22 DST免疫扩段(复查批,用户裁「按照建议执行」): 冬令时11/1后ET窗右移1h会丢采集段 ⇒ 小时段+1; 脚本由 get_extended_hours_session 自门控, 夏令时多余tick空转零风险; 取代脚本头「11月两行各推后1h」的手工操作; 回滚=恢复 16-21/4-7
*/5 4-8 * * 2-6 cd /opt/ftseek/FTSeek-US && PYTHONPATH=. timeout 120 venv/bin/python3.11 scripts/overnight_earnings_carry_shadow.py >> logs/overnight_earnings_carry_shadow.log 2>&1 # OVERNIGHT-CARRY-SHADOW 盘后窗 (同脚本, cohort=当日 bmo 财报票→T 日盘后承接); 其余同上行; 回滚=删本行; 2026-09-22 DST免疫扩段(复查批,用户裁「按照建议执行」): 冬令时11/1后ET窗右移1h会丢采集段 ⇒ 小时段+1; 脚本由 get_extended_hours_session 自门控, 夏令时多余tick空转零风险; 取代脚本头「11月两行各推后1h」的手工操作; 回滚=恢复 16-21/4-7
```
