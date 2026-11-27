# DEB-20261127 · DA 来源状态可见性（RFC-218 §27 · 呈现层正确性 · 默认 OFF 未激活）

- **状态**：已实施（影子就绪），激活 BLOCKED §10.4 单变量 A/B＋用户裁
- **授权**：用户裁「按照建议 继续执行合理优化落地」＋§15.7 输入修复队列（健康空财报 §26 之后的下一批单变量）
- **载体**：skill 仓 `/root/.hermes/skills/deep-stock-analysis/scripts/`（不在 git 跟踪）`config.py`＋`llm_synth.py`
- **开关**：`DA_SOURCE_STATUS_FULL`（默认 0；刻意不复用 `DA_CAPITAL_CODE_FIX`/`DA_SHORTK_NA`/`DA_EARNINGS_TRISTATE`/`DA_INPUT_FULL`）

## 1. 病灶（§15.7 锚点，行号已按当前代码核实）

`llm_synth._format_data_for_prompt`「## 数据源状态」段（RFC 旧锚 `564-581` → 当前 `594-611`）：

```python
for label, key in (("Futu新闻","news"),("Finnhub新闻","news_v2"),("研报","research"),("社区情绪","sentiment")):
    status = _list_source_status(collected.get(key), _source_statuses.get(key))
    if status == "failed": ...FAILED...
    elif status == "empty": ...EMPTY...
    elif status == "disabled": ...DISABLED...
```

**仅 4 个 list 源**。两类不可见：

1. **dict 型源无 FAILED/EMPTY 区分**：资金分布/基本面/分析师/内部人/公司概况/大盘/行业等 dict 源，
   采集失败（`_error`）与健康空（空 dict）在「数据源状态」段完全不可见。19 维审计虽列「缺失明细」，
   但 `_classify_data_sources` 把 `_error` 与空 dict **同归「缺失」**，混淆「采集失败」与「源成功但无结果」。
2. **渲染层丢弃事件完全不可见（核心病灶）**：`_render_capital_section`（`465-512`）OFF 门 `code==0`、
   ON 门 `_capital_dist_valid`（八字段）。provider 实写 `code=<ticker字符串>` → OFF 门失败 →
   正式「## 资金面」段丢弃（无分时 fallback 时整段丢弃，`return []`）。而 `_classify_data_sources`（`1310`）
   分类的是**原始 payload**（cap_dist 有信息量 → 判「完整」），**不是渲染决策** ⇒ 模型见「19/19 有效」
   却无资金段，**矛盾不可见**。§15.7「不在数据源状态、不在质量审计」即此**丢弃事件**未反映于任何呈现段。

**事实边界（不夸大）**：这是呈现层可见性缺口，**非已证明的生产误判事故**。资金修复 `DA_CAPITAL_CODE_FIX` 仍 OFF，
本批不激活资金修复、不改采集/计算/覆盖分类。生产命中次数（多少候选资金段被丢弃）未统计。

## 2. 渲染丢弃门盘点（只读核实，确定扩面范围）

| 源 | 渲染门 | 丢弃可见性现状 | 本批处理 |
|---|---|---|---|
| capital_distribution | `_render_capital_section` OFF `code==0`/ON 八字段 | 审计判完整却丢段，**完全不可见** | RENDER_DROPPED（同源门）＋FAILED/EMPTY |
| capital_flow | 同上 fallback 分支 | 同上 | FAILED/EMPTY |
| fundamentals / analyst_consensus / insider_transactions / insider_sentiment / company_profile / industry_peers | `isinstance(dict) and "_error" not in` | `_error`→丢段；审计列缺失但不分 FAILED/EMPTY | FAILED/EMPTY |
| market_context | 同上（子项 VIX 缺失已有显式行） | 整段 `_error`→丢；子项 VIX 已显式 | FAILED/EMPTY（顶层） |
| news / news_v2 / research / sentiment | 旧 4 源循环 | 已有 FAILED/EMPTY/DISABLED | **逐字节保留** |
| tech/cap/deriv_anomaly | `render_anomaly_section` 单一真源 | 已显式区分无异动/采集失败 | **排除**（已可见） |
| earnings_calendar | §26 三态开关 | §26 域 | **排除**（避免耦合） |
| snapshot / kline | 恒渲染 | 无丢弃 | **排除** |

## 3. 修复合同（只动呈现层，计算/采集/审计分类一行不动）

- `config.py` 新增 `DA_SOURCE_STATUS_FULL`（默认 0）。
- **OFF＝逐字节旧 4 源渲染**（ON 块由 `if getattr(config,"DA_SOURCE_STATUS_FULL",False)` 门控，OFF 不执行）。
- ON：helper `_source_status_full_notes(collected)` 向 `_status_notes` **只增**状态行：
  1. **资金分布 RENDER_DROPPED**：`_cap_heads = [s.split("\n",1)[0] for s in _render_capital_section(cap_dist,cap_flow)]`
     （**复用渲染单一真源**，不另算门）；`_formal="## 资金面" in _cap_heads`、`_fallback="## 资金面 (分时)" in _cap_heads`。
     cap_dist 有信息量（`_has_meaningful_data`）∧ ¬_formal → 披露：有 fallback→「仅呈现分时fallback」；无→「整段未呈现，模型实际无资金数据，禁止据此论断资金流向」。
     文案明示「审计'完整'指原始 payload」——**解释矛盾而非制造矛盾**。
  2. 资金分布/资金分时 FAILED（`_error`）／EMPTY（空 dict）。
  3. 其余 7 dict 源 FAILED（`_error`/非 dict）／EMPTY（空 dict）；ok 态不发行。
- **单一真源/互补不重复**：不引入第二套覆盖计数（无「X/N 有效」）；与 19 维审计互补
  （审计＝原始 payload 三级分类；本段＝呈现层可见性，FAILED≠健康空，RENDER_DROPPED＝审计完整但模型看不到）。
  RENDER_DROPPED 触发条件 `_has_meaningful_data(cap_dist)` 与审计「完整」判据**同源** ⇒ 仅在审计判完整时触发，口径自洽。
- **消费链现状（如实钉死，本批不改）**：挑战者 `_challenger_data_view` `_FULL` 白名单**不含「数据源状态」**
  （含「数据质量审计」）→ ON 新段挑战者仍不可见＝既有缺口，属 §15.7「挑战者事实一致性」独立批，本批不动白名单。
  溯源校验 full 模式从 prompt 全文提数字 → 新行纯状态文案无数字，不触校验。覆盖分类/采集器/计算行两态逐字一致。

## 4. 验证（全部 `PYTHONDONTWRITEBYTECODE=1`，可复跑）

| 项 | 结果 | 凭据 |
|---|---|---|
| 新契约 `tests/test_da_srcstatus_full.py` | 68 passed | `/tmp/rfc218_s27_newtests.log` |
| OFF 扫描（800 合成正交＋67 真实 OFF＋67 ON 隔离＋134 覆盖/审计不变＋ON 抽查） | checks=1071 fails=0 SWEEP_ALL_OK | `/tmp/rfc218_s27_off_sweep.log` |
| 变异(a) 同源门伪造 `_formal=True` / (b) OFF 门伪造 `if True` | 3 红 / 22 红→恢复 sha 核验→68 绿 | `_mutation_{a,b}_red.log`＋`_pre_mutation.sha256`＋`_restore_green.log` |
| DA 相邻＋§26 回归 | 1F/244P（唯一 F＝既有 PROMPT_HASH 漂移 `80176956bfbb≠c7444d67ce58`） | `_adjacent.log` |
| py_compile 双解释器／fresh-import／undefined names | exit0×2／`DA_SOURCE_STATUS_FULL=False`／rc0 | `_transform.log`／`_env_import_smoke.log` |

覆盖点：OFF 逐字节（合成×开关正交＋67 真实）；ON 只增「数据源状态」段其余逐字节不动（含覆盖统计/质量审计）；
dict 源 FAILED/EMPTY/非dict 各分支；资金 RENDER_DROPPED 整段/仅 fallback；与资金开关同源（fix-OFF 丢弃→披露，fix-ON 正式块→无披露）；
完整旧数据 ON==OFF==备份（不增噪）；与短K/财报三态/INPUT_FULL 互不牵连；挑战者白名单缺口现状钉死；回滚态删属性默认 OFF。

## 5. 收益边界与未闭合

- **无任何收益证据**（呈现层正确化≠盈利）；ON 态模型方向/分值行为变化未验证。
- 激活须 §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE），不与资金/短K/财报三态捆绑。
- 未闭合：挑战者「数据源状态」白名单缺口（独立批）；审计「缺失」混淆采集失败/健康空的**采集端**区分（采集器域，本批只呈现层补）；
  生产资金段丢弃命中次数未统计；全量 pytest/CI 未跑；既有 PROMPT_HASH 漂移未修（另案）。

## 6. 回滚

- `.env.us` 加 `DA_SOURCE_STATUS_FULL=0`（或删行）→ 逐字节旧渲染；或恢复备份 `*.bak-20261127-rfc218-srcstatus`（sha 核验）。
- 与资金/短K/财报三态/INPUT_FULL/EXT 各开关互不牵连。
