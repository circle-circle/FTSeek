"""UE [市场佐证] 段的**时点诚实化**契约 (2026-09-14, DEB-20260914-premarket-data-validity-audit §一 P1-3)。

用户裁"诚实化 + 补时点标签"。本文件钉四件事 (全部对应实测病灶, 不是纸面推断):

① **回退源的时点还原**: `universe_expansion_shadow.jsonl` 的 `date` 是**写入时刻的 ET 日期**
   (writer `date = _today_et_str()`, cron = ET 00:30), 而那一刻取到的行情是**上一交易日
   收盘** ⇒ 记录 date=2026-09-14 装的是 09-11(周五)的板块涨跌。修复前该采集日被当数据时点
   透传 (`published_at`), **时点错一个交易日**, 且渲染进 brief 的文本里没有任何日期。
   本钉: `date` 必须是**数据所属交易日**, `caliber` 必须写明"上一交易日 X 收盘"。

② **失实声明不得回归**: 生产两窗 (ET 07:40 da_feed / 09:10) 结构上不可能拿到"当日"脉搏
   (脉搏由 sector_pulse.py 在 ET 10:00-15:30 写, 唯一命中者是 ET 12:30 的 `--midday`
   **只读影子**) ⇒ 该输入从未进过任何生产指令。原渲染标签"盘中板块脉搏(实时)"是失实声明;
   docstring 里"每日盘前生成(07:42 ET)"(实为 DA 在 ET 08:00 写)、"周更机械影子 capture"
   (实为 ET 00:30 日更) 同样是失实声明。本钉: 源码里不得再出现无口径的旧标签行。

③ **丢弃必须留痕**: 脉搏文件存在但不是当日 ⇒ 不再静默 (原 `except Exception: pass`)。
   本钉用 capfd 抓 stdout。

④ **未来日期守卫**: `_age_days` 为**负数**时原逻辑直接放行 (守卫只写 `> 4`) ⇒ writer 时钟
   可疑时未来日期的数据会被当"最新"消费。本钉: 未来日期一律拒绝并告警。

回滚: 删本文件 (回到无管控状态); 代码侧回滚见 DEB-20260914 §二 (本批同时升
PROMPT_VERSION v2.13 / BRIEF_TEMPLATE_VERSION ue_brief_v4, 两者回滚须同步)。
"""
import json
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import scripts.llm_universe_scan as ue  # noqa: E402

TODAY = "2026-09-14"          # 周一
PREV_SESSION = "2026-09-11"   # 上一交易日 = 周五
LABOR_DAY_TUESDAY = "2026-09-08"   # 前一交易日须跨过 09-07 劳动节 → 09-04(周五)
LABOR_DAY_PREV = "2026-09-04"


def _env(tmp_path, monkeypatch, *, rotation=None, pulse=None, captures=None):
    """把 `_ROOT` 与机械 capture 路径指到 tmp (测试禁写生产 data/, 见 tests/conftest.py)。"""
    (tmp_path / "data" / "shadow").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(ue, "_ROOT", tmp_path)
    monkeypatch.setattr(ue, "_MECH_CAPTURE_PATH",
                        tmp_path / "data" / "shadow" / "universe_expansion_shadow.jsonl")
    monkeypatch.setattr(ue, "_today", lambda: TODAY)
    if rotation is not None:
        (tmp_path / "data" / "deep_sector_rotation.json").write_text(
            json.dumps(rotation, ensure_ascii=False), encoding="utf-8")
    if pulse is not None:
        (tmp_path / "data" / "sector_pulse.json").write_text(
            json.dumps(pulse, ensure_ascii=False), encoding="utf-8")
    if captures is not None:
        with open(ue._MECH_CAPTURE_PATH, "w", encoding="utf-8") as f:
            for r in captures:
                f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _capture(date, *, strong=(), weak=(), surfaced=()):
    return {
        "type": "capture", "date": date,
        "strong_sectors": [{"etf": e, "name": n, "change_pct": c} for e, n, c in strong],
        "weak_sectors": [{"etf": e, "name": n, "change_pct": c} for e, n, c in weak],
        "surfaced": [{"symbol": s, "direction": d, "change_pct": c} for s, d, c in surfaced],
    }


def _rotation(date, *, strongest=("Technology",), weakest=("Utilities",),
              changes=None, generated_at="2026-09-14T08:03:00-04:00"):
    return {"date": date, "generated_at": generated_at,
            "strongest": list(strongest), "weakest": list(weakest),
            "sector_changes": changes if changes is not None else
            {"Technology": -0.0, "Utilities": -0.98}}


class TestFallbackTimePoint:
    """① 回退源的 `date` 必须是数据所属交易日, 而不是采集时刻。"""

    def test_capture_date_is_rewritten_to_data_session(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch, captures=[_capture(
            TODAY, strong=[("XLK", "Technology", 1.32), ("XLI", "Industrials", 1.07)],
            surfaced=[("ESTC", "UP", 19.3)])])
        out = ue._gather_market_corroboration()
        assert out, "回退源应供出材料"
        assert out["date"] == PREV_SESSION, (
            f"capture 的 date={TODAY} 是**采集日**, 数据实为 {PREV_SESSION} 收盘 ⇒ "
            f"透传 date 会让 published_at 差一个交易日 (实测缺陷)")
        assert f"上一交易日 {PREV_SESSION}" in out["caliber"], out["caliber"]
        assert "非今日盘中" in out["caliber"], "口径必须显式否掉'这是今日盘中'的误读"

    def test_published_at_uses_data_session_not_capture_stamp(self, tmp_path, monkeypatch):
        """结构化字段与渲染串必须同时点 (否则索引/正文不对应)。"""
        _env(tmp_path, monkeypatch, captures=[_capture(
            TODAY, strong=[("XLK", "Technology", 1.32)])])
        out = ue._gather_market_corroboration()
        f = ue._fact("market_corroboration", "轮入板块: x", published_at=out.get("date"))
        assert f["published_at"] == PREV_SESSION

    def test_future_dated_capture_is_refused(self, tmp_path, monkeypatch, capfd):
        """④ writer 时钟跑到未来时, 不许把未来日期的数据当'最新'消费。"""
        _env(tmp_path, monkeypatch, captures=[_capture("2026-09-18")])
        out = ue._gather_market_corroboration()
        assert out == {}, "未来日期的 capture 必须拒绝 (原 `_age_days > 4` 守卫对负数放行)"
        assert "日期在未来" in capfd.readouterr().out

    def test_stale_capture_still_refused(self, tmp_path, monkeypatch, capfd):
        """既有陈旧守卫不得被本批改坏 (回归闸)。"""
        _env(tmp_path, monkeypatch, captures=[_capture("2026-09-01")])
        out = ue._gather_market_corroboration()
        assert out == {}
        assert "已陈旧" in capfd.readouterr().out


class TestMainSourceCaliber:
    """主源 (DA 日更盘前轮动) 也必须带时点。"""

    def test_rotation_carries_caliber_and_date(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch, rotation=_rotation(TODAY))
        out = ue._gather_market_corroboration()
        assert out["date"] == TODAY
        assert out["caliber"].startswith(f"DA 日更盘前轮动 {TODAY}")
        assert "盘前生成" in out["caliber"]
        assert out["strong_sectors"] == ["Technology(-0.0%)"], "主源渲染格式不得漂移"

    def test_rotation_stale_falls_back_visibly(self, tmp_path, monkeypatch, capfd):
        """D2: da_feed(ET 07:40) 早于 DA(ET 08:00) 生产 ⇒ 次次落回退。替换必须可见。"""
        _env(tmp_path, monkeypatch, rotation=_rotation(PREV_SESSION),
             captures=[_capture(TODAY, strong=[("XLK", "Technology", 1.32)])])
        out = ue._gather_market_corroboration()
        assert "DA 日更盘前轮动" not in out.get("caliber", "")
        assert "capture" in out["caliber"]
        assert "落 capture 回退" in capfd.readouterr().out


class TestPulseHonesty:
    """②③ 脉搏: 生产窗结构上不可得 ⇒ 声明不许失实, 丢弃不许静默。"""

    def test_today_pulse_gets_asof_caliber(self, tmp_path, monkeypatch):
        _env(tmp_path, monkeypatch, rotation=_rotation(TODAY),
             pulse={"date": TODAY, "asof_et": "15:30", "ranked": ["Semis(+1.5%)"]})
        out = ue._gather_market_corroboration()
        assert out["intraday_pulse"] == ["Semis(+1.5%)"], "当日脉搏(仅 midday 影子的时点)应可注入"
        assert "15:30" in out["caliber_pulse"]

    def test_previous_session_pulse_is_dropped_and_logged(self, tmp_path, monkeypatch, capfd):
        """盘前两窗的真实情形: 只有上一交易日的脉搏。按 DEB-20260729 §十 的价值判断
        **不注入**, 但必须留痕 —— 否则"这条输入从未生效"无人可知。"""
        _env(tmp_path, monkeypatch, rotation=_rotation(TODAY),
             pulse={"date": PREV_SESSION, "asof_et": "15:30", "ranked": ["Semis(+1.5%)"]})
        out = ue._gather_market_corroboration()
        assert "intraday_pulse" not in out, "盘前不注入上一交易日脉搏 (价值判断保留)"
        assert "盘中脉搏未注入" in capfd.readouterr().out

    def test_pulse_read_failure_is_not_silent(self, tmp_path, monkeypatch, capfd):
        """③ 原 `except Exception: pass` 必须已改成留痕 (不许静默吞错)。"""
        _env(tmp_path, monkeypatch, rotation=_rotation(TODAY))
        (tmp_path / "data" / "sector_pulse.json").write_text("{ 不是合法 json", encoding="utf-8")
        out = ue._gather_market_corroboration()
        assert out, "脉搏读坏不得连带毁掉主源材料 (fail-open)"
        assert "盘中脉搏读取失败" in capfd.readouterr().out


class TestPrevTradingDayHelper:

    def test_monday_maps_to_friday(self):
        assert ue._prev_trading_day_str(TODAY) == PREV_SESSION

    def test_skips_holiday(self):
        """跨劳动节 (2026-09-07 周一) 的周二, 上一个交易日是 09-04 周五 —— 证明确实用日历。"""
        assert ue._prev_trading_day_str(LABOR_DAY_TUESDAY) == LABOR_DAY_PREV

    def test_calendar_failure_fails_open(self, monkeypatch, capfd):
        """日历不可用时不得抛 (时点口径退回原值, 并留痕)。"""
        import app.utils.holiday_calendar as hc

        def _boom():
            raise RuntimeError("calendar down")

        monkeypatch.setattr(hc, "get_holiday_calendar", _boom)
        assert ue._prev_trading_day_str(TODAY) == TODAY
        assert "上一交易日推算失败" in capfd.readouterr().out


class TestNoUndatedLabelRegression:
    """源码棘轮: 无口径的旧标签不得回归 (变红 = 有人把时点又抹掉了)。"""

    SRC = None

    @classmethod
    def _src(cls):
        if cls.SRC is None:
            import pathlib
            cls.SRC = (pathlib.Path(ue.__file__).read_text(encoding="utf-8"))
        return cls.SRC

    @pytest.mark.parametrize("old", [
        '"轮入板块: "', '"轮出板块: "', '"宇宙外异动: "',
        '"盘中板块脉搏(实时): "',
    ])
    def test_old_undated_form_is_gone(self, old):
        assert old not in self._src(), f"失实/无时点声明回归: {old}"

    @pytest.mark.parametrize("claim", [
        "ET 08:00",          # 主源真实生成时刻 (原写 07:42)
        "ET 00:30",          # capture 真实采集时刻 (原写"周更")
        "结构上永不成立",       # 脉搏在生产两窗的真相 (原写"一并注入")
    ])
    def test_corrected_claims_are_documented(self, claim):
        """修正后的**事实**必须留在 docstring 里 —— 否则下一个读者会重新把它当疑问去查一遍。

        实现注: 上面那条只查**代码字面量**(带引号, 注释/docstring 里引用旧措辞是合法的);
        本条的三个 claim 只出现在 docstring 散文里, 故正向钉住。
        """
        assert claim in self._src(), f"修正说明被删/改: {claim}"

    def test_caliber_is_rendered_into_model_visible_text(self):
        """口径必须进**模型看得到的正文** (只写进 docstring 等于没修)。"""
        src = self._src()
        for frag in ('f"轮入板块{_cal_pre}: "', 'f"轮出板块{_cal_pre}: "',
                     'f"宇宙外异动{_cal_pre}: "', 'f"盘中板块脉搏 [{_cal_p or'):
            assert frag in src, f"渲染行缺时点口径: {frag}"