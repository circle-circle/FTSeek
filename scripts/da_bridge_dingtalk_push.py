#!/usr/bin/env python3
"""盘前 DA bridge 生成完成 · 钉钉摘要推送 (2026-07-12)。

背景: 指令来源已收敛为单一作业 A(盘前 ET06:00 deep-stock-analysis)。
原 B(bridge-generation)自带的钉钉"盘前Bridge生成完成"摘要随 B 暂停而消失,
本脚本把该摘要独立出来: 读 A 写的 bridge 文件 (opportunity_scan_{ET日期}.json) → 推钉钉。

设计:
  - 与 A 解耦: 不改 A 的 prompt; 单挂 cron 在 A 完成之后 (盘前) 触发。
  - 新鲜度守卫: 只推"今日 ET"的 bridge 文件; 文件不存在/非今日 → 跳过不推 (绝不推陈旧)。
  - 失败安全: 取价/推送异常都吞掉, 绝不抛错。
运行: PYTHONPATH=. ./venv/bin/python scripts/da_bridge_dingtalk_push.py [--date YYYY-MM-DD] [--dry]
"""
import argparse
import json
import os
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.utils.timezone import ET

DATA = Path(__file__).resolve().parents[1] / "data"

# 自含 dotenv 加载: FINNHUB_API_KEY / 钉钉 webhook 均在 .env.us, 保证 cron 下也能拿到
try:
    from dotenv import load_dotenv
    _ROOT = Path(__file__).resolve().parents[1]
    load_dotenv(_ROOT / ".env")
    load_dotenv(_ROOT / ".env.us")
except Exception:
    pass


def _today_et():
    return datetime.now(ET).strftime("%Y-%m-%d")


def _bridge_path(date):
    return DATA / f"opportunity_scan_{date}.json"


def _finnhub_price(symbol):
    """best-effort 取现价, 失败返回 0。"""
    key = os.getenv("FINNHUB_API_KEY", "")
    if not key:
        return 0.0
    sym = symbol.replace("US.", "")
    try:
        url = f"https://finnhub.io/api/v1/quote?symbol={sym}&token={key}"
        with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as resp:
            return float(json.loads(resp.read()).get("c", 0) or 0)
    except Exception:
        return 0.0


def _health_section(date):
    """DEB-20260819 §十九-P0-2: 全链健康心跳 (静默降级可见化).

    05:50 cron 中段失败 (富途断连等) 是静默降级 — 卡陈旧仍注入。
    推送加一行健康摘要: 卡重建状态/日K新鲜度/regime。失败安全: 全 try。
    """
    # §二十四-N3: 基准从"calendar当日"改"全池日K数据末日" — 周一/节后不误报
    #   (周六重建的卡含周五数据=周一DA的正确输入; 标签对数据, 不对日历)
    #   顺带终局票(SKYT类)自然识别: 落后全池末日>1自然日=陈旧
    parts = []
    pool_max_end = ""
    try:
        import glob as _glob
        files = sorted(_glob.glob(str(DATA / "replay_bars_daily" / "US_*.json")))
        ends = {}
        for fp in files:
            try:
                ends[fp] = json.loads(open(fp).read())[-1][0][:10]
            except Exception:
                ends[fp] = ""
        if ends:
            pool_max_end = max(ends.values())
            from datetime import datetime as _dt
            def _lag(e):
                if not e:
                    return 999
                return (_dt.strptime(pool_max_end, "%Y-%m-%d")
                        - _dt.strptime(e, "%Y-%m-%d")).days
            # §二十四-N1: 终局票(落后>7天,同构建器判别)剔出分母, 防心跳永久⚠️报警疲劳
            final_n = sum(1 for e in ends.values() if _lag(e) > 7)
            active = {fp: e for fp, e in ends.items() if _lag(e) <= 7}
            bad = [fp for fp, e in active.items() if _lag(e) > 1]
            n_ok = len(active) - len(bad)
            note = f"+{final_n}终局" if final_n else ""
            parts.append(f"日K{'✅' if not bad else '⚠️陈旧'}"
                         f"({n_ok}/{len(active)}@{pool_max_end}{note})")
    except Exception:
        pass
    try:
        cards = list((DATA / "symbol_rule_cards").glob("*.json"))
        if cards:
            if pool_max_end:
                # 卡新鲜 = generated >= 全池数据末日 (重建晚于数据即新鲜)
                n_fresh = 0
                for fp in cards:
                    try:
                        g = json.loads(fp.read_text()).get("generated", "")
                        n_fresh += 1 if g >= pool_max_end else 0
                    except Exception:
                        pass
            else:
                n_fresh = sum(1 for fp in cards
                              if json.loads(fp.read_text()).get("generated") == date)
            mark = ("✅全部新鲜" if n_fresh == len(cards)
                    else f"⚠️{n_fresh}/{len(cards)}新鲜")
            parts.append(f"股性卡{mark}")
        else:
            parts.append("股性卡⚠️缺失")
    except Exception:
        pass
    try:
        import sqlite3 as _sq
        con = _sq.connect(str(DATA / "ftseek_us.db"))
        r = con.execute("SELECT date, regime FROM playbook_regime_log "
                        "ORDER BY date DESC LIMIT 1").fetchone()
        if r:
            parts.append(f"regime={r[1]}({r[0]})")
        con.close()
    except Exception:
        pass
    # H3 (§二十二): 三段全异常时兜底输出, 防静默降级上移一层
    return "🩺 链路健康: " + " | ".join(parts) if parts else "🩺 链路健康: ⚠️检查异常"


def _pangan_section(candidates):
    """DEB-20260819 §十五附加 + §十七-L1: 股性卡注入/引用统计 (盘后判分的第一手观测).

    口径 (§十七-L1 修正版):
      注入组 = pangan_injected 非空的候选 (实际写入 prompt 的规则列表)
      引用   = thesis/bull_case/bear_case 文本中出现卡规则关键词 (剔除打标字段防自引用)
      判分分组 = cited vs 注入未引用 (同为带卡票, 选样受控) — 非"非空vs空"(带卡vs无卡选样混淆)
    失败安全: bridge 无该字段 (旧文件) → 返回空段, 不影响主消息。
    """
    with_card = [c for c in candidates if c.get("pangan_injected")]
    if not with_card:
        return []
    # §二十二-22.4: 两级计数 — 规则级关键词为准(cited), 宽词("股性")作参考列
    _KW_RULE = ("贴日低", "贴日高", "跳空高开", "开盘30min", "开盘5min", "NR7", "窄幅日",
                "上破开盘")
    _KW_WIDE = ("股性档案", "股性")
    lines = ["", "🎯 股性卡引用 (DEB-20260819)",
             f"带卡候选: {len(with_card)}/{len(candidates)}"]
    cited_n = wide_n = 0
    for c in with_card:
        refs = c.get("pangan_injected", [])
        # 自引用防护: 扫描文本必须剔除打标字段本身
        #   (规则名含"贴日低"等关键词, 否则注入组 100% 假阳性)
        _c = {k: v for k, v in c.items() if k != "pangan_injected"}
        text = json.dumps(_c, ensure_ascii=False)
        cited = any(k in text for k in _KW_RULE)
        wide = any(k in text for k in _KW_WIDE)
        cited_n += cited
        wide_n += wide
        mark = "✅引用" if cited else ("🔸宽提" if wide else "➖注入未引用")
        rules = "/".join(r.split("_", 1)[-1][:8] for r in refs[:3])
        lines.append(f"  {c.get('symbol','?')}: {mark} [{rules}]")
    lines.append(f"引用率(规则级): {cited_n}/{len(with_card)} | 宽词提及: {wide_n}")
    return lines


def _build_message(date, bridge):
    candidates = bridge.get("candidates", []) or []
    lines = [f"📊 盘前Bridge生成完成 ({date})",
             f"候选: {len(candidates)}只(动态扫描+熟股)", ""]
    for c in candidates:
        sym = c.get("symbol", "")
        current = _finnhub_price(sym)
        direction = c.get("direction", "")
        conf = c.get("confidence", 0)
        entry = c.get("entry_price", 0)
        sl = c.get("stop_loss", 0)
        debate = (c.get("debate_outcome", "") or "")[:40]
        price_str = f"现价${current:.2f}" if current else "现价N/A"
        lines.append(f"{sym}: {direction} conf={conf} {price_str}")
        lines.append(f"  LLM建议入场${entry} SL${sl} (实际以re-price为准)")
        if debate:
            lines.append(f"  辩论: {debate}")
    _pg = _pangan_section(candidates)
    if _pg:
        lines.extend(_pg)
        lines.append("")
    _hs = _health_section(date)
    if _hs:
        lines.append(_hs)
        lines.append("")
    lines.append("instruction直写DB ✅  实际执行价=re-price实时bid/ask")
    return "\n".join(lines)


def main(date=None, dry=False):
    date = date or _today_et()
    path = _bridge_path(date)

    # 新鲜度守卫: 文件必须存在, 且其 trade_date 就是目标日 (绝不推陈旧文件)
    if not path.exists():
        print(f"[skip] bridge 文件不存在: {path.name} (A 可能尚未生成或非交易日)")
        return
    try:
        bridge = json.loads(path.read_text())
    except Exception as e:
        print(f"[skip] bridge 文件解析失败: {e}")
        return
    td = bridge.get("trade_date")
    if td and td != date:
        print(f"[skip] bridge trade_date={td} ≠ 目标日 {date}, 视为陈旧, 不推")
        return

    msg = _build_message(date, bridge)
    print(msg)
    if dry:
        print("\n[dry-run] 未推送")
        return
    try:
        from app.notifier.dingtalk import DingTalkNotifier
        DingTalkNotifier().send_text(msg)
        print("\n[ok] 钉钉已推送")
    except Exception as e:
        print(f"\n[warn] 钉钉推送失败(不影响链路): {e}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="盘前 DA bridge 钉钉摘要 (只读 bridge 文件)")
    ap.add_argument("--date", help="bridge 日期 YYYY-MM-DD (默认今日 ET)")
    ap.add_argument("--dry", action="store_true", help="只打印不推送")
    a = ap.parse_args()
    main(date=a.date, dry=a.dry)
