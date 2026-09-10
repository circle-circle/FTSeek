#!/usr/bin/env python3.11
"""ERROR/CRITICAL 突增告警 (2026-09-10 重写: error_alert.sh → Python + app/push hub).

为什么重写而不是调阈值 —— 原 `error_alert.sh` 有 6 处独立缺陷 (DEB-20260910 §4.1, 全部实测):
  1. `THRESHOLD=20` 条/15min 而实测 ERROR+CRITICAL 只有 **0-5 条/日** ⇒ 恒不触发,
     近 7 日 0 告警 (`data/.alert_state` 停在 error_spike_2026080522)。它是 AGENT.md 认定的
     两道 ERROR 覆盖之一 (另一道 intraday_heartbeat), 实际是纸面的。
  2. `$0 ~ /ERROR|CRITICAL/` 级别盲 —— 匹配行内**任意位置**的字符串 ⇒ INFO/WARNING 正文
     含 "ERROR" 即计入。
  3. `$1 " " $2 >= cutoff` 对**无时间戳行**做字符串比较 ⇒ 中文首字节 > ASCII 数字, 恒判"最近"
     (②批消掉了 60.9% 的无时间戳 print 行, 但判据本身仍脆)。
  4. 告警正文 shell 拼 JSON (`-d "{\\"msgtype\\":...\\"$MSG\\"}}"`) ⇒ 错误消息里出现 `"` 或
     反斜杠即非法 JSON ⇒ curl 失败 ⇒ `|| true` 静默吞掉 —— 恰好在错误最异常时哑火。
  5. 冷却键 `error_spike_$(date +%Y%m%d%H)` 是**全局小时级**, 与注释「同一类错误 1 小时内只
     告警一次」不符: 一小时内第二类错误不再告警。
  6. 直连 DingTalk webhook (curl + 从 .env.us grep URL), 绕开 app/push hub —— AGENT.md 军规
     点名的旁路形态, 无去重/聚合/健康监控。
  另有一处**告警链断裂**(本批新发现): 它只读 logs/ftseek_us.log, 而 daily_invariant_check.py
  (cron `5 5 * * 2-6`) 写 logs/invariant_check.log ⇒ 该脚本自述的「违反 → logger.error →
  error_alert 推钉钉」是纸面的, 不变量自检违反从未推送过。

本实现的对应修复:
  · 级别 = **字面量** `"] ERROR:"` / `"] CRITICAL:"` (main.py:37-52 格式契约, 与
    intraday_heartbeat.py:244 同口径, 单一真源) —— 不做行内任意位置子串匹配。
  · 时间窗 = `line[:16]` 字符串比较 (同 intraday_heartbeat); 无时间戳行**显式跳过并计数**
    (多行 record 的续行属此类, 计数进诊断而不进判据)。
  · 阈值 = **相对基线**: 近 7 日同一 15 分钟时段 (hour, minute//15) 的 ERROR+CRITICAL 计数
    中位数 M, 触发 `count >= max(3, ⌈3M⌉)`; M=0 时退化为 `count >= 3`。硬编码 20 在 0-5/日
    的现实下等于关闭。
  · 覆盖面 = logs/ 下**全部**活跃 `.log` + 当日轮转档 (含 logs/invariant_check.log) ——
    修掉告警链断裂, 且新增日志文件自动纳管, 不需要登记 (登记表 = 下一次断裂)。
    唯一排除: 本脚本自己的 logs/error_alert.log (自扫会形成反馈环)。
  · 冷却键 = `error_spike_<YYYYMMDD>_<HH>_<指纹>` (指纹 = 去数字后消息前 60 字), 兑现
    「同一类错误 1 小时内只告警一次」且**不同类互不压制**; 一次运行只推**一条**聚合消息,
    正文列出本轮所有尚未冷却的类 (避免 N 类 = N 条推送刷屏)。
  · 推送走 app/push hub (`sync=True` — cron 进程短命, 异步 fire-and-forget 可能在 worker
    发出前就退出); JSON 由 requests `json=` 序列化, 不再手拼。
  · **推送成功才写冷却键** —— 推送失败下轮重试, 不用"已告警"掩盖"没送到"。
  · 检测器自身异常 → logger.error + exit 1 ("零违反"与"守卫自己崩了"必须可区分,
    同 daily_invariant_check 的 *_detector_error 纪律)。

用法 (cron, 盘中每 15 分钟; 北京 21:00-05:00 = ET 9:00-17:00):
  */15 21-23 * * 1-5 cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python scripts/error_alert.py >> logs/error_alert.log 2>&1
  */15 0-5 * * 2-6   cd /opt/ftseek/FTSeek-US && PYTHONPATH=. venv/bin/python scripts/error_alert.py >> logs/error_alert.log 2>&1

手工: `--dry-run` (算但不推不写状态) / `--force` (跳过阈值判定, 供 DEB §4.3 判据 1 探针) /
      `--window-min N` / `--state PATH` / `--logs-dir PATH`。
退出码: 0 = 正常 (告警或未告警); 1 = 检测器/推送异常 (cron 日志可见)。
状态: data/.alert_state (JSON; 原 .sh 的纯文本形态解析失败即自愈重建)。
回滚: crontab 2 行指回 scripts/archive/error_alert.sh + 删本文件与状态文件。
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import logging
import math
import re
import statistics
import sys
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

LOG_DIR = REPO / "logs"
STATE = REPO / "data" / ".alert_state"
# 自扫排除: 本脚本的 stdout 落在 logs/error_alert.log, 若纳入扫描面, 它自己的
# "检测器异常" ERROR 行会进下一轮计数 ⇒ 正反馈 (告警制造告警)。
SELF_LOG_STEM = "error_alert.log"

WINDOW_MIN = 15
BASELINE_DAYS = 7          # 近 7 日同时段中位数
MIN_THRESHOLD = 3          # M=0 时的地板 (DEB §4.2: 硬编码 20 等于关闭)
THRESHOLD_MULT = 3         # count >= ⌈3M⌉
LEVEL_MARKERS = ("] ERROR:", "] CRITICAL:")
_TS_RE = re.compile(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}")
_TAIL_BYTES = 2_000_000    # 当前档只读尾部 2MB (15min 窗实测 ~12KB; 轮转档全读)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s")
# logger 名用 `ftseek.<module>` (DEB §5 建议的 SSOT 约定), **不用裸名** —— 裸名 logger 是
# 同批新增的 W5 棘轮禁止面 (裸名无法按子系统设级/过滤, 见 daily_invariant_check._check_bare_logger)。
logger = logging.getLogger("ftseek.error_alert")


# ───────────────────────── 扫描面 ─────────────────────────

def _is_self_log(path: Path) -> bool:
    return path.name == SELF_LOG_STEM or path.name.startswith(SELF_LOG_STEM + "-")


def active_sources(logs_dir: Path = LOG_DIR) -> list:
    """logs/ 下全部活跃 `.log` (不含轮转档 `*-YYYYMMDD[.gz]`, 不含本脚本自己的日志)。

    刻意用 glob 而非**登记表**: 登记表的形态正是本批修的病灶 —— 原 .sh 只登记了
    ftseek_us.log, 于是 daily_invariant_check 的 ERROR 从未被扫到 (告警链断裂)。
    """
    return sorted(p for p in logs_dir.glob("*.log")
                  if p.is_file() and not _is_self_log(p))


def rotated_sources(day: datetime, logs_dir: Path = LOG_DIR) -> list:
    """某日的轮转档 (`*-YYYYMMDD` 与 `*-YYYYMMDD.gz`, dateext -%Y%m%d 口径)。

    与 daily_alert_summary.resolve_log_sources 同口径 (logrotate: daily + copytruncate +
    dateext, 轮转由 /etc/cron.daily 触发, 近 7 日实测 03:12-03:48 CST — **在盘中窗口内**)。
    该函数在 daily_alert_summary 模块内, 而 import 那个模块会执行 load_dotenv + 把 HK 根
    注入 sys.path (scripts/daily_alert_summary.py:34-39), cron 告警脚本不引入这些副作用
    ⇒ 本地同构实现, 并由 tests/test_error_alert.py 交叉钉住两者口径一致。
    """
    stamp = day.strftime("%Y%m%d")
    out = []
    for cand in (logs_dir / f"*-{stamp}.gz", logs_dir / f"*-{stamp}"):
        out.extend(p for p in sorted(logs_dir.glob(cand.name)) if p.is_file())
    return out


def window_sources(now: datetime, logs_dir: Path = LOG_DIR) -> list:
    """15min 窗口的扫描面 = 活跃档 + **当日**轮转档。

    当日档必须在内: 轮转发生在 03:12-03:48 CST, 正落在 cron 窗 (0-5 时) 里 ⇒ 03:45 那次
    运行的窗口横跨轮转点, 只读当前档会丢掉轮转前那几分钟 (daily_alert_summary 9/8 实证
    「轮转档 145 条 / 当前档 3 条 ⇒ 报真相的 4%」是同一族盲区)。
    """
    srcs = active_sources(logs_dir) + rotated_sources(now, logs_dir)
    return [p for p in srcs if not _is_self_log(p)]


def baseline_sources(now: datetime, logs_dir: Path = LOG_DIR) -> list:
    """基线扫描面 = 近 BASELINE_DAYS+1 日的轮转档 (按**行内时间戳**分桶, 不信文件名日期)。

    文件名日期 = 轮转当日, 内容 ≈ 前一日中午 → 当日 03:48 ⇒ 名字与内容有半日错位。
    故只把文件名当**候选筛选**, 分桶一律用行自身时间戳 (多取 1 天覆盖错位)。
    """
    out = []
    for off in range(0, BASELINE_DAYS + 2):
        out.extend(rotated_sources(now - timedelta(days=off), logs_dir))
    return [p for p in out if not _is_self_log(p)]


# ───────────────────────── 行解析 ─────────────────────────

def _open_text(path: Path):
    if path.suffix == ".gz":
        return gzip.open(path, "rt", encoding="utf-8", errors="ignore")
    return open(path, "r", encoding="utf-8", errors="ignore")


def _iter_lines(path: Path, tail_bytes: int = 0):
    """逐行产出; tail_bytes>0 且文件更大时只读尾部 (丢弃首个残行)。"""
    try:
        if tail_bytes and path.suffix != ".gz" and path.stat().st_size > tail_bytes:
            with open(path, "rb") as f:
                f.seek(path.stat().st_size - tail_bytes)
                blob = f.read().decode("utf-8", errors="ignore")
            yield from blob.split("\n")[1:]      # [0] 是 seek 落点截断的残行
            return
        with _open_text(path) as f:
            for line in f:
                yield line.rstrip("\n")
    except Exception as e:
        # 单个文件读失败不得让整轮告警失明 (轮转竞态/权限), 但必须留痕
        logger.warning("[error_alert] 读取 %s 失败 (跳过该文件): %s", path.name, e)


def _parse_line(line: str):
    """→ (ts16, level, msg) 或 None (非 ERROR/CRITICAL 行)。

    无时间戳行由调用方计数 (unparsed) —— 本函数只在有时间戳时才判级别。
    """
    if not _TS_RE.match(line):
        return None
    for marker in LEVEL_MARKERS:
        idx = line.find(marker)
        if idx >= 0:
            return line[:16], marker[2:-1], line[idx + len(marker):].strip()
    return None


def scan(sources, since: str = None, until: str = None, tail_bytes: int = 0) -> dict:
    """扫 sources 里的 ERROR/CRITICAL 行。

    since/until: `YYYY-MM-DD HH:MM` 字符串窗口 (含 since, 不含 until); None = 不过滤
    (基线扫描按行自身时间戳分桶)。返回:
      count      窗口内 ERROR+CRITICAL 行数
      unparsed   无时间戳而被跳过的**非空**行数 (多行 record 续行属此类; 只进诊断不进判据)
      groups     指纹 → {count, levels, sample, sources}
      slots      (date, hour, quarter) → 计数 (基线用)
      per_source 文件名 → 命中数 (只列非零)
      covered    行时间戳出现过的日期集合 (基线"哪些天真有数据"的依据)
    """
    res = {"count": 0, "unparsed": 0, "groups": {}, "slots": {},
           "per_source": {}, "covered": set(), "files": len(sources)}
    for path in sources:
        in_window = since is None      # 无窗口过滤 (基线扫描) 时全部算"窗内"
        for line in _iter_lines(path, tail_bytes=tail_bytes):
            if not line.strip():
                continue
            got = _parse_line(line)
            if got is None:
                # 无时间戳行 = 多行 record 的续行, 或非标准格式输出 (print/Futu SDK 自带日志)。
                # 只在**前一条有时间戳的行落在窗口内**时计数 —— 否则读尾部 2MB 会把窗口外
                # 的历史续行一起算进来, 诊断数字虚高到不可读 (首版实测 8577 行)。
                if in_window and not _TS_RE.match(line):
                    res["unparsed"] += 1
                continue
            ts16, level, msg = got
            in_window = not (since and ts16 < since) and not (until and ts16 >= until)
            if since and ts16 < since:
                continue
            if until and ts16 >= until:
                continue
            res["count"] += 1
            res["covered"].add(ts16[:10])
            res["per_source"][path.name] = res["per_source"].get(path.name, 0) + 1
            hour, minute = int(ts16[11:13]), int(ts16[14:16])
            slot = (ts16[:10], hour, minute // WINDOW_MIN)
            res["slots"][slot] = res["slots"].get(slot, 0) + 1
            fp = fingerprint(msg)
            g = res["groups"].setdefault(
                fp, {"count": 0, "levels": set(), "sample": "", "sources": set()})
            g["count"] += 1
            g["levels"].add(level)
            g["sources"].add(path.name)
            if not g["sample"]:
                g["sample"] = _sanitize(line)[:220]
    return res


def fingerprint(msg: str) -> str:
    """错误指纹 = 去掉数字后的消息前 60 字 (DEB §4.2)。

    去数字: 同一类错误的 price/qty/symbol 计数各不相同, 不去数字则每条都是"新类",
    冷却键形同虚设 (缺陷 5 的另一面)。
    """
    return re.sub(r"\d+", "#", msg).strip()[:60] or "(空消息)"


def _sanitize(text: str) -> str:
    """控制字符 → 空格: 保证推送正文每条错误占一行、可读 (DEB §4.3 判据 3)。"""
    return re.sub(r"[\r\n\t\x0b\x0c]+", " ", text).strip()


# ───────────────────────── 阈值 ─────────────────────────

def slot_of(now: datetime, window_min: int = WINDOW_MIN) -> tuple:
    """窗口起点所属的 15min 时段 (date, hour, quarter)。

    用**窗口起点**而非 now: cron 在 :15:00 与 :15:40 都应归到同一时段, 否则延迟几秒
    就换桶, 基线与当前值不可比。
    """
    start = now - timedelta(minutes=window_min)
    return start.strftime("%Y-%m-%d"), start.hour, start.minute // window_min


def baseline_median(now: datetime, logs_dir: Path = LOG_DIR,
                    window_min: int = WINDOW_MIN) -> dict:
    """近 7 日同时段中位数 M + 阈值 T = max(MIN_THRESHOLD, ⌈THRESHOLD_MULT × M⌉)。

    样本 = 有数据覆盖的那几天 (covered) 在同一 (hour, quarter) 时段的计数, 缺桶记 0。
    只取今天之前的日期 (今天的数据正是被判定对象, 混进基线会自我抬阈值)。
    """
    today = now.strftime("%Y-%m-%d")
    _, hour, quarter = slot_of(now, window_min)
    srcs = baseline_sources(now, logs_dir)
    got = scan(srcs)
    days = sorted(d for d in got["covered"] if d < today)[-BASELINE_DAYS:]
    samples = [got["slots"].get((d, hour, quarter), 0) for d in days]
    median = statistics.median(samples) if samples else 0.0
    threshold = max(MIN_THRESHOLD, int(math.ceil(THRESHOLD_MULT * median)))
    return {"median": median, "threshold": threshold, "samples": samples,
            "days": days, "slot": [hour, quarter], "files": len(srcs),
            "total_errors": got["count"]}


# ───────────────────────── 状态 ─────────────────────────

def _empty_state() -> dict:
    """空冷却状态的单一真源 (首跑 / 状态文件损坏 / 旧 .sh 纯文本形态自愈, 三者同一份)。"""
    return {"version": 2, "cooldown": {}, "baseline_cache": {}}


def load_state(path: Path = STATE) -> dict:
    """读冷却状态。损坏/旧纯文本形态 → 空 dict 自愈 (最坏后果 = 重复推一次)。"""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            raw.setdefault("cooldown", {})
            raw.setdefault("baseline_cache", {})
            return raw
    except FileNotFoundError:
        # 首跑 (或状态文件被清) 是**正常态**不是异常。2026-09-10: 原写法是裸 `pass`
        #   ⇒ 撞 except:pass 棘轮 (tests/test_except_pass_ratchet.py, 基线 955, DEB-20260831 P1③)。
        #   棘轮的 R5 纪律是"新增异常处理必须 warn 留痕", 但此处留痕会在每次首跑制造一条
        #   零信息量 WARNING ⇒ 改为**显式 return 同一份空状态**: 语义与 fall-through 逐字等价,
        #   而"什么都不做的 except 体"这个吞错形态消失。空状态收敛到 _empty_state() 单一真源,
        #   防两处字面量各自漂移 (漏一个键 ⇒ save_state 的剪枝会 KeyError)。
        return _empty_state()
    except Exception as e:
        logger.warning("[error_alert] 状态文件 %s 不可读 (%s) → 重建 (旧 .sh 是纯文本单行)",
                       path.name, e)
    return _empty_state()


def save_state(state: dict, now: datetime, path: Path = STATE) -> None:
    """原子写 + 剪枝 (冷却键只留今昨两日, 基线缓存只留今昨两个日期前缀)。"""
    keep = {(now - timedelta(days=k)).strftime("%Y%m%d") for k in (0, 1)}

    def _fresh_day(key: str) -> str:
        parts = str(key).split("_")
        return parts[2] if len(parts) > 2 else ""

    state["cooldown"] = {k: v for k, v in (state.get("cooldown") or {}).items()
                         if _fresh_day(k) in keep}
    state["baseline_cache"] = {k: v for k, v in (state.get("baseline_cache") or {}).items()
                               if str(k).split(":")[0] in keep}
    state["version"] = 2
    try:
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.rename(path)
    except Exception as e:
        logger.warning("[error_alert] 状态写失败 (下轮可能重复推一次, 不阻断): %s", e)


# ───────────────────────── 推送 ─────────────────────────

def build_message(count: int, threshold: int, base: dict, groups: dict,
                  now: datetime, window_min: int, per_source: dict,
                  unparsed: int, cooled: list) -> tuple:
    """→ (title, body)。正文用 json.dumps 之外的**纯文本**排版, 序列化交给 requests json=。"""
    start = now - timedelta(minutes=window_min)
    title = (f"[FTSeek-US] ERROR 突增 {count} 条/{window_min}min "
             f"(阈值 ≥{threshold})")
    top = sorted(groups.items(), key=lambda kv: -kv[1]["count"])[:3]
    lines = [
        f"窗口: {start:%Y-%m-%d %H:%M} → {now:%H:%M} CST",
        f"计数: {count} 条 ERROR/CRITICAL | 阈值 ≥{threshold} "
        f"(近 {len(base.get('days') or [])} 日同时段中位数 M={base.get('median')})",
        "来源: " + ", ".join(f"{k}={v}" for k, v in sorted(
            per_source.items(), key=lambda kv: -kv[1])[:5]),
        "Top 错误类:",
    ]
    for fp, g in top:
        levels = "/".join(sorted(g["levels"]))
        lines.append(f" • [{g['count']}次 {levels}] {g['sample'][:160]}")
    if cooled:
        lines.append(f"本小时已告警过、本轮压制: {len(cooled)} 类")
    lines.append(f"无时间戳跳过行: {unparsed} (多行 record 续行, 只进诊断)")
    lines.append(f"[error_alert.py] 基线口径 = 近 {BASELINE_DAYS} 日同 15min 时段中位数; "
                 f"冷却键含错误指纹 (同类 1 小时 1 次, 不同类互不压制)")
    return title, "\n".join(lines)


def push(title: str, body: str, dedup_key: str) -> bool:
    """走 app/push hub, **sync=True** — cron 进程短命, 异步可能在 worker 发出前退出。"""
    hub = None
    try:
        from app.push import get_push_hub
        from app.push.message import Category, Level, PushMessage
        hub = get_push_hub()
        ok = hub.push(PushMessage(title=title, body=body, level=Level.HIGH,
                                  category=Category.SYSTEM, dedup_key=dedup_key),
                      sync=True)
        return bool(ok)
    except Exception as e:
        logger.error("[error_alert] 推送失败 (下轮重试, 冷却键不写): %s", e, exc_info=True)
        return False
    finally:
        try:
            if hub is not None:
                hub.stop()      # flush 聚合 + 关线程池, 防 cron 进程挂在非 daemon 线程上
        except Exception as e:
            # 2026-09-10: 原写法是裸 `pass` ⇒ 撞 except:pass 棘轮, 且与本脚本的立项理由
            #   **自相矛盾** (DEB-20260910 §4/§8.7 修的正是"守卫自己坏了不可见")。
            #   sync=True 时消息已发出 ⇒ stop() 失败主要意味着聚合缓冲未 flush / 线程池未关
            #   (最坏: cron 进程挂住到超时)。留 WARNING 带栈, 不改返回值 (推送成败以 hub.push 为准)。
            logger.warning("[error_alert] push hub 关闭异常 (sync 推送已发出, 聚合可能未 flush): %s",
                           e, exc_info=True)


# ───────────────────────── 主流程 ─────────────────────────

def run(now: datetime = None, window_min: int = WINDOW_MIN, state_path: Path = STATE,
        logs_dir: Path = LOG_DIR, dry_run: bool = False, force: bool = False) -> int:
    """一次巡检。返回退出码 (0 正常 / 1 检测器异常)。"""
    now = now or datetime.now()
    since = (now - timedelta(minutes=window_min)).strftime("%Y-%m-%d %H:%M")
    until = now.strftime("%Y-%m-%d %H:%M")
    try:
        srcs = window_sources(now, logs_dir)
        got = scan(srcs, since=since, until=until, tail_bytes=_TAIL_BYTES)
    except Exception as e:
        logger.error("[error_alert] 扫描异常 → 本轮 ERROR 覆盖失明 (不得读成'零错误'): %s",
                     e, exc_info=True)
        return 1

    cache_key = f"{now:%Y%m%d}:{now.hour}"
    state = load_state(state_path)
    base = (state.get("baseline_cache") or {}).get(cache_key)
    if not base:
        try:
            base = baseline_median(now, logs_dir, window_min)
        except Exception as e:
            # 基线算不出来 → 退到地板阈值 3 (宁可敏感, 不可关闭); 失败必须留痕
            logger.warning("[error_alert] 基线计算失败 (%s) → 用地板阈值 %d",
                           e, MIN_THRESHOLD)
            base = {"median": -1, "threshold": MIN_THRESHOLD, "days": [],
                    "samples": [], "slot": list(slot_of(now, window_min))[1:],
                    "files": 0, "total_errors": -1, "error": type(e).__name__}
        state.setdefault("baseline_cache", {})[cache_key] = base
    threshold = int(base.get("threshold") or MIN_THRESHOLD)

    count = got["count"]
    triggered = force or count >= threshold
    diag = {"window": f"{since}→{until}", "count": count, "threshold": threshold,
            "median": base.get("median"), "files": len(srcs),
            "unparsed": got["unparsed"], "groups": len(got["groups"]),
            "per_source": got["per_source"], "force": force, "dry_run": dry_run}

    if not triggered:
        logger.info("[error_alert] %s ERROR/CRITICAL %d 条 < 阈值 %d (基线 M=%s, 扫 %d 文件, "
                    "无时间戳跳过 %d 行)", since, count, threshold, base.get("median"),
                    len(srcs), got["unparsed"])
        if not dry_run:
            state["last_run"] = diag
            save_state(state, now, state_path)
        return 0

    # 冷却: 逐**指纹**判定 (缺陷 5 修复) — 本轮只推尚未冷却的类, 一条聚合消息
    day, hour = f"{now:%Y%m%d}", f"{now:%H}"
    cooldown = state.get("cooldown") or {}
    cooled, fresh = [], {}
    for fp, g in got["groups"].items():
        if f"error_spike_{day}_{hour}_{fp}" in cooldown:
            cooled.append(fp)
        else:
            fresh[fp] = g
    if not fresh:
        logger.info("[error_alert] 触发但全部 %d 类本小时已告警过 → 压制 (计数 %d, 阈值 %d)",
                    len(cooled), count, threshold)
        if not dry_run:
            state["last_run"] = dict(diag, suppressed=len(cooled))
            save_state(state, now, state_path)
        return 0

    title, body = build_message(count, threshold, base, fresh, now, window_min,
                                got["per_source"], got["unparsed"], cooled)
    # hub 侧 60s 去重键 = 本小时 + 本轮类集合摘要 (与冷却键同源, 防同一轮重复投递)
    dedup_key = "error_spike_{}_{}_{}".format(
        day, hour, hashlib.md5("|".join(sorted(fresh)).encode("utf-8")).hexdigest()[:10])
    logger.info("[error_alert] 触发: %s", json.dumps(
        dict(diag, push_classes=len(fresh), suppressed=len(cooled)), ensure_ascii=False))
    if dry_run:
        logger.info("[error_alert] --dry-run 不推送。正文预览:\n%s\n%s", title, body)
        return 0

    if not push(title, body, dedup_key):
        # 推送失败: **不写冷却键** ⇒ 下一轮重试 (不得用"已告警"掩盖"没送到")
        logger.error("[error_alert] 推送未成功 → 冷却键不写, 下轮重试 (计数 %d)", count)
        state["last_run"] = dict(diag, push_failed=True)
        save_state(state, now, state_path)
        return 1

    cd = state.setdefault("cooldown", {})
    for fp, g in fresh.items():
        cd[f"error_spike_{day}_{hour}_{fp}"] = {
            "count": g["count"], "levels": sorted(g["levels"]),
            "sample": g["sample"][:120], "pushed_at": now.isoformat(timespec="seconds")}
    state["last_run"] = dict(diag, pushed=True, push_classes=len(fresh))
    save_state(state, now, state_path)
    logger.info("[error_alert] 已推送 (类 %d, 冷却键 %d 条已登记)", len(fresh), len(cd))
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="ERROR/CRITICAL 突增告警 (DEB-20260910 §4)")
    ap.add_argument("--window-min", type=int, default=WINDOW_MIN)
    ap.add_argument("--dry-run", action="store_true", help="算但不推送不写状态")
    ap.add_argument("--force", action="store_true",
                    help="跳过阈值判定 (DEB §4.3 判据 1 探针用)")
    ap.add_argument("--state", default=str(STATE))
    ap.add_argument("--logs-dir", default=str(LOG_DIR))
    args = ap.parse_args(argv)
    return run(window_min=args.window_min, state_path=Path(args.state),
               logs_dir=Path(args.logs_dir), dry_run=args.dry_run, force=args.force)


if __name__ == "__main__":
    sys.exit(main())
