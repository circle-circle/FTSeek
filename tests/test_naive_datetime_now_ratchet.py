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
  5. **账本列** (`trade_records.created_at` / `open_positions.updated_at` 等) — 最高危,
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
   别名清剿本身留下一阶 (15 处需逐点判列归属, 与本阶"单列闭环"纪律冲突, 不混做)。

回滚: 删本文件 (回到无管控状态, 基线与盘点结论在本头注留档)。
"""
import pathlib
import re
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_NOW = 446     # 2026-09-05 B1 第三阶: 455 → 446 — 只降不升。构成: last_sync_at 写方 6 处
                       #   翻 ET (−6) + 2 处 docstring 字面量按头注边界①约定改写 (−2) + 主正则加
                       #   (?<!\w) 修掉 `_datetime.now()` 的 1 处双计 (−1)。旧正则口径下的等价历史
                       #   基线是 454, 故本阶真实清剿量 = 454 − 446 = 8。
BASELINE_UTCNOW = 4    # 同类病灶 (naive UTC), 未清剿, 先钉住不增长
BASELINE_NOW_ALIAS = 15    # 别名裸 now (`_dt.now()` 14 + `_datetime.now()` 1) — 第三阶新增:
                           #   堵住"把 datetime.now() 改名成 _dt.now() 就能让棘轮变绿"的规避通道
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
        f"请改用 now_et()/now_utc() 或显式带尺。\n当前命中: {_top(per, 2)}")


def test_naive_alias_utcnow_not_growing():
    """同族规避通道的 utcnow 侧, 基线钉在 0."""
    per = _scan()
    current = sum(v[3] for v in per.values())
    assert current <= BASELINE_UTCNOW_ALIAS, (
        f"app/ 下别名裸 utcnow 增长: {BASELINE_UTCNOW_ALIAS} → {current}。改用 now_utc()。")


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


def test_stage4_ledger_columns_not_flipped_yet():
    """第四阶的账本列**不得被顺手翻** — 它们有字符串排序/比较消费方, 须先出归一方案单独 DEB.

    这是一条"债标记"测试: 它钉住的是**当前的分歧状态**而非理想状态。第四阶落地时本测试
    **必须被改写或删除**, 而不是让它继续绿 (继续绿 = 归一方案没做而列被翻了, 或债被遗忘)。
    """
    ts = (PROJECT_ROOT / "app" / "storage" / "trade_store.py").read_text(encoding="utf-8")
    assert "⚠️ 尺度债 — B1 第四阶范围" in ts, (
        "updated_at 处的尺度债注释被删 — 第四阶会失去指路标, 且相邻两列不同尺会被误读为疏漏")
    assert "ORDER BY updated_at DESC LIMIT 1" in ts, \
        "尺度债注释里的消费方实证 (position_monitor:1229) 丢失"
    pm = (PROJECT_ROOT / "app" / "pipeline" / "position_monitor.py").read_text(encoding="utf-8")
    assert "ORDER BY updated_at DESC LIMIT 1" in pm, (
        "position_monitor 的 `ORDER BY updated_at DESC` 消费方已不在原位 — 若该查询已被改成"
        "解析后比较, 说明第四阶的前置条件已变化, 请重出消费方清单并更新本测试与 DEB")
