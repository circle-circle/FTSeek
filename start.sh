#!/bin/bash
# FTSeek-US 启动脚本
# Task #21 增强：OpenD 连通性、venv、配置完整性快速校验

set -e

PROJECT_DIR="/opt/ftseek/FTSeek-US"
VENV_DIR="$PROJECT_DIR/venv"
PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"
OPEND_HOST="${OPEND_HOST:-127.0.0.1}"
OPEND_PORT="${OPEND_PORT:-11111}"

echo "========================================"
echo "  FTSeek-US 美股交易平台"
echo "========================================"

cd "$PROJECT_DIR"

# ── 1. OpenD 连通性检查 ──
# FTSeek 项目使用 OpenD 外部管理模式（OPEND_MANAGED=external）
# 启动前必须确保 OpenD 已就绪，否则 main.py 会进入无限重试
if command -v nc >/dev/null 2>&1; then
    if ! nc -z "$OPEND_HOST" "$OPEND_PORT" 2>/dev/null; then
        echo "❌ [ERROR] OpenD 不可达：${OPEND_HOST}:${OPEND_PORT}"
        echo "   请先启动 OpenD: /opt/ftseek/start_opend.sh"
        echo "   或检查端口: ss -tlnp | grep ${OPEND_PORT}"
        exit 1
    fi
    echo "✅ OpenD 连通性 OK (${OPEND_HOST}:${OPEND_PORT})"
else
    # 退化方案：bash /dev/tcp 探测
    if ! (exec 3<>/dev/tcp/${OPEND_HOST}/${OPEND_PORT}) 2>/dev/null; then
        echo "❌ [ERROR] OpenD 不可达：${OPEND_HOST}:${OPEND_PORT}"
        echo "   请先启动 OpenD: /opt/ftseek/start_opend.sh"
        exit 1
    fi
    exec 3<&- 3>&- 2>/dev/null || true
    echo "✅ OpenD 连通性 OK (${OPEND_HOST}:${OPEND_PORT}, via /dev/tcp)"
fi

# ── 2. 虚拟环境检查 ──
if [ ! -d "$VENV_DIR" ]; then
    echo "创建虚拟环境..."
    python3.11 -m venv "$VENV_DIR"
fi

if [ ! -x "$PYTHON" ]; then
    echo "❌ venv python 不存在: $PYTHON"
    echo "   请重建 venv: python3 -m venv $VENV_DIR && $PIP install -r requirements.lock"
    exit 1
fi

# 激活虚拟环境
# shellcheck source=/dev/null
source "$VENV_DIR/bin/activate"

# 确认激活生效
if [ "$(command -v python)" != "$PYTHON" ]; then
    echo "⚠️  venv 激活异常，期望 $PYTHON, 实际 $(command -v python)"
    exit 1
fi
echo "✅ Python venv: $($PYTHON --version 2>&1) @ $VENV_DIR"

# ── 3. 依赖检查（唯一 manifest = lock）──
if [ ! -f "requirements.lock" ]; then
    echo "❌ 未找到 requirements.lock"
    exit 1
fi
echo "检查依赖（lock 模式）..."
# C2 (2026-08-24, 体检 E1): lock 失败即终止, 不回退松散 manifest。
if ! $PIP install -q -r requirements.lock; then
    echo "❌ lock 依赖安装失败 — 环境不完整, 终止启动"
    exit 1
fi

# ── 4. 配置文件完整性快速校验 ──
if [ ! -f ".env.us" ]; then
    echo "❌ 未找到 .env.us 配置文件"
    echo "   请复制 .env.us.example 并配置:"
    echo "   cp .env.us.example .env.us && vim .env.us"
    exit 1
fi

# 必要环境变量校验（仅检查 key 是否存在且非空）
MISSING_KEYS=()
for key in FTSEEK_US_API_TOKEN; do
    val=$(grep -E "^${key}=" .env.us | head -1 | cut -d'=' -f2- | tr -d '"' | tr -d "'" | xargs)
    if [ -z "$val" ]; then
        MISSING_KEYS+=("$key")
    fi
done

if [ ${#MISSING_KEYS[@]} -gt 0 ]; then
    echo "❌ .env.us 缺失或未设置: ${MISSING_KEYS[*]}"
    exit 1
fi

# unified_config.yaml 完整性校验（若存在 config_validate.py 则调用）
if [ -f "config/unified_config.yaml" ] && [ -f "scripts/config_validate.py" ]; then
    if ! $PYTHON scripts/config_validate.py --quick >/dev/null 2>&1; then
        echo "⚠️  unified_config.yaml 校验未通过（非阻断），请稍后核查"
    else
        echo "✅ unified_config.yaml 校验通过"
    fi
fi

echo "✅ 配置完整性 OK"

# ── 5. 创建必要目录 ──
mkdir -p logs data

# ── 6. 启动服务 ──
echo "启动 FTSeek-US 服务..."
echo "API: http://localhost:8901"
echo "日志：logs/ftseek_us.log"
echo ""

exec $PYTHON main.py --start 2>&1
