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
    """初生豁免: 注册未满 `_GRACE_DAYS` 天的事项缺产出不报; 满整则必须报。

    `created` 必须**相对今天**算 —— 首版硬编码 `"2026-08-30"` (注释还自述"9/1 视角 <10 天"),
    到 2026-09-09 正好满 10 天豁免期 ⇒ 本例**变红且永不自愈** (日期钉死在过去, 只会越来越旧)。
    现与同文件 `test_stale_file_warned` 的相对写法 (`time.time() - (_STALE_DAYS+2)*86400`) 对齐,
    并把**边界**一并钉住: 原判据是 `days < _GRACE_DAYS` 而非 `<=`, 漂成 `<=` 必须红。
    """
    from datetime import date, timedelta

    today = date.today()
    inside = (today - timedelta(days=sr._GRACE_DAYS - 1)).isoformat()
    edge = (today - timedelta(days=sr._GRACE_DAYS)).isoformat()
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {}))
    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", data_path="data/shadow/no_such_new.jsonl",
                               created=inside)])
    assert sr.check_mounting() == []

    monkeypatch.setattr(sr, "REGISTRY",
                        [_item("t1", data_path="data/shadow/no_such_new.jsonl",
                               created=edge)])
    assert any("产出缺失" in w for w in sr.check_mounting()), (
        f"满 _GRACE_DAYS={sr._GRACE_DAYS} 天整仍被豁免 ⇒ 判据从 `<` 漂成了 `<=`")


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


# ── 2026-09-01 复查收口: 引用判定收紧 (注释/docstring 提及不算挂载) ──

def test_code_only_strips_comments_and_docstrings():
    src = ('# see scripts/x.py\n'
           'MODULE = "scripts/x.py"\n'
           '\n'
           'def f():\n'
           '    """mentions scripts/x.py in doc"""\n'
           '    return MODULE\n')
    out = sr._code_only(src)
    assert "scripts/x.py" in out          # 字符串字面量 (真引用形态) 保留
    assert "see" not in out               # # 注释剥离
    assert "mentions" not in out          # docstring 剥离


def test_code_only_keeps_multiline_call_args():
    # 跨行调用的路径参数仍算代码 (NL 不重置逻辑行)
    src = 'r = subprocess.run(\n    [str(REPO / "scripts/x.py")],\n    capture_output=True,\n)\n'
    assert "scripts/x.py" in sr._code_only(src)


def test_comment_mention_is_not_mounting(monkeypatch):
    # 复查实锤同型: da_glm_shadow.py:165 的注释提及曾掩盖 eval 悬空
    src = "import os\n# 结算时由 scripts/no_such_eval.py 的 is_tainted() 对比\nX = 1\n"
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {"/repo/scripts/a.py": src}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert any("悬空观察" in w for w in sr.check_mounting())


def test_docstring_mention_is_not_mounting(monkeypatch):
    # 复查实锤同型: pipeline.py:8538 的 docstring 提及曾掩盖判罪线无挂载
    src = ('def _mark():\n'
           '    """供 scripts/no_such_eval.py 按臂分组复盘。"""\n'
           '    return 1\n')
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {"/repo/app/playbook/p.py": src}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert any("悬空观察" in w for w in sr.check_mounting())


def test_subprocess_string_arg_is_mounting(monkeypatch):
    src = ('import subprocess\n'
           'r = subprocess.run(\n'
           '    [str(REPO / "scripts/no_such_eval.py")], capture_output=True)\n')
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {"/repo/ops/hermes/x.py": src}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert sr.check_mounting() == []


def test_runner_constant_is_mounting(monkeypatch):
    src = 'EVAL = REPO / "scripts" / "no_such_eval.py"\n'
    monkeypatch.setattr(sr, "_mounted_sources", lambda: ("", {"/repo/ops/hermes/r.py": src}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert sr.check_mounting() == []


# ── 2026-09-01 复查收口: cron 解析面收紧 (注释行不计 / /etc/cron.d 含点号名并入) ──

def test_crontab_comment_line_is_not_mounting(monkeypatch):
    monkeypatch.setattr(sr, "_mounted_sources",
                        lambda: ("# 35 6 15 9 * ... scripts/no_such_eval.py ...\n", {}))
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert any("悬空观察" in w for w in sr.check_mounting())


def test_cron_surface_includes_dotfiles(tmp_path, monkeypatch):
    # 401bedcb 教训: cronie 不忽略含点文件名 — .bak 克隆同样被执行, 必须在解析面内
    monkeypatch.setattr(sr, "_ETC_CRON_D", tmp_path / "cron.d")
    monkeypatch.setattr(sr, "_ETC_CRONTAB", tmp_path / "crontab")
    d = tmp_path / "cron.d"
    d.mkdir()
    (d / "ftseek-tasks.bak_zombie").write_text(
        "* * * * * root venv/bin/python scripts/no_such_eval.py\n")
    (d / "ftseek-tasks").write_text("# only comments\n")
    (tmp_path / "crontab").write_text("")
    texts = sr._cron_surface_texts()
    assert any("no_such_eval.py" in t for t in texts)


def test_etc_cron_d_dotfile_counts_as_mounting(tmp_path, monkeypatch):
    monkeypatch.setattr(sr, "_ETC_CRON_D", tmp_path / "cron.d")
    monkeypatch.setattr(sr, "_ETC_CRONTAB", tmp_path / "crontab")
    d = tmp_path / "cron.d"
    d.mkdir()
    (d / "ftseek-tasks.bak_zombie").write_text(
        "* * * * * root venv/bin/python scripts/no_such_eval.py\n")
    (tmp_path / "crontab").write_text("# empty\n")
    monkeypatch.setattr(sr, "_source_files", lambda: {})
    monkeypatch.setattr(sr, "REGISTRY", [_item("t1", eval_script="scripts/no_such_eval.py")])
    assert sr.check_mounting() == []


# ── 2026-09-01 复查收口: where: 计数口径 ──

def test_count_mode_where_filters_rows(tmp_path):
    import json
    p = tmp_path / "x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in [
        {"run_mode": "live"}, {"run_mode": "live"}, {"run_mode": "test"}]) + "\n")
    it = _item("t1", data_path=str(p))
    it.count_mode = "where:run_mode=live"
    assert sr._count_samples(it, tmp_path) == 2
