# PROPOSAL-20260909: daily_alert_summary 判级信任日志级别字段 (INFO 行不再被关键词抬成 WARN)

**状态**: 待用户裁决（判据变更，零代码已改）
**立案依据**: REVIEW-20260909-postclose-recap.md §F-4
**性质**: 判据变更（改「告警严重度」的判定规则），非行为修复 — 故先落档，裁决后实施

---

## 1. 病灶

`scripts/daily_alert_summary.py` 的判级（`parse_log_alerts` :184-192，`parse_log_alerts_window` 同型）用**关键词匹配**定级，不信任日志行的级别字段：

```python
level = "INFO"
if "ERROR" in line or "CRITICAL" in line or "异常" in line: level = "ERROR"
elif "WARN" in line or "告警" in line or "拦截" in line or "拒绝" in line: level = "WARN"
elif "熔断" in line or "失败" in line: level = "WARN"
```

后果：日志框架已明确标 `INFO` 的行，只要正文含「拦截/拒绝/失败/告警」关键词即被抬级为 WARN。

**实证**（9/9 复算，修后口径）：600 条 WARN 中含 `[再入场] … INFO: … 反手拦截` ×27 —— 这些是再入场反手闸的**正常工作留痕**（设计内拦截，INFO 级），被判级规则抬成 WARN 后占掉「警告」基数的 4.5%，长期会钝化 WARN 档的信号价值（狼来了效应）。

## 2. 判据变更点

| 维度 | 现行判据 | 提案判据 |
|---|---|---|
| 行内**有**日志级别字段（`] LEVEL:` 型） | 忽略级别字段，关键词可抬级 | **级别字段为准**：INFO/DEBUG 不抬级；WARNING/ERROR/CRITICAL 按字段 |
| 行内**无**级别字段（启动脚本 echo、print 直出） | 关键词判级 | 关键词判级（**保持不变**，兜底不丢告警） |

即：**显式级别信任，无级别才猜**。ERROR 档判定不变（现判据的 ERROR 分支本来就是级别字段主导）。

## 3. 影响面

- **WARN 基数**：预计 9/9 口径 600 → ~573（−27 条 INFO 反手拦截；其他 INFO 关键词行同理下沉为不计）。
- **ERROR 基数**：不变。
- **消费方**：仅 `daily_alert_summary.py` 输出（盘后告警汇总推送/台账）；无代码消费 WARN 计数做分支（已 grep 确认告警表走日志解析面，见 F-9 验证）。
- **不碰**：闸门行为、推送通道、noise 过滤（`_is_noise_alert` 不动）。

## 4. 预注册验证判据（实施时）

1. 同一份 9/8 会话日志，改前/改后各跑一遍 `parse_log_alerts_window`：
   - WARN 计数差 == 当窗 INFO 级关键词行数（可逐行列出差集，**全部差集行的级别字段必须是 INFO/DEBUG**）；
   - ERROR 计数差 == 0；
   - 无级别行的关键词命中**仍在**（抽 3 条启动脚本 echo 型行验证兜底不丢）。
2. `pytest tests/test_daily_alert_summary*` 全绿（现有契约覆盖窗口/去重，判级改动需同步更新契约期望值）。

## 5. 回滚

`git revert` 单提交即恢复（纯判级函数改动，无数据迁移）。

## 6. 裁决请求

- [ ] 批准方案（§2）：有级别信级别，无级别保留关键词兜底
- [ ] 或驳回：维持关键词抬级（理由：宁可 WARN 虚高不漏拦截事件）

## 7. 关联

- REVIEW-20260909 §F-4（病灶登记）、§C.3（Finnhub 429 ×68/日 与再入场反手闸 ×27/日 因判级修复才首次可见——本提案是把该修复的**判级副作用**收尾）
- F-7（Finnhub 429 攒样 3–5 日后再判，与本提案独立）
