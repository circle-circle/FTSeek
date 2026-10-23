# DEB-20261023 DA 链路根因修复（可靠性 / 可观测性 / 数据真值）

- **状态**: 已实施（无交易门/仓位/止损止盈/频率变更）
- **触发**: 用户 2026-10-23「针对异常问题按照建议做修复，确保方案和建议能真正合理且能从根源上解决问题」
- **范围**: DA 载体、pipeline 契约、审计工具、数据源重试、bridge 重载、推送等待
- **不包含**: DA/FTSeek 执行口径统一、confidence/admission 阈值调整、watchlist extras 上限（均需单独裁决）

## 1. 实时证据（2026-10-23 批次）

| 事实 | 读数 |
|---|---|
| 载体启动 → 完成 | 08:00:59 → 08:50:13 ET（49m14s；历史最长 54m18s） |
| 分析规模 | 31 只；bridge 15 条（8 SHORT / 7 LONG）；DB 15 条 |
| 闸门 | BLOCK 16 / WARN 13 / PASS 2；无分析异常 |
| 输入降级 | TSLA/UNH/VZ 快照超时 → 正确 SKIP；4/31 覆盖率 <100% |
| 输入真值 | CCI Prompt 昨收 78.65→78.83；CCI 研报 5/5 同稿已省略；规范辩论状态生效 |
| 执行面 | 21:06 generate 将 BABA 以 `admission: NOTIONAL_TOO_SMALL(2185<2500)` 改为 SKIP；其余 14 条 DA 行未进入 generate（非 watchlist 或未通过 screener），仍可执行 |
| 假失败先例 | 10/12 carrier 报 `pipeline_json_parse_failed`，实际 bridge 20 条、DB 66 条已落盘 |

## 2. 已实施修复

### 2.1 DA 载体（`ops/hermes/da_main_carrier.py`，线上副本已同步）

- **preflight fail-soft**：`is_trading_day` / `watchlist` / `pullback_stats` 超时或异常不再杀死批次；watchlist 为空时降级 scanner-only，而不是直接失败。
- **实时可见**：批次启动即写 `status=running`，每 60s 写 `heartbeat(analyses_done)`；pipeline stderr 通过文件句柄实时落 `logs/da_pipeline_stderr.log`，不再等进程结束才写。
- **硬死线 + finalize**：`DA_CARRIER_DEADLINE_ET=0857`；deadline 前将 pipeline 超时收紧；超时/rc=124/rc≠0 时调用 `pipeline.py --mode finalize`，从当日 checkpoint 写 bridge/DB。
- **真值对账**：完成后读 bridge `trade_date/candidate_count` 与 DB 当日 DA 行数；stdout 解析失败但产物有效时记 `complete_unparsed_stdout`，不再误报失败。
- **单例锁**：`data/.da_main_carrier.lock`，防止双季/重试同批并发。
- **pullback**：恢复 `logs/pullback_shadow.log`；由 44 只串行改为 4 路并行（`PULLBACK_MAX_WORKERS`）。

### 2.2 DA pipeline（外部 skill 仓库）

- `--mode finalize --date YYYY-MM-DD`：只读 checkpoint，复用 Stage 6 writers，不扫描/不调 LLM；用于超时恢复。
- **decision_id fallback**：IC snapshot 写失败时生成 `DA-<date>-<symbol>-noic`，避免整份 bridge 因一条空 id 被判 invalid、DA 行降为优先级 0。
- bridge 写入改为 `_atomic_write_json`（tmp+fsync+replace）；返回候选/DB 计数供 carrier 对账。
- `_ckpt_save_raw` 二次写回合并修正后的 `collected.snapshot`，raw 不再保留修正前昨收；`prev_close_raw` 保留源值，`prev_close_raw_source` 由 `collect_snapshot` 在 1% 推导前写入；reconcile 幂等。
- ET 日期统一走 `_et_now()`（`_ck_date`、Stage 6、gate jsonl），移除月份近似 DST。
- checkpoint 策略指纹加入 `PROMPT_HASH` 与本批真值开关；同日换 Prompt 后重跑不再复用旧分析。
- 扫描打分 4 路并行且保序（`DA_SCAN_WORKERS`），仍受 `_OPEND_SEM=4` 限流。
- Finnhub `_finnhub_get` 增加 429/5xx/网络错误的有界退避重试（`DA_FINNHUB_RETRY_ATTEMPTS`）与并发上限（`DA_FINNHUB_MAX_CONC`）；`news_v2` 保留完整 `publish_time`。
- `_quality_effective` / `data_quality.source_failed` 为 additive 真值视图；不改变 `_quality`、prompt 或门。

### 2.3 FTSeek 侧

- `app/playbook/pipeline.py`：同日 bridge 被重跑/finalize 重写（mtime 变化）时允许刷新内存 adapter；未变化时保持 P0-2 一次加载语义。
- `scripts/da_bridge_dingtalk_push.py`：`--wait-da` 等主链终态（死线 09:27 ET），`sync=True` 同步推送后再写 `pushed` 台账；crontab 两行已加 `--wait-da`，备份 `data/crontab.backup.20261023_da_wait_fix.txt`。
- `scripts/da_chain_contract_audit.py`：research 渲染时间改认 `publish_time`；不再把 `PARTIAL_AGREE + challenger_agreed_raw=true` 当异常（规范状态才是裁决依据）。

## 3. 验证

```bash
# 外部 skill
cd /root/.hermes/skills/deep-stock-analysis
PYTHONPATH=scripts /opt/ftseek/FTSeek-US/venv/bin/python -m pytest tests -q   # 124 passed
# 主仓定向
cd /opt/ftseek/FTSeek-US
PYTHONPATH=. ./venv/bin/python -m pytest \
  tests/test_da_main_carrier.py tests/test_stale_bridge_guard.py \
  tests/test_da_bridge_dingtalk_push.py tests/test_da_chain_contract_audit.py \
  tests/playbook/test_da_candidate_selection.py tests/playbook -q          # 143+1218 passed
# finalize 集成（副本，不写生产 DB）
cp -a data/da_checkpoint/2026-10-23 /tmp/da_finalize_test/da_checkpoint/
cd scripts && /opt/ftseek/FTSeek-US/venv/bin/python pipeline.py \
  --mode finalize --date 2026-10-23 --output-dir /tmp/da_finalize_test \
  --no-db-write --no-pool-write --format json
# → analyses_count=31, bridge_candidates=15
```

## 4. 回滚

| 修复 | 回滚 |
|---|---|
| carrier 行为 | git 恢复 `ops/hermes/da_main_carrier.py` + 同步线上副本；`DA_CARRIER_DEADLINE_ET` 可放宽；`DA_CARRIER_HEARTBEAT_SEC=0` 关心跳 |
| pipeline finalize/fallback | git 恢复 external `pipeline.py`；finalize 仅在 carrier 超时/失败路径调用 |
| 扫描并行 | `DA_SCAN_WORKERS=1` |
| pullback 并行 | `PULLBACK_MAX_WORKERS=1` |
| Finnhub 重试 | `DA_FINNHUB_RETRY_ATTEMPTS=1` |
| bridge 同日刷新 | git 恢复 `app/playbook/pipeline.py` |
| push 等待 | 从 crontab 两行删除 `--wait-da`（备份见上） |
| 真值视图 | `_quality_effective`/`source_failed` additive，无门消费；删除即回退 |

## 5. 待用户裁决（本批未改）

1. **执行口径统一**：`run_playbook_generate` 只处理 watchlist；BABA 被 admission 拦下，其余 14 条 DA 行绕过同一实际尺寸复核。是否让所有 DA 指令在实际下单前统一过 admission/confidence 门？这会改变成交分布。
2. **WARN/confidence 语义**：WARN 将 confidence 降到 0.35，但 `confidence_sizing_enabled=false` 且非 watchlist 不过 supply gate，实际接近标签。是否只用于排序、还是应参与资格/仓位？
3. **watchlist extras 上限**：今日 19/31 来自 extras；并行化已释放时间，但 watchlist 扩容仍会线性推高时长。
4. **源失败计入 prompt/质量门**：本批只做 additive 有效视图；是否让 `_quality` 本身计入 source failed，会改变数据充分性路由与供给。
5. **重跑撤回语义 / 同日 stale 指令**：bridge union + DB UPSERT 不撤回已翻 SKIP 的旧行；需定义批次级对账规则。
