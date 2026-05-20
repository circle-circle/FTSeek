#!/bin/bash
# FTSeek-US 安装脚本

set -e

FTSEEK_US_DIR="/opt/ftseek/FTSeek-US"
PYTHON="/usr/bin/python3.8"

echo "=========================================="
echo "FTSeek-US 安装脚本"
echo "=========================================="

# 1. 检查 Python
echo "[1/5] 检查 Python..."
if ! command -v $PYTHON &> /dev/null; then
    echo "❌ Python 3.8 未安装"
    exit 1
fi
echo "✅ Python 版本：$($PYTHON --version)"

# 2. 检查富途 SDK
echo "[2/5] 检查富途 SDK..."
if $PYTHON -c "import futu" 2>/dev/null; then
    echo "✅ 富途 SDK 已安装"
else
    echo "⚠️ 富途 SDK 未安装，尝试安装..."
    $PYTHON -m pip install futu-api -q
    if $PYTHON -c "import futu" 2>/dev/null; then
        echo "✅ 富途 SDK 安装成功"
    else
        echo "❌ 富途 SDK 安装失败，请手动安装：pip3.8 install futu-api"
    fi
fi

# 3. 检查依赖
echo "[3/5] 检查依赖..."
$PYTHON -m pip install python-dotenv pyyaml -q
echo "✅ 依赖已安装"

# 4. 创建目录
echo "[4/5] 创建目录..."
mkdir -p $FTSEEK_US_DIR/{data,logs}
echo "✅ 目录已创建"

# 5. 配置环境变量
echo "[5/5] 配置环境变量..."
if [ ! -f "$FTSEEK_US_DIR/.env" ]; then
    cp $FTSEEK_US_DIR/.env.example $FTSEEK_US_DIR/.env
    echo "✅ .env 已创建 (请编辑填写配置)"
else
    echo "✅ .env 已存在"
fi

# 6. 配置 systemd (可选)
echo ""
echo "是否配置 systemd 服务？(y/n)"
read -r response
if [[ "$response" =~ ^[Yy]$ ]]; then
    systemctl daemon-reload
    systemctl enable ftseek-us
    echo "✅ systemd 服务已配置"
    echo "   启动命令：systemctl start ftseek-us"
    echo "   状态命令：systemctl status ftseek-us"
fi

# 完成
echo ""
echo "=========================================="
echo "✅ 安装完成!"
echo "=========================================="
echo ""
echo "下一步:"
echo "1. 编辑配置：vi $FTSEEK_US_DIR/.env"
echo "2. 测试连接：cd $FTSEEK_US_DIR && ./ftseek_us.sh test"
echo "3. 启动服务：./ftseek_us.sh start"
echo ""
echo "文档：$FTSEEK_US_DIR/README.md"
echo ""
