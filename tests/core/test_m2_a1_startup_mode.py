import inspect

import pytest

from app.core.trading_mode import validate_startup_mode_config


@pytest.mark.parametrize(
    ("account", "trade_env", "allow", "valid"),
    [
        ("sim", "1", "false", True),
        ("sim", "1", "true", True),
        ("sim", "0", "false", False),
        ("sim", "0", "true", False),
        ("real", "0", "false", False),
        ("real", "0", "true", True),
        ("real", "1", "false", False),
        ("real", "1", "true", False),
    ],
)
def test_startup_mode_matrix(account, trade_env, allow, valid):
    result = validate_startup_mode_config(account, trade_env, allow)
    assert result["valid"] is valid


@pytest.mark.parametrize(
    ("account", "trade_env", "allow", "code"),
    [
        ("paper", "1", "false", "INVALID_AUTO_TRADE_ACCOUNT"),
        ("sim", "sandbox", "false", "INVALID_TRADE_ENV"),
        ("sim", "1", "yes", "INVALID_ALLOW_REAL_TRADING"),
    ],
)
def test_invalid_values_are_rejected(account, trade_env, allow, code):
    result = validate_startup_mode_config(account, trade_env, allow)
    assert result["valid"] is False
    assert code in {error["code"] for error in result["errors"]}


def test_main_service_db_override_is_rejected():
    result = validate_startup_mode_config("sim", "1", "false", "/tmp/other.db")
    assert result["valid"] is False
    assert "FTSEEK_DB_FILE_OVERRIDE" in {error["code"] for error in result["errors"]}


def test_startup_snapshot_reads_env_file_once(tmp_path, monkeypatch):
    from app.core import trading_mode

    env_file = tmp_path / ".env.us"
    env_file.write_text(
        "AUTO_TRADE_ACCOUNT=real\nTRADE_ENV=0\nALLOW_REAL_TRADING=true\n"
    )
    monkeypatch.setattr(trading_mode, "_ENV_FILE", env_file)
    real_open = open
    calls = []

    def count_open(path, *args, **kwargs):
        calls.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(trading_mode, "open", count_open, raising=False)
    snapshot = trading_mode.read_startup_mode_snapshot()

    assert len(calls) == 1
    assert snapshot["AUTO_TRADE_ACCOUNT"] == "real"
    assert snapshot["TRADE_ENV"] == "0"
    assert snapshot["ALLOW_REAL_TRADING"] == "true"


def test_futu_trade_env_is_diagnostic_only(monkeypatch):
    import main
    from app.core import trading_mode

    values = {
        "AUTO_TRADE_ACCOUNT": "sim",
        "TRADE_ENV": "1",
        "ALLOW_REAL_TRADING": "false",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "0",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: values)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "false")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)
    monkeypatch.setenv("FUTU_TRADE_ENV", "0")

    result = main._validate_startup_mode_or_exit()
    assert result["valid"] is True
    assert result["broker_route"] == "sim"
    assert result["db_route"] == "sim"


def test_invalid_snapshot_exits(monkeypatch):
    import main
    from app.core import trading_mode

    values = {
        "AUTO_TRADE_ACCOUNT": "real",
        "TRADE_ENV": "1",
        "ALLOW_REAL_TRADING": "true",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: values)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)

    with pytest.raises(SystemExit) as exc:
        main._validate_startup_mode_or_exit()
    assert exc.value.code == 1


def test_legacy_global_real_flip_is_blocked_before_scheduler(monkeypatch):
    import main
    from app.core import trading_mode
    import app.push

    values = {
        "AUTO_TRADE_ACCOUNT": "real",
        "TRADE_ENV": "0",
        "ALLOW_REAL_TRADING": "true",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: values)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)
    monkeypatch.delenv("FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP", raising=False)
    pushed = []
    monkeypatch.setattr(
        app.push,
        "push_text_safe",
        lambda *args, **kwargs: pushed.append((args, kwargs)) or True,
    )

    with pytest.raises(SystemExit) as exc:
        main._validate_startup_mode_or_exit()

    assert exc.value.code == 1
    assert pushed


def test_legacy_global_flip_requires_explicit_override(monkeypatch):
    import main
    from app.core import trading_mode

    values = {
        "AUTO_TRADE_ACCOUNT": "real",
        "TRADE_ENV": "0",
        "ALLOW_REAL_TRADING": "true",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: values)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.setenv("FTSEEK_ALLOW_LEGACY_GLOBAL_FLIP", "1")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)

    result = main._validate_startup_mode_or_exit()

    assert result["valid"] is True
    assert result["broker_route"] == "real"


def test_external_broker_override_is_checked_against_file_db_route(monkeypatch):
    import main
    from app.core import trading_mode

    snapshot = {
        "AUTO_TRADE_ACCOUNT": "sim",
        "TRADE_ENV": "1",
        "ALLOW_REAL_TRADING": "false",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: snapshot)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "real")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "true")
    monkeypatch.delenv("FTSEEK_DB_FILE", raising=False)

    with pytest.raises(SystemExit):
        main._validate_startup_mode_or_exit()


def test_external_db_override_blocks_main_startup(monkeypatch):
    import main
    from app.core import trading_mode

    snapshot = {
        "AUTO_TRADE_ACCOUNT": "sim",
        "TRADE_ENV": "1",
        "ALLOW_REAL_TRADING": "false",
        "FTSEEK_DB_FILE": "",
        "FUTU_TRADE_ENV": "",
    }
    monkeypatch.setattr(trading_mode, "read_startup_mode_snapshot", lambda: snapshot)
    monkeypatch.setenv("AUTO_TRADE_ACCOUNT", "sim")
    monkeypatch.setenv("ALLOW_REAL_TRADING", "false")
    monkeypatch.setenv("FTSEEK_DB_FILE", "/tmp/other.db")

    with pytest.raises(SystemExit):
        main._validate_startup_mode_or_exit()


def test_startup_gate_precedes_opend_and_di():
    import main

    source = inspect.getsource(main._run_new_scheduler)
    assert source.index("_validate_startup_mode_or_exit()") < source.index("socket.socket")
    assert source.index("_validate_startup_mode_or_exit()") < source.index("get_di_container")
    assert "_db_live != _broker_live" not in source
