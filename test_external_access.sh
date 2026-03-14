#!/bin/bash
# FTSeek 外网访问测试脚本

HOST="39.97.58.4"
PORT="8899"
TOKEN="5a8f072c2cba9db1579dd829214a38112fdcddefe089d4ba58e05742097dd272"

echo "=========================================="
echo "  FTSeek 外网访问测试"
echo "=========================================="
echo ""
echo "服务器：$HOST:$PORT"
echo "时间：$(date '+%Y-%m-%d %H:%M:%S')"
echo ""

# 测试 1: 主页访问
echo -n "1️⃣  主页访问测试： "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST:$PORT/")
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ 成功 (HTTP $HTTP_CODE)"
else
    echo "❌ 失败 (HTTP $HTTP_CODE)"
fi

# 测试 2: 专业监控大屏
echo -n "2️⃣  专业监控大屏： "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST:$PORT/static/risk_dashboard_pro_v2.html")
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ 成功 (HTTP $HTTP_CODE)"
else
    echo "❌ 失败 (HTTP $HTTP_CODE)"
fi

# 测试 3: 风险大屏
echo -n "3️⃣  风险大屏访问： "
HTTP_CODE=$(curl -s -o /dev/null -w "%{http_code}" "http://$HOST:$PORT/risk/dashboard")
if [ "$HTTP_CODE" = "200" ]; then
    echo "✅ 成功 (HTTP $HTTP_CODE)"
else
    echo "❌ 失败 (HTTP $HTTP_CODE)"
fi

# 测试 4: API 状态
echo -n "4️⃣  API 状态检查： "
RESPONSE=$(curl -s "http://$HOST:$PORT/api/risk/status?token=$TOKEN")
if echo "$RESPONSE" | grep -q '"status":"ok"'; then
    echo "✅ 成功"
    echo "   详情：$RESPONSE"
else
    echo "❌ 失败"
    echo "   响应：$RESPONSE"
fi

# 测试 5: 专业绩效指标 API
echo -n "5️⃣  绩效指标 API: "
RESPONSE=$(curl -s "http://$HOST:$PORT/api/risk/metrics/performance?days=90&token=$TOKEN")
if echo "$RESPONSE" | grep -q '"sharpe_ratio"'; then
    echo "✅ 成功"
    # 提取关键指标
    SHARPE=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['metrics']['sharpe_ratio'])" 2>/dev/null)
    SORTINO=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['metrics']['sortino_ratio'])" 2>/dev/null)
    MAX_DD=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['metrics']['max_drawdown'])" 2>/dev/null)
    echo "   夏普比率：$SHARPE"
    echo "   索提诺比率：$SORTINO"
    echo "   最大回撤：$MAX_DD"
else
    echo "❌ 失败"
    echo "   响应：${RESPONSE:0:100}..."
fi

# 测试 6: 策略表现 API
echo -n "6️⃣  策略表现 API: "
RESPONSE=$(curl -s "http://$HOST:$PORT/api/risk/strategy/performance?days=90&token=$TOKEN")
if echo "$RESPONSE" | grep -q '"strategies"'; then
    echo "✅ 成功"
    BEST=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['best_strategy'])" 2>/dev/null)
    echo "   最佳策略：$BEST"
else
    echo "❌ 失败"
fi

# 测试 7: 市场状态 API
echo -n "7️⃣  市场状态 API: "
RESPONSE=$(curl -s "http://$HOST:$PORT/api/risk/market/regime?code=HK.00700&token=$TOKEN")
if echo "$RESPONSE" | grep -q '"regime_type"'; then
    echo "✅ 成功"
    REGIME=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['regime_type'])" 2>/dev/null)
    CONF=$(echo "$RESPONSE" | python3.8 -c "import sys,json; d=json.load(sys.stdin); print(d['confidence'])" 2>/dev/null)
    echo "   市场状态：$REGIME"
    echo "   置信度：$CONF"
else
    echo "❌ 失败"
fi

# 测试 8: 完整大屏数据 API
echo -n "8️⃣  大屏数据 API: "
RESPONSE=$(curl -s "http://$HOST:$PORT/api/risk/dashboard?token=$TOKEN")
if echo "$RESPONSE" | grep -q '"dashboard"'; then
    echo "✅ 成功"
    echo "   数据完整"
else
    echo "❌ 失败"
fi

echo ""
echo "=========================================="
echo "  测试总结"
echo "=========================================="
echo ""
echo "✅ 外网访问地址:"
echo "   专业监控大屏：http://$HOST:$PORT/static/risk_dashboard_pro_v2.html"
echo "   原版风险大屏：http://$HOST:$PORT/risk/dashboard"
echo "   系统主页：http://$HOST:$PORT/"
echo ""
echo "📊 核心 API 端点:"
echo "   绩效指标：http://$HOST:$PORT/api/risk/metrics/performance"
echo "   策略表现：http://$HOST:$PORT/api/risk/strategy/performance"
echo "   资金曲线：http://$HOST:$PORT/api/risk/metrics/equity_curve"
echo "   市场状态：http://$HOST:$PORT/api/risk/market/regime"
echo ""
echo "🔑 API Token:"
echo "   $TOKEN"
echo ""
echo "=========================================="
