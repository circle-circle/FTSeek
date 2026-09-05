r"""`data/risk_audit.json` 的 `time` 字段: 写方与两个读方**必须同尺**的契约
(2026-09-05, DEB-20260905 M-6/B1 第四阶).

为什么需要这条契约 (第四阶盘点出的**真陷阱**):
该字段构成一个**闭合的 naive-CST 环路**, 三处同尺 ⇒ **当前行为是正确的**:

  写方  `app/us_pipeline/risk_state_machine.py:333`  `_audit_log()`
        → `"time": datetime.now().isoformat()`            = naive 宿主本地(CST)墙钟
  读方1 `app/us_pipeline/risk_state_machine.py:85,100`  `restore_state()`
        → `now = datetime.now()` 与 `datetime.fromisoformat(rec["time"])` 相减 (4h 恢复窗)
  读方2 `app/us_pipeline/emergency_interceptor.py:425,432`  `_is_same_risk_off_recently()`
        → `now = _dt.now()` 与 `_dt.fromisoformat(rec["time"])` 相减 (2h 同 reason 去重)

**单翻任意一侧 = 静默功能死亡**, 而不是报错:
  - `aware - naive` 抛 `TypeError: can't subtract offset-naive and offset-aware datetimes`;
  - 读方2 的 `except Exception: return False` (`emergency_interceptor.py:444`) 会把它**静默吞掉**
    ⇒ 持久化去重永久失效 ⇒ **进程重启后同一条新闻反复触发 RISK_OFF** —— 恰好是这段代码
    (P0 2026-06-05) 被写出来要解决的那个 bug 原样复活;
  - 读方1 的 `except Exception as e: logger.error(...)` 会留一行错误日志, 但 RISK_OFF 状态**不再恢复**
    ⇒ 重启后风控失忆 (方向=放松, 更危险)。

因此本阶**刻意不翻**这三处 (它们不是病灶, 是"正确但脆弱"), 改为立契约钉住同尺不变量。
本文件的测试是**原子翻容忍 / 单侧翻必红**的: 三处一起翻成 ET 仍全绿 (尺度由源码动态推导),
只翻一侧则行为测与同尺测一起变红。

现况取证 (2026-09-05 实测 `data/risk_audit.json`): 27 条记录, `time` **全部 naive**
(如 `2026-06-06T03:00:07.020743`), 最后写入 **2026-06-12 22:30** ⇒ 该路径已**冷了近 3 个月**
(无 RISK_OFF/HALT 事件)。所以今天零血量, 但下一次 RISK_OFF 触发时它会复活 —— 属于
" dormant landmine", 必须先立守卫再谈翻转。

**若日后要翻, 前置条件 (已入 DEB §12.5)**: ①三处必须**同一提交**内原子翻;
②读方必须先加**容错解析** (naive 一律按 CST 解释, 照 `trade_store._parse_sync_ts` 的写法),
因为磁盘上存量的 naive 记录不会自清 —— `_audit_log` 只做 `logs[-100:]` 截断, 而该文件
3 个月才 27 条, 存量记录会留存极久; ③翻完把本文件的尺度断言与 DEB 台账同步更新。

回滚: 删本文件 (环路本身未被本阶改动, 删测试即回到"无守卫"状态)。
"""
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import pytest

from app.us_pipeline.risk_state_machine import RiskState, RiskStateMachine
from app.us_pipeline.emergency_interceptor import USEmergencyInterceptor
from app.utils.timezone import now_et

_RSM = PROJECT_ROOT / "app" / "us_pipeline" / "risk_state_machine.py"
_EI = PROJECT_ROOT / "app" / "us_pipeline" / "emergency_interceptor.py"

COOLDOWN_SEC = 7200      # _is_same_risk_off_recently 的同-reason 去重窗 (2h)
RESTORE_WINDOW_SEC = 14400  # restore_state 的 RISK_OFF 恢复窗 (4h)
REASON = "突发利空: US.AVGO 财报暴雷 (测试用同一 reason)"


# ── 源码尺度推导 (让测试对"原子翻"保持中立) ────────────────────────────────
def _func_body(path: Path, name: str) -> str:
    lines = path.read_text(encoding="utf-8").splitlines()
    start = indent = None
    for i, ln in enumerate(lines):
        if re.match(rf"\s*(async\s+)?def {re.escape(name)}\s*\(", ln):
            start, indent = i, len(ln) - len(ln.lstrip())
            break
    assert start is not None, f"{path.name} 里找不到 def {name} — 函数被改名/删除? 请同步更新本契约"
    out = [lines[start]]
    for ln in lines[start + 1:]:
        stripped = ln.strip()
        if stripped and (len(ln) - len(ln.lstrip())) <= indent and re.match(r"(def |class |@)", stripped):
            break
        out.append(ln)
    return "\n".join(out)


def _classify(expr: str) -> str:
    """把一个取"现在"的表达式判为 aware / naive / unknown.

    别名宽容: 仓内惯例是 `from app.utils.timezone import now_et as _now_et_xxx`
    (如 `_now_et_pb` / `_now_et_cal` / `_now_et_ic` / `_now_et_ts`), 故按**后缀匹配**识别
    `*now_et/now_utc/now_cst*()` 而非只认裸名 —— 否则原子翻成 ET 后本契约会**误红**
    (2026-09-05 双向证伪的第二条实跑撞出来的: 只翻写方→3 红符合预期, 三处原子翻→
    行为测 6 绿但同尺测因不认别名而误红)。
    """
    e = expr.strip()
    if re.search(r"\b\w*now_(et|utc|cst)\w*\s*\(\s*\)", e):
        return "aware"
    if re.search(r"\bget_trade_date\s*\(\s*\)", e):
        return "aware"
    m = re.search(r"\.now\(([^)]*)\)", e)
    if m:
        return "aware" if m.group(1).strip() else "naive"
    return "unknown"


def _writer_expr() -> str:
    body = _func_body(_RSM, "_audit_log")
    m = re.search(r'"time"\s*:\s*([^,\n]+)', body)
    assert m, "_audit_log 里找不到 record['time'] 的赋值 — 写方被重构? 请同步更新本契约"
    return m.group(1)


def _reader_expr(path: Path, func: str) -> str:
    body = _func_body(path, func)
    m = re.search(r"^\s*now\s*=\s*(.+)$", body, re.M)
    assert m, f"{func} 里找不到 `now = ...` — 读方被重构? 请同步更新本契约"
    return m.group(1)


def _writer_scale() -> str:
    scale = _classify(_writer_expr())
    assert scale != "unknown", (
        f"无法判定写方尺度: {_writer_expr()!r} — 若这是新的带尺写法, 请扩 _classify 的识别式, "
        f"不要直接把断言删掉")
    return scale


def _ts(delta: timedelta) -> str:
    """按**写方当前尺度**造一个时间戳 — 这样原子翻成 ET 后本文件无需改动."""
    if _writer_scale() == "aware":
        return (now_et() + delta).isoformat()
    return (datetime.now() + delta).isoformat()


def _write_audit(path: Path, records):
    path.write_text(json.dumps(records, indent=2), encoding="utf-8")


def _rsm_stub(audit_path):
    """RiskStateMachine 的最小替身: _audit_log / restore_state 只用这几个属性."""
    return SimpleNamespace(_audit_path=str(audit_path), _state=RiskState.NORMAL,
                           _reason="", _state_change_time=0.0)


def _ei_stub(audit_path):
    """USEmergencyInterceptor._is_same_risk_off_recently 的最小替身."""
    return SimpleNamespace(state_machine=SimpleNamespace(_audit_path=str(audit_path)),
                           _reason_cooldown_seconds=COOLDOWN_SEC)


# ── 1. 同尺不变量 (源码级, 三处必须一致) ───────────────────────────────────
def test_all_three_sites_agree_on_scale():
    scales = {
        "写方 risk_state_machine._audit_log": _classify(_writer_expr()),
        "读方1 risk_state_machine.restore_state": _classify(_reader_expr(_RSM, "restore_state")),
        "读方2 emergency_interceptor._is_same_risk_off_recently":
            _classify(_reader_expr(_EI, "_is_same_risk_off_recently")),
    }
    assert "unknown" not in scales.values(), f"有站点尺度判不出 (被重构?): {scales}"
    assert len(set(scales.values())) == 1, (
        f"risk_audit.json 的 time 字段**读写不同尺** = 静默功能死亡: {scales}。\n"
        f"aware 与 naive 相减抛 TypeError, 而读方2 的 `except: return False` 会静默吞掉 ⇒ "
        f"RISK_OFF 持久化去重永久失效 (重启后同一新闻反复触发); 读方1 则不再恢复 RISK_OFF ⇒ 风控失忆。\n"
        f"要翻必须**三处同一提交内原子翻**, 且读方先加容错解析 (naive 按 CST 解释, 照 "
        f"trade_store._parse_sync_ts), 因为磁盘存量 naive 记录不会自清 (_audit_log 只截断 logs[-100:], "
        f"而该文件 3 个月才 27 条)。详见 DEB §12.5 与本文件头注。")


# ── 2. 行为契约: 真写方 → 真读方, 去重必须命中 ─────────────────────────────
def test_interceptor_dedup_hits_for_recent_same_reason(tmp_path):
    audit = tmp_path / "risk_audit.json"
    RiskStateMachine._audit_log(_rsm_stub(audit), "NORMAL", "RISK_OFF", REASON, "NewsInterceptor")
    assert json.loads(audit.read_text())[0]["new_state"] == "RISK_OFF", "前置: 真写方应已落一条 RISK_OFF"
    assert USEmergencyInterceptor._is_same_risk_off_recently(_ei_stub(audit), REASON) is True, (
        "写方刚落的 RISK_OFF 记录, 读方2 在 2h 窗内**必须**判为同一事件并去重。"
        "返回 False 通常意味着 aware/naive 相减抛 TypeError 被 `except: return False` 静默吞掉")


def test_restore_state_recovers_risk_off_within_window(tmp_path):
    audit = tmp_path / "risk_audit.json"
    RiskStateMachine._audit_log(_rsm_stub(audit), "NORMAL", "RISK_OFF", REASON, "NewsInterceptor")
    stub = _rsm_stub(audit)
    RiskStateMachine.restore_state(stub)
    assert stub._state == RiskState.RISK_OFF, (
        "4h 窗内的 RISK_OFF 记录, 重启后**必须**恢复为 RISK_OFF。仍是 NORMAL 通常意味着 "
        "aware/naive 相减抛 TypeError 被 `except: logger.error` 吞掉 ⇒ 风控失忆 (放松方向)")


# ── 3. 负对照: 证明上面两条不是因为"函数恒真/恒恢复"而空过 ─────────────────
def test_interceptor_dedup_misses_outside_cooldown(tmp_path):
    audit = tmp_path / "risk_audit.json"
    _write_audit(audit, [{
        "time": _ts(-timedelta(seconds=COOLDOWN_SEC + 600)),   # 2h 窗之外
        "old_state": "NORMAL", "new_state": "RISK_OFF",
        "reason": REASON, "source": "NewsInterceptor",
    }])
    assert USEmergencyInterceptor._is_same_risk_off_recently(_ei_stub(audit), REASON) is False, \
        "超过 2h 冷却窗的同 reason 记录**不应**再被去重 (否则说明判定恒真, 上一条测试是空过)"


def test_restore_state_skips_outside_window(tmp_path):
    audit = tmp_path / "risk_audit.json"
    _write_audit(audit, [{
        "time": _ts(-timedelta(seconds=RESTORE_WINDOW_SEC + 600)),   # 4h 窗之外
        "old_state": "NORMAL", "new_state": "RISK_OFF",
        "reason": REASON, "source": "NewsInterceptor",
    }])
    stub = _rsm_stub(audit)
    RiskStateMachine.restore_state(stub)
    assert stub._state == RiskState.NORMAL, \
        "超过 4h 的 RISK_OFF **不应**被恢复 (否则说明恢复判定恒真, 上一条测试是空过)"


def test_dedup_misses_for_different_reason(tmp_path):
    audit = tmp_path / "risk_audit.json"
    RiskStateMachine._audit_log(_rsm_stub(audit), "NORMAL", "RISK_OFF", REASON, "NewsInterceptor")
    assert USEmergencyInterceptor._is_same_risk_off_recently(
        _ei_stub(audit), "完全不同的事件: US.TSLA 召回 (测试)") is False, \
        "不同 reason (标的集合不同) 不应被去重 — 证明命中的确来自 reason 匹配而非时间窗"


def test_restore_skipped_when_later_normal_reset_exists(tmp_path):
    """P0 2026-06-05 语义: RISK_OFF 之后若已有 NORMAL 重置, 不应再恢复."""
    audit = tmp_path / "risk_audit.json"
    _write_audit(audit, [
        {"time": _ts(-timedelta(minutes=30)), "old_state": "NORMAL", "new_state": "RISK_OFF",
         "reason": REASON, "source": "NewsInterceptor"},
        {"time": _ts(-timedelta(minutes=10)), "old_state": "RISK_OFF", "new_state": "NORMAL",
         "reason": "手动确认", "source": "manual"},
    ])
    stub = _rsm_stub(audit)
    RiskStateMachine.restore_state(stub)
    assert stub._state == RiskState.NORMAL, "已有 NORMAL 重置 ⇒ 不应恢复 RISK_OFF"
