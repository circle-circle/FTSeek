#!/bin/bash
# Phase 1 快速验证脚本
# 运行方式: bash scripts/verify_phase1.sh

set -e

echo "============================================================"
echo "FTSeek Phase 1 四时段自动化 - 快速验证"
echo "============================================================"

# 切换到项目目录
cd /opt/ftseek/FTSeek

# 颜色定义
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 检查Python环境
echo -e "\n${YELLOW}[1/5] 检查Python环境...${NC}"
python3 --version
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ Python环境正常${NC}"
else
    echo -e "${RED}❌ Python环境异常${NC}"
    exit 1
fi

# 编译检查
echo -e "\n${YELLOW}[2/5] 编译检查...${NC}"
python3 -m py_compile app/ai/llm_market_analyst.py
python3 -m py_compile app/strategy/cross_market_scheduler.py
python3 -m py_compile app/evidence/attribution_engine.py
python3 -m py_compile app/strategy/us_overnight_monitor.py
python3 -m py_compile app/core/session_manager.py
python3 -m py_compile app/review/daily_review.py
echo -e "${GREEN}✅ 所有文件编译通过${NC}"

# 运行集成测试
echo -e "\n${YELLOW}[3/5] 运行集成测试...${NC}"
python3 tests/integration/test_phase1_automation.py
if [ $? -eq 0 ]; then
    echo -e "${GREEN}✅ 集成测试通过${NC}"
else
    echo -e "${RED}❌ 集成测试失败${NC}"
    exit 1
fi

# 检查数据目录
echo -e "\n${YELLOW}[4/5] 检查数据目录...${NC}"
mkdir -p data/pre_market_plans
mkdir -p data/post_market_reviews
mkdir -p data/overnight_predictions
echo -e "${GREEN}✅ 数据目录已创建${NC}"
ls -la data/ | grep -E "pre_market_plans|post_market_reviews|overnight_predictions"

# 检查LLM配置
echo -e "\n${YELLOW}[5/5] 检查LLM配置...${NC}"
if grep -q "llm_market_analyst" config.yaml 2>/dev/null || [ -f config/config.yaml ]; then
    echo -e "${GREEN}✅ LLM配置存在${NC}"
    grep -A 3 "llm_market_analyst" config.yaml 2>/dev/null || grep -A 3 "llm_market_analyst" config/config.yaml 2>/dev/null || echo "请手动检查config.yaml中的llm_market_analyst配置"
else
    echo -e "${YELLOW}⚠️  未找到LLM配置，请确保config.yaml中包含:${NC}"
    echo "ai:"
    echo "  llm_market_analyst:"
    echo "    enabled: true"
    echo "    model: qwen3.5-plus"
fi

echo -e "\n${GREEN}============================================================"
echo "✅ Phase 1 验证完成！"
echo "============================================================${NC}"

echo -e "\n${YELLOW}下一步操作:${NC}"
echo "1. 检查LLM配置是否启用"
echo "2. 配置crontab盘后复盘任务: crontab -e"
echo "3. 添加以下行 (周一至周五 16:30):"
echo "   30 16 * * 1-5 cd /opt/ftseek/FTSeek && python3 -c \"from app.review.daily_review import run_post_market_review; run_post_market_review()\" >> logs/post_market_review.log 2>&1"
echo "4. 重启FTSeek服务以启用新功能"
echo "5. 观察运行日志确认功能正常"

echo -e "\n${YELLOW}文件位置:${NC}"
echo "- 盘前计划: data/pre_market_plans/{date}.json"
echo "- 盘后复盘: data/post_market_reviews/{date}.json"
echo "- 夜间预测: data/overnight_predictions/{date}.json"

echo -e "\n${YELLOW}日志文件:${NC}"
echo "- 主日志: logs/ftseek.log"
echo "- 盘后复盘: logs/post_market_review.log (待crontab配置)"

echo ""
