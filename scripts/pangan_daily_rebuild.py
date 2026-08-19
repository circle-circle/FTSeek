#!/usr/bin/env python3
"""股性卡每日全链重建 (DEB §14.3-Q6 裁决: 每日增量+重建, 成本可忽略).

链路: 分钟K增量(44票×~390根, 秒-分级) → 三族回放 → 卡重建
挂载: 盘后 cron (refresh_daily_bars.py 之后), 幂等。
"""
import json
import subprocess
import sys
import time
from datetime import date, timedelta
from pathlib import Path

ROOT = Path("/opt/ftseek/FTSeek-US")
PY = str(ROOT / "venv" / "bin" / "python")
DIR = ROOT / "data" / "replay_bars_1m"

# ── Step 1: 分钟K增量 (每票从文件末日拉到今天) ──
def step1_minute_increment():
    from futu import OpenQuoteContext, RET_OK, KLType, AuType
    ctx = OpenQuoteContext(host="127.0.0.1", port=11111)
    end = date.today().strftime("%Y-%m-%d")
    n = 0
    try:
        for fp in sorted(DIR.glob("US_*.json")):
            sym = "US." + fp.stem.replace("US_", "")
            try:
                bars = json.loads(fp.read_text())
            except Exception:
                continue
            start = bars[-1][0][:10] if bars else end
            new_rows, page_key = {}, None
            for _ in range(3):
                ret, df, page_key = ctx.request_history_kline(
                    sym, ktype=KLType.K_1M, start=start, end=end,
                    max_count=1000, page_req_key=page_key, autype=AuType.QFQ)
                if ret != RET_OK or df is None:
                    break
                for _, r in df.iterrows():
                    new_rows[str(r["time_key"])[:19]] = [
                        str(r["time_key"])[:19], float(r["open"]), float(r["high"]),
                        float(r["low"]), float(r["close"]), float(r["volume"])]
                if page_key is None:
                    break
                time.sleep(0.6)
            time.sleep(0.6)
            if new_rows:
                merged = {b[0]: b for b in bars}
                merged.update(new_rows)
                out = [merged[k] for k in sorted(merged)]
                tmp = fp.with_suffix(".tmp")
                tmp.write_text(json.dumps(out))
                tmp.rename(fp)
                n += 1
    finally:
        ctx.close()
    print(f"[step1] 分钟K增量更新 {n} 只")

# ── Step 2: 三族回放 ──
def step2_replays():
    for script in ("symbol_rule_replay.py", "daily_r9r10_replay.py", "minute_rules_replay.py"):
        t0 = time.time()
        r = subprocess.run([PY, str(ROOT / "scripts" / script)], capture_output=True, text=True)
        status = "OK" if r.returncode == 0 else f"FAIL({r.returncode})"
        print(f"[step2] {script}: {status} ({time.time()-t0:.0f}s)")
        if r.returncode != 0:
            print(r.stderr[-500:])

# ── Step 3: 卡重建 ──
def step3_build():
    r = subprocess.run([PY, str(ROOT / "scripts" / "build_symbol_rule_cards.py")],
                       capture_output=True, text=True)
    line = (r.stdout or "").splitlines()
    print(f"[step3] 卡重建: {line[0] if line else 'FAIL'}")

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--skip-minute":
        print("[skip] step1 分钟K增量")
    else:
        step1_minute_increment()
    step2_replays()
    step3_build()
    print("[done] 股性卡全链重建完成")
