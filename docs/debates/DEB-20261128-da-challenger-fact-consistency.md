# DEB-20261128 · DA 挑战者事实一致性（RFC-218 §28 · 呈现层可见性 · 默认 OFF 未激活）

- **状态**：已实施（影子就绪），激活 BLOCKED §10.4 单变量 A/B＋用户裁
- **授权**：用户裁「今日开盘，请核查继续下一步」＋「按照建议 继续执行合理优化落地」＋§15.7 输入修复队列（来源状态 §27 之后的下一批单变量＝挑战者事实一致性）
- **载体**：skill 仓 `/root/.hermes/skills/deep-stock-analysis/scripts/`（不在 git 跟踪）`config.py`＋`llm_synth.py`
- **开关**：`DA_CHALLENGER_FACT_CONSISTENCY`（默认 0；刻意不复用 `DA_CAPITAL_CODE_FIX`/`DA_SHORTK_NA`/`DA_EARNINGS_TRISTATE`/`DA_SOURCE_STATUS_FULL`/`DA_INPUT_FULL`）

## 1. 病灶（§15.7 锚点核实＋读码精化＋真实 checkpoint 实锤）

`llm_synth._challenger_data_view`（RFC 旧锚 `2592-2619` → 当前 `2776-2820`）按段标题白名单重排挑战者数据视图：

```python
_FULL = ("数据覆盖统计","标的基本面",...,"资金面",...,"行业对比","财报日历","数据质量审计","前序回吐率")
_TRUNC = ("最新新闻","研报","社区讨论")
for p in re.split(r'(?=^## )', data_text, flags=re.M):
    _head = p.lstrip()[:40]
    if any(t in _head for t in _FULL): out.append(p)            # 全量
    elif any(t in _head for t in _TRUNC): out.append(p[:600]+…)  # 截断
    # 其余丢弃
```

**「数据源状态」不在 `_FULL`** → 挑战者看不到分析师所见的源 FAILED/EMPTY/RENDER_DROPPED 状态。
信息不对称：分析师可据「Futu新闻 FAILED」判断弃权（SKIP）是否因缺材料、可反证「没有催化剂」类论断；
挑战者却看不到该段，无法核验分析师的弃权理由与源失败论据 → 低质量反证。

**真实 checkpoint 精化（67 份冻结 raw，10 份含「## 数据源状态」）**：OFF 态该段并非一律「丢弃」——
段首 40 字常含 `- 研报: FAILED` / `- 社区情绪: …`，与 `_TRUNC` 关键词「研报」「社区讨论」**子串碰撞**：

| OFF 形态 | 份数 | 机理 |
|---|---|---|
| 整段丢弃 | 9/10 | 段首 40 字无 `_TRUNC` 关键词（如 Futu新闻 在前）→ 未识别段丢弃 |
| 截断至 600＋标记 | 1/10（US.UNH） | 段首含「研报」→ 命中 `_TRUNC` → `p[:600]+…` |

即挑战者 OFF 要么看不到、要么只看到**被截断的残缺**源状态；分析师恒见全量。§27 已把分析师侧「数据源状态」
扩面至 dict 源＋渲染丢弃（`DA_SOURCE_STATUS_FULL`），但**挑战者侧白名单缺口未补**（§27 明确留独立批）。

**事实边界（不夸大）**：这是**呈现层可见性/信息对称缺口**，非已证明的生产误判事故；不声称「挑战者因此误判」。

## 2. 修复合同（只动挑战者可见性，采集/计算/分析师渲染一行不动）

- `config.py` 新增独立开关 `DA_CHALLENGER_FACT_CONSISTENCY`（默认 0）。
- `llm_synth._challenger_data_view`：`_FULL` 定义后、`_TRUNC` 前插入条件块
  `if getattr(config, "DA_CHALLENGER_FACT_CONSISTENCY", False): _FULL = _FULL + ("数据源状态",)`。
- **OFF＝逐字节旧挑战者视图**（条件块不执行，`_FULL` 不变；丢弃/截断/回退 `[:4000]` 逐字保留）。
- **ON＝只增「数据源状态」段全量放行**：`_FULL` 先于 `_TRUNC` 判定 → 该段无论是否含「研报/社区讨论」
  关键词都走全量（不再被截断/丢弃）。其余白名单段、`_TRUNC` 三段截断、未识别段丢弃、空视图/异常回退
  `[:4000]`、`DA_CHALLENGER_FULL_DATA=0` 旧截断**逐字节不动**。
- **隔离不变量**：ON 与 OFF 唯一差异＝「数据源状态」段（ON 全量 vs OFF 形态）；
  `on.replace(全量段, OFF段形态, 1) == off`（OFF 段形态＝丢弃""或截断 600）。67 份真实 checkpoint 全成立。

## 3. 两角色事实一致性核（§15.7「同事件版本/同时点价格/弃权是否因缺材料」）

- **同事件版本/同时点价格（结构保证）**：`synthesize` 内 `data_text = _format_data_for_prompt(collected)`
  为单一真源；分析师用 `data_text`，挑战者用 `_challenger_data_view(data_text)`（`_run_challenger(result, data_text, …)`）。
  两角色共享**同一** `data_text` → 白名单段（含 ON 后的数据源状态）逐字同源，事件版本/价格时点天然一致。
  契约测试钉死：ON 挑战者所见「数据源状态」段文本 == 分析师 prompt 中该段文本（合成＋67 真实）。
- **弃权是否因缺材料（可见性闭合）**：ON 后挑战者可见源 FAILED/EMPTY/RENDER_DROPPED → 可核验分析师
  `skip_reason` 是否对应真实缺材料、可据源失败反证「无催化剂」论断。本批只补**可见性**，不改挑战者评分/合并/门
  （`_merge_debate_results`/`signal_gate` 一行不动）；「挑战者实际更少误判/弃权更准」属 A/B 预注册指标，本批不测不声称。
- **news UTC→ET** 另案（不在本批）。

## 4. 验证凭据（全 `PYTHONDONTWRITEBYTECODE=1`）

| 项 | 结果 | 日志 |
|---|---|---|
| 新契约 `tests/test_da_challenger_fact_consistency.py` | **45 passed** | `/tmp/rfc218_s28_newtests.log` |
| OFF 逐字节扫描探针（合成 16 组×env2×5 开关正交＋67 真实 OFF==备份＋ON 隔离） | **checks=502 fails=0 SWEEP_ALL_OK**（real_n=67 with_status=10） | `/tmp/rfc218_s28_off_sweep.log`（探针 `/tmp/rfc218_s28_off_sweep.py`） |
| 变异（门控 `if getattr(...)`→`if True:`，OFF 泄漏全量段） | **8 红**（OFF 逐字节/丢弃/回滚等）→还原 sha 核验→**45 绿** | `/tmp/rfc218_s28_mutation_red.log`／`_restore_green.log`／`_pre_mutation.sha256`（post 副本 `/tmp/rfc218_s28_llm_synth.post`） |
| DA 相邻（capital/output/fundamental/prompt_injection/shortk/earnings/srcstatus）＋§28 | **1F/357P**；唯一失败＝既有 PROMPT_HASH 漂移 `80176956bfbb≠c7444d67ce58`（非本批引入，record-only 不修） | `/tmp/rfc218_s28_adjacent.log` |
| py_compile venv＋cron `python3.11` 双解释器×2 文件 | exit0×4 | `/tmp/rfc218_s28_transform.log` |
| fresh-import 真实 env＋`check_undefined_names.py` | `DA_CHALLENGER_FACT_CONSISTENCY=False`（资金/短K/财报三态/源状态亦 False）；rc0 | `/tmp/rfc218_s28_env_import_smoke.log` |

- 备份 `*.bak-20261128-rfc218-challenger`＋SHA256：config pre=`9436e985c2af` post=`e309cbe460c6`；
  llm_synth pre=`223216d70dae` post=`3783eaad24d2`（pre＝§27 post，基线无漂移；备份=pre 逐字核验）。
- §27 既有测试 `test_challenger_whitelist_status_section_absent_current`（钉死白名单缺口）随本批闭合更名为
  `test_challenger_whitelist_status_section_gap_closed_by_s28`：改钉「门控独立开关存在＋审计段仍在白名单＋默认 OFF 生产行为不变（非全量）」。
- 变换在 DA 主批结束后执行（脚本自检 pgrep 拒跑，00:33 CST 核无 DA 进程）。**无 app/ 改动、无重启**（carrier/cron fresh-import）；
  DB 未触；零订单/付费 LLM/激活。obsidian_vault 当晚 DA 归档 WIP 未触碰未提交。
- 本测试只验呈现层可见性正确，不证收益；未跑全量 pytest/CI，不声称全绿。

## 5. 收益边界（与 §9/§15/§22.8/§23.7/§24.5/§25.5/§26.5/§27.5 一致）

- **无任何收益证据**。呈现层信息对称≠盈利；ON 态挑战者 verdict/合并方向/confidence 行为变化未验证。
- 激活须 §10.4 单变量 A/B（n≥120/≥20 独立 ET 日/预注册 MDE），且不与资金/短K/财报三态/源状态/其他输入变量捆绑；
  「挑战者可见源失败」不得直接当「反证更准/更赚」的证据。

## 6. 回滚

- `.env.us` 加 `DA_CHALLENGER_FACT_CONSISTENCY=0`（或删行）→ 逐字节旧挑战者视图；或恢复备份（sha 核验）。
- 与 `DA_CAPITAL_CODE_FIX`/`DA_SHORTK_NA`/`DA_EARNINGS_TRISTATE`/`DA_SOURCE_STATUS_FULL`/`DA_INPUT_FULL`/EXT 各开关互不牵连。

## 7. 未闭合排队

- §25.7/§26.7/§27.7 队列：news UTC→ET 另案；事件预期差/价格响应/持仓期风险/执行可行性（用户优先补充二~五）属研究→可执行指令缺口，另案预注册。
- HK→US 联动（§25）：采集器已落地（指数＋8 行业篮子＋7 ADR 港股腿日内/breadth/翻红翻绿），首个真实 tick 周一 09:37 CST；
  DA 注入 `DA_HK_CONTEXT` 未建未激活，BLOCKED n≥60 HK 交易日＋行业级传导命中率研究＋§10.4 A/B＋用户单独裁决。
- 本批 ON 态「挑战者误判率/弃权准确率」前后对比属 A/B 预注册指标，本批不测不声称。
- 采集端「缺失」混淆采集失败/健康空属采集器域（§27 已留），本批只在挑战者呈现层补可见性，未改采集。
