#!/usr/bin/env python3
"""F5 一次性盘前探针 (DEB-20260924 §三.9): MarketRegimeGate 的 spy_price 盘前是否=昨收。

只读: 不下单/不写 DB/不改配置。在盘前 (ET 04:00-09:30) 运行才有裁决意义;
盘中运行只能证明取数路径活着 (spy_price 应≈现价, 三值互异)。

裁决判据 (见数前写死):
  - spy_price ≈ prev_close (|diff|<0.05%) 且 ≠ pre_price ⇒ 「盘前=昨收」成立,
    F5 升级为 P0 同型缺陷 (边界日暴露 33%, 见 DEB §三.9), 提修复批;
  - spy_price ≈ pre_price 或介于两者之间随盘前成交走动 ⇒ LV3 push 吃盘前成交,
    F5 关闭 (该路径无缺陷, 与 DA 的日K路径机制不同)。
"""
import sys
sys.path.insert(0, "/opt/ftseek/FTSeek-US")

def main():
    from app.core.config_container import get_config  # noqa: F401  (预热配置)
    from app.data.market_data_router import MarketDataRouter
    import subprocess, json
    PY = "/opt/ftseek/FTSeek-US/venv/bin/python3.11"
    # 对照腿: DA skill 的 collect_snapshot (get_market_snapshot 通道, E4 同源)
    r = subprocess.run([PY, "-c",
        "import sys;sys.path.insert(0,'/root/.hermes/skills/deep-stock-analysis/scripts');"
        "import data_collector as d,json;s=d.collect_snapshot('US.SPY');"
        "print(json.dumps({'prev_close':s.get('prev_close'),'pre_price':s.get('pre_price'),"
        "'last_price':s.get('last_price'),'session':s.get('session'),'update_time':s.get('update_time')}))"],
        capture_output=True, text=True, timeout=90)
    snap = json.loads(r.stdout.strip().splitlines()[-1]) if r.stdout.strip() else {}

    router = MarketDataRouter()
    ctx = router.get_spy_context() or {}
    spy_price = ctx.get("spy_price", 0.0)
    ma20 = ctx.get("spy_ma20", 0.0)

    pc = snap.get("prev_close") or 0.0
    pp = snap.get("pre_price") or 0.0
    print("=== F5 探针 (只读) ===")
    print(f"router.get_spy_context().spy_price = {spy_price}")
    print(f"snapshot: prev_close={pc} pre_price={pp} last_price={snap.get('last_price')} "
          f"session={snap.get('session')} update_time={snap.get('update_time')}")
    print(f"spy_ma20 = {ma20}  (spy_price {'>' if spy_price > ma20 else '<='} MA20)")
    if pc and spy_price:
        d_pc = abs(spy_price - pc) / pc * 100
        d_pp = abs(spy_price - pp) / pp * 100 if pp else None
        print(f"|spy_price - prev_close| = {d_pc:.3f}%   |spy_price - pre_price| = "
              f"{d_pp if d_pp is None else round(d_pp,3)}%")
        if snap.get("session") in ("pre", "pre_market") or (pp and pc and pp != pc):
            if d_pc < 0.05 and (d_pp is None or d_pp > 0.05):
                print("裁决: 🔴 盘前=昨收 成立 ⇒ F5 升级为 P0 同型缺陷")
            elif d_pp is not None and d_pp < 0.05:
                print("裁决: ✅ spy_price 跟随盘前价 ⇒ F5 关闭 (LV3 push 吃盘前成交)")
            else:
                print("裁决: ⚠️ 介于两者之间/无法归类 ⇒ 人工看数值, 不自动裁")
        else:
            print("裁决: (当前非盘前时段, 本跑只证明路径活着; 裁决须在 ET 04:00-09:30 的读数上做)")

if __name__ == "__main__":
    main()
