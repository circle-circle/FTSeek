# 全链路优化实施方案（2026-10-04 立案）

> 用户裁决：同意优化，但**不得影响 2026-10-05（周一）开盘**。
> 本方案遵守：OKR-2026-10 参数冻结、影子先行、行为变更一律预注册、重启军规。

**修订记录**
- v1 (2026-10-04 批0): 初版立案。
- v2 (2026-10-04 批0 复查修订, Claude Code 复查 + 用户裁「按最优建议执行」):
  ① 批3 由"流程执行"改为**用户裁决点（默认不激活）** — 激活窗 10/13-16 ⊂ OKR-2026-10
  判分窗 (9/17-10/31)，虽不破 KR1 字面判据（退出哈希唯一），但入场时序 regime 中途切换
  会造成"哈希唯一、行为两版"的隐性混样（KR2-09 BROKEN 同源教训：判据面漏维度）；
  ② 批1 补缝表双登记 + 棘轮纪律 + 七项检查串 grep 守卫 + RFC-060 脏树处置；
  ③ B 补 `timeout` 硬壳 + 失败闭环写全（含 freeze_sentinel 层）+ `--wait 90` 实测定量；
  ④ 批2 补前置交付物「相位依赖矩阵」（已完成，附录 C）+ 首跑语义测试 + 回滚实演；
  ⑤ 勘误：`unified_config.yaml` 实际路径 `config/unified_config.yaml`；行号校准；
  crontab 16-22/4-8 两行是 9/22 已落地的 DST 免疫扩段**本身**，非待改项。
  复查实证底稿见附录 A/B/C。

---

## 0. 难度与总量总览

| 批次 | 内容 | 难度 | 工作量 | 风险 | 部署窗 |
|---|---|---|---|---|---|
| 批0（今日） | 计划落档 + v2 复查修订 + 启动段耗时实测 + 相位依赖矩阵 | 低 | 1h | 零（纯文档+只读分析） | ✅ 已完成 |
| 批1 | A1 循环抖动影子遥测 + B 启动冒烟 systemd 强制 + C 告警跨进程锁 | **中** | 0.5-1 天 | 低（A1 纯观测 / B 仅运维 / C 仅告警链） | 10/5 周一 ET 16:00 后 |
| 批2 | A2 `auto_execute` 独立快循环（代码惰性落地，flag=false） | **中** | 0.5 天 | 低（flag 关 = 行为零变化） | 10/9 周五盘后（A1 攒 3-5 交易日数据后） |
| 批3 | A2 flag=true 激活 | **裁决点** | — | — | **默认不激活**，见 §4 三态裁决 |
| 批4 | D 冬令时 cron 窗口审计 + 调整 | 低 | 2h + 变更单 | 低（提前演练+备份） | 10/20 前（11/1 DST 前） |
| 批5 | E admission 收口 / pipeline 继续拆分 / 三态状态机 | 高 | 多天 | 中-高 | **OKR-10 结算后另立 DEB** |

---

## 1. 明日开盘保护窗（硬规则）

**冻结窗 = 2026-10-05 ET 04:00 → 16:00（周一盘前到收盘）：**

1. 不改 `app/`、`scripts/`、`config/`、systemd unit、crontab — 任何一行都不动；
2. 不主动 `systemctl restart ftseek-us`（若意外崩溃，systemd 自愈按现有代码重启，
   树内无本方案新增运行时代码 ⇒ 与今日行为完全一致）；
3. 批1 只在今天（10/4 周日）写文档；运行时代码 10/5 收盘后才落 working tree。

**理由**：服务为 `Restart=always`，盘中/隔夜任何崩溃自愈都会加载树内最新代码。
若今天把批1代码写进 `app/` 或 `scripts/`，等于把"未冒烟的变更"暴露给一次不可控的
自动重启 — 这正是 AGENT.md 重启前 `git status` 核对要防的事故形态。

**v2 补充实证（2026-10-04）**：
- cron 直接从工作树执行 `scripts/error_alert.py`（每 15min）⇒ C 的代码**今天落树即
  下个 cron 窗上线**，无需重启。冻结纪律对 cron 脚本比 app/ 更硬，不是更软。
- 当前运行进程（PID 3883669, 10/4 16:03:56 CST 启动）**晚于** RFC-060 批脏文件
  mtime（10/4 00:53）⇒ 运行中进程已含该批未提交改动，树=运行时一致，冻结窗内
  崩溃自愈无隐藏分叉。但批1 落地前必须处置（commit 或明确 stash），否则批1
  checklist #2 过不去（见 §2 checklist #0）。

---

## 2. 批1（10/5 周一盘后，一次重启完成三项）

### A1. `llm_strategy_loop` 循环抖动影子遥测（难度：低）

**问题**：盘前生成、topup、定桶、auto_execute(名义 180s)、再入场、退出兜底全部串行在
一个线程。历史上单轮 58min（watchdog 被迫加"长轮护航"补丁，预算 2700s）。
`auto_execute` 的 180s 名义间隔**没有任何实测数据** — 无法回答"门禁解除到进场
实际延迟多少"。（既有护航遥测是 **>600s 才落 warning 日志**的轮次粒度
（`scheduler_core.py:1132`），慢轮可见但无相位分解、无结构化产物 — A1 与其互补，
非平行尺子。）

**实施**：

1. `app/core/scheduler_core.py::_llm_strategy_loop` 每个相位任务包一层
   `_phase_telemetry(task_name, nominal_sec=None)` 上下文管理器；
2. 落 `data/llm_loop_phase_jitter.jsonl`（有界追加：单文件 ≤10MB 滚动截断保尾部
   30 天语义；字段：`ts / phase / task / start / end / dur_ms / nominal_ms /
   lag_ms / prev_gap_ms`）；
3. `auto_execute` 额外记录 `now - _playbook_auto_execute_last_run` 实际间隔；
4. 遥测本体全部 `try/except` 包裹 — 任何写盘失败只 warn，绝不影响循环。

**v2 实施纪律（复查新增，缺一则全链测试抓红/棘轮转红）**：

5. **缝表双登记**：`data/llm_loop_phase_jitter.jsonl` 是新生产 data/ 写入路径 ⇒
   ① `tests/conftest.py:58 _PRODUCTION_DATA_SEAMS` 登记一行（模块级路径常量 +
   沙箱重定向）；② `tests/playbook/offline_chain_driver.py:~287` 改道清单同步
   （该清单**不读**缝表，独立维护 — 漏登 = 离线链真跑时直写生产 jsonl）；
6. **时间戳必须 tz-aware**（ISO8601 带偏移）— naive-now 棘轮基线 435 只降不升，
   时钟病灶台账有前科；
7. **except 棘轮**：新增的 try/except 站点须同步棘轮基线；提交前跑四棘轮；
8. 消费端读数脚本（p50/p90/max + auto_execute 实际间隔分布）随本批交付，避免
   10/9 go/no-go 裁决时临场写分析代码引入偏差。

**测试**：`tests/test_llm_loop_phase_telemetry.py`（合成时钟 + 异常注入，
断言 jsonl 字段完整且遥测异常不外溢；data/ 路径走缝表沙箱）。

**验收**：连续 3-5 交易日产出 auto_execute 实际间隔分布（p50/p90/max）。

**回滚**：删遥测块 + 重启（零行为面）。

### B. 启动冒烟 systemd 机器强制（难度：中）

**问题**："重启必跑启动冒烟"目前是人工纪律。漏跑 = 服务"看起来起来了"但
ConfigGov 可能降级运行（base.* 事故教训原话："服务已启动≠健康"）。

**实施**（unit 改动）：

```ini
# /etc/systemd/system/ftseek-us.service
TimeoutStartSec=180        # 原 120 → 180，给 --wait 90 留余量（实测见附录 B）
ExecStartPost=/bin/bash -c 'timeout 100 env PYTHONPATH=/opt/ftseek/FTSeek-US \
  /opt/ftseek/FTSeek-US/venv/bin/python \
  /opt/ftseek/FTSeek-US/scripts/startup_smoke_check.py --wait 90 --push'
```

**语义**：
- `start.sh` 以 `exec python main.py --start` 结束 ⇒ Type=simple 主进程即 python，
  `ExecStartPost` 在进程拉起后并行执行；
- 冒烟脚本 `--wait` 模式已要求"启动段不早于脚本启动前 120s"（防假阳性，
  `startup_smoke_check.py:195-204`），与 ExecStartPost 时序天然匹配；
- **`timeout 100` 硬壳（v2 新增）**：`--push` 走飞书网络，挂死无超时保护 ⇒
  硬壳把 ExecStartPost 总时长钉在 ≤100s，远离 TimeoutStartSec=180；
- `--push` 失败被脚本 try/except 吞掉**不改 exit code**（已核实 :223-228）⇒
  监控面故障不会误杀交易进程；
- 冒烟失败 ⇒ unit failed ⇒ `Restart=always` 重试（StartLimit 20/600 + RestartSec=30
  兜底）。这是**刻意强化**：冒烟失败本来就该当"服务没起来"处理。

**失败闭环写全（v2 新增，含已核实的 sentinel 层）**：

```
冒烟失败 → unit failed → 主进程收 SIGTERM（KillMode=control-group, TimeoutStopSec=120
  优雅退出）→ Restart=always 每 30s 重试 → 20次/600s 击穿 start-limit →
  freeze_sentinel.py（每分钟 cron，已核实 :217-231）reset-failed + restart +
  critical 告警（冷却 900s）→ 持续 flap 直至人工到场
```
- 冒烟**真阳性**（服务确实坏）时服务处于 flap 态、存量仓看护降级 — 刻意接受，
  与现状（崩溃循环）等价但多了 critical 告警必达；
- 冒烟**假阴性**（服务健康但断言红）代价 = 每次 ~30s+ 重启空窗 ⇒ 假阴性率必须
  ≈0，由下列两道防线保证。

**假阴性防线（v2 新增，批1 上线前置）**：

1. `--wait 90` 定量依据：n=20 次历史重启实测，启动段 banner→七项全绿 **9.8-14.2s**
   （含盘中 RTH 样本），加 start.sh 前置 ~7s 与 5s 轮询粒度，端到端 ≲25s ⇒
   90s 余量 >3.5×（附录 B 全表）；
2. **七项检查串 grep 守卫**：批1 同车的 A1 改 `scheduler_core.py`，而七项之二
   （`[PlaybookExit] 快循环已启动` @ scheduler_core.py:2871、`账户路由
   AUTO_TRADE_ACCOUNT=` 横幅同文件）就在该文件内 ⇒ 批1 提交前必须 grep 本批
   diff 不触碰以下七个判定面（缺一 = 上线即 flap storm）：
   `ConfigValidator] 校验结果: ✅ 全部通过` / `[ConfigGov] ✅ 漂移检测已启动` /
   `[ConfigGov] ✅ 热重载已启动` / `[ConfigAudit] ✅ SSOT 配置一致性审计通过` /
   `[StartupMode] 模式一致:` 正则面 + `账户路由 AUTO_TRADE_ACCOUNT=` 正则面 /
   `[PlaybookExit] 快循环已启动` / 启动段零 `Traceback|ImportError|
   ModuleNotFoundError| CRITICAL`。

**演练**（10/5 盘后）：
1. `systemctl daemon-reload && systemctl restart ftseek-us`，核验
   `systemctl show ftseek-us -p ExecMainStatus,SubState` + journal 冒烟七项全绿；
2. **故障注入（v2 新增）**：临时构造一次冒烟失败（如 `bash -c 'exit 1'` 占位
   ExecStartPost 或测试环境模拟），实证 unit failed → 主进程 SIGTERM →
   Restart=always → （击穿后）sentinel 告警全链路符合预期，再恢复正常 unit。
   只验绿路径 = 没验证 B 的核心语义。

**回滚**：删 `ExecStartPost` 行 + `TimeoutStartSec` 回 120 + `daemon-reload` + restart。

### C. 告警冷却跨进程文件锁（难度：低-中）

**问题**：`error_alert.py` 冷却状态 `data/.alert_state` 读-判-写无锁。9/22 实证两条
cron 同秒命中（CST 05:15:01,932/934），读到的都是空冷却 ⇒ 同文双推。
PushHub 去重是进程内 60s 表，跨进程无效。

**实施**：

1. 新增 `app/push/file_lock.py`：`fcntl.flock(LOCK_EX)` 上下文管理器，
   锁文件 `data/.error_alert.cooldown.lock`（flock 随进程死亡内核自动放锁，
   无残留锁问题；路径锚定照 `error_alert.py:70 STATE = REPO/"data"/...` 先例）；
2. `scripts/error_alert.py` 将 `load_state → 冷却判定 → save_state` 整段包进锁
   （该函数 :408-480 有多个 save_state 调用点，须整段包，不是只包单个 save）；
3. 锁获取失败 fail-open（告警链可用性优先，只 warn 留痕）。

**v2 实施纪律**：锁文件 `data/.error_alert.cooldown.lock` 是新生产 data/ 写入路径
⇒ conftest 缝表 + offline_chain_driver 改道清单**双登记**（同 A1 第 5 条）。

**测试**：`tests/test_error_alert_cross_process_lock.py` — `multiprocessing`
起两个实例 + stub 推送，断言同指纹只发 1 次（data/ 路径走缝表沙箱）。

**回滚**：删锁包装（cron 下轮生效，无需重启主进程）。

### 批1 上线 checklist（照 AGENT.md 军规）

```
0. [v2] 处置 RFC-060 批脏树（当前 9 个 M 文件已含于运行进程）：
   commit 或明确 stash，使批1 diff 可隔离核验
1. SELECT symbol FROM trade_records WHERE exit_time IS NULL   # 未平仓/在途
2. git status --porcelain -- app/ scripts/                     # 只含本批自己的改动
3. [v2] 七项检查串 grep 守卫（B 节清单）— 本批 diff 零触碰
4. [v2] 四棘轮 + 缝表双登记核验（A1 jsonl / C lock 两处新路径）
5. 备份 systemd unit + crontab + .alert_state
6. systemctl restart ftseek-us                                 # ExecStartPost 自跑冒烟
7. 人工核 journal 冒烟七项 + 首小时盯盘（chase_cap 遥测/退出快环心跳/组合兜底）
8. [v2] B 故障注入演练（见 B 节演练 2）— 可在盘后低峰单独做
```

---

## 3. 批2（10/9 周五盘后）：auto_execute 独立快循环（惰性落地）

**前置**：A1 已攒 3-5 交易日数据。若实测 p90 间隔 ≤ 300s（名义 180s 的 1.67 倍）
且无 >600s 单次 ⇒ **批2/批3 直接销案**（数据说了算，默认不激活）。

**前置交付物（v2 新增）**：相位依赖矩阵 — **已完成，见附录 C**。核心结论：
`run_playbook_auto_execute`（pipeline.py:3964）函数体对 `scheduler` 参数**零使用**
（3964-4380 行无一次 `scheduler.` 访问），全部输入自包含（DB/配置文件/标记文件/
自构造 gate），与循环先前相位**无内存态手-off**；唯一耦合是"指令表由先前相位
写入"这一 DB 中介边，fast loop 每 180s 轮询只会更及时；唯一调用点
scheduler_core.py:2296（已核实全仓无第二入口），单宿主互斥无漏网。
DB 并发安全性由 exit fast loop 先例实证（7/21 起双线程读写同库，预算查询均为
`safe_sqlite_connect` 短连接 + timeout=5）。

**实施**（完全照 `PlaybookExitFastLoop` 已验证模式，`scheduler_core.py:2863`）：

1. `config/unified_config.yaml::scheduler` 新增两键（**默认 false，落地即惰性**）：
   - `playbook_auto_execute_fast_loop_enabled: false`
   - 复用既有 `playbook_auto_execute_interval_sec: 180`（:2091）
2. `run_forever` spawn 新线程 `PlaybookAutoExecuteFastLoop`（flag=true 才起）；
3. `_llm_strategy_loop` 旧调用点加单宿主互斥（flag=true 时跳过，防双线程重复下单，
   同 exit fast loop `scheduler_core.py:2313` 模式）；
4. watchdog 注册 `auto_execute_loop`，stale 阈值 900s（名义 180s × 5；exit loop
   先例 interval=10s/stale=120s=12×，5× 在既有包线内）；
5. 幂等既有（已核实）：指令 `executed=1` 跳过（pipeline.py:3974 docstring）+
   `PlaybookService._SYMBOL_ORDER_LOCKS` per-symbol 互斥（playbook_service.py:1261）
   + 每轮预算重算（pipeline.py:4080+ 短连接现查）。

**测试**：`tests/test_auto_execute_fast_loop.py`
- flag=false ⇒ 零行为差异（不 spawn、旧调用点照常）；
- flag=true ⇒ 旧调用点跳过 + 新线程 180s 节流；
- **首跑语义（v2 新增）**：线程启动后首轮立即执行还是等 180s — 二选一钉死进
  测试，不允许实现期自由发挥（建议首轮立即，与 exit loop 先例对齐）；
- watchdog 注册 + 心跳；
- 线程异常隔离不外溢。

**回滚**：flag=false + restart（旧宿主恢复，单键热载不了线程 spawn，必须重启）。
**回滚实演（v2 新增）**：批3 激活前，须在盘后完整实演一次
flag=true→restart→观测→flag=false→restart，没演练过的回滚等于没回滚。

---

## 4. 批3：激活 = 用户裁决点（v2 改写，默认不激活）

**为什么不是流程执行（v2 核心修订）**：原激活窗 10/13-16 ⊂ OKR-2026-10 判分窗
（9/17-10/31）。字面上不破 KR1（判据=退出哈希唯一，批3 不动退出逻辑）；但实质是
窗口期内的**入场侧时序 regime 切换**，而哈希体系只版本化退出逻辑 ⇒ "哈希唯一、
行为两版"的隐性混样 — KR2-09 BROKEN 的教训正是判据与事实分叉。10 月 cohort 纯度
是 KR1（变更预算 0 + 移窗禁令）的立法意图，优先于入场延迟优化（9/15 用户已裁
"缩短门禁解除→进场延迟"，方向授权在，但时点须裁）。

**三态裁决（到时按顺序取最先满足者）**：

- **(c) 数据销案【默认】**：A1 实测 p90 ≤ 300s 且无单次 >600s ⇒ 串行宿主对
  auto_execute 的 starvation 不显著 ⇒ 批2/批3 一并销案，flag 永留 false；
- **(a) 延后至 KR2 出结论**：A1 数据显示确有必要，但 200 笔窗口未关 ⇒ 激活顺延至
  KR2 三态结论落地后（与批5 同窗，cohort 全程单 regime，最干净）；
- **(b) 窗口内激活【须开跑会级裁决】**：仅在 A1 数据证明入场延迟正实质吃掉成交价
  质量、且用户明确裁决时。激活前必须：① OKR-2026-10 变更记录落行（"入场侧时序
  regime 切换，退出哈希不变"）；② KR2 分析预案写明按 A1 遥测做 regime 分组
  敏感性检查；③ 下文量化核验判据预写。

**若走 (b) 的激活流程**：改 flag → restart → 冒烟 → 首小时三项量化核验
（**v2 预写判据**：auto_execute 实际间隔 p90∈[170,250]s、指令幂等重复成交=0、
watchdog stale 告警=0）。任一缺位 ⇒ flag 回 false + restart。
窗口选择：无在途出场、非财报密集日、ET 10:00-11:00（避开开盘 5min 缓冲与尾盘
PRE_CLOSE）。

---

## 5. 批4（10/20 前）：DST cron 审计

**问题**：crontab 多处 ET 窗口已自注"11 月重评清单"（error_alert 四窗、
eth_event_plan、盘后作业组等）。11/1 冬令时后 CST↔ET 偏移右移 1h，
未审计 = 已知盲区带日期。

**v2 勘误**：EXT 的 16-22/4-8 两行是 9/22 复查批**已落地的 DST 免疫扩段本身**
（窗外 tick 在 `_in_session_window` 即返回，零 Futu 连接），审计中标注"已免疫"
即可，不是待改项。

**实施**：
1. 产出 `docs/ops/DST-CRON-AUDIT-20261101.md`：逐行列出 cron 表达式、当前 ET 语义、
   11/1 后 ET 语义、缺口/重叠判定；
2. 只调窗口行（不动业务逻辑），每行带回滚注释；
3. 备份 `data/crontab.backup.20261020_dst.txt`；
4. 10/27 前复核一轮（DST 前 5 个交易日观察）；
5. **v2 新增**：范围扩一条 — 代码内硬编码 UTC 偏移/EDT 假设 sweep（对照时钟病灶
   台账：0924 新形态 18 处的兜底白名单在 EST 下语义是否仍成立），grep 清单随
   审计文档落档；
6. **v2 新增**：11/2（首个 EST 交易日）盘前人工核对一次实际触发时刻 vs 审计表。

**难度**：低（纯运维 + 表格），但必须逐行人工核对，禁止批量 sed。

---

## 6. 批5（OKR-10 结算后另立 DEB，本方案不动）

| 项 | 为什么现在不动 |
|---|---|
| admission 收口到 `PlaybookDB.save_instruction`（D2/P1-5） | 写入方语义变更，须 DEB + 影子 |
| `pipeline.py` 继续拆分（admission/门链/mirror 臂） | 纯搬家可以做，但与批2争同一文件，串行避冲突 |
| 三态显式状态机 + 切态演练（D4/P1-4） | 触及 LIVE 路由，必须四锁全关下 dry-run 演练 |
| `trailing_stop` 双路径收口（D3/P1-2） | 已知 landmine，单独改会关掉实际止损 |

---

## 7. 本方案明确不做（红线）

1. 不调仓位 / 频率 / SL / TP / 日帽 — OKR-10 参数冻结 + "禁用仓位频率追样本"；
2. 不加第二个新入场假设（C1 限额已在用）；
3. 不动 `auto_trading_enabled` 三处裸 regex 读法（刻意设计）；
4. 不复活主管线下单或 ORB 残留；
5. 不在冻结窗（10/5 盘前-收盘）落任何运行时代码；
6. **【v2】批3 默认不激活** — 激活是用户裁决点（§4 三态），不是流程执行。

---

## 附录 A：v2 复查事实核实表（2026-10-04，Claude Code）

| 计划声称 | 实证 | 结论 |
|---|---|---|
| exit fast loop 模式 `scheduler_core.py:2864` | def 在 :2863，watchdog stale=120 在 :2876 | ✅（行号已校准） |
| 单宿主互斥 `scheduler_core.py:2314` | `if not self.PLAYBOOK_EXIT_FAST_LOOP_ENABLED:` 在 :2313 | ✅（已校准） |
| 幂等 executed=1 + per-symbol 互斥 | pipeline.py:3974 docstring；playbook_service.py:1261 `_SYMBOL_ORDER_LOCKS` | ✅ |
| `unified_config.yaml::scheduler` 180s 键 | 实际路径 `config/unified_config.yaml:2091` | ⚠️ 路径已修正 |
| smoke `--wait`/`--push`/120s 防假阳性 | :191-204 均在；--push 失败被吞不改 exit code（:223-228） | ✅ |
| systemd Type=simple / TimeoutStartSec=120 / start.sh `exec python` | unit 文件与 start.sh 末行均属实；StartLimit 20/600、RestartSec=30 | ✅ |
| crontab "11月重评" 标注 | error_alert 四窗/eth_event_plan/盘后组多处属实；EXT 16-22/4-8 是 9/22 免疫扩段本身 | ✅+勘误 |
| 冻结窗"树内无新增运行时代码" | 运行进程 16:03:56 启动晚于 RFC-060 脏文件 mtime 00:53 ⇒ 树=运行时一致 | ✅（批1 前须处置脏树） |
| 9/15 注释"单轮 p50=1s/p90=9s" | scheduler_core.py:2288 — 是 auto_execute **单次调用耗时**，非间隔分布；A1 补的正是间隔维度 | ✅ 互补 |

## 附录 B：启动段耗时实测（B 节 `--wait 90` 定量依据）

口径：`[StartupMode]` 横幅 → `[PlaybookExit] 快循环已启动`（七项最后出现的标记），
数据源 logs/ftseek_us.log + 轮转档，n=20（2026-10-01 ~ 10-04，含盘中 RTH 样本）：

| 统计 | 值 |
|---|---|
| min / p50 / max | 9.8s / 11.2s / 14.2s |
| 加 start.sh 前置（fork→横幅，10/4 实测） | ~7s |
| 加冒烟 5s 轮询粒度后的端到端上界 | ≲25s |
| `--wait 90` 余量 | >3.5× |
| TimeoutStartSec=180 对 ExecStartPost 上界（timeout 100 硬壳） | 余量 80s |

结论：`--wait 90` / `TimeoutStartSec=180` / `timeout 100` 三值照计划落地，无需调整。

## 附录 C：批2 前置交付物 — auto_execute 相位依赖矩阵（已完成）

**函数**：`app/playbook/pipeline.py:3964 run_playbook_auto_execute(scheduler, *, force=False)`

**C-1. 对 scheduler 对象的依赖：零**（已核实 3964-4380 行无一次 `scheduler.` 访问）⇒
fast loop 传同一 self 即可，无属性可用性/时序问题。

**C-2. 每轮自包含输入（读侧，均不依赖循环先前相位的内存态）**：

| 输入 | 来源 | 并发安全性 |
|---|---|---|
| `playbook.auto_execute` 等配置 | config 热容器 / playbook config | 既有热载机制 |
| 交易日/ET 时钟 | `_today_et_str`/`_is_trading_day_today`/`_now_et` | 纯函数 |
| bridge 健康 | `_check_bridge_health_and_alert`（只读+告警，日去重） | ✅ |
| kill switch | 标记文件直读（fail-close，与 order_executor 同函数同口径） | ✅ |
| regime / risk_state | `MarketRegimeGate()`/`RiskStateMachine()` 现构造 | ✅ |
| 日亏熔断 / 预算 / 日帽 / 在仓数 | trade_records DB，`safe_sqlite_connect` 短连接 timeout=5，失败 fail-close | ✅ exit loop 先例 |
| capital_safety 预算 | 单例 `get_capital_safety_system()`，已被 scan/monitor/llm 多线程共享 — 批2 不引入新并发类别 | ✅ |
| 今日指令 | `route_playbook_db("sim")` 每次新建 PlaybookDB 实例 | ✅ |

**C-3. 唯一跨相位耦合边（DB 中介）**：指令表 ← 先前相位写入
（generate/topup/normalize/再入场 re-arm，TRADING 分支现行顺序
generate→morning_push→premarket_invalidation→topup→normalize→auto_execute）。
fast loop 化后语义变化：不再保证"同轮内 auto_execute 排在定桶之后"，改为 180s 轮询
见到即执行 — 最差情形是新指令多等 ≤180s，平均反而**更快**（不再等整轮）；指令
生成侧均幂等（ catch-up 同名调用既有），无丢失风险。

**C-4. 反向依赖：零**。再入场扫描/退出监控读 DB 与 broker，不读 auto_execute 的
内存产物。

**C-5. 入口唯一性**：全仓唯一调用点 scheduler_core.py:2296（已 grep 核实无 dashboard
路由/startup catch-up/其他入口）⇒ 单宿主互斥只需处理该点。

**C-6. 残留风险清单（批2 实施时逐条过）**：① 首跑语义（立即 vs 等 180s）钉测试；
② 与再入场扫描的并发由 executed=1 + SymbolLock 双保险覆盖；③ 下单汇合点
（pipeline.py:3788 记录器）保持唯一。
