import importlib.util
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

import app.config_mgmt as config_mgmt_package  # noqa: E402
from app.core import config_container as config_container_module  # noqa: E402

spec = importlib.util.spec_from_file_location(
    "config_validate", PROJECT_ROOT / "scripts" / "config_validate.py"
)
config_validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(config_validate)


class Result:
    is_valid = True
    errors = []
    summary = "✅ 全部通过"


def test_quick_mode_dispatch(monkeypatch):
    monkeypatch.setattr(config_validate, "run_quick_validation", lambda: 7)
    assert config_validate.main(["--quick"]) == 7


def test_quick_mode_validates_yaml_and_required_keys(tmp_path, monkeypatch):
    yaml_path = tmp_path / "unified_config.yaml"
    yaml_path.write_text("llm:\n  enabled: true\n", encoding="utf-8")

    class Validator:
        def __init__(self, config, strict_mode):
            assert strict_mode is True

        def validate_all(self):
            return Result()

    monkeypatch.setattr(config_validate, "YAML_PATH", yaml_path)
    monkeypatch.setattr(config_mgmt_package, "ConfigValidator", Validator)
    monkeypatch.setattr(config_container_module, "get_config", lambda: object())
    assert config_validate.run_quick_validation() == 0
