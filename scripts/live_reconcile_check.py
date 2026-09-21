#!/usr/bin/env python3
"""实盘对账检查 (RFC-062 在建清单⑤ / 门2 G2-3, 2026-08-02) — 券商持仓 vs 内部账本.

为什么: R4 实证 SIM 账面都会漂移 (50 项修复) — 实盘漂移=真金白银。本脚本对比
  broker 持仓 (futu position_list_query, 经 manual_order_service 在产连接) 与内部
  open_positions 表, 漂移/查询失败即写审计 jsonl + PushHub 告警。SIM 期即跑
  (G2-3 要求实盘连续 5 日零漂移 — 本脚本即该判据的数据源)。

口径: symbol+direction+qty 三元组对比 (qty 容差 1e-6); broker 空仓=空集。
  内部 open_positions 是唯一内部真相源 (R4 口径)。
输出: data/audit/reconcile_live.jsonl 逐行 {ts, status, broker_n, internal_n, drift}。
  status: ok / drift / broker_query_failed。告警仅 drift 与 broker_query_failed。

用法: venv/bin/python scripts/live_reconcile_check.py
cron (建议, 盘中每 30min, 待用户注册):
  */30 21-23,0-4 * * 2-6 cd /opt/ftseek/FTSeek-US && venv/bin/python scripts/live_reconcile_check.py >> logs/reconcile_live.log 2>&1
"""
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "audit" / "reconcile_live.jsonl"


def _internal_open() -> dict:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT symbol, direction, size FROM open_positions").fetchall()
    conn.close()
    return {sym: (str(direction).upper(), float(size or 0)) for sym, direction, size in rows}


def _traded_symbols() -> set:
    conn = sqlite3.connect(str(DB))
    rows = conn.execute("SELECT DISTINCT symbol FROM trade_records").fetchall()
    conn.close()
    return {r[0] for r in rows}


def _external_allowlist() -> set:
    """共享 SIM 账户已知外部持仓 (live_readiness.reconcile_external_symbols).

    2026-08-02 实证: HK.00700/00981 (本系统不交易港股); US.SNDK broker LONG 44
    但系统史上全 SHORT 且笔笔已平 — 数学上不可能是系统残留; US.BABA 50 股不匹配
    任何系统手数 (33/21/23/22) 且 07-02 有 BROKER_GHOST_CLOSE 记录。裁决前先入
    白名单防误报, 用户可从 config 删除任一标的恢复告警。
    """
    try:
        from app.core.config_container import get_config
        vals = get_config().get_dotted("live_readiness.reconcile_external_symbols", []) or []
        return {str(s) for s in vals}
    except Exception:
        return set()


def _broker_positions() -> tuple:
    """查询 broker 持仓 — 环境对齐自动交易账户 (AUTO_TRADE_ACCOUNT).

    2026-08-11 根因修复: 旧实现复用 manual_order_service._trd_env(), 其 _is_live()
    硬编码 True (Phase 0 三态共存: 手动交易固定 REAL) → 对账一直查 REAL 账户与 SIM
    内部持仓比对, 有持仓日必 drift (8/10 TSM internal_only 实锤; 白名单项 HK.00981/
    US.SNDK 实为 REAL 账户持仓, 被误标"共享SIM外部持仓"一周)。
    环境解析与 di_container.py AUTO_TRADE_ACCOUNT 同源 (.env.us 实时读取, cron 无
    EnvironmentFile 也能工作); real 需 ALLOW_REAL_TRADING=true 安全闸。
    返回 (positions_dict, account_label)。
    """
    from app.core.trading_mode import _read_env_file
    from futu import OpenSecTradeContext, TrdEnv, TrdMarket

    account = _read_env_file("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
    if account == "real" and _read_env_file("ALLOW_REAL_TRADING", "").strip().lower() != "true":
        account = "sim"  # 与 di_container 安全闸一致: 强制降级
    trd_env = TrdEnv.REAL if account == "real" else TrdEnv.SIMULATE
    acc_id = int(_read_env_file("REAL_ACCOUNT_ID" if account == "real" else "SIMULATE_ACCOUNT_ID", "0"))

    host = _read_env_file("FUTU_OPEND_HOST", "127.0.0.1")
    port = int(_read_env_file("FUTU_OPEND_PORT", "11111"))
    ctx = OpenSecTradeContext(filter_trdmarket=TrdMarket.US, host=host, port=port, is_encrypt=False)
    try:
        if trd_env == TrdEnv.REAL:
            pwd_md5 = _read_env_file("FUTU_UNLOCK_PWD_MD5", "")
            if not pwd_md5:
                raise RuntimeError("REAL 账户对账需 FUTU_UNLOCK_PWD_MD5")
            ret, data = ctx.unlock_trade(password_md5=pwd_md5, is_unlock=True)
            if ret != 0:
                raise RuntimeError(f"unlock_trade failed: {data}")
        ret, positions = ctx.position_list_query(trd_env=trd_env, acc_id=acc_id)
        if ret != 0:
            raise RuntimeError(f"position_list_query failed: {positions}")
        out = {}
        for _, row in positions.iterrows():
            qty = float(row.get("qty", 0) or 0)
            if qty == 0:
                continue
            side = str(row.get("position_side", "")).upper()
            direction = "SHORT" if (qty < 0 or side == "SHORT") else "LONG"
            out[str(row["code"])] = (direction, abs(qty))
        return out, f"{account}(acc={acc_id})"
    finally:
        ctx.close()


def _diff(a: dict, b: dict, traded: set, allowlist: set) -> tuple:
    """对比内部(a) vs broker(b)。返回 (drift, external)。

    broker_only 且 (在 config 白名单 或 系统史上从未交易过该标的, 如共享 SIM 账户
    里的手动/港股持仓) → external (报出不告警); 其余 broker_only / internal_only /
    mismatch → 真漂移。
    """
    drift, external = [], []
    for sym in sorted(set(a) | set(b)):
        ia, ib = a.get(sym), b.get(sym)
        if ia is None:
            if sym in allowlist:
                external.append({"symbol": sym, "issue": "external_allowlist", "broker": ib})
            elif sym not in traded:
                external.append({"symbol": sym, "issue": "external_broker_only", "broker": ib})
            else:
                drift.append({"symbol": sym, "issue": "broker_only", "broker": ib})
        elif ib is None:
            drift.append({"symbol": sym, "issue": "internal_only", "internal": ia})
        elif ia[0] != ib[0] or abs(ia[1] - ib[1]) > 1e-6:
            drift.append({"symbol": sym, "issue": "mismatch", "internal": ia, "broker": ib})
    return drift, external


def _alert(msg: str):
    try:
        from app.push import get_push_hub
        from app.push.message import Level, Category
        # [RFC-085·续2 2026-09-21] sync=True 必须: __main__ 末尾 os._exit() 会在异步
        #   send worker 投递前杀掉进程 ⇒ 漂移告警静默丢失 (与 UE 推送同根因)。
        get_push_hub().push_text(msg, level=Level.HIGH, category=Category.HEALTH,
                                 sync=True)
    except Exception:
        pass


def _last_status() -> str:
    """上一条审计记录的 status — 仅状态变化时告警 (30min cron 下 OpenD 长断不刷屏)."""
    try:
        with open(OUT, encoding="utf-8") as f:
            lines = f.read().strip().splitlines()
        return json.loads(lines[-1]).get("status", "") if lines else ""
    except Exception:
        return ""


def main() -> int:
    no_alert = "--no-alert" in sys.argv
    ts = datetime.now(timezone.utc).isoformat()
    internal = _internal_open()
    prev = _last_status()
    try:
        broker, acct_label = _broker_positions()
    except Exception as e:
        rec = {"ts": ts, "status": "broker_query_failed", "error": str(e),
               "internal_n": len(internal)}
        with open(OUT, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        print(f"[reconcile] broker 查询失败: {e} (内部 {len(internal)} 仓)")
        if not no_alert and prev != "broker_query_failed":
            _alert(f"⚠️ 对账检查: broker 持仓查询失败\n{e}\n内部持仓 {len(internal)} 个")
        return 0

    drift, external = _diff(internal, broker, _traded_symbols(), _external_allowlist())
    status = "drift" if drift else "ok"
    rec = {"ts": ts, "status": status, "account": acct_label, "broker_n": len(broker),
           "internal_n": len(internal), "external_n": len(external),
           "drift": drift, "external": external}
    with open(OUT, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    print(f"[reconcile] {status} [{acct_label}]: broker {len(broker)} vs 内部 {len(internal)} "
          f"· 漂移 {len(drift)} · 外部持仓 {len(external)} (不告警)")
    if drift and not no_alert and prev != "drift":
        lines = "\n".join(f"  {d['symbol']}: {d['issue']}" for d in drift)
        _alert(f"🚨 对账漂移 {len(drift)} 项 (broker {len(broker)} vs 内部 {len(internal)})\n{lines}")
    return 0


if __name__ == "__main__":
    import os
    _rc = main()
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(_rc)  # futu SDK 后台线程阻解释器退出 → 硬退出 (审计行已在 main 内落盘)
