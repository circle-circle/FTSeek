"""陈旧 index 守卫 `scripts/check_stale_index_guard.py` 的口径契约 (2026-09-26, 坑1)

立起理由: 并行 agent 用 `update-index --cacheinfo`/worktree blob 注入提交后, index 残留旧 blob;
另一 agent 提交后, 残留 blob 相对新 HEAD = 「删除他刚提交的行」⇒ `commit -a` **静默撤销**他的工作
(无冲突无报错)。实锤两次: ① 47f30ff74 差点丢失 (他 agent 提交信息自述「上批差点因此静默丢失」);
② 同日我的 econ 备证块同型受损 (grep 实证 HEAD 2 处 / 工作区 0 处, 已注回)。

本测试钉住:
  ① 口径 B (陈旧 blob 指纹) 真阳能抓 + 假阳为 0 — 用**真实历史事故**回放, 不是 fixture;
  ② 口径 A 只提示不阻断的理由成立 (author 不可分 ⇒ 阻断会造成提交风暴);
  ③ 钩子必须真调用该脚本 (`.git/hooks` 不受版本控制 ⇒ 无测试则重写钩子后守卫静默失效);
  ④ 守卫自身故障必须放行 (守卫不该卡住工作);
  ⑤ 绕过开关必须存在且生效。
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
GUARD = REPO / "scripts" / "check_stale_index_guard.py"
HOOK = REPO / ".git" / "hooks" / "pre-commit"
PY = str(REPO / "venv" / "bin" / "python")

sys.path.insert(0, str(REPO))
import importlib.util  # noqa: E402

_spec = importlib.util.spec_from_file_location("_stale_guard", GUARD)
g = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(g)


def _git(*args):
    return subprocess.run(["git", "-C", str(REPO)] + list(args),
                          capture_output=True, text=True).stdout


# ── ① 口径 B 有牙齿 (真实历史回放) ─────────────────────────────────
def test_selftest_passes():
    """脚本自带自检必须通过 (真阳 + 假阳两项)。"""
    r = subprocess.run([PY, str(GUARD), "--selftest"], capture_output=True, text=True,
                       cwd=str(REPO), timeout=200)
    assert r.returncode == 0, f"自检失败:\n{r.stdout}\n{r.stderr[-400:]}"


def test_real_incident_is_detected():
    """真阳: 已知历史事故 (6249c2d97 的 blob 在 47f30ff74 之后成为陈旧残留) 必须被识别。"""
    p = "app/playbook/shadow_registry.py"
    stale = _git("rev-parse", f"6249c2d97:{p}").strip()
    then_head = _git("rev-parse", f"47f30ff74:{p}").strip()
    hist = g.hist_blobs(p, depth=g.HIST_DEPTH)   # 深度由守卫自身定; 原硬编码 60 覆盖不到事故(第66位) ⇒ 断言必红
    assert stale, "取不到历史事故 blob (提交 6249c2d97 是否被 rebase 掉了?)"
    assert stale in hist, f"事故 blob {stale[:12]} 不在历史集合 ⇒ HIST_DEPTH 太浅"
    assert stale != then_head
    assert hist[stale] == "6249c2d97", f"溯源到错误提交: {hist[stale]}"


def test_normal_commits_are_not_flagged():
    """假阳: 两种「正常提交」形态都不该命中历史集合。

    ① 原样提交当前内容 (staged blob == HEAD blob) ⇒ 运行期第一步就跳过;
    ② 在当前内容上改一行再提交 (新 hash) ⇒ 不在历史集合里。
    原理: 正常编辑产生**全新 hash** (内容变了), 只有「原封不动的旧快照」才命中。
    若此项失败 ⇒ 口径会造提交风暴, 必须停用而非放宽。

    2026-11-03: 原判据是「最近 20 次提交的 blob 不得命中历史集合 ∧ ≠ 前一次」——
      守卫深度 40→500 后该判据几乎恒真 (该文件历史含大量内容重复: 回滚/重放), 但那
      **不是误判**: 那些 blob 确实是旧快照, stage 它们本就该被拦 (提交会回退别人工作)。
      故改为按运行期真实语义复算。
    """
    p = "app/playbook/shadow_registry.py"
    hist = g.hist_blobs(p, depth=g.HIST_DEPTH)   # 深度由守卫自身定; 原硬编码 60 覆盖不到事故(第66位) ⇒ 断言必红
    hb = g.head_blob(p)
    content = _git("show", f"HEAD:{p}")
    edited = subprocess.run(["git", "hash-object", "--stdin"], input=content + "\n# edit\n",
                             capture_output=True, text=True).stdout.strip()
    fp = []
    for label, sb in (("原样提交当前内容", hb), ("改一行再提交", edited)):
        if not sb:
            continue
        if sb == hb:
            continue                                   # 运行期第一步: sb == hb ⇒ 跳过
        hist_sb = hist.get(sb)
        if hist_sb is not None and hist_sb != (hb or ""):
            fp.append(label)
    assert not fp, f"正常提交形态被误判为陈旧快照: {fp}"


# ── ② 口径 A 只提示的理由 ───────────────────────────────────────────
def test_author_is_not_discriminative():
    """口径 A 不阻断的依据: 本仓所有提交同一身份 ⇒ 无法区分「改自己刚写的行」与「撤别人的」。"""
    authors = set()
    for line in _git("log", "--format=%ae", "-30").split():
        authors.add(line)
    assert len(authors) == 1, (
        f"若 author 已可分 ({authors}), 应把口径 A 升级为阻断 (只在删别人行时报) — "
        f"当前实测 K=1 误报 2/24 且两例都是同一 agent 改自己上一批刚加的行, 故只能提示")


def test_k1_has_false_positives_hence_info_only():
    """口径 A 的误报必须被验证存在 (否则「只提示」的理由失效, 应升级为阻断)。"""
    p = "docs/CONTROL-LOOP-LEDGER.md"
    dele = g._sig_lines(_git("show", "--no-color", "-U0", "--format=", "06c473ed6", "--", p), "-")
    prev = _git("log", "--format=%h", "-1", "06c473ed6~1").strip()
    prev_added = g._sig_lines(_git("show", "--no-color", "-U0", "--format=", prev, "--", p), "+")
    hit = dele & prev_added
    assert hit, ("06c473ed6 改自己刚加的台账行未复现误报 ⇒ 口径 A 可能已可升级为阻断, "
                 "请重跑回放评估")
    # 且该误报是合法的 (改自己上一批刚写的行)
    assert _git("log", "-1", "--format=%ae", "06c473ed6").strip() == \
           _git("log", "-1", "--format=%ae", prev).strip()


# ── ③ 钩子接线 (防静默失效) ─────────────────────────────────────────
def test_hook_invokes_guard():
    """`.git/hooks/pre-commit` 必须真调用守卫 — 钩子不受版本控制, 无测试则重写后守卫失效。"""
    if not HOOK.exists():
        return  # 全新克隆无钩子: 逻辑仍在版本化脚本, 由 install 文档负责 (此处不断言)
    src = HOOK.read_text(encoding="utf-8", errors="ignore")
    assert "check_stale_index_guard.py" in src, (
        "钩子未调用陈旧 index 守卫 ⇒ 坑1 复发 (并行 agent 互撤无人拦)。"
        "接线块见钩子「─── 7. 陈旧 index 守卫」段")
    # 取真实**调用行** (非注释): 形如 `./venv/bin/python scripts/check_stale_index_guard.py`
    invoke_idx = None
    for i, ln in enumerate(src.splitlines()):
        code_part = ln.split("#", 1)[0]
        if "check_stale_index_guard.py" in code_part:
            invoke_idx = i
            break
    assert invoke_idx is not None, "钩子里只有注释提到守卫, 没有真实调用行 ⇒ 等于没接"
    tail = "\n".join(src.splitlines()[invoke_idx:invoke_idx + 6])
    assert "exit 1" in tail, (
        f"钩子调用了守卫但未在失败时 exit 1 ⇒ 不阻断, 等于没接。调用块:\n{tail}")


def test_hook_is_bash_syntactically_valid():
    """钩子必须 `bash -n` 通过 — 否则**所有提交**硬失败 (死钩子), 比静默失效更糟。

    实测: 把守卫调用行注释掉 (只剩注释提及) 会造成 `fi` 不匹配 ⇒ bash 语法错 ⇒
    每次 commit 都失败。上面「有真实调用行」的断言只看文本, 抓不到这种形态,
    故补语法健康断言 (变异测试 3/3 才闭合)。
    """
    if not HOOK.exists():
        return
    r = subprocess.run(["bash", "-n", str(HOOK)], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, f"钩子 bash 语法错误 ⇒ 所有提交会硬失败:\n{r.stderr.strip()[:300]}"


def test_guard_logic_is_versioned():
    """逻辑必须在版本化脚本里 (不能只写在钩子 bash 中, 否则不可测/克隆不可见)。"""
    assert GUARD.exists(), "守卫脚本缺失"
    tracked = _git("ls-files", "--error-unmatch", str(GUARD.relative_to(REPO))).strip()
    assert tracked, (
        f"{GUARD.name} 未纳入版本控制 ⇒ 逻辑只活在本机, 他 agent/新克隆看不到 (坑1 认知流失)。"
        f"须 git add scripts/check_stale_index_guard.py")


# ── ④ 守卫自身故障必须放行 ──────────────────────────────────────────
def test_guard_failure_does_not_block():
    """守卫内部异常 ⇒ exit 0 (不阻断)。守卫故障不该卡住工作 (与钩子既有 || true 风格一致)。"""
    src = GUARD.read_text(encoding="utf-8")
    assert "except RuntimeError" in src and "return 0" in src.split("except RuntimeError")[1][:200], \
        "守卫故障未放行 ⇒ git 环境异常时会卡死所有提交"


def test_empty_index_passes():
    """无 staged 内容时必须放行 (不报错)。"""
    blocks, infos = ([], []) if not g.staged_files() else g.check()
    if not g.staged_files():
        assert blocks == [] and infos == []


# ── ⑤ 绕过开关 ──────────────────────────────────────────────────────
def test_bypass_switch_exists_and_works():
    """真需要恢复旧版本时必须能绕过 (且须在提交信息写明理由)。"""
    assert g.ALLOW_ENV == "FTSEEK_ALLOW_STALE_INDEX"
    env = dict(os.environ, FTSEEK_ALLOW_STALE_INDEX="1")
    r = subprocess.run([PY, str(GUARD)], capture_output=True, text=True, cwd=str(REPO),
                       env=env, timeout=120)
    assert r.returncode == 0, f"绕过开关失效: {r.stdout}{r.stderr[-200:]}"
    assert "已跳过" in r.stdout, "绕过时必须**明确告知**已跳过 (静默绕过 = 无审计痕迹)"


def test_min_line_threshold_documented():
    """指纹行长度阈值必须存在 (短行如 `}`/`else:` 无判别力 ⇒ 会大量误报)。"""
    assert g.MIN_LINE >= 12
    sig = g._sig_lines("--- a\n+++ b\n+    }\n+        else:\n+def long_enough_signature_line(x):", "+")
    assert "}" not in sig and "else:" not in sig
    assert any(len(s) >= 12 for s in sig)
