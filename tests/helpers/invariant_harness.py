"""daily_invariant_check 单测隔离 harness (2026-09-14).

立起理由 — 三个实测缺口同源 (脚本的生产读面只有一半可注入):

① **生产读面泄漏**: `main()` 的 `DB`/`OUT` 可 patch, 但 W7 昨收分母哨兵三个档
   (`_W7_PMI`/`_W7_ETS`/`_W7_BASELINE`)、W3 的 sim/live 双库、W4/W5/W6 的扫描面与三个
   棘轮基线都是 **import 期从真 `REPO` 定死**的。只 patch DB/OUT 的"夹具干净应全绿"用例
   于是读到生产态: 2026-09-11 的昨收分母分叉 2 行 (INTC#2535 8.01pp / ITUB#2534 2.60pp)
   让 7 条与该议题无关的不变量用例连红 3 天 —
   `test_attribution_watch_retirement` ×3 / `test_invariant_d4_settled_fields` ×2 /
   `test_invariant_i10_exit_time` ×2。红着的守卫等于没有守卫, 真回归会被淹进噪声。
② **外部副作用**: `main(push_enabled=True)` 是默认值, W1_* 关键违反会**直推钉钉**
   (脚本自己的 docstring 写明 `push_enabled=False` 是"供测试/演练"的路径, 但四个测试
   文件都走的默认值) ⇒ 跑一遍单测就可能给值班人发一条真告警。
③ **基线误写**: 三个棘轮 (config_freeze / field_cardinality / log_hygiene) 与 W7 都有
   "基线文件不存在 → 就地建基线"分支。一旦 `REPO` 被指到 tmp (扫描面为空 ⇒ 计数 0)
   而基线路径仍指生产 `data/`, 一次单测就能把生产棘轮基线改写成 0 = 判据被静默清零。

用法 (在各测试文件的 `_run`/`_run_invariant` 里, patch 完 DB/OUT 之后调一次):

    isolate_invariant_check(dic, tmp_path, monkeypatch)
    rc = dic.main(push_enabled=False)

判据零变更: 本 harness 只把读面指到空目录, 各哨兵由此走**它们自己既有的**静默分支
(`skipped_no_shadow` / `skipped_old_schema` / `baseline_established`), 不新增任何豁免。

回滚: 删本文件 + 还原四处调用为裸 `dic.main()`。
"""
from pathlib import Path

# import 期就从真 REPO 定死、必须逐个改指的单值常量
# (_FIELD_CARD_SCRIPT 刻意不在列: W2 采集器要用真脚本, 其库口径由 mod._DB = DB 覆写,
#  DB 已指向夹具 ⇒ 表缺失走 skipped_old_schema, 不碰生产库。)
_REDIRECT_CONSTANTS = (
    "_FREEZE_BASELINE",
    "_FIELD_CARD_BASELINE",
    "_W7_PMI",
    "_W7_ETS",
    "_W7_BASELINE",
)


def isolate_invariant_check(dic, tmp_path, monkeypatch) -> Path:
    """把 daily_invariant_check 的全部生产读面指到 tmp 空目录, 返回该目录。"""
    iso = Path(tmp_path) / "_invariant_iso"
    iso.mkdir(parents=True, exist_ok=True)

    # REPO 决定所有"函数内才求值"的读面: W3 的 sim/live 双库路径、W4/W5/W6 的扫描面。
    # `_hygiene_py_files` 的 docstring 已写明 monkeypatch dic.REPO 是预期注入点。
    monkeypatch.setattr(dic, "REPO", iso)

    for name in _REDIRECT_CONSTANTS:
        if hasattr(dic, name):
            monkeypatch.setattr(dic, name, iso / f"absent_{name}.dat")

    baselines = getattr(dic, "_HYGIENE_BASELINES", None)
    if isinstance(baselines, dict):
        monkeypatch.setattr(dic, "_HYGIENE_BASELINES", {
            dim: iso / f"absent_hygiene_{dim}.json" for dim in baselines
        })
    return iso
