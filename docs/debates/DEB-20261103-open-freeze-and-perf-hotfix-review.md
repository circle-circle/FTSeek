# 议题：11-02/11-03 开盘冻结与卡顿批（protobuf upb / 执行优先 / GC 护栏 / 数据源单例）的独立复核与修正

> 关联：`DEB-20260910-logging-observability-batch`（日志/观测基建）、
> `docs/circuit-breaker-map.md`（熔断分层地图，本次未新增熔断）、
> `docs/OKR-2026-11.md`（当前阶段：DA-only 净 bps/笔 > 0，行为变更一律影子先行）。
>
> 数据截止：2026-11-03 10:50 CST（= 2026-11-02 22:50 ET，休市）。
> 复核窗口覆盖 4 个 hotfix 提交：`6fc2f13b0`（DA bridge 相位判据）、
> `73c970e05`（开盘冻结归因 + GC 风暴护栏）、`7afc587f7`（protobuf upb + 执行优先 + 轮次计时/堆归还）、
> `4fe1c2946`（数据源单例收敛 + 步骤 RSS 归因 + 开盘速判器）。
>
> 本文性质：**事后复核**。4 个提交已先行上线生产，复核发生在上线之后；本文只做
> 「证据核验 + 缺陷修正 + 治理偏差登记」，不改任何交易判定，不回滚已生效的机制。
> §5 登记本次**有意不改**的项与理由，§7 登记治理偏差（不隐藏）。

**状态**：已决策（复核结论：技术实现成立；3 处缺陷已修正并通过测试；治理缺口以本文件 + handoff 条目补齐）

## 状态

- [x] 进行中（证据整理完成；4 个提交逐项复核完毕）
- [x] 待决策（无 —— 本文件不是新议题的执行令）
- [x] 已决策（复核结论与 3 处修正已落地，见 §3/§4）
- [ ] 已验证（盘中项未完成，见 §6 待复核）
- [ ] 已关闭（待今晚盘中复核 + 推送后关闭）

## 0. 结论先行

1. **四项改动的技术实现成立**，核心断言经独立复算/实跑复核为真：protobuf 实测 `upb`、
   GC 阈值降频 **178.6x**（复算一致）、数据源单例确为既有契约的漏用、
   棘轮基线 905 的「本批净贡献 0」经同口径复算为真。
2. **执行优先的核心断言成立**：入场执行只依赖 DB 的 `triggered` 指令，不依赖同轮
   generate/topup 产出；复核另排掉两个排序隐患（generate 不写已有指令、
   `premarket_invalidation` 是 record-only 影子），vol_bucket 无缺口。
3. **查出 6 处问题**（§3），其中 3 处为代码缺陷已当场修正（§4）；3 处为治理/完整性
   缺口，登记而不强改（§5、§7）。
4. **最大缺口是治理而非代码**：4 个提交全部未落档、`handoff.md` 停更在 2026-11-02、
   「执行优先」作为盘中行为变更未影子先行即默认上线、且同批一次动了 4 个独立变量
   —— 出事时无法归因（§7）。

## 1. 背景与触发

用户令「请检查另外 agent 的优化修复点，是否合理正常」。

背景事实（来自被复核方提交自述与线上日志，本次独立取证）：

- 2026-11-02 09:30 ET 美股开盘段，进程级静默约 3 分钟：心跳停滞 101s →
  py-spy + SIGUSR1 + SIGKILL；SIGUSR1 进程内快照抓到
  `Current thread ... Garbage-collecting @ scheduler_core.run_forever`；
  同期日志 0 行而 journald/cron 有记录 ⇒ 冻结是进程级而非单个线程。
- 同日该进程内存峰值口径 **1.45GB（⚠️ 2026-11-03 全链审计后标注为"待核"：11-02 冻结时刻无
  `/proc/<pid>/status` 快照留存，无法确认它是 RSS 还是 VmPeak/VmSize）**；宿主 3.5GB 且
  `swappiness=0`。**当前进程实测：VmSize/VmPeak = 1.71GB，而 RSS 仅 66–148MB**，
  `/proc/maps` 显示 **956MB 是 glibc malloc arena 的 PROT_NONE 预留（15 段 × 64MB）+ 264MB
  线程栈（33×8MB）** ⇒ 同量级的"1.4GB"极可能是**虚拟地址空间而非驻留内存**。
  该口径问题已单列进全链审计（见 `DEB-20261103-full-chain-perf-risk-audit.md`）。
- 11-03 上午连续 6 次重启/部署（09:34 / 09:40 / 09:41 / 09:47 / 10:16 / 10:25），
  每次都带一批热修。

## 2. 逐项复核（方法 + 结论）

### 2.1 protobuf 3.20.3 → 4.25.3（upb）

| 核验项 | 方法 | 结果 |
|---|---|---|
| 运行时实现 | `./venv/bin/python -c "from google.protobuf.internal import api_implementation; print(api_implementation.Type())"` | `upb` ✓（提交自述为真） |
| 版本 | `google.protobuf.__version__` | 4.25.3 ✓ |
| 依赖闭合 | `./venv/bin/pip check` | `No broken requirements found` ✓ |
| futu pb2 与 4.x 兼容 | 读 `venv/.../futu/common/pb/Common_pb2.py` 头 | 使用 `from google.protobuf.internal import builder as _builder`（protoc ≥3.19 新式 API）⇒ **不会**触发 4.21+ 的 `Descriptors cannot not be created directly` ✓ |
| 活体链路 | `logs/ftseek_us.log` 10:25:20 起 | `[MarketDataRouter] Futu LV3 提供商已初始化`、`[富途] 连接成功 127.0.0.1:11111 env=模拟` ✓ |
| 重启后错误面 | 同上 | ERROR/CRITICAL = **0** ✓ |

**结论成立。** 唯一未覆盖面：美股推送解码路径需美股开盘（今晚 **22:30 CST** —— 11-01 起美东
转 EST，09:30 ET = 22:30 CST，不是夏令时口径的 21:30）自然复核
（提交自述已如实标注）。**风险登记**：升级属二进制依赖变更，回滚需同时覆盖 venv
覆盖件 + `requirements.lock` + 重启（提交已留备份目录 `/opt/ftseek/backups/protobuf-3.20.3-*/`）。

### 2.2 GC 护栏（阈值 20000/25/25 + `gc.freeze()`）

| 核验项 | 方法 | 结果 |
|---|---|---|
| 降频倍数 | 复算：原 700×10×10=70,000 次分配一次全堆扫描 → 新 20000×25×25=12,500,000 次 | **178.6x**（提交自述 178x ✓） |
| 线上生效 | 日志 `[GC] 阈值 (700, 10, 10) → (20000, 25, 25)` | ✓ |
| 冻结生效 | 日志 `[GC] 长命对象已冻结 (startup)` | ✓ |
| 堆水位 | `[GC] 10min 汇总` RSS=148MB → 130MB（对比 11-02 的 1.45GB **口径待核**，见 §1 与全链审计） | ✓（休市时段，非开盘洪峰口径） |
| 接线安全 | `main.py`：`install_gc_guard()`（启动早期）+ `freeze_long_lived()`（`run_forever` 前），两处均 try/except 包裹 | ✓ 启动不可被护栏搞挂 |
| `malloc_trim` | `ctypes.CDLL("libc.so.6").malloc_trim(0)`，glibc only，失败跳过；CDLL 调用释放 GIL | ✓ 安全（arena 锁保护） |

**结论成立。** 副作用登记：`malloc_trim` 把页还给 OS，后续再分配会产生 minor fault
（批内自述的机制是「缩冷页集 → 减换出 → 减全堆 GC 缺页」，方向自洽）。

### 2.3 数据源单例收敛（`get_alpha_vantage_provider()` + 四个 router 旁路）

| 核验项 | 方法 | 结果 |
|---|---|---|
| 既有契约 | `app/data/market_data_router.py:1269` `get_market_data_router()`（2026-07-11 建，注释原文「所有非启动路径应通过 get_market_data_router() 复用同一实例」）+ `scheduler_core.py:305-317` 启动时注册 | ✓ 四个旁路确为漏用，不是新造抽象 |
| 共享后是否有人 close 共享实例 | 逐个查 `open_checker` / `accuracy_tracker` / `data_collector` / `price_calibrator` / `position_exit_monitor` | ✓ **无任何调用方 close 共享 router**（避免「关掉进程级 router」这一致命面） |
| AV provider close 调用点 | 全仓查 `.close()` | 只有 `alphavantage_provider.py:826` 的方法定义本身，无运行期调用点 ✓ |
| 单例语义 | `get_vix_or_none()` 内部缓存 TTL 仍按 `is_trading` 分档 60s/600s | ✓ 未让 VIX 变旧 |
| 线上计数 | 10:25:20 后 `[AlphaVantage] 初始化完成` = 1、`[MarketDataRouter] 初始化完成` = 1 | ⚠ 见 §5.1：**重启后首启计数不能证明盘中不再重复**，需盘中复核 |

**结论成立**，但见 §5.1（router 内建 AV 未收敛 ⇒ 每进程 AV 实例预期为 2 而非 1）。

### 2.4 盘中「执行优先」（`scheduler.execution_priority_first`，默认 true）

核心断言「执行只依赖 DB `triggered` 指令，不依赖同轮 generate/topup 产出」核验：

| 核验项 | 方法 | 结果 |
|---|---|---|
| 执行输入 | `app/playbook/pipeline_parts/auto_execute_flow.py`：读 DB `triggered=1/executed=0`，自带三重门（`auto_execute` 配置开关 / 交易日 / 开盘 5 分钟缓冲） | ✓ 不依赖同轮产出 |
| 排序隐患① | `generation_flow.py` 中对**已有**指令的写操作仅 `update_instruction_tracking`（定桶 / regime 标注），`run_playbook_generate` 本身不改已有指令 | ✓ 无「执行了同轮将被作废的指令」风险 |
| 排序隐患② | `app/playbook/premarket_invalidation.py` 头注释「record-only shadow…零行为变更」（只写 `data/premarket_invalidation_shadow.jsonl`） | ✓ 提前执行不会跳过失效门 |
| vol_bucket 缺口 | 定桶在 generate 之后同轮执行 ⇒ 下一轮执行时桶已就位；reentry 指令按设计不走 vol_bucket（`auto_execute_flow.py:536-543` 专用分支） | ✓ 无「执行时桶缺失」 |
| 代价量化 | TRADING 分支末尾固定 60s 休眠（`scheduler_core.py:2422-2423`） | 新指令晚一轮 = +60s + 本轮剩余耗时 ≈ **60–180s**（与提交自述一致） |
| 回滚 | `FTSEEK_EXEC_FIRST=0`（env，立即）/ `scheduler.execution_priority_first=false`（YAML，需重启） | ✓ 双闸 |

**结论成立**，但见 §7（治理：未影子先行 + 一批多变量）。

### 2.5 except-pass 棘轮基线 900 → 905

提交自述：「实测 905 且该值早于本批（worktree @5bec3843d 同口径 905），本批净贡献 0；
原 900 是 10-28 的旧值，之后并行工作流新增 5 处未同步基线 ⇒ 棘轮常驻红会掩埋新缺口」。

**独立复算**（同正则 `except[^:\n]*:\s*\n\s*(pass|continue)\s*(?:#.*)?$`、同排除面
`{venv,node_modules,.git,tests,archive,__pycache__,.backup_premigration,obsidian_vault,docs}`）：

| 版本 | 计数 |
|---|---|
| HEAD（4fe1c2946） | **905** |
| 批前 `5bec3843d` | **905** |

⇒ 自述为真：905 早于本批存在，本批净贡献 0，原基线 900 已陈旧导致棘轮常驻红。
**本次校准是恢复信号，不是放宽掩盖。**（本批两处 `except: pass` → `logger.debug` 因行尾
带注释，本来就不落在该正则的计数面内，故净 0。）

### 2.6 DA bridge 相位判据（`scripts/pre_market_check.py`）

预检 job 固定 CST 20:45 = EDT 08:45 / EST 07:45；新判据 ET < 08:45 判「尚未到 DA 生成
时点」（INFO，不产生 warning），≥ 08:45 才 WARN。**只改告警级别，不改任何交易判定** ✓
（提交已退役的「DA 18:00任务」措辞属 EDT 时代 CST 口径，同批更正 ✓）。

### 2.7 观测件（`round_timer` / `open_health_report.py`）

- `scripts/open_health_report.py`：只读（仅 `open` / `gzip.open` 读日志，无写路径），
  实跑输出自洽；跨重启「开始→完成」按伪配对待 ✓。
- `round_timer`：两次 `/proc/self/status` 读，微秒级 ✓。**但发现方向语义缺陷，见 §3.1。**

## 3. 复核发现的问题

| # | 问题 | 严重度 | 位置 | 处置 |
|---|---|---|---|---|
| 3.1 | RSS 台阶判定用 `abs(delta)`：堆**回落** ≥50MB 会打 WARNING 且文案为「单步长尾…同轮后续步骤全被它排队」⇒ 归因错误 + 噪声 | 中 | `app/observability/round_timer.py` | §4.1 已修 |
| 3.2 | `[GC] 10min 汇总` 的 gen 计数取自 `gc.get_stats()`（**进程累计值**），却与「本窗口停顿」并排放在「10min 汇总」标签下 ⇒ 连续两次窗口读数相同也会被误读为窗口值 | 低 | `app/observability/gc_guard.py` | §4.2 已修 |
| 3.3 | `open_checker.py` 仍 `from app.data.market_data_router import MarketDataRouter`，但已改走 `get_market_data_router()` ⇒ 死 import | 低 | `app/us_pipeline/open_checker.py:23` | §4.3 已修 |
| 3.4 | router 内建 `AlphaVantageProvider` 未收敛 ⇒ 每进程 AV 实例预期 **2** 个（router 1 + 单例 1），各带独立 500/日配额与缓存；「137→1」标题偏乐观 | 低 | `app/data/market_data_router.py:115` | §5.1 登记不改（理由） |
| 3.5 | 4 个提交全部未落档、`handoff.md` 停更在 2026-11-02 | **高（治理）** | 仓库 | §4.4 + handoff 条目已补 |
| 3.6 | 「执行优先」为盘中交易路径行为变更，**未影子先行**即默认上线，且同批一次改动 4 个独立变量（protobuf / 执行优先 / GC / 单例）⇒ 首场出事无法归因 | **高（治理）** | `scheduler_core.py` | §7 登记（不在本文件强改） |

## 4. 本次修正（2026-11-03，只动观测/标签/死代码，零交易语义变更）

### 4.1 RSS 方向语义（`app/observability/round_timer.py`）

- **前**：`abs(delta) >= 阈值` ⇒ 任一方向都进 WARNING，文案统一为「单步长尾/RSS 台阶」。
- **后**：上涨 ≥ 阈值 = 「台阶」（WARNING，内存驱动）；下降 ≥ 阈值 = 「释放」（INFO，非告警）；
  耗时 ≥ 30s 才是「单步长尾」（WARNING，耗时驱动）。三种归因分列，不再互相冒充。
- 记号格式 `; RSS {前:.0f}→{后:.0f}MB ({delta:+.0f}, 台阶|释放)`。
- 测试：`tests/observability/test_round_timer.py` 更新 1 例 + 新增 1 例
  （回落 ⇒ 无 WARNING、无「单步长尾」字样）。

### 4.2 GC 汇总口径（`app/observability/gc_guard.py`）

- 汇总行改为 `[GC] %dmin 汇总: 启动以来 gen0=… gen1=… gen2=… | 本窗口停顿 … ；
  进程启动以来最长 …ms | RSS=…MB`，并在函数 docstring 写明「代计数是进程累计值，
  只有停顿计数是窗口值」。
- 兼容性：`scripts/open_health_report.py` 的解析正则为**子串搜索**
  （`gen0=(\d+)次/(\d+)回收 gen1=…` / `本窗口停顿 (\d+) 次, 最长 (\d+)ms` /
  `启动以来最长 (\d+)ms` / `RSS=(\d+)MB`），新文案全部命中，无需改解析器 ✓。

### 4.3 死 import（`app/us_pipeline/open_checker.py`）

删 `from app.data.market_data_router import MarketDataRouter`（该类在本文件已无任何
引用，仅剩注释里提到 `MarketDataRouter.get_quote`）。

### 4.4 落档补齐

- 本文件（4 提交的事后复核 + 修正记录 + 治理偏差登记）。
- `handoff.md` 顶部补 2026-11-03 条目（进度队列恢复连续性）。
- `docs/debates/README.md` 索引表登记本文件。

### 4.5 验证

- `pytest tests/observability/test_round_timer.py tests/observability/test_gc_guard.py
  tests/test_provider_singleton.py tests/test_exec_priority_first.py
  tests/test_except_pass_ratchet.py` → **28 passed**；扩测（含 `test_market_regime.py`）
  **57 passed**。
- `py_compile` 四个改动文件 → 通过。
- `scripts/open_health_report.py` 实跑 → 输出自洽。
- except-pass 棘轮计数不变（本批修正未新增 `except: pass`）。

### 4.6 部署记录（2026-11-03 11:16 CST，用户授权后执行）

- 提交：`9d0e76cf0`（9 文件：app 3 + scripts 1 + tests 1 + docs 3 + handoff 1），
  **未**带入工作树里另一 agent 的 `obsidian_vault` 33 改 8 新。
- 推送：`git push origin rfc-060-phase1-hotfixes` ⇒ `73c970e05..9d0e76cf0`
  （普通推送，非 force）⇒ 生产已生效但远端无记录的两个 hotfix 一并补上远端记录。
- 重启前核①未平仓 3 笔（US.MRK / US.LMT / US.ACN，均非在途出场）；核②
  `git status --porcelain -- app/ scripts/` 为空（已提交）。
- 重启：`systemctl restart ftseek-us` ⇒ 11:16:07 起进程；
  `scripts/startup_smoke_check.py --wait 90` ⇒ **7/7 全过（exit=0）**
  （ConfigValidator / 漂移检测 / 热重载 / SSOT 审计 0 BLOCKING / 双臂安全确认 /
  退出快循环 / 零 Traceback-CRITICAL）。
- 重启后：0 ERROR/CRITICAL；持仓按 `open_positions` SSOT 恢复 3 只
  （US.ACN / US.LMT / US.MRK，与重启前核一致）；`[GC] 阈值 → (20000,25,25)` 与
  `长命对象已冻结 (startup)` 均已落日志；源码 mtime（11:02:25–11:02:59）早于进程
  启动（11:16:07）⇒ **跑的是修正后代码**。

## 5. 登记：本次有意不改的项与理由

### 5.1 router 内建 AlphaVantageProvider 保留（不收敛）

- 理由一：该实例**每进程只建 1 个**，不存在「反复构造」的浪费，收敛它换不来性能。
- 理由二：router 侧 AV 的 `cache_ttl_seconds` 来自 router 配置，且 router 的
  `get_vix()` 路径喂给 regime 判定；改走单例会把 TTL 换成默认 900s ⇒ **改动 regime 侧
  VIX 新鲜度语义**，属敏感路径行为变更，收益为负。
- 代价登记：**每进程 AV 实例数预期为 2**（router 1 + 单例 1），日配额因此是 2×500 而非
  500 —— 与「配额门此前形同虚设」的判断方向一致（137 个实例 ⇒ 配额计数被反复归零）。

### 5.2 「执行优先」不回收为影子模式

- 改动已在生产生效且回滚闸齐备（env 立即 / YAML 需重启）；此刻回收为影子 = 再动一次
  盘中路径，变量更多。
- 替代动作：登记 §7 治理偏差 + 今晚盘中按预注册停止条件 S1/S2/S3 盯盘
  （S1 当日前 3 小时成交笔数 < 近 5 日同时段均值 70%；S2 trigger→成交 p90 反而 >+60s；
  S3 同标的同轮重复下单/双平仓任一起）。任一条触发 ⇒ 当天回滚 `FTSEEK_EXEC_FIRST=0`。

## 6. 待复核（需今晚 22:30 CST 美股开盘，EST 口径）

| # | 待核项 | 判据 | 工具 |
|---|---|---|---|
| 6.1 | 数据源单例真实效果 | 一个完整交易时段内 `[AlphaVantage] 初始化完成` / `[MarketDataRouter] 初始化完成` 各 ≤ 2 次（2 = §5.1 的 router + 单例），**不回到 137/17 量级** | `grep -c` 主日志 |
| 6.2 | protobuf 4.25.3 下的美股推送解码 | 开盘后 Futu LV3 推送解码正常、价格实时跳动、0 ERROR | `logs/ftseek_us.log` |
| 6.3 | 执行优先的入场延迟 | trigger→成交 p90 不比改动前更差（>+60s 即触发 S2） | `scripts/open_health_report.py`（入场执行段）+ 成交笔数对比 |
| 6.4 | GC 停顿 | 开盘洪峰期 gen2 停顿与 RSS 不复现 11-02 的 7259ms 形态（1.45GB 那条口径待核，见 §1） | `[GC] 10min 汇总` + `open_health_report.py` |

### 6.4 结算（2026-11-17，补）

- **gen2 面 PASS**：11-03 至今每进程 gen2 恒 1 次（仅启动显式 collect），开盘段零自动 gen2 / 零 gen2 长停顿；开盘段 RSS ≤285MB（1.45GB 口径见 `DEB-20261103-full-chain-perf-risk-audit.md`，判为虚拟地址非驻留）。原判据「gen2 停顿与 RSS 不复现 7259ms 形态」成立。
- **新面 opened**：长停顿以 gen1/swap 缺页形态复发（11-16 23:31 盘中 20972ms / 11-17 05:01 EOD 11322ms collected=0），非开盘洪峰、非 gen2，不属原判据文字但「不复现长停顿」意图未达成。诊断与执行提案见 `DEB-20261117-gc-guard-completion-swap-pause.md`。
- §6.1–6.3 仍未结算（待盘中复核），本文「已验证/已关闭」勾选不变。

## 7. 治理偏差登记（不隐藏）

1. **未落档上线**：4 个提交（`6fc2f13b0` / `73c970e05` / `7afc587f7` / `4fe1c2946`）在
   上线时未写 DEB/RFC、未更新 `handoff.md` ⇒ 违反宪章 §三.3（每次改动更新进度队列）
   与 AGENT.md（行为变更落档）。**本文件 + handoff 条目为事后补正**，不等于流程合规。
2. **行为变更未影子先行**：「执行优先」直接默认 ON 上线，AGENT.md 与 OKR-2026-11 要求
   行为变更影子先行。缓解措施只有回滚闸 + 预注册停止条件，非影子对照。
3. **一次一变量被打破**：同批 4 个独立变量（protobuf 依赖 / 盘中执行顺序 / GC 阈值与
   freeze / 数据源单例）同时生效，首场异常无法归因到具体一项。缓解：每个变量都有
   独立回滚开关，回滚粒度仍在。
4. **分支未推送**：本分支 `rfc-060-phase1-hotfixes` 领先 `origin` **2 个提交**
   （`7afc587f7` / `4fe1c2946`），生产已生效但远端无记录 ⇒ 灾备/协作可见性缺口。
   本次修正提交后应一并推送（`git push`，非 force）。
5. **重启冒烟**：AGENT.md 要求每次重启后跑 `scripts/startup_smoke_check.py --wait 90`。
   复核方在 10:49 CST 补跑该脚本时，七条断言全绿，但因**启动段已超 120s** 触发脚本的
   「启动段早于本次重启」过期判据（exit=2）—— 该判据设计为「重启后立即跑」，故此结果
   不构成对 10:25 那次重启的独立背书，也不构成服务异常。

## 8. 边界声明

- 本次修正**不碰**任何入场/出场/仓位/阈值/门链判定；改的是日志方向语义、汇总标签口径、
  一处死 import。
- 本文所有数字来自本地可复算来源：提交历史、`logs/ftseek_us.log`、`/proc/self/status`、
  venv 实测与 pytest；未引用任何未复核的外部结论。
- §6 的 4 项在今晚盘中复核完成前，**不得**把本文当成本批「已验证已关闭」。

## 9. 讨论记录

- 2026-11-03：复核方（本次会话）按 AGENTS.md 必读顺序读 `AGENT.md` / `handoff.md` /
  宪章 / OKR-2026-11，逐项复算 4 个提交的自述断言；结论见 §0。修正批落地后经用户授权
  完成重启 + 冒烟（见 §4.6）。
- 2026-11-03（修正）：§3.1/§3.2/§3.3 三处代码缺陷已修并加测；§5.1/§5.2 登记不改项；
  §3.5 治理缺口以本文件 + handoff 条目补正；§3.6 的影子先行缺口**未**在本文强改，
  以「预注册停止条件 S1/S2/S3 + 今晚盘中四项复核」替代（诚实边界：这不是影子对照）。
