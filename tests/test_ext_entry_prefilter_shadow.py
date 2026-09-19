"""入口预筛影子 (只记录不拦) 的契约测试 — 2026-09-19 用户令「按建议执行」."""
import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _executor_module():
    spec = importlib.util.spec_from_file_location(
        "ext_entry_executor_mod", ROOT / "scripts" / "extended_hours_entry_executor.py"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_prefilter_verdicts_bands_and_gates():
    ee = _executor_module()
    hot = ee.prefilter_verdicts(6.2, 12.5, 3_000_000)
    assert hot["chg_band"] == "5~10" and hot["price_band"] == "$5~20"
    assert hot["passes_chg_3"] and hot["passes_chg_5"] and hot["passes_price_5_20"]

    lukewarm = ee.prefilter_verdicts(1.4, 30.0, 900_000)
    assert lukewarm["chg_band"] == "0~2" and lukewarm["price_band"] == "$20~50"
    assert not lukewarm["passes_current_gate"]      # 涨幅低于现役闸 2.0
    assert not lukewarm["passes_chg_3"]

    penny = ee.prefilter_verdicts(19.7, 0.42, 5000)
    assert penny["price_band"] == "penny_lt5"
    assert not penny["passes_current_gate"]         # $5 价格下限闸

    # 2026-09-19 180 日样本裁决后的候选判据 (唯一两窗口同向为正的维度 = 时段成交量)
    big = ee.prefilter_verdicts(6.2, 12.5, 3.0e7)
    assert big["passes_vol_20m"] and big["passes_vol_20m_chg_3"]
    assert not big["passes_negative_band_risk"]
    thin = ee.prefilter_verdicts(1.1, 40.0, 3.0e6)
    assert not thin["passes_vol_20m"] and thin["passes_negative_band_risk"]


def test_prefilter_shadow_records_one_line_per_run(tmp_path, monkeypatch):
    ee = _executor_module()
    target = tmp_path / "prefilter.jsonl"
    monkeypatch.setattr(ee, "PREFILTER_SHADOW", target)
    monkeypatch.setattr(ee, "PREFILTER_SHADOW_ON", True)
    ee._record_prefilter_shadow("afterhours", [
        {"symbol": "US.AAA", "px": 12.5, "chg": 6.2, "vol": 3_000_000},
        {"symbol": "US.BBB", "px": 0.42, "chg": 19.7, "vol": 1000},
    ])
    rows = [json.loads(l) for l in target.read_text().splitlines() if l.strip()]
    assert len(rows) == 1                       # 每次运行一行 (不按候选膨胀)
    assert rows[0]["n"] == 2 and rows[0]["session"] == "afterhours"
    assert {r["symbol"] for r in rows[0]["rows"]} == {"US.AAA", "US.BBB"}
    assert rows[0]["thresholds"]["chg"] == ee.ENTRY_CHANGE_PCT


def test_prefilter_shadow_never_raises_on_bad_payload(tmp_path, monkeypatch):
    """埋点不得影响交易主流程 (但失败要可见 —— 2026-09-19 坑: except:pass 吞掉 NameError)."""
    ee = _executor_module()
    monkeypatch.setattr(ee, "PREFILTER_SHADOW", tmp_path / "x" / "y.jsonl")
    monkeypatch.setattr(ee, "PREFILTER_SHADOW_ON", True)
    ee._record_prefilter_shadow("afterhours", [{"symbol": None, "px": None, "chg": "oops", "vol": None}])
    ee._record_prefilter_shadow("afterhours", [])
