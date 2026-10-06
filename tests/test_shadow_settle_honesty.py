"""影子结算日报诚实化护栏回归 (2026-09-15, 用户批准 1+2 落地).

触发事故 (09-14 日报实锤):
  ① 延迟成交双计: DELL/AAOI 被 vwap_slope 门拦后当日延迟成交 (playbook_instructions
     executed=1), 仍计入"被拦反事实"门统计。
  ② 单笔主导无标记: NOK 一笔 -259.11 占门合计 125% (其余 4 笔全小赢), 日报笔均
     -41.36 呈现为"门画像"; 实测 n≥3 门×日 4/5 被单笔主导。
  ③ 判读行引导单日符号下"松/紧"结论, 与周六预注册周检 (t/MDE, n≥30) 纪律冲突。

被测对象: scripts/settle_shadow_portfolio.py 的 summarize() (纯函数) 与
_load_delayed_inst_ids() (DB 降级安全)。结算/写盘逻辑零改动。
回滚: 删本文件即可。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

spec = importlib.util.spec_from_file_location(
    "settle_shadow_portfolio", PROJECT_ROOT / "scripts" / "settle_shadow_portfolio.py")
ssp = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ssp)


def _row(inst_id, symbol, gate, net, date="2026-09-14"):
    return {"inst_id": inst_id, "date": date, "symbol": symbol,
            "blocked_by": gate, "net": net}


# 09-14 生产实况复刻: 2614/2618 延迟成交, 2617 (NOK) 单笔主导
DAY = [
    _row(2616, "US.SKHY", "falsification_vwap_slope", 15.55),
    _row(2614, "US.DELL", "falsification_vwap_slope", 16.22),
    _row(2618, "US.AAOI", "falsification_vwap_slope", 8.73),
    _row(2615, "US.BE", "falsification_vwap_slope", 11.79),
    _row(2617, "US.NOK", "falsification_vwap_slope", -259.11),
    _row(2682, "US.RUM", "band", -26.65),
]
DELAYED = {2614, 2618}


def test_delayed_fill_excluded_from_gate_stats():
    out = "\n".join(ssp.summarize(DAY, DAY, DELAYED, skipped=0))
    assert "剔除延迟成交 2 笔" in out
    # vwap_slope 日n = 5-2 = 3 (SKHY/BE/NOK)
    vwap_line = [l for l in out.splitlines() if "falsification_vwap_slope" in l][0]
    cells = [c.strip() for c in vwap_line.strip("|").split("|")]
    assert cells[1] == "3", f"延迟成交未剔除: {vwap_line}"


def test_single_trade_dominance_flagged_and_named():
    out = "\n".join(ssp.summarize(DAY, DAY, DELAYED, skipped=0))
    assert "⚠ falsification_vwap_slope" in out
    assert "US.NOK" in out and "单笔噪声" in out
    # band 单笔 n=1 → 不触发主导标记 (len(v)>=2 门槛)
    band_flags = [l for l in out.splitlines() if l.startswith("⚠ band")]
    assert not band_flags


def test_dominance_flag_silent_when_no_single_driver():
    # sum=+45, max|单笔|=30 → 67% < 80% 阈值 = 无单笔主导
    balanced = [_row(1, "A", "g", 30.0), _row(2, "B", "g", -10.0), _row(3, "C", "g", 25.0)]
    out = "\n".join(ssp.summarize(balanced, balanced, set(), 0))
    assert not [l for l in out.splitlines() if l.startswith("⚠")]
    # 反例护栏: [+50,-40,+30] sum=+40, 单笔 +50 占 125% (剔除即翻号) → 必须标记
    driven = [_row(1, "A", "g", 50.0), _row(2, "B", "g", -40.0), _row(3, "C", "g", 30.0)]
    out2 = "\n".join(ssp.summarize(driven, driven, set(), 0))
    assert any(l.startswith("⚠ g") for l in out2.splitlines())


def test_guardrail_lines_present():
    out = "\n".join(ssp.summarize(DAY, DAY, DELAYED, skipped=0))
    assert "判读护栏" in out and "n<30" in out
    # 2026-10-06 P2: 末行指向真实消费方 (周日 IC 周报影子组合段)。
    # 旧文案"周六预注册周检"是错误指引 — ftseek_weekly_checkpoint.py 不读本台账 (grep 0 命中)。
    assert "周日 IC 周报" in out
    assert "周六预注册周检" not in out
    assert "先到先占" in out          # 归因披露 (blocked_by 非单门完整总体)
    # 旧判读行 (单日笔均→松/紧) 必须已移除
    assert "该松" not in out


def test_cumulative_column_uses_all_rows_minus_delayed():
    hist = [_row(900, "US.X", "falsification_vwap_slope", -100.0, date="2026-09-01"),
            _row(901, "US.Y", "falsification_vwap_slope", 20.0, date="2026-09-02"),
            _row(2614, "US.DELL", "falsification_vwap_slope", 16.22, date="2026-09-14")]
    all_rows = hist + DAY
    out = "\n".join(ssp.summarize(DAY, all_rows, DELAYED, skipped=0))
    vwap_line = [l for l in out.splitlines() if "falsification_vwap_slope" in l][0]
    cells = [c.strip() for c in vwap_line.strip("|").split("|")]
    # 累计n: 900/901/2616/2615/2617 = 5 (2614/2618 延迟成交剔除)
    assert cells[5] == "5", vwap_line
    assert cells[6] == str(int(round(-100.0 + 20.0 + 15.55 + 11.79 - 259.11)))


def test_header_first_line_preserves_silent_convention():
    """shell 包装器按首行两计数判 eff_new (N==M ⇒ 静默) — 首行格式不可破坏.

    2026-10-06: 守卫从字面 '^新结算 0 笔' 改为 eff_new 判定 (N_new==M_delayed),
    修复 '7 进 7 剔' 形态的零信息量推送漏网 (当日实弹)。本测试钉住守卫依赖的
    首行三形态: 0 笔 / N 笔无剔除 / N 笔含剔除段。
    """
    out0 = ssp.summarize([], [], set(), skipped=2)
    assert out0[0].startswith("新结算 0 笔")
    out6 = ssp.summarize(DAY, DAY, DELAYED, skipped=0)
    assert out6[0].startswith("新结算 6 笔")
    assert "剔除延迟成交 2 笔" in out6[0]
    # 全部剔除形态 (今日实弹: 7 进 7 剔) — 首行必须同时含两个计数供 shell 比对
    all_delayed = {_r["inst_id"] for _r in DAY}
    out_full = ssp.summarize(DAY, DAY, all_delayed, skipped=0)
    assert out_full[0].startswith("新结算 6 笔")
    assert "剔除延迟成交 6 笔" in out_full[0]
    assert not any(l.startswith("| 门 |") for l in out_full)   # eff_new=0 ⇒ 无表格 = 守卫静默正确


def test_no_rows_still_prints_guardrails():
    out = "\n".join(ssp.summarize([], [], set(), 0))
    assert "判读护栏" in out and "| 门 |" not in out


def test_delayed_loader_degrades_on_missing_db(tmp_path, monkeypatch):
    monkeypatch.setattr(ssp, "_ROOT", str(tmp_path))   # 无 data/ftseek_us.db
    assert ssp._load_delayed_inst_ids() == set()


def test_delayed_loader_reads_executed(tmp_path, monkeypatch):
    (tmp_path / "data").mkdir()
    db = tmp_path / "data" / "ftseek_us.db"
    c = sqlite3.connect(str(db))
    c.execute("CREATE TABLE playbook_instructions (id INTEGER, executed INTEGER)")
    c.executemany("INSERT INTO playbook_instructions VALUES (?,?)",
                  [(2614, 1), (2615, 0), (2618, 1)])
    c.commit()
    c.close()
    monkeypatch.setattr(ssp, "_ROOT", str(tmp_path))
    assert ssp._load_delayed_inst_ids() == {2614, 2618}
