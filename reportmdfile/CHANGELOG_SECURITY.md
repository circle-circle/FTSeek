# 🔐 FTSeek 安全加固更新日志

**日期**: 2026-02-26  
**执行**: 阿呆 (AI 助手)  
**级别**: 关键安全更新

---

## 修复的安全问题

### 1. API Token 硬编码默认值 🔴 高危

**问题**: `config.py` 中 API Token 有默认值 `ftseek_secret_token_change_me`，生产环境容易忘记修改导致未授权访问。

**修复**:
- ✅ 移除默认值，启动时强制检查环境变量 `API_TOKEN`
- ✅ 未设置时抛出 `RuntimeError` 阻止启动
- ✅ 开发模式可通过 `FTSEEK_DEV_MODE=true` 生成临时 Token
- ✅ 提供清晰的错误提示和修复指引

**文件**: `app/config.py`

---

### 2. 钉钉 Secret 明文硬编码 🟡 中危

**问题**: Webhook URL 和 Secret 直接写在代码中，版本控制会泄露。

**修复**:
- ✅ 改为环境变量加载
- ✅ 提供 `.env.example` 模板
- ✅ systemd 服务文件配置示例

**文件**: `app/config.py`, `.env.example`, `scripts/ftseek.service`

---

### 3. 健康检查端点无鉴权 🟡 中危

**问题**: `/health` 接口无需鉴权，可被用于探测服务状态。

**修复**:
- ✅ 支持可选 Token 鉴权
- ✅ 提供 token 时进行校验

**文件**: `app/server.py`

---

### 4. 依赖版本未锁定 🟢 低危

**问题**: `requirements.txt` 无版本锁定，可能导致环境不一致。

**修复**:
- ✅ 创建 `requirements.lock` 锁定生产版本
- ✅ 更新 `requirements.txt` 添加最低版本要求

**文件**: `requirements.txt`, `requirements.lock`

---

### 5. 无安全配置文档

**问题**: 用户不知道如何安全配置。

**修复**:
- ✅ 创建 `SECURITY.md` 安全配置指南
- ✅ 创建 `security_check.sh` 安全检查脚本
- ✅ 创建 `setup_security.sh` 一键配置脚本

**文件**: `SECURITY.md`, `scripts/security_check.sh`, `scripts/setup_security.sh`

---

## 新增文件

| 文件 | 用途 |
|------|------|
| `.env.example` | 环境变量配置模板 |
| `SECURITY.md` | 安全配置指南 |
| `requirements.lock` | 依赖版本锁定 |
| `scripts/security_check.sh` | 安全检查脚本 |
| `scripts/setup_security.sh` | 一键配置脚本 |
| `CHANGELOG_SECURITY.md` | 本文档 |

---

## 修改的文件

| 文件 | 修改内容 |
|------|----------|
| `app/config.py` | 强制 API Token 环境变量，移除默认值 |
| `app/server.py` | 健康检查端点支持鉴权 |
| `requirements.txt` | 更新版本要求 |
| `scripts/ftseek.service` | 配置示例更新 |

---

## 配置检查清单

部署前请确认：

- [ ] API Token 已生成并配置（至少 32 字符）
- [ ] 钉钉 Webhook URL 已配置
- [ ] 钉钉 Secret 已配置
- [ ] 交易环境确认为模拟环境（`TRADE_ENV=1`）
- [ ] `.env` 文件权限设置为 `600`
- [ ] 运行 `scripts/security_check.sh` 无 FAIL 项

---

## 升级步骤

### 方式 A: 使用一键脚本（推荐）

```bash
cd /opt/ftseek/FTSeek
sudo ./scripts/setup_security.sh
```

### 方式 B: 手动配置

1. **生成 API Token**:
   ```bash
   python3 -c "import secrets; print(secrets.token_hex(32))"
   ```

2. **编辑服务文件**:
   ```bash
   sudo nano /opt/ftseek/FTSeek/scripts/ftseek.service
   ```
   填入你的 Token 和钉钉配置。

3. **重新加载并重启**:
   ```bash
   sudo cp /opt/ftseek/FTSeek/scripts/ftseek.service /etc/systemd/system/
   sudo systemctl daemon-reload
   sudo systemctl restart ftseek
   ```

4. **验证配置**:
   ```bash
   sudo ./scripts/security_check.sh
   ```

---

## 后续建议

### 短期（1 周内）

- [ ] 配置防火墙限制 API 端口访问
- [ ] 检查日志是否有异常访问记录
- [ ] 备份 `.env` 文件到安全位置

### 中期（1 个月内）

- [ ] 配置 HTTPS 反向代理
- [ ] 设置日志轮转和归档
- [ ] 添加 Prometheus 监控指标

### 长期

- [ ] 实现信号持久化（SQLite）
- [ ] 添加交易审计日志
- [ ] 实现策略回测框架

---

## 联系支持

发现安全漏洞请报告给项目负责人。

**安全原则**: 零信任，最小权限，纵深防御。
