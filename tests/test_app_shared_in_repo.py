"""`app/shared` 必须在本仓版本控制内 钉子 (DEB-20260910-logging §11.13, 2026-09-11)

盲点 (安检仪关 6 归属核验时顺带查出, 存量非本批引入):
  `app/shared` 曾是**符号链接** → `../../FTSeek/app/shared` (仓外, git 形态 120000)。
  而 `pathlib` 的 `**` 递归**不跟随符号链接目录**, `scripts/deploy_freshness_check.py:139`
  的 `newest_code_ts()` 正是用 `(REPO / "app").rglob("*.py")` ⇒ 实测 466 个 `.py` 命中里
  `app/shared/` **0 个**, 而 `os.walk(followlinks=True)` 实有 2 个 (`__init__.py` +
  `market_hours.py` 24KB, 提供 `get_market_phase`, 被 `app/data/subscription_manager.py:25`
  导入 ⇒ 落在**市场阶段判定**链上)。

  叠加第二层风险: 链接目标所在的 `/opt/ftseek/FTSeek` 已于 2026-06-14 停用
  (`.DECOMMISSIONED`: 功能全迁 FTSeek-US / DO NOT RESTART, `ftseek.service` 已 masked)
  且**没有 .git** ⇒ 实盘依赖寄居在"仓外 + 无版本控制 + 标着别删"的目录里。
  合起来就是: **有人改它 → 服务继续跑旧代码 → 守卫永不自愈 → 也没有提交记录可追。**

修法 = §11.12 建议的 (b) 收回本仓 (符号链接退役, `market_hours.py` 逐字节一致迁入,
mtime 复原 ⇒ 搬家不是部署事件, 不触发无谓重启)。成为真目录后守卫**自动可见**, 无需改守卫代码。

钉住四件事:
  ① `app/shared` 是真目录不是符号链接, 两个源文件在位且可导入 (导入路径落在仓内)
  ② **`app/` 全树无符号链接** — 通用棘轮, 防任何新链接再造同款盲点
  ③ **守卫的 rglob 视野 == 文件系统真相** (`os.walk(followlinks=True)` 对照)
     —— 这才是盲点本身的钉子: 谁的目录从守卫眼里消失, 这里就碎
  ④ 两个文件被 git 以普通 blob (100644) 跟踪, 且未被 .gitignore 吞掉;
     `__init__.py` 不再宣称"US access: symlink"这条已失效的接入方式
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

SHARED = PROJECT_ROOT / "app" / "shared"
SHARED_SOURCES = ("__init__.py", "market_hours.py")

_spec = importlib.util.spec_from_file_location(
    "deploy_freshness_check_for_shared", PROJECT_ROOT / "scripts" / "deploy_freshness_check.py"
)
dfc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dfc)


def _git(*args: str) -> str | None:
    """git 不可用/非检出目录时返回 None (调用方 skip), 但**检出目录内的不合规必须红**。"""
    if shutil.which("git") is None:
        return None
    try:
        r = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True,
                           text=True, timeout=60)
    except Exception:  # noqa: BLE001 — 钉子自身不得因环境炸掉, 但也不得假绿
        return None
    return r.stdout if r.returncode == 0 else None


def _walk_py_files(root: Path) -> set[str]:
    """文件系统真相: 跟随符号链接目录的 os.walk (带 realpath 去环, 防循环链接卡死)。"""
    seen_dirs: set[str] = set()
    out: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        real = os.path.realpath(dirpath)
        if real in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(real)
        dirnames[:] = [d for d in dirnames if d != "__pycache__"]
        for name in filenames:
            if name.endswith(".py"):
                out.add(str((Path(dirpath) / name).relative_to(PROJECT_ROOT)))
    return out


def _guard_scan_py_files() -> set[str]:
    """守卫的视野: 逐字复刻 newest_code_ts() 的扫描面 (SCAN_DIRS/SCAN_FILES 取自守卫本身)。"""
    paths = [PROJECT_ROOT / f for f in dfc.SCAN_FILES]
    for d in dfc.SCAN_DIRS:
        paths.extend((PROJECT_ROOT / d).rglob("*.py"))
    out: set[str] = set()
    for p in paths:
        if "__pycache__" in p.parts or not p.is_file():
            continue
        out.add(str(p.relative_to(PROJECT_ROOT)))
    return out


def test_app_shared_is_real_directory_not_symlink():
    assert SHARED.exists(), f"{SHARED} 不存在"
    assert not SHARED.is_symlink(), (
        "app/shared 又变回符号链接了 ⇒ 部署断层守卫对它失明 (pathlib.rglob 不跟随链接目录), "
        "且实盘依赖再次寄居仓外。见 DEB-20260910-logging §11.13。"
    )
    assert SHARED.is_dir()
    for name in SHARED_SOURCES:
        f = SHARED / name
        assert f.is_file() and not f.is_symlink(), f"app/shared/{name} 缺失或又是链接"
        assert f.stat().st_size > 0, f"app/shared/{name} 是空文件"


def test_app_shared_importable_from_inside_repo():
    import app.shared as shared_pkg
    import app.shared.market_hours as mh

    for mod in (shared_pkg, mh):
        assert mod.__file__ is not None
        resolved = Path(mod.__file__).resolve()
        assert PROJECT_ROOT in resolved.parents, (
            f"{mod.__name__} 实际加载自仓外 {resolved} ⇒ 跑的不是本仓代码"
        )
    assert "app/shared" in str(Path(shared_pkg.__file__).as_posix())
    for export in ("MarketType", "MarketPhase", "get_market_phase", "MarketHours"):
        assert hasattr(shared_pkg, export), f"app.shared 少了导出 {export}"
    assert callable(mh.get_market_phase)


def test_no_symlinks_anywhere_under_app():
    """通用棘轮: `app/` 下不得有任何符号链接 (链接目录 = 守卫盲点, 链接文件 = 仓外不可追代码)。"""
    links = sorted(
        str(p.relative_to(PROJECT_ROOT))
        for p in (PROJECT_ROOT / "app").rglob("*")
        if p.is_symlink()
    )
    assert not links, f"app/ 下出现 {len(links)} 个符号链接 (守卫视野会漏): {links[:5]}"


def test_guard_scan_sees_everything_on_disk():
    """盲点本体的钉子: 守卫 rglob 视野必须覆盖 os.walk(followlinks=True) 的文件系统真相。"""
    truth: set[str] = set()
    for d in dfc.SCAN_DIRS:
        truth |= _walk_py_files(PROJECT_ROOT / d)
    for f in dfc.SCAN_FILES:
        p = PROJECT_ROOT / f
        if p.is_file():
            truth.add(f)
    vision = _guard_scan_py_files()
    missing = sorted(truth - vision)
    assert not missing, (
        f"{len(missing)} 个 .py 在磁盘上却不在守卫扫描面内 (部署断层将不可观测): {missing[:5]}"
    )
    assert "app/shared/market_hours.py" in vision, "本次修复的直接对象必须重新可见"


def test_guard_newest_code_ts_still_sane():
    got = dfc.newest_code_ts()
    assert got is not None
    mtime, rel = got
    assert mtime > 0 and rel.endswith(".py")
    assert (PROJECT_ROOT / rel).is_file(), f"守卫报的最新代码 {rel} 不存在"


def test_shared_sources_tracked_as_regular_blobs():
    out = _git("ls-files", "-s", "--", "app/shared")
    if out is None:
        pytest.skip("git 不可用或非检出目录 — 文件系统钉子已独立覆盖")
    modes = {}
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 4:
            modes[parts[3]] = parts[0]
    for name in SHARED_SOURCES:
        path = f"app/shared/{name}"
        assert path in modes, f"{path} 未被 git 跟踪 (仓外不可追代码)"
        assert modes[path] == "100644", f"{path} 的 git 形态是 {modes[path]}, 应为普通文件 100644"
    assert "app/shared" not in modes, "app/shared 仍以符号链接形态 (120000) 被跟踪"


def test_shared_sources_not_gitignored():
    if shutil.which("git") is None:
        pytest.skip("git 不可用")
    r = subprocess.run(["git", "check-ignore", "-v", "app/shared/market_hours.py"],
                       cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=60)
    assert r.returncode != 0, f"app/shared/market_hours.py 被 .gitignore 吞掉: {r.stdout.strip()}"


def test_init_docstring_no_longer_advertises_symlink():
    src = (SHARED / "__init__.py").read_text(encoding="utf-8")
    assert "US access: symlink" not in src, (
        "__init__.py 仍在宣称符号链接接入方式 — 该说明随 2026-06-14 港股停用已失效, "
        "留着会把下一个维护者引回盲点"
    )
