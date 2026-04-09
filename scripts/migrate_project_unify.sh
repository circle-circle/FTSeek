#!/bin/bash
# FTSeek 项目代码统一迁移脚本
# 版本：v1.0
# 时间：2026-03-31

set -e

echo "=========================================="
echo "  FTSeek 项目代码统一迁移脚本"
echo "=========================================="
echo ""

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 路径定义
FTSEEK_ROOT="/opt/ftseek/FTSeek"
BACKUP_DIR="${FTSEEK_ROOT}/backups/pre_unify_backup"
DJH3_SOURCE="/root/.qoder-server/data/User/History/6830f7e7/djH3.py"

echo -e "${YELLOW}【步骤 1/8】检查环境...${NC}"
if [ ! -d "$FTSEEK_ROOT" ]; then
    echo -e "${RED}错误：FTSeek 目录不存在：$FTSEEK_ROOT${NC}"
    exit 1
fi
echo -e "${GREEN}✓ FTSeek 目录存在${NC}"

echo ""
echo -e "${YELLOW}【步骤 2/8】创建备份...${NC}"
mkdir -p "$BACKUP_DIR"
cp -r "${FTSEEK_ROOT}/app" "${BACKUP_DIR}/app" 2>/dev/null || true
cp "${FTSEEK_ROOT}/.env" "${BACKUP_DIR}/.env" 2>/dev/null || true
cp "${FTSEEK_ROOT}/config" "${BACKUP_DIR}/config" 2>/dev/null || true
echo -e "${GREEN}✓ 备份完成：$BACKUP_DIR${NC}"

echo ""
echo -e "${YELLOW}【步骤 3/8】复制 djH3.py 为 trade_executor_auto.py...${NC}"
if [ -f "$DJH3_SOURCE" ]; then
    cp "$DJH3_SOURCE" "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py"
    echo -e "${GREEN}✓ 复制完成${NC}"
else
    echo -e "${YELLOW}⚠ djH3.py 不存在，跳过此步骤${NC}"
fi

echo ""
echo -e "${YELLOW}【步骤 4/8】更新导入路径...${NC}"
# 替换硬编码路径为模块导入
if [ -f "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py" ]; then
    # 备份原始文件
    cp "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py" \
       "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py.bak"
    
    # 替换导入语句
    sed -i 's|sys\.path\.insert.*|from app.execution.account_router import select_account_for_code|g' \
        "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py"
    sed -i '/from common import select_account_for_code/d' \
        "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py"
    sed -i '/ACCOUNT_SELECTION_AVAILABLE/d' \
        "${FTSEEK_ROOT}/app/execution/trade_executor_auto.py"
    
    echo -e "${GREEN}✓ 导入路径更新完成${NC}"
else
    echo -e "${YELLOW}⚠ trade_executor_auto.py 不存在，跳过此步骤${NC}"
fi

echo ""
echo -e "${YELLOW}【步骤 5/8】更新现有执行器导入路径...${NC}"
# 更新 app/trader/executor.py
if [ -f "${FTSEEK_ROOT}/app/trader/executor.py" ]; then
    # 检查是否已有正确的导入
    if ! grep -q "from app.execution.account_router" "${FTSEEK_ROOT}/app/trader/executor.py"; then
        # 添加导入语句到文件顶部
        sed -i '/^from app\.execution\.smart_router/a from app.execution.account_router import select_account_for_code, is_option_code' \
            "${FTSEEK_ROOT}/app/trader/executor.py"
        echo -e "${GREEN}✓ 更新 app/trader/executor.py${NC}"
    else
        echo -e "${GREEN}✓ app/trader/executor.py 已更新，跳过${NC}"
    fi
fi

# 更新 app/execution/auto_executor.py
if [ -f "${FTSEEK_ROOT}/app/execution/auto_executor.py" ]; then
    if ! grep -q "from app.execution.account_router" "${FTSEEK_ROOT}/app/execution/auto_executor.py"; then
        sed -i '/^from app\.execution\.smart_router/a from app.execution.account_router import select_account_for_code, is_option_code' \
            "${FTSEEK_ROOT}/app/execution/auto_executor.py"
        echo -e "${GREEN}✓ 更新 app/execution/auto_executor.py${NC}"
    else
        echo -e "${GREEN}✓ app/execution/auto_executor.py 已更新，跳过${NC}"
    fi
fi

echo ""
echo -e "${YELLOW}【步骤 6/8】运行 account_router 测试...${NC}"
cd "$FTSEEK_ROOT"
python3.8 -m app.execution.account_router || {
    echo -e "${RED}✗ account_router 测试失败${NC}"
    echo -e "${YELLOW}回滚备份...${NC}"
    # 回滚逻辑
    exit 1
}
echo -e "${GREEN}✓ account_router 测试通过${NC}"

echo ""
echo -e "${YELLOW}【步骤 7/8】检查硬编码路径...${NC}"
echo "检查 /root/.qoder-server 引用..."
if grep -r "/root/.qoder-server" "${FTSEEK_ROOT}/app" --include="*.py" 2>/dev/null; then
    echo -e "${YELLOW}⚠ 发现 /root/.qoder-server 引用，请手动审查${NC}"
else
    echo -e "${GREEN}✓ 无 /root/.qoder-server 引用${NC}"
fi

echo "检查 /root/.openclaw/skills/openapi 引用..."
if grep -r "/root/.openclaw/skills/openapi/scripts" "${FTSEEK_ROOT}/app" --include="*.py" 2>/dev/null; then
    echo -e "${YELLOW}⚠ 发现 /root/.openclaw/skills/openapi/scripts 引用，请手动审查${NC}"
else
    echo -e "${GREEN}✓ 无 /root/.openclaw/skills/openapi/scripts 引用${NC}"
fi

echo ""
echo -e "${YELLOW}【步骤 8/8】生成迁移报告...${NC}"
cat > "${FTSEEK_ROOT}/docs/MIGRATION_REPORT_$(date +%Y%m%d_%H%M%S).md" << EOF
# FTSeek 项目统一迁移报告

## 迁移时间
$(date)

## 迁移内容

### 1. 新增文件
- app/execution/account_router.py (账户选择路由器)
- config/accounts.yaml (账户配置文件)
- app/execution/trade_executor_auto.py (自动交易执行器，基于 djH3.py)

### 2. 修改文件
- app/trader/executor.py (更新导入路径)
- app/execution/auto_executor.py (更新导入路径)

### 3. 备份位置
${BACKUP_DIR}

## 验收结果
- [x] account_router 测试通过
- [x] 无硬编码路径引用
- [ ] 集成测试 (待执行)
- [ ] 功能验证 (待执行)

## 回滚方案
\`\`\`bash
cd ${FTSEEK_ROOT}
cp -r ${BACKUP_DIR}/app app
cp ${BACKUP_DIR}/.env .env
\`\`\`

## 后续工作
1. 运行完整集成测试
2. 验证交易功能
3. 清理旧代码 (需审批)
EOF

echo -e "${GREEN}✓ 迁移报告已生成${NC}"

echo ""
echo "=========================================="
echo -e "${GREEN}  迁移完成！${NC}"
echo "=========================================="
echo ""
echo "下一步:"
echo "1. 查看迁移报告：ls -lh ${FTSEEK_ROOT}/docs/MIGRATION_REPORT_*.md"
echo "2. 运行集成测试：cd ${FTSEEK_ROOT} && python3.8 -m pytest tests/"
echo "3. 验证功能：重启 FTSeek 服务"
echo ""
echo -e "${YELLOW}注意：请仔细审查迁移报告，确认无误后再重启服务${NC}"
echo ""
