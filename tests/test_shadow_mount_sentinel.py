"""挂载核对哨兵行为级测试 (2026-09-01 建议①, 两例悬空实锤驱动).

红绿纪律: 注入伪 crontab/引用源, 哨兵必须机械给出一致判定 —
未挂载必报 / 在挂必静默 / eval_on_demand 豁免 / 初生期豁免缺失文件 / 超龄必报。
"""
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import app.playbook.shadow_registry as sr  # noqa: E402


def _item(iid, eval_script=None, data_path="data/ftseek_us.db (x)",
          created="2026-01-01", on_demand=False, status=sr.ShadowStatus.OBSERVING):
    return sr.ShadowItem(id=iid, name=iid, status=status, description="",
                         data_path=data_path, eval_script=eval_script,
                         created=created, eval_on_demand=on_demand)


def test_detects_unmounted_eval(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    ws = sr.check_mounting()
    assert any("悬空观察" in w and "t1" in w for w in ws)


def test_silent_when_in_crontab(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources",
                        lambda: ("15 6 * * 2-6 ... scripts/no_such_eval.py ...", {}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert sr.check_mounting() == []


def test_silent_when_referenced_by_ops(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources",
                        lambda: ("", {"/repo/ops/hermes/x.py": "scripts/no_such_eval.py"}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert sr.check_mounting() == []


def test_registry_self_reference_not_counted(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources",
                        lambda: ("", {"/repo/app/playbook/shadow_registry.py":
                                      "scripts/no_such_eval.py"}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert any("悬空观察" in w for w in sr.check_mounting())


def test_eval_on_demand_exempt(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", eval_script="scripts/no_such_eval.py", on_demand=True)])
    assert sr.check_mounting() == []


def test_missing_file_grace_for_new_items(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", data_path="data/shadow/no_such_new.jsonl",
                               created="2026-08-30")])  # 9/1 视角 <10 天
    assert sr.check_mounting() == []


def test_missing_file_warned_after_grace(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", data_path="data/shadow/no_such_old.jsonl")])
    assert any("产出缺失" in w for w in sr.check_mounting())


def test_stale_file_warned(monkeypatch, tmp_path):
    stale = tmp_path / "stale.jsonl"
    stale.write_text("{}")
    old = time.time() - (sr._STALE_DAYS + 2) * 86400
    os.utime(stale, (old, old))
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", data_path=str(stale))])
    assert any("产出超龄" in w for w in sr.check_mounting())


def test_killed_items_ignored(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", eval_script="scripts/no_such_eval.py",
                               status=sr.ShadowStatus.KILLED)])
    assert sr.check_mounting() == []
