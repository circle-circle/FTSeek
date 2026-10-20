#!/usr/bin/env python3.11
"""日K缓存增量刷新 (2026-08-17, 数据卫生根因修复).

背景: data/replay_bars_daily/*.json (110票×5年日K) 停更于 2026-07-24 —
  A2 plan-vs-actual 报告用它算 prev_close/gap, 7/24 后 165 行 gap 被三周
  漂移污染 (如 CRM "gap 16%" / GOOG "gap 11%" 实为缓存陈旧伪缺口, big 桶
  120 行中 118 行是脏数据)。market_profile_builder 的"近20日"指标同受累。

设计:
  - 增量尾部刷新: 每票从缓存末日(含, 防末bar盘中写入截断)重拉 KL_DAY, 按日期
    merge 回写。~1 call/票, 配额安全 (对比全量重拉 ~8 page/票)。
  - QFQ 与 historical_robustness_check.fetch_daily 同口径 (尾行新鲜即可满足
    prev_close; 全序列QFQ重基线需全量重拉, 留独立任务)。
  - 原子写 (tmp+rename), 幂等 (末日期>=最新交易日则跳过)。
  - 只读交易系统数据, 不触碰执行路径。

用法: python3 scripts/refresh_daily_bars.py [--dry-run] [--limit N]
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

REPO = Path(__file__).resolve().parents[1]
DAILY_DIR = REPO / "data" / "replay_bars_daily"
# 2026-09-10 (DEB-20260910 §1 议题一 A 案, 用户裁「按照建议执行优化」): 执行级日门。
#   病灶: 原"幂等"只是**数据级**(不重复写行), 无执行级门 ⇒ AFTER_HOURS 每轮重跑,
#   实测一晚 32 轮 × 281 票 = ~8992 次 Futu 历史K线请求, 有效仅首轮 ~281 次 (32× 浪费);
#   单轮 7m45s ≈ 整个盘后窗 (ET 16:00-20:00) 被占满; 且本脚本自建 OpenQuoteContext
#   绕过 ftseek_lv3_provider 的 60次/30s 限速窗, 与同轮 PlaybookIC 回填抢配额 ⇒
#   `K线配额窗口满 等待超时` 近两日 13 次 100% 落在北京 04-07 时 ⇒ Router fallback →
#   TwelveData Read timed out → `所有数据源均失败` (而这正是本脚本立项要防的病)。
#   根因: :60 `start = last`(含末日重拉) ⇒ 每轮每票必发请求, "跳过"判定在 API 调用**之后**;
#   提交 3f2d946c 的「已最新110票零拉取」与代码不符 (票池已 110→281)。
STATE = REPO / "data" / "refresh_daily_bars_state.json"
ET = ZoneInfo("America/New_York")
# 失败重试上限: **必须有限** — 原实现是 32 轮无脑重试; 但若改成"一日一次"则盘后首轮遇
#   OpenD 抖动当天就彻底不刷新, 反而加速复发 7/24 停更三周。故: 失败不写 done 标记 →
#   下一轮**只重试失败票**(成本 ~1.65s/票, 非 281 票全量), 连续 MAX_ATTEMPTS 轮仍失败
#   即视为已知病灶, 写标记停止 + 单条 WARNING 列名 (DEB §1.5 判据 5: ≤3 条/日)。
MAX_ATTEMPTS_ON_FAIL = 3


# 2026-09-10 (可观测性批): print → logger。本脚本经 scheduler._load_script_module 在
#   **服务进程内**执行, print 落到 systemd stdout 后无时间戳/无级别/无 logger 名 —
#   实测占主日志 60.9% 且不可按级别过滤、不可按时序归因。命名沿用 in-process 脚本
#   既有约定 ftseek.<name> (同 ah_exit_shadow / futu_channel_shadow / stall_entry_shadow)。
logger = logging.getLogger("ftseek.refresh_daily_bars")


def _today_et() -> str:
    """美东交易日 (盘后窗 ET 16:00-20:00 内日期稳定, 不受北京跨日影响)。"""
    return datetime.now(ET).strftime("%Y-%m-%d")


def _now_et() -> datetime:
    return datetime.now(ET)


def _in_postmarket_window(now: datetime | None = None) -> bool:
    """cron 双季发车守卫: 只在 ET 16:00 后真正刷新.

    CST 04:05/05:05 两条 cron 覆盖 EDT/EST; 另一季会落在 ET 15:05,
    此时必须退出, 否则会把当日未收盘的日K 当成完整 bar 写入。
    """
    now = now or _now_et()
    return now.hour >= 16


def _load_state() -> dict:
    """读执行门状态。损坏/缺失 → 按"今日未跑"处理 (fail-open 向**做刷新**方向, 不向跳过)。"""
    try:
        st = json.loads(STATE.read_text(encoding="utf-8"))
        return st if isinstance(st, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        logger.warning("[日K刷新] 状态文件不可读 (%s) → 视为今日未跑, 重新刷新", e, exc_info=True)
        return {}


def _save_state(st: dict) -> None:
    """原子写 (tmp+rename, 与本脚本日K回写同形态)。写失败不得阻断: 最坏是下轮重刷。"""
    try:
        tmp = STATE.with_suffix(".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.rename(STATE)
    except Exception as e:
        logger.warning("[日K刷新] 状态文件写失败 (下轮将重刷, 不阻断): %s", e)


def run_check(scheduler=None, *, enforce_window: bool = False) -> None:
    """调度器/cron 入口: 执行级日门 + 失败有限重试。

    一夜语义: 首轮全量刷新 → fail==0 即写 done, 后续 ~31 轮直接跳过;
              fail>0 则后续轮**只重试失败票**, 至多 MAX_ATTEMPTS_ON_FAIL 轮后停并 WARNING。

    2026-10-18: 原由 AFTER_HOURS 主循环调用, 单轮阻塞 658s。现改由独立 cron 双季
    发车; cron 路径 enforce_window=True, 用 ET 16:00 守卫拒绝另一季 15:05 的误发。
    调度器旧调用点已移除; 回滚 = crontab 删两行 + 恢复 scheduler_core 调用块。
    """
    if enforce_window:
        try:
            from app.core.config_container import ConfigContainer
            if not bool(ConfigContainer.get_instance().get_dotted(
                    "playbook.daily_bars_refresh_enabled", True)):
                logger.debug("[日K刷新] playbook.daily_bars_refresh_enabled=false, 跳过")
                return
        except Exception as _cfg_err:
            logger.warning("[日K刷新] 配置读取失败 (fail-open 继续刷新): %s", _cfg_err)
        if not _in_postmarket_window():
            logger.debug("[日K刷新] ET %s 不在盘后窗口, 跳过 (cron 双季守卫)",
                         _now_et().strftime("%H:%M"))
            return

    today = _today_et()
    st = _load_state()
    if st.get("date") != today:
        st = {"date": today, "done": False, "attempts": 0, "failed_syms": []}
    if st.get("done"):
        # DEBUG 而非 INFO: 盘后窗 ~31 轮, 打 INFO 就是把刚清掉的噪声换个形态请回来。
        logger.debug("[日K刷新] %s 已完成 (attempts=%s), 本轮跳过", today, st.get("attempts"))
        return

    retry_only = set(st.get("failed_syms") or [])
    res = do_refresh(limit=0, dry_run=False,
                     only_syms=(retry_only or None))
    st["attempts"] = int(st.get("attempts") or 0) + 1
    st.update({"ok": res["ok"], "skip": res["skip"], "fail": res["fail"],
               "failed_syms": res["failed_syms"]})

    if res["fail"] == 0:
        st["done"] = True
        st["failed_syms"] = []
        logger.info("[日K刷新] %s 完成 (attempts=%d), 后续轮次跳过", today, st["attempts"])
    elif st["attempts"] >= MAX_ATTEMPTS_ON_FAIL:
        st["done"] = True
        logger.warning(
            "[日K刷新] %s 连续 %d 轮仍有 %d 票失败 → 视为已知病灶, 今日停止重试: %s",
            today, st["attempts"], res["fail"], ", ".join(res["failed_syms"][:20]))
    else:
        logger.warning(
            "[日K刷新] %s 第 %d/%d 轮有 %d 票失败, 下一轮只重试这些票: %s",
            today, st["attempts"], MAX_ATTEMPTS_ON_FAIL, res["fail"],
            ", ".join(res["failed_syms"][:20]))
    _save_state(st)


def do_refresh(limit: int = 0, dry_run: bool = False,
               only_syms: "set[str] | None" = None) -> dict:
    """增量刷新日K缓存。返回 {ok, skip, fail, failed_syms} 供执行门判定。

    only_syms 非空 → 只刷这些标的 (失败重试路径, 成本 ~1.65s/票 而非 281 票全量)。
    """
    files = sorted(DAILY_DIR.glob("US_*.json"))
    if only_syms:
        files = [fp for fp in files if fp.stem.replace("_", ".") in only_syms]
    if limit:
        files = files[:limit]
    logger.info("[日K刷新] 扫描 %d 个日K缓存文件%s", len(files),
                f" (只重试 {len(only_syms)} 票)" if only_syms else "")

    from futu import KLType, RET_OK, AuType, OpenQuoteContext
    ctx = OpenQuoteContext(host=os.getenv("FUTU_OPEND_HOST", "127.0.0.1"),
                           port=int(os.getenv("FUTU_OPEND_PORT", "11111")))
    ok = skip = fail = 0
    failed_syms: list = []
    try:
        for fp in files:
            sym = fp.stem.replace("_", ".")
            try:
                rows = json.loads(fp.read_text())
            except Exception:
                fail += 1
                failed_syms.append(sym)
                logger.warning("[日K刷新] %s 缓存 JSON 解析失败 (计入失败, 原实现静默)", sym)
                continue
            if not rows:
                fail += 1
                failed_syms.append(sym)
                logger.warning("[日K刷新] %s 缓存为空 (计入失败, 原实现静默)", sym)
                continue
            last = rows[-1][0]
            start = last  # 含末日重拉, 防截断
            if dry_run:
                logger.info("[日K刷新] %s: last=%s → 将增量拉取 %s~today", sym, last, start)
                continue
            new_rows: list[list] = []
            page_key = None
            err_txt = ""
            for _ in range(3):  # 增量最多3页足够
                ret, data, page_key = ctx.request_history_kline(
                    sym, ktype=KLType.K_DAY, start=start,
                    end=datetime.now().strftime("%Y-%m-%d"),
                    max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
                if ret != RET_OK or data is None:
                    # ret != RET_OK 时 futu 把错误正文放在 data (str)。原实现只打 ret=-1,
                    #   错误正文整条丢弃 → US.SKYT 连续 31 夜 ret=-1 无从根因 (2026-09-10)。
                    err_txt = data if isinstance(data, str) else f"<{type(data).__name__}>"
                    break
                for _, r in data.iterrows():
                    new_rows.append([str(r.get("time_key", ""))[:10],
                                     float(r["open"]), float(r["high"]),
                                     float(r["low"]), float(r["close"])])
                if page_key is None:
                    break
                time.sleep(1.2)
            time.sleep(1.2)  # kline 配额保护 (同 fetch_daily)
            if not new_rows:
                fail += 1
                failed_syms.append(sym)
                # DEBUG 而非 WARNING: 汇总行已带失败票名单 (run_check 的 WARNING),
                #   逐票再打 WARNING 会让单票永久失败刷 3 条/夜 × 逐票 = 超 DEB §1.5 判据 5。
                #   错误正文 (err_txt) 仍保留 — 这是本批修的原缺陷 (只打 ret=-1 丢正文)。
                logger.debug("[日K刷新] %s 拉取失败 (ret=%s): %s", sym, ret,
                             err_txt or "空返回")
                continue
            # merge: 按日期覆盖同名旧行
            merged = {r[0]: r for r in rows}
            added = sum(1 for r in new_rows if r[0] not in merged)
            merged.update({r[0]: r for r in new_rows})
            out = sorted(merged.values(), key=lambda b: b[0])
            if added == 0 and out[-1][0] == last:
                skip += 1
                continue
            tmp = fp.with_suffix(".tmp")
            tmp.write_text(json.dumps(out))
            tmp.rename(fp)
            ok += 1
            if added:
                logger.info("[日K刷新] %s: %s → %s (+%d行)", sym, last, out[-1][0], added)
    finally:
        ctx.close()
    logger.info("[日K刷新] 完成: 刷新 %d | 已最新跳过 %d | 失败 %d", ok, skip, fail)
    return {"ok": ok, "skip": skip, "fail": fail, "failed_syms": failed_syms}


def main() -> int:
    # 独立 CLI 运行时必须自配 handler: 本模块 logger 无 handler, 靠 root 传播;
    #   服务进程内由 main.py 的 basicConfig 提供, 独立运行则需自建 (同 ah_exit_shadow.py:452),
    #   否则 print→logger 后 CLI 输出静默变空。
    if not logging.getLogger().handlers:
        logging.basicConfig(level=logging.INFO,
                            format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--ignore-gate", action="store_true",
                    help="CLI 手工运行默认绕开执行级日门 (显式意图); 本开关仅为语义明示")
    ap.add_argument("--cron", action="store_true",
                    help="按执行级日门 + ET 盘后窗运行 (cron 双季发车用)")
    args = ap.parse_args()
    if args.cron:
        run_check(None, enforce_window=True)
        return 0
    res = do_refresh(limit=args.limit, dry_run=args.dry_run)
    if res["fail"]:
        logger.warning("[日K刷新] 失败票: %s", ", ".join(res["failed_syms"][:20]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
