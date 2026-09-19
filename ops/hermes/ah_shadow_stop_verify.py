#!/usr/bin/env python3
"""AH 影子停用的**运行时**验证 (真门控窗口 = ET 19:50–20:05 = CST 07:50–08:05)。

坑 (自己踩过, 写进脚本防复发):
  ① 不能用 `l[:16] >= "2026-09-19 07:40"` 这种逐字符比较 —— 无时间戳的行以 `[` 开头 (0x5B > '2')
     会全部"通过"过滤, 窗口读数因此虚高;
  ② 第一次核对搭在了 CST 傍晚(=ET 清晨=PREMARKET 相位), 两个门都不在该窗口 ⇒ 那次"0 行"是空验证。
     判据: 窗口内必须看到**负面对照**(同挂载点的 entry_sync/stale_anchor/refresh_daily_bars 影子)
     被调度过, 才能说"调度器进了那个分支而影子没写"。
"""
import re
import subprocess
from datetime import datetime, timedelta
from pathlib import Path

REPO = Path("/opt/ftseek/FTSeek-US")
JSONL = REPO / "data" / "shadow" / "ah_exit_shadow.jsonl"
LOG = REPO / "logs" / "ftseek_us.log"
PURGE_MTIME = datetime(2026, 9, 18, 18, 16)   # 清理写回时刻 (之后若无写入 = 停用生效)
TS = re.compile(r"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2})")


def _window(cutoff: datetime) -> list[str]:
    out = []
    for line in LOG.read_text(errors="ignore").splitlines():
        m = TS.match(line)
        if not m:
            out.append(line)          # 无时间戳的裸 print: 只能算"窗口附近", 单独标注
            continue
        try:
            if datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S") >= cutoff:
                out.append(line)
        except ValueError:
            continue
    return out


def main() -> int:
    now = datetime.now()
    cutoff = now - timedelta(minutes=40)      # cron 08:15 触发 ⇒ 覆盖 07:35-08:15 (含两个门)
    print(f"[AH 停用运行时验证] {now:%Y-%m-%d %H:%M:%S} CST; 窗口 = {cutoff:%H:%M} 起")
    if not JSONL.exists():
        print("  ✗ 影子文件不存在(异常)")
        return 0
    st = JSONL.stat()
    mtime = datetime.fromtimestamp(st.st_mtime)
    print(f"  影子 jsonl: mtime={mtime:%m-%d %H:%M} size={st.st_size} 行={len(JSONL.read_text().splitlines())}")
    win = _window(cutoff)
    ts_win = [l for l in win if TS.match(l)]
    print(f"  窗口内带时间戳行数: {len(ts_win)} (裸 print {len(win) - len(ts_win)} 行一并检查)")
    hits = [l for l in win if "AH影子" in l or "ah_exit_shadow" in l]
    print(f"  ① 影子统计行/异常: {len(hits)}" + (f" -> {hits[-3:]}" if hits else " (0)"))
    neg = [l for l in win if re.search(r"entry_sync_shadow|stale_anchor|refresh_daily_bars|日K缓存刷新", l)]
    print(f"  ② 负面对照(同挂载点是否被调度): {len(neg)}" + (f" -> {neg[-2:]}" if neg else " (0 ⇒ 无法证伪)"))
    print(f"  ③ 服务: {subprocess.run(['systemctl','is-active','ftseek-us'], capture_output=True, text=True).stdout.strip()}")
    if hits:
        print("  ⇒ **未停用**: 窗口内仍有影子活动, 复查配置/重启")
    elif mtime <= PURGE_MTIME:
        if neg:
            print("  ⇒ **停用生效且已证伪条件成立**: 调度器进过该分支(负面对照非空)而影子未写")
        else:
            print("  ⇒ 停用生效(文件未增长)但**结论弱**: 负面对照为空, 无法排除'根本没进该分支'")
    else:
        print("  ⇒ 文件在清理后被写过但日志无影子行: 需查是谁写的")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())