# RFC-126: EXT 旁路归因收口 — did 写入 + admission verdict 留痕 (record-only)

- 日期: 2026-10-01
- 状态: 实施中 (用户 10/1 "同意" 采纳建议2后, 经取证修正为最小方案)
- 类型: L1 功能修复 (遥测/归因, **零交易行为变更**)
- 前置: RFC-124 §2.1 更正 (EXT 实弹运行证伪"已自堵"结论); 10/1 ACN SHORT 空 did 成交实证
- 回滚: revert 本提交 (did 为新增列写入, verdict 为新增 metadata 键, 均无消费者依赖)

## 1. 问题 (地面真值)

10/1 08:10 ET EXT 盘前旁路实弹成交 US.ACN SHORT 23股 (futu_order_id=8219132, FILLED_ALL):
- `trade_records.decision_id` 为空 → 体检/诊断/OKR 全链无法归因 (observe_nightly_supply P1 实证);
- 不写 playbook_instructions 指令行 → 统一入口 (admission 收口点 save_instruction) 完全绕过;
- EXT 与主链同标的方向冲突 (DA 给 ACN BUY conf=35, EXT 开 SHORT) — 靠重复持仓守卫
  (按 symbol 不分方向) 拦住对撞, 属侥幸而非设计。

## 2. 取证修正: "接 admission 费用门"增量拦截 ≈ 0 (不实施硬门)

用户原同意的建议2含"过 admission 费用门"。实施前按纪律核实真实数据:

| 判据 | 生产实值 (unified_config.yaml L1575-1596) | 对 EXT 历史 7 笔效果 |
|---|---|---|
| min_entry_price | $5.0 | EXT 自有 MIN_PRICE=$5 地板 (9/17 hotfix ce54f64eb, CTNT/GIPR 事故后用户裁方案A) — **同源同值, 再接=重复门** |
| min_notional_usd | $2500 | EXT 仓位定长 $5000 → 7 笔 notional 全 ≥$4816, **拦截 0 笔** |
| ACN 10/1 实测 | admission.evaluate → **PASS** (fee_bps=8.7) | 接门也拦不住 |

费用黑洞案 (CTNT 费$914 / GIPR 费$204) 均发生于 9/17 $5 地板上线**前**; 其后 5 笔
费用 ≤$10.5。**黑洞已在源头堵死** (堵它的是 $5 地板, 非影子开关 — RFC-124 §2.1 已更正)。

结论: 硬接 admission enforce = 无效复杂度 (违反"做减法/源头过滤>层层校验")。
改为 **verdict 留痕 record-only**: 统一口径可审计, 攒样后若数据显示需要再裁 enforce。

## 3. 实施 (最小改动)

### 3.1 did 归因 (scripts/extended_hours_entry_executor.py)
- `_execute` 成交路径生成 `did = f"EXT-{trade_date}-{symbol}-{session}"` (幂等: 同单重跑同 did);
- 透传 `_write_open_position` → `trade_records.decision_id` 列 (已存在, EXT 此前不写);
- `open_positions` 无 decision_id 列 — 不动 schema, position_id (EXT-uuid) 已可关联。

### 3.2 admission verdict 留痕 (record-only)
- 成交写库前调用 `app.playbook.admission.evaluate` (纯函数, 零 IO, 与主链同源同配置);
- verdict/reasons/fee_bps 写入 `rec["admission"]` (audit jsonl) + trade_records.metadata_json
  新增键 `admission_verdict/admission_reasons/admission_fee_bps`;
- **不改 direction, 不拦单** — EXT 无 TP 概念, ratio 判据天然不触发; 价格/名义门与既有
  $5 地板重叠, 预期 verdict 恒 PASS (若出现 REJECT = 新信号, 值得人工看)。

### 3.3 体检脚本 (scripts/observe_nightly_supply.py)
- 段4 空 did 成交 P1 规则升级: `EXT-` 前缀视为已归因 (显示为 ✅), 仍空 did 才 P1;
- 新增: EXT 成交的 admission verdict 汇总 (REJECT>0 时 P2 提示人工复核)。

## 4. 验证计划 (Gate 5)

1. 契约测试 (tests/test_ext_admission_attribution.py):
   - did 格式/幂等; verdict 落 metadata_json; REJECT 不拦单 (record-only 语义);
   - 既有 EXT 测试全量回归 (test_extended_hours_entry_executor.py 等)。
2. 9/17-10/1 历史 7 笔重放 admission.evaluate: 预期全 PASS (与 §2 取证一致)。
3. 明晨盘前窗口 (16-22 CST cron) 首笔 EXT 成交后: 体检脚本段4 应显示 EXT-did ✅。

## 5. 遗留 (不在本 RFC 范围)

- EXT 与主链同标的反向对撞目前靠 symbol 级重复持仓守卫兜底, 无方向感知 — 登记观察,
  攒冲突样本后再裁是否需要方向级守卫;
- EXT SHORT 实弹攒样判据按 RFC-099 §10 (每方向 ≥30 笔) 继续, 本 RFC 的 did/verdict
  正是攒样所需归因基础设施。
