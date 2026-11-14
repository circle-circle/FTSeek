"""RFC-207 §2 · EventContext record-only 影子 — 单测钉子。

守五件事：
  1. disabled 零副作用：除读 flag 外不碰 DB / 不建目录 / 不落文件 / 不调 LLM；
  2. 付费闸：env flag + `--execute` 两者缺一即零付费，dry-run 不落盘；
  3. 幂等：相同 input_hash 不重复调用 LLM，latest 指向同一不可变 run；
  4. 不可覆盖 + 失败关闭：旧 run 文件永不改写，无效旧 run 不再付费重试；
  5. 未来拒绝：as-of / ET 日 / 事件 / raw checkpoint 的未来证据均不得进入简报；
  6. 评估器只读且只有 OK/ALREADY_CURRENT/DISABLED/NO_DATA/ERROR 五态，无 PnL。
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts import da_event_context_shadow as shadow  # noqa: E402
from scripts import da_event_context_shadow_eval as evaluator  # noqa: E402

NOW = datetime(2026, 11, 13, 18, 0, 0, tzinfo=timezone.utc)
DATE = "2026-11-12"
AS_OF = "2026-11-12T21:00:00+00:00"
SAVED_AT = "2026-11-12T13:24:35+00:00"


# ---------------------------------------------------------------------------
# 最小只读输入（tmp_path 内，绝不指向生产 data/）
# ---------------------------------------------------------------------------


def _init_event_db(path: Path) -> None:
    conn = sqlite3.connect(path)
    try:
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
        conn.execute(
            """CREATE TABLE source_health (
                source_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                latency_ms INTEGER,
                last_success_at TEXT,
                last_error TEXT
            )"""
        )
        rows = [
            (
                "EV-NVDA-1", "REUTERS", "T1", "https://example.com/nvda-guidance",
                "GUIDANCE", "2026-11-12T12:00:00+00:00",
                "Nvidia raises data centre outlook", "NVDA sees stronger demand.",
                "{}", "CONFIRMED", 0.9, "v1",
                "2026-11-12T13:05:00+00:00", 3900000,
                json.dumps({"materiality": 0.6, "entities": {"symbols": ["NVDA"]}}),
            ),
            (
                "EV-AMD-1", "REUTERS", "T1", "https://example.com/amd-launch",
                "PRODUCT", "2026-11-12T14:00:00+00:00",
                "AMD unveils new accelerator", "AMD targets AI inference.",
                "{}", "CONFIRMED", 0.8, "v1",
                "2026-11-12T14:05:00+00:00", 300000,
                json.dumps({"materiality": 0.5, "entities": {"symbols": ["AMD"]}}),
            ),
            (
                # 未来事件：first_seen_at > as_of，必须被时间窗排除。
                "EV-FUTURE", "REUTERS", "T1", "https://example.com/future",
                "MACRO", "2026-11-12T22:30:00+00:00",
                "Future-only headline", "Should never appear in the brief.",
                "{}", "CONFIRMED", 0.9, "v1",
                "2026-11-12T23:00:00+00:00", 0,
                json.dumps({"materiality": 0.9, "entities": {"symbols": ["NVDA"]}}),
            ),
        ]
        conn.executemany(
            "INSERT INTO canonical_events VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows,
        )
        conn.execute(
            "INSERT INTO source_health VALUES (?,?,?,?,?)",
            ("REUTERS", "OK", 120, "2026-11-12T20:00:00+00:00", None),
        )
        conn.commit()
    finally:
        conn.close()


def _write_checkpoint(
    directory: Path,
    symbol: str,
    *,
    trade_date: str = DATE,
    saved_at: str = SAVED_AT,
    headlines: list[tuple[str, str, str]] | None = None,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    headlines = headlines or [
        (
            f"{symbol} extends rally on AI demand {symbol}",
            f"{symbol} shares move as analysts reiterate constructive view.",
            f"https://example.com/{symbol.lower()}-news",
        )
    ]
    payload = {
        "symbol": f"US.{symbol}",
        "trade_date": trade_date,
        "saved_at": saved_at,
        "schema": "da_checkpoint_v1",
        "collected": {
            "news": [
                {
                    "title": title,
                    "summary": summary,
                    "url": url,
                    "source": "Reuters",
                    "publish_time": "2026-11-12T12:00:00Z",
                }
                for title, summary, url in headlines
            ],
        },
        "user_prompt": "fixture only",
    }
    path = directory / f"US.{symbol}.raw.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


class FakeLLM:
    """零网络假 LLM：主分析 1 次 + 挑战者 1 次；只引用输入 facts 的 event_id。"""

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def __call__(self, system: str, user: str, **kwargs):
        self.calls.append({"system": system, "user": user, "kwargs": kwargs})
        payload = json.loads(user[user.index("{"):])
        if "挑战者" in system:
            return {
                "opposition": "ABSTAIN",
                "detail": "",
                "evidence_ids": [],
                "fatal_flaw": None,
                "counter_transmissions": [],
                "missing_queries": [],
            }
        facts = payload.get("facts") or []
        event_id = facts[0]["event_id"] if facts else "UNAVAILABLE"
        return {
            "transmissions": [{
                "event_ids": [event_id],
                "theme": "ai_demand",
                "sign": "POSITIVE",
                "horizon": "INTRADAY",
                "price_in": "UNKNOWN",
                "invalidation": "guidance withdrawn",
                "causal_chain": "demand -> revenue estimate revision",
                "counter_evidence": "price already reflects guidance",
                "confidence": 0.55,
                "beneficiary_symbols": ["NVDA"],
                "loser_symbols": [],
            }],
            "unknowns": ["fixture_only"],
        }


@pytest.fixture
def sandbox(tmp_path, monkeypatch):
    db = tmp_path / "event_stream.db"
    _init_event_db(db)
    checkpoint_root = tmp_path / "da_checkpoint"
    _write_checkpoint(checkpoint_root / DATE, "NVDA")
    _write_checkpoint(checkpoint_root / DATE, "AMD")
    monkeypatch.setenv(shadow.FLAG, "true")
    monkeypatch.setattr(shadow, "_now_utc", lambda: NOW)
    return SimpleNamespace(
        db=db, checkpoint_root=checkpoint_root, out=tmp_path / "live", tmp=tmp_path,
    )


@pytest.fixture
def fake_llm(monkeypatch):
    fake = FakeLLM()
    monkeypatch.setattr(shadow, "_resolve_llm_call", lambda: fake)
    return fake


def _argv(env, *extra: str) -> list[str]:
    return [
        "--db", str(env.db),
        "--checkpoint-root", str(env.checkpoint_root),
        "--out-dir", str(env.out),
        "--date", DATE,
        "--as-of", AS_OF,
        *extra,
    ]


def _run(capsys, env, *extra: str) -> dict:
    code = shadow.main(_argv(env, *extra))
    captured = capsys.readouterr()
    assert code in (0, 2), captured.err
    return json.loads(captured.out)


def _artifact_files(env) -> list[Path]:
    directory = env.out / DATE
    return sorted(directory.glob("*.json")) if directory.is_dir() else []


# ---------------------------------------------------------------------------
# 1. disabled：零副作用
# ---------------------------------------------------------------------------


def test_disabled_prints_and_has_zero_side_effects(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.FLAG, "false")

    def _boom():
        raise AssertionError("LLM must not be resolved while disabled")

    monkeypatch.setattr(shadow, "_resolve_llm_call", _boom)
    out = tmp_path / "live"
    code = shadow.main([
        "--db", str(tmp_path / "missing.db"),
        "--checkpoint-root", str(tmp_path / "missing_checkpoints"),
        "--out-dir", str(out),
        "--execute",
    ])
    captured = capsys.readouterr()
    assert code == 0
    assert captured.out.strip().startswith("DISABLED")
    assert captured.err == ""
    assert not out.exists()
    assert not (tmp_path / "missing.db").exists()
    assert not (tmp_path / "missing_checkpoints").exists()


def test_disabled_evaluator_ignores_latest(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.FLAG, "false")
    latest = tmp_path / "latest.json"
    latest.write_text("{not json", encoding="utf-8")
    code = evaluator.main(["--latest", str(latest)])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "DISABLED"


# ---------------------------------------------------------------------------
# 2. 付费闸 + 正常生成
# ---------------------------------------------------------------------------


def test_dry_run_is_zero_paid_and_writes_nothing(sandbox, fake_llm, capsys):
    report = _run(capsys, sandbox)
    assert report["status"] == "DRY_RUN"
    assert report["llm_calls"] == 0
    assert report["would_write"].endswith(".json")
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_execute_generates_artifact_and_latest_with_two_calls(
    sandbox, fake_llm, capsys,
):
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "OK", report
    assert report["validation_errors"] == []
    assert report["llm_calls"] == 2
    assert len(fake_llm.calls) == 2
    assert report["event_count"] == 2            # EV-FUTURE 被时间窗排除
    assert report["fact_count"] >= 3             # 2 条 stock + supplemental
    assert report["transmission_count"] == 1

    artifacts = _artifact_files(sandbox)
    assert len(artifacts) == 1
    assert artifacts[0].name == f"{report['input_hash']}.json"
    artifact = json.loads(artifacts[0].read_text(encoding="utf-8"))
    assert artifact["record_only"] is True
    assert artifact["production_writes"] is False
    assert artifact["snapshot"]["event_count"] == 2
    assert artifact["supplemental"]["evidence_hash"]
    assert artifact["supplemental"]["checkpoints"]
    assert "EV-FUTURE" not in json.dumps(artifact["brief"]["facts"])

    latest_path = sandbox.out / "latest.json"
    latest = json.loads(latest_path.read_text(encoding="utf-8"))
    assert latest["schema"] == shadow.REPORT_SCHEMA
    assert latest["status"] == "OK"
    assert latest["input_hash"] == report["input_hash"]
    assert latest["brief_id"] == report["brief_id"]
    assert latest["check_count"] == 1
    assert latest["caveat"] == shadow.CAVEAT


def test_supplemental_limit_is_capped_at_48(sandbox, fake_llm, capsys):
    report = _run(capsys, sandbox, "--execute", "--supplemental-limit", "1")
    assert report["status"] == "OK"
    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["supplemental"]["merged_fact_count"] == 1
    assert artifact["supplemental"]["omitted_fact_count"] == 1
    assert artifact["limits"]["supplemental_limit"] == 1


def test_lookback_beyond_24h_is_rejected(sandbox, fake_llm, capsys):
    code = shadow.main(_argv(sandbox, "--execute", "--lookback-hours", "36"))
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "ERROR"
    assert "24h" in report["error"]
    assert fake_llm.calls == []


def test_llm_exception_is_error_and_writes_no_latest(sandbox, monkeypatch, capsys):
    def _raise(*args, **kwargs):
        raise RuntimeError("upstream down")

    monkeypatch.setattr(shadow, "_resolve_llm_call", lambda: _raise)
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "ERROR"
    assert report["error_type"] == "RuntimeError"
    assert not (sandbox.out / "latest.json").exists()
    assert _artifact_files(sandbox) == []


# ---------------------------------------------------------------------------
# 3. 幂等 / 4. 不可覆盖
# ---------------------------------------------------------------------------


def test_same_input_hash_skips_llm_and_is_already_current(sandbox, fake_llm, capsys):
    first = _run(capsys, sandbox, "--execute")
    assert len(fake_llm.calls) == 2
    artifact = _artifact_files(sandbox)[0]
    original_bytes = artifact.read_bytes()

    second = _run(capsys, sandbox, "--execute")
    assert second["status"] == "ALREADY_CURRENT"
    assert len(fake_llm.calls) == 2              # 零新增付费
    assert second["brief_id"] == first["brief_id"]
    assert second["input_hash"] == first["input_hash"]
    assert second["check_count"] == 2
    assert len(_artifact_files(sandbox)) == 1    # 不新建 run
    assert artifact.read_bytes() == original_bytes

    latest = json.loads((sandbox.out / "latest.json").read_text(encoding="utf-8"))
    assert latest["status"] == "ALREADY_CURRENT"
    assert latest["generated_at"] == first["generated_at"]
    assert latest["brief_id"] == first["brief_id"]


def test_same_day_later_as_of_reuses_content_hash(sandbox, fake_llm, capsys):
    _run(capsys, sandbox, "--execute")
    code = shadow.main([
        "--db", str(sandbox.db),
        "--checkpoint-root", str(sandbox.checkpoint_root),
        "--out-dir", str(sandbox.out),
        "--date", DATE,
        "--as-of", "2026-11-12T22:30:00+00:00",   # 同一 ET 日、证据未变
        "--execute",
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "ALREADY_CURRENT"
    assert len(fake_llm.calls) == 2
    assert len(_artifact_files(sandbox)) == 1


def test_missing_latest_reuses_immutable_artifact_without_paying(
    sandbox, fake_llm, capsys,
):
    first = _run(capsys, sandbox, "--execute")
    artifact = _artifact_files(sandbox)[0]
    original_bytes = artifact.read_bytes()
    (sandbox.out / "latest.json").unlink()

    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "ALREADY_CURRENT"
    assert report["brief_id"] == first["brief_id"]
    assert len(fake_llm.calls) == 2
    assert artifact.read_bytes() == original_bytes
    assert (sandbox.out / "latest.json").is_file()


def test_existing_invalid_artifact_fails_closed_without_overwrite_or_pay(
    sandbox, fake_llm, capsys,
):
    _run(capsys, sandbox, "--execute")
    artifact = _artifact_files(sandbox)[0]
    artifact.write_text('{"input_hash": "corrupt"}', encoding="utf-8")
    original_bytes = artifact.read_bytes()
    (sandbox.out / "latest.json").unlink()

    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "ERROR"
    assert "artifact" in report["error"]
    assert len(fake_llm.calls) == 2              # 不重复付费
    assert artifact.read_bytes() == original_bytes
    assert not (sandbox.out / "latest.json").exists()


# ---------------------------------------------------------------------------
# 5. 未来拒绝
# ---------------------------------------------------------------------------


def test_future_as_of_rejected_before_any_work(sandbox, fake_llm, capsys):
    code = shadow.main([
        "--db", str(sandbox.db),
        "--checkpoint-root", str(sandbox.checkpoint_root),
        "--out-dir", str(sandbox.out),
        "--as-of", "2026-11-14T00:00:00+00:00",
        "--execute",
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "ERROR"
    assert "future" in report["error"]
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_future_date_rejected(sandbox, fake_llm, capsys):
    code = shadow.main([
        "--db", str(sandbox.db),
        "--checkpoint-root", str(sandbox.checkpoint_root),
        "--out-dir", str(sandbox.out),
        "--date", "2026-11-14",
        "--as-of", "2026-11-13T18:00:00+00:00",
        "--execute",
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "ERROR"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_future_event_never_reaches_the_brief(sandbox, fake_llm, capsys):
    report = _run(capsys, sandbox, "--execute")
    latest = json.loads((sandbox.out / "latest.json").read_text(encoding="utf-8"))
    serialized = json.dumps(latest["brief"])
    assert "EV-FUTURE" not in serialized
    assert "Future-only headline" not in serialized
    assert report["event_count"] == 2


def test_future_raw_checkpoint_fails_closed(sandbox, fake_llm, capsys):
    _write_checkpoint(
        sandbox.checkpoint_root / DATE, "NVDA",
        saved_at="2026-11-13T02:00:00+00:00",   # 采集时间晚于 as_of
    )
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "ERROR"
    assert "future" in report["error"].lower()
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_no_checkpoints_is_no_data(sandbox, fake_llm, capsys):
    report = _run(
        capsys,
        SimpleNamespace(
            db=sandbox.db,
            checkpoint_root=sandbox.tmp / "empty_checkpoints",
            out=sandbox.out,
            tmp=sandbox.tmp,
        ),
        "--execute",
    )
    assert report["status"] == "NO_DATA"
    assert report["reason"] == "no_target_checkpoints"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_missing_event_store_is_no_data(sandbox, fake_llm, capsys):
    sandbox.db.unlink()
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "NO_DATA"
    assert report["reason"] == "event_store_missing"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_unregistered_raw_symbol_stays_in_universe_without_supplemental(
    sandbox, fake_llm, capsys,
):
    # TSLA 有当日 raw 档, 但不在 da_event_supplemental 的已注册模式表内:
    # 它必须进入 target_universe, 且不得让 supplemental 抽取 KeyError。
    _write_checkpoint(sandbox.checkpoint_root / DATE, "TSLA")
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "OK"
    assert "TSLA" in report["target_universe"]
    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["supplemental"]["case_symbols"] == ["US.AMD", "US.NVDA"]
    assert "TSLA" in artifact["target_universe"]


def test_mismatched_checkpoint_trade_date_is_excluded(sandbox, fake_llm, capsys):
    _write_checkpoint(
        sandbox.checkpoint_root / DATE, "TSLA", trade_date="2026-11-11",
    )
    report = _run(capsys, sandbox, "--execute")
    assert report["status"] == "OK"
    assert "TSLA" not in report["target_universe"]
    assert any(item["symbol"] == "TSLA" for item in report["excluded_checkpoints"])


def _no_checkpoint_env(sandbox, name: str = "empty_checkpoints") -> SimpleNamespace:
    return SimpleNamespace(
        db=sandbox.db,
        checkpoint_root=sandbox.tmp / name,
        out=sandbox.out,
        tmp=sandbox.tmp,
    )


@pytest.fixture
def fake_watchlist(monkeypatch):
    import app.utils.watchlist_manager as watchlist_manager

    monkeypatch.setattr(
        watchlist_manager, "get_stock_codes",
        lambda: ["US.NVDA", "US.AMD", "US.TSLA"],
    )


# ---------------------------------------------------------------------------
# 7. Pre-DA 兜底：--allow-no-checkpoint
# ---------------------------------------------------------------------------


def test_no_checkpoint_fallback_generates_from_eventstore_with_fake_llm(
    sandbox, fake_llm, fake_watchlist, capsys,
):
    env = _no_checkpoint_env(sandbox)
    db_before = sandbox.db.read_bytes()
    report = _run(capsys, env, "--execute", "--allow-no-checkpoint")

    assert report["status"] == "OK", report
    assert report["target_source"] == "watchlist"
    assert report["supplemental_status"] == "disabled_no_checkpoint"
    assert report["supplemental_merged_fact_count"] == 0
    assert report["target_universe"] == ["AMD", "NVDA", "TSLA"]
    assert report["event_count"] == 2          # EV-FUTURE 仍被时间窗排除
    assert report["fact_count"] == 2           # 无 supplemental
    assert report["llm_calls"] == 2
    assert len(fake_llm.calls) == 2

    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["target_source"] == "watchlist"
    assert artifact["input_components"]["target_source"] == "watchlist"
    assert artifact["input_components"]["supplemental_status"] == "disabled_no_checkpoint"
    assert artifact["supplemental"]["status"] == "disabled_no_checkpoint"
    assert artifact["supplemental"]["merged_fact_count"] == 0
    assert artifact["supplemental"]["evidence_hash"] is None
    assert artifact["supplemental"]["checkpoints"] is None
    assert artifact["record_only"] is True
    assert artifact["production_writes"] is False
    assert "supplemental_evidence_disabled_no_checkpoint" in artifact["brief"]["unknowns"]

    latest = json.loads((sandbox.out / "latest.json").read_text(encoding="utf-8"))
    assert latest["target_source"] == "watchlist"
    assert latest["supplemental_status"] == "disabled_no_checkpoint"
    assert latest["supplemental_merged_fact_count"] == 0
    assert sandbox.db.read_bytes() == db_before      # 生产 DB 零写入


def test_no_checkpoint_fallback_rerun_is_already_current(
    sandbox, fake_llm, fake_watchlist, capsys,
):
    first = _run(capsys, _no_checkpoint_env(sandbox), "--execute", "--allow-no-checkpoint")
    second = _run(capsys, _no_checkpoint_env(sandbox), "--execute", "--allow-no-checkpoint")
    assert second["status"] == "ALREADY_CURRENT"
    assert second["input_hash"] == first["input_hash"]
    assert second["target_source"] == "watchlist"
    assert second["supplemental_merged_fact_count"] == 0
    assert len(fake_llm.calls) == 2                  # 零新增付费
    assert len(_artifact_files(sandbox)) == 1


def test_no_checkpoint_fallback_dry_run_stays_zero_paid(
    sandbox, fake_llm, fake_watchlist, capsys,
):
    report = _run(capsys, _no_checkpoint_env(sandbox), "--allow-no-checkpoint")
    assert report["status"] == "DRY_RUN"
    assert report["target_source"] == "watchlist"
    assert report["supplemental_status"] == "disabled_no_checkpoint"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_no_checkpoint_fallback_uses_target_symbols_override(
    sandbox, fake_llm, monkeypatch, capsys,
):
    import app.utils.watchlist_manager as watchlist_manager

    def _boom():
        raise AssertionError("watchlist must not be read when override is given")

    monkeypatch.setattr(watchlist_manager, "get_stock_codes", _boom)
    report = _run(
        capsys,
        _no_checkpoint_env(sandbox),
        "--execute", "--allow-no-checkpoint",
        "--target-symbols", " us.nvda , AMD ",
    )
    assert report["status"] == "OK", report
    assert report["target_source"] == "cli_target_symbols"
    assert report["supplemental_status"] == "disabled_no_checkpoint"
    assert report["target_universe"] == ["AMD", "NVDA"]
    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["supplemental"]["merged_fact_count"] == 0


def test_no_checkpoint_fallback_never_uses_previous_day_raw(
    sandbox, fake_llm, fake_watchlist, capsys,
):
    env = _no_checkpoint_env(sandbox)
    _write_checkpoint(
        env.checkpoint_root / DATE, "TSLA",
        trade_date="2026-11-11",                 # 前一日 raw：不得顶替当日证据
        headlines=[("STALE previous day headline", "must not be merged", "https://example.com/stale")],
    )
    report = _run(capsys, env, "--execute", "--allow-no-checkpoint")
    assert report["status"] == "OK", report
    assert report["target_source"] == "watchlist"
    assert report["supplemental_status"] == "disabled_no_checkpoint"
    assert any(
        item["symbol"] == "TSLA" and item["reason"].startswith("trade_date_mismatch")
        for item in report["excluded_checkpoints"]
    )
    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["supplemental"]["merged_fact_count"] == 0
    assert artifact["supplemental"]["checkpoints"] is None
    assert "STALE previous day headline" not in json.dumps(artifact)


def test_no_checkpoint_fallback_respects_as_of_and_excludes_future(
    sandbox, fake_llm, fake_watchlist, capsys,
):
    code = shadow.main([
        "--db", str(sandbox.db),
        "--checkpoint-root", str(_no_checkpoint_env(sandbox).checkpoint_root),
        "--out-dir", str(sandbox.out),
        "--date", DATE,
        "--as-of", "2026-11-12T22:30:00+00:00",
        "--execute", "--allow-no-checkpoint",
    ])
    report = json.loads(capsys.readouterr().out)
    assert code == 0
    assert report["status"] == "OK", report
    assert report["as_of"] == "2026-11-12T22:30:00+00:00"
    latest = json.loads((sandbox.out / "latest.json").read_text(encoding="utf-8"))
    assert latest["as_of"] == "2026-11-12T22:30:00+00:00"
    assert "EV-FUTURE" not in json.dumps(latest["brief"]["facts"])


def test_no_checkpoint_without_flag_stays_no_data(sandbox, fake_llm, capsys):
    report = _run(capsys, _no_checkpoint_env(sandbox), "--execute")
    assert report["status"] == "NO_DATA"
    assert report["reason"] == "no_target_checkpoints"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_empty_watchlist_fallback_is_no_data(sandbox, fake_llm, monkeypatch, capsys):
    import app.utils.watchlist_manager as watchlist_manager

    monkeypatch.setattr(watchlist_manager, "get_stock_codes", lambda: [])
    report = _run(capsys, _no_checkpoint_env(sandbox), "--execute", "--allow-no-checkpoint")
    assert report["status"] == "NO_DATA"
    assert report["reason"] == "no_target_symbols_for_fallback"
    assert report["target_source"] == "watchlist"
    assert fake_llm.calls == []
    assert not sandbox.out.exists()


def test_target_symbols_requires_allow_no_checkpoint(sandbox, fake_llm, capsys):
    code = shadow.main(_argv(sandbox, "--execute", "--target-symbols", "NVDA"))
    report = json.loads(capsys.readouterr().out)
    assert code == 2
    assert report["status"] == "ERROR"
    assert "--allow-no-checkpoint" in report["error"]
    assert fake_llm.calls == []


def test_allow_no_checkpoint_keeps_checkpoint_path_when_raw_exists(
    sandbox, fake_llm, capsys,
):
    report = _run(
        capsys, sandbox, "--execute", "--allow-no-checkpoint",
        "--target-symbols", "TSLA",
    )
    assert report["status"] == "OK", report
    assert report["target_source"] == "checkpoint"
    assert report["supplemental_status"] == "merged"
    assert report["target_override_ignored"] is True
    assert "TSLA" not in report["target_universe"]
    artifact = json.loads(_artifact_files(sandbox)[0].read_text(encoding="utf-8"))
    assert artifact["supplemental"]["merged_fact_count"] >= 1


# ---------------------------------------------------------------------------
# 6. 评估器
# ---------------------------------------------------------------------------


def _eval(tmp_path, capsys, *extra: str) -> tuple[int, dict]:
    code = evaluator.main(["--latest", str(tmp_path / "live" / "latest.json"), *extra])
    return code, json.loads(capsys.readouterr().out)


def test_evaluator_no_data(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv(shadow.FLAG, "true")
    code, report = _eval(tmp_path, capsys)
    assert code == 1
    assert report["status"] == "NO_DATA"


def test_evaluator_ok_and_already_current(sandbox, fake_llm, monkeypatch, capsys):
    monkeypatch.setattr(evaluator, "_now_utc", lambda: NOW)
    first = _run(capsys, sandbox, "--execute")
    code, report = _eval(sandbox.tmp, capsys, "--date", DATE)
    assert code == 0
    assert report["status"] == "OK"
    assert report["event_count"] == first["event_count"]
    assert report["fact_count"] == first["fact_count"]
    assert report["transmission_count"] == first["transmission_count"]
    assert report["validation_errors"] == []
    assert report["age_hours"] == 0.0
    assert report["artifact_exists"] is True
    assert "pnl" not in report and "net_pnl" not in report

    latest_before = (sandbox.out / "latest.json").read_bytes()
    _run(capsys, sandbox, "--execute")           # ALREADY_CURRENT
    code, report = _eval(sandbox.tmp, capsys, "--date", DATE)
    assert code == 0
    assert report["status"] == "ALREADY_CURRENT"
    assert report["checked_age_hours"] == 0.0
    assert (sandbox.out / "latest.json").read_bytes() != latest_before  # 仅元数据更新


def test_evaluator_error_states(sandbox, fake_llm, monkeypatch, capsys):
    monkeypatch.setattr(evaluator, "_now_utc", lambda: NOW)
    _run(capsys, sandbox, "--execute")
    latest_path = sandbox.out / "latest.json"
    original = json.loads(latest_path.read_text(encoding="utf-8"))

    def _write(mutator):
        doc = json.loads(json.dumps(original))
        mutator(doc)
        latest_path.write_text(json.dumps(doc), encoding="utf-8")

    _write(lambda doc: doc.update(generated_at="2026-11-10T00:00:00+00:00"))
    code, report = _eval(sandbox.tmp, capsys)
    assert code == 2 and report["status"] == "ERROR"
    assert any(item.startswith("latest_stale") for item in report["errors"])

    _write(lambda doc: doc.update(generated_at="2026-11-20T00:00:00+00:00"))
    code, report = _eval(sandbox.tmp, capsys)
    assert code == 2 and "generated_at_in_future" in report["errors"]

    _write(lambda doc: doc["brief"].update(validation_errors=["llm_not_executed"]))
    code, report = _eval(sandbox.tmp, capsys)
    assert code == 2 and "llm_not_executed" in report["validation_errors"]

    _write(lambda doc: doc.update(date_et="2026-11-10"))
    code, report = _eval(sandbox.tmp, capsys, "--date", DATE)
    assert code == 2 and any(item.startswith("date_mismatch") for item in report["errors"])

    latest_path.write_text("{not json", encoding="utf-8")
    code, report = _eval(sandbox.tmp, capsys)
    assert code == 2 and report["reason"] == "latest_unreadable"


def test_evaluator_is_read_only(sandbox, fake_llm, monkeypatch, capsys):
    monkeypatch.setattr(evaluator, "_now_utc", lambda: NOW)
    _run(capsys, sandbox, "--execute")
    latest_path = sandbox.out / "latest.json"
    before = latest_path.read_bytes()
    _eval(sandbox.tmp, capsys)
    assert latest_path.read_bytes() == before
    assert len(_artifact_files(sandbox)) == 1