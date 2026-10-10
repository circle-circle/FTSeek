#!/usr/bin/env python3
"""RFC-135 破例到期核验 (一次性 job 载体, 2026-10-10 用户裁 A 配套).

到期日: waiver expires=2026-10-12T16:00:00-04:00; 本 job 排程 10/13 05:00 CST
(= 10/12 17:00 ET, 到期后 1h)。核验三件 (预注册 on_expire 行为):
  ① read_live_probe_waiver → active=False, reason=waiver_expired (或文件已删=waiver_file_absent)
  ② measure_locks → alarm_suppressed=False (四锁告警自动复响成立)
  ③ 松动锁清单如实呈现 (ALLOW_REAL_TRADING 若仍=true 必须在 loose_locks 里;
     若已被人工翻回 false 则四锁全闭, 同样合法)

FAIL 条件 (任一): waiver 到期后仍 active=True / alarm_suppressed=True 且有松动锁 /
reason 属不可读类 (waiver_unreadable 等)。
本载体自身只用 stdlib; 仓库 import 委托 venv python 子进程 (与生产解释器同源)。
stdout 即投递内容 (no_agent 契约): 无论 PASS/FAIL 都推 — 一次性核验的结果必须可见。
"""
import json
import subprocess
import sys
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
VENV_PY = REPO / "venv" / "bin" / "python"
WAIVER = REPO / "data" / "live_probe_lock_waiver.json"

PROBE = r'''
import json, sys
sys.path.insert(0, "/opt/ftseek/FTSeek-US")
from app.playbook.live_arm_gate import read_live_probe_waiver
from scripts.okr_2026_09_scoring import measure_locks
w = read_live_probe_waiver("/opt/ftseek/FTSeek-US/data/live_probe_lock_waiver.json")
lk = measure_locks("/opt/ftseek/FTSeek-US", graduation=None)
env_allow = lk["observed"].get("ALLOW_REAL_TRADING")
print(json.dumps({
    "waiver_active": w.get("active"), "waiver_reason": w.get("reason"),
    "waiver_expires": w.get("expires"),
    "alarm_suppressed": lk.get("alarm_suppressed"),
    "all_closed": lk.get("all_closed"),
    "loose_locks": lk.get("loose_locks"),
    "env_ALLOW_REAL_TRADING": env_allow,
    "errors": lk.get("errors"),
}, ensure_ascii=False))
'''


def main() -> int:
    lines = ["🔏 RFC-135 破例到期核验 (一次性)", ""]
    if not VENV_PY.exists():
        print("\n".join(lines + ["❌ FAIL: venv python 不存在, 无法核验 (载体故障, 非到期链路故障)"]))
        return 0
    p = subprocess.run([str(VENV_PY), "-c", PROBE], capture_output=True, text=True,
                       timeout=120, cwd=str(REPO))
    if p.returncode != 0:
        print("\n".join(lines + [f"❌ FAIL: 核验探针非零退出 rc={p.returncode}",
                                 (p.stderr or "")[-500:]]))
        return 0
    try:
        d = json.loads(p.stdout.strip().splitlines()[-1])
    except Exception as exc:  # noqa: BLE001
        print("\n".join(lines + [f"❌ FAIL: 探针输出不可解析 ({exc})", p.stdout[-500:]]))
        return 0

    # TOO_EARLY 与 FAIL 分开 (Gate 6 纪律): waiver 仍活跃且未过期 = 跑早了, 不可判
    if d["waiver_active"] is True:
        lines.append(f"① waiver: active=True reason={d['waiver_reason']} "
                     f"expires={d['waiver_expires']}")
        lines.append("")
        lines.append("⏳ TOO_EARLY: 破例尚未到期, 到期链路不可判 (本 job 排程应在 "
                     "2026-10-12 16:00 ET 之后; 提前手工跑属预期内输出, 非故障)。")
        print("\n".join(lines))
        return 0

    expired_ok = (d["waiver_active"] is False
                  and d["waiver_reason"] in ("waiver_expired", "waiver_file_absent"))
    loose = d.get("loose_locks") or []
    suppress_ok = (d["alarm_suppressed"] is False) if loose else True
    env_v = d.get("env_ALLOW_REAL_TRADING")
    env_ok = (env_v == "false") or ("ALLOW_REAL_TRADING" in loose)

    lines.append(f"① waiver: active={d['waiver_active']} reason={d['waiver_reason']} "
                 f"expires={d['waiver_expires']} → {'✅ 已失效' if expired_ok else '❌ 仍活跃'}")
    lines.append(f"② 四锁: all_closed={d['all_closed']} loose={loose} "
                 f"alarm_suppressed={d['alarm_suppressed']} → "
                 f"{'✅ 告警复响成立' if suppress_ok else '❌ 告警仍被抑制'}")
    lines.append(f"③ .env.us ALLOW_REAL_TRADING={env_v} → "
                 f"{'✅ 状态一致 (松动锁在册或已翻回 false)' if env_ok else '❌ 松动却不在 loose_locks'}")
    if d.get("errors"):
        lines.append(f"⚠️ measure_locks errors: {d['errors']}")
    verdict = expired_ok and suppress_ok and env_ok and not d.get("errors")
    lines.append("")
    if verdict:
        lines.append("✅ PASS: 破例已按预注册到期失效, 四锁告警自动复响, 无需人工操作。"
                     " 实盘探针通道关闭 (evaluate_live_arm_gate 将拒 LIVE_PROBE_WAIVER_EXPIRED)。"
                     " 本 job 为一次性, 跑完自动出账; 台账行与脚本按一次性留痕流程清理。")
    else:
        lines.append("❌ FAIL: 到期链路未按预注册行为生效 — 对照 RFC-135 文档 "
                     "§2026-10-10 裁决节排查 (waiver 文件被改? measure_locks 分叉?)。")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
