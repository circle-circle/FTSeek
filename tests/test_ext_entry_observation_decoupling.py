#!/usr/bin/env python3
"""2026-09-23 复查批的回归测试 (印记防回退)。

覆盖三件在本轮"全链路复查"中发现并修掉的缺陷:
  ① 观测被涨幅带截断: 判据②评估必须**先于**带/量闸 (否则对照台账样本失真)
  ② 读数口径漂移: `prefilter_verdicts.passes_current_gate` 必须跟生产闸 (带) 一致
  ③ P1 类级治本: `_probe_mode()` 必须在下单口**短路** (探针/测试永不触真下单)
"""
import ast
import importlib.util
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
SRC = (ROOT / "scripts" / "extended_hours_entry_executor.py").read_text()


def _load():
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    spec = importlib.util.spec_from_file_location("ext_entry_exec_review", ROOT / "scripts" / "extended_hours_entry_executor.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


E = _load()


def _code_without_docstring(src):
    tree = ast.parse(src)
    first = tree.body[0] if tree.body else None
    if isinstance(first, ast.Expr) and isinstance(getattr(first, "value", None), ast.Constant) \
            and isinstance(first.value.value, str):
        src = src.replace(first.value.value, "", 1)
    return "\n".join(line for line in src.splitlines() if not line.strip().startswith("#"))


CODE = _code_without_docstring(SRC)


# ── ① 观测与决策解耦 ────────────────────────────────────────────────────
def test_earnings_eval_precedes_band_gate():
    """判据②评估在带/量闸之前 ⇒ 带外票也进对照台账 (样本不被带截断)。"""
    calls = [i for i, line in enumerate(SRC.splitlines()) if "earnings, earnings_reason = _earnings_surprise(" in line]
    # 闸判定行按语义匹配 (2026-09-23 RFC-099: 决策 pass 的闸带方向前缀
    #   `if (_direction == "LONG" and not _chg_ok) or not _vol_ok:` ⇒ 字面量匹配会漏), 两处都必须钉住。
    gates = [i for i, line in enumerate(SRC.splitlines())
             if line.strip().startswith("if ") and "_chg_ok" in line and "_vol_ok" in line]
    assert len(calls) == 2 and len(gates) == 2, (len(calls), len(gates))
    for call, gate in zip(calls, gates):
        assert call < gate, "判据②评估必须早于带/量闸"


def test_gate_count_semantics_unchanged():
    """带/量不达标仍计 momentum_volume (计数语义不变, 看板/测试解析不受影响)。"""
    hits = [i for i, line in enumerate(SRC.splitlines())
            if line.strip().startswith("if ") and "_chg_ok" in line and "_vol_ok" in line]
    assert hits, "闸判定行缺失"
    for idx in hits:                      # 两处 (观测 pass + 决策 pass) 计数语义都必须不变
        assert 'counts["momentum_volume"]' in SRC.splitlines()[idx + 1], SRC.splitlines()[idx]


def test_shadow_row_carries_entry_context(tmp_path, monkeypatch):
    """对照台账行必须带入场侧上下文 (chg/volume/in_band/vol_ok) — 否则两变量不可分离评估。

    走真实通路: collect() 写 `_ENTRY_CTX_OBS` → 判据② → 台账 (不靠传参, 因签名不能动)。
    """
    path = tmp_path / "shadow.jsonl"
    monkeypatch.setattr(E, "EARNINGS_CRITERION_SHADOW_PATH", path)
    monkeypatch.setattr(E, "_EARNINGS_CRITERION_SHADOW_KEYS", set())
    monkeypatch.setattr(E, "_ENTRY_CTX_OBS", {("US.XYZ", "premarket"): {
        "change_pct": 3.4, "volume": 250000, "in_band": False, "vol_ok": True}})
    E._record_earnings_criterion_shadow(
        "US.XYZ", "premarket", "XYZ", (None, "surprise_stale"),
        ({"surprise_pct": 12.0, "actual": 1.0, "estimate": 0.9}, None),
        extra=E._ENTRY_CTX_OBS.get(("US.XYZ", "premarket")),
    )
    row = path.read_text().strip().splitlines()[0]
    import json
    obj = json.loads(row)
    assert obj["legacy_blocked_new_pass"] is True
    assert obj["in_band"] is False and obj["change_pct"] == 3.4 and obj["vol_ok"] is True


# ── ② 读数口径与生产一致 ────────────────────────────────────────────────
def test_prefilter_verdicts_follows_band(monkeypatch):
    """带生效时 passes_current_gate 必须按带判 (旧阈 ≥2% 会把带内温和票误判 false)。"""
    monkeypatch.setattr(E, "ENTRY_CHG_BAND", "0.5,2.0")
    # 带内温和票 (1.2%) — 旧口径会判 False
    assert E.prefilter_verdicts(1.2, 45.0, 300000)["passes_current_gate"] is True
    # 带外 pop 票 (3.4%) — 旧口径会判 True
    assert E.prefilter_verdicts(3.4, 45.0, 300000)["passes_current_gate"] is False


def test_prefilter_verdicts_falls_back_without_band(monkeypatch):
    monkeypatch.setattr(E, "ENTRY_CHG_BAND", "")
    assert E.prefilter_verdicts(1.2, 45.0, 300000)["passes_current_gate"] is False
    assert E.prefilter_verdicts(3.4, 45.0, 300000)["passes_current_gate"] is True


# ── ③ 探针/测试模式短路下单口 ───────────────────────────────────────────
def test_probe_mode_short_circuits_order_port():
    """`_probe_mode()` 判定必须出现在 place_order 之前 (生产 unset ⇒ 零行为变更)。"""
    lines = SRC.splitlines()
    probe_idx = [i for i, l in enumerate(lines) if "if _probe_mode():" in l]
    order_idx = [i for i, l in enumerate(lines) if "tctx.place_order(" in l]
    assert probe_idx and order_idx, (probe_idx, order_idx)
    assert min(probe_idx) < min(order_idx)
    assert '"skip": "probe_mode"' in SRC


def test_probe_mode_helper_is_env_driven():
    assert "FUTSEEK_PROBE" in SRC.split("def _probe_mode")[1].split("def ", 1)[0]