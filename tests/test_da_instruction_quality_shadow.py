"""DA 指令质量日报告影子 + 评估器契约 (record-only, 2026-11-13)。

钉住:
  · 开关默认 false; 关闭时只读开关本身 —— 零数据文件读、零写、零目录创建。
  · `_llm_output` 卡片复用 `da_model_ab_replay.score()/day_bars()`; direction 缺失回退 action。
  · SKIP 带执行字段 → direction_plan_inconsistent + skip_inconsistent 可见。
  · 无本地 bars 时 reachable/hit/fwd 必须为 null, **不伪造价格读数**。
  · 内容哈希幂等: 同内容重跑不新增历史、不覆盖旧历史。
  · 汇总 (valid/research/consistency/geometry/skip/direction_counts/per-symbol reasons)。
  · 评估器 OK/WARN/NO_DATA/ERROR + 一致率/几何率/年龄; WARN 仅报告态 (退出码 0)。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import scripts.da_instruction_quality_shadow as shadow
import scripts.da_instruction_quality_shadow_eval as evaluator
from scripts import da_model_ab_replay as replay
from scripts import shadow_runtime

DAY = "2026-11-13"
ET = ZoneInfo("America/New_York")


# ── fixtures ────────────────────────────────────────────────────────────────

def _research_fields() -> dict:
    return {
        "executive_summary": "summary",
        "causal_chain": "cause -> effect",
        "bull_case": [{"thesis": "bull"}],
        "bear_case": [{"thesis": "bear"}],
    }


def buy_card(*, entry: float = 100.0, stop: float = 95.0, tp: float = 110.0) -> dict:
    return {
        "direction": "BUY",
        **_research_fields(),
        "execution_plan": {
            "entry_price": entry,
            "stop_loss_price": stop,
            "take_profit_price": tp,
        },
    }


def skip_card(*, with_execution: bool = False) -> dict:
    plan = {
        "entry_price": None,
        "entry_archetype": None,
        "stop_loss": None,
        "stop_loss_price": None,
        "take_profit": None,
        "take_profit_price": None,
        "chase_cap_price": None,
        "confirmation": {"price_level": None},
    }
    if with_execution:
        plan["entry_price"] = 100.0
    return {
        "direction": "SKIP",
        **_research_fields(),
        "skip_reason": "event window",
        "execution_plan": plan,
    }


def write_checkpoint(root: Path, symbol: str, card, *, day: str = DAY,
                     outer_extra: dict | None = None,
                     raw_text: str | None = None) -> Path:
    directory = root / day
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{symbol}.json"
    if raw_text is not None:
        path.write_text(raw_text, encoding="utf-8")
        return path
    outer = {"symbol": symbol, "_llm_output": card}
    outer.update(outer_extra or {})
    path.write_text(json.dumps(outer, ensure_ascii=False), encoding="utf-8")
    return path


def run_shadow(ckpt: Path, out: Path, *extra: str) -> int:
    return shadow.main([
        "--date", DAY,
        "--checkpoint-root", str(ckpt),
        "--out-root", str(out),
        *extra,
    ])


def latest_json(out: Path) -> dict:
    return json.loads((out / "latest.json").read_text(encoding="utf-8"))


# ── 1. 关闭路径 ─────────────────────────────────────────────────────────────

def test_disabled_default_false(tmp_path, monkeypatch):
    """未设置开关 (且环境文件无该键) 时默认 false。"""
    monkeypatch.delenv(shadow.ENV_FLAG, raising=False)
    runtime = shadow._shadow_runtime()
    empty_env = tmp_path / "missing.env"

    class _Proxy:
        def env_flag(self, name):
            return runtime.env_flag(name, env_file=empty_env)

        def __getattr__(self, item):
            return getattr(runtime, item)

    monkeypatch.setattr(shadow, "_shadow_runtime", lambda: _Proxy())
    assert shadow.env_enabled() is False


def test_disabled_reads_no_files_and_writes_nothing(tmp_path, monkeypatch, capsys):
    """开关关闭 ⇒ DISABLED / return 0 / 零数据读、零写、零目录创建。"""
    monkeypatch.setenv(shadow.ENV_FLAG, "0")
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    out = tmp_path / "out"

    def _trip(*args, **kwargs):
        raise AssertionError("disabled path touched the filesystem")

    import builtins
    monkeypatch.setattr(builtins, "open", _trip)
    for name in ("open", "read_text", "write_text", "write_bytes", "mkdir", "exists"):
        monkeypatch.setattr(Path, name, _trip)

    code = run_shadow(ckpt, out)

    assert code == 0
    assert "DISABLED" in capsys.readouterr().out
    import os
    assert not os.path.exists(out)
    assert os.path.exists(ckpt / DAY / "US.AAA.json")   # 输入没被改写


def test_main_json_output_is_machine_parseable(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.ENV_FLAG, "1")
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    out = tmp_path / "out"
    assert shadow.main([
        "--date", DAY, "--checkpoint-root", str(ckpt),
        "--out-root", str(out), "--json",
    ]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["schema"] == shadow.SCHEMA
    assert payload["metrics"]["total"] == 1


# ── 2. 打分为不一致 / 回退 action ───────────────────────────────────────────

def test_skip_with_execution_fields_is_flagged_inconsistent(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.SKIPBAD", skip_card(with_execution=True))

    body = shadow.build_report(DAY, ckpt)
    metrics = body["metrics"]

    assert metrics["total"] == 1
    assert metrics["valid_card"] == 1
    assert metrics["skip_count"] == 1
    assert metrics["skip_inconsistent"] == 1
    assert metrics["direction_plan_consistent"] == 0
    assert metrics["direction_plan_inconsistent"] == 1
    assert metrics["direction_plan_consistent_rate"] == 0.0
    row = body["symbols"][0]
    assert row["direction"] == "SKIP"
    assert row["direction_plan_consistent"] is False
    assert "direction_plan_inconsistent" in row["reasons"]
    assert row["skip_reason"] == "event window"


def test_direction_falls_back_to_action(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    # 卡片内 action 回退
    card = buy_card()
    card.pop("direction")
    card["action"] = "BUY"
    write_checkpoint(ckpt, "US.INNER", card)
    # 外层 checkpoint action 回退
    write_checkpoint(ckpt, "US.OUTER", {}, outer_extra={"action": "SKIP"})

    body = shadow.build_report(DAY, ckpt)
    rows = {row["symbol"]: row for row in body["symbols"]}

    assert rows["US.INNER"]["direction"] == "BUY"
    assert rows["US.INNER"]["valid_card"] is True
    assert rows["US.OUTER"]["direction"] == "SKIP"
    assert rows["US.OUTER"]["valid_card"] is True
    assert body["metrics"]["direction_counts"] == {"BUY": 1, "SKIP": 1}


# ── 3. 无 bars 不伪造价格指标 ──────────────────────────────────────────────

def test_missing_bars_never_fabricates_price_metrics(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.NOBARS", buy_card())

    body = shadow.build_report(DAY, ckpt)
    metrics = body["metrics"]
    row = body["symbols"][0]

    assert row["bars_available"] is False
    assert row["reachable"] is None
    assert row["hit"] is None
    assert row["fwd_pct"] is None
    assert "missing_bars" in row["reasons"]
    # 几何判据只依赖卡片, 与 bars 无关
    assert row["price_geometry_valid"] is True
    assert row["rr"] == 2.0
    assert metrics["bars_missing"] == 1
    assert metrics["price_metrics_available"] == 0
    assert metrics["reachable_count"] == 0
    assert metrics["hit_count"] == 0
    assert metrics["hit_rate_reachable"] is None


def test_present_bars_compute_reachability_without_fabrication(tmp_path, monkeypatch):
    bars_dir = tmp_path / "bars"
    bars_dir.mkdir()
    (bars_dir / f"US_REACH_{DAY}.json").write_text(
        json.dumps([[0, 100.0, 110.0, 95.0, 108.0, 0]]), encoding="utf-8")
    monkeypatch.setattr(replay, "BARS_DIRS", (bars_dir,))
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.REACH", buy_card())

    body = shadow.build_report(DAY, ckpt)
    row = body["symbols"][0]

    assert row["bars_available"] is True
    assert row["reachable"] is True
    assert row["hit"] is True
    assert row["fwd_pct"] == 8.0
    assert body["metrics"]["hit_rate_reachable"] == 1.0


# ── 4. 汇总 + per-symbol reasons ───────────────────────────────────────────

def test_aggregation_counts_and_per_symbol_reasons(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    write_checkpoint(ckpt, "US.BBB", skip_card())
    write_checkpoint(ckpt, "US.CCC", skip_card(with_execution=True))
    write_checkpoint(ckpt, "US.DDD", {"direction": "HOLD", **_research_fields()})
    short = {
        "direction": "SELL_SHORT",
        **_research_fields(),
        "execution_plan": {
            "entry_price": 100.0, "stop_loss_price": 110.0, "take_profit_price": 90.0,
        },
    }
    write_checkpoint(ckpt, "US.EEE", short)

    metrics = shadow.build_report(DAY, ckpt)["metrics"]

    assert metrics["total"] == 5
    assert metrics["valid_card"] == 4
    assert metrics["valid_card_rate"] == 0.8
    assert metrics["research_complete"] == 4
    assert metrics["research_complete_rate"] == 1.0
    assert metrics["direction_plan_consistent"] == 3
    assert metrics["direction_plan_inconsistent"] == 1
    assert metrics["direction_plan_consistent_rate"] == 0.75
    assert metrics["directional_count"] == 2
    assert metrics["price_geometry_valid"] == 2
    assert metrics["price_geometry_rate"] == 1.0
    assert metrics["skip_count"] == 2
    assert metrics["skip_inconsistent"] == 1
    assert metrics["direction_counts"] == {"BUY": 1, "HOLD": 1, "SELL_SHORT": 1, "SKIP": 2}
    assert metrics["bars_missing"] == 5

    rows = {row["symbol"]: row for row in shadow.build_report(DAY, ckpt)["symbols"]}
    assert rows["US.AAA"]["reasons"] == ["missing_bars"]
    assert rows["US.BBB"]["reasons"] == ["skip", "missing_bars"]
    assert rows["US.CCC"]["reasons"] == [
        "direction_plan_inconsistent", "skip", "missing_bars"]
    assert rows["US.DDD"]["reasons"] == ["invalid_direction", "missing_bars"]
    assert rows["US.DDD"]["valid_card"] is False
    assert rows["US.EEE"]["reasons"] == ["missing_bars"]


def test_price_geometry_invalid_is_counted(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.BADGEOM", buy_card(stop=105.0, tp=90.0))

    body = shadow.build_report(DAY, ckpt)
    row = body["symbols"][0]
    metrics = body["metrics"]

    assert row["price_geometry_valid"] is False
    assert "price_geometry_invalid" in row["reasons"]
    assert metrics["price_geometry_invalid"] == 1
    assert metrics["price_geometry_unknown"] == 0
    assert metrics["price_geometry_rate"] == 0.0


# ── 5. 幂等 + 不可变历史 ───────────────────────────────────────────────────

def test_idempotent_content_hash_and_immutable_history(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    out = tmp_path / "out"

    first = shadow.write_report(shadow.build_report(DAY, ckpt), out)
    artifact = Path(first["artifact_path"])
    original = artifact.read_bytes()
    assert first["artifact_written"] is True

    second = shadow.write_report(shadow.build_report(DAY, ckpt), out)

    assert second["content_hash"] == first["content_hash"]
    assert second["artifact_path"] == first["artifact_path"]
    assert second["artifact_written"] is False
    assert second["latest_written"] is False
    assert artifact.read_bytes() == original
    assert len(list((out / DAY).glob("*.json"))) == 1
    assert not list(out.glob(".latest.json.*"))

    latest = latest_json(out)
    assert latest["content_hash"] == first["content_hash"]
    assert latest["artifact"] == f"{DAY}/{first['content_hash']}.json"

    # 内容真的变了 ⇒ 新哈希、新历史、旧历史不动
    write_checkpoint(ckpt, "US.AAA", skip_card())
    third = shadow.write_report(shadow.build_report(DAY, ckpt), out)
    assert third["content_hash"] != first["content_hash"]
    assert len(list((out / DAY).glob("*.json"))) == 2
    assert artifact.read_bytes() == original


def test_raw_and_bak_files_are_excluded(tmp_path, monkeypatch):
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    (ckpt / DAY / "US.AAA.raw.json").write_text("{}", encoding="utf-8")
    (ckpt / DAY / "US.AAA.raw.json.bak").write_text("{}", encoding="utf-8")
    (ckpt / DAY / "US.BBB.bak").write_text("{}", encoding="utf-8")

    body = shadow.build_report(DAY, ckpt)

    assert body["source_files"] == ["US.AAA.json"]
    assert body["metrics"]["total"] == 1


# ── 6. 评估器 ──────────────────────────────────────────────────────────────

def _write_eval_payload(tmp_path: Path, payload: dict, name: str = "report.json") -> Path:
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def _payload(*, consistency: float = 1.0, geometry: float = 1.0,
             total: int = 4, valid: int = 4, directional: int = 2,
             generated_at: str | None = None, day: str = DAY) -> dict:
    return {
        "schema": shadow.SCHEMA,
        "date": day,
        "generated_at": generated_at or datetime.now(ET).isoformat(timespec="seconds"),
        "content_hash": "abc123",
        "metrics": {
            "total": total,
            "valid_card": valid,
            "directional_count": directional,
            "skip_count": 1,
            "skip_inconsistent": 0,
            "direction_plan_consistent": int(round(consistency * valid)),
            "direction_plan_consistent_rate": consistency,
            "price_geometry_valid": int(round(geometry * directional)),
            "price_geometry_rate": geometry,
        },
    }


def test_evaluator_ok_and_reads_rates_and_age(tmp_path):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=ET)
    payload = _payload(generated_at=(now - timedelta(hours=2)).isoformat(timespec="seconds"))
    path = _write_eval_payload(tmp_path, payload)

    result = evaluator.evaluate(path, now=now)

    assert result["state"] == evaluator.STATE_OK
    assert result["direction_plan_consistent_rate"] == 1.0
    assert result["price_geometry_rate"] == 1.0
    assert result["age_hours"] == 2.0
    assert result["date_age_days"] == 0
    assert result["report_only"] is True
    assert result["warning_reasons"] == []


def test_evaluator_warn_on_low_rates_and_stale_age(tmp_path):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=ET)
    payload = _payload(
        consistency=0.5, geometry=0.5,
        generated_at=(now - timedelta(hours=48)).isoformat(timespec="seconds"))
    path = _write_eval_payload(tmp_path, payload)

    result = evaluator.evaluate(path, now=now)

    assert result["state"] == evaluator.STATE_WARN
    assert "direction_plan_consistency_below_threshold" in result["warning_reasons"]
    assert "price_geometry_below_threshold" in result["warning_reasons"]
    assert "stale_report" in result["warning_reasons"]
    assert result["report_only"] is True


def test_evaluator_no_data_for_missing_or_empty(tmp_path, monkeypatch):
    missing = evaluator.evaluate(tmp_path / "nope.json")
    assert missing["state"] == evaluator.STATE_NO_DATA

    monkeypatch.setenv(shadow.ENV_FLAG, "1")
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "empty_ckpt"
    out = tmp_path / "out_empty"
    run_shadow(ckpt, out)
    empty = evaluator.evaluate(out / "latest.json")
    assert empty["state"] == evaluator.STATE_NO_DATA
    assert empty["total"] == 0


def test_evaluator_error_for_unreadable_schema_or_timestamp(tmp_path):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=ET)

    broken = tmp_path / "broken.json"
    broken.write_text("{not json", encoding="utf-8")
    assert evaluator.evaluate(broken, now=now)["state"] == evaluator.STATE_ERROR

    wrong_schema = _write_eval_payload(
        tmp_path, {**_payload(), "schema": "other"}, name="wrong_schema.json")
    assert evaluator.evaluate(wrong_schema, now=now)["state"] == evaluator.STATE_ERROR

    no_metrics = _write_eval_payload(
        tmp_path, {"schema": shadow.SCHEMA, "date": DAY}, name="no_metrics.json")
    assert evaluator.evaluate(no_metrics, now=now)["state"] == evaluator.STATE_ERROR

    bad_ts = _write_eval_payload(
        tmp_path, _payload(generated_at="not-a-timestamp"), name="bad_ts.json")
    bad_ts_result = evaluator.evaluate(bad_ts, now=now)
    assert bad_ts_result["state"] == evaluator.STATE_ERROR
    assert bad_ts_result["error"] == "MISSING_OR_INVALID_GENERATED_AT"


def test_evaluator_cli_exit_codes(tmp_path, capsys):
    now = datetime(2026, 11, 13, 12, 0, tzinfo=ET)
    now_text = now.isoformat()

    ok_path = _write_eval_payload(tmp_path, _payload(), name="ok.json")
    assert evaluator.main(["--path", str(ok_path), "--now", now_text, "--json"]) == 0
    capsys.readouterr()      # 清掉 OK 输出, 只解析下一条

    warn_path = _write_eval_payload(
        tmp_path, _payload(consistency=0.1), name="warn.json")
    assert evaluator.main(["--path", str(warn_path), "--now", now_text, "--json"]) == 0
    warn_out = json.loads(capsys.readouterr().out)
    assert warn_out["state"] == evaluator.STATE_WARN
    assert warn_out["report_only"] is True

    assert evaluator.main([
        "--path", str(tmp_path / "missing.json"), "--now", now_text, "--json"]) == 1
    broken = tmp_path / "broken2.json"
    broken.write_text("{", encoding="utf-8")
    assert evaluator.main(["--path", str(broken), "--now", now_text, "--json"]) == 2


def test_evaluator_date_lookup_uses_history_artifact(tmp_path, monkeypatch):
    monkeypatch.setenv(shadow.ENV_FLAG, "1")
    monkeypatch.setattr(replay, "BARS_DIRS", ())
    ckpt = tmp_path / "ckpt"
    write_checkpoint(ckpt, "US.AAA", buy_card())
    out = tmp_path / "out"
    run_shadow(ckpt, out)

    code = evaluator.main([
        "--root", str(out), "--date", DAY,
        "--now", datetime.now(ET).isoformat(), "--json",
    ])

    assert code == 0