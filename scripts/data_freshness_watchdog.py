#!/usr/bin/env python3
"""数据新鲜度看门狗 (2026-07-23)。

回答: 那些"本应持续写入"的数据流, 是否有谁静默停更了 ——
      LLM 审计库空转两个月无人知 / shadow 墓地(4 个停更无人察觉)的根因,
      都是"降级不报警"。本脚本对关键数据源做 last_write_age 检查, 超期即飞书告警。

设计:
  - 纯只读 stat mtime (或 jsonl 末行时间), 零 live 行为变更。
  - 每个源配 max_age_hours + 仅工作日选项 (盘后才更新的源, 周末不告警)。
  - 失败安全: 单源异常不影响其他源; 推送异常吞掉。
  - 自我兜底: 本脚本也在监控 llm_audit.db, 形成"观测的观测"。
运行: PYTHONPATH=. ./venv/bin/python scripts/data_freshness_watchdog.py [--push] [--dry]
"""
import argparse
import time
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data"

try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    load_dotenv(ROOT / ".env.us")
except Exception:
    pass

# ── 监控清单: (相对路径, 最大静默小时, 说明, 仅工作日) ──
#   max_age_hours: 超过此时长未更新 → 告警
#   weekday_only: True 时周末(周六日)不告警 (盘后才写的源)
WATCHLIST = [
    ("data/llm_audit.db",                          30, "LLM 审计库 (成本观测地基)",        False),
    ("data/opportunity_scan_{today}.json",         30, "DA 指令生成 bridge",               True),
    ("data/factor_ic_latest.json",                 30, "因子 IC 回填",                     True),
    ("data/shadow/llm_universe_scan.jsonl",        30, "UE 宇宙扫描 shadow",               True),
    ("data/shadow/premarket_peer_shadow.jsonl",    30, "盘前 peer shadow",                 True),
    ("data/shadow/divergence_arbitration_shadow.jsonl", 48, "分歧仲裁 shadow",             True),
    ("data/shadow/exec_gate_audit.jsonl",          30, "执行闸门审计",                     True),
    ("data/shadow/sltp_verify_shadow.jsonl",       30, "止盈止损校验 shadow",             True),
    # 已知墓地 (DEB-20260723 time-exit-censoring 实证6): 若已裁决停用应从清单移除, 否则持续告警提醒清理
    ("data/shadow/prompt_eval.jsonl",              72, "[疑墓地] prompt A/B 评估",         True),
    ("data/shadow/shadow_technical_direction.jsonl", 72, "[疑墓地] 技术方向 shadow",       True),
    ("data/shadow/playbook_direction_audit.jsonl", 72, "[疑墓地] playbook 方向审计",      True),
    ("data/shadow/edge_gate_shadow.jsonl",         72, "[疑墓地] edge gate shadow",        True),
    # 2026-07-26 减法R2-Q6 (裁决: 不建新注册表, 高危文件入现有 watchdog): "生产者死了消费者不知道"
    #   三文件 — SHORT 门控两件 (88天空文件静默 fail-open 实案, 责任 DEB-20260726-subtraction-R2 Q1)
    #   + price_fail_closed (责任同 DEB Q2, 已清空保留机制)。生产者待接线前持续告警=设计意图。
    ("data/hard_to_borrow.json",                  168, "[SHORT门控] 借券成本数据 (生产者待裁)", False),
    ("data/short_interest.json",                  168, "[SHORT门控] 轧空数据 (生产者待裁)",     False),
    ("data/price_fail_closed.json",               999, "[人工名单] 期权跳过集 (仅人工维护, 静止正常)", False),
]


def _resolve(rel: str) -> Path:
    today = datetime.now().strftime("%Y-%m-%d")
    return ROOT / rel.format(today=today)


def _age_hours(path: Path) -> float:
    return (time.time() - path.stat().st_mtime) / 3600.0


def _is_weekend() -> bool:
    return datetime.now().weekday() >= 5  # 5=周六 6=周日


def check(dry=False):
    weekend = _is_weekend()
    stale, missing, ok = [], [], []
    for rel, max_age, desc, weekday_only in WATCHLIST:
        if weekday_only and weekend:
            continue
        path = _resolve(rel)
        try:
            if not path.exists():
                missing.append((rel, desc))
                continue
            age = _age_hours(path)
            if age > max_age:
                stale.append((rel, desc, age, max_age))
            else:
                ok.append((rel, age))
        except Exception as e:
            missing.append((rel, f"{desc} (stat异常:{e})"))

    return stale, missing, ok


def _build_message(stale, missing) -> str:
    if not stale and not missing:
        return None  # 全新鲜 → 不推送 (无噪音)
    lines = ["🛰️ 数据新鲜度告警"]
    if stale:
        lines.append("── 静默停更 (超期未写入) ──")
        for rel, desc, age, max_age in stale:
            days = age / 24
            age_str = f"{age:.0f}h" if age < 48 else f"{days:.1f}天"
            lines.append(f"⏰ {desc}: 已 {age_str} 未更新 (阈值 {max_age}h)")
    if missing:
        lines.append("── 文件缺失 ──")
        for rel, desc in missing:
            lines.append(f"❓ {desc}: {rel}")
    return "\n".join(lines)


def main(push=False, dry=False):
    stale, missing, ok = check(dry=dry)
    print("=" * 60)
    print(f"数据新鲜度看门狗 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"正常 {len(ok)} | 停更 {len(stale)} | 缺失 {len(missing)}")
    for rel, desc, age, max_age in stale:
        print(f"  [STALE] {desc}: {age:.0f}h > {max_age}h")
    for rel, desc in missing:
        print(f"  [MISS ] {desc}: {rel}")
    for rel, age in ok:
        print(f"  [ok   ] {rel}: {age:.1f}h")
    print("=" * 60)

    msg = _build_message(stale, missing)
    if msg and push and not dry:
        try:
            from app.notifier.feishu import send_text
            send_text(msg)
            print("[pushed] 飞书")
        except Exception as e:
            print(f"[warn] 推送失败: {e}")
    elif not msg:
        print("[ok] 全部新鲜, 不推送")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="数据新鲜度看门狗 (只读)")
    ap.add_argument("--push", action="store_true", help="有告警时推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    a = ap.parse_args()
    main(push=a.push, dry=a.dry)
