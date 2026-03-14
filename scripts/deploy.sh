#!/bin/bash
# FTSeek v2.1 部署脚本

set -e

echo "========================================"
echo "  FTSeek v2.1 部署脚本"
echo "========================================"
echo ""

cd /opt/ftseek/FTSeek

# 1. 检查 Python
echo "📌 检查 Python..."
python3 --version

# 2. 生成 API Token
echo ""
echo "🔐 生成 API Token..."
if [ -z "$API_TOKEN" ]; then
    export API_TOKEN=$(python3 -c 'import secrets; print(secrets.token_hex(32))')
    echo "   已生成临时 Token: ${API_TOKEN:0:16}..."
else
    echo "   使用已有 Token"
fi

# 3. 开发模式
export FTSEEK_DEV_MODE=1
echo "   开发模式：已启用"

# 4. 初始化数据库
echo ""
echo "🗄️  初始化数据库..."
python3 -c "
from app.storage.database import init_database
db = init_database()
print(f'   ✅ 数据库：{db.db_path}')
"

# 5. 验证模块加载
echo ""
echo "📦 验证模块加载..."
python3 -c "
from app.signal.fusion_engine import get_fusion_engine
engine = get_fusion_engine()
modules = ['technical', 'sentiment', 'fusion', 'output']
for m in modules:
    mod = engine.get_module(m)
    print(f'   ✅ {m}: {type(mod).__name__}')
"

# 6. 验证策略注册
echo ""
echo "🧩 验证策略注册..."
python3 -c "
from app.strategy import STRATEGY_REGISTRY
print(f'   已注册策略：{len(STRATEGY_REGISTRY)} 个')
if 'fusion_v2' in STRATEGY_REGISTRY:
    print('   ✅ fusion_v2 已注册')
else:
    print('   ⚠️  fusion_v2 未注册')
"

# 7. 检查钉钉配置
echo ""
echo "🔔 检查钉钉配置..."
if [ -n "$DINGTALK_WEBHOOK_URL" ]; then
    echo "   ✅ Webhook URL: 已配置"
    echo "   ✅ Secret: $([ -n \"$DINGTALK_SECRET\" ] && echo '已配置' || echo '未配置')"
else
    echo "   ⚠️  Webhook URL: 未配置"
    echo ""
    echo "   配置方法:"
    echo "   1. 钉钉群 → 群设置 → 智能群助手"
    echo "   2. 添加机器人 → 自定义"
    echo "   3. 复制 Webhook URL 和 Secret"
    echo "   4. 设置环境变量:"
    echo "      export DINGTALK_WEBHOOK_URL='https://...'"
    echo "      export DINGTALK_SECRET='SEC...'"
fi

# 8. 显示配置摘要
echo ""
echo "========================================"
echo "  部署配置摘要"
echo "========================================"
echo "  API Token: ${API_TOKEN:0:16}..."
echo "  开发模式：$FTSEEK_DEV_MODE"
echo "  数据库：/opt/ftseek/FTSeek/data/ftseek.db"
echo "  钉钉推送：$([ -n \"$DINGTALK_WEBHOOK_URL\" ] && echo '已配置' || echo '未配置')"
echo "========================================"
echo ""

# 9. 运行测试
echo "🧪 运行模块化测试..."
python3 test_modular.py 2>&1 | tail -20

echo ""
echo "========================================"
echo "  ✅ 部署完成!"
echo "========================================"
echo ""
echo "下一步:"
echo "  1. 配置钉钉机器人 (如需要推送)"
echo "  2. 编辑 config/watchlist.json 添加监控股票"
echo "  3. 运行 ./run.sh 启动系统"
echo ""
echo "测试钉钉推送:"
echo "  export DINGTALK_WEBHOOK_URL='https://...'"
echo "  export DINGTALK_SECRET='SEC...'"
echo "  python3 test_dingtalk.py"
echo ""
