# DEB-20260914 · 盘前数据有效性审计与修复（DA / UE / Playbook 输入侧）

> **性质**: 审计取证 + 落地修复（`app/` + hermes DA skill 双侧改动，全部为**输入有效性**，
> 不改任何策略判据/阈值/门限）。
> **触发**: 用户裁「全部处理（含 P2）」——承接本轮审计（"DA/UE 都是盘前获取的数据，比如
> vix 等指标，有哪些数据是不准确的，比如需要开盘后才知道？请扫描和检查，确保输入的
> 信息和数据是合理和有效的"）。
> **DS 版本**: DA skill 0.78 → **0.79**（`SKILL.md` frontmatter + CHANGELOG + 仓内同步棘轮
> `tests/test_da_output_contract.py` 三处已同步；`PROMPT_HASH` **不变** `b6b8034c471a`，实测改前/改后一致）。
> **红线**: 本批不解锁任何执行开关、不动 admission/毕业门/熔断阈值。

---

## 零、一句话结论

盘前链路的**结构性问题是「时点不清的值被当成实时值用」**，而不是"某个数据源挂了"。
本批修掉三类确认病灶（**执行门 VIX 的 fail-close 是纸面的**、**DA 的 VIX 自 8/11 起结构性
恒缺却被覆盖审计掩盖**、**板块脉搏无日期且无新鲜度守卫**），并把"缺失/陈旧"从**静默**改为
**显式**（prompt 里写明、审计里具名、日志里可见）。策略面零改动 ⇒ 本批的效果是"让现有
判据看到真实的输入"，不是"改变判据"。

---

## 一、病灶清单（含实测读数，2026-09-14）

### P0-1 执行侧 VIX 门的 fail-close 恒不可达（守卫纸面化）

| 项 | 内容 |
|---|---|
| 病根 | `app/analysis/market_state.py::_fetch_vix_data` 末行原为 `return 20.0  # 合理默认值` ⇒ 该函数**永不返回 None** ⇒ `get_vix_cached()` 文档承诺的 "取不到返回 None (fail-close 友好)" **不可能成立** ⇒ `app/pipeline/order_executor.py`(下单前) 与 `app/playbook/playbook_service.py`(开仓前) 的 `if vix is None: 拒绝新仓` **恒不可达**。 |
| 后果 | VIX 源全挂时 VIXGuard 看到兜底 20.0 (<28) → 不拦截，即"看不到就放行"，与该守卫注释"宁可错过不可放错"完全相反。**同型先例：回撤熔断静默失效案**。 |
| 口径分裂 | 执行门走 FRED VIXCLS 日频（实测 09-14 序列最新仅到 09-10 ⇒ T-1/T-2 收盘值），而 regime 侧 `app/playbook/market_regime.py` 走 Futu VIXY×0.85 实时代理（2026-08-03 修复）⇒ 同一天的 VIX 门读两个口径，盘中 VIX 冲高在执行门上不可见。 |
| 修法 | ① 抽出单一真源 `app/data/vix_source.py`（`VIXY_CONTANGO_FACTOR=0.85` + `vixy_to_vix()` + `fetch_vixy_via_opend()`）；② `_fetch_vix_data` 增"★0 盘前/盘中 VIXY 实时代理"腿（与 router 同条件 PRE_MARKET/TRADING），末行改为**如实 `logger.warning(...)` + `return None`**；③ router / alphavantage_provider / vix_monitor 三处原复制的 `×0.85` 字面量改为引用真源（**收敛而非变更**，逐字同值）；④ 两处守卫分支加注释记录"2026-09-14 前不可达、现真实可达"。 |

### P0-2 DA 的 VIX 结构性恒缺，且缺失在覆盖审计里不可见

| 项 | 内容 |
|---|---|
| 实测 | `collect_snapshot("US.VIX")` → `{"_error": "empty"}`；`collect_snapshot("US.VIXY")` → `last_price 17.30, session=premarket`。**富途不供指数快照**（DEB-20260818-futu-capability-audit §89/§144），而 DA 的 `_get_vix()` 直查 `US.VIX` ⇒ 该维度自 8/11 起**恒缺失**。 |
| 掩盖机制 | `llm_synth._classify_data_sources` 把 `market_context`（spy/qqq/vix 三合一）**只要有 spy/qqq 有值就整项记 ok** ⇒ VIX 子项缺失在 19 维覆盖审计里**完全不可见**；而 prompt 里 "VIX>25 = 降低所有做多信号" 是模型引用 VIX 的依据。 |
| 修法 | ① `data_collector._get_vix()` 改走 `US.VIXY`×0.85，系数复用仓内真源（头部 `sys.path.append` 仓根，**不复制字面量**；导入失败如实 `_error`，宁可让审计报缺失）；② `_classify_data_sources` 对 `market_context` 逐子项校验，缺一即降级并具名（`market_context(vix缺失)`），**总维数仍 19**（口径不变）；③ `llm_synth` 渲染补口径标签「代理值: US.VIXY×0.85 校准, VIXY=$17.3; VIX 现货无盘前交易」，取不到时**显式写入「VIX **不可用** — 禁止在论据/可观测指标里引用任何 VIX 具体数值」**（同 DA-A1 行业 PE 中枢先例）。 |

**修复后实测**：`collect_market_context()["vix"]` = `{'current': 14.71, 'chg_pct': -4.74, 'proxy': 'US.VIXY', 'proxy_factor': 0.85, 'vixy_price': 17.3, 'caliber': '盘前/盘中实时代理 (VIX 现货无盘前交易)'}`（此前为 `{"_error": "VIX unavailable"}`）。

### P1-1 DA 板块脉搏：无日期标签 + 零新鲜度守卫

| 项 | 内容 |
|---|---|
| 病根 | `Pipeline.run_full` 只要 `data/sector_pulse.json` 存在就注入，标签 `盘中板块脉搏(15:30 ET)` —— **无日期**。而该文件由 `scripts/sector_pulse.py` 在**盘中**每 30min 覆写（cron CST 22:00-23:30 + 00:00-03:30 = **ET 10:00-15:30**）⇒ DA 08:00 ET 盘前读到的**永远是上一交易日**读数（周一跨周末 = 3 天前）；脉搏 cron 挂掉时 `sector_pulse.py` 自身"成功<6 块则保留旧文件"的逻辑会让旧文件长期留存。 |
| 后果 | 模型把上一交易日**尾盘**的板块强弱当成"今天盘中"的实时轮动，据此判个股板块顺风/逆风（同族：LIF 盘前盲区——时点不清的值直接喂给模型）。 |
| 修法 | 逻辑抽为模块级 `build_sector_context(scan_result, today_et=None, pulse_path=None)`（后两个参数**仅为可测性注入**，生产走默认值）：① 标签改为「上一交易日(YYYY-MM-DD)盘中板块脉搏」；② 日期必须等于**上一个已收盘交易日**（由 `trading_calendar.latest_trading_day` 算，跨周末/跨假日正确）才注入，否则丢弃 + stderr 具名报警；③ 板块涨跌段 fail-open 保留（日历故障不中断 DA 主链）；④ "昨日板块涨跌"→"上一交易日板块涨跌"。 |

### P1-2 Playbook 盘中证据：脉搏记录无新鲜度检查

| 项 | 内容 |
|---|---|
| 病根 | `app/playbook/pipeline.py::_gather_intraday_evidence` 读 `sector_pulse.json` 只记 `asof_et`（如 `"15:30"`）**不记日期**、**无任何新鲜度检查**。而 topup **盘前窗 ET 08:45-09:30** 读到的必然是上一交易日读数。 |
| 后果 | 该 evidence 落进 `raw_instruction.evidence`，供 S1 记分牌按"证据一致度"切片（**胜出者才升确认/否决门**）⇒ 陈旧读数被当成当日盘中读数参与切片，污染候选证据归因。 |
| 修法 | 只接受 `date == 当日 ET` 的脉搏（盘前窗必然为旧 ⇒ `sector_pulse_chg=null` + `pulse_stale_reason` 写明原因，不拿昨收读数冒充日内）；当日读数补记 `pulse_date` / `pulse_age_min`（供按陈旧度切片）。**本条 record-only，零交易行为变更**；`data_root` 参数化（默认仍生产 `data/`）以符合 conftest 的"测试禁写生产 data/"硬闸。 |

### P1-3 UE [市场佐证] 段：三处失实声明 + 全程无数据时点（2026-09-14 追加批）

用户裁「诚实化 + 补时点标签」。触发：P1-1/P1-2 修完后复查 UE 侧同名病灶，**全部结论用生产文件探针取证**
（冻结 `_today()` 后直接调用 `_gather_market_corroboration()`），不是读注释推断。

| 项 | 内容 |
|---|---|
| 病根① 时点错一个交易日 | 回退源 `universe_expansion_shadow.jsonl` 的 `date` 取**写入时刻的 ET 日期**（`universe_expansion_shadow.py` `date = _today_et_str()`，cron = **ET 00:30**），而那一刻取到的行情是**上一交易日收盘** ⇒ 实测记录 `date=2026-09-14` 装的是 **09-11（周五）**的板块涨跌；消费端把该采集日当数据时点透传 `published_at`。 |
| 病根② 模型看不到任何时点 | 渲染进 brief 的是光秃秃的 `轮入板块: XLK(Technology) +1.32%, XLI(Industrials) +1.07%` —— 模型无从判断是今日、上一交易日还是上周（同族：P1-1 的"无日期脉搏标签"）。 |
| 病根③ 失实声明（一） | docstring 写"每日盘前生成 (**07:42** ET)"，实为 DA 在 **ET 08:00** 写 ⇒ UE 的 **da_feed 早跑（ET 07:40）结构上永远早于生产者 20 分钟**，该窗次次落到 capture 回退（实测：冻结 `_today()=2026-09-14`，主源不命中，返回的是回退源格式 `XLK(Technology) +1.32%` ≠ 主源格式）。 |
| 病根④ 失实声明（二） | docstring 写"**周更**机械影子 capture"，实为 **ET 00:30 日更**（cron `30 12 * * 1-5`）。 |
| 病根⑤ 失实声明（三） | docstring 写"盘中脉搏 sector_pulse.json（若存在且新鲜）一并注入"、渲染标签 `盘中板块脉搏(实时)` —— 实测**生产两窗（ET 07:40 / 09:10）结构上永不成立**（脉搏由 `sector_pulse.py` 在 **ET 10:00–15:30** 写），唯一能命中"当日"的是 **ET 12:30 的 `--midday`**，而它是 **record-only 影子**（"不进 inject/DA供料/push"）⇒ **该输入从未进过任何生产指令**。 |
| 病根⑥ 静默吞错 | 脉搏分支 `except Exception: pass` —— 盘前窗日复一日被丢弃而无人可知（"静默死亡"族）。 |
| 病根⑦ 未来日期守卫缺口 | `_age_days > 4` 才拒绝 ⇒ **负数（未来日期）直接放行**，writer 时钟可疑时未来日期的数据会被当"最新"消费。实测：冻结 `_today()=2026-09-08`、capture `date=2026-09-14` ⇒ age=−6 ⇒ 放行。 |
| 修法 | ① 新增 `_prev_trading_day_str()`（走 `holiday_calendar.prev_trading_day("US", d)`，日历故障 fail-open 原样返回并留痕）把回退源的 `date` **还原成数据所属交易日**（`published_at` 随之正确）；② 三条源的渲染行**各自带时点口径**（`轮入板块 [DA 日更盘前轮动 … 盘前生成]: …` / `轮入板块 [机械 capture 回退源 … **数据为上一交易日 X 收盘涨跌**, 非今日盘中]: …` / `盘中板块脉搏 [当日盘中脉搏 (asof 15:30 ET)]: …`）；③ 主源不命中时打印"落 capture 回退"（替换可见）；④ 脉搏丢弃时打印具名原因（不再静默）；⑤ 未来日期一律拒绝 + 告警（与陈旧守卫对称）；⑥ 三处 docstring 失实声明改写为实测事实。 |

**刻意不做（保留 DEB-20260729 §十 的价值判断）**：**不放宽脉搏守卫**，盘前仍不注入上一交易日脉搏。
`DEB-20260729` §十 作者结论是「UE 死接线的一行修法 = 守卫放宽为"最近交易日"，但昨日尾盘脉搏对今日盘前
UE 边际价值低，**建议不修** —— pulse 的真归宿是盘中板块 veto(③)」。该"建议不修"**从未进入用户裁决点**
（当时获批的是 ①②③④，死接线只是附带披露），且 ③ 至今仍挂在参数冻结的保险丝后排队；本批只消除
**"宣称有而实际没有"的静默**（失实声明 + 无时点 + 吞错），**不改变**该价值判断，也不外扩材料。

**同批副作用（须诚实申报）**：本批改了 brief(user)侧渲染 ⇒ 升 `PROMPT_VERSION` **v2.12→v2.13**、
`BRIEF_TEMPLATE_VERSION` **ue_brief_v3→ue_brief_v4**；`PROMPT_HASH_V2` 只哈希 `SYSTEM_PROMPT_V2`
故**一字不变**（仍 `70e4680597af`、2582 字符）—— 这是同族盲区**第三次**出现。**thinking-eval 预注册 A/B
正在跑**（现 6/≥10 轮，最后一轮 09-11；输入就是 `_build_clue_brief()`），本批使其序列新增一个 brief 分段点：
好在 A/B 是**同 brief 双臂配对**设计（两臂同步移动 ⇒ 配对端点不受污染），且三指纹自 2026-09-13 起
已落进 `thinking_eval.jsonl`（现有 6 轮早于该基建故 `brief_template_version=None`）⇒ **9/22 到期读数须
先按 `brief_template_version` 分段**（v3/v4 不得混算）**再判"有效轮次≥10"**。判据本身（按 `prompt_hash`
切分）**不动** —— 改判据＝改尺子，须用户裁（DEB-20260910 §17.6 待裁项）。

### P2（已在同批处理）

- **DA 快照 T-1 口径标注**：盘前/盘后 `成交量/换手率/振幅/量比/买卖盘比` 均为**上一交易日收盘**累计口径（盘前不刷新），原只标了"当前价格"；现整组标注并指明"今日量能只能看盘前段的盘前成交量"。
- **Playbook 采集侧 VIX 取数口径**：`_get_vix_cached()` 原走 `AlphaVantageProvider.get_vix()`（失败兜底 `return 20.0` 假值；盘前/盘后走 FRED 日频）⇒ 改为走**执行门同源**的 `market_state.get_checker().get_vix_cached()`（盘前/盘中优先 VIXY 代理，取不到如实 `None`）；`SentimentData.vix` 默认值由数值哨兵 `20.0` 改 `None`（"缺失"与"真值恰为 20.0"从此可区分）；`vix_quality` 由 `vix != 20.0` 改 `vix is not None`（真值 20.0 不再被误判缺失）。
- **Fear&Greed 口径诚实化**：`app/plan/analysts/sentiment_analyst.py` 原 docstring 写 "CNN Fear & Greed" —— **不实**：`api.alternative.me/fng/` 是 **BTC/加密**口径。已在 docstring/返回字段（`index`/`is_equity_index`/`caliber_note`/`source`）与日志里写实，**方向覆盖逻辑一字未改**（改判定须另开辩论）。同处 Yahoo `^VIX` 标注 `caliber=spot_index`（现货无盘前交易 ⇒ 非盘中时段恒等于上一交易日收盘）。
- **零消费者采集已退役（核实，非本批新增）**：`data/macro_indicators.jsonl` 冻结在 2026-08-29、`option_chain` 同批停采 —— crontab 里两条已带 `# [2026-08-29 stop-zero-consumer]` 注释停用，全仓 `app/` 内**零读取者**。残留的旧数据文件（`macro_indicators.jsonl` / `macro_indicators_latest.json`）为孤儿产物，**本批不动**（删文件不在授权范围）。

---

## 二、改动清单与回滚

| 文件 | 改动 | 回滚 |
|---|---|---|
| `app/data/vix_source.py` | **新增**（换算唯一入口 + OpenD 取数） | 删文件 + 三处引用还原为字面量 0.85 |
| `app/analysis/market_state.py` | VIXY 腿 + 末行如实 None + regime 兜底注释/日志纠正 | `git checkout` 该文件（或按下方注释还原末行 `return 20.0`） |
| `app/data/market_data_router.py` · `app/data/alphavantage_provider.py` · `app/risk/vix_monitor.py` | `×0.85` 字面量 → 引用真源（**逐字同值**） | 同上（单文件 `git checkout`） |
| `app/pipeline/order_executor.py` · `app/playbook/playbook_service.py` | **仅注释**（记录 fail-close 由纸面变可达） | `git checkout` |
| `app/playbook/pipeline.py` | 脉搏新鲜度守卫 + `data_root` 参数化 | `git checkout` |
| `app/playbook/data_collector.py` | VIX 走执行门同源 + `SentimentData.vix` 默认 None + `vix_quality` 改判 | `git checkout` |
| `app/plan/analysts/sentiment_analyst.py` | 口径诚实化（docstring/返回字段/日志） | `git checkout` |
| hermes DA skill `scripts/{data_collector,llm_synth,pipeline}.py` | P0-2 / P1-1 / P2 三项 | `.bak-deb20260914-vix`（三个文件整文件还原；**回滚须另开辩论**——恢复即等于让 VIX 再次恒缺、脉搏再次无日期） |
| hermes DA skill `SKILL.md` + `references/CHANGELOG.md` | 版本 0.78 → 0.79 | 同步回改三处（含仓内棘轮） |
| `scripts/llm_universe_scan.py`（追加批 P1-3） | 新增 `_prev_trading_day_str()`（日历故障 fail-open）+ 回退源 `date` 还原为数据所属交易日 + 三条源渲染行补时点口径 + 替换/丢弃/未来日期均显式留痕 + 三处 docstring 失实声明改写 | `git checkout` 该文件（**注意**：回滚它必须同时回滚 4 个测试里的版号钉，否则棘轮红；PROMPT_HASH_V2 不受影响） |
| `tests/{test_ue_market_corroboration_caliber.py(新增), test_ue_contract_validator.py, test_ue_planned_preview.py, test_ue_planned_executability.py, test_u1_fwd_return_sign_contract.py}` | P1-3 契约 20 例 + 5 处版号钉 v2.12→v2.13 / ue_brief_v3→ue_brief_v4（哈希与长度两条钉**不动**，本批不碰 `SYSTEM_PROMPT_V2`） | `git checkout` + 删新增测试文件 |

---

## 三、验证证据

**新增测试 65 例全绿**（P0/P1/P2 四批 45 例 + P1-3 追加批 20 例）：

| 文件 | 例数 | 钉住的不变量 |
|---|---|---|
| `tests/test_vix_single_source_contract.py` | 21 | 换算唯一入口 / 三处原复制点无字面量（AST 棘轮）/ 全源失败如实 None（禁 20.0·18.0 哨兵）/ 代理腿限 PRE_MARKET·TRADING 且禁直查 `US.VIX` / fail-close 分支真实可达 / 显式 fail-open 语义不变 |
| `tests/test_da_sector_pulse_freshness.py` | 8 | 脉搏日期==上一已收盘交易日才注入、带日期标签、过期/未来一律丢弃且涨跌段 fail-open、**跨劳动节由交易日历算**、源码无日期化标签棘轮（含反向 mutation 验证） |
| `tests/playbook/test_intraday_evidence_pulse_freshness.py` | 7 | 非当日脉搏 null + stale_reason、不进 agree/avail、当日补 `pulse_date`/`pulse_age_min`、映射不上仍 null、record-only 不抛 |
| `tests/playbook/test_data_collector_vix_source.py` | 9 | 默认值非哨兵、走执行门同源、取不到 None（不回落 20.0）、**None 也进缓存**（防逐标的 OpenD 连接风暴）、真值 20.0 必须算 ok（回归闸） |
| `tests/test_ue_market_corroboration_caliber.py`（追加批） | 20 | 回退源 `date` 必须是**数据所属交易日**（含跨劳动节 09-08→09-04）、口径串必须写明"上一交易日…非今日盘中"、`published_at` 与渲染串同时点、**未来日期拒绝 + 陈旧拒绝**（回归闸）、主源口径与替换可见、当日脉搏带 asof / 上一交易日脉搏**不注入但留痕**、脉搏读坏不连带毁主源且不静默、`_prev_trading_day_str` 日历故障 fail-open、源码棘轮禁旧无口径标签回归（含反向 mutation 验证）+ 四条口径渲染片段必须在场 |

**回归**（P1-3 追加批落地后重跑，本机 2026-09-14 17:04，`pytest tests/ --ignore=tests/integration --ignore=tests/e2e -p no:randomly`）= **3 failed, 5089 passed, 41 skipped, 354.30s**，三例失败**全部与本批无关**且**全部来自并行工作流的在途产物**（见 §四.3，已逐例归因）：
`test_control_loop_ledger.py::test_crontab_loops_are_ledgered_or_baselined`（crontab 第 276 行 `cutover_preflight.py --watch --push` 未入台账）·
`test_dead_db_literal_gate.py::test_new_unconditional_live_db_site_must_be_declared` ·
`test_except_pass_ratchet.py::test_except_pass_not_growing`（959 > 951，**剔除那两个未跟踪文件后 = 949 ≤ 951**，即本批净贡献 **−1** 且余量为正）。
**本批锁定的定向批次**：`tests/test_ue_market_corroboration_caliber.py` 20 passed；受影响 UE 套件 370 passed；`tests/playbook/ tests/risk/ test_market_regime / test_playbook_service / test_da_* = 1203 passed`。

**实测取证**（本机、2026-09-14）：`US.VIX → _error: empty` vs `US.VIXY → 17.30 (session=premarket)`；DA `market_context.vix` 由 `{"_error": ...}` → `14.71`；Playbook `_get_vix_cached()` → `17.84`（float）；覆盖审计由"19/19 满"→ `19 总, ok 0, 降级 ['market_context(vix缺失)']`（构造缺失场景时）。

**P1-3 追加批的实测取证**（生产文件探针，不用读注释推断）：冻结 `_today()` 直接调 `_gather_market_corroboration()` ——
`_today()=2026-09-14` ⇒ 主源不命中（`data/deep_sector_rotation.json` 实际 date=**2026-09-11**）落回退源，回退源渲染格式 `XLK(Technology) +1.32%`（=capture 格式，非主源 `Technology(+1.4%)` 格式）；
`_today()=2026-09-11` ⇒ 主源命中且 **pulse=YES**（证明脉搏只在"文件 date == 当日"即 ET 10:00–15:30 窗成立）；
`data/sector_pulse.json` 实测 `date=2026-09-11, asof_et=15:30, mtime=09-12 03:30 CST`（=ET 09-11 15:30，与上面推论一致）。

**两道日志/吞错棘轮**：本批新增代码**零** `except:pass` 增量（`app/data/vix_source.py` 的 close 守卫与 VIXY 失败路径均改为 `logger.warning(..., exc_info=True)`），`tests/test_except_pass_ratchet.py` 与 `tests/test_w456_log_hygiene_ratchet.py` 对本批文件**零新增违反**。

---

## 四、刻意不做 / 未决（含本批发现的其它红）

1. **不动任何判据**：桌面化的 VIX 阈值、`vix_below_25` 的 fail-open、`get_vix_regime` 兜底 20.0（实测落 `(20,30)` ⇒ `caution`、仓位×0.7——原注释"默认正常"与边界不符，本批**只改注释不改行为**）、加密 F&G 覆盖美股方向 —— 全是"守卫/兜底语义"级别，改它们属于策略变更，须另开辩论。
2. **~~UE 侧脉搏守卫未动~~ → 已修（限"诚实化"半，2026-09-14 追加批见 §一 P1-3）**。
   原表述"`intraday_pulse` 实际上从未注入过 UE"**不精确**，更正为：生产两窗（ET 07:40 da_feed / 09:10 premarket）
   **结构上永不成立**，唯一能命中的是 ET 12:30 的 `--midday`，而它是 **record-only 影子**（不进
   inject/DA供料/push）⇒ **该输入从未进过任何生产指令**。修的是三处**失实声明**（docstring"07:42 ET"/
   "周更 capture"/"盘中脉搏…一并注入" + 渲染标签"盘中板块脉搏(实时)"）、**无时点**（三条源的渲染行都不带
   数据时点，且回退源的 `date` 是采集日、数据实为上一交易日收盘 ⇒ `published_at` 差一个交易日）、
   **静默吞错**与**未来日期守卫缺口**；守卫本身**按 DEB-20260729 §十 的价值判断保留不放宽**。
3. **本批无关的仓内既存红**（**非本批引入**，属并行工作流 `DEB-20260914-cutover-bidirectional-seamless`）：`tests/test_control_loop_ledger.py`（`scripts/cutover_preflight.py` 的 cron 未入台账）、`tests/test_dead_db_literal_gate.py`（`cutover_preflight.py:59` / `cutover_rehearse.py:45` 直引废弃库名）、`tests/test_except_pass_ratchet.py`（**+8 超基线全部来自** `scripts/cutover_preflight.py`(9) 与 `cutover_rehearse.py`(1) 两文件的 10 处；剔除后 959→**949 ≤ 951**。本批自身净 **−1**，超基线量占比 **0**）。**未代改**——那是另一条工作流的在途产物，介入会与其冲突。
4. **DA 侧仍未接 execution 侧同一实例**：DA 的 `_get_vix()` 走自己的 `collect_snapshot("US.VIXY")`（值同源、链路不同）。真正合一需要 DA 进程内引入 `app.data.vix_source.fetch_vixy_via_opend`（本批已把仓根入 `sys.path`，改动量很小），但 DA 的快照层另有盘前字段与连接风暴限流，合并须单独评估 ⇒ 留作下一步。
5. **`macro_indicators` / `option_chain` 孤儿数据文件**未删（见 §一 P2 末条）。

---

## 五、下一步（建议顺序）

1. 观察 DA 首次以 v0.79 产出的 bridge：核对 `market_context.vix.proxy` 在场、`data_quality_note` 不再把 `market_context` 记满分、板块脉搏标签带日期且**周一为周五日期**。
2. 观察 Playbook topup 盘前窗（08:45-09:30 ET）落库的 `evidence`：应见 `pulse_stale_reason` 非空、`sector_pulse_chg=null`；盘中窗（10:30-11:30）应见 `pulse_date=当日` + `pulse_age_min` ≈ 0-60。
3. 本批未覆盖的影子面：`raw_instruction.evidence` 的 S1 记分牌切片需在下一次读数时**按 `pulse_stale_reason` 分组**重算（本批只是让该分组第一次可用）。
4. **UE 侧（P1-3 追加批）**：下次 07:40 / 09:10 生产的 stdout 应出现 `[info] 盘中脉搏未注入 …`（证明这条输入从未生效、且现在可见）；若 DA 当日未产出轮动文件，还应出现 `[info] 主源 … 落 capture 回退`。**取证时务必核对 brief 正文里的口径串**（`data/shadow/llm_universe_scan.jsonl` 已落 `brief_template_version=ue_brief_v4` 与 `brief_template_hash`）。
5. **thinking-eval 9/22 到期读数（有到期日，优先级最高）**：先按 `brief_template_version` 把轮次切成 v3/v4 两段（现有 6 轮早于三指纹基建，`brief_template_version=None` ⇒ 属"**无从分段**"的既成事实，须如实标注），再判"有效轮次≥10"；**不得**把 v2.9→v2.13 的轮次混成同一序列宣布毕业（P0.5 明令禁止）。切分键要不要从 `prompt_hash` 改成 `brief_template_version` ＝ 改判据 ⇒ **须用户裁**（DEB-20260910 §17.6 待裁项）。