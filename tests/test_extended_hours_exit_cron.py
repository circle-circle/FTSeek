import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.extended_hours_exit_cron import collector_command


def test_collector_command_selects_session_specific_script():
    assert collector_command("premarket")[1].endswith("premarket_exit_shadow.py")
    assert collector_command("afterhours")[1].endswith("afterhours_exit_shadow.py")
    assert collector_command(None) is None
    assert collector_command("overnight") is None
