"""DA 板块脉搏日期/新鲜度守卫契约测试 (2026-09-14, 用户裁「全部处理」P1-1).

被测对象在 skill 仓 (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不在本仓
→ 全文件以 `skill 不可用即 skip` 保护 (同 test_da_trading_day_guard /
test_da_output_contract 惯例)。

立项根因 (盘前数据有效性审计, 2026-09-14 实测):
  `data/sector_pulse.json` 由 `scripts/sector_pulse.py` 在**盘中**每 30min 覆写
  (cron: CST 22:00-23:30 + 次日 00:00-03:30 = ET 10:00-15:30)。而 DA 跑
  **08:00 ET 盘前** ⇒ DA 读到的这份**永远是上一交易日**的收盘读数 (周一跨周末
  = 3 天前); 若脉搏 cron 挂掉则是任意久的旧文件 (`sector_pulse.py` 自身有
  "成功<6 块则保留旧文件" 的宁旧勿假逻辑, 会让旧文件一直留着)。

  原实现 (`Pipeline.run_full` 内联段) 只要文件存在就注入, 且标签写成
  `盘中板块脉搏(15:30 ET)` —— **无日期**: 读起来就是"今天盘中"。后果是模型把上一
  交易日尾盘的板块强弱当成今日实时轮动, 据此判个股板块顺风/逆风 (同族: LIF 盘前
  盲区 —— 时点不清的值直接喂给模型)。

本测试钉住的不变量:
  A. 脉搏日期 == **上一个已收盘交易日** 才注入, 且标签**带日期**、措辞为"上一交易日";
  B. 非当日/过期/未来日期的脉搏**一律不注入**, 且不因守卫而丢掉可用的板块涨跌段
     (fail-open: 日历故障不该断 DA 主链);
  C. 上一交易日必须由**交易日历**算出 (跨周末 + 跨假日), 不是"减一天"或"减三天";
  D. 回归闸: 源码里不得再出现无日期的 "盘中板块脉搏(" 标签。

已知天花板 (ponytail 标注):
  - 本测试只钉 DA **注入**侧。UE 侧 (`scripts/llm_universe_scan.py:996`) 与
    Playbook 记录侧 (app/playbook/pipeline.py::_gather_intraday_evidence) 各有
    自己的 date 守卫, 分别由各自测试钉住, 不在此重复断言。
  - 交易日历 L1 (`ftseek_ssot`) 在无 OpenD 时会退到 L2 规则层; 本测试只断言跨
    "周末 + 劳动节" 这类三层应当一致的日期 (劳动节 NYSE 全日休市, 无争议)。
"""
import ast
import json
import sys
from datetime import date

import pytest

_SKILL = "/root/.hermes/skills/deep-stock-analysis/scripts"
if _SKILL not in sys.path:
    sys.path.insert(0, _SKILL)

try:
    import pipeline as PL
    import trading_calendar as TC
except Exception as _e:  # pragma: no cover - 环境无 skill 时整体跳过
    pytest.skip(f"skill 不可用: {_e}", allow_module_level=True)

_SKILL_PIPELINE = f"{_SKILL}/pipeline.py"

# 一个最小 scan_result: 只带板块涨跌 (scanner rotation 形状)
_SR = {"sector_rotation": {"sector_changes": {"Technology": -1.2, "Energy": 0.8}}}


def _pulse_file(tmp_path, day: str, asof: str = "15:30"):
    """写一份 sector_pulse.json 到测试沙箱 (绝不碰生产 data/)。"""
    p = tmp_path / "sector_pulse.json"
    p.write_text(json.dumps({
        "date": day, "asof_et": asof,
        "changes": {"Technology": -1.2, "Energy": 0.8},
        "ranked": ["Technology(-1.2%)", "Energy(+0.8%)"],
        "weakest3": ["Technology"], "strongest3": ["Energy"],
    }, ensure_ascii=False), encoding="utf-8")
    return str(p)


class TestPulseDateGuard:
    def test_previous_session_pulse_is_injected_with_date(self, tmp_path):
        """A: 周一盘前 → 上一交易日(周五)脉搏应注入, 且标签带日期 + '上一交易日'。"""
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 14),          # 周一
            pulse_path=_pulse_file(tmp_path, "2026-09-11"))   # 周五
        assert "上一交易日(2026-09-11)盘中板块脉搏" in ctx
        assert "15:30 ET" in ctx
        assert "Energy(+0.8%)" in ctx

    def test_stale_pulse_is_dropped_but_sector_changes_kept(self, tmp_path):
        """B: 脉搏 cron 挂掉 (陈旧文件) → 丢弃脉搏, 但板块涨跌段照旧可用 (fail-open)。"""
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 14),
            pulse_path=_pulse_file(tmp_path, "2026-09-04"))
        assert "盘中板块脉搏" not in ctx, "过期脉搏绝不能注入"
        assert "当前快照板块ETF涨跌(弱→强)" in ctx, "日历/脉搏缺失不该断掉涨跌段"

    def test_future_dated_pulse_is_dropped(self, tmp_path):
        """B: 日期晚于期望 (时钟异常/人为写坏) 同样不注入 —— 不可信即不用。"""
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 14),
            pulse_path=_pulse_file(tmp_path, "2026-09-14"))
        assert "盘中板块脉搏" not in ctx

    def test_missing_pulse_file_is_silent(self, tmp_path):
        """文件不存在 → 只有涨跌段, 不抛异常。"""
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 14),
            pulse_path=str(tmp_path / "nope.json"))
        assert ctx == "当前快照板块ETF涨跌(弱→强): Technology(-1.20%), Energy(+0.80%)"

    def test_label_never_says_yesterday_or_today(self, tmp_path):
        """A 的负向面: 标签里不得出现无日期的时点措辞 ('昨日'/'今日盘中')。"""
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 14),
            pulse_path=_pulse_file(tmp_path, "2026-09-11"))
        assert "昨日" not in ctx and "今日" not in ctx

    def test_holiday_backoff_uses_trading_calendar(self, tmp_path):
        """C: 劳动节次日的上一交易日 = 节前周五 (跨假日), 由交易日历算出而非减一天。"""
        # 2026-09-07 是劳动节 (9月第1个周一, NYSE 全日休市) → 09-08(周二) 的
        # 上一交易日应为 09-04(周五), 而不是 09-07。
        prev, _layer = TC.latest_trading_day(date(2026, 9, 8) - __import__("datetime").timedelta(days=1))
        assert prev == date(2026, 9, 4), "前置事实: 09-07 劳动节应被日历跳过"
        ctx = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 8),
            pulse_path=_pulse_file(tmp_path, "2026-09-04"))
        assert "上一交易日(2026-09-04)盘中板块脉搏" in ctx
        # 反面: 一份 09-07 (假日, 不该有脉搏) 的文件必须被拒
        ctx_bad = PL.build_sector_context(
            _SR, today_et=date(2026, 9, 8),
            pulse_path=_pulse_file(tmp_path, "2026-09-07"))
        assert "盘中板块脉搏" not in ctx_bad


class TestSourceRatchet:
    def test_no_datelabel_pulse_label_in_source(self):
        """D: 源码里不得再有**无日期**的 '盘中板块脉搏(' 标签注入点。

        实现注: f-string 在 AST 里被拆成多段 Constant (`'上一交易日('` / `')盘中板块脉搏('`),
        逐段判定必然误伤 —— 故按 **JoinedStr 整棵子树的源码段** 判定 (ast.get_source_segment),
        并显式排除 docstring (讲病史处会引用旧标签原文)。
        """
        src = open(_SKILL_PIPELINE, encoding="utf-8").read()
        tree = ast.parse(src)
        docs = {ast.get_docstring(n, clean=False)
                for n in ast.walk(tree)
                if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                  ast.ClassDef))}
        parents = {}
        for p in ast.walk(tree):
            for c in ast.iter_child_nodes(p):
                parents[c] = p

        def _inside_fstring(node) -> bool:
            p = parents.get(node)
            while p is not None:
                if isinstance(p, ast.JoinedStr):
                    return True
                p = parents.get(p)
            return False

        bad = []
        for node in ast.walk(tree):
            if isinstance(node, ast.JoinedStr):
                seg = ast.get_source_segment(src, node) or ""
                if "盘中板块脉搏(" in seg and "上一交易日" not in seg:
                    bad.append((node.lineno, seg[:80]))
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in docs or _inside_fstring(node):
                    continue
                if "盘中板块脉搏(" in node.value and "上一交易日" not in node.value:
                    bad.append((node.lineno, node.value[:80]))
        assert not bad, f"仍有无日期的脉搏标签: {bad}"

    def test_run_full_delegates_to_guard_function(self):
        """D: 注入点必须走 `build_sector_context()` (守卫唯一入口, 防内联复发)。"""
        src = open(_SKILL_PIPELINE, encoding="utf-8").read()
        tree = ast.parse(src)
        cls = next(n for n in ast.walk(tree)
                   if isinstance(n, ast.ClassDef) and n.name == "Pipeline")
        run_full = next(n for n in cls.body
                        if isinstance(n, ast.FunctionDef) and n.name == "run_full")
        calls = {n.func.id for n in ast.walk(run_full)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
        assert "build_sector_context" in calls, \
            "run_full 必须调用 build_sector_context() (内联段一封, 日期守卫就失效)"
