"""W1 冻结守卫两维口径钉子 (2026-09-03 复盘批, REVIEW-20260903 §四.1).

病灶: `config_version` 在本仓有两个语义 — trade_records 的**列**是配置档案版本 (恒 v2),
metadata_json.config_version 才是**退出参数哈希** (exit-*, KR2 判分真口径)。原守卫只查列
→ 8/24 起退出参数换了 4 版而日级守卫恒 "ok", 只有周六周检查得出来 (损失封顶从"当天"
退化为"最多 6 天")。本文件钉住: ①退出哈希维必须存在且以 exit_versions 豁免清单对账;
②档案列维原行为不得回退; ③无哈希行只计数不当版本 (防首跑假漂移)。

2026-09-06 追加 (修尺子批④, OKR-2026-09 KR2 判分单位修订, 用户授权代裁): ④**cohort 维** —
主 cohort 窗 (`_FREEZE_COHORT_START=2026-09-03` → 9/30) 内退出哈希唯一值数必须 = 1, ≥2 即
`W1_kr2_cohort_broken` 当天告警; ⑤**豁免清单对本维不适用** (变更预算归零: 前 4 版全走了
豁免, 再开一次口子 = 判据名存实亡) ⇒ 已豁免的哈希进 cohort 窗同样判 broken, 且全窗维
`exit_status` 仍可为 'ok' —— **两口径并列, 不得互相冒充**。

回滚: 删本文件 + revert 对应提交即可, 零生产代码依赖。
"""
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

_spec = importlib.util.spec_from_file_location(
    "daily_invariant_check", PROJECT_ROOT / "scripts" / "daily_invariant_check.py"
)
dic = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dic)

_COLS = ("id INTEGER PRIMARY KEY, symbol TEXT, source_type TEXT, trade_date TEXT, "
         "config_version TEXT, metadata_json TEXT")

_EXEMPT = "exit-aaaaaaaaaaaa"
_NEW = "exit-bbbbbbbbbbbb"


def _mk_db(tmp_path):
    db = tmp_path / "ftseek_us.db"
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_COLS})")
    conn.commit()
    conn.close()
    return db


def _row(db, *, profile="v2", exit_hash=_EXEMPT, n=1, day="2026-09-02"):
    conn = sqlite3.connect(str(db))
    meta = "{}" if exit_hash is None else json.dumps({"config_version": exit_hash})
    for i in range(n):
        conn.execute(
            "INSERT INTO trade_records (symbol, source_type, trade_date, config_version, "
            "metadata_json) VALUES (?,?,?,?,?)",
            (f"US.T{i}", "PLAYBOOK_SIM", day, profile, meta))
    conn.commit()
    conn.close()


def _baseline(tmp_path, *, versions=("v2",), exit_versions=(_EXEMPT,)):
    p = tmp_path / "config_freeze_baseline.json"
    p.write_text(json.dumps({
        "versions": list(versions),
        "exit_versions": {v: "测试豁免说明 (裁决留痕)" for v in exit_versions},
    }, ensure_ascii=False), encoding="utf-8")
    return p


def _run(tmp_path, monkeypatch, db, baseline):
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "_FREEZE_BASELINE", baseline)
    hits = []

    def rec(k, rid, sym, msg):
        hits.append((k, str(sym), msg))

    return dic._check_config_freeze(rec), hits


def test_unexempted_exit_hash_alarms(tmp_path, monkeypatch):
    """KR2 真口径: 冻结窗内出现未豁免的退出参数哈希 → 当天告警 (不等周六周检)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=3)
    _row(db, exit_hash=_NEW, n=2)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_status"] == "drift"
    assert info["status"] == "drift", "两维取劣 — 退出维漂移不得被档案维 ok 掩盖"
    assert info["exit_versions"] == {_EXEMPT: 3, _NEW: 2}, "每哈希笔数=cohort 样本量, 须可见"
    assert [h[0] for h in hits] == ["W1_exit_version_drift"]
    assert _NEW in hits[0][1] and "豁免" in hits[0][2]


def test_all_exit_hashes_exempted_is_ok(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=4)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_status"] == "ok" and info["status"] == "ok"
    assert info["exit_versions"] == {_EXEMPT: 4}
    assert hits == []


def test_rows_without_exit_hash_are_counted_not_versioned(tmp_path, monkeypatch):
    """无哈希行 (旧行/写入侧未落 metadata) 只计数 — 当作版本会让首跑即假漂移。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=2)
    _row(db, exit_hash=None, n=3)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["exit_hash_missing_rows"] == 3
    assert info["exit_versions"] == {_EXEMPT: 2}
    assert info["exit_status"] == "ok" and hits == []


def test_profile_column_dimension_not_regressed(tmp_path, monkeypatch):
    """回归红线: 档案列维原行为 (基线比对 + W1_config_version_drift) 一行不改。"""
    db = _mk_db(tmp_path)
    _row(db, profile="v3", exit_hash=_EXEMPT)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["profile_status"] == "drift"
    assert info["exit_status"] == "ok"
    assert info["status"] == "drift"
    assert [h[0] for h in hits] == ["W1_config_version_drift"]


def test_no_rows_in_window_unchanged(tmp_path, monkeypatch):
    db = _mk_db(tmp_path)
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["status"] == "no_rows_in_window" and hits == []


def test_baseline_bootstrap_unchanged(tmp_path, monkeypatch):
    """无基线文件 → 自建基线并声明 baseline_established (原语义, 不判漂移)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_NEW)
    missing = tmp_path / "absent_baseline.json"
    monkeypatch.setattr(dic, "DB", db)
    monkeypatch.setattr(dic, "_FREEZE_BASELINE", missing)
    info = dic._check_config_freeze(lambda *a: None)
    assert info["status"] == "baseline_established"
    assert json.loads(missing.read_text(encoding="utf-8"))["versions"] == ["v2"]


def test_source_pins_metadata_hash_caliber():
    """源级契约: 退出维必须读 metadata_json 哈希 (防被"简化"回只读列 → 尺子再度失明)。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert "json_extract(metadata_json,'$.config_version')" in src
    assert "W1_exit_version_drift" in src
    assert 'baseline.get("exit_versions")' in src


def test_exempt_but_unobserved_hashes_are_visible_not_alarmed(tmp_path, monkeypatch):
    """幽灵豁免 (在册零笔) 只作信息级可见 — "先登记后上线"是合法顺序, 不得告警。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=2)
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_EXEMPT, _NEW)))
    assert info["exit_exempt_unobserved"] == [_NEW]
    assert info["exit_status"] == "ok" and hits == []


# ══════════ cohort 维 (2026-09-06 KR2 判分单位修订: 全窗 → 主 cohort 窗) ══════════
def test_cohort_single_version_is_ok(tmp_path, monkeypatch):
    """cohort 窗 (9/3 起) 内只有一个退出哈希 ⇒ single_version, 零告警 (判据在轨)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=5, day="2026-09-02")     # 窗前 (多版本期), 不入 cohort
    _row(db, exit_hash=_NEW, n=3, day="2026-09-03")
    _row(db, exit_hash=_NEW, n=2, day="2026-09-04")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_EXEMPT, _NEW)))
    c = info["cohort"]
    assert c["window_start"] == "2026-09-03" and c["n_unique"] == 1
    assert c["versions"] == {_NEW: {"n": 5, "first": "2026-09-03", "last": "2026-09-04"}}
    assert c["status"] == "single_version" and hits == []


def test_cohort_second_version_alarms_same_day(tmp_path, monkeypatch):
    """KR2 回滚线: cohort 窗内第 2 个哈希 ⇒ 当天告警 (损失封顶当天, 不等 9/30 结算)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_NEW, n=3, day="2026-09-03")
    _row(db, exit_hash="exit-cccccccccc", n=1, day="2026-09-08")
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_NEW, "exit-cccccccccc")))
    assert info["cohort"]["n_unique"] == 2 and info["cohort"]["status"] == "broken"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"]
    assert "BROKEN" in hits[0][2] and "豁免" in hits[0][2] and "10 月开跑会" in hits[0][2]


def test_cohort_broken_even_when_hash_is_exempted(tmp_path, monkeypatch):
    """**关键纪律**: 变更预算归零 ⇒ 豁免清单对 cohort 维不适用。

    全窗维 exit_status 仍 'ok' (该哈希在册豁免) 而 cohort 维 broken — 两口径并列,
    任何一侧都不得冒充另一侧 (原判分式查档案列恒 v2 = 结构性假绿的同族教训)。
    """
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=4, day="2026-09-03")
    _row(db, exit_hash=_NEW, n=2, day="2026-09-04")        # _NEW 也在豁免清单里
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_EXEMPT, _NEW)))
    assert info["exit_status"] == "ok", "全窗维: 两版均已豁免 ⇒ 无未豁免漂移"
    assert info["cohort"]["n_unique"] == 2 and info["cohort"]["status"] == "broken"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"], "cohort 维必须独立告警"


def test_cohort_rows_without_hash_are_counted_not_versioned(tmp_path, monkeypatch):
    """未平仓行 metadata 无哈希 ⇒ 只计数不当版本 (与全窗维同一纪律, 否则首跑即假 broken)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_NEW, n=2, day="2026-09-03")
    _row(db, exit_hash=None, n=3, day="2026-09-04")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_NEW,)))
    assert info["cohort"]["hash_missing_rows"] == 3
    assert info["cohort"]["n_unique"] == 1 and info["cohort"]["status"] == "single_version"
    assert hits == []


def test_cohort_no_rows_in_window(tmp_path, monkeypatch):
    """窗内零成交 (如长假/链路断流) ⇒ no_rows, 与"单版本"必须可区分 (不得当达成)。"""
    db = _mk_db(tmp_path)
    _row(db, exit_hash=_EXEMPT, n=2, day="2026-09-02")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path))
    assert info["cohort"]["n_unique"] == 0 and info["cohort"]["status"] == "no_rows"
    assert info["cohort"]["versions"] == {} and hits == []


def test_cohort_start_is_pinned_and_documented():
    """源级契约: cohort 起点写死在守卫里且带修订出处 (改窗 = 改判据 = 须先改 OKR)。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert '_FREEZE_COHORT_START = "2026-09-03"' in src
    assert "W1_kr2_cohort_broken" in src and "OKR-2026-09 KR2" in src
    assert "变更预算" in src and "豁免通道" in src, "修订理由必须写在代码注释里 (可审计)"


# ══════════ sub_cohort 观测面 (2026-09-11, DEB-20260909 §7.6 选项(d) 双口径并列) ══════════
# 选项(d) 的两条红线: ①主 cohort **判分维一字不动** (不重开窗、不放宽 BROKEN);
# ②子窗**只观测不判分** (不产生违反、不冒充 KR2 达标证据)。以下逐条钉死。
# 夹具须带 exit_price 列 — 既有 _COLS 没有该列, 正是守卫 skipped_old_schema 降级路径的成因。
_COLS_CLOSED = ("id INTEGER PRIMARY KEY, symbol TEXT, source_type TEXT, trade_date TEXT, "
                "config_version TEXT, metadata_json TEXT, exit_price REAL")

_OLD = "exit-de49b1299290"      # 变更前版本 (9/3→9/9)
_CUR = "exit-13f6c2cc7fc4"      # 9/9 行动项C 变更后版本


def _mk_db_closed(tmp_path, *, name="ftseek_us.db"):
    db = tmp_path / name
    conn = sqlite3.connect(str(db))
    conn.execute(f"CREATE TABLE trade_records ({_COLS_CLOSED})")
    conn.commit()
    conn.close()
    return db


def _row_closed(db, *, exit_hash=_CUR, n=1, day="2026-09-10", exit_price=100.0, profile="v2"):
    """exit_price=0/None 表示未平仓 — 子窗只数已闭合行 (未平仓不构成样本)。"""
    conn = sqlite3.connect(str(db))
    meta = "{}" if exit_hash is None else json.dumps({"config_version": exit_hash})
    for i in range(n):
        conn.execute(
            "INSERT INTO trade_records (symbol, source_type, trade_date, config_version, "
            "metadata_json, exit_price) VALUES (?,?,?,?,?,?)",
            (f"US.S{i}", "PLAYBOOK_SIM", day, profile, meta, exit_price))
    conn.commit()
    conn.close()


def test_sub_cohort_single_version_observed_without_alarming(tmp_path, monkeypatch):
    """子窗内单版本 ⇒ 如实报 single_version + 笔数, 但**零违反** (观测面不判分)。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_CUR, n=8, day="2026-09-10")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_CUR,)))
    s = info["sub_cohort"]
    assert s["window_start"] == "2026-09-10" and s["n_unique"] == 1
    assert s["n_closed"] == 8 and s["status"] == "single_version"
    assert s["not_kr2_verdict"] is True, "必须自带防误读标记, 不得被面板当成 KR2 达标证据"
    assert hits == [], "观测面不得产生违反 — 否则就是新增了一把判分尺子"


def test_sub_cohort_does_not_relax_main_cohort_broken(tmp_path, monkeypatch):
    """**核心红线**: 主 cohort 已 BROKEN 时, 子窗单版本**不得**冲淡/覆盖主判定。

    这是选项(d) 与选项(a)/(c) 的分界: 移窗或裁"行为中性豁免"都会让 BROKEN 消失,
    双口径并列则两份事实同时在场 (沿用 KR2 边界注"不得互相冒充"的既有惯例)。
    """
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OLD, n=37, day="2026-09-03")     # 变更前
    _row_closed(db, exit_hash=_CUR, n=9, day="2026-09-09")      # 变更后 (跨子窗起点)
    _row_closed(db, exit_hash=_CUR, n=8, day="2026-09-10")      # 子窗内
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_OLD, _CUR)))
    assert info["cohort"]["n_unique"] == 2 and info["cohort"]["status"] == "broken"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"], "主判定必须照告, 不被子窗抑制"
    assert info["sub_cohort"]["n_unique"] == 1 and info["sub_cohort"]["status"] == "single_version"
    assert "选项(d)" in hits[0][2] and "sub_cohort" in hits[0][2], \
        "告警文案须自陈已按选项(d) 落档且不放宽 (运维读到告警即知处置口径)"


def test_sub_cohort_broken_is_reported_but_still_not_a_verdict(tmp_path, monkeypatch):
    """子窗自身也分裂 ⇒ status='broken' 如实报, 但**不新增违反键** (仍只有主维那一条)。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OLD, n=2, day="2026-09-03")
    _row_closed(db, exit_hash=_CUR, n=3, day="2026-09-10")
    _row_closed(db, exit_hash="exit-dddddddddddd", n=1, day="2026-09-11")
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_OLD, _CUR, "exit-dddddddddddd")))
    assert info["sub_cohort"]["n_unique"] == 2 and info["sub_cohort"]["status"] == "broken"
    assert info["sub_cohort"]["not_kr2_verdict"] is True
    assert "不是" in info["sub_cohort"]["note"], \
        "note 文本须自陈非判分对象 (消费方即使忽略 not_kr2_verdict 标记也读得到)"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"], \
        "子窗分裂不得自造告警键 (它不是判据); 主维已就同一事实告过警"


def test_sub_cohort_counts_only_closed_rows(tmp_path, monkeypatch):
    """子窗只数已闭合行; 未平仓行仍计入主 cohort 维 (两维口径差异是**有意**的)。

    主维必须含未平仓行才能发现 hash_missing_rows (写入侧停止落哈希); 子窗的用途是
    "破判后还有多少**可测**样本", 未平仓行不构成样本。
    """
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_CUR, n=5, day="2026-09-10", exit_price=100.0)
    _row_closed(db, exit_hash=None, n=3, day="2026-09-10", exit_price=0)   # 未平仓
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_CUR,)))
    assert info["sub_cohort"]["n_closed"] == 5, "未平仓行不得计入子窗样本量"
    assert info["sub_cohort"]["n_unique"] == 1
    assert info["cohort"]["hash_missing_rows"] == 3, "主维仍须看见无哈希行 (口径不得趋同)"
    assert hits == []


def test_sub_cohort_excludes_straddler_opened_before_window(tmp_path, monkeypatch):
    """骑墙笔 (开仓在子窗前、退出在子窗后) 不入子窗 — 窗按 trade_date=开仓日切。

    实证依据: trade 1473 US.SNDK 开仓 9/9 (变更前)、退出 9/10T09:38 ET (变更后),
    是窗内唯一混源笔; 把混源交易放进"单版本窗"会重新制造歧义 (故起点取 9/10 而非 9/9)。
    """
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_CUR, n=1, day="2026-09-09")    # 骑墙笔: 开仓 9/9
    _row_closed(db, exit_hash=_CUR, n=4, day="2026-09-10")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_CUR,)))
    assert info["sub_cohort"]["n_closed"] == 4, "9/9 开仓行不得混入 9/10 子窗"
    assert info["sub_cohort"]["versions"][_CUR]["first"] == "2026-09-10"
    assert hits == []


def test_sub_cohort_skipped_on_old_schema_without_hostaging_main_verdict(tmp_path, monkeypatch):
    """缺 exit_price 列的旧库/最小夹具 ⇒ 观测面 skipped_old_schema, **主 cohort 维照判**。

    回归红线 (2026-09-11 落地时实测教训): 观测面比主维多依赖一列, 初版无独立容错时
    本文件既有钉子 **12/14 被打红** = 主判据被观测面绑架。本例把该回归永久钉住。
    """
    db = _mk_db(tmp_path)                                     # 无 exit_price 列
    _row(db, exit_hash=_NEW, n=3, day="2026-09-03")
    _row(db, exit_hash="exit-cccccccccc", n=1, day="2026-09-08")
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_NEW, "exit-cccccccccc")))
    assert info["sub_cohort"]["status"] == "skipped_old_schema"
    assert info["sub_cohort"]["not_kr2_verdict"] is True
    assert info["cohort"]["n_unique"] == 2 and info["cohort"]["status"] == "broken"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"], "旧库形态下主判据不得失明"


def test_sub_cohort_no_rows_is_distinguishable_from_single_version(tmp_path, monkeypatch):
    """子窗零闭合 ⇒ no_rows, 与 single_version 必须可区分 (零样本不得读成"判据在轨")。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OLD, n=2, day="2026-09-03")     # 全在子窗前
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_OLD,)))
    assert info["sub_cohort"]["status"] == "no_rows" and info["sub_cohort"]["n_closed"] == 0
    assert info["sub_cohort"]["versions"] == {} and hits == []


def test_sub_cohort_start_pinned_and_scoring_window_untouched():
    """源级契约: 子窗起点写死 + 主判分窗**未被子窗改动** (选项(d) 的定义性约束)。

    子窗起点若改成自动推导"最新哈希首个 trade_date", 任何未来换版都会静默把窗口重置到
    自己上线那天 ⇒ 判据永远单版本 = 比 9/3 勘误修掉的"读错列恒 v2"更隐蔽的结构性假绿。
    """
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert '_SUB_COHORT_START = "2026-09-10"' in src
    assert '_FREEZE_COHORT_START = "2026-09-03"' in src, "选项(d) 不得移动主判分窗"
    assert '"not_kr2_verdict": True' in src, "观测面必须自带非判据标记"
    assert "exit_price > 0" in src, "子窗只数已闭合行的口径须写在 SQL 里"


# ══════════ W1 事件型直推 (2026-09-11, DEB-20260909 §7.2/§7.7⑤: 告警链二次断裂) ══════════
# 病灶: 本脚本单次运行恒只输出 1 行汇总 ERROR, 而 error_alert.py 阈值地板 MIN_THRESHOLD=3
# ⇒ 9/11 实测 W1_kr2_cohort_broken 落日志但**从未推送** (判据破了没人知道)。
# 速率型行数阈值与"判据破"这类事件型告警结构性不兼容; 调低阈值只会让日常噪声刷屏。
class _FakeHub:
    def __init__(self, *, ok=True, push_raises=None, stop_raises=None):
        self.ok, self.push_raises, self.stop_raises = ok, push_raises, stop_raises
        self.sent, self.stop_calls = [], 0

    def push(self, msg, sync=False):
        if self.push_raises is not None:
            raise self.push_raises
        self.sent.append({"msg": msg, "sync": sync})
        return self.ok

    def stop(self):
        self.stop_calls += 1
        if self.stop_raises is not None:
            raise self.stop_raises


def _arm_push(monkeypatch, tmp_path, hub, *, prev=None):
    """把 dic.OUT 指到 tmp (测试**绝不**写生产 data/invariant_check_latest.json) 并装假 hub。"""
    out = tmp_path / "invariant_check_latest.json"
    if prev is not None:
        out.write_text(json.dumps(prev, ensure_ascii=False), encoding="utf-8")
    monkeypatch.setattr(dic, "OUT", out)
    import app.push as push_mod
    monkeypatch.setattr(push_mod, "get_push_hub", lambda: hub)
    return out


_V = {"W1_kr2_cohort_broken": 1}
_EX = {"W1_kr2_cohort_broken": "exit-a,exit-b"}


def test_w1_push_fires_on_first_violation_and_stops_hub(tmp_path, monkeypatch):
    """首次违反必推; sync=True (短命 cron 进程要真实送达结果); 推完必须 stop() 关线程池。

    非 W1 违反即使同轮存在也**不得混入直推体** — 各走各的通道 (不动别人的判据)。
    """
    hub = _FakeHub(ok=True)
    _arm_push(monkeypatch, tmp_path, hub)
    viol = dict(_V, I1_pnl_vs_prices=3, W2_new_field_degen=1)
    out = dic._push_w1_critical(viol, _EX, "2026-09-11T05:05:09-04:00")
    assert out["attempted"] is True and out["ok"] is True and out["error"] is None
    assert out["keys"] == ["W1_kr2_cohort_broken"], "只推 W1_* 键"
    assert len(hub.sent) == 1 and hub.sent[0]["sync"] is True
    assert hub.stop_calls == 1, "不 stop 会让 cron 进程挂在 hub 的非 daemon worker 上"
    msg = hub.sent[0]["msg"]
    assert "W1" in msg.title
    assert msg.level.value == "high" and msg.category.value == "risk_alert"
    assert "invariant_check_latest.json" in msg.body, "告警须指向可审计产物"
    assert "I1_pnl_vs_prices" not in msg.body and "W2_new_field_degen" not in msg.body, \
        "非 W1 违反仍走 error_alert 速率通道, 不得混入事件型直推"


def test_w1_push_suppressed_when_same_keyset_already_delivered(tmp_path, monkeypatch):
    """KR2 已破是**持续状态**: 与上次已成功送达的键集相同则不重推 (否则每晚刷屏 ⇒ 守卫被无视)。"""
    hub = _FakeHub(ok=True)
    _arm_push(monkeypatch, tmp_path, hub,
              prev={"w1_direct_push": {"keys": ["W1_kr2_cohort_broken"],
                                       "attempted": True, "ok": True}})
    out = dic._push_w1_critical(_V, _EX, "2026-09-12T05:05:09-04:00")
    assert out["attempted"] is False and out["suppressed_reason"]
    assert hub.sent == [], "已送达的同一事实不得重推"


def test_w1_push_redelivers_when_keyset_changes(tmp_path, monkeypatch):
    """键集变化 (如全窗维另报 W1_exit_version_drift) ⇒ 必推, 不被旧送达记录抑制。"""
    hub = _FakeHub(ok=True)
    _arm_push(monkeypatch, tmp_path, hub,
              prev={"w1_direct_push": {"keys": ["W1_kr2_cohort_broken"],
                                       "attempted": True, "ok": True}})
    viol = {"W1_kr2_cohort_broken": 1, "W1_exit_version_drift": 2}
    out = dic._push_w1_critical(viol, _EX, "2026-09-12T05:05:09-04:00")
    assert out["attempted"] is True and out["suppressed_reason"] is None
    assert out["keys"] == ["W1_exit_version_drift", "W1_kr2_cohort_broken"]
    assert len(hub.sent) == 1


def test_w1_push_retries_after_failed_delivery(tmp_path, monkeypatch):
    """上次 attempted 但 ok=False ⇒ **必须重推** (宁可重推不可漏推; 漏推=判据破无人知)。"""
    hub = _FakeHub(ok=True)
    _arm_push(monkeypatch, tmp_path, hub,
              prev={"w1_direct_push": {"keys": ["W1_kr2_cohort_broken"],
                                       "attempted": True, "ok": False,
                                       "error": "RuntimeError: 上轮网络故障"}})
    out = dic._push_w1_critical(_V, _EX, "2026-09-12T05:05:09-04:00")
    assert out["attempted"] is True and out["ok"] is True
    assert len(hub.sent) == 1, "失败送达不算送达"


def test_w1_push_swallows_failure_but_keeps_hub_hygiene(tmp_path, monkeypatch):
    """推送异常不得上抛 (JSON 落盘与 exit 码是主职责), 但须显性化 error + 仍 stop()。"""
    hub = _FakeHub(push_raises=RuntimeError("dingtalk 502"))
    _arm_push(monkeypatch, tmp_path, hub)
    out = dic._push_w1_critical(_V, _EX, "2026-09-11T05:05:09-04:00")
    assert out["attempted"] is True and out["ok"] is None
    assert "RuntimeError" in out["error"] and "dingtalk 502" in out["error"]
    assert hub.stop_calls == 1, "推送失败也要关线程池, 否则 cron 进程照样挂住"


def test_w1_push_stop_failure_does_not_change_verdict(tmp_path, monkeypatch):
    """stop() 失败只 WARNING 带栈 (W4/W6 棘轮不许 except:pass), 不改 out['ok'] — 成败以 push 为准。"""
    hub = _FakeHub(ok=True, stop_raises=RuntimeError("线程池已关"))
    _arm_push(monkeypatch, tmp_path, hub)
    out = dic._push_w1_critical(_V, _EX, "2026-09-11T05:05:09-04:00")
    assert out["ok"] is True and out["error"] is None, "stop 失败不得污染推送结论"
    assert hub.stop_calls == 1


def test_w1_push_noop_without_w1_keys(tmp_path, monkeypatch):
    """只有 I/F 系列违反 (走原速率通道) ⇒ 不构造 hub、不推送 (不动别人的判据)。"""
    hub = _FakeHub(ok=True)
    _arm_push(monkeypatch, tmp_path, hub)
    out = dic._push_w1_critical({"I1_pnl_mismatch": 4, "F_field_dropout": 1}, {}, "2026-09-11T05:05:09")
    assert out == {"keys": [], "attempted": False, "ok": None, "error": None,
                   "suppressed_reason": None}
    assert hub.sent == [] and hub.stop_calls == 0


def test_w1_push_reads_prev_state_before_out_is_overwritten():
    """源级契约: 直推调用点必须在 OUT.write_text **之前** (防刷屏判据读的是上一轮的 OUT)。

    顺序写反的后果是首次告警即被自我抑制 — 读到的"上次送达记录"其实是本轮自己,
    等于把断链修成了一个更隐蔽的断链 (与"读错列恒 v2"同族的结构性假绿)。
    """
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    call = "_push_w1_critical(viol, examples, checked_at)"
    call_at = src.index(call)
    write_at = src.index("OUT.write_text(json.dumps(result")
    assert call_at < write_at, "直推必须在覆写 OUT 之前调用"
    # 只数调用点 (不能用 "_push_w1_critical(viol" — 那会连 def 行一起数到)
    assert src.count(call) == 1, "只许一处调用点 (两处=同一事实推两遍)"


def test_main_push_enabled_flag_gates_external_side_effect(tmp_path, monkeypatch):
    """push_enabled=False 时不得发外部通知, 但 W1 键集仍须落 JSON (否则产物看着像"无 W1 违反")。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert "def main(push_enabled: bool = True) -> int:" in src
    assert "if push_enabled:" in src and '"w1_direct_push": w1_push' in src
    assert "push_enabled=False" in src, "禁用原因须写在产物里, 不得静默缺键"


# ══════════ oct_cohort 判分维 (2026-10-04, OKR-2026-10 KR1, 开跑会 A1) ══════════
# 与 sub_cohort (纯观测) 的本质区别: oct_cohort **判分并告警** (W1_oct_cohort_broken)。
# 三条红线: ①起点字面钉死 (移窗=改判据面=须先落 DEB+改 OKR) ②本维 BROKEN 不得污染
# 主 cohort 判定/全窗 status (口径并列不冒充) ③not_kr2_09_verdict 防误读标记必在。
_OCT_CUR = "exit-34c7153088a1"   # OKR-2026-10 KR1 现行版本 (DEB-20260917, 用户裁+豁免在册)
_OCT_NEW = "exit-dddddddddddd"   # 假想的 10 月违规换版


def test_oct_cohort_start_is_pinned_and_documented():
    """源级契约: oct 窗起点写死 + 修订出处可审计 (改窗 = 改判据 = 须先改 OKR)。"""
    src = (PROJECT_ROOT / "scripts" / "daily_invariant_check.py").read_text(encoding="utf-8")
    assert '_OCT_COHORT_START = "2026-09-17"' in src
    assert "OKR-2026-10 KR1" in src and "W1_oct_cohort_broken" in src
    assert "不得静默移窗" in src, "移窗禁令必须写在告警文本里 (可审计)"


def test_oct_cohort_single_version_in_track_without_alarming(tmp_path, monkeypatch):
    """窗内单版本 ⇒ in_track, 不产生任何 W1 违反 (判分维的'绿'必须是无声的)。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OCT_CUR, n=5, day="2026-09-18")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_OCT_CUR,)))
    o = info["oct_cohort"]
    assert o["n_unique"] == 1 and o["status"] == "single_version"
    assert o["n_closed"] == 5 and o["okr"] == "2026-10"
    assert o["not_kr2_09_verdict"] is True
    assert [h[0] for h in hits] == []


def test_oct_cohort_second_hash_alarms_p0_key(tmp_path, monkeypatch):
    """窗内第 2 个哈希 ⇒ W1_oct_cohort_broken 当天告警, 且豁免清单**不适用** (预算 0)。

    子集关系钉死 (2026-10-04 落地时实测): oct 窗 (9/17→) ⊂ 主 cohort 窗 (9/3→, 查询无尾界),
    ⇒ 10 月任何违规换版**必然同时**触发主窗 W1_kr2_cohort_broken — 两键并报是正确语义
    (9 月判据 BROKEN 终局不改写 + 10 月判据独立告警), 不得把主键当噪音摘掉。
    """
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OCT_CUR, n=4, day="2026-09-18")
    _row_closed(db, exit_hash=_OCT_NEW, n=2, day="2026-09-25")
    # _OCT_NEW 已入豁免清单 (模拟'先豁免后换版') — 两个判分维照样各自 BROKEN
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_OCT_CUR, _OCT_NEW)))
    o = info["oct_cohort"]
    assert o["n_unique"] == 2 and o["status"] == "broken"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken", "W1_oct_cohort_broken"]
    oct_hit = next(h for h in hits if h[0] == "W1_oct_cohort_broken")
    assert "不得静默移窗" in oct_hit[2]


def test_oct_cohort_broken_does_not_mask_or_fake_main_verdicts(tmp_path, monkeypatch):
    """口径并列: oct BROKEN 不改主 cohort 判定; 主 BROKEN 也不豁免 oct 维各自告警。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OLD, n=2, day="2026-09-05")        # 主 cohort 窗第 1 版
    _row_closed(db, exit_hash=_CUR, n=2, day="2026-09-12")       # 主 cohort 窗第 2 版 → 主 BROKEN
    _row_closed(db, exit_hash=_OCT_CUR, n=3, day="2026-09-18")   # oct 窗单版本 → in_track
    info, hits = _run(tmp_path, monkeypatch, db,
                      _baseline(tmp_path, exit_versions=(_OLD, _CUR, _OCT_CUR)))
    assert info["cohort"]["status"] == "broken"
    assert info["oct_cohort"]["status"] == "single_version"
    assert [h[0] for h in hits] == ["W1_kr2_cohort_broken"], \
        "oct in_track 不得消掉主判定; oct 未破也不得多报"


def test_oct_cohort_hashless_rows_counted_not_versioned(tmp_path, monkeypatch):
    """无哈希行 (extended_hours_entry 链缺口) 只计数不当版本 — 缺失不制造假 broken。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_OCT_CUR, n=3, day="2026-09-18")
    _row_closed(db, exit_hash=None, n=4, day="2026-09-19")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_OCT_CUR,)))
    o = info["oct_cohort"]
    assert o["hash_missing_rows"] == 4
    assert o["n_unique"] == 1 and o["status"] == "single_version"
    assert hits == []


def test_oct_cohort_skipped_on_old_schema_without_hostaging(tmp_path, monkeypatch):
    """缺 exit_price 列 ⇒ oct 维 skipped_old_schema, 主 cohort 判分不得被绑架 (9/11 教训)。"""
    db = _mk_db(tmp_path)                                  # 无 exit_price 列
    _row(db, exit_hash=_NEW, n=3, day="2026-09-03")
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_NEW,)))
    assert info["oct_cohort"]["status"] == "skipped_old_schema"
    assert info["oct_cohort"]["not_kr2_09_verdict"] is True
    assert info["cohort"]["n_unique"] == 1 and info["cohort"]["status"] == "single_version"


def test_oct_cohort_no_rows_distinguishable(tmp_path, monkeypatch):
    """oct 窗零闭合行 ⇒ no_rows, 不得读成'单版本在轨' (零样本≠判据通过)。"""
    db = _mk_db_closed(tmp_path)
    _row_closed(db, exit_hash=_CUR, n=2, day="2026-09-12")   # 全在 oct 窗前
    info, hits = _run(tmp_path, monkeypatch, db, _baseline(tmp_path, exit_versions=(_CUR,)))
    assert info["oct_cohort"]["status"] == "no_rows"
    assert info["oct_cohort"]["n_closed"] == 0
    assert hits == []
