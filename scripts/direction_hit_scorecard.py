#!/usr/bin/env python3
"""方向命中率记分牌 (2026-07-24) — 回答"LLM 到底看没看清方向"的信心仪表.

背景 (用户 2026-07-24): 对标的选择与指令方向没有底气。经查:
  - 方向信息其实已被生成 (挑战者辩论), 但分歧被降级为仓位折扣而非方向信号;
  - LLM 信号无法经典回测 (回放历史 LLM = 前视偏差), 正确形态是前向指令台账:
    每日指令冻结落库 + fwd_return_t1/t3 回填 —— 基建已在跑, 本脚本把它变成报表。

口径: playbook_instructions, direction IN (BUY, SELL_SHORT), fwd_return 方向调整
  (BUY 用原值, SELL_SHORT 取负)。命中 = 方向调整后 T+1 收益 > 0。
只读, 落 data/direction_scorecard_latest.json + 控制台表。

用法: ./venv/bin/python scripts/direction_hit_scorecard.py [--since 2026-07-01]
"""
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "ftseek_us.db"
OUT = ROOT / "data" / "direction_scorecard_latest.json"


def q(conn, where: str, params=()):
    """返回 (n, hit_pct_t1, dir_ret_t1_pct, hit_pct_t3, dir_ret_t3_pct)."""
    r = conn.execute(f"""
        SELECT COUNT(*) n,
          ROUND(AVG(CASE WHEN (direction='BUY' AND fwd_return_t1>0)
                          OR (direction='SELL_SHORT' AND fwd_return_t1<0) THEN 1.0 ELSE 0 END)*100,1) hit1,
          ROUND(AVG(CASE direction WHEN 'BUY' THEN fwd_return_t1 ELSE -fwd_return_t1 END)*100,3) ret1,
          ROUND(AVG(CASE WHEN fwd_return_t3 IS NULL THEN NULL
                         WHEN (direction='BUY' AND fwd_return_t3>0)
                          OR (direction='SELL_SHORT' AND fwd_return_t3<0) THEN 1.0 ELSE 0 END)*100,1) hit3,
          ROUND(AVG(CASE WHEN fwd_return_t3 IS NULL THEN NULL
                         WHEN direction='BUY' THEN fwd_return_t3 ELSE -fwd_return_t3 END)*100,3) ret3
        FROM playbook_instructions
        WHERE fwd_return_t1 IS NOT NULL AND direction IN ('BUY','SELL_SHORT') AND {where}
    """, params).fetchone()
    return dict(n=r[0], hit_t1=r[1], dir_ret_t1=r[2], hit_t3=r[3], dir_ret_t3=r[4])


def src_expr():
    """来源切分: raw_instruction_json 的 source 标记 (DA=deep_analysis默认无标/UE/topup)."""
    return """CASE
        WHEN raw_instruction_json LIKE '%topup_pool%' THEN 'topup'
        WHEN raw_instruction_json LIKE '%universe_expansion%' THEN 'UE'
        ELSE 'DA' END"""


def main() -> int:
    since = "2026-07-01"
    if "--since" in sys.argv:
        since = sys.argv[sys.argv.index("--since") + 1]
    conn = sqlite3.connect(str(DB))

    report = {"generated_at": datetime.now().isoformat(), "since": since,
              "benchmark": "命中=方向调整后 T+1 收益>0; 50%=掷硬币基线", "sections": {}}

    print(f"═══ 方向命中率记分牌 (since {since}, 前向台账非回测) ═══")
    print(f"{'切片':<26} {'n':>5} {'T+1命中%':>9} {'T+1方向收益%':>12} {'T+3命中%':>9} {'T+3收益%':>9}")
    print("-" * 78)

    def emit(section, label, where, params=()):
        d = q(conn, f"date>=? AND {where}", (since, *params))
        report["sections"].setdefault(section, {})[label] = d
        if d["n"]:
            print(f"{label:<26} {d['n']:>5} {d['hit_t1']:>9} {d['dir_ret_t1']:>12} "
                  f"{str(d['hit_t3'] or '-'):>9} {str(d['dir_ret_t3'] or '-'):>9}")

    emit("total", "全部", "1=1")
    emit("direction", "BUY", "direction='BUY'")
    emit("direction", "SELL_SHORT", "direction='SELL_SHORT'")
    # 来源切分 (跨策略污染纪律: topup/UE/DA 分开看)
    for s in ("DA", "UE", "topup"):
        emit("source", f"来源:{s}", f"({src_expr()})=?", (s,))
    # D4 (2026-07-24, DEB §十-b): topup 盘前/盘中窗切分 — 盘中窗预注册判据:
    #   n≥30 前不下结论; 命中<48% 或期望显著负 → 关盘中窗 (intraday_start=end 一行回滚)
    emit("source", "topup·盘中",
         "raw_instruction_json LIKE '%topup_pool%' AND raw_instruction_json LIKE '%\"timing\": \"intraday\"%'")
    emit("source", "topup·盘前",
         "raw_instruction_json LIKE '%topup_pool%' AND raw_instruction_json NOT LIKE '%\"timing\": \"intraday\"%'")
    # 辩论共识切分 (conf 0-100 制: <=40 分歧 / 41-75 部分一致 / >75 高确认)
    emit("debate", "分歧(conf<=40)", "confidence<=40")
    emit("debate", "部分一致(40-75]", "confidence>40 AND confidence<=75")
    emit("debate", "高确认(>75)", "confidence>75")
    # 2026-07-25 (信息源复盘): debate_outcome 是隐藏分层器 (分歧BUY 67% vs 弃权 47%) — 固定成周度切片;
    #   SHORT 相关 outcome 历史全烂 (分歧SHORT 33%/-2.85%, 一致SHORT 0%/-4.61%), 监控是否持续
    emit("debate_outcome", "弃权(一方SKIP)", "raw_instruction_json LIKE '%一方弃权%'")
    emit("debate_outcome", "方向分歧·BUY", "raw_instruction_json LIKE '%方向分歧(BUY%'")
    emit("debate_outcome", "方向分歧·SHORT", "raw_instruction_json LIKE '%方向分歧(SELL_SHOR%'")
    emit("debate_outcome", "2模型一致", "raw_instruction_json LIKE '%2模型一致%'")
    # 2026-07-25 (信息源复盘): UE 按 self_tier 分层 — 八月终裁事件赛道 (HARD_EVENT 专营)
    #   判据消费端; 写侧 07-22 已落 origin.self_tier, 此处补读侧。n<30 不下结论。
    emit("ue_tier", "UE·HARD_EVENT", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%HARD_EVENT%'")
    emit("ue_tier", "UE·MARKET_CORROB", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%MARKET_CORROBORATED%'")
    emit("ue_tier", "UE·THESIS_ONLY", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%THESIS_ONLY%'")
    emit("ue_tier", "UE·tier未落(旧)", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json NOT LIKE '%self_tier%'")
    # 2026-07-25 (E1, DEB-da-ue-pe-agent-optimization): prompt_version 自动 A/B 切片 —
    #   每次 prompt 变更自动可判, 不再靠人工回忆"哪天改的 prompt"。n<30 不下结论。
    emit("prompt_ver", "UE·v2.1", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%v2.1%'")
    emit("prompt_ver", "UE·v2.2", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%v2.2%'")
    # 2026-07-25 (P2 rubric 判据 Q2): rubric_score 高低桶 vs 命中 — 与 conf 相关性对比的消费端
    emit("rubric", "UE·rubric≥3", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"rubric_score\": 3%' OR raw_instruction_json LIKE '%\"rubric_score\": 4%')")
    emit("rubric", "UE·rubric≤2", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"rubric_score\": 0%' OR raw_instruction_json LIKE '%\"rubric_score\": 1%' OR raw_instruction_json LIKE '%\"rubric_score\": 2%')")
    # 2026-07-25 (A2 self-critique shadow 判分): DROP/DOWNGRADE vs KEEP 的 fwd 对比 —
    #   若 DROP 组显著更差 → 再裁升级实拦 (那才是机制变更)
    emit("critique", "UE·critique:KEEP", "raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%\"verdict\": \"KEEP\"%'")
    emit("critique", "UE·critique:DOWN/DROP", "raw_instruction_json LIKE '%universe_expansion%' AND (raw_instruction_json LIKE '%\"verdict\": \"DROP\"%' OR raw_instruction_json LIKE '%\"verdict\": \"DOWNGRADE\"%')")
    # 2026-07-27 (S1, DEB-20260727 Q1): 首报渠道×fwd 切片 — S0 起 origin.info_channel 落真渠道;
    #   判据: n≥30/源, 某源命中≥55% 且另一源≤40% → 信息饮食权重调整立案
    for _ch in ("us_bloomberg", "us_cls", "us_cnbc_direct", "us_seekingalpha_direct",
                "us_wallstreetcn", "sec_8k", "none_matched"):
        emit("info_channel", f"UE·渠道:{_ch}",
             f"raw_instruction_json LIKE '%universe_expansion%' AND raw_instruction_json LIKE '%\"info_channel\": \"{_ch}\"%'")
    # regime 切分 (07-24 起落库补全, 早期多为 NULL)
    emit("regime", "regime:cautious", "LOWER(market_regime) LIKE '%caution%'")
    emit("regime", "regime:risk_on", "LOWER(market_regime) LIKE '%risk_on%'")
    emit("regime", "regime:未记录", "market_regime IS NULL OR market_regime=''")
    # 逐周趋势 (方向能力是否稳定 — 波动大=无稳定趋势判读能力)
    print("-" * 78)
    for wk, in conn.execute(
            "SELECT DISTINCT strftime('%W',date) FROM playbook_instructions "
            "WHERE date>=? AND fwd_return_t1 IS NOT NULL ORDER BY 1", (since,)):
        emit("weekly", f"第{wk}周", "strftime('%W',date)=?", (wk,))

    OUT.write_text(json.dumps(report, ensure_ascii=False, indent=1))
    print(f"\n解读指引: ①任何切片 n<30 不下结论 ②50%±5 视为无边缘 "
          f"③关注'分歧vs高确认'差值与逐周稳定性, 而非单周绝对值")
    print(f"已落盘 → {OUT.relative_to(ROOT)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
