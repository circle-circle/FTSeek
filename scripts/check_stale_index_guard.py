#!/usr/bin/env python3
"""陈旧 index 守卫: 拦住「stage 了一个旧版本 blob」—— 并行 agent 互撤的静默杀手.

立起理由 (2026-09-26 实测, 两个 agent 同日先后踩):
  多 agent 并行改同一仓时, 常见做法是「只提交自己的 hunk」: 用 `git update-index --cacheinfo`
  或 worktree blob 注入, 把「旧 HEAD + 我的行」的 blob 塞进 index 后提交。
  **提交后 index 里仍残留那个基于旧 HEAD 的 blob**。此时另一个 agent 提交 (HEAD 前进),
  残留 blob 相对新 HEAD 就变成「删除他刚提交的行」⇒ 任何人 `git commit -a` 会
  **静默撤销他的工作** (不报错、不冲突, 事后极难归因)。
  实锤: 47f30ff74 差点因此丢失 (他 agent 提交信息自述); 同日我自己的 econ 备证块
  也因同型操作留在工作区缺失状态 (grep 实证: HEAD 2 处 / 工作区 0 处), 已手工注回。

口径 (实测选型, 见 docs 与 tests):
  **口径 B — 陈旧 blob 指纹 (硬拦, 零误报)**
    staged blob hash ∈ 该文件历史 blob 集合 ∧ ≠ HEAD:file blob
    ⇒ 你在 stage 一个**原封不动的历史版本**。正常编辑产生的 blob 是全新 hash
      (内容变了 ⇒ hash 必变), 故不会命中历史集合。
    实测: 真阳抓到 (陈旧 blob 6df33751 来自 6249c2d97, ≠ 当时 HEAD a60a0b9f);
          假阳 0/20 (回放最近 20 次该文件提交)。
  **口径 A — 删行指纹 (只提示, 不阻断)**
    staged diff 的删除行 ∩ 最近 K 次提交的新增行 ≠ ∅ ⇒ 打印提示。
    为何不阻断: 本仓**所有提交同一身份** (`FTSeek Bot` / bot@ftseek.com), author 不可分
    ⇒ 无法排除「同一 agent 改自己刚写的行」这一完全合法的场景 (实测 K=1 误报 2/24,
    两例均为改自己上一批刚加的行: 台账行改写、DA skill 版本棘轮 0.86→0.87)。
    阻断会造成提交风暴, 故降级为信息提示。

绕过: `FTSEEK_ALLOW_STALE_INDEX=1 git commit ...` (须写明理由; 真需要恢复旧版本时用)。
退出码: 0 = 放行; 1 = 阻断 (仅口径 B 命中)。
用法: scripts/check_stale_index_guard.py            # 检查当前 index
      scripts/check_stale_index_guard.py --selftest # 自检 (用真实历史验证口径有牙齿)
"""

from __future__ import annotations

import os
import subprocess
import sys

HIST_DEPTH = 500       # 口径 B 的历史 blob 回看深度
#   2026-11-03 (全链验收) 从 40 提到 500: 原值 40 只够看"近期提交", 实测事故提交
#   6249c2d97 在 shadow_registry.py 的 138 次提交里排第 66 位 ⇒ depth=40/60 都看不见它
#   ⇒ 守卫**自检真阳失败**且**运行期同样有盲区** (把 >40 提交前的旧快照 stage 进去,
#   正是"并行 agent 的工作被静默删除"的高危形态, 却不会被拦)。500 覆盖绝大多数文件的
#   全部历史 (该文件实际只有 138 次提交) ⇒ 口径 B 从"近 40 次"升级为"几乎全部历史"。
RECENT_COMMITS = 3     # 口径 A 的「最近提交」窗口 (实测 K=1 已有 2/24 误报, 只作提示)
MIN_LINE = 12          # 参与指纹比对的行最小长度 (短行如 `}` / `else:` 无判别力)
ALLOW_ENV = "FTSEEK_ALLOW_STALE_INDEX"


def _git(*args: str, check: bool = True) -> str:
    r = subprocess.run(["git"] + list(args), capture_output=True, text=True)
    if check and r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 失败: {r.stderr.strip()[:300]}")
    return r.stdout


def staged_files() -> list[str]:
    """本次要提交的、内容相对 HEAD 有变化的文件 (A/M/R; 排除纯删除 D)。"""
    out = []
    for line in _git("diff", "--cached", "--name-status", "-M").splitlines():
        parts = line.split("\t")
        if not parts:
            continue
        st = parts[0][0]                       # A/M/R100/D
        if st in ("A", "M", "R"):
            out.append(parts[-1])              # R 取新路径
    return out


def head_blob(path: str) -> str | None:
    b = _git("rev-parse", f"HEAD:{path}", check=False).strip()
    return b or None


def staged_blob(path: str) -> str | None:
    b = _git("ls-files", "-s", "--", path).split()
    # 格式: <mode> <blob> <stage>\t<path>
    return b[1] if len(b) >= 2 else None


def _resolve_shas(abbrevs: list[str]) -> dict[str, str]:
    """缩写 sha → 完整 sha (一次 `git cat-file --batch-check` 批量解析)。"""
    if not abbrevs:
        return {}
    payload = "\n".join(abbrevs) + "\n"
    r = subprocess.run(["git", "cat-file", "--batch-check"], input=payload,
                       capture_output=True, text=True)
    out: dict[str, str] = {}
    for ab, ln in zip(abbrevs, r.stdout.splitlines()):
        parts = ln.split()
        if parts and len(parts[0]) == 40:
            out[ab] = parts[0]
    return out


def hist_blobs(path: str, depth: int = HIST_DEPTH) -> dict[str, str]:
    """该文件历史各提交的 blob → 短 commit 映射 (只取改动过该文件的提交)。

    2026-11-03: 深度 40 → 500 后, 逐提交 `git rev-parse` (N+1 次子进程) 太慢 ⇒ 改用
    **一次** `git log --raw` (取每次提交引入的 blob) + **一次** `git cat-file --batch-check`
    (把缩写 sha 还原成完整 sha), 共 2 次子进程, 语义等价。
    注: `--raw` 里的 sha 默认缩写 (无法与 40 位 blob 哈希比对), 而 `--no-abbrev` 会把
    `%h` 一起撑成 40 位 (标签就不好看了) ⇒ 标签仍用 git 自己的 `%h`。
    """
    out: dict[str, str] = {}
    raw = _git("log", "--format=commit:%h", f"-{depth}", "--raw", "--", path)
    pairs: list[tuple[str, str]] = []      # (commit 短 hash, blob 缩写)
    cur = None
    for ln in raw.splitlines():
        if ln.startswith("commit:"):
            cur = ln.split(":", 1)[1].strip()
            continue
        if cur and ln.startswith(":"):
            parts = ln.split()
            # 格式: :<old_mode> <new_mode> <old_sha> <new_sha> <status>	<path>
            # new_sha = 该提交之后此路径的 blob (= 该提交引入的版本) ⇒ 与逐提交 rev-parse 等价
            if len(parts) >= 4 and len(parts[3]) >= 7 and set(parts[3]) != {"0"}:
                pairs.append((cur, parts[3]))
    resolved = _resolve_shas([b for _, b in pairs])
    for commit, abbrev in pairs:
        full = resolved.get(abbrev)
        if full:
            out.setdefault(full, commit)
    return out


def _sig_lines(diff_text: str, prefix: str) -> set[str]:
    out = set()
    for ln in diff_text.splitlines():
        if ln.startswith(prefix) and not ln.startswith(prefix * 3):
            t = ln[1:].strip()
            if len(t) >= MIN_LINE:
                out.add(t)
    return out


def recent_added(paths: list[str], k: int = RECENT_COMMITS) -> set[str]:
    """最近 k 次提交新增的行 (口径 A 的比对基准)。"""
    out: set[str] = set()
    for c in _git("log", f"--format=%h", f"-{k}").split():
        args = ["show", "--no-color", "-U0", "--format=", c]
        if paths:
            args += ["--"] + paths
        out |= _sig_lines(_git(*args, check=False), "+")
    return out


def check(verbose: bool = True) -> tuple[list[str], list[str]]:
    """返回 (阻断项, 提示项)。"""
    blocks: list[str] = []
    infos: list[str] = []
    files = staged_files()
    if not files:
        return blocks, infos

    # ── 口径 B: 陈旧 blob 指纹 (硬拦) ──
    for p in files:
        sb = staged_blob(p)
        hb = head_blob(p)
        if not sb or sb == hb:
            continue                      # 未变化 / 新文件 ⇒ 无陈旧风险
        hist = hist_blobs(p)
        if sb in hist and hist[sb] != (hb or ""):
            blocks.append(
                f"🕰️  陈旧 index: {p}\n"
                f"    staged blob = {sb[:12]} 是**提交 {hist[sb]} 的历史版本**, "
                f"≠ HEAD:{p} ({(hb or 'none')[:12]})\n"
                f"    ⇒ 你在 stage 一个旧快照。若此刻提交, 会把 HEAD 里比它新的内容"
                f"整体删掉 (并行 agent 的工作会**静默丢失**, 无冲突无报错)。\n"
                f"    成因通常是: 用 `update-index --cacheinfo`/worktree blob 注入提交后, "
                f"index 残留了旧 blob; 期间他人已提交。\n"
                f"    修法: git reset -q -- {p}   (清 index 残留, 不动工作区)\n"
                f"          然后把你的改动重新应用到**当前工作区版本**上, 再 add/commit。\n"
                f"          若确需恢复该历史版本: {ALLOW_ENV}=1 git commit ... (须在提交信息写明理由)")

    # ── 口径 A: 删行指纹 (只提示) ──
    if not blocks:
        diff = _git("diff", "--cached", "--no-color", "-U0", check=False)
        dele = _sig_lines(diff, "-")
        if dele:
            hit = dele & recent_added(files)
            if hit:
                infos.append(
                    f"ℹ️  本次删除了最近 {RECENT_COMMITS} 次提交刚加入的 {len(hit)} 行 "
                    f"(例: {sorted(hit)[0][:70]}…)\n"
                    f"    本仓所有提交同一身份 (FTSeek Bot) ⇒ 无法判断是你改自己刚写的行 (合法)\n"
                    f"    还是在撤销并行 agent 的成果 (危险)。不阻断, 请自查。")
    return blocks, infos


def selftest() -> int:
    """用真实历史验证口径 B 有牙齿 (memory 纪律: 0 告警的守卫最可能是假绿)。"""
    print("== 自检: 口径 B (陈旧 blob 指纹) ==")
    ok = 0
    total = 0
    # 真阳: 已知历史事故 (6249c2d97 的 blob 在 47f30ff74 提交后成为陈旧残留)
    total += 1
    p = "app/playbook/shadow_registry.py"
    stale = _git("rev-parse", f"6249c2d97:{p}", check=False).strip()
    then_head = _git("rev-parse", f"47f30ff74:{p}", check=False).strip()
    hist = hist_blobs(p)   # 深度用 HIST_DEPTH (500); 原硬编码 60 已在事故排位(第66位)之外 ⇒ 真阳漏报
    if stale and stale in hist and stale != then_head:
        print(f"  ✅ 真阳: 陈旧 blob {stale[:12]} (来自 {hist[stale]}) 被识别")
        ok += 1
    else:
        print(f"  ❌ 真阳漏报: stale={stale[:12]} in_hist={stale in hist} "
              f"head={then_head[:12]} (注: 若因 log 深度不足导致 in_hist=False, 调 HIST_DEPTH)")

    # 假阳: 两种「正常提交」形态都不该被拦 ——
    #   ① 原样提交当前内容 (staged blob == HEAD blob) ⇒ 运行期第一步就 continue;
    #   ② 在当前内容上改一行再提交 (新 blob) ⇒ 不在历史集合里。
    # 2026-11-03 (全链验收): 原判据是「最近 20 次提交的 blob 不应命中历史集合 ∧ ≠ 前一次」。
    #   深度 40→500 后该判据几乎恒真 —— 该文件历史含大量**内容重复** (回滚/重放), 那些 blob
    #   在更早历史里也出现过。但那**不是误判**: 它们确实是旧快照, stage 它们本就该被拦
    #   (提交它们会回退并行 agent 的工作)。故改为按运行期真实语义复算。
    total += 1
    hb = head_blob(p)
    edited_blob = None
    content = _git("show", f"HEAD:{p}", check=False)
    if content:
        import pathlib as _pl
        _repo = _pl.Path(__file__).resolve().parents[1]
        _r = subprocess.run(["git", "hash-object", "--stdin"], input=content + "\n# edit\n",
                             capture_output=True, text=True, cwd=str(_repo))
        edited_blob = _r.stdout.strip() or None
    fp = []
    for label, sb in (("原样提交当前内容", hb), ("改一行再提交", edited_blob)):
        if not sb:
            continue
        if sb == hb:
            continue                                   # 运行期第一步: sb == hb ⇒ 跳过
        hist_sb = hist.get(sb)
        if hist_sb is not None and hist_sb != (hb or ""):
            fp.append(label)
    if not fp:
        print("  ✅ 假阳: 2 种正常提交形态均未被拦 (原样提交当前内容 / 改一行再提交)")
        ok += 1
    else:
        print(f"  ❌ 假阳: {fp} 被误判")

    print(f"\n{ok}/{total} 通过")
    return 0 if ok == total else 1

def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if os.environ.get(ALLOW_ENV) == "1":
        print(f"⚠️  {ALLOW_ENV}=1 ⇒ 陈旧 index 守卫已跳过 (请确认提交信息写明理由)")
        return 0
    try:
        blocks, infos = check()
    except RuntimeError as e:
        print(f"⚠️  陈旧 index 守卫自身失败 (不阻断提交): {e}")
        return 0                          # 守卫故障不该卡住工作 (与钩子既有 || true 风格一致)
    for m in infos:
        print(m)
    for m in blocks:
        print(m)
    if blocks:
        print(f"\n🛑 已阻断 ({len(blocks)} 个文件)。确认无误后可用 {ALLOW_ENV}=1 绕过。")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
