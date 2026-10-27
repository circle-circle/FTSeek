"""error_alert.py 契约 (2026-09-10, DEB-20260910 §4 议题四 A 案: .sh → Python + push hub).

原 `error_alert.sh` 六处缺陷全部实测 (§4.1), 后果是**恒不触发**: THRESHOLD=20 条/15min 而
实测 ERROR+CRITICAL 只有 0-5 条/日 ⇒ 近 7 日 0 告警, `data/.alert_state` 停在
`error_spike_2026080522`。它是 AGENT.md 认定的两道 ERROR 覆盖之一, 实际是纸面的。

本文件把 §4.3 的四条预注册判据 + 六处缺陷的回归**机器化**, 且刻意双向:
  判据 1 注入 3 条 ERROR **必须**触发 (改前注入 20 条以下收不到);
  判据 2 注入 20 条正文含 "ERROR" 的 INFO **不得**触发;
  判据 3 消息含 `"` 与换行的 ERROR **必须**可推且正文可读 (原 .sh 此时拼出非法 JSON,
          curl 失败被 `|| true` 吞掉 —— 恰好在错误最异常时哑火);
  另加: 冷却键逐指纹 (缺陷 5) / 推送失败不写冷却 / 多日志覆盖 (告警链断裂) /
        轮转档纳入窗口 / 相对基线阈值 (非硬编码 20)。
"""
import json
import os
import subprocess
import time
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import scripts.error_alert as ea

def _line(ts, name, level, msg):
    """主日志格式 (main.py:37-52 契约): asctime [threadName] [name] LEVEL: msg。"""
    return f"{ts:%Y-%m-%d %H:%M:%S},000 [MainThread] [{name}] {level}: {msg}"


@pytest.fixture
def env(tmp_path, monkeypatch):
    """隔离的 logs/ + 状态文件 + 假推送 (捕获正文, 不发网络)。"""
    logs = tmp_path / "logs"
    logs.mkdir()
    state = tmp_path / "state.json"
    pushed = []
    monkeypatch.setattr(ea, "push",
                        lambda title, body, dedup_key: pushed.append(
                            {"title": title, "body": body, "key": dedup_key}) or True)
    now = datetime(2026, 9, 10, 21, 15, 0)
    return {"logs": logs, "state": state, "pushed": pushed, "now": now,
            "box": {"push_ok": True}}


def _run(env, now=None, force=False, dry_run=False, window_min=15, state_path=None):
    return ea.run(now=now or env["now"], window_min=window_min,
                  state_path=state_path or env["state"],
                  logs_dir=env["logs"], dry_run=dry_run, force=force)


def _write(env, name, lines, day=None):
    """写日志文件; day 非空则写成该日轮转档 `<name>-YYYYMMDD`。"""
    p = env["logs"] / (f"{name}-{day:%Y%m%d}" if day else name)
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


# ─────────────────── 判据 1: 可触发性 (主判据) ───────────────────

def test_three_errors_trigger(env):
    """3 条 ERROR (地板阈值) 必须触发并推送 —— 改前同法注入 20 条以下收不到。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "app.data.router", "ERROR", f"所有数据源均失败: US.GPN {i}")
                                  for i in range(3)])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1, f"必须推送 1 条聚合消息, 实际 {len(env['pushed'])}"
    body = env["pushed"][0]["body"]
    assert "ERROR 突增 3 条/15min" in env["pushed"][0]["title"]
    assert "ftseek_us.log=3" in body and "所有数据源均失败" in body


def test_one_error_triggers_immediately(env):
    """ERROR/CRITICAL 是 P0: 1 条即触发, 由同签名冷却负责降噪。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "x", "ERROR", "e0")])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1


def test_critical_also_counts(env):
    """CRITICAL 与 ERROR 同级纳入计数 (原 .sh 注释亦如此声明)。"""
    ts = env["now"] - timedelta(minutes=2)
    _write(env, "ftseek_us.log", [_line(ts, "risk", "CRITICAL", f"kill_switch {i}")
                                  for i in range(3)])
    _run(env)
    assert len(env["pushed"]) == 1 and "CRITICAL" in env["pushed"][0]["body"]


# ─────────────────── 判据 2: 误报证明 (级别盲修复) ───────────────────

def test_info_lines_mentioning_error_do_not_trigger(env):
    """20 条正文含 "ERROR"/"CRITICAL" 的 **INFO** 行 ⇒ 不得触发 (缺陷 2: 行内任意位置匹配)。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log",
           [_line(ts, "app.x", "INFO", f"正文提到 ERROR 与 CRITICAL 字样但不算错误 {i}")
            for i in range(20)])
    assert _run(env) == 0
    assert env["pushed"] == [], "级别盲匹配复活 ⇒ INFO 正文含 ERROR 即告警 (原缺陷 2)"


def test_warning_lines_do_not_trigger(env):
    """WARNING 不计入 (本批 ③ 降噪后 WARNING 仍是 ERROR 的 143 倍, 计入即恒告警)。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "app.x", "WARNING", f"配额窗口满 {i}")
                                  for i in range(30)])
    _run(env)
    assert env["pushed"] == []


def test_untimestamped_error_lines_skipped_and_counted(env):
    """缺陷 3: 无时间戳行不得计入窗口 (原 .sh 字符串比较 ⇒ 中文首字节恒判"最近")。"""
    _write(env, "ftseek_us.log",
           ["完成: 刷新 0 | 已最新跳过 280 | 失败 1 ERROR",
            "扫描 281 个日K缓存文件 CRITICAL"] * 10)
    assert _run(env) == 0
    assert env["pushed"] == [], "无时间戳行被计入 ⇒ 恒告警 (原缺陷 3)"


def test_benign_yfinance_throttle_noise_does_not_trigger(env):
    """外部库瞬时限流「假 ERROR」(yfinance 降级路径): 不计入、不告警 (2026-10-27 增)。

    实证 10-27: Yahoo 限速使 yfinance 自身打 ERROR, 但调用侧已捕获 (None → data_quality
    missing, 下轮重试), 4 只失败票中 3 只本不入选、CVLT 仍 ok_dims=19 入场 ⇒ 零交易影响。
    """
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [
        _line(ts, "yfinance", "ERROR",
              "Failed to get ticker 'CVE' reason: ('Connection aborted.', ConnectionResetError(104, 'Connection reset by peer'))"),
        _line(ts, "yfinance", "ERROR", "$CVE: possibly delisted; no price data found  (period=60d)"),
        _line(ts, "yfinance", "ERROR", "$DIS: no price data found (period=60d)"),
    ])
    assert _run(env) == 0
    assert env["pushed"] == [], "yfinance 降级路径瞬时限流噪音不应触发 P0 告警"


def test_benign_filter_does_not_mask_real_error(env):
    """窄豁免不得误伤: 同文件里的真 ERROR 仍必须触发。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [
        _line(ts, "yfinance", "ERROR", "Failed to get ticker 'CVE' reason: reset"),
        _line(ts, "app.data.router", "ERROR", "所有数据源均失败: US.GPN"),
    ])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1, "真 ERROR 必须仍触发 (窄豁免误伤)"
    assert "所有数据源均失败" in env["pushed"][0]["body"]


# ─────────────────── 判据 3: JSON 健壮性 / 正文可读 ───────────────────

def test_message_with_quote_and_newline_stays_pushable(env):
    """含 `"` 与真实换行的 ERROR: 必须触发, 且正文单条错误占一行 (原 .sh 此时拼出非法 JSON)。"""
    ts = env["now"] - timedelta(minutes=3)
    p = env["logs"] / "ftseek_us.log"
    # 多行 record: 首行带时间戳+级别, 续行是消息里的换行 (③批的横幅形态)
    p.write_text(
        f'{ts:%Y-%m-%d %H:%M:%S},000 [MainThread] [app.x] ERROR: 解析失败 sym="US.GPN" '
        f'body={{"a": 1}} 反斜杠 \\ 结尾\n'
        '  续行: Traceback 第二行 (无时间戳)\n'
        f'{ts:%Y-%m-%d %H:%M:%S},100 [MainThread] [app.x] ERROR: 第二条 "引号" 错误\n'
        f'{ts:%Y-%m-%d %H:%M:%S},200 [MainThread] [app.x] ERROR: 第三条\n',
        encoding="utf-8")

    assert _run(env) == 0
    assert len(env["pushed"]) == 1, "含引号/换行的错误必须仍能推送"
    body = env["pushed"][0]["body"]
    # (a) 钉钉 payload 合法性: requests json= 序列化后必须能 round-trip
    payload = {"msgtype": "text", "text": {"content": env["pushed"][0]["title"] + "\n" + body}}
    wire = json.dumps(payload, ensure_ascii=False)
    assert json.loads(wire) == payload, "推送正文使 JSON 非法 (原缺陷 4)"
    # (b) 可读性: 每条错误占一行, 续行不得混进正文
    assert "Traceback 第二行" not in body, "续行不属窗口判据, 不得进正文"
    for ln in body.split("\n"):
        if ln.startswith(" • "):
            assert "\n" not in ln and ln.count("•") == 1


def test_multiline_record_continuation_counted_as_unparsed(env, caplog):
    """无时间戳续行必须**显式计数**并落诊断 (盲区可见, 不靠人事后对账)。"""
    ts = env["now"] - timedelta(minutes=3)
    p = env["logs"] / "ftseek_us.log"
    p.write_text(
        f'{ts:%Y-%m-%d %H:%M:%S},000 [MainThread] [app.x] ERROR: 横幅首行\n'
        + "".join(f"  续行 {i}\n" for i in range(4))
        + f'{ts:%Y-%m-%d %H:%M:%S},100 [MainThread] [app.x] ERROR: 第二条\n'
        + f'{ts:%Y-%m-%d %H:%M:%S},200 [MainThread] [app.x] ERROR: 第三条\n',
        encoding="utf-8")
    _run(env)
    st = json.loads(env["state"].read_text(encoding="utf-8"))
    assert st["last_run"]["unparsed"] == 4, f"续行计数错: {st['last_run']}"


# ─────────────────── 缺陷 5: 冷却键逐指纹 ───────────────────

def test_cooldown_is_per_fingerprint_not_per_hour(env):
    """同一小时内**第二类**错误必须仍告警 (原 .sh 全局小时键 ⇒ 第二类被压制)。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"第一类 失败 id={i}") for i in range(3)])
    _run(env)
    assert len(env["pushed"]) == 1

    ts2 = env["now"] - timedelta(minutes=1)
    _write(env, "ftseek_us.log",
           [_line(ts, "a", "ERROR", f"第一类 失败 id={i}") for i in range(3)]
           + [_line(ts2, "b", "ERROR", f"第二类 超时 id={i}") for i in range(3)])
    _run(env)
    assert len(env["pushed"]) == 2, "第二类错误被小时级冷却键压制 (原缺陷 5)"
    assert "第二类 超时" in env["pushed"][1]["body"]
    assert "第一类 失败" not in env["pushed"][1]["body"], "已冷却的类不得重复推"


def test_same_class_within_hour_suppressed(env):
    """兑现注释语义: 同一类错误 1 小时内只告警一次。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"同一类 id={i}") for i in range(3)])
    _run(env)
    _run(env, now=env["now"] + timedelta(minutes=15))
    _run(env, now=env["now"] + timedelta(minutes=30))
    assert len(env["pushed"]) == 1, f"同类错误一小时内推了 {len(env['pushed'])} 次"


def test_next_hour_can_alert_again(env):
    """跨小时冷却键失效 (键含 <日期>_<小时>) — 持续故障必须持续可见。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"持续故障 id={i}") for i in range(3)])
    _run(env)
    later = env["now"] + timedelta(hours=1)
    p = env["logs"] / "ftseek_us.log"
    p.write_text("\n".join(_line(later - timedelta(minutes=5), "a", "ERROR",
                                 f"持续故障 id={i}") for i in range(3)) + "\n", encoding="utf-8")
    _run(env, now=later)
    assert len(env["pushed"]) == 2


def test_fingerprint_ignores_numbers():
    """指纹去数字: 同类错误只是 id/price 不同不得算新类 (否则冷却键形同虚设)。"""
    assert ea.fingerprint("所有数据源均失败: US.GPN 第 3 次, 耗时 1.25s") == \
        ea.fingerprint("所有数据源均失败: US.GPN 第 9 次, 耗时 7.50s")
    assert ea.fingerprint("A 失败") != ea.fingerprint("B 失败")


# ─────────────────── 推送失败不得写冷却 ───────────────────

def test_push_failure_does_not_write_cooldown(env, monkeypatch):
    """推送失败 → exit 1 + 冷却键不写 ⇒ 下轮重试 (不得用"已告警"掩盖"没送到")。"""
    monkeypatch.setattr(ea, "push", lambda *a, **k: False)
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"e{i}") for i in range(3)])
    assert _run(env) == 1
    st = json.loads(env["state"].read_text(encoding="utf-8"))
    assert st["cooldown"] == {}, "推送失败却写了冷却键 ⇒ 该错误永久静默"
    assert st["last_run"]["push_failed"] is True


# ─────────────────── 告警链断裂: 多日志覆盖 ───────────────────

def test_invariant_check_log_is_covered(env):
    """daily_invariant_check 写 logs/invariant_check.log — 原 .sh 只读主日志 ⇒ 从未推送过。"""
    now = datetime(2026, 9, 10, 5, 15, 0)       # cron `5 5 * * 2-6` 之后第一个 */15 窗
    ts = now - timedelta(minutes=10)
    lines = [f"{ts:%Y-%m-%d %H:%M:%S},000 [invariant_check] ERROR: "
             f"[不变量自检] {n} 处违反: I1_pnl_vs_prices×{n}" for n in (1, 2, 3)]
    _write(env, "invariant_check.log", lines)
    assert _run(env, now=now) == 0
    assert len(env["pushed"]) == 1, "不变量自检违反仍进不了告警链"
    assert "invariant_check.log" in env["pushed"][0]["body"]


def test_self_log_excluded_from_scan(env):
    """本脚本自己的 logs/error_alert.log 必须在扫描面外 (否则自扫形成正反馈环)。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "error_alert.log", [_line(ts, "error_alert", "ERROR", f"检测器异常 {i}")
                                    for i in range(5)])
    _write(env, "error_alert.log-20260910", [_line(ts, "error_alert", "ERROR", "旧档也不算")])
    assert _run(env) == 0
    assert env["pushed"] == [], "自扫反馈环: 告警制造告警"
    assert all("error_alert.log" not in p.name for p in ea.active_sources(env["logs"]))


def test_new_log_file_auto_covered(env):
    """新增日志文件自动纳管 — 覆盖面靠 glob 而非登记表 (登记表正是断裂的成因)。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "brand_new_tool.log", [_line(ts, "new", "ERROR", f"新工具炸了 {i}")
                                       for i in range(3)])
    _run(env)
    assert len(env["pushed"]) == 1, "新日志文件需要人工登记才纳管 ⇒ 下一次断裂"


def test_rotated_archive_inside_window_is_scanned(env):
    """轮转发生在 03:12-03:48 CST (盘中窗内) ⇒ 当日轮转档必须纳入 15min 窗口。"""
    now = datetime(2026, 9, 10, 3, 45, 0)
    ts = now - timedelta(minutes=10)            # 03:35, 轮转前 ⇒ 只在轮转档里
    _write(env, "ftseek_us.log", [])
    _write(env, "ftseek_us.log", [], day=now)   # 当日轮转档
    p = env["logs"] / f"ftseek_us.log-{now:%Y%m%d}"
    p.write_text("\n".join(_line(ts, "a", "ERROR", f"轮转前的错误 {i}")
                           for i in range(3)) + "\n", encoding="utf-8")
    assert _run(env, now=now) == 0
    assert len(env["pushed"]) == 1, "只读当前档 ⇒ 轮转窗内的错误全丢 (daily_alert_summary 9/8 同族盲区)"


# ─────────────────── 阈值: 相对基线, 非硬编码 20 ───────────────────

def test_threshold_is_one_even_with_busy_baseline(env):
    """基线中位数保留诊断, 但不得抬高 P0 触发线。"""
    now = env["now"]
    slot_ts = now - timedelta(minutes=10)       # 与窗口同 (hour, quarter) 时段
    for off in range(1, 8):
        day = now - timedelta(days=off)
        _write(env, "ftseek_us.log",
               [_line(slot_ts - timedelta(days=off), "a", "ERROR", f"常态错误 {i}")
                for i in range(4)],
               day=day)
    base = ea.baseline_median(now, env["logs"])
    assert base["median"] == 4 and base["threshold"] == 1, base

    _write(env, "ftseek_us.log", [_line(slot_ts, "a", "ERROR", f"今天 {i}") for i in range(1)])
    _run(env)
    assert len(env["pushed"]) == 1, "1 条 ERROR 必须立即告警"


def test_threshold_floor_is_one(env):
    """零基线 (M=0) 时也保持 1 条即触发。"""
    base = ea.baseline_median(env["now"], env["logs"])
    assert base["median"] == 0 and base["threshold"] == 1, base
    assert ea.MIN_THRESHOLD == 1


def test_baseline_cache_reused_within_hour(env, monkeypatch):
    """基线按 (日期, 小时) 缓存 — 468 个轮转档不得每 15 分钟重扫一遍。"""
    calls = []
    monkeypatch.setattr(ea, "baseline_median",
                        lambda *a, **k: calls.append(a) or {"median": 0, "threshold": 3,
                                                             "days": [], "samples": []})
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"e{i}") for i in range(1)])
    for k in range(3):        # 21:15/21:30/21:45 — 同一小时内 (缓存键 = <日期>:<小时>)
        _run(env, now=env["now"] + timedelta(minutes=15 * k))
    assert len(calls) == 1, f"一小时内重算基线 {len(calls)} 次 (应缓存)"
    _run(env, now=env["now"] + timedelta(minutes=45))   # 22:00 跨小时 → 必须重算
    assert len(calls) == 2, "跨小时不重算 ⇒ 阈值跟着陈旧基线走"


# ─────────────────── 状态文件 ───────────────────

def test_legacy_plain_text_state_self_heals(env):
    """原 .sh 的 `data/.alert_state` 是纯文本单行 ⇒ 解析失败必须自愈, 不得崩。"""
    env["state"].write_text("error_spike_2026080522\n", encoding="utf-8")
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"e{i}") for i in range(3)])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1
    st = json.loads(env["state"].read_text(encoding="utf-8"))
    assert st["version"] == 2 and len(st["cooldown"]) == 1


def test_cooldown_keys_pruned(env):
    """冷却键只留今昨两日 — 状态文件不得无界增长。"""
    old = (env["now"] - timedelta(days=5)).strftime("%Y%m%d")
    env["state"].write_text(json.dumps(
        {"version": 2, "cooldown": {f"error_spike_{old}_21_旧类": {"count": 3}},
         "baseline_cache": {f"{old}:21": {"threshold": 3}}}), encoding="utf-8")
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", "e")])
    _run(env)
    st = json.loads(env["state"].read_text(encoding="utf-8"))
    today = env["now"].strftime("%Y%m%d")
    assert all(f"_{today}_" in key for key in st["cooldown"]), (
        f"5 日前的冷却键未被剪掉: {st['cooldown']}"
    )
    assert all(k.startswith(env["now"].strftime("%Y%m%d")) for k in st["baseline_cache"]), \
        f"陈旧基线缓存未剪: {list(st['baseline_cache'])}"


def test_state_write_failure_not_fatal(env, monkeypatch, tmp_path, caplog):
    """状态写失败只 WARNING, 不得让告警本身失败 (最坏 = 下轮重复推一次)。"""
    monkeypatch.setattr(ea, "STATE", tmp_path / "no_dir" / "s.json")
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"e{i}") for i in range(3)])
    assert _run(env, state_path=tmp_path / "no_dir" / "s.json") == 0
    assert len(env["pushed"]) == 1
    assert any("状态写失败" in r.message for r in caplog.records)


# ─────────────────── 检测器自身异常必须可区分 ───────────────────

def test_scan_error_is_loud_and_nonzero_exit(env, monkeypatch, caplog):
    """扫描炸了 → exit 1 + ERROR 留痕 ("零告警"与"守卫崩了"必须可区分)。"""
    def boom(*a, **k):
        raise RuntimeError("扫描炸了 (测试注入)")

    monkeypatch.setattr(ea, "window_sources", boom)
    assert _run(env) == 1
    assert any(r.levelname == "ERROR" and "失明" in r.message for r in caplog.records)
    assert env["pushed"] == []


def test_baseline_error_falls_back_to_floor(env, monkeypatch, caplog):
    """基线算不出 → 仍保持 1 条即触发 + WARNING 留痕。"""
    def boom(*a, **k):
        raise RuntimeError("基线炸了 (测试注入)")

    monkeypatch.setattr(ea, "baseline_median", boom)
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [_line(ts, "a", "ERROR", f"e{i}") for i in range(3)])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1
    assert any("地板阈值" in r.message for r in caplog.records)


# ─────────────────── 口径交叉核对 (防重复实现漂移) ───────────────────

def test_rotated_sources_matches_daily_alert_summary_convention():
    """轮转档命名口径必须与 daily_alert_summary.resolve_log_sources 一致 (logrotate dateext)。

    在**子进程**里比对: import daily_alert_summary 会执行 load_dotenv + 改写 sys.path
    (其 :45-53), 在 pytest 会话里做这件事会污染后续用例的模块解析。
    (2026-09-11: 原第二导入根 = 已停用港股仓, 随日报港股段退役; 子进程隔离照旧必要,
     因为 load_dotenv 与 sys.path 改写两个副作用仍在。)
    """
    repo = Path(__file__).resolve().parents[1]
    code = (
        "import sys, json; sys.path.insert(0, %r);"
        "from datetime import datetime;"
        "import importlib.util;"
        "spec = importlib.util.spec_from_file_location('das', %r);"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m);"
        "print(json.dumps(m.resolve_log_sources(%r, datetime(2026,9,8,0,0),"
        " datetime(2026,9,10,23,59))))"
    ) % (str(repo), str(repo / "scripts" / "daily_alert_summary.py"),
         str(repo / "logs" / "ftseek_us.log"))
    try:
        out = subprocess.run([str(repo / "venv" / "bin" / "python"), "-c", code],
                             capture_output=True, text=True, timeout=120, cwd=str(repo))
        theirs = json.loads(out.stdout.strip().splitlines()[-1])
    except Exception as e:                      # 无 venv / 无日志档的环境 (CI) 不比
        pytest.skip(f"无法在子进程加载 daily_alert_summary: {e}")
    # resolve_log_sources 是**按单个日志族**解析的 (传 log_path), 故比对时把本实现的
    # glob 结果按同一族过滤 — 比的是"命名/后缀/存在性判定"口径, 不是覆盖面。
    mine = [str(p) for d in (datetime(2026, 9, 8), datetime(2026, 9, 9), datetime(2026, 9, 10))
            for p in ea.rotated_sources(d, repo / "logs")
            if p.name.startswith("ftseek_us.log-")]
    theirs_archives = [p for p in theirs if not p.endswith("ftseek_us.log")]
    assert sorted(mine) == sorted(theirs_archives), \
        f"轮转档口径漂移 (改 logrotate dateformat 必须同步两处): mine={mine} theirs={theirs_archives}"


def test_crontab_points_at_python_version():
    """crontab 是挂载面之一: 必须已从 .sh 切到 .py, 否则重写等于没上线。"""
    try:
        cur = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=30)
    except Exception as e:
        pytest.skip(f"crontab 不可用: {e}")
    if cur.returncode != 0:
        pytest.skip("crontab -l 失败 (无用户 crontab 的环境)")
    active = [ln for ln in cur.stdout.splitlines()
              if ln.strip() and not ln.strip().startswith("#") and "error_alert" in ln]
    assert active, "crontab 里已无 error_alert 条目 ⇒ 告警面被静默摘除"
    for ln in active:
        assert "error_alert.py" in ln, f"仍指向旧 .sh (六缺陷仍在): {ln}"
        assert "error_alert.log" in ln, f"stdout 未落盘 ⇒ 守卫自己崩了查不到: {ln}"
    assert len(active) >= 2, f"至少应覆盖盘中两条, 实际 {len(active)}: {active}"


def test_legacy_shell_script_no_longer_mounted():
    """旧 .sh 必须已移出 scripts/ 顶层 (减法: 留着就还会有人手跑它 = 六缺陷复活)。"""
    repo = Path(__file__).resolve().parents[1]
    assert not (repo / "scripts" / "error_alert.sh").exists(), \
        "scripts/error_alert.sh 仍在原位 — 应移入 scripts/archive/ 并在 DEB 落档"


# ─────────── 裸 Traceback 崩溃尾 (2026-10-15 审计盲区修复: 两条 cron 静默崩 21-25 天) ───────────

def test_orphan_traceback_tail_triggers(env):
    """cron 崩溃的裸 traceback (无时间戳无前缀) 必须触发告警 — 审计实证的原盲区形态。"""
    p = _write(env, "some_cron.log", [
        "Traceback (most recent call last):",
        '  File "scripts/x.py", line 137, in main',
        "TypeError: float() argument must be a string or a real number, not 'NoneType'",
    ])
    fresh = (env["now"] - timedelta(minutes=5)).timestamp()  # mtime 对齐 fake now
    os.utime(p, (fresh, fresh))
    assert _run(env) == 0
    assert len(env["pushed"]) == 1
    assert "裸Traceback崩溃尾" in env["pushed"][0]["body"]
    assert "some_cron.log" in env["pushed"][0]["body"]


def test_orphan_traceback_stale_mtime_not_counted(env):
    """mtime 早于窗口起点的旧崩溃尾不在本轮重复计 (它在自己那轮已告过)。"""
    p = _write(env, "old_cron.log", [
        "Traceback (most recent call last):",
        "ValueError: old crash",
    ])
    old = (env["now"] - timedelta(hours=1)).timestamp()  # 对齐 fake now, 早于窗口
    os.utime(p, (old, old))
    assert _run(env) == 0
    assert env["pushed"] == []


def test_traceback_after_error_line_not_double_counted(env):
    """logger.error(exc_info=True) 的 traceback 续行已随那条 ERROR 计入 ⇒ 不再出裸TB类。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "ftseek_us.log", [
        _line(ts, "app.x", "ERROR", "炸了"),
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
        "ValueError: boom",
    ])
    assert _run(env) == 0
    assert len(env["pushed"]) == 1
    assert "裸TB" not in env["pushed"][0]["body"]


def test_traceback_followed_by_new_output_not_counted(env):
    """崩溃后又有新的带时间戳输出追加 ⇒ 崩溃不再是文件尾 (已被覆盖), 不计。"""
    ts = env["now"] - timedelta(minutes=5)
    _write(env, "cron.log", [
        "Traceback (most recent call last):",
        "ValueError: boom",
        _line(ts, "app.x", "INFO", "后续正常运行"),
    ])
    assert _run(env) == 0
    assert env["pushed"] == []
