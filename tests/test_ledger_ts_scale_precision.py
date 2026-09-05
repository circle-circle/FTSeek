r"""账本时间列的尺度精度契约 (2026-09-05, DEB-20260905 §十三 · 精度与数据修复批).

被测: 新共享解析器 `app.utils.timezone.parse_ledger_ts` 及其 5 个接入点 —
  ① `app/plan/position_intelligence._calc_hold_days`      (缺陷1)
  ② `app/plan/position_intelligence._get_avg_hold_days`   (缺陷2)
  ③ `app/pipeline/position_monitor._reconcile_position_store` 10min 冷却窗 (缺陷3)
  ④ `app/web_dashboard/data_service._is_meta_older_than`  (缺陷4)
  ⑤ `app/web_dashboard/order/audit._earliest_ledger_iso`  (缺陷5)
外加 B4: `app/data/futu_lv3_provider.get_push_tickers` 的 freshness gate。

病灶 (共性): 账本列实测 **3 种尺度 × 2 种分隔符混存** —
aware-ET+'T' / naive-CST+'T' / naive-UTC+空格。原实现各自用不同方式"猜"尺度:
定长截断偏移 (`txt[:26]`)、SQL 字符串比较 (`exit_time >= cutoff`)、剥偏移后当 UTC
(`s.split("+")[0]`)、SQL `julianday()` 跨尺相减、SQL `MIN()` 挑字符串最小值。
五种猜法全部会算错, 且**都不抛异常** ⇒ 静默污染。

尺度归因规则 (与 `trade_store._parse_sync_ts` / `to_et()` 同一契约):
  带偏移 → 原样; 'Z' → UTC; naive → **CST**。
naive=CST 的实证 (2026-09-05, 795 行 trade_records 全量复算): naive `entry_time` 353 行
按 CST→ET(-12h) 解读有 **330 行 (93.5%)** 落在美股正常交易时段, 按 ET 解读仅 **8 行
(2.3%)**; 16 行"负持仓时长"(naive entry / aware exit)按 CST 解读后全部转正。

⚠️ 与 `parse_iso_safe` 的分歧是**刻意的**: 后者把 naive 当 UTC。见
`test_parse_ledger_ts_diverges_from_parse_iso_safe_on_purpose`。

刻意**不在本批范围** (各有独立登记, 勿在此扩权):
  - `trade_store:1111` 热路径 `updated_at` 写方翻转 = B1 **第五阶**, 须单独 DEB
    (由 `test_stage5_ledger_columns_not_flipped_yet` /
    `test_updated_at_adjacent_scale_debt_is_documented` 钉住);
  - 手动单路径自造的 `datetime.now()` = **T4 钩子** (`order/audit.py` 头注), 触发条件
    是"手动单再启用时";
  - 存量 16 行负时长的**物理迁移** — 本批只修读方 (已证负时长归零), 不改任何存储字节。

回滚: `git revert <本批提交>` + 删本文件 ⇒ 5 个读方回到各自的"猜尺度"实现。
因本批**未改任何存储数据、未改任何下单路径**, 回滚零数据迁移、零成交影响。
"""
import sqlite3
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.utils.timezone import (
    CST,
    ET,
    _localize,
    now_et,
    now_utc,
    parse_iso_safe,
    parse_ledger_ts,
    to_et,
)


def _et(*args):
    """pytz 安全的 ET datetime 构造。

    ⚠️ 不能写 `datetime(…, tzinfo=ET)`: venv 里装的是 pytz, 那样拿到的是 **LMT
    (UTC-7:52:58)** 而不是 EDT/EST, 会让所有基于冻结时钟的断言整体偏 ~8 分钟
    (本文件首版就踩过, UBER 用例因此算出 2 天而非 1 天)。必须走 localize。
    """
    return _localize(datetime(*args), ET)

# ── 实测样本: 全部取自 data/ftseek_us.db 2026-09-05 的真实行 ──────────────────
AWARE_ET = "2026-09-04T13:30:12.864694-04:00"      # open_positions.updated_at 现役格式
NAIVE_CST = "2026-09-05T01:30:12.768948"           # trade_records.created_at 同瞬间的 CST 写法
NAIVE_UTC_SPACE = "2026-09-04 17:30:12"            # data_service 旧写方的第三种尺度
# id=854 US.XLK: 修复前 julianday 相减 = -0.332 天的那一对
NEG_ENTRY_NAIVE = "2026-06-02T23:09:10"
NEG_EXIT_AWARE = "2026-06-02T11:11:42-04:00"


# ═══════════════════════════════════════════════════════════════════════
#  A. parse_ledger_ts 契约
# ═══════════════════════════════════════════════════════════════════════

def test_parse_ledger_ts_preserves_aware_offset():
    got = parse_ledger_ts(AWARE_ET)
    assert got is not None and got.tzinfo is not None
    assert got.utcoffset() == timedelta(hours=-4)
    assert got.astimezone(ET).isoformat() == AWARE_ET


def test_parse_ledger_ts_naive_is_attributed_to_cst():
    """naive → CST。这是整批修复的地基, 实证见模块头注 (330/353 vs 8/353)。"""
    got = parse_ledger_ts(NAIVE_CST)
    assert got is not None and got.tzinfo is not None
    assert got.utcoffset() == timedelta(hours=8)
    assert got.astimezone(ET).hour == 13          # 01:30 CST(次日) == 13:30 ET(前日)


def test_parse_ledger_ts_agrees_across_distinguishable_scales():
    """**可区分**的两种尺度 (aware-ET / naive-CST) 写同一瞬间 ⇒ 解析后必须相等。

    原 5 种"猜尺度"的读法全都判它们不等, 这正是 4 个活体缺陷的共同根因。
    """
    a = parse_ledger_ts(AWARE_ET).replace(microsecond=0)
    b = parse_ledger_ts(NAIVE_CST).replace(microsecond=0)
    assert a == b, f"同一瞬间两种写法解析结果不一致: {a} / {b}"


def test_naive_utc_is_indistinguishable_from_naive_cst():
    """⚠️ 诚实登记一条**解析无法修复**的事实: naive-UTC 与 naive-CST 字节形态完全相同。

    `data_service._reconcile_ghosts` 旧写方产出 `now_utc().strftime("%Y-%m-%d %H:%M:%S")`
    = naive-UTC + 空格分隔, 而账本列其余 naive 值是 naive-CST。两者都无偏移标记 ⇒
    任何解析器都只能二选一。本批的契约是"naive 一律按 CST"(实证 330/353), 于是
    naive-UTC 行会被读成 CST ⇒ **偏 8 小时**, 且无法在读侧消除。

    ⇒ 所以本批必须同时**改掉那个写方** (见
    `test_ghost_reconcile_writer_emits_self_describing_et`), 让新数据自描述尺度。
    存量 naive-UTC 行仅可能出现在 is_anomalous=1 的幽灵标记里, 而该写方全库零触发
    (logs/ 无任何 [GhostReconcile] 记录) ⇒ 现库零存量。
    """
    as_cst = parse_ledger_ts(NAIVE_UTC_SPACE)          # "2026-09-04 17:30:12"
    true_utc = parse_ledger_ts("2026-09-04 17:30:12+00:00")
    assert as_cst is not None and true_utc is not None
    assert (true_utc - as_cst) == timedelta(hours=8), (
        "naive-UTC 被按 CST 解读应偏 8h — 若这里变成 0, 说明有人给解析器加了"
        "'空格分隔=UTC' 之类的猜测规则, 那会反过来把 naive-CST 行读错 8h")


def test_ghost_reconcile_writer_emits_self_describing_et():
    """钉住写方修复: `_reconcile_ghosts` 不得再产出 naive 的 `updated_at`。

    这是上一条"解析无法区分 naive-UTC/naive-CST"的**唯一**根治手段。
    """
    src = (PROJECT_ROOT / "app" / "web_dashboard" / "data_service.py").read_text(encoding="utf-8")
    body = src.split("def _reconcile_ghosts")[1].split("def _is_meta_older_than")[0]
    assert 'strftime("%Y-%m-%d %H:%M:%S")' not in body, (
        "_reconcile_ghosts 又写回 naive 时间串了 ⇒ open_positions.updated_at 会重新出现"
        "无法自描述尺度的第三种写法")
    assert "now_et().isoformat()" in body, "该写方应产出 aware-ET isoformat"


def test_parse_ledger_ts_tolerates_z_space_and_date_only():
    assert parse_ledger_ts("2026-09-05T01:30:12Z") == parse_ledger_ts("2026-09-05 01:30:12+00:00")
    assert parse_ledger_ts("2026-09-05").astimezone(CST).hour == 0
    assert parse_ledger_ts("2026-09-05 01:30:12.123456").tzinfo is not None


@pytest.mark.parametrize("bad", [None, "", "   ", "not-a-date", "0", 12345, float("nan")])
def test_parse_ledger_ts_returns_none_on_unparseable(bad):
    """解析失败必须返回 None — 调用方据此**跳过**, 不得回退成"当作现在/epoch"。"""
    assert parse_ledger_ts(bad) is None


def test_parse_ledger_ts_accepts_datetime_objects():
    naive = datetime(2026, 9, 5, 1, 30, 12)
    got = parse_ledger_ts(naive)
    assert got.tzinfo is not None and got.utcoffset() == timedelta(hours=8)
    aware = _et(2026, 9, 4, 13, 30, 12)
    assert parse_ledger_ts(aware) == aware


def test_parse_ledger_ts_diverges_from_parse_iso_safe_on_purpose():
    """钉住"勿合并"的契约: 同一 naive 串, 两个解析器给出**不同类型**的结果。

    实测 (venv 有 pytz): `parse_iso_safe(NAIVE_CST)` 走 `datetime.fromisoformat` 成功分支
    ⇒ 返回 **naive** datetime (它头注说的"naive 视为 UTC"只在 strptime 兜底分支成立,
    而该分支对合法 ISO 串永远不可达)。`parse_ledger_ts` 返回 **aware CST**。
    两者相减直接 TypeError ⇒ 若有人"顺手统一", 本测试变红而不是静默偏 8h。
    """
    ledger = parse_ledger_ts(NAIVE_CST)
    iso = parse_iso_safe(NAIVE_CST)
    assert ledger.tzinfo is not None, "parse_ledger_ts 必须返回 aware"
    assert iso.tzinfo is None, (
        "parse_iso_safe 的行为变了 — 若它现在也附尺度, 请重新评估两者是否该合并, "
        "并同步更新 parse_ledger_ts 的头注")
    with pytest.raises(TypeError):
        ledger - iso


# ═══════════════════════════════════════════════════════════════════════
#  B. 缺陷1 — _calc_hold_days 的 +12h 偏置
# ═══════════════════════════════════════════════════════════════════════

def test_calc_hold_days_has_no_offset_truncation_bias(monkeypatch):
    """14h 前开的仓必须是 0 天。

    修复前: `txt[:26]` 把 `-04:00` 截掉 → naive ET 墙钟, 再与 `datetime.now()`
    (宿主 CST 墙钟) 相减 ⇒ 凭空 +12h → 26h → **1 天**。实测当天 UBER/RIVN 因此被
    Rule 4 误报"持仓2天(历史平均1天) ⇒ 减仓50%"。
    """
    from app.plan import position_intelligence as pi

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    entry = (frozen - timedelta(hours=14)).isoformat()
    assert pi._calc_hold_days(entry) == 0

    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    assert pi._calc_hold_days((frozen - timedelta(hours=38)).isoformat()) == 1


def test_calc_hold_days_is_scale_invariant(monkeypatch):
    """同一瞬间的 aware-ET 与 naive-CST 两种写法 ⇒ 持仓天数必须相同。"""
    from app.plan import position_intelligence as pi

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    aware = (frozen - timedelta(days=2, hours=3)).isoformat()
    naive_cst = (frozen - timedelta(days=2, hours=3)).astimezone(CST).replace(tzinfo=None).isoformat()
    assert pi._calc_hold_days(aware) == pi._calc_hold_days(naive_cst) == 2


def test_calc_hold_days_reproduces_live_uber_case(monkeypatch):
    """实测样本回归: UBER 2026-09-03T09:39:51-04:00 入场。

    2026-09-05 08:46 ET 时真值 = 1 天; 修复前算出 2 天 ⇒ 触发 Rule 4。
    """
    from app.plan import position_intelligence as pi

    frozen = _et(2026, 9, 5, 8, 46, 0)
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    assert pi._calc_hold_days("2026-09-03T09:39:51.298036-04:00") == 1
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    assert pi._calc_hold_days("2026-09-04T13:30:12.864694-04:00") == 0


def test_calc_hold_days_unparseable_returns_zero():
    from app.plan import position_intelligence as pi

    assert pi._calc_hold_days("") == 0
    assert pi._calc_hold_days(None) == 0
    assert pi._calc_hold_days("garbage") == 0


def test_calc_hold_days_never_negative(monkeypatch):
    from app.plan import position_intelligence as pi

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_et", lambda: frozen)
    assert pi._calc_hold_days((frozen + timedelta(days=5)).isoformat()) == 0


# ═══════════════════════════════════════════════════════════════════════
#  C. 缺陷2 — _get_avg_hold_days 的跨尺负时长
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def avg_db(tmp_path, monkeypatch):
    """造一个只含 trade_records 的临时库, 并把 _resolve_trade_records_db 指过去。"""
    from app.plan import position_intelligence as pi

    db = tmp_path / "tr.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE trade_records (id INTEGER PRIMARY KEY, symbol TEXT, direction TEXT, "
        "entry_time TEXT, exit_time TEXT)"
    )
    conn.commit()
    conn.close()
    monkeypatch.setattr(pi, "_resolve_trade_records_db", lambda: str(db))
    return str(db)


def _insert(db, rows):
    conn = sqlite3.connect(db)
    conn.executemany(
        "INSERT INTO trade_records (symbol,direction,entry_time,exit_time) VALUES (?,?,?,?)", rows
    )
    conn.commit()
    conn.close()


def test_avg_hold_days_mixed_scale_pair_is_positive(avg_db):
    """id=854 US.XLK 复现: naive-CST entry + aware-ET exit。

    修复前 SQL `julianday(exit)-julianday(entry)` = **-0.332 天** (把 23:09 CST 与
    11:11 ET 当同一把尺相减)。真实间隔 = +2 分 32 秒。
    """
    from app.plan.position_intelligence import DEFAULT_AVG_HOLD_DAYS, _get_avg_hold_days

    _insert(avg_db, [
        ("US.XLK", "LONG", NEG_ENTRY_NAIVE, NEG_EXIT_AWARE),
        ("US.XLK", "LONG", "2026-06-03T23:09:10", "2026-06-03T12:11:42-04:00"),
    ])
    got = _get_avg_hold_days("US.XLK", "LONG")
    assert got >= 1                       # 不再是负数被 max(1,…) 掩盖前的错值
    assert got != DEFAULT_AVG_HOLD_DAYS   # 两个可解析配对 ⇒ 不走兜底


def test_avg_hold_days_cross_scale_error_can_move_the_rounded_result(avg_db):
    """强证伪: 跨尺误差**能真的改变**四舍五入后的输出, 不只是被 max(1,…) 地板掩盖。

    构造: 1 个 aware-aware 的 3 天对 + 1 个 naive-CST/aware-ET 的 2.5 分钟对。
      · 修复前 SQL `julianday` 跨尺相减: (3 + (-0.332)) / 2 = 1.334 → round → **1**
      · 修复后同尺解析:              (3 + 0.0017) / 2 = 1.501 → round → **2**
    ⇒ 均值差一档, Rule 4 的阈值 (hold_days > avg*1.5) 从 1.5 天变成 3 天, 建议会变。
    这条比 `test_avg_hold_days_mixed_scale_pair_is_positive` 强: 后者在修复前也能过
    (负均值被 `max(1, int(round(-0.33)))` 地板抬回 1), 抓不到 bug。
    """
    from app.plan.position_intelligence import _get_avg_hold_days

    _insert(avg_db, [
        ("US.MIX", "LONG", "2026-06-10T10:00:00-04:00", "2026-06-13T10:00:00-04:00"),
        ("US.MIX", "LONG", NEG_ENTRY_NAIVE, NEG_EXIT_AWARE),
    ])
    assert _get_avg_hold_days("US.MIX", "LONG") == 2


def test_avg_hold_days_negative_duration_is_gone(avg_db, monkeypatch):
    """直接验底层时长符号: 混存对必须为正 (修复前全库有 16 行为负)。"""
    entry = parse_ledger_ts(NEG_ENTRY_NAIVE)
    exit_ = parse_ledger_ts(NEG_EXIT_AWARE)
    assert (exit_ - entry).total_seconds() == pytest.approx(152.0, abs=1.0)


def test_avg_hold_days_counts_only_parseable_pairs(avg_db):
    """cnt 只数**成功解析的配对**。

    修复前 `COUNT(*)` 数的是"两列非 NULL 的行", 而 `julianday('')`=NULL 会被 AVG 跳过
    却仍被 COUNT 计入 ⇒ cnt 虚高, 让数据不足的组越过 `<2` 门槛拿到一个假均值。
    """
    from app.plan.position_intelligence import DEFAULT_AVG_HOLD_DAYS, _get_avg_hold_days

    _insert(avg_db, [
        ("US.FAKE", "LONG", "2026-06-02T23:09:10-04:00", "2026-06-02T23:39:10-04:00"),
        ("US.FAKE", "LONG", "", "2026-06-02T23:39:10-04:00"),
        ("US.FAKE", "LONG", "garbage", "also-garbage"),
    ])
    assert _get_avg_hold_days("US.FAKE", "LONG") == DEFAULT_AVG_HOLD_DAYS


def test_avg_hold_days_insufficient_data_falls_back(avg_db):
    from app.plan.position_intelligence import DEFAULT_AVG_HOLD_DAYS, _get_avg_hold_days

    _insert(avg_db, [("US.SOLO", "LONG", AWARE_ET, AWARE_ET)])
    assert _get_avg_hold_days("US.SOLO", "LONG") == DEFAULT_AVG_HOLD_DAYS
    assert _get_avg_hold_days("US.NONEXISTENT", "LONG") == DEFAULT_AVG_HOLD_DAYS


def test_avg_hold_days_floor_is_one_day(avg_db):
    """分钟级 scalping 均值 → max(1, round(≈0)) = 1 (保留既有地板语义)。"""
    from app.plan.position_intelligence import _get_avg_hold_days

    _insert(avg_db, [
        ("US.SCALP", "LONG", "2026-06-02T10:00:00-04:00", "2026-06-02T10:05:00-04:00"),
        ("US.SCALP", "LONG", "2026-06-03T10:00:00-04:00", "2026-06-03T10:07:00-04:00"),
    ])
    assert _get_avg_hold_days("US.SCALP", "LONG") == 1


def test_avg_hold_days_multi_day_average(avg_db):
    from app.plan.position_intelligence import _get_avg_hold_days

    _insert(avg_db, [
        ("US.SWING", "LONG", "2026-06-02T10:00:00-04:00", "2026-06-05T10:00:00-04:00"),
        ("US.SWING", "LONG", "2026-06-10T10:00:00-04:00", "2026-06-13T10:00:00-04:00"),
    ])
    assert _get_avg_hold_days("US.SWING", "LONG") == 3


# ═══════════════════════════════════════════════════════════════════════
#  D. 缺陷4 — _is_meta_older_than 的提前删除
# ═══════════════════════════════════════════════════════════════════════

def test_is_meta_older_than_aware_et_not_four_hours_early(monkeypatch):
    """23h 前的 aware-ET 行**不该**被判为超过 24h。

    修复前: 剥掉 `-04:00` 后把 ET 墙钟当 UTC ⇒ 账龄虚增 4h → 27h → True ⇒
    `_reconcile_ghosts` 会**提前 4h** DELETE 真实持仓记录 (G-09 正门)。
    """
    from app.web_dashboard.data_service import DataService

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_utc", lambda: frozen.astimezone())
    row_23h = (frozen - timedelta(hours=23)).isoformat()
    assert DataService._is_meta_older_than(row_23h, hours=24) is False


def test_is_meta_older_than_aware_et_true_past_threshold(monkeypatch):
    from app.web_dashboard.data_service import DataService

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_utc", lambda: frozen.astimezone())
    row_25h = (frozen - timedelta(hours=25)).isoformat()
    assert DataService._is_meta_older_than(row_25h, hours=24) is True


def test_is_meta_older_than_naive_cst_not_eight_hours_late(monkeypatch):
    """30h 前的 naive-CST 行**该**被判为超过 24h。

    修复前: CST 墙钟被当 UTC ⇒ 账龄少算 8h → 22h → False ⇒ 幽灵仓删除**延迟 8h**。
    """
    from app.web_dashboard.data_service import DataService

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_utc", lambda: frozen.astimezone())
    naive_cst_30h = (frozen - timedelta(hours=30)).astimezone(CST).replace(tzinfo=None)
    assert DataService._is_meta_older_than(naive_cst_30h.isoformat(), hours=24) is True


def test_is_meta_older_than_is_scale_invariant(monkeypatch):
    """同一瞬间三种写法 ⇒ 判定必须一致 (修复前三种各差 4h/8h/12h)。"""
    from app.web_dashboard.data_service import DataService

    frozen = _et(2026, 9, 5, 12, 0, 0)
    monkeypatch.setattr("app.utils.timezone.now_utc", lambda: frozen.astimezone())
    instant = frozen - timedelta(hours=23)
    variants = [
        instant.isoformat(),                                              # aware-ET
        instant.astimezone(CST).replace(tzinfo=None).isoformat(),          # naive-CST 'T'
        instant.astimezone(CST).replace(tzinfo=None).isoformat(sep=" "),   # naive-CST 空格
        instant.astimezone(CST).isoformat(),                               # aware-CST
        instant.astimezone(now_utc().tzinfo).isoformat(),                  # aware-UTC
    ]
    results = [DataService._is_meta_older_than(v, hours=24) for v in variants]
    assert results == [False] * len(variants), f"同一瞬间判定不一致: {list(zip(variants, results))}"
    # 注: naive-**UTC** 写法刻意不在样本里 — 它与 naive-CST 字节同形, 解析器无法区分,
    # 见 test_naive_utc_is_indistinguishable_from_naive_cst。


@pytest.mark.parametrize("bad", ["", None, "garbage", "0"])
def test_is_meta_older_than_conservative_on_unparseable(bad):
    """解析不出 → False (= 不删)。删除是不可逆动作, 兜底必须朝保守方向。"""
    from app.web_dashboard.data_service import DataService

    assert DataService._is_meta_older_than(bad, hours=24) is False


# ═══════════════════════════════════════════════════════════════════════
#  E. 缺陷5 — _earliest_ledger_iso 的字符串 MIN
# ═══════════════════════════════════════════════════════════════════════

def test_earliest_ledger_iso_picks_true_earliest_not_string_min():
    """混存尺度下 SQL `MIN()` 会挑错行。

    样本: naive-CST `2026-09-05T01:30:12.768948` (= 2026-09-04T13:30:12.768948-04:00)
    比 aware `…13:30:12.864694-04:00` **早 0.096 秒**, 但字符串序里 '2026-09-05…' >
    '2026-09-04…' ⇒ MIN 挑了后者。挑错行 = 写进 trade_records.entry_time 后归日错一天。
    """
    from app.web_dashboard.order.audit import _earliest_ledger_iso

    mixed = [AWARE_ET, NAIVE_CST]
    # 旧行为: SQL MIN() 挑字符串最小值 = AWARE_ET ('2026-09-04…' < '2026-09-05…')
    assert min(mixed) == AWARE_ET
    # 真实最早瞬间却是 NAIVE_CST (= 2026-09-04T13:30:12.768948-04:00, 早 0.096 秒)
    assert parse_ledger_ts(NAIVE_CST) < parse_ledger_ts(AWARE_ET)
    got = _earliest_ledger_iso(mixed)
    assert got is not None
    assert parse_ledger_ts(got) == parse_ledger_ts(NAIVE_CST)


def test_earliest_ledger_iso_output_is_aware_et():
    """输出归一成 aware-ET ⇒ 不再向 trade_records.entry_time 泄漏 naive-CST (R-12 口径)。"""
    from app.web_dashboard.order.audit import _earliest_ledger_iso

    got = _earliest_ledger_iso([NAIVE_CST])
    assert got is not None
    assert got.endswith("-04:00") or got.endswith("-05:00")
    assert parse_ledger_ts(got).tzinfo is not None


@pytest.mark.parametrize("bad", [[], [""], [None], ["garbage"], ["", "nope"]])
def test_earliest_ledger_iso_returns_none_when_no_usable_value(bad):
    from app.web_dashboard.order.audit import _earliest_ledger_iso

    assert _earliest_ledger_iso(bad) is None


def test_earliest_ledger_iso_accepts_generator():
    """调用点传的是生成器 (cursor.fetchall() 的推导) ⇒ 必须支持一次性迭代。"""
    from app.web_dashboard.order.audit import _earliest_ledger_iso

    assert _earliest_ledger_iso(v for v in [AWARE_ET, NAIVE_CST]) is not None


# ═══════════════════════════════════════════════════════════════════════
#  F. B4 — get_push_tickers 的 freshness gate
# ═══════════════════════════════════════════════════════════════════════

@pytest.fixture
def provider():
    from app.data.futu_lv3_provider import FutuLV3Provider

    p = FutuLV3Provider(host="127.0.0.1", port=1)   # 不连接: __init__ 是懒连接
    return p


def _seed_tickers(p, sym, age_sec, n=3):
    with p._push_cache_lock:
        p._push_ticker_cache[sym] = [
            {"price": 100.0 + i, "volume": 100, "turnover": 10000.0,
             "direction": "BUY", "time": "2026-09-05 09:30:0%d.000" % i, "sequence": i}
            for i in range(n)
        ]
        p._push_ticker_seen_at[sym] = time.time() - age_sec


def test_push_tickers_fresh_cache_is_returned(provider):
    _seed_tickers(provider, "US.AAPL", age_sec=1)
    got = provider.get_push_tickers("US.AAPL")
    assert got is not None and len(got) == 3


def test_push_tickers_stale_cache_returns_none(provider):
    """停更超过 TTL ⇒ 返回 None, 走既有"无逐笔数据"降级分支 (不注入因子, 不阻断下单)。

    修复前 `if cached: return list(cached)` 会把几小时前的 500 笔当实时数据返回, 而
    `analyze_tickers` 头注明确假定缓存新鲜 ("用最近 N 笔近似") ⇒ ticker_velocity 与
    buy_sell_ratio 全在描述远古窗口, 却被 market_data_router 注入 order_flow_imbalance。
    """
    _seed_tickers(provider, "US.STALE", age_sec=provider._ticker_cache_ttl + 1)
    assert provider.get_push_tickers("US.STALE") is None


def test_push_tickers_ttl_is_aligned_with_medium_window(provider):
    """TTL=300s 对齐 ticker_analyzer.MEDIUM_WINDOW (中窗口 ~250 笔 ≈ 5min)。"""
    from app.data.ticker_analyzer import MEDIUM_WINDOW

    assert provider._ticker_cache_ttl == MEDIUM_WINDOW


def test_push_tickers_gate_reaches_parity_with_orderbook(provider):
    """同一个类里 orderbook 早有 freshness gate、ticker 没有 = 自相矛盾。现已对齐。"""
    with provider._push_cache_lock:
        provider._push_orderbook_cache["US.PAR"] = {
            "BidPrice": 1.0, "BidVol": 1.0, "AskPrice": 2.0, "AskVol": 1.0,
            "BidNum": 1.0, "AskNum": 1.0, "BidOrderNum": 1.0, "AskOrderNum": 1.0,
            "timestamp": time.time() - provider.cache_ttl - 5,
        }
    _seed_tickers(provider, "US.PAR", age_sec=provider._ticker_cache_ttl + 5)
    assert provider.get_push_orderbook("US.PAR") is None
    assert provider.get_push_tickers("US.PAR") is None


def test_push_tickers_returns_copy_not_reference(provider):
    _seed_tickers(provider, "US.COPY", age_sec=0)
    got = provider.get_push_tickers("US.COPY")
    got.append({"price": 0})
    assert len(provider._push_ticker_cache["US.COPY"]) == 3


def test_stale_ticker_count_is_observable(provider):
    """热路径只打 debug (避免刷屏) ⇒ 命中面必须经健康端点可观测, 别重演 B5 的 debug 全吞。"""
    _seed_tickers(provider, "US.FRESH1", age_sec=1)
    _seed_tickers(provider, "US.STALE1", age_sec=provider._ticker_cache_ttl + 1)
    _seed_tickers(provider, "US.STALE2", age_sec=provider._ticker_cache_ttl + 99)
    assert provider._count_stale_ticker_symbols() == 2
    assert provider.get_health_status()["ticker_cache_stale_symbols"] == 2
    assert provider.get_health_status()["ticker_cache_symbols"] == 3


def test_ticker_seen_at_missing_defaults_to_stale(provider):
    """有缓存但无 seen_at (状态不一致) ⇒ 按过期处理, 保守方向。"""
    with provider._push_cache_lock:
        provider._push_ticker_cache["US.ORPHAN"] = [{"price": 1.0, "volume": 1, "turnover": 1.0,
                                                    "direction": "BUY", "time": "", "sequence": 1}]
    assert provider.get_push_tickers("US.ORPHAN") is None


def test_unsubscribe_clears_ticker_seen_at(provider, monkeypatch):
    """三处 clear 点必须连 seen_at 一起清 — 否则重连/退订后残留旧时刻会误判新鲜度。"""
    import inspect

    from app.data import futu_lv3_provider as mod

    src = inspect.getsource(mod)
    assert src.count("_push_ticker_cache.clear()") == 3
    assert src.count("_push_ticker_seen_at.clear()") == 3, (
        "clear 点数量不匹配: 每处 _push_ticker_cache.clear() 都必须伴随 "
        "_push_ticker_seen_at.clear(), 否则重订阅后会拿残留 epoch 误判缓存新鲜")


# ═══════════════════════════════════════════════════════════════════════
#  G. 缺陷3 — 对账 10min 冷却窗 (行为级, 能区分修前/修后)
# ═══════════════════════════════════════════════════════════════════════

def _build_reconcile_env(tmp_path, monkeypatch, exit_offsets_minutes):
    """搭出 `_reconcile_position_store` 走到冷却窗所需的最小环境。

    exit_offsets_minutes: 每个 symbol 对应的"退出发生在多少分钟前"。
    返回 (被调用 _reconcile_is_system_owned 的 symbol 集合, 冷却窗判定日志)。
    """
    from app.core.db_utils import get_main_db
    from app.pipeline import position_monitor as pm
    from app.storage.trade_store import TradeStore

    db_path = tmp_path / "main.db"
    store = TradeStore(str(db_path))
    if not getattr(store, "_initialized", False):
        store._init_db()
    conn = store._get_conn()
    now = now_et()
    for sym, minutes in exit_offsets_minutes.items():
        conn.execute(
            "INSERT INTO trade_records (trade_date, symbol, direction, entry_time, exit_time, "
            "exit_reason, source_type, created_at) VALUES (?,?,?,?,?,?,?,?)",
            (now.strftime("%Y-%m-%d"), sym, "LONG",
             (now - timedelta(minutes=minutes + 60)).isoformat(),
             (now - timedelta(minutes=minutes)).isoformat(), "TIME_EXIT", "PLAYBOOK_SIM",
             now.isoformat()),
        )
    conn.commit()

    reached = []
    monkeypatch.setattr(pm, "_reconcile_is_system_owned", lambda s: reached.append(s) or False)
    monkeypatch.setattr(pm, "_load_price_fail_closed_set", lambda: set())
    monkeypatch.setattr(pm, "_is_manual_position_for_reconcile", lambda s: False)
    monkeypatch.setattr("app.core.db_utils.get_main_db", lambda: db_path)
    # broker 持仓 dict 的键名取自 _reconcile_position_store 实际用到的字段
    # (direction / qty / cost) — 少一个就会在告警日志的 f-string 里 KeyError。
    _broker = {sym: {"direction": "LONG", "qty": 10, "cost": 100.0, "size": 10}
               for sym in exit_offsets_minutes}
    monkeypatch.setattr("app.core.position_sync.get_us_position_sync",
                        lambda: type("S", (), {"query_broker_positions": lambda self=None: _broker})())
    monkeypatch.setattr("app.playbook.pipeline.load_playbook_open_symbols", lambda: set())
    monkeypatch.setattr("app.playbook.pipeline.playbook_open_check", lambda s, o: False)

    class _FakeStore:
        """最小 PositionStore 替身: 内存零仓 ⇒ 所有 broker 持仓都走"未知新持仓"分支。"""

        def all(self):
            return []

        def count(self):
            return 0

        def get(self, symbol):
            return None

    class _FakeScheduler:
        position_store = _FakeStore()
        running = True

    return _FakeScheduler(), reached


def test_reconcile_old_exit_does_not_suppress_rebuild(tmp_path, monkeypatch):
    """5h 前退出的仓**不该**被当成"刚刚平仓"。

    修复前: cutoff 用空格分隔格式化成 `YYYY-MM-DD HH:MM:SS`, 与 'T'+偏移的 exit_time
    做 SQL 字符串比较; 同日内 'T'(0x54) > ' '(0x20) ⇒ **恒真** ⇒ "最近 10 分钟"退化成
    "今天退出过"。用 2026-09-04 15:00 ET 复跑当天 10 笔退出误判 10/10。
    后果: 当天稍晚的仓位重建被跳过 ⇒ 主链侧不可见/无 SL (G-09 正门)。
    """
    scheduler, reached = _build_reconcile_env(tmp_path, monkeypatch, {"US.OLD": 300})
    from app.pipeline.position_monitor import _reconcile_position_store

    _reconcile_position_store(scheduler)
    assert "US.OLD" in reached, "5h 前的退出仍被误判为'刚退出' ⇒ 冷却窗字符串比较回潮"


def test_reconcile_recent_exit_still_suppresses_rebuild(tmp_path, monkeypatch):
    """3min 前退出的仓**该**被抑制 (Futu 结算延迟窗口) — 修复不得把真阳性一起改掉。"""
    scheduler, reached = _build_reconcile_env(tmp_path, monkeypatch, {"US.NEW": 3})
    from app.pipeline.position_monitor import _reconcile_position_store

    _reconcile_position_store(scheduler)
    assert "US.NEW" not in reached


def test_reconcile_window_boundary_is_ten_minutes(tmp_path, monkeypatch):
    """窗口边界: 9min 内抑制, 11min 外放行。"""
    from app.pipeline.position_monitor import _reconcile_position_store

    scheduler, reached = _build_reconcile_env(tmp_path, monkeypatch, {"US.IN": 9, "US.OUT": 11})
    _reconcile_position_store(scheduler)
    assert "US.IN" not in reached
    assert "US.OUT" in reached


def test_reconcile_source_string_compare_pattern_is_gone():
    """源码契约: 冷却窗不得再用 `exit_time>=?` 的字符串比较。

    换分隔符**不足以**修复: 跨 DST 反例 '2026-11-01T08:30:00-04:00' 与
    '2026-11-01T08:30:00-05:00' 的字符串序与真实瞬间序相反 ⇒ 必须解析后比较。
    """
    src = (PROJECT_ROOT / "app" / "pipeline" / "position_monitor.py").read_text(encoding="utf-8")
    assert "exit_time>=?" not in src, "冷却窗回退成 SQL 字符串比较 (跨 DST 会错序)"
    assert "parse_ledger_ts" in src, "冷却窗未接入共享解析器"
    assert 'strftime("%Y-%m-%d %H:%M:%S")' not in src.split("RECENT_CLOSE_WINDOW_MINUTES")[1][:2000], (
        "冷却窗 cutoff 仍在用空格分隔的字符串格式")


def test_reconcile_sim_exits_still_gate_main_chain(tmp_path, monkeypatch):
    """诚实登记**未修**的部分: 该查询无 source_type 过滤, PLAYBOOK_SIM 的退出记录
    仍会抑制主链仓重建。本批只修时间精度, 不动这个语义 (改它会让 sim 与主链的
    隔离边界变化, 属独立议题)。本测试钉住现状, 日后要改语义时它会变红。
    """
    scheduler, reached = _build_reconcile_env(tmp_path, monkeypatch, {"US.SIMGATED": 2})
    from app.pipeline.position_monitor import _reconcile_position_store

    _reconcile_position_store(scheduler)
    assert "US.SIMGATED" not in reached
    src = (PROJECT_ROOT / "app" / "pipeline" / "position_monitor.py").read_text(encoding="utf-8")
    window_src = src.split("RECENT_CLOSE_WINDOW_MINUTES")[1][:2000]
    assert "AND source_type" not in window_src, (
        "冷却窗查询已加入 source_type 过滤 ⇒ 语义变更, 请更新 DEB 并重评 sim/主链隔离边界")


# ═══════════════════════════════════════════════════════════════════════
#  H. 存量数据回归 (只读现库; 无库则跳过)
# ═══════════════════════════════════════════════════════════════════════

_LIVE_DB = PROJECT_ROOT / "data" / "ftseek_us.db"


@pytest.mark.skipif(not _LIVE_DB.exists(), reason="现库不存在")
def test_live_db_has_no_negative_holding_duration():
    """全库回归: 修复前 16 行 `julianday(exit)-julianday(entry) < 0`, 修复后必须为 0。

    本批**未改任何存储字节** — 16 行的原文仍在库里, 只是读方不再跨尺相减。
    """
    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT entry_time, exit_time FROM trade_records "
            "WHERE entry_time IS NOT NULL AND exit_time IS NOT NULL "
            "AND entry_time<>'' AND exit_time<>''"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "现库无带退出的记录, 本测试失去意义"
    negative = []
    unparsed = []
    for entry_raw, exit_raw in rows:
        e, x = parse_ledger_ts(entry_raw), parse_ledger_ts(exit_raw)
        if e is None or x is None:
            unparsed.append((entry_raw, exit_raw))
            continue
        if (x - e).total_seconds() < 0:
            negative.append((entry_raw, exit_raw))
    assert not unparsed, f"现库有解析不出的账本时间串: {unparsed[:5]}"
    assert not negative, f"仍有负持仓时长 (跨尺相减未修净): {negative[:5]}"


@pytest.mark.skipif(not _LIVE_DB.exists(), reason="现库不存在")
def test_live_db_timestamps_all_parseable():
    """全库 2382 个账本时间值必须 100% 可解析 — 解析器的全覆盖性实证。"""
    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    try:
        total = unparsed = 0
        for col in ("entry_time", "exit_time", "created_at"):
            for (v,) in conn.execute(
                f"SELECT {col} FROM trade_records WHERE {col} IS NOT NULL AND {col}<>''"
            ):
                total += 1
                if parse_ledger_ts(v) is None:
                    unparsed += 1
        for col in ("entry_time", "updated_at"):
            for (v,) in conn.execute(
                f"SELECT {col} FROM open_positions WHERE {col} IS NOT NULL AND {col}<>''"
            ):
                total += 1
                if parse_ledger_ts(v) is None:
                    unparsed += 1
    finally:
        conn.close()
    assert total > 0
    assert unparsed == 0, f"{unparsed}/{total} 个账本时间值解析不出"


@pytest.mark.skipif(not _LIVE_DB.exists(), reason="现库不存在")
def test_live_positions_rule4_no_longer_misfires():
    """端到端回归: 现库 3 个活仓的 hold_days 必须等于真值, 且 Rule 4 不得误报。

    修复前实测 (2026-09-05 20:46 CST): UBER/RIVN hold_days=2 (真值 1)、HOOD=1 (真值 0)
    ⇒ UBER/RIVN 触发 Rule 4 → HOLD 变 PARTIAL_EXIT + "持仓2天(历史平均1天) ⇒ 减仓50%"。
    """
    from app.plan.position_intelligence import _calc_hold_days, _get_avg_hold_days

    conn = sqlite3.connect(f"file:{_LIVE_DB}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT symbol, direction, entry_time FROM open_positions WHERE size > 0"
        ).fetchall()
    finally:
        conn.close()
    assert rows, "现库无活仓"
    now = now_et()
    for symbol, direction, entry_raw in rows:
        entry = parse_ledger_ts(entry_raw)
        assert entry is not None, f"{symbol} entry_time 解析不出: {entry_raw!r}"
        truth = (now - entry).days
        got = _calc_hold_days(entry_raw)
        assert got == truth, f"{symbol}: hold_days={got} 与真值 {truth} 不符 (尺度偏置回潮)"
        avg = _get_avg_hold_days(symbol, direction)
        if got > 0 and avg > 0 and got > avg * 1.5:
            pytest.fail(f"{symbol}: Rule 4 仍在触发 (hold_days={got}, avg={avg}) — "
                        f"若这是真实信号请更新本测试并记入 DEB")
