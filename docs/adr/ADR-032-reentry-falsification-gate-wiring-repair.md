# ADR-032: 盘中再入场证伪门接线修复 (四死门复活/诚实降级)

**日期**: 2026-08-22
**状态**: 已采纳 (用户裁决 "请修复", 承接本会话两轮证伪门核查)
**关联**: 73c853b9 / 0379a90d / 392f018e (被修对象), ADR-031 (同日反馈链路)

## 背景

两轮核查 (handoff 2026-08-22b) 发现盘中再入场 9 门中 4 门为死门: 接线按想象
schema 读 provider, 真实签名不同 → 取值恒 None → 永不触发; bare-except fail-open
把 ImportError/KeyError 全部吞成静默放行; 纯函数契约测试注入参数全绿, 接线零覆盖。
死门伪装成保护比没有保护更危险 (给人已设防的错觉)。

## 四门死因与修复

| 门 | 死因 (多重) | 修复 |
|---|---|---|
| analyst_divergence | ①读不存在的 `buy_pct/sell_pct` (真实 schema `{periods:[{strong_buy,buy,hold,sell,strong_sell}]}`); ②语义错位: 评级水平≠修订动量 | `_derive_analyst_trend`: periods[0] vs [1] 净多头占比环比, ±5pp 死区 → upgrade/downgrade/None (符合原意"修订动量") |
| earnings_proximity | ①`sym in _earn` 对 List[Dict] 恒 False; ②`_earn[sym]` 字符串下标 list 会 TypeError; ③**导入名 `get_earnings_calendar` 不存在** (ImportError 被 fail-open 吞掉, 活体探针抓到) | 导入改规范用法 `EarningsCalendar()` (全库一致); `_find_days_to_earnings` 遍历行匹配 Futu 格式 symbol 取 days_until |
| ticker_sentiment_divergence | 读不存在的 `buy_strength/sell_strength` (真实 schema `{sentiment[-1,1], buy_ratio, volume_weighted, confidence}`) | `_derive_ticker_direction`: confidence>0 且 sentiment 出 ±0.15 死区 → bullish/bearish/None |
| social_sentiment_divergence | 数据源 `collect_sentiment` 返回结构**全程无极性分数** (仅计数/点赞/标题), 本门所需带符号 score 无从取得 | **诚实降级**: 默认 False + 注释留档; 与其死门伪装保护不如明示不可实现, 待接入有符号分数源再启用 |

## 验证

- 新增 `tests/test_intraday_reentry_wiring.py` 8 例: **用 provider 真实输出形态做
  fixture** 钉死 helper 契约 (含 social 默认关断哨兵), 防同类 schema 臆写复发;
- 活体探针 (真 provider): MU 真实 periods → delta +2.4pp < 死区 → None (正确的
  None); NVDA 真实行 `days_until=3` (8/26 盘后财报) 命中;
- 全量 1440 passed (基线 1432 + 新增 8), 零回归。

## 影响与纪律声明

**本修复激活此前从未生效的拒绝路径** (analyst 修订动量背离 / 财报 <3 天 /
逐笔情绪背离), 属验证期内入场路径机制变更 — 用户明示裁决 "请修复" 后执行;
变更日期 2026-08-22 在案, 8/29 终裁读数如受影响可按此切窗。social 门默认关断
不改变现网行为 (原本也恒不触发)。回滚 = revert 本 hunk, 无数据迁移。
