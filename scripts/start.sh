#!/bin/bash
# ============================================================
#  FTSeek 一键启动脚本
#  用法:
#    ./start.sh           # 启动 OpenD (会提示输入验证码)
#    ./start.sh --api     # 仅启动 API 服务 (不启动 OpenD)
#    ./start.sh --code 123456  # 启动 OpenD 并自动输入验证码
# ============================================================

set -e
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OPEND_DIR="/opt/ftseek/Futu_OpenD_9.6.5618_Centos7/Futu_OpenD_9.6.5618_Centos7"
FTSEEK_DIR="/opt/ftseek/FTSeek"
PYTHON=python3.8

GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}  FTSeek - 富途自动化交易系统${NC}"
echo -e "${GREEN}========================================${NC}"

# ---------- 仅 API 模式 ----------
if [ "$1" = "--api" ]; then
    echo -e "${YELLOW}[仅API模式] 启动 FastAPI 服务...${NC}"
    cd "$FTSEEK_DIR"
    $PYTHON -m app.main
    exit 0
fi

# ---------- 停掉旧进程 ----------
echo -e "${YELLOW}停止旧进程...${NC}"
pkill -9 -f FutuOpenD 2>/dev/null || true
sleep 1

# ---------- 启动 OpenD ----------
echo -e "${YELLOW}启动 OpenD...${NC}"
cd "$OPEND_DIR"

if [ "$1" = "--code" ] && [ -n "$2" ]; then
    # 自动输入验证码模式
    CODE=$2
    echo -e "${GREEN}将使用验证码: $CODE${NC}"
    expect -c "
        set timeout 120
        spawn ./FutuOpenD
        expect {
            -re {input_phone_verify_code.*code=} {
                sleep 1
                send \"input_phone_verify_code -code=$CODE\r\"
                puts \"\n>>> 验证码已发送: $CODE\"
            }
            timeout { puts \"超时\"; exit 1 }
        }
        set timeout 60
        expect {
            -re {Ready|login_succ} { puts \"\n>>> 登录成功!\" }
            -re {NeedPhoneVerifyCode} { puts \"\n>>> 验证码错误或过期\"; exit 1 }
            timeout { puts \"登录超时\" }
        }
        set timeout -1
        expect eof
    " &
    OPEND_PID=$!
else
    # 交互模式: 直接在终端运行，手动输入验证码
    echo ""
    echo -e "${YELLOW}OpenD 启动后会要求输入手机验证码 (发到 186****2990)${NC}"
    echo -e "${YELLOW}请在看到提示后输入:${NC}"
    echo -e "${GREEN}  input_phone_verify_code -code=你的验证码${NC}"
    echo ""
    echo -e "${YELLOW}等待 OpenD 登录成功后，按 Ctrl+C 退出前台，然后运行:${NC}"
    echo -e "${GREEN}  systemctl start ftseek${NC}"
    echo ""
    ./FutuOpenD
    exit 0
fi

# 等待 OpenD 就绪
echo -e "${YELLOW}等待 OpenD 就绪...${NC}"
for i in $(seq 1 30); do
    if ss -tlnp | grep -q ':11111 '; then
        echo -e "${GREEN}OpenD 已就绪! (端口 11111 已监听)${NC}"
        break
    fi
    sleep 2
done

if ! ss -tlnp | grep -q ':11111 '; then
    echo -e "${RED}OpenD 端口 11111 未就绪，请检查日志${NC}"
    echo -e "${YELLOW}继续启动 FTSeek API 服务...${NC}"
fi

# ---------- 启动 FTSeek ----------
echo -e "${YELLOW}启动 FTSeek 服务...${NC}"
cd "$FTSEEK_DIR"
$PYTHON -m app.main
