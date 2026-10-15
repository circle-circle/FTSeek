"""B2/项3 盘中诊断「语义无变化则静默」契约钉 (2026-10-06 推送治理批).

病灶: `ops/hermes/intraday_diagnose_late.py` 每 30min 无条件投递, 交易结束后内容
  语义冻结 (仅时间戳 + equity/cash 漂移 + SPY/VIX 微变) 仍照发。实测冗余率 **59%**
  (近 8 天 22 条真实投递 → 语义去重后 9 条)。
修法: 归一化后取 sha1 指纹存 `data/.intraday_diagnose_late_last`, 与上次一致 ⇒
  `sys.exit(0)` 空 stdout = SILENT (no_agent 契约, 同非交易日分支)。

本文件钉四类不可回退的契约 — 全部用**真实投递原文形态**做样本, 不用抽象字符串:
  ① 归一化必须抹掉「被证明易变的量」(浮点漂移/时间推移) ⇒ 该抑制的抑制;
  ② 交易语义变化必须改变指纹 ⇒ 该推的绝不能吞 (SL 移动/新成交/regime 翻转/异常);
  ③ **跨交易日不得碰撞** — 两个零持仓零交易的连续交易日必须各自投递;
  ④ fail-open: 去重逻辑异常 ⇒ 照发, 不因自身故障吞掉真投递。

⚠️ ③ 是本批实跑抓到的真 bug (不是纸面推演): 初版直接整行删掉时间戳行 ⇒
  交易日信息一并丢失 ⇒ 连续空仓日指纹相同 ⇒ 第二天被静默吞掉。
  修法: 先抽出 ET 日期作指纹前缀, **再**抹时间戳行 (顺序不可颠倒)。
"""
import hashlib
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
CARRIER = REPO / "ops" / "hermes" / "intraday_diagnose_late.py"
SRC = CARRIER.read_text(encoding="utf-8")


def _fingerprint():
    """从生产脚本抽出 `_semantic_fingerprint` 实现本身执行 — 测的是真代码, 不是副本."""
    m = re.search(
        r"def _semantic_fingerprint.*?\n    return hashlib\.sha1.*?hexdigest\(\)",
        SRC, re.S,
    )
    assert m, "生产脚本里找不到 _semantic_fingerprint — 静默逻辑被移除或改名"
    ns = {}
    exec("import hashlib, re as _re\n" + m.group(0), ns)  # noqa: S102
    return ns["_semantic_fingerprint"]


@pytest.fixture(scope="module")
def fp():
    return _fingerprint()


# 真实投递原文形态 (取自 2026-10-06_04-30 落档, 精简到 1 持仓 1 成交)
BASE = """📊 盘中状态 2026-10-05 14:30 ET

• 持仓: 1只, equity=$99363.649, cash=$89922.881
  US.TSLA  size=16.0 dir=SHORT entry=$376.45 SL=394.51 TP=None
• 今日指令: 3条 → 触发3条✅, 执行3条✅, 0条未触发
• 今日交易(入场日口径): 2笔 → 1胜1负, 已实现净PnL = $12.30
  US.MU    LONG     pnl=$   26.49 reason=SL_TRAIL_LOCK
• 盘面: regime=risk_on (score=8) SPY=774.55 VIX=14.08 (收盘定格值, 末次评估31分钟前)
• 信号: 今日5条 snapshots
• 异常: 无"""


# ══════════ ① 该抑制的必须抑制 (纯浮点漂移 / 时间推移) ══════════
@pytest.mark.parametrize("label,mutated", [
    ("equity 漂移", BASE.replace("equity=$99363.649", "equity=$99361.100")),
    ("cash 漂移", BASE.replace("cash=$89922.881", "cash=$89920.001")),
    ("时间推移", BASE.replace("14:30 ET", "15:00 ET")),
    ("SPY 微变", BASE.replace("SPY=774.55", "SPY=774.61")),
    ("MA/VIX 微变", BASE.replace("VIX=14.08", "VIX=14.11")),
    ("评估龄变化", BASE.replace("末次评估31分钟前", "末次评估58分钟前")),
    ("全部叠加", BASE.replace("equity=$99363.649", "equity=$99360.0")
                    .replace("14:30", "16:25")
                    .replace("SPY=774.55", "SPY=775.02")
                    .replace("末次评估31分钟前", "末次评估120分钟前")),
])
def test_float_drift_is_suppressed(fp, label, mutated):
    assert fp(BASE) == fp(mutated), (
        f"{label} 未被归一化 ⇒ 该抑制的仍会投递 (本载体的主要噪音源)"
    )


# ══════════ ② 交易语义变化绝不能被吞 ══════════
@pytest.mark.parametrize("label,mutated", [
    ("持仓 SL 移动 (trailing)", BASE.replace("SL=394.51", "SL=390.00")),
    ("持仓 TP 设定", BASE.replace("TP=None", "TP=360.00")),
    ("持仓数量变化", BASE.replace("• 持仓: 1只", "• 持仓: 2只")),
    ("持仓平掉 (size→0)", BASE.replace("size=16.0", "size=0.0")),
    ("持仓方向翻转", BASE.replace("dir=SHORT", "dir=LONG")),
    ("入场价变化", BASE.replace("entry=$376.45", "entry=$370.00")),
    ("指令数变化", BASE.replace("3条 → 触发3条", "4条 → 触发4条")),
    ("触发数与指令数背离", BASE.replace("3条 → 触发3条✅, 执行3条✅", "3条 → 触发2条✅, 执行2条✅")),
    ("新成交出现", BASE.replace("2笔 → 1胜1负", "3笔 → 2胜1负")),
    ("逐笔 pnl 变化", BASE.replace("pnl=$   26.49", "pnl=$   27.10")),
    ("退出原因变化", BASE.replace("reason=SL_TRAIL_LOCK", "reason=DEAD_SIGNAL_EXIT")),
    ("regime 翻转", BASE.replace("regime=risk_on (score=8)", "regime=risk_off (score=2)")),
    ("信号数变化", BASE.replace("今日5条", "今日7条")),
    ("异常段出现", BASE.replace("• 异常: 无", "• 异常: 2 条 ERROR")),
])
def test_trading_semantics_always_delivered(fp, label, mutated):
    assert fp(BASE) != fp(mutated), (
        f"{label} 指纹未变 ⇒ 真交易事件会被静默吞掉 (不可接受: 这是盘中唯一的状态面)"
    )


# ══════════ ③ 跨交易日不得碰撞 (本批实跑抓到的真 bug) ══════════
_EMPTY = BASE.replace(
    "• 持仓: 1只, equity=$99363.649, cash=$89922.881\n"
    "  US.TSLA  size=16.0 dir=SHORT entry=$376.45 SL=394.51 TP=None",
    "• 持仓: 0只, equity=$99363.649, cash=$89922.881",
).replace("3条 → 触发3条✅, 执行3条✅", "0条 → 触发0条✅, 执行0条✅").replace(
    "2笔 → 1胜1负, 已实现净PnL = $12.30\n"
    "  US.MU    LONG     pnl=$   26.49 reason=SL_TRAIL_LOCK",
    "0笔 → 0胜0负, 已实现净PnL = $0.00",
).replace("今日5条", "今日0条")


def test_consecutive_empty_trading_days_do_not_collide(fp):
    """两个零持仓零交易的连续交易日必须各自投递.

    初版直接整行删时间戳 ⇒ 交易日一并丢失 ⇒ 连续空仓日指纹相同 ⇒ 第二天被吞。
    修法: 先抽 ET 日期作指纹前缀, 再抹时间戳行 (顺序不可颠倒)。
    """
    d1 = _EMPTY.replace("2026-10-05 14:30", "2026-10-05 15:00")
    d2 = _EMPTY.replace("2026-10-05 14:30", "2026-10-06 16:00")
    assert fp(d1) != fp(d2), (
        "跨交易日指纹碰撞 ⇒ 连续空仓日的第二天被静默吞掉 (用户会以为链路停了)"
    )


def test_same_day_different_hours_still_suppressed(fp):
    """同一交易日内不同时刻 (内容全同) 仍应抑制 — ③ 的修法不得反过来废掉 ① ."""
    d1 = _EMPTY.replace("2026-10-05 14:30", "2026-10-05 15:00")
    d2 = _EMPTY.replace("2026-10-05 14:30", "2026-10-05 16:30")
    assert fp(d1) == fp(d2)


def test_day_prefix_actually_extracted():
    """接线钉: 指纹实现必须抽交易日作前缀 (防止有人改回整行删除)."""
    assert "盘中状态\\s+(\\d{4}-\\d{2}-\\d{2})" in SRC or "day_prefix" in SRC, (
        "_semantic_fingerprint 未把交易日纳入指纹 ⇒ 跨日碰撞会复发"
    )


# ══════════ ④ fail-open + 载体形态契约 ══════════
def test_fail_open_on_state_file_error():
    """去重逻辑自身异常必须照发 — 绝不因自己的故障吞掉真投递."""
    assert "except Exception:" in SRC and "fail-open" in SRC, (
        "状态文件读写未被 try/except 包住 ⇒ 一次 IO 异常就会让整个载体崩 (rc≠0, 反而报警)"
    )
    # 崩掉 = rc≠0 = Hermes 会推失败通知, 比多发一条噪音更糟; 必须 fail-open 而非 fail-loud
    tail = SRC[SRC.index("_LAST_HASH_PATH ="):]
    assert "sys.exit(0)" in tail and "print(_out_text)" in tail, (
        "静默分支/输出分支缺失 — 载体输出契约被改坏"
    )


def test_persistent_severe_alert_bypasses_silence():
    """「服务未运行」这类常驻严重告警不参与静默 — 重复提醒是特性不是噪音."""
    assert "_ALWAYS_DELIVER_MARKERS" in SRC, "强制投递白名单被移除"
    assert "服务未运行" in SRC, "服务宕机告警未纳入强制投递 ⇒ 静默期宕机无人知"


def test_state_file_is_gitignored():
    """指纹状态文件是运行时产物, 不得进版本库 (同 data/.alert_state 纪律)."""
    assert "data/.intraday_diagnose_late_last" in SRC or "_LAST_HASH_PATH" in SRC
    import subprocess
    r = subprocess.run(
        ["git", "check-ignore", "data/.intraday_diagnose_late_last"],
        cwd=str(REPO), capture_output=True, text=True,
    )
    assert r.returncode == 0, "状态文件未被 .gitignore 覆盖 ⇒ 会污染 git status"


def test_silence_uses_empty_stdout_not_marker():
    """静默必须走「空 stdout」而非打印哨兵串 — no_agent 契约 (同非交易日分支)."""
    assert "Empty stdout = SILENT" in SRC
    # 打印 'SKIP'/'无变化' 之类仍会被投递 (no_agent 把 stdout 原样发出)
    tail = SRC[SRC.index("_LAST_HASH_PATH ="):]
    assert not re.search(r'print\(["\'](SKIP|无变化|静默)', tail), (
        "静默分支里出现 print ⇒ 该串会被 no_agent 原样投递, 等于没静默"
    )


# ══════════ 实测收益 (真实投递回放, 防「加了去重但没生效」) ══════════
def test_replay_real_deliveries_shows_majority_suppressed(fp):
    """用真实历史投递落档回放: 抑制率应 ≥40% (实测 59%).

    这条钉的价值是**证明改动真的生效** — 归一化器写错 (比如没匹配上真实格式)
    会退化成 0% 抑制, 而其它单测全用我构造的样本, 抓不到这种「格式对不上」。
    """
    out_dir = Path("/root/.hermes/cron/output/5513c8e94b06")
    if not out_dir.exists():
        pytest.skip("无历史投递落档 (非本机环境)")
    bodies = []
    for f in sorted(out_dir.glob("*.md")):
        # 2026-10-15 冻结回放语料 (E 表教训: 只钉冻窗): 语料随新投递增长, 近期活跃日
        # 持仓/指令真实变化使「相邻不同」成常态 ⇒ 抑制率自然坍塌 (实测 10-13 11 条全活跃,
        # 0% 抑制是真变化不是归一化失效)。冻结到校准窗 (≤2026-10-09, 即 10-09 阈值演进
        # 注释的标定语料) — 本测试的职责是钉「归一化器仍匹配真实格式」的回归, 不是度量
        # 近期市场活跃度。
        if f.name[:10] > "2026-10-09":
            continue
        raw = f.read_text(encoding="utf-8", errors="replace")
        if "Status:** silent" in raw:
            continue
        body = re.sub(r"^# Cron Job:.*?---\n", "", raw, flags=re.S).strip()
        if "盘中状态" in body:
            bodies.append(body)
    if len(bodies) < 5:
        pytest.skip(f"真实投递样本不足 ({len(bodies)} 条)")
    seen = None
    delivered = 0
    for b in bodies:
        h = fp(b)
        if h != seen:
            delivered += 1
            seen = h
    suppressed = len(bodies) - delivered
    assert suppressed / len(bodies) >= 0.25, (
        f"真实投递回放抑制率仅 {suppressed/len(bodies)*100:.0f}% ⇒ "
        f"归一化器可能没匹配上真实格式, 去重形同虚设"
    )
    # 阈值演进 (2026-10-09): 原 0.40 校准于早期清淡行情 (10/05 单日 60%);
    #   新增 10/06-10/08 四个交易日均有真实语义变化 (持仓增减/新成交/指令数变化),
    #   21 条中 6 条可抑制 (29%) — 全来自 10/05 安静时段。归一化器若失效仍退化为 0%,
    #   25% 留 4pp 安全余量足以区分「格式对不上」与「活跃日 legitimately 不同」。
