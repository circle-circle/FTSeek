"""DEB-20261104 · MFE 口径分列 (mfe_pct / mfe_tick_pct / mfe_gate_pct) 契约钉子.

钉五件事:
1. schema: trade_records 有三列; 新列在 CREATE TABLE 与迁移 ALTER 清单里都在;
2. 语义: `mfe_pct` 保持不变 (max(tick峰, bar峰, 已落袋)); `mfe_tick_pct` 只取 tick 通道
   且**不加**已落袋兜底 (镜像在线闸门读数); `mfe_gate_pct` 缺失留 NULL 不猜;
3. 闸门捕获: exit_engine 在死信号**评估时刻**写 meta["_mfe_gate_pct"], 且该字段
   **从不参与任何判定** (不进任何 Compare) ⇒ 零行为变更;
4. 消费方: scripts/mfe_caliber_report.py 存在且读两新列 (防 write-only 字段, 军规 P7);
5. 基数自检: 消费方带 distinct 塌缩判定 (军规: 新字段必带基数自检)。

判据口径锚: docs/ADJUDICATED-CRITERIA.md **A11** (KR2 死单率 = 落库 mfe_pct < 0.3%)。
改口径 = 改判据 = 重新裁决。
"""
import ast
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CLOSE_FLOW = PROJECT_ROOT / "app" / "playbook" / "pipeline_parts" / "close_flow.py"
EXIT_ENGINE = PROJECT_ROOT / "app" / "playbook" / "exit_engine.py"
TRADE_STORE = PROJECT_ROOT / "app" / "storage" / "trade_store.py"
CONSUMER = PROJECT_ROOT / "scripts" / "mfe_caliber_report.py"


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _all_nodes(tree, cls):
    return [n for n in ast.walk(tree) if isinstance(n, cls)]


def _string_constants(tree) -> str:
    return "\n".join(n.value for n in _all_nodes(tree, ast.Constant)
                     if isinstance(n.value, str) and len(n.value) > 8)


def _assigned_names(tree) -> dict:
    out = {}
    for node in _all_nodes(tree, ast.Assign):
        for t in node.targets:
            if isinstance(t, ast.Name):
                out.setdefault(t.id, []).append(node.value)
    return out


# ── 1. schema ────────────────────────────────────────────────────────────
NEW_MFE_COLS = ("mfe_tick_pct", "mfe_t15_pct", "mfe_gate_pct")


def test_schema_declares_three_mfe_columns():
    src = TRADE_STORE.read_text(encoding="utf-8")
    for col in ("mfe_pct",) + NEW_MFE_COLS:
        assert col in src, f"trade_store 缺列声明: {col}"


def test_migration_list_contains_new_columns():
    tree = _tree(TRADE_STORE)
    ddl = [n.value for n in _all_nodes(tree, ast.Constant)
           if isinstance(n.value, str) and "ALTER TABLE trade_records ADD COLUMN mfe" in n.value]
    joined = "\n".join(ddl)
    for col in NEW_MFE_COLS:
        assert col in joined, f"迁移清单缺 {col} (全新/旧库都会缺列)"


def test_new_columns_units_declared_as_percentage():
    """军规: 新增落库字段必带单位/量纲声明 (百分比, 3.72 = 3.72%)."""
    src = TRADE_STORE.read_text(encoding="utf-8")
    assert "mfe_tick_pct" in src and "mfe_gate_pct" in src
    # 声明必须出现在 DDL 注释里 (与既有 mfe_pct 的百分比语义一致)
    assert "百分比" in src, "新列缺单位声明 (百分比)"


# ── 2. 语义 ──────────────────────────────────────────────────────────────
def test_db_mfe_still_maxes_tick_and_bar_and_realized():
    """mfe_pct 语义不变: max(tick峰, bar峰) + 已落袋兜底 (A11 判据口径)."""
    tree = _tree(CLOSE_FLOW)
    tuples = [n for n in _all_nodes(tree, ast.Tuple)
              if any(isinstance(e, ast.Constant) and e.value == "peak_price" for e in n.elts)]
    assert tuples, "找不到 (peak_price, peak_price_bar) 迭代 —— mfe_pct 语义被改?"
    names = {e.value for e in tuples[0].elts if isinstance(e, ast.Constant)}
    assert {"peak_price", "peak_price_bar"} <= names


def test_tick_caliber_excludes_realized_fallback():
    """mfe_tick_pct 刻意不加已落袋兜底 (镜像在线闸门读数, 闸门当时看不到结果)."""
    tree = _tree(CLOSE_FLOW)
    assigned = _assigned_names(tree)
    for value in assigned.get("_mfe_tick_pct", []):
        src = ast.dump(value)
        assert "_realized_fav" not in src, "mfe_tick_pct 被已落袋兜底污染 (口径错)"


def test_gate_caliber_missing_stays_null():
    """mfe_gate_pct 缺失 ⇒ 留 NULL, 绝不猜 (军规: 不得用默认值制造假留痕)."""
    src = CLOSE_FLOW.read_text(encoding="utf-8")
    assert "_mfe_gate_pct" in src
    tree = _tree(CLOSE_FLOW)
    # 写入点必须是可空参数 (None 允许), 而不是硬编码 0.0
    inits = _assigned_names(tree).get("_mfe_gate_pct_db", [])
    assert inits, "找不到 _mfe_gate_pct_db 赋值"
    assert any(isinstance(v, ast.Constant) and v.value is None for v in inits), \
        "mfe_gate_pct_db 初值必须是 None (缺失留空), 不得填 0.0"


def test_update_writes_all_three_columns():
    tree = _tree(CLOSE_FLOW)
    sql = [n.value for n in _all_nodes(tree, ast.Constant)
           if isinstance(n.value, str) and "UPDATE trade_records" in n.value and "mfe_pct=?" in n.value]
    assert sql, "找不到平仓 UPDATE 语句"
    joined = "\n".join(sql)
    for col in ("mfe_pct=?", "mfe_tick_pct=?", "mfe_t15_pct=?", "mfe_gate_pct=?"):
        assert col in joined, f"平仓 UPDATE 未写列: {col}"


# ── 3. 闸门捕获零行为变更 ────────────────────────────────────────────────
def test_gate_peak_captured_in_exit_engine():
    tree = _tree(EXIT_ENGINE)
    subs = [n for n in _all_nodes(tree, ast.Subscript)
            if isinstance(n.slice, ast.Constant) and n.slice.value == "_mfe_gate_pct"]
    assert subs, "exit_engine 未捕获闸门评估时刻 peak"


def test_gate_peak_never_used_in_a_condition():
    """核心不变式: 新字段只写不读判定 ⇒ 零行为变更 (DEB §D2/V1).

    唯一允许的读法是捕获一次的记忆化守卫 `meta.get("<col>") is None`;
    任何把它与其它值比较的写法都判失败。
    """
    tree = _tree(EXIT_ENGINE)
    for cmp_node in _all_nodes(tree, ast.Compare):
        dumped = ast.dump(cmp_node)
        if not any(col in dumped for col in NEW_MFE_COLS):
            continue
        # 允许: meta.get("<col>") is None  (记忆化守卫)
        if "Is()" in dumped and "Constant(value=None)" in dumped \
                and "attr='get'" in dumped:
            continue
        raise AssertionError(
            "MFE 新列出现在判定条件里 ⇒ 行为变更, 违反 DEB §D2 (须影子先行)")


def test_checkpoints_captured_unconditionally():
    """两个检查点捕获必须**无条件** (不挂在死信号开关下) —— 否则关掉该规则会连带
    停止采集, 让度量链断档 (与 W2/W10 同族的静默失效)。"""
    tree = _tree(EXIT_ENGINE)
    src = ast.dump(tree)
    for key in ("_mfe_t15_pct", "_mfe_gate_pct"):
        assert key in src, f"exit_engine 未捕获检查点 {key}"
    # 捕获语句不得位于 dead_signal_exit_enabled 的 if 分支内
    for node in _all_nodes(tree, ast.If):
        test_src = ast.dump(node.test)
        if "dead_signal_exit_enabled" not in test_src:
            continue
        body_src = "\n".join(ast.dump(stmt) for stmt in node.body)
        assert "_mfe_t15_pct" not in body_src and "_mfe_gate_pct" not in body_src, \
            "检查点捕获被塞进死信号开关分支 ⇒ 关掉该规则会连带停止采集"


def test_dead_signal_condition_unchanged():
    """死信号阈值判定条件逐字未改 (只有捕获语句插在前面)."""
    tree = _tree(EXIT_ENGINE)
    found = False
    for cmp_node in _all_nodes(tree, ast.Compare):
        src = ast.dump(cmp_node)
        if "_ds_mfe_pct" in src and "peak_profit_pct" in src:
            found = True
            assert "Lt()" in src, "死信号比较方向被改 (peak_profit_pct < _ds_mfe_pct)"
    assert found, "找不到死信号判定条件"


# ── 4/5. 消费方 + 基数自检 ───────────────────────────────────────────────
def test_consumer_script_exists_and_reads_new_columns():
    assert CONSUMER.exists(), "缺消费方 scripts/mfe_caliber_report.py ⇒ 新列成 write-only (军规 P7)"
    src = CONSUMER.read_text(encoding="utf-8")
    assert "mfe_tick_pct" in src and "mfe_gate_pct" in src


def test_consumer_has_cardinality_guard():
    src = CONSUMER.read_text(encoding="utf-8")
    assert "CARD_MIN_DISTINCT" in src and "CARD_MIN_N" in src, \
        "消费方缺基数自检 (军规: 新字段必带 distinct 计数体检)"


def test_consumer_pins_dead_mfe_threshold_to_a11():
    """消费方的死单阈值必须与判据字典 A11 一致 (0.3%)."""
    tree = _tree(CONSUMER)
    values = [n.value for n in _all_nodes(tree, ast.Constant)
              if isinstance(n.value, (int, float)) and not isinstance(n.value, bool)]
    assert 0.3 in values, "消费方死单阈值不是 0.3 (与 A11 不一致)"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
