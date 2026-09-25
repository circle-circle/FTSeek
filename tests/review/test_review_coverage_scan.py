import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "review_coverage_scan.py"


def _load():
    spec = importlib.util.spec_from_file_location("review_coverage_scan", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def test_skill_corpus_is_visible_and_test_refs_do_not_mark_producer_alive(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    hermes = tmp_path / ".hermes"
    _write(repo / "app" / "consumer.py", 'PATH = "data/visible.jsonl"\n')
    _write(repo / "tests" / "only_test.py", 'PATH = "data/dead.jsonl"\n')
    _write(hermes / "skills" / "probe" / "reader.py", 'PATH = "data/skill.jsonl"\n')

    report = module.build_report(
        cron_text="",
        registry_items=[
            {"id": "visible", "data_path": "data/visible.jsonl", "eval_script": None},
            {"id": "dead", "data_path": "data/dead.jsonl", "eval_script": None},
            {"id": "skill", "data_path": "data/skill.jsonl", "eval_script": None},
        ],
        roots=[repo / "app", repo / "scripts", repo / "ops", repo / "tests",
               hermes / "scripts", hermes / "skills"],
        today="2026-09-19",
    )

    dead = {row["id"] for row in report["dead_producers"]}
    assert "visible" not in dead
    assert "skill" not in dead
    assert "dead" in dead
    assert "hermes/skills" in report["corpus"]["roots"]
    assert report["corpus"]["rules"] == {
        "file_suffixes": [".py", ".sh"],
        "excluded_path_parts": ["__pycache__"],
        "selection": "regular files only",
    }
    assert report["push_exit_census_meta"]["categories_are_overlapping"] is True


def test_push_exit_census_separates_hub_notifier_and_curl(tmp_path):
    module = _load()
    hub = tmp_path / "app" / "weekly.py"
    direct = tmp_path / "scripts" / "direct.py"
    shell = tmp_path / "ops" / "fallback.sh"
    _write(hub, "from app.push import get_push_hub\n")
    _write(direct, "from app.notifier.feishu import send_text\n")
    _write(shell, "curl -sf https://example.invalid/webhook\n")

    rows = module.push_exit_census([hub, direct, shell])
    by_path = {str(path): row for path, row in rows.items()}

    assert by_path[str(hub)]["categories"] == ["app.push"]
    assert by_path[str(direct)]["categories"] == ["notifier"]
    assert "curl" in by_path[str(shell)]["categories"]


def test_cron_review_output_is_scanned(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    _write(repo / "app" / "reader.py", 'LOG = "logs/review.log"\n')
    # cron-redirected logs are never read by code; liveness is mtime
    # freshness against the schedule's own max natural interval (+1 tick).
    _write(repo / "logs" / "review.log", "run output\n")

    report = module.build_report(
        cron_text="30 6 * * 2-6 cd /repo && python scripts/review.py >> logs/review.log 2>&1\n"
        .replace("/repo", str(repo)),
        registry_items=[],
        roots=[repo / "app"],
        today="2026-09-19",
    )

    assert report["producers"][0]["id"] == "cron:logs/review.log"
    assert report["producers"][0]["alive"] is True
    assert report["producers"][0]["alive_detail"]["alive_basis"] == "mtime_freshness"


def test_cron_log_liveness_uses_schedule_threshold(tmp_path):
    module = _load()
    repo = tmp_path / "repo"
    log = repo / "logs" / "weekly.log"
    _write(log, "x\n")
    cron_line = f"0 9 * * 6 cd {repo} && python scripts/w.py >> logs/weekly.log 2>&1"

    # fresh -> alive
    alive, detail = module._cron_log_alive(cron_line, "logs/weekly.log")
    assert alive is True
    assert detail["threshold_days"] == 8  # weekly: max gap 7 + 1 tick

    # stale beyond schedule threshold -> dead
    import os as _os
    import time as _time
    old = _time.time() - 10 * 86400
    _os.utime(log, (old, old))
    alive, detail = module._cron_log_alive(cron_line, "logs/weekly.log")
    assert alive is False
    assert detail["age_days"] > detail["threshold_days"]

    # missing file -> dead with explicit note (never silently alive)
    alive, detail = module._cron_log_alive(cron_line, "logs/nope.log")
    assert alive is False
    assert detail["note"] == "file_missing"


def test_cron_threshold_matches_schedule_family():
    import importlib.util as _ilu
    spec = _ilu.spec_from_file_location("rcs", SCRIPT)
    module = _ilu.module_from_spec(spec)
    spec.loader.exec_module(module)
    # trading-day daily `* * 2-6`: longest natural gap = 3d (Fri->Mon) + 1 = 4
    assert module._cron_log_threshold_days("2 6 * * 2-6 cd /r && python s.py >> logs/a.log 2>&1") == 4
    # plain daily
    assert module._cron_log_threshold_days("0 8 * * * cd /r && python s.py >> logs/a.log 2>&1") == 2
    # weekly single day
    assert module._cron_log_threshold_days("0 9 * * 6 cd /r && python s.py >> logs/a.log 2>&1") == 8
