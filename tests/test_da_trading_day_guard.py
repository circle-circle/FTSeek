"""DA 交易日守卫契约测试 (2026-09-07, SKILL.md pitfall -6 / handoff「下一步」0-假日).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护 (同 test_da_output_contract / test_da_fundamental_contract 惯例)。

立项根因 (2026-09-07 核实, 三层证据):
  1. `pipeline.main()` 原本**零日期判断**, cron `0 20 * * 1-5` 只排周末不排美股假日
     → 假日照跑: 白烧 LLM + 写陈旧指令 + (修复前) IC 快照 trade_date 写成非交易日。
  2. `ic_writer._get_et_trade_date()` 自带 6 条硬编码 `known_holidays`, **漏劳动节**
     以及 MLK/总统日/耶稣受难日/阵亡将士日/六月节 → 2026-09-07(劳动节,周一) 返回
     非交易日 → FTSeek `backfill_forward_returns.py` 找不到日K → close_t0/t1 恒 NULL
     → 该批 snapshot **静默永久退出 IC 样本**(不报错, 最难发现的一类损失)。
  3. 仓内本有 SSOT `app/utils/holiday_calendar.py`, DA 侧零引用 → 平行真源必然漂移。

本测试钉住的不变量 (按"最容易出事"排序):
  A. L2 规则层 2026-2028 全日休市日**逐日精确** (含 Good Friday 复活节算法 / observed
     平移 / 元旦落周六不补休特例) — 规则算错 = 守卫在假日放行或在交易日误拦。
  B. L2 与本仓 SSOT 离线表**跨仓一致**, 唯一允许的差异 = 2026-12-24 (已登记, 见下)。
  C. 哥伦布日 / 退伍军人日**必须是交易日** (NYSE 照常开盘; 联邦假日只休债市) —
     拿债市日历套股市是本类修复最典型的复发方向。
  D. `latest_trading_day` 跨"周末+假日"连续前推正确, 且不越过 max_back 死循环。
  E. 失败语义 = **fail-open** (三层全灭→视为交易日→照跑), 与 UE / FTSeek playbook 同口径。
     用户批准的方案即此; 谁把它改成 fail-close → 本测试立红 (fail-close 会在日历
     故障日静默停掉整条 DA 主链, 比白跑一轮代价大得多)。
  F. ic_writer **不再持有自己的假日表** (反平行真源回归闸) 且真的委托给适配层。
  G. pipeline 守卫在 `Pipeline(...)` 构造**之前**生效: 非交易日零扫描/零 LLM/零写入,
     `--force` 逃逸口有效, `--format json` 下仍吐合法 JSON (cron Step4 按 JSON 解析)。

已知天花板 (ponytail 标注):
  - L1 (`ftseek_ssot`) 在无 OpenD 时走本仓离线硬编码表, 有 OpenD 时走 Futu 权威源
    → **同一日期在不同环境可能由不同层给出答案**。故 A/B/C 全部直接断言 L2 纯函数
    (`rule_holidays` / `_l2_rule`), 不依赖 L1 可用性; 只有"三层都同意"的关键日期
    (劳动节等) 才断言 `resolve()` 的布尔值。
  - 2026-12-24 是 L1 离线表与 L2 的**唯一分歧**: NYSE 该日提前收盘(13:00 ET)而非
    全日休市, 按"是否交易日"口径应为 True(L2); 但本仓离线 `US_HOLIDAYS` 保守标为整休。
    这是**仓内侧**口径不一致 (Futu 可用时 L1 也返回 True), 已登记 handoff, 本测试只
    钉住"L2 的口径正确 + 分歧范围恰好只有这一天", 不去掩盖也不去"修正"它。
"""
import ast
import inspect
import json
import sys
from datetime import date, datetime, timedelta

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

try:
    import trading_calendar as TC
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    pytest.skip(f"skill 不可用 (trading_calendar): {_e}", allow_module_level=True)

try:
    import ic_writer as IW
    import pipeline as PL
except Exception as _e:  # pragma: no cover
    pytest.skip(f"skill 不可用 (ic_writer/pipeline): {_e}", allow_module_level=True)


# ── 权威参照表: NYSE 全日休市日 (公开历表 + 与本仓 SSOT 交叉核验) ──────────────
NYSE_FULL_CLOSURES = {
    2026: {date(2026, 1, 1), date(2026, 1, 19), date(2026, 2, 16), date(2026, 4, 3),
           date(2026, 5, 25), date(2026, 6, 19), date(2026, 7, 3), date(2026, 9, 7),
           date(2026, 11, 26), date(2026, 12, 25)},
    # 2027: 六月节(6/19)落周六→观察在周五 6/18; 独立日(7/4)落周日→观察在周一 7/5;
    #       圣诞(12/25)落周六→观察在周五 12/24 (故 2027-12-24 是整休日, 非半日)。
    2027: {date(2027, 1, 1), date(2027, 1, 18), date(2027, 2, 15), date(2027, 3, 26),
           date(2027, 5, 31), date(2027, 6, 18), date(2027, 7, 5), date(2027, 9, 6),
           date(2027, 11, 25), date(2027, 12, 24)},
    # 2028: 元旦落周六 → NYSE **不设**补休 (先例 2021-12-31 周五为完整交易日) → 仅 9 天。
    2028: {date(2028, 1, 17), date(2028, 2, 21), date(2028, 4, 14), date(2028, 5, 29),
           date(2028, 6, 19), date(2028, 7, 4), date(2028, 9, 4), date(2028, 11, 23),
           date(2028, 12, 25)},
}


# ══════════════════════════════════════════════════════════════════════════
# A. L2 规则层精确性
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("year", [2026, 2027, 2028])
def test_rule_holidays_exact_per_year(year):
    """规则现算必须逐日等于权威表 (多一天=交易日误拦, 少一天=假日放行)。"""
    assert TC.rule_holidays(year) == NYSE_FULL_CLOSURES[year]


def test_rule_holidays_are_all_weekdays():
    """卫生检查: 全日休市日不可能落在周末 (落了说明 observed 平移算反)。"""
    for year in NYSE_FULL_CLOSURES:
        for d in TC.rule_holidays(year):
            assert d.weekday() < 5, f"{d} 是周末, observed 平移有误"


def test_good_friday_uses_real_easter_algorithm():
    """耶稣受难日 = 复活节 − 2 天; 复活节无固定日期, 必须靠算法 (硬编码必错)。"""
    assert TC._easter(2026) == date(2026, 4, 5)
    assert TC._easter(2027) == date(2027, 3, 28)
    assert TC._easter(2028) == date(2028, 4, 16)
    for year in (2026, 2027, 2028):
        gf = TC._easter(year) - timedelta(days=2)
        assert gf in TC.rule_holidays(year) and gf.weekday() == 4  # 恒为周五


def test_new_year_on_saturday_is_not_observed():
    """元旦落周六 → NYSE 不补休 (前一日周五照常开盘)。2028 正是此例。"""
    assert date(2028, 1, 1).weekday() == 5
    assert TC._observed(date(2028, 1, 1), 2028) is None
    assert date(2028, 1, 1) not in TC.rule_holidays(2028)
    assert date(2027, 12, 31) not in TC.rule_holidays(2027)   # 周五完整交易日
    assert TC._l2_rule(date(2027, 12, 31)) is True


@pytest.mark.parametrize("fixed_day,observed_expected", [
    (date(2026, 6, 19), date(2026, 6, 19)),   # 六月节落周五 → 当天
    (date(2027, 6, 19), date(2027, 6, 18)),   # 落周六 → 前移周五
    (date(2026, 7, 4), date(2026, 7, 3)),     # 独立日落周六 → 前移周五
    (date(2027, 7, 4), date(2027, 7, 5)),     # 落周日 → 后移周一
    (date(2026, 12, 25), date(2026, 12, 25)),
    (date(2027, 12, 25), date(2027, 12, 24)),  # 落周六 → 前移周五 (整休, 非半日)
])
def test_observed_shift_direction(fixed_day, observed_expected):
    """固定日期假日的 observed 平移: 周六→前一日, 周日→后一日 (方向反了会漏整休日)。"""
    assert TC._observed(fixed_day, fixed_day.year) == observed_expected
    assert observed_expected in TC.rule_holidays(fixed_day.year)


# ══════════════════════════════════════════════════════════════════════════
# B. 跨仓一致性 (反平行真源)
# ══════════════════════════════════════════════════════════════════════════
def test_rule_layer_matches_ftseek_ssot_offline_table():
    """L2 必须与本仓 SSOT 离线表一致, **唯一**允许差异 = 2026-12-24 (见模块 docstring)。

    这条是"平行真源"的正面回归闸: 任一侧改表而另一侧没跟上 → 差异集合变大 → 立红。
    """
    from app.utils.holiday_calendar import US_HOLIDAYS
    for year in (2026, 2027, 2028):
        rule = TC.rule_holidays(year)
        ssot = {d for d in US_HOLIDAYS if d.year == year}
        assert rule - ssot == set(), f"{year}: L2 多出 SSOT 没有的假日 {sorted(rule - ssot)}"
        assert ssot - rule == {date(2026, 12, 24)} & ssot, (
            f"{year}: L2 与 SSOT 差异超出已登记的 2026-12-24 → {sorted(ssot - rule)}")


def test_christmas_eve_2026_is_the_only_documented_divergence():
    """分歧范围钉死: L2 认为 2026-12-24 是交易日(提前收盘), SSOT 离线表认为整休。"""
    assert TC._l2_rule(date(2026, 12, 24)) is True          # NYSE 提前收盘 13:00 ET, 仍是交易日
    assert date(2026, 12, 24) not in TC.rule_holidays(2026)
    assert date(2026, 12, 25) in TC.rule_holidays(2026)
    # 2027-12-24 则是**整休日**(圣诞落周六的观察日) — 两天同名不同性质, 别一刀切
    assert TC._l2_rule(date(2027, 12, 24)) is False


# ══════════════════════════════════════════════════════════════════════════
# C. 联邦假日 ≠ 股市假日 (最典型复发方向)
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("d,why", [
    (date(2026, 10, 12), "哥伦布日 (10月第2个周一)"),
    (date(2027, 10, 11), "哥伦布日"),
    (date(2026, 11, 11), "退伍军人日"),
    (date(2027, 11, 11), "退伍军人日"),
])
def test_federal_only_holidays_are_trading_days(d, why):
    """NYSE 在这两天**照常开盘** (只有债市休市) → 守卫必须放行。"""
    assert TC._l2_rule(d) is True, f"{d} {why} 被误判为休市"
    assert d not in TC.rule_holidays(d.year)
    assert TC.is_us_trading_day(d) is True


# ══════════════════════════════════════════════════════════════════════════
# D. latest_trading_day 前推
# ══════════════════════════════════════════════════════════════════════════
@pytest.mark.parametrize("start,expected", [
    (date(2026, 9, 7), date(2026, 9, 4)),    # 劳动节周一 → 上周五 (本次事故的确切场景)
    (date(2026, 9, 6), date(2026, 9, 4)),    # 周日 → 周五
    (date(2026, 9, 5), date(2026, 9, 4)),    # 周六 → 周五
    (date(2026, 9, 4), date(2026, 9, 4)),    # 交易日自身 (含 d)
    (date(2026, 9, 8), date(2026, 9, 8)),    # 假日后首个交易日不越过
    (date(2026, 11, 26), date(2026, 11, 25)),  # 感恩节 → 周三
    (date(2026, 11, 27), date(2026, 11, 27)),  # 感恩节次日 = 半日, **仍是交易日**
    (date(2026, 7, 3), date(2026, 7, 2)),    # 独立日观察日 → 周四
    (date(2027, 12, 24), date(2027, 12, 23)),  # 圣诞观察日(整休) → 周四
    (date(2028, 1, 1), date(2027, 12, 31)),  # 元旦落周六(不补休) → 周五完整交易日
])
def test_latest_trading_day(start, expected):
    got, layer = TC.latest_trading_day(start)
    assert got == expected, f"{start} → {got} (期望 {expected}, 判定层 {layer})"
    assert layer != "exhausted"


def test_latest_trading_day_never_returns_non_trading_day():
    """穷举 2026 全年: 返回值必须是交易日且不晚于起点 (前推不能反向)。"""
    d = date(2026, 1, 1)
    while d.year == 2026:
        got, layer = TC.latest_trading_day(d)
        assert got <= d
        assert TC.is_us_trading_day(got) is True, f"{d} → {got} 仍非交易日 (层 {layer})"
        d += timedelta(days=1)


def test_latest_trading_day_exhausts_instead_of_looping(monkeypatch):
    """max_back 是死循环保险丝: 全灭时必须返回 'exhausted' 而不是挂住。"""
    monkeypatch.setattr(TC, "resolve", lambda d: (False, "rule"))
    got, layer = TC.latest_trading_day(date(2026, 9, 8), max_back=3)
    assert layer == "exhausted" and got == date(2026, 9, 8)


# ══════════════════════════════════════════════════════════════════════════
# E. 失败语义 = fail-open (用户批准口径, 不得改成 fail-close)
# ══════════════════════════════════════════════════════════════════════════
def test_resolve_fail_open_when_all_layers_dead(monkeypatch):
    """三层全灭 → (True, 'fail-open'): 宁可白跑一轮, 不可静默停掉整条主链。"""
    monkeypatch.setattr(TC, "_l1_ftseek", lambda d: None)
    monkeypatch.setattr(TC, "_l2_rule", lambda d: None)
    monkeypatch.setattr(TC, "_l3_legacy", lambda d: None)
    TC.resolve.cache_clear()
    try:
        assert TC.resolve(date(2026, 9, 7)) == (True, "fail-open")
        assert TC.is_us_trading_day(date(2026, 9, 7)) is True
    finally:
        TC.resolve.cache_clear()


def test_resolve_layer_precedence_is_ssot_first():
    """层序不可调换: L1 是唯一知道**临时全国哀悼日**(无规则可算)的层。"""
    src = inspect.getsource(TC.resolve)
    assert src.index("ftseek_ssot") < src.index('"rule"') < src.index("legacy")


def test_legacy_layer_is_known_incomplete_and_must_stay_that_way():
    """L3 逐字保留原 6 条表 — 它**漏劳动节**, 这正是本次修复的对象。

    钉住"legacy 不完整"看似反直觉, 实则是反平行真源闸: 谁往 legacy 里补日期,
    说明又在造第二份假日表 (模块 docstring 明令禁止) → 本测试立红指路改 L2。
    """
    assert len(TC._LEGACY_KNOWN_HOLIDAYS) == 6
    assert (9, 7) not in TC._LEGACY_KNOWN_HOLIDAYS          # 劳动节: legacy 会漏
    assert TC._l3_legacy(date(2026, 9, 7)) is True          # ← 已知错误, 故 legacy 必须排最后
    assert TC._l3_legacy(date(2026, 11, 26)) is False       # 感恩节: legacy 有
    assert TC._l3_legacy(date(2026, 9, 5)) is False         # 周末: legacy 也拦得住


def test_labor_day_2026_resolves_false_under_live_layers():
    """事故主角: 2026-09-07 劳动节。L1/L2 都必须判 False (只有已知错误的 L3 会放行)。"""
    ok, layer = TC.resolve(date(2026, 9, 7))
    assert ok is False, f"劳动节被判为交易日 (层 {layer}) — 缺陷复发"
    assert layer in ("ftseek_ssot", "rule")
    assert TC._l2_rule(date(2026, 9, 7)) is False
    assert TC.resolve(date(2026, 9, 8))[0] is True          # 假日后首个交易日不得误拦


def test_today_et_not_naive_local_date():
    """ET 口径防 CST 陷阱: 容器时区 CST(UTC+8), `date.today()` 会把 ET 周日判成周一。"""
    assert TC.now_et().tzinfo is not None
    assert TC.today_et() == TC.now_et().date()
    assert str(TC.ET_TZ) in ("America/New_York", "US/Eastern")


# ══════════════════════════════════════════════════════════════════════════
# F. ic_writer 委托 (A 方案)
# ══════════════════════════════════════════════════════════════════════════
def test_ic_writer_no_longer_owns_a_holiday_table():
    """反平行真源闸: ic_writer **代码**里不得再出现自带假日表 (docstring 提及不算)。

    走 AST 而非字符串匹配: 修复说明的 docstring 里必须保留 `known_holidays` 字样
    (记录根因), 字符串匹配会把它误判成复发。这里只钉"没有任何赋值语句在造表"。
    """
    tree = ast.parse(inspect.getsource(IW))
    assigned = {t.id for n in ast.walk(tree) if isinstance(n, ast.Assign)
                for t in n.targets if isinstance(t, ast.Name)}
    assert "known_holidays" not in assigned, "ic_writer 又长出了自己的假日表 → 平行真源复发"
    called = {n.func.attr for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)}
    assert "latest_trading_day" in called, "委托未接线 (没有调用适配层)"


class _FrozenDatetime:
    """把 ic_writer 的 `datetime.now(_ET_TZ)` 冻结到指定 ET 时刻 (只替 now, 其余透传)。"""

    frozen = None

    @classmethod
    def now(cls, tz=None):
        return cls.frozen.astimezone(tz) if tz else cls.frozen


@pytest.fixture
def freeze_et(monkeypatch):
    def _freeze(y, m, d, hour=8):
        _FrozenDatetime.frozen = datetime(y, m, d, hour, 0, tzinfo=TC.ET_TZ)
        monkeypatch.setattr(IW, "datetime", _FrozenDatetime)
    return _freeze


@pytest.mark.parametrize("et_now,expected", [
    ((2026, 9, 7, 8), "2026-09-04"),    # 劳动节盘前 → 上周五 (**修复前这里返回 09-07**)
    ((2026, 9, 6, 20), "2026-09-04"),   # 周日晚 (cron 实际触发时刻) → 上周五
    ((2026, 9, 8, 8), "2026-09-08"),    # 假日后首个交易日 → 当天
    ((2026, 11, 26, 8), "2026-11-25"),  # 感恩节 → 周三
    ((2026, 11, 27, 8), "2026-11-27"),  # 感恩节次日(半日) → 当天, 不得再前推
    # 圣诞(周五整休) → 周四或周三皆可, 取决于哪层给出答案: 12/24 是 L1/L2 唯一分歧日
    # (L2: 提前收盘=交易日; L1 离线表: 保守整休; L1 有 OpenD 时走 Futu 亦为交易日)。
    # 两个答案**都是真交易日** ⇒ IC 回填都成立, 差别仅一天新鲜度, 故不钉死单日。
    ((2026, 12, 25, 8), {"2026-12-24", "2026-12-23"}),
])
def test_ic_writer_trade_date_is_a_real_trading_day(freeze_et, et_now, expected):
    freeze_et(*et_now[:3], hour=et_now[3])
    got = IW._get_et_trade_date()
    assert got in (expected if isinstance(expected, set) else {expected}), (
        f"trade_date={got}, 期望 {expected}")
    parsed = date.fromisoformat(got)
    assert TC.is_us_trading_day(parsed) is True, f"trade_date={got} 非交易日 → IC 静默丢样"


def test_ic_writer_falls_back_to_weekend_only_if_calendar_unavailable(monkeypatch):
    """适配层整体不可用 → 退回**纯周末前推**(不改既有语义), 并留痕到 stderr。

    这条钉的是"退路存在": 兜底路径宁可漏假日也不能抛异常打断 IC 写入。
    """
    monkeypatch.setitem(sys.modules, "trading_calendar", None)   # import 时抛 ImportError
    _FrozenDatetime.frozen = datetime(2026, 9, 6, 20, 0, tzinfo=TC.ET_TZ)
    monkeypatch.setattr(IW, "datetime", _FrozenDatetime)
    assert IW._get_et_trade_date() == "2026-09-04"               # 周日 → 周五


def test_ic_writer_real_now_is_a_trading_day():
    """不冻结的真实调用 (CI 每日体检): 产出的 trade_date 必须永远是交易日。"""
    got = IW._get_et_trade_date()
    parsed = date.fromisoformat(got)
    assert TC.is_us_trading_day(parsed) is True
    assert parsed <= TC.today_et()


# ══════════════════════════════════════════════════════════════════════════
# G. pipeline 守卫 (B 方案)
# ══════════════════════════════════════════════════════════════════════════
class _StubPipeline:
    """替身: 只记录"是否被构造/被调用", 绝不触发扫描/LLM/DB 写入。"""

    constructed = 0
    called = []

    def __init__(self, *a, **kw):
        type(self).constructed += 1
        self.kwargs = kw

    def run_scan_only(self):
        type(self).called.append("scan")
        return {"stub": True}

    def run_full(self):
        type(self).called.append("full")
        return {"stub": True}


@pytest.fixture
def guard_env(monkeypatch):
    """冻结 ET 当日 + 换上替身 Pipeline, 返回一个跑 main() 的闭包。"""
    def _run(fake_et_day, argv_extra=(), fmt="json"):
        _StubPipeline.constructed = 0
        _StubPipeline.called = []
        monkeypatch.setattr(PL, "Pipeline", _StubPipeline)
        monkeypatch.setattr(TC, "today_et", lambda: fake_et_day)
        argv = ["pipeline.py", "--market", "US", "--limit", "1", "--top", "1",
                "--format", fmt, "--no-db-write", "--no-pool-write"]
        monkeypatch.setattr(sys, "argv", argv + list(argv_extra))
        PL.main()
        return _StubPipeline.constructed, _StubPipeline.called
    return _run


LABOR_DAY = date(2026, 9, 7)
TUESDAY = date(2026, 9, 8)


def test_pipeline_exposes_force_escape_hatch():
    """`--force` 必须存在 (人工假日复盘/调试的唯一入口; cron 不得传)。"""
    src = inspect.getsource(PL.main)
    assert '"--force"' in src
    assert "跳过美股交易日守卫" in src
    assert "action=\"store_true\"" in src.split('"--force"')[1][:80]


def test_guard_runs_before_pipeline_construction():
    """顺序不变量: 守卫必须在 `Pipeline(...)` 构造**之前** — 晚一步就已经开始扫描了。"""
    src = inspect.getsource(PL.main)
    assert src.index("交易日守卫") < src.index("pipeline = Pipeline(")


def test_guard_skips_on_holiday(guard_env, capsys):
    """非交易日: 零构造/零扫描, 且 JSON 契约不破 (cron Step4 按 JSON 解析)。"""
    constructed, called = guard_env(LABOR_DAY)
    out = capsys.readouterr()
    assert constructed == 0 and called == []
    payload = json.loads(out.out)
    assert payload["skipped"] is True
    assert payload["skip_reason"] == "NON_TRADING_DAY"
    assert payload["trade_date_et"] == "2026-09-07"
    assert payload["calendar_layer"] in ("ftseek_ssot", "rule")
    # 跳过时仍给**同形状空值**: cron `e0d574cc1a3c` Step4 明文提取这两个键,
    # 缺键会让 agent 在假日自由发挥编摘要 (形状契约比"少写几个键"重要)。
    assert payload["stages"]["signal"]["candidates_total"] == 0
    assert payload["stages"]["output"]["analyses_count"] == 0
    assert payload["stages"]["output"]["analyses"] == []
    assert "非美股交易日" in out.err


def test_guard_skips_on_weekend(guard_env, capsys):
    """周末同样拦 (cron 已排周末, 但人工误触发时守卫是第二道闸)。"""
    constructed, _ = guard_env(date(2026, 9, 6))     # 周日
    capsys.readouterr()
    assert constructed == 0


def test_guard_does_not_over_block_trading_day(guard_env, capsys):
    """交易日必须照跑 — 误拦比白跑代价大得多 (整条主链静默停摆)。"""
    constructed, called = guard_env(TUESDAY)
    capsys.readouterr()
    assert constructed == 1 and called == ["scan"]


def test_force_bypasses_guard(guard_env, capsys):
    """`--force` 在假日也要能跑 (逃逸口失效 = 人工复盘无路可走)。"""
    constructed, called = guard_env(LABOR_DAY, argv_extra=("--force",))
    out = capsys.readouterr()
    assert constructed == 1 and called == ["scan"]
    assert "NON_TRADING_DAY" not in out.out


def test_guard_is_fail_open_on_calendar_error(guard_env, capsys, monkeypatch):
    """日历抛异常 → fail-open 照跑, 并在 stderr 留痕 (不得静默停链)。"""
    def _boom(d):
        raise RuntimeError("calendar down")
    monkeypatch.setattr(TC, "resolve", _boom)
    constructed, called = guard_env(LABOR_DAY)
    out = capsys.readouterr()
    assert constructed == 1 and called == ["scan"]
    assert "fail-open" in out.err


def test_guard_text_format_stays_human_readable(guard_env, capsys):
    """`--format text` 下不吐 JSON, 只给人读的消息 (两种格式各有契约)。"""
    constructed, _ = guard_env(LABOR_DAY, fmt="text")
    out = capsys.readouterr()
    assert constructed == 0
    assert "非美股交易日" in (out.out + out.err)
    with pytest.raises(json.JSONDecodeError):
        json.loads(out.out)
