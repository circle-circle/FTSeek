# DEB-20260903-fullchain-bughunt — 三模型全链路盲审: 逻辑异常/bug 候选 132 项 (共性 72) — 核实完成: 证实 120/132, 驳回 10, 存疑 2

- **立案日**: 2026-09-03
- **发起人**: llm-eval-harness bug-hunt 链路 (kimi-k3, qwen3.8-max, qwen3.8-max-0902 @ 百炼 compatible-mode)
- **状态**: 已裁决 (核实完成: 证实 120/132, 驳回 10, 存疑 2) — 待下游分析与修复
- **纪律**: 同一题面同一采样口径 `{"temperature": 0.0, "max_tokens": 8192, "seed": 20260903}` (所有臂共用, max_retries=0); 答案全文落盘不截断; 模型自报 severity 仅参考, 以裁决后 severity 为准; off_manifest/越界引用=幻觉证据, 计入该模型误报
- **关联**: 评测报告 report_deb20260903.md · 台账 findings.db (run=deb20260903) · 原始答案 bug_hunt/deb20260903/answers/

## 一、审计设置

| 模型 | 调用 | 失败 | 契约违规 | findings | 输入tok | 输出tok |
|---|---|---|---|---|---|---|
| kimi-k3 | 9 | 5 | 0 | 55 | 208204 | 100775 |
| qwen3.8-max | 9 | 0 | 0 | 112 | 559164 | 200291 |
| qwen3.8-max-0902 | 9 | 0 | 0 | 118 | 559164 | 212143 |

- 目标仓 `/opt/ftseek/FTSeek-US` @ `955514fb5a1a` 全程只读。
- 打包与盲区: s1_entry_config: 22 in / 0 truncated / 4 omitted; s2_market_data: 12 in / 1 truncated / 28 omitted; s3_scheduling_pipeline: 14 in / 2 truncated / 34 omitted; s4_signals_sources: 35 in / 0 truncated / 35 omitted; s5_decision_orchestration: 27 in / 1 truncated / 20 omitted; s6_playbook_engine: 17 in / 2 truncated / 39 omitted; s7_risk_execution: 14 in / 1 truncated / 60 omitted; s8_monitoring_surface: 24 in / 0 truncated / 33 omitted; s9_review_backtest: 19 in / 1 truncated / 28 omitted
- 局限: 每片段单次采样 (能力 vs 运气不可分); 超长文件中段不可见; 召回是相对口径 (对三模型并集+核实证实的池)。

## 二、裁决回写规范 (分析 agent 阅读)

对每个议题, 把 `裁决: 待回写` 行替换为以下格式之一 (标记行 `<!-- DEB-VERDICT -->` 保留):

```
裁决: CONFIRMED | severity=4 | 置信=high | 理由: <对照代码的论证, 引具体行为证>
裁决: REJECTED | 置信=high | 理由: <为何不成立: 路径不可达/上游已守卫/语义误读>
裁决: UNCERTAIN | 置信=low | 理由: <需要运行态才能判定, 说明缺什么证据>
```

判定标准: 该缺陷在**真实运行路径**上是否会造成错误行为 (错单/漏单/资损/风控失效/状态错乱 = severity 4-5)。
发现同一根因被拆到不同文件的两议题时, 在理由中注明 `merge=<另一议题号>`。

## 三、共性议题 (≥2 模型命中 — 高置信候选, 优先裁决)

### 议题 #11 [共性 3/3] `config/config_us.py`:33-189 — config_us 导出的 RISK_CONFIG/DECISION_CONFIG 等是 import 期快照，热重载不会更新

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S4
- **kimi-k3** (S3, config_effect, config/config_us.py:173-189): WATCH_LIST_US 先从 yaml SSOT 赋值, 随后被已弃用的 JSON 加载覆盖
  - 证据引用: `33 行: `WATCH_LIST_US = _loader.WATCH_LIST_US` (来自 unified_config.yaml);189 行: `WATCH_LIST_US = _load_watchlist_from_json()` 重新赋值, 覆盖前者;186-187 行 fallback: `# fallback到默认值 ... return ["US.TSLA", "US.NVDA", ..., "US.NVDA"]` (硬编码且 US.NVDA 重复);对照 app/utils/watchlist_manager.py 35 行: 'P0 修复 (2026-05-03):`
  - 影响声称: config_us.WATCH_LIST_US 的消费者拿到的是已弃用 JSON (或 JSON 缺失时的硬编码 10 只股票, 含重复 NVDA), 与 watchlist_manager 读取的 yaml SSOT 名单分叉; 改 unified_config.yaml 的 watchlist 对这部分消费者不生效, 且文件头'所有配置已迁移至 unified_config.yaml'的声明被
  - 建议: 删除 173-189 行的 JSON 加载与硬编码 fallback, 保留 33 行 loader 赋值; 如需应急兜底应对齐 watchlist_manager 的 yaml 优先策略
- **qwen3.8-max** (S4, config_effect, config/config_us.py:33-42): config_us 导出的 RISK_CONFIG/DECISION_CONFIG 等是 import 期快照，热重载不会更新
  - 证据引用: `    WATCH_LIST_US = _loader.WATCH_LIST_US
    DECISION_CONFIG = _loader.DECISION_CONFIG
    RISK_CONFIG = _loader.RISK_CONFIG
    
    # P1 新增: 统一配置导出 (迁移自硬编码)
    EXECUTION_CONFIG = _loader.EXECUTION_CONFIG
    OBSERVABILITY_CONFIG = _loader.OBSERVABILITY_CONFIG
    DATABASE_CONFIG = _loader.DATABA`
  - 影响声称: _loader.RISK_CONFIG 等属性每次访问会基于当前 YAML 生成新字典，但 config_us 在 import 时把字典固化为模块常量；后续热重载 unified_config.yaml 后，仍 from config_us import RISK_CONFIG 的链路会继续读旧风控/执行参数。
  - 建议: 不要导出静态字典快照；改为导出函数/属性（如 get_risk_config()）实时读 loader，或在热重载回调中显式刷新 config_us 模块级导出常量。
- **qwen3.8-max** (S3, config_effect, config/config_us.py:33-189): WATCH_LIST_US 先声明从 unified_config 读取，后被 JSON/硬编码覆盖
  - 证据引用: `    WATCH_LIST_US = _loader.WATCH_LIST_US
    return ["US.TSLA", "US.NVDA", "US.MU", "US.GOOG", "US.BABA", "US.AAPL", "US.MSFT", "US.AMZN", "US.META", "US.NVDA"]
WATCH_LIST_US = _load_watchlist_from_json()`
  - 影响声称: unified_config.yaml 的 watchlist 对 config_us.WATCH_LIST_US 不再生效；若 watchlist_us.json 存在则读 JSON，否则回退硬编码名单，造成与 WatchlistManager 的 YAML SSOT 双源漂移，且硬编码列表中 US.NVDA 重复。
  - 建议: 删除 _load_watchlist_from_json 对 WATCH_LIST_US 的覆盖，统一使用 _loader.WATCH_LIST_US 或 WatchlistManager 从 unified_config.yaml 加载；如保留 JSON 回退需明确优先级并去重。
- **qwen3.8-max-0902** (S4, config_effect, config/config_us.py:33-189): WATCH_LIST_US 先取 unified_config，随后被 watchlist_us.json 覆盖
  - 证据引用: `    WATCH_LIST_US = _loader.WATCH_LIST_US
WATCH_LIST_US = _load_watchlist_from_json()`
  - 影响声称: 声称 SSOT 的 unified_config.yaml watchlist 对 config_us.WATCH_LIST_US 消费链不生效；与 WatchlistManager 的 YAML 优先加载形成双名单漂移，可能导致交易标的池不一致。
  - 建议: 删除 JSON 覆盖，或明确 watchlist_us.json 仅作为 YAML 缺失时的 fallback，并统一所有消费者走同一真源。

<!-- DEB-VERDICT cluster=11 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: config/config_us.py:33 从 unified_config.yaml 赋值 WATCH_LIST_US = _loader.WATCH_LIST_US，但 config/config_us.py:189 无条件执行 WATCH_LIST_US = _load_watchlist_from_json() 覆盖前者，YAML SSOT 的 watchlist 修改永不生效

### 议题 #4 [共性 3/3] `app/config_mgmt/validator.py`:35-206 — watchlist.us_stocks 的 min_length=1 声明未被校验实现

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S3
- **kimi-k3** (S2, config_effect, app/config_mgmt/validator.py:177-186): REQUIRED_KEYS 声明的 min_length 约束无任何代码执行
  - 证据引用: `35 行: `"watchlist.us_stocks": {"type": list, "min_length": 1},`;但 _check_required_keys (177-186) 只判 `if value is None:`, _check_types (195-206) 只判 `isinstance(value, expected_type)`, 全文无 min_length 消费`
  - 影响声称: watchlist.us_stocks 为空列表 [] 时 strict 校验照样通过, 系统以空监控名单启动, 信号扫描池为空且无告警, 与声明的'min_length: 1'防御意图不符
  - 建议: 在 _check_types 或新增 _check_length 中对 spec 含 min_length 的键执行 `len(value) >= spec['min_length']` 校验, 失败计入 errors
- **qwen3.8-max** (S2, config_effect, app/config_mgmt/validator.py:35-206): watchlist.us_stocks 的 min_length=1 声明未被校验实现
  - 证据引用: `    "watchlist.us_stocks": {"type": list, "min_length": 1},
        for key, spec in REQUIRED_KEYS.items():
            value = self.config.get(key)
            if value is None:
                error = f"必需配置缺失: {key}"
            expected_type = spec.get("type")
            if expected_type and no`
  - 影响声称: REQUIRED_KEYS 声明 watchlist.us_stocks 需要 min_length=1，但 _check_required_keys 只查 None，_check_types 只查类型；空列表 [] 会通过启动校验，系统可能以空名单启动。
  - 建议: 在类型校验或必需键校验中实现 min_length：若 isinstance(value, list) 且 len(value) < spec['min_length']，strict_mode 下计入 errors。
- **qwen3.8-max-0902** (S3, config_effect, app/config_mgmt/validator.py:35-206): REQUIRED_KEYS 声明 min_length 但类型校验未实现，空 watchlist 可通过启动校验
  - 证据引用: `"watchlist.us_stocks": {"type": list, "min_length": 1},
            expected_type = spec.get("type")
            if expected_type and not isinstance(value, expected_type):
                result.errors.append(
                    f"类型错误: {key} = {value!r} (期望 {expected_type})"
                )`
  - 影响声称: watchlist.us_stocks 为空列表时只满足 type=list，不会触发 min_length 错误；系统可能以空扫描池启动。
  - 建议: 在 ConfigValidator 中消费 min_length 等 spec；空列表应记为 strict 错误。

<!-- DEB-VERDICT cluster=4 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/config_mgmt/validator.py:35 声明 "watchlist.us_stocks": {"type": list, "min_length": 1}，但 _check_required_keys (line 181) 仅判 value is None、_check_types (line 203) 仅判 isinstance，全文无代码消费 min_length 字段，空列表 [] 可通过启动校验

### 议题 #10 [共性 3/3] `config/config_loader.py`:104-406 — MIN_CONFIDENCE/MIN_CONSENSUS 的 loader 默认值与 dataclass 默认值不一致

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S2
- **kimi-k3** (S2, config_effect, config/config_loader.py:196-204): 环境变量缺失时 ${VAR} 占位符原样保留为真值字符串
  - 证据引用: `200-203: `def replacer(match):\n env_var = match.group(1)\n return os.getenv(env_var, match.group(0))` — 环境变量未设置时返回字面量 "${VAR}";下游如 318 行 `"DINGTALK_WEBHOOK_URL_US": self.get('service.dingtalk.webhook_url', '')``
  - 影响声称: env 缺失时配置值变成字面量 '${DINGTALK_WEBHOOK_URL_US}' 等非空字符串, 下游 `if webhook_url:` 之类的'是否已配置'判断被欺骗, 告警通道看似已配置实际 POST 到垃圾地址, 告警静默丢失; 数值型配置残留占位符还会引发下游类型错误
  - 建议: 未解析的占位符替换为空字符串或 None, 并在 _load 后对仍含 '${' 的值输出 WARNING 列表, 使缺失 env 显式可观测
- **kimi-k3** (S2, config_effect, config/config_loader.py:271-272): 同一配置键的兜底默认值在不同位置不一致 (MIN_CONFIDENCE 0.80 vs 0.70, MIN_CONSENSUS 0.25 vs 0.3)
  - 证据引用: `271-272: `'MIN_CONFIDENCE': self.get('risk.signal_quality.min_confidence', 0.80),` / `'MIN_CONSENSUS': self.get('risk.signal_quality.min_consensus', 0.25),`;而 104-105 行 dataclass: `min_confidence: float = 0.70` / `min_consensus: float = 0.3`;379 行 validate 也用 0.70`
  - 影响声称: yaml 缺失 risk.signal_quality.min_confidence 时, 走 RISK_CONFIG 的消费者按 0.80 过滤信号, 走 dataclass/validate 路径的按 0.70, 同一系统出现两套信号门槛, 排查'配置已改但不生效'类问题时无法定位
  - 建议: 全仓统一该键的兜底默认值 (建议以 dataclass 0.70/0.3 为 SSOT), 或在 get() 无兜底时直接报错而非给默认值
- **qwen3.8-max** (S2, config_effect, config/config_loader.py:190-406): 单例 get_config 在首次初始化后忽略传入的 config_path
  - 证据引用: `    @classmethod
    def get_instance(cls, config_path: str = None) -> 'ConfigLoader':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance

def get_config(config_path: str = None) -> ConfigLoader:
    """获取全局配置实例"""
    return ConfigLoader.get_`
  - 影响声称: 首次 get_config() 创建默认 unified_config.yaml 单例后，后续 get_config('other.yaml') 不会加载新路径；测试、多环境或脚本入口尝试覆盖配置文件时会静默使用原配置。
  - 建议: 若传入不同 config_path，应显式报错或重建实例；或提供 reset_instance/reload(path) 接口，避免调用方误以为路径覆盖生效。
- **qwen3.8-max-0902** (S2, config_effect, config/config_loader.py:104-272): MIN_CONFIDENCE/MIN_CONSENSUS 的 loader 默认值与 dataclass 默认值不一致
  - 证据引用: `    min_confidence: float = 0.70
    min_consensus: float = 0.3
            'MIN_CONFIDENCE': self.get('risk.signal_quality.min_confidence', 0.80),
            'MIN_CONSENSUS': self.get('risk.signal_quality.min_consensus', 0.25),`
  - 影响声称: 当 YAML 缺失对应键时，不同入口读到不同阈值（0.70/0.30 vs 0.80/0.25），造成信号质量门槛口径不一致。
  - 建议: 统一默认值来源，删除兼容属性中的独立默认值，或让兼容属性引用同一 dataclass 默认值。
- **qwen3.8-max-0902** (S2, config_effect, config/config_loader.py:206-214): 环境变量占位符解析不递归处理 list，列表内 ${VAR} 不会替换
  - 证据引用: `def _resolve_dict_env(self, d: dict) -> dict:
        """递归解析字典中的环境变量"""
        result = {}
        for k, v in d.items():
            if isinstance(v, dict):
                result[k] = self._resolve_dict_env(v)
            else:
                result[k] = self._resolve_env_vars(v)
        return`
  - 影响声称: 若 unified_config.yaml 的列表项包含 ${VAR}，运行时会保留字面占位符，可能导致名单、密钥或配置项无效。
  - 建议: 在 _resolve_env_vars/_resolve_dict_env 中识别 list/tuple，并递归处理每个元素。
- **qwen3.8-max-0902** (S2, config_effect, config/config_loader.py:190-194): ConfigLoader.get_instance 在单例已存在时忽略新 config_path
  - 证据引用: `@classmethod
    def get_instance(cls, config_path: str = None) -> 'ConfigLoader':
        if cls._instance is None:
            cls._instance = cls(config_path)
        return cls._instance`
  - 影响声称: 首次 get_config() 后，后续显式传入备用/测试配置路径不会生效，测试或多配置场景会静默读取默认 unified_config.yaml。
  - 建议: 若已存在实例且 config_path 不同，应报错或重建实例；或提供显式 reset 接口。

<!-- DEB-VERDICT cluster=10 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: config/config_loader.py:104 dataclass SignalQuality.min_confidence 默认 0.70 / min_consensus 默认 0.3，但 config/config_loader.py:271-272 RISK_CONFIG 属性的 get() fallback 分别为 0.80 和 0.25，YAML 键缺失时两条消费路径拿到不同默认值

### 议题 #8 [共性 3/3] `app/utils/timezone.py`:169-237 — get_market_session 未同步半日市/16:00 边界，与 is_market_open 语义不一致

环节: s1_entry_config · 类别: time_boundary · 自报最高 severity: S3
- **kimi-k3** (S2, time_boundary, app/utils/timezone.py:230-237): get_market_session 与 is_market_open 在 16:00 整及半日市判定不一致
  - 证据引用: `is_market_open 已修复: 172-181 行注释 '原上界用 `<=` 使 16:00 整被判开盘, 改 `<`', 并用动态 `_close_mins = int(get_us_close_minutes_et(dt.date()))`, `return 570 <= _mins < _close_mins`;但 get_market_session 233 行仍是固定 `elif 9.5 <= hour_min <= 16.0:``
  - 影响声称: 16:00 整 is_market_open 返回 False 而 get_market_session 返回 'regular';半日市 (如感恩节后 13:00 收) 下午 is_market_open 已判闭市, get_market_session 仍报 'regular', 两个 SSOT 时间函数互相矛盾, 依赖 session 的链路与依赖 is_market_open 的链路行为分
  - 建议: get_market_session 复用 is_market_open 的动态收盘分钟数与 `<` 上界, 保持同一判定口径
- **qwen3.8-max** (S3, time_boundary, app/utils/timezone.py:169-237): get_market_session 未同步半日市/16:00 边界，与 is_market_open 语义不一致
  - 证据引用: `    return 570 <= _mins < _close_mins
    elif 9.5 <= hour_min <= 16.0:
        return "regular"`
  - 影响声称: is_market_open 已按节假日半日市动态收盘分钟判断，且 16:00 使用 < 边界；但 get_market_session 仍固定 9.5 <= hour_min <= 16.0 返回 regular。半日市 13:00 收盘后或 16:00 整点，依赖 session 的链路可能仍认为盘中。
  - 建议: get_market_session 复用 holiday_calendar/get_us_close_minutes_et，并将 regular 区间改为 9.5 <= hour_min < 动态收盘分钟，保持与 is_market_open 一致。
- **qwen3.8-max-0902** (S3, time_boundary, app/utils/timezone.py:180-234): get_market_session 未同步半日市/16:00 边界，与 is_market_open 不一致
  - 证据引用: `_mins = dt.hour * 60 + dt.minute
    return 570 <= _mins < _close_mins
    elif 9.5 <= hour_min <= 16.0:
        return "regular"`
  - 影响声称: 半日市提前收盘后或 16:00 整，get_market_session 仍可能返回 regular，而 is_market_open 已关闭；依赖 session 的信号、过期或归因逻辑可能误判盘中。
  - 建议: get_market_session 使用与 is_market_open 相同的动态收盘分钟和 < 边界。

<!-- DEB-VERDICT cluster=8 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/utils/timezone.py:233 get_market_session 用固定 9.5 <= hour_min <= 16.0 判定 regular (含 16:00 整且无半日市感知)，而 is_market_open (line 181) 用 570 <= _mins < _close_mins (动态收盘、严格 <)，16:00 整点及半日市 13:00-16:00 两函数判定矛盾

### 议题 #14 [共性 3/3] `main.py`:296-414 — ConfigValidator/ConfigAudit 的阻断异常被外层 except 降级吞掉

环节: s1_entry_config · 类别: error_swallowing · 自报最高 severity: S4
- **kimi-k3** (S4, error_swallowing, main.py:413-414): 配置治理的 blocking raise 被外层 except Exception 吞掉, "实盘启动已阻止"实际不阻止
  - 证据引用: `310-319: `if not validation_result.is_valid:` ... `raise RuntimeError(f"ConfigValidator 发现 {len(validation_result.errors)} 个阻塞错误, 请修复配置后重试")`;407-409: `except ConfigAuditError as e: logger.critical("[ConfigAudit] ❌ 配置一致性审计失败 — 实盘启动已阻止: %s", e)\n raise`;但这两处 raise 都在 296 行 `try:` 内, 被 413-414 捕获: `ex`
  - 影响声称: strict_mode=True 的 ConfigValidator 与 strict 审计 (注释 305-306 行自称'双层防御: Validator + audit 均为 blocking') 实际上永远不阻断启动: 配置校验失败仅降级为 WARNING, 系统带错误配置继续运行; 且 Layer 1 raise 发生在 366-388 行 drift_detector/hot_reloa
  - 建议: 将 Layer 1 校验与 Layer 1.7 审计移出包裹 ConfigGov 初始化的 try/except, 或在外层 except 中对 RuntimeError/ConfigAuditError 重新 raise (如 `except (RuntimeError, ConfigAuditError): raise`), 使 blocking 语义与日志一致
- **qwen3.8-max** (S4, error_swallowing, main.py:296-414): ConfigValidator/ConfigAudit 的阻断异常被外层 except 降级吞掉
  - 证据引用: `        if not validation_result.is_valid:
            logger.critical(
                "[ConfigGov] ❌ 配置校验失败 %d 个错误 — 实盘启动已阻止: %s",
                len(validation_result.errors),
                "; ".join(validation_result.errors[:5]),
            )
            raise RuntimeError(
                f`
  - 影响声称: 注释与日志声称配置校验/审计失败会阻止启动，但 raise 会被同一 try 的宽泛 except 捕获并继续运行；缺失或非法的关键风控配置仍可能进入调度器，启动期风控门槛失效。
  - 建议: 将 blocking 校验（ConfigValidator、ConfigAuditError）移出宽泛降级 try，或在 except 中识别并重新抛出阻断异常；校验失败应直接 sys.exit(1)/raise 到入口。
- **qwen3.8-max-0902** (S4, error_swallowing, main.py:296-414): 启动期 strict 配置校验/审计异常被外层降级 try 吞掉，未阻断启动
  - 证据引用: `try:
        from app.config_mgmt import ConfigValidator, ConfigDriftDetector, ConfigHotReloader
            raise RuntimeError(
                f"ConfigValidator 发现 {len(validation_result.errors)} 个阻塞错误, "
                f"请修复配置后重试"
            )
    except Exception as e:
        logger.warning(f`
  - 影响声称: ConfigValidator strict_mode=True 和 ConfigAudit 的 blocking 语义失效：配置校验失败只记录 warning 后继续启动，错误风控/关键配置可能进入运行链路。
  - 建议: 将 blocking 校验移出降级 try，或在 except 中识别配置校验/审计异常并重新抛出、退出进程。

<!-- DEB-VERDICT cluster=14 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: main.py:316-319 ConfigValidator 的 raise RuntimeError 和 main.py:409 ConfigAuditError 的 raise 均被 main.py:413 外层 except Exception 捕获后降级为 warning 日志继续启动，配置治理的阻断机制形同虚设

### 议题 #15 [共性 3/3] `main.py`:680-687 — --stop/--status/--test 未实现，任何参数都会启动交易调度器

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, main.py:681-687): --start/--stop/--status/--test 参数被解析但完全未使用, 任何调用都启动调度器
  - 证据引用: `681-687: `parser.add_argument("--start", ...)` 等四个参数, `args = parser.parse_args()` 后无条件 `_run_new_scheduler(args)`;而 `_run_new_scheduler(args)` (119 行) 函数体内再未引用 args。ftseek_us.sh 53 行: `$PYTHON $MAIN_PY --test``
  - 影响声称: `main.py --stop/--status/--test` 均不会执行停服/查状态/测试连接, 而是尝试完整启动交易系统: 锁空闲时 `ftseek_us.sh test` 会意外拉起真实交易进程; 锁被占时则打印'已在运行'退出码 1, 误导运维判断
  - 建议: 在 main() 中按 args 分支: --stop/--status 走进程查询与信号发送, --test 走连接测试子命令, 仅 --start 调用 _run_new_scheduler; 或删除未实现的参数避免误用
- **qwen3.8-max** (S3, logic_anomaly, main.py:680-687): --stop/--status/--test 未实现，任何参数都会启动交易调度器
  - 证据引用: `    parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--test", action="store_true", help="测试连接")

    args = parser.parse_`
  - 影响声称: main.py 解析了 stop/status/test 参数但没有分派；直接运行 main.py --stop/--status/--test 或 ftseek_us.sh test 都会启动完整交易服务，而不是停止/状态/连接测试，运维误操作可能意外拉起交易进程。
  - 建议: 根据 args.start/stop/status/test 实现分派；--stop 发送停止信号或调用停止逻辑，--status 查询进程/健康状态，--test 只做 OpenD/依赖连通性检查。
- **qwen3.8-max-0902** (S3, logic_anomaly, main.py:681-687): CLI 子命令未分发，--stop/--status/--test 都会启动交易服务
  - 证据引用: `parser.add_argument("--start", action="store_true", help="启动服务")
    parser.add_argument("--stop", action="store_true", help="停止服务")
    parser.add_argument("--status", action="store_true", help="查看状态")
    parser.add_argument("--test", action="store_true", help="测试连接")

    args = parser.parse_args`
  - 影响声称: ftseek_us.sh test 会执行 main.py --test，但 main.py 不判断参数，直接进入 _run_new_scheduler，可能启动完整交易服务；--stop/--status 也无法停止或查看状态。
  - 建议: 在 main() 中根据 args.start/stop/status/test 分发；--test 只做连通性检查，--stop/--status 走 PID/锁或系统信号。

<!-- DEB-VERDICT cluster=15 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: main.py:681-687 argparse 定义了 --start/--stop/--status/--test 四个参数，但 main.py:687 无条件调用 _run_new_scheduler(args) 且该函数(line 119)函数体内从未引用 args，--stop/--status/--test 均会启动交易调度器

### 议题 #28 [共性 3/3] `app/storage/daily_log_store.py`:80-104 — record_tick 每次新建连接却做"10秒才commit"优化 — 未 commit 的 INSERT 随 conn.close() 回滚, 大部分 tick 静默丢失

环节: s2_market_data · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, app/storage/daily_log_store.py:80-104): record_tick 每次新建连接却做"10秒才commit"优化 — 未 commit 的 INSERT 随 conn.close() 回滚, 大部分 tick 静默丢失
  - 证据引用: `L81 `conn = sqlite3.connect(self.db_path)` (每次调用新建连接); L96-102 `# 优化：每 10 秒 commit 一次，避免 IO 瓶颈 ... if now - self._last_commit > 10.0: conn.commit() ... else: pass # 延迟 commit`; L104 `conn.close()``
  - 影响声称: sqlite3 默认隔离级别下 INSERT 处于隐式事务, close() 前未 commit 即回滚; 距上次 commit ≤10s 的记录全部丢失, 只有踩中 >10s 窗口的那一条被持久化 → market_ticks 大面积缺数据, 盘后影子测试基于残缺数据且无报错 (L106-107 仅 debug)
  - 建议: 每次 commit, 或持有长连接+周期批量 commit, 或 connect(isolation_level=None) 自动提交; 拆除"新连接+延迟commit"组合
- **qwen3.8-max** (S3, logic_anomaly, app/storage/daily_log_store.py:96-104): 延迟提交在新连接/即关即闭下会导致大部分 tick 未持久化
  - 证据引用: `now = time.time() / if now - self._last_commit > 10.0: / conn.commit() / self._last_commit = now / else: / pass # 延迟 commit / conn.close()`
  - 影响声称: record_tick 每次新建 sqlite 连接；未满足 10 秒阈值时关闭连接前不 commit，SQLite 会回滚当前 INSERT，导致绝大多数分钟级快照丢失，只剩偶尔触发 commit 的那一条。
  - 建议: 改为持久连接/写入队列并在同一事务中批量 commit，或每次 insert 后立即 commit；不要把“延迟 commit”实现为跨连接关闭。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/storage/daily_log_store.py:96-104): 延迟 commit 后立即 close，未提交事务被回滚导致大量 tick 丢失
  - 证据引用: `if now - self._last_commit > 10.0:
    conn.commit()
    self._last_commit = now
else:
    pass # 延迟 commit

conn.close()`
  - 影响声称: record_tick 每次新建 sqlite 连接并 INSERT，但只有距上次超过 10 秒才 commit；未 commit 的连接被 close 后事务自动回滚，绝大多数分钟快照不会落库，影子回放数据缺失。
  - 建议: 要么每次写入立即 commit，要么使用长连接/连接池并在同一事务中定期 commit；关闭连接前必须确保事务已提交。

<!-- DEB-VERDICT cluster=28 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: daily_log_store.py:96-104 每次record_tick新建连接(L81), 条件commit仅10s间隔触发(L98), 否则L104 conn.close()回滚未提交INSERT, 绝大部分tick静默丢失(影子测试数据源不可靠)

### 议题 #21 [共性 3/3] `app/data/market_data_router.py`:589-622 — 真实 OrderBook 覆盖后 lv3_book_is_proxy 未同步更新

环节: s2_market_data · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **kimi-k3** (S3, dataflow_inconsistency, app/data/market_data_router.py:613-622): router 读取 futu_quote 的 high_price/low_price/open_price 键, 但 provider 产出的报价字典只有 high/low/open 键 — day_high/day_low/open_price 恒为 0
  - 证据引用: `L613 `day_high = float(futu_quote.get("high_price", 0))`, L621 `ts_ctx["day_low"] = float(futu_quote.get("low_price", 0))`, L622 `ts_ctx["open_price"] = float(futu_quote.get("open_price", 0))`; 而 futu_lv3_provider.py 快照路径 L886-888 产出 `"open": float(open_price), "high": float(high), "low": float(low)`
  - 影响声称: ts_ctx["day_high"]/["day_low"]/["open_price"] 恒 0.0; L614 `if day_high > 0` 永不成立 → high_distance_pct 永不计算 (落 L851 setdefault 0.0); ORB/日内高低点的下游消费方拿到全 0 数据
  - 建议: 改为 futu_quote.get("high")/get("low")/get("open"), 与 provider 产出键对齐
- **qwen3.8-max** (S3, dataflow_inconsistency, app/data/market_data_router.py:613-622): 读取 futu_quote 不存在的 high_price/low_price/open_price 键
  - 证据引用: `day_high = float(futu_quote.get("high_price", 0)) / ts_ctx["day_low"] = float(futu_quote.get("low_price", 0)) / ts_ctx["open_price"] = float(futu_quote.get("open_price", 0)) / "high": float(high), / "low": float(low),`
  - 影响声称: FutuLV3Provider.get_quote 返回字段为 high/low/open，而路由读取 high_price/low_price/open_price，导致 day_high/day_low/open_price 恒为 0，high_distance_pct 无法计算，下游消费这些字段时会拿到失真上下文。
  - 建议: 改为读取 futu_quote['high']/['low']/['open']，或在 provider 输出层统一字段名并增加字段契约测试。
- **qwen3.8-max** (S2, dataflow_inconsistency, app/data/market_data_router.py:589-600): 真实 OrderBook 覆盖后 lv3_book_is_proxy 未同步更新
  - 证据引用: `bid_vol = futu_quote.get("bid_vol", 0) / ask_vol = futu_quote.get("ask_vol", 0) / total_vol = bid_vol + ask_vol / ts_ctx["lv3_book_is_proxy"] = not (total_vol > 0) / if ob_factors["bid_price"] > 0: / ts_ctx["lv3_bid"] = ob_factors["bid_price"] / ts_ctx["lv3_bid_vol"] = ob_factors["bid_vol"]`
  - 影响声称: lv3_book_is_proxy 先按 quote 的 bid_vol/ask_vol 计算；后续 Push OrderBook 覆盖 lv3_bid/ask/vol 后没有重算该哨兵，可能把已有真实盘口的样本继续标记为 proxy，影响 S2 校准/遥测和退化样本识别。
  - 建议: 在 orderbook 覆盖 lv3_bid_vol/lv3_ask_vol 后重算 lv3_book_is_proxy 和顶层 order_flow_imbalance，或明确只使用 ob_* 字段。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/data/market_data_router.py:613-622): 消费 quote 字段使用 high_price/low_price/open_price，与上游 quote 键不一致
  - 证据引用: `day_high = float(futu_quote.get("high_price", 0))
ts_ctx["day_high"] = day_high
ts_ctx["day_low"] = float(futu_quote.get("low_price", 0))
ts_ctx["open_price"] = float(futu_quote.get("open_price", 0))`
  - 影响声称: FutuLV3Provider.get_quote 返回的键是 high/low/open，不是 high_price/low_price/open_price；因此 day_high/day_low/open_price 恒为 0，high_distance_pct 无法注入，ORB/日内高低点相关下游特征会失真。
  - 建议: 改为读取 futu_quote.get("high"), futu_quote.get("low"), futu_quote.get("open")，或在 provider 出口统一字段命名并在消费端使用同一 schema。

<!-- DEB-VERDICT cluster=21 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: market_data_router.py:613/621/622 读取futu_quote的high_price/low_price/open_price键, 但get_quote(futu_lv3_provider.py:834-838)返回high/low/open键, .get默认0导致day_high/day_low/open_price恒为0; 且L691-695真实OrderBook覆盖bid/ask/vol后L597的lv3_book_is_proxy标志未同步更新

### 议题 #16 [共性 3/3] `app/data/futu_lv3_provider.py`:686-750 — _subscribed_symbols 读写未加锁，订阅/退订/重连可能竞态

环节: s2_market_data · 类别: race_condition, state_machine · 自报最高 severity: S3
- **kimi-k3** (S3, state_machine, app/data/futu_lv3_provider.py:711-732): subscribe 部分/全部类型订阅失败仍把 symbols 记入 _subscribed_symbols 并返回 True
  - 证据引用: `L717-721 `if ret != 0: logger.warning(f"[FutuLV3] 订阅 {st_name} 部分失败: {data}, " ...)` 后仅继续循环; L728 `self._subscribed_symbols.update(new_symbols)` 无条件执行; 而 L686 `new_symbols = [s for s in futu_symbols if s not in self._subscribed_symbols]` 会过滤已标记标的`
  - 影响声称: 订阅失败 (无 LV3 权限/槽位满/单类型失败) 的标的被标记为已订阅, 后续 subscribe 因 L686 过滤永不再试; watchdog 因集合非空持续健康检查但无推送; get_quote L824 对未真正订阅的标的调 get_stock_quote
  - 建议: 按 (symbol, sub_type) 记录订阅状态, 仅成功项入集合; 失败类型保留待下次重试
- **kimi-k3** (S3, state_machine, app/data/futu_lv3_provider.py:742-750): 订阅失败重连后的重试映射表缺 ORDER_BOOK/TICKER/K_5M/K_60M/K_DAY, 重连后这些类型订阅被静默丢弃
  - 证据引用: `重试路径 L744-747 `_sub = {'QUOTE': _ST.QUOTE, 'K_1M': _ST.K_1M, 'K_15M': _ST.K_15M, 'K_30M': _ST.K_30M}.get(st_name)`; 正常路径 L699-709 sub_type_map 含 K_5M/K_60M/K_DAY/ORDER_BOOK/TICKER; L750 `self._subscribed_symbols.update(futu_symbols_retry)` 仍全量标记已订阅`
  - 影响声称: 动态活跃集 (_configured_active_set_sub_types 默认含 ORDER_BOOK+TICKER, L90-111) 触发重连重试后, 盘口/逐笔订阅静默丢失且标的已标记 → 不再补订 → _push_orderbook_cache/_push_ticker_cache 停止更新, 消费端无感知降级
  - 建议: 重试路径复用正常路径的 sub_type_map (提取共用常量/函数)
- **qwen3.8-max** (S3, state_machine, app/data/futu_lv3_provider.py:742-749): 重连后的重订阅映射丢失 ORDER_BOOK/TICKER 等订阅类型
  - 证据引用: `return base + [t for t in ('ORDER_BOOK', 'TICKER') if t not in base] / _sub = { 'QUOTE': _ST.QUOTE, 'K_1M': _ST.K_1M, 'K_15M': _ST.K_15M, 'K_30M': _ST.K_30M, }.get(st_name)`
  - 影响声称: 活跃集可配置订阅 ORDER_BOOK/TICKER，但 subscribe 异常重连后的重试映射只包含 QUOTE/K_1M/K_15M/K_30M；重连后盘口/逐笔推送不会恢复，下游会误判为无数据或持续降级。
  - 建议: 重连重试路径复用首次订阅的完整 sub_type_map，或从配置重新解析 sub_types，确保 ORDER_BOOK/TICKER/K_5M/K_60M/K_DAY 都能恢复。
- **qwen3.8-max** (S3, state_machine, app/data/futu_lv3_provider.py:716-732): subscribe 返回失败仍更新已订阅集合并返回 True
  - 证据引用: `ret, data = quote_ctx.subscribe(new_symbols, [sub_type], subscribe_push=True) / if ret != 0: / self._subscribed_symbols.update(new_symbols) / return True`
  - 影响声称: 即使 Futu subscribe 对某个或全部 sub_type 返回非零，代码仍将 symbols 加入 _subscribed_symbols 并返回成功；后续健康检查、push 读取和路由会基于虚假订阅状态运行，造成无推送但状态显示已订阅。
  - 建议: 按 sub_type 记录成功/失败，仅把成功订阅的类型/标的加入状态；关键类型失败时返回 False 并触发重连或降级。
- **qwen3.8-max-0902** (S3, state_machine, app/data/futu_lv3_provider.py:742-747): 重连后重订阅映射表丢失 ORDER_BOOK/TICKER 等订阅类型
  - 证据引用: `for st_name in (sub_types or _configured_stock_sub_types()):
    from futu import SubType as _ST
    _sub = {
        'QUOTE': _ST.QUOTE, 'K_1M': _ST.K_1M,
        'K_15M': _ST.K_15M, 'K_30M': _ST.K_30M,
    }.get(st_name)`
  - 影响声称: 首次 subscribe 可包含 ORDER_BOOK/TICKER（_configured_active_set_sub_types），但订阅异常触发 force_reconnect 后的重试映射表不包含这些类型；重连后会静默丢失订单簿/逐笔订阅，导致微观结构因子在断线恢复后不可用。
  - 建议: 重试路径复用与首次订阅相同的完整 sub_type_map，或在 force_reconnect 后按最近一次有效订阅类型集合完整恢复；同时对每个订阅类型检查 ret 并记录失败。
- **qwen3.8-max-0902** (S3, state_machine, app/data/futu_lv3_provider.py:716-728): 订阅失败仍把标的写入已订阅集合
  - 证据引用: `ret, data = quote_ctx.subscribe(new_symbols, [sub_type], subscribe_push=True)
if ret != 0:
    logger.warning(
        f"[FutuLV3] 订阅 {st_name} 部分失败: {data}, "
        f"symbols={new_symbols}"
    )
self._subscribed_symbols.update(new_symbols)`
  - 影响声称: 即使 quote_ctx.subscribe 返回非 0，_subscribed_symbols 仍更新为已订阅；后续 get_quote 会按已订阅路径读取 get_stock_quote，watchdog 也会以有订阅作为健康判断前提，造成假订阅状态、陈旧行情或误判健康。
  - 建议: 按订阅类型/标的记录成功集合，仅把成功订阅写入 _subscribed_symbols；失败标的应重试、降级或明确标记不可用。
- **qwen3.8-max-0902** (S2, race_condition, app/data/futu_lv3_provider.py:686-728): _subscribed_symbols 读写未加锁，订阅/退订/重连可能竞态
  - 证据引用: `new_symbols = [s for s in futu_symbols if s not in self._subscribed_symbols]
self._subscribed_symbols.update(new_symbols)`
  - 影响声称: subscribe、unsubscribe_all、close 和健康守护线程都会访问/修改 _subscribed_symbols；无锁并发下可能重复订阅、漏订阅、退订后又被旧集合恢复，导致 OpenD 槽位和推送状态不一致。
  - 建议: 用同一把锁保护 _subscribed_symbols 的读取、更新、清空；或将订阅状态机收敛到单线程/队列中处理。

<!-- DEB-VERDICT cluster=16 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: futu_lv3_provider.py:728 在subscribe循环(L711-726)后无条件执行_subscribed_symbols.update(部分失败仍标记已订阅); L744-747重连重订阅映射仅含QUOTE/K_1M/K_15M/K_30M四项, 丢失ORDER_BOOK/TICKER/K_5M/K_60M/K_DAY五种类型; subscribe(L665)未持self._lock而disconnect(L1460)/close(L2030)持锁存在竞态

### 议题 #17 [共性 3/3] `app/data/futu_lv3_provider.py`:1803-1810 — 逐笔方向 int 映射两处互相矛盾: _map_ticker_direction 为 1=BUY/2=SELL, get_recent_tickers 为 2=BUY/1=SELL

环节: s2_market_data · 类别: dataflow_inconsistency, logic_anomaly, numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S3, dataflow_inconsistency, app/data/futu_lv3_provider.py:1803-1810): 逐笔方向 int 映射两处互相矛盾: _map_ticker_direction 为 1=BUY/2=SELL, get_recent_tickers 为 2=BUY/1=SELL
  - 证据引用: `L115-116 docstring "兼容历史 int 约定 (1=BUY 2=SELL 4=NEUTRAL)" 且 L122-125 `if raw in ("BUY", 1): return "BUY"` / `if raw in ("SELL", 2): return "SELL"`; 而 L1803-1810 注释 "方向映射: 2=买入, 1=卖出, 4=中性 (富途定义)" 且 `if raw_dir == 2: direction = "BUY"` / `elif raw_dir == 1: direction = "SELL"``
  - 影响声称: get_ticker_sentiment 在 push 缓存不足时 (L1933-1937) 降级用 get_recent_tickers, 两条路径对同一 int 方向给出相反 BUY/SELL → sentiment/buy_ratio/volume_weighted 符号反转, 污染 order_flow 类因子
  - 建议: get_recent_tickers 统一调用 _map_ticker_direction (SSOT), 删除内联映射
- **qwen3.8-max** (S3, numeric_unit, app/data/futu_lv3_provider.py:1803-1810): 逐笔方向映射与 push 路径的整型枚举相反
  - 证据引用: `if raw in ("BUY", 1): / return "BUY" / if raw in ("SELL", 2): / return "SELL" / # 方向映射: 2=买入, 1=卖出, 4=中性 (富途定义) / if raw_dir == 2: / direction = "BUY" / elif raw_dir == 1: / direction = "SELL"`
  - 影响声称: _map_ticker_direction 将 1 映射为 BUY、2 映射为 SELL，而 get_recent_tickers 将 2 映射为 BUY、1 映射为 SELL；当 push 缓存不足回退到 API 拉取时，买卖方向可能反转，导致 ticker sentiment / order flow 信号反向。
  - 建议: 统一 Futu TickerDirect 枚举映射，push 与拉取路径共用同一映射函数，并用真实样本验证整型/字符串枚举。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/data/futu_lv3_provider.py:1803-1810): get_recent_tickers 的成交方向映射与 push 路径相反
  - 证据引用: `raw_dir = row.get('Direction', 4)
if raw_dir == 2:
    direction = "BUY"
elif raw_dir == 1:
    direction = "SELL"`
  - 影响声称: _map_ticker_direction 明确将 1 映射为 BUY、2 映射为 SELL，而 get_recent_tickers 将 2 映射为 BUY、1 映射为 SELL；当 push ticker 缓存不足降级到 API 时，买卖方向反转，污染 ticker_buy_sell_ratio、order_flow_imbalance 等因子。
  - 建议: 统一使用 _map_ticker_direction 或核对 Futu get_recent_tickers 的 Direction 枚举后修正；为 push 与 API 路径增加同一样本的方向一致性测试。

<!-- DEB-VERDICT cluster=17 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: futu_lv3_provider.py:122-125 _map_ticker_direction(push路径)定义1=BUY/2=SELL, 而L1803-1810 get_recent_tickers(pull路径)定义2=BUY/1=SELL, 两路径对同一Futu SDK Direction字段映射互斥, 必有一路径方向反转导致buy_sell_ratio和order_flow_imbalance符号错误

### 议题 #66 [共性 3/3] `app/news_v2/sources/sec_edgar_source.py`:34-115 — watchlist_codes 被保存但从未用于过滤 SEC 公告

环节: s4_signals_sources · 类别: config_effect, dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/news_v2/sources/sec_edgar_source.py:45-115): 声称'只采集 watchlist 公司公告', 实际 watchlist_codes 存储后零使用, 拉取全市场流
  - 证据引用: `7行 docstring `100% 相关性 (只采集 watchlist 公司的公告)`; 34行 `self.watchlist_codes = [c.replace("US.", "") for c in (watchlist_codes or [])]` 在 fetch() 全文未被引用; 48行 `url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"` 全市场流; 115行 `if len(items) >= 20: break`; 103-113行构造 item 未设 related_stocks`
  - 影响声称: 全市场 SEC 公告涌入管线, watchlist 公司公告可能被 20 条截断挤出; 且条目无 related_stocks, 下游标的映射/信号生成无法关联到持仓股
  - 建议: 按 watchlist 公司名/CIK 过滤 entry 并填充 related_stocks, 或修正 docstring 与字段
- **qwen3.8-max** (S2, config_effect, app/news_v2/sources/sec_edgar_source.py:34-34): watchlist_codes 被保存但从未用于过滤 SEC 公告
  - 证据引用: `self.watchlist_codes = [c.replace("US.", "") for c in (watchlist_codes or [])]`
  - 影响声称: 代码说明称只采集 watchlist 公司公告，但实际 fetch 未过滤，可能引入大量无关新闻并影响下游信号。
  - 建议: 在 fetch 中按 watchlist_codes 过滤，或删除该误导性参数。
- **qwen3.8-max-0902** (S2, config_effect, app/news_v2/sources/sec_edgar_source.py:34-48): watchlist_codes 保存后未用于过滤 SEC 公告
  - 证据引用: `self.watchlist_codes = [c.replace("US.", "") for c in (watchlist_codes or [])]
        url = "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent"`
  - 影响声称: 声称只采 watchlist，但实际采集全量 current 公告，可能引入无关新闻。
  - 建议: 按 watchlist 过滤 entry，或明确移除 watchlist 语义。

<!-- DEB-VERDICT cluster=66 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: sec_edgar_source.py:34 self.watchlist_codes被存储但fetch():45-123全文未引用该属性,:48直接拉取SEC全市场RSS流,docstring:7声称"100%相关性(只采集watchlist公司公告)"与实际行为不符

### 议题 #59 [共性 3/3] `app/llm/prompts/debate.py`:37-53 — 降级规则要求 market_regime=DEGRADED, 但输出格式枚举不含该值

环节: s4_signals_sources · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/llm/prompts/debate.py:37-53): 降级规则要求 market_regime=DEGRADED, 但输出格式枚举不含该值
  - 证据引用: `37行 `7. market_regime 标记为 "DEGRADED" 而非 NEUTRAL`; 53行 `"market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",``
  - 影响声称: 若下游用 json_schema_guard 的 enums 按 4 值白名单校验, 数据缺失的降级场景 (恰恰最需要保留计划时) 输出被判 enum_violation → drop → None, 与'禁止交白卷'的意图自相矛盾
  - 建议: 输出格式枚举中加入 DEGRADED, 或降级规则改用 NEUTRAL+risk_warnings 标记
- **qwen3.8-max** (S2, dataflow_inconsistency, app/llm/prompts/debate.py:53-53): 降级要求输出 DEGRADED，但 schema 枚举未包含该值
  - 证据引用: `"market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",`
  - 影响声称: 若 LLM 按降级规则输出 DEGRADED，可能被下游枚举校验拒绝，导致数据缺失时计划不可用。
  - 建议: 在 market_regime 枚举中加入 DEGRADED，或明确要求降级时仍使用已有枚举值。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/llm/prompts/debate.py:37-53): Prompt 要求 DEGRADED 但输出枚举未包含
  - 证据引用: `7. market_regime 标记为 "DEGRADED" 而非 NEUTRAL
  "market_regime": "RISK_ON/RISK_OFF/ROTATION/NEUTRAL",`
  - 影响声称: 若下游按 prompt 枚举校验，DEGRADED 会被拒绝，导致降级计划解析失败。
  - 建议: 统一枚举，加入 DEGRADED，或改用 NEUTRAL 并在 notes 中说明降级。

<!-- DEB-VERDICT cluster=59 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: debate.py:37降级规则要求market_regime输出"DEGRADED",但:53 schema枚举仅含"RISK_ON/RISK_OFF/ROTATION/NEUTRAL",LLM遵从降级指令后输出会被schema校验拒绝

### 议题 #61 [共性 3/3] `app/news_v2/quality/dedup_v2.py`:43-64 — 过期记录仍参与去重判定: 清理只在 __init__ 跑一次, 查询无 expires_at 过滤

环节: s4_signals_sources · 类别: time_boundary · 自报最高 severity: S3
- **kimi-k3** (S2, time_boundary, app/news_v2/quality/dedup_v2.py:43-64): 过期记录仍参与去重判定: 清理只在 __init__ 跑一次, 查询无 expires_at 过滤
  - 证据引用: `25行 `self._cleanup_expired()` 仅在构造函数调用; 54-57行 `SELECT id, source FROM dedup_records WHERE title_hash = ?` 与 64行 `SELECT simhash, source, title FROM dedup_records` 均无 expires_at 条件`
  - 影响声称: 进程长期运行时, 超过 TTL(48h) 的记录继续参与精确+SimHash 匹配, 周期性重复标题 (如 'Fed Holds Rates Steady') 的新新闻被旧记录误判为重复而抑制; 全表扫描行数也只增不减
  - 建议: 两条 SELECT 加 `AND expires_at > ?`, 并在 is_duplicate/mark_as_seen 中周期性触发清理
- **qwen3.8-max** (S3, time_boundary, app/news_v2/quality/dedup_v2.py:64-64): SimHash 去重扫描未过滤 expires_at，过期记录仍会命中
  - 证据引用: `cursor = conn.execute("SELECT simhash, source, title FROM dedup_records")`
  - 影响声称: TTL 过期后记录仍可能参与去重，导致本应重新生效的新闻被错误压制，影响新闻时效与信号生成。
  - 建议: 查询时增加 WHERE expires_at > ?，并定期清理过期记录。
- **qwen3.8-max-0902** (S3, time_boundary, app/news_v2/quality/dedup_v2.py:43-64): 过期去重记录未在查询时过滤
  - 证据引用: `conn.execute("DELETE FROM dedup_records WHERE expires_at < ?", (time.time(),))
                cursor = conn.execute("SELECT simhash, source, title FROM dedup_records")`
  - 影响声称: 清理只在初始化执行，查询不过滤 expires_at，过期记录会长期导致新闻误判为重复。
  - 建议: 查询加 WHERE expires_at > now，并增加周期性清理。

<!-- DEB-VERDICT cluster=61 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: dedup_v2.py:54-57精确去重SELECT和:64 SimHash全表扫描SELECT均无expires_at过滤条件,_cleanup_expired():25仅在__init__调用一次,过期记录持续参与去重判定可导致误判

### 议题 #46 [共性 3/3] `app/factors/capital_flow.py`:74-102 — 只校验 close 长度，未校验 volume 窗口长度

环节: s4_signals_sources · 类别: dataflow_inconsistency, numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S3, numeric_unit, app/factors/capital_flow.py:100-102): 均量窗口边界 bug: len(volumes)≤20 时 avg_vol 回退为 1(股), vol_ratio 虚增数百万倍
  - 证据引用: `100行 `avg_vol = sum(volumes[1:21]) / 20 if len(volumes) > 20 else 1`; 102行 `vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0`; 74行仅要求 `len(closes) < self.corr_window + 2` (即≥7) 就放行`
  - 影响声称: 恰好 20 条或 7-20 条量数据时 avg_vol=1, vol_ratio=curr_vol/1 达数百万, 119行 `vol_ratio >= self.vol_surge`(2.0) 恒真 → 新股/短历史标的永远报'放量', 评分与 reason ('Vol=5000000.0x') 严重失真, 资金流门形同虚设
  - 建议: else 分支改为 `sum(volumes[1:])/max(len(volumes)-1,1)`, 或数据不足 21 条时直接返回 Insufficient Data
- **qwen3.8-max** (S3, numeric_unit, app/factors/capital_flow.py:100-102): 成交量均值不足时回退为 1，会制造虚假放量倍数
  - 证据引用: `avg_vol = sum(volumes[1:21]) / 20 if len(volumes) > 20 else 1
            curr_vol = volumes[0]
            vol_ratio = curr_vol / avg_vol if avg_vol > 0 else 0`
  - 影响声称: 当历史量不足 21 根时，正常成交量除以 1 会得到巨大 vol_ratio，误触发放量/资金流确认。
  - 建议: 历史不足时返回失败或用可用样本均值，而不是常数 1。
- **qwen3.8-max** (S2, dataflow_inconsistency, app/factors/capital_flow.py:92-92): 只校验 close 长度，未校验 volume 长度即按下标取量
  - 证据引用: `vc = window_vols[i] # 直接用 Volume 绝对值，或者相对于均量的比`
  - 影响声称: 若 volume 序列比 close 短，会触发 IndexError；虽然被外层捕获，但会造成门控异常与不稳定结果。
  - 建议: 同时校验 len(volumes) >= self.corr_window + 1，否则返回 Insufficient Data。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/factors/capital_flow.py:74-95): 只校验 close 长度，未校验 volume 窗口长度
  - 证据引用: `if len(closes) < self.corr_window + 2:
                return GateResult(name="CapitalFlowMonitor", passed=False, score=0.0, reason="Insufficient Data")
            window_vols = volumes[:self.corr_window+1]
                vc = window_vols[i]`
  - 影响声称: 若 volume 数组短于 close 数组，窗口索引会异常；异常被捕获后门直接失败。
  - 建议: 同时校验 len(volumes) >= self.corr_window + 1，否则返回数据不足。
- **qwen3.8-max-0902** (S3, numeric_unit, app/factors/capital_flow.py:100-100): 历史量不足时平均量硬编码为 1
  - 证据引用: `avg_vol = sum(volumes[1:21]) / 20 if len(volumes) > 20 else 1`
  - 影响声称: 当样本不足时，vol_ratio = curr_vol / 1，可能把正常成交量误判为巨量激增。
  - 建议: 样本不足时返回数据不足，或用实际可用均值并标记低置信。

<!-- DEB-VERDICT cluster=46 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: capital_flow.py:74仅校验len(closes)未校验len(volumes),:83窗口切片长度错配导致:92循环越界索引volumes[i]可能IndexError;:100均量不足时fallback=1(股)使vol_ratio虚增数百万倍

### 议题 #51 [共性 3/3] `app/factors/regime_filter.py`:79-91 — spy_trend_ma 配置未参与计算，代码固定读取 ma50

环节: s4_signals_sources · 类别: config_effect, numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S2, numeric_unit, app/factors/regime_filter.py:88-91): VIX 评分无上界钳位, VIX<15 时 score 超过 1.0
  - 证据引用: `90行 `score = max(0.5, 1.0 - (vix - 15.0) / 30.0)`; 66-68行注释明确 VIX 可能为负数; trade_quality_gate.py 23行约定 `score: float  # 0.0 - 1.0 质量分``
  - 影响声称: VIX=10 → score=1.167; 负数 VIX (脏数据) → score 更大; regime 门在加权总分 (权重0.15) 中贡献超标, 低 VIX/脏数据时抬高 opportunity_score; 且 89 行注释 'VIX 20 -> 1.0, VIX 30 -> 0.0' 与公式实际映射 (0.83/0.5) 不符
  - 建议: 改为 `score = min(1.0, max(0.5, 1.0 - (vix - 15.0) / 30.0))` 并修正注释
- **qwen3.8-max** (S3, numeric_unit, app/factors/regime_filter.py:90-90): RegimeFilter 分数只设下限未设上限，可能超过 1
  - 证据引用: `score = max(0.5, 1.0 - (vix - 15.0) / 30.0)`
  - 影响声称: 当 VIX 很低或为负时，score 可大于 1（如 VIX=0 时得到 1.5），使质量门控加权失真并可能放大通过分。
  - 建议: 对 score 做 [0.0, 1.0] 钳位，并在计算前清洗负数/异常 VIX。
- **qwen3.8-max** (S2, config_effect, app/factors/regime_filter.py:79-79): spy_trend_ma 配置未参与计算，代码固定读取 ma50
  - 证据引用: `spy_ma = spy_ctx.get('ma50', 0.0) # 假设外部已计算或提供`
  - 影响声称: 修改 spy_trend_ma 只影响提示文案，不影响实际趋势判断，导致配置不可信。
  - 建议: 按 self.spy_trend_ma 获取或计算对应 MA，再用于趋势判断。
- **qwen3.8-max-0902** (S2, numeric_unit, app/factors/regime_filter.py:90-90): VIX 很低时 score 可超过 1
  - 证据引用: `score = max(0.5, 1.0 - (vix - 15.0) / 30.0)`
  - 影响声称: score 未做上限钳位，VIX=0 时可达约 1.5，会放大质量门总分。
  - 建议: 使用 min(1.0, max(0.0, score)) 将分数钳位到 [0,1]。

<!-- DEB-VERDICT cluster=51 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: regime_filter.py:43读取self.spy_trend_ma配置但:79硬编码spy_ctx.get('ma50')从未引用该配置;:90 score=max(0.5,1.0-(vix-15)/30)当VIX<0时score>1.0无上界钳位违反:24行score 0-1约定

### 议题 #49 [共性 3/3] `app/factors/leader_pool.py`:80-97 — 相对强度 RS 用带符号涨跌幅相除, 异号/同负时龙头判定方向反转

环节: s4_signals_sources · 类别: config_effect, logic_anomaly, numeric_unit · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, app/factors/leader_pool.py:80-92): 相对强度 RS 用带符号涨跌幅相除, 异号/同负时龙头判定方向反转
  - 证据引用: `81行 `spy_chg = (spy_price - spy_prev) / spy_prev`; 88行 `stock_chg = (closes[0] - closes[1]) / closes[1]`; 92行 `rs = stock_chg / spy_chg if abs(spy_chg) > 0.001 else 1.0`; 107行 `elif rs > 1.5:` → '强劲龙头' score=1.0; 117行 `elif rs < 0.5:` → '显著弱于大盘' passed=False`
  - 影响声称: 个股+1%/SPY-2% → rs=-0.5 → 相对强势标的被误判'显著弱于大盘'并 passed=False 硬拦; 个股-2%/SPY-0.5% → rs=4 → 跌得更狠的标的反被判'强劲龙头' score=1.0 放行; 另注释声称'过去20天'涨幅, 实际只用 1 日涨幅
  - 建议: 改用超额收益 stock_chg - spy_chg 或 (1+stock_chg)/(1+spy_chg) 比值, 并按注释实现 20 日窗口
- **qwen3.8-max** (S3, numeric_unit, app/factors/leader_pool.py:88-88): 前一收盘价可能为 0 导致除零异常
  - 证据引用: `stock_chg = (closes[0] - closes[1]) / closes[1]`
  - 影响声称: 若 closes[1] 为 0 或脏数据，会抛异常；外层异常处理会返回 passed=True、score=0.5，导致坏数据下仍可能通过评估。
  - 建议: 在除法前检查 closes[1] > 0，否则返回 Insufficient Data 或中性失败结果。
- **qwen3.8-max** (S2, config_effect, app/factors/leader_pool.py:96-97): 衰竭均线窗口硬编码为 5，配置项 ma_exhaustion 未生效
  - 证据引用: `ma5 = sum(closes[:5]) / 5 if len(closes) >= 5 else current_price
            is_exhausted = current_price < ma5`
  - 影响声称: exhaustion_ma_days 配置只会影响日志/文案，不会改变实际衰竭判定窗口，造成配置与行为不一致。
  - 建议: 使用 self.ma_exhaustion 计算对应均线，而不是固定 closes[:5]。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/factors/leader_pool.py:87-92): RS 用当日涨幅相除，大盘下跌时个股上涨会得到负相对强度
  - 证据引用: `if len(closes) >= 2:
                stock_chg = (closes[0] - closes[1]) / closes[1]
            else:
                stock_chg = 0.0

            rs = stock_chg / spy_chg if abs(spy_chg) > 0.001 else 1.0`
  - 影响声称: 当 SPY 当日下跌而个股上涨时，rs 会为负并可能落入 rs<0.5 的失败分支，导致强势股被误判为弱势。
  - 建议: 改用超额收益或排名型相对强度，例如 stock_chg - spy_chg，或在分母为负时做方向校正。
- **qwen3.8-max-0902** (S2, numeric_unit, app/factors/leader_pool.py:88-88): 个股前收为 0 时存在除零风险
  - 证据引用: `stock_chg = (closes[0] - closes[1]) / closes[1]`
  - 影响声称: 若历史数据异常使 closes[1]=0，会抛出异常并进入异常兜底，可能给出中性通过分。
  - 建议: 检查 closes[1] > 0 后再计算，或使用安全除零并返回数据不足。

<!-- DEB-VERDICT cluster=49 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: leader_pool.py:92 rs=stock_chg/spy_chg使用带符号涨跌幅相除,两者同负时rs为正可误判为强劲龙头(:107 rs>1.5);:88 closes[1]=0时除零异常被:129外层catch但整函数结果作废;:96 MA窗口硬编码5未使用:46 self.ma_exhaustion配置

### 议题 #50 [共性 3/3] `app/factors/mfff.py`:113-138 — threshold 参数不影响 score/direction, 且默认值 1.0 使 above_threshold 几乎恒 False

环节: s4_signals_sources · 类别: config_effect · 自报最高 severity: S2
- **kimi-k3** (S2, config_effect, app/factors/mfff.py:113-138): threshold 参数不影响 score/direction, 且默认值 1.0 使 above_threshold 几乎恒 False
  - 证据引用: `114/117行方向判定硬编码 `if raw_score >= 0.3:` / `elif raw_score <= -0.3:`; 138行 `"above_threshold": abs(raw_score) >= threshold,` 为唯一用途; 权重和为 1.0 故 |raw_score|≤1, 默认 threshold=1.0`
  - 影响声称: param_space 扫描 threshold [0.5,1.0,1.5,2.0] 对因子输出零影响; metadata 中 above_threshold 在默认参数下恒 False, 下游若消费该字段会得到死信号
  - 建议: 将 threshold 接入方向/置信度判定, 或从 param_space 移除
- **qwen3.8-max** (S2, config_effect, app/factors/mfff.py:114-122): MFFF 的 threshold 参数未参与方向判定
  - 证据引用: `if raw_score >= 0.3:
            direction = "LONG"
            confidence = min(1.0, abs(raw_score))
        elif raw_score <= -0.3:
            direction = "SHORT"
            confidence = min(1.0, abs(raw_score))
        else:
            direction = "NEUTRAL"
            confidence = 1.0 - abs(r`
  - 影响声称: threshold 只出现在 metadata，不影响信号方向，参数扫描/调参会产生误导性结论。
  - 建议: 用 threshold 替代固定 0.3 阈值，或从参数空间移除该字段。
- **qwen3.8-max-0902** (S2, config_effect, app/factors/mfff.py:114-122): MFFF threshold 不参与方向判断
  - 证据引用: `if raw_score >= 0.3:
            direction = "LONG"
            confidence = min(1.0, abs(raw_score))
        elif raw_score <= -0.3:
            direction = "SHORT"
            confidence = min(1.0, abs(raw_score))
        else:
            direction = "NEUTRAL"
            confidence = 1.0 - abs(r`
  - 影响声称: threshold 仅出现在 metadata，调参不影响信号生成。
  - 建议: 将 threshold 用于 above_threshold 过滤或方向阈值。

<!-- DEB-VERDICT cluster=50 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: mfff.py:97 threshold被读取但:114-122方向判定使用硬编码0.3阈值完全不参与;默认threshold=1.0且权重和=1.0故|raw_score|≤1,metadata:138 above_threshold=abs(raw_score)>=1.0几乎恒False

### 议题 #52 [共性 3/3] `app/factors/sss.py`:136-155 — SSS 与 MFFF 的 score 极性语义相反, 同注册表内聚合会相互抵消

环节: s4_signals_sources · 类别: config_effect, dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, config_effect, app/factors/sss.py:136-155): threshold 参数被扫描/存储但完全不参与计算
  - 证据引用: `53行 param_space 含 `"threshold": [-2, -1.5, -1, -0.5, 0, 0.5, 1]`; 61行默认 `"threshold": -1.0`; 方向判定 138/141行用硬编码 `if raw_score <= -0.5:` / `elif raw_score >= 0.5:`; threshold 仅出现在 155行 params_used`
  - 影响声称: 参数扫描框架对 threshold 的 7 个取值产生完全相同的输出, 扫描结果误导优化决策; 配置调 threshold 无任何效果
  - 建议: 用 `self.params.get('threshold')` 替换硬编码 ±0.5, 或将 threshold 移出 param_space
- **kimi-k3** (S2, dataflow_inconsistency, app/factors/sss.py:136-146): SSS 与 MFFF 的 score 极性语义相反, 同注册表内聚合会相互抵消
  - 证据引用: `sss.py 138-139行 `if raw_score <= -0.5: direction = "LONG"   # 超卖 → 做多反转` (score 负=看多); mfff.py 114-115行 `if raw_score >= 0.3: direction = "LONG"` (score 正=看多); 两因子同经 factors/__init__.py 29-36行注册进同一 FactorRegistry`
  - 影响声称: 下游若遍历注册表对 FactorValue.score 求和/平均, 两个同向看多信号 (SSS=-1, MFFF=+1) 会抵消为 0; 只有 direction_hint 字段语义一致, score 字段跨因子不可比
  - 建议: 统一约定 score 正=看多 (SSS 取反输出), 或在 BaseFactor 文档/类型上强制下游只用 direction_hint
- **qwen3.8-max** (S2, config_effect, app/factors/sss.py:138-146): SSS 的 threshold 参数未参与方向判定
  - 证据引用: `if raw_score <= -0.5:
            direction = "LONG"   # 超卖 → 做多反转
            confidence = min(1.0, abs(raw_score))
        elif raw_score >= 0.5:
            direction = "SHORT"  # 超买 → 做空反转
            confidence = min(1.0, abs(raw_score))
        else:
            direction = "NEUTRAL"
         `
  - 影响声称: threshold 仅被记录到 params_used，不会改变方向提示，导致参数配置无效。
  - 建议: 将 threshold 用于 direction_hint 判定，或删除该参数避免误用。
- **qwen3.8-max-0902** (S2, config_effect, app/factors/sss.py:138-146): threshold 参数只写入 metadata，不参与方向判断
  - 证据引用: `if raw_score <= -0.5:
            direction = "LONG"   # 超卖 → 做多反转
            confidence = min(1.0, abs(raw_score))
        elif raw_score >= 0.5:
            direction = "SHORT"  # 超买 → 做空反转
            confidence = min(1.0, abs(raw_score))
        else:
            direction = "NEUTRAL"
         `
  - 影响声称: 参数空间中的 threshold 调优不会改变信号阈值，造成配置假象。
  - 建议: 用 self.params.get('threshold') 驱动方向/过滤，或删除该参数。

<!-- DEB-VERDICT cluster=52 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: sss.py:138 raw_score<=-0.5→LONG(负=看多)与mfff.py:114 raw_score>=0.3→LONG(正=看多)极性相反,两者同经factors/__init__.py:29-36注册进同一FactorRegistry,简单聚合时信号相互抵消;threshold参数:155仅写入params_used不参与:138-146方向判定

### 议题 #55 [共性 3/3] `app/llm/agent_bridge.py`:288-790 — 读取 core_principles 但默认记忆键是 core_principle

环节: s4_signals_sources · 类别: dataflow_inconsistency, error_swallowing, logic_anomaly, state_machine, time_boundary · 自报最高 severity: S3
- **kimi-k3** (S3, error_swallowing, app/llm/agent_bridge.py:779-790): 净期望统计用列名下标访问普通 tuple, 必抛 TypeError 并被静默吞掉, 该 prompt 行永不显示
  - 证据引用: `779行 `_conn = sqlite3.connect(_trade_db, timeout=3)` (未设 row_factory, 对比本文件63行 AgentRequestQueue._conn 有 `conn.row_factory = sqlite3.Row`); 790行 `_n, _pnl = int(_row["n"]), float(_row["pnl"])`; 799行 `except Exception as _perf_e:` 仅 debug 日志`
  - 影响声称: fetchone() 返回 tuple, `_row["n"]` 必抛 TypeError → 被 799 行吞掉 → 2026-08-25 上线的'美股净期望(费用后, 近30日)'行 100% 不显示, 且 debug 级日志难以察觉, 功能死透但无人知晓
  - 建议: 连接后加 `_conn.row_factory = sqlite3.Row`, 或改用 `_row[0]/_row[1]` 位置下标
- **kimi-k3** (S3, state_machine, app/llm/agent_bridge.py:652-699): 单次响应超时后 _hermes_available=False 永久锁死, 进程内无任何恢复路径
  - 证据引用: `652-654行 `if not self._hermes_available: ... return self._fallback_response(request_id)`; 699行 `self._hermes_available = False`; 唯一置回 True 的 675 行 `self._hermes_available = True` 位于成功读取分支, 但 652 行 early-return 使其永不可达`
  - 影响声称: 任意一次 300s 超时后, 本进程内所有后续 wait_for_response 直接走 fallback, 盘前计划/盘后归因静默丢失直到重启; 且解析失败的毒响应文件不删除, 循环空转 300s 后才触发上述锁死
  - 建议: 将不可用标记改为带 TTL 的冷却 (如 30 分钟后自动重试), 或在每次调用前做一次轻量探测恢复
- **kimi-k3** (S2, dataflow_inconsistency, app/llm/agent_bridge.py:760-760): 记忆键名不匹配: 存储用 core_principle(单数), 读取用 core_principles(复数)
  - 证据引用: `288行 `"core_principle": "稳定赚钱 > 激进收益",` (字符串); 760行 `f"核心原则: {', '.join(self._memory.get('core_principles', [])[:3])}",``
  - 影响声称: get('core_principles', []) 永远命中默认值 [], 盘前/盘后 prompt 中'核心原则'永远为空串, LLM 看不到核心原则, 完全静默
  - 建议: 统一键名为 core_principle 并按字符串处理, 或存储侧改为列表
- **qwen3.8-max** (S2, logic_anomaly, app/llm/agent_bridge.py:790-790): 未设置 row_factory 却按列名访问 SQLite 结果
  - 证据引用: `_n, _pnl = int(_row["n"]), float(_row["pnl"])`
  - 影响声称: 默认返回 tuple，_row["n"] 会抛 TypeError；异常被吞掉后，净期望统计永远不会注入 LLM 上下文。
  - 建议: 设置 conn.row_factory = sqlite3.Row，或改用元组索引访问。
- **qwen3.8-max** (S1, dataflow_inconsistency, app/llm/agent_bridge.py:760-760): 读取 core_principles 但默认记忆字段是 core_principle
  - 证据引用: `f"核心原则: {', '.join(self._memory.get('core_principles', [])[:3])}",`
  - 影响声称: 默认记忆中的核心原则不会被注入 prompt，导致上下文缺失。
  - 建议: 统一记忆 schema，读取 core_principle 或将其规范为 core_principles 列表。
- **qwen3.8-max** (S2, error_swallowing, app/llm/agent_bridge.py:694-696): LLM/Hermes 响应解析失败时坏文件不被清理，只等到超时
  - 证据引用: `except Exception as e:
                    logger.warning(f"[Agent-US] 读取响应失败: {e}")
            time.sleep(2)`
  - 影响声称: 非法 JSON 会反复读取失败并拖到超时才降级，可能造成计划生成延迟或后续读到过期/损坏响应。
  - 建议: 解析失败时隔离或删除该响应文件，并尽快进入 fallback。
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/llm/agent_bridge.py:288-760): 读取 core_principles 但默认记忆键是 core_principle
  - 证据引用: `"core_principle": "稳定赚钱 > 激进收益",
            f"核心原则: {', '.join(self._memory.get('core_principles', [])[:3])}",`
  - 影响声称: 核心原则不会进入 prompt，降低记忆注入一致性。
  - 建议: 统一键名，或同时读取 core_principle/core_principles。
- **qwen3.8-max-0902** (S3, error_swallowing, app/llm/agent_bridge.py:779-790): SQLite 未设置 Row 却按字典访问查询结果
  - 证据引用: `_conn = sqlite3.connect(_trade_db, timeout=3)
            _row = _conn.execute(
            _n, _pnl = int(_row["n"]), float(_row["pnl"])`
  - 影响声称: 默认 fetchone 返回 tuple，按字典访问会异常；异常被静默吞掉，净期望统计永远不可用。
  - 建议: 设置 conn.row_factory = sqlite3.Row，或按 tuple 索引访问。
- **qwen3.8-max-0902** (S3, time_boundary, app/llm/agent_bridge.py:664-696): 响应文件 JSON 损坏时只重试读取直到超时
  - 证据引用: `response = json.loads(cleaned_text)
                except Exception as e:
                    logger.warning(f"[Agent-US] 读取响应失败: {e}")
            time.sleep(2)`
  - 影响声称: 若响应文件最终仍为损坏 JSON，会阻塞到超时才降级，延迟盘前计划或 EOD 流程。
  - 建议: 对同一响应文件设置最大解析失败次数，失败后删除/隔离并提前 fallback。
- **qwen3.8-max-0902** (S2, state_machine, app/llm/agent_bridge.py:655-679): wait_for_response 成功读取后不删除响应文件
  - 证据引用: `response_file = os.path.join(RESPONSES_DIR, f"{request_id}.json")
                    request_file = os.path.join(REQUESTS_DIR, f"{request_id}.json")
                    if os.path.exists(request_file):
                        os.remove(request_file)`
  - 影响声称: 响应文件残留可能被后续轮询重复读取，造成重复记忆更新或重复计划。
  - 建议: 成功处理后删除或归档 response_file。

<!-- DEB-VERDICT cluster=55 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: agent_bridge.py:779 sqlite3.connect未设row_factory,::790 _row["n"]对tuple按下标访问必抛TypeError被:799静默吞掉,净期望行永不显示;::288默认记忆键core_principle(单数/字符串)与:760读取core_principles(复数/列表)不匹配致核心原则在prompt中恒为空

### 议题 #96 [共性 3/3] `app/strategy/pure/voting_llm.py`:61-71 — std_dev 惩罚施加在 min_confidence 门之后 — 输出置信度可跌破门槛

环节: s6_playbook_engine · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S2, logic_anomaly, app/strategy/pure/voting_llm.py:61-71): std_dev 惩罚施加在 min_confidence 门之后 — 输出置信度可跌破门槛
  - 证据引用: `61-64: confidence = float(vote.get("final_confidence", 0.5)) / min_conf = config.get("min_confidence", 0.70) / if confidence < min_conf: return None; 70-71: if std_dev > 20: confidence *= 0.85`
  - 影响声称: confidence=0.70 且 std_dev>20 的投票先过门再被罚到 0.595 仍产出 SignalCandidate (82 行 round(confidence,4)) → docstring 宣称的 min_confidence=0.70 fail-safe 对最终输出不成立。
  - 建议: 先做 std_dev 惩罚再判 min_confidence, 或在惩罚后复检 confidence < min_conf → return None。
- **qwen3.8-max** (S3, logic_anomaly, app/strategy/pure/voting_llm.py:61-71): 置信度门槛在高分歧惩罚前执行，惩罚后可低于 min_confidence 仍返回候选
  - 证据引用: `    confidence = float(vote.get("final_confidence", 0.5))
    min_conf = config.get("min_confidence", 0.70)
    if confidence < min_conf:
        return None

    strength = int(vote.get("final_strength", 50))
    std_dev = float(vote.get("std_dev", 0))

    # 高分歧时降低置信度
    if std_dev > 20:
        `
  - 影响声称: 当原始 confidence 略高于 min_confidence 且 std_dev>20 时，返回的 SignalCandidate.confidence 可能已经低于 min_confidence，配置门槛被绕过，弱共识信号仍可进入下游。
  - 建议: 将 confidence *= 0.85 移到 min_confidence 检查之前，或在惩罚后再次判断 confidence < min_conf 并返回 None。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/strategy/pure/voting_llm.py:61-71): min_confidence 门槛在高分歧惩罚之前执行，惩罚后置信度可能低于门槛
  - 证据引用: `confidence = float(vote.get("final_confidence", 0.5))
    min_conf = config.get("min_confidence", 0.70)
    if confidence < min_conf:
        return None
    if std_dev > 20:
        confidence *= 0.85`
  - 影响声称: 若原始 confidence 刚好高于 0.70 且 std_dev>20，惩罚后可能低于 0.70 但仍返回 SignalCandidate，破坏最低置信度门，下游可能消费低置信信号。
  - 建议: 先应用 std_dev 惩罚，再执行 `if confidence < min_conf: return None`。

<!-- DEB-VERDICT cluster=96 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: voting_llm.py:63 min_confidence 门控在 70-71 行 std_dev>20 的 0.85 惩罚之前执行，惩罚后 confidence 可跌破 min_conf 仍返回 SignalCandidate

### 议题 #94 [共性 3/3] `app/strategy/pure/fast_lane.py`:81-93 — fast_lane 用 plan 真置信度过门却硬编 confidence=0.85, strength 又用真值 — 同一候选两个口径

环节: s6_playbook_engine · 类别: dataflow_inconsistency, logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S2, logic_anomaly, app/strategy/pure/fast_lane.py:81-93): fast_lane 用 plan 真置信度过门却硬编 confidence=0.85, strength 又用真值 — 同一候选两个口径
  - 证据引用: `81: confidence = plan_match.get("confidence", 0.85) / 82: if confidence < config.get("min_confidence", 0.70): / 92: strength=int(float(confidence) * 100), / 93: confidence=0.85,`
  - 影响声称: plan 置信度 0.70 的擦线信号过门后对外宣称 0.85, 而 strength=70 暴露真值 → 下游仲裁 (base_adapter.py 71 行所述 priority × confidence) 被虚高置信度加权, 且候选对象内部字段自相矛盾。
  - 建议: 透传过门真值: confidence=round(float(confidence), 4), 删除硬编 0.85。
- **qwen3.8-max** (S2, dataflow_inconsistency, app/strategy/pure/fast_lane.py:81-93): fast_lane 用 plan confidence 做门槛但最终输出固定 0.85，可能放大置信度
  - 证据引用: `    confidence = plan_match.get("confidence", 0.85)
    if confidence < config.get("min_confidence", 0.70):
        ctx.metadata["fast_lane_skip"] = {
            "reason": "CONFIDENCE_BELOW_MIN",
            "detail": f"{confidence} < {config.get('min_confidence', 0.70)}",
        }
        return `
  - 影响声称: 若 plan_match.confidence 仅为 0.70~0.84，信号仍会把 0.85 传给下游，造成置信度语义漂移，可能影响后续按 confidence 做风控/仲裁的逻辑。
  - 建议: 返回时使用原始 confidence（可做 clamp），例如 confidence=round(float(confidence), 4)，避免门槛值与输出值不一致。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/strategy/pure/fast_lane.py:81-93): fast_lane 用 plan confidence 做门槛和 strength，但最终输出固定 confidence=0.85
  - 证据引用: `confidence = plan_match.get("confidence", 0.85)
        if confidence < config.get("min_confidence", 0.70):
            return None
        confidence=0.85,`
  - 影响声称: 上游 plan_match 的真实置信度被门槛与 strength 消费后，在输出 SignalCandidate 时被固定值 0.85 覆盖；低于 0.85 的计划会被抬高，高于 0.85 的计划会被压低，导致下游置信度门/仲裁权重失真。
  - 建议: 将 `confidence=0.85` 改为 `confidence=round(confidence, 4)`，或明确固定值语义并同步 strength/下游门。

<!-- DEB-VERDICT cluster=94 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: fast_lane.py:92 strength 用真实 confidence 计算但 93 行输出硬编 confidence=0.85，下游消费者看到 strength 与 confidence 口径不一致（如 plan_conf=0.72 → strength=72 但 confidence=0.85）

### 议题 #89 [共性 3/3] `app/playbook/playbook_service.py`:688-691 — 方向审计读路径硬编码仓库根, 写路径走 config(CWD 相对) — 配置改路径或 CWD 变化即读写分离

环节: s6_playbook_engine · 类别: config_effect, dataflow_inconsistency · 自报最高 severity: S2
- **kimi-k3** (S2, dataflow_inconsistency, app/playbook/playbook_service.py:688-690): 方向审计读路径硬编码仓库根, 写路径走 config(CWD 相对) — 配置改路径或 CWD 变化即读写分离
  - 证据引用: `playbook_service.py 689-690: root = Path(__file__).resolve().parent.parent.parent / log_path = root / "data" / "shadow" / "playbook_direction_audit.jsonl"; pipeline.py 465: log_path = str(_cfg.get_dotted("playbook.technical_arbitration.shadow_log_path", "data/shadow/playbook_direction_audit.jsonl"))`
  - 影响声称: 写入侧路径可被 config 覆盖且相对 CWD 解析, 读取侧忽略该 config 键并锚定仓库根: 一旦配置项被设置或进程 CWD 非仓库根, 两侧不是同一文件 → Dashboard 审计列静默恒空, 无任何报错。
  - 建议: 读取侧改用同一 config 键 playbook.technical_arbitration.shadow_log_path 并采用与写入侧一致的路径基准 (或两侧统一用 __file__ 锚定仓库根)。
- **qwen3.8-max** (S2, config_effect, app/playbook/playbook_service.py:688-690): 方向审计读取路径硬编码，未消费 pipeline 写入的可配置路径
  - 证据引用: `            from pathlib import Path
            root = Path(__file__).resolve().parent.parent.parent
            log_path = root / "data" / "shadow" / "playbook_direction_audit.jsonl"`
  - 影响声称: pipeline._log_playbook_direction_audit 的写入路径可由 playbook.technical_arbitration.shadow_log_path 配置，但这里固定读取默认根目录文件；若配置改路径，Dashboard 的 audit_map 将读不到数据。
  - 建议: 读取同一配置项 playbook.technical_arbitration.shadow_log_path，并与写入侧统一解析相对路径。
- **qwen3.8-max-0902** (S2, config_effect, app/playbook/playbook_service.py:689-691): 方向审计读取路径硬编码，未跟随写入侧可配置的 shadow_log_path
  - 证据引用: `root = Path(__file__).resolve().parent.parent.parent
            log_path = root / "data" / "shadow" / "playbook_direction_audit.jsonl"
            if not log_path.exists():`
  - 影响声称: 写入侧 `_log_playbook_direction_audit` 使用 `playbook.technical_arbitration.shadow_log_path` 配置路径；读取侧固定读取默认路径。若配置改路径，Dashboard/漏斗审计会读不到真实审计文件，产生展示与决策链不一致。
  - 建议: 读取侧同样通过配置读取 `playbook.technical_arbitration.shadow_log_path`，或把路径提取为共享常量/函数保证写读同源。

<!-- DEB-VERDICT cluster=89 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: playbook_service.py:689-690 读方向审计硬编 Path(__file__) 推导的仓库根路径，pipeline.py:465 写侧走 config shadow_log_path 可配置路径，配置自定义时读写分离

### 议题 #90 [共性 3/3] `app/playbook/playbook_service.py`:879-884 — custom_params 部分覆盖把未指定的 entry/SL/TP/position_size 全部置 None

环节: s6_playbook_engine · 类别: logic_anomaly · 自报最高 severity: S4
- **kimi-k3** (S4, logic_anomaly, app/playbook/playbook_service.py:879-882): custom_params 部分覆盖把未指定的 entry/SL/TP/position_size 全部置 None
  - 证据引用: `879: entry_price = custom_params.get("entry_price") if custom_params else inst.get("entry_price", 0) / 880: stop_loss = custom_params.get("stop_loss") if custom_params else ... / 882: position_size = custom_params.get("position_size") if custom_params else ...; docstring 737: "custom_params: 用户自定义参数`
  - 影响声称: 只要 custom_params 是非空 dict 但缺键 (docstring 自带用例『如修改止损』只改 stop_loss; 或 pipeline 只携带 archetype_route), entry_price/take_profit 变 None、position_size 被 883-884 行兜成 1 股 → 以 1 股、缺失 SL/TP 锚的参数进入 _execute_via_d
  - 建议: 逐字段回退而非整体切换: merged = {k: inst.get(k, 0) for k in ("entry_price","stop_loss","take_profit","position_size")}; merged.update(custom_params or {}), 再取 entry_price=merged["entry_price"] 等。
- **qwen3.8-max** (S3, logic_anomaly, app/playbook/playbook_service.py:882-884): position_size 为 0/缺失时强制改为 1，可能绕过零仓位意图
  - 证据引用: `        position_size = custom_params.get("position_size") if custom_params else inst.get("position_size", 0)
        if not position_size or position_size <= 0:
            position_size = 1`
  - 影响声称: 若上游风控/sizing 输出 0 表示不开仓，这里仍会最小化为 1 股并继续走下单流程，可能在本应零仓位时产生真实订单。
  - 建议: 区分“缺失需要兜底”与“显式为 0 表示拒绝”；若 position_size <= 0 应返回失败或跳过，除非有明确 min_share 配置。
- **qwen3.8-max-0902** (S4, logic_anomaly, app/playbook/playbook_service.py:879-882): custom_params 非空但缺键时会把指令原始参数覆盖为 None/1
  - 证据引用: `entry_price = custom_params.get("entry_price") if custom_params else inst.get("entry_price", 0)
        stop_loss = custom_params.get("stop_loss") if custom_params else inst.get("stop_loss", 0)
        take_profit = custom_params.get("take_profit") if custom_params else inst.get("take_profit", 0)
  `
  - 影响声称: 只要传入任意非空 custom_params（例如只修改止损），未携带的 entry_price/stop_loss/take_profit 会变成 None；position_size 还会因后续 `if not position_size` 兜底成 1，导致下单参数与指令不一致，可能产生错误订单或风控参数缺失。
  - 建议: 改为逐键回退：`custom_params.get('entry_price', inst.get('entry_price', 0)) if custom_params else inst.get('entry_price', 0)`，stop_loss/take_profit/position_size 同理。

<!-- DEB-VERDICT cluster=90 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: playbook_service.py:879-882 当 custom_params 为非空 dict 但缺 entry_price/stop_loss/take_profit/position_size 键时，.get() 返回 None 覆盖指令原始值，导致下单参数丢失

### 议题 #91 [共性 3/3] `app/playbook/playbook_service.py`:2010-2057 — 路由限价单进入 FAILED/CANCELLED 等终态时未检查 _dealt，可能丢弃部分成交

环节: s6_playbook_engine · 类别: race_condition, state_machine · 自报最高 severity: S4
- **kimi-k3** (S3, race_condition, app/playbook/playbook_service.py:2032-2057): 路由限价单撤单失败仍『按放弃处理』— 撤单后成交产生无登记孤儿仓
  - 证据引用: `2032-2034: _cancel_res = _dual.cancel_order(str(futu_order_id), target_env) / if not _cancel_res.get("success"): logger.warning(f"[archetype路由] {symbol} 撤单失败 (继续按放弃处理): ..."); 2055-2057: return _skip_terminal("archetype_ttl_expired", f"...已撤单放弃 (绝不市价追)")`
  - 影响声称: 撤单因 API 异常/竞态失败且订单在 2038 行最后一次复查之后才成交时, broker 侧有真实持仓, 但指令已落 skip_reason 终态、无 trade_records 行 → EOD 兜底 (pipeline.py 8788-8794 只 SELECT trade_records) 与盘中退出监控均看不到该仓, 持仓无 SL 守护。
  - 建议: 撤单失败时不得直接终态放弃: 轮询 order_list_query 直至订单进入 DEAD/FILLED 终态再决策; 或增加按 broker 实际持仓对账补登 trade_records 的兜底。
- **qwen3.8-max** (S4, state_machine, app/playbook/playbook_service.py:2010-2014): 路由限价单进入 FAILED/CANCELLED 等终态时未检查 _dealt，可能丢弃部分成交
  - 证据引用: `                if any(_k in _status for _k in
                       ("FAILED", "CANCELLED", "DELETED", "REJECTED", "TIMEOUT")):
                    return _skip_terminal(
                        "archetype_order_dead",
                        f"{mode_label} {symbol} 路由限价单被拒/已撤 (status={_status}), `
  - 影响声称: _query_once 同时返回 _dealt；若订单在部分成交后被撤单/标记终态，这里会直接 skip_terminal，不记录已成交部分，可能形成 broker 有仓位但 playbook 认为已放弃的状态。
  - 建议: 在判定 archetype_order_dead 前先检查 _dealt > 0；若存在部分成交，应按部分成交路径记账或至少告警/复查，而不是直接终态跳过。
- **qwen3.8-max** (S3, state_machine, app/playbook/playbook_service.py:2041-2057): TTL 到期部分成交但成交均价缺失时会放弃，可能遗留真实仓位
  - 证据引用: `        _fin_dealt = max(_dealt, _dealt2)
        if _fin_dealt > 0 and (_davg2 > 0 or _davg > 0):
            _fill_px = _davg2 if _davg2 > 0 else _davg
            logger.info(f"[archetype路由] {symbol} TTL 到期部分成交 {_fin_dealt}/{req_qty}, "
                        f"撤余单后按已成交量记账 @{_fill_px}")
        `
  - 影响声称: 如果 _fin_dealt>0 但 dealt_avg_price 查询异常/为 0，会落入 archetype_ttl_expired 终态；此时可能已有部分成交，导致执行状态与真实持仓不一致。
  - 建议: 当 _fin_dealt>0 但均价缺失时，不应直接放弃；应回退使用 limit_price 或查询 deal_list 获取成交价，并标记 price_source 降级，同时告警。
- **qwen3.8-max-0902** (S4, state_machine, app/playbook/playbook_service.py:2010-2014): 路由限价单进入 FAILED/CANCELLED 等终态时忽略已发生的部分成交
  - 证据引用: `if any(_k in _status for _k in
                       ("FAILED", "CANCELLED", "DELETED", "REJECTED", "TIMEOUT")):
                    return _skip_terminal(
                        "archetype_order_dead",`
  - 影响声称: 轮询中已经从 `_query_once()` 拿到 `_dealt`，但订单状态若为 CANCELLED/FAILED 等（可能含部分成交）直接走 `_skip_terminal`，不按已成交量记账，可能造成真实成交被跳过、持仓与台账不一致。
  - 建议: 在终态分支先检查 `_dealt > 0`：若存在部分成交，按部分成交路径记账/收尾；只有零成交才进入 `archetype_order_dead`。
- **qwen3.8-max-0902** (S3, state_machine, app/playbook/playbook_service.py:2041-2057): TTL 到期部分成交但成交均价缺失时直接放弃，可能遗漏真实持仓
  - 证据引用: `_fin_dealt = max(_dealt, _dealt2)
        if _fin_dealt > 0 and (_davg2 > 0 or _davg > 0):
        return _skip_terminal(
            "archetype_ttl_expired",`
  - 影响声称: 当 `_fin_dealt > 0` 但 `dealt_avg_price` 查询为 0 时，代码落入 `archetype_ttl_expired` 跳过，而不是用可得的成交价/兜底价记账；部分成交可能变成未记录的孤儿仓位。
  - 建议: 部分成交时禁止直接按未成交跳过；若 `dealt_avg_price` 缺失，应重试查询或使用限价/成交价兜底并记录部分成交，同时保留告警。

<!-- DEB-VERDICT cluster=91 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: playbook_service.py:2010-2014 路由限价单进入 CANCELLED/FAILED 终态时直接 return _skip_terminal，不检查 _dealt 是否 >0，部分成交（broker 撤余单场景）被丢弃产生孤儿仓

### 议题 #13 [共性 2/3] `main.py`:96-179 — 配置完整性检查用'任一分段出现即算被消费', 死配置检测近乎失效

环节: s1_entry_config · 类别: config_effect, dataflow_inconsistency, error_swallowing, logic_anomaly · 自报最高 severity: S4
- **kimi-k3** (S4, dataflow_inconsistency, main.py:169-179): 实盘安全拦截仍读已废弃的 TRADE_ENV 环境变量, 与权威源 broker_adapter.trade_env 脱节
  - 证据引用: `169-172: `_trade_env = os.environ.get("TRADE_ENV", "<unset, default=1>")` / `_futu_trade_env = os.environ.get("FUTU_TRADE_ENV", "<unset, default=1>")` / `if (_trade_env == "0" or _futu_trade_env == "0") and not _allow_real:`;而本文件 617-620 行注释自述: '判定源与 order_executor.py:263 对齐 — 直接取 broker_adapter.tra`
  - 影响声称: 启动期的实盘拦截门检查的是已废弃、可能未设置或残留旧值的环境变量; 若 broker_adapter.trade_env 实际为 REAL 而 TRADE_ENV/FUTU_TRADE_ENV 未同步 (默认按模拟盘处理), 拦截不触发, 系统在 ALLOW_REAL_TRADING!=true 的情况下仍可能进入实盘交易, 安全门形同虚设
  - 建议: 拦截逻辑改为与 617-620 行相同的权威源: 在 DI 装配后读取 scheduler.execution_engine.broker_adapter.trade_env 做实盘判定 (或将该检查移到 broker_adapter 可用之后), 废弃的环境变量仅作交叉校验
- **kimi-k3** (S3, error_swallowing, main.py:124-138): 进程锁获取失败仅 print 警告后继续启动, 防双开保护静默失效
  - 证据引用: `123-138: `LOCK_FILE = "/run/ftseek-us.lock"` ... `except BlockingIOError: ... sys.exit(1)` 仅覆盖'已被加锁';而 `except Exception as e:\n print(f"⚠️ 进程锁警告：{e}")` 捕获 os.open 失败 (如 /run 目录无写权限) 后不退出, 继续执行后续启动流程`
  - 影响声称: 当 /run/ftseek-us.lock 因权限等原因无法创建时, 进程在无任何互斥保护下启动; 此时通过 ftseek_us.sh start 或 systemd 重复拉起会产生两个并发交易进程, 导致重复下单/重复持仓同步
  - 建议: 锁文件打开失败应视为致命错误退出 (与 BlockingIOError 一致), 或将锁路径改到项目可写目录 (如 data/ftseek-us.lock) 并在失败时 sys.exit(1)
- **kimi-k3** (S2, logic_anomaly, main.py:152-153): 预检用裸 int() 解析 FUTU_OPEND_PORT, 异常值在 try 外直接崩溃
  - 证据引用: `153 行: `_opend_port = int(os.environ.get("FUTU_OPEND_PORT", 11111))` — 不在 157 行 try 块内;对照 config/config_us.py 86-99 行 `_safe_int_env` 的 docstring: 'D-DAY (2026-05-17): .env 中字段异常 (例如 REDACTED 占位) 时 fallback 默认值, 防止 import 时整个 config_us 加载失败'`
  - 影响声称: FUTU_OPEND_PORT 被写成非数字 (如占位符 REDACTED) 时, ValueError 在 socket 检查之前抛出, 启动以未处理 traceback 崩溃, 绕过了 config_us 已为同类问题建立的兜底防御, 且无任何友好提示
  - 建议: 复用 config_us._safe_int_env (或等价 try/except) 解析端口, 与 102 行 FUTU_OPEND_PORT 的处理保持一致
- **kimi-k3** (S1, logic_anomaly, main.py:96-102): 配置完整性检查用'任一分段出现即算被消费', 死配置检测近乎失效
  - 证据引用: `97-102: `segments = key.split(".")` / `found = any(seg in code_keys for seg in segments)` / `if not found:` / `if key not in code_keys:` / `dead.append(...)`;code_keys 来自 74-77 行对全仓 `get_dotted("...")` 与 `.get("...")` 首个参数的扫描`
  - 影响声称: 如 'risk.position.max_per_symbol' 只要任意代码出现 .get("risk") 即判'已消费'; 'risk'/'capital' 这类通用分段几乎必然出现在某处 .get() 中, 该检查实际上永远报'✅ 所有关键配置均有代码消费者', 死配置不可见, 与函数 docstring '让死配置可见' 的目的相反
  - 建议: 改为要求完整点式 key (或其末段带类型的精确匹配) 出现在 code_keys 中, 或维护 key→消费点 的白名单映射做精确核对
- **qwen3.8-max-0902** (S2, config_effect, main.py:152-153): OpenD 端口启动预检直接 int() 解析环境变量，异常值会崩溃
  - 证据引用: `_opend_host = os.environ.get("FUTU_OPEND_HOST", "127.0.0.1")
    _opend_port = int(os.environ.get("FUTU_OPEND_PORT", 11111))`
  - 影响声称: FUTU_OPEND_PORT 被写成非数字时，预检前抛 ValueError；而 config_us.py 已提供 _safe_int_env 的容错解析。
  - 建议: 复用 _safe_int_env 或 try/except 回退默认端口并告警。

<!-- DEB-VERDICT cluster=13 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: main.py:137-138 的 except Exception 在进程锁获取失败(如 /run 不可写)时仅 print 警告后继续启动，双实例防护静默失效；main.py:153 裸 int() 解析 FUTU_OPEND_PORT 遇异常值直接崩溃而不走 config_us.py:86-99 的 _safe_int_env 安全路径

### 议题 #20 [共性 2/3] `app/data/market_data_router.py`:426-451 — 期权路径未定义 daily_klines，后续引用会触发 UnboundLocalError

环节: s2_market_data · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S2, logic_anomaly, app/data/market_data_router.py:443-451): 期权分支不赋值 daily_klines, 但 L451 无条件引用 → 每次期权上下文必抛 UnboundLocalError
  - 证据引用: `L426-435 期权分支只设 klines=None 与 ts_ctx; daily_klines 仅在 else 分支 L445 `daily_klines = self.get_daily_bars(symbol, count=60)` 赋值; L451 `if daily_klines and len(daily_klines) >= 30:` 与 L426/L436 同级, 期权路径引用未绑定局部变量; L521-522 注释自证: "daily_klines 仅在非期权主路径赋值 (期权/报价路径无此局部) → locals 守卫"`
  - 影响声称: 期权且 Futu 有报价时 L451 抛 UnboundLocalError, 被 L738 宽 except 捕获 → 每次调用都记一条 "Futu LV3 主通道失败" warning 并跳过 L478 之后全部注入; 错误噪声掩盖真实故障
  - 建议: 在 L419 `klines = None` 旁初始化 `daily_klines = None`, 或期权分支设置 ts_ctx 后直接跳过指标段
- **qwen3.8-max** (S3, logic_anomaly, app/data/market_data_router.py:426-451): 期权路径未定义 daily_klines，后续引用会触发 UnboundLocalError
  - 证据引用: `if is_option: / klines = None  # 期权不需要 K 线技术指标 / daily_klines = self.get_daily_bars(symbol, count=60) / if daily_klines and len(daily_klines) >= 30:`
  - 影响声称: daily_klines 只在非期权 else 分支赋值；当标的是期权且 Futu 报价成功时，代码仍会执行 if daily_klines，抛出 UnboundLocalError 后被外层 except 吞掉，导致期权上下文增强中断并产生误导性错误日志。
  - 建议: 在函数入口初始化 daily_klines=None，或将日K技术指标计算放入非期权分支，并沿用 locals().get 式守卫。

<!-- DEB-VERDICT cluster=20 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: market_data_router.py:451 与L426/L436同级缩进(20空格), 期权分支(L426-435)从未定义daily_klines变量, L451 `if daily_klines`必抛UnboundLocalError, 每次期权上下文查询必崩溃

### 议题 #22 [共性 2/3] `app/data/market_data_router.py`:861-862 — ts_ctx['timestamp'] 用服务器本地 datetime.now() naive 时间, 与同函数内 US/Eastern "今日"口径混用

环节: s2_market_data · 类别: time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, time_boundary, app/data/market_data_router.py:861-862): ts_ctx['timestamp'] 用服务器本地 datetime.now() naive 时间, 与同函数内 US/Eastern "今日"口径混用
  - 证据引用: `L862 `ts_ctx["timestamp"] = datetime.now().isoformat()`; 同函数 L520/L558/L629 均用 `_dt.now(_pz_vw.timezone('US/Eastern'))` 推今日; rejection_logger.py L71-75 注释明确既定约定 "改写 naive ET (date() 对 naive 不做时区转换), 写读同口径"`
  - 影响声称: 上下文时间戳时区口径不统一 (服务器本地 vs ET), 下游若按日期/新鲜度解析该字段, 在 UTC/CST 服务器上与 ET 交易日错位, 与本仓已修复的同类时区 bug 模式一致
  - 建议: 改为 now_et().replace(tzinfo=None).isoformat() 或输出显式带时区的 ISO
- **qwen3.8-max** (S2, time_boundary, app/data/market_data_router.py:862-862): 市场上下文 timestamp 使用本地 naive 时间
  - 证据引用: `ts_ctx["timestamp"] = datetime.now().isoformat()`
  - 影响声称: timestamp 未带时区且取决于运行主机本地时区；若主机不在美东/UTC，下游新鲜度判断、日志归因和跨模块时间口径会与 ET/UTC 错位。
  - 建议: 统一写入 aware ISO 时间，例如 UTC 或 America/New_York，并在消费端明确时区语义。

<!-- DEB-VERDICT cluster=22 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: market_data_router.py:862 ts_ctx["timestamp"]=datetime.now().isoformat()为服务器本地naive时间, 同函数L558/L629用US/Eastern计算"今日", rejection_logger.py:73注释既定约定为naive ET, 若服务器部署非ET则timestamp与ET交易日口径错位

### 议题 #24 [共性 2/3] `app/data/market_data_router.py`:1093-1153 — health_check 只统计 TwelveData/Finnhub/AlphaVantage, 主通道 Futu LV3 不在健康口径内

环节: s2_market_data · 类别: other, resource_leak · 自报最高 severity: S2
- **kimi-k3** (S2, resource_leak, app/data/market_data_router.py:1147-1153): router.close() 调 self.futu.disconnect(), 但 provider 可见接口是 close() — hasattr 守卫使关闭静默跳过
  - 证据引用: `L1148-1150 `if self.futu and hasattr(self.futu, 'disconnect'): try: self.futu.disconnect()`; futu_lv3_provider.py L2028 `def close(self):`, 且 L2161 ` _futu_lv3_instance.close()` (reset 单例同样走 close); 注意 provider 中段 1062-1753 不可见, 若 disconnect 定义在截断区则本条不成立`
  - 影响声称: 若 provider 无 disconnect 方法, router.close() 永不关闭 Futu 连接 → OpenD 槽位/连接泄漏, 与 L1147 注释 "P1 修复: 关闭 Futu LV3 连接，释放 OpenD 槽位" 意图相悖且无任何日志
  - 建议: 统一调用 self.futu.close(), 或 hasattr 同时回退检查 close
- **kimi-k3** (S2, other, app/data/market_data_router.py:1093-1135): health_check 只统计 TwelveData/Finnhub/AlphaVantage, 主通道 Futu LV3 不在健康口径内
  - 证据引用: `L1095-1130 仅遍历 self.td/self.fh/self.av; L1133 `status["healthy"] = sources_healthy > 0`; L1135 `status["sources_total"] = len([s for s in [self.td, self.fh, self.av] if s])`; 而 docstring L4-5 标明 "Primary: Futu LV3"`
  - 影响声称: 主数据源宕掉时只要任一备用源有 token, health_check 仍报 healthy=True → 监控对主通道失能无感知 (provider 自带 get_health_status L1986 未被此处使用)
  - 建议: 把 self.futu 的连接/健康状态纳入 sources 与 healthy 判定
- **qwen3.8-max** (S2, resource_leak, app/data/market_data_router.py:1148-1153): close 调用不存在的 disconnect，Futu 连接不会被关闭
  - 证据引用: `if self.futu and hasattr(self.futu, 'disconnect'): / self.futu.disconnect() / def close(self):`
  - 影响声称: FutuLV3Provider 可见接口为 close() 而非 disconnect()；MarketDataRouter.close 因 hasattr 检查失败不会释放 Futu 连接/订阅，关闭流程无法释放 OpenD 槽位。
  - 建议: 调用 self.futu.close()，或为 FutuLV3Provider 增加 disconnect() 别名并统一关闭语义。

<!-- DEB-VERDICT cluster=24 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: market_data_router.py:1095-1135 health_check仅遍历self.td/self.fh/self.av三个备用源, 主通道Futu LV3不在健康口径内(L1133/L1135); 但disconnect不存在之说不成立——futu_lv3_provider.py:1455确有disconnect方法定义, hasattr(L1148)检查会通过并正常调用

### 议题 #19 [共性 2/3] `app/data/futu_lv3_provider.py`:2093-2111 — unsubscribe_all 部分类型退订失败仍清空 _subscribed_symbols 与全部 push 缓存

环节: s2_market_data · 类别: state_machine · 自报最高 severity: S2
- **kimi-k3** (S2, state_machine, app/data/futu_lv3_provider.py:2093-2111): unsubscribe_all 部分类型退订失败仍清空 _subscribed_symbols 与全部 push 缓存
  - 证据引用: `L2101-2109 退订失败仅 `failed += 1` 并 warning; L2111 `self._subscribed_symbols.clear()` 与 L2112-2116 四个 push 缓存 clear 无条件执行`
  - 影响声称: 退订失败的类型在 OpenD 侧仍活跃推送, 但本地已遗忘 → close() (L2034 依赖 _subscribed_symbols) 不再补退订 → 槽位泄漏; 且 watchdog L327 `if not self._subscribed_symbols: continue` 使健康检查被跳过, 推送异常无人发现
  - 建议: 仅清除确认退订成功的 (symbol, sub_type); 存在失败时保留集合并告警
- **qwen3.8-max-0902** (S2, state_machine, app/data/futu_lv3_provider.py:2101-2111): unsubscribe 失败仍清空本地订阅集合
  - 证据引用: `ret, msg = self._quote_ctx.unsubscribe(symbols_list, [sub_type])
if ret == 0:
    success += 1
else:
    failed += 1
self._subscribed_symbols.clear()`
  - 影响声称: 即使部分/全部 unsubscribe 返回失败，本地 _subscribed_symbols 也被清空；系统认为无订阅，但 OpenD 侧可能仍占用槽位，后续重订阅策略与真实服务端状态不一致。
  - 建议: 仅移除成功退订的 symbols/sub_types；失败时保留状态并记录告警，或通过查询服务端订阅状态后再同步本地集合。

<!-- DEB-VERDICT cluster=19 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: futu_lv3_provider.py:2111 self._subscribed_symbols.clear()在退订循环(L2096-2109)后无条件执行, 部分退订失败(ret!=0仅failed+=1)时服务端仍有活跃订阅但本地集合已清空, 后续subscribe会误判需重订浪费槽位

### 议题 #31 [共性 2/3] `app/core/scheduler_core.py`:75-84 — PositionStore 与 daily_active_pool 同步失败后继续运行，形成池子不一致

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency, error_swallowing · 自报最高 severity: S2
- **qwen3.8-max** (S2, dataflow_inconsistency, app/core/scheduler_core.py:75-84): PositionStore 与 daily_active_pool 同步失败后继续运行，形成池子不一致
  - 证据引用: `            try:
                from app.utils.daily_active_pool import get_daily_active_pool
                pool = get_daily_active_pool(market_prefix="US.")
                pool.add_position(symbol, metadata={
                    "direction": position.direction.value if hasattr(position.directio`
  - 影响声称: 内存持仓已写入但动态池未写入，后续依赖 daily_active_pool 的扫描/监控可能漏掉该标的，造成持仓管理与扫描池不一致。
  - 建议: 增加失败重试/补偿队列，或在启动与周期任务中做 PositionStore 对 daily_active_pool 的对账。
- **qwen3.8-max-0902** (S2, error_swallowing, app/core/scheduler_core.py:75-84): 动态池同步失败仅打日志继续运行
  - 证据引用: `            except Exception as e:
                logger.error(f"[daily_active_pool] {symbol} 添加同步失败: {e}")
                # 不中断主流程 (内存持仓已写入), 但异常向监控系统暴露`
  - 影响声称: PositionStore 已写入但 daily_active_pool 未同步，扫描/监控消费者可能读取过期或不完整动态池，导致重复关注、遗漏退出或调度池与内存持仓不一致。
  - 建议: 记录失败事件并触发补偿/周期重同步；对关键消费者明确内存持仓为权威源，并在动态池长期不一致时告警或降级。

<!-- DEB-VERDICT cluster=31 -->
裁决: REJECTED | 置信=high | 理由: scheduler_core.py:75-84 注释明确标注"非关键路径"(line 74 Task #49),daily_active_pool 为诊断辅助池,position_store(line 70)已正确写入,设计意图即为失败不阻断主流程

### 议题 #36 [共性 2/3] `app/pipeline/order_executor.py`:86-181 — 平仓退避状态使用无锁全局 dict，多线程并发下可能丢失计数

环节: s3_scheduling_pipeline · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max** (S3, race_condition, app/pipeline/order_executor.py:86-181): 平仓退避状态使用无锁全局 dict，多线程并发下可能丢失计数
  - 证据引用: `_close_retry_state: Dict[Tuple[str, str], Dict[str, Any]] = {}
    _state = _close_retry_state.setdefault(_key, {
    _state["fail_count"] = _state.get("fail_count", 0) + 1`
  - 影响声称: scan/monitor/即时触发等多线程可能同时更新 _close_retry_state，导致失败计数丢失、冷却未生效或永久阻塞误清除，放大平仓重试风险。
  - 建议: 为 _close_retry_state 增加 threading.Lock/RLock，或使用线程安全结构并在所有读写处加锁。
- **qwen3.8-max-0902** (S3, race_condition, app/pipeline/order_executor.py:177-181): 平仓退避状态使用无锁全局字典并发更新
  - 证据引用: `    _state = _close_retry_state.setdefault(_key, {
        "fail_count": 0, "cooldown_until": None,
        "last_reason": "", "last_failed_at": None, "blocked": False,
    })
    _state["fail_count"] = _state.get("fail_count", 0) + 1`
  - 影响声称: 多个平仓/监控线程并发更新同一 (symbol, direction) 退避状态时可能互相覆盖，导致失败计数丢失、冷却未生效或永久阻塞阈值误判，削弱平仓失败保护。
  - 建议: 为 _close_retry_state 增加全局锁或按 key 加锁；也可将退避状态放入数据库/原子存储中更新。

<!-- DEB-VERDICT cluster=36 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: order_executor.py:181 _state["fail_count"]=_state.get("fail_count",0)+1 为读-改-写非原子操作,CPython GIL 下单条 dict op 安全但此复合操作可丢失增量,导致退避阶梯跳级延迟

### 议题 #39 [共性 2/3] `app/pipeline/order_executor.py`:331-351 — 取消旧订单验证超时后仍返回可继续平仓

环节: s3_scheduling_pipeline · 类别: logic_anomaly, state_machine · 自报最高 severity: S5
- **qwen3.8-max** (S5, state_machine, app/pipeline/order_executor.py:346-351): 未结订单取消验证超时后仍返回可继续平仓
  - 证据引用: `        # 验证超时 (仍有未结订单), 警告但仍返回 (调用方会再尝试)
        logger.warning(
            f"[平仓·待成交] {symbol} 取消后 {_verify_max}s 仍有未结订单, "
            f"已取消 {len(_canceled)} 单 (继续尝试)"
        )
    return True, _canceled`
  - 影响声称: _close_pre_check 会把仍有未结订单的状态误判为安全并继续发平仓单，可能导致重复下单、交叉交易或撤单未完成时的错单。
  - 建议: 只有当 _query_broker_open_orders 最终确认为空时才返回 True；取消失败或验证超时时应返回 False 并进入退避/告警。
- **qwen3.8-max-0902** (S4, logic_anomaly, app/pipeline/order_executor.py:331-351): 取消旧订单验证超时后仍返回可继续平仓
  - 证据引用: `        logger.warning(
            f"[平仓·待成交] {symbol} 取消后 {_verify_max}s 仍有未结订单, "
            f"已取消 {len(_canceled)} 单 (继续尝试)"
        )
    return True, _canceled`
  - 影响声称: 在旧订单未确认完全撤销时继续发送新平仓单，可能导致同标的重复委托、交叉交易、持仓不足错误，甚至放大平仓失败级联。
  - 建议: 验证超时后若仍有未结订单，应返回 False 或进入退避/人工处理；必须等待 broker 清算确认后再允许新平仓委托。

<!-- DEB-VERDICT cluster=39 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: order_executor.py:351 验证超时后仍 return True,_canceled 告知调用方可安全发新单,但 _query_broker_open_orders 仍有未结订单,可导致双重下单

### 议题 #40 [共性 2/3] `app/pipeline/order_executor.py`:410-428 — broker 持仓为 0 时直接清理内存持仓，未先检查/清理未结订单

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency, state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/pipeline/order_executor.py:410-428): broker 持仓为 0 时直接清理内存持仓，未先检查/清理未结订单
  - 证据引用: `    # 2. 强制刷新 broker 端持仓
    _broker_qty = _refresh_broker_position_qty(scheduler, position.symbol)
    if _broker_qty is not None and _broker_qty <= 0:
        return "broker 端已无持仓 (已标记内存平仓)"`
  - 影响声称: 若该标的仍有未成交订单，内存持仓被移除后旧订单可能稍后成交，形成 broker 有真实持仓但 PositionStore 无记录的幽灵仓。
  - 建议: 在 broker=0 分支也调用 _wait_or_cancel_open_orders 或至少查询并取消未结订单，再清理内存持仓。
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/pipeline/order_executor.py:411-428): broker 查询空结果被当作零持仓并直接清理内存仓位
  - 证据引用: `    _broker_qty = _refresh_broker_position_qty(scheduler, position.symbol)
    if _broker_qty is not None and _broker_qty <= 0:
                _ps.remove(position.symbol)
        return "broker 端已无持仓 (已标记内存平仓)"`
  - 影响声称: 若 broker 接口瞬时返回空列表或部分数据缺失，系统会把真实持仓误判为零持仓，删除 PositionStore 并标记平仓，导致 position_sync 与真实持仓失去收敛，后续监控、止损和记账全部漏掉该仓位。
  - 建议: 区分查询失败、空列表和确认零持仓；对零持仓收敛增加重试/二次确认/账户状态校验，避免单次异常快照直接删除内存仓位。

<!-- DEB-VERDICT cluster=40 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: order_executor.py:412-428 broker_qty<=0 时直接 remove+release+return,跳过第3步 _wait_or_cancel_open_orders,遗留的待成交订单可在清理后成交造成短暂仓账不一致

### 议题 #42 [共性 2/3] `app/pipeline/order_executor.py`:699-708 — TWAP 部分成交被标记为 FILLED

环节: s3_scheduling_pipeline · 类别: state_machine, time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S3, state_machine, app/pipeline/order_executor.py:699-702): TWAP 部分成交被标记为 FILLED
  - 证据引用: `    # 将 TWAP 结果回填到 order 对象, 保持与下游一致
    order.state = OrderState.FILLED if twap_result.filled_qty > 0 else OrderState.REJECTED
    order.filled_quantity = float(twap_result.filled_qty)
    order.avg_fill_price = float(twap_result.avg_fill_price)`
  - 影响声称: 只要 filled_qty > 0 就置为 FILLED，部分成交场景下订单状态与实际不符，下游若按状态判断完成/补单/回滚会失真。
  - 建议: 当 0 < filled_qty < total_qty 时应标记 PARTIALLY_FILLED，仅完全成交才标记 FILLED。
- **qwen3.8-max** (S3, time_boundary, app/pipeline/order_executor.py:707-708): TWAP 回填时间混用本地 naive 时间与 ET aware 时间
  - 证据引用: `    order.filled_at = datetime.fromtimestamp(twap_result.finished_at) if twap_result.finished_at else now_et()
    order.sent_at = datetime.fromtimestamp(twap_result.started_at) if twap_result.started_at else now_et()`
  - 影响声称: datetime.fromtimestamp 生成本地时区 naive 时间，而 now_et() 是 ET aware；后续比较、入库、归因可能出现时区错位或 aware/naive 异常。
  - 建议: 统一使用带时区的转换，例如 datetime.fromtimestamp(ts, tz=ET) 或封装 now_et/from_epoch_et。
- **qwen3.8-max-0902** (S3, state_machine, app/pipeline/order_executor.py:700-702): TWAP 部分成交被标记为 FILLED
  - 证据引用: `    order.state = OrderState.FILLED if twap_result.filled_qty > 0 else OrderState.REJECTED
    order.filled_quantity = float(twap_result.filled_qty)
    order.avg_fill_price = float(twap_result.avg_fill_price)`
  - 影响声称: TWAP 只成交部分数量时订单状态被标为 FILLED，而非 PARTIALLY_FILLED；下游若依据 state 判断完整成交，可能按请求数量而非实际成交量建仓或记账，造成仓位/资金不一致。
  - 建议: 当 0 < filled_qty < order.quantity 时设置 OrderState.PARTIALLY_FILLED，并要求下游统一使用 filled_quantity 作为仓位规模。

<!-- DEB-VERDICT cluster=42 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: order_executor.py:700 twap_result.filled_qty>0 即置 FILLED,但 filled_quantity 可能<order.quantity(部分成交),应使用 PARTIALLY_FILLED(models.py:100 已定义此状态);707-708 datetime.fromtimestamp 返回 naive 与 now_et() aware 混用

### 议题 #32 [共性 2/3] `app/core/scheduler_core.py`:842-848 — 信号冷却恢复用带时区 ISO 字符串与 DB 文本比较

环节: s3_scheduling_pipeline · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max** (S2, time_boundary, app/core/scheduler_core.py:842-848): 信号冷却恢复依赖 ISO 字符串比较，时区/格式不一致会漏恢复
  - 证据引用: `            cutoff = (now_et() - timedelta(minutes=cooldown_min)).isoformat()
            with sqlite3.connect(str(db_path)) as conn:
                rows = conn.execute(
                    "SELECT DISTINCT symbol, MAX(exit_time) FROM trade_records "
                    "WHERE exit_time IS NOT NULL`
  - 影响声称: trade_records.exit_time 若存储为 naive、UTC、带 Z 或不同格式，SQLite 字符串比较可能错误，导致重启后冷却期漏恢复或误恢复，增加重复开仓风险。
  - 建议: 统一存储为 UTC epoch 或带时区 ISO，并在 Python 侧解析后比较；避免依赖文本比较处理时间边界。
- **qwen3.8-max-0902** (S2, time_boundary, app/core/scheduler_core.py:842-846): 信号冷却恢复用带时区 ISO 字符串与 DB 文本比较
  - 证据引用: `            cutoff = (now_et() - timedelta(minutes=cooldown_min)).isoformat()
                    "SELECT DISTINCT symbol, MAX(exit_time) FROM trade_records "
                    "WHERE exit_time IS NOT NULL AND exit_time > ? GROUP BY symbol",`
  - 影响声称: 若 trade_records.exit_time 存储为 naive、UTC 或其他文本格式，SQL 字符串比较可能漏选冷却中的平仓记录，重启后冷却失效，导致同标的短时间重复入场。
  - 建议: 统一存储 timezone-aware 时间（建议 UTC 或 ET）并在 SQL 中使用规范化比较；或读取后在 Python 中解析时间再过滤。

<!-- DEB-VERDICT cluster=32 -->
裁决: REJECTED | 置信=high | 理由: 所有 exit_time 写入路径(position.exit_time=now_et() at order_executor.py:4835; _et_now=now_et() at position_sync.py:1649)均为 ET tz-aware ISO,cutoff(line 842)同用 now_et() ISO,SQL 字符串比较格式一致

### 议题 #43 [共性 2/3] `app/pipeline/order_executor.py`:849-882 — 反手平空订单仅提交即视为已平仓

环节: s3_scheduling_pipeline · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/pipeline/order_executor.py:849-882): 反手平空函数未确认订单结果，执行引擎缺失时仍返回成功
  - 证据引用: `        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.submit_order(order)
        scheduler._set_cooldown(symbol)
        logger.info(f"[空头转多头] {symbol} 空头已平仓, 下轮扫描将尝试开多头")
        return True`
  - 影响声称: submit_order 返回 REJECTED/未成交或 execution_engine 不存在时，函数仍设置冷却并返回 True，导致冲突处理假成功、反向持仓可能继续存在。
  - 建议: 检查返回订单状态与成交量；只有确认平仓成功才设置冷却并返回 True，引擎缺失应返回 False。
- **qwen3.8-max-0902** (S4, state_machine, app/pipeline/order_executor.py:849-862): 反手平空订单仅提交即视为已平仓
  - 证据引用: `                result = scheduler.execution_engine.submit_order(order)
        scheduler._set_cooldown(symbol)
        logger.info(f"[空头转多头] {symbol} 空头已平仓, 下轮扫描将尝试开多头")`
  - 影响声称: 若订单被拒绝、未成交或仍处于待成交状态，系统仍会设置冷却并记录已平仓；原有空头持仓可能继续存在，而后续扫描误以为可开多头，造成方向冲突、重复下单或仓位账本失真。
  - 建议: 提交后必须检查 order.state / filled_quantity，必要时查询 broker 订单与持仓；只有确认成交后才设置冷却和发送通知，失败应进入重试、回滚或人工告警路径。

<!-- DEB-VERDICT cluster=43 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: order_executor.py:849-862 submit_order 仅提交限价单(LIMIT ±2%)即 set_cooldown+log"空头已平仓"+return True,实盘路径(engine.py:464-471)仅转 SENT 不等成交,未成交时限价单挂起但冷却期阻止重发

### 议题 #44 [共性 2/3] `app/pipeline/order_executor.py`:948-979 — 反手平多订单仅提交即视为已平仓

环节: s3_scheduling_pipeline · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/pipeline/order_executor.py:948-979): 反手平多函数未确认订单结果，执行引擎缺失时仍返回成功
  - 证据引用: `        if scheduler.execution_engine:
            try:
                result = scheduler.execution_engine.submit_order(order)
        scheduler._set_cooldown(symbol)
        logger.info(f"[多头转空头] {symbol} 多头已平仓, 下轮扫描将尝试开空头(期权)")
        return True`
  - 影响声称: 与平空路径相同：订单未确认或引擎缺失时仍标记冲突已处理，可能留下真实多头持仓并错误进入冷却。
  - 建议: 以订单最终状态/成交回报作为成功条件；失败时保留冲突状态并触发重试或人工告警。
- **qwen3.8-max-0902** (S4, state_machine, app/pipeline/order_executor.py:948-960): 反手平多订单仅提交即视为已平仓
  - 证据引用: `                result = scheduler.execution_engine.submit_order(order)
        scheduler._set_cooldown(symbol)
        logger.info(f"[多头转空头] {symbol} 多头已平仓, 下轮扫描将尝试开空头(期权)")`
  - 影响声称: 若平多订单未实际成交，系统仍会设置冷却并认为多头已清理；原有多头可能继续存在，后续开空头逻辑可能基于错误前提运行，导致对冲/方向状态不一致。
  - 建议: 以订单最终状态和成交数量为唯一依据；未成交时不得设置冷却或宣称平仓，应保留原持仓并触发重试或冲突重处理。

<!-- DEB-VERDICT cluster=44 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: order_executor.py:948-960 与#43对称,submit_order 限价单仅提交即 log"多头已平仓"+set_cooldown+return True,engine.py:469 实盘仅置 SENT 不等待 FILLED

### 议题 #63 [共性 2/3] `app/news_v2/sources/finnhub_news_source.py`:40-40 — _last_timestamp 初始化后未使用，缺少增量/时效过滤

环节: s4_signals_sources · 类别: dead_code · 自报最高 severity: S2
- **qwen3.8-max** (S1, dead_code, app/news_v2/sources/finnhub_news_source.py:40-40): _last_timestamp 初始化后未使用，缺少增量/时效过滤
  - 证据引用: `self._last_timestamp = int(time.time()) - 3600`
  - 影响声称: 没有基于时间水位过滤旧新闻，可能重复或过期新闻进入下游，削弱新闻时间衰减控制。
  - 建议: 实现 timestamp 水位过滤，或删除无用字段避免误导。
- **qwen3.8-max-0902** (S2, dead_code, app/news_v2/sources/finnhub_news_source.py:40-40): _last_timestamp 初始化后未使用
  - 证据引用: `self._last_timestamp = int(time.time()) - 3600`
  - 影响声称: 没有时间窗口过滤，可能让过期新闻进入管线；字段语义误导维护者。
  - 建议: 使用 from/to 参数做时间过滤，或删除该字段。

<!-- DEB-VERDICT cluster=63 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: finnhub_news_source.py:40 self._last_timestamp初始化后全文零引用(无增量过滤/无时效校验),是死代码

### 议题 #54 [共性 2/3] `app/factors/trade_quality_gate.py`:68-95 — 模块初始化失败后当前评估仍继续访问空模块

环节: s4_signals_sources · 类别: error_swallowing · 自报最高 severity: S4
- **qwen3.8-max** (S4, error_swallowing, app/factors/trade_quality_gate.py:85-86): 质量门控在禁用/加载失败时直接放行
  - 证据引用: `if not self.enabled:
            return {"opportunity_score": 0.5, "passed_gate": True, "gate_details": {}}`
  - 影响声称: 若任一子模块导入失败导致 enabled=False，整个质量门控会被静默绕过，可能让低质量信号进入交易链路。
  - 建议: 关键风控门控失败时应 fail-closed，或至少显式拒绝并告警，而不是默认 passed_gate=True。
- **qwen3.8-max-0902** (S3, error_swallowing, app/factors/trade_quality_gate.py:68-95): 模块初始化失败后当前评估仍继续访问空模块
  - 证据引用: `except Exception as e:
            logger.error(f"[交易质量门] 模块加载失败: {e}")
            self.enabled = False
        self._init_modules()
        res = self._modules['regime'].check(symbol, history, market_ctx)`
  - 影响声称: 导入失败时 _modules 为空，evaluate 会 KeyError，质量门可能崩溃。
  - 建议: _init_modules 失败后立即返回降级结果，或在 evaluate 中重新检查 enabled。

<!-- DEB-VERDICT cluster=54 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: trade_quality_gate.py:70 init失败设enabled=False后evaluate():85-86无条件返回passed_gate=True/opportunity_score=0.5,质量门控整体旁路;_initialized未置True使下次重试,但当前调用已放行

### 议题 #60 [共性 2/3] `app/news_v2/processor/event_signal_generator.py`:79-108 — 信号生成无任何新闻时效/时间衰减检查

环节: s4_signals_sources · 类别: dataflow_inconsistency, logic_anomaly, time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/news_v2/processor/event_signal_generator.py:103-108): impacted_tickers 方向映射: 非 UP/NEUTRAL 的任何值一律落 SHORT
  - 证据引用: `103行 `d = it.get("direction", "NEUTRAL")`; 104-105行仅跳过 NEUTRAL; 108行 `"direction": "LONG" if d == "UP" else "SHORT",``
  - 影响声称: LLM 输出的 direction 若为小写 'up'、空串或其他非预期文本, 全部被静默映射为 SHORT, 信号方向可能反转; 本文件无任何枚举校验 (json_schema_guard 的 enums 能力未在此使用)
  - 建议: 显式校验 d ∈ {"UP","DOWN"}, 非法值 continue 丢弃
- **kimi-k3** (S2, time_boundary, app/news_v2/processor/event_signal_generator.py:79-87): 信号生成无任何新闻时效/时间衰减检查
  - 证据引用: `80-85行硬过滤仅 `classification.credibility` / `abs(classification.sentiment)` / `action_hint in ("BUY","SELL")`; 全函数未引用新闻 timestamp 或 age; 140行 `"horizon_min": classification.time_horizon_minutes` 仅透传不校验`
  - 影响声称: 若上游管线未做时效过滤, 隔夜/过期新闻的分类结果仍会生成'新'交易信号, 唯一时间防护只有同标的 5 分钟冷却; 与 models.py is_expired(120min) 的时效语义无衔接
  - 建议: 在硬过滤中增加新闻年龄上限 (如 age_minutes ≤ horizon_min 或固定阈值)
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/news_v2/processor/event_signal_generator.py:103-108): impacted ticker 的非 UP 方向一律映射为 SHORT
  - 证据引用: `d = it.get("direction", "NEUTRAL")
        if d == "NEUTRAL":
            continue
        candidates.append({
            "symbol": it["ticker"],
            "direction": "LONG" if d == "UP" else "SHORT",`
  - 影响声称: 若 LLM 输出 DOWN 以外的未知方向（如 BUY/SELL/BEARISH/空），会被静默当作 SHORT。
  - 建议: 白名单校验 direction，未知值丢弃或转 NEUTRAL。

<!-- DEB-VERDICT cluster=60 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: event_signal_generator.py:103-108 impacted_tickers方向d非UP且非NEUTRAL的任何值(如DOWN_UNKNOWN)均映射为SHORT;全函数无新闻timestamp引用(:79-85硬过滤仅校验credibility/sentiment/action_hint),过期新闻与即时新闻同等对待

### 议题 #73 [共性 2/3] `app/decision/strategies/price_break.py`:45-86 — 阻力/支撑为 0 时仍可通过 validate_data，产生假突破信号

环节: s5_decision_orchestration · 类别: logic_anomaly, numeric_unit · 自报最高 severity: S3
- **qwen3.8-max** (S2, logic_anomaly, app/decision/strategies/price_break.py:83-86): 价格跌破支撑做空强度随跌破幅度增大而降低，与注释相反
  - 证据引用: `# 计算强度：跌破幅度越大，强度越高
strength = max(0, 40 - int(breakdown_ratio * 100))`
  - 影响声称: 跌破支撑越深，strength 越低甚至归零，导致更强空头突破信号被弱化，可能影响仲裁与仓位。
  - 建议: 改为随跌破幅度增加，例如 strength = min(100, 40 + int(breakdown_ratio * 100))。
- **qwen3.8-max** (S3, numeric_unit, app/decision/strategies/price_break.py:45-51): 阻力/支撑为 0 时仍可通过 validate_data，产生假突破信号
  - 证据引用: `resistance = market_data.get("resistance", 0)
support = market_data.get("support", 0)
if current_price > resistance and volume_ratio > 1.5:`
  - 影响声称: 若上游缺失或错误写入 resistance=0，任意正价格都会满足 current_price > resistance，配合放量即生成 LONG 信号。
  - 建议: 在 validate_data 或 generate_signal 中校验 resistance/support 为正且与 current_price 同量级，否则返回空信号。
- **qwen3.8-max-0902** (S3, numeric_unit, app/decision/strategies/price_break.py:45-85): resistance/support 为 0 时突破/跌破比率会除零
  - 证据引用: `resistance = market_data.get("resistance", 0)
support = market_data.get("support", 0)
breakout_ratio = (current_price - resistance) / resistance
breakdown_ratio = (support - current_price) / support`
  - 影响声称: validate_data 只检查字段存在，不检查数值有效；当 resistance 或 support 为 0 时 generate_signal 会抛 ZeroDivisionError，可能中断该策略或上层信号流程。
  - 建议: 在计算前校验 resistance > 0 / support > 0，无效时返回 StrategyResult()。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/decision/strategies/price_break.py:83-86): SHORT 跌破强度公式与“跌破幅度越大强度越高”相反
  - 证据引用: `# 计算强度：跌破幅度越大，强度越高
breakdown_ratio = (support - current_price) / support
strength = max(0, 40 - int(breakdown_ratio * 100))`
  - 影响声称: 跌破越深，strength 越低；若下游以 strength 过滤或排序，会把更强的做空信号压低，造成仲裁/执行优先级失真。
  - 建议: 将 SHORT 强度改为随 breakdown_ratio 增大而增大，例如 40 + int(breakdown_ratio * 100) 并做上限约束。

<!-- DEB-VERDICT cluster=73 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: price_break.py:53/85 resistance/support 默认为 0 时 breakout_ratio/breakdown_ratio 触发 ZeroDivisionError 崩溃; 且 line 86 做空 strength=max(0,40-int(breakdown_ratio*100)) 与 line 84 注释"跌破幅度越大强度越高"相反(跌破50%时strength=0, 跌破5%时strength=35)

### 议题 #74 [共性 2/3] `app/decision/strategies/rsi_rebound.py`:52-54 — RSI 超卖做多强度随 RSI 降低而下降，与注释相反

环节: s5_decision_orchestration · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max** (S2, logic_anomaly, app/decision/strategies/rsi_rebound.py:52-54): RSI 超卖做多强度随 RSI 降低而下降，与注释相反
  - 证据引用: `# 计算强度：RSI 越低，强度越高
strength = max(10, 70 - int((30 - rsi) * 2))`
  - 影响声称: RSI 从 29 降到 10 时 strength 从约 68 降到 30，更强的超卖信号反而得到更低强度，可能被仲裁/过滤降级。
  - 建议: 改为单调递增公式，例如 strength = min(90, 70 + int((30 - rsi) * 2))。
- **qwen3.8-max-0902** (S2, logic_anomaly, app/decision/strategies/rsi_rebound.py:52-54): RSI 超卖做多强度与“RSI 越低强度越高”相反
  - 证据引用: `# 计算强度：RSI 越低，强度越高
strength = max(10, 70 - int((30 - rsi) * 2))`
  - 影响声称: RSI 越低，(30 - rsi) 越大，strength 越小；与注释相反，可能使更超卖的反弹信号获得更低强度。
  - 建议: 改为 RSI 越低 strength 越高，例如 70 + int((30 - rsi) * 2) 并设置上限。

<!-- DEB-VERDICT cluster=74 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: rsi_rebound.py:54 strength=max(10,70-int((30-rsi)*2)), RSI=10 时 strength=30, RSI=29 时 strength=68, 与 line 53 注释"RSI 越低强度越高"相反, 超卖越严重信号越弱

### 议题 #69 [共性 2/3] `app/coordinator/signal_arbitrator.py`:90-106 — 策略缺少 priority 时仲裁回退常量 5，优先级维度可能失效

环节: s5_decision_orchestration · 类别: config_effect, logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max** (S2, logic_anomaly, app/coordinator/signal_arbitrator.py:105-106): 仲裁平手依赖传入列表顺序，未显式使用信号到达时间
  - 证据引用: `scored.sort(key=lambda x: (-x[0], -x[1]))
winner_score, _, winner_strat, winner_sig = scored[0]`
  - 影响声称: 当 score 与 priority 相同，稳定排序只依赖 candidates 输入顺序；若调用方未按到达时间排序，所谓“先到先得”平手裁决会变成调用方顺序决定。
  - 建议: 为每个候选信号携带 timestamp/sequence，并将到达顺序显式纳入 sort key 或 tie-breaker。
- **qwen3.8-max-0902** (S2, config_effect, app/coordinator/signal_arbitrator.py:90-92): 策略缺少 priority 时仲裁回退常量 5，优先级维度可能失效
  - 证据引用: `priority = int(getattr(strat, "priority", 5))`
  - 影响声称: 本片段中的 BaseStrategy/内置策略未显式提供 priority；若实际传入对象也没有 priority/effective_priority，所有策略 priority 均为 5，仲裁退化为仅按 confidence 比较，不符合 priority × confidence 设计。
  - 建议: 在 BaseStrategy 或策略注册层强制定义 priority/effective_priority，并在仲裁前校验缺失。

<!-- DEB-VERDICT cluster=69 -->
裁决: REJECTED | 置信=high | 理由: signal_arbitrator.py:90-92 getattr(strat,"priority",5) 是标准 Python 缺省模式, 5 作为中性默认值使未设优先级策略与中优先级等同, 不构成逻辑缺陷; 同分排序依赖候选顺序是 Python 稳定排序的确定性行为

### 议题 #72 [共性 2/3] `app/decision/strategies/ma_cross.py`:106-114 — 均线死叉做空强度随空头排列增强而降低，与注释相反

环节: s5_decision_orchestration · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max** (S2, logic_anomaly, app/decision/strategies/ma_cross.py:106-114): 均线死叉做空强度随空头排列增强而降低，与注释相反
  - 证据引用: `# 计算强度：排列越整齐，强度越高
strength = 50 - int(alignment_score / 100 * 40)  # 10-50`
  - 影响声称: 空头排列越完整，alignment_score 越高，strength 反而越低，导致强趋势信号被降权。
  - 建议: 改为 strength = 50 + int(alignment_score / 100 * 40) 或其他单调递增公式。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/decision/strategies/ma_cross.py:107-114): MA 死叉空头排列越强，strength 反而越低
  - 证据引用: `# 计算强度：排列越整齐，强度越高
alignment_score = 0
strength = 50 - int(alignment_score / 100 * 40)  # 10-50`
  - 影响声称: 空头排列越完整，alignment_score 越高，但 strength 被减到更低；与注释和多头的强度语义相反，可能系统性削弱 SHORT 信号。
  - 建议: 将死叉强度改为 50 + int(alignment_score / 100 * 40) 或统一 strength 语义。

<!-- DEB-VERDICT cluster=72 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: ma_cross.py:114 死叉做空 strength=50-int(alignment_score/100*40), 空头排列满分100时strength仅10、排列混乱时strength接近50, 与 line 107 注释"排列越整齐强度越高"完全相反; 对比同文件 line 77 多头用 PLUS 正确

### 议题 #68 [共性 2/3] `app/coordinator/dual_mode_params.py`:130-134 — 自动交易计数跨日重置使用服务器 date.today() 而非 ET 交易日

环节: s5_decision_orchestration · 类别: time_boundary · 自报最高 severity: S4
- **qwen3.8-max** (S3, time_boundary, app/coordinator/dual_mode_params.py:130-134): 自动交易计数跨日重置使用服务器 date.today() 而非 ET 交易日
  - 证据引用: `def _maybe_reset_counter() -> None:
    today = date.today()
    if _counter_state["date"] != today:
        _counter_state["date"] = today
        _counter_state["count"] = 0`
  - 影响声称: 若服务器时区为 CST，CST 午夜约为 ET 正午，会导致美股盘中交易额度提前重置，可能放大当日自动交易次数。
  - 建议: 跨日重置统一使用 app.utils.timezone.now_et().date()，并结合交易日历判断交易日。
- **qwen3.8-max-0902** (S4, time_boundary, app/coordinator/dual_mode_params.py:130-134): 当日自动交易计数按服务器本地 date.today() 重置
  - 证据引用: `today = date.today()
if _counter_state["date"] != today:
    _counter_state["date"] = today
    _counter_state["count"] = 0`
  - 影响声称: 若服务器时区不是 ET（文件注释已提示 CST 午夜 = ET 正午），自动交易额度会在 ET 盘中重置，导致当日交易限制失效或额度翻倍。
  - 建议: 跨日重置统一使用 app/utils/timezone.now_et().date()，并考虑持久化计数避免进程重启归零。

<!-- DEB-VERDICT cluster=68 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: dual_mode_params.py:131 使用 date.today() 即服务器本地日期重置交易计数, 若服务器时区非 ET 则跨日重置时刻偏移数小时, 可能导致 ET 交易日尾声交易被计入次日配额或次日交易提前放行

### 议题 #67 [共性 2/3] `app/agents/debate/claude_code_runner.py`:183-196 — _call_llm 忽略 model 参数，重试策略的切换模型不生效

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/agents/debate/claude_code_runner.py:183-196): _call_llm 忽略 model 参数，重试策略的切换模型不生效
  - 证据引用: `def _call_llm(self, prompt: str, system_prompt: str = None,
              model: str = None, temperature: float = 0.3) -> Optional[str]:
    from app.llm.unified_service import UnifiedLLMService
    return UnifiedLLMService.call_text(
        "claude_code", prompt,
        system=system_prompt or ""`
  - 影响声称: run_analysis 中 current_model/fallback 只修改本地变量，实际调用固定走 claude_code 路由；json_parse_error/model_not_found 的 switch_model 重试语义失效。
  - 建议: 将 model 传入 UnifiedLLMService 或按 model 选择路由，确保 fallback 模型真正生效。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/agents/debate/claude_code_runner.py:183-196): 重试中的 current_model/timeout 未传入 UnifiedLLMService，模型切换无效
  - 证据引用: `def _call_llm(self, prompt: str, system_prompt: str = None,
                  model: str = None, temperature: float = 0.3) -> Optional[str]:
return UnifiedLLMService.call_text(
    "claude_code", prompt,`
  - 影响声称: run_analysis 在 JSON 解析失败或 model_not_found 时切换 current_model，但 _call_llm 调用 UnifiedLLMService 时未使用 model/self.timeout，导致分类重试的 switch_model 语义实际不生效。
  - 建议: 将 model 和 timeout 传入 UnifiedLLMService.call_text，或在路由层显式支持模型覆盖。

<!-- DEB-VERDICT cluster=67 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: claude_code_runner.py:183 函数签名接受 model 参数, 但 line 191-196 UnifiedLLMService.call_text() 调用从未传入 model, 上方重试循环的 current_model 切换完全无效, 始终使用 YAML 路由默认模型

### 议题 #77 [共性 2/3] `app/orb/engine.py`:314-411 — 跨日重置不清理 _ranges，旧 ready 区间可能阻止当日重建

环节: s5_decision_orchestration · 类别: state_machine, time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S2, state_machine, app/orb/engine.py:387-411): 开盘区间 fallback 一旦 ready 即永久跳过，短暂数据缺失导致当日禁用
  - 证据引用: `if symbol in self._ranges and self._ranges[symbol].get("ready"):
    continue  # 已构建, 跳过
"ready": True,`
  - 影响声称: 09:30-09:45 内若首次取 K 线不足而生成 fallback，ready=True 后不会重试真实 K 线；数据短暂异常会使该标的当日无法进入正常 ORB 逻辑。
  - 建议: fallback 区间应标记 provisional/ready=False 或增加重试窗口，在 09:45 前持续尝试重建真实开盘区间。
- **qwen3.8-max-0902** (S3, state_machine, app/orb/engine.py:387-411): fallback 伪区间被标记 ready 后阻塞真实开盘区间重建
  - 证据引用: `if symbol in self._ranges and self._ranges[symbol].get("ready"):
    continue  # 已构建, 跳过
"ready": True,
"fallback": True,`
  - 影响声称: K 线短暂缺失时生成的 fallback 区间会占据 _ranges 并被视为 ready；随后 _build_opening_ranges 跳过该标的，真实开盘区间无法补建，而 _check_breakout 又拒绝 fallback，导致该标的当日无谓错过突破。
  - 建议: fallback 区间不应标记为可交易的 ready，或应允许在 09:45 前对 fallback 状态持续重建。
- **qwen3.8-max-0902** (S3, time_boundary, app/orb/engine.py:314-388): 跨日重置不清理 _ranges，旧 ready 区间可能阻止当日重建
  - 证据引用: `# 不再清除区间，允许隔夜持仓继续使用
if symbol in self._ranges and self._ranges[symbol].get("ready"):
    continue  # 已构建, 跳过`
  - 影响声称: 跨日后旧 _ranges 仍保留；若旧 opening range 仍是 ready 且非 rolling，Phase 1 的 ready_count 和跳过逻辑可能把它当作当日已构建区间，导致使用昨日区间做今日突破判断。
  - 建议: 将隔夜持仓所需区间与当日可交易区间分离；每日开盘前重置或失效非持仓必需的 _ranges。

<!-- DEB-VERDICT cluster=77 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: engine.py:313-320 跨日重置明确注释"不再清除区间"(line 320), 配合 line 387 的 ready 跳过逻辑, 导致昨日 fallback 伪区间(line 401-410, quality_score=0.3)永久阻止今日真实开盘区间重建, ORB 突破检测使用昨日 high/low

### 议题 #80 [共性 2/3] `app/orb/engine.py`:2240-2269 — ORB 下单前风控/PDT 门禁异常被静默吞掉后继续下单

环节: s5_decision_orchestration · 类别: config_effect, error_swallowing · 自报最高 severity: S3
- **qwen3.8-max** (S3, config_effect, app/orb/engine.py:2252-2257): ORB 冷却门禁依赖未注入的 _scheduler，实际永不生效
  - 证据引用: `_scheduler = getattr(self, '_scheduler', None) or getattr(self, 'scheduler', None)`
  - 影响声称: ORBEngine.__init__ 未接收/保存 scheduler，该属性通常为 None，导致 _is_in_cooldown 检查被跳过，同标的止损冷却对 ORB 不生效。
  - 建议: 在 ORBEngine 构造时注入 scheduler/冷却服务，或改为调用独立 cooldown registry；若无法获取应 fail-close。
- **qwen3.8-max** (S3, error_swallowing, app/orb/engine.py:2240-2269): ORB 下单前风控/PDT 门禁异常被静默吞掉后继续下单
  - 证据引用: `            except Exception:
                pass`
  - 影响声称: RiskStateMachine、cooldown、PDT 任一依赖导入或检查异常时，门禁直接 pass，订单继续提交，可能绕过风控。
  - 建议: 门禁异常应返回失败或显式降级审批，不应默认放行；至少记录 error 并拒绝下单。
- **qwen3.8-max-0902** (S3, error_swallowing, app/orb/engine.py:2246-2248): RiskStateMachine 门禁异常被 pass，ORB 下单 fail-open
  - 证据引用: `return {"success": False, "error": f"risk_state_{_state}"}
            except Exception:
                pass`
  - 影响声称: RiskStateMachine 导入、实例化或检查失败时静默放行；同类 except pass 也出现在冷却期和 PDT 门禁附近，可能让 ORB 绕过关键风控。
  - 建议: 风控门禁异常应 fail-close 返回下单失败，或至少记录明确告警并阻断。

<!-- DEB-VERDICT cluster=80 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: engine.py:2247/2256/2269 三道风控门禁(RiskStateMachine/冷却期/PDT)的 except 块均为 pass 后继续执行到 line 2271 下单, 任一门禁异常即 fail-open; 且 line 2252 _scheduler 属性从未被赋值, 冷却门禁永远 None 跳过

### 议题 #95 [共性 2/3] `app/strategy/pure/insider.py`:73-90 — insider cluster 去重在门槛检查前写入，失败评估也会抑制后续有效信号

环节: s6_playbook_engine · 类别: logic_anomaly, state_machine · 自报最高 severity: S3
- **qwen3.8-max** (S2, logic_anomaly, app/strategy/pure/insider.py:75-90): insider cluster 去重在门槛检查前写入，被拒 cluster 也会锁定 24 小时
  - 证据引用: `    with _dedup_lock:
        last_emit = _cluster_dedup.get(fp)
        if last_emit and (now - last_emit) < _DEDUP_TTL:
            return None
        _cluster_dedup[fp] = now
        # 顺带清理过期条目
        expired = [k for k, v in _cluster_dedup.items() if (now - v) >= _DEDUP_TTL]
        for k in e`
  - 影响声称: 同一 fingerprint 首次即使因 min_buyers/min_amount/strength 不达标返回 None，也已被去重 24 小时；若数据后续修正或配置下调，同 cluster 不会重新评估。
  - 建议: 将 _cluster_dedup[fp] = now 移到所有门槛检查通过、即将返回 SignalCandidate 之前。
- **qwen3.8-max-0902** (S3, state_machine, app/strategy/pure/insider.py:73-90): insider cluster 去重在门槛检查前写入，失败评估也会抑制后续有效信号
  - 证据引用: `fp = _cluster_fingerprint(cluster, symbol)
    now = datetime.now()
    with _dedup_lock:
        last_emit = _cluster_dedup.get(fp)
        if last_emit and (now - last_emit) < _DEDUP_TTL:
            return None
        _cluster_dedup[fp] = now
    if cluster.unique_buyers < min_buyers:
        re`
  - 影响声称: 同一 cluster 首次评估若因数据瞬时不足或阈值未过而被拒绝，24 小时去重键仍已写入；后续同指纹数据修复或阈值配置变化时信号会被静默抑制，造成漏信号。
  - 建议: 将 `_cluster_dedup[fp] = now` 移动到所有门槛通过并确认生成 SignalCandidate 之后。

<!-- DEB-VERDICT cluster=95 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: insider.py:79 _cluster_dedup[fp]=now 在 86-92 行 min_buyers/min_buy_amount 门槛检查之前写入，失败的 cluster 也锁定 24h 去重窗口抑制后续有效信号

### 议题 #84 [共性 2/3] `app/playbook/pipeline.py`:431-471 — shadow_portfolio_entries 去重集合在写盘前置位，写盘失败会永久吞掉该行

环节: s6_playbook_engine · 类别: state_machine, time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, time_boundary, app/playbook/pipeline.py:470-471): 方向审计写入用本地时间戳, 下游按 ET 日期子串过滤 — CST 服务器上近半天记录错位
  - 证据引用: `pipeline.py 471: "timestamp": datetime.now().isoformat(), (同文件 361/436 行均用 _now_et()); playbook_service.py 700: if trade_date in ts: (trade_date 为 ET 日期); pipeline.py 543 注释自证: "entry_time 可能 naive (CST 服务器存)"`
  - 影响声称: 服务器为 CST 时, ET 约 11:00 之后写入的审计行时间戳落在 CST 次日, _get_audit_map(ET 今日) 子串匹配不到 → pipeline_view 的 audit 列从下午起缺失、并被错挂到次日, 方向仲裁审计 (ADR-008) 数据失真。
  - 建议: 写入侧改 datetime.now(_now_et()).isoformat() 与文件内其他埋点一致; 或读取侧把 timestamp 解析后按 ET 归日再比较, 不用子串匹配。
- **qwen3.8-max-0902** (S2, state_machine, app/playbook/pipeline.py:431-454): shadow_portfolio_entries 去重集合在写盘前置位，写盘失败会永久吞掉该行
  - 证据引用: `_SP_EMITTED.add(_lk)
        with open(_Psp(__file__).resolve().parents[2] / "data" / "shadow_portfolio_entries.jsonl",
                  "a", encoding="utf-8") as _fsp:
    except Exception:
        pass`
  - 影响声称: 若首次追加影子入场行时文件写入失败，`_SP_EMITTED` 已标记该 `(inst_id,date)`，后续轮询不会重试，导致影子组合样本丢失且异常被静默吞掉。
  - 建议: 仅在文件写入成功后再 `_SP_EMITTED.add(_lk)`；写失败至少记录 warning 并允许后续重试。

<!-- DEB-VERDICT cluster=84 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: pipeline.py:471 _log_playbook_direction_audit 用 datetime.now()（服务器本地时间）写 timestamp，playbook_service.py:700 读侧用 ET 日期子串过滤，CST 服务器近 2h/日记录错位

### 议题 #92 [共性 2/3] `app/playbook/playbook_service.py`:2122-2131 — skip_instruction 不检查 executed — 已执行指令可被改写为 outcome=skipped

环节: s6_playbook_engine · 类别: dataflow_inconsistency, state_machine · 自报最高 severity: S2
- **kimi-k3** (S2, state_machine, app/playbook/playbook_service.py:2122-2131): skip_instruction 不检查 executed — 已执行指令可被改写为 outcome=skipped
  - 证据引用: `2122-2131: inst = self._find_instruction_by_id(instruction_id) / if not inst: return {"success": False, "message": "指令不存在"} / self.db.update_instruction_tracking(instruction_id, {"outcome": "skipped"}); 对比同文件 747: if inst.get("executed"): return {"success": False, "message": "指令已执行"}`
  - 影响声称: 对已执行 (executed=1、持仓存在) 的指令调用 skip 会直接覆盖 outcome=skipped → 指令状态机出现 executed=1 ∧ outcome=skipped 的矛盾态, 统计/对账/反馈口径混乱。
  - 建议: 与 execute_instruction 对齐, 前置守卫: if inst.get("executed"): return {"success": False, "message": "指令已执行, 不可跳过"}。
- **qwen3.8-max** (S2, dataflow_inconsistency, app/playbook/playbook_service.py:2126-2131): skip_instruction 只写 outcome=skipped，不把 skip_reason 写回指令跟踪表
  - 证据引用: `        skip_reason = custom_reason or reason or "用户主动跳过"

        # 更新 DB
        self.db.update_instruction_tracking(instruction_id, {
            "outcome": "skipped",
        })`
  - 影响声称: playbook_instructions 侧的 skip_reason 字段不会被更新，而 Dashboard/其他执行路径依赖 skip_reason；人工跳过原因只存在于 feedback 表，查询口径不一致。
  - 建议: 在 update_instruction_tracking 中同时写入 {"outcome": "skipped", "skip_reason": skip_reason}。

<!-- DEB-VERDICT cluster=92 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: playbook_service.py:2122-2131 skip_instruction 未检查 inst["executed"]（对比 747 行 execute_instruction 有此守卫），已执行指令可被改写 outcome=skipped 造成状态不一致

### 议题 #87 [共性 2/3] `app/playbook/pipeline.py`:9008-9018 — EOD 订单已成交但无法取得价格时直接 continue，trade_records 保持未平仓

环节: s6_playbook_engine · 类别: logic_anomaly, state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/playbook/pipeline.py:9008-9018): EOD 订单已成交但无法取得价格时直接 continue，trade_records 保持未平仓
  - 证据引用: `            # 订单已成交 → 用 filled_price 记账 (优先), fill=0 时用当前报价兜底
            if exit_price <= 0:
                logger.info(f"[playbook dual] EOD {symbol} order_filled 但 fill=0 (src={_price_source}), 用报价记账")
                try:
                    q = data_router.get_quote(symbol)
                   `
  - 影响声称: 若 broker 已确认 order_filled 但 filled_price 与报价均不可得，DB 中的 trade_records 不会被平仓；后续监控可能继续把它当作持仓，下一轮还可能重复尝试平仓或进入 ghost 路径。
  - 建议: 对 order_filled 但价格缺失的情况设置兜底价（如 entry_price 并标 void/price_source=entry_fallback），或至少增加 fail_count/告警，不能无限期保持 open。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/playbook/pipeline.py:9009-9018): EOD 订单已成交但无成交价且报价兜底失败时静默 continue，不计失败次数
  - 证据引用: `if exit_price <= 0:
                _price_source = "quote_fallback"
            if exit_price <= 0:
                continue`
  - 影响声称: `order_filled` 为 True 但 `filled_price` 缺失且行情也取不到时，仓位不被 `_close_trade_record` 关闭，也不增加 `eod_close_fail_count` 或告警；孤儿仓可能无重试、无可见性。
  - 建议: 该分支应计入失败次数/告警，或在确认订单已成交时使用入场价/限价等保守兜底记账并标记异常来源。

<!-- DEB-VERDICT cluster=87 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: pipeline.py:9017-9018 EOD 订单已成交但报价兜底失败 exit_price 仍 ≤0 时 continue 且不增 _fail_count，trade_record 保持未平仓无限重试（自修复但延迟）

### 议题 #100 [共性 2/3] `app/execution/slippage_control.py`:14-38 — 滑点计算未保护 expected_price=0，会除零异常

环节: s7_risk_execution · 类别: numeric_unit · 自报最高 severity: S3
- **qwen3.8-max** (S3, numeric_unit, app/execution/slippage_control.py:14-38): 滑点计算未保护 expected_price=0，会除零异常
  - 证据引用: `slippage = (actual_price - expected_price) / expected_price
            slippage = (expected_price - actual_price) / expected_price
            return (actual_price - expected_price) / expected_price
            return (expected_price - actual_price) / expected_price`
  - 影响声称: 当 expected_price 为 0（例如市价单缺参考价、数据缺失或异常信号）时，is_acceptable/calculate_slippage 会抛出 ZeroDivisionError，可能中断执行或风控检查路径。
  - 建议: 在计算前校验 expected_price > 0；无效价格时返回不可接受滑点或显式错误，而不是让异常扩散。
- **qwen3.8-max-0902** (S2, numeric_unit, app/execution/slippage_control.py:21-26): 滑点计算未防护 expected_price 为 0
  - 证据引用: `        if side == "BUY":
            slippage = (actual_price - expected_price) / expected_price
        else:  # SELL
            slippage = (expected_price - actual_price) / expected_price
        
        return slippage <= self.max_slippage`
  - 影响声称: expected_price 为 0 时触发 ZeroDivisionError；若上游在异常行情或市价单场景传入 0，会导致滑点检查异常并可能中断订单流程。
  - 建议: expected_price<=0 时返回不可接受或走降级逻辑，避免除零。

<!-- DEB-VERDICT cluster=100 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: slippage_control.py L22 和 L36 直接除以 expected_price 无零值保护，expected_price=0 必抛 ZeroDivisionError；实际运行时上游通常保证 price>0 但函数接口无防御

### 议题 #99 [共性 2/3] `app/execution/order_state_machine.py`:15-62 — 订单状态机缺少 CANCEL_REQUESTED 及 SENT 到部分成交的关键转换

环节: s7_risk_execution · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max** (S3, state_machine, app/execution/order_state_machine.py:25-62): UNKNOWN 被定义为终态但既非 complete 也非 active
  - 证据引用: `OrderState.UNKNOWN: set(),  # 终态
        return self.current_state in {
            OrderState.FILLED,
            OrderState.CANCELED,
            OrderState.REJECTED,
            OrderState.EXPIRED,
        }
        return self.current_state in {
            OrderState.CREATED,
            OrderS`
  - 影响声称: 订单进入 UNKNOWN 后，VALID_TRANSITIONS 将其视为终态，但 is_complete 返回 False、is_active 也返回 False。依赖这两个状态判断生命周期的代码可能既不继续处理也不清理，导致订单悬挂、资源占用或后续撤单/补偿逻辑不触发。
  - 建议: 将 UNKNOWN 纳入 is_complete 的终态集合，或显式提供 is_terminal/is_unknown 处理路径；对 UNKNOWN 订单应触发查询、对账或人工告警。
- **qwen3.8-max-0902** (S3, state_machine, app/execution/order_state_machine.py:15-26): 订单状态机缺少 CANCEL_REQUESTED 及 SENT 到部分成交的关键转换
  - 证据引用: `    VALID_TRANSITIONS: Dict[OrderState, Set[OrderState]] = {
        OrderState.CREATED: {OrderState.VALIDATED, OrderState.CANCELED},
        OrderState.VALIDATED: {OrderState.SENT, OrderState.CANCELED, OrderState.REJECTED, OrderState.FILLED},
        OrderState.SENT: {OrderState.ACKED, OrderState.R`
  - 影响声称: broker_adapter 会将 FILLED_PART 映射为 PARTIALLY_FILLED、CANCELLING_PART/ALL 映射为 CANCEL_REQUESTED；若订单当前处于 SENT 或需要进入取消请求态，transition 会抛 ValueError，导致订单生命周期处理中断或状态卡死。
  - 建议: 补充 SENT->PARTIALLY_FILLED/FILLED、ACKED/SENT->CANCEL_REQUESTED、CANCEL_REQUESTED->CANCELED/FILLED 等转换，覆盖 broker 实际可能回报的状态。

<!-- DEB-VERDICT cluster=99 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: order_state_machine.py L25 将 UNKNOWN 定义为终态(空转换集)，但 is_complete()(L47-52) 和 is_active()(L56-62) 的集合均不含 UNKNOWN，处于 UNKNOWN 状态的订单既非完成也非活跃，状态查询逻辑出现空洞；SENT→PARTIALLY_FILLED 直接转换路径确实缺失(L18)

### 议题 #97 [共性 2/3] `app/execution/broker_adapter.py`:55-941 — 非重入锁内递归重连会导致死锁

环节: s7_risk_execution · 类别: dataflow_inconsistency, logic_anomaly, numeric_unit, race_condition, resource_leak, state_machine · 自报最高 severity: S5
- **qwen3.8-max** (S4, race_condition, app/execution/broker_adapter.py:55-391): 非重入锁内递归重连会导致死锁
  - 证据引用: `self._reconnect_lock = threading.Lock()
        with self._reconnect_lock:
                if self._reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS:
                    self._attempt_reconnect()`
  - 影响声称: _attempt_reconnect 在持有 threading.Lock 时递归调用自身；threading.Lock 非重入，递归会再次申请同一把锁，导致重连线程死锁。重连失败后无法完成恢复，也可能无法触发断连回调，使后续订单连接状态判断失真。
  - 建议: 将递归重试改为循环，并在锁外执行 connect/disconnect；或改用 threading.RLock，同时避免在持锁期间做长阻塞网络调用。
- **qwen3.8-max** (S5, race_condition, app/execution/broker_adapter.py:519-578): 撤单/改单在断连或 ImportError 时虚假返回成功
  - 证据引用: `if not self.is_connected():
            logger.warning("[富途] 未连接，模拟取消成功")
            return True
        except ImportError:
            return True
        if not self.is_connected():
            logger.warning("[富途] 未连接，模拟改单成功")
            return True
        except ImportError:
            retu`
  - 影响声称: cancel_order/amend_order 在无法接触券商时返回 True，上游订单状态机可能把订单标记为已取消/已改单，但交易所侧订单仍可能存活。后续重新下单、平仓或风控占用计算会与真实订单状态不一致，产生重复下单、撤单竞态或资金占用误判。
  - 建议: 断连或依赖缺失时应返回 False/UNKNOWN，并触发订单状态查询与对账；不得在真实撤单/改单未确认前返回成功。
- **qwen3.8-max** (S5, logic_anomaly, app/execution/broker_adapter.py:415-514): 断连/缺依赖时按环境变量而非真实交易环境模拟成交
  - 证据引用: `if not self.is_connected():
            _auto_acc_disc = os.environ.get("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            if _auto_acc_disc == "real":
                raise RuntimeError(
                    f"[实盘安全] Broker 未连接, 拒绝提交 {order.symbol} 订单 — "
                    f"请勿在断连状态下交易"
    `
  - 影响声称: submit_order 是否模拟成交取决于 AUTO_TRADE_ACCOUNT 字符串，而不是适配器自身 trade_env。如果实例以实盘语义创建但环境变量未设置为 real，断连或缺依赖时会返回 simulated_fill，制造幽灵成交与虚假持仓；真实订单可能并未发送到券商。
  - 建议: 以 self.trade_env == 0 或明确实盘配置作为唯一实盘判定；实盘路径断连/缺依赖必须 fail-close，禁止返回模拟成交。
- **qwen3.8-max** (S4, numeric_unit, app/execution/broker_adapter.py:81-941): 名义金额硬兜底对零价/无效价格订单失效
  - 证据引用: `if qty <= 0 or price <= 0:
            return True  # 无效参数不在此拦截（上游应已处理）
            if not self._notional_sanity_check(order.symbol, order.quantity, order.price, is_close=_is_close_order):
                if not self._notional_sanity_check(symbol, qty, price if price > 0 else 1.0):`
  - 影响声称: MARKET 单或 price<=0 的订单会直接通过 _notional_sanity_check；try_direct_short 对市价卖空用 1.0 作为价格兜底，导致名义金额被严重低估。broker 层 30% equity 硬兜底在关键市价单场景形同虚设，可能放行超大订单。
  - 建议: 对无价格订单使用最近成交价/参考价估算名义金额，或直接拒绝无法估算名义金额的订单；qty<=0 也应视为无效订单而非放行。
- **qwen3.8-max** (S4, logic_anomaly, app/execution/broker_adapter.py:183-249): 真实账户探测失败设置的 connected=False 被 connect 覆盖
  - 证据引用: `self._discover_real_account()

            self.connected = True
                # 不 fallback，阻断连接
                self.connected = False`
  - 影响声称: _discover_real_account 在 REAL_ACCOUNT_ID 指定账户不存在时显式设置 self.connected=False 以阻断连接，但 connect 随后无条件设置 self.connected=True。无效账户配置被吞掉，适配器误认为连接成功，后续下单可能落到错误/空账户。
  - 建议: 让 _discover_real_account 返回 bool，connect 根据探测结果决定是否置 connected=True；账户探测失败应立即关闭 trade_ctx 并返回连接失败。
- **qwen3.8-max** (S3, numeric_unit, app/execution/broker_adapter.py:700-700): 持仓 current_price 用 market_val/max(1, qty) 计算，零/负 qty 会失真
  - 证据引用: `"current_price": float(row.get("market_val", 0)) / max(1, float(row.get("qty", 1))),`
  - 影响声称: 若 qty 为 0、缺失或负数（例如平仓后残余市值、做空数量符号约定不一致），分母被强制为 1，current_price 会变成 market_val 或严重偏离真实价格。下游若消费该 current_price，可能误判盈亏、止损距离或仓位价值。
  - 建议: qty<=0 时跳过持仓或使用券商 last_price；对做空持仓明确数量符号约定，并在价格推导时保留原始 qty 符号与数量。
- **qwen3.8-max** (S2, resource_leak, app/execution/broker_adapter.py:139-182): 实盘解锁失败时已创建的 trade_ctx 未关闭
  - 证据引用: `self.trade_ctx = OpenSecTradeContext(
                filter_trdmarket=TrdMarket.US,
                host=self.host,
                port=self.port,
                is_encrypt=False,
            )
                    self.connected = False
                    return False`
  - 影响声称: connect 在创建 OpenSecTradeContext 后，如果实盘解锁失败直接返回 False，没有关闭已创建的 trade_ctx。重复连接失败会泄漏 OpenD 连接资源，可能耗尽连接槽位。
  - 建议: 在所有失败分支调用 self.disconnect() 或显式 close trade_ctx 后再返回 False。
- **qwen3.8-max** (S3, state_machine, app/execution/broker_adapter.py:479-483): 订单状态检查把 CANCELLED_PART 也当失败，可能丢弃部分成交
  - 证据引用: `if 'FAIL' in status or 'DEL' in status or 'CANCEL' in status:
                            logger.error(f"🚨 [订单失败] {order.symbol} Status={status}")
                            raise RuntimeError(f"订单提交后失败: {order.symbol} status={status}")`
  - 影响声称: 状态包含 CANCEL 即抛异常，CANCELLED_PART 也会命中。若订单已部分成交后被取消，调用方可能把整笔订单当作提交失败处理，导致部分成交未被入账、持仓/资金状态与券商不一致。
  - 建议: 区分 CANCELLED_PART 与完全取消/失败；对部分成交应返回已成交数量并进入部分成交状态，而不是直接抛 RuntimeError。
- **qwen3.8-max-0902** (S4, race_condition, app/execution/broker_adapter.py:353-391): 重连函数在非重入锁内递归调用自身，会死锁
  - 证据引用: `        with self._reconnect_lock:
                if self._reconnect_attempts < self.MAX_RECONNECT_ATTEMPTS:
                    self._attempt_reconnect()`
  - 影响声称: threading.Lock 不是可重入锁；首次重连失败后在仍持有锁的情况下递归调用 _attempt_reconnect，会永久阻塞心跳/重连线程，导致断连后无法完成后续重连或触发断开回调。
  - 建议: 改为循环重试而非递归，或在释放锁后再发起下一轮重试；如确需递归，应使用 threading.RLock。
- **qwen3.8-max-0902** (S4, race_condition, app/execution/broker_adapter.py:521-555): 断连时撤单/改单直接返回成功，造成虚假取消/修改
  - 证据引用: `        if not self.is_connected():
            logger.warning("[富途] 未连接，模拟取消成功")
            return True
        if not self.is_connected():
            logger.warning("[富途] 未连接，模拟改单成功")
            return True`
  - 影响声称: 撤单或改单请求在 broker 断连时被标记为成功，但交易所侧订单可能仍然存活；上游若据此释放风控、重发订单或更新状态，可能产生重复下单、未撤销旧单、仓位/资金计算错误。
  - 建议: 断连时撤单/改单应返回失败或 UNKNOWN，并触发订单状态重查/对账；不能将本地模拟成功当作 broker 侧已生效。
- **qwen3.8-max-0902** (S4, dataflow_inconsistency, app/execution/broker_adapter.py:415-424): submit_order 断连时是否模拟成交取决于环境变量而非适配器 trade_env
  - 证据引用: `        if not self.is_connected():
            _auto_acc_disc = os.environ.get("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            if _auto_acc_disc == "real":
                raise RuntimeError(
            return self._simulate_fill(order)`
  - 影响声称: 适配器自身已有 trade_env=0 表示真实环境，但断连提交时只按 AUTO_TRADE_ACCOUNT 判断；若实盘适配器在 AUTO_TRADE_ACCOUNT 未设为 real 的情况下断连，会返回 simulated_fill，形成真实交易意图下的幻影成交。
  - 建议: 实盘安全判断应与 self.trade_env 或统一账户配置强一致；trade_env==0 时断连必须 fail-close，禁止 simulated_fill。
- **qwen3.8-max-0902** (S3, numeric_unit, app/execution/broker_adapter.py:81-82): 名义硬兜底在 price<=0 时直接放行，市价单可绕过 30% 资金上限
  - 证据引用: `        if qty <= 0 or price <= 0:
            return True  # 无效参数不在此拦截（上游应已处理）`
  - 影响声称: submit_order 用 order.price 调用 _notional_sanity_check；若市价单或异常订单 price 为 0，兜底名义金额检查被直接跳过，最后一道资金安全墙失效。
  - 建议: 开仓单缺少有效价格时应 fail-close，或使用最近价/参考价计算名义金额；不应把 price<=0 当成功放行。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/execution/broker_adapter.py:184-249): 真实账户探测失败设置的 connected=False 被 connect 随后覆盖为 True
  - 证据引用: `                self._discover_real_account()

            self.connected = True
                # 不 fallback，阻断连接
                self.connected = False`
  - 影响声称: _discover_real_account 在 REAL_ACCOUNT_ID 无效时试图阻断连接，但 connect() 不检查其结果并立即 self.connected=True；无效真实账户仍可能进入下单链路，导致拒单或错误账户路由。
  - 建议: 让 _discover_real_account 返回 bool，connect 根据返回值决定是否 return False；不要在探测失败后无条件置 connected=True。

<!-- DEB-VERDICT cluster=97 -->
裁决: CONFIRMED | severity=5 | 置信=high | 理由: _attempt_reconnect 在 L353 持 threading.Lock 后于 L391 递归调用自身，Lock 非重入必死锁 (L55); cancel_order(L521) 和 amend_order(L553) 在断连时无条件返回 True，实盘模式下造成虚假取消/改单; _discover_real_account 于 L249 设 connected=False 被 connect() L186 无条件覆盖为 True，指定账户不存在时仍放行下单

### 议题 #101 [共性 2/3] `app/risk/capital_safety.py`:163-168 — 净值非正触发 BLACKOUT 后未持久化状态

环节: s7_risk_execution · 类别: logic_anomaly, state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S2, logic_anomaly, app/risk/capital_safety.py:163-168): 净值非正触发 BLACKOUT 后未持久化状态
  - 证据引用: `if current_equity <= 0:
            logger.error(f"[资本安全] 净值为 0 或负数: ${current_equity:,.2f}")
            self._state.water_level = WaterLevel.BLACKOUT
            self._state.is_locked = True
            self._state.lock_reason = "净值为零或负数"
            return self._state`
  - 影响声称: 该分支直接 return，未调用 _save_state。若进程随后重启，净值非正导致的 BLACKOUT/锁定状态可能丢失，系统可能基于旧状态重新允许开仓。
  - 建议: 在返回前调用 _save_state()，或统一走保存逻辑后再返回；DB 保存失败时维持 fail-safe 拦截。
- **qwen3.8-max-0902** (S4, state_machine, app/risk/capital_safety.py:163-168): 净值<=0 触发 BLACKOUT 锁定时未持久化状态
  - 证据引用: `        if current_equity <= 0:
            logger.error(f"[资本安全] 净值为 0 或负数: ${current_equity:,.2f}")
            self._state.water_level = WaterLevel.BLACKOUT
            self._state.is_locked = True
            self._state.lock_reason = "净值为零或负数"
            return self._state`
  - 影响声称: 该分支直接 return，未调用 _save_state；进程重启后可能从 DB 恢复旧的未锁定/正净值状态，绕过熔断锁定，继续允许开仓。
  - 建议: 在设置 BLACKOUT/is_locked 后立即 _save_state() 再返回，或统一走保存后的异常分支。

<!-- DEB-VERDICT cluster=101 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: capital_safety.py L163-168 在 current_equity<=0 时设置 BLACKOUT+is_locked=True 后立即 return，跳过 L254 _save_state()，崩溃重启后 DB 无 BLACKOUT 记录，_load_state(L708) 加载旧 GREEN 状态导致熔断丢失

### 议题 #102 [共性 2/3] `app/risk/capital_safety.py`:680-806 — capital_state 表缺少 trade_env 唯一约束，INSERT OR REPLACE 实际仍会无限插行

环节: s7_risk_execution · 类别: resource_leak · 自报最高 severity: S2
- **qwen3.8-max** (S2, resource_leak, app/risk/capital_safety.py:680-806): INSERT OR REPLACE 缺少唯一键，实际仍会无限插行
  - 证据引用: `CREATE TABLE IF NOT EXISTS capital_state (
                    id INTEGER PRIMARY KEY,
                INSERT OR REPLACE INTO capital_state 
                (initial_capital, current_equity, peak_equity, cash, market_value, 
                 buying_power, drawdown_pct, max_drawdown_pct, water_level,`
  - 影响声称: capital_state 只有自增 id 主键，没有 trade_env 唯一约束；INSERT OR REPLACE 未提供冲突键，实际不会按环境替换旧行。每次 update_account 仍会新增一行，长期导致 SQLite 表膨胀。
  - 建议: 为 capital_state 增加 UNIQUE(trade_env) 或改用 ON CONFLICT(trade_env) DO UPDATE，并在迁移中清理历史重复行。
- **qwen3.8-max-0902** (S2, resource_leak, app/risk/capital_safety.py:680-696): capital_state 表缺少 trade_env 唯一约束，INSERT OR REPLACE 实际仍会无限插行
  - 证据引用: `            c.execute("""
                CREATE TABLE IF NOT EXISTS capital_state (
                    id INTEGER PRIMARY KEY,
                    initial_capital REAL,
                    current_equity REAL,
                    peak_equity REAL,
                    cash REAL,
                   `
  - 影响声称: _save_state 注释声称按 trade_env UPSERT，但表只有 id 主键且无 UNIQUE(trade_env)；INSERT OR REPLACE 没有冲突键可命中，每次更新都会新增一行，长期造成 DB 膨胀并可能拖慢加载。
  - 建议: 为 trade_env 建唯一索引/约束，或显式 UPDATE/DELETE 旧行后插入；迁移时清理历史重复行。

<!-- DEB-VERDICT cluster=102 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: capital_safety.py L680-696 CREATE TABLE 仅声明 id INTEGER PRIMARY KEY，trade_env 列无 UNIQUE 约束；L787 INSERT OR REPLACE 因 trade_env 非唯一键退化为普通 INSERT，每次 _save_state() 追加新行导致表无限增长；_load_state 用 ORDER BY id DESC LIMIT 1(L714) 取最新行功能不受影响

### 议题 #123 [共性 2/3] `app/web_dashboard/order/oco_handler.py`:35-73 — OCO 推送只处理首条订单且只处理 FILLED_ALL，部分成交/批量推送会漏处理

环节: s8_monitoring_surface · 类别: logic_anomaly, race_condition · 自报最高 severity: S4
- **qwen3.8-max** (S4, race_condition, app/web_dashboard/order/oco_handler.py:61-73): OCO 取消配对单时不检查撤单结果就清除 DB 映射
  - 证据引用: `            if order_id == sl_id and tp_id:
                # SL 成交，取消 TP
                logger.info(f"[OCO] {symbol} SL 成交 (order_id={order_id})，取消 TP (order_id={tp_id})")
                self.service._cancel_broker_order(tp_id)
                
                # 清除 DB 记录
                conn = se`
  - 影响声称: _cancel_broker_order 返回 bool，但这里忽略返回值。若撤单因网络/已成交等原因失败，配对订单仍留在 broker，而 DB OCO 映射已清除，后续无法自动取消，可能导致重复平仓或裸单。
  - 建议: 检查撤单结果；失败时保留 DB 映射、重试撤单并告警，仅在确认已撤/已成交后清除映射。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/web_dashboard/order/oco_handler.py:35-42): OCO 推送只处理首条订单且只处理 FILLED_ALL，部分成交/批量推送会漏处理
  - 证据引用: `row = ret_data.iloc[0] | if "FILLED_ALL" not in order_status: return ret_code, ret_data`
  - 影响声称: 若一次推送包含多条订单更新，仅处理第一条；若 SL/TP 部分成交，不会取消配对单，可能导致 SL 与 TP 同时存活，触发重复平仓或反向风险。
  - 建议: 遍历 ret_data 全部订单，并对部分成交/全部成交都执行配对单检查；部分成交时应取消或按比例调整另一条保护单。

<!-- DEB-VERDICT cluster=123 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: oco_handler.py:64 调用 _cancel_broker_order 未检查返回值即于 line 68-73 清除 DB 映射, 撤单失败时配对单在 broker 仍活跃但 DB 已失联形成孤儿挂单

### 议题 #111 [共性 2/3] `app/push/health.py`:69-74 — 通道连续失败 3 次只记日志，没有降级/熔断动作

环节: s8_monitoring_surface · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max** (S2, logic_anomaly, app/push/health.py:69-74): 通道连续失败 3 次只记日志，没有降级/熔断动作
  - 证据引用: `                    self._fail_count[ch.name] = self._fail_count.get(ch.name, 0) + 1
                    if self._fail_count[ch.name] >= 3:
                        logger.warning(
                            f"[PushHealth] {ch.name} 连续失败 {self._fail_count[ch.name]} 次, "
                            f`
  - 影响声称: 连续失败仅产生日志，没有调整优先级、标记不可用或熔断；失败通道仍可能继续作为推送通道使用，导致告警持续丢失。
  - 建议: 实现失败降级逻辑：调整 priority、标记 unavailable、暂停该通道并切换备用通道，同时发出可观测告警。
- **qwen3.8-max-0902** (S2, logic_anomaly, app/push/health.py:70-74): 健康监控声称连续失败自动降级，但代码只打警告未调整优先级
  - 证据引用: `if self._fail_count[ch.name] >= 3: logger.warning(f"[PushHealth] {ch.name} 连续失败 {self._fail_count[ch.name]} 次, 建议人工检查")`
  - 影响声称: 连续失败的通道不会被降权或禁用，推送仍可能优先选择坏通道，造成关键告警继续失败。
  - 建议: 实现文档所述降级逻辑：连续失败达到阈值时提高 priority、标记 unavailable 或暂停通道，并记录恢复条件。

<!-- DEB-VERDICT cluster=111 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: health.py:70-74 连续失败>=3 次仅 logger.warning 建议人工检查, 文件头注释声称自动降级(优先级+50)但代码中无任何降级或熔断动作

### 议题 #109 [共性 2/3] `app/push/aggregator.py`:96-105 — 聚合桶先被 pop 再调用推送回调，回调失败时聚合消息永久丢失

环节: s8_monitoring_surface · 类别: error_swallowing · 自报最高 severity: S3
- **qwen3.8-max** (S3, error_swallowing, app/push/aggregator.py:98-105): 聚合窗口 flush 失败后消息桶被丢弃且无重试
  - 证据引用: `        bucket = self._buckets.pop(key, None)
        if not bucket or not bucket.messages:
            return
        try:
            agg_msg = self._build_summary(bucket)
            self._flush_cb(agg_msg)
        except Exception as e:
            logger.warning(f"[PushAggregator·flush] {key} 失`
  - 影响声称: flush_callback 发送聚合消息失败（抛异常）时，桶已被 pop，消息不会重放或进入死信，低优先级交易事件可能永久丢失。
  - 建议: 失败时将聚合消息写入本地死信/重试队列，或恢复桶并限制重试次数；同时检查 callback 返回值。
- **qwen3.8-max-0902** (S3, error_swallowing, app/push/aggregator.py:96-105): 聚合桶先被 pop 再调用推送回调，回调失败时聚合消息永久丢失
  - 证据引用: `bucket = self._buckets.pop(key, None) | agg_msg = self._build_summary(bucket) | self._flush_cb(agg_msg) | logger.warning(f"[PushAggregator·flush] {key} 失败: {e}")`
  - 影响声称: flush_callback 失败只记录 warning，但桶已被移除且无重试；低优先级聚合告警在网络/通道故障时会静默丢失。
  - 建议: 回调失败时应将聚合消息放入重试队列或重新入桶，并限制重试次数；关键级别消息不应只依赖一次性回调。

<!-- DEB-VERDICT cluster=109 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: aggregator.py:98 先 pop 桶再 line 103 调用 _flush_cb, 回调异常时 line 104-105 仅记 warning 但桶内消息已永久丢失无重试机制

### 议题 #116 [共性 2/3] `app/web_dashboard/order/executor.py`:219-273 — 订单已成功提交后，未包裹的 _mark_position_manual 异常会把订单误报为提交异常

环节: s8_monitoring_surface · 类别: race_condition, state_machine · 自报最高 severity: S3
- **qwen3.8-max** (S3, race_condition, app/web_dashboard/order/executor.py:270-273): 下单异常/超时不记录幂等 key，broker 已收单时重试可能重复下单
  - 证据引用: `        except Exception as e:
            order.status = "error"
            order.error_msg = "订单提交异常"
            logger.error(f"[手动下单] 下单异常: {e}")`
  - 影响声称: place_order 若因网络超时等原因抛异常，但 broker 实际已接收订单，由于成功后才记录幂等 key，同 key 重试不会被拦截，可能造成重复真实下单。
  - 建议: 对超时/未知状态增加订单查询/对账确认，或使用请求级预占 + 查询补偿机制，避免未知状态直接允许盲目重试。
- **qwen3.8-max-0902** (S3, state_machine, app/web_dashboard/order/executor.py:219-273): 订单已成功提交后，未包裹的 _mark_position_manual 异常会把订单误报为提交异常
  - 证据引用: `service._mark_position_manual(order.symbol, order.direction) | except Exception as e: order.status = "error" | order.error_msg = "订单提交异常"`
  - 影响声称: place_order RET_OK 且 record_idempotency 已记录后，如果 _mark_position_manual 抛异常，外层会把订单状态改为 error；用户看到失败但真实订单已存在，且同 key 重试会被幂等拒绝。
  - 建议: 将成功提交后的标记/注册/审计等后处理单独 try/except；即使失败也应保持 order.status=submitted 并返回后处理告警。

<!-- DEB-VERDICT cluster=116 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:219 _mark_position_manual 未被 try/except 包裹, 异常传播至 line 270 外层 except 将已成功提交的 order.status 覆写为 error 且 record_idempotency 已于 line 217 记录致重试被拒

### 议题 #117 [共性 2/3] `app/web_dashboard/order/executor.py`:429-728 — 手动平仓异常分支未释放 _close_in_progress/_pending_close 竞态锁

环节: s8_monitoring_surface · 类别: race_condition, state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/web_dashboard/order/executor.py:726-728): 手动平仓异常分支未释放 _pending_close/_close_in_progress 竞态锁
  - 证据引用: `        except Exception as e:
            logger.error(f"[手动平仓] 异常: {e}")
            return {"success": False, "error": "操作失败，请稍后重试"}`
  - 影响声称: 在 421-436 已设置 position metadata 竞态锁后，如果 place_order 或后续代码抛异常进入该外层 except，锁不会释放；ExitMonitor/组合止损可能因 _pending_close 永久跳过该标的，直到重启。
  - 建议: 在 try/except/finally 中判断 _lock_acquired，或统一调用 _release_close_locks(symbol)，确保所有异常路径释放竞态锁。
- **qwen3.8-max** (S3, race_condition, app/web_dashboard/order/executor.py:474-491): 平仓成交回报延迟时可能误判未成交并尝试撤单
  - 证据引用: `                if _filled_qty <= 0:
                    if "CANCEL" in _order_status.upper():
                        # 已取消: 不写 trade_records
                        logger.warning(
                            f"[手动平仓] {symbol} 订单已取消 ({_order_status}), 不写入 trade_records"
                        )
 `
  - 影响声称: 若 broker 成交回报延迟导致 _filled_qty 暂为 0，代码会按未成交处理并尝试撤单；如果订单实际已成交或撤单失败，本地仍返回未成交且不记账，可能造成仓位状态、记账与真实成交不一致。
  - 建议: 撤单后重新查询订单状态并检查 _cancel_broker_order 返回值；若撤单失败且订单已成交，按成交路径补偿记账/对账。
- **qwen3.8-max-0902** (S4, race_condition, app/web_dashboard/order/executor.py:429-728): 手动平仓异常分支未释放 _close_in_progress/_pending_close 竞态锁
  - 证据引用: `_meta['_close_in_progress'] = True | _meta['_pending_close'] = True | except Exception as e: logger.error(f"[手动平仓] 异常: {e}") | return {"success": False, "error": "操作失败，请稍后重试"}`
  - 影响声称: 若设置竞态锁后在下单/查询/记账任意环节抛异常，_pending_close 会残留；退出监控看到 _pending_close 会跳过该标的，导致该仓位后续失去自动退出/组合止损保护。
  - 建议: 在 close_position 的异常兜底或 finally 中统一调用 _release_close_locks(symbol)，确保只要进入过平仓尝试就释放竞态锁。
- **qwen3.8-max-0902** (S4, race_condition, app/web_dashboard/order/executor.py:435-438): 设置平仓竞态锁失败仍继续向 broker 提交平仓单
  - 证据引用: `logger.warning(f"[手动平仓] 设置竞态锁失败 (非致命): {_lock_err}") | ret, data = service._broker_ctx.place_order(`
  - 影响声称: 当 scheduler/position_store 不可用或导入失败时，手动平仓不会设置 _close_in_progress，ExitMonitor 可能同时触发平仓，造成手动/自动双通道重复平仓。
  - 建议: 竞态锁获取失败时应阻断手动平仓或至少要求显式确认；更稳妥是让平仓锁成为强依赖，失败即返回可重试错误。
- **qwen3.8-max-0902** (S3, race_condition, app/web_dashboard/order/executor.py:457-502): 平仓后仅等待 1 秒并以 dealt_qty<=0 判定未成交，可能误撤已成交单且不记账
  - 证据引用: `_t.sleep(1.0) | _filled_qty = float(_data2.iloc[0].get("dealt_qty", 0) or 0) | if _filled_qty <= 0: | service._cancel_broker_order(order.order_id) | order.error_msg = "平仓单未成交, 已自动撤单"`
  - 影响声称: 市价单或快速成交限价单若 broker 回报延迟，可能被误判为未成交并尝试撤单；实际仓位已平但 trade_records 不记账，导致面板状态、盈亏和风控口径失真。
  - 建议: 对市价单/平仓单增加成交确认重试与终态查询，区分可撤挂单与已成交/部分成交；未知状态不要直接撤单，应进入人工/对账队列。
- **qwen3.8-max-0902** (S3, state_machine, app/web_dashboard/order/executor.py:557-713): 全平成功路径仅依赖 position_store.remove，移除失败时不释放竞态锁
  - 证据引用: `if _effective_full: | _sched.position_store.remove(symbol) | if not _effective_full: | _release_close_locks(symbol)`
  - 影响声称: 注释已说明全平路径 position_store.remove 失败需要兜底释放，但代码只在非全平时释放；若 remove 失败，_pending_close/_close_in_progress 会残留，导致该标的被组合级止损跳过。
  - 建议: 在 _effective_full 分支中，无论 remove 是否成功都调用 _release_close_locks(symbol)，或在 remove 失败时显式清理 metadata 锁。

<!-- DEB-VERDICT cluster=117 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:726-728 外层 except 未调用 _release_close_locks, 若 line 433 设置锁后任何后续代码抛异常则 _pending_close 永久残留致 ExitMonitor 永久跳过该标的

### 议题 #113 [共性 2/3] `app/web_dashboard/data_service.py`:788-792 — enriched 持仓视图未兼容 DB fallback 字段名

环节: s8_monitoring_surface · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/web_dashboard/data_service.py:788-789): enriched 持仓视图未兼容 DB fallback 字段名
  - 证据引用: `            avg_cost = safe_float(p.get("average_cost"))
            cur_price = safe_float(p.get("nominal_price"))`
  - 影响声称: _fetch_positions_from_db 返回的是 cost_price/cur_price 字段，而此处读取 average_cost/nominal_price；broker 超时降级到 DB 时，面板会得到 0 成本价/现价，误导人工操作与风控展示。
  - 建议: 统一字段映射：p.get("average_cost", p.get("cost_price")) 和 p.get("nominal_price", p.get("cur_price"))，或在 DB fallback 中提供相同字段名。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/web_dashboard/data_service.py:788-792): enriched 持仓消费 Futu 字段，DB fallback 提供不同字段导致价格/成本变 0
  - 证据引用: `avg_cost = safe_float(p.get("average_cost")) | cur_price = safe_float(p.get("nominal_price"))`
  - 影响声称: _fetch_positions_from_db 返回的是 cost_price/cur_price，而 _build_enriched_positions 读取 average_cost/nominal_price；当 broker 超时或不可用走 DB fallback 时，面板会显示成本价/现价为 0，误导人工操作。
  - 建议: 统一持仓字段契约：在 DB fallback 中补齐 average_cost/nominal_price，或在 enriched 构建时兼容 cost_price/cur_price/data_source。

<!-- DEB-VERDICT cluster=113 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: data_service.py:788-789 读取 Futu 字段 average_cost/nominal_price, 但 DB fallback(line 377-393) 仅提供 cost_price/cur_price 无这两个键, fallback 路径下成本和现价均变 0

### 议题 #114 [共性 2/3] `app/web_dashboard/data_service.py`:877-884 — 幽灵仓对账未归一化 symbol，可能误标/误删真实持仓

环节: s8_monitoring_surface · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/web_dashboard/data_service.py:877-879): 幽灵仓对账未归一化 symbol，可能误标/误删真实持仓
  - 证据引用: `        for sym, meta in (meta_map or {}).items():
            if sym in futu_symbols:
                continue`
  - 影响声称: futu_symbols 使用 Futu code（如 US.AAPL），meta_map 使用 open_positions.symbol；若 DB 中存储的是裸 symbol，对账会把真实持仓误判为幽灵并标记/删除。
  - 建议: 在对账前统一 symbol 前缀（都转成 US.xxx 或裸 symbol），并用规范化后的集合比较。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/web_dashboard/data_service.py:877-884): 幽灵仓对账直接用 DB symbol 与 Futu US.* code 比较，可能误标真实持仓
  - 证据引用: `for sym, meta in (meta_map or {}).items(): | if sym in futu_symbols: | continue | ghosts_to_mark.append(sym)`
  - 影响声称: futu_symbols 是 US.AAPL 格式，而 open_positions.symbol 可能不带 US. 前缀（同文件 DB fallback 对 symbol 做了 US. 补全）；若前缀不一致，真实持仓会被标记 is_anomalous 甚至 24h 后删除。
  - 建议: 对账前统一 symbol 规范化：将 DB symbol 与 Futu code 都转换为同一 canonical 格式，再比较。

<!-- DEB-VERDICT cluster=114 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: data_service.py:877-879 meta_map 键为 DB 裸 symbol(如 AAPL)而 futu_symbols 为 US.前缀(如 US.AAPL), sym in futu_symbols 恒为 False 致所有 DB 持仓被误标幽灵仓

### 议题 #119 [共性 2/3] `app/web_dashboard/order/executor.py`:1085-1108 — 实盘修改止损时 broker STOP 失败仍返回 success=True

环节: s8_monitoring_surface · 类别: logic_anomaly, state_machine · 自报最高 severity: S4
- **qwen3.8-max** (S4, state_machine, app/web_dashboard/order/executor.py:1085-1108): 实盘修改止损时 broker STOP 失败仍返回 success=True
  - 证据引用: `                else:
                    logger.error(f"[止损] {symbol} STOP 单失败 (DB 仍更新): {data2}")
        return {
            "success": True,
            "symbol": symbol,
            "new_sl": new_sl,
            "broker_order_id": new_oid,
            "status": sl_status,
        }`
  - 影响声称: 实盘环境若 STOP 下单失败，只更新 DB 而返回成功，面板/调用方认为止损已挂到 broker；实际仓位可能没有券商端止损，造成保护缺失。
  - 建议: 区分 simulate/db_only 与 real broker_attached；实盘 broker STOP 失败时返回 success=False 或至少 retryable 错误，并触发告警/人工确认。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/web_dashboard/order/executor.py:1085-1108): 实盘 STOP 单失败仍返回 success=True，仅以 status=db_only 区分
  - 证据引用: `logger.error(f"[止损] {symbol} STOP 单失败 (DB 仍更新): {data2}") | return { "success": True,`
  - 影响声称: 面板调用方通常依据 success 判断操作是否成功；实盘 broker STOP 失败但接口返回成功，可能让操作者误以为保护单已挂，实际只有 DB 记录。
  - 建议: 实盘 broker 挂单失败应返回 success=False 或至少返回明确的 warning/degraded 状态，并要求前端强提示。

<!-- DEB-VERDICT cluster=119 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:1085-1108 实盘 STOP 单 place_order 失败后 line 1102-1108 仍返回 success=True 仅以 status=db_only 区分, 前端误示成功而仓位无 broker 端止损保护

### 议题 #121 [共性 2/3] `app/web_dashboard/order/executor.py`:1474-1503 — 实盘保护性 STOP 下单失败后只记日志，不重试/不阻断后续流程

环节: s8_monitoring_surface · 类别: logic_anomaly · 自报最高 severity: S4
- **qwen3.8-max** (S4, logic_anomaly, app/web_dashboard/order/executor.py:1474-1476): 实盘保护性 STOP 下单失败后只记日志，不重试/不阻断后续流程
  - 证据引用: `        else:
            error_msg = str(data) if not data.empty else "Empty response"
            logger.error(f"[保护止损] ❌ {order.symbol} STOP 单失败: {error_msg}")`
  - 影响声称: place_protective_stop 在实盘 SL 下单失败后继续执行，调用方随后认为流程完成；仓位可能缺少券商端止损且无重试/告警闭环。
  - 建议: SL 下单失败时应返回失败状态、保留 pending protection 重试或发高优先级告警，不应静默继续。
- **qwen3.8-max** (S3, logic_anomaly, app/web_dashboard/order/executor.py:1500-1501): TP 持仓未确认时声称监控线程重试，但代码未重新入队
  - 证据引用: `                if not _pos_confirmed:
                    logger.warning(f"[保护止盈] {order.symbol} 持仓未确认 (等了18s), TP 单延后由监控线程重试")`
  - 影响声称: SELL TP 因持仓未入账被跳过后，没有将订单重新加入 _pending_protections 或其他重试队列，日志承诺的重试不存在，TP 保护单可能永久缺失。
  - 建议: 未确认持仓时将保护挂单重新注册到延迟队列/后台重试，或返回明确失败并告警。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/web_dashboard/order/executor.py:1500-1503): TP 等待持仓确认超时后声称由监控线程重试，但实际没有任何重试注册
  - 证据引用: `if not _pos_confirmed: logger.warning(f"[保护止盈] {order.symbol} 持仓未确认 (等了18s), TP 单延后由监控线程重试") | if _pos_confirmed or trd_side_tp == TrdSide.BUY:`
  - 影响声称: 当买入后 broker 持仓确认超过 18 秒时，TP 单被跳过且无后续重试；日志暗示有监控重试但代码没有实现，导致止盈保护静默丢失。
  - 建议: 未确认持仓时应将 TP 挂单任务重新入队或落库待重试，并设置告警；不能仅打日志后丢弃。
- **qwen3.8-max-0902** (S3, logic_anomaly, app/web_dashboard/order/executor.py:1474-1480): 实盘 STOP 止损挂单失败后仍继续挂 TP，可能形成只有止盈没有止损的裸仓位
  - 证据引用: `logger.error(f"[保护止损] ❌ {order.symbol} STOP 单失败: {error_msg}") | if _tp_price > 0:`
  - 影响声称: SL 是核心风险控制，失败后不中止保护流程而继续挂 TP，会导致仓位可能只有止盈单没有止损单；面板与 DB 也可能误认为保护已部分完成。
  - 建议: SL 挂单失败应视为保护失败：重试、显性告警、标记仓位无保护，必要时禁止继续自动交易或要求人工确认。

<!-- DEB-VERDICT cluster=121 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:1474-1476 STOP 单失败仅记日志后 line 1480 继续挂 TP, 形成有止盈无止损的裸仓位; line 1501 声称监控线程重试但无任何重试注册机制

### 议题 #125 [共性 2/3] `app/evolution/promotion_engine.py`:55-106 — PromotionEngine 的临界 t 值忽略传入 alpha，Bonferroni 校正实际失效

环节: s9_review_backtest · 类别: logic_anomaly, numeric_unit · 自报最高 severity: S3
- **qwen3.8-max** (S3, logic_anomaly, app/evolution/promotion_engine.py:55-106): PromotionEngine 的临界 t 值忽略传入 alpha，Bonferroni 校正实际失效
  - 证据引用: `z = 1.96
t_approx = z + (z**3 + z) / (4 * df) + (z**5 + 2 * z**3 + z) / (96 * df**2)
adjusted_alpha = self.ALPHA / max(active_count, 1)
critical_t = self._critical_t(n, adjusted_alpha)`
  - 影响声称: 多候选并行时 adjusted_alpha 会变小，理应使用更严格的临界值；但 _critical_t 内部仍固定使用 z=1.96，导致显著性门禁被放宽，可能把噪声候选误判为可转正。
  - 建议: 让 _critical_t 根据 alpha 计算临界值，或使用统计库/逆正态近似支持可变 alpha；对多候选并行保留真实 Bonferroni 校正。
- **qwen3.8-max-0902** (S2, numeric_unit, app/evolution/promotion_engine.py:55-106): _critical_t 接收 adjusted_alpha 但固定使用 z=1.96，Bonferroni 校正未生效
  - 证据引用: `        # 正态近似: z_0.025 = 1.96
        z = 1.96
        # Bonferroni 校正 (多候选并行)
        adjusted_alpha = self.ALPHA / max(active_count, 1)
        critical_t = self._critical_t(n, adjusted_alpha)`
  - 影响声称: should_promote 对多候选并行计算 adjusted_alpha，但临界值函数未根据 alpha 变化，仍固定 0.05 双尾临界值，导致多候选时显著性门槛被低估；若该模块重新启用，可能错误促进策略转正。
  - 建议: 让 _critical_t 根据 adjusted_alpha 计算对应分位数，或使用统计库/查表得到正确双尾临界值。

<!-- DEB-VERDICT cluster=125 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: _critical_t(n, alpha) 在 app/evolution/promotion_engine.py:41-59 接收 alpha 参数但函数体从未使用，z=1.96 硬编码于第56行，Bonferroni 校正后的 adjusted_alpha(第105行) 传入后被忽略，临界值永远对应 α=0.05，多候选并行检验门槛偏松可导致不合格策略被转正。

### 议题 #126 [共性 2/3] `app/review/cohort_ab.py`:63-96 — cohort A/B 已平仓样本未过滤 exit_price>0/PHANTOM 等无效成交

环节: s9_review_backtest · 类别: dataflow_inconsistency, numeric_unit · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/review/cohort_ab.py:63-72): cohort A/B 已平仓样本未过滤 exit_price>0/PHANTOM 等无效成交
  - 证据引用: `where = "WHERE exit_time IS NOT NULL AND exit_time != '' AND exit_reason != ''"`
  - 影响声称: 只要 exit_time/exit_reason 非空就计入 A/B 实验，可能纳入无效、幽灵或已撤销成交，导致 cohort PnL、胜率和持仓时长被污染，进而影响 PASS/FAIL 门禁。
  - 建议: 增加 exit_price > 0、排除 PHANTOM_RECONCILE_VOID 及与 daily_review 相同的 source_type/trade_mode 白名单，确保 A/B 只统计真实已平仓成交。
- **qwen3.8-max** (S3, numeric_unit, app/review/cohort_ab.py:88-96): cohort A/B 决策门禁使用 realized_pnl 毛盈亏而非净盈亏
  - 证据引用: `pnls = [r["realized_pnl"] or 0 for r in rows]
"sum_pnl": round(sum(pnls), 2),`
  - 影响声称: A/B 门禁以毛 PnL 比较，忽略费用后可能把净表现更差的 B 组误判为 PASS；这会直接影响退出策略是否推广到主线。
  - 建议: 使用 COALESCE(pnl_net, realized_pnl) 作为 cohort PnL/胜负口径，与复盘和资金归因保持一致。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/cohort_ab.py:63-73): cohort A/B closed 样本未过滤 exit_price>0/PHANTOM，且使用 realized_pnl 毛额
  - 证据引用: `    where = "WHERE exit_time IS NOT NULL AND exit_time != '' AND exit_reason != ''"
    params: list = []
    if since:
        where += " AND trade_date >= ?"
        params.append(since)
    rows = con.execute(
        f"""SELECT symbol, direction, realized_pnl, pnl_pct, exit_reason,
             `
  - 影响声称: daily_review 对 closed 成交使用 exit_price>0、排除 PHANTOM_RECONCILE_VOID，并强调净额口径；此处仅要求 exit_time/exit_reason 非空且用 realized_pnl，可能把幽灵/零价退出或费用前毛额纳入 A/B 门禁，影响推广决策。
  - 建议: 统一真实成交过滤：exit_price > 0、排除 PHANTOM_RECONCILE_VOID，并使用 COALESCE(pnl_net, realized_pnl) 计算 PnL。

<!-- DEB-VERDICT cluster=126 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: app/review/cohort_ab.py:63 的 WHERE 仅过滤 exit_time/exit_reason 非空，未排除 PHANTOM_RECONCILE_VOID 亦未要求 exit_price>0（对比 daily_review.py:79,84 均有此过滤），幽灵/无效成交会污染 A/B 对比；同时第88行使用 realized_pnl 毛额（order_executor.py:4485 证实未扣费），与系统净口径 COALESCE(pnl_net,realized_pnl) 不一致。

### 议题 #127 [共性 2/3] `app/review/daily_review.py`:76-83 — 每日复盘主查询硬排除 trade_mode='live'，可能漏掉真实成交记录

环节: s9_review_backtest · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/review/daily_review.py:78-80): 复盘白名单同时包含 PLAYBOOK_LIVE 又排除 trade_mode='live'，可能漏掉真实实盘成交
  - 证据引用: `AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE')
AND exit_reason != 'PHANTOM_RECONCILE_VOID'
AND trade_mode != 'live'`
  - 影响声称: 若 PLAYBOOK_LIVE 记录的真实成交以 trade_mode='live' 标记，日复盘、计划执行率和后续归因会系统性漏掉实盘成交，导致复盘统计与真实资金结果不一致。
  - 建议: 明确 source_type 与 trade_mode 的权威语义，避免同时选择 PLAYBOOK_LIVE 又排除 live；若需统计实盘，应保留 trade_mode='live' 的 PLAYBOOK_LIVE 记录。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/daily_review.py:76-83): 每日复盘主查询硬排除 trade_mode='live'，可能漏掉真实成交记录
  - 证据引用: `        trades = self._query(
            "SELECT * FROM trade_records WHERE trade_date = ? "
            "AND source_type IN ('PLAYBOOK_SIM', 'PLAYBOOK_LIVE') "
            "AND exit_reason != 'PHANTOM_RECONCILE_VOID' "
            "AND trade_mode != 'live' "
            "ORDER BY created_at",
    `
  - 影响声称: 若 trade_records.trade_mode='live' 表示真实成交，则每日复盘及其胜率、盈亏、漏斗、评分都会系统性排除真实交易，导致复盘口径与真实成交不一致。
  - 建议: 明确 trade_mode 语义；如果 'live' 是实盘成交，应纳入复盘统计或单独分口径展示，不应在总复盘查询中静默排除。

<!-- DEB-VERDICT cluster=127 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: app/review/daily_review.py:80 硬编码 trade_mode!='live'，而 trade_store.py:71-84 的 _get_trade_mode() 在 AUTO_TRADE_ACCOUNT=real 时返回 'live'，导致实盘模式下当日所有交易被排除，每日复盘报告为空，与复盘目的矛盾；白名单含 PLAYBOOK_LIVE 但 trade_mode='live' 行全被过滤，逻辑上自相矛盾。

### 议题 #131 [共性 2/3] `app/review/review_orchestrator.py`:101-119 — collect_daily 按 trade_date 归集当日交易，与同文件“出场日记账”口径冲突

环节: s9_review_backtest · 类别: dataflow_inconsistency, time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:101-114): ReviewOrchestrator 日统计使用未过滤 trade_records 且按毛盈亏计胜负
  - 证据引用: `WHERE tr.trade_date = ?
closed = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]
pnl = t.get('realized_pnl', 0) or 0`
  - 影响声称: 日复盘/上下文可能纳入未按 daily_review 白名单与 PHANTOM 排除的记录，并用 realized_pnl 毛口径计胜率/盈亏；与下游净口径 COALESCE(pnl_net, realized_pnl) 和出场日归集不一致，造成复盘结论偏离真实成交。
  - 建议: 统一白名单/排除条件，按 substr(exit_time,1,10) 出场日归集，并用 COALESCE(pnl_net, realized_pnl) 判定胜负与汇总盈亏。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:114-119): 复盘编排器日统计仍用 realized_pnl 毛额，与验证器/日复盘净额口径不一致
  - 证据引用: `            pnl = t.get('realized_pnl', 0) or 0
            by_source[src]['pnl'] += pnl
            if pnl > 0:
                by_source[src]['wins'] += 1
            elif pnl < 0:
                by_source[src]['losses'] += 1`
  - 影响声称: 同文件后续验证使用 COALESCE(pnl_net, realized_pnl)，daily_review 也声明 pnl_net 为唯一对外口径；此处继续用毛额会漏计费用，产生虚高胜率/盈亏，造成复盘报告与验证证据不一致。
  - 建议: 统一改为净额口径：读取或计算 COALESCE(pnl_net, realized_pnl)，并在 by_source、历史模式、趋势统计中保持一致。
- **qwen3.8-max-0902** (S3, time_boundary, app/review/review_orchestrator.py:101-105): collect_daily 按 trade_date 归集当日交易，与同文件“出场日记账”口径冲突
  - 证据引用: `               WHERE tr.trade_date = ?
               ORDER BY tr.created_at""",
            (date,)
        )
        closed = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]`
  - 影响声称: 同文件 _validate_general 明确说明当日总盈亏应按 substr(exit_time,1,10) 出场日归集，否则隔夜仓当日出场盈亏会漏记；collect_daily 仍按 trade_date 取数，可能把当日出场的前一日持仓漏掉，导致日复盘盈亏/胜率失真。
  - 建议: 对已平仓当日统计改用 substr(exit_time,1,10)=date 作为当日口径，或明确 trade_date 已等于出场日并全链路统一。

<!-- DEB-VERDICT cluster=131 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: app/review/review_orchestrator.py:101 按 trade_date(开仓日，见 trade_store.py:94) 归集，多日持仓在出场日复盘不可见；第114行用 realized_pnl 毛额计胜负，同文件第2012/2137/2208行已切 COALESCE(pnl_net,realized_pnl) 净口径，内部不一致。

### 议题 #124 [共性 2/3] `app/catalyst/sources/hkex_source.py`:162-170 — HKEX 时间解析用 naive datetime.timestamp() 再减 8 小时，依赖服务器本地时区

环节: s9_review_backtest · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S3, time_boundary, app/catalyst/sources/hkex_source.py:167-169): HKEX 时间解析对 naive datetime 再减 8 小时，会产生错误事件时间
  - 证据引用: `dt = datetime.strptime(date_str, "%Y/%m/%d %H:%M")
return dt.timestamp() - 8 * 3600`
  - 影响声称: datetime.strptime 生成无时区时间，timestamp() 会按服务器本地时区解释；随后又减 8 小时，导致除非服务器恰好是 HKT 否则时间偏移错误。催化事件 24 小时窗口、盘前/盘后事件判定可能错位或漏报。
  - 建议: 显式把 HKEX 时间当作 UTC+8：datetime.strptime(...).replace(tzinfo=timezone(timedelta(hours=8))).timestamp()，不要再手工减 8 小时。
- **qwen3.8-max-0902** (S2, time_boundary, app/catalyst/sources/hkex_source.py:162-170): HKEX 时间解析用 naive datetime.timestamp() 再减 8 小时，依赖服务器本地时区
  - 证据引用: `    def _parse_hkex_date(self, date_str: str) -> float:
        """解析 HKEX 日期格式 (YYYY/MM/DD HH:MM)"""
        try:
            from datetime import datetime
            # 格式: 2026/05/27 14:30
            dt = datetime.strptime(date_str, "%Y/%m/%d %H:%M")
            # HKEX 时间是 HKT (UTC+8)
          `
  - 影响声称: datetime.strptime 返回 naive 时间，.timestamp() 会按服务器本地时区解释；只有服务器恰为 UTC 时减 8 小时才正确。不同部署时区会导致 HKEX 公告时间偏移，影响 24 小时窗口过滤和催化事件时效判断。
  - 建议: 显式将 HKT 时间绑定 UTC+8 时区后转 UTC，例如 datetime(..., tzinfo=timezone(timedelta(hours=8))).timestamp()。

<!-- DEB-VERDICT cluster=124 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: app/catalyst/sources/hkex_source.py:167-169 对 naive datetime 调用 .timestamp() 再减 8*3600，.timestamp() 以服务器本地时区解释 naive 时间，仅当服务器为 UTC 时结果正确；若服务器为 UTC+8（HKEX 自然时区）则偏差 16 小时，时间戳脆弱依赖部署环境。

### 议题 #132 [共性 2/3] `app/review/review_orchestrator.py`:2058-2087 — 执行异常验证按 now_et 当日搜索日志，忽略传入复盘 date

环节: s9_review_backtest · 类别: dataflow_inconsistency, logic_anomaly, time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S2, time_boundary, app/review/review_orchestrator.py:2059-2064): 执行类 claim 的日志验证忽略传入 date，只匹配当前 ET 日期
  - 证据引用: `today = _lg_now_et().strftime("%Y-%m-%d")
for line in lines:
    if today in line and any(kw in line.lower() for kw in keywords):`
  - 影响声称: 复盘或验证历史日期时，日志证据实际只搜索当天日志；历史执行异常会被漏检，当天无关日志可能误证历史 claim，降低验证可信度。
  - 建议: 使用传入 date 匹配日志时间戳，或读取对应日期归档日志；仅在 date 为空时回退到当前日期。
- **qwen3.8-max** (S2, dataflow_inconsistency, app/review/review_orchestrator.py:2083-2085): 漏斗验证仍读取已停写的 decisions 表，与 playbook_instructions 新口径不一致
  - 证据引用: `SUM(CASE WHEN direction != 'NEUTRAL' THEN 1 ELSE 0 END) as actionable
FROM decisions WHERE date(created_at) = ?`
  - 影响声称: 模块注释已说明 decisions.db 停写并改读 playbook_instructions，但验证器仍查 decisions，容易恒返回无决策数据，导致漏斗类 claim 被误判为不可验证。
  - 建议: 将漏斗验证改为 playbook_instructions 的 date/executed/direction 口径，与 collect_daily 和 RichContextBuilder 保持一致。
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/review/review_orchestrator.py:2082-2087): 漏斗验证仍查询已停写的 decisions.db，而采集层已改用 playbook_instructions
  - 证据引用: `            decisions = self._query(
                "SELECT COUNT(*) as total, "
                "SUM(CASE WHEN direction != 'NEUTRAL' THEN 1 ELSE 0 END) as actionable "
                "FROM decisions WHERE date(created_at) = ?",
                (date,), db_path=self.decisions_db
            )`
  - 影响声称: 文件头部注释已说明 decisions.db 停写并改读 playbook_instructions；此处仍查旧表，会得到恒 0 或陈旧数据，使漏斗相关 claim 验证失败或给出错误证据。
  - 建议: _validate_funnel 与 _verify_db_evidence 的 decision 查询都应改为 playbook_instructions，并按 direction/executed 统计。
- **qwen3.8-max-0902** (S3, time_boundary, app/review/review_orchestrator.py:2058-2061): 执行异常验证按 now_et 当日搜索日志，忽略传入复盘 date
  - 证据引用: `                from app.utils.timezone import now_et as _lg_now_et
                today = _lg_now_et().strftime("%Y-%m-%d")  # 2026-07-28: CST→ET (日期切片口径)
                keywords = ["滑点", "重复", "ghost", "幽灵", "持仓", "对账", "同步",
                           "slippage", "duplicate", "ghost", "reconcil`
  - 影响声称: 函数接收 date 参数，但日志匹配使用当前 ET 日期；复盘历史日期时会搜索错误日期窗口，可能漏掉历史执行异常或错误验证当前日志。
  - 建议: 使用传入 date 过滤日志，或按日期归档日志后读取对应日期文件；不要固定使用 now_et。
- **qwen3.8-max-0902** (S2, logic_anomaly, app/review/review_orchestrator.py:2063-2067): _validate_execution 匹配到首条日志即 break，error_count 永远最多为 1
  - 证据引用: `                for line in lines:
                    if today in line and any(kw in line.lower() for kw in keywords):
                        evidence_found = True
                        error_count += 1
                        break`
  - 影响声称: error_count 用于证据描述，但 break 使计数只反映首条匹配，无法体现异常频次，可能低估执行问题严重程度。
  - 建议: 若需要计数，应遍历全部行统计匹配数后再返回；若只需布尔证据，则不要输出误导性 count。

<!-- DEB-VERDICT cluster=132 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: app/review/review_orchestrator.py:2059 用 _lg_now_et() 取当日而非传入 date 参数做日志匹配，历史复盘验证结果必然错误；第2067行 break 使 error_count 永远 ≤1 失去计数意义；decisions 表停写子项不成立（decision_recorder.py:200 仍在 INSERT），但前两项已构成实质缺陷。

## 四、独有议题 (单模型命中 — 核实其真伪与独到价值)

### 议题 #6 [独有 1/3] `app/utils/performance_decay.py`:12-67 — SQLite 查询异常路径未关闭连接

环节: s1_entry_config · 类别: resource_leak · 自报最高 severity: S2
- **qwen3.8-max** (S2, resource_leak, app/utils/performance_decay.py:12-67): SQLite 查询异常路径未关闭连接
  - 证据引用: `        conn = sqlite3.connect(db_path)
    except Exception as e:
        logger.error(f"计算衰减因子失败: {e}")
        return 1.0`
  - 影响声称: conn 在 try 中打开，但 except 返回前没有 close；若 cursor.execute 或后续计算抛异常，SQLite 连接句柄会泄漏，长期运行可能累积资源。
  - 建议: 使用 try/finally 或 contextlib.closing/sqlite3.connect 上下文管理器确保连接关闭。

<!-- DEB-VERDICT cluster=6 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: app/utils/performance_decay.py:13 创建 sqlite3.connect(db_path) 后，line 65-67 的 except Exception 直接 return 1.0 未调用 conn.close()，异常路径连接泄漏(虽 CPython GC 会延迟回收，但非 with/contextlib 管理)

### 议题 #12 [独有 1/3] `ftseek_us.sh`:29-41 — pkill/pgrep 模式 'main.py --start' 不匹配项目路径, 可误杀/误报同机其他项目

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S3
- **kimi-k3** (S3, logic_anomaly, ftseek_us.sh:29-41): pkill/pgrep 模式 'main.py --start' 不匹配项目路径, 可误杀/误报同机其他项目
  - 证据引用: `29 行: `pkill -f "main.py --start"`;40-41 行: `pgrep -f "main.py --start"`;26-28 行注释自述原模式 'ftseek_us' 会自杀式误杀故改为此模式, 但新模式不含任何项目标识 (对比 7 行 venv 路径含 FTSeek-US)`
  - 影响声称: 同主机若部署姊妹系统 (如 FTSeek-CN/HK, 其进程 cmdline 同样含 'main.py --start'), `ftseek_us.sh stop` 会把姊妹交易进程一并杀死, `status` 会把别的项目误报为本系统在运行; 且 stop 后无条件 echo '已停止', pkill 未命中也报成功
  - 建议: 模式改为含项目路径的精确匹配, 如 `pkill -f "$FTSEEK_US_DIR/main.py --start"`, 并在 pkill 后校验退出码再提示

<!-- DEB-VERDICT cluster=12 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: ftseek_us.sh:29 pkill -f "main.py --start" 和 line 40-41 pgrep -f "main.py --start" 不含项目路径标识，同机其他含 main.py --start 的 Python 进程会被误杀/误报；line 7 的 venv 路径已含 FTSeek-US 但未用于匹配模式

### 议题 #7 [独有 1/3] `app/utils/retry.py`:76-89 — retry_on_none 用尽重试后返回 None 却记录“重试后成功”

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/utils/retry.py:76-89): retry_on_none 用尽重试后返回 None 却记录“重试后成功”
  - 证据引用: `if retry_on_none and result is None and attempt < retries:
                    if attempt > 0:
                        logger.info(
                            "[retry] %s 第 %d 次重试后成功", label, attempt
                        )
                    return result`
  - 影响声称: 当 retry_on_none=True 且最后一次仍返回 None 时，日志误报成功，掩盖行情/持仓获取失败，下游可能基于 None 做判断。
  - 建议: 在 retry_on_none 最终仍为 None 时记录失败或返回明确哨兵，不要记录成功。

<!-- DEB-VERDICT cluster=7 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/utils/retry.py:76-89 当 retry_on_none 用尽重试(最后一轮 attempt==retries 且 result 仍为 None)时，line 85-88 的 if attempt > 0 分支记录 "第 N 次重试后成功" 但实际返回 None，日志与实际结果矛盾

### 议题 #2 [独有 1/3] `app/config_mgmt/drift_detector.py`:106-130 — 漂移检测哈希的是被热重载原地更新的同一 _raw_config, 合法热重载被误报为外部漂移

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/config_mgmt/drift_detector.py:106-130): 漂移检测哈希的是被热重载原地更新的同一 _raw_config, 合法热重载被误报为外部漂移
  - 证据引用: `docstring 66 行: '定期检查配置文件是否被外部修改 (非热重载修改)';但 103/108 行快照均取 `self.config._raw_config`, 而 app/config_mgmt/hot_reloader.py 134 行 `self.config._load()` 更新的正是同一 loader 实例的 _raw_config;113-118 行 hash 变化即 `logger.warning("[DriftDetector] ⚠️ 检测到配置漂移! ...")``
  - 影响声称: 每次 Layer 3 热重载生效后, Layer 2 在最迟 4 小时后必报一次'配置漂移'假警并写漂移日志, 真正的外部篡改告警被噪声淹没, 运维对漂移告警产生告警疲劳
  - 建议: 漂移检测应独立对配置文件磁盘内容做哈希 (与热重载的内存态隔离), 或由 HotReloader 在重载成功后主动调用 drift_detector._take_snapshot() 重置基线

<!-- DEB-VERDICT cluster=2 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/config_mgmt/drift_detector.py:108 快照取自 self.config._raw_config，而 app/config_mgmt/hot_reloader.py:134 热重载调 self.config._load() 原地更新同一 _raw_config，每次合法热重载都会触发 drift_detector line 113 hash_changed 误报为外部漂移

### 议题 #9 [独有 1/3] `app/utils/watchlist_manager.py`:110-120 — self_managed 过滤不剥 US. 前缀, 与相邻黑名单过滤口径不一致, 可能永不命中

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S2
- **kimi-k3** (S2, config_effect, app/utils/watchlist_manager.py:110-120): self_managed 过滤不剥 US. 前缀, 与相邻黑名单过滤口径不一致, 可能永不命中
  - 证据引用: `黑名单过滤 (104 行) 有去前缀: `if s.get("code", "").replace("US.", "") not in blacklist`;而 110-111 行注释写明自管名单为裸代码: '排除 self_managed_symbols (AMD/AAPL等用户自管)';119 行却直接比较: `if s.get("code", "") not in self_managed` — 用 'US.AMD' 比对 'AMD'`
  - 影响声称: 若 yaml 中 risk.self_managed_symbols 按注释示例存裸代码 (AMD/AAPL), 该过滤永远不匹配, 用户自管标的仍进入扫描池, 浪费 API 预算并可能绕过信号管道的 self_managed 检查, 且无任何告警 (设计意图即静默过滤)
  - 建议: 与黑名单过滤对齐: `s.get("code", "").replace("US.", "") not in [m.replace("US.", "") for m in self_managed]`, 统一两侧口径

<!-- DEB-VERDICT cluster=9 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: app/utils/watchlist_manager.py:104 黑名单过滤用 .replace("US.", "") 去前缀后比对，但 line 119 自管名单过滤直接比对 s.get("code", "") not in self_managed 不去前缀；当前 YAML (line 1720-1722) 恰用带前缀格式所以暂不触发，但与黑名单口径不一致且注释(line 111)暗示裸代码格式，添加裸代码将永不命中

### 议题 #5 [独有 1/3] `app/utils/dynamic_pool.py`:197-213 — YAML risk_adjustments 可覆盖 danger/blackout 的 max_symbols=0，导致危险态不停止开仓

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, config_effect, app/utils/dynamic_pool.py:197-213): YAML risk_adjustments 可覆盖 danger/blackout 的 max_symbols=0，导致危险态不停止开仓
  - 证据引用: `if yaml_adjustments and risk_level in yaml_adjustments:
            adj = yaml_adjustments[risk_level]
        else:
            adj = _RISK_ADJUSTMENTS.get(risk_level, {})

        if adj.get("max_symbols", -1) == 0:
        profile = StockFilterProfile(
            max_symbols=max(1, int(base.max_`
  - 影响声称: 若 YAML 定义了 danger/blackout 但遗漏 max_symbols: 0，系统不会返回停止开仓 profile，而生成至少 1 个 max_symbols 的池，破坏危险状态禁开新仓。
  - 建议: 对 danger/blackout 强制 max_symbols=0，或启动校验强制要求 YAML 覆盖包含 stop 语义。

<!-- DEB-VERDICT cluster=5 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: app/utils/dynamic_pool.py:197-200 YAML risk_adjustments 优先于硬编码 _RISK_ADJUSTMENTS，若 YAML 为 danger/blackout 定义了不含 max_symbols 的字段(如仅 max_symbols_mult)，line 202 的 adj.get("max_symbols", -1) == 0 不成立，危险态停止开仓的安全闸被 YAML 误配绕过

### 议题 #3 [独有 1/3] `app/config_mgmt/hot_reloader.py`:202-234 — 热重载失效快照未包含 WatchlistManager，watchlist 变更不会即时生效

环节: s1_entry_config · 类别: config_effect · 自报最高 severity: S2
- **qwen3.8-max** (S2, config_effect, app/config_mgmt/hot_reloader.py:202-234): 热重载失效快照未包含 WatchlistManager，watchlist 变更不会即时生效
  - 证据引用: `    try:
        from app.playbook.config import reset_playbook_config
        reset_playbook_config()
        done.append("PlaybookConfig")
    except Exception as e:
        logger.warning("[HotReloader] PlaybookConfig 失效失败 (沿用旧快照): %s", e)

    try:
        from app.core.feature_flags import get_`
  - 影响声称: WatchlistManager 在初始化时把 watchlist 缓存到 _watchlist_data，并提供了 reload()，但热重载回调只失效 PlaybookConfig/FeatureFlags；unified_config.yaml 的 watchlist 变更后，扫描池/名单消费者仍读旧名单，直到重启或手工调用 reload。
  - 建议: 在 invalidate_config_snapshots 中调用 reload_watchlist()/WatchlistManager.reload()，或让 WatchlistManager 注册为热重载下游消费者。

<!-- DEB-VERDICT cluster=3 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/utils/watchlist_manager.py:41 从 unified_config.yaml get_dotted("watchlist") 快照到 self._watchlist_data，但 app/config_mgmt/hot_reloader.py:202-235 invalidate_config_snapshots 只失效 PlaybookConfig 和 FeatureFlags，未调用 WatchlistManager.reload()，YAML watchlist 热重载后不生效

### 议题 #1 [独有 1/3] `analyze_signals.py`:239-245 — 胜率统计声称按方向调整收益, 实际用原始 fwd_ret_t1>0, 做空信号胜率被反向计算

环节: s1_entry_config · 类别: logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, analyze_signals.py:239-245): 胜率统计声称按方向调整收益, 实际用原始 fwd_ret_t1>0, 做空信号胜率被反向计算
  - 证据引用: `230 行注释: `"  (Win = direction-adjusted forward return > 0)"`;但 239 行: `wins = sum(1 for r in bucket if r['fwd_ret_t1'] > 0)`;245 行: `all_wins = sum(1 for r in data if r['fwd_ret_t1'] > 0)` — 均未乘方向符号`
  - 影响声称: 对 SHORT 信号, 下跌 (fwd_ret_t1<0) 本是盈利却被计为亏损, 分桶胜率与 OVERALL 胜率在含做空样本时系统性失真, 基于该报表做出的策略取舍决策被误导
  - 建议: 按 direction 调整: `adj = r['fwd_ret_t1'] * (1 if r['direction']=='LONG' else -1)`, 以 adj>0 判胜; 或修正注释明确统计的是原始收益口径

<!-- DEB-VERDICT cluster=1 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: analyze_signals.py:230 打印 "Win = direction-adjusted forward return > 0"，但 line 239 和 line 245 计算 wins 用 r['fwd_ret_t1'] > 0 未乘 direction 符号，而 line 159-160 数据确含 direction 字段(LONG/SHORT)，做空信号的正向收益被错误计为亏损

### 议题 #27 [独有 1/3] `app/data/split_event_guard.py`:35-51 — 拆股 guard 用服务器本地 date.today() 判定"今天", 与美股 ET 交易日口径错位

环节: s2_market_data · 类别: time_boundary · 自报最高 severity: S2
- **kimi-k3** (S2, time_boundary, app/data/split_event_guard.py:35-51): 拆股 guard 用服务器本地 date.today() 判定"今天", 与美股 ET 交易日口径错位
  - 证据引用: `L38 `d = event_date or date.today()`; L45 `today_set = self._splits_today.get(date.today(), set())`; L51 `return set(self._splits_today.get(date.today(), set()))`; 模块 docstring L5 自述 "每日 04:00 ET 拉当日 split_events"`
  - 影响声称: 服务器非 ET 时区 (如 CST, 比 ET 快 12-13h) 时, ET 午后本地已次日 → mark 与 is_split_today 落到不同日期键, 当日拆股标记查不到, scoring 跳过/FLASH_EXIT 豁免失效; 或前一日标记被错误沿用
  - 建议: 统一用 app.utils.timezone.now_et().date() (rejection_logger L74-75 已确立 ET 口径先例)

<!-- DEB-VERDICT cluster=27 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: split_event_guard.py:38/45/51 使用date.today()(服务器本地时间)判定"今天"拆股, 模块docstring自述"每日04:00 ET拉当日", 若服务器部署非ET时区则跨日窗口(如ET 17:00后CST已是次日)拆股检测错位

### 议题 #29 [独有 1/3] `app/storage/shadow_signal_logger.py`:44-86 — shadow 信号日期使用本地 naive 时间，未与 ET 交易日口径对齐

环节: s2_market_data · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/storage/shadow_signal_logger.py:44-86): shadow 信号日期使用本地 naive 时间，未与 ET 交易日口径对齐
  - 证据引用: `"timestamp": datetime.now().isoformat(),
today = datetime.now().date()`
  - 影响声称: 美股交易日以美东时间为边界，而此处用服务器本地时间写入和筛选今日信号；若服务器时区不是美东，盘前/盘后跨日信号会被归入错误日期，且与 rejection_logger 的 ET 口径不一致。
  - 建议: 统一使用 now_et() 或 UTC aware 时间，并在查询今日信号时按美东交易日转换。

<!-- DEB-VERDICT cluster=29 -->
裁决: CONFIRMED | severity=1 | 置信=medium | 理由: shadow_signal_logger.py:44写入timestamp和L86读取today均用datetime.now()(服务器本地), 写读内部一致但与ET交易日口径可能错位, 仅影响影子信号按日统计精度, 不影响实盘交易决策

### 议题 #25 [独有 1/3] `app/data/providers/base.py`:58-61 — 缓存时间比较混用 aware/naive datetime，缺失 fetched_at 时必然异常

环节: s2_market_data · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/data/providers/base.py:58-61): 缓存时间比较混用 aware/naive datetime，缺失 fetched_at 时必然异常
  - 证据引用: `fetched_at = datetime.fromisoformat(cached.get("fetched_at", "2000-01-01").replace("Z", "+00:00"))
age_seconds = (now_utc() - fetched_at).total_seconds()`
  - 影响声称: 默认值 "2000-01-01" 解析为 naive datetime；now_utc() 通常为 aware datetime，二者相减会 TypeError。若子类返回无时区 fetched_at，也会触发同类异常，导致缓存判断失败。
  - 建议: 强制 fetched_at 为 UTC aware；解析时对 naive 时间显式替换 tzinfo=timezone.utc，并在缺失时按过期处理而非构造异常值。

<!-- DEB-VERDICT cluster=25 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: base.py:60 防御默认值"2000-01-01"无Z后缀, .replace("Z","+00:00")为no-op, fromisoformat返回naive datetime, 与now_utc()(L294-296返回aware UTC)相减必抛TypeError; 实际provider(fundamental_provider.py:74)均用含Z的now_iso, 仅fetched_at缺失时防御路径触发

### 议题 #26 [独有 1/3] `app/data/providers/registry.py`:70-71 — 无 Provider 时 ThreadPoolExecutor max_workers=0 会抛异常

环节: s2_market_data · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/data/providers/registry.py:70-71): 无 Provider 时 ThreadPoolExecutor max_workers=0 会抛异常
  - 证据引用: `with ThreadPoolExecutor(max_workers=min(len(self._providers), 10)) as executor:`
  - 影响声称: 若 auto_discover 未注册任何 Provider，get_all(parallel=True) 会构造 max_workers=0 的线程池并抛 ValueError，导致 enrichment 全部失败而不是返回空/降级结果。
  - 建议: max_workers=max(1, min(len(self._providers), 10))，或在 len(self._providers)==0 时直接返回带 _meta 的空结果。

<!-- DEB-VERDICT cluster=26 -->
裁决: CONFIRMED | severity=2 | 置信=low | 理由: registry.py:71 ThreadPoolExecutor(max_workers=min(len(self._providers),10)), 若auto_discover未注册任何provider则max_workers=0抛ValueError; 但auto_discover(L148)在模块级执行且providers/目录有多个provider实现, 实际触发概率极低

### 议题 #23 [独有 1/3] `app/data/market_data_router.py`:999-1001 — SPY 历史 K 线请求边界使用本地 date.today() 而非美东日期

环节: s2_market_data · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/data/market_data_router.py:999-1001): SPY 历史 K 线请求边界使用本地 date.today() 而非美东日期
  - 证据引用: `_end = _d.today().isoformat()
_start = (_d.today() - _td(days=400)).isoformat()`
  - 影响声称: 美股交易日和收盘边界应按美东时间判断；若运行主机时区不是 America/New_York，request_history_kline 的 end 可能过早或过晚，导致 MA200/MA50/MA20 计算使用错误最后交易日。
  - 建议: 用 datetime.now(pytz.timezone("America/New_York")).date() 计算 _end/_start，并与 holiday_calendar 的交易日口径一致。

<!-- DEB-VERDICT cluster=23 -->
裁决: CONFIRMED | severity=2 | 置信=low | 理由: market_data_router.py:1000-1001 用date.today()(服务器本地)作为SPY历史K线end边界, 若非ET时区end可能超前一日, 但request_history_kline返回最近可用数据且MA200/MA50使用closes[-200:]切片, 对计算结果影响极有限

### 议题 #18 [独有 1/3] `app/data/futu_lv3_provider.py`:1862-1895 — 盘口缺失时用 last_price 兜底会使 spread/流动性评分失真

环节: s2_market_data · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max** (S2, dataflow_inconsistency, app/data/futu_lv3_provider.py:1862-1895): 盘口缺失时用 last_price 兜底会使 spread/流动性评分失真
  - 证据引用: `if bid <= 0 or ask <= 0: / pq = self.get_push_quote(symbol) / if pq: / bid = float(pq.get('bid', 0) or 0) / ask = float(pq.get('ask', 0) or 0) / mid = (bid + ask) / 2 / spread_pct = (ask - bid) / mid * 100`
  - 影响声称: Push quote 的 bid/ask 在缺失时会用 last_price 兜底；get_order_book_quality 再基于该兜底计算 spread，可能得到 spread=0、liquidity_score=100、tradeable=True，误判无真实盘口标的的流动性。
  - 建议: 当 bid/ask 来自 last_price 兜底时标记 proxy 并跳过或保守化流动性评分，避免把无盘口样本评为高流动性。

<!-- DEB-VERDICT cluster=18 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: futu_lv3_provider.py:1862-1868 push Quote缓存在盘口缺失时bid/ask回退为last_price(L484-485), 导致L1889-1895 spread_pct=0/liquidity_score=100/tradeable=True, 无真实盘口却报满分流动性, 可能误导流动性门控交易决策

### 议题 #30 [独有 1/3] `app/core/atr_provider.py`:156-159 — ATRProvider.invalidate 未使用锁，与线程安全声明不一致

环节: s3_scheduling_pipeline · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max** (S2, race_condition, app/core/atr_provider.py:156-159): ATRProvider.invalidate 未使用锁，与线程安全声明不一致
  - 证据引用: `        if symbol:
            self._cache.pop(symbol, None)
        else:
            self._cache.clear()`
  - 影响声称: 该类其他读路径使用 self._lock，但 invalidate 直接修改共享缓存；并发调用时可能导致缓存字典状态不一致或迭代/诊断异常。
  - 建议: 在 invalidate 和 cache_stats 等所有访问 _cache 的路径统一使用 self._lock。

<!-- DEB-VERDICT cluster=30 -->
裁决: REJECTED | 置信=medium | 理由: atr_provider.py:156-159 dict.pop/clear 在 CPython GIL 下为原子操作不会 crash,且 invalidate 语义为"清空缓存",与并发 get_atr(line 100-101 with lock)竞态仅导致缓存未命中重建,无数据损坏风险

### 议题 #37 [独有 1/3] `app/pipeline/order_executor.py`:241-245 — broker 持仓 qty 解析失败时返回 0，被误判为无持仓

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency · 自报最高 severity: S4
- **qwen3.8-max** (S4, dataflow_inconsistency, app/pipeline/order_executor.py:241-245): broker 持仓 qty 解析失败时返回 0，被误判为无持仓
  - 证据引用: `                _qty = _p.get("qty", 0) if isinstance(_p, dict) else getattr(_p, "qty", 0)
                try:
                    return abs(float(_qty))
                except Exception:
                    return 0.0`
  - 影响声称: 当 broker 返回 qty 为 None、字符串异常或字段缺失时，会被当成持仓为 0，触发 _close_pre_check 的 broker-zero 清理路径，造成内存/资金账本与真实持仓错误收敛。
  - 建议: 解析失败应返回 None 表示未知，而不是 0.0；调用方仅在明确为 0 时才清理持仓。

<!-- DEB-VERDICT cluster=37 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: order_executor.py:244 except Exception 返回 0.0 而非 None,与 line 246 broker无此symbol 的 0.0 不可区分;调用方 line 412 _broker_qty<=0 触发清理,将 qty 解析异常误判为无持仓并删除内存仓位

### 议题 #38 [独有 1/3] `app/pipeline/order_executor.py`:285-289 — broker 未结订单查询异常时返回空列表，被误判为无未结订单

环节: s3_scheduling_pipeline · 类别: error_swallowing · 自报最高 severity: S4
- **qwen3.8-max** (S4, error_swallowing, app/pipeline/order_executor.py:285-289): broker 未结订单查询异常时返回空列表，被误判为无未结订单
  - 证据引用: `    except ImportError:
        return []
    except Exception as _e:
        logger.debug(f"[平仓·open_orders] {symbol} 查询异常: {_e}")
        return []`
  - 影响声称: 查询失败与真实无未结订单不可区分，_wait_or_cancel_open_orders 会直接返回安全，可能在存在未知挂单时继续下单。
  - 建议: 查询失败应返回 None 或抛出专用异常，调用方按未知状态处理，禁止继续平仓。

<!-- DEB-VERDICT cluster=38 -->
裁决: CONFIRMED | severity=3 | 置信=high | 理由: order_executor.py:287-289 except Exception 返回 [] 与无未结订单不可区分,调用方 _wait_or_cancel_open_orders(line 301-302) 视 [] 为安全直接放行,实际可能存在未结订单但因查询异常被忽略

### 议题 #41 [独有 1/3] `app/pipeline/order_executor.py`:643-657 — 缺少执行引擎时非 real 环境直接模拟成交

环节: s3_scheduling_pipeline · 类别: state_machine · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, state_machine, app/pipeline/order_executor.py:643-657): 缺少执行引擎时非 real 环境直接模拟成交
  - 证据引用: `                order.state = OrderState.FILLED
                order.filled_quantity = order.quantity
                order.avg_fill_price = signal_price or order.price`
  - 影响声称: 若 execution_engine 初始化失败且 AUTO_TRADE_ACCOUNT 未显式为 real，订单会本地标记成交并可能进入持仓管理，形成与 broker 不一致的幽灵仓位，影响后续退出和风控。
  - 建议: 除明确回测/纸面模式外，缺少执行引擎应统一 REJECTED；模拟成交应由独立 simulated broker adapter 显式完成，而不是在订单优化层直接伪造成交。

<!-- DEB-VERDICT cluster=41 -->
裁决: REJECTED | 置信=high | 理由: order_executor.py:643-657 有明确守卫: AUTO_TRADE_ACCOUNT=="real" 时 line 647 置 REJECTED 拒绝模拟成交;else 分支仅在 sim 模式下模拟成交,属设计意图(注释"P0 2026-06-13: 实盘必须拒绝")

### 议题 #33 [独有 1/3] `app/core/scheduler_core.py`:2524-2538 — 突发新闻即时扫描冷却字典无锁，可能重复触发扫描

环节: s3_scheduling_pipeline · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max** (S2, race_condition, app/core/scheduler_core.py:2524-2538): 突发新闻即时扫描冷却字典无锁，可能重复触发扫描
  - 证据引用: `            cooldowns = getattr(self, "_news_immediate_cooldowns", {})
            for symbol in symbols:
                last_triggered = cooldowns.get(symbol, 0)
                if now_ts - last_triggered < cooldown_sec:
                    logger.debug("[NewsV2-Immediate] %s 冷却中，跳过", symbol)
    `
  - 影响声称: 多个新闻回调并发时，可能在同一 symbol 上同时通过冷却检查并重复调用 trigger_immediate_scan，造成重复扫描/重复信号压力。
  - 建议: 对 _news_immediate_cooldowns 的读取、判断、写入加锁，或使用原子化的 per-symbol lock/set。

<!-- DEB-VERDICT cluster=33 -->
裁决: REJECTED | 置信=medium | 理由: scheduler_core.py:2524-2538 dict 操作在 GIL 下原子安全;最坏情况(两线程同时判定冷却过期)仅多触发一次 trigger_immediate_scan(line 2536),扫描本身幂等且有独立去重,不会造成错单或资损

### 议题 #34 [独有 1/3] `app/core/scheduler_core.py`:2720-2735 — 内部人扫描哨兵使用相对路径 data/ 可能与项目数据目录不一致

环节: s3_scheduling_pipeline · 类别: other, time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/core/scheduler_core.py:2729-2735): 内部人扫描窗口按小时判断会覆盖开盘后 09:30-10:00
  - 证据引用: `                # 每日只扫一次 (ET 06:00-09:30 之间触发, 给盘前留时间)
                if hour < 6 or hour >= 10:`
  - 影响声称: 注释目标是盘前 06:00-09:30，但代码只排除 10:00 之后，09:00-09:59 仍会触发，可能在开盘后执行盘前扫描，产生非预期时段的信号或重复扫描。
  - 建议: 使用分钟级边界判断，例如 hour*60+minute < 9*60+30；若确实允许 09:00-10:00，应同步注释和交易时段语义。
- **qwen3.8-max-0902** (S2, other, app/core/scheduler_core.py:2720-2720): 内部人扫描哨兵使用相对路径 data/ 可能与项目数据目录不一致
  - 证据引用: `_sentinel_file = Path("data") / ".insider_scanned_today"`
  - 影响声称: 若服务进程工作目录不是项目根目录，哨兵文件会读写到错误位置，导致每日扫描可能重复触发或误认为未扫描，破坏每日一次的去重边界。
  - 建议: 使用 app.core.paths.get_data_dir() 或统一路径解析模块生成哨兵文件路径，避免依赖当前工作目录。

<!-- DEB-VERDICT cluster=34 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: scheduler_core.py:2735 条件 hour<6 or hour>=10 实际允许 hour∈{6,7,8,9} 即 06:00-09:59,而注释(line 2729)明确标注"06:00-09:30 之间触发",09:30-10:00 开盘后窗口仍在扫描范围内与文档不符;相对路径 Path("data")(line 2720)未使用 paths.get_data_dir() 但 CWD 通常=项目根故影响有限

### 议题 #35 [独有 1/3] `app/core/scheduler_core.py`:2821-2840 — 延迟审计队列在引擎不可用或处理失败时直接丢弃任务

环节: s3_scheduling_pipeline · 类别: error_swallowing · 自报最高 severity: S2
- **qwen3.8-max** (S2, error_swallowing, app/core/scheduler_core.py:2821-2840): 延迟审计队列在引擎不可用或处理失败时直接丢弃任务
  - 证据引用: `        if not llm_engine:
            logger.debug("[LLM 审计] LLM 引擎不可用，跳过延迟队列")
            self._deferred_audit_queue = []
            return
        self._deferred_audit_queue = self._deferred_audit_queue[10:]`
  - 影响声称: LLM 引擎暂不可用或单条审计异常时，队列被清空或按固定 10 条切掉，失败任务不会重试，审计数据链路静默丢失。
  - 建议: 保留失败项并设置最大重试次数；引擎不可用时保留队列待下次处理，而不是清空。

<!-- DEB-VERDICT cluster=35 -->
裁决: REJECTED | 置信=medium | 理由: scheduler_core.py:2821-2823 LLM 审计为辅助诊断功能,queue 清空属有意设计(防止无限堆积);line 2822 日志明确"跳过延迟队列",非静默丢弃;审计失败不影响订单执行/资金/风控主链路

### 议题 #45 [独有 1/3] `app/pipeline/order_executor.py`:5181-5183 — signal_lifecycle 对非主线仓位仍写 MAIN_PIPELINE

环节: s3_scheduling_pipeline · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/pipeline/order_executor.py:5181-5183): signal_lifecycle 对非主线仓位仍写 MAIN_PIPELINE
  - 证据引用: `                    getattr(position, "owner_strategy", None) or "main",
                    "MAIN_PIPELINE",
                    _sl_trade_date,`
  - 影响声称: playbook/orb 等 owner_strategy 的平仓结果被 source_type 误标为 MAIN_PIPELINE，后续按来源归因、绩效统计和闭环学习会被污染。
  - 建议: 根据 owner_strategy 或 position.metadata 使用 _OWNER_SOURCE_TYPE 派生真实 source_type，而不是硬编码 MAIN_PIPELINE。

<!-- DEB-VERDICT cluster=45 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: order_executor.py:5182 source_type 硬编码 "MAIN_PIPELINE",未使用 _OWNER_SOURCE_TYPE 映射(line 117-122 定义了 playbook_sim→PLAYBOOK_SIM 等),而 trade_records 路径(line 5058-5061)正确使用了该映射,signal_lifecycle 表 playbook/orb 仓位归因错误

### 议题 #58 [独有 1/3] `app/llm/features/json_schema_guard.py`:31-38 — JSON 抽取只识别对象，不支持顶层数组

环节: s4_signals_sources · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/llm/features/json_schema_guard.py:31-38): JSON 抽取只识别对象，不支持顶层数组
  - 证据引用: `m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    start = text.find("{")`
  - 影响声称: 当 LLM 按 weekly 等 prompt 返回顶层数组或数组 fenced block 时，解析失败返回 None。
  - 建议: 支持顶层数组，使用 json.loads 后判断 dict/list，或正则同时匹配 [...]。

<!-- DEB-VERDICT cluster=58 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: json_schema_guard.py:31正则仅匹配\{.*?\}对象,:38 text.find("{")仅查找花括号,顶层数组([...]格式)无法被步骤2/3抽取;步骤1 json.loads可解析数组但:69 validate_schema首行检查isinstance(obj,dict)会拒绝,函数签名返回Optional[Dict]

### 议题 #53 [独有 1/3] `app/factors/trade_quality_gate.py`:36-36 — min_score_to_trade 配置读取后全文零引用, 综合分门槛从未生效

环节: s4_signals_sources · 类别: config_effect · 自报最高 severity: S2
- **kimi-k3** (S2, config_effect, app/factors/trade_quality_gate.py:36-36): min_score_to_trade 配置读取后全文零引用, 综合分门槛从未生效
  - 证据引用: `36行 `self.min_score = self.config.get('min_score_to_trade', 0.60)`; evaluate/_finalize (72-165行) 只用各门 passed 硬拦, final_score 从不与 self.min_score 比较`
  - 影响声称: yaml 中调 min_score_to_trade 无任何效果; 五门全过但加权总分仅 0.3 的低质信号照样 passed_gate=True 放行, 与'漏斗筛选'设计意图不符
  - 建议: 在 _finalize 中加 `passed_gate = passed_gate and final_score >= self.min_score`

<!-- DEB-VERDICT cluster=53 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: trade_quality_gate.py:36 self.min_score=self.config.get('min_score_to_trade',0.60)读取后全文零引用,evaluate/_finalize(:72-165)仅使用各门passed硬性拦截,final_score从不与min_score比较,综合分门槛形同虚设

### 议题 #62 [独有 1/3] `app/news_v2/quality/dedup_v2.py`:90-92 — check_and_mark 非原子，先查后写存在并发重复窗口

环节: s4_signals_sources · 类别: race_condition · 自报最高 severity: S2
- **qwen3.8-max** (S2, race_condition, app/news_v2/quality/dedup_v2.py:90-92): check_and_mark 非原子，先查后写存在并发重复窗口
  - 证据引用: `if self.is_duplicate(title_hash, simhash, source, title, similarity_threshold):
            return False
        self.mark_as_seen(title_hash, simhash, source, title)`
  - 影响声称: 两个线程可能同时通过 is_duplicate 检查，再分别 mark_as_seen，导致重复新闻都被放行。
  - 建议: 在同一把锁/事务内完成查重与写入，或使用 INSERT ... WHERE NOT EXISTS 保证原子性。

<!-- DEB-VERDICT cluster=62 -->
裁决: CONFIRMED | severity=1 | 置信=medium | 理由: dedup_v2.py:90 is_duplicate和:92 mark_as_seen各自独立获取self._lock,:87-92 check_and_mark在两次锁释放间存在TOCTOU窗口,并发调用可产生重复记录;SQLite UNIQUE约束防数据损坏但允许重复插入被IGNORE

### 议题 #48 [独有 1/3] `app/factors/execution_assessor.py`:103-125 — 点差过大拦截分支在默认配置下不可达 (死代码)

环节: s4_signals_sources · 类别: dead_code · 自报最高 severity: S2
- **kimi-k3** (S2, dead_code, app/factors/execution_assessor.py:103-125): 点差过大拦截分支在默认配置下不可达 (死代码)
  - 证据引用: `103-104行 `if liq_score < 0.4: est_spread_bps = 50`; 118行 `if liq_score < self.min_liquidity:` (默认0.4) 已先行拦截; 122行 `elif est_spread_bps > self.max_slippage:` (默认50)`
  - 影响声称: est_spread_bps=50 仅当 liq_score<0.4 出现, 而该情况已在 118 行返回; 到达 122 行时 est∈{20,5,1}, 均不大于 50 → '预估点差过大'拒绝路径永不触发, 给人有点差风控的假象
  - 建议: 调整 spread 映射/阈值使分支可达, 或删除该分支避免误导

<!-- DEB-VERDICT cluster=48 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: execution_assessor.py:103 liq_score<0.4→est_spread_bps=50,:118 liq_score<self.min_liquidity(默认0.4)先行拦截并return,:122 elif est_spread_bps>self.max_slippage(默认50)在默认配置下不可达(50>50为False);仅当用户修改配置时才有意义

### 议题 #57 [独有 1/3] `app/llm/features/audit_db.py`:105-127 — record_call 异常路径连接未关闭, 句柄泄漏

环节: s4_signals_sources · 类别: resource_leak · 自报最高 severity: S1
- **kimi-k3** (S1, resource_leak, app/llm/features/audit_db.py:105-127): record_call 异常路径连接未关闭, 句柄泄漏
  - 证据引用: `95行 `conn = sqlite3.connect(_get_db_path())`; 124-125行 `conn.commit()` / `conn.close()` 不在 finally; 126行 `except Exception as e: logger.debug(...)` 吞掉异常但 conn 未关`
  - 影响声称: 审计写入反复失败 (如磁盘满/锁冲突) 时, 每次调用泄漏一个 SQLite 连接句柄, 长期运行累积
  - 建议: 用 `with closing(sqlite3.connect(...)) as conn:` 或在 finally 中 close

<!-- DEB-VERDICT cluster=57 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: audit_db.py:95 conn=sqlite3.connect()在try块内,:124-125 conn.commit()/conn.close()不在finally块中;若:96-123任何操作抛异常,:126 except捕获后conn未关闭导致SQLite连接句柄泄漏

### 议题 #64 [独有 1/3] `app/news_v2/sources/rsshub_us_source.py`:133-140 — pubDate 解析失败时用当前时间作为新闻时间

环节: s4_signals_sources · 类别: time_boundary · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, time_boundary, app/news_v2/sources/rsshub_us_source.py:133-140): pubDate 解析失败时用当前时间作为新闻时间
  - 证据引用: `timestamp = time.time()
                        if pub_date:
                            try:
                                from email.utils import parsedate_to_datetime
                                dt = parsedate_to_datetime(pub_date)
                                timestamp = dt.timestamp()
`
  - 影响声称: 旧新闻或异常日期会被当作刚发生，绕过基于年龄的过期/衰减判断。
  - 建议: 解析失败时丢弃，或使用当前时间但标记低置信/不可交易。

<!-- DEB-VERDICT cluster=64 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: rsshub_us_source.py:133 timestamp=time.time()作为默认值,:134-140 pubDate解析失败时保持当前时间,导致过期新闻被视为即时新闻,可能触发基于时效的信号生成

### 议题 #47 [独有 1/3] `app/factors/capital_flow.py`:143-145 — 五门异常处理语义不一致: 资金流门 fail-closed, 其余三门 fail-open

环节: s4_signals_sources · 类别: error_swallowing · 自报最高 severity: S2
- **kimi-k3** (S2, error_swallowing, app/factors/capital_flow.py:143-145): 五门异常处理语义不一致: 资金流门 fail-closed, 其余三门 fail-open
  - 证据引用: `本文件 145行 `return GateResult(name="CapitalFlowMonitor", passed=False, score=0.0, reason=f"Error: {e}")`; 对比 leader_pool.py 131行 / execution_assessor.py 136行 / regime_filter.py 97行异常时均 `passed=True, score=0.5``
  - 影响声称: 同类数据异常 (如 volumes 长度不足触发 IndexError) 在资金流门硬拦交易, 在其他门静默放行, 门控行为不可预期, 排障时难以定位是哪一门拦截
  - 建议: 统一异常策略 (建议 fail-closed 或返回 metadata 显式标记 error, 由 TradeQualityGate 统一裁决)

<!-- DEB-VERDICT cluster=47 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: capital_flow.py:145异常时返回passed=False(score=0.0,fail-closed),对比leader_pool.py:131/execution_assessor.py:136/regime_filter.py:97异常时均返回passed=True(fail-open),五门中仅此一门fail-closed,语义不一致可能导致资金流门异常时单独阻断所有交易

### 议题 #65 [独有 1/3] `app/news_v2/sources/rsshub_us_source.py`:177-181 — 备用实例切换为死代码: RSSHUB_INSTANCES 只剩 1 个元素

环节: s4_signals_sources · 类别: dead_code · 自报最高 severity: S1
- **kimi-k3** (S1, dead_code, app/news_v2/sources/rsshub_us_source.py:177-181): 备用实例切换为死代码: RSSHUB_INSTANCES 只剩 1 个元素
  - 证据引用: `24-26行 `RSSHUB_INSTANCES = ["https://rsshub.rssforever.com",]`; 177行 `if not items and self._active_instance < len(RSSHUB_INSTANCES) - 1:` 即 `0 < 0` 恒 False`
  - 影响声称: 实例故障转移与递归重试永远不会发生, 唯一实例不可用时该源直接归零; 代码给维护者'有容灾切换'的假象
  - 建议: 补充可用备用实例, 或删除切换分支并明确单实例风险

<!-- DEB-VERDICT cluster=65 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: rsshub_us_source.py:24-26 RSSHUB_INSTANCES仅含1个元素,:177条件self._active_instance<len(RSSHUB_INSTANCES)-1即0<0恒False,备用实例切换逻辑为死代码

### 议题 #56 [独有 1/3] `app/llm/agent_bridge.py`:999-1021 — _check_plan_response/_check_eod_response 未剥离 markdown fence, 且解析失败的毒文件不删除

环节: s4_signals_sources · 类别: logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/llm/agent_bridge.py:999-1021): _check_plan_response/_check_eod_response 未剥离 markdown fence, 且解析失败的毒文件不删除
  - 证据引用: `1000-1001行 `with open(latest, 'r', encoding='utf-8') as f: response = json.load(f)` (EOD 路径 1257-1258行同样); 对比 wait_for_response 666-671行有 `if cleaned_text.startswith('```json'):` 等清洗; 解析失败走 1019-1021行 `return None`, 而 `os.remove(latest)` 在 1010行 (成功分支内)`
  - 影响声称: Hermes 写入带 ```json fence 的响应时, 这两条轮询路径永远解析失败返回 None, 毒文件残留后被反复读取反复失败, 盘前计划静默丢失 (调用方无法区分'无响应'与'解析失败')
  - 建议: 抽取公共清洗函数复用; 解析失败时将文件移入 quarantine 或删除

<!-- DEB-VERDICT cluster=56 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: agent_bridge.py:1000-1001 _check_plan_response和:1257-1258 _check_eod_response直接用json.load未像wait_for_response:666-671那样剥离```json fence;:1019-1021解析失败return None但不删除毒文件,下次轮询同一文件再次失败形成死循环

### 议题 #81 [独有 1/3] `app/orb/exits.py`:9-103 — 2R 止盈依赖 sl_distance，但 FloorStop 只按 ATR×mult 计算

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/orb/exits.py:9-103): 2R 止盈依赖 sl_distance，但 FloorStop 只按 ATR×mult 计算
  - 证据引用: `止盈 (2R) 与 突破失败 (breakdown) 由 ORBEngine._monitor_exits 直接判定,
优先级高于本栈 — 因为它们依赖 sl_distance / orb_mid (engine 持有).
atr_stop = entry_price - atr * self._atr_mult
stop = max(orb_mid, atr_stop) if orb_mid > 0 else atr_stop`
  - 影响声称: 若 engine 用自适应止损生成 pos.sl_distance，而 FloorStop 仍按固定 ATR 倍数计算，风险单位、2R 止盈距离和硬止损距离会不一致，可能出现提前扫损或止损/止盈风险比失真。
  - 建议: 统一止损来源：FloorStop 直接消费 ORBPosition.sl_distance，或明确删除自适应止损路径，保证 2R 与硬止损使用同一风险单位。

<!-- DEB-VERDICT cluster=81 -->
裁决: REJECTED | 置信=high | 理由: exits.py:9-17 文档明确记载 2R 止盈与突破失败由 ORBEngine._monitor_exits 先行判定(engine.py:1478-1495 使用 pos.sl_distance), 退出栈仅负责 FloorStop(ATR+orb_mid) 和 EOD, 这是已记录的架构分层而非数据流脱节

### 议题 #70 [独有 1/3] `app/coordinator/symbol_lock.py`:63-90 — SymbolLock observe 模式冲突放行但不记录请求者，后续 release 无法释放其逻辑持仓

环节: s5_decision_orchestration · 类别: state_machine · 自报最高 severity: S2
- **qwen3.8-max** (S2, state_machine, app/coordinator/symbol_lock.py:63-90): SymbolLock observe 模式冲突放行但不记录请求者，后续 release 无法释放其逻辑持仓
  - 证据引用: `decision = "OBSERVE_CONFLICT" if self._observe_only else "DENIED"
if strategy is not None and holder != strategy:
    return`
  - 影响声称: observe 模式下第二个策略拿到 True 但 holder 不变；其后续 release 会被忽略，锁状态与实际多持仓不一致，reconcile 也可能误判。
  - 建议: observe 模式应记录次级持有者/观察列表，或返回 False；release 应能释放请求者对应的逻辑锁。

<!-- DEB-VERDICT cluster=70 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: symbol_lock.py:63-71 observe 模式冲突时返回 True(放行)但 _locks 字典仅保留原 holder, 请求者 strategy 身份未记录, 其后续 release(line 86 holder!=strategy 检查)无法释放锁, 需依赖 reconcile_with_positions(line 104)对账修复

### 议题 #75 [独有 1/3] `app/orb/engine.py`:75-80 — ORBPosition.r_multiple 未使用兜底后的 sl_distance

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/orb/engine.py:75-80): ORBPosition.r_multiple 未使用兜底后的 sl_distance
  - 证据引用: `self.sl_distance = sl_distance if sl_distance > 0 else atr_at_entry  # 兜底
self.r_multiple = sl_distance     # 风险单位`
  - 影响声称: 构造时若传入 sl_distance=0（默认值），self.sl_distance 会回退为 atr_at_entry，但 r_multiple 仍为 0；后续若用 r_multiple 表达风险单位/止盈倍数，会得到 0R。
  - 建议: 在 sl_distance 兜底后赋值：self.r_multiple = self.sl_distance，或让 r_multiple 成为基于 self.sl_distance 的 property。

<!-- DEB-VERDICT cluster=75 -->
裁决: REJECTED | 置信=high | 理由: engine.py:80 r_multiple 赋值使用原始 sl_distance 而非 line 75 兜底后的 self.sl_distance 是事实, 但 grep 全仓确认 r_multiple 除定义外从未被读取, 2R 止盈(line 1484)直接使用 pos.sl_distance(已兜底), 死代码不影响运行

### 议题 #71 [独有 1/3] `app/coordinator/symbol_lock.py`:157-161 — get_symbol_lock_manager 单例忽略后续 observe_only 参数

环节: s5_decision_orchestration · 类别: config_effect · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, config_effect, app/coordinator/symbol_lock.py:157-161): get_symbol_lock_manager 单例忽略后续 observe_only 参数
  - 证据引用: `def get_symbol_lock_manager(observe_only: bool = True) -> SymbolLockManager:
    global _singleton
    if _singleton is None:
        _singleton = SymbolLockManager(observe_only=observe_only)
    return _singleton`
  - 影响声称: 若首次调用以默认 observe_only=True 创建单例，后续即使传入 False 也不会切换到强制模式，symbol 互斥锁可能长期只观察不阻断。
  - 建议: 单例创建后根据参数调用 set_observe_only，或显式提供配置初始化入口并禁止参数误导。

<!-- DEB-VERDICT cluster=71 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: symbol_lock.py:157-161 单例创建后 observe_only 参数被静默忽略, 若首次以 observe_only=False 创建则后续所有 True 调用仍为强制模式(反之亦然), 但 set_observe_only() 方法(line 36)提供了显式修改路径, 实际影响可控

### 议题 #82 [独有 1/3] `app/orb/exits.py`:158-166 — ORBExitStack.evaluate 不消费 ORBPosition.sl_distance，止损字段与退出栈脱节

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max** (S3, dataflow_inconsistency, app/orb/exits.py:158-166): ORBExitStack.evaluate 不消费 ORBPosition.sl_distance，止损字段与退出栈脱节
  - 证据引用: `def evaluate(
    self,
    direction: str,
    entry_price: float,
    current_price: float,
    atr: float,
    orb_mid: float,
    current_time: dt_time,
) -> Optional[ORBExitDecision]:`
  - 影响声称: ORBPosition 维护并隔夜调整 sl_distance，但退出栈只使用 atr*atr_mult；若引擎依赖 stack 做 FloorStop，pos.sl_distance 的放宽/收紧不会生效，造成风控状态不一致。
  - 建议: 将 sl_distance 或有效止损价传入 ORBExitStack.evaluate，并让 ORBFloorStop 优先使用仓位维护的止损距离。

<!-- DEB-VERDICT cluster=82 -->
裁决: REJECTED | 置信=high | 理由: exits.py:158-166 evaluate() 不接收 sl_distance 是设计意图: engine._monitor_exits(line 1478-1524) 已先行处理 sl_distance 相关的 2R 止盈和突破失败并 continue 跳过, 退出栈仅处理 ATR 硬止损与盘末退出两条独立路径

### 议题 #76 [独有 1/3] `app/orb/engine.py`:239-244 — ORB 与主线标的池重叠仅打 critical 日志，不阻断启动

环节: s5_decision_orchestration · 类别: config_effect · 自报最高 severity: S3
- **qwen3.8-max** (S3, config_effect, app/orb/engine.py:239-244): ORB 与主线标的池重叠仅打 critical 日志，不阻断启动
  - 证据引用: `if overlap:
    logger.critical(
        "🚨 [ORB·ADR-006] 标的池与主线重叠: %s — "
        "请修改 unified_config.yaml 消除重叠", overlap
    )`
  - 影响声称: 重叠配置下 ORB daemon 仍启动，可能与主线同时评估/交易同一标的，破坏互斥设计。
  - 建议: 检测到 overlap 时 raise/return 阻止启动，或自动从 ORB universe 剔除重叠标的并告警。

<!-- DEB-VERDICT cluster=76 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: engine.py:239-244 检测到 ORB 与主线标的池重叠仅 logger.critical 告警后继续启动, 不阻断也不 return, 两套策略可对同一标的同时下单造成双倍仓位, 虽有 reconcile 机制兜底但运行时风险暴露真实存在

### 议题 #78 [独有 1/3] `app/orb/engine.py`:792-808 — 最大持仓数使用的账户资金与仓位权益口径不一致

环节: s5_decision_orchestration · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max** (S2, dataflow_inconsistency, app/orb/engine.py:792-808): 最大持仓数使用的账户资金与仓位权益口径不一致
  - 证据引用: `def _get_account_equity(self) -> float:
    return 100000.0`
  - 影响声称: _calculate_max_positions 使用 _get_account_equity 的 PositionStore/initial_capital/100K 兜底，而另一处 _get_equity 使用 capital_safety buying_power/1M 兜底；持仓数量限制与资金口径可能不一致。
  - 建议: 统一账户权益读取来源，最大持仓数也应使用 capital_safety 的 buying_power/equity 或同一 helper。

<!-- DEB-VERDICT cluster=78 -->
裁决: CONFIRMED | severity=2 | 置信=low | 理由: engine.py:792 _get_account_equity() 默认返回 $100K(line 808) 用于最大持仓数计算(line 765), 而同文件 _get_equity() 默认返回 $1M(line 2004) 用于仓位 sizing(line 1280), 两个资金口径在 fallback 路径上差 10x, 可能导致持仓数与仓位不匹配

### 议题 #79 [独有 1/3] `app/orb/engine.py`:2003-2004 — ORB 权益读取失败时 fallback 为 $1,000,000

环节: s5_decision_orchestration · 类别: numeric_unit · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, numeric_unit, app/orb/engine.py:2003-2004): ORB 权益读取失败时 fallback 为 $1,000,000
  - 证据引用: `# 极端情况: 返回模拟盘默认值
return 1000000.0  # 模拟盘默认 $1M (非 $100K)`
  - 影响声称: capital_safety/config 不可用时用 $1M 兜底，可能显著高估可动用资金，导致仓位或风险计算偏大。
  - 建议: 权益读取失败应 fail-close、使用更保守默认值，或拒绝开仓直到权益可确认。

<!-- DEB-VERDICT cluster=79 -->
裁决: UNCERTAIN | 置信=low | 理由: engine.py:2004 _get_equity() 极端 fallback 返回 $1M 用于仓位计算(line 1280), 若实际账户仅 $100K 则仓位放大 10x 后果严重; 但需验证 capital_safety 和 config capital.initial 是否总能在生产环境提供有效值使此 fallback 不可达

### 议题 #83 [独有 1/3] `app/playbook/overnight_hold_shadow.py`:23-23 — 隔夜 shadow 输出使用相对路径，依赖进程当前工作目录

环节: s6_playbook_engine · 类别: config_effect · 自报最高 severity: S2
- **qwen3.8-max** (S2, config_effect, app/playbook/overnight_hold_shadow.py:23-23): 隔夜 shadow 输出使用相对路径，依赖进程当前工作目录
  - 证据引用: `_OUTPUT = Path("data/overnight_hold_shadow.jsonl")`
  - 影响声称: 若服务不是从仓库根目录启动，或测试/脚本改变 CWD，shadow 行会写到非预期位置，造成影子结算缺数和审计链路断裂。
  - 建议: 改为基于 __file__ 的仓库根路径，如 Path(__file__).resolve().parents[2] / "data" / "overnight_hold_shadow.jsonl"。

<!-- DEB-VERDICT cluster=83 -->
裁决: CONFIRMED | severity=1 | 置信=high | 理由: overnight_hold_shadow.py:23 _OUTPUT=Path("data/overnight_hold_shadow.jsonl") 使用相对路径，进程 CWD 非仓库根时影子文件写入错误位置（对比 pipeline.py 用 __file__ 绝对路径）

### 议题 #88 [独有 1/3] `app/playbook/playbook_service.py`:291-292 — factor_screen 缺失日期时不清空 screen_map，但仍标记 stale

环节: s6_playbook_engine · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max** (S2, dataflow_inconsistency, app/playbook/playbook_service.py:291-292): factor_screen 缺失日期时不清空 screen_map，但仍标记 stale
  - 证据引用: `        if screen_date and screen_date != trade_date:
            screen_map = {}`
  - 影响声称: 当 factor_screen_latest.json 没有 timestamp/screen_date 时，screen_map 仍会展示旧因子分，而漏斗层会标记 screen_stale=True，前端可能同时看到“陈旧筛选”和具体因子评分，产生误导。
  - 建议: 改为 if (not screen_date) or screen_date != trade_date: screen_map = {}，与 screen_stale 语义一致。

<!-- DEB-VERDICT cluster=88 -->
裁决: CONFIRMED | severity=1 | 置信=medium | 理由: playbook_service.py:291 当 screen_date 为空字符串时条件不成立不清空 screen_map，但 435 行 screen_stale=True；陈旧因子分通过 417-420 行泄漏到 per-instruction 输出（文件自 2026-07-10 无生产者，实际触发概率低）

### 议题 #85 [独有 1/3] `app/playbook/pipeline.py`:861-861 — BridgeVotingAdapter.vote 对 score 无类型守卫 — bridge 候选 score:null 直接 TypeError

环节: s6_playbook_engine · 类别: logic_anomaly · 自报最高 severity: S2
- **kimi-k3** (S2, logic_anomaly, app/playbook/pipeline.py:861-861): BridgeVotingAdapter.vote 对 score 无类型守卫 — bridge 候选 score:null 直接 TypeError
  - 证据引用: `861: score = float(cand.get("score", 5.0)); schema 校验 937: if not c.get("symbol") or not c.get("direction") or c.get("decision_id") 的反面 — 实际为 if not c.get("symbol") or not c.get("direction") or not c.get("decision_id"): return False (不要求 score); 同函数 877: if isinstance(_raw_conf, bool) or not isinsta`
  - 影响声称: bridge JSON 中 score 显式为 null 时 (schema 合法, get 的默认值不生效) float(None) 抛 TypeError, vote() 无 try/except → 该候选异常中断, 视调用方处理可能打断整个生成循环。
  - 建议: 与 confidence 同款守卫: _raw = cand.get("score"); score = float(_raw) if (isinstance(_raw, (int, float)) and not isinstance(_raw, bool)) else 5.0。

<!-- DEB-VERDICT cluster=85 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: pipeline.py:861 float(cand.get("score", 5.0)) 当 score 键存在但值为 None 时 .get() 返回 None 而非默认 5.0，float(None) 抛 TypeError；schema 校验 937 行不检查 score 字段

### 议题 #93 [独有 1/3] `app/playbook/playbook_service.py`:2200-2207 — _et_today 的 ImportError 兜底退化为服务器本地日期, 与 ET 约定不一致

环节: s6_playbook_engine · 类别: time_boundary · 自报最高 severity: S1
- **kimi-k3** (S1, time_boundary, app/playbook/playbook_service.py:2200-2207): _et_today 的 ImportError 兜底退化为服务器本地日期, 与 ET 约定不一致
  - 证据引用: `2203-2206: from app.web_dashboard.data_service import et_today / return et_today() / except ImportError: / return date.today().isoformat()`
  - 影响声称: 兜底路径返回服务器本地日期 (pipeline.py 543 注释自证服务器为 CST), 与 ET 交易日最多差一天 → 兜底生效时 get_today_instructions / get_pipeline_view 查询错误日期, 且仅捕获 ImportError, et_today() 自身异常会直接外抛。
  - 建议: 兜底同样按 ET 取日: datetime.now(ZoneInfo("America/New_York")).date().isoformat(), 并将 except 范围扩到 Exception。

<!-- DEB-VERDICT cluster=93 -->
裁决: CONFIRMED | severity=1 | 置信=medium | 理由: playbook_service.py:2206-2207 ImportError 兜底用 date.today() 返回服务器本地日期而非 ET 日期，CST 服务器 23:00 后与 ET 日期差一天；但 et_today 在 data_service.py:41 正常定义，ImportError 极少触发

### 议题 #86 [独有 1/3] `app/playbook/pipeline.py`:8822-8925 — trade_records.metadata_json 先读后写且无并发保护，可能覆盖其他写入

环节: s6_playbook_engine · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, race_condition, app/playbook/pipeline.py:8822-8925): trade_records.metadata_json 先读后写且无并发保护，可能覆盖其他写入
  - 证据引用: `_meta = _json.loads(r["metadata_json"]) if r["metadata_json"] else {}
                conn.execute("UPDATE trade_records SET metadata_json=? WHERE id=?",
                             (_json.dumps(_meta), r["id"]))
                conn.commit()`
  - 影响声称: EOD 兜底在读取行后可能经过行情查询/下单等耗时操作，再用整包 `_meta` 覆盖 `metadata_json`；若盘中退出监控、对账或其他线程同时更新同一行，后写会吞掉并发字段更新。
  - 建议: 对 metadata 做局部 JSON patch/单独列更新，或在事务内使用版本戳/`WHERE metadata_json=?` 乐观锁，避免整字段盲覆盖。

<!-- DEB-VERDICT cluster=86 -->
裁决: UNCERTAIN | 置信=low | 理由: pipeline.py:8822 读 metadata_json 后 8923 写回间隔内有 broker 操作，若其他线程/进程并发写同字段会丢失更新；需确认 EOD 期间是否有并发写入 metadata_json

### 议题 #104 [独有 1/3] `app/risk/position_exit_monitor.py`:420-423 — _has_exit_record 在 DB 异常时返回 True，可能吞掉正常退出

环节: s7_risk_execution · 类别: error_swallowing · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, error_swallowing, app/risk/position_exit_monitor.py:420-423): _has_exit_record 在 DB 异常时返回 True，可能吞掉正常退出
  - 证据引用: `        except Exception as e:
            # P0-fix: DB错误时 fail-safe 返回 True (假设已退出, 跳过本轮避免双重平仓)
            logger.warning(f"[exit_monitor] _has_exit_record DB查询失败 (fail-safe=True): {e}")
            return True`
  - 影响声称: 该函数用于防止重复退出，但 DB 查询失败时假设已退出；若 DB 抖动/损坏，真实未退出仓位可能被连续跳过，止损/止盈无法执行。
  - 建议: 区分防重复与风控退出授权：DB 失败时应结合内存冷却、持仓状态或 fail-open 并加强告警；对强制止损路径不应因去重查询失败而整体跳过。

<!-- DEB-VERDICT cluster=104 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: position_exit_monitor.py L420-423 在 DB 查询异常时 fail-safe 返回 True(假设已退出)，若首次平仓命令因网络问题未实际执行但 DB 写入异常，后续 scan 因 fail-safe=True 永远跳过该仓位重试，导致仓位滞留无法退出

### 议题 #105 [独有 1/3] `app/risk/position_exit_monitor.py`:566-575 — 组合回撤计算对取价异常逐仓吞掉，可能低估真实回撤

环节: s7_risk_execution · 类别: error_swallowing · 自报最高 severity: S3
- **qwen3.8-max** (S3, error_swallowing, app/risk/position_exit_monitor.py:566-575): 组合回撤计算对取价异常逐仓吞掉，可能低估真实回撤
  - 证据引用: `for p in positions:
            try:
                price = get_price_fn(p.symbol)
            except Exception as _silent_e:
                logger.critical(f"[退出监控·P0] 组合PnL计算异常，回撤检测可能失效: {_silent_e}", exc_info=True)`
  - 影响声称: 任一持仓取价异常时，该持仓盈亏不会计入 total_pnl，但扫描继续。若异常发生在亏损较大的持仓上，组合回撤会被低估，REDUCE_50/FLAT_ALL 等组合风控可能漏触发。
  - 建议: 取价失败时对该持仓采用保守估值或使整轮回撤检查失败并告警；至少应累计失败仓位并在失败比例过高时触发更高级别风控。

<!-- DEB-VERDICT cluster=105 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: position_exit_monitor.py L566-575 循环内 try/except 捕获单仓 get_price_fn 异常后仅 log.critical 但 continue，亏损仓位 PnL 不计入 total_pnl(L571)，导致组合回撤被系统性低估，REDUCE_50/FLAT_ALL 熔断可能延迟或失效

### 议题 #106 [独有 1/3] `app/risk/position_exit_monitor.py`:807-807 — REDUCE_50 对 1 股持仓会减成 0 股，实际全平而非减半

环节: s7_risk_execution · 类别: numeric_unit · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, numeric_unit, app/risk/position_exit_monitor.py:807-807): REDUCE_50 对 1 股持仓会减成 0 股，实际全平而非减半
  - 证据引用: `                        close_qty = max(1, getattr(p, 'size', 100) // 2)`
  - 影响声称: 当 size=1 时，//2 为 0，max(1,0) 变成关闭 1 股；与 close_ratio=0.5 的语义不一致，小仓位会被过度减仓甚至直接清仓。
  - 建议: 明确最小减仓单位：若无法执行 50% 则跳过、仅告警，或按规则定义允许全平；不要让 close_qty 与 close_ratio 语义冲突。

<!-- DEB-VERDICT cluster=106 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: position_exit_monitor.py L807 close_qty = max(1, size//2)，当 size=1 时 1//2=0 → max(1,0)=1 即全平，与 REDUCE_50 语义不符但数学上不可避免(最小交易单位=1股)，注释称"减仓 50%"与实际行为不一致

### 议题 #103 [独有 1/3] `app/risk/capital_safety.py`:858-869 — 资本安全单例创建后忽略后续传入的 trade_env/initial_capital

环节: s7_risk_execution · 类别: dataflow_inconsistency · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, dataflow_inconsistency, app/risk/capital_safety.py:858-869): 资本安全单例创建后忽略后续传入的 trade_env/initial_capital
  - 证据引用: `    if _capital_instance is None:
        # 2026-07-03: 跟随 AUTO_TRADE_ACCOUNT (TRADE_ENV 仅控制 DB 路由, 不反映 broker 账户)
        if trade_env is None:
            _auto_acc = os.getenv("AUTO_TRADE_ACCOUNT", "sim").strip().lower()
            trade_env = 1 if _auto_acc == "sim" else 0
        
        _cap`
  - 影响声称: 若首个调用以模拟环境创建单例，后续实盘调用仍会拿到模拟规则实例；实盘现金检查可能被放宽，或模拟盘误用实盘拦截逻辑。
  - 建议: 单例获取时校验 trade_env/initial_capital 是否一致；不一致时重建、报错或显式重置，而不是静默返回旧实例。

<!-- DEB-VERDICT cluster=103 -->
裁决: CONFIRMED | severity=2 | 置信=low | 理由: capital_safety.py L858-869 get_capital_safety_system() 单例创建后忽略后续 trade_env/initial_capital 参数，代码语义成立；但运行中实际影响有限——单例通常在启动时创建一次，全局 AUTO_TRADE_ACCOUNT 守卫一致性，需运行态确认是否存在跨环境重入调用

### 议题 #107 [独有 1/3] `app/risk/position_exit_monitor.py`:923-924 — scan_positions 后段加载 playbook 护栏未加异常保护

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/risk/position_exit_monitor.py:923-924): scan_positions 后段加载 playbook 护栏未加异常保护
  - 证据引用: `        from app.playbook.pipeline import load_playbook_open_symbols, playbook_open_check as _pb_check_em
        _pb_open_syms = load_playbook_open_symbols()`
  - 影响声称: 同一文件中前段对 playbook 导入失败做了安全降级，但此处循环外导入和加载没有 try/except；一旦模块缺失或加载异常，整个 scan_positions 会抛出，导致所有持仓退出监控中断。
  - 建议: 对该导入/加载加保护，失败时按安全侧跳过 playbook 检查或按既定降级策略处理，不应让单个护栏异常杀死整个退出扫描。

<!-- DEB-VERDICT cluster=107 -->
裁决: CONFIRMED | severity=3 | 置信=medium | 理由: position_exit_monitor.py L923-924 从 playbook.pipeline 导入并调用 load_playbook_open_symbols() 无 try/except 包裹，若该模块异常整个 scan_positions 循环崩溃，所有持仓退出监控失效；下游 L926 的 per-position try/except 无法守卫此循环外导入

### 议题 #98 [独有 1/3] `app/execution/broker_adapter.py`:1046-1054 — 期权下单断连时无条件返回 simulated_fill

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, logic_anomaly, app/execution/broker_adapter.py:1046-1054): 期权下单断连时无条件返回 simulated_fill
  - 证据引用: `        if not self.is_connected():
            logger.info(
                f"[期权·模拟] {direction} {option_code} qty={qty} price={price}"
            )
            return {
                "order_id": f"OPT_{option_code}_{direction}",
                "futu_order_id": f"SIM_OPT_{option_code[:20]}",
 `
  - 影响声称: submit_option_order 未检查 trade_env/AUTO_TRADE_ACCOUNT；若在真实环境调用且 broker 断连，会返回模拟成交，导致下游误以为期权仓位已建立或平掉。
  - 建议: 真实环境断连应抛错或返回失败；仅明确模拟环境允许 simulated_fill。

<!-- DEB-VERDICT cluster=98 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: broker_adapter.py L1046-1054 submit_option_order 在断连时无条件返回 simulated_fill，未像 submit_order(L418-423) 那样检查 AUTO_TRADE_ACCOUNT 区分模拟/实盘，实盘模式下期权订单被虚假认为已成交，策略持有不存在的持仓

### 议题 #108 [独有 1/3] `app/risk/position_exit_monitor.py`:2183-2198 — ATR 刷新失败后无限期返回过期缓存

环节: s7_risk_execution · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max** (S3, logic_anomaly, app/risk/position_exit_monitor.py:2183-2198): ATR 刷新失败后无限期返回过期缓存
  - 证据引用: `if cached and (now - cached.get('ts', 0)) < 300:
            return cached.get('value', 0.0)
        if daily_atr <= 0:
            if cached:
                return cached.get('value', 0.0)
            return 0.0`
  - 影响声称: 缓存 TTL 只在成功路径生效；一旦日 K ATR 计算失败，即使缓存已过期也会继续返回旧 ATR。止损/追踪线可能长期基于过期波动率计算，导致风控阈值过松或过紧。
  - 建议: 为失败路径设置最大缓存年龄；超过最大年龄返回 0/触发保守处理，并记录数据源异常，而不是无限期复用旧 ATR。

<!-- DEB-VERDICT cluster=108 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: position_exit_monitor.py L2195-2197 当 daily_atr<=0 且有缓存时直接返回缓存值，不检查缓存时间戳(L2183-2186 的 300s TTL 仅在外层判断)；若 ATR 持续获取失败，过期缓存被无限期使用，止损位可能偏离实际波动

### 议题 #110 [独有 1/3] `app/push/dedup.py`:26-30 — 去重缓存在发送结果未知前就记录 key，失败重试会被误判为重复

环节: s8_monitoring_surface · 类别: logic_anomaly · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, logic_anomaly, app/push/dedup.py:26-30): 去重缓存在发送结果未知前就记录 key，失败重试会被误判为重复
  - 证据引用: `if ts is not None and (now - ts) < self.ttl: return False | self._cache[key] = now`
  - 影响声称: should_send 在调用方真正发送成功前就写入时间戳；若首次推送失败并在 TTL 内重试，同 key 会被跳过，导致告警丢失。
  - 建议: 将去重记录改为发送成功后确认，或提供 mark_sent/rollback 接口；失败重试应能绕过去重或缩短失败窗口。

<!-- DEB-VERDICT cluster=110 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: dedup.py:28-30 should_send 在返回 True(可发送)时立即 line 30 记录 key, 实际发送失败后重试因 key 已存在被误判为重复而丢弃

### 议题 #115 [独有 1/3] `app/web_dashboard/order/executor.py`:34-34 — 延迟保护挂单注册表是无锁全局 dict，多线程并发读写

环节: s8_monitoring_surface · 类别: race_condition · 自报最高 severity: S3
- **qwen3.8-max** (S3, race_condition, app/web_dashboard/order/executor.py:34-34): 延迟保护挂单注册表是无锁全局 dict，多线程并发读写
  - 证据引用: `_pending_protections: Dict[str, dict] = {}`
  - 影响声称: submit_order、threading.Timer、_protection_monitor_loop 都会读写/删除 _pending_protections，但没有任何锁；并发修改可能导致 RuntimeError、丢失注册或重复处理，进而影响 SL/TP 保护挂单。
  - 建议: 为 _pending_protections 增加 threading.Lock 或使用线程安全队列，所有读写/弹出都在锁内进行。

<!-- DEB-VERDICT cluster=115 -->
裁决: CONFIRMED | severity=2 | 置信=low | 理由: executor.py:34 _pending_protections 为无锁全局 dict, 主线程/Timer/监控线程并发读写无同步, CPython GIL 使单操作原子但 pop 与赋值间存在理论竞态

### 议题 #112 [独有 1/3] `app/web_dashboard/data_service.py`:137-144 — 持仓变化推送只比较数量和浮盈，忽略同数量持仓的 qty/成本变化

环节: s8_monitoring_surface · 类别: dataflow_inconsistency · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, dataflow_inconsistency, app/web_dashboard/data_service.py:137-144): 持仓变化推送只比较数量和浮盈，忽略同数量持仓的 qty/成本变化
  - 证据引用: `if len(old or []) != len(new or []): return True | if abs(safe_float(n.get("unrealized_pl", 0)) - safe_float(o.get("unrealized_pl", 0))) > 1: return True | return False`
  - 影响声称: 当持仓数量不变但某个持仓的 qty、成本、方向发生变化且浮盈变化不超过 $1 时，不会推送 positions_update；面板可能继续显示旧持仓状态。
  - 建议: 对 positions_raw 的关键字段做结构化比较：code、qty、direction、cost_price、unrealized_pl 等，任一变化即推送。

<!-- DEB-VERDICT cluster=112 -->
裁决: CONFIRMED | severity=2 | 置信=medium | 理由: data_service.py:137-144 _has_meaningful_change 仅比较持仓数量和 unrealized_pl 差值>$1, 同数量下 qty 或 avg_cost 变化若 unrealized_pl 变动<$1 则推送被静默过滤

### 议题 #118 [独有 1/3] `app/web_dashboard/order/executor.py`:768-781 — 今日订单 filled_qty 用 qty 而非 dealt_qty，部分成交口径错误

环节: s8_monitoring_surface · 类别: numeric_unit · 自报最高 severity: S2
- **qwen3.8-max-0902** (S2, numeric_unit, app/web_dashboard/order/executor.py:768-781): 今日订单 filled_qty 用 qty 而非 dealt_qty，部分成交口径错误
  - 证据引用: `_is_filled = "FILLED" in _st | "filled_qty": int(row.get("qty", 0)) if _is_filled else 0,`
  - 影响声称: 只要状态包含 FILLED 就把全部 qty 记为 filled_qty，忽略 dealt_qty/部分成交；面板成交数量与真实成交不一致，影响人工判断和指标口径。
  - 建议: filled_qty 应优先取 dealt_qty；fallback 时也要明确区分全部成交、部分成交与未成交。

<!-- DEB-VERDICT cluster=118 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: executor.py:780 filled_qty 取 row.get("qty") 而非 dealt_qty, 对于 FILLED 状态订单若存在部分成交场景则展示口径不准确

### 议题 #120 [独有 1/3] `app/web_dashboard/order/executor.py`:1318-1334 — 开仓单部分成交即视为完成并移出保护队列，剩余成交部分失去 SL/TP 保护

环节: s8_monitoring_surface · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, state_machine, app/web_dashboard/order/executor.py:1318-1334): 开仓单部分成交即视为完成并移出保护队列，剩余成交部分失去 SL/TP 保护
  - 证据引用: `if "FILLED" in status.upper() or dealt_qty > 0: | if dealt_qty > 0: order.quantity = dealt_qty | place_protective_stop(service, order) | _pending_protections.pop(order_id, None)`
  - 影响声称: dealt_qty>0 可能只是部分成交；此时按部分数量挂保护单并从等待队列移除，后续剩余开仓单成交时不会补挂保护单，造成新增仓位无止损/止盈。
  - 建议: 仅在订单终态且全部成交时移除保护队列；部分成交应按已成交量动态挂保护并继续跟踪剩余未成交部分，或撤销剩余开仓单。

<!-- DEB-VERDICT cluster=120 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:1318-1333 部分成交(dealt_qty>0)即触发保护并以 pop 移出队列, 后续剩余数量成交时无机制再挂 SL/TP 致剩余仓位裸奔

### 议题 #122 [独有 1/3] `app/web_dashboard/order/executor.py`:1568-1583 — 取消订单组时忽略撤单失败并直接清空 DB 中的 SL/TP 订单 ID

环节: s8_monitoring_surface · 类别: state_machine · 自报最高 severity: S4
- **qwen3.8-max-0902** (S4, state_machine, app/web_dashboard/order/executor.py:1568-1583): 取消订单组时忽略撤单失败并直接清空 DB 中的 SL/TP 订单 ID
  - 证据引用: `service._cancel_broker_order(sl_id) | service._cancel_broker_order(tp_id) | "UPDATE open_positions SET sl_broker_order_id=NULL, tp_broker_order_id=NULL WHERE symbol=?"`
  - 影响声称: 若 broker 撤单失败（网络、已成交、部分成交等），本地仍清除 SL/TP 订单 ID，形成孤儿保护单；后续修改止损/平仓无法追踪并撤销，可能导致双倍平仓或裸空头。
  - 建议: 只有确认撤单成功或订单已终态时才清除 DB 字段；失败时保留订单 ID 并告警/重试，必要时进入人工核对。

<!-- DEB-VERDICT cluster=122 -->
裁决: CONFIRMED | severity=4 | 置信=high | 理由: executor.py:1568-1583 cancel_order_group 忽略 _cancel_broker_order 返回值(line 1570/1574)并无条件 line 1578-1583 清除 DB 映射, 撤单失败时 DB 丢失对活跃 broker 挂单的追踪

### 议题 #128 [独有 1/3] `app/review/daily_review.py`:464-466 — 综合评分胜率使用 realized_pnl 毛口径，与报告声明的净口径不一致

环节: s9_review_backtest · 类别: numeric_unit · 自报最高 severity: S3
- **qwen3.8-max** (S3, numeric_unit, app/review/daily_review.py:464-466): 综合评分胜率使用 realized_pnl 毛口径，与报告声明的净口径不一致
  - 证据引用: `winning = [t for t in trades if (t.get('realized_pnl') or 0) > 0]
closed = [t for t in trades if t.get('exit_price') and t.get('exit_price') > 0]
win_rate = len(winning) / len(closed) if closed else 0`
  - 影响声称: 评分中的胜率可能被毛盈利但净亏损的交易抬高；与 summary 中 pnl_net/COALESCE(pnl_net, realized_pnl) 净口径不一致，导致评分和对外报告互相矛盾。
  - 建议: 评分胜率统一使用净盈亏：COALESCE(pnl_net, realized_pnl) > 0，并保持与 summary、Obsidian 推送同口径。

<!-- DEB-VERDICT cluster=128 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/review/daily_review.py:464-466 综合评分胜率用 realized_pnl 毛口径判赢，同文件第822行注释明确 "pnl_net 为唯一对外口径" 并用 COALESCE(pnl_net,realized_pnl) 计算净额，两处口径矛盾，评分可能将毛赢净亏的交易计为胜场。

### 议题 #129 [独有 1/3] `app/review/daily_review.py`:776-784 — plan_vs_actual 仅按 symbol 匹配计划与实际交易，忽略 direction

环节: s9_review_backtest · 类别: logic_anomaly · 自报最高 severity: S3
- **qwen3.8-max-0902** (S3, logic_anomaly, app/review/daily_review.py:776-784): plan_vs_actual 仅按 symbol 匹配计划与实际交易，忽略 direction
  - 证据引用: `            trades = self._query(
                "SELECT symbol FROM trade_records WHERE trade_date = ? "
                "AND source_type IN ('PLAYBOOK_SIM','PLAYBOOK_LIVE') AND trade_mode != 'live'",
                (date,)
            )
            actual_symbols = set(t.get('symbol', '') for t `
  - 影响声称: playbook_instructions 明确有 direction，但实际成交只取 symbol 后做集合交集；同标的反方向成交也会被判为已执行计划，导致执行率、错过机会和计划外交易统计错误。
  - 建议: 按 (symbol, direction) 对齐 playbook_instructions 与 trade_records，再计算 executed/missed/unplanned。

<!-- DEB-VERDICT cluster=129 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/review/daily_review.py:776-784 plan_vs_actual 仅 SELECT symbol 后做集合交集匹配，完全忽略 direction；若计划 BUY AAPL 而实际 SELL AAPL 会被计为已执行，方向错误无法检出。

### 议题 #130 [独有 1/3] `app/review/daily_review.py`:920-920 — capital_state 按 date(last_update) 查询，可能因时区归一化错配 ET 交易日

环节: s9_review_backtest · 类别: time_boundary · 自报最高 severity: S3
- **qwen3.8-max** (S3, time_boundary, app/review/daily_review.py:920-920): capital_state 按 date(last_update) 查询，可能因时区归一化错配 ET 交易日
  - 证据引用: `FROM capital_state WHERE date(last_update) = ? ORDER BY last_update DESC LIMIT 1`
  - 影响声称: SQLite date() 会将带时区偏移的时间归一化，若 last_update 存储 UTC/带偏移时间，ET 盘后或夜间查询可能取错日期或取不到资金快照，进而影响风控评分、回撤展示和复盘结论。
  - 建议: 与全仓 ET 墙钟口径一致，使用 substr(last_update,1,10)=? 或显式存储 ET 日期列，避免 date() 隐式时区转换。

<!-- DEB-VERDICT cluster=130 -->
裁决: CONFIRMED | severity=2 | 置信=high | 理由: app/review/daily_review.py:920 用 date(last_update)=? 匹配 ET 交易日，但 capital_safety.py:174 用 datetime.now() 写入 CST 裸时间戳（daily_alert_summary.py:278 确认生产服务器为 CST），美股盘中 12:00-16:00 ET 对应 CST 次日 00:00-04:00，date() 提取的 CST 日期比 ET 交易日多一天，资金状态查询系统性返回空。

