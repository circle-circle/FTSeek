r"""硬编码 UTC-4 "永久EDT假钟" 棘轮守卫 (2026-09-20, 时钟台账变种 1 收口批).

病灶: `datetime.now(timezone.utc) - timedelta(hours=4)` 假设美东恒为 EDT。
2026-11-01 起进入 EST (UTC-5) 后全程快 1 小时, 实证后果:
  · `ue_midday_shadow_eval` 盘中守卫: ET 15:00-16:00 被读成 ≥16 ⇒ 当日未成形
    bar 混入评估, 守卫失效 (本批已修);
  · 日期戳/产物归日: ET 23:00-24:00 窗内落次日 (`universe_expansion_shadow`
    / `gen_event_symbols`, 后者还连累交易日守卫查错日, 本批已修)。
正例范本: `app/utils/timezone.py` (pytz→zoneinfo→固定偏移链, 固定偏移是**带
显式告警**的最后兜底, 不是静默假钟); `business_heartbeat.py` 注释明确拒绝此写法。

纪律: 与 test_naive_datetime_now_ratchet 同款数值棘轮, 基线只降不升。
白名单 2 处均为 ZoneInfo(→pytz) 链**末端兜底** — 触发条件是 tzdata 整体损坏,
届时任何 ET 实现同样失效, 硬编码在此不再构成病灶:
  · scripts/llm_universe_scan.py:320  (ZoneInfo → pytz → 此)
  · scripts/da_glm_shadow.py:47       (ZoneInfo → 此)
注: `stall_entry_shadow_tag.py` 的 `(d - timedelta(hours=4))` 是文档化冻结启发式
("沿用旧口径, 不重算已打标历史"), 形态不同 (非 now() 假钟), 不在本棘轮管辖;
若它改成 now() 形态会立刻被本棘轮拦截。
"""
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("app", "scripts", "ops")
_PATTERN = re.compile(
    r"\.now\(timezone\.utc\)\s*-\s*timedelta\(hours=4\)"
    r"|\.utcnow\(\)\s*-\s*timedelta\(hours=4\)"
)
BASELINE_UTC4 = 2  # 2026-09-20: 5 → 2 (本批修 universe_expansion/gen_event_symbols/ue_midday 3 处)


def _hits() -> list:
    found = []
    for d in _SCAN_DIRS:
        root = _ROOT / d
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            for m in _PATTERN.finditer(text):
                line = text[: m.start()].count("\n") + 1
                found.append(f"{path.relative_to(_ROOT)}:{line}")
    return found


def test_hardcoded_utc4_not_growing():
    hits = _hits()
    assert len(hits) <= BASELINE_UTC4, (
        f"硬编码 UTC-4 假钟增长: {BASELINE_UTC4} → {len(hits)}\n"
        f"新增点: {sorted(set(hits))}\n"
        "该写法冬令时 (EST=UTC-5) 全程快 1 小时且日期戳跨日错位 — 用 "
        "app.utils.timezone.now_et 或 ZoneInfo('America/New_York')。"
        "若确属 ZoneInfo/pytz 链末端兜底, 把 BASELINE_UTC4 保持不变并在提交信息说明理由。"
    )


def test_whitelisted_sites_are_fallback_only():
    """白名单 2 处必须仍是 except 链末端兜底 (防止有人把它们提为主路径)。"""
    for rel in ("scripts/llm_universe_scan.py", "scripts/da_glm_shadow.py"):
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "ZoneInfo" in text, f"{rel}: 主路径 ZoneInfo 消失, 兜底白名单前提不再成立"
