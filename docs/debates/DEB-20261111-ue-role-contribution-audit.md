# DEB-20261111 · UE 是否还有作用：现役角色与增量贡献只读审计

- 状态：已执行测量/契约收口；零交易行为变更。
- 触发：用户问「UE 是否还有作用？继续」。
- 范围：UE 直写轨、UE→DA 候选供料、DA prompt 注入、confirm/score 观察轨、11 月与 10 月 A9/A10 成交归因。
- 结论一句话：**UE 不再直接下单；仍作为 DA 候选/事件供料与观察轨存在。11 月独有候选有分析增量但无成交增量；10 月有正净额小样本，尚不足以判 edge。**

## 1. 现役消费者核查

| 面 | 现役状态 | 证据 |
|---|---|---|
| UE 直写 `inject` | 关闭 | `universe_expansion.entry_enabled: false`；C18 禁止重开；OKR 记录最后 UE 指令/成交 2026-10-05 |
| UE premarket fresh-event 当日写库 | 关闭 | cron 注释明确 2026-10-18 起不再串 `da_fresh_event_batch.py --execute`；脚本保留人工影子/回滚 |
| UE→DA `da_feed` | 开启 | cron `scan --da-feed` 08:00 ET；`da_candidate_feed: true`、`da_candidate_feed_long: true`；DA skill `_load_ue_short_candidates` 消费最新有效 `da_feed` 批次 |
| DA prompt 注入 | 仅 UE 独有候选 | DA skill Stage3 门控 `_source == "ue_candidate"` 且有 `ue_causal_chain`；重叠票只回填 `ue_*` 溯源，不改 `_source`，不注入 UE 判断 |
| `confirm` | 观察/影子 | cron 09:40 ET；`cmd_confirm` 只写 `ue_confirm_gate.jsonl`，不写指令、不下单 |
| `score` | 观察/评分 | cron 05:40 CST；只评 premarket scan，不评 `da_feed`，避免同日双计 |
| `sector_pulse` | UE 材料输入 | 盘中板块脉搏写 `data/sector_pulse.json`，UE `_gather_market_corroboration` 消费；非下单闸 |

## 2. 关键口径修正

此前 `ue_da_chain_health --limit 30` 的 `prompt_ue_context=0 / analysis_ue_context=1` 不能直接解读为「UE 注入断了」：

1. 旧 `latest_da_rows` 只取 `BUY/SELL_SHORT`，而 11-10 UE 独有候选 NVCR 最终是 `SKIP`，不在旧分母。
2. 重叠票 LLY 有 `_ue_batch_id/_ue_direction` 是溯源回填；按设计不进入 prompt。
3. 新审计按 checkpoint `_ue_origin is True` 且有因果链定义 prompt-eligible，并读 raw `user_prompt` 实测送达。

11-10 实测：NVCR raw prompt 含「事件扫描器(UE)」；LLY raw prompt 不含。11 月全量 UE 独有分析 18 个，prompt 送达 18/18，缺失 0。

## 3. 11 月贡献审计（2026-11-01 → 2026-11-10）

产物：`/tmp/ftseek_ue_contribution_20261101_20261110.json`。

| 指标 | UE 独有 | UE 重叠 | 非 UE |
|---|---:|---:|---:|
| DA analyses | 18 | 9 | 211 |
| prompt-eligible / prompt 送达 | 18 / 18 | 0 / 0 | 0 / 0 |
| 真指令（排除 supply_placeholder） | 5 | 3 | 129 |
| 可执行指令 BUY/SELL_SHORT | 2 | 3 | 110 |
| executed | 0 | 2 | 71 |
| A9/A10 已平成交 | 0 | 2 | 61 |
| 净额 | $0 | +$17.38 | +$237.16 |
| 净 bps | — | +17.38 | +8.24 |

UE 独有漏斗：18 分析 → 5 真指令 → 2 可执行 → 0 executed → 0 已平成交。另有 10 个 supply_placeholder、3 个无 DB 行。2 个可执行指令未成交原因：LITE `thesis_drift_invalidate`；CAPR `vwap_gate_blocked`。3 个 SKIP 中 INTT/IBRX/NVCR 均触发 `PRICE_BELOW_FEE_FLOOR`。

UE 重叠票 AMZN/LLY 的 +$17.38 是 DA 独立决策成交，**不能归因为 UE prompt 增量**；重叠票按设计不注入 UE 判断。

11-10 单日：UE scan 9 浮现，其中 LONG1/SHORT2/SKIP6；可执行候选 3（LLY、NVCR、XGN）。DA 分析 2：NVCR 独有 SHORT→SKIP，LLY 重叠 LONG→BUY；XGN 未进深分析。

## 4. 10 月对照（2026-10-01 → 2026-10-30）

产物：`/tmp/ftseek_ue_contribution_20261001_20261030.json`。

- UE 独有 A9/A10 已平：n=14，毛 +$361.97，费 $125.05，净 +$236.92，+29.76bps。
- 按日聚类：n_days=12，日均 +$5.69，t=0.27，MDE80=$58.94，CI80 正态近似 [-$21.29, +$32.67]。
- 判定：**n<30 且 |效应|<MDE，不可分辨；不得宣称 UE 供料已有稳定 edge。**
- 结构：FRO +$272.35、BKV +$91.92 贡献主要利润；NBIS −$147.97、WING −$86.43 是主要尾损。费用为 `fees_source=est`，非逐笔对账。

## 5. 已执行优化（零行为）

1. `scripts/ue_da_chain_health.py` 增加 `--date/--start/--end` 与 `ue_contribution` 段：
   - 只认最新有效 `da_feed` 批次；
   - 分离 UE 独有 / UE 重叠 / 非 UE；
   - 排除 `supply_placeholder`；
   - checkpoint 按 `_ic_decision_id` 连接真指令，避免同日同票多_runs 串账；
   - prompt-eligible 与 raw prompt 送达分开设列；
   - 成交按 `decision_id` 严格连接，A9 含/不含双口径，A10 现役时代，按日聚类 + MDE80。
2. 新增/扩展 8 个契约测试，覆盖窗口过滤、独有/重叠 prompt 门、指令分类、A9 双口径、decision_id 连接、小样本不可判。
3. 本 DEB + handoff 落档；未改 YAML/ENV/DB/cron/推送，未重启服务。

## 6. 建议与不做

- **保留 UE→DA 供料，暂不退役**：11 月无独有成交，但 10 月有正净额小样本；当前证据不足以支持「无作用」终裁。
- **不恢复 UE inject / fresh-event execute**：C18/RFC-148 已关闭直写轨。
- **不把重叠票 prompt 注入当 bug 修**：这是刻意的零行为溯源设计；若改成重叠票也注入 UE 判断，属于 DA 输入行为变更，须另立 RFC/影子/用户裁。
- **下一步只读优化候选**：UE 独有候选多次撞 DA `PRICE_BELOW_FEE_FLOOR`，可先做只读 what-if，量化「UE 供料预筛对齐 DA fee floor」会减少多少无效深分析；若改变候选分布，再走影子。
- **裁决门槛**：UE 独有成交 n≥30 前只报漏斗与描述性净额，不下 edge 结论；到 11 月 OKR 结算仍 n<30，则维持观察供料，不放大、不转硬闸。

## 7. 可复现命令

```bash
PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py --date 2026-11-10
PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py --start 2026-11-01 --end 2026-11-10 --json
PYTHONPATH=. ./venv/bin/python scripts/ue_da_chain_health.py --start 2026-10-01 --end 2026-10-30 --json
PYTHONPATH=. ./venv/bin/python -m pytest -q tests/test_ue_da_chain_health_analysis_coverage.py
./venv/bin/python -m py_compile scripts/ue_da_chain_health.py
git diff --check -- scripts/ue_da_chain_health.py tests/test_ue_da_chain_health_analysis_coverage.py
```

## 8. 验证 / 未验证

- 已验证：8 passed；py_compile OK；diff check OK；11 月/10 月只读审计产物落 `/tmp`；DB 连接均 `mode=ro`。
- 未验证：未跑全仓测试；未改 DA skill/prompt；未回补历史 DB；未验证未来 UE 独有成交样本；未调用券商。
- 部署：不需要重启；`ue_da_chain_health.py` 为 standalone 只读脚本，无 app/ops/cron 服务消费方。
