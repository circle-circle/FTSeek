# DEB-20261128 · HK 采集器有限值入口守卫（B1 · 单变量 correctness fix · 影子面零判定消费）

- **状态**：已落地。用户裁「同意，港股数据现阶段更适合候选发现与背景输入，按照建议执行」；本批＝外部审计「港股采集器三处先硬化」之 **#1 非有限数值可进入聚合**（#2 成分时点/状态、#3 字段语义另批排队）。
- **范围**：`scripts/hk_market_context_collector.py` 单文件 +9/-5（含 `import math`）。单变量＝**有限有效性**：NaN/±inf 在价格、涨跌幅、成交额入口统一标缺失（None），不输出 Infinity/NaN，不归零，不伪装有限值。**有限值行为逐字节不变。**
- **分类依据（为何无开关直接修）**：采集器是 record-only 影子（RFC-218 §25），`DA_HK_CONTEXT` 未建未激活，零判定消费（不接 DA prompt/sizing/gates/方向，不下单不落库不推送）；cron fresh-import 无重启。属观测质量 correctness fix（先例：RFC-171 §9 L1 报表正确性豁免影子）。行为差异仅＝非法值从「伪有限/Infinity」变「缺失 None」，不改任何交易规则。

## 1. 病灶（隔离合成实证，pre-fix）

探针日志 `/tmp/rfc218_b1_pre_evidence.log`（真实函数，非桩）：

| 输入 | pre-fix 输出 | 危害 |
|---|---|---|
| `chg_pct(inf, 100)` | `inf` | Infinity 进序列/rollup/落盘 JSON |
| `chg_pct(100, inf)` | **`-100.0`（伪有限）** | 最危险：看起来合法的 -100% 进篮子均值/breadth down/翻绿事件 |
| `chg_pct(nan, 100)` | `nan` | NaN 进聚合；且 NaN 两比较均 False → 误计 breadth **flat** |
| `chg_pct(1e308, 1e-308)` | `inf` | 两输入均有限为正，比值溢出仍产 Infinity |
| `_num(inf)` | `inf` | 旧守卫 `v == v` 只防 NaN 放 inf；last/turnover/high/low 全通道 |
| `aggregate_sector`（inf 成员） | `eq=Infinity, dispersion=Infinity, breadth up=3, n_available=3, turnover 含 inf 成员` | 行业篮子被单一非法值整体污染，且计为「可用样本」 |
| `build_tick_row`（inf 报价） | tick JSON 含 `Infinity` | 落盘后下游 `json.loads` 默认接受 Infinity → 研究期毒样本 |

**边界**：合成隔离复现，**非**证明周五真实快照已损坏（真实数据全有限，§4 幂等回归逐字一致）；生产命中次数未知（自然 tick 周一 2026-11-30 09:37 CST 才开始积累）。

## 2. 修复（三处入口同一逻辑变量）

1. `chg_pct`：float 转换后、非正检查前加 `math.isfinite(last) and math.isfinite(prev_close)` 输入守卫（拦伪有限 -100%）；结果 `r` 再加 `isfinite(r)` 守卫（拦有限输入比值溢出）。docstring 同步「非有限」。
2. `_num`：`return v if v == v else None`（NaN-only）→ `return v if math.isfinite(v) else None`（NaN+±inf）。bool 拒、None/非法字符串→None、零/负数保留逐字不变。
3. `aggregate_sector`：`if c is None:` → `if c is None or not math.isfinite(c):`（防直接调用者绕过 build_tick_row；非有限成员进 missing，不进 eq/median/dispersion/breadth/n_available/turnover）。

**未动**：非正拒、round 4 位、FLAT_BAND_PP 平盘带、stale 闸（仍只核指数＝B2 排队）、时段闸、HH:MM 去重、MAX_TICK_ROWS、flock/原子写、fail-safe exit0、ADR 映射、UNITS、注册表、crontab。

## 3. 验证（全部 `PYTHONDONTWRITEBYTECODE=1`）

- 新契约 `tests/test_hk_collector_finite_guard.py` **36 例**＋旧 `tests/test_hk_market_context_collector.py` **48 例**合跑 **84 passed**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_hk_collector_finite_guard.py tests/test_hk_market_context_collector.py -q` → `/tmp/rfc218_b1_green.log`（还原后复跑 `_restore_green.log`）。
- **真实数据回归**：`data/shadow/hk_us_linkage/_verify/2026-11-27.json` 的 tick_rows → `derive_day_doc` 幂等重算 == 存储 doc 逐字一致（HSI/HSTECH/semis 14.8624 3/3/tech 5.47 8/8 等全有限值不变；快照 gitignored 缺失时该例自动 skip）。
- **变异 3 组全红→还原 SHA 核验→绿**（`/tmp/rfc218_b1_mutation_summary.log`）：
  - m1 `_num` 还原 NaN-only → **6 红**（`_m1_num_nan_only_red.log`）；
  - m2 删 `chg_pct` 输入守卫 → **2 红**（`_m2_chg_input_guard_removed_red.log`）——恰为「伪有限 -100%」类（inf/字符串 inf 作 prev_close），证明结果守卫不可替代输入守卫；
  - m3 删 aggregate 守卫 → **3 红**（`_m3_agg_guard_removed_red.log`）。
  - 每组还原后 SHA256 == post `4bbf3dd41c1ff002dbd4d4a02796b5e514cbe84aebcb1506bdc8284365dbdb27`。
- `py_compile` venv＋cron `/root/.local/bin/python3.11` 双解释器 exit0；cron 解释器 fresh-import 冒烟 `chg_pct(inf,100)=None / chg_pct(110,100)=10.0`；`check_undefined_names.py` rc0（`/tmp/rfc218_b1_undefined_names.log`）。
- Batch A 契约复跑 **41 passed**（`/tmp/rfc218_b1_batchA_recheck.log`）。未跑全量 pytest/CI，不声称全绿。

## 4. 凭据 / 回滚

- pre SHA256 `4db07dab582c06aaa5fe922902337d878a5901c621b4dc8f505e89f7a30046bd`（备份 `scripts/hk_market_context_collector.py.bak-20261128-hk-finite`，`*.bak*` gitignore 不入库）；post SHA256 `4bbf3dd41c1ff002dbd4d4a02796b5e514cbe84aebcb1506bdc8284365dbdb27`。
- 无 app/ 改动、无 config/.env.us 改动、无注册表/crontab 改动、**无重启**；DB 未触；零订单/零付费 LLM/零激活；obsidian_vault 与 entry_confirm 并行 WIP 未触碰未提交。
- **回滚**＝恢复备份（SHA 核验）或 revert 本批 commit；cron fresh-import 下一 tick 即生效，无需重启。

## 5. 未验证 / 后续单变量队列

- **收益增量未证（正确性≠盈利）**；港股定位维持用户裁决＝**候选发现与背景输入**，非方向信号。
- 生产自然 tick 未积累（首个周一 09:37 CST）；inf 实际命中率未知。
- **B2**（成分级时点/状态 stale：指数新鲜≠成分新鲜，停牌/前日报价仍进篮子）另批；**B3**（字段语义：`open_chg_pct`=首观测、翻红翻绿=穿零非趋势反转、high/low=采样极值，改名或接真实 OHLC 分列）另批；**C** 美股对应腿（先同公司可交易 ADR，再固定行业映射）；**D** 研究预注册（固定 HK 截止/US 决策/可成交时点、对齐交易日、控制盘前/期货/共同消息、按日聚类、成本后增量与 MDE；感恩节次日前收区间差必须先统一）；**E** `DA_HK_CONTEXT` 注入 **BLOCKED**（n≥60 HK 交易日＋传导研究＋§10.4 单变量 A/B＋用户单独裁决，不与其他输入开关捆绑）。
- A3 独立性判据分歧（merge `_self_debate` vs gate/adapter `_fallback`）钉死待裁，见 Batch A DEB §2.1，本批不修。
