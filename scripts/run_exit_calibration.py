#!/usr/bin/env python3
"""
MAE/MFE 退出校准分析 — CLI 入口

用法:
  python scripts/run_exit_calibration.py          # 分析最近 7 天
  python scripts/run_exit_calibration.py --days 14 # 分析最近 14 天
"""
import sys
import json
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.risk.exit_calibration_analyzer import ExitCalibrationAnalyzer


def main():
    days = 7
    if "--days" in sys.argv:
        idx = sys.argv.index("--days")
        if idx + 1 < len(sys.argv):
            days = int(sys.argv[idx + 1])

    analyzer = ExitCalibrationAnalyzer()
    report = analyzer.analyze(days=days)

    print(f"\n{'='*60}")
    print(f"  MAE/MFE 退出校准报告 (最近 {days} 天)")
    print(f"{'='*60}")
    print(f"  总交易数:       {report.total_trades}")
    print(f"  有效分析:       {report.analyzed_trades}")
    print(f"  胜率:           {report.win_rate:.0%}")
    print(f"  平均 PnL:       {report.avg_pnl_pct:+.2f}%")
    print(f"  平均 MAE:       {report.avg_mae_pct:.2f}%")
    print(f"  平均 MFE:       {report.avg_mfe_pct:.2f}%")
    print(f"")
    print(f"  分类:")
    print(f"    NoiseShakeout:   {report.noise_shakeout_count:3d} ({report.noise_shakeout_pct:.0f}%)")
    print(f"    ProfitGiveback:  {report.profit_giveback_count:3d} ({report.profit_giveback_pct:.0f}%)")
    print(f"    WellCalibrated:  {report.well_calibrated_count:3d} ({report.well_calibrated_pct:.0f}%)")
    print(f"    Unclassified:    {report.unclassified_count:3d}")

    if report.recommendations:
        print(f"\n  建议 ({len(report.recommendations)} 条):")
        for key, rec in report.recommendations.items():
            if key.startswith("_"):
                print(f"    ⚠️  {rec}")
            elif isinstance(rec, dict):
                action = rec.get("action", "?")
                reason = rec.get("reason", "")
                print(f"    🔧 [{action}] {key}: {reason}")
            else:
                print(f"    📊 {key}: {json.dumps(rec, ensure_ascii=False)[:80]}")
    else:
        print(f"\n  ✅ 无调整建议 (参数校准良好)")

    print(f"{'='*60}\n")


if __name__ == "__main__":
    main()
