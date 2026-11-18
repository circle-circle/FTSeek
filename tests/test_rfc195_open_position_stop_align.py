"""[RFC-195] open_positions 止损对齐指令真值 (恢复路径合成 SL 的收口)。

病灶 (2026-11-09 实测 US.LLY): position_sync 在 playbook 落库前抢先建仓, 写入 ATR×mult
被 2% 硬底截断的合成 SL (1351.79 = −2%); 31s 后 reconcile 用指令真值 (1311.52 = −5%)
补录 trade_record ⇒ 同一笔仓两表两个止损。出场链只读 trade_records (无交易影响), 但
open_positions 是推送/看板/旧风控告警的取值面。

[DEB-20261117 P1] 追加 10 例, 锁死原实现遗留的三个覆盖洞 (2026-11-17 夜盘日志 + 生产库
副本 A/B 实证):
  · TP 只在原值为空时补 ⇒ BWXT 合成 114.18 (真值 110.48) / ELV 现存 493.22 (真值 475.73)
    永不修复;
  · WHERE 只判 SL 漂移 ⇒ "SL 已对齐 / TP 仍合成"的行连 UPDATE 都进不去
    (ELV 生产库副本: 旧代码 rowcount=0, 新代码 rowcount=1);
  · WHERE 用 `strategy_id LIKE 'playbook%'` 当归属判据 ⇒ 标签被抢先写成 auto_main 的行
    匹配不到 (CAT; 证据强度与已排除的 OXY 反例见对应测试 docstring)。
归属 SSOT 是 trade_records, 故新增 `playbook_owned` 由调用方在补录成功后显式声明。
刻意不改 strategy_id/owner_strategy (ADR-015 派生 ⇒ 属行为变更), 亦有钉子锁住。
"""
import sqlite3

import pytest

from app.core.position_sync import _align_open_position_stops


@pytest.fixture()
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / "pos.db"))
    c.execute(
        "CREATE TABLE open_positions ("
        " symbol TEXT PRIMARY KEY, strategy_id TEXT, initial_sl REAL, current_sl REAL,"
        " initial_tp REAL, updated_at TEXT)"
    )
    yield c
    c.close()


def test_align_overwrites_synthetic_stop(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.LLY','playbook_sim',1351.79,1351.79,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.LLY", 1311.52, 1484.09) == 1
    row = conn.execute(
        "SELECT initial_sl, current_sl, initial_tp FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row[0] == pytest.approx(1311.52)
    assert row[1] == pytest.approx(1311.52), "未收紧过的 current_sl 应跟着对齐"
    assert row[2] == pytest.approx(1484.09)


def test_align_keeps_tightened_current_sl(conn):
    """current_sl 已被 trailing/规则0.5 抬高 ⇒ 只改 initial_sl, 不得松掉活止损。"""
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.LLY','playbook_sim',1351.79,1362.42,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.LLY", 1311.52, 1484.09) == 1
    row = conn.execute(
        "SELECT initial_sl, current_sl FROM open_positions WHERE symbol='US.LLY'"
    ).fetchone()
    assert row[0] == pytest.approx(1311.52)
    assert row[1] == pytest.approx(1362.42)


def test_align_skips_manual_and_zero_stop(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.MCD','extended_hours_entry',267.61,267.61,0,'t0')"
    )
    assert _align_open_position_stops(conn, "US.MCD", 250.0, 300.0) == 0
    assert _align_open_position_stops(conn, "US.MCD", 0, 0) == 0
    row = conn.execute(
        "SELECT initial_sl FROM open_positions WHERE symbol='US.MCD'"
    ).fetchone()
    assert row[0] == pytest.approx(267.61), "非 playbook 仓位不得被改写"


def test_align_noop_when_already_equal(conn):
    conn.execute(
        "INSERT INTO open_positions VALUES ('US.XOM','playbook_sim',156.94,156.94,177.65,'t0')"
    )
    assert _align_open_position_stops(conn, "US.XOM", 156.94, 177.65) == 0


# ---------------------------------------------------------------------------
# [DEB-20261117 P1] 两个"永久卡死"覆盖洞的回归钉子 (11-17 实盘实证)
# ---------------------------------------------------------------------------


def _row(conn, symbol):
    return conn.execute(
        "SELECT initial_sl, current_sl, initial_tp, strategy_id FROM open_positions "
        "WHERE symbol=?", (symbol,)).fetchone()


def test_align_overwrites_synthetic_take_profit(conn):
    """洞①: 发现路径已写固定 5% 合成 TP ⇒ 真值必须覆盖, 不能"只补空"。

    实证 US.BWXT 2026-11-17 (日志 22:50:38): 发现路径合成 SL=$133.25 (10.9%) /
    TP=$114.18 (cost 120.19 × 0.95), 指令真值 SL=127.03 / TP=110.48。
    量纲: 绝对价格 (USD), 非 bps/非百分比。
    """
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.BWXT','playbook_sim',133.25,133.25,114.18)"
    )
    assert _align_open_position_stops(conn, "US.BWXT", 127.03, 110.48) == 1
    sl, _, tp, _ = _row(conn, "US.BWXT")
    assert sl == pytest.approx(127.03)
    assert tp == pytest.approx(110.48)


def test_align_fixes_tp_even_when_sl_already_aligned(conn):
    """洞③: SL 已对齐的行也必须能进 UPDATE — 否则洞① 的修复被旧 WHERE 吃掉。

    这是 US.ELV 2026-11-17 的真实形态 (生产库副本 A/B 实证): reconcile 22:51:40 先把
    SL 对齐到 420.28 (日志"行数=1"), TP 却因 WHERE 只判 SL 漂移而永久留在 493.22;
    旧代码复跑 rowcount=0, 新代码 rowcount=1 且 TP→475.73。
    """
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.ELV','playbook_sim',420.28,420.28,493.22)"
    )
    assert _align_open_position_stops(conn, "US.ELV", 420.28, 475.73) == 1
    sl, cur_sl, tp, _ = _row(conn, "US.ELV")
    assert tp == pytest.approx(475.73)
    assert sl == pytest.approx(420.28), "SL 已同源, 不得被改写"
    assert cur_sl == pytest.approx(420.28)


def test_align_keeps_tightened_current_sl_on_tp_only_fix(conn):
    """TP 单腿修复不得顺带松掉已被 trailing 收紧的活止损 (CAS 语义仍生效)。"""
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.BA','playbook_sim',160.10,166.80,175.00)"
    )
    assert _align_open_position_stops(conn, "US.BA", 160.10, 181.55) == 1
    sl, cur_sl, tp, _ = _row(conn, "US.BA")
    assert cur_sl == pytest.approx(166.80), "已收紧的 current_sl 必须原样保留"
    assert sl == pytest.approx(160.10)
    assert tp == pytest.approx(181.55)


def test_align_does_not_invent_tp_when_truth_missing(conn):
    """指令真值 tp=0 (缺失) ⇒ 不得把已有 TP 抹成 0, 也不得凭空造一个。"""
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.CAT','playbook_sim',570.00,570.00,660.00)"
    )
    assert _align_open_position_stops(conn, "US.CAT", 588.50, 0) == 1
    sl, _, tp, _ = _row(conn, "US.CAT")
    assert sl == pytest.approx(588.50)
    assert tp == pytest.approx(660.00), "真值缺失时保留原 TP, 禁止抹零"


def test_align_tolerates_sub_cent_drift(conn):
    """量纲钉子: 0.005 是**绝对价格**容差 (USD), 不是 bps — 半分钱以内的差不算漂移。"""
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.UBER','playbook_sim',66.100,66.100,62.000)"
    )
    assert _align_open_position_stops(conn, "US.UBER", 66.104, 62.003) == 0
    sl, _, tp, _ = _row(conn, "US.UBER")
    assert sl == pytest.approx(66.100)
    assert tp == pytest.approx(62.000)


def test_align_covers_mislabeled_row_when_ownership_proven(conn):
    """洞②: 标签被抢先写成 auto_main 的行, 在归属已被补录证明后必须能对齐。

    实证 US.CAT 2026-11-17: 发现路径 22:51:09 合成 SL=$642.62 / TP=$549.20
    (cost 578.10 + ATR 32.26×2 / ×0.95), 指令真值 609.86 / 530.38; 22:51:40 补录成功
    (日志 id=1913, sl=$609.86) 却**没有**对齐日志行 (BWXT/ELV 同周期都在 1~7ms 内打出)。
    ⚠️ 证据强度: 该行 `strategy_id=auto_main` 是会话内直接观测, 行已于 23:24:53 平仓清理
    (`Ghost position: US.CAT (DB only, closing)`) ⇒ 现库不可复核。旁证 = 上述"补录成功但
    无对齐行", 以及 `position_sync.py:2342-2353` 既有注释已把"旧逻辑标 auto_main →
    主 ExitMonitor 接管 → 双重管理"登记为历史实锤失效模式。
    反例已排除: 现存 US.OXY 的 `extended_hours_entry` 是正当标签 (盘后入场、
    `playbook_instruction_id=None`、两表 SL 均 55.77 零分叉), 不构成误标实例。
    """
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.CAT','auto_main',642.62,642.62,549.195)"
    )
    assert _align_open_position_stops(
        conn, "US.CAT", 609.86, 530.38, playbook_owned=True) == 1
    sl, cur_sl, tp, _ = _row(conn, "US.CAT")
    assert sl == pytest.approx(609.86)
    assert cur_sl == pytest.approx(609.86)
    assert tp == pytest.approx(530.38)


def test_align_still_refuses_mislabeled_row_by_default(conn):
    """放宽是**显式选择**: 不传 playbook_owned 时, 非 playbook 标签行一律不动。"""
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.MCD','auto_main',267.61,267.61,300.00)"
    )
    assert _align_open_position_stops(conn, "US.MCD", 250.00, 290.00) == 0
    sl, _, tp, _ = _row(conn, "US.MCD")
    assert sl == pytest.approx(267.61)
    assert tp == pytest.approx(300.00)


def test_align_does_not_relabel_strategy_id(conn):
    """刻意不做: 不回写 strategy_id/owner_strategy。

    改标签会经 ADR-015 的 owner_strategy → source_type 派生影响 order_executor 放行判定,
    属交易行为变更, 不允许夹带进测量完整性批次。
    """
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.CAT','auto_main',642.62,642.62,549.195)"
    )
    _align_open_position_stops(conn, "US.CAT", 609.86, 530.38, playbook_owned=True)
    assert _row(conn, "US.CAT")[3] == "auto_main"


def test_legit_non_playbook_label_is_not_a_mislabel(conn):
    """反例钉子 (2026-11-17 现存 US.OXY): 非 playbook 标签**不必然**是误标。

    OXY 走盘后入场 (`entry_price_source='ext_broker_deal_confirm'`)、
    `playbook_instruction_id=None` ⇒ 没有指令真值可对, 且两表 SL 均为 55.77 零分叉。
    归属放宽只能由"刚补录成功"的调用方显式声明, 绝不能因为标签看着不像 playbook 就动它。
    """
    conn.execute(
        "INSERT INTO open_positions (symbol, strategy_id, initial_sl, current_sl, initial_tp) "
        "VALUES ('US.OXY','extended_hours_entry',55.77,55.77,NULL)"
    )
    # 默认模式: 标签不匹配 ⇒ 一行不动
    assert _align_open_position_stops(conn, "US.OXY", 55.77, 62.00) == 0
    # 即便调用方错误地声明归属, 零漂移的 SL 也不该被改写; TP 从 NULL 补成真值
    assert _align_open_position_stops(conn, "US.OXY", 55.77, 62.00,
                                      playbook_owned=True) == 1
    sl, _, tp, label = _row(conn, "US.OXY")
    assert sl == pytest.approx(55.77), "已同源的 SL 不得被改写"
    assert tp == pytest.approx(62.00)
    assert label == "extended_hours_entry"


def test_reconcile_call_site_proves_ownership():
    """调用点钉子: reconcile 补录分支必须传 playbook_owned=True。

    洞② 的修复只有在这个调用点生效才算落地 — 助手放宽了但调用方不传, 等于没修。
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "app" / "core" / "position_sync.py").read_text()
    assert "float(row['take_profit'] or 0), playbook_owned=True)" in src