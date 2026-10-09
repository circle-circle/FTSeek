# -*- coding: utf-8 -*-
"""data/replay_cache 按龄保留契约 (2026-10-08 运维体检批).

钉四件事:
  1. 只按 mtime 判过期 (新鲜文件不动);
  2. 只扫顶层 *.json (不递归 — 未来子目录须扩面批显式接管);
  3. dry-run (缺省) 只报不删; --apply 才真删且删后汇总不炸;
  4. 目录缺失/负数 days 的 fail 行为 (空列表 / 拒绝, 不抛栈)。
"""
import json
import time

import pytest

from scripts.data_retention import collect_stale, main


def _touch(path, age_days):
    path.write_text("[]", encoding="utf-8")
    stamp = time.time() - age_days * 86400
    import os
    os.utime(path, (stamp, stamp))
    return path


def test_collect_stale_splits_by_mtime(tmp_path):
    stale = _touch(tmp_path / "US_OLD_2026.json", age_days=100)
    fresh = _touch(tmp_path / "US_NEW_2026.json", age_days=10)

    assert collect_stale(tmp_path, days=90) == [stale]
    assert fresh.exists()


def test_collect_stale_only_top_level_json(tmp_path):
    _touch(tmp_path / "US_OLD.json", age_days=100)
    nested_dir = tmp_path / "subdir"
    nested_dir.mkdir()
    _touch(nested_dir / "US_OLD_NESTED.json", age_days=100)
    _touch(tmp_path / "notes.txt", age_days=100)

    assert collect_stale(tmp_path, days=90) == [tmp_path / "US_OLD.json"]


def test_dry_run_does_not_delete(tmp_path, capsys):
    _touch(tmp_path / "US_OLD.json", age_days=100)

    assert main(["--cache-dir", str(tmp_path), "--days", "90"]) == 0
    assert (tmp_path / "US_OLD.json").exists()
    assert "DRY-RUN" in capsys.readouterr().out


def test_apply_deletes_and_summary_survives(tmp_path, capsys):
    old = _touch(tmp_path / "US_OLD.json", age_days=100)
    _touch(tmp_path / "US_NEW.json", age_days=1)

    assert main(["--cache-dir", str(tmp_path), "--days", "90", "--apply"]) == 0
    assert not old.exists()
    assert (tmp_path / "US_NEW.json").exists()
    out = capsys.readouterr().out
    assert "APPLY" in out and "已删除 1 个" in out


def test_missing_dir_returns_empty(tmp_path):
    assert collect_stale(tmp_path / "no_such_dir", days=90) == []


def test_negative_days_rejected(tmp_path):
    with pytest.raises(ValueError, match="days 必须 >= 0"):
        collect_stale(tmp_path, days=-1)
