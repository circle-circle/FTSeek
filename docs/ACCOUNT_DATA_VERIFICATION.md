# 富途模拟账户数据验证报告

**时间**: 2026-03-31 19:27  
**状态**: ✅ 可以拉取并同步

---

## ✅ 验证结果

| 数据类型 | API | 状态 | 数量 |
|---------|-----|------|------|
| **持仓** | position_list_query | ✅ | 5 条 |
| **今日订单** | history_order_list_query | ✅ | 16 条 |
| **今日成交** | history_deal_list_query | ✅ | 11 条 |
| **资金** | accinfo_query | ✅ | 可查询 |

---

## 📊 结论

✅ **可以拉取并同步富途模拟账户的资金、收益和交易记录**

同步功能已就绪，执行：
```bash
cd /opt/ftseek/FTSeek
python3 -m app.simulation.sync_service
```

**验证完成时间**: 2026-03-31 19:27
