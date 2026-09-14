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
**追补（§八，用户裁「修复再提交」）**：同族第四条 —— 重启后实测"**哪些阶段该走 VIXY 代理**"
这条判据在 4 个生产者的每一处都写着、还分成两派，盘前同刻出现两个 VIX（执行门 14.71 /
看板 17.84）⇒ 收敛为单一入口 `vix_source.is_vixy_proxy_window`（§8.1 有事故依据）。

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

---

## 六、交付留痕（2026-09-14 17:0x，用户裁「按照建议执行」）

用户就"提交 / 重启 / 9-22 读数"三问裁「**按照建议执行**」⇒ 本 DEB 记录三项决定：

| 决定 | 内容 | 依据 |
|---|---|---|
| **提交** | **`41ae0675b`**（24 文件，+1548 / −67）。门禁标签 `[hotfix]`（staged 面含 `app/pipeline/order_executor.py` 触发高风险门；按仓内体例 f2f2333e3 标 hotfix，**本批不引入架构决策 ⇒ 决策记录是本 DEB 而非 ADR，不硬凑编号**）。未纳入：`config/unified_config.yaml`（UE 兜底换档，他批）与 `scripts/cutover_*.py` 等并行工作流在途产物。连带提交 `handoff.md` / `docs/debates/README.md`（经查 `README.md` **只含本批索引行**，`handoff.md` 连带切换基建批两条增量） | 本 DEB §二 回滚表 |
| **不重启** ~~（已作废）~~ | ~~`app/` 那 9 个文件**不在本批重启**：重启时机留给要一起上线的那批，避免未验收批单独上生产。代价显式记账：执行侧 VIX fail-close 的可达性在重启前仍在纸面~~ ⇒ **用户后续裁「重启观察」已覆盖**：2026-09-14 17:34:15 完成重启，见 §七（代价已解除、功能已实证） | 本批 §一 P0-1 + handoff §⑥ |
| **9/22 读数延后裁** | thinking-eval 到期读数先按 `brief_template_version` 切段；**切段后若有效轮次不足 10，届时另行提请用户裁"该读数还算不算数"**——改判据本批**不代裁** | §五.5 + DEB-20260910 §17.6 |

**本条为追补**：`handoff.md` 中本批条目头部的版号与追记（`已提交 41ae0675b`）系提交后追补，该追补文本**不在 `41ae0675b` 内**，随下一次提交入库（仓内 `handoff.md` 追补钩子体例）。

---

## 七、重启与观察（2026-09-14 17:33–17:55，用户后续裁「重启观察」⇒ 覆盖 §六 的"不重启"）

**执行**：`systemctl restart ftseek-us` 17:33:54 → 17:34:15（21s），`MainPID=1348442`（旧 1330923），**`NRestarts=0`**；冒烟 7/7 绿 exit=0；启动段 ERROR/CRITICAL/Traceback **0**；台账 pre/post/复核三次采样逐字相同（两库 `open_positions` 指纹与 14:23 基线同值）⇒ 零数据污染。**§六"不重启"决定已被用户裁覆盖**，其记录的代价（fail-close 停留在纸面）**已解除**。

**功能验收（只读探针，非推理）**：`MarketStateChecker._fetch_vix_data()` 正常路径 → **14.71**（VIXY×0.85，走新增的 `app/data/vix_source.py`）；**强制全源失败 → `None`** ⇒ `order_executor.py:3418` / `playbook_service.py:932` 的 `if vix is None: 拒绝新仓` **自 17:34 起真实可达**（本 DEB §一 P0-1 的修复目标达成）。加载一致性：进程启动时刻 17:34:15 晚于全部 `app/` 文件 mtime（≤16:12:46），且 `deploy_freshness_check.py` 由 `stale_drift=7465s` 转为两次静默 exit=0。

### §七.1 ⚠️ 新发现：盘前 VIX 口径分裂**并未消除，只是分裂对象换了一侧**（须另裁）

| 消费方 | 盘前取值 | 依据 |
|---|---|---|
| 执行门（`market_state` → `order_executor` / `playbook_service`）+ regime 侧（`market_regime` → router） | **14.71**（VIXY×0.85，新鲜代理） | 本批新增的 PRE_MARKET·TRADING 代理腿 |
| `app/risk/vix_monitor.py`（VIX Monitor 循环）→ `data/vix_cache/vix_data.json` → **看板 `data_service` / `day_score` / `order.thesis` / `data_health_check`** | **17.84**（FRED 日频，昨日收盘） | `vix_monitor.py:172-180`：**仅 `phase==TRADING` 走 VIXY-first；`PRE_MARKET` 走 FRED-first**（本批未动） |

**改动前后的分裂面对比（实测，不是推断）**：改前 `git show HEAD~1:app/analysis/market_state.py | grep -c "VIXY\|vix_source"` = **0** ⇒ 执行门与 `vix_monitor` 同为 FRED(17.84) **一致**、与 regime(VIXY) **不一致**；改后执行门与 **regime 对齐（14.71）**、与 **`vix_monitor`→看板（17.84）出现新分歧**。**净效果**：驱动仓位的那一门移到了"新鲜侧"（与仓位缩放同源），而**人看的看板仍显示 T-1 值**。两侧各有道理（盘前 VIX 现货不撮合：FRED = 昨日真收盘但陈旧；VIXY×0.85 = 代理但新鲜）⇒ **改任一侧都是消费者可见的行为变更，须另开辩论**；本批（`41ae0675b`）按"不改变调用方兜底/分支语义"原则**未动 `vix_monitor` 的 phase 顺序**（该文件在该提交里只做了 ×0.85 字面量 → 真源的逐字同值替换）。

> **已闭环**：用户随后裁「**修复再提交**」⇒ §七.1 的"须另开辩论"由该裁决就地满足，收敛方案与证据见 **§八**（`vix_monitor` 的 phase 顺序在 §八 已改；本文 §7.1 的"未动"仅描述 `41ae0675b` 时点的状态）。

**后续观察点**（本条未验，留待 2026-09-14 晚间）：19:40 `--da-feed`、20:00 DA v0.79 首轮、21:10 UE premarket 三段（核对 `brief_template_version=ue_brief_v4` 落库，并核对三条源渲染行的时点口径串首次出现在生产 brief 正文里）。

---

## 八、盘前 VIX 口径收敛（2026-09-14 18:2x，用户裁「修复再提交」⇒ 就地裁决 §七.1 的"须另开辩论"）

§七.1 明确写了"改任一侧都是消费者可见的行为变更，**须另开辩论**"。用户就此直接裁「**修复再提交**」—— 该裁决即为本次辩论，故**不另立 DEB**，方向按 §七.1 已列的证据定（不是偏好选择，见下表"方向依据"）。

### §8.1 根因：不是某处写错，而是**同一条规则写了四份、且分成两派**

§一 P0-1 收敛了"VIXY→VIX 的**换算系数**"（0.85 的唯一入口），但**漏掉了同等重要的另一半**："**哪些市场阶段该走代理**"这条判据当时仍散在 4 个生产者里，且分两派：

| 生产者 | 修前判据 | 盘前派别 |
|---|---|---|
| `app/data/market_data_router.py::get_vix_or_none` | `phase in (TRADING, PRE_MARKET)` | A 派（代理） |
| `app/analysis/market_state.py::_fetch_vix_data` | 同上（本批 §一 P0-1 新增） | A 派（代理） |
| `app/data/alphavantage_provider.py::get_vix_or_none` | `phase == TRADING` | **B 派（日频）** |
| `app/risk/vix_monitor.py::get_vix` | `phase == TRADING` | **B 派（日频）** |

⇒ 实测同刻两个 VIX：执行门/regime **14.71** vs `vix_cache`（看板）**17.84**（§七.1）。**方向依据**（既有事故，非口味）：2026-08-03 regime 精准性验证 —— 盘前 FRED VIXCLS 更新滞后 2–3 天，7/31 盘前生成的 **23 条指令全用 7/29 的 20.66**，与 SPY 实际波动 12.4% 脱节、污染 regime（`market_data_router.py:955-963` + obsidian `2026-08-03_regime_validation`）。事实基础：VIX 现货（CBOE 现金指数）**盘前不撮合**，VIXY 是 ETF、有盘前成交 ⇒ 盘前唯一"可用且新鲜"的 VIX 就是代理值。故 **B 派向 A 派对齐**。

### §8.2 改动（1 真源 + 4 消费方；判据/阈值零改动）

| # | 文件 | 改动 |
|---|---|---|
| 1 | `app/data/vix_source.py` | 新增 `VIXY_PROXY_PHASES = ("PRE_MARKET", "TRADING")` + `is_vixy_proxy_window(phase=None)`，**唯一判据入口**（只决定**尝试顺序**，不取数、不兜底）。取不到 phase ⇒ **fail-open 为 True** 且告警可见（只影响顺序，代理失败必继续回落 ⇒ 无害；反向 fail-closed 会在时钟坏时静默退回 T-1 陈旧值 —— 正是本族病灶形态，不能反向引入） |
| 2 | `app/analysis/market_state.py` | 代理腿条件由自带元组改为 `is_vixy_proxy_window()`（**行为不变**，只去重复） |
| 3 | `app/data/market_data_router.py` | 同上（**行为不变**） |
| 4 | `app/data/alphavantage_provider.py` | 顺序判据由 `is_trading` 改为 `is_vixy_proxy_window(phase)`；**cache TTL 仍按 `is_trading` 分档（60s/600s）刻意不动** —— TTL 管"多久允许再打一次 API"，窗口管"哪个源优先"，两件事；合并会把盘前 API 调用率抬 10 倍 |
| 5 | `app/risk/vix_monitor.py` | 两套 `if is_trading / else` 顺序链折叠为**一个 order 元组**（首个非 None 者胜），判据同 1；顺带更正类 docstring 的数据源优先级（原文只列 FRED/AV/Yahoo，漏 VIXY 且未写窗口） |

未纳入（明确列举，防"顺手改"）：① `app/risk/position_exit_monitor.py::_get_vix` —— **退出参数域，P0.5 明令不动**；其降级腿用 `vixy_close * 1.05`（与 0.85 口径相反、且 ~20% 偏高），**仅记录不改**，待退出域另开辩论时一并处理；② `app/plan/analysts/sentiment_analyst.py` 的 Yahoo `^VIX` 现货 —— 已在 §一 P2 标注口径，本次不动；③ `app/data/yahoo_provider.py` 委托 `VIXMonitor` ⇒ **自动继承**本次口径（无需改，但已确认）。

### §8.3 生效面 / 血溅半径（诚实列全，含"人读文本"那一处）

| 消费方 | 变化 | 性质 |
|---|---|---|
| 看板 VIX 数值（`web_dashboard/data_service`）、`data_health_check` 文本 | 盘前 17.84 → ~14.71 | **显示值变化**（人看到的值与系统决策用的值自此一致） |
| `web_dashboard/order/thesis.py`（两处 `ctx["vix"]` 展示） | 同上 | 显示值变化，无门控 |
| **`app/plan/day_score.py` 的 VIX 因子** | 17.84 落在 `(15,20]` ⇒ **无分支（0 分）**；14.71 `< 15` ⇒ **+10 分 + 追加"VIX=…低位，市场稳定有利趋势"/"可适度放大仓位追趋势"** | ⚠️ **本批最需盯的一处（见下）** |
|  其下游①：`us_pipeline/battle_plan_manager.py::check_trade` 的 `level=="AVOID" → 拒绝新仓` **硬门** | 盘前 score **抬高 10 分** ⇒ 在"FRED 说 15–20 / 代理说 <15"的日子可能**跨出 AVOID**（例：44→54）⇒ 原先被拒的新仓改为放行 | **门控行为变化**（方向：更接近真实波动，但确属放宽） |
| ⤷ 其下游②：看板 `routes/battle_plan.py` 的 `level` / `risk_budget_pct` / `daily_risk_budget` | `NORMAL(20%)` ↔ `STRONG(30%)` 之间可能移位（level 边界 75/60/45）| 给操作员的仓位预算建议变化（"看板是实盘手动操作台"，人会照此手操） |
| 执行门（`order_executor`/`playbook_service`）、regime 侧 | **无变化**（本已是代理派） | — |
| Finnhub 调用次数（AV / vix_monitor 盘前） | 成功时省一次 FRED；失败时多一次 Finnhub | 量级 ≤1 次/缓存周期（AV 600s / vix_monitor 180s） |
| 失败面 | 代理腿失败仍回落原链（FRED→AV→Yahoo，顺序不变） | **无新增失败面** |

**关于 day_score 那一处必须说清（本批唯一具有门控后果的副作用）**：
①**它不是纯展示**——`day_score` 的 `score → level` 既进 `battle_plan_manager.check_trade()` 的 `level=="AVOID" → 拒绝新仓` **硬门**，又进看板的 `risk_budget_pct`（20%/30%）/`daily_risk_budget` 建议值；盘前 +10 分是真实的判分位移（**方向为"更宽松"**：低 VIX 加仓建议 + 更少被 AVOID 拒）。
②**精度边界**：14.71 距 15 这条边界仅 **0.29（~2%）**，而 VIXY 代理自身误差量级 ±5–10%（backwardation 时更大）⇒ **"盘前 day_score 的 VIX 分档"从此对代理噪声敏感**。这**不是本次引入的**（旧的 T-1 值同样可能跨界，只是它的错法是滞后 2–3 天、更隐蔽），但它说明**该边界在盘前这一段被使用的合理性本身值得质疑**（判据是围绕"当日波动"设计的，喂给它的却是隔日值 —— 这一点在修前修后都成立）。
③**本批的处置**：按"策略判据/阈值零改动"原则，**不改** `day_score` 的 `vix_low=15` 等阈值、不改 level 边界、不改 `battle_plan_manager` 的门；只把这一处**列为本批最需盯的副作用**并写进 handoff 的观察点（重启后首个盘前 `day_score` 读数应记 `VIX` 分档与 level，供日后判断是否需要另开辩论）。**若用户认为"盘前 day_score 不该吃代理值"，那是一个独立的价值判断（= 让看板/评分留在 FRED 日频），须另开辩论** —— 本批不擅自代裁。

### §8.4 钉子与变异（"这道闸今天该红"已用变异实证）

新增 `tests/test_vix_proxy_window_single_source.py`（21 例）：窗口内容钉死、五阶段判定、字符串/时钟解析、fail-open 方向 + 告警可见、**四消费方一致性**（同阶段四个生产者取值必须同时由该判据决定）、**翻转判据四处必须一起翻**、以及两条源码棘轮（四文件必须调用唯一入口；`(MarketPhase.TRADING, MarketPhase.PRE_MARKET)` 这写法不得再出现在 `app/` 任何文件）。另更新 `tests/test_vix_single_source_contract.py` 的 `test_vixy_leg_is_guarded_to_premarket_and_trading` → `test_vixy_leg_uses_shared_proxy_window`（**不是删断言**：阶段窗口本身改由新文件的语义测试 + 四消费方一致性钉住，理由写在测试 docstring 里；该文件仍 21 例）。

**四刀变异全杀，且各杀不同钉子**（每刀跑后复原，复原后 42 例全绿）：

| 刀 | 变异内容 | 杀它的钉子 |
|---|---|---|
| A | `vix_monitor` 还原为 `phase == MarketPhase.TRADING`（= **修前行为**） | **四消费方一致性 [PRE_MARKET]**（当场红：两个值 14.71/17.84 并存）+ 判据唯一性 |
| B | `alphavantage_provider` 还原为 `if is_trading:`（= **修前行为**） | **四消费方一致性 [PRE_MARKET]** |
| C | 窗口收窄为 `("TRADING",)` | 窗口内容钉死 + 阶段判定 + 时钟解析（4 例红） |
| D | `market_state` 自带旧元组、不走真源 | 源码棘轮「不得自带阶段对」+ `test_vixy_leg_uses_shared_proxy_window` |

⇒ **A/B 两刀证明：这道闸在修前状态下会当场红** —— 即"今天该有的那道闸"确实存在了，而不是事后补的说法。

### §8.5 验证与回滚

- 钉子：新增 21 例（`test_vix_proxy_window_single_source.py`）+ 改后 `test_vix_single_source_contract.py` 仍 21 例 ⇒ 两文件 **42 passed**；受影响 18 套件（VIX/regime/router/AV/playbook data_collector/day_score/exit/entry-gate 等）**402 passed**；**全量套件 = 3 failed, 5110 passed, 41 skipped, 333.13s**（对比 §三 上一轮 5089 passed ⇒ **+21 = 恰为本批新增钉子数**；3 例失败与 §三/§四 逐例归因的**同三条**、均在本批文件之外，属并行工作流在途产物：`test_control_loop_ledger` / `test_dead_db_literal_gate` / `test_except_pass_ratchet`（959 > 951）——**未代改**）。
- 生效需重启：`app/` 五个文件改动，**重启后按 §七 同法复核**（探针 + 台账零污染 + 启动段计数），并**新增核对一条**：盘前 `data/vix_cache/vix_data.json` 的 `vix` 与 `market_state._fetch_vix_data()` 取到同一个值（口径一致性的生产实证，探针见 §七.1 表两行应同值）。
- 回滚：四个消费方各自还原为原 phase 比较、删 `is_vixy_proxy_window` + 删新测试文件。**回滚须另开辩论** —— 恢复分派等于让看板值继续与执行门值不一致（且盘前重新退回 T-1 陈旧口径，与 2026-08-03 事故的修法相反）。

### §8.6 重启生效与生产实证（2026-09-14 18:21:39 —— 追补，本节读数为实测）

- **重启**：18:21:33 → **18:21:39（6s）**，`MainPID` 1348442 → **1351070**，`NRestarts=0`；冒烟 `startup_smoke_check.py --wait 90` **7/7 绿 exit=0**；启动段（318 行）ERROR/CRITICAL/Traceback **0**（WARNING 7 条 = 既有 6 形态 + 1 条关机期新形态，见下）；台账 pre(18:21:33) / post(18:24:25) **逐字相同**（sim `bd7ceb90d4d672fa` / live `9c2f66d5d791bc16`，与 14:23 基线同值）⇒ **零污染**；`deploy_freshness_check.py` 手跑 **exit=0 静默**（重启前 18:10/18:20 两次 `stale_drift=1932s_outside_safe_window | newest=app/analysis/market_state.py` 检测到的正是本批未上线的代码 ⇒ 重启后漂移消失，互为旁证）。
- **口径一致性的生产实证（§8.5 新增的核对项，已取得）**：探针 A `MarketStateChecker._fetch_vix_data()` = **14.71**、B 全源失败 = **None**；且 **`data/vix_cache/vix_data.json` 的 `vix` 由 17.84（18:19:39，FRED 日频）翻为 14.71（18:26:56，VIXY 代理）** —— 即 §七.1 表格的两行**此后同值**，**分裂在生产上归零（不只是代码层）**。注意首轮 18:21:56 的循环检查因内存缓存仍在 180s TTL 内而**未触发取数**（故 18:19:39 的值多活了 5 分钟），至 18:26:56 缓存过期后按新顺序取到代理值。
- **副作用的生产读数（`day_score`，只读复算，非推理）**：修前口径 17.84 ⇒ `score=75 / level=STRONG / budget=30%` 且**无 VIX 行**；修后 14.71 ⇒ `score=85 / level=STRONG / budget=30%` + `VIX=14.7 低位，市场稳定有利趋势` / `可适度放大仓位追趋势`。**今日实例未跨档**（75 与 85 都是 STRONG），但**修前正好压在 75 这条线上** ⇒ 任何其它因子 1 分变动即翻转档位（与 §8.3 的告诫一致）。
- **遗留诚实性缺口（本批未改，供用户判断）**：该缓存写入的 `"source"` 是**常数字符串** `"fred/alphavantage/finnhub/fallback"` ⇒ **读它无法分辨该值来自 FRED 日频还是 VIXY 代理**（本次 17.84 ↔ 14.71 的切换在该字段上完全不可见）。这正是本 DEB 主题"口径不清的值被当实时值用"的同型残余；补 `source=<实际源>`（或 `caliber`）属**消费者可见的 schema 变更**（现有读者只取 `vix`/`timestamp`，故技术风险低），但**本批不做**（用户裁的是"修复口径分裂"，未含 schema 变更）——列为下一个可选增量。
- **一处新形态告警（非退化，如实记）**：18:21:37 旧进程**关机期** 1 条 `[调度器] 线程 PositionMonitorLoop 未能在 2s 内退出，跳过`（17:33 那次重启无此行，本次为新出现）。属**关机路径**且重启返回正常、启动 7/7 绿 ⇒ **无后果**；与仓内已登记的 futu 线程退出挂死族同源，**仅记录不处置**。