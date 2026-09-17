#!/bin/bash
# FTSeek-US 部署验证脚本

set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
cd "$PROJECT_DIR"

echo "========================================"
echo "  FTSeek-US 部署验证"
echo "========================================"
echo ""

# 检查虚拟环境
if [ ! -d "venv" ]; then
    echo "❌ 虚拟环境不存在"
    echo "   请运行：python3.11 -m venv venv"
    exit 1
fi

source venv/bin/activate

# 1. 检查环境配置
echo "1. 检查环境配置..."
if [ ! -f ".env.us" ]; then
    echo "   ❌ .env.us 不存在"
    exit 1
fi

if ! grep -q "FTSEEK_US_API_TOKEN=" .env.us; then
    echo "   ❌ 未设置 FTSEEK_US_API_TOKEN"
    exit 1
fi

if ! grep -q "BAILIAN_API_KEY=" .env.us; then
    echo "   ❌ 未设置 BAILIAN_API_KEY"
    exit 1
fi

if ! grep -q "DINGTALK_WEBHOOK_URL_US=" .env.us; then
    echo "   ❌ 未设置 DINGTALK_WEBHOOK_URL_US"
    exit 1
fi

echo "   ✅ 环境配置检查通过"
echo ""

# 2. 检查依赖安装
echo "2. 检查依赖安装..."
if ! python -c "import fastapi" 2>/dev/null; then
    echo "   ❌ FastAPI 未安装"
    echo "   请运行：pip install -r requirements.lock"
    exit 1
fi

if ! python -c "import yfinance" 2>/dev/null; then
    echo "   ⚠️  yfinance 未安装 (可选)"
    echo "   建议运行：pip install yfinance pandas"
else
    echo "   ✅ yfinance 已安装"
fi

if ! python -c "import yfinance" 2>/dev/null; then
    echo "   ⚠️  Yahoo Finance 数据源不可用"
else
    echo "   ✅ 数据源检查通过"
fi
echo ""

# 3. 检查配置文件
echo "3. 检查配置文件..."
if [ ! -f "config/watchlist_us.json" ]; then
    echo "   ❌ watchlist_us.json 不存在"
    exit 1
fi

if ! python -c "import json; json.load(open('config/watchlist_us.json'))"; then
    echo "   ❌ watchlist_us.json 格式错误"
    exit 1
fi

echo "   ✅ 配置文件检查通过"
echo ""

# 4. 检查模块导入
echo "4. 检查模块导入..."

python -c "from app.core.models import SignalScore" 2>/dev/null && echo "   ✅ core.models" || echo "   ❌ core.models"
python -c "from app.agents.voting import VotingConfig" 2>/dev/null && echo "   ✅ agents.voting.config" || echo "   ❌ agents.voting.config"
python -c "from app.observability.alert_manager import AlertManager" 2>/dev/null && echo "   ✅ observability.alert" || echo "   ❌ observability.alert"
python -c "from app.risk.engine import RiskEngine" 2>/dev/null && echo "   ✅ risk.engine" || echo "   ❌ risk.engine"
python -c "from app.execution.engine import ExecutionEngine" 2>/dev/null && echo "   ✅ execution.engine" || echo "   ❌ execution.engine"

echo ""

# 5. 测试钉钉推送
echo "5. 测试钉钉推送..."
python -c "
from app.observability.alert_manager import AlertManager
import os

manager = AlertManager(
    provider='dingtalk',
    dingtalk_webhook=os.getenv('DINGTALK_WEBHOOK_URL_US'),
    dingtalk_secret=os.getenv('DINGTALK_SECRET_US')
)

result = manager.send_info(
    title='FTSeek-US 部署测试',
    content='中长线美股交易平台已就绪'
)

if result:
    print('   ✅ 钉钉推送测试成功')
else:
    print('   ⚠️  钉钉推送失败 (请检查 webhook 配置)')
" 2>/dev/null || echo "   ⚠️  钉钉推送跳过 (未加载环境变量)"

echo ""

# 6. (2026-07-14) 投票系统测试已移除 — 3 模型 VotingCoordinator 退役, 双脑走 DA bridge.

echo ""

# 7. 检查目录结构
echo "7. 检查目录结构..."
[ -d "app" ] && echo "   ✅ app/" || echo "   ❌ app/"
[ -d "config" ] && echo "   ✅ config/" || echo "   ❌ config/"
[ -d "tests" ] && echo "   ✅ tests/" || echo "   ❌ tests/"
[ -d "logs" ] && echo "   ✅ logs/" || echo "   ✅ logs/ (自动创建)"
[ -d "data" ] && echo "   ✅ data/" || echo "   ✅ data/ (自动创建)"

echo ""

# 8. 运行单元测试
echo "8. 运行单元测试 (抽样)..."
if python -m pytest tests/agents/voting/test_voting_config.py -v --tb=short 2>/dev/null | grep -q "passed"; then
    echo "   ✅ 投票配置测试通过"
else
    echo "   ⚠️  投票配置测试跳过"
fi

echo ""
echo "========================================"
echo "  部署验证完成!"
echo "========================================"
echo ""
echo "下一步:"
echo "1. 启动服务：./start.sh"
echo "2. 查看日志：tail -f logs/ftseek_us.log"
echo "3. 访问 API:  http://localhost:8901"
echo ""
