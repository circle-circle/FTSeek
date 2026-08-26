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
    ("data/factor_ic_latest.json",                 50, "因子 IC 回填 (Tue-Sat cron, 周一 gap 49h 为计划内)", True),
    ("data/shadow/llm_universe_scan.jsonl",        30, "UE 宇宙扫描 shadow",               True),
    ("data/shadow/premarket_peer_shadow.jsonl",    30, "盘前 peer shadow",                 True),
    ("data/shadow/divergence_arbitration_shadow.jsonl", 48, "分歧仲裁 shadow",             True),
    ("data/shadow/exec_gate_audit.jsonl",          30, "执行闸门审计",                     True),
    ("data/shadow/sltp_verify_shadow.jsonl",       30, "止盈止损校验 shadow",             True),
    # 2026-08-26 复查加固: U11 确认门兜底告警 (主防线=双季cron+当日幂等+窗口守卫日志)。
    #   74h 实测口径 (8/26 独立复核修正原"假日不误报"注释, 证伪见 DEB-20260825 复核段):
    #   美国假日后第一个交易日 06:25 检查必误报一次 (周一假日→周二 80.75h; 连休两天→104.75h),
    #   当晚 confirm 跑后自恢复, 零数据污染 — 误报本身证明链路活着, 接受为已知噪音。
    #   代价: 真静默死亡最坏 ~5 天检出 — 可接受, 本条只是兜底中的兜底。
    ("data/shadow/ue_confirm_gate.jsonl",         74, "UE 开盘确认门 shadow (U11)",       True),
    # 已知墓地 (DEB-20260723 time-exit-censoring 实证6): 若已裁决停用应从清单移除, 否则持续告警提醒清理
    # 2026-08-26: prompt_eval + shadow_technical_direction 已归档至
    #   data/shadow/archive_20260826/ (上游停产实锤, 全影子普查裁决), 条目移除。
    ("data/shadow/playbook_direction_audit.jsonl", 72, "playbook 方向审计 (活跃, 8/26普查4消费方)", True),
    ("data/shadow/edge_gate_shadow.jsonl",         72, "[疑墓地] edge gate shadow",        True),
    # 2026-07-26 减法R2-Q6 (裁决: 不建新注册表, 高危文件入现有 watchdog): "生产者死了消费者不知道"
    #   price_fail_closed (责任 DEB-20260726 Q2, 已清空保留机制)。
    #   [2026-08-23] SHORT 门控两件 (hard_to_borrow/short_interest.json) 已随死门删除移出清单 —
    #   用户裁"不值得" (88天空文件静默 fail-open 实案, 责任同 R2 Q1), 消费门已删, 文件不再需要。
    ("data/price_fail_closed.json",               999, "[人工名单] 期权跳过集 (仅人工维护, 静止正常)", False),
]


def _now_et():
    """[v0.70 审计修复 2026-08-26] {today} 占位符必须按 ET 日期解析.

    主机是 CST: 06:25 CST 运行时 ET 还是前一天 (17:25 ET), bridge 文件由生产端
    按 ET 日期命名 → 用 CST 日期会找"今天"的文件 (19:00CST 后才产出) → 每天必报
    一次假 MISS (实测 8/26 06:25 告警实锤, 当时昨日文件仅 11h 新鲜).
    """
    try:
        from zoneinfo import ZoneInfo
        return datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        try:
            import pytz
            return datetime.now(pytz.timezone("America/New_York"))
        except Exception:
            return datetime.now()  # 最后兜底: 维持旧行为 (已知有每日假MISS)


def _resolve(rel: str) -> Path:
    today = _now_et().strftime("%Y-%m-%d")
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
            # [v0.70] 显示解析后的真实路径 (原样输出 rel 会把 {today} 占位符直接暴露在告警里)
            try:
                shown = str(_resolve(rel))
            except Exception:
                shown = rel
            lines.append(f"❓ {desc}: {shown}")
    return "\n".join(lines)


def main(push=False, dry=False):
    stale, missing, ok = check(dry=dry)
    print("=" * 60)
    print(f"数据新鲜度看门狗 {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"正常 {len(ok)} | 停更 {len(stale)} | 缺失 {len(missing)}")
    for rel, desc, age, max_age in stale:
        print(f"  [STALE] {desc}: {age:.0f}h > {max_age}h")
    for rel, desc in missing:
        try:
            _shown = str(_resolve(rel))
        except Exception:
            _shown = rel
        print(f"  [MISS ] {desc}: {_shown}")
    for rel, age in ok:
        print(f"  [ok   ] {rel}: {age:.1f}h")
    print("=" * 60)

    msg = _build_message(stale, missing)
    if msg and push and not dry:
        # 2026-08-09 (告警风暴消除): 文件持久化冷却 — 30min 内同内容不重复推.
        #   看门狗是 cron 独立进程, 进程内状态无效; 持久化到 data/watchdog_last_push.json.
        #   若 OpenD/Futu 持续不可用, watchdog 每轮都检到停更 → 无冷却=每轮1条告警风暴.
        import json as _json
        _state_file = DATA / "watchdog_last_push.json"
        _now = time.time()
        _suppress = False
        try:
            if _state_file.exists():
                with open(_state_file) as _f:
                    _prev = _json.load(_f)
                if _prev.get("msg_hash") == _hash_msg(msg) and _now - _prev.get("ts", 0) < 1800:
                    _suppress = True
        except Exception:
            pass
        if _suppress:
            print("[cooldown] 30min 内同告警已推过, 跳过")
        else:
            try:
                from app.notifier.feishu import send_text
                send_text(msg)
                print("[pushed] 飞书")
                try:
                    with open(_state_file, "w") as _f:
                        _json.dump({"ts": _now, "msg_hash": _hash_msg(msg)}, _f)
                except Exception:
                    pass
            except Exception as e:
                print(f"[warn] 推送失败: {e}")
    elif not msg:
        print("[ok] 全部新鲜, 不推送")


def _hash_msg(msg: str) -> str:
    import hashlib
    return hashlib.md5(msg.encode("utf-8")).hexdigest()[:16]


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="数据新鲜度看门狗 (只读)")
    ap.add_argument("--push", action="store_true", help="有告警时推送飞书")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    a = ap.parse_args()
    main(push=a.push, dry=a.dry)
