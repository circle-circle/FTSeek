from pathlib import Path

import pytest

from app.web_dashboard import mode_switch_service as mode_service


@pytest.fixture
def isolated_env_file(tmp_path, monkeypatch):
    env_file = tmp_path / ".env.us"
    lock_file = tmp_path / ".env.us.lock"
    monkeypatch.setattr(mode_service, "ENV_FILE", env_file)
    monkeypatch.setattr(mode_service, "_ENV_LOCK_FILE", lock_file)
    return env_file


def _read(path: Path):
    values = {}
    for line in path.read_text().splitlines():
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key] = value
    return values


def test_write_env_keys_uses_one_replace(isolated_env_file, monkeypatch):
    isolated_env_file.write_text("AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\nKEEP=yes\n")
    real_replace = mode_service.os.replace
    calls = []

    def replace_once(source, target):
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(mode_service.os, "replace", replace_once)
    mode_service.write_env_keys({"AUTO_TRADE_ACCOUNT": "real", "TRADE_ENV": "0"})

    assert len(calls) == 1
    assert _read(isolated_env_file) == {
        "AUTO_TRADE_ACCOUNT": "real",
        "TRADE_ENV": "0",
        "KEEP": "yes",
    }


def test_write_failure_preserves_complete_old_snapshot(isolated_env_file, monkeypatch):
    original = "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\n"
    isolated_env_file.write_text(original)

    def fail_replace(source, target):
        raise OSError("replace failed")

    monkeypatch.setattr(mode_service.os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        mode_service.write_env_keys({"AUTO_TRADE_ACCOUNT": "real", "TRADE_ENV": "0"})

    assert isolated_env_file.read_text() == original
    assert list(isolated_env_file.parent.glob(".env.us.*.tmp")) == []


def test_batch_write_collapses_duplicate_mode_keys(isolated_env_file):
    isolated_env_file.write_text(
        "AUTO_TRADE_ACCOUNT=sim\nAUTO_TRADE_ACCOUNT=real\nTRADE_ENV=0\n"
    )
    mode_service.write_env_keys({"AUTO_TRADE_ACCOUNT": "sim", "TRADE_ENV": "1"})

    text = isolated_env_file.read_text()
    assert text.count("AUTO_TRADE_ACCOUNT=") == 1
    assert _read(isolated_env_file)["TRADE_ENV"] == "1"


def test_switch_to_sim_repairs_half_flip_atomically(isolated_env_file, monkeypatch):
    isolated_env_file.write_text(
        "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=0\nALLOW_REAL_TRADING=false\nLIVE_CONSERVATIVE=0\n"
    )
    monkeypatch.setattr(mode_service, "_backup_env_file", lambda: None)
    monkeypatch.setattr(
        mode_service,
        "_restart_ftseek_us",
        lambda: {"success": True, "message": " restarted"},
    )
    real_replace = mode_service.os.replace
    calls = []

    def replace_once(source, target):
        calls.append((source, target))
        real_replace(source, target)

    monkeypatch.setattr(mode_service.os, "replace", replace_once)
    result = mode_service.switch_mode("simulate")

    values = _read(isolated_env_file)
    assert result["success"] is True
    assert result.get("no_op") is not True
    assert len(calls) == 1
    assert values["AUTO_TRADE_ACCOUNT"] == "sim"
    assert values["TRADE_ENV"] == "1"
    assert values["ALLOW_REAL_TRADING"] == "false"


def test_switch_to_live_writes_one_complete_snapshot(isolated_env_file, monkeypatch):
    isolated_env_file.write_text(
        "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\nALLOW_REAL_TRADING=true\n"
        "LIVE_CONSERVATIVE=1\nplaybook.live_trial=true\n"
        "REAL_ACCOUNT_ID=123\nFUTU_UNLOCK_PWD_MD5=secret\n"
    )
    monkeypatch.setattr(mode_service, "_backup_env_file", lambda: None)
    monkeypatch.setattr(
        mode_service,
        "_restart_ftseek_us",
        lambda: {"success": True, "message": " restarted"},
    )
    import app.playbook.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_check_graduation_gate", lambda: {"graduated": True})
    real_write = mode_service.write_env_keys
    calls = []

    def capture(updates):
        calls.append(dict(updates))
        real_write(updates)

    monkeypatch.setattr(mode_service, "write_env_keys", capture)
    result = mode_service.switch_mode("live_normal", confirm="CONFIRM")

    assert result["success"] is True
    assert calls == [{
        "AUTO_TRADE_ACCOUNT": "real",
        "TRADE_ENV": "0",
        "ALLOW_REAL_TRADING": "true",
        "LIVE_CONSERVATIVE": "0",
        "playbook.live_trial": "false",
    }]
    assert _read(isolated_env_file).items() >= calls[0].items()


def test_live_switch_requires_existing_permission(isolated_env_file, monkeypatch):
    original = (
        "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\nALLOW_REAL_TRADING=false\n"
        "REAL_ACCOUNT_ID=123\nFUTU_UNLOCK_PWD_MD5=secret\n"
    )
    isolated_env_file.write_text(original)
    monkeypatch.setattr(mode_service, "_backup_env_file", lambda: pytest.fail("must not back up"))

    result = mode_service.switch_mode("live_conservative", confirm="CONFIRM")

    assert result["success"] is False
    assert "ALLOW_REAL_TRADING" in result["message"]
    assert isolated_env_file.read_text() == original


@pytest.mark.parametrize(
    ("account", "expected"),
    [
        ("sim", {"AUTO_TRADE_ACCOUNT": "sim", "TRADE_ENV": "1"}),
        (
            "real",
            {
                "AUTO_TRADE_ACCOUNT": "real",
                "TRADE_ENV": "0",
                "ALLOW_REAL_TRADING": "true",
            },
        ),
    ],
)
def test_set_auto_account_uses_atomic_snapshot(isolated_env_file, monkeypatch, account, expected):
    isolated_env_file.write_text(
        "AUTO_TRADE_ACCOUNT=sim\nTRADE_ENV=1\nALLOW_REAL_TRADING=true\n"
    )
    import app.playbook.pipeline as pipeline
    monkeypatch.setattr(pipeline, "_check_graduation_gate", lambda: {"graduated": True})
    calls = []
    real_write = mode_service.write_env_keys

    def capture(updates):
        calls.append(dict(updates))
        real_write(updates)

    monkeypatch.setattr(mode_service, "write_env_keys", capture)
    service = mode_service.ModeSwitchService()
    result = service.set_auto_account(account)

    assert result["success"] is True
    assert calls == [expected]
    assert _read(isolated_env_file).items() >= expected.items()
