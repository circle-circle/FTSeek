r"""裸 datetime.now() / utcnow() 棘轮守卫 (2026-09-05, DEB-20260905 M-6/B1 第一阶, 用户裁"需要").

背景 (REPORT G-16 / T1): 服务器本地时区是 CST (Asia/Shanghai), 交易口径是 ET。裸
`datetime.now()` 写出的是 **naive CST 墙钟**, 一旦落库/落盘并与 ET 或 UTC 口径的值比较,
就是 12h/8h 级错位 — 实证三例: ①账本 `created_at` 归日偏移 (因子研究样本贴错日期);
②`trade_store.get_pending_sync_positions` 把 CST 墙钟交给 SQLite `datetime('now')`(UTC) 比较
→ 判据 8h 内恒 False (D-06, 本批已修); ③`trade_records.exit_time` 混存 394 行 ET-aware /
327 行 naive / 1 行 +00:00 → 任何 `substr(exit_time,1,10)` 的分桶都不可照搬全期。

纪律 (与 `tests/test_except_pass_ratchet.py` 同款棘轮): 存量分批清剿, **基线只降不升**;
新增必须失败。选**数值棘轮**而非逐文件白名单: 127 个文件的清单维护成本高于收益,
而数值棘轮已能拦住任何净增长 (DEB 原文的"白名单制"以此实现同等约束)。

──────────────────────── B1 存量清剿的可行次序 (本批盘点结论, 供后续批次执行) ────────────────────────
app/ 下裸 `datetime.now()` = **455 处 / 127 文件** (另 `datetime.utcnow()` 4 处, 同类病灶)。
**不可一次全仓翻**: 把写入方从 naive-CST 改成 aware-ET 会让同一列**混存两种格式**, 而
ISO 字符串的字典序在混存时**不再等于时间序** — 实证: `2026-09-05T06:00:00-04:00` (=18:00 CST)
字典序**小于** `2026-09-05T17:00:00` (=17:00 CST, 更早的瞬间) ⇒ 任何 `ORDER BY <该列>` 或
`substr(col,1,10)` 分桶在切换日附近会错序/错日。故清剿必须**按列 (而非按文件) 推进**,
每列先审全部消费方 (排序/分桶/字符串比较/展示) 再翻, 且翻转当口需一次数据归一或打标。

建议次序 (风险从低到高, 每批把 BASELINE 调低):
  1. **纯信息位** (无解析无比较, 翻转零风险) — 本批已做: `daily_loss_baseline.json` 的
     `ts`/`flip_at` (pipeline `_check_daily_loss_circuit`, 唯一读点是原值透传)。
  2. **死变量** — 本批已做: pipeline `_force_close_orphaned_sim_positions` 的 `now_iso`
     (赋值后全函数零引用, 删)。
  3. **已合规的 fallback 保留不动** — pipeline:6135 `now_iso = datetime.now().isoformat()`
     在 `except` 分支, 主路径是 `datetime.now(_now_et())` (2026-07-03 修, "与 entry_time
     格式对齐"); 该处计入基线但**不应清剿** (清剿=让异常路径也炸)。
  4. **单列闭环** (写方与读方同批改, 读方做成格式兼容) — **已完成 (2026-09-05 B1 第三阶)**:
     `last_sync_at` 读方 `_parse_sync_ts` (兼容 naive-CST 与 aware, NULL/脏值 fail-loud) +
     写方 **6 处**全改 `now_et()`。
     **盘点更正 (复算义务)**: 第一阶记的"写方 3 处"是**漏盘** — 除 `mark_sync_pending` /
     `mark_sync_complete` / `mark_anomalous` 外, 还有 `upsert_open_position` (**最热路径**,
     16 个调用点) / `unmark_anomalous` / `update_position_field`。**只翻 3 处会让该列永久混存**
     (而非过渡期混存) = 把本节开头警告的错序风险固化进稳态。
     **该列可一次翻完的实证** (审计全部消费方): 全仓**零** `ORDER BY last_sync_at`、零
     `substr(last_sync_at,…)`、零字符串比较; 唯一语义读方 `_parse_sync_ts` 已双格式兼容;
     唯一展示读方 `position_monitor:458` 把它塞进 `metadata["_close_blocked_at"]`, 而该 key
     **全仓零读取点** (3 处写 + `order_executor:581` 一处 pop) ⇒ 纯遥测, 且该 key 本已三处异型
     (aware-ET 串 / naive 串 / `time.time()` float)。另: `last_sync_at` 在 web_dashboard 两条
     INSERT (`order/executor.py:42` SQL_TAKEOVER、`order/audit.py:473` stub) 的列清单里**都不存在**
     ⇒ 6 处即全仓完整写方集合, 本阶可真正闭环。
     **无需数据迁移的实证**: 现库 3 行 (UBER/RIVN/HOOD) 该列**全为 NULL** — 活行由
     `upsert_position_simple` 写 (R-12 已用 `now_et()`, `entry_time`/`updated_at` 同值), 而其列清单
     不含 `last_sync_at` ⇒ 既无历史 naive 行需归一, 也不存在切换日错序窗口。
  5. **账本列** (`trade_records.created_at` / `open_positions.updated_at` 等) = **B1 第五阶**
     (编号更正: 第三阶稿件曾把本项称"第四阶", 而 §11.7 路线图里第四阶已用于别名裸 now 清剿 ⇒
     现统一为**第五阶**, 见 DEB §12.6; `trade_store.py` 那条 ⚠️ 尺度债注释与本文件末尾的债标记
     测试已同步改名, 避免下一位执行者误以为账本列就在当前阶范围内而"顺手翻") — 最高危,
     须先出消费方清单 + 归一方案, 单独 DEB 裁决 (DEB M-6 已注: B1 完成前的行在复盘查询中
     以 `entry_time` 而非 `created_at` 归日)。**第三阶新取证 (证实"最高危"不是修辞)**:
     `updated_at` 有真消费方做**字符串排序与比较** — `position_monitor:1229`
     `ORDER BY updated_at DESC LIMIT 1`、`position_monitor:1164` `updated_at>=?`; 且该列
     **已经在混存** (`upsert_open_position` 写 naive vs `upsert_position_simple` 写 `now_et()`),
     不是"翻后才混存" ⇒ 归一/打标方案必须**先于**翻转。`trade_store.py` 已在该 INSERT 处留
     ⚠️ 尺度债注释指回本节。实测 `trade_records.created_at` 亦为 naive CST 而同行 `entry_time`
     为 aware ET (US.HOOD: created_at=2026-09-05T01:30:12 vs entry_time=2026-09-04T13:30:12-04:00,
     同一瞬间两种尺) ⇒ 与 DEB M-6 那条"以 entry_time 归日"的注记互为印证。

──────────────── 守卫自身的两个已知边界 (第三阶补, 别踩) ────────────────
① **纯文本正则, 刻意不 strip docstring/注释**。取舍: `tokenize`/AST 方案会把 f-string 里的
   真调用 (`f"{datetime.now()}"` 在 py3.11 tokenize 下整段是 STRING) **漏计 = 假阴性**, 比
   假阳性危险得多; 故宁可让文档里的字面量也计入。代价是一条写作约定: **`app/` 下新增注释或
   docstring 若要提及这个写法, 用不带括号的 `datetime.now` 或"裸 now"表述**, 否则会抬高
   BASELINE_NOW 而与"只降不升"冲突。本批即按此约定写 — 顺带把第一阶留在 `_parse_sync_ts` 与
   `get_pending_sync_positions` docstring 里的 2 处字面量改写掉, 故 455→446 中有 2 处是文档而非代码。
   (本测试文件在 `tests/` 下, 不在扫描根 `app/` 内, 故头注可自由写字面量。)
② **别名盲区已堵 (第三阶)**。原正则只咬 `datetime.now()`, 而 `from datetime import datetime as _dt`
   之后的 `_dt.now()` **完全不计入** ⇒ "把 `datetime.now()` 改名成 `_dt.now()`" 就能让棘轮变绿而
   病灶原样保留 = 一条真实可用的规避通道。实测 `app/` 下别名裸 now **15 处** (`_dt.now()` 14 +
   `_datetime.now()` 1), 本批新增 `BASELINE_NOW_ALIAS`/`BASELINE_UTCNOW_ALIAS` 独立钉住。
   同时给主正则加 `(?<!\w)` 负向前瞻: 否则 `_datetime.now()` 含子串 `datetime.now()` 会被**双计**
   (实证 `app/strategy/regime_filter.py` 旧正则多计 1 ⇒ 第一阶的 455 里有 1 处是重复计数,
   新口径下的等价历史基线是 454)。`test_alias_patterns_are_disjoint_from_plain` 钉住互斥性。
   **第四阶已逐点归类并清剿: 15 → 11 (见下节)**。

──────────────── 第四阶: 别名裸 now 15 处的逐点归类 (2026-09-05) ────────────────
按"每处先判归属再决定动不动"的纪律逐点核过, 结果与"15 处清剿"的原计划**严重不符** —
真正可翻的只有 **4 处**, **10 处是豁免**, 还有 **1 处是必须留、且要立契约**的风控闭环:

- **A 已合规 except 兜底 (5 处, 永久豁免)**: 主路径已是 `now_et()`, 清剿兜底 = 让异常路径也炸
  (同 §次序 3 的 `pipeline:6135` 判例)。站点: `signal_pipeline:1251` / `position_monitor:1019` /
  `trade_store:997` / `playbook/pipeline:2316` / `playbook/pipeline:5202`。
- **B 注释里的字面量 (3 处, 不是代码)**: 纯文本正则的既定代价 (见边界①)。这些注释正是 R-8a
  (2026-08-23 DEB-20260823) 修复的留痕, 记着"原用裸 now = 宿主 CST, 实测 15,150 行中 9,049 行
  (59.7%) 错位"这类实证数字, 删掉是纯信息损失。站点: `trade_store:987` /
  `playbook/pipeline:3507` / `playbook/pipeline:5120`。
- **C 刻意 CST 的自证字段 (2 处, 豁免)**: 字段名就叫 `ts_cst`, 与并列的 `"tz": "ET"` 一起构成
  "ET 主口径 + CST 对照列", 用途是**与 08-23 之前的历史行对齐排查** (源码注释自陈) —
  正属棘轮失败提示语里"确属本地墙钟语义"那一类。站点: `playbook/pipeline:3513` / `:5139`。
- **D 风控闭合环路 (1 处, 刻意不翻 + 立契约)**: 见下段, 第四阶最重要的发现。
  站点: `emergency_interceptor:425`。
- **E 真实病灶 (3 处, 本阶已翻)**: 都是**读侧算日期串**、只喂给 WHERE 而**不写库** ⇒ 零混存风险,
  无需归一方案 (与账本列本质不同)。而它们过滤的列都是 **ET 口径**:
  `trade_records.trade_date` (写方 `_et_trade_date(position.entry_time)`: order_executor:5254/:5370、
  position_monitor:1585) 与 `signal_snapshots.trade_date` (DDL 自注 "(YYYY-MM-DD, ET)", 写方
  `get_trade_date()`)。原按宿主 CST 取"今天", 在 **ET 12:00-23:59 (北京次日 00:00-11:59)** 会把
  cutoff 推后 1 天 ⇒ "近30日"实际 29 日、IC 样本窗静默少一天、当天 ET 信号被提前当成熟样本回填。
  其中 `deep_target_analysis` 的数字是**注入 LLM prompt 的校准表述** ⇒ 口径失真即误导。
  站点 (原盘点行号 → 翻后现位, 因插入注释而下移): `deep_target_analysis` :37 → **:45** /
  `signal_ic_tracker` :485 → **:492** / `signal_ic_tracker` :762 → **:776**。
  改法: 前两处用 `now_et()` 做同款日历日算术; 第三处**直接调写方同一个 `get_trade_date()`**,
  同尺由构造保证而不靠人工记着两边一致。
- **F 死表写方 (1 处, 本阶已翻)**: `regime_filter` (原 :109 → 现 **:119**) 写
  `regime_history.timestamp`/`created_at`。
  该表经 `ops/hermes/intraday_diagnose_late.py:198` 于 2026-09-05 核查判定为**死表** (唯一写者链
  playbook 化后停跑, 8/23-9/5 全部轮转日志 RegimeHysteresis=0 行, 最后一行停在 2026-07-29,
  生产代码零读者) ⇒ 本改**零运行时行为变更**; 价值在除后患 —— 该表建有
  `idx_regime_ts ON regime_history(timestamp)` = 明示按 timestamp 排序/范围查的意图, 若日后复活
  而列内混存即错序。零读者 ⇒ 历史 naive 行无需归一。

**D 类 = 一个真陷阱 (`data/risk_audit.json` 的 `time` 字段)**: 它构成**闭合的 naive-CST 环路** —
写方 `risk_state_machine._audit_log:333` 与两个读方 (`restore_state:85`/`:100` 的 **4h 恢复窗**、
`emergency_interceptor._is_same_risk_off_recently:425`/`:432` 的 **2h 同-reason 去重**) **三处同尺
⇒ 当前行为是正确的**。单翻任意一侧 ⇒ `aware - naive` 抛 TypeError, 而读方2 的
`except Exception: return False` 会**静默吞掉** ⇒ RISK_OFF 持久化去重永久失效 = 进程重启后同一条
新闻反复触发 RISK_OFF, **恰好是这段 P0(2026-06-05) 代码被写出来要解决的 bug 原样复活**;
读方1 则不再恢复 RISK_OFF ⇒ **风控失忆 (放松方向, 更危险)**。
故立 `tests/test_risk_audit_time_scale_contract.py` (7 例) 钉住同尺不变量, 设计成
**原子翻容忍 / 单侧翻必红**: 尺度由源码动态推导 (含 `now_et as _now_et_xxx` 别名宽容), 三处一起翻
仍全绿, 只翻一侧则同尺测 + 两条行为测一起红, 另有 4 条负对照防"函数恒真"式空过。
**双向证伪实跑过**: ①单翻写方 → 日志赫然出现
`can't subtract offset-naive and offset-aware datetimes` 且被 except 吞掉, 3 红;
②三处原子翻 → 7 绿 (该轮还撞出"分类器不认 `now_et` 别名导入"的缺陷并已修 — 双向证伪的第二条
价值正在于此: 只跑单侧翻不会发现守卫自己会把正确改动判红)。
现况取证: 该文件 27 条记录 `time` **全 naive** (如 `2026-06-06T03:00:07.020743`), 最后写入
**2026-06-12 22:30** ⇒ 路径已冷近 3 个月, 今天零血量, 属 **dormant landmine** (下一次 RISK_OFF
触发即复活)。日后要翻的前置条件: ①三处同一提交内**原子翻**; ②读方先加**容错解析**
(naive 一律按 CST 解释, 照 `trade_store._parse_sync_ts`), 因磁盘存量 naive 记录不会自清 —
`_audit_log` 只做 `logs[-100:]` 截断, 而该文件 3 个月才 27 条。

**豁免台账已做成可执行** (`_ALIAS_EXEMPTIONS` + `test_alias_exemptions_ledger_is_exact`):
11 处逐条登记 (路径 / 行内锚点 / 期望计数 / 类别 / 理由), 并断言
**Σ期望计数 == 实测别名总数** ⇒ 任何一处新出现的别名裸 now 都会因"台账对不上"而红
(不只是"数字变大"而红, 报错直接指出是哪一处没登记); 删掉或重构任一豁免点同样会红 (锚点失配),
逼迫同步更新台账而不是让它悄悄腐化。

回滚: 删本文件 (回到无管控状态, 基线与盘点结论在本头注留档)。
"""
import pathlib
import re
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_NOW = 435     # 2026-09-21 全链路复查: 445 → 435 (实测) — 本批 −2:
                       #   position_monitor manual_order_audit 日期查询 fallback (原裸 CST
                       #   墙钟, ET 盘中 12:00 后日期错位一天) 改 zoneinfo ET→UTC 降级;
                       #   position_exit_monitor.is_us_market_open 的 ET=None fallback
                       #   (原裸 CST 墙钟, 09:30-16:00 窗口判断整体错位) 改走 now_et()。
                       #   其余 −8 为 2026-09-05 钉基线后各批已清剿但未收紧的存量, 本次一并钉死。
                       # 2026-09-05 §十三 精度批: 446 → 445 — 只降不升。本阶 −1:
                       #   `position_intelligence._calc_hold_days` 的裸 now 被 `now_et()` 取代
                       #   (该处正是缺陷1 的 +12h 偏置源头: 截断偏移后的 naive ET 墙钟与
                       #   naive CST 墙钟相减)。同批新增的 2 处 docstring 提及按头注边界①
                       #   约定写成不带括号的 `datetime.now`, 故不计入本正则。
                       # 2026-09-05 B1 第三阶: 455 → 446。构成: last_sync_at 写方 6 处
                       #   翻 ET (−6) + 2 处 docstring 字面量按头注边界①约定改写 (−2) + 主正则加
                       #   (?<!\w) 修掉 `_datetime.now()` 的 1 处双计 (−1)。旧正则口径下的等价历史
                       #   基线是 454, 故该阶真实清剿量 = 454 − 446 = 8。
BASELINE_UTCNOW = 4    # 同类病灶 (naive UTC), 未清剿, 先钉住不增长
BASELINE_NOW_ALIAS = 11    # 别名裸 now — 第三阶新增 (堵"改名规避棘轮"通道), 第四阶 15 → 11:
                           #   翻掉 4 处真病灶 (3 处读侧日期串 + 1 处死表写方), 余 11 处**全部**由
                           #   下方 _ALIAS_EXEMPTIONS 台账逐条解释 (5 except 兜底 + 3 注释 +
                           #   2 刻意 ts_cst + 1 风控闭合环路), 无一处"未解释"。
BASELINE_UTCNOW_ALIAS = 0  # 别名裸 utcnow — 实测为 0, 钉在 0 (防同一规避通道换到 utcnow)
_SCAN_ROOT = PROJECT_ROOT / "app"
_EXCLUDE_PARTS = {"venv", "node_modules", ".git", "tests", "archive",
                  "__pycache__", ".backup_premigration", "obsidian_vault"}
# (?<!\w) 负向前瞻是必需的: `_datetime.now()` 含子串 `datetime.now()`, 不加就会被主正则与别名
# 正则**双计** (实证 app/strategy/regime_filter.py 曾因此多计 1 处)。互斥性由
# test_alias_patterns_are_disjoint_from_plain 钉住。
_PAT_NOW = re.compile(r"(?<!\w)datetime\.now\(\)")
_PAT_UTCNOW = re.compile(r"(?<!\w)datetime\.utcnow\(\)")
_PAT_NOW_ALIAS = re.compile(r"\b(?:_dt|_datetime|_DT|dt)\.now\(\)")
_PAT_UTCNOW_ALIAS = re.compile(r"\b(?:_dt|_datetime|_DT|dt)\.utcnow\(\)")

# ── 别名裸 now 的**豁免台账** (第四阶逐点归类的产物, 头注有完整判定依据) ──────────
# 每条 = (相对路径, 行内锚点, 期望出现次数, 类别, 理由)。
# test_alias_exemptions_ledger_is_exact 断言 Σ期望次数 == 实测别名总数 ⇒ 台账必须**穷尽**:
# 新增一处未登记的别名裸 now 会红 (而不只是数字变大), 删/重构任一豁免点也会红 (锚点失配)。
_ALIAS_EXEMPTIONS = [
    ("app/pipeline/signal_pipeline.py",
     '_signal_date = _dt.now().strftime("%Y-%m-%d")', 1, "A except 兜底",
     "主路径 :1249 已是 now_et(); 清剿兜底=让异常路径也炸 (同 pipeline:6135 判例)"),
    ("app/pipeline/position_monitor.py",
     '_today = _dt.now().strftime("%Y-%m-%d")', 1, "A except 兜底",
     "主路径 :1017 已是 now_et(); 同上"),
    ("app/storage/trade_store.py",
     '_now = _dt.now().isoformat()', 1, "A except 兜底",
     "upsert_position_simple 主路径已是 now_et() (R-12 2026-08-23); 同上"),
    ("app/playbook/pipeline.py",
     'today = _dt.now().date().isoformat()', 1, "A except 兜底",
     "晨报推送主路径 :2314 已是 now_et(); 同上"),
    ("app/playbook/pipeline.py",
     '_ts = _dt.now().isoformat(timespec="seconds")', 1, "A except 兜底",
     "主路径 :5196 已是 _dt.now(_now_et()) 带尺; 清剿兜底=让异常路径也炸"),
    ("app/storage/trade_store.py",
     '原用裸 _dt.now() = 宿主 Asia/Shanghai (CST)', 1, "B 注释非代码",
     "R-12 修复留痕 (记着 entry_time 两表差 12h 的实证), 删掉是纯信息损失"),
    ("app/playbook/pipeline.py",
     '原用裸 _dt.now() = 宿主 Asia/Shanghai, ET 下午的', 1, "B 注释非代码",
     "R-8a 修复留痕 (exec_gate_audit 归因), 同上"),
    ("app/playbook/pipeline.py",
     '原用裸 _dt.now() = 宿主 Asia/Shanghai。ET 12:00-15:59', 1, "B 注释非代码",
     "R-8a 修复留痕 (含 15,150 行中 9,049 行=59.7% 错位的实证数字), 同上"),
    ("app/playbook/pipeline.py",
     '"ts_cst": _dt.now().isoformat(timespec="seconds"),', 2, "C 刻意 CST 自证字段",
     "字段名即 ts_cst, 与并列的 tz='ET' 构成 ET 主口径 + CST 对照列, 用于与 08-23 之前历史行"
     "对齐排查 (源码注释自陈) — 属棘轮提示语里'确属本地墙钟语义'那一类"),
    ("app/us_pipeline/emergency_interceptor.py",
     'now = _dt.now()', 1, "D 风控闭合环路",
     "与 risk_state_machine._audit_log:333 (写) / restore_state:85 (读) 三处同尺 = 当前正确; "
     "单翻任一侧 → aware-naive 抛 TypeError 被 except 静默吞 → RISK_OFF 去重永久失效。"
     "由 tests/test_risk_audit_time_scale_contract.py 钉住 (原子翻容忍/单侧翻必红)"),
]

def _scan():
    """返回 {相对路径: (裸 now, 裸 utcnow, 别名裸 now, 别名裸 utcnow)} (仅含有命中的文件)."""
    per = {}
    for path in _SCAN_ROOT.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        counts = (len(_PAT_NOW.findall(text)), len(_PAT_UTCNOW.findall(text)),
                  len(_PAT_NOW_ALIAS.findall(text)), len(_PAT_UTCNOW_ALIAS.findall(text)))
        if any(counts):
            per[str(path.relative_to(PROJECT_ROOT))] = counts
    return per


def _top(per, idx, k=8):
    return sorted(((v[idx], k_) for k_, v in per.items()), reverse=True)[:k]


def _alias_hit_lines():
    """别名裸 now 的**行级**事实来源: {相对路径: [[行号, 未核销命中数, 行文本], ...]}.

    台账核销按行做, 故需要行级计数; 它与 `_scan()` 的整文计数必须一致 (下方 ① 断言), 否则说明
    出现了跨行写法 ⇒ 单行文本的锚点会"看不见"该命中, 台账即失真。
    """
    out = {}
    for path in _SCAN_ROOT.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in path.parts):
            continue
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        rows = []
        for lineno, line in enumerate(lines, start=1):
            hits = len(_PAT_NOW_ALIAS.findall(line))
            if hits:
                rows.append([lineno, hits, line.strip()])
        if rows:
            out[str(path.relative_to(PROJECT_ROOT))] = rows
    return out


def test_naive_datetime_now_not_growing():
    per = _scan()
    current = sum(v[0] for v in per.values())
    assert current <= BASELINE_NOW, (
        f"app/ 下裸 datetime.now() 增长: {BASELINE_NOW} → {current} (naive = 服务器 CST 墙钟, "
        f"与 ET/UTC 口径比较即 8~12h 错位)。请改用 app.utils.timezone 的 now_et()/now_utc(),"
        f"或 datetime.now(CST)/datetime.now(ET) 显式带尺; 确属本地墙钟语义 (文件名/日志) 时"
        f"把 BASELINE_NOW 保持不变并在提交信息说明理由。\n"
        f"当前重灾文件: {_top(per, 0)}")


def test_naive_datetime_utcnow_not_growing():
    per = _scan()
    current = sum(v[1] for v in per.values())
    assert current <= BASELINE_UTCNOW, (
        f"app/ 下裸 datetime.utcnow() 增长: {BASELINE_UTCNOW} → {current} "
        f"(naive UTC 与 datetime.now() 同族病灶, 且 3.12 起已弃用)。改用 now_utc()。")


def test_naive_alias_now_not_growing():
    """别名裸 now (`_dt.now()` 等) 不得增长 — 否则"改名"即可绕过棘轮 (头注边界②)."""
    per = _scan()
    current = sum(v[2] for v in per.values())
    assert current <= BASELINE_NOW_ALIAS, (
        f"app/ 下别名裸 now 增长: {BASELINE_NOW_ALIAS} → {current}。把 `datetime.now()` 改写成 "
        f"`_dt.now()` **不改变病灶** (仍是 naive 本地墙钟), 只躲开了主正则 — 本守卫同时钉住两种写法。"
        f"请改用 now_et()/now_utc() 或显式带尺。\n当前命中: {_top(per, 2)}\n"
        f"若属**刻意豁免** (except 兜底 / 注释留痕 / 刻意 CST 自证字段 / 同尺闭合环路), 须同时登记进 "
        f"_ALIAS_EXEMPTIONS 台账 (路径/行内锚点/次数/类别/理由) — 见 "
        f"test_alias_exemptions_ledger_is_exact, 台账必须穷尽到每一处命中。")


def test_naive_alias_utcnow_not_growing():
    """同族规避通道的 utcnow 侧, 基线钉在 0."""
    per = _scan()
    current = sum(v[3] for v in per.values())
    assert current <= BASELINE_UTCNOW_ALIAS, (
        f"app/ 下别名裸 utcnow 增长: {BASELINE_UTCNOW_ALIAS} → {current}。改用 now_utc()。")


def test_alias_exemptions_ledger_is_exact():
    """`_ALIAS_EXEMPTIONS` 必须是**穷尽且精确**的可执行台账, 不是注释性清单.

    为什么值得单列一条测试: 第四阶把 15 处别名裸 now 判成"4 处真病灶 + 11 处豁免"。豁免一旦只是
    注释, 就会两种腐化: ①新增一处别名裸 now 时, 数字棘轮只说"变大了", 不说是哪一处、也不逼人给出
    理由; ②豁免点被删除/重构后, 台账继续留着一条指向不存在代码的理由 = 死账。本测试把两条都堵死:
    - **未登记的新增** ⇒ 有命中行未被任何锚点核销 ⇒ 红, 报错直接给出 `file:line` 与该行文本;
    - **豁免点被删/重构** ⇒ 锚点计数失配 ⇒ 红, 逼迫重新逐点判定并同步台账与 BASELINE_NOW_ALIAS;
    - **锚点写错 (自身不含别名裸 now)** ⇒ 红, 防止台账用空锚点凑数。
    """
    per = _scan()
    rows_by_file = _alias_hit_lines()

    # ① 守卫自证: 行级与整文两种口径必须同集合、同计数 (否则台账核销失真)
    assert set(rows_by_file) == {p for p, v in per.items() if v[2]}, (
        "行级扫描与整文扫描的命中文件集合不一致 — _alias_hit_lines / _scan 有一侧的扫描根或"
        "排除词表被改动, 台账核销已失真")
    for path, rows in rows_by_file.items():
        assert sum(r[1] for r in rows) == per[path][2], (
            f"{path}: 行级别名命中 {sum(r[1] for r in rows)} != 整文命中 {per[path][2]} — 可能存在"
            f"**跨行**的别名裸 now 写法, 而台账锚点是单行文本 ⇒ 会漏看该命中")

    # ② 每条锚点在原位、计数相符、且自身真被别名正则计入; 用它核销命中行
    for path, anchor, expected, cat, reason in _ALIAS_EXEMPTIONS:
        assert _PAT_NOW_ALIAS.findall(anchor), (
            f"台账锚点自身不含别名裸 now, 无法核销任何命中 (空锚点凑数): {path} / {anchor!r}")
        text = (PROJECT_ROOT / path).read_text(encoding="utf-8", errors="ignore")
        actual = text.count(anchor)
        assert actual == expected, (
            f"{path} 的豁免锚点计数失配: 台账登记 {expected} / 实测 {actual} [{cat}] — 该豁免点已被"
            f"删除或重构。请重新逐点判定 (清剿 or 豁免) 并同步 _ALIAS_EXEMPTIONS 与 "
            f"BASELINE_NOW_ALIAS。\n锚点: {anchor!r}\n原登记理由: {reason}")
        need = expected
        for row in rows_by_file.get(path, []):
            if need <= 0:
                break
            take = min(row[2].count(anchor), need)
            if take:
                row[1] -= take
                need -= take
        assert need == 0, (
            f"{path} 的锚点只在**别名命中行**上核销了 {expected - need}/{expected} 处 [{cat}] — "
            f"锚点文本跨行或与源码不一致。锚点: {anchor!r}")

    # ③ 台账穷尽性: 无未核销命中, 且 Σ台账 == 实测总数
    leftover = [(p, r[0], r[2], r[1]) for p, rows in rows_by_file.items() for r in rows if r[1] > 0]
    total = sum(v[2] for v in per.values())
    ledger_sum = sum(entry[2] for entry in _ALIAS_EXEMPTIONS)
    assert not leftover, (
        f"发现 **{sum(x[3] for x in leftover)} 处未登记**的别名裸 now (实测 {total} / 台账 Σ "
        f"{ledger_sum}):\n  " + "\n  ".join(f"{p}:{ln}  `{txt[:90]}`" for p, ln, txt, _n in leftover[:12])
        + "\n请逐点判定: 属真病灶 → 改用 now_et()/now_utc() 并调低 BASELINE_NOW_ALIAS; "
          "属豁免 → 在 _ALIAS_EXEMPTIONS 登记 (路径/行内锚点/次数/类别/理由), 四类判例见本文件头注。")
    assert ledger_sum == total, (
        f"台账 Σ期望 {ledger_sum} != 实测别名裸 now {total}, 且无未核销命中 ⇒ 台账存在**重复登记**或"
        f"多余条目 (同一处被两条锚点覆盖), 请核对 _ALIAS_EXEMPTIONS。")


def test_alias_patterns_are_disjoint_from_plain():
    """主正则与别名正则必须**互斥**: 同一处调用只能计一次, 否则基线数字无意义.

    回归实证: `_datetime.now()` 含子串 `datetime.now()`, 主正则若不加 `(?<!\\w)` 就会双计
    (`app/strategy/regime_filter.py` 曾因此多计 1 处 ⇒ 第一阶的 455 含 1 处重复计数)。
    """
    plain = ["datetime.now()", "x = datetime.now().isoformat()", "f'{datetime.now()}'"]
    alias = ["_dt.now()", "_datetime.now()", "dt.now()", "y = _dt.now().isoformat()"]
    for snippet in plain:
        assert _PAT_NOW.findall(snippet), f"{snippet} 应被主正则计入"
        assert not _PAT_NOW_ALIAS.findall(snippet), f"{snippet} 不应再被别名正则重复计入"
    for snippet in alias:
        assert _PAT_NOW_ALIAS.findall(snippet), f"{snippet} 应被别名正则计入"
        assert not _PAT_NOW.findall(snippet), f"{snippet} 不应被主正则重复计入 (双计)"
    for snippet in ["_dt.now(CST)", "_dt.now(_now_et())", "_datetime.now(timezone.utc)",
                    "datetime.now(CST)", "datetime.now(timezone.utc)", "now_et()"]:
        assert not _PAT_NOW.findall(snippet) and not _PAT_NOW_ALIAS.findall(snippet), \
            f"{snippet} 属带尺合规写法, 两个正则都不应计入"
    assert _PAT_UTCNOW.findall("datetime.utcnow()")
    assert not _PAT_UTCNOW_ALIAS.findall("datetime.utcnow()")
    assert _PAT_UTCNOW_ALIAS.findall("_dt.utcnow()")
    assert not _PAT_UTCNOW.findall("_dt.utcnow()")


def test_scan_is_not_vacuous():
    """守卫自证: 扫描逻辑坏了 (路径改名/正则失效) 必须红, 不可与"零违反"同形。"""
    per = _scan()
    assert per, "扫描结果为空 — app/ 路径或排除词表被改动, 守卫已失效 (非“清剿完成”)"
    assert len(per) > 100, (
        f"命中文件数 {len(per)} 远低于盘点值 130 (第三阶口径: 含仅命中别名正则的文件, 第一阶"
        f"口径为 127) — 若确已大规模清剿, 请同步调低 BASELINE_* 并更新头注的次序清单; "
        f"否则是扫描范围被缩小")
    assert sum(v[0] for v in per.values()) > 0


def test_pattern_only_matches_naive_forms():
    """守卫量的是对的东西: 带尺写法不得计入 (否则清剿无路可走)."""
    compliant = [
        "datetime.now(CST)", "datetime.now(ET)", "datetime.now(timezone.utc)",
        "datetime.now(_now_et())", "now_et()", "now_utc()", "now_cst()",
    ]
    for snippet in compliant:
        assert not _PAT_NOW.findall(snippet), f"{snippet} 属合规写法, 不应计入棘轮"
        assert not _PAT_UTCNOW.findall(snippet)
    for snippet in ["datetime.now()", "datetime.now().isoformat()", "x = datetime.now()"]:
        assert _PAT_NOW.findall(snippet), f"{snippet} 属裸调用, 必须计入"
    assert _PAT_UTCNOW.findall("datetime.utcnow().isoformat()")


def test_cleared_sites_stay_cleared():
    """本批已清剿的三处不得回潮 (信息位 ts/flip_at + 死变量 now_iso)."""
    pipe = (PROJECT_ROOT / "app" / "playbook" / "pipeline.py").read_text(encoding="utf-8")
    assert '"ts": _now_et_ts().isoformat()' in pipe, "daily_loss_baseline 的 ts 应写 ET"
    assert '"flip_at": _now_et_ts().isoformat()' in pipe, "flip_at 应写 ET"
    assert "now_iso = datetime.now().isoformat()\n\n        for r in rows:" not in pipe, \
        "_force_close_orphaned_sim_positions 的死变量 now_iso 不得回潮"


def test_last_sync_at_writers_stay_et():
    """B1 第三阶已翻的 6 处 last_sync_at 写方不得回潮 (回潮 = 该列重新 naive/aware 混存)."""
    ts = (PROJECT_ROOT / "app" / "storage" / "trade_store.py").read_text(encoding="utf-8")
    assert ts.count("now_et().isoformat()") >= 6, (
        "trade_store.py 里 last_sync_at 的 6 个写方应全部用 now_et(); 实测 "
        f"{ts.count('now_et().isoformat()')} 处 — 回潮会让该列重新混存 (读方 _parse_sync_ts "
        f"虽仍兼容, 但错序风险与 D-06 病灶会一起回来)")
    assert '""", (now_et().isoformat(), trace_id, symbol))' in ts, \
        "mark_sync_pending 的 last_sync_at 写点回潮"
    assert ts.count('""", (now_et().isoformat(), symbol))') == 2, \
        "mark_sync_complete 与 mark_anomalous 两处 last_sync_at 写点应均为 now_et()"
    assert '""", (new_version, now_et().isoformat(), symbol))' in ts, \
        "unmark_anomalous 的 last_sync_at 写点回潮"
    assert '""", (value, new_version, source, now_et().isoformat(), trace_id,' in ts, \
        "update_position_field 的 last_sync_at 写点回潮"
    assert '# last_sync_at → ET (2026-09-05 M-6/B1 第三阶)' in ts, \
        "upsert_open_position 的 last_sync_at 写点 (最热路径) 标注丢失或已回潮"


def test_stage5_ledger_columns_not_flipped_yet():
    """第五阶的账本列**不得被顺手翻** — 它们有字符串排序/比较消费方, 须先出归一方案单独 DEB.

    这是一条"债标记"测试: 它钉住的是**当前的分歧状态**而非理想状态。第五阶落地时本测试
    **必须被改写或删除**, 而不是让它继续绿 (继续绿 = 归一方案没做而列被翻了, 或债被遗忘)。

    函数名与断言里的阶次原为 `stage4`/"第四阶" — 第四阶已被别名裸 now 清剿占用 (§11.7 路线图),
    故 2026-09-05 第四阶批内**同步改名**为第五阶 (含 `app/storage/trade_store.py` 那条 ⚠️ 尺度债
    注释), 否则源码里的"第四阶范围"会把下一位执行者指向错误的批次 = 恰好诱发本测试要防的顺手翻。
    """
    ts = (PROJECT_ROOT / "app" / "storage" / "trade_store.py").read_text(encoding="utf-8")
    assert "⚠️ 尺度债 — B1 第五阶范围" in ts, (
        "updated_at 处的尺度债注释被删或阶次标注回退 — 第五阶会失去指路标, 且相邻两列不同尺会被"
        "误读为疏漏 (标注漂移的历史见 DEB §12.6)")
    assert "ORDER BY updated_at DESC LIMIT 1" in ts, \
        "尺度债注释里的消费方实证 (position_monitor:1229) 丢失"
    pm = (PROJECT_ROOT / "app" / "pipeline" / "position_monitor.py").read_text(encoding="utf-8")
    assert "ORDER BY updated_at DESC LIMIT 1" in pm, (
        "position_monitor 的 `ORDER BY updated_at DESC` 消费方已不在原位 — 若该查询已被改成"
        "解析后比较, 说明第五阶的前置条件已变化, 请重出消费方清单并更新本测试与 DEB")
