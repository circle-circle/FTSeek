"""B3 schema v2→v1 显式投影 (仅供真实快照回归证明「旧值逐字保留, 仅命名变更」).

非生产消费者。v1→v2 改名映射 (DEB-20261128-hk-collector-field-semantics):
  rollup.open_chg_pct      → first_obs_chg_pct   (指数+篮子)
  篮子 rollup.high/low_chg_pct → obs_high/obs_low_chg_pct (采样极值, 语义不变仅改名)
  指数 rollup.high/low_chg_pct → day_high/day_low_chg_pct (交易所真实值分列);
      v1 缺失回退采样极值的混合语义在投影中如实还原 (dh if not None else oh)。
  指数 rollup 新增 obs_high/obs_low_chg_pct (v1 无, 投影剔除)。
  schema_version 2→1; units 还原 v1 字典。tick_rows/events/adr 等其余面未动。
"""
import copy

V1_UNITS = {
    "chg_pct": "percent",
    "high_chg_pct": "percent",
    "low_chg_pct": "percent",
    "chg_pct_eq": "percent",
    "dispersion_pp": "percent_points",
    "turnover_hkd": "HKD_absolute",
    "price": "HKD_absolute",
    "breadth": "count",
}


def project_day_doc_to_v1(doc):
    d = copy.deepcopy(doc)
    d["schema_version"] = 1
    d["units"] = dict(V1_UNITS)
    for roll in d.get("indices", {}).values():
        roll["open_chg_pct"] = roll.pop("first_obs_chg_pct")
        dh, dl = roll.pop("day_high_chg_pct"), roll.pop("day_low_chg_pct")
        oh, ol = roll.pop("obs_high_chg_pct"), roll.pop("obs_low_chg_pct")
        roll["high_chg_pct"] = dh if dh is not None else oh   # v1 回退混合语义如实还原
        roll["low_chg_pct"] = dl if dl is not None else ol
    for roll in d.get("sectors", {}).values():
        roll["open_chg_pct"] = roll.pop("first_obs_chg_pct")
        roll["high_chg_pct"] = roll.pop("obs_high_chg_pct")
        roll["low_chg_pct"] = roll.pop("obs_low_chg_pct")
    return d
