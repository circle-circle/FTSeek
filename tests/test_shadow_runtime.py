from __future__ import annotations

import json

from scripts import shadow_runtime as runtime


def test_env_flag_defaults_false_and_reads_exact_key(tmp_path, monkeypatch):
    env = tmp_path / ".env.us"
    env.write_text("OTHER=1\nFLAG=true\n")
    monkeypatch.delenv("FLAG", raising=False)
    monkeypatch.delenv("MISSING", raising=False)
    assert runtime.env_flag("FLAG", env_file=env) is True
    assert runtime.env_flag("MISSING", env_file=env) is False
    assert runtime.env_flag("OTHER", env_file=tmp_path / "missing") is False
    env.write_text("FLAG=not-a-boolean\n")
    assert runtime.env_flag("FLAG", env_file=env) is False


def test_env_process_value_wins_and_false_is_explicit(tmp_path, monkeypatch):
    env = tmp_path / ".env.us"
    env.write_text("FLAG=true\n")
    monkeypatch.setenv("FLAG", "0")
    assert runtime.env_flag("FLAG", env_file=env) is False


def test_atomic_and_immutable_writes(tmp_path):
    latest = tmp_path / "latest.json"
    runtime.atomic_write_json(latest, {"b": 2, "a": 1})
    runtime.atomic_write_json(latest, {"a": 3})
    assert json.loads(latest.read_text()) == {"a": 3}
    assert not list(tmp_path.glob(".latest.json.*"))
    evidence = tmp_path / "evidence.json"
    runtime.write_json_new(evidence, {"x": 1})
    try:
        runtime.write_json_new(evidence, {"x": 2})
    except FileExistsError:
        pass
    else:
        raise AssertionError("immutable evidence must not be overwritten")
    assert json.loads(evidence.read_text()) == {"x": 1}
    assert runtime.sha256_json({"b": 2, "a": 1}) == runtime.sha256_json({"a": 1, "b": 2})
