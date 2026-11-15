"""量能曲线尾部窗口读取的等价性契约 (2026-11-15 性能批)。

病灶: `_intraday_vol_curve_p50` 每标的每日首读把 5-7 MB / 8.5 万根 bar 的
`data/replay_bars_1m/{sym}.json` 全量 json.load, 只为取最近 20 个交易日 ⇒
日首 8-15 标的 = 40-105 MB 解析突发。现改为从文件尾按块回读 + 整体解析尾部片段。

该曲线是**盘中量能门的分母** (交易判定输入), 所以本用例的重点不是"跑通了",
而是"与全量解析逐值相同" —— 合成文件覆盖三种边界:
  1. 正常多日文件 (尾部窗口生效);
  2. 文件比窗口还小 (自然退化为全量语义);
  3. 尾部片段拼接后必须仍是合法 JSON 数组 (元素不被截断)。
"""
import json
from datetime import datetime, tzinfo

import pytest

from app.playbook.pipeline_parts.intraday_indicators import _read_bars_window


def _bar(ts: str, vol: float):
    return [ts, 100.0, 101.0, 99.0, 100.5, vol]


def _write_bars(path, days: int, bars_per_day: int = 5):
    bars = []
    for day in range(days):
        date = "2026-01-%02d" % (day + 1)
        for minute in range(bars_per_day):
            bars.append(_bar("%s 09:%02d:00" % (date, 30 + minute), 1000.0 + day * 10 + minute))
    path.write_text(json.dumps(bars), encoding="utf-8")
    return bars


def test_tail_window_matches_full_parse(tmp_path, monkeypatch):
    from app.playbook.pipeline_parts import intraday_indicators as ii

    path = tmp_path / "US_TEST.json"
    bars = _write_bars(path, days=30, bars_per_day=7)
    # 把块大小压到 256B: 合成文件本身很小, 不压块就会"一次读入整文件"(pos==0 分支),
    # 窗口逻辑根本没被行使。压块后才真正验证"只解析尾部 + 片段与全量逐值相同"。
    monkeypatch.setattr(ii, "_BAR_READ_CHUNK", 256)
    tail = ii._read_bars_window(path, 5)
    assert len(tail) < len(bars), "应当只解析尾部片段 (未生效则说明回落到全量)"
    assert tail == bars[-len(tail):]
    # 尾部必须覆盖至少 5 个交易日
    assert len({bar[0][:10] for bar in tail}) >= 5


def test_small_file_falls_back_to_full_semantics(tmp_path):
    path = tmp_path / "US_SMALL.json"
    bars = _write_bars(path, days=3, bars_per_day=4)
    assert _read_bars_window(path, 20) == bars


def test_returns_list_of_well_formed_bars(tmp_path):
    path = tmp_path / "US_OK.json"
    _write_bars(path, days=12, bars_per_day=3)
    for bar in _read_bars_window(path, 4):
        assert len(bar) == 6
        assert isinstance(bar[0], str) and bar[0][:4] == "2026"
        assert isinstance(bar[5], float)


def test_curve_value_identical_to_full_load(tmp_path):
    """端到端: 用尾部片段算出的 p50 曲线必须与全量一致 (判定输入不能漂)。"""
    from app.playbook.pipeline_parts import intraday_indicators as ii

    path = tmp_path / "US_CURVE.json"
    bars = _write_bars(path, days=14, bars_per_day=6)
    today = "2026-01-15"

    def curve(source_bars):
        by_day = {}
        for bar in source_bars:
            by_day.setdefault(str(bar[0])[:10], []).append(float(bar[5]))
        dates = sorted(k for k in by_day if k < today)[-20:]
        if len(dates) < 10:
            return None
        curves = []
        for date in dates:
            running, cum = 0.0, []
            for vol in by_day[date]:
                running += vol
                cum.append(running)
            curves.append(cum)
        out = []
        for i in range(max(len(c) for c in curves)):
            vals = sorted(c[i] for c in curves if i < len(c))
            if not vals:
                break
            n = len(vals)
            out.append(vals[n // 2] if n % 2 else (vals[n // 2 - 1] + vals[n // 2]) / 2.0)
        return out

    assert curve(ii._read_bars_window(path, 20 + 3)) == curve(bars)


def test_production_files_when_present(tmp_path):
    """生产数据存在时抽查 3 个真实文件 (只读), 缺失则跳过 (不造数据)。"""
    from pathlib import Path
    prod = Path(__file__).resolve().parents[2] / "data" / "replay_bars_1m"
    files = sorted(prod.glob("*.json"))[:3]
    if not files:
        pytest.skip("data/replay_bars_1m 不存在 (裸 worktree/新环境)")
    for path in files:
        with path.open(encoding="utf-8") as fh:
            full = json.load(fh)
        tail = _read_bars_window(path, 20 + 3)
        assert tail == full[-len(tail):], f"{path.name} 尾部片段与全量不一致"


# ── 默认参数自洽 (2026-11-15 第二批: 两处"默认值写错") ─────────────────────
def test_default_tz_provider_returns_tzinfo():
    """默认 now_et_provider 必须返回 tzinfo。

    病灶: 6 处默认分支塞 `app.utils.timezone.now_et` (返回 datetime), 而函数体是
    `datetime.now(provider())` ⇒ 不注入 provider 必抛 TypeError。生产 wrapper 注入了
    正确的 `_now_et` (返回 ET), 所以线上从未暴露 —— 只咬直接调用/脚本/新用例。
    """
    from app.playbook.pipeline_parts import intraday_indicators as ii

    tz = ii._resolve_tz_provider(None)()
    assert isinstance(tz, tzinfo), f"默认 provider 返回了 {type(tz).__name__}, 必须是 tzinfo"
    assert datetime.now(tz).tzinfo is not None


def test_default_repo_root_finds_replay_bars_dir():
    """默认 repo root 必须真的指到仓库根 (原 parents[2] 会算成 app/)。"""
    from pathlib import Path

    from app.playbook.pipeline_parts import intraday_indicators as ii

    root = ii._resolve_repo_root(None)
    assert root == Path(__file__).resolve().parents[2], f"默认 root 定位错误: {root}"
    if not (root / "data" / "replay_bars_1m").is_dir():
        pytest.skip("裸 worktree: 无 data/replay_bars_1m")
    assert (root / "data" / "replay_bars_1m").is_dir()


def test_curve_callable_without_any_injection():
    """零参数调用必须可用 (修复前: 先 TypeError, 修掉后仍会因路径错误静默 None)。"""
    from pathlib import Path

    from app.playbook.pipeline_parts import intraday_indicators as ii

    prod = Path(__file__).resolve().parents[2] / "data" / "replay_bars_1m"
    if not (prod / "US_AAPL.json").is_file():
        pytest.skip("裸 worktree: 无 US_AAPL.json")
    curve = ii._intraday_vol_curve_p50("US.AAPL", cache={})
    assert curve, "零参数调用应能读到真实 bar 并算出曲线 (静默 None = 默认值又坏了)"
    assert len(curve) > 100