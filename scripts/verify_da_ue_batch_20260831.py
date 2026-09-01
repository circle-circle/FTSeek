#!/usr/bin/env python3
"""2026-08-31 DA/UE 首批生产运行晨间核对 (一次性, 只读).

核对对象 (均为 2026-08-31 ET 日的生产运行):
  DA 19:00 CST — skill 仓 v0.72 修复批首轮 (PROMPT_HASH bd93af5b0570):
    ① _prompt_hash 全为新指纹 ②契约违约 skip_reason_missing→0 (基线 8/25-28 均值 8.5/天,
    三天 26 次额外 repair 调用) ③_finish_reason 全 stop ④挑战者 verdict 分布
    (新挑战者 0813 第 3 生产日样本; B11 观察窗 9/1-9/12 的前哨数据)。
  UE 21:10 CST — v2.8 模板 (eb75d45a72c2) + glm-5 首轮生产运行:
    ①scan 记录 date/hash/版本 ②model_used=glm-5* (否则=降级链触发) ③IT 填充率
    (非SKIP 中 invalidation_triggers≥1 占比; B6③ 目标≥50%, 观察窗 10 交易日, 本日仅报数)
    ④llm_audit 延迟 (B6① <300s) ⑤contract_stats。
  playbook_instructions (ftseek_us.db): DA 行 decision_conf 分布 (B12 观察: 恒 0.70 常量退化
  是否延续) + UE 行数。

判据来源: docs/ADJUDICATED-CRITERIA.md B6/B11/B12 + DEB-20260831-da-debate-quality-and-decision-conf
+ handoff 2026-08-31f。只读, 不写任何数据文件; 成功后自删 crontab 行 (防一次性行滞留年度复燃);
失败同样删行但推送 HIGH (人工可 --date 手动重跑)。

用法: PYTHONPATH=. ./venv/bin/python scripts/verify_da_ue_batch_20260831.py [--date YYYY-MM-DD] [--no-push]
"""
import argparse
import json
import sqlite3
import subprocess
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parent.parent
ET = ZoneInfo("America/New_York")
CST = ZoneInfo("Asia/Shanghai")

EXPECTED_DA_PROMPT_HASH = "bd93af5b0570"   # skill v0.72 (2026-08-31d)
EXPECTED_UE_PROMPT_HASH = "eb75d45a72c2"   # UE v2.8
EXPECTED_UE_PROMPT_VERSION = "v2.8"
UE_MODEL_PREFIX = "glm-5"
B6_LATENCY_LIMIT_MS = 300_000              # B6① 延迟<300s
IT_FILL_TARGET = 0.50                      # B6③ trig 填充率≥50% (观察窗内仅报数)
CRON_MARKER = "ONESHOT-20260901-da-ue-verify"

failures: list[str] = []
notes: list[str] = []


def ok(tag: str, msg: str) -> str:
    return f"  ✅ {tag}: {msg}"


def bad(tag: str, msg: str) -> str:
    failures.append(f"{tag}: {msg}")
    return f"  🔴 {tag}: {msg}"


def warn(tag: str, msg: str) -> str:
    notes.append(f"{tag}: {msg}")
    return f"  ⚠️ {tag}: {msg}"


def check_da(date: str) -> list[str]:
    lines = [f"【DA 侧】 data/da_checkpoint/{date}/ (v0.72 首轮)"]
    d = REPO / "data" / "da_checkpoint" / date
    files = sorted(d.glob("*.json")) if d.is_dir() else []
    if not files:
        lines.append(bad("DA-ckpt", f"目录不存在或为空: {d} — DA 轮未产出"))
        return lines
    lines.append(ok("DA-ckpt", f"{len(files)} 个 checkpoint"))

    hashes, finish, verdicts, models = Counter(), Counter(), Counter(), Counter()
    vio_total, vio_skip, repairable_n, no_llm = 0, 0, 0, 0
    fallback_n = 0
    for f in files:
        try:
            doc = json.loads(f.read_text(encoding="utf-8"))
        except Exception as e:
            lines.append(bad("DA-parse", f"{f.name} 解析失败: {e}"))
            continue
        lo = doc.get("_llm_output") or {}
        if not isinstance(lo, dict) or not lo:
            no_llm += 1
            continue
        hashes[lo.get("_prompt_hash")] += 1
        finish[lo.get("_finish_reason")] += 1
        c = lo.get("_contract") or {}
        for v in c.get("violations") or []:
            name = v if isinstance(v, str) else v.get("type", "?")
            vio_total += 1
            if name == "skip_reason_missing":
                vio_skip += 1
        repairable_n += len(c.get("repairable") or [])
        deb = lo.get("_debate") or {}
        verdicts[deb.get("challenger_verdict")] += 1
        models[deb.get("challenger_model")] += 1
        if deb.get("challenger_fallback"):
            fallback_n += 1

    n = len(files)
    if hashes.get(EXPECTED_DA_PROMPT_HASH, 0) == n:
        lines.append(ok("DA-hash", f"全部 {n}/{n} 为新指纹 {EXPECTED_DA_PROMPT_HASH}"))
    else:
        lines.append(bad("DA-hash", f"新指纹仅 {hashes.get(EXPECTED_DA_PROMPT_HASH, 0)}/{n}, 分布={dict(hashes)}"))
    if no_llm:
        lines.append(warn("DA-nollm", f"{no_llm} 个文件无 _llm_output (SKIP/异常路径?)"))
    if vio_skip == 0:
        lines.append(ok("DA-skip_reason", "skip_reason_missing 违约=0 (基线 8.5/天 → 修复④生效)"))
    else:
        lines.append(bad("DA-skip_reason", f"skip_reason_missing 违约={vio_skip} (基线 8.5/天, 修复④未兑现?)"))
    if vio_total - vio_skip > 0:
        lines.append(warn("DA-vio", f"其他类型违约 {vio_total - vio_skip} 次 (明细另查)"))
    lines.append(f"  ℹ️ DA-repair: repairable={repairable_n} (基线 ~8.5/天; 理想≈0)")
    if finish and set(finish) == {"stop"}:
        lines.append(ok("DA-finish", f"finish_reason 全 stop ({sum(finish.values())})"))
    else:
        lines.append(bad("DA-finish", f"finish_reason 分布异常: {dict(finish)}"))
    lines.append(f"  ℹ️ DA-verdict: {dict(verdicts)} (B11 前哨: AGREE 占比 "
                 f"{(verdicts.get('AGREE', 0) / max(1, sum(verdicts.values())) * 100):.0f}%)")
    lines.append(f"  ℹ️ DA-challenger_model: {dict(models)}; fallback={fallback_n}")
    return lines


def check_ue_scan(date: str) -> list[str]:
    lines = [f"【UE 侧】 data/shadow/llm_universe_scan.jsonl (v2.8+glm-5 首轮)"]
    p = REPO / "data" / "shadow" / "llm_universe_scan.jsonl"
    if not p.exists():
        lines.append(bad("UE-scan", "jsonl 不存在"))
        return lines
    scans = []
    _bad = 0
    for ln in p.read_text(encoding="utf-8").splitlines():
        if not ln.strip():
            continue
        try:
            r = json.loads(ln)
        except Exception:
            _bad += 1
            continue
        if r.get("type") == "scan" and r.get("date") == date:
            scans.append(r)
    if _bad:
        lines.append(bad("UE-scan", f"jsonl 有 {_bad} 行解析失败 (样本可能不完整)"))
    if not scans:
        lines.append(bad("UE-scan", f"无 date={date} 的 scan 记录 — UE 轮未落库"))
        return lines
    pre = [s for s in scans if s.get("scan_window") == "premarket"]
    s = max(pre or scans, key=lambda x: x.get("scanned_at", ""))
    lines.append(ok("UE-scan", f"date={date} window={s.get('scan_window')} scanned_at={s.get('scanned_at')}"))

    if s.get("prompt_hash") == EXPECTED_UE_PROMPT_HASH:
        lines.append(ok("UE-hash", f"prompt_hash={s.get('prompt_hash')} (v2.8)"))
    else:
        lines.append(bad("UE-hash", f"prompt_hash={s.get('prompt_hash')} ≠ 期望 {EXPECTED_UE_PROMPT_HASH}"))
    if s.get("prompt_version") != EXPECTED_UE_PROMPT_VERSION:
        lines.append(warn("UE-ver", f"prompt_version={s.get('prompt_version')}"))

    model = s.get("model_used") or ""
    if model.startswith(UE_MODEL_PREFIX):
        lines.append(ok("UE-model", f"model_used={model} (未触发降级链)"))
    else:
        lines.append(bad("UE-model", f"model_used={model} — 非 {UE_MODEL_PREFIX}* (降级链触发/配置未生效)"))

    cs = s.get("contract_stats") or {}
    lines.append(f"  ℹ️ UE-contract: clean={cs.get('clean')} violations={cs.get('violations')} "
                 f"repaired={cs.get('repaired')} still_bad={cs.get('still_bad')} dropped={cs.get('dropped')}")
    if cs.get("still_bad") or cs.get("dropped"):
        lines.append(warn("UE-contract", "存在 still_bad/dropped 候选"))

    surfaced = s.get("surfaced") or []
    active = [c for c in surfaced if not (c.get("skip_reason") or "")]
    it_fill = sum(1 for c in active if (c.get("invalidation_triggers") or []))
    rate = it_fill / len(active) if active else 0.0
    mark = ok if rate >= IT_FILL_TARGET else warn
    lines.append(mark("UE-IT填充", f"非SKIP {len(active)}/{len(surfaced)}, trig≥1 填充 {it_fill} "
                      f"= {rate * 100:.0f}% (B6③ 目标≥50%, 10交易日观察窗第1天仅报数)"))
    return lines


def check_ue_latency(date: str) -> list[str]:
    lines = ["【UE 延迟】 data/llm_audit.db (B6① <300s)"]
    db = REPO / "data" / "llm_audit.db"
    if not db.exists():
        lines.append(bad("UE-audit", "llm_audit.db 不存在"))
        return lines
    # UE 轮排程 21:10 CST, 窗口取当日 20:00-23:59 CST (目标 ET 日与 CST 日同号)
    day = datetime.strptime(date, "%Y-%m-%d")
    lo = day.replace(tzinfo=CST).replace(hour=20, minute=0).timestamp()
    hi = day.replace(tzinfo=CST).replace(hour=23, minute=59, second=59).timestamp()
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT model, latency_ms, success, prompt_hash FROM llm_calls "
        "WHERE feature='universe_expansion_reasoning' AND is_test=0 AND timestamp BETWEEN ? AND ? "
        "ORDER BY timestamp", (lo, hi)).fetchall()
    con.close()
    if not rows:
        lines.append(bad("UE-audit", "窗口内无 universe_expansion_reasoning 生产调用记录"))
        return lines
    worst = 0.0
    for model, latency, success, ph in rows:
        latency = latency or 0.0
        worst = max(worst, latency)
        lines.append(f"  ℹ️ 调用: model={model} latency={latency / 1000:.1f}s success={success} hash={ph}")
        if not success:
            lines.append(warn("UE-audit", f"存在失败调用 (model={model})"))
    if worst < B6_LATENCY_LIMIT_MS:
        lines.append(ok("UE-latency", f"最大延迟 {worst / 1000:.1f}s < 300s (B6①)"))
    else:
        lines.append(bad("UE-latency", f"最大延迟 {worst / 1000:.1f}s ≥ 300s (B6① 越限)"))
    return lines


def check_playbook(date: str) -> list[str]:
    lines = [f"【playbook_instructions】 data/ftseek_us.db date={date}"]
    db = REPO / "data" / "ftseek_us.db"
    con = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    da_rows = con.execute(
        "SELECT decision_conf FROM playbook_instructions WHERE date=? "
        "AND raw_instruction_json NOT LIKE '%universe_expansion%'", (date,)).fetchall()
    ue_rows = con.execute(
        "SELECT symbol, direction, skip_reason FROM playbook_instructions WHERE date=? "
        "AND raw_instruction_json LIKE '%universe_expansion%'", (date,)).fetchall()
    con.close()
    if da_rows:
        confs = Counter(r[0] for r in da_rows if r[0] is not None)
        nulls = sum(1 for r in da_rows if r[0] is None)
        lines.append(ok("PB-DA", f"DA 相关指令 {len(da_rows)} 行 (含 intraday_reentry 等最小写路径 "
                                 f"{nulls} 行, 该路径不写 decision_conf)"))
        if confs and all(abs(v - 0.70) < 1e-9 for v in confs):
            lines.append(f"  ℹ️ PB-conf: 全指令行 decision_conf 恒 0.70 延续 ({sum(confs.values())} 行; "
                         f"B12 观察: 常量退化未变, 符合预期)")
        elif confs:
            lines.append(warn("PB-conf", f"decision_conf 分布出现非 0.70 值: {dict(confs)}"))
        else:
            lines.append(warn("PB-conf", "无任何带 decision_conf 的全指令行"))
    else:
        lines.append(bad("PB-DA", "无 DA 指令行 — DA 写库链路异常"))
    if ue_rows:
        skipped = sum(1 for r in ue_rows if r[2])
        lines.append(ok("PB-UE", f"UE 指令 {len(ue_rows)} 行 (skip/未触发 {skipped}): "
                        + ", ".join(f"{s}({d})" for s, d, _ in ue_rows)))
    else:
        lines.append(warn("PB-UE", "无 UE 指令行 (若今晚浮现全为不可注入/未过门, 属可能正常; 结合 scan 记录判断)"))
    return lines


def remove_cron_line() -> str:
    """自删一次性 crontab 行 (成功/失败都删, 防 9/1 固定日期行年度复燃)。"""
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, check=True).stdout
    except Exception as e:
        return f"crontab -l 失败 (跳过自删): {e}"
    kept = [l for l in cur.splitlines() if CRON_MARKER not in l]
    if len(kept) == len(cur.splitlines()):
        return "crontab 中未找到一次性行 (可能已删)"
    try:
        subprocess.run(["crontab", "-"], input="\n".join(kept) + "\n", text=True, check=True)
        return "一次性 crontab 行已自删"
    except Exception as e:
        return f"crontab 自删失败, 需人工清理含 {CRON_MARKER} 的行: {e}"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--date", default=None, help="目标 ET 日 (默认: 当前 ET 日)")
    ap.add_argument("--no-push", action="store_true", help="不推送 (测试用)")
    args = ap.parse_args()
    date = args.date or datetime.now(ET).date().isoformat()

    lines = [f"🔍 DA/UE 2026-08-31 首批生产运行晨间核对 | 目标 ET 日 {date}",
             f"运行时刻 {datetime.now(CST).strftime('%Y-%m-%d %H:%M CST')}"]
    for fn in (check_da, check_ue_scan, check_ue_latency, check_playbook):
        try:
            lines.extend(fn(date))
        except Exception as e:
            lines.append(bad(fn.__name__, f"检查异常: {e}"))

    cron_msg = remove_cron_line()
    lines.append(f"【收尾】{cron_msg}")

    if failures:
        head = f"⚠️ DA/UE 首批核对 {date}: {len(failures)} 项未过"
        level_name = "HIGH"
    else:
        head = f"✅ DA/UE 首批核对 {date}: 全部通过"
        level_name = "INFO"
    output = head + "\n" + "\n".join(lines)
    print(output)

    if not args.no_push:
        try:
            from app.push import get_push_hub
            from app.push.message import Level, Category
            get_push_hub().push_text(
                output,
                level=Level.HIGH if failures else Level.INFO,
                category=Category.HEALTH,
            )
        except Exception as e:
            print(f"[push] 推送失败: {e}")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except SystemExit:
        raise
    except Exception as e:
        print(f"🔴 核对脚本自身崩溃: {e}")
        try:
            remove_cron_line()
        except Exception as _e:
            print(f"WARNING: 一次性 cron 行移除失败, 需人工清理: {_e}")
        sys.exit(2)
