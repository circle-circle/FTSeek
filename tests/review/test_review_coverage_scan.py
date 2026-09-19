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

    report = module.build_report(
        cron_text="30 6 * * 2-6 cd /repo && python scripts/review.py >> logs/review.log 2>&1\n",
        registry_items=[],
        roots=[repo / "app"],
        today="2026-09-19",
    )

    assert report["producers"][0]["id"] == "cron:logs/review.log"
    assert report["producers"][0]["alive"] is True
