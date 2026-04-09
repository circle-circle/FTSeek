# FTSeek 增强大盘指南

**更新时间**: 2026-03-31 20:13  
**状态**: ⚠️ 增强 API 开发中

---

## 📊 当前可用 API

### 基础 API (`/api/simulation/`)

| 端点 | 功能 | 状态 |
|------|------|------|
| `GET /positions` | 获取持仓 | ✅ |
| `GET /trades` | 获取交易 | ✅ |
| `GET /metrics` | 获取指标 | ✅ |

---

## 🔧 增强功能开发中

### 1. 美股/港股区分

**计划 API**:
```
GET /api/simulation/v2/positions?market=HK  # 港股持仓
GET /api/simulation/v2/positions?market=US  # 美股持仓
GET /api/simulation/v2/dashboard-summary    # 大盘总览（分市场）
```

**返回格式**:
```json
{
  "status": "ok",
  "data": {
    "hk_positions": {"count": 5, "market_value": 100000},
    "us_positions": {"count": 3, "market_value": 50000}
  }
}
```

### 2. 交易 - 信号 - 策略联动

**计划 API**:
```
GET /api/simulation/v2/signal-linkage?signal_id=xxx
GET /api/simulation/v2/strategy-performance
```

**返回格式**:
```json
{
  "signal": {"id": "xxx", "strategy": "confluence_v2"},
  "trades": [{"id": "yyy", "pnl": 1000}],
  "position": {"unrealized_pnl": 500}
}
```

---

## 📝 当前替代方案

### 查看港股持仓
```bash
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx" | \
python3 -c "import sys,json; d=json.load(sys.stdin); \
print('港股:', [p for p in d['data'] if p['code'].startswith(('HK.','0'))])"
```

### 查看美股持仓
```bash
curl "http://39.97.58.4:8899/api/simulation/positions?token=xxx" | \
python3 -c "import sys,json; d=json.load(sys.stdin); \
print('美股:', [p for p in d['data'] if p['code'].startswith('US.')])"
```

---

## 🎯 下一步

1. ✅ 清理演示数据 - 已完成
2. ✅ 同步真实数据 - 已完成
3. ⏳ 增强 API 开发 - 进行中
4. ⏳ 交易 - 信号 - 策略联动 - 开发中

---

**预计完成时间**: 30 分钟
