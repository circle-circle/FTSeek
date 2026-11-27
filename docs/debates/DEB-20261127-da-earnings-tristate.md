# DEB-20261127 · DA 健康空财报三态（RFC-218 §26 · 呈现层正确性 · 默认 OFF 未激活）

- **状态**：已实施（影子就绪），激活 BLOCKED §10.4 单变量 A/B＋用户裁
- **授权**：用户裁「按照建议 继续执行合理优化落地」＋§15.7 输入修复队列（短K §24 之后的下一批单变量）
- **载体**：skill 仓 `/root/.hermes/skills/deep-stock-analysis/scripts/`（不在 git 跟踪）`config.py`＋`llm_synth.py`
- **开关**：`DA_EARNINGS_TRISTATE`（默认 0；刻意不复用 `DA_CAPITAL_CODE_FIX`/`DA_SHORTK_NA`/`DA_INPUT_FULL`）

## 1. 病灶（§15.7 锚点，行号已按当前代码核实）

`llm_synth._format_data_for_prompt` 财报日历段（RFC 旧锚 `1019-1043` → 当前 `1083-1107`）：

```python
earnings = collected.get("earnings_calendar", {})
if isinstance(earnings, dict) and "_error" not in earnings:
    next_e = earnings.get("next_earnings")
    if next_e:
        ... 渲染「## 财报日历」...
```

`next_earnings` 为空（含 `_error`/键缺失）时**整段静默省略**。模型无法区分：

1. **窗口内未发现事件（源健康）**——有效信息（「未来 N 天无财报」对「财报前 3 日不开仓」约束是正向输入）；
2. **源失败/降级**——财报风险未知；
3. **未提供**——财报风险未知。

**生产分布实锤**（本批探针，`data/da_checkpoint/` 冻结归档）：

| 归档日 | 份数 | 健康空 | has_next |
|---|---|---|---|
| 2026-11-25 | 35 | 34 | 1 |
| 2026-11-27 | 32 | 31 | 1 |

**65/67 为健康空，OFF 全部无栏目**——主流状态被当缺失丢弃，与 DEB-20261119 审计「37/37 健康空财报无栏目」一致。

## 2. 采集端既有合同（只读核实，本批不改采集）

`data_collector.collect_earnings_calendar`（Finnhub 主源＋仓内 `EarningsCalendar` 备源）：

| 情形 | 返回契约 | 旧渲染 |
|---|---|---|
| 备源健康＋窗口内无该标的财报 | `next_earnings=None, _quality="ok", lookahead_days=7, _note=...` | 省略 |
| Finnhub 仅过期记录＋备源不可用 | `_quality="degraded", _degraded_reason=...` | 省略 |
| Finnhub 空应答＋备源不可用 | `next_earnings=None, all_upcoming=[]`（无标记） | 省略 |
| 两源皆失败 | `{"_error": ...}` | 省略 |
| 键缺失/非 dict | — | 省略 |
| 有未来财报 | `next_earnings={date,epsEstimate,...}` | 渲染 |

事实边界（如实记录，不夸大）：

- 备源 `get_earnings_calendar_with_status(horizon="3month")` 实际查 3 个月，但健康空标记**声明** `lookahead_days=7`（保守）。
  本批**只渲染采集器声明值**（7 天），不自行扩窗声称 3 个月；采集器不改。
- `collected["_timestamp"]`＝`collect_all` 起点 `time.time()`（采集时点，非逐源 fetch 时点）→ as-of 用它，ET 呈现，缺失/非法→N/A。
- 健康空 dict 因 `lookahead_days=7`（非下划线键、非零）已被 `_has_meaningful_data` 判「完整」——覆盖分类本就正确，
  缺的是财报段自身的状态呈现；本批不动覆盖分类。

## 3. 修复合同（只动呈现层，计算/采集一行不动）

- `config.py` 新增 `DA_EARNINGS_TRISTATE`（默认 0）。
- **OFF＝逐字节旧渲染**（旧块整体保留在 `elif` 分支，逐字未动）。
- **ON 且 `next_earnings` 存在＝亦逐字节旧渲染**（修复只针对空态）。
- ON 空态：新 helper `_earnings_tristate_section` 渲染「## 财报日历」状态段：
  1. `_quality=="ok"` → `状态: 查询窗口内未发现财报事件 (源健康, 这是有效信息, 非数据缺失)`
     ＋`查询窗口: 未来{lookahead_days}天`（int∧非bool∧1..366 才渲染，否则`未声明`，不编造）
     ＋`数据源`＋`as-of`＋含义行（窗口内无触发日期、不得编造日期、窗口外未知、不得推断长期无财报）；
  2. `_quality=="degraded"` → `财报日历降级 — 财报风险未知`＋原因（截 120 字）＋源＋as-of＋警示；
  3. 无标记空 → `财报日期未确认 — 无下次财报记录, 采集器未声明源健康`＋源＋as-of＋警示；
  4. `_error` → `财报日历源失败 — 财报风险未知`＋错误（截 120 字）＋as-of＋警示；
  5. 键缺失/None/非 dict → `未提供财报日历数据`＋警示。
  - 警示统一：`未知≠无财报: 不得据此认为财报风险已排除, 不得编造财报日期; 财报相关论据须标注数据不可用`。
  - 健康空＝已确认信息，**不带**「未知≠无财报」警示（语义区分）。
- **消费链零改动**（已核）：
  - 标题保持「## 财报日历」→ 挑战者 `_challenger_data_view` `_FULL` 白名单原样放行（无需改白名单）；
  - 溯源校验 `_verify_sourced_numbers` full 模式从 **prompt 全文**提数字 → 新行窗口/as-of 数字自动可溯源（无需改 `_extract_source_numbers`/校验器）；
  - 覆盖分类/`_has_meaningful_data`/采集器/计算行：两态逐字一致。

## 4. 验证（全部 `PYTHONDONTWRITEBYTECODE=1`，venv pytest）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约 `tests/test_da_earnings_tristate.py` | **71 passed** | `/tmp/rfc218_s26_newtests.log` |
| OFF 逐字节扫描：15 态×DA_INPUT_FULL×DA_SHORTK_NA×DA_CAPITAL_CODE_FIX（120）＋67 真实 checkpoint OFF＋67 ON（has_next 逐字节/空态只增段）＋65 段标题 | **checks=319 fails=0 SWEEP_ALL_OK** | `/tmp/rfc218_s26_off_sweep.log`（探针 `_off_sweep.py`） |
| 变异(a) 窗口伪造`未来30天` | **4 红** | `/tmp/rfc218_s26_mutation_a_red.log` |
| 变异(b) 健康空误判未确认(`_q=="okk"`) | **19 红** | `/tmp/rfc218_s26_mutation_b_red.log` |
| 恢复 sha 核验→复绿 | sha `e955386c03f0…` OK→**71 绿** | `/tmp/rfc218_s26_pre_mutation.sha256`＋`_restore_green.log` |
| DA 相邻 capital/output/fundamental/prompt_injection/shortk | **1F/173P**；唯一失败＝既有 PROMPT_HASH 漂移（backup 预载同断言同值 `80176956bfbb≠c7444d67ce58`） | `/tmp/rfc218_s26_adjacent.log`＋`_output_baseline.log` |
| py_compile venv＋python3.11 双解释器 | exit0×2 | `/tmp/rfc218_s26_transform.log` |
| fresh-import 真实 env | `DA_EARNINGS_TRISTATE=False`（短K/资金亦 False，INPUT_FULL=True 不变） | `/tmp/rfc218_s26_env_import_smoke.log` |
| `check_undefined_names.py` llm_synth＋config | rc0 | 本批命令记录 |

## 5. 部署 / 回滚

- 仓外两文件备份 `*.bak-20261127-rfc218-earnings`＋SHA256：config pre=`2ddbb5947c51` post=`a8db306f79f6`；
  llm_synth pre=`4b535772ed1b` post=`e955386c03f0`（pre＝§24 post，基线无漂移；备份=pre 逐字核验）。
- 变换在 DA 主批结束后执行（脚本 pgrep 自检拒跑）；**无 app/ 改动、无重启**（carrier/cron fresh-import）。
- `.env.us` **未加** `DA_EARNINGS_TRISTATE` 行（未设＝默认 OFF，生产逐字节不变）。
- 回滚：`.env.us` 加 `DA_EARNINGS_TRISTATE=0`（或删行）→ 逐字节旧渲染；或恢复备份（sha 核验）。
  与资金/短K/DA_INPUT_FULL/EXT 各开关互不牵连。

## 6. 收益边界 / 未验证

- **无任何收益证据**（呈现层正确性≠盈利）。ON 态模型方向/分值行为变化未验证；
  「健康空可见 ⇒ 模型更少误判财报风险」属 A/B 预注册指标，本批不测不声称。
- 激活须 §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE）＋用户批准，不与资金/短K捆绑。
- 健康空声明窗口 7d 与实际备源 horizon 3mo 的口径差＝采集器既有保守声明，本批只渲染声明值（未改采集）。
- as-of＝采集起点时点（非逐源 fetch 时点）；Finnhub 空应答无窗口声明→未确认态（不冒充健康空）。
- 既有 PROMPT_HASH 漂移未修（另案）；全量 pytest/CI 未跑，不声称全绿。

## 7. 队列（本批不捆绑）

- 输入修复各自独立批单变量：来源状态可见性、挑战者事实一致性（`_challenger_data_view`「数据源状态」不在 `_FULL`）、news UTC→ET。
- 待裁语义不擅自改：adapter 同日重跑、EXT dry=True 静默、watchlist fail-close、半日起点扩窗。
- BLOCKED 激活：资金 `DA_CAPITAL_CODE_FIX`、短K `DA_SHORTK_NA`、本批 `DA_EARNINGS_TRISTATE`、EXT_BLACKLIST_GUARD、DA_HK_CONTEXT。
