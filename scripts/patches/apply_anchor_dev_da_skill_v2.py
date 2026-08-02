#!/usr/bin/env python3
"""DEB-feedback-loop F1 v2 (2026-08-02, R3-1): DA skill anchor_dev_pct 补源.

对 /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py 打补丁 (须 v1 已打):

  v1 两处缺口 (07-31 实测 DA 覆盖 8/16, bridge 文件 0/16):
  ① _anchor_dev 依赖 a.snapshot.price — 盘前 (06:00-06:35 ET) quote 失败率 ~50%
     (deep_analyzer._analyze_snapshot: snapshot={"_error":...} 时无 price 键)
  ② v1 只把字段加进 _inst (直写 DB 路径), bridge 文件 cand 没有 →
     app 侧 BridgeVotingAdapter 从文件加载恒 NULL (靠 skill 直写 DB 的 UPSERT 才救回一半)

  v2 编辑:
  A. cand = { 之前插入新计算块 — snapshot 缺价时回退 scan_result 候选价
     (同一次 cron 运行内, 分钟级, 与分析时刻价同尺)
  B. cand dict 加 "anchor_dev_pct": _anchor_dev (bridge 文件带上)
  C. 删除 v1 的重复计算块 (否则后执行的 v1 块会用 None 覆盖新值)

幂等: 已打 v2 直接退出 0。前置: v1 块必须存在 (否则报错先跑 v1)。
用法: ./venv/bin/python scripts/patches/apply_anchor_dev_da_skill_v2.py [target_path]
"""
import pathlib
import sys

TARGET = pathlib.Path(sys.argv[1] if len(sys.argv) > 1
                      else "/root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py")

# ── v1 已打的重复计算块 (将被删除, 由 cand 前的新块统一计算) ──
V1_BLOCK = '''                    # anchor_dev_pct (DEB-feedback-loop F1, 2026-07-31): LLM entry 相对分析时刻
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

# ── A: cand 构造点锚 + 新计算块 (16 空格缩进, 与 cand = { 同级) ──
CAND_ANCHOR = '''                cand = {
                    "symbol": vr["symbol"],
'''
NEW_COMPUTE = '''                # anchor_dev_pct v2 (DEB-feedback-loop F1 补源, 2026-08-02):
                #   snapshot 缺价 (盘前 quote 失败) 回退 scan 候选价 (同一次运行, 分钟级, 同尺);
                #   同时注入 bridge 文件 cand (v1 仅 _inst 直写 DB, 文件侧 0 覆盖).
                _anchor_dev = None
                try:
                    _mkt = float((a.get("snapshot") or {}).get("price", 0) or 0)
                    if _mkt <= 0:
                        _spx = {str(c.get("symbol") or "").replace("US.", ""):
                                float(c.get("price") or c.get("last_price") or 0)
                                for c in (scan_result.get("candidates") or [])}
                        _mkt = _spx.get(str(vr.get("symbol") or "").replace("US.", ""), 0.0)
                    _e0 = float(vr.get("entry_price") or 0)
                    if _mkt > 0 and _e0 > 0:
                        _anchor_dev = round((_e0 - _mkt) / _mkt * 100, 3)
                except Exception:
                    _anchor_dev = None

                cand = {
                    "symbol": vr["symbol"],
'''

# ── B: cand dict 加字段 ──
CAND_TAIL = '''                    "debate": _debate if _debate else None,
                }
'''
CAND_TAIL_NEW = '''                    "debate": _debate if _debate else None,
                    "anchor_dev_pct": _anchor_dev,
                }
'''


def main() -> int:
    src = TARGET.read_text()
    if "anchor_dev_pct v2" in src:
        print("已打 v2 补丁, 跳过 (幂等)")
        return 0
    if src.count(V1_BLOCK) != 1:
        print(f"FAIL: v1 块匹配数={src.count(V1_BLOCK)} (期望1) — 先跑 apply_anchor_dev_da_skill.py",
              file=sys.stderr)
        return 1
    if src.count(CAND_ANCHOR) != 1:
        print(f"FAIL: cand 锚匹配数={src.count(CAND_ANCHOR)} (期望1), 未改动", file=sys.stderr)
        return 1
    if src.count(CAND_TAIL) != 1:
        print(f"FAIL: cand 尾锚匹配数={src.count(CAND_TAIL)} (期望1), 未改动", file=sys.stderr)
        return 1
    src = (src
           .replace(V1_BLOCK, "")            # C: 删 v1 重复块
           .replace(CAND_ANCHOR, NEW_COMPUTE)  # A: 新计算块 (含 scan 价回退)
           .replace(CAND_TAIL, CAND_TAIL_NEW))  # B: cand 带字段
    TARGET.write_text(src)
    print(f"OK: {TARGET} v2 已打 (scan价回退 + bridge文件cand带字段 + v1重复块删除)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
