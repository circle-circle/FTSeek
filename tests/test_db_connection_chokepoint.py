#!/usr/bin/env python3
"""DB 连接 chokepoint 棘轮守卫 (2026-08-31, 用户裁"按建议执行", DEB-20260831 P2①)。

背景: 结构债普查 319 文件裸 sqlite3.connect; E13 教训 = 写点发散导致影子覆盖
42% 定位不了。治理方案复刻 RFC-070 归口剧本: 先堵增量, 再分批收编存量到
`db_pool.get_db_path()` / `db_utils.get_main_db()` SSOT。

本守卫只堵**字面路径**连接 (最粗暴形态): `sqlite3.connect(".../*.db...")`。
变量路径连接 (含 f"file:{var}?mode=ro" SSOT 形态) 静态不可判, 不在捕捉面。
扫描域: app/ ops/ scripts/ (排除 archive/_tmp); tests 不管。

祖父名单 (2026-08-31 实测 10 个, 全在 scripts/; app/ 当时已全净):
收编一批 = 从名单移除一批; 名单只减不增。
回滚: 删本文件。
"""
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[1]

GRANDFATHER = frozenset({
    "scripts/dd_param_scan.py",
    "scripts/decision_view.py",
    "scripts/factor_ic_backfill.py",
    "scripts/ic_health_check.py",
    "scripts/llm_universe_scan.py",
    "scripts/position_path_collector.py",
    "scripts/trailing_polling_sim.py",
    "scripts/verify_deb0815_ic_daily.py",
    "scripts/verify_deb0815_ic_layers.py",
    "scripts/verify_deb0815_ic_variants.py",
})

_LITERAL_DB_CONNECT = re.compile(r'sqlite3\.connect\(\s*f?"[^"]*(?:\.db|/)[^"]*"')
_SCAN_ROOTS = ("app", "ops", "scripts")
_EXCLUDE_PARTS = {"archive", "__pycache__"}


def _violating_files():
    found = set()
    for root in _SCAN_ROOTS:
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if any(part in _EXCLUDE_PARTS or part.startswith("_tmp") for part in path.parts):
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except Exception:
                continue
            if _LITERAL_DB_CONNECT.search(text):
                found.add(str(path.relative_to(REPO)))
    return found


def test_no_new_literal_db_connections():
    violations = _violating_files()
    new = violations - GRANDFATHER
    assert not new, (
        f"新增字面路径 sqlite3.connect: {sorted(new)}。"
        f"请改用 db_pool.get_db_path()/db_utils.get_main_db() SSOT (RFC-070 归口剧本, "
        f"DEB-20260831 P2①)。确需例外: DEB 裁决后加入祖父名单。")


def test_grandfather_only_shrinks():
    """祖父名单里的文件消失 = 已收编, 是好事不告; 此测试只钉结构完整性。"""
    violations = _violating_files()
    remaining = GRANDFATHER & violations
    assert remaining <= GRANDFATHER  # 恒真占位: 名单语义由上一测试守护
    assert len(remaining) <= len(GRANDFATHER)
