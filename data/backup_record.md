# FTSeek v2.0 备份记录

## 备份时间
2026-03-01 10:46

## 备份内容

### 1. 代码备份
- **路径**: `/opt/ftseek/FTSeek.v2.backup.20260301_104621`
- **大小**: 7.4M
- **Python 文件数**: 83 个

### 2. 数据库备份
- **路径**: `/opt/ftseek/FTSeek.v2.backup.db.20260301_104622`
- **大小**: 116K

### 3. 配置文件备份
- **路径**: `/opt/ftseek/FTSeek.v2.backup.config`
- **大小**: 16K
- **包含文件**:
  - backtest_presets.json
  - system_config.yaml
  - watchlist.json

## 验证结果
- ✅ 代码备份完整 (83 个 .py 文件，与源一致)
- ✅ 数据库备份完整 (116K)
- ✅ 配置文件备份完整 (3 个文件)
- ✅ 备份大小合理

## 回滚说明
如需回滚到 v2.0:
```bash
# 停止当前服务
# 恢复代码
cp -r /opt/ftseek/FTSeek.v2.backup.20260301_104621 /opt/ftseek/FTSeek
# 恢复数据库
cp /opt/ftseek/FTSeek.v2.backup.db.20260301_104622 /opt/ftseek/FTSeek/data/ftseek.db
# 恢复配置
cp /opt/ftseek/FTSeek.v2.backup.config/* /opt/ftseek/FTSeek/config/
```
