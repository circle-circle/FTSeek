# FTSeek Harness 驾驭工程架构

> 从被动响应到主动自治：5层自主优化系统

## 版本历史
- v1.0 (2026-04-14): 初始架构设计

---

## 1. 架构概览

```
┌─────────────────────────────────────────────────────────────────────┐
│                    HARNESS 驾驭工程 5层自治系统                      │
├─────────────────────────────────────────────────────────────────────┤
│ Layer 5: GOVERNANCE  治理层  - 规则、审计、回滚、技能进化            │
│ Layer 4: DECIDE      决策层  - 分级、方案选择、安全边界             │
│ Layer 3: DIAGNOSE    诊断层  - 根因分析、多Agent并行、影响链        │
│ Layer 2: OBSERVE     感知层  - 定时巡检、事件驱动、趋势分析         │
│ Layer 1: EXECUTE     执行层  - 自动修复、建议生成、验证闭环         │
└─────────────────────────────────────────────────────────────────────┘
```

### 设计原则

1. **渐进式授权**: 从只读分析 → 建议生成 → 安全自动修复 → 复杂自主决策
2. **安全边界**: 所有自动修复必须可验证、可回滚、影响范围可控
3. **技能驱动**: 诊断规则和修复方案来自已有技能知识库，不是硬编码
4. **人在回路**: P0自动执行，P1建议确认，P2仅告警，P3周报汇总
5. **持续进化**: 每次诊断/修复结果反馈回技能库，系统越来越聪明

---

## 2. 五层架构详细设计

### Layer 2: OBSERVE 感知层

#### 2.1 感知源矩阵

| 感知源 | 触发方式 | 频率 | 输出 | 状态 |
|--------|---------|------|------|------|
| Cron定时巡检 | crontab | 4种定时任务 | JSON审计报告 | ✅ 已实现 |
| Webhook事件 | HTTP POST | 实时 | 事件 payload | ⏳ 待启用 |
| 日志流监控 | tail -f | 实时 | 模式匹配告警 | ⏳ 待实现 |
| 指标趋势分析 | 历史对比 | 每次巡检 | 变化率报告 | ⏳ 部分实现 |

#### 2.2 感知规则引擎

```yaml
observe_rules:
  # 规则1: 策略性能退化
  - name: strategy_performance_degradation
    pattern: "策略计算.*(\d+)ms"
    threshold: "avg > 5000ms"
    severity: P1
    action: trigger_diagnosis
    
  # 规则2: OpenD连接不稳定  
  - name: opend_instability
    pattern: "触发重连|on_disconnect"
    threshold: "> 10次/小时"
    severity: P1
    action: trigger_diagnosis
    
  # 规则3: 配置漂移
  - name: config_drift
    check: config_drift_detector
    threshold: "任何冲突"
    severity: P0
    action: auto_fix
    
  # 规则4: API限流
  - name: api_rate_limit
    pattern: "限流拒绝"
    threshold: "> 20次/天"
    severity: P2
    action: monitor_trend
    
  # 规则5: 数据库锁
  - name: database_locked
    pattern: "database is locked"
    threshold: "出现即触发"
    severity: P0
    action: auto_fix
    
  # 规则6: Pipeline失败
  - name: pipeline_failure
    pattern: "Pipeline 执行异常|TypeError"
    threshold: "出现即触发"
    severity: P0
    action: auto_fix
```

#### 2.3 趋势分析引擎

不只是检测绝对值，而是检测变化率：

```python
def detect_trend(metric_name: str, current: float, history: list) -> str:
    """检测指标趋势"""
    if len(history) < 3:
        return "INSUFFICIENT_DATA"
    
    recent_avg = sum(history[-3:]) / 3
    old_avg = sum(history[:3]) / 3
    
    change_rate = (recent_avg - old_avg) / max(old_avg, 1)
    
    if change_rate > 0.5:
        return "RAPID_INCREASING"  # 快速恶化
    elif change_rate > 0.2:
        return "INCREASING"        # 逐渐恶化
    elif change_rate < -0.5:
        return "RAPID_DECREASING"  # 快速改善
    elif change_rate < -0.2:
        return "DECREASING"        # 逐渐改善
    else:
        return "STABLE"            # 稳定
```

---

### Layer 3: DIAGNOSE 诊断层

#### 3.1 多Agent并行诊断架构

当感知层触发 diagnosis 时，启动并行诊断：

```
感知层触发 → 诊断引擎
              ├── Agent A: 日志深度分析
              │   ├── 错误模式归类
              │   ├── 时间线重建
              │   └── 关联事件发现
              │
              ├── Agent B: 代码根因定位
              │   ├── 调用链追踪
              │   ├── 性能瓶颈分析
              │   └── 依赖关系图
              │
              ├── Agent C: 配置一致性检查
              │   ├── 三向对比 (systemd/.env/yaml)
              │   ├── 运行时值验证
              │   └── 历史变更追踪
              │
              └── Agent D: 影响范围评估
                  ├── 受影响模块列表
                  ├── 用户影响评估
                  └── 修复优先级建议
                  
诊断引擎汇总 → 综合诊断报告
```

#### 3.2 诊断技能知识库

将现有技能转化为可执行的诊断规则：

```python
DIAGNOSIS_SKILLS = {
    "confidence_death_spiral": {
        "skill": "ftseek-pipeline-debugging",
        "symptoms": ["duplicate_signal", "confidence < threshold", "circuit_breaker"],
        "diagnosis_steps": [
            "检查4层阈值是否对齐",
            "检查duplicate penalty是否过重",
            "检查MIN_AGREE_VOTES设置"
        ],
        "expected_root_cause": "阈值不一致导致信号被逐层过滤"
    },
    
    "zero_trade_scenario": {
        "skill": "ftseek-trading-pipeline-debug",
        "symptoms": ["signals generated", "no execution", "拒绝日志"],
        "diagnosis_steps": [
            "追踪信号从生成到执行的完整路径",
            "检查每个过滤层的拒绝原因",
            "验证watchlist一致性"
        ],
        "expected_root_cause": "置信度阈值过高或watchlist不一致"
    },
    
    "opend_instability": {
        "symptoms": ["频繁重连", "239秒无行情", "心跳超时"],
        "diagnosis_steps": [
            "检查OpenD进程状态",
            "分析网络延迟模式",
            "检查订阅槽位使用率"
        ],
        "expected_root_cause": "订阅过多或网络不稳定"
    }
}
```

#### 3.3 根因定位算法

```python
def locate_root_cause(symptoms: list, history: dict) -> DiagnosisResult:
    """根因定位"""
    # 1. 模式匹配
    matched_rules = []
    for rule_name, rule in DIAGNOSIS_SKILLS.items():
        if any(symptom in rule["symptoms"] for symptom in symptoms):
            matched_rules.append(rule_name)
    
    # 2. 时间线分析
    timeline = build_timeline(history)
    first_occurrence = find_first_occurrence(timeline, symptoms)
    
    # 3. 影响链追踪
    impact_chain = trace_impact(first_occurrence, history)
    
    # 4. 根因假设生成
    root_cause_hypotheses = generate_hypotheses(
        matched_rules, timeline, impact_chain
    )
    
    return DiagnosisResult(
        root_causes=root_cause_hypotheses,
        confidence=calculate_confidence(root_cause_hypotheses),
        evidence=collect_evidence(timeline, impact_chain)
    )
```

---

### Layer 4: DECIDE 决策层

#### 4.1 问题分级矩阵

| 严重度 | 定义 | 响应时间 | 处理方式 | 示例 |
|--------|------|---------|---------|------|
| P0 | 系统完全不可用 | <5分钟 | 自动修复 | Pipeline崩溃、DB锁死 |
| P1 | 核心功能降级 | <30分钟 | 建议确认 | 策略超时5000ms |
| P2 | 非核心功能异常 | <2小时 | 趋势监控 | API限流K_DAY |
| P3 | 潜在风险 | 下次报告 | 记录观察 | 代码TODO堆积 |

#### 4.2 修复方案决策树

```
问题检测
    │
    ├── P0 → 自动修复
    │   ├── 有已知修复方案? → 执行 → 验证 → 成功则结束
    │   │                              └── 失败 → 升级P1
    │   └── 无已知方案? → 诊断 → 生成方案 → 人工确认
    │
    ├── P1 → 建议确认
    │   ├── 诊断根因
    │   ├── 生成修复方案 (代码补丁/配置变更)
    │   ├── 评估影响范围
    │   └── 推送给用户确认
    │
    ├── P2 → 趋势监控
    │   ├── 记录指标
    │   ├── 趋势分析
    │   └── 周报汇总
    │
    └── P3 → 记录观察
        └── 加入技能库观察列表
```

#### 4.3 安全边界检查

任何自动修复前必须检查：

```python
def safety_check(fix_plan: dict) -> SafetyResult:
    """安全边界检查"""
    checks = {
        "reversible": fix_plan.get("reversible", False),
        "rollback_available": fix_plan.get("rollback", None) is not None,
        "impact_scope": fix_plan.get("impact_scope", "unknown"),
        "affected_modules": fix_plan.get("affected_modules", []),
        "requires_restart": fix_plan.get("requires_restart", False),
        "data_modification": fix_plan.get("data_modification", False),
    }
    
    # 高风险操作不允许自动执行
    if checks["data_modification"]:
        return SafetyResult(
            allowed=False,
            reason="修改数据操作，需要人工确认"
        )
    
    # 影响范围超过3个模块需要确认
    if len(checks["affected_modules"]) > 3:
        return SafetyResult(
            allowed=False,
            reason=f"影响{len(checks['affected_modules'])}个模块，需要人工确认"
        )
    
    # 不可回滚的操作需要确认
    if not checks["reversible"]:
        return SafetyResult(
            allowed=False,
            reason="不可回滚操作，需要人工确认"
        )
    
    return SafetyResult(allowed=True)
```

---

### Layer 1: EXECUTE 执行层

#### 5.1 自动修复执行引擎

```python
class AutoFixEngine:
    def execute(self, fix_plan: dict) -> ExecutionResult:
        # 1. 安全边界检查
        safety = safety_check(fix_plan)
        if not safety.allowed:
            return ExecutionResult(
                status="BLOCKED",
                reason=safety.reason
            )
        
        # 2. 执行前快照
        snapshot = self.create_snapshot(fix_plan)
        
        # 3. 执行修复
        try:
            result = self.apply_fix(fix_plan)
        except Exception as e:
            # 4. 失败自动回滚
            self.rollback(snapshot)
            return ExecutionResult(
                status="FAILED",
                error=str(e),
                rollback_success=True
            )
        
        # 5. 验证修复效果
        verification = self.verify_fix(fix_plan)
        if not verification.success:
            self.rollback(snapshot)
            return ExecutionResult(
                status="VERIFICATION_FAILED",
                verification_detail=verification
            )
        
        # 6. 记录修复历史
        self.record_fix(fix_plan, snapshot, verification)
        
        return ExecutionResult(
            status="SUCCESS",
            verification=verification
        )
```

#### 5.2 已知自动修复方案库

```yaml
auto_fix_recipes:
  - name: pipeline_result_type_error
    trigger: "PipelineResult.*missing.*completed_at"
    severity: P0
    fix:
      type: patch
      file: "app/pipeline/orchestrator.py"
      action: "添加completed_at参数"
      verification: "重启服务，检查Pipeline是否正常"
    reversible: true
    
  - name: systemd_watchlist_override
    trigger: "WATCH_LIST_HK.*硬编码"
    severity: P0
    fix:
      type: patch
      file: "/etc/systemd/system/ftseek.service"
      action: "注释WATCH_LIST_HK行"
      verification: "systemctl daemon-reload && restart"
    reversible: true
    
  - name: database_wal_mode
    trigger: "database is locked"
    severity: P0
    fix:
      type: sql
      action: "PRAGMA journal_mode=WAL"
      verification: "检查数据库是否可正常读写"
    reversible: true
```

---

### Layer 5: GOVERNANCE 治理层

#### 6.1 修复审计日志

每次修复操作必须记录：

```json
{
  "audit_id": "fix-20260414-001",
  "timestamp": "2026-04-14T09:30:00",
  "issue": "REG-004: PipelineResult TypeError",
  "severity": "P0",
  "diagnosis": {
    "root_cause": "orchestrator.py 缺少 completed_at 参数",
    "confidence": 0.95,
    "evidence": ["TypeError日志", "代码行134"]
  },
  "fix": {
    "type": "patch",
    "file": "app/pipeline/orchestrator.py",
    "action": "添加completed_at=datetime.now()参数",
    "auto_executed": true
  },
  "verification": {
    "status": "SUCCESS",
    "checks": ["服务重启成功", "Pipeline正常执行", "无TypeError"]
  },
  "rollback": {
    "available": true,
    "snapshot_path": "backups/orchestrator_20260414_0930.py"
  }
}
```

#### 6.2 技能进化机制

每次诊断/修复后更新技能库：

```python
def evolve_skill(diagnosis_result: DiagnosisResult, fix_result: ExecutionResult):
    """技能进化"""
    if fix_result.status == "SUCCESS":
        # 成功的修复 → 加入自动修复库
        auto_fix_recipes.add({
            "name": generate_recipe_name(diagnosis_result),
            "trigger": diagnosis_result.symptoms,
            "fix": fix_result.fix_plan,
            "success_rate": 1.0,
            "last_success": datetime.now()
        })
    
    elif fix_result.status == "FAILED":
        # 失败的修复 → 更新诊断规则
        update_diagnosis_skill({
            "rule": diagnosis_result.rule_name,
            "failure_reason": fix_result.error,
            "lessons_learned": fix_result.rollback_actions
        })
```

---

## 3. 渐进式实施路线图

### Phase 1: 增强感知 + 自动诊断（第1周）

**目标**: deep_analysis.py 不只是发现问题，还能诊断根因

**实施步骤**:
1. 将现有 debug 技能转化为诊断规则
2. deep_analysis.py 集成诊断引擎
3. 输出综合诊断报告（不只是问题列表）
4. 增加趋势分析（对比历史审计数据）

**交付物**:
- `scripts/auto_audit/diagnosis_engine.py`
- 增强的 `deep_analysis.py`
- 诊断规则库 `config/diagnosis_rules.yaml`

### Phase 2: 决策层 + P0自动修复（第2周）

**目标**: 对最安全的P0问题实现自动修复

**实施步骤**:
1. 实现问题分级矩阵
2. 实现安全边界检查
3. 实现3个P0自动修复方案
4. 实现修复验证和回滚机制

**交付物**:
- `scripts/auto_audit/decision_engine.py`
- `scripts/auto_audit/auto_fix_engine.py`
- 自动修复方案库 `config/auto_fix_recipes.yaml`

### Phase 3: 事件驱动 + Webhook（第3周）

**目标**: 从定时巡检升级到实时事件驱动

**实施步骤**:
1. 启用 webhook 平台
2. 配置钉钉告警 webhook
3. 实现事件→诊断→修复的实时链路

**交付物**:
- Webhook 订阅配置
- 事件处理引擎
- 实时诊断报告

### Phase 4: 多Agent并行诊断（第4周）

**目标**: 复杂问题并行深度分析

**实施步骤**:
1. 实现 delegate_task 诊断编排
2. 4种专项诊断 Agent
3. 诊断结果汇总和关联分析

**交付物**:
- `scripts/auto_audit/agent_orchestrator.py`
- 4种诊断 Agent 配置
- 并行诊断报告模板

### Phase 5: 治理层 + 技能进化（第5周）

**目标**: 系统持续自我进化

**实施步骤**:
1. 实现修复审计日志
2. 实现技能进化机制
3. 实现周报自动生成

**交付物**:
- 审计日志系统
- 技能进化引擎
- 自动周报生成器

---

## 4. 文件结构规划

```
scripts/auto_audit/
├── __init__.py
├── utils.py                          # 已存在
├── pre_market_check.py               # 已存在
├── post_market_audit.py              # 已存在
├── config_drift_detector.py          # 已存在
├── deep_analysis.py                  # 已存在（待增强）
├── run_all_audits.py                 # 已存在
│
│ # Phase 1 新增
├── diagnosis_engine.py               # 诊断引擎
├── diagnosis_rules.yaml              # 诊断规则库
├── trend_analyzer.py                 # 趋势分析器
│
│ # Phase 2 新增
├── decision_engine.py                # 决策引擎
├── auto_fix_engine.py                # 自动修复引擎
├── auto_fix_recipes.yaml             # 自动修复方案库
├── safety_checker.py                 # 安全边界检查
│
│ # Phase 3 新增
├── event_processor.py                # 事件处理引擎
├── webhook_handlers.py               # Webhook 处理器
│
│ # Phase 4 新增
├── agent_orchestrator.py             # 多Agent编排器
├── agents/
│   ├── log_analyzer.py               # 日志分析Agent
│   ├── code_root_cause.py            # 代码根因Agent
│   ├── config_checker.py             # 配置检查Agent
│   └── impact_assessor.py            # 影响评估Agent
│
│ # Phase 5 新增
├── governance.py                     # 治理层引擎
├── audit_logger.py                   # 审计日志
├── skill_evolution.py                # 技能进化
└── weekly_report.py                  # 自动周报

data/
├── regression_tracker.json           # 已存在
├── audit_history.json                # 已存在
├── deep_analysis_history.json        # 已存在
├── diagnosis_cache.db                # 诊断缓存
├── fix_audit_log.json                # 修复审计日志
└── skill_evolution_log.json          # 技能进化日志

config/
├── diagnosis_rules.yaml              # 诊断规则
├── auto_fix_recipes.yaml             # 自动修复方案
├── severity_matrix.yaml              # 严重度矩阵
└── governance_policies.yaml          # 治理策略
```

---

## 5. 风险与缓解

| 风险 | 影响 | 缓解措施 |
|------|------|---------|
| 自动修复引入新Bug | 高 | 安全边界检查 + 自动回滚 + P0只修复已知问题 |
| 诊断误报 | 中 | 多证据验证 + 置信度阈值 |
| 性能开销 | 低 | 异步执行 + 资源限制 |
| 配置漂移未被检测 | 中 | 三向对比 + 运行时验证 |
| Webhook安全风险 | 高 | HMAC验证 + IP白名单 |

---

## 6. 成功指标

| 指标 | 当前状态 | Phase 1 目标 | Phase 2 目标 | Phase 5 目标 |
|------|---------|-------------|-------------|-------------|
| 问题发现时间 | 用户发现后告诉我 | <10分钟 | <5分钟 | <1分钟 |
| 根因定位时间 | 我来分析 | <30分钟 | <10分钟 | <3分钟 |
| P0修复时间 | 我来修复 | 我来修复 | <5分钟自动 | <2分钟自动 |
| 重复问题发生率 | 频繁 | 减少50% | 减少80% | 减少95% |
| 人工干预次数 | 每次都要 | 减少30% | 减少60% | 减少90% |

---

## 7. 附录：现有资产清单

### 已实现的感知能力
- [x] 盘前巡检 (pre_market_check.py)
- [x] 盘后审计 (post_market_audit.py)
- [x] 配置漂移检测 (config_drift_detector.py)
- [x] 6维度深度分析 (deep_analysis.py)
- [x] 历史回归追踪 (regression_tracker.json)
- [x] Crontab 定时调度

### 待实现的诊断能力
- [ ] 诊断规则引擎
- [ ] 根因定位算法
- [ ] 多Agent并行诊断
- [ ] 趋势分析引擎

### 待实现的执行能力
- [ ] 自动修复引擎
- [ ] 安全边界检查
- [ ] 回滚机制
- [ ] 验证闭环

### 待实现的治理能力
- [ ] 修复审计日志
- [ ] 技能进化机制
- [ ] 自动周报生成
- [ ] Webhook 事件驱动
