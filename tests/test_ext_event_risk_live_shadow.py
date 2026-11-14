"""EXT 在途事件风险连续影子 · 封闭式单测 (record-only live shadow + 评估器)。

全部使用 tmp_path 下的最小 SQLite 库与临时 output-dir: 不触碰生产 data/、不联网、
不依赖运行时刻 (as_of 一律显式传入)。

覆盖:
  · 开关默认 false ⇒ DISABLED / 返回 0 / 零 DB 与零文件副作用;
  · 只评未平仓 EXT (exit_time IS NULL AND decision_id LIKE 'EXT-%');
  · as_of 之后的事件不可见 (复用底层 evaluate 的窗口语义);
  · 底层 INVALIDATE 一律降级为实盘 REVIEW, 且保留 official_invalidate_candidate;
  · 幂等: 同内容不新增/不覆盖证据, latest.json 每轮原子更新;
  · NO_CHANGE 不写逐仓证据;
  · latest 记录全部持仓与评估错误;
  · 评估器 OK/REVIEW/DISABLED/NO_DATA/ERROR + age/counts/stale。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

import scripts.ext_event_risk_live_shadow as live
import scripts.ext_event_risk_live_shadow_eval as live_eval

ENV = live.ENV_FLAG
ENTRY = "2026-11-11T16:32:40.391220-05:00"  # 2026-11-11T21:32:40.391220Z
AS_OF = "2026-11-12T14:30:42+00:00"
AS_OF_LATER = "2026-11-13T15:00:00+00:00"
NVDA_DECISION = "EXT-2026-11-11-US.NVDA-afterhours"


@pytest.fixture(autouse=True)
def _no_ambient_flag(monkeypatch):
    """开关只由测试显式给出的 env file 决定, 不受宿主环境影响。"""
    monkeypatch.delenv(ENV, raising=False)


# ---------------------------------------------------------------------------
# 最小库 / 事件
# ---------------------------------------------------------------------------


def _trade_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            trade_date TEXT,
            symbol TEXT,
            direction TEXT,
            entry_time TEXT,
            exit_time TEXT,
            quantity INTEGER,
            decision_id TEXT,
            metadata_json TEXT,
            mode TEXT,
            trade_mode TEXT,
            strategy_id TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return path


def _trade_row(
    path: Path,
    *,
    trade_id: int,
    decision_id: Optional[str] = NVDA_DECISION,
    symbol: str = "US.NVDA",
    direction: str = "LONG",
    entry_time: str = ENTRY,
    exit_time: Optional[str] = None,
    quantity: int = 3,
) -> None:
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO trade_records (id, trade_date, symbol, direction, entry_time, "
        "exit_time, quantity, decision_id, metadata_json, mode, trade_mode, strategy_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            trade_id,
            "2026-11-11",
            symbol,
            direction,
            entry_time,
            exit_time,
            quantity,
            decision_id,
            json.dumps({"decision_id": decision_id}),
            "futu_simulate",
            "simulate",
            "extended_hours_entry",
        ),
    )
    conn.commit()
    conn.close()


def _event_db(path: Path) -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE canonical_events (
            event_id TEXT PRIMARY KEY,
            source_id TEXT NOT NULL,
            source_tier TEXT NOT NULL,
            source_url TEXT NOT NULL,
            event_type TEXT NOT NULL,
            event_time TEXT,
            headline TEXT NOT NULL,
            summary TEXT,
            facts_json TEXT NOT NULL,
            status TEXT NOT NULL,
            confidence REAL NOT NULL,
            reasoning_version TEXT NOT NULL,
            first_seen_at TEXT,
            published_to_seen_ms INTEGER,
            event_meta_json TEXT
        )"""
    )
    conn.commit()
    conn.close()
    return path


def _insert_event(
    path: Path,
    *,
    event_id: str,
    first_seen_at: str,
    headline: str,
    summary: str = "",
    source_id: str = "FINNHUB_GENERAL_NEWS",
    source_tier: str = "T1",
    source_url: Optional[str] = None,
    event_type: str = "NEWS_WIRE",
    event_time: str = "2026-11-12T01:14:21+00:00",
    materiality: Optional[float] = 0.45,
    entities: Optional[Dict[str, Any]] = None,
) -> None:
    meta: Dict[str, Any] = {
        "schema": "event_meta_v1",
        "source_id": source_id,
        "source_tier": source_tier,
        "event_type": event_type,
        "entities": entities or {"cik": None, "company": None, "symbols": []},
    }
    if materiality is not None:
        meta["materiality"] = materiality
    conn = sqlite3.connect(path)
    conn.execute(
        "INSERT INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            event_id,
            source_id,
            source_tier,
            source_url or f"https://example.test/{event_id}",
            event_type,
            event_time,
            headline,
            summary,
            "{}",
            "UNVERIFIED",
            0.5,
            "v0",
            first_seen_at,
            None,
            json.dumps(meta, sort_keys=True),
        ),
    )
    conn.commit()
    conn.close()


def _media_review_event(path: Path) -> None:
    _insert_event(
        path,
        event_id="media-probe",
        first_seen_at="2026-11-12T01:44:03+00:00",
        headline="Nvidia faces probe over China chip exports",
        summary="A media report linked to Nvidia.",
    )


def _official_invalidate_event(path: Path, *, first_seen_at: str) -> None:
    _insert_event(
        path,
        event_id="official-ceo-resigns",
        first_seen_at=first_seen_at,
        headline="8-K - NVIDIA CORP (0001045810) (Filer)",
        summary="Item 5.02: NVIDIA CEO Jensen Huang has resigned effective immediately.",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        source_url="https://www.sec.gov/Archives/edgar/data/1045810/example",
        event_type="SEC_8K_FILING",
        materiality=0.87,
        entities={"cik": "0001045810", "company": "NVIDIA CORP", "symbols": []},
    )


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _env_file(tmp_path: Path, *, enabled: bool) -> Path:
    path = tmp_path / f"env-{int(enabled)}"
    path.write_text(f"{ENV}={'true' if enabled else 'false'}\n", encoding="utf-8")
    return path


def _run_live(
    tmp_path: Path,
    *,
    trade_db: Path,
    event_db: Path,
    output_dir: Path,
    as_of: str = AS_OF,
    enabled: bool = True,
) -> int:
    return live.main(
        [
            "--trade-db",
            str(trade_db),
            "--event-db",
            str(event_db),
            "--as-of",
            as_of,
            "--output-dir",
            str(output_dir),
            "--env-file",
            str(_env_file(tmp_path, enabled=enabled)),
        ]
    )


def _latest(output_dir: Path) -> Dict[str, Any]:
    return json.loads((output_dir / "latest.json").read_text(encoding="utf-8"))


def _review_artifacts(output_dir: Path) -> list:
    return sorted((output_dir / "reviews").glob("*.json"))


# ---------------------------------------------------------------------------
# 开关 / 副作用
# ---------------------------------------------------------------------------


def test_disabled_prints_and_has_zero_side_effects(tmp_path: Path, capsys) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    _media_review_event(event_db)
    before = (_sha(trade_db), _sha(event_db))
    output_dir = tmp_path / "live"

    rc = _run_live(
        tmp_path,
        trade_db=trade_db,
        event_db=event_db,
        output_dir=output_dir,
        enabled=False,
    )

    assert rc == 0
    assert "DISABLED" in capsys.readouterr().out
    assert not output_dir.exists()
    assert (_sha(trade_db), _sha(event_db)) == before
    assert not list(tmp_path.rglob("latest.json"))


def test_default_flag_is_false_when_env_file_has_no_key(tmp_path: Path, capsys) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("# nothing here\n", encoding="utf-8")
    output_dir = tmp_path / "live"

    rc = live.main(
        [
            "--trade-db",
            str(trade_db),
            "--event-db",
            str(event_db),
            "--output-dir",
            str(output_dir),
            "--env-file",
            str(empty_env),
        ]
    )

    assert rc == 0
    assert "DISABLED" in capsys.readouterr().out
    assert not output_dir.exists()


# ---------------------------------------------------------------------------
# 持仓来源 / 窗口
# ---------------------------------------------------------------------------


def test_only_open_ext_positions_are_evaluated(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)  # 未平仓 EXT — 唯一入选
    _trade_row(
        trade_db,
        trade_id=2000,
        decision_id="DA-2026-11-11-US.AMD",
        symbol="US.AMD",
    )  # 未平仓但非 EXT
    _trade_row(
        trade_db,
        trade_id=2001,
        exit_time="2026-11-12T10:00:00-05:00",
    )  # 已平仓 EXT
    _trade_row(
        trade_db,
        trade_id=2002,
        decision_id=None,
        symbol="US.TSLA",
    )  # 未平仓 but 无 EXT decision_id
    event_db = _event_db(tmp_path / "event_stream.db")
    _media_review_event(event_db)
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0

    latest = _latest(output_dir)
    assert latest["position_source"]["table"] == "trade_records"
    assert "exit_time IS NULL" in latest["position_source"]["sql"]
    assert [row["trade_id"] for row in latest["positions"]] == [1864]
    assert latest["counts"]["positions_total"] == 1


def test_events_after_as_of_are_invisible(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    _official_invalidate_event(event_db, first_seen_at="2026-11-13T00:00:00+00:00")
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0

    position = _latest(output_dir)["positions"][0]
    assert position["underlying_verdict"] == "NO_CHANGE"
    assert position["live_verdict"] == "NO_CHANGE"
    assert position["official_invalidate_candidate"] is False
    assert position["assessment_event"] is None
    assert _review_artifacts(output_dir) == []


# ---------------------------------------------------------------------------
# INVALIDATE → REVIEW 降级
# ---------------------------------------------------------------------------


def test_underlying_invalidate_is_downgraded_to_review(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    _official_invalidate_event(event_db, first_seen_at="2026-11-12T02:00:00+00:00")
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0

    latest = _latest(output_dir)
    position = latest["positions"][0]
    assert position["underlying_verdict"] == "INVALIDATE"
    assert position["live_verdict"] == "REVIEW"
    assert position["official_invalidate_candidate"] is True
    assert position["downgraded_from_invalidate"] is True
    assert latest["counts"]["review"] == 1
    assert latest["counts"]["official_invalidate_candidates"] == 1
    assert all(row["live_verdict"] in live.LIVE_VERDICTS for row in latest["positions"])

    artifacts = _review_artifacts(output_dir)
    assert len(artifacts) == 1
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["kind"] == "review_artifact"
    assert artifact["live_verdict"] == "REVIEW"
    assert artifact["official_invalidate_candidate"] is True
    assert artifact["underlying"]["verdict"] == "INVALIDATE"
    assert artifact["downgrade"] == {
        "from": "INVALIDATE",
        "to": "REVIEW",
        "reason": (
            "live shadow never acts on its own; an official/primary-source "
            "contradiction is surfaced for human review only"
        ),
    }
    # 硬边界: 无动作、无下单、无反事实 PnL。
    assert artifact["production_write"] is False
    assert artifact["auto_action"] is False
    assert artifact["orders_placed"] == 0
    assert artifact["counterfactual_status"] == "not_computed"
    assert artifact["avoided_pnl_usd"] is None
    assert latest["production_write"] is False
    assert latest["auto_action"] is False
    assert latest["orders_placed"] == 0
    assert latest["avoided_pnl_usd"] is None


def test_map_live_verdict_never_emits_invalidate() -> None:
    for underlying in ("INVALIDATE", "REVIEW", "NO_CHANGE", "", "SOMETHING_NEW"):
        mapped = live.map_live_verdict(underlying)
        assert mapped["live_verdict"] in live.LIVE_VERDICTS
    assert live.map_live_verdict("INVALIDATE")["official_invalidate_candidate"] is True
    assert live.map_live_verdict("NO_CHANGE")["live_verdict"] == "NO_CHANGE"


# ---------------------------------------------------------------------------
# 幂等 / latest
# ---------------------------------------------------------------------------


def test_review_artifact_is_idempotent_and_latest_refreshes(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    _media_review_event(event_db)
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0
    first_latest = _latest(output_dir)
    first_position = first_latest["positions"][0]
    assert first_position["live_verdict"] == "REVIEW"
    assert first_position["artifact_written"] is True
    assert first_latest["counts"]["new_review_artifacts"] == 1
    artifacts = _review_artifacts(output_dir)
    assert len(artifacts) == 1
    artifact_bytes = artifacts[0].read_bytes()
    artifact_path = first_position["artifact"]

    # 第二轮: 事件内容与判决不变, 只把 as_of 推后 ⇒ 不新增、不覆盖证据。
    assert _run_live(
        tmp_path,
        trade_db=trade_db,
        event_db=event_db,
        output_dir=output_dir,
        as_of=AS_OF_LATER,
    ) == 0

    second_latest = _latest(output_dir)
    second_position = second_latest["positions"][0]
    assert second_position["artifact_written"] is False
    assert second_position["artifact"] == artifact_path
    assert second_latest["counts"]["new_review_artifacts"] == 0
    assert second_latest["as_of"].startswith("2026-11-13T15:00:00")
    assert _review_artifacts(output_dir) == artifacts
    assert artifacts[0].read_bytes() == artifact_bytes

    # 事件内容变化 (新官方反证) ⇒ 新证据文件, 老证据原样保留。
    _official_invalidate_event(event_db, first_seen_at="2026-11-13T12:00:00+00:00")
    assert _run_live(
        tmp_path,
        trade_db=trade_db,
        event_db=event_db,
        output_dir=output_dir,
        as_of=AS_OF_LATER,
    ) == 0
    third_latest = _latest(output_dir)
    assert third_latest["counts"]["new_review_artifacts"] == 1
    assert len(_review_artifacts(output_dir)) == 2
    assert artifacts[0].read_bytes() == artifact_bytes


def test_no_change_writes_latest_but_no_per_position_artifact(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")  # 零事件
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0

    latest = _latest(output_dir)
    assert latest["counts"] == {
        "positions_total": 1,
        "evaluated": 1,
        "no_change": 1,
        "review": 0,
        "official_invalidate_candidates": 0,
        "new_review_artifacts": 0,
        "errors": 0,
    }
    assert latest["positions"][0]["artifact"] is None
    assert latest["positions"][0]["artifact_written"] is False
    assert not (output_dir / "reviews").exists()


def test_latest_records_position_evaluation_errors(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864, entry_time="2026-11-14T10:00:00-05:00")
    event_db = _event_db(tmp_path / "event_stream.db")
    output_dir = tmp_path / "live"

    rc = _run_live(
        tmp_path,
        trade_db=trade_db,
        event_db=event_db,
        output_dir=output_dir,
        as_of="2026-11-13T00:00:00+00:00",
    )

    assert rc == 2  # 单仓失败不中断整轮, 但必须显性化 (latest + 退出码)
    latest = _latest(output_dir)
    assert latest["counts"]["errors"] == 1
    assert latest["counts"]["evaluated"] == 0
    assert latest["positions"][0]["status"] == "ERROR"
    assert latest["positions"][0]["live_verdict"] is None
    assert "as_of must be strictly after entry_time" in latest["errors"][0]["error"]


def test_missing_trade_db_writes_fatal_latest(tmp_path: Path) -> None:
    event_db = _event_db(tmp_path / "event_stream.db")
    output_dir = tmp_path / "live"
    missing = tmp_path / "nope.db"

    rc = _run_live(
        tmp_path, trade_db=missing, event_db=event_db, output_dir=output_dir
    )

    assert rc == 2
    latest = _latest(output_dir)
    assert latest["fatal_error"]
    assert latest["positions"] == []
    assert latest["counts"]["positions_total"] == 0


# ---------------------------------------------------------------------------
# 评估器
# ---------------------------------------------------------------------------


def _write_latest(output_dir: Path, payload: Dict[str, Any]) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / "latest.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


def _synthetic_latest(*, counts: Dict[str, int], generated_at: datetime) -> Dict[str, Any]:
    return {
        "schema": live_eval.WRITER_SCHEMA,
        "mode": "live_record_only",
        "generated_at": generated_at.isoformat(),
        "as_of": generated_at.isoformat(),
        "as_of_source": "cli",
        "counts": counts,
        "positions": [],
        "errors": [],
    }


def _run_eval(tmp_path: Path, *extra: str, enabled: bool) -> int:
    return live_eval.main(
        [
            "--output-dir",
            str(tmp_path / "live"),
            "--env-file",
            str(_env_file(tmp_path, enabled=enabled)),
            *extra,
        ]
    )


def test_evaluator_disabled_state(tmp_path: Path, capsys) -> None:
    now = datetime.now(timezone.utc)
    _write_latest(
        tmp_path / "live",
        _synthetic_latest(
            counts={
                "positions_total": 1,
                "evaluated": 1,
                "no_change": 1,
                "review": 0,
                "official_invalidate_candidates": 0,
                "new_review_artifacts": 0,
                "errors": 0,
            },
            generated_at=now,
        ),
    )

    rc = _run_eval(tmp_path, enabled=False)

    assert rc == 0
    assert "status=DISABLED" in capsys.readouterr().out


def test_evaluator_no_data_state(tmp_path: Path, capsys) -> None:
    rc = _run_eval(tmp_path, enabled=True)

    assert rc == 1
    assert "status=NO_DATA" in capsys.readouterr().out


def test_evaluator_ok_state_with_age_and_counts(tmp_path: Path, capsys) -> None:
    now = datetime.now(timezone.utc)
    generated_at = now - timedelta(seconds=120)
    _write_latest(
        tmp_path / "live",
        _synthetic_latest(
            counts={
                "positions_total": 1,
                "evaluated": 1,
                "no_change": 1,
                "review": 0,
                "official_invalidate_candidates": 0,
                "new_review_artifacts": 0,
                "errors": 0,
            },
            generated_at=generated_at,
        ),
    )

    rc = _run_eval(tmp_path, "--json", enabled=True)

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "OK"
    assert result["counts"]["positions_total"] == 1
    assert result["age_source"] == "generated_at"
    assert 119 <= result["age_seconds"] <= 125
    assert result["age_human"]
    assert result["stale"] is None


def test_evaluator_review_state_and_stale_flag(tmp_path: Path, capsys) -> None:
    now = datetime.now(timezone.utc)
    _write_latest(
        tmp_path / "live",
        _synthetic_latest(
            counts={
                "positions_total": 2,
                "evaluated": 2,
                "no_change": 1,
                "review": 1,
                "official_invalidate_candidates": 1,
                "new_review_artifacts": 0,
                "errors": 0,
            },
            generated_at=now - timedelta(minutes=10),
        ),
    )

    rc = _run_eval(tmp_path, "--json", "--max-age-minutes", "5", enabled=True)

    assert rc == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "REVIEW"
    assert result["counts"]["review"] == 1
    assert result["counts"]["official_invalidate_candidates"] == 1
    assert result["stale"] is True


def test_evaluator_error_on_malformed_latest(tmp_path: Path, capsys) -> None:
    output_dir = tmp_path / "live"
    output_dir.mkdir()
    (output_dir / "latest.json").write_text("{not json", encoding="utf-8")

    rc = _run_eval(tmp_path, "--json", enabled=True)

    assert rc == 2
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "ERROR"
    assert any("cannot read latest" in note for note in result["notes"])


def test_evaluator_error_when_writer_recorded_position_errors(
    tmp_path: Path, capsys
) -> None:
    now = datetime.now(timezone.utc)
    payload = _synthetic_latest(
        counts={
            "positions_total": 1,
            "evaluated": 0,
            "no_change": 0,
            "review": 0,
            "official_invalidate_candidates": 0,
            "new_review_artifacts": 0,
            "errors": 1,
        },
        generated_at=now,
    )
    payload["errors"] = [{"trade_id": 1864, "error": "ValueError: boom"}]
    _write_latest(tmp_path / "live", payload)

    rc = _run_eval(tmp_path, "--json", enabled=True)

    assert rc == 2
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "ERROR"
    assert len(result["errors"]) == 1


def test_evaluator_ignore_flag_reads_latest_while_disabled(tmp_path: Path, capsys) -> None:
    now = datetime.now(timezone.utc)
    _write_latest(
        tmp_path / "live",
        _synthetic_latest(
            counts={
                "positions_total": 0,
                "evaluated": 0,
                "no_change": 0,
                "review": 0,
                "official_invalidate_candidates": 0,
                "new_review_artifacts": 0,
                "errors": 0,
            },
            generated_at=now,
        ),
    )

    rc = _run_eval(tmp_path, "--json", "--ignore-flag", enabled=False)

    assert rc == 0
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "OK"


def test_evaluator_end_to_end_after_live_run(tmp_path: Path, capsys) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    _trade_row(trade_db, trade_id=1864)
    event_db = _event_db(tmp_path / "event_stream.db")
    _media_review_event(event_db)
    output_dir = tmp_path / "live"

    assert _run_live(
        tmp_path, trade_db=trade_db, event_db=event_db, output_dir=output_dir
    ) == 0
    capsys.readouterr()

    rc = _run_eval(tmp_path, "--json", enabled=True)

    assert rc == 1
    result = json.loads(capsys.readouterr().out)
    assert result["state"] == "REVIEW"
    assert result["counts"]["positions_total"] == 1
    assert result["counts"]["review"] == 1
    assert result["positions"][0]["trade_id"] == 1864
    assert result["positions"][0]["live_verdict"] == "REVIEW"
    assert result["age_seconds"] is not None