# ADR-040: 信号精准批 (U1 / U1b / U1c) + KR2 §7.6 选项(d) 双口径并列 — 前瞻收益符号契约归位与零收益三态

- **状态**: 已实施 (2026-09-11, 用户分四轮逐项裁决: 「按建议执行 + **要盈利核心还是要信号精准**」
  →「核查确认无误按建议执行」→「挂起到 U2 落定后」/「升 v2.11 记 U1, 缺口另立项」
  →「请确认修复提交 重启」, 并就本次提交另裁两项: 提交范围 =「我的三批 + 对方 U2 批」、重启 =「不重启」)
- **主记录 (辩论/取证/预注册判据/逐轮回执)**:
  `docs/debates/DEB-20260910-da-ue-target-instruction-chain-review.md` (§11 信号精准批全程, 含 §11.9 零收益三态落地回执)
  + `docs/debates/DEB-20260909-exit-params-multi-model-debate.md` (§7.7 选项清单 / §7.10 选项(d) 落地回执)
  + `handoff.md` (2026-09-11 06:45 / 07:40 / 08:45 三条批次记录)。
  本 ADR 只固化**决策与其理由**, 证据与实测数字不在此重复。
- **立此 ADR 的直接缘由 (诚实登记)**: 本批改动触及 `scripts/daily_invariant_check.py`, 其新增形参
  `def main(push_enabled: bool = True)` 命中仓库 `commit-msg` 钩子高风险词表的 `enabled.*true` ⇒ 钩子要求
  提交引用 `[ADR-XXX]`/`[RFC-XXX]`/`[hotfix]`。**该命中是误报** (它是"本轮是否真发钉钉"的开关形参, 默认值
  服务的是既有 cron 行为不变, 不是策略启用开关), 但钩子按文本形态判定、**意图不可 grep** ⇒ 不辩解、不绕过。
  **处置选择**: 不硬凑无关编号、不用 `--no-verify` 静默绕过, 而是按仓库既有体例 (ADR-038 裁决执行批 /
  ADR-039 日志加固批) 补一份真决策记录。

## 背景 (病灶族)

三批看似无关的缺陷, 根因同族: **写侧契约无单一真源 ⇒ 每个读侧各自重新签名、各自重新定义判据, 而零测试覆盖让它们长期潜伏**。

- **U1 (UE 反馈反号)**: 写侧 `app/playbook/ic_tracker.py` 已按方向签名 (`sign`: BUY=+1 / SELL_SHORT=−1),
  读侧 `scripts/llm_universe_scan.py` 又乘一次 `sign` ⇒ 自 2026-07-25 上线以来 UE SHORT 有 fwd 共 **66** 条、
  **66/66 标签反转**, 其中 **41** 条做对的空头被标"推错"喂进抑制规则。
- **U1b (零收益被计为推错)**: 反馈表表头原文「负=推错」、抑制规则原文「连续推错(**连续负收益**)…不得再次浮现」
  ⇒ 两处都把"推错"定义为负; 而渲染式 `(" ✓" if adj > 0 else " ✗")` 把 `0.0` 归进 `✗`
  ⇒ **文档自我矛盾, 不是策略选择**, 且矛盾方向是用假阴性驱动"不得再次浮现"。
- **U1c (活体 cron 尺子反号, 本批最重)**: `scripts/direction_counterfactual_ruler.py` (**cron `50 6 * * 2-6`
  每日 06:50**) 修前 docstring 明写第二张库 `signal_snapshots.fwd_ret_*` 是"未方向调整的原始 close-to-close",
  于是 B 节又乘一次 `sign` ⇒ SHORT 全反号。实测反驳该前提: 写侧 `app/analysis/signal_ic_tracker.py::_calc_fwd`
  用**倒数式**签名 (LONG/NEUTRAL `(tN/t0)−1`, SHORT `(t0/tN)−1`), 生产库 1366/1366 条 SHORT 行匹配倒数式。
  ⇒ **不是"两张库两种约定", 而是两张库同约定、只是公式不同**; 而"错的那份信念"恰好写在活体尺子里
  (仓内 `scripts/factor_ic_backfill.py:134` 早在 2026-07-10 就写对了)。危害比 U1 更直接: 改的是**判据输出**,
  B 节 `flag` 判据 = 费后 `cost_adj = nominated − 13bp > 0`, 修前 SHORT 的 nominated 恒负
  ⇒ **自 2026-08-20 立项起每日输出 `pool_does_not_support_this_side` 于空头侧 = 用反过来的证据反对做空**。
- **KR2 告警链二次断裂**: `daily_invariant_check.py` 的守卫 `rec()` 只累加 Counter 不逐条打日志, 全函数末尾
  只出 **1** 条汇总 ERROR, 而 `error_alert.py:78 MIN_THRESHOLD=3` ⇒ **`1 ≥ 3` 恒假, 单条违反结构上永远够不到
  地板** (KR2 破了两天零通知)。这是 ADR-039「未决/移交」第 2 条预告过的形态, 本轮真发生。

## 决策

### ① 契约单一真源落在**写侧**, 不落读侧 (U1)
- 读侧 `llm_universe_scan.py` 去掉二次签名, 只做 `fraction → percent` (`adj = float(f) * 100`)。
- 契约文本落在 `app/playbook/ic_tracker.py` **模块 docstring** —— 理由: 读方是散的、会新增, 写方只有一处;
  落在读侧等于每新增一个读方就要重抄一遍契约。
- **唯一合法例外写进契约并由测试钉住**: "反签名还原价位" `close_t1 = entry × (1 + sign × fwd)`
  (`scripts/loser_hold_path_replay.py:62`) 代数上两方向恒等 (`sign² = 1`), **是正确的**, 不得"顺手修"。
  本条来自对 peer 一处提案的**独立证伪**: 若采信"这也是同族 bug", 只会在 SHORT 行破坏正确代码
  (数值实证: entry 100 / close_T1 95, SHORT 去 sign 得 105.0, 错 10 块)。
- **刻意不立全仓正则棘轮**: 同一文本形态 `sign * fwd` 在上述例外处是正确的、在三个读方是 bug ⇒ 意图不可 grep,
  只能按读方逐个上行为钉子 (另加一条 AST 结构钉: 尺子两个 section 函数体内不得出现 `sign` 标识符)。

### ② 零收益三态 `✓ / ✗ / ＝`, 判定口径是**精确零**不是显示零 (U1b)
- 渲染改 `(" ✓" if adj > 0 else " ✗" if adj < 0 else " ＝")`; 表头补 `＝=零收益持平, ＝不计入推错`;
  抑制规则**显式**加一句「表中 ＝ (零收益) 不是推错, 不得计入「连续推错」或据以触发本规则」。
  **三处必须同改**: 只改渲染不改表头, `＝` 在表内无定义; 只改代码不改 prompt 文本, 模型仍会把 `＝` 读成一种推错
  (四臂证伪 C 臂实测: 只回退 prompt 文本时 6 条行为钉**全绿**, 只红源码文本钉 ⇒ 没有那条文本钉, 假修复能过 CI)。
- **不得放宽为 `round(adj,1)==0` 或 `|adj|<0.05`**: 实测 2026-08-11 US.CVX 真值 **+0.0249%**、渲染恰为 `+0.0%`
  ⇒ 容差带会把**一笔真实盈利改判为持平**, 那是把渲染层的显示精度当业务判据 (B 臂实测: 注入该变体后 5 条零收益钉
  全绿, 只被 `test_tiny_nonzero_must_not_be_absorbed_into_neutral` **一条**抓住 ⇒ 判据错误, 不是风格分歧)。

### ③ 修尺子 B 节, 不动 A 节判读 (U1c)
- `direction_counterfactual_ruler.py` B 节去 re-sign; `flip` 保留取负并**注明为一阶近似**
  (倒数式与取负仅在二阶项上不同)。同时更正模块 docstring 与函数 docstring 的错误前提
  (**错误前提本身就是病灶**, 留着会被下一个人重新"修"回反号)。
- A 节 (读 `playbook_instructions`, 从未再签名, 一直是对的) **一行未动**; 其 `_verdict` 只看命中率的
  **预注册判读规则不动** (改它 = 改判据, 宪章律6) ⇒ 只登记"命中率口径对均值不对称失明"这一事实。
- 该脚本不在 `docs/REPLAY-SCRIPT-LEDGER.md` (非证据冻结)、产物 json/jsonl **零程序性读者** (纯遥测)
  ⇒ 修 B 节不改交易路径、不影响任何已裁决结论。

### ④ 版本戳升 v2.11 并**就地声明归因盲区**; 哈希口径一行未动
- `PROMPT_VERSION` v2.10 → **v2.11**, 并在同一行注释里写明: 本版两处改动均在 brief/user 侧, 而
  `PROMPT_HASH_V2` (`:126`) 只哈希 `SYSTEM_PROMPT_V2` ⇒ **升版号不改变哈希**, 不得据 v2.11 认定
  brief 侧变更已被指纹覆盖。实测坐实: v2.10 与 v2.11 的 `PROMPT_HASH_V2` **完全相同** `70e4680597af`、
  `SYSTEM_PROMPT_V2` 同为 **2582** 字符。
- **`:126` 刻意不动**: 改哈希口径会让既有样本的归因戳全部失效, 属行为变更, 须独立授权 (按用户裁「缺口另立项」)。
- 配套钉子: `test_brief_only_bump_leaves_hash_identical_to_v210` 把"盲区"本身变成**可执行断言**
  (钉住哈希值与字符数) ⇒ 将来谁把指纹扩到 brief 侧, 这条会红并提示他同步更新记录, 而不是静默漂移。
- 版本钉类名去版本号 (`TestPromptV210Contract` → `TestPromptVersionContract`): 类名里写死版本号
  每次升版都失真, 等于留一个说谎的标识符。

### ⑤ KR2 §7.6 选**选项(d) 双口径并列**, 不移窗、不放宽
- 主判分窗 `_FREEZE_COHORT_START="2026-09-03"` **一字未动** (被 `test_w1_exit_version_guard.py:224` 字面钉死);
  新增 `_SUB_COHORT_START="2026-09-10"` + `config_freeze.sub_cohort` **纯观测面**: 只数已闭合行, 输出
  versions/n_unique/n_closed/status, **恒带 `not_kr2_verdict: true`**, 不调 `rec()`、不产生违反、不参与 status 取劣。
- **决定性理由是功效算术, 不是折中**: 按 trade_date 已闭合 15/11/7/5/8 = **9.2 笔/交易日** ⇒
  19 交易日 × 9.2 ≈ **175 < 200 目标**, 即**主窗未破也买不到功效**; 而移窗 (选项(a)) 要付的代价恰是
  `de49` 那 **37** 笔已闭合样本 ⇒ **(a) 在功效上一无所获, 只丢掉 37 笔并改写一个预注册窗口**。
- **子窗起点选 9/10 而非 9/9 有实测依据**: 骑墙笔 trade 1473 (US.SNDK) 两版本退出时刻**零重叠**
  (`de49` 末次 9/9T13:33 ET → `13f6` 首次 9/10T09:38 ET, ≈20h)。且**刻意不**从"最新哈希首现日"自动推导
  —— 自动推导会让**任何**未来换版静默重置窗口, 是比"读错列 ≡ 恒 v2"更隐蔽的结构性假绿。
- **撤回一处数据文件自行改写判据**: `data/config_freeze_baseline.json` 的 `exit-13f6c2cc7fc4` 条目原自行宣称
  "哈希入指纹故判分窗口自本版本新开" ⇒ 改为如实记录主 cohort 判 BROKEN、不重开不放宽、sub_cohort 非判分对象
  (恢复"**改本文件 ≠ 改判据**")。
- **文档面勘误**: `docs/OKR-2026-09.md` 原称违约会"经 `error_alert.py` 推钉钉"是**文档自身的既存错误**,
  该跳从未成立; 变更记录补 9/9、9/11 两行 (9/6 之后零行 = 分叉根因, 已闭口)。

### ⑥ W1 关键违反直走 push hub, 不经行数阈值 (修 §7.2 断链根因)
- `_push_w1_critical()`: `W1_*` 关键违反直走 `app/push` hub `sync=True` (HIGH/RISK_ALERT)。
  `hub.py:96` 实证 `if not sync and not …should_send()` ⇒ **`sync=True` 完全绕过 dedup**, 故指纹无处可用、
  防刷屏必须自己做。
- 防刷屏判据 = 与**上次已成功送达**的 W1 键集比对 (相同抑制 / 首次推 / 上次失败重试 / 键集变化必推);
  在 `main()` 内**覆写 `OUT` 之前**调用 (否则第二次比对的是尚未被覆写的旧状态, 判据被自己废掉 —— 已删一处
  重复调用点并加源序钉子 `test_w1_push_reads_prev_state_before_out_is_overwritten`)。
- 结果落 `data/invariant_check_latest.json::w1_direct_push` (attempted/ok/error/suppressed_reason) 可审计;
  推送失败**显性化不上抛不吞** (JSON 与 exit 码照落)。
- `main()` 新增 `push_enabled` 形参 (**即本 ADR「立此缘由」中被钩子命中的那一行**):
  `False` = 走完整违反路径而不真发钉钉, 使生产库实跑演练成为可能 (零外部副作用)。
- `hub.stop()` 卫生: 短命 cron 进程可能挂在非 daemon worker 上 (正是 `error_alert.py:360-384` 专门防的失败形态)
  ⇒ `finally: hub.stop()`, stop 失败 `logger.warning(..., exc_info=True)` 不改判。

## 明确不做 / 纪律边界

- **不下 alpha 结论**: 尺子 B 节是**候选/信号层** (全 snapshot 含未执行); 宪章律9 要求按日聚类值 + MDE,
  本批**没有算这两样** ⇒ 读数只是"仪器修好后的读数", 不是判据。UE SHORT T+20 为 **−4.297%** 也说明
  优势依赖周期、非全域。
- **不动空头生成**: KR3 只允许一个预注册进场侧假设; 扩产空头属信号行为变更, 须先影子 + 预注册判据。
- **不追溯推翻已裁决**: DEB-20260816 §13.3「不修复」条款基于其**自己的** DA 日志回放 (n=74、open→close 口径),
  **不是**本尺子 B 节 ⇒ 该裁决不因 U1c 失效。但两者当时指向相反方向而无人察觉, 原因正是 B 节符号被翻
  —— 此点带入「空头干旱」议题下次重议。
- **不承诺收益**: U1/U1b/U1c 都是**行为变更** (改注入 prompt 的材料 / 改每日判据输出), 按 DEB-20260910 §七
  须记版本指纹, 且**不得**把后续盈亏变化归因于本批 (同期在途: ADR-039 日志批、M2 批、盘前失效门 measurement 批)。
- **H5 持久化反号副本刻意不改写** (`data/shadow/llm_universe_scan.jsonl` 等 ≥25 处"推错"引用):
  那是**当时真实产出**, 改写 = 伪造历史, 且 jsonl 喂预采样 ⇒ 只在 DEB/handoff 记更正。
- **证据冻结件不动**: `scripts/entry_gate_replay.py` 被 `docs/REPLAY-SCRIPT-LEDGER.md` 列为证据冻结
  ("不可改不可删") ⇒ 我曾改过它, 发现后已 `git checkout` 撤回, 实测 `git diff --stat` **为空 = 与 HEAD 逐字节相同**;
  其 `:77` 兜底的量纲缺陷 (留 fraction 而主路径是 percentage, 跨 **100×** 混进同一均值) 按仓内既有
  **"债标记测试"体例**钉现状 (`test_debt_*` ×2 + `test_entry_gate_replay_is_ledger_frozen`),
  **冻结例外获批之日这两例必须变红并被改写成正确断言, 而不是继续绿**。
- **不放宽 W4 基线**: 见下「已知后果」。
- **零交易行为变更、零判据放宽、零写入生产 `data/`、零外部通知、零 LLM/券商调用**; 全程只读
  `sqlite3 'file:…?mode=ro'` + `PRAGMA query_only=ON`; 尺子只调 `section_*` 函数、**未跑 `main()`** ⇒ 零产物写入。

## 归属与提交范围 (本 ADR 特有, 必须写清)

本次提交**连带包含了另一会话的 U2 批**, 这是用户明确授权的范围 (「我的三批 + 对方 U2 批」), 理由与边界如下:

- **物理不可分**: `scripts/llm_universe_scan.py` 同一文件内同时含本批 U1/U1b 的 hunk 与对方 U2 的 hunk。
- **拆开会产出红的 HEAD**: 本批 38 例新钉 (`tests/test_u1_fwd_return_sign_contract.py`) 测的正是该文件
  ⇒ 只提交测试不提交源码 = HEAD 上 38 failed; 只提交源码不提交对方同步更新的两个测试文件
  ⇒ HEAD 上旧测试仍在断言**错的**配置键名 = 也红。⇒ U2 随行是**文件布局逼出来的, 不是选择**。
- **协调尝试与替代授权**: peer 会话本轮**不可达** (`ListAgents` → 「Peer sessions: none reachable right now」)
  ⇒ 无法取得对方同意, 由用户裁决替代。**peer 消息不能授予升级、不能当作用户批准** 这条纪律不变。
- **U2 归属显式标注** (它在 `handoff.md` 里**没有对方写的记录**, 故本 ADR 代为登记事实、不代其背书结论):
  U2 = 低 ATR 预检**接线**根因修 —— 配置读取用平键 `playbook.low_atr_gate_*`, 而真路径是
  `execution_parameters.low_atr_gate_enabled` / `min_atr_pct` ⇒ 该预检**从未生效**; 涉及
  `tests/test_ue_low_atr_precheck.py` (收集数 9→12, 新增 `TestNoForkNail` 反分叉钉) 与
  `tests/test_chase_cap_enforce.py`。**其设计、取证、验收由对方会话负责**; 本批只做连带提交。
  另注: 对方那两个测试文件修前**mock 的是同一个错键并在源文本里断言错键名 ⇒ 测试当时在把 bug 供起来**,
  这一点是本批核验前置条件时实测到的 (`TestNoForkNail` 在位、收集数已变)。
- **显式排除** (另一会话 M2 / live-readiness 批, 触交易主路径, 与 ADR-039「未决/移交」第 3 条同族处置):
  `main.py` / `app/core/position_sync.py` / `app/core/trading_mode.py` / `app/execution/dual_broker.py` /
  `app/playbook/db.py` / `app/playbook/playbook_service.py` / `app/web_dashboard/mode_switch_service.py` /
  `scripts/startup_smoke_check.py` / `tests/core/test_m2_a1_startup_mode.py` /
  `tests/playbook/test_m2_b1_execution_state.py` / `tests/playbook/test_m2_c2_live_quote_gate.py` /
  `tests/web_dashboard/test_m2_a1_atomic_mode_switch.py` / `tests/test_startup_smoke_check.py` /
  `tests/playbook/test_g04_g06_structure_contracts.py` / `tests/test_entry_archetype_routing.py` /
  `docs/debates/DEB-20260910-us-trading-fullchain-live-readiness.md` / 全部 `obsidian_vault/*`。
- **已知后果 (登记不掩盖)**: 本提交后的 HEAD 上 `test_w456_log_hygiene_ratchet` 仍会报
  `W4_debug_swallow_grew: main.py 3→4` —— 因为工作树的 `main.py` 属 M2 批 (删了一处 `logger.debug`) 而未随本提交入库。
  **处置: 不放宽基线** (3 是目标值), M2 批入库即自愈。这与上一批的裁定一致。

## 验证与纪律

- **全量回归复跑两次一致: 3825 passed / 41 skipped / 0 failed, exit 0** (第一次 403.32s 干净跑;
  第二次 637.92s, 因与一条本会话遗留的逐目录 `--collect-only` 循环争抢 CPU 而变慢 —— **秒数不可比、结果可比**,
  该循环已按 PID `kill`)。定向面: `test_u1_fwd_return_sign_contract.py` 38 例 + `test_ue_contract_validator.py`
  18 例 = 71 passed → 扩面 4 文件 89 passed; `test_w1_exit_version_guard.py` 14→31 例 (既有 14 全绿
  = 主判据未被新增观测面污染)。
- **账目逐项闭合 (不留白)**: 上轮基线 **3813** +8 (本批 U1 钉 30→38) +1 (`test_ue_contract_validator.py` 17→18)
  +3 (对方 U2 `test_ue_low_atr_precheck.py` 9→12, 隔离副本对 HEAD 实测) = **3825** ✓。
  **刻意不**计入 `tests/test_settle_premarket_invalidation.py` 的 11 例: 它由 `ea614074` 于 **06:18** 引入、
  **早于** 07:40 那次 3813 基线跑 ⇒ 已在基线内, 计入即双算 (先前按"HEAD 前移 4 个提交"推算得 **3836** 的错账,
  错因正是这一项, **已更正不静默**)。
- **一处实测差异登记但不桥接 (不编造解释)**: `pytest --collect-only` 稳定报 **3865** 项 (三跑一致、3865 个唯一
  node id、0 collection error; 已排除 conftest 钩子、动态参数化、`unittest.subTest`、模块级 skip),
  而运行汇总为 3825+41 = **3866** 项, **差 1**; 属收集模式与运行模式的计量差, 本批**未归因、不桥接**,
  登记待查 (不影响判绿: 两次全量均 0 failed)。
- **四臂证伪全部在隔离 `git worktree` 副本内做, 主工作区零改动** (跑完 `worktree remove --force` + `prune`;
  `/tmp/wt-be57742a` 是他人副本, 未读写): A 回潮渲染式 → 22 failed / 16 passed (红的全是 SHORT 族与三态族,
  BUY 正值格保持绿); B 注入 `round(adj,1)==0` → 1 failed / 5 passed (只红 CVX 那条 = 判别力精确);
  C 只回退 prompt 文本 → 1 failed / 6 passed; D 当前态 → 38 passed。
  另有 Round B (尺子反号回潮) 3 failed / 19 passed、Round C (UE 反号回潮) 10 failed / 20 passed
  —— 后者红的**恰好**是 5 条实测真值 SHORT 行 + 8 例矩阵中 4 例 (全 SELL_SHORT) + t1 兜底, **4 个 BUY 格全绿**
  ⇒ 判别力精确落在病灶上, 不多不少。**既有测试对这两把尺子的覆盖修前为 0** (`grep -rl` 只命中新文件)
  ⇒ 零覆盖才让它们潜伏 3 周。
- **静态关**: 改动文件 `py_compile` 0 错、`scripts/check_undefined_names.py` **exit 0**。
- **未重启服务 (用户裁「不重启」, 且有实测支撑)**: deploy drift = **−6236s** (负值 = 进程比最新改动还新),
  服务于 **09:00:26** 自动重启, 晚于本批所有改动文件的 mtime; 且 `llm_universe_scan.py` /
  `direction_counterfactual_ruler.py` / `daily_invariant_check.py` 均为 **cron 路径, 不入 `ftseek-us` 导入图**
  (`deploy_freshness_check.py:43-44` 只扫 `app/` 与 `main.py`; 本批唯一入 `app/` 的改动是
  `app/playbook/ic_tracker.py` 的 **docstring**, 零语义变更)。

## 未决 / 移交 (本批查出, 均**未动**, 需用户裁)

1. **实例 D — DA 主交易决策链上的同族二值判定 (比 U1b 更重)**: `app/pipeline/llm_pipeline.py:861`
   `_build_track_record_context` 用 `{'✓' if f5 > 0 else '✗'}` 二分 `playbook_instructions.fwd_return_t5`,
   `:862/:864/:866/:875` 的计数把**零收益计为一次推错**, 喂给 `:888`「同方向连续✗仍同向出手 → 降级
   `watch_only`/`NO_TRADE`」⇒ 与 U1b 同一病灶但落在**真实交易降级路径**上 (U1b 只影响 UE 浮现抑制, D 影响是否停手)。
   仓内已有三态先例 `scripts/gate_block_counterfactual.py:137` (±0.001 → 「中性」)。**需独立契约记录 + 用户授权**。
2. **thinking-eval 9/22 出数的切分键**: `docs/CONTROL-LOOP-LEDGER.md:133` 的 cron `1e96c03c945e`
   (2026-09-22 09:30 CST 单次) 预注册判据键在 `prompt_hash`, 而 v2.10/v2.11 哈希相同 ⇒ 该判据在 9/11 这次
   brief 变更上**不会触发序列归零**, 9/11 起样本将与 9/8–9/10 混在同一把尺子里。按宪章律6 (阈值预注册、
   期末前不得改) + 律9, **本批不动**, 交用户裁: 按 `prompt_version` 二次切分, 或裁"brief 变更属材料级、可合并"。
3. **`direction_hit_scorecard.py:197-200` 版本切片污染 (活体尺子)**: 注释自称"prompt_version 自动 A/B 切片",
   实现却是 `LIKE '%v2.1%'` / `'%v2.2%'` 两条**硬编码子串** (自 2026-07-25 未动), 而 `v2.10`/`v2.11` 都含子串
   `v2.1` ⇒ 实测 v2.1 切片 **20 行 = 11 行真 v2.1 + 9 行 v2.10** (**45% 错版本**), 且 v2.11 会继续被吸进去;
   全表 **181** 行中 **104** 行 (**57%**) 无任何切片。**未修**: ≥6 份 DEB 引用其现算数字, 重切片 = 改变已被引用的
   证据 ⇒ 需独立裁决 + 注记方案。
4. **指纹扩到 brief 侧 (另立项)**: 最省路径已知 = **接线既有死代码** `app/llm/model_call.py:659-662`
   `prompt_hash(system, user)` = `md5((system or "") + "\n---\n" + (user or ""))[:12]`, **零生产调用者**
   (仅 `tests/coordinator/test_model_call.py:292-299` 引用) ⇒ 是接线不是发明。对照: 仓外 DA 侧
   `_prompt_fingerprint(SYSTEM_PROMPT, USER_PROMPT_TEMPLATE)` **同时**哈希 user 模板, UE 侧无对等物。
   由 2/3/4 合看: **版本戳在仓内多点硬编码、无单一真源** —— 这是 §七「记全 Prompt 指纹」在 UE 侧
   无法一步到位的结构性原因, 也是另立项应当一次解决的范围。
5. **登记不改写**: `alpha_t1/_t5/_t20` 全仓**无活体写侧**但生产库 4431 行非空 (跨度 05-14→09-09)
   ⇒ 存在仓外或已退役写入方, B 节 alpha 读数**无法从代码复现口径**, 只登记不结论;
   仓外 `dc_calibration_collector.py:187` `sign * srow["fwd_ret_t1"]` 其查询不过滤 direction
   ⇒ 仅 (gate=SELL ∧ snapshot=SHORT) 交集反号, **上界 64 行 = 潜伏小样本**, 属仓外文件不改只登记;
   `scripts/verify_da_ue_batch_20260831.py:36-42` 硬编码 `EXPECTED_UE_PROMPT_VERSION="v2.8"` 已过期
   (历史 ONESHOT 产物, 登记不改写); DDOG 09-03 的 fwd 是否确为正 (即 09-04 抑制是否真实误杀) **仍待查**。

## 回滚

逐面独立, **零生产代码依赖、零数据库迁移、零服务重启需求**:
- U1/U1b: revert `scripts/llm_universe_scan.py` 四处 (渲染三态 / 表头 / 抑制规则 / `PROMPT_VERSION`)
  + revert `app/playbook/ic_tracker.py` 契约段 + `git checkout tests/test_ue_contract_validator.py`
  + 删 `tests/test_u1_fwd_return_sign_contract.py` (或整文件回到 30 例版)。
- U1c: revert `scripts/direction_counterfactual_ruler.py` 的 B 节去 re-sign 与两处 docstring 更正。
- KR2 §7.6(d): revert `scripts/daily_invariant_check.py` 五处 (子窗常量+注释块 / `_W1_KEY_PREFIX` /
  `sub_cohort_pairs` 独立 try / `info["sub_cohort"]` 块 / `_push_w1_critical` + `main()` 接线与 `push_enabled` 形参)
  + revert 两个文档/基线文件对应段落 + `git checkout tests/test_w1_exit_version_guard.py`。
- U2 (对方批): 归属对方会话, 本 ADR 不代拟其回滚步骤。
