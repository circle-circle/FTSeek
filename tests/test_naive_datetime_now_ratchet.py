"""裸 datetime.now() / utcnow() 棘轮守卫 (2026-09-05, DEB-20260905 M-6/B1 第一阶, 用户裁"需要").

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
  4. **单列闭环** (写方与读方同批改, 读方做成格式兼容) — 已完成读方: `last_sync_at`
     (`_parse_sync_ts` 兼容 naive-CST 与 aware, NULL/脏值 fail-loud); 写方 3 处
     (`mark_sync_pending`/`mark_sync_complete`/`mark_anomalous`) 可随下一批翻。
  5. **账本列** (`trade_records.created_at` / `open_positions.updated_at` 等) — 最高危,
     须先出消费方清单 + 归一方案, 单独 DEB 裁决 (DEB M-6 已注: B1 完成前的行在复盘查询中
     以 `entry_time` 而非 `created_at` 归日)。

回滚: 删本文件 (回到无管控状态, 基线与盘点结论在本头注留档)。
"""
import pathlib
import re
import sys

PROJECT_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

BASELINE_NOW = 455     # 2026-09-05 M-6/B1 第一阶: 458 → 455 (清 2 信息位 + 1 死变量) — 只降不升
BASELINE_UTCNOW = 4    # 同类病灶 (naive UTC), 本批未清剿, 先钉住不增长
_SCAN_ROOT = PROJECT_ROOT / "app"
_EXCLUDE_PARTS = {"venv", "node_modules", ".git", "tests", "archive",
                  "__pycache__", ".backup_premigration", "obsidian_vault"}
_PAT_NOW = re.compile(r"datetime\.now\(\)")
_PAT_UTCNOW = re.compile(r"datetime\.utcnow\(\)")


def _scan():
    """返回 {相对路径: (裸 now 数, 裸 utcnow 数)} (仅含有命中的文件)."""
    per = {}
    for path in _SCAN_ROOT.rglob("*.py"):
        if any(part in _EXCLUDE_PARTS for part in path.parts):
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        n_now = len(_PAT_NOW.findall(text))
        n_utc = len(_PAT_UTCNOW.findall(text))
        if n_now or n_utc:
            per[str(path.relative_to(PROJECT_ROOT))] = (n_now, n_utc)
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


def test_scan_is_not_vacuous():
    """守卫自证: 扫描逻辑坏了 (路径改名/正则失效) 必须红, 不可与"零违反"同形。"""
    per = _scan()
    assert per, "扫描结果为空 — app/ 路径或排除词表被改动, 守卫已失效 (非“清剿完成”)"
    assert len(per) > 100, (
        f"命中文件数 {len(per)} 远低于盘点值 127 — 若确已大规模清剿, 请同步调低 BASELINE_NOW "
        f"并更新本文件头注的次序清单; 否则是扫描范围被缩小")
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
