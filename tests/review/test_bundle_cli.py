import importlib.util
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "review_bundle.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("review_bundle", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeHub:
    def __init__(self, ok=True):
        self.ok = ok
        self.messages = []

    def push(self, message, sync=False):
        assert sync is True
        self.messages.append(message)
        return self.ok


def _db_with_trade():
    from tests.review.test_bundle_builder import _create_db, _insert_trade

    connection = _create_db()
    _insert_trade(
        connection, symbol="US.A", direction="LONG",
        entry_time="2026-09-18T10:00:00-04:00", exit_time="2026-09-18T11:00:00-04:00",
        session="regular", net=25.0,
    )
    return connection


def test_run_writes_atomic_and_idempotent_bundle_and_markdown(tmp_path):
    module = _load_module()
    output_dir = tmp_path / "reviews"
    connection = _db_with_trade()

    first = module.run("2026-09-18", connection, output_dir)
    first_json = (output_dir / "bundle_2026-09-18.json").read_text(encoding="utf-8")
    first_md = (output_dir / "REVIEW_2026-09-18.md").read_text(encoding="utf-8")
    second = module.run("2026-09-18", connection, output_dir)

    assert first["bundle"] == second["bundle"]
    assert first_json == (output_dir / "bundle_2026-09-18.json").read_text(encoding="utf-8")
    assert first_md == (output_dir / "REVIEW_2026-09-18.md").read_text(encoding="utf-8")
    assert first_json == (output_dir / "bundle_latest.json").read_text(encoding="utf-8")
    assert "净收益" in first_md


def test_push_guard_marks_only_after_successful_sync_push(tmp_path):
    module = _load_module()
    connection = _db_with_trade()
    output_dir = tmp_path / "reviews"
    hub = FakeHub(ok=False)

    first = module.run(
        "2026-09-18", connection, output_dir, push=True, hub_factory=lambda: hub
    )
    failed_state = output_dir / "bundle_2026-09-18.push.json"
    assert first["pushed"] is False
    assert not failed_state.exists()

    hub.ok = True
    hub.messages.clear()
    second = module.run(
        "2026-09-18", connection, output_dir, push=True, hub_factory=lambda: hub
    )
    third = module.run(
        "2026-09-18", connection, output_dir, push=True, hub_factory=lambda: hub
    )

    assert second["pushed"] is True
    assert third["pushed"] is "already"
    assert len(hub.messages) == 1
    state = json.loads(failed_state.read_text(encoding="utf-8"))
    assert state["report_date"] == "2026-09-18"
    assert state["pushed_at"]


def test_vault_write_is_opt_in_and_idempotent(tmp_path):
    module = _load_module()
    connection = _db_with_trade()
    output_dir = tmp_path / "reviews"
    vault_dir = tmp_path / "vault"

    module.run("2026-09-18", connection, output_dir, vault_dir=vault_dir)
    first = (vault_dir / "2026-09-18_review.md").read_text(encoding="utf-8")
    module.run("2026-09-18", connection, output_dir, vault_dir=vault_dir)

    assert first == (vault_dir / "2026-09-18_review.md").read_text(encoding="utf-8")


def test_cli_dispatches_arguments(monkeypatch):
    module = _load_module()
    calls = []

    def fake_run(*args, **kwargs):
        calls.append((args, kwargs))
        return {"bundle": "x", "markdown": "y", "pushed": False}

    monkeypatch.setattr(module, "run", fake_run)

    exit_code = module.main(
        ["--date", "2026-09-18", "--period", "day", "--push", "--vault"]
    )

    assert exit_code == 0
    assert calls[0][0][0] == "2026-09-18"
    assert calls[0][1]["push"] is True
    assert calls[0][1]["vault_dir"] is not None
