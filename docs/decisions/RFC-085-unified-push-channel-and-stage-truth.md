# RFC-085 · 统一推送渠道 + 阶段真实内容

- **日期**: 2026-09-18
- **触发**: 用户实测盘后单 US.WBD 的成交推送（飞书纯文本「盘前事件开仓已成交」）与平仓推送（钉钉富卡）
  **落在两个平台、两种版式**，且成交文案把**盘后**说成**盘前**。用户裁决：
  ①交易类事件**双发钉钉+飞书**、其余保持主通道优先；②**同批回收**全部直连通知器；
  ③要独立**阶段日报**（有样本才推）；④日报/周报加**阶段×方向×出场原因**（ETH 单列）。
- **性质**: 纯展示/通知层改动，**零交易行为变更**（不动入场/出场/仓位/阈值），无需预注册裁决。

## 根因（实测，非推断）

| 项 | 事实 |
|---|---|
| 事件 | US.WBD 建仓 `2026-09-18 18:35 ET`（`session=afterhours`，`EXT-4553c8388794`），出场 19:29:49 `SL_TRAIL_LOCK`，净 +$51.66 |
| 成交推送 | `scripts/extended_hours_entry_executor.py::_push` → **直连 `FeishuNotifier().send_text()`**；文案硬编码「**盘前**事件开仓」 |
| 平仓推送 | `app/push/message.py::trade_exit` → `PushHub` → **钉钉**（主通道 priority 1） |
| 机制 | Hub 走「主通道成功即停」，只有 `Level.CRITICAL` 才广播；延长时段链（盘前/盘后）绕过 Hub ⇒ **盘中→钉钉、盘前/盘后→飞书** 的系统性分流 |
| 直连清单 | 全库 **14 处**绕过 Hub：飞书 3 处（全部在延长时段链：entry/exit/orphan watch）＋ 钉钉 11 处 |

## 变更

1. **路由**: `PushHub.broadcast_trade_events`（yaml `push.broadcast_trade_events`，默认 true）
   = 成交/平仓 `TRADE_ENTRY|TRADE_EXIT` **广播全部可用通道**；其他类别（日报/周报/告警）仍主通道优先，避免噪音翻倍。
2. **SSOT 出口**: `app/push/hub.py::push_text_safe()`（异常永不上抛）＋ `HubTextAdapter`
   （保留 `notifier.send_text / notify_system_alert` 调用形状，单点替换对象型持有者）。
3. **阶段 SSOT**: `app/core/trade_identity.py::phase_cn()` / `phase_from_time()`
   （盘前 04:00–09:29 / 盘中 09:30–15:59 / 盘后 16:00–19:59 ET，**盘外返回空——不臆造**）。
   `PushMessage` 卡片新增「🏷️ 口径: 模拟 · **阶段: 盘后**」；成交与平仓都带。
4. **延长时段开仓成交**由飞书纯文本改为统一 `trade_entry` 富卡，原信息**一行不丢**
   （财报超预期 / 阶段涨跌 / EXT 量 / `initial_sl` 及来源 / SIM 无券商 STOP 兜底提示 / 持仓 id）
   —— 经 `extra_lines` 通用补充行渲染。
5. **回收 14 处直连**（钉钉 11 + 飞书 3）。`app/utils/critical_alerts.py` 的旧双路**保留不动**：
   它此前已完成「PushHub 优先、旧路径仅兜底」收尾（DEB-20260910-logging §11），再改动会回退那轮结论。
6. **平仓推送的阶段语义**: 标**出场时刻**阶段；若与入场阶段不同（盘前建仓→盘中平掉）追加
   「🕐 入场阶段: 盘前」，避免被读成同一阶段。

## 回滚

`git revert <本提交>` + `systemctl restart ftseek-us`（纯展示层，回滚无数据影响）。

## 未做（留给后续）

- 日报/周报的阶段维度与独立阶段日报：本轮只落**推送层**；统计层改动随 RFC-086（阶段统计与报告）另批落地。