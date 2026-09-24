r"""硬编码 UTC-4 "永久EDT假钟" 棘轮守卫 (2026-09-20, 时钟台账变种 1 收口批;
2026-09-24 扩围: 新增 `timezone(timedelta(hours=-4))` 形态 + 注释行过滤).

病灶: `datetime.now(timezone.utc) - timedelta(hours=4)` 假设美东恒为 EDT。
2026-11-01 起进入 EST (UTC-5) 后全程快 1 小时, 实证后果:
  · `ue_midday_shadow_eval` 盘中守卫: ET 15:00-16:00 被读成 ≥16 ⇒ 当日未成形
    bar 混入评估, 守卫失效 (本批已修);
  · 日期戳/产物归日: ET 23:00-24:00 窗内落次日 (`universe_expansion_shadow`
    / `gen_event_symbols`, 后者还连累交易日守卫查错日, 本批已修)。

变种1 (2026-09-24 扩围): `timezone(timedelta(hours=-4))` 直接构造固定偏移,
与 `.now()-timedelta(hours=4)` 同病灶: 11/1 后恒快 1h。主路径已全部修至
ZoneInfo("America/New_York"); 剩余 16 处均为 except 链末端兜底 (ZoneInfo→此)。

正例范本: `app/utils/timezone.py` (pytz→zoneinfo→固定偏移链, 固定偏移是**带
显式告警**的最后兜底, 不是静默假钟); `business_heartbeat.py` 注释明确拒绝此写法。

纪律: 数值棘轮, 基线只降不升。
白名单 18 处:
  旧形态 (.now(utc)-4h) — 2 处 ZoneInfo(→pytz) 链末端兜底:
    · scripts/llm_universe_scan.py:320  (ZoneInfo → pytz → 此)
    · scripts/da_glm_shadow.py:47       (ZoneInfo → 此)
  新形态 (timezone(timedelta(hours=-4))) — 16 处 except 链末端兜底:
    · app/playbook/overnight_hold_shadow.py:59          (now_et → ZoneInfo → 此)
    · scripts/orphan_ext_order_watch.py:64               (now_et → ZoneInfo → 此)
    · scripts/premarket_reprice_shadow.py:65             (now_et → ZoneInfo → 此)
    · scripts/premarket_exit_shadow.py:158               (now_et → ZoneInfo → 此)
    · scripts/settle_eod_depth_gate_shadow.py:58         (ZoneInfo → 此, 反序兜底)
    · scripts/breakeven_shadow_settle.py:92              (ZoneInfo → 此)
    · scripts/overnight_arm_report.py:40                 (先置此 → ZoneInfo 覆写, 等效兜底)
    · scripts/vol_arm_full_replay.py:26                  (ZoneInfo → 此, 回放脚本)
    · scripts/vol_arm_threshold_replay.py:34             (ZoneInfo → 此, 回放脚本)
    · scripts/stall_entry_shadow_tag.py:76               (ZoneInfo → 此; :71 是冻结启发式)
    · ops/hermes/insider_monitor_check.py:26             (ZoneInfo → 此)
    · ops/hermes/intraday_diagnose.py:32                 (ZoneInfo → 此)
    · ops/hermes/intraday_diagnose_late.py:31            (ZoneInfo → 此)
    · ops/hermes/premarket_trend_verify.py:42            (ZoneInfo → 此)
    · ops/hermes/open_hour_verify_20260908.py:31         (ZoneInfo → 此)
    · ops/audit/audit_inputs.py:8                        (ZoneInfo → 此)
注: `stall_entry_shadow_tag.py:71` 的 `(d - timedelta(hours=4))` 是文档化冻结启发式
("沿用旧口径, 不重算已打标历史"), 形态不同 (非 now() 假钟也非 timezone() 构造),
不在本棘轮管辖; 若它改成 now() 形态会立刻被本棘轮拦截。
"""
import ast
import re
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SCAN_DIRS = ("app", "scripts", "ops")
_PATTERN = re.compile(
    r"\.now\(timezone\.utc\)\s*-\s*timedelta\(hours=4\)"
    r"|\.utcnow\(\)\s*-\s*timedelta\(hours=4\)"
    r"|timezone\(timedelta\(hours=-4\)\)"
    r"|_dt\.timezone\(_dt\.timedelta\(hours=-4\)\)"
    r"|datetime\.timezone\(datetime\.timedelta\(hours=-4\)\)"
)

# 2026-09-24: 旧形态 2 + 新形态 16 = 18 (修 extended_hours_market_session + audit_inputs 后)
BASELINE_UTC4 = 18

# 所有白名单文件 (必须含 ZoneInfo — 证明是 except 链末端兜底, 非主路径假钟)
_WHITELIST_FILES = (
    # 旧形态 (.now(utc)-4h) 白名单
    "scripts/llm_universe_scan.py",
    "scripts/da_glm_shadow.py",
    # 新形态 (timezone(timedelta(hours=-4))) 白名单 — except 链末端兜底
    "app/playbook/overnight_hold_shadow.py",
    "scripts/orphan_ext_order_watch.py",
    "scripts/premarket_reprice_shadow.py",
    "scripts/premarket_exit_shadow.py",
    "scripts/settle_eod_depth_gate_shadow.py",
    "scripts/breakeven_shadow_settle.py",
    "scripts/overnight_arm_report.py",
    "scripts/vol_arm_full_replay.py",
    "scripts/vol_arm_threshold_replay.py",
    "scripts/stall_entry_shadow_tag.py",
    "ops/hermes/insider_monitor_check.py",
    "ops/hermes/intraday_diagnose.py",
    "ops/hermes/intraday_diagnose_late.py",
    "ops/hermes/premarket_trend_verify.py",
    "ops/hermes/open_hour_verify_20260908.py",
    "ops/audit/audit_inputs.py",
)


def _hits() -> list:
    """扫描所有 .py 文件, 仅匹配代码行 (排除 # 注释 + docstring 内的文本)。"""
    found = []
    for d in _SCAN_DIRS:
        root = _ROOT / d
        if not root.is_dir():
            continue
        for path in root.rglob("*.py"):
            text = path.read_text(encoding="utf-8", errors="ignore")
            # 用 AST 精确识别 docstring 行 (排除 docstring 内的文本匹配)
            docstring_lines: set[int] = set()
            try:
                tree = ast.parse(text)
                for node in ast.walk(tree):
                    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef,
                                         ast.ClassDef, ast.Module)):
                        if node.body and isinstance(node.body[0], ast.Expr) \
                                and isinstance(node.body[0].value, ast.Constant) \
                                and isinstance(node.body[0].value.value, str):
                            doc = node.body[0]
                            for ln in range(doc.lineno, (doc.end_lineno or doc.lineno) + 1):
                                docstring_lines.add(ln)
            except SyntaxError:
                pass  # AST 解析失败 → 仅靠 # 注释过滤

            lines = text.split("\n")
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                if stripped.startswith("#"):
                    continue
                if (i + 1) in docstring_lines:
                    continue  # docstring 内的文本 (如 business_heartbeat.py 的警告)
                if _PATTERN.search(line):
                    found.append(f"{path.relative_to(_ROOT)}:{i + 1}")
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
    """白名单 18 处必须仍含 ZoneInfo (防止有人把 ZoneInfo 移除使兜底变主路径)。"""
    for rel in _WHITELIST_FILES:
        text = (_ROOT / rel).read_text(encoding="utf-8")
        assert "ZoneInfo" in text, f"{rel}: 主路径 ZoneInfo 消失, 兜底白名单前提不再成立"
