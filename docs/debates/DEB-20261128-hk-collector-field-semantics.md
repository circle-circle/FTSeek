# DEB-20261128 · HK 采集器观测字段语义诚实化（B3 · 单变量 correctness fix · 影子面零判定消费 · schema v1→v2）

- **状态**：已落地。用户裁「同意，港股数据现阶段更适合候选发现与背景输入，按照建议执行」→「继续」×2；本批＝外部审计「港股采集器三处先硬化」之 **#3 「动态趋势」字段语义过强**（#1 有限值＝B1 `678c2cac1`；#2 成分时点/状态＝B2 `0685e9585`）。三处硬化至此全部完成。
- **范围**：`scripts/hk_market_context_collector.py` 单文件（`_rollup`/`derive_day_doc` 指数分支/UNITS/SCHEMA_VERSION/docstring）。单变量＝**观测字段语义命名**：`open_chg_pct`→`first_obs_chg_pct`（首观测，非交易所开盘）；采样极值→`obs_high/obs_low_chg_pct`（非真实盘中高低）；指数交易所真实盘中高低**分列**为 `day_high/day_low_chg_pct`（缺失→None，**废止 v1 回退采样值冒充的混合语义**）。`SCHEMA_VERSION` 1→2。**tick_rows 真值源 schema 零改动**；事件 kind `sign_cross_up/down` 名未动（本就诚实），docstring 补「采样穿零非趋势反转」语义声明。
- **分类依据（为何无开关直接修）**：采集器 record-only 影子（RFC-218 §25），`DA_HK_CONTEXT` 未建未激活，零判定消费；六面零引用审计证零活跃消费者（§2）；cron fresh-import 无重启。观测质量 correctness fix（先例 RFC-171 §9 L1；同批 B1/B2 同类）。**现在是改名唯一低成本窗口**——任何消费者建立前；改名后旧值经显式投影逐字保留（§4）。

## 1. 病灶（隔离合成实证，pre-fix）

探针日志 `/tmp/rfc218_b3_pre_evidence.log`（真实函数，非桩）：

| # | 输入 | pre-fix 输出 | 危害 |
|---|---|---|---|
| (a) | 真实开盘 103（+3%），首观测 09:37 已回落 101（+1%） | `open_chg_pct=1.0`；`open_price` 虽在 `_snapshot_rows` 抓取列中但被 `build_tick_row` 丢弃，真实开盘从未入文档 | 「open」名不副实：模型把首观测当开盘价，稀疏采样被当完整日内路径 |
| (b) | 指数交易所 hi=104（+4%，采样点间尖峰）；篮子采样均值 1.0→2.0 | 指数 `high_chg_pct=4.0`（交易所真实）vs 篮子 `high_chg_pct=2.0`（采样均值极值） | **同键双语义**：消费者无法区分真实盘中高与采样极值 |
| (c) | 指数交易所 hi=inf（B1→None） | `high_chg_pct=1.0`＝采样 max **回退冒充**交易所高 | 未知被伪装成已知，违反「未知不归零/不伪装」 |

**边界**：合成隔离复现，非证明周五真实快照已损坏（真实数据交易所高低齐全，(c) 未命中；(a)(b) 是语义误导非值错误）。生产自然 tick 未积累（首个周一 11-30 09:37 CST）。

## 2. 六面零引用审计（skill six-surface-zero-ref；旧 rollup 键名退役前置）

检索词：`open_chg_pct` / `high_chg_pct` / `low_chg_pct`（rollup 级语义）/ `hk_market_context_collector`（动态加载面）。

| 面 | 命令 | 命中 | 分类 |
|---|---|---|---|
| 1 仓库引用 | `grep -rn --exclude-dir={venv,.git,data,logs,obsidian_vault,node_modules,archive,__pycache__} --include='*.py,*.md,*.yaml,*.yml,*.toml,*.sh,*.json' 'open_chg_pct' .`＋`grep -rln archive/`＋high/low 同式 | collector `_rollup`:264-266（目标定义）；tests 3 文件（目标契约，本批同步更新）；B1/B2 DEB＋handoff（历史记录/队列描述）；archive/ 0 | 零活跃消费者 |
| 2 crontab | `crontab -l \| grep hk_market_context`＋`grep /etc/crontab /etc/cron.d/*`＋`docs/references/crontab-loop-baseline.txt` | 用户 crontab 2 行按**脚本路径**调用（脚本保留，仅内部字段改名）；/etc 面 0；基线文件无字段字面量 | 脚本消费不变，字段零引用 |
| 3 systemd | `systemctl cat ftseek-us \| grep -c 'open_chg_pct\|hk_market_context'` | 0 | 零 |
| 4 Hermes prompt | `grep -c 'open_chg_pct\|hk_market_context' /root/.hermes/cron/jobs.json` | 0 | 零 |
| 5 git hooks | `grep -rln 'open_chg_pct\|high_chg_pct' .git/hooks/`（除 sample） | 0 | 零 |
| 6 调度/动态字符串 | `grep -rn 'hk_market_context_collector' app/ scripts/`（除自身）＋仓外 `/root/.hermes/skills/deep-stock-analysis/scripts/` grep 三键 | 0（无 importlib/getattr 字符串派发；DA_HK_CONTEXT 未建） | 零 |

data/shadow 旧文档含 v1 键＝**历史观测记录，保留不改写**（投影回归兼容，§4）。

## 3. 修复与取舍

1. `_rollup`：`first_obs_chg_pct`/`obs_high_chg_pct`/`obs_low_chg_pct`/`latest_chg_pct`（latest 语义未动）；全缺失→四 None。
2. `derive_day_doc` 指数分支：`day_high_chg_pct=max(hi) if hi else None`、`day_low` 同理——与采样 obs 列**分列**，缺失不回退（病灶 c 废止）；指数同时保留 obs 列（采样路径范围，诚实命名）。
3. 篮子 rollup 无 day_high/day_low：篮子均值的真实盘中高低**不可由成分高低推导**，不伪造。
4. UNITS 补全 6 新键＋latest 声明；tick 级 `high/low_chg_pct` 保留（真值源，语义本就诚实＝该 tick 交易所高低 chg）。
5. `derive_events` docstring：采样穿零非趋势反转、~15min 采样可能漏区间内穿越、None 跳过 prev 取最近非 None。kind 名未动。
6. **取舍留痕**：①真实开盘 `open_price` 已在抓取列但未持久化——接入＝新数据通道，**另批**（本批不扩面，`test_no_open_price_fabrication` 钉死不得伪装）；②旧 v1 文档不改写不重生成，研究期（D）经 `tests/hk_day_doc_v1_compat.py` 显式投影兼容混合 schema；③指数 obs 列与 day 列并存＝采样路径与真实极值各自诚实，不互相替代。

**未动**：build_tick_row（B1/B2 守卫原样）、tick_rows schema、事件 kind、aggregate_sector、stale 闸、时段闸、去重/cap/flock/原子写/fail-safe、ADR 映射、crontab、注册表计数口径。

## 4. 验证（全部 `PYTHONDONTWRITEBYTECODE=1`）

- 新契约 `tests/test_hk_collector_field_semantics.py` **15 例**＋旧 48＋B1 36＋B2 13 合跑 **112 passed**：`PYTHONPATH=. ./venv/bin/python -m pytest tests/test_hk_collector_field_semantics.py tests/test_hk_collector_constituent_stale.py tests/test_hk_market_context_collector.py tests/test_hk_collector_finite_guard.py -q` → `/tmp/rfc218_b3_green.log`（还原后复跑 `_restore_green.log` 仍 112 绿）。
- **真实数据回归（语义升级）**：周五快照 `data/shadow/hk_us_linkage/_verify/2026-11-27.json` tick_rows → `derive_day_doc` v2 重算 → `project_day_doc_to_v1` 显式投影 == 存储 v1 doc **逐字一致**（B1/B2 两处回归同步升级为投影比对；证明**旧值全保留，仅命名/schema 元数据变更**；投影含 v1 回退混合语义如实还原，映射逐键 documented）。快照 gitignored 缺失时自动 skip。
- 既有测试最小更新 3 处：旧契约 `open_chg_pct`→`first_obs_chg_pct` 断言 1 行；B1/B2 周五回归改投影比对（各 +3 行）。其余断言零改动。
- **变异 3 组全红→还原 SHA 核验→绿**（`/tmp/rfc218_b3_mutation_summary.log`）：
  - m1 恢复 day_high 缺失回退采样值（病灶 c 复辟）→ **9 红**（`_m1_red.log`）；
  - m2 `first_obs=vals[-1]`（首观测变末观测）→ **11 红**（`_m2_red.log`，含两处周五投影回归）；
  - m3 `obs_high=min(vals)`（极值翻转）→ **10 红**（`_m3_red.log`）。
  - 每组还原后 SHA256 == post `50bd9738fd6561298f4626c2aa25736b8d12ebf12294209ae62f140175a2444e`。
- `py_compile` venv＋cron `/root/.local/bin/python3.11` 双解释器 exit0；cron fresh-import 冒烟：v2 命名＋day/obs 分列（1.0/4.0）＋tick 真值源 high_chg_pct=4.0 未动；`check_undefined_names.py` rc0。
- Batch A 契约复跑 **41 passed**（`/tmp/rfc218_b3_batchA_recheck.log`）。未跑全量 pytest/CI，不声称全绿。

## 5. 凭据 / 回滚

- pre SHA256 `f04eaf0930dfdcf9ed8ed6a60741d36d63176da3899f58abe22d737a8af459d8`（=B2 post；备份 `scripts/hk_market_context_collector.py.bak-20261128-hk-field-semantics`，*.bak* gitignore）；post SHA256 `50bd9738fd6561298f4626c2aa25736b8d12ebf12294209ae62f140175a2444e`（副本 `/tmp/rfc218_b3_collector.post`）。
- 无 app/ 改动、无 config/.env.us/注册表/crontab 改动、**无重启**；DB 未触；零订单/零付费 LLM/零激活；obsidian_vault 与并行 WIP 未触碰未提交。
- **回滚**＝恢复备份（SHA 核验）或 revert 本批 commit；cron fresh-import 下一 tick 即生效。已写 v2 文档若回滚需经投影逆映射读（研究期 D 兼容合同已留痕）；周五 v1 快照从未改写。

## 6. 未验证 / 后续单变量队列

- **收益增量未证（正确性≠盈利）**；港股定位维持用户裁决＝**候选发现与背景输入**，非方向信号。
- 真实开盘 `open_price` 未接入（抓取列已有，持久化＝新通道另批）；v2 自然 tick 未积累（首个周一 09:37 CST）；混合 v1/v2 文档的研究期兼容仅投影契约覆盖，D 预注册须显式声明 schema 处理。
- **C** 美股对应腿（先同公司可交易 ADR：比例/汇率/转换成本/OTC 可交易性；再固定少量行业映射）；**D** 研究预注册（固定 HK 截止/US 决策/可成交时点、对齐交易日、控制盘前/期货/共同消息、按日聚类、成本后增量 MDE；感恩节次日前收区间差先统一）；**E** `DA_HK_CONTEXT` 注入 **BLOCKED**（n≥60 HK 交易日＋传导研究＋§10.4 单变量 A/B＋用户单独裁决，不捆绑）。
- A3 独立性判据分歧待裁（Batch A DEB §2.1）；原审计 P1（日报/教训净额、EXT 推送失败登记、跨轮 pre-sizing、黑名单 warning）排队独立批，不在 HK 范围。
