# DEB-20261127 · HK→US 时间差行业/动态趋势参考输入：影子采集先行（record-only），DA 注入未实施未激活

> 状态：**影子采集已落地**（采集器＋测试＋注册表＋台账＋cron）；**DA 参考输入注入未实施、未激活**（后续独立批）。
> 授权链：DEB-20261127-rfc218-s23 §4/§7#8「HK-US 联动采集器是否推进」待裁 → 用户裁（2026-11-27）：
> 「港股的波动趋势，不仅仅只是看个股，看行业和动态趋势，可以作为信息参考输入，打港股和美股的时间差」。
> 关联：RFC-218 §25、DEB-20261127-rfc218-s23（§4 预注册／§5 权限探针）、DEB-20261127-da-selection-direction-review-verification（DA 链路解释批）。

## 1. 本批裁决与依据

| 项 | 裁决 | 依据 | 变量类型 |
|---|---|---|---|
| HK 行业/趋势影子采集器 | **落地**（record-only，cron 已挂） | 用户裁承接 s23 §4 预注册第一步；权限探针 ret_code=0 已证数据面可行 | 新观测面（零判定消费） |
| 范围：单股 ADR → 指数＋行业篮子＋动态趋势 | **扩展**（用户明确「不仅看个股，看行业和动态趋势」） | 单日实证：半导体篮子 +14.86%（3/3 涨）vs 银行 -1.30%（1涨3跌），单看腾讯 +3.23% 会漏掉轮动 | 观测口径扩展 |
| DA 参考输入注入（`DA_HK_CONTEXT`） | **不做，未实施未激活** | 新输入变量假设，传导强度/时滞/可交易性未验证；须 n≥60 交易日＋传导研究＋§10.4 A/B | 待裁（后续独立批） |
| 美股侧 ADR 盘前/开盘反应采集 | **本批不采**（v1 只采港股腿） | OTC ADR 行情不可用已探针实证；交易所上市 ADR 美股侧采集属后续扩展 | 后续扩展 |
| 全市场 113 行业板块 breadth（plate API） | **本批不做** | 需 get_plate_stock 逐板块拉成分，配额面未探针；curated 8 篮子先行 | 后续扩展 |
| sizing / gates / 方向判定接入 | **任何阶段不接**（本批及激活前） | s23 §4 预注册硬边界；影子只攒序列证据 | 硬边界 |

## 2. 范围扩展：从单股 ADR 到行业/动态趋势（用户裁决的核心）

- s23 §4 原预注册仅「按 ADR 映射落港股日内 chg/量/时点」（7 组单股）。用户裁决明确**不止个股**：
  要看**行业**与**动态趋势**，作为信息参考输入打时间差。
- 本批采集面三层：
  1. **指数层**：恒生指数 HK.800000 / 恒生科技 HK.800700 / 国企指数 HK.800100（含 high/low chg，刻画日内振幅）。
  2. **行业层**：8 个 curated 等权篮子（32 成分股，2026-11-27 探针全 NORMAL）→ 等权 chg/中位数/离散度/breadth（涨/平/跌家数）/成交额。
  3. **动态趋势层**：日内 chg 序列（~24 tick/交易日）＋**翻红/翻绿事件**（相邻 tick chg 符号穿越 0）＋全日 rollup（open/high/low/latest，从 tick_rows 幂等重算）。
- **单日描述性实证**（2026-11-27 收盘 forced verify tick，`data/shadow/hk_us_linkage/_verify/2026-11-27.json`）：
  恒生科技 +6.45%、半导体篮子 **+14.86%（3/3 涨）**、科技互联网 +5.47%（8/8 涨）、 autos_ev +4.55%（5/5 涨），
  而银行 -1.30%（1涨3跌）、能源 -1.84%（1涨2跌）、电信公用 -0.40%。**科技/半导体强、银行/能源弱的行业轮动**——
  单看个股（腾讯 +3.23%）无法呈现该结构。此为单日描述性事实，**不构成传导强度证据**（见 §7）。

## 3. 采集合同（schema / 单位 / 闸 / 原子性 / 计数轴）

- 载体：`scripts/hk_market_context_collector.py::record_tick`；产物 `data/shadow/hk_us_linkage/<HK交易日>.json`（gitignored）。
- 数据源：**只读** OpenD `get_market_snapshot`（无订阅、无历史 K 线；权限探针 s23 §5 ret_code=0）。
- 单位声明（军规 2026-09-02，`UNITS` dict）：chg_pct/high_chg_pct/low_chg_pct/chg_pct_eq=percent；dispersion_pp=percent_points；
  turnover_hkd/price=HKD_absolute；breadth=count。
- 时段闸 `session_for`（CST=HKT）：早盘 09:30–12:00 / 午盘 13:00–16:00 / 两收盘锚 12:00–12:10、16:00–16:10；周末/窗外→None。
- 假日/停市闸 `is_stale_tick`：任一指数 update_time 日期≠今日 或 sec_status≠NORMAL → stale 跳过；指数全缺→stale（宁缺毋滥，不伪装样本）。
- 未知不归零：`chg_pct` 对 None/bool/非正 prev_close/非正 last → None；行业全缺→chg_pct_eq=None、n_available=0。
- 并发安全：`_observation_lock`（flock LOCK_EX）锁内读-改-写 + `_atomic_write_json`（mkstemp 唯一 tmp + os.replace）；HH:MM 去重 + MAX_TICK_ROWS=40 防刷屏（复用 §23 范式）。
- fail-safe：写盘失败/OpenD 不可达 → warning，进程恒 exit 0（不抛进 cron，不误报 error_alert）。
- **计数轴 = HK 交易日数**（每日 1 文档，ticks=int≥1 计 1，**不是 ticks 累加**）；`--force` 验证 tick 落 `_verify/` 子目录（注册表 glob 不递归 ⇒ 不污染样本计数）。
- `--dry-run` 全程零写盘（含 mkdir）；`--force` 绕过时段/假日闸落 `_verify/`。

## 4. 行业篮子与 ADR 映射（成分 / OTC 不可用 / parity）

- 8 篮子（等权）：tech_internet（8）/ semis（3）/ autos_ev（5）/ pharma_biotech（3）/ banks_financials（4）/ energy_resources（3）/ consumer（3）/ telecom_utilities（3）。
- ADR 港股腿 7 组：00700→TCEHY / 09988→BABA / 09618→JD / 03690→MPNGY / 09888→BIDU / 01810→XIAOMY / 09999→NTES。
  与 `app/catalyst/sources/hkex_source.py::HK_STOCK_MAPPING` **同源**（刻意不 import 防新闻面重依赖 requests/bs4/news_v2，parity 由契约测试钉死）。
- **OTC ADR 行情不可用**（2026-11-27 探针实证）：TCEHY/MPNGY/XIAOMY OpenD 报「暂不提供美股 OTC 市场行情」⇒ 只标 `us_quote_status=OTC_UNAVAILABLE`，不采美股侧；
  交易所上市 ADR（BABA/JD/BIDU/NTES）标 `EXCHANGE_LISTED_DEFERRED`（美股侧采集后续扩展）。

## 5. 激活路径预注册（DA 参考输入注入，本批不实施）

1. **攒样**：n≥60 独立 HK 交易日观测（注册表 min_n=60，USER_ADJUDICATION，无自动转正门；观测数据本身不是待裁料，不首命中推送）。
2. **行业级传导命中率研究**（预注册，读数不得事后改）：
   - HK 行业日 chg（等权篮子收盘 chg）vs 美股**同源行业代理开盘方向**（open vs prev_close）；
   - 大幅波动子集 |HK 行业 chg|≥2%；
   - 对比**无条件基线**（美股代理无条件开盘方向命中率）；
   - MDE 预注册；通过后才谈接入 DA 输入。
3. **DA 注入**：独立开关 `DA_HK_CONTEXT` 默认 OFF（本批**未建该开关、未改 DA**）；激活须用户单独裁决 + §10.4 单变量 A/B（不与资金分布/其他输入变量捆绑）。
4. **美股行业代理映射**（预注册，v1 **不采集**，供研究阶段对齐）：
   tech_internet→QQQ/中概ADR；semis→SOXX/SMH；autos_ev→TSLA/LI/NIO/XPEV；pharma_biotech→XBI/BGNE；
   banks_financials→XLF/SPY；energy_resources→XLE；consumer→XLY/NKE；telecom_utilities→XLU。
5. 任何阶段不接 sizing/gates/方向判定；回滚=删 cron 两行。

## 6. 时间差机制（港股白天波动 → 美股开盘的可用窗）

- 港股收盘 **16:00 CST**（=HKT）；DA 主批 **21:00 CST**（EST）/ 20:00 CST（EDT）；美股开盘 **22:30 CST**（EST）/ 21:30 CST（EDT）。
- HK 收盘 → 美股开盘 ≈ **6.5h（EST）/ 5.5h（EDT）**；DA 在 HK 收盘后 ~5h（EST）/~4h（EDT）、美股开盘前 ~1.5h。
- 含义：港股白天的行业/趋势信息在 DA 运行时**已完整可用**，理论上可作为美股开盘前的参考输入。
  本批只把该信息**采集落档**（影子），**不注入 DA**；是否注入、注入后是否有增量，须 §5 研究＋A/B 证明。
- 港股时段与美股盘前/盘后重叠（s23 §4）：ADR 港股日内动量对美股 ADR 开盘价有理论传导，但**强度/时滞/可交易性未验证**。

## 7. 收益边界

- 本批**无任何收益证据**，纯观测面新增（record-only）。单日行业轮动（§2）只是描述性事实，**不构成 HK→US 传导强度/可交易性证据**。
- 传导命中率、时滞、经济增量均属 §5 预注册研究＋§10.4 A/B 的待验指标，本批不测不声称。正确性/观测 ≠ 盈利。

## 8. 部署凭据

- cron 两行（系统 crontab，CST=HKT）：`7,22,37,52 9-11,13-15 * * 1-5` + `2 12,16 * * 1-5`，载体 `scripts/hk_market_context_collector.py`，timeout 120，日志 `logs/hk_market_context.log`。
  分钟错峰避开 */5 三族/error_alert/skill 测试门（11:02）；:07/:37 仅与 bash health_check 同分（轻量）。首个真实 tick=周一 2026-11-30 09:37 CST（港股周末休市）。
- 装机前备份 `/opt/ftseek/backups/crontab-20261127-221103-pre-hk-context.txt`（447 行）；diff 仅新增。
- 台账 `docs/CONTROL-LOOP-LEDGER.md` §2.2 `crontab-hk-market-context`；注册表 `hk_us_linkage_context_shadow`（OBSERVING/min_n=60/USER_ADJUDICATION/intent=hk_us_linkage_context）。
- 测试/变异/挂载/冒烟凭据见 RFC-218 §25 与 handoff 同日条目。

## 9. 待裁 / 后续扩展（不在本批实施）

1. DA 参考输入注入（`DA_HK_CONTEXT`）：BLOCKED §5 研究＋§10.4 A/B＋用户单独裁决。
2. 美股侧 ADR 盘前/开盘反应采集（交易所上市 ADR）：后续扩展，须另探针配额。
3. 全市场 113 行业板块 breadth（plate API 逐板块成分）：后续扩展，配额面未探针。
4. 行业篮子成分股动态维护（curated 静态快照，成分变更须重探针）。

**最后更新：2026-11-27。**
