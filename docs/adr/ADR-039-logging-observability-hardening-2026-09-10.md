# ADR-039: 日志/可观测性加固批 — 告警链重写 + 三条日志卫生棘轮 + 异常留痕纪律 + 降噪与死组件退役

- **状态**: 已实施 (2026-09-10, 用户分五轮逐项裁决: 「深度分析」→「按照建议执行」→「按照建议执行优化」
  →「请重启再观察」→「需要」/「按照建议执行」)
- **主记录 (辩论/取证/预注册判据/逐轮回执)**: `docs/debates/DEB-20260910-logging-observability-batch.md`
  (§1–§9 立项与裁决, §7 落地回执, §10–§10.8 定稿后核查与两批提交回执)。本 ADR 只固化**决策与其理由**,
  证据与实测数字不在此重复。
- **立此 ADR 的直接缘由 (诚实登记)**: 本批改动触及 `app/core/scheduler_core.py`, 命中仓库 `commit-msg`
  钩子的高风险词表 (`scheduler_core` = 交易主路径) ⇒ 钩子要求提交引用 `[ADR-XXX]`/`[RFC-XXX]`/`[hotfix]`。
  本批此前只有 DEB 记录、无 ADR/RFC 编号可引。**处置选择**: 不硬凑无关编号 (如引 ADR-036)、不用
  `--no-verify` 静默绕过, 而是按仓库既有体例 (ADR-036 治理加固批 / ADR-038 裁决执行批) 补一份真决策记录。

## 背景 (病灶族)

同一天内连续发现的多起事故与存量, 根因都指向同一族: **"零违反"与"守卫自己失明"在读数上不可区分**。
- 主日志 `print` 噪声占 **60.9%** (今日 1396/2291 行) ⇒ 真信号被淹。
- 全仓 4302 个 `except` 块只 58 处带 `exc_info` ⇒ **98.7% 的异常路径只留 `str(e)`**, 无栈无行号;
  改前两日主日志 **0 条 Traceback** (不是没出错, 是出错不留栈)。
- 告警链 `error_alert.sh` 用 `/ERROR|CRITICAL/` **级别盲匹配** (INFO 正文含 "ERROR" 即计入), 且只读
  `logs/ftseek_us.log` ⇒ `logs/invariant_check.log` 里的不变量违反**从未被推送过**。
- 非阻断配置审计项**自带** `⚠️ [INFO]`/`⚠️ [DRIFT]` 标签却用 `logger.warning` 输出 ⇒ 消息自称 INFO
  而级别是 WARNING, 名实矛盾, 且与相邻的 `✅ 审计通过` 行互相打脸 (每次重启都出现); 7 日本族 120 条
  WARNING 占全仓 7.0%、零阻断价值。
- 死审计组件 (`app/observability/audit_logger.py`) 在册但零挂载 ⇒ "有审计"是纸面。
- `pangan_daily_rebuild.py::step0` 的日K增量摘要被 futu SDK 的 `on_disconnect` 顶掉, 连续五档日志里
  真实读数从未出现, 而行首 `OK` 只是子进程 returncode ⇒ **病灶被 "OK" 掩盖**。

## 决策

### ① 告警链重写: `error_alert.sh` → `scripts/error_alert.py`
- 判级改**字面量** `"] ERROR:"` / `"] CRITICAL:"`, 与本仓日志形态、与 `intraday_heartbeat` 同源单一真源
  (不再级别盲匹配)。
- 扫描面扩到含 `logs/invariant_check.log` ⇒ 不变量违反首次真进告警链。
- 相对基线 + 阈值地板 + 逐指纹冷却 (降噪与"可触发"两面同时守); 旧脚本移 `scripts/archive/error_alert.sh`,
  crontab 两行 `*/15` 改指 `.py`, **回滚指引写进 crontab 注释**; `AGENT.md`/`docs/OKR-2026-09.md`/
  `docs/CONTROL-LOOP-LEDGER.md` 引用同步。

### ② 三条日志卫生棘轮 W4/W5/W6 (入 `scripts/daily_invariant_check.py`, 复用既有 cron 不新建调度)
- W4 `debug_swallow` = `except` 里用 `logger.debug` 吞错; W5 `bare_logger` = `getLogger("无点号裸名")`;
  W6 `except_stack` = 告警不带 `exc_info`。AST 单遍量三维, 嵌套 handler 按节点身份去重。
- 棘轮语义: **基线外新增文件 / 基线内计数增长 ⇒ 当晚告警 + exit 1**; 存量静默计数; 愈合提示;
  **文件解析失败 ⇒ `W456_parse_error` 显性化, 不得当"零违反"**。
- 基线域 `data/{debug_swallow,except_stack,bare_logger}_baseline.json` (gitignored, 首跑自建),
  文件自带"改本文件 = 改判据, 须先落 DEB"纪律注记。
- 价值当场自证: 它咬住的第一个对象不是历史墓地, 而是**同一天正在写的新代码**。

### ③ 异常留痕纪律 (R5 模式)
- `scheduler_core.py` 23 处隔离异常补 `exc_info=True`, 4 处 `logger.debug` → `warning` + `exc_info`。
- 新写异常处理一律 `logger.warning(..., exc_info=True)`: **不能用 `debug`** (撞 W4)、**不能省 `exc_info`**
  (撞 W6)。`except:pass` 棘轮 (既有) 基线只降不升, 本轮 955→952。

### ④ 降噪与"名实相符"
- 非阻断配置审计 WARNING → INFO (**blocking 项仍走 `logger.critical`** ⇒ 阻断语义与 `strict=True` 的
  raise 不变), 清单合成**单条原子 record** (handler 锁保证不被启动期多线程穿插)。
- `positions/reconciler.py` 成功摘要降噪; **失败仍 WARNING 且带栈** (钉子直测, 防"降噪"降成"降可见性")。

### ⑤ 减法: 死组件退役 + 结算器"配置停"
- 删 `app/observability/audit_logger.py` 及其测试 (减法三步: 核对零挂载 → 配置停/退役 → 留证据注释)。
- 新键 `playbook.overnight_hold_settler_enabled: false` = 结算器减法第②步"配置停" (第③步代码退役待观察
  一周后另做)。**零 live 消费方已实证**, 故默认 false 对任何 live 判据零影响; 回滚 = 置 true + 重启。
- `shadow_registry.py` 补登记**第六个挂载面**: `scheduler_core.py` AFTER_HOURS 段经 `_load_script_module`
  进程内 importlib **按文件路径加载、无 import 语句** ⇒ crontab / Hermes / `rg import` 三面全查不到
  (8/28 注释因此误判"无排班", 实际盘后窗每 ~7m47s 重算重印 74 笔反事实全表、一晚 32 轮)。
  **制度化结论**: 零引用核对面必须含"进程内按路径动态加载"这一面。

### ⑥ 流归属 (第 7 个解析面)
- `pangan_daily_rebuild.py::step0` 摘要改从**子进程 stderr** 取, 抽纯函数 `_pick_refresh_summary(stderr, stdout)`
  便于钉子直测 (真跑子进程 = 281 票 × 1.2s ≈ 7m45s 且打真 Futu 配额, 故不真跑);
  两流都无摘要 ⇒ 显式 `无摘要输出` 而非空串 (空串与"摘要为空"不可区分 = 静默失败形态)。
- 与 §8.7「`logger.error` 存在 ≠ 告警存在」同族, 是其镜像: **"日志行存在 ≠ 日志行有用"**。

### ⑦ print → logger (7 个 in-process 脚本)
- `refresh_daily_bars` / `settle_overnight_shadow` / `cluster_concentration_shadow` / `short_funnel_shadow_tag` /
  `conflict_shadow_tag` / `gap_semantics_shadow_tag` / `band_vol_shadow_tag`: 命名统一到 in-process 既有约定
  `ftseek.<name>`, **消息正文逐字不变**以免打断既有 grep; 多行报告块合成单条 record;
  各脚本 `__main__` 补 `if not logging.getLogger().handlers: basicConfig(...)` 守卫
  (否则 print→logger 后**独立 CLI 运行输出会静默变空**)。
- `refresh_daily_bars` 另修: 不再丢弃 futu 在 `ret != RET_OK` 时放在 `data` 的错误正文
  (`US.SKYT` 连续 31 夜 `ret=-1` 无从根因), 并补两处**原先完全静默**的失败计数。

## 验证与纪律

- 全量 `pytest tests/` = **3755 passed / 0 failed / 41 skipped**; 本批新增 6 个测试文件 + 既有门扩双向臂
  (`test_no_archive_imports` 的 PROSE_ALLOWLIST: 键 = (文件, **稳定子串**) 而非行号, 未登记命中红 +
  登记项不再命中也红, `IMPORT_SHAPE` 优先判定 ⇒ 真 import 形态永不豁免)。
- 受控重启两次 (13:01 / 16:55), 冒烟 7/7, 新启动段 0 ERROR / 0 Traceback / 0 `no such column`;
  重启时机一律排在港股收盘后、美股开盘前 (真钱成交被动监听空窗无实盘暴露)。
- **零交易行为变更**: 本批全在日志 / 告警 / 死代码 / 配置停一面; 唯一"行为"变化是结算器停跑 (零 live 消费方)。
- 服务当前仍为模拟盘 (`broker=sim db=sim ALLOW_REAL_TRADING=False`)。

## 未决 / 移交

- **W456 非密封耦合**: W4/W5/W6 扫活体源码树, 使 8 个与日志卫生无关的不变量测试各付一次 ~15s 扫描
  (全量回归 ≈ +124s), 且他人新增违反会让这 8 个测试红而失败信息不含文件行号 ⇒ 误归因。
  推荐修法 (甲) 需动 2 个**他人在途**文件, 故等其批入库后另做。详见 DEB §9.1③。
- **`error_alert.py` 阈值地板 3 与"单条发射器"不兼容** (`1 ≥ 3` 为假) ⇒ W1~W6 违反不会推钉钉。
  推荐"必推白名单"方案属告警判据变更, 须预注册后另批。详见 DEB §9.1④。
- **两批共写文件** `main.py` (日志格式加 `%(threadName)s`) 与 `app/core/position_sync.py` (唯一未命名线程补名)
  因与另一批 (M2/live-readiness) 同文件混改, 未随本 ADR 的提交入库; 与其配套的
  `scripts/startup_smoke_check.py` 断言修订亦顺延 (依赖对方 `main.py` 的 `[StartupMode]` 标记)。
  详见 DEB §10.8「显式排除」。
- `ops/hermes/ftseek_us_restart_20260829_hotfix.sh` 仍引用已搬家的 `error_alert.sh` (带日期一次性工件),
  由运维面裁改写 / 加注释 / 一并归档。

## 回滚

`git revert` 对应提交 + crontab 两行指回 `/bin/bash scripts/error_alert.sh` (注释已写); 亦可逐面回滚:
W456 删三份 `data/*_baseline.json` 即回未管控态; 议题二键置 `true` + 重启即恢复 8/10 起原行为;
死审计组件 revert 即复位。
