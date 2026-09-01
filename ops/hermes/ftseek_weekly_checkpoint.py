#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""FTSeek-US 周六周检 (no_agent, 2026-08-23 立, 用户裁"需要"):
一次跑齐 OKR-2026-09 的三个周度检查点, 输出一段紧凑汇报 (stdout 即推送):

  1. 实盘四锁 + 毕业门禁 (KR4: 打标前严格不切实盘)
  2. 参数冻结核查 (KR2: 08-24 起 config_version 唯一值数必须=1)
  3. VWAP 斜率证伪门周检 (DEB-20260823-phase1, 预注册判据, 宪章第9律)

设计: 确定性脚本零推理; 任一检查异常输出告警行 (不静默)。
挂载: Hermes cron 周六 09:05 CST (=ET 周五 21:05, 全周数据已落)。
回滚: 删本脚本 + 对应 cron job。
"""
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
DB = REPO / "data" / "ftseek_us.db"
REPORT = REPO / "data" / "falsification_gate_review_report.json"
FREEZE_START = "2026-08-24"  # OKR-2026-09 KR2 冻结窗口起点


def check_locks():
    lines = (REPO / ".env.us").read_text(encoding="utf-8").splitlines()
    env = {l.split("=", 1)[0]: l.split("=", 1)[1]
           for l in lines if "=" in l and not l.strip().startswith("#")}
    cfg = (REPO / "config/unified_config.yaml").read_text(encoding="utf-8")
    locks = {
        "AUTO_TRADE_ACCOUNT": env.get("AUTO_TRADE_ACCOUNT", "?"),
        "ALLOW_REAL_TRADING": env.get("ALLOW_REAL_TRADING", "?"),
        "execute_mode": "futu_simulate" if "execute_mode: futu_simulate" in cfg
        else "⚠️ 非simulate",
    }
    ok = (locks["AUTO_TRADE_ACCOUNT"] == "sim"
          and locks["ALLOW_REAL_TRADING"] == "false"
          and locks["execute_mode"] == "futu_simulate")
    return ok, locks


def check_graduation():
    """毕业门禁实测 (打标状态)。异常按 fail-close 报。

    ⚠️ 2026-08-24: 底层 ic_tracker._check_graduation 已 DEPRECATED
    (confidence-based 判据测常量无意义, 8/24 修尺子裁决)。
    graduated 值仅供兼容展示, 不作任何决策依据; 新判据待按日聚类尺子预注册+用户批准。
    """
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    code = ("from app.playbook.pipeline import _check_graduation_gate; "
            "import json; print(json.dumps(_check_graduation_gate()))")
    r = subprocess.run([str(REPO / "venv/bin/python"), "-c", code],
                       capture_output=True, text=True, timeout=90,
                       cwd=str(REPO), env=env)
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return {"graduated": False, "reason": f"查询失败: {r.stderr[-120:]}"}


def check_freeze():
    cc = sqlite3.connect(str(DB), timeout=5)
    rows = cc.execute(
        "SELECT COALESCE(json_extract(metadata_json,'$.config_version'),'(null)') v,"
        " COUNT(*) n FROM trade_records WHERE source_type='PLAYBOOK_SIM' "
        "AND exit_time IS NOT NULL AND trade_date>=? GROUP BY v",
        (FREEZE_START,)).fetchall()
    cc.close()
    total = sum(n for _, n in rows)
    return rows, total


def check_gate():
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts/falsification_gate_weekly_review.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    if r.returncode != 0:
        return {"error": f"周检脚本失败: {r.stderr[-150:]}"}
    try:
        return json.loads(REPORT.read_text(encoding="utf-8"))
    except Exception:
        return {"error": "报告文件读取失败"}


def check_service():
    r = subprocess.run(["systemctl", "is-active", "ftseek-us"],
                       capture_output=True, text=True)
    return r.stdout.strip()


def check_graduation_gate():
    """锚限价影子毕业主门 (DEB-20260806 §4.3 预注册; 停止规则: 每周五机械判定)."""
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts" / "limit_entry_graduation_gate.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    try:
        rep = json.loads((REPO / "data" / "limit_entry_graduation_report.json")
                         .read_text(encoding="utf-8"))
        return rep
    except Exception:
        return {"error": r.stderr[-150:] or "报告读取失败"}


def check_e2_gate():
    """E2 入场动量确认毕业门 (DEB-20260807 §5.1 预注册)."""
    r = subprocess.run([str(REPO / "venv/bin/python"),
                        str(REPO / "scripts" / "e2_momentum_graduation_gate.py")],
                       capture_output=True, text=True, timeout=120, cwd=str(REPO))
    try:
        rep = json.loads((REPO / "data" / "e2_momentum_graduation_report.json")
                         .read_text(encoding="utf-8"))
        return rep
    except Exception:
        return {"error": r.stderr[-150:] or "报告读取失败"}


def check_shadow_registry():
    """影子注册表汇总 (2026-08-23 接入, 原模块零引用): 各影子攒样进度 + 状态变迁."""
    code = ("from app.playbook.shadow_registry import refresh_counts, summary, check_mounting; "
            "import json; notifs = refresh_counts(); "
            "print(json.dumps({'notifs': notifs, 'summary': summary(), "
            "'mounts': check_mounting()}, ensure_ascii=False))")
    r = subprocess.run([str(REPO / "venv/bin/python"), "-c", code],
                       capture_output=True, text=True, timeout=90,
                       cwd=str(REPO))
    try:
        return json.loads(r.stdout.strip().splitlines()[-1])
    except Exception:
        return None


def main():
    from datetime import date
    out = [f"【FTSeek-US 周六周检 {date.today()}】"]

    # 0. 服务
    svc = check_service()
    out.append(f"服务: {'✓' if svc == 'active' else '⚠️ ' + svc}")

    # 1. 四锁 + 打标
    ok, locks = check_locks()
    grad = check_graduation()
    mark = "✓" if ok and not grad.get("graduated") else "⚠️"
    out.append(f"{mark} 四锁: {locks['AUTO_TRADE_ACCOUNT']}/"
               f"{locks['ALLOW_REAL_TRADING']}/{locks['execute_mode']} | "
               f"毕业门禁 graduated={grad.get('graduated')} "
               f"({'维持拦截' if not grad.get('graduated') else '⚠️ 已打标! 需用户签字确认'})")

    # 2. 参数冻结
    rows, total = check_freeze()
    if total == 0:
        out.append(f"参数冻结: 窗口({FREEZE_START}起)尚无平仓样本")
    elif len(rows) == 1:
        out.append(f"✓ 参数冻结: config_version 唯一 ({rows[0][0][:16]}…, n={total})")
    else:
        # KR2 例外通道: config_freeze_baseline.json exit_versions 豁免清单
        # (已裁决版本分裂不重复告警; 2026-09-01 体检定性 8/29 early_tight_sl 毕业案后加)
        exempt = {}
        _exempt_err = None
        try:
            _bl = json.loads((REPO / "data" / "config_freeze_baseline.json")
                             .read_text(encoding="utf-8"))
            exempt = _bl.get("exit_versions", {})
        except Exception as e:
            # R5 留痕: 豁免清单不可读必须显性 (否则退回全告警, 不静默)
            _exempt_err = str(e)[:80]
        unknown = [(v, n) for v, n in rows if v not in exempt]
        if not unknown:
            desc = " / ".join(f"{v[:16]}…:{n}" for v, n in rows)
            out.append(f"✓ 参数冻结: {len(rows)} 个版本均在豁免清单 (KR2 例外通道, "
                       f"见 config_freeze_baseline.json) — {desc}")
        else:
            desc = " / ".join(f"{v[:16]}…:{n}" for v, n in unknown)
            out.append(f"⚠️ 参数冻结已破: {len(unknown)} 个未豁免版本 — {desc} (KR2 破, 需复盘)")
        if _exempt_err:
            out.append(f"⚠️ 豁免清单读取失败 (按未豁免告警口径处理): {_exempt_err}")

    # 3. 证伪门
    g = check_gate()
    if g.get("error"):
        out.append(f"⚠️ 证伪门周检: {g['error']}")
    else:
        out.append(
            f"证伪门(VWAP斜率): 遥测拦 {g.get('n_telemetry_blocks', g.get('n_shadow_rows', 0))} = "
            f"真被拦 {g.get('n_true_blocked', 0)} + 延迟成交 "
            f"{g.get('n_delayed_executed', 0)}")
        if g.get("n_with_counterfactual"):
            out.append(
                f"  被拦组净收益 pooled={g.get('blocked_pooled_mean_net_ret')} | "
                f"日聚类差值={g.get('blocked_daily_clustered_diff_mean')} "
                f"t={g.get('blocked_daily_clustered_t')} MDE={g.get('mde_daily_clustered')}")
        out.append(f"  判定: {g.get('verdict')}")

    # 4. 锚限价影子毕业主门 (DEB-20260806 §4.3, 每周五机械判定; 未过=继续攒样, 不重置)
    lg = check_graduation_gate()
    if lg.get("error"):
        out.append(f"⚠️ 锚限价毕业门: {lg['error']}")
    else:
        sf = lg.get("sign_flip_permutation", {})
        dc = lg.get("double_condition", {})
        sg = lg.get("sign_common_gate", {})
        out.append(
            f"锚限价影子(主门5m): n={lg.get('n_paired_passive')} "
            f"δ̄=${lg.get('delta_mean')} 置换p={sf.get('one_sided_p')} | "
            f"双条件[δ̄≥$1:{dc.get('mean_ge_1')}, CI下界>0:{dc.get('ci_lo_gt_0')}] | "
            f"符号共门p={sg.get('one_sided_p')}")
        out.append(f"  判定: {lg.get('verdict')} (停止规则: 只降级不重置)")

    # 4b. E2 入场动量确认毕业门 (DEB-20260807 §5.1)
    e2 = check_e2_gate()
    if e2.get("error"):
        out.append(f"⚠️ E2动量毕业门: {e2['error']}")
    else:
        dc2 = e2.get("daily_clustered", {})
        out.append(
            f"E2动量确认: 拦截n={e2.get('n_blocked')} 净${e2.get('blocked', {}).get('net')} "
            f"(均${e2.get('blocked', {}).get('mean')}) vs 放行n={e2.get('n_passed')} "
            f"净${e2.get('passed', {}).get('net')} (均${e2.get('passed', {}).get('mean')})")
        out.append(
            f"  日聚类 {dc2.get('n_paired_days')}配对日 diff=${dc2.get('diff_mean')} "
            f"t={dc2.get('t')} MDE={dc2.get('mde')}")
        out.append(f"  判定: {e2.get('verdict')}")

    # 5. 影子注册表 (分流对比组 + 全部影子攒样进度)
    reg = check_shadow_registry()
    if reg is None:
        out.append("⚠️ 影子注册表读取失败")
    else:
        # 挂载核对哨兵 (2026-09-01): 悬空观察/产出断流告警优先于进度汇总
        for m in reg.get("mounts", []):
            out.append(m)
        for n in reg.get("notifs", []):
            out.append(f"🔔 {n}")
        out.append(reg.get("summary", ""))

    print("\n".join(out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
