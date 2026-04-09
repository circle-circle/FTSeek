# FTSeek 开市前检查清单

**版本**: V1.0  
**创建时间**: 2026-03-14  
**适用**: 每个交易日开市前

---

## ✅ 系统检查 (08:30 前完成)

### 1. 服务状态检查

```bash
# 检查 FutuOpenD
ps aux | grep FutuOpenD | grep -v grep
# ✅ 应显示运行中的进程

# 检查 Nginx
systemctl status nginx
# ✅ 应显示 active (running)

# 检查 FTSeek 服务
ps aux | grep futu_web | grep -v grep
# ✅ 应显示运行中的进程
```

- [ ] FutuOpenD 运行正常
- [ ] Nginx 运行正常
- [ ] FTSeek Web 服务运行正常

---

### 2. API 连接检查

```bash
cd /opt/ftseek/FTSeek
export API_TOKEN=test_token
python3 -c "
from app.ai import AIService
ai = AIService()
print('AI API:', '✅' if ai.provider else '❌')
"
```

- [ ] AI API (Bailian) 连接正常
- [ ] 富途 OpenD 连接正常
- [ ] 钉钉推送正常

---

### 3. 配置文件检查

```bash
cat /opt/ftseek/FTSeek/.env | grep -E "^(FTSEEK_|AI_|BAILIAN_)"
```

**必须确认**:
- [ ] `FTSEEK_ENABLE_AI=true`
- [ ] `BAILIAN_API_KEY=sk-xxx` (已配置)
- [ ] `FTSEEK_AI_WEIGHT=0.3`
- [ ] `AI_DAILY_BUDGET=50`
- [ ] `FTSEEK_TRADING_MODE=simulate` (⚠️ 必须为 simulate)

---

## ✅ 定时任务检查 (08:40 前完成)

### 4. 调度器状态

```bash
cd /opt/ftseek/FTSeek
python3 -c "
from scripts.scheduler import get_trading_scheduler
scheduler = get_trading_scheduler()
print(f'定时任务：{len(scheduler.scheduler.jobs)} 个')
"
```

- [ ] 调度器已启动
- [ ] 定时任务数量 ≥ 13 个
- [ ] 盘前检查任务已配置 (09:00)
- [ ] 收盘复盘任务已配置 (16:30)

---

### 5. 实时行情订阅

```bash
python3 -c "
from app.data.realtime_subscriber import get_realtime_subscriber
subscriber = get_realtime_subscriber()
status = subscriber.get_subscription_status()
print(f'连接：{status[\"connected\"]}')
print(f'已订阅：{status[\"subscribed_codes\"]}')
"
```

- [ ] 行情订阅已连接
- [ ] 已订阅股票：HK.00700, HK.9988 等

---

## ✅ 风控系统检查 (08:50 前完成)

### 6. 风险监控状态

```bash
python3 -c "
from app.risk.realtime_alert import get_realtime_risk_monitor
monitor = get_realtime_risk_monitor()
status = monitor.get_current_status()
print(f'连续亏损：{status[\"consecutive_losses\"]}')
print(f'回撤：{status[\"drawdown\"]:.2%}')
print(f'日盈亏：HK${status[\"daily_pnl\"]:.2f}')
"
```

- [ ] 连续亏损 < 3
- [ ] 回撤 < 10%
- [ ] 日盈亏正常 (无异常亏损)

---

### 7. 动态仓位配置

```bash
python3 -c "
from app.risk.dynamic_position_sizer import get_dynamic_position_sizer
sizer = get_dynamic_position_sizer()
print(f'基础仓位：{sizer.base_position:.0%}')
print(f'最小仓位：{sizer.min_position:.0%}')
print(f'最大仓位：{sizer.max_position:.0%}')
"
```

- [ ] 基础仓位：10%
- [ ] 最小仓位：5%
- [ ] 最大仓位：30%

---

## ✅ 数据记录检查 (08:55 前完成)

### 8. 交易记录系统

```bash
ls -la /opt/ftseek/FTSeek/data/trade_records/
ls -la /opt/ftseek/FTSeek/data/prediction_tracking/
```

- [ ] trade_records 目录存在
- [ ] prediction_tracking 目录存在
- [ ] 目录可写

---

### 9. 日志系统

```bash
ls -la /opt/ftseek/FTSeek/logs/
du -sh /opt/ftseek/FTSeek/logs/
```

- [ ] 日志目录存在
- [ ] 日志文件大小正常 (< 100MB)
- [ ] 日志轮转正常

---

## ✅ 端到端测试 (09:00 前完成)

### 10. 完整流程测试

```bash
cd /opt/ftseek/FTSeek
python3 scripts/test_full_flow.py
```

**必须全部通过**:
- [ ] Step 1: 数据订阅 ✅
- [ ] Step 2: 因子计算 ✅
- [ ] Step 3: 策略融合 ✅
- [ ] Step 4: 风控检查 ✅
- [ ] Step 5: 交易执行 ✅
- [ ] Step 6: 记录验证 ✅
- [ ] Step 7: 报告生成 ✅

---

## ✅ 最终确认 (09:05 前完成)

### 11. 开市前最终检查

```bash
echo "=== 开市前最终检查 ==="
echo "时间：$(date)"
echo ""
echo "交易模式：$(grep FTSEEK_TRADING_MODE .env)"
echo "AI 权重：$(grep FTSEEK_AI_WEIGHT .env)"
echo ""
echo "✅ 所有检查项已完成"
```

- [ ] 交易模式：**simulate** (模拟)
- [ ] AI 权重：0.3
- [ ] 所有检查项通过
- [ ] 钉钉推送测试成功

---

## 🚨 紧急情况处理

### 如果发现异常

1. **API 连接失败**
   ```bash
   # 重启 FutuOpenD
   /opt/ftseek/Futu_OpenD_9.6.5618_Centos7/start.sh
   
   # 重启 FTSeek
   cd /opt/ftseek/FTSeek && ./start.sh
   ```

2. **配置错误**
   ```bash
   # 恢复备份配置
   cp config/strategy_params.json.bak config/strategy_params.json
   ```

3. **风控触发**
   ```bash
   # 查看风险状态
   python3 -c "from app.risk.realtime_alert import get_realtime_risk_monitor; print(get_realtime_risk_monitor().get_alerts())"
   ```

4. **系统崩溃**
   ```bash
   # 查看日志
   tail -100 logs/error.log
   
   # 联系技术支持
   ```

---

## 📞 紧急联系人

| 角色 | 联系方式 |
|------|----------|
| 系统管理员 | [待填写] |
| 策略负责人 | [待填写] |
| 风控负责人 | [待填写] |

---

## 📊 检查记录

| 日期 | 检查人 | 状态 | 备注 |
|------|--------|------|------|
| 2026-03-15 | [待填写] | ⏳ 待执行 | 首个交易日 |

---

*检查清单版本：V1.0*  
*最后更新：2026-03-14*  
*下次审查：2026-03-21*
