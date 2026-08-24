"""ops/hermes 运维脚本双向对账测试 (2026-08-24, 复查A5).

背景: /root/.hermes/scripts/ 的 FTSeek 运维脚本由 cron 调度但不受 git 控制,
alpha_backfill.py 8/20 的超时修复发生在库外 (0d0a6a6f 补记), git 零痕迹。
本测试把 ops/hermes/ 变成记录性 SSOT: 双向 sha256 对账, 任一方向漂移即红。

方向1: 库内文件 ↔ 线上同名文件 必须逐字节一致 (改了没回库 / 库改了没部署)。
方向2: 线上目录不得出现库内没有的 FTSeek 运维脚本 (新脚本裸奔复发防线)。

可移植性: 线上目录不存在的环境 (如 CI 容器) 整组 skip, 不误伤。
"""
import hashlib
import os
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
OPS_DIR = REPO / "ops" / "hermes"
LIVE_DIR = Path("/root/.hermes/scripts")

pytestmark = pytest.mark.skipif(
    not LIVE_DIR.is_dir(), reason="非生产机 (无 /root/.hermes/scripts), 对账无意义")

# 线上目录里的非 FTSeek-US 遗产脚本 — 2026-08-24 逐一人工核实后显式豁免
# (新增未知脚本仍会触发方向2报警, 强制当次分诊)
LEGACY_EXCLUDE = {
    "fix_llm.py": "一次性补丁脚本, 指向旧部署 /opt/ftseek/FTSeek (非-US), 已消费",
    "autoheal_watchdog.py": "旧部署 /opt/ftseek/FTSeek 的诊断器 (ftseek.db/port8899)",
    "check_queue.py": "/opt/ftseek/autoheal 子系统队列预检, 非 FTSeek-US 链路",
    "ftseek_open_check.py": "旧部署 HK 首日验证 (decisions_hk.db/service=ftseek)",
}


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def test_repo_and_live_scripts_in_sync():
    """方向1: 每个入库副本必须与线上同名文件 sha256 一致."""
    repo_files = sorted(
        p for p in OPS_DIR.iterdir() if p.suffix in (".py", ".sh") and p.is_file())
    assert repo_files, "ops/hermes/ 为空 — SSOT 记录缺失"
    drifted = []
    for rp in repo_files:
        lp = LIVE_DIR / rp.name
        if not lp.exists():
            drifted.append(f"{rp.name}: 库内有, 线上已删除 (部署回线上或删库内副本)")
        elif _sha(rp) != _sha(lp):
            drifted.append(f"{rp.name}: 内容漂移 (改了没回库, 或库改了没部署)")
    assert not drifted, "运维脚本漂移:\n  " + "\n  ".join(drifted)


def test_no_unversioned_new_scripts_online():
    """方向2: 线上新出现的 FTSeek 运维脚本必须当次同步回库 (alpha_backfill 教训)."""
    known = {p.name for p in OPS_DIR.iterdir() if p.is_file()}
    unversioned = sorted(
        p.name for p in LIVE_DIR.iterdir()
        if p.suffix in (".py", ".sh")
        and p.name not in known
        and p.name not in LEGACY_EXCLUDE)
    assert not unversioned, (
        "线上存在未入库的运维脚本 (改前先 cp 回 ops/hermes/ 并 commit;"
        " 若属旧部署遗产, 在 LEGACY_EXCLUDE 登记核实理由):\n  "
        + "\n  ".join(unversioned))


def test_legacy_exclusions_still_match_old_deployment():
    """豁免纪律: LEGACY_EXCLUDE 每项必须仍指向旧部署特征 (路径含非-US根),
    防止豁免集被滥用成'新脚本不回库'的后门."""
    import re
    # 旧部署根 /opt/ftseek/FTSeek 精确匹配 (负向界定排除 -US); 或 autoheal 子系统
    _old_root = re.compile(r"/opt/ftseek/FTSeek(?!-)|/opt/ftseek/autoheal")
    for name in LEGACY_EXCLUDE:
        p = LIVE_DIR / name
        if not p.exists():
            continue
        content = p.read_text(encoding="utf-8", errors="replace")
        assert _old_root.search(content), (
            f"{name} 已不含旧部署路径特征却仍在 LEGACY_EXCLUDE — 重新分诊:"
            f"要么入库 ops/hermes/, 要么更新理由")
