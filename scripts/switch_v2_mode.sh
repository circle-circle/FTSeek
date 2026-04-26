#!/bin/bash
# 切换到 active 模式

MODE=${1:-active}
echo "切换到 $MODE 模式..."

# 更新 service 文件
sed -i "s/--mode observe/--mode $MODE/g" /etc/systemd/system/ftseek-news-v2.service
sed -i "s/(混合模式-观察中)/(混合模式-$MODE)/g" /etc/systemd/system/ftseek-news-v2.service

systemctl daemon-reload
systemctl restart ftseek-news-v2

echo "✅ 已切换到 $MODE 模式"
systemctl status ftseek-news-v2 --no-pager | head -8
