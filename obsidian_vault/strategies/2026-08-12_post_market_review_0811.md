---
date: 2026-08-12
type: post_market_review
trade_date: 2026-08-11
win_rate_net: 22%
pnl_net: -141.76
total_trades: 9
---

# 8/11 盘后复盘 + MFE_STALL入场侧诊断

## 一、当日总览
- 净值 $97,909 (日内-$56, 回撤-0.34%, 水位green)
- 9笔退出: 毛-$103.61 / 费$38.15 / 净-$141.76, 净胜率22%
- 唯一赢家 DUOL +$93.74 (SL_TRAIL_LOCK); 5日均值 -$40/日

## 二、退出族归因(近7日, 45笔)
| 退出族 | n | 净胜率 | PnL | 判定 |
|---|---|---|---|---|
| SL_TRAIL_LOCK | 12 | 83% | +$273 | 🟢唯一正贡献 |
| PRE_CLOSE_EXIT | 1 | 100% | +$94 | 🟢 |
| SL_BREAKEVEN | 17 | 29% | -$43 | ⚠️含假盈利(费用吃毛利) |
| DEAD_SIGNAL_EXIT | 4 | 0% | -$116 | 🔴入场即死 |
| **MFE_STALL_EXIT** | **10** | **0%** | **-$532** | 🔴**最大失血点** |

## 三、MFE_STALL入场侧诊断 (DA指令, 近7日, STALL n=6 vs OTHER n=18)

### 数值特征: 无区分度
| 特征 | STALL | OTHER | 结论 |
|---|---|---|---|
| confidence | 43.3 | 31.4 | ❌conf高的反而stall(8/7"conf无预测力"再实锤) |
| atr_pct | 0.04 | 0.04 | 无差 |
| risk_reward | 2.68 | 2.95 | 无差 |
| SL距入场% | 4.82 | 3.77 | 弱信号, 不显著 |

### 文本/条件特征: 有信号 ⚠️
| 特征 | STALL | OTHER | 差 |
|---|---|---|---|
| **news_positive触发** | **100% (6/6)** | 72% (13/18) | ⚠️STALL笔无一例外 |
| insider_net_buy触发 | 50% | 22% | ⚠️2.3倍 |
| thesis含"等回调/回踩" | 67% | 44% | ⚠️thesis自己都在说等回调 |

### 解读
1. **news_positive是STALL笔的必要条件**(6/6=100%) — 新闻驱动的追涨入场,
   价格已消化消息, 入场后MFE从未起步(GOOG MFE+0.25%/ORCL+0.32%)
2. thesis文本与触发矛盾: DA分析师自己在thesis里写"等待回调/分批承接",
   但触发机制照追不误 — **论文(thesis)与扳机(trigger)脱节**
3. conf完全反向(conf高→更易stall), 再次确认conf不是质量信号

### 样本局限
- n=6 vs 18,  Fisher精确检验不显著(p≈0.2量级) — **方向可靠, 幅度不可靠**
- news_positive在OTHER组也72%常见 → 不是充分条件, 只是必要嫌疑
- 需攒样验证, 暂不动门禁

## 四、其他落地项
1. ✅ 隔夜shadow emit修复生效: 8/11三笔benign退出全落盘, 8/10两笔补录完成
2. ✅ Qoder未提交改动已commit(c74661f3): RFC-064隔夜重置/极端gap作废/盘前锚价修复
3. ✅ v0.64门禁首日实战: LIF盘前-20%作废成功(没接飞刀), JD被SL过宽拦截
4. 🔴 TSM 09:31 TIME_EXIT秒杀=wall-clock bug最后一单(RFC-064已修)
5. 盘前失效评估器首跑: 9只评估0作废(锚点全挺过盘前)

## 五、下一步(挂起)
- MFE_STALL影子打标: 给news_positive+insider_net_buy触发的入场打shadow标签攒样(n≥30再裁是否加门禁)
- DEAD_SIGNAL入场侧同法诊断(4笔0胜率-$116)
- 费用治理: 7日费$187.71占毛亏57%
