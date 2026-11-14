"""RFC-205 EXT 在途事件再评估影子 · 封闭式单测。

全部使用 tmp_path 下的最小 SQLite 库，不触碰生产 data/、不联网、不依赖事件时刻。
覆盖：窗口边界（入场前排除 / 未来事件排除 / as_of 覆盖）、URL+标题去重、
REVIEW 与 INVALIDATE 的证据层级、官方 Item 5.02 通用样板不得反证、
反事实必须 unavailable、只读不改库、输出哈希与 CLI 拒绝覆盖。
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "ext_event_risk_shadow.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("ext_event_risk_shadow_test", SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


shadow = _load_module()

ENTRY = "2026-11-11T16:32:40.391220-05:00"  # 2026-11-11T21:32:40.391220Z
EXIT = "2026-11-12T09:30:42.518331-05:00"   # 2026-11-12T14:30:42.518331Z
TARGET_EVENT = "853ef3ee05478043a7c9"
TARGET_FIRST_SEEN = "2026-11-12T01:44:03.138077+00:00"


def _trade_db(path: Path, *, decision_id: str = "EXT-2026-11-11-US.NVDA-afterhours") -> Path:
    conn = sqlite3.connect(path)
    conn.execute(
        """CREATE TABLE trade_records (
            id INTEGER PRIMARY KEY,
            trade_date TEXT,
            symbol TEXT,
            direction TEXT,
            entry_time TEXT,
            exit_time TEXT,
            entry_price REAL,
            exit_price REAL,
            quantity INTEGER,
            realized_pnl REAL,
            pnl_net REAL,
            exit_reason TEXT,
            decision_id TEXT,
            metadata_json TEXT,
            source_type TEXT,
            mode TEXT
        )"""
    )
    metadata = {
        "session": "afterhours",
        "gate_mode": "research_momentum",
        "decision_id": decision_id,
        "stop_loss": 156.0,
        "admission_verdict": "PASS",
    }
    conn.execute(
        "INSERT INTO trade_records VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (
            1864,
            "2026-11-11",
            "US.NVDA",
            "LONG",
            ENTRY,
            EXIT,
            174.14,
            149.76,
            28,
            -682.64,
            -686.88,
            "OVERNIGHT_GAP_STOP",
            decision_id,
            json.dumps(metadata),
            "PLAYBOOK_SIM",
            "futu_simulate",
        ),
    )
    conn.commit()
    conn.close()
    return path


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
    facts: Optional[Dict[str, Any]] = None,
) -> None:
    meta: Dict[str, Any] = {
        "schema": "event_meta_v1",
        "source_id": source_id,
        "source_tier": source_tier,
        "event_type": event_type,
        "entities": entities
        or {"cik": None, "company": None, "symbols": []},
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
            json.dumps(facts or {}, sort_keys=True),
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


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _evaluate(tmp_path: Path, **kwargs: Any) -> Dict[str, Any]:
    defaults = {
        "trade_db": tmp_path / "ftseek_us.db",
        "event_db": tmp_path / "event_stream.db",
        "trade_id": 1864,
        "decision_id": "EXT-2026-11-11-US.NVDA-afterhours",
        "as_of_override": None,
        "extra_aliases": (),
        "materiality_threshold": 0.40,
    }
    defaults.update(kwargs)
    return shadow.evaluate(**defaults)


def _nvda_case_dbs(tmp_path: Path) -> tuple[Path, Path]:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    # 早期媒体综述：material 但非 CEO 级
    _insert_event(
        event_db,
        event_id="early-market-wrap",
        first_seen_at="2026-11-11T23:19:04.330812+00:00",
        headline="Stocks jump as oil tumbles, Nvidia falls amid internal probe - Reuters",
        summary="A broad market wrap mentions Nvidia in passing.",
    )
    # 目标事件：T1 媒体，materiality .45，CEO 可能被罢免
    _insert_event(
        event_db,
        event_id=TARGET_EVENT,
        first_seen_at=TARGET_FIRST_SEEN,
        headline="Cramer's stance on Nvidia's stock amid reports that CEO Jensen Huang may be ousted",
        summary="Jim Cramer said he was saddened by a news report that Nvidia's board is preparing to fire Huang.",
    )
    # 官方 8-K：Item 5.02 通用样板，无 CEO 级显式短语
    _insert_event(
        event_db,
        event_id="official-8k-generic",
        first_seen_at="2026-11-12T13:59:03.973749+00:00",
        headline="8-K - NVIDIA CORP (0001045810) (Filer)",
        summary=(
            "Item 5.02: Departure of Directors or Certain Officers; Election of "
            "Directors; Appointment of Certain Officers; Compensatory Arrangements."
        ),
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        source_url="https://www.sec.gov/Archives/edgar/data/1045810/000104581026000082/",
        event_type="SEC_8K_FILING",
        event_time="2026-11-12T08:52:41-05:00",
        materiality=0.87,
        entities={"cik": "0001045810", "company": "NVIDIA CORP", "symbols": []},
    )
    # 媒体称已辞职，但仍是 T1，不能反证
    _insert_event(
        event_db,
        event_id="media-resigned",
        first_seen_at="2026-11-12T14:19:06.818213+00:00",
        headline="Jensen Huang's Nvidia fortune is jointly controlled now that he's resigned",
        summary="A media report about Huang's trusts.",
        event_time="2026-11-12T14:13:15+00:00",
    )
    return trade_db, event_db


def test_nvda_case_is_review_and_primary_event_is_853(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    result = _evaluate(tmp_path)

    assert result["verdict"] == "REVIEW"
    assert result["production_write"] is False
    assert result["auto_action"] is False
    assert result["as_of_source"] == "exit_time"
    assert result["as_of"] == "2026-11-12T14:30:42.518331+00:00"
    assert result["window"]["entry_time_utc"] == "2026-11-11T21:32:40.391220+00:00"
    assert result["assessment_event"]["event_id"] == TARGET_EVENT
    assert result["assessment_event"]["first_seen_at"] == TARGET_FIRST_SEEN
    assert result["assessment_event"]["source_id"] == "FINNHUB_GENERAL_NEWS"
    assert result["assessment_event"]["evidence_tier"] == "MAJOR_MEDIA"
    assert result["assessment_event"]["materiality"] == 0.45
    # 更早的媒体综述也是 material，但 primary 选 CEO 级严重度更高的 853。
    assert result["first_material_event"]["event_id"] == "early-market-wrap"
    assert result["contradiction_check"]["invalidation_candidates"] == []
    official = result["contradiction_check"]["official_primary_events_examined"]
    assert len(official) == 1
    assert official[0]["event_id"] == "official-8k-generic"
    assert official[0]["explicit_contradiction"] is False
    # 原始 event_time / first_seen 必须逐字保留
    assert result["assessment_event"]["event_time"] == "2026-11-12T01:14:21+00:00"
    assert result["assessment_event"]["first_seen_at"] == TARGET_FIRST_SEEN
    # 决策元数据与哈希
    assert result["decision_metadata"]["gate_mode"] == "research_momentum"
    assert result["decision_metadata_sha256"]
    assert result["trade_record"]["pnl_net"] == -686.88
    assert result["input_hashes"]["trade_db_file_sha256"]
    assert result["input_hashes"]["event_db_file_sha256"]
    assert result["evaluation_sha256"]
    assert result["payload_sha256"]


def test_no_future_events_are_visible(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="future-event",
        first_seen_at="2026-11-12T15:00:00+00:00",  # 晚于 exit
        headline="Nvidia CEO Jensen Huang resigns in official filing",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
    )
    assert trade_db and event_db
    result = _evaluate(tmp_path)

    assert result["verdict"] == "NO_CHANGE"
    assert result["assessment_event"] is None
    assert result["material_event_count"] == 0
    assert result["window_audit"]["excluded_after_as_of"] == 1
    assert result["window_audit"]["included_in_window"] == 0


def test_events_before_or_at_entry_are_excluded(tmp_path: Path) -> None:
    trade_db = _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="before-entry",
        first_seen_at="2026-11-11T20:00:00+00:00",
        headline="Nvidia CEO Jensen Huang resigns in official filing",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
    )
    _insert_event(
        event_db,
        event_id="exactly-at-entry",
        first_seen_at="2026-11-11T21:32:40.391220+00:00",
        headline="Nvidia CEO Jensen Huang resigns in official filing",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
    )
    assert trade_db and event_db
    result = _evaluate(tmp_path)

    assert result["verdict"] == "NO_CHANGE"
    assert result["window_audit"]["excluded_before_or_at_entry"] == 2
    assert result["window_audit"]["included_in_window"] == 0


def test_url_and_title_dedup_keeps_earliest(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    base_url = "https://www.example.test/story?id=1"
    _insert_event(
        event_db,
        event_id="base",
        first_seen_at="2026-11-11T23:00:00+00:00",
        headline="Nvidia faces probe over China chip exports",
        source_url=base_url,
    )
    _insert_event(
        event_db,
        event_id="same-url",
        first_seen_at="2026-11-11T23:30:00+00:00",
        headline="Nvidia different headline, same URL",
        source_url=base_url,
    )
    _insert_event(
        event_db,
        event_id="same-title",
        first_seen_at="2026-11-12T00:30:00+00:00",
        headline="Nvidia faces probe over China chip exports - Another Publisher",
        source_url="https://another.example.test/story",
    )
    # 传递性去重：沿用“同 URL 不同标题”副本的标题，仍应归入同一组。
    _insert_event(
        event_db,
        event_id="transitive-title",
        first_seen_at="2026-11-12T01:00:00+00:00",
        headline="Nvidia different headline, same URL",
        source_url="https://third.example.test/story",
    )
    result = _evaluate(tmp_path)

    assert result["verdict"] == "REVIEW"
    assert result["dedup"]["direct_events_before_dedup"] == 4
    assert result["dedup"]["direct_events_after_dedup"] == 1
    assert result["dedup"]["duplicates_removed"] == 3
    reasons = {item["reason"] for item in result["dedup"]["duplicates"]}
    assert reasons == {"url", "title"}
    assert result["dedup"]["duplicates"][-1]["reason"] == "title"
    assert result["dedup"]["duplicates"][-1]["duplicate_event_id"] == "transitive-title"
    assert result["assessment_event"]["event_id"] == "base"


def test_unverified_media_headline_requires_review_not_invalidate(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="media-rumor",
        first_seen_at="2026-11-12T01:00:00+00:00",
        headline="Nvidia CEO Jensen Huang resigns, unconfirmed report says",
        source_id="GOOGLE_NEWS_RSS",
        source_tier="T2",
        event_type="NEWS_DISCOVERY",
        materiality=0.45,
    )
    result = _evaluate(tmp_path)

    assert result["verdict"] == "REVIEW"
    assert result["contradiction_check"]["invalidation_candidates"] == []
    assert result["assessment_event"]["evidence_tier"] == "DISCOVERY_RSS"
    assert result["counterfactual"]["status"] == "unavailable"


def test_official_explicit_contradiction_invalidates(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="official-ceo-resigns",
        first_seen_at="2026-11-12T02:00:00+00:00",
        headline="8-K - NVIDIA CORP (0001045810) (Filer)",
        summary="Item 5.02: NVIDIA CEO Jensen Huang has resigned effective immediately.",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
        materiality=0.87,
        entities={"cik": "0001045810", "company": "NVIDIA CORP", "symbols": []},
    )
    result = _evaluate(tmp_path)

    assert result["verdict"] == "INVALIDATE"
    assert result["assessment_event"]["event_id"] == "official-ceo-resigns"
    candidates = result["contradiction_check"]["invalidation_candidates"]
    assert len(candidates) == 1
    assert candidates[0]["evidence"]["kind"] == "leadership_crisis"
    assert "CEO" in candidates[0]["evidence"]["excerpt"]
    assert result["counterfactual"]["status"] == "unavailable"


def test_official_generic_item_502_cannot_invalidate(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="official-generic-only",
        first_seen_at="2026-11-12T02:00:00+00:00",
        headline="8-K - NVIDIA CORP (0001045810) (Filer)",
        summary=(
            "Item 5.02: Departure of Directors or Certain Officers; Election of "
            "Directors; Appointment of Certain Officers."
        ),
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
        materiality=0.87,
    )
    result = _evaluate(tmp_path)

    # 官方来源使其成为 material，但缺少 CEO 级显式短语 ⇒ 只能 REVIEW。
    assert result["verdict"] == "REVIEW"
    assert result["contradiction_check"]["invalidation_candidates"] == []
    assert result["assessment_event"]["event_id"] == "official-generic-only"


def test_dedup_promotes_material_duplicate_without_losing_earliest(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    # 先到的 T2 副本 materiality 未过阈值；后到的 T1 副本过阈值，标题相同。
    _insert_event(
        event_db,
        event_id="early-low-tier",
        first_seen_at="2026-11-11T23:00:00+00:00",
        headline="Nvidia faces probe over China chip exports",
        source_id="GOOGLE_NEWS_RSS",
        source_tier="T2",
        event_type="NEWS_DISCOVERY",
        materiality=0.25,
        source_url="https://aggregator.example.test/story",
    )
    _insert_event(
        event_db,
        event_id="late-material-tier",
        first_seen_at="2026-11-12T00:00:00+00:00",
        headline="Nvidia faces probe over China chip exports",
        source_id="FINNHUB_GENERAL_NEWS",
        source_tier="T1",
        event_type="NEWS_WIRE",
        materiality=0.45,
        source_url="https://wire.example.test/story",
    )
    result = _evaluate(tmp_path)

    assert result["verdict"] == "REVIEW"
    assert result["dedup"]["direct_events_before_dedup"] == 2
    assert result["dedup"]["direct_events_after_dedup"] == 1
    assert result["assessment_event"]["event_id"] == "early-low-tier"
    assert result["assessment_event"]["first_seen_at"] == "2026-11-11T23:00:00+00:00"
    assert result["assessment_event"]["material"] is True
    assert result["assessment_event"]["material_from_duplicate_event_ids"] == [
        "late-material-tier"
    ]
    assert result["assessment_event"]["dedup_group_event_ids"] == [
        "early-low-tier",
        "late-material-tier",
    ]


def test_official_duplicate_behind_media_copy_still_invalidates(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    headline = "NVIDIA CEO Jensen Huang resigns"
    _insert_event(
        event_db,
        event_id="early-media-copy",
        first_seen_at="2026-11-12T01:00:00+00:00",
        headline=headline,
        source_id="GOOGLE_NEWS_RSS",
        source_tier="T2",
        event_type="NEWS_DISCOVERY",
        materiality=0.45,
        source_url="https://aggregator.example.test/story",
    )
    _insert_event(
        event_db,
        event_id="later-official-copy",
        first_seen_at="2026-11-12T01:30:00+00:00",
        headline=headline,
        summary="Item 5.02: NVIDIA CEO Jensen Huang has resigned effective immediately.",
        source_id="SEC_EDGAR_8K_ATOM",
        source_tier="T0",
        event_type="SEC_8K_FILING",
        materiality=0.87,
        source_url="https://www.sec.gov/Archives/edgar/data/1045810/example",
    )
    result = _evaluate(tmp_path)

    # 标题去重保留最早媒体副本，但官方副本仍在“去重前官方事件”集合里接受反证检查。
    assert result["dedup"]["direct_events_after_dedup"] == 1
    assert result["verdict"] == "INVALIDATE"
    assert result["assessment_event"]["event_id"] == "later-official-copy"
    candidates = result["contradiction_check"]["invalidation_candidates"]
    assert [item["event_id"] for item in candidates] == ["later-official-copy"]


def test_counterfactual_is_never_fabricated(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    result = _evaluate(tmp_path)

    cf = result["counterfactual"]
    assert cf["status"] == "unavailable"
    assert cf["claimed"] is False
    assert cf["avoided_pnl_usd"] is None
    assert "No event-window bars" in cf["reason"]
    assert cf["required_inputs_for_future_evaluation"]
    # 交易本身的已实现亏损可以保留作上下文，但不得被包装成“避免的亏损”。
    assert result["trade_record"]["pnl_net"] == -686.88
    serialized = json.dumps(result)
    assert "avoided_pnl" in serialized  # 字段存在且为 null
    assert '"avoided_pnl_usd": "' not in serialized


def test_as_of_override_excludes_later_official_event(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    result = _evaluate(
        tmp_path,
        as_of_override="2026-11-12T02:00:00+00:00",
    )

    assert result["as_of_source"] == "cli_override"
    assert result["as_of"] == "2026-11-12T02:00:00+00:00"
    assert result["verdict"] == "REVIEW"
    assert result["assessment_event"]["event_id"] == TARGET_EVENT
    material_ids = {event["event_id"] for event in result["material_events"]}
    assert "official-8k-generic" not in material_ids
    assert "media-resigned" not in material_ids
    assert result["window_audit"]["excluded_after_as_of"] >= 2


def test_evaluation_is_deterministic_and_read_only(tmp_path: Path) -> None:
    trade_db, event_db = _nvda_case_dbs(tmp_path)
    before_trade = _sha(trade_db)
    before_event = _sha(event_db)

    first = _evaluate(tmp_path)
    second = _evaluate(tmp_path)

    assert first["evaluation_sha256"] == second["evaluation_sha256"]
    assert first["payload_sha256"] == second["payload_sha256"]
    assert _sha(trade_db) == before_trade
    assert _sha(event_db) == before_event
    # SQLite 只读连接可能创建空的 -shm/-wal 旁文件（不落已提交页）；
    # 判据是主库内容不变且 WAL 没有新增已提交内容。
    for suffix in ("-wal", "-journal"):
        sidecar = tmp_path / f"ftseek_us.db{suffix}"
        assert not sidecar.exists() or sidecar.stat().st_size == 0


def test_cli_refuses_to_overwrite_output(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    out = tmp_path / "artifact.json"
    out.write_text("SENTINEL\n", encoding="utf-8")

    rc = shadow.main(
        [
            "--trade-id",
            "1864",
            "--decision-id",
            "EXT-2026-11-11-US.NVDA-afterhours",
            "--trade-db",
            str(tmp_path / "ftseek_us.db"),
            "--event-db",
            str(tmp_path / "event_stream.db"),
            "--out",
            str(out),
            "--quiet",
        ]
    )

    assert rc == 2
    assert out.read_text(encoding="utf-8") == "SENTINEL\n"


def test_cli_writes_new_artifact_with_required_fields(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    out = tmp_path / "artifact.json"

    rc = shadow.main(
        [
            "--trade-id",
            "1864",
            "--decision-id",
            "EXT-2026-11-11-US.NVDA-afterhours",
            "--trade-db",
            str(tmp_path / "ftseek_us.db"),
            "--event-db",
            str(tmp_path / "event_stream.db"),
            "--out",
            str(out),
            "--quiet",
        ]
    )

    assert rc == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["production_write"] is False
    assert payload["verdict"] == "REVIEW"
    assert payload["assessment_event"]["event_id"] == TARGET_EVENT
    for key in (
        "schema",
        "as_of",
        "db_paths",
        "input_hashes",
        "evaluation_sha256",
        "payload_sha256",
        "decision_metadata",
        "trade_record",
        "counterfactual",
        "limitations",
    ):
        assert key in payload
    assert payload["output_path"] == str(out)


def test_decision_id_mismatch_is_refused(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    with pytest.raises(ValueError, match="decision_id mismatch"):
        _evaluate(tmp_path, decision_id="EXT-WRONG")


def test_missing_trade_is_refused(tmp_path: Path) -> None:
    _nvda_case_dbs(tmp_path)
    with pytest.raises(LookupError, match="not found"):
        _evaluate(tmp_path, trade_id=9999)


def test_relevance_requires_direct_symbol_or_alias(tmp_path: Path) -> None:
    _trade_db(tmp_path / "ftseek_us.db")
    event_db = _event_db(tmp_path / "event_stream.db")
    _insert_event(
        event_db,
        event_id="sector-only",
        first_seen_at="2026-11-12T01:00:00+00:00",
        headline="Semiconductor stocks fall as chip selloff broadens",
        summary="No company-specific name is mentioned here.",
    )
    result = _evaluate(tmp_path)

    assert result["verdict"] == "NO_CHANGE"
    assert result["dedup"]["direct_events_before_dedup"] == 0
    assert result["window_audit"]["included_in_window"] == 1