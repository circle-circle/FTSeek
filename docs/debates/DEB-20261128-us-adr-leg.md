# DEB-20261128 · 美股 ADR 对应腿影子采集（C 批 · 单变量＝新观测通道 · record-only · 零判定消费）

- **状态**：已落地。用户裁决链：「同意，港股数据现阶段更适合候选发现与背景输入，按照建议执行」→「继续」×3（B1/B2/B3 采集器三处硬化完成后）→ 本批＝外部审计推荐顺序之 **第 3 步「补美股对应腿：先同公司可交易 ADR，再固定少量行业映射」** 的前半（同公司 ADR 4 只；行业映射另批）。
- **范围**：新增 `scripts/us_adr_context_collector.py`（297 行）＋`tests/test_us_adr_context_collector.py`（27 例）＋crontab +2 行＋本 DEB。**无 app/ 改动、无 config/.env.us 改动、无重启**（cron fresh-import）；DB 未触；零订单/零付费 LLM/零推送/零激活。
- **硬边界**：不接 DA prompt / sizing / gates / 方向；不下单不落库不推送。港股定位维持**候选发现与背景输入**；`DA_HK_CONTEXT` 未建未激活，E 注入 BLOCKED（n≥60 HK 交易日＋传导研究＋§10.4 单变量 A/B＋用户单独裁决，不捆绑）。

## 1. 探针双证据（真实 OpenD，2026-11-28 03:35 CST＝ET 周五 14:35 半日盘后）

1. **OTC 毒化整批**（`/tmp/rfc218_c_us_probe.log`）：TCEHY 混入请求 → `ret_code=-1`「暂不提供美股 OTC 市场行情 TCEHY」，**整请求失败**（非单票缺失）。⇒ OTC 票（TCEHY/MPNGY/XIAOMY）**绝不入请求**，只在文档 `otc_unavailable` 标状态；替代源须另行裁决（本批不采）。
2. **交易所上市 4 只 ret_code=0**（`/tmp/rfc218_c_us_probe2.log`）：BABA 89.68/prev 86.26、JD 27.35/26.38、BIDU 63.49/63.49、NTES 106.85/104.26；`update_time` 盘后仍更新（14:35 ET），格式 `YYYY-MM-DD HH:MM:SS.mmm`（日期闸 `[:10]` 可用）。
3. **ADS 比例凭据双源**：①公开 ADS 条款（BABA 1:8 自 2019／JD 1:2／BIDU 1:8 自 2021／NTES 1:5）；②真实港美股平价交叉验证——HK 周五收盘（09988=88.3／09618=106.2／09888=62.65／09999=167.6 HKD）× ratio ÷ US last 隐含 FX **7.766–7.894 ≈ 联系汇率 7.8 中枢**（JD/NTES 带内；BABA/BIDU 微超带＝美股盘后漂移；其余比例隐含 FX 远离 7.8，比例识别唯一）。活体 force 探针复算同值（§4）。

## 2. 设计与取舍

1. **复用 HK 采集器原语**（`from scripts.hk_market_context_collector import _atomic_write_json, _num, _observation_lock, chg_pct, derive_events`）：B1 有限值守卫／flock 锁内 RMW＋mkstemp/os.replace 原子写／穿零事件**单一真源继承**，不复制第二份实现。HK 采集器代码零改动（SHA `50bd9738…` 核验＝B3 post）。
2. **ET 时区闸**（`session_for`）：premarket 04:00–09:30／regular_open 09:30–10:15（含 09:45 入场参考锚）；周末→None。**用 ET 墙钟而非固定 CST 小时 ⇒ 冬夏令时自动适应**；cron CST 宽窗（16–23 时）＋闸拒窗外。半日市 13:00 ET 收盘在本窗之后不受影响；**美股假日**（ weekday cron 照跑）由成分日期闸→全 stale→`skipped_stale` 自然跳过。
3. **B2 范式成分级守卫**：`update_time[:10]==今日 ET ∧ sec_status=="NORMAL"`，违反→当日 chg=None＋`stale_reason` 枚举（quote_not_today／non_normal_status／合并／missing_from_snapshot），**不归零**，原始 last/prev_close/update_time/sec_status 保留可复算。`is_stale_tick`＝全 ADR 无当日新鲜报价才整 tick 跳（宁缺毋滥，镜像 HK 指数闸）；部分新鲜照常记录新鲜子集。
4. **B3 诚实 rollup**：first_obs／obs_high／obs_low／latest（采样观测语义）；**v1 不采 OHLC**——探针虽见 open/high/low 列，但盘前 high/low 语义歧义（是否含盘前？交易所口径？），接入＝新数据通道另批，不伪装。`derive_events` 穿零＝采样穿零非趋势反转（继承 B3 docstring 语义）。
5. **配对合同（研究层 D 消费）**：`date`＝**ET 交易日**（非 CST），与 HK 同 ET 日期 day-doc 配对（HK 收盘→美股盘前/开盘）。**chg_pct＝US 日窗口，与 HK 窗口不同，不得直接相减**；感恩节次日型前收区间差（HK 11-27 相对 11-26 vs US 11-27 相对 11-25）由研究层统一，采集层只如实记录双方 prev_close 原始值（`pairing_note` 落文档）。
6. **FX 声明近似**：`fx_hkd_per_usd_declared=7.8`（联系汇率中枢，仅供研究粗平价）；精确平价/转换成本研究（D）须用实时 FX。`shares_per_ads` 单位 count 入 UNITS 契约。
7. **注册表隔离（已核）**：`shadow_registry._count_samples` 用 `p.glob("*.json")` **非递归**（:5349），条目 5197 `hk_us_linkage_context_shadow` data_path＝`data/shadow/hk_us_linkage` 根 ⇒ `us_leg/` 子目录**不污染 HK 样本计数**（min_n=60 口径不变）。注册表描述补美股腿＝app/ 改动，**排队治理批**（本批无 app/ 改动无重启）。
8. **落盘**：`data/shadow/hk_us_linkage/us_leg/<ET交易日>.json`；MAX_TICK_ROWS=40（预期 ~26 tick/日＝盘前 22＋开盘窗 4，留余量）；HH:MM CST 去重；force→`_verify/` 子目录（不污染样本）；dry 零写盘；fail-safe 恒 exit 0。

## 3. 契约与变异（全部 `PYTHONDONTWRITEBYTECODE=1`）

- **27 例绿**（TestSessionGateET 5／TestTickGuards 6／TestDayDoc 5／TestRecordTick 11）：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_us_adr_context_collector.py -q` → `/tmp/rfc218_c_green.log`。钉死：ET 边界（04:00/09:29/09:30/09:45/10:14/10:15）＋DST 同墙钟＋周末；前日/停牌/合并/缺失四通道 stale＋原始值保留＋B1 inf/nan 继承＋全 vs 部分 stale；rollup 诚实命名（无 open_chg_pct）＋比例/FX/OTC 元数据＋穿零事件＋幂等＋单位声明；dry 零写盘／**OTC 绝不入请求**（requested==list(ADR_META)）／date=ET 非 CST／同分去重／cap／全 stale 跳／force→_verify／周末跳／error fail-safe／ret≠0 fail-safe／main 恒 0。
- **HK 四文件合跑 112 绿**（import 耦合不改 HK 代码）→ `/tmp/rfc218_c_hk_regress.log`。
- **变异 3 组全红→还原 SHA 核验→复绿**（`/tmp/rfc218_c_mutation_summary.log`）：m1 开盘窗上界 615→616（10:15 边界泄漏）→**1 红**；m2 删成分日期检查两行→**4 红**（前日/合并/全 stale 判定/整 tick 跳）；m3 BABA 比例 8→1（整行锚唯一，BIDU 同 8 不串）→**1 红**。每组还原后 27 绿（`_m{1,2,3}_red.log`／`_m{1,2,3}_restore_green.log`）。post SHA `9e06ff1e47886914e661ea9bd8ac8ab543cb119456e783bc9bd51549c76e62e6`（副本 `/tmp/rfc218_c_collector.post`；新文件批无 pre 备份，回滚＝删文件＋删 cron）。
- py_compile venv＋`/root/.local/bin/python3.11` 双解释器 exit0；`check_undefined_names.py` rc0（`/tmp/rfc218_c_undefined_names.log`）；双解释器 fresh-import 冒烟（ET 闸/stale/rollup/比例/OTC 隔离合成核，零写盘零网络）`/tmp/rfc218_c_import_smoke.py`。
- **活体 force 探针**（03:44 CST＝ET 周五 14:44 半日盘后）：`--force` → `us_leg/_verify/2026-11-27.json` written ticks=1，4 票全新鲜无 stale_reason，chg BABA+3.9648/JD+3.677/BIDU 0.0/NTES+2.4842，隐含 FX 7.766–7.894 复算一致（`/tmp/rfc218_c_force_probe.log`）。
- **cron +2 行**（458→460，diff 仅 +2；备份 `/tmp/cron_pre.bak`）：`7,22,37,52 16-23 * * 1-5`（CST 宽窗覆盖冬夏两季盘前+开盘窗，ET 闸拒窗外）＋`45 21,22 * * 1-5`（09:45 ET 入场参考锚，冬 22:45/夏 21:45 CST 双候闸拒一）。`docs/references/crontab-loop-baseline.txt` 无 HK/US 行，无需更新。

## 4. 测试环境两处教训（本批实遇，留痕防再犯）

1. **conftest autouse 夹具污染 tmp_path 根**：`_isolate_live_probe_waiver_file` 向每个 tmp_path 写 `live_probe_lock_waiver.json` ⇒ 「零写盘」断言不得用 `list(tmp_path.iterdir())==[]`，须用 HK 范式「尚不存在的子目录作 out_dir＋断言目录不存在」。首跑 3 红即此因（非采集器缺陷），修正后 27 绿。
2. **futu import 需 HOME**：`_snapshot_rows` 内 `from futu import RET_OK`，futu SDK import 期读 `os.environ["HOME"]` ⇒ 变异子进程精简环境（仅 PATH/PYTHONPATH）缺 HOME 致 7 例 TestRecordTick 共同假红（KeyError: 'HOME'），污染红计数。变异环境改完整 `os.environ` 副本后红因精准（1/4/1）。
3. 另删一处恒真冗行 `assert mod.main([...]) == 0 or True`（且可能在 ET 窗内触真实 OpenD）；`test_main_exit0_on_error` 已用 monkeypatch 正经覆盖 main 恒 0。

## 5. 未验证 / 边界（不声称闭合）

- **头号未验证：盘前快照可用性**。探针在常规/盘后时段；**若盘前 snapshot 的 update_time 不推进**（OpenD 盘前是否刷新待观察）→ 日期闸全 stale 跳 → 只剩开盘窗样本。周一 11-30 17:07 CST（＝04:07 ET）首个自然盘前 tick 揭示；若全 stale 须另裁盘前数据源（不擅改闸）。
- **收益增量未证（正确性≠盈利）**；HK→US 传导强度/时滞/可交易性未验证；自然 tick 未积累（首个周一 17:07 CST）；美股假日 stale 跳仅契约覆盖未活体。
- OTC 三票（腾讯/美团/小米）美股侧不可用已实证，替代源须裁；行业映射（港股半导体≠美国芯片同向）另批。
- 注册表条目描述补美股腿＝app/ 治理批排队；`DA_HK_CONTEXT` 注入维持 BLOCKED。
- **分支留痕**：本批提交时生产 checkout 在并行 agent 的 `fix/20261128-entry-confirm-integrity-race` 分支（P0-3，已重启部署），**未触碰该分支**；C 批经临时 worktree 基于 origin/main（`244705657`）提交推送 main。生产 checkout 内 C 两文件以 untracked 形式存在（cron 即用此路径，内容与 main 提交版 SHA 一致）；**该分支合并/切换 main 前须先移除 untracked 副本**（否则 git 拒绝覆盖；移除后合并即恢复 tracked 版，cron 15min 节奏窗口可忽略）。handoff.md 双方均顶部追加，合并时平凡冲突保留两条即可。
- 全量 pytest/CI 未跑，不声称全绿（既有基线不修：DA output_contract 1F／EXT 7F／playbook 18F／台账幽灵 job_id 1F）。

## 6. 回滚

`crontab /tmp/cron_pre.bak`（或删末尾两行）＋删 `scripts/us_adr_context_collector.py`＋`tests/test_us_adr_context_collector.py`（或 `git revert` 本 commit）；产物留档不删。cron fresh-import 即生效，无重启无配置项。不人工造单、不动方向/阈值/仓位/live 权限。

## 7. 队列（C 之后）

- **D 研究预注册**：固定 HK 截止/US 决策/可成交时点；对齐两地交易日（感恩节次日前收区间差先统一）；控制前一美股收益/当时盘前/期货/共同消息；按独立交易日聚类（一天 26 ticks≠26 样本）；成本后增量与 MDE——测「港股相对已有美股盘前信息的增量」非方向命中率。
- 小批：HK 真实开盘 open_price 持久化；US OHLC 接入（盘前语义先裁）；注册表描述治理批（app/→重启协议）；OTC 替代源裁决。
- 观察点：周一 09:37 CST 首个 HK 自然 tick（v2＋stale_reason）；周一 17:07 CST 首个 US 盘前自然 tick（头号未验证项）；05:00 Hermes 日报／06:02 EXT 半日分歧。
- 原审计 P1 四项（日报/教训净额、EXT 推送失败登记、跨轮 pre-sizing、黑名单 warning）排队独立批，不在 HK 范围。
