# DEB-20261128 · HK 采集器成分级时点/状态守卫（B2 · 单变量 correctness fix · 影子面零判定消费）

- **状态**：已落地。用户裁「同意，港股数据现阶段更适合候选发现与背景输入，按照建议执行」→「继续」；本批＝外部审计「港股采集器三处先硬化」之 **#2 指数新鲜不保证成分股新鲜**（#1 有限值＝B1 已落 `678c2cac1`；#3 字段语义＝B3 排队）。
- **范围**：`scripts/hk_market_context_collector.py` 单文件 +12/-0（仅 `build_tick_row` 股票 else 分支，含 4 行注释）。单变量＝**成分报价当日有效性**：镜像指数 stale 判据（`update_time` 日期==tick 当日 ∧ `sec_status=="NORMAL"`），非新鲜成分当日 `chg_pct` 标缺失（None，不归零）＋条件字段 `stale_reason`；原始 `last/prev_close/update_time/sec_status/turnover` 全保留（tick_rows 真值源可复算）。**全新鲜数据输出逐字节不变**（`stale_reason` 键仅非新鲜时出现）。
- **分类依据（为何无开关直接修）**：采集器 record-only 影子（RFC-218 §25），`DA_HK_CONTEXT` 未建未激活，零判定消费（不接 DA prompt/sizing/gates/方向，不下单不落库不推送）；cron fresh-import 无重启。观测质量 correctness fix（先例：RFC-171 §9 L1 报表正确性豁免影子；同批 B1 同类）。行为差异仅＝停牌/前日报价成分从「计入当日篮子」变「标缺失排除」，不改任何交易规则。

## 1. 病灶（隔离合成实证，pre-fix）

探针日志 `/tmp/rfc218_b2_pre_evidence.log`（真实函数，非桩；ts=2026-11-30T10:07+08:00）：

| 输入 | pre-fix 输出 | 危害 |
|---|---|---|
| 指数当日新鲜＋00981 报价 `update_time=2026-11-29 16:00`（前一日 +10%） | `is_stale_tick=False` 整 tick 放行；semis `eq=11.6667, n_available=3, breadth up=3, turnover=3e6` | 前一日报价冒充当日样本：篮子均值/breadth/成交额全被旧值污染，且计为「可用」 |
| 00981 当日报价但 `sec_status=SUSPENDED` | 同样计入：`n_available=3, eq=11.6667` | 停牌股无当日连续定价，+10% 可能是停牌前旧价，仍进当日篮子 |

**根因**：stale 闸 `is_stale_tick` 只核**指数**日期/状态（tick 级），股票分支无成分级校验；`aggregate_sector` 只看 `chg_pct is None`（B1 后加有限性），无法区分「当日新鲜」与「前日/停牌残留」。

**边界**：合成隔离复现，**非**证明周五真实快照已损坏（真实 32 股＋3 指数全新鲜，§3 幂等回归逐字一致）；生产命中次数未知（首个自然 cron tick 周一 2026-11-30 09:37 CST）。

## 2. 修复（股票分支单一逻辑变量）

`build_tick_row` 股票 else 分支，`turnover` 赋值后追加：

```python
reasons = []
if rec["update_time"][:10] != ts[:10]:
    reasons.append("quote_not_today")
if rec["sec_status"] != "NORMAL":
    reasons.append("non_normal_status")
if reasons:
    rec["chg_pct"] = None
    rec["stale_reason"] = "+".join(reasons)
```

- `stale_reason` 枚举 ∈ {`quote_not_today`, `non_normal_status`, `quote_not_today+non_normal_status`}（单位/量纲声明按军规 2026-09-02 写入契约 docstring；`n_available/n_missing`=count）。
- `update_time` 缺失/空串 → `""[:10] != ts[:10]` → `quote_not_today`（未知不算新鲜，不归零）。
- 下游自然排除：`aggregate_sector`/ADR 港股腿/day-doc 序列均已跳过 `chg_pct is None`（B1 既有路径），无需改聚合代码。

**设计取舍（留痕待研究复议）**：
1. **当日盘中临时停牌（intraday halt）也被排除**——镜像指数 NORMAL 判据，从严。原始值保留于 tick_rows 可复算；若后续研究证明该排除损失有效信息（如复牌后当日继续交易），另批再议放宽，不本批擅放。
2. **指数级 `is_stale_tick` 闸不动**（仍只核指数；成分 stale 不使整 tick 被跳，其他新鲜成分/指数照常记录）——保持「指数 stale→整 tick 跳」与「成分 stale→该成分缺失」两层职责分离。
3. **指数分支不受影响**（B2 限定股票 else 分支；指数新鲜度由 tick 级闸负责）。

**未动**：B1 有限值守卫、非正拒、round 4 位、FLAT_BAND_PP、时段闸、HH:MM 去重、MAX_TICK_ROWS、flock/原子写、fail-safe exit0、ADR 映射、UNITS、注册表、crontab。

## 3. 验证（全部 `PYTHONDONTWRITEBYTECODE=1`）

- 新契约 `tests/test_hk_collector_constituent_stale.py` **13 例**＋旧 `tests/test_hk_market_context_collector.py` **48 例**＋B1 `tests/test_hk_collector_finite_guard.py` **36 例**合跑 **97 passed**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_hk_collector_constituent_stale.py tests/test_hk_market_context_collector.py tests/test_hk_collector_finite_guard.py -q` → `/tmp/rfc218_b2_green.log`（变异还原后复跑同命令仍 97 绿）。
- 契约钉死：前日报价/停牌排除（eq/n_available/breadth/turnover 四通道）；双原因合并枚举；`update_time` 缺失→quote_not_today；全新鲜无 `stale_reason` 键；**指数新鲜不救 stale 成分**（核心审计点）；全 stale 篮子→None 非 0；tick 级闸仍指数限定；指数分支不受影响；n_available+n_missing 守恒；ADR 腿 None；序列排除 None 无 Infinity/NaN。
- **真实数据回归**：`data/shadow/hk_us_linkage/_verify/2026-11-27.json` tick_rows → `derive_day_doc` 幂等重算 == 存储 doc **逐字一致**（周五全新鲜→条件字段不出现；快照 gitignored 缺失时该例自动 skip）。
- **变异 3 组全红→还原 SHA 核验→绿**（`/tmp/rfc218_b2_mutation_summary.log`）：
  - m1 `if reasons:`→`if False:`（禁守卫）→ **8 红**（`_m1_red.log`）；
  - m2 删日期检查 → **6 红**（`_m2_red.log`）；
  - m3 删状态检查 → **4 红**（`_m3_red.log`）。
  - 每组还原后 SHA256 == post `f04eaf0930dfdcf9ed8ed6a60741d36d63176da3899f58abe22d737a8af459d8`。
- `py_compile` venv＋cron `/root/.local/bin/python3.11` 双解释器 exit0；cron 解释器 fresh-import 冒烟：前日报价→None+quote_not_today、SUSPENDED→None+non_normal_status、新鲜→10.0 无键、混合篮子 n_available=1 eq=20.0；`check_undefined_names.py` rc0。
- Batch A 契约复跑 **41 passed**（`/tmp/rfc218_b2_batchA_recheck.log`）。未跑全量 pytest/CI，不声称全绿。

## 4. 凭据 / 回滚

- pre SHA256 `4bbf3dd41c1ff002dbd4d4a02796b5e514cbe84aebcb1506bdc8284365dbdb27`（=B1 post；备份 `scripts/hk_market_context_collector.py.bak-20261128-hk-constituent-stale`，`*.bak*` gitignore 不入库）；post SHA256 `f04eaf0930dfdcf9ed8ed6a60741d36d63176da3899f58abe22d737a8af459d8`（post 副本 `/tmp/rfc218_b2_collector.post`）。
- 无 app/ 改动、无 config/.env.us 改动、无注册表/crontab 改动、**无重启**；DB 未触；零订单/零付费 LLM/零激活；obsidian_vault 与 entry_confirm 并行 WIP 未触碰未提交。
- **回滚**＝恢复备份（SHA 核验）或 revert 本批 commit；cron fresh-import 下一 tick 即生效，无需重启。

## 5. 未验证 / 后续单变量队列

- **收益增量未证（正确性≠盈利）**；港股定位维持用户裁决＝**候选发现与背景输入**，非方向信号。
- 生产自然 tick 未积累（首个周一 09:37 CST）；停牌/前日报价实际命中率未知；intraday-halt 排除的信息损失未量化（§2 取舍 1，研究后复议）。
- **B3**（字段语义：`open_chg_pct`=首观测、翻红翻绿=穿零非趋势反转、high/low=采样极值→改名「首观测/观测最高/观测最低」或接真实 OHLC 分列）另批；**C** 美股对应腿（先同公司可交易 ADR：比例/汇率/转换成本/OTC 可交易性；再固定少量行业映射）；**D** 研究预注册（固定 HK 截止/US 决策/可成交时点、对齐交易日、控制盘前/期货/共同消息、按日聚类、成本后增量与 MDE；感恩节次日前收区间差必须先统一）；**E** `DA_HK_CONTEXT` 注入 **BLOCKED**（n≥60 HK 交易日＋传导研究＋§10.4 单变量 A/B＋用户单独裁决，不与其他输入开关捆绑）。
- A3 独立性判据分歧（merge `_self_debate` vs gate/adapter `_fallback`）钉死待裁（Batch A DEB §2.1）；原审计 P1（日报/教训净额、EXT 推送失败登记、跨轮 pre-sizing、黑名单 warning）排队独立批，不在 HK 范围。
