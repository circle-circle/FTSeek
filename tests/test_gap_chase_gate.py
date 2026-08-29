"""gap_chase_gate 影子门: 评估逻辑行为级测试 + 注册表钉子。

红绿纪律: 判定函数对构造数据必须给出机械一致结论; 注册项必须保持 OBSERVING 直到裁决。
"""
import json
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.gap_chase_gate_eval import load_cohorts, sign_test_one_sided, verdict  # noqa: E402
from app.playbook.shadow_registry import REGISTRY, ShadowStatus  # noqa: E402


def _mk_db(tmp_path: Path, rows: list) -> str:
    db = tmp_path / "t.db"
    con = sqlite3.connect(str(db))
    con.execute("""CREATE TABLE trade_records
                   (symbol TEXT, entry_time TEXT, pnl_net REAL, realized_pnl REAL,
                    source_type TEXT, metadata_json TEXT)""")
    for pnl, gap in rows:
        md = {"entry_gap": {"aligned_gap_pct": gap}} if gap is not None else {}
        con.execute("INSERT INTO trade_records VALUES ('US.X','2026-08-01T10:00',?,?, 'PLAYBOOK_SIM', ?)",
                    (pnl, pnl, json.dumps(md)))
    con.commit()
    con.close()
    return str(db)


def test_verdict_requires_min_n():
    assert verdict([-10.0] * 29) == "KEEP_SAMPLING"  # 全亏但样本不足
    assert verdict([-10.0] * 30) == "GRADUATE_CANDIDATE"


def test_verdict_requires_negative_mean_and_sign():
    assert verdict([-2.0] * 20 + [3.0] * 10) == "GRADUATE_CANDIDATE"  # 均-0.33, 20/30亏 p≈0.049
    assert verdict([-100.0] * 14 + [10.0] * 16) == "KEEP_SAMPLING"   # 亏损笔数未过半
    assert verdict([10.0] * 30) == "KEEP_SAMPLING"                   # 拦截组赚钱 → 不该拦


def test_sign_test_boundaries():
    assert sign_test_one_sided(0, 0) == 1.0
    assert abs(sign_test_one_sided(30, 30) - 1 / 2 ** 30) < 1e-12
    assert sign_test_one_sided(20, 30) < 0.05 < sign_test_one_sided(19, 30)


def test_load_cohorts_splits_on_gate(tmp_path):
    db = _mk_db(tmp_path, [(-5.0, 6.0), (3.0, 5.0), (1.0, 4.9), (-2.0, None), (7.0, 12.0)])
    blocked, rest, cov = load_cohorts(db, "2026-07-01")
    assert sorted(blocked) == [-5.0, 3.0, 7.0]   # gap>=5 三笔
    assert rest == [1.0]                          # 4.9 不进拦截组
    assert abs(cov - 4 / 5) < 1e-9                # None 无字段不计覆盖


def test_registry_pin():
    items = {it.id: it for it in REGISTRY}
    it = items.get("gap_chase_gate")
    assert it is not None, "gap_chase_gate 未注册"
    assert it.status in (ShadowStatus.OBSERVING, ShadowStatus.READY_TO_EVALUATE)
    assert it.min_n == 30 and it.eval_script == "scripts/gap_chase_gate_eval.py"
    assert "单侧符号检验" in it.graduation_criteria
    assert Path(it.eval_script).exists() or (Path(__file__).resolve().parents[1] / it.eval_script).exists()
