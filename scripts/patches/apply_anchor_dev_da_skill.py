#!/usr/bin/env python3
"""DEB-feedback-loop F1 (2026-07-31): DA skill 侧 anchor_dev_pct 插桩.

对 /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py 打补丁:
  1. _rr 计算块后插入 _anchor_dev 计算 (LLM entry vs 分析时刻 snapshot 价, 带符号 %)
  2. _inst dict 的 "risk_reward" 行后加 "anchor_dev_pct": _anchor_dev

幂等: 已打补丁时直接退出 0。FTSeek 侧 db.py 已先加列 (ensure_tables 幂等 ALTER),
旧 skill 不写该列为 NULL, 向后兼容。
"""
import pathlib
import sys

TARGET = pathlib.Path("/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py")

OLD_RR = '''                    _rr = None
                    try:
                        _e = float(vr["entry_price"]); _s = float(vr["stop_loss"]); _t = float(vr["take_profit"])
                        _rps = abs(_e - _s)
                        if _rps > 0:
                            _rr = round(abs(_t - _e) / _rps, 2)
                    except Exception:
                        _rr = None
'''
NEW_RR = '''                    _rr = None
                    try:
                        _e = float(vr["entry_price"]); _s = float(vr["stop_loss"]); _t = float(vr["take_profit"])
                        _rps = abs(_e - _s)
                        if _rps > 0:
                            _rr = round(abs(_t - _e) / _rps, 2)
                    except Exception:
                        _rr = None
                    # anchor_dev_pct (DEB-feedback-loop F1, 2026-07-31): LLM entry 相对分析时刻
                    #   快照价的带符号偏离 (%) — 与 UE scan→inject 漂移同尺, 供入场区合理性裁决.
                    _anchor_dev = None
                    try:
                        _mkt = float((a.get("snapshot") or {}).get("price", 0) or 0)
                        _e0 = float(vr.get("entry_price") or 0)
                        if _mkt > 0 and _e0 > 0:
                            _anchor_dev = round((_e0 - _mkt) / _mkt * 100, 3)
                    except Exception:
                        _anchor_dev = None
'''
OLD_FIELD = '''                        "risk_reward": _rr,
'''
NEW_FIELD = '''                        "risk_reward": _rr,
                        "anchor_dev_pct": _anchor_dev,
'''


def main() -> int:
    src = TARGET.read_text()
    if '"anchor_dev_pct": _anchor_dev,' in src:
        print("已打补丁, 跳过 (幂等)")
        return 0
    if src.count(OLD_RR) != 1:
        print(f"FAIL: _rr 锚点匹配数={src.count(OLD_RR)} (期望1), 未改动", file=sys.stderr)
        return 1
    if src.count(OLD_FIELD) != 1:
        print(f"FAIL: risk_reward 行匹配数={src.count(OLD_FIELD)} (期望1), 未改动", file=sys.stderr)
        return 1
    src = src.replace(OLD_RR, NEW_RR).replace(OLD_FIELD, NEW_FIELD)
    TARGET.write_text(src)
    print(f"OK: {TARGET} 已插入 anchor_dev_pct 插桩")
    return 0


if __name__ == "__main__":
    sys.exit(main())
