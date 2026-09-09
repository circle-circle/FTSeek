# ADR-007: Dashboard 架构加固 — 推流·缓存·错误统一

**日期**: 2026-06-15  
**状态**: 设计中

> ⚠️ 状态回写 (2026-09-06 治理审计): 自 2026-06-15 起近 3 个月停留"设计中", 状态悬挂;
> 需重新裁决推进或关闭, 勿据本文档预期 dashboard 架构变更。  
**作者**: Hermes

---

## 现状诊断

### 轮询现状 (10个 setInterval)

| 页面 | 端点 | 频率 | 数据来源 |
|------|------|------|----------|
| dashboard | `/api/dashboard/snapshot` | 30s | DataService 缓存 |
| dashboard | `/api/analytics/signal-quality` | 60s | SQLite 直查 |
| dashboard | `/api/risk/command` | 120s | 文件直读 |
| battle_plan | `/api/battle-plan` | 60s | DataService + Pipeline |
| stock_detail | orderbook + ticker | 30s | DataService 缓存 |
| trade | `/api/trade/snapshot` | 15s | DataService 缓存 |
| trade | slow_data + exec_quality | 5min | SQLite 直查 |
| trade | ext_rth_ui | 60s | 纯前端计算 |
| watchlist | fetchData | 30s | DataService 缓存 |
| system | loadSystemStatus | 30s | 进程状态 |

**问题**: 每分钟 ~12 次 HTTP 请求，90% 数据没变化。

### WebSocket 现状

已有完整基础设施 (`routes/ws.py`):
- `ConnectionManager` 管理连接（上限10）
- 订阅模型 (`subscribe` / `broadcast`)
- 心跳 30s
- `publish_event()` / `publish_event_sync()` API
- Nginx 已配置 `/ws/` 代理

**但只有 3 处调用**：
- `manual_order_service.py` → `order_update` (下单确认)
- `position_exit_monitor.py` → `position_update` / `risk_alert`

**大量事件类型未被激活**: `quote_update`, `signal_new` 等定义了但没人发。

### Nginx 现状

已有: HTTPS(DigiCert), gzip, rate-limit, WebSocket proxy, 安全头  
缺失: 静态资源缓存头（已部分在 FastAPI 层补了 Cache-Control）

### 错误响应现状

`api_response.py` 已创建 (`api_ok` / `api_error`) 但 **0 处路由使用**。  
30+ 处使用 5 种不同错误格式:
- `{"error": "..."}` + HTTP 200 (analytics.py: 12处)
- `JSONResponse({"error": "..."}, status_code=N)` (battle_plan.py: 10处)
- `{"success": false, "error": "..."}` (risk.py: 3处, dashboard.py: 1处)
- `{"error": "..."}` + HTTP 200 (dashboard.py: 1处)
- HTTPException (少量)

---

## 设计方案

### Phase 1: WebSocket 推流激活 (高影响)

**目标**: 将 DataService 的缓存刷新周期作为事件源，自动推送变更到前端。

#### 架构

```
┌─────────────────────────────────────────────────┐
│ DataService                                      │
│  cache.set(key, value) ──→ 检测值变化 ──→ publish_event() │
│                                                  │
│  positions_raw 变化 → emit "positions_update"     │
│  account_raw 变化   → emit "account_update"       │
│  quotes 变化        → emit "quote_update"         │
└────────────────────┬────────────────────────────┘
                     │
                     ▼
┌─────────────────────────────────────────────────┐
│ ws.py ConnectionManager.broadcast()              │
│  过滤: 只推 delta (价格变>0.01 才推)              │
└────────────────────┬────────────────────────────┘
                     │
        ┌────────────┼────────────┐
        ▼            ▼            ▼
   dashboard     trade.html   watchlist
   (订阅:        (订阅:       (订阅:
    positions,    positions,    quotes)
    account,      account,
    quotes)       orders)
```

#### 实现步骤

**Step 1: DataService 缓存写入时发事件**

```python
# data_service.py — _SharedCache.set() 增加变更检测
class _SharedCache:
    def set(self, key: str, value, ttl: float):
        old = self._store.get(key, (None,))[0]
        with self._lock:
            self._store[key] = (value, time.time() + ttl)
        # 异步通知（非阻塞）
        if old is not None and self._has_changed(key, old, value):
            self._emit_change(key, value)

    def _has_changed(self, key: str, old, new) -> bool:
        """智能变更检测 — 避免无意义推送"""
        if key == "positions_raw":
            # 比较持仓数量+每个持仓的unrealized_pnl
            return self._diff_positions(old, new)
        elif key.startswith("quotes_"):
            # 价格变动 > $0.01 才算变化
            return self._diff_quotes(old, new)
        elif key == "account_raw":
            # nav 变动 > $1 才算
            return abs(_nav(old) - _nav(new)) > 1.0
        return old != new  # 默认: 任何变化都推
```

**Step 2: 前端 WS 客户端 (base.html)**

```javascript
// base.html — 全局 WebSocket 客户端
const FTSeek_WS = {
    ws: null,
    handlers: {},       // event_type -> [callback]
    reconnectDelay: 2000,
    maxReconnectDelay: 30000,
    _fallbackTimers: [], // 降级回 polling 时的 timer

    connect() {
        const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
        this.ws = new WebSocket(`${proto}//${location.host}/ws/events`);

        this.ws.onopen = () => {
            this.reconnectDelay = 2000;
            // 订阅当前页面需要的事件
            const events = document.body.dataset.wsEvents?.split(',') || ['*'];
            this.ws.send(JSON.stringify({ type: 'subscribe', events }));
            // WS 连接成功 → 停止 fallback polling
            this._stopFallback();
        };

        this.ws.onmessage = (e) => {
            const msg = JSON.parse(e.data);
            if (msg.type === 'heartbeat' || msg.type === 'pong') return;
            (this.handlers[msg.type] || []).forEach(fn => fn(msg.data));
        };

        this.ws.onclose = () => {
            // 断线 → 自动降级回 polling
            this._startFallback();
            setTimeout(() => this.connect(), this.reconnectDelay);
            this.reconnectDelay = Math.min(this.reconnectDelay * 1.5, this.maxReconnectDelay);
        };
    },

    on(eventType, callback) {
        (this.handlers[eventType] ||= []).push(callback);
    },

    // 降级管理
    _stopFallback() { this._fallbackTimers.forEach(clearInterval); this._fallbackTimers = []; },
    _startFallback() { /* 各页面注册自己的 fallback polling */ },
};

// 页面初始化
document.addEventListener('DOMContentLoaded', () => FTSeek_WS.connect());
```

**Step 3: 各页面注册处理器**

```javascript
// dashboard.html
document.body.dataset.wsEvents = 'positions_update,account_update,quote_update';

FTSeek_WS.on('positions_update', (data) => {
    // 局部更新持仓区域，无需全量 reload
    renderPositions(data.positions);
});

FTSeek_WS.on('quote_update', (data) => {
    // 只更新价格数字
    data.forEach(q => updatePriceCell(q.symbol, q.price, q.change_pct));
});

// trade.html
document.body.dataset.wsEvents = 'positions_update,account_update,order_update,fill';
```

**Step 4: 保留 polling 作为降级**

```
WS 连接中 → 事件驱动更新 (0 HTTP 请求)
WS 断线   → 自动回退到现有 setInterval polling
WS 重连   → 停止 polling, 切回事件驱动
```

#### 预期效果

| 指标 | Before | After |
|------|--------|-------|
| HTTP 请求/分钟 | ~12 | ~0 (WS在线时) |
| 数据延迟 | 15-60s | <1s (缓存刷新即推) |
| 断线体验 | 无感知 | 自动降级polling |
| Futu RPC | 不变 | 不变 (由DataService控制) |

#### 不推的数据 (保持 polling)

| 端点 | 原因 |
|------|------|
| `/api/analytics/signal-quality` | SQLite 聚合，低频，60s 够了 |
| `/api/risk/command` | 文件读取，120s 够了 |
| `/api/battle-plan` | Pipeline 输出，一天一次 |
| ext_rth_ui | 纯前端计算，无服务端数据 |

---

### Phase 2: Nginx 静态资源缓存 (低影响)

**目标**: 让浏览器缓存静态文件 30 天，减少每次页面加载的带宽。

#### 实现

```nginx
# 在现有 nginx conf 的 server {} 内添加:

# ── 静态资源长缓存 (JS/CSS 带 hash 或版本) ──
location /static/ {
    proxy_pass http://127.0.0.1:8901;
    proxy_set_header Host $host;
    expires 30d;
    add_header Cache-Control "public, immutable";
    access_log off;
}

# ── HTML 模板不缓存 (总是拿最新) ──
location ~ \.html$ {
    proxy_pass http://127.0.0.1:8901;
    proxy_set_header Host $host;
    add_header Cache-Control "no-cache, no-store, must-revalidate";
}
```

#### 注意

- 当前 JS 文件无 hash (chart.umd.min.js, lightweight-charts.js)
- 短期: 用 `expires 7d` + ETag (nginx 自动)
- 长期: 文件名加版本号 (`app.v2.js`) 或 hash → `immutable`

---

### Phase 3: 错误响应统一迁移 (低影响)

**目标**: 所有 API 路由使用 `api_ok()` / `api_error()` 统一格式。

#### 迁移策略

**向后兼容**: 前端目前按各自格式解析，不能一次性全改。  
**渐进式**: 先改 analytics (12处最多)，前端加一个兼容层。

#### 前端兼容层

```javascript
// base.html — 统一 fetch wrapper
async function apiFetch(url, opts = {}) {
    const res = await fetch(url, opts);
    const data = await res.json();

    // 新格式: {ok, data, error}
    if ('ok' in data) {
        if (!data.ok) throw { code: data.error?.code, message: data.error?.message };
        return data.data;
    }

    // 旧格式兼容: {error: "..."} 或 {success: false}
    if (data.error && typeof data.error === 'string') {
        throw { code: 'LEGACY_ERROR', message: data.error };
    }
    if (data.success === false) {
        throw { code: 'LEGACY_ERROR', message: data.error || 'Unknown error' };
    }

    return data;  // 旧格式成功响应直接返回
}
```

#### analytics.py 迁移示例

```python
# Before (12处相同模式)
except Exception as e:
    return {"error": str(e), "history": []}

# After
from app.web_dashboard.api_response import api_ok, api_error, err_broker_unavailable

# 正常路径
return api_ok({"history": history, "total_return": ...})

# 异常路径
except FutuNotConnected:
    return err_broker_unavailable()
except Exception as e:
    logger.exception("equity_history failed")
    return api_error("ANALYTICS_ERROR", str(e), retryable=True,
                     data={"history": [], "total_return": 0})
```

#### 迁移清单

| 文件 | 处数 | 风险 |
|------|------|------|
| analytics.py | 12 | 低 — 都是 try/except 兜底 |
| battle_plan.py | 10 | 中 — 有 JSONResponse 直接返回 |
| risk.py | 3 | 低 |
| dashboard.py | 2 | 低 |
| **总计** | **27** | |

---

## 实施优先级

| Phase | 影响 | 复杂度 | 建议 |
|-------|------|--------|------|
| **1. WS推流** | 🔴 高 — 消除90%无效请求 | 中 — 4步 | 先做 |
| **2. Nginx缓存** | 🟡 低 — 省带宽 | 低 — 5行nginx | 顺手做 |
| **3. 错误统一** | 🟢 低 — 代码质量 | 低 — 机械替换 | 最后做 |

**建议执行顺序**: Phase 2 (5分钟) → Phase 1 (核心工作) → Phase 3 (扫尾)

---

## 风险与缓解

| 风险 | 缓解 |
|------|------|
| WS 连接不稳定 | 自动降级 polling (已有机制) |
| 推送风暴 (频繁价格变动) | delta 过滤: 价格变>$0.01 才推 |
| 前端兼容 | `apiFetch()` wrapper 兼容新旧格式 |
| Nginx 缓存过期不刷新 | 7天TTL + ETag (非 immutable) |
