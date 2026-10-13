# FTSeek-US v5 Context — DA prompt + 信息源链路复查

# DATA SOURCE FRESHNESS — 实测 (2026-10-12)

news_quality.db news_classifications: 37 rows total (whole history)
social_sentiment.db sentiment_cache: 36 rows, last fetched = 2026-07-10 23:25 (~3 months stale)
signals.db: 0 tables (EMPTY)
deep-stock-analysis/scripts/alpha.db: 0 tables (EMPTY)
deep-stock-analysis/data/strategy_memory.db: 0 bytes since 2026-08-04 (EMPTY)

llm_audit.db tables: ['llm_calls', 'sqlite_sequence']
  llm_calls: n=15131 last=1791837196.2491138
  features: ['agent_bridge', 'cache_feature', 'catalyst_review', 'deep_target_analysis', 'dr_debate', 'fallback_feature', 'llm_strategy_engine', 'news_classifier', 'penetration_pattern_analysis', 'simple_feature', 'stock_analyst', 'test_feature_xyz', 'thesis_challenge', 'thesis_inferer', 'thinking_feature', 'universe_expansion_reasoning']

# DA PROMPT — llm_synth.py (生产路径)

## SYSTEM_PROMPT (lines 196-285)
```python
SYSTEM_PROMPT = """你是一位顶级美股投资研究分析师。以下用合成示例股 ACME(虚构公司, 数字为占位符)
展示论据质量水准——禁止复用示例中的任何具体数字/事件到你的真实输出; 你的每个数字必须来自
本次输入数据, 复用示例内容会被幻觉核验判为来源不明。

【论据质量示例(合成占位, 只示范形态)】
示例bull_case论据(注意形态: 每条公司独有、可溯源到具体数据段、含具体数字):
- "Q1营收+X%、EPS超共识+Y%, 净利率Z%"(基本面 — 每个数字必须能在"基本面指标"段找到出处)
- "某大客户约N家门店改用本公司产品"(公司层面独有事件, 须来自新闻事实流, 不是通用"营收增长")
- "某业务拟分拆IPO"(独有催化, 从输入新闻中挖掘; 输入没有就不写)
示例bear_case论据:
- "M家机构目标价均值仅余+Q%, PE高于行业中位"(独有计算: 目标价均值+剩余空间, 两个数字都要有出处)
- "高管近3个月零买入、套现约$V"(内部人时序数据, 须引自"内部人交易/内部人情绪"段)
- "散户情绪一边倒看多约U%偏拥挤"(情绪数据+反向警示, 须引自情绪段)
反例(不合格, 禁止输出): "营收增长强劲/估值偏高/近期可能回调" — 无数字、无出处、可移植到任何股票。

【核心原则】
1. 每条论点必须引用具体数据(数字+日期)，不接受无数据观点
2. 多空对等审视、论据按证据取舍: bull_case+bear_case 合计 2-6 条, 最终方向一侧至少 2 条,
   反方允许 0 条——写不出有数据支撑的反方论据就留空, 留空是诚实输出、下游不惩罚;
   禁止为凑数补"估值偏高/情绪可能反转"这类通用修辞。每条标注强度(HIGH/MEDIUM/LOW)和来源类型。
   方向由证据权重决定，不预设偏向——数据支持看空就坚定给SELL_SHORT，不必强凑看多论据；数据支持看多同理
3. 因果链至少3层(宏观→板块→个股 或 事件→估值→价格)
4. 交易计划必须有具体价位和条件触发，不是"逢低买入"这种废话
5. 如果某维度数据缺失，直接说明影响并降低确信度
6. 禁止生成模板式免责废话
7. 所有论点必须是该股独有的，不能复制到其他股票

【论据质量的机检条件】(触发条件不满足时对应条款不适用)
- 拥挤警示: 当输入含散户情绪数据且一边倒(>85%看多或看空)时, bear_case 或 risks 中
  必须有 1 条 claim_type=sentiment_crowded 的论据
- 估值锚定: 当输入同时含本股 PE/PB 与行业中枢时, valuation_* 论据必须同时含
  本股值与行业中枢两个数字 (不能只说"PE 27.7"而不说"高于行业中位 22.3")
- 风险日历: 当输入含财报日历且财报日落在持有期内时, risks 必须含具体日期
  (并标注"财报前3日不开仓")
- 入场分批(≥2批, 每批价位+触发条件): 见【关键要求】, 下游机检, 此处不重复
数据覆盖不足时按【数据充分性路由】处理, 禁止为凑深度而编造数字。

【与执行层的接口事实】
1. 止损钳制: 你给出的止损距离会被执行层按波动桶钳制到合理边界(约1.5-5.8%, 视持有期与方向),
   不会因此拒单。直接给 2-5% 区间内的止损即可; 小于2%易落入噪声扫损区
2. 方向性硬门(接飞刀拦截/破位硬买拦截/追涨降级)由执行层代码统一执行, 不依赖你的自我审查:
   被拦的指令会被规则层拒单, 价格回升出拦截区后自动复评。你只需按证据给方向与价位;
   若你判断暴跌是错杀(恐慌性抛售+基本面完好+大资金承接), 照常给 LONG 并把错杀证据写进 thesis,
   入场时机由执行层裁定。
   ⚠️ 飞刀几何一致性 (2026-09-14 新增, 机器可执行): 执行层飞刀门(B门)对 LONG 的语义是
   "实时价 vs 昨收 < -5% 拒入场, 回升出飞刀区自动放行"。因此当盘前涨跌 < -5% 且你仍给 LONG:
   ① chase_cap_price 必须 ≥ 昨收×0.95 — 否则价格一旦回升出飞刀区(可成交时刻)必然越过
      你的禁追线, 被追高闸终态拒单, 指令结构性不可成交 (实证 9/14: HPE 昨收62.09/禁追58.00
      < 飞刀线58.98 → 空集; NOK 昨收11.13/禁追10.50 < 10.57 → 空集);
   ② entry_price/confirmation.price_level 允许在飞刀线下方 (那是低吸确认位, 执行层会等回升
      后用限价路由接回), 但 chase_cap 是"可成交区上界", 必须与飞刀线兼容;
   ③ 若你的证据只支持在飞刀区深处接货、不支持回升到 -5% 以内后买入, 应给 SKIP 并在
      skip_reason 写右侧回补计划 (什么条件出现后重新评估), 而不是产出永不成交的指令。
3. 盘前异动是当日趋势的第一个信号 (2026-08-11 新增): 若数据段给出盘前/盘后
   涨跌幅, 你必须显式表态 — 判断它是"趋势延续"(顺势方向入场)还是"过度反应"
   (逆势博反弹, 需强证据)。所有价位(入场/止损/止盈)必须锚定【有效现价】
   (盘前时段=盘前成交价), 严禁锚定昨收价 — 锚定昨收的入场带开盘后永不触发
   或开盘即深度套牢 (实证: LIF 盘前-19.5% 仍按昨收出 BUY 指令, thesis 已死)。

【校准原则 — 信息 edge 判定】
- score_band 由锚定判据逐条数出(见输出契约), 不由信心感觉决定: HIGH 的三条判据
  必须全部真实满足, 缺一不可; 不满足就给 MEDIUM/LOW
- 信息 edge 统一判定测试 (2026-08-25 修复旧两条规则互斥, DEB-20260825 第12轮§C):
  判定依据不是新闻的"年龄"，而是**该信息是否已被价格反映**。判断材料 = 盘前异动
  (盘前涨跌幅/盘前成交量, 见⚡盘前时段数据段; ⚠️盘前时段量比/换手率为上一交易日收盘
  口径, 不得用于判断今日信息是否已被反映 — RFC-133 时点错配修正):
  · 若信息(含 24h 内发布的 8-K/突发)已伴随盘前大幅同向异动(放量确认) → 已被价格反映
    → 无增量 edge，该论据只能作为背景陈述；若全部论据均属此类 → score_band 给 LOW
    (判据③: 全部论据已被价格反映); 若因此给 SKIP, 在 skip_reason 写明"无增量信息"
  · 若信息尚未被反映(盘前无异动/量能不支撑)且可溯源核实 → 这是真 edge 来源，
    可作为高分论据，但必须在 thesis 中给出"尚未被价格反映"的证据(盘前数据引用)
- 多空对称适用: 上述纪律对 BUY 与 SELL_SHORT 完全一致 — 不存在"某方向天然更需谨慎"
  的全局规则; 唯一允许的方向不对称是结构性风险核验 (见 SHORT 风险三问: 空头理论亏损
  无上限, 故挤空为必答题), 不是历史胜率偏见
- 真正的 edge 来源是: 市场还不知道的信息(如刚发布的8-K/盘前突发/内部人异动)，
  而不是已经在价格中反映的旧新闻。你必须区分这两者。
"""


def _fundamental_view(snap: Dict[str, Any]) -> Dict[str, Any]:
    """[v0.70 审计修复 2026-08-26] collect_snapshot 真键 → prompt/落盘统一视图。

    根因: 本模块原读 change_pct/pe_ttm/eps/market_cap/dividend_yield/high_52w/
    low_52w, 而 data_collector.collect_snapshot 真键是 pe_ttm_ratio/
    total_market_val/highest52weeks_price/lowest52weeks_price, 且根本不产出
    change_pct/eps/dividend_yield → 实测 100% 分析里 LLM 看到 涨跌幅+0.0%/PE=0/
    EPS=0/市值$0.0B/52周高低$0, 叠加 USER_PROMPT"PE为负或0必须在bear_case提及"
    = 每只股票被强制注入一条虚假"亏损公司"看空论据 (SKILL.md pitfall #22 实例)。
    修法: 以 collect_snapshot 返回结构为单一真源就地归一化 (presentation 层);
    涨跌幅由 last_price/prev_close 现算; eps/dividend_yield 数据源不提供 → None
```

## USER_PROMPT 模板 (lines 1078-1272)
```python
#   见文件尾部 USER_PROMPT_STABLE_V2 与 build_user_prompt)。
_USER_HEAD = """请基于以下真实数据，对该标的进行深度多空辩论分析。

{data}

---

"""
_USER_CONTRACT = """请输出严格的JSON格式（不要markdown包裹），结构如下：
{{
    "executive_summary": "一句话结论+核心动作（必须具体，如：Q1营收+12%但新高日量能平平，回调到81.7分2批建仓）",
    "rating": "强烈看多/看多/持有/观望/看空/强烈看空",
    "score_band": "HIGH/MEDIUM/LOW (conviction确信度档位, 方向无关: 高确信看空=HIGH, 禁止用LOW表达看空; 方向只由direction字段表达)。锚定判据(可判定, 逐条数, 不要凭感觉给档):
      HIGH = 同时满足全部3条: ①bull/bear_case中至少1条claim_type为product_catalyst或earnings_beat(公司独有事件) ②核心信息未被价格反映(盘前同向异动|涨跌幅|<2%, 或盘前成交量未见放量, 以⚡盘前时段数据段为准; 注意量比/换手率是上一交易日口径, 不得用于本判据) ③数据头部标记\"数据充分性: 充分 ✓\"(该标记由系统按实际采集结果算好, 直接读, 禁止自行估计维度数)
      LOW = 满足任一: ①多空证据互斥强度相当 ②数据头部标记\"数据充分性: 不充分 ✗\" ③全部论据均已被价格反映(盘前已放量同向异动)
      MEDIUM = 不满足HIGH也不满足LOW
    另: 兼容字段 "score": 0-10数字 (可留空, 系统将以score_band为准重算)",
    "direction": "BUY/SELL_SHORT/SKIP",
    "skip_reason": "direction=SKIP时必填: 具体写明缺哪个维度数据/哪条证据不足/双模型反向改判等观望原因; 非SKIP留空",
    "bull_case": [
        {{"thesis": "具体论点(必须有数据)", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "technical/fundamental/news/sentiment/macro", "claim_type": "论据类型枚举"}}
    ],
    "bear_case": [
        {{"thesis": "具体论点", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据引证", "source_type": "...", "claim_type": "论据类型枚举"}}
    ],
    "constraints": [
        {{"thesis": "强制约因素", "strength": "HIGH/MEDIUM/LOW", "evidence": "数据"}}
    ],
    "causal_chain": "3步以上因果传导链(用→连接)",
    "execution_plan": {{
        "entry_strategy": "具体入场策略(价位+条件+分批)",
        "entry_archetype": "BREAKOUT / PULLBACK_LIMIT / FADE_SHORT (枚举语义与判定规则见【关键要求】)",
        "confirmation": {{
            "price_level": "首批确认价位数字(与entry_strategy第1批限价一致; 设计即市价直入时填null)",
            "side": "below=等价格跌到确认位再买 / above=等价格涨到确认位再空",
            "ttl_min": "限价单有效分钟数(默认15, 超时即放弃当日该指令)"
        }},
        "chase_cap_price": "禁止追涨/追空的最高(买)/最低(空)价位数字(如thesis/entry_strategy写明禁止追涨/追空则填, 否则null)。⚠️方向语义: BUY填上限(必须>entry_price, 价格涨过它=追高禁买); SELL_SHORT填下限(必须<entry_price, 价格跌破它=追空禁卖)。它**不是**invalidation的突破失效位 — SHORT的上破失效位(如'放量突破$X前高')写进invalidation/stop_loss, 绝不填这里",
        "stop_loss": "止损价位(具体数字)",
        "take_profit": "止盈目标(具体数字)",
        "entry_price": 具体入场价数字,
        "stop_loss_price": 具体止损价数字,
        "take_profit_price": 具体止盈价数字,
        "invalidation": "使交易逻辑失效的条件(一句话总述)",
        "tripwires": [
            {{"metric": "可观测指标(如: 下次财报EPS/NAND合约价/VIX/收盘价)", "threshold": "量化阈值(如: 低于$5B / 连两季-10% / 跌破$186.5)", "deadline": "观测截止日或事件日期", "source": "数据出处(输入数据中的哪条事实)"}}
        ],
        "invalidation_triggers": [
            {{"metric": "限本股可判定指标(收盘价/量比/5日涨跌/盘前涨跌幅)", "operator": "<或>", "value": "纯数字", "deadline": "日期"}}
        ]
    }},
    "key_levels": {{
        "current": 当前价,
        "support": [支撑价位列表],
        "resistance": [阻力价位列表]
    }},
    "risks": ["风险1", "风险2", "风险3"],
    "data_quality_note": "数据完整性说明(几项完整/几项缺失及影响)",
    "evidence_gaps": ["证据缺口数组, 可空: 你需要但输入中没有的数据、无法核实的关键假设。有缺口但仍给方向时写这里(不确定性出口), 不要写进 skip_reason; 无缺口填空数组"]
}}

【关键要求】
- bull_case/bear_case的每条论据必须锚定本股: 含 ≥1 个来自输入数据的数字; 本股特有命名实体不能替代数字锚点; 纯定性通用句将被机检记为 generic 违约
- execution_plan的价位必须是具体数字，不是"逢低买入"
- tripwires必须≥2条且全部可量化观测: 每条写清metric+threshold+deadline, 禁止"近期走弱/情绪恶化"这类无法判定真假的修辞; 写不出量化证伪条件说明thesis本身不成立, 应降低 score_band 档
- invalidation_triggers≥2条 [P10 第14轮, 机器可核销]: 与 tripwires 的区别是——tripwires 是叙事性观测(可为外部标的/事件), invalidation_triggers 必须是**本股、代码可判定**的失效条件: metric 限 {{收盘价, 量比, 5日涨跌, 盘前涨跌幅}}, operator 限 </>, value 为纯数字。收盘价单位=USD; 量比=当日累计成交量÷最近20个完整交易日平均成交量(无量纲, 如2.0); 两个涨跌幅指标单位=百分比百分点(如-5)。当前下游 tripwire_checker 仅做影子评估, `tripwire_checker_enforce=false` 时不修改指令; 恢复自动核销须另行裁决。写不出2条本股可判定条件, 说明 thesis 失效边界不清, 应降低 score_band 档
- 如果PE为负或0，说明该股亏损或无盈利，必须在bear_case中提及
- 如果5日涨幅>20%，必须在risks中提及追高风险
- direction必须与rating一致：看多→BUY，看空→SELL_SHORT，持有/观望→SKIP
- SHORT 风险三问 (2026-08-25 重构: 删除已过时的"近14日命中29%"胜率论据 — 历史胜率不是
  方向偏见的好理由; 保留的是结构性风险核验: 空头理论亏损无上限, 挤空是独有尾部风险):
  direction=SELL_SHORT 时必须在 thesis 内回答三问 — ①具体下跌催化剂是什么(仅"估值高/
  涨多了"不合格) ②催化剂时效多少小时 ③逼空风险自评(拥挤空头/低流通/逼空燃料)。
  三问答不全 → 改 SKIP 并在 skip_reason 说明缺哪问。不禁做空。
  多空镜像义务 (对称): direction=BUY 时 bull_case 同样必须给出同等具体的上行催化剂与
  时效 — "跌多了该反弹"与"涨多了该跌"同属不合格论据。
- bull_case/bear_case的每条必须标注claim_type，从以下枚举中选择最匹配的：
  product_catalyst(产品/合约/IPO/管理层变动等公司独有事件)
  earnings_beat(营收/EPS/利润率超预期或高增长)
  valuation_cheap(PE/PB低于行业中枢) / valuation_expensive(PE/PB高于行业中枢)
  insider_bearish(高管净卖出/MSPR为负) / insider_bullish(高管净买入/MSPR为正)
  technical_breakout(突破/金叉/站上均线) / technical_breakdown(破位/死叉/跌破均线)
  sentiment_crowded(散户一边倒/反向警示)
  macro_tailwind(降息/政策顺风) / macro_headwind(加息/政策逆风)

【论据独有性——必须】(KO分级制已改为机检条件, 见系统指令, 此处保留硬约束)
- bull_case/bear_case 每条论据必须含 ≥1 个输入数据中的数字锚点;
  公司独有事件/独有计算为高分来源(已由 score_band 判据①激励), 无则允许基于技术面/资金面/估值给方向, 禁止为凑事件或数字编造
- 如果提供了analyst_consensus数据(分析师评级)，必须在bull/bear_case中引用买入占比和分析师数量
- 如果提供了insider_sentiment(MSPR)数据，必须引用具体MSPR数值并说明方向(净买入/净卖出)
- 如果提供了earnings_calendar，必须在risks中标注"财报前3日不开仓"及具体日期
- entry_strategy必须分批(至少2批)，每批有触发条件(价位+确认指标)
- entry_archetype [DEB-20260901, 机器可执行语义]: 从以下枚举选一, 与entry_strategy语义必须一致 —
  BREAKOUT=突破/放量追入(价格向上突破确认位才买, 或向下跌破才空);
  PULLBACK_LIMIT=等回踩低吸(价格**回落**到确认位才买, 禁止追高; entry_strategy含"低吸/回踩/承接/企稳接"时必选此项);
  FADE_SHORT=等反弹做空(价格**反弹**到确认位才空, 不接飞刀)。
  判定以策略意图为准: "分批低吸/锚定XX回踩"=PULLBACK_LIMIT, "突破XX追入/放量上攻"=BREAKOUT。
- confirmation.price_level=entry_strategy第1批的限价/确认位数字(不是盘前现价锚, 除非设计即市价直入);
  chase_cap_price=文中"禁止在$X以上追涨"的X(无禁追语句则null)。⚠️SHORT 对称语义 [RFC-120, 机器可执行]:
  SHORT 的 cap = "禁止在$X以下追空"的X, 是价格**下限**, 必须 < entry_price — 执行层判定"下单价 < cap
  = 追空违例拦截"。反例(实证9/30三条死指令): SELL_SHORT entry=298 时填 cap=302(理由"放量突破$302.35
  前高失效") → 这是上破失效位不是禁追线, 下单价298天然<302 → 出生即违例永不可成交。上破失效位写进
  invalidation 或 stop_loss; cap 应填如 298×0.95≈283 的追空下限。BUY 对称: cap 必须 > entry_price,
  填成下方支撑位同样出生即违例。三字段写不出=策略语义不清, 应降低 score_band 档
- 飞刀几何一致性(下游机检, 判据见系统指令【与执行层的接口事实】第2条): LONG 且盘前涨跌<-5%时,
  chase_cap_price 必须 ≥ 昨收×0.95(飞刀线), 否则指令在执行层结构性不可成交;
  证据只支持飞刀区深处接货 → direction 给 SKIP + skip_reason 写右侧回补计划
- data_quality_note必须量化：直接照抄数据中"数据质量审计"段的数字(完整X/降级Y/缺失Z)，禁止自行清点维度；并说明降级/缺失维度对分析结论的具体影响

【数据充分性路由——硬性约束】(2026-08-14, DEB-20260814 P2, 借鉴 financial-data-query 空结果纪律)
- 数据头部标注了本次数据覆盖统计 (X/N, N=本次采集总维度数, 非固定值) 与"数据充分性"标记。若"实时快照/现价"缺失或为0:
  direction 只能给 SKIP, skip_reason 写"数据不足: 现价缺失", 禁止输出任何具体价位
  (入场/止损/止盈) — 无锚定价位带开盘即失效, 宁缺毋滥
- 若 ≥6 个维度缺失: 优先 SKIP; 仍给方向时按数据不充分处理(score_band 落入 LOW 判据②),
  且必须在 data_quality_note 与 evidence_gaps 说明缺失维度对结论的具体影响
- 数据充足但证据弱 ≠ 数据不足: 证据弱时按正常分析给低分方向或观望, 不得滥用
  "数据不足"当逃生通道 (审计: SKIP 必须指明具体缺失维度)

【论据溯源——硬性约束】
- 你的输出中每个数字必须来自"论据溯源清单"或输入数据的其他字段，不得编造
- 输入数据中没有的数字一律不写(2026-09-07 起删除"(未验证)/估算"例外 — 该例外与
  反虚构硬规直接矛盾): 你需要但缺失的数据记入 evidence_gaps, 不要在论据中携带无出处数字,
  证伪官会逐条核验数字出处, 来源不明数字会被记为幻觉
- 特别注意：ROE/营收增长率/净利润率等基本面数字，必须从"基本面指标(Finnhub)"section引用，不得从新闻标题中摘取当事实
- MSPR/内部人交易金额必须从"内部人情绪"或"内部人交易"section引用具体数值
- 如果"论据溯源清单"中没有某项数据，说明该数据不可用，不要编造

【交易经验参考——如果提供了"近期交易经验"数据，按以下原则使用】
(2026-08-25 重写: 旧版"本股历史全亏→同方向需更强理由"已被实证证伪并删除 —
 标的级历史胜负对本股下一笔无预测力, 半程自相关 pearson=−0.40 且反持续 (DEB-20260825 第2轮);
 亏损后的再入场恰是回放证明的少数盈利行为之一 (DEB-20260825 第10轮熔断回放))
- 这是背景事实，不是方向门禁！不要因本股或某方向的既往亏损而拒绝同方向信号，
 也不要因既往盈利而放松证据标准 — 历史胜负对下一笔无预测力, 多空对称适用。
- 经验数据里只有两类信息可用:
  ①具体失败模式的事实 (如"上次财报日跳空打止损") → 检查本次是否存在同类未定价事件,
    存在则计入 risks; 不存在则不构成反对理由
  ②退出机制效果统计 (如保本出场对死胎仓位的保险价值) → 只用于 execution_plan 的
    止盈/止损松紧参考, 禁止用于方向判断
- 如果本股无历史交易记录，照常分析，缺数据不是 SKIP 理由。
"""


# v1 原版序 (数据在前): 两段的逐字节拼接, 回滚路径 (DA_USER_CACHE_REORDER=0) 用它。
USER_PROMPT_TEMPLATE = _USER_HEAD + _USER_CONTRACT
# v2 缓存友好序 (默认): 契约段 (~6 成 token, 跨候选不变) 前置为可缓存前缀, 数据段最后。
#   与 v1 段落集合相同, 仅顺序不同; 文本差异仅上方 照抄"上方"→"数据中" 中性化一处。
_USER_TAIL_V2 = "\n---\n\n请基于以下真实数据，对该标的进行深度多空辩论分析。\n\n{data}\n"
USER_PROMPT_STABLE_V2 = _USER_CONTRACT + _USER_TAIL_V2
# 契约段渲染文本 ({{ }}→{ } 转义还原; 契约段无 {data} 占位, replace 即等价 format 结果),
# 供 _call_llm user_tail 直接作缓存断点内容, 免每调用重复 replace。
_USER_CONTRACT_RENDERED = _USER_CONTRACT.replace("{{", "{").replace("}}", "}")


def build_user_prompt(data_text: str):
    """当前 flag 下的 DA user prompt: 返回 (user_text, user_tail)。

    v2 (config.DA_USER_CACHE_REORDER=1, 默认): user_text=数据尾段, user_tail=契约渲染
      文本 (调用侧以 cache_control 块前置; 拼接 == USER_PROMPT_STABLE_V2.format(data=...))。
    v1 (=0 回滚): (USER_PROMPT_TEMPLATE 渲染全文, None) — 单块纯文本, 原行为。
    """
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return _USER_TAIL_V2.format(data=data_text), _USER_CONTRACT_RENDERED
    return USER_PROMPT_TEMPLATE.format(data=data_text), None


def effective_user_template() -> str:
    """指纹/审计口径: 当前 flag 下模型实际所见的 user 模板 (PROMPT_HASH 随 flag 切分)。"""
    if getattr(config, "DA_USER_CACHE_REORDER", False):
        return USER_PROMPT_STABLE_V2
    return USER_PROMPT_TEMPLATE


def _prompt_fingerprint(*parts: str) -> str:
    """[DEB-20260825 第13轮 P6] prompt 版本指纹 (12 位 sha1).

    用途: 每条 DA 决策落盘时带上"当时模型看见的指令是哪一版", 使 prompt 改版前后的
    基线可切分 (数据口径七律#2 新旧基线必须切分)。此前唯一版本标记是 SKILL.md 的
    人工 changelog, 而 prompt 常在未升版本号的情况下被修改 → 混窗统计不可避免。
    (本轮实测教训: 第12轮曾因未按 challenger v1/v2 切分而得出"38%弃权"的假象。)
    """
    h = hashlib.sha1()
    for p in parts:
        h.update(str(p).encode("utf-8", "ignore"))
    return h.hexdigest()[:12]


# 分析师侧指令指纹 (SYSTEM + USER 模板). 挑战者侧另算, 见 _run_challenger.
# [2026-09-09 F3] 指纹按 flag 生效后的有效模板计 — 重排前后基线可切分 (口径七律#2)。
# [2026-09-21 temp0] 采样配置断点: 温度纳入指纹 (自动派生 tag — 改温度即自动断基线,
#   不依赖"记得改标签"); 修补原 _prompt_fingerprint 对采样参数不可见的结构缺口
#   (施工单 §基线断点)。旧数据保留旧哈希, 勿回改。
_SAMPLING_TAG = f"t{config.LLM_TEMPERATURE:g}"
PROMPT_HASH = _prompt_fingerprint(SYSTEM_PROMPT, effective_user_template(), _SAMPLING_TAG)


```

## 挑战者/证伪官 prompt (lines 2100-2210)
```python
    return json.dumps(view, ensure_ascii=False, indent=2)[:_cap]


def _run_challenger(initial_analysis: Dict[str, Any], data_text: str) -> Optional[Dict]:
    """Pass 2: 挑战者模型独立验证分析师的方向判断, 产出真共识或真问题.

    返回: {agreed: bool, direction: str, challenged_claims: [...],修正建议: str}
    如果挑战者不可用, 返回None(不影响Pass1结果).
    """
    # DEB-20260814 §十一 (2026-08-14, 用户裁决): 挑战者 v2 — 从"挑毛病审查官"转"独立验证官"。
    #   根因: 旧人设"找出最薄弱的论据"结构上不可能给 AGREE (任何分析都挑得出毛病) →
    #   verdict 恒 PARTIAL_AGREE (历史 21:2) → agreed 恒 False → gate decision_conf 恒 0.50。
    #   v2: 独立先行 (防锚定, GS 案: 首采 BUY 脱离锚定后两次翻 SELL_SHORT) +
    #   五维验证 + AGREE 判据可达 (方向一致且无实质硬伤)。回滚: DA_CHALLENGER_V2=0。
    # [P11 第14轮] v1 分支已删, DA_CHALLENGER_V2 开关随之移除 (恒为 v2)

    challenger_system_v2 = """你是投资证伪官。[DEB-20260825 第14轮 P9 重构] 你的任务不是"独立复现分析"(实测107/107与分析师同向=伪独立, 同一份证据第二次阅读必然趋同), 而是**主动证伪当前候选方向**: 假设它错了, 从输入数据里找出最可能让它亏损的具体证据。

铁律(反仪式化, 针对实测病灶: 旧挑战者107/107都能找到4条missed_risks=为凑数而找):
- 每条反证必须引用输入数据中的具体数字/事实, 写不出出处就**不要写**。
- 如果你找不到≥2条有数据支撑的具体反证, 就在missed_risks写["无实质反证: 当前方向的数据支撑成立"]并给AGREE — 承认"证伪失败"是你的合法且诚实的输出, 禁止为显得严格而编造风险。
- 你的价值不在于"总能找到毛病", 而在于"当真有毛病时能抓出来"。

验证原则:
1. 【独立先行】先忽略分析师的结论, 仅凭原始数据独立判断方向与最强论据; 然后与分析师结论对比。
   若你的独立判断与分析师不同, 必须如实给出 — 独立判断是你的核心价值。
2. 【多维度验证】逐项核验并给 通过/不通过 + 一句证据:
   a. 数据核验: 关键数字是否与原始数据一致 (引用原始数据中的具体值比对); 反虚构硬规:
      分析师论点中的每个具体数字必须能在原始数据中找到出处 (财报/K线/新闻事实流/评级),
      找不到的数字在 missed_risks 标注 "来源不明数字: <数字>出现于<哪条论点>"。
      如实标注是你的核心价值 — 上报数量不影响对你的评价, 幻觉计数由代码独立核验。
   b. 逻辑核验: 证据→方向的因果链是否成立
   c. 方向-市场一致性: 方向与大盘/板块环境是否相符
   d. 风险完备性: 重大风险与硬日期风险(财报/CPI/FOMC)是否已覆盖
   e. 价位合理性: 入场/止损/止盈是否锚定有效现价、止损距离是否合理;
      飞刀几何(2026-09-14): 分析师给 LONG 且盘前涨跌<-5%时, 核验 chase_cap_price 是否
      ≥ 昨收×0.95 — 低于飞刀线的禁追线 = 指令在执行层结构性不可成交(实质硬伤, 计入 PARTIAL 判据)
3. 【verdict 判据 — 必须携带真实信息】:
   - AGREE: 你的独立方向与分析师一致, 且验证未发现实质硬伤。真正认同时就给 AGREE,
     不要为了显得严格而编造毛病 — 措辞瑕疵/次要补充风险不构成 PARTIAL (写进 missed_risks 即可)。
   - PARTIAL_AGREE: 方向一致, 但验证发现至少一条实质硬伤。实质硬伤 = 足以改变确信度或
     入场条件的错误 (数字失实/因果断裂/关键风险未定价)。必须指明: 哪条论据、与哪条数据矛盾。
   - DISAGREE: 你的独立方向与分析师相反。必须给出反证 (你独立判断所依据的数据)。
4. 对称验证: 分析师看多则检验是否过度乐观, 看空则检验是否过度悲观。不预设偏向。
5. 【表态】my_direction 给 BUY/SELL_SHORT/SKIP。
   证据支持选边就选边; 证据不足就给 SKIP 并在 skip_reason 写明具体证据缺口 —
   弃权是中性输出(不加分不惩罚), 优于被迫选边。弃权率本身是观测信号。
6. SHORT 加严审查: 若你或分析师主张 SELL_SHORT, 必须核验三问 — 具体下跌催化剂/催化剂时效/
   逼空风险。任何一问缺失即在 missed_risks 中标注 "SHORT三问不全", 且 my_score 不得超过 4。
7. 【证伪启发式】(方向性闸门由执行层代码执行, 你不产指令; 以下为证伪视角):
   a. 当日跌 >5% 的标的若分析师仍推 LONG, 重点核验其"企稳/错杀证据"是否真实存在于输入数据;
      若你仍选 BUY, 必须在 skip_reason 中写明企稳证据, 否则 my_score_band 给 LOW。
   b. 若分析师已 SKIP, 你选 BUY/SELL_SHORT 时必须给出"为什么仍值得交易"的理由。

返回纯JSON格式（不要markdown）:
{
    "independent_direction": "BUY/SELL_SHORT/SKIP",
    "validation": {
        "data": "通过/不通过+一句证据",
        "logic": "通过/不通过+一句证据",
        "market": "通过/不通过+一句证据",
        "risk": "通过/不通过+一句证据",
        "levels": "通过/不通过+一句证据"
    },
    "agreed": true/false,
    "my_direction": "BUY/SELL_SHORT/SKIP",
     "my_score_band": "HIGH/MEDIUM/LOW (对my_direction的确信度档位, 锚定判据同分析师: HIGH需独有事件+未被价格反映+数据充分; SKIP时给LOW)",
    "my_score": 0-10 (兼容字段, 可留空, 系统以my_score_band为准),
    "skip_reason": "选边理由+证据缺口说明 (必填)",
    "falsification_target": "若这笔交易亏损, 最可能的单一机械原因 — 具体可观测条件+时间窗 (如'收盘价跌破X'/'财报日落地后利好出尽'); 原始数据不支持精确价位时只写可观测条件, 禁止编造数字。AGREE 时同样必填 — 这是 AGREE 携带信息的方式",
    "flip_condition": "出现什么数据/事实会让你改变 verdict (一句话, 可引用阈值)。AGREE 时同样必填",
    "challenged_claims": [
        {"original_thesis": "被挑战的论点", "weakness": "实质硬伤原因", "counter_evidence": "反证数据(必须含原始数据出处)"}
    ],
    "missed_risks": ["被忽视的风险; 若无实质反证写'无实质反证', 禁止编造"],
    "verdict": "AGREE / PARTIAL_AGREE / DISAGREE"
}"""

    challenger_system = challenger_system_v2  # v1 已删 (P11, 第14轮)

    challenger_user = f"""【证伪任务 — 第14轮 P9】候选方向 = {initial_analysis.get('direction','?')}。

第一步【假设它错了】:
仅根据以下原始数据, 假设 {initial_analysis.get('direction','?')} 方向会亏损, 找出最可能导致亏损的具体证据。
把你在"不看分析师结论"下独立倾向的方向填入 independent_direction (作为防锚定观测)。

原始数据:
{_challenger_data_view(data_text)}

---

第二步【对照分析师的论据】:
分析师的结论与论据如下。逐条核验其数字是否与上方原始数据一致:

{_challenger_analysis_view(initial_analysis)}

---

第三步【裁决 verdict】:
- 你找到了≥2条有数据出处的具体反证, 且足以推翻方向 → DISAGREE, my_direction 给反方向
- 找到反证但不足以推翻方向(只够降低确信度) → PARTIAL_AGREE, 指明哪条论据与哪条数据矛盾
- 找不到≥2条有出处的具体反证 → AGREE, 并在 missed_risks 写 "无实质反证" (诚实, 不编造)

无论 verdict 为何, falsification_target 与 flip_condition 必填 (AGREE 也不例外) —
"证伪失败"不等于零信息: 你必须指出这笔交易最可能的亏损机制, 以及什么数据出现会让你翻向。

【反虚构硬规】: 你的每条 challenged_claims 的 counter_evidence 与 missed_risks 里若含数字,
该数字必须能在上方原始数据中找到。找不到来源的数字 = 你的幻觉, 会被记录。
validation 五维照常核验(数据/逻辑/市场/风险/价位), 每维给 通过/不通过+一句证据。

my_score / my_score_band: 表达你对"自己my_direction"的确信度档位 (HIGH/MEDIUM/LOW,
```


# 信息源采集 — data_collector.py

# /root/.hermes/skills/deep-stock-analysis/scripts/data_collector.py (1229 lines)
## lines 1-115
```python
#!/usr/bin/env python3
"""
DataCollector — 增量数据采集层
================================
只采集 FTSeek 现有 Provider 不覆盖的数据源。
FTSeek 已有: snapshot/kline/capital_flow/fundamental/news(RSS)/sentiment(Reddit/HN)
本模块增量: 3维异动 + 富途社区情绪 + 富途新闻/研报 + 全市场涨跌榜

设计原则 (Ponytail):
- 不重复 FTSeek 已有的采集逻辑
- 每个方法独立可调用，返回纯 dict/list
- 失败返回 {"_error": "..."}，不抛异常
"""
import subprocess
import json
import os
import re
import sys
import time
import threading
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional

# 从 config 取所有路径/密钥，不硬编码
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import config

PYTHON = config.PYTHON
FUTUAPI_SCRIPTS = config.FUTUAPI_SCRIPTS
ANOMALY_SCRIPTS = config.ANOMALY_SCRIPTS
NEWS_BASE = config.NEWS_BASE
OPEND_HOST = config.OPEND_HOST
OPEND_PORT = config.OPEND_PORT
FINNHUB_BASE = config.FINNHUB_BASE

# ── 复用仓内单一真源 (2026-09-14, 用户裁「全部处理」P0-2) ────────────────────
#   用途: VIX 走 US.VIXY 代理时复用 app/data/vix_source.vixy_to_vix 的 0.85 校准,
#   不在本 skill 复制第二份系数 (同系数此前仓内 4 处复制, 任一漂移即口径分裂)。
#   append **不是** insert(0): FTSeek 仓根有 `config/` **包**, 而本 skill 用同名
#   `config.py` 模块 —— 插到最前会让后续 `import config` 解析到仓库包, 静默换掉
#   DA 自己的配置 (同 pipeline.py:149 注释)。实测 import 耗时 ~0.08s, 无副作用。
_FTSEEK_REPO = os.environ.get("FTSEEK_REPO", "/opt/ftseek/FTSeek-US")
if _FTSEEK_REPO not in sys.path:
    sys.path.append(_FTSEEK_REPO)

# ── OpenD 连接风暴治理 (2026-08-14, 8/13 盘中 35/35 快照超时实锤) ──────────
# 根因: collect_all 每维一个子进程连 OpenD (单标的最多 ~26 个), 5 标的并发 →
#   瞬时上百次握手; 盘中 live 服务已占 ~20 连接轮询 → OpenD 握手排队 →
#   全部卡死在子进程 timeout (15 并发对照实验 0/15 成功)。
# 修复: 全部 OpenD 子进程统一走 _opend_subprocess_run 信号量限流 (源头限流)。
#   DA_OPEND_MAX_CONC 可调 (默认 4: 实测单调 3s 稳定, 15 并发全灭)。
#   注意: _curl_json (Finnhub/新闻 HTTP) 不走 OpenD, 不限流。
_OPEND_MAX_CONC = int(os.environ.get("DA_OPEND_MAX_CONC", "4"))
_OPEND_SEM = threading.Semaphore(_OPEND_MAX_CONC)


def _opend_subprocess_run(cmd, timeout):
    """OpenD 子进程统一收口 — 信号量限并发连接, 防连接风暴 (见上方注释)."""
    with _OPEND_SEM:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def _run_futu_script(script_name: str, *args: str, timeout: int = 30) -> dict:
    script_path = os.path.join(FUTUAPI_SCRIPTS, script_name)
    if not os.path.exists(script_path):
        return {"_error": f"script not found: {script_path}"}
    cmd = [PYTHON, script_path] + list(args) + ["--json"]
    try:
        result = _opend_subprocess_run(cmd, timeout=timeout)
        stdout = result.stdout
        first, last = stdout.find('{'), stdout.rfind('}')
        if first >= 0 and last > first:
            try:
                return json.loads(stdout[first:last + 1])
            except json.JSONDecodeError:
                pass
        return {"_error": "no JSON", "_raw": stdout[:300]}
    except subprocess.TimeoutExpired:
        return {"_error": f"timeout {timeout}s"}
    except Exception as e:
        return {"_error": str(e)}


def _run_anomaly_script(anomaly_type: str, symbol: str, time_range: int = 7) -> dict:
    script_path = ANOMALY_SCRIPTS.get(anomaly_type)
    if not script_path or not os.path.exists(script_path):
        return {"_error": f"not found: {anomaly_type}"}
    cmd = [PYTHON, script_path, symbol, "--time-range", str(time_range), "--json"]
    try:
        result = _opend_subprocess_run(cmd, timeout=30)
        stdout = result.stdout
        first, last = stdout.find('{'), stdout.rfind('}')
        if first >= 0 and last > first:
            try:
                return json.loads(stdout[first:last + 1])
            except json.JSONDecodeError:
                pass
        return {"_error": "no JSON"}
    except Exception as e:
        return {"_error": str(e)}


def _curl_json(url: str, params: Dict[str, str], timeout: int = 15) -> dict:
    cmd = ["curl", "-sG", url, "-H", "User-Agent: deep-stock-analysis/0.2"]
    for k, v in params.items():
        cmd.extend(["--data-urlencode", f"{k}={v}"])
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return json.loads(result.stdout)
    except Exception as e:
        return {"_error": str(e)}


# ── FTSeek 没有的增量数据源 ──────────────────────

```

## lines 129-170 (matched: def collect_news)
```python


def collect_news(symbol: str, size: int = 10, lang: str = "zh-CN") -> List[dict]:
    """富途新闻 (FTSeek 用 RSS/finnhub, 这里是富途原生新闻源)"""
    keyword = symbol.split(".")[-1] if "." in symbol else symbol
    r = _curl_json(f"{NEWS_BASE}/news_search", {
        "keyword": keyword, "size": str(size),
        "news_type": "1", "lang": lang, "sort_type": "2",
    })
    if r.get("code") == 0:
        return r.get("data", [])
    return []


def collect_research_reports(symbol: str, size: int = 5, lang: str = "zh-CN") -> List[dict]:
    """富途研报"""
    keyword = symbol.split(".")[-1] if "." in symbol else symbol
    r = _curl_json(f"{NEWS_BASE}/news_search", {
        "keyword": keyword, "size": str(size),
        "news_type": "3", "lang": lang, "sort_type": "2",
    })
    if r.get("code") == 0:
        return r.get("data", [])
    return []


def collect_community_sentiment(symbol: str, size: int = 30) -> List[dict]:
    """富途社区帖子 (FTSeek 用 Reddit/HN, 这里是富途社区)"""
    keyword = symbol.split(".")[-1] if "." in symbol else symbol
    r = _curl_json(f"{NEWS_BASE}/stock_feed", {"keyword": keyword, "size": str(size)})
    if r.get("code") == 0:
        return r.get("data", [])
    return []


# ── P1 新闻层升级 (2026-08-14): Finnhub company-news 带摘要事实流 ──
# 背景: 原 collect_news 只喂富途中文标题(实测CRM仅3条无正文), 而 Finnhub 近7天
#   有74条带summary(最长1879字)。本函数做信噪比过滤后注入, 补新闻维度数据洞。
# 实测口径 (CRM 74条): 模板垃圾仅3条, 标题含ticker约1/3 → 相关性过滤有效。
# 回滚: DA_NEWS_V2=0 (零采集成本, 显示段自然为空)。
_NEWS_V2_TEMPLATE_JUNK = re.compile(
    r"Stay informed|making waves|Discover which|top movers within", re.I)
```

## lines 171-212 (matched: def collect_finnhub_news)
```python


def collect_finnhub_news(symbol: str, days: int = 7, top_n: int = 6) -> List[dict]:
    """Finnhub company-news: 7天窗 + 相关性过滤 + 去重, 返回带摘要的新闻列表。

    返回字段: time(ISO)/title/summary(截断200字)/source/url/low_relevance(bool)
    失败或开关关闭返回 [] (不阻塞主链路)。
    """
    if os.environ.get("DA_NEWS_V2", "1") == "0":
        return []
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    to_ = datetime.now().strftime("%Y-%m-%d")
    from_ = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    data = _finnhub_get("company-news", {"symbol": bare, "from": from_, "to": to_})
    if not isinstance(data, list) or not data:
        return []
    # 1) 时间倒序 + 标题前40字去重
    seen, uniq = set(), []
    for n in sorted(data, key=lambda x: -(x.get("datetime") or 0)):
        k = (n.get("headline") or "")[:40]
        if k and k not in seen:
            seen.add(k)
            uniq.append(n)
    # 2) 模板垃圾过滤 + ticker 相关性过滤
    pat = re.compile(rf"\b{re.escape(bare)}\b", re.I)
    relevant = [n for n in uniq
                if not _NEWS_V2_TEMPLATE_JUNK.search(n.get("headline", ""))
                and pat.search(n.get("headline", ""))]
    low_rel = False
    if not relevant:  # ticker 少见于标题时如实降级, 取最新N条并标注
        relevant, low_rel = uniq[:top_n], True
    # 3) 组装输出
    out = []
    for n in relevant[:top_n]:
        ts = n.get("datetime") or 0
        out.append({
            "time": datetime.utcfromtimestamp(ts).strftime("%m-%d") if ts else "",
            "title": (n.get("headline") or "")[:150],
            "summary": (n.get("summary") or "")[:200],
            "source": n.get("source", ""),
            "url": n.get("url", ""),
            "low_relevance": low_rel,
```

## lines 568-609 (matched: def collect_fundamentals)
```python


def collect_fundamentals(symbol: str) -> dict:
    """Finnhub 基本面指标: 营收增长/ROE/利润率/PE/PB等 (133字段)"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("stock/metric", {"symbol": bare, "metric": "all"})
    if "_error" in data:
        return data
    m = data.get("metric", {})
    return {
        "revenue_growth_yoy": m.get("revenueGrowthTTMYoy"),
        "revenue_growth_5y": m.get("revenueGrowth5Y"),
        "eps_growth_5y": m.get("epsGrowth5Y"),
        "roe_ttm": m.get("roeTTM"),
        "roa_ttm": m.get("roaTTM"),
        "net_profit_margin_ttm": m.get("netProfitMarginTTM"),
        "gross_margin_ttm": m.get("grossMarginTTM"),
        "operating_margin_ttm": m.get("operatingMarginTTM"),
        "pe_normalized": m.get("peNormalizedAnnual"),
        "pb_annual": m.get("pbAnnual"),
        "ps_annual": m.get("psAnnual"),
        "revenue_per_share_ttm": m.get("revenuePerShareTTM"),
        "book_value_per_share": m.get("bookValuePerShareAnnual"),
        "debt_to_equity": m.get("totalDebt/totalEquityAnnual"),
        "dividend_yield": m.get("dividendYieldIndicatedAnnual"),
        "payout_ratio": m.get("payoutRatioTTM"),
        "beta": m.get("beta"),
        "_source": "finnhub/stock/metric",
    }


def collect_analyst_consensus(symbol: str) -> dict:
    """Finnhub 分析师评级: strongBuy/buy/hold/sell/strongSell (按月)"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("stock/recommendation", {"symbol": bare})
    if "_error" in data:
        return data
    if isinstance(data, list) and data:
        latest = data[0]
        total = sum(latest.get(k, 0) for k in ["strongBuy", "buy", "hold", "sell", "strongSell"])
        return {
            "period": latest.get("period"),
```

## lines 242-283 (matched: def collect_snapshot)
```python
# ── 快照 (FTSeek 有 FundamentalProvider, 但 market_scanner 快速评分需要轻量快照) ───

def collect_snapshot(symbol: str) -> dict:
    """轻量快照 — 供 market_scanner 快速评分用。
    FTSeek 的 FundamentalProvider 更完整(多源降级), 但需要初始化 Provider 链。
    这里直接调 OpenD, 秒级返回, 适合扫描场景。

    2026-08-11 (LIF 盘前-20%盲区实锤): 补盘前/盘后字段 + effective_price。
    根因: 原实现只取 last_price/prev_close — 盘前时段 last_price=昨收,
    盘前真实异动 (pre_price/pre_change_rate) 完全不可见 → DA 06:22 ET 扫描
    看到 LIF "涨跌幅+2.2%" (昨收口径), 实际盘前 -19.5%, 指令建立在失效 thesis 上。
    effective_price = 按时段取当前真实价格 (盘前→pre_price, 盘后→after_price,
    盘中/收盘→last_price), 并给 effective_chg_pct (vs prev_close)。
    """
    # 从config读OpenD地址，不硬编码
    host = OPEND_HOST
    port = OPEND_PORT
    # 用subprocess参数传递symbol，不f-string注入（防代码注入）
    code = """
import json, sys
from datetime import datetime, timedelta, timezone
from futu import OpenQuoteContext, RET_OK
symbol = sys.argv[1]
host = sys.argv[2]
port = int(sys.argv[3])
q = OpenQuoteContext(host, port)
try:
    ret, data = q.get_market_snapshot([symbol])
    if ret == RET_OK and len(data) > 0:
        row = data.iloc[0]
        fields = {
            'code': str(row.get('code','')),
            'name': str(row.get('name','')),
            'last_price': float(row.get('last_price',0) or 0),
            'prev_close': float(row.get('prev_close_price',0) or 0),
            'prev_close_price': float(row.get('prev_close_price',0) or 0),
            'volume': float(row.get('volume',0) or 0),
            'turnover_rate': float(row.get('turnover_rate',0) or 0),
            'pe_ttm_ratio': float(row.get('pe_ttm_ratio',0) or 0),
            'pe_ratio': float(row.get('pe_ratio',0) or 0),
            'pb_ratio': float(row.get('pb_ratio',0) or 0),
            'total_market_val': float(row.get('total_market_val',0) or 0),
```

## lines 363-404 (matched: def collect_snapshot)
```python


def collect_snapshot_batch(symbols: List[str], timeout: int = 30, chunk_size: int = 30) -> List[dict]:
    """批量快照 — 单连接拉多标的 (含盘前/盘后/隔夜字段), 2026-08-11 新增。

    用途: premarket_movers 渠道。逐标的 collect_snapshot 每只一次连接 (~300ms),
    100+ 只池子太慢; get_market_snapshot 单调用支持批量, 秒级。
    ⚠️ 分批: 实测单次 >~130 只返回空 (API 隐性上限), 按 chunk_size 分批合并。
    只返回盘前/隔夜真实异动字段, 非交易时段 pre_price=0 的行由调用方过滤。
    """
    syms = [s for s in symbols if s and isinstance(s, str)]
    if not syms:
        return []
    host = OPEND_HOST
    port = OPEND_PORT
    code = """
import json, sys
from futu import OpenQuoteContext, RET_OK
symbols = sys.argv[1].split(',')
host = sys.argv[2]
port = int(sys.argv[3])
q = OpenQuoteContext(host, port)
try:
    ret, data = q.get_market_snapshot(symbols)
    if ret == RET_OK and data is not None and len(data) > 0:
        out = []
        for _, row in data.iterrows():
            out.append({
                'code': str(row.get('code','')),
                'name': str(row.get('name','')),
                'last_price': float(row.get('last_price',0) or 0),
                'prev_close_price': float(row.get('prev_close_price',0) or 0),
                'pre_price': float(row.get('pre_price',0) or 0),
                'pre_change_rate': float(row.get('pre_change_rate',0) or 0),
                'pre_volume': float(row.get('pre_volume',0) or 0),
                'after_price': float(row.get('after_price',0) or 0),
                'after_change_rate': float(row.get('after_change_rate',0) or 0),
                'overnight_change_rate': float(row.get('overnight_change_rate',0) or 0),
                'update_time': str(row.get('update_time','')),
                'total_market_val': float(row.get('total_market_val',0) or 0),
                'volume': float(row.get('volume',0) or 0),
            })
```

## lines 624-665 (matched: _finnhub_get)
```python
    """Finnhub 内部人交易: 最近3个月的买入/卖出"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("stock/insider-transactions", {"symbol": bare})
    if "_error" in data:
        return data
    transactions = data.get("data", [])
    # 最近的交易 (按日期倒序)
    recent = transactions[:20]
    # 统计净买入
    buy_count = sum(1 for t in recent if t.get("transactionType", "").upper() in ("B", "BUY"))
    sell_count = sum(1 for t in recent if t.get("transactionType", "").upper() in ("S", "SELL"))
    total_value = sum(t.get("value", 0) or 0 for t in recent)
    
    # 列出最近5笔
    recent_list = []
    for t in recent[:10]:
        recent_list.append({
            "date": t.get("transactionDate"),
            "name": (t.get("name", "") or "")[:20],
            "type": t.get("transactionType"),
            "shares": t.get("shareCount"),
            "price": t.get("transactionPrice"),
            "value": t.get("value"),
        })
    
    return {
        "total_transactions": len(transactions),
        "recent_buy_count": buy_count,
        "recent_sell_count": sell_count,
        "total_recent_value": total_value,
        "recent_transactions": recent_list,
        "_source": "finnhub/insider-transactions",
    }


def collect_insider_sentiment(symbol: str) -> dict:
    """Finnhub 内部人情绪: MSPR (Monthly Share Purchase Ratio)"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("stock/insider-sentiment", {"symbol": bare})
    if "_error" in data:
        return data
    records = data.get("data", [])
```

## lines 678-719 (matched: _finnhub_get)
```python
    """Finnhub 公司概况: 行业/市值/流通股"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("stock/profile2", {"symbol": bare})
    if "_error" in data:
        return data
    return {
        "name": data.get("name"),
        "industry": data.get("finnhubIndustry"),
        "country": data.get("country"),
        "exchange": data.get("exchange"),
        "market_cap": data.get("marketCapitalization"),
        "share_outstanding": data.get("shareOutstanding"),
        "_source": "finnhub/profile2",
    }


def collect_earnings_calendar(symbol: str) -> dict:
    """Finnhub 财报日历: 下次财报日期+预期"""
    bare = symbol.split(".")[-1] if "." in symbol else symbol
    data = _finnhub_get("calendar/earnings", {"symbol": bare})
    if "_error" in data:
        return data
    cal = data.get("earningsCalendar", [])
    # 找未来的财报 — 用ET时区（Finnhub返回的date是ET交易日）
    from datetime import datetime, timezone, timedelta
    _now = datetime.now()
    _offset = timedelta(hours=-4) if 3 <= _now.month <= 11 else timedelta(hours=-5)
    today_str = datetime.now(timezone(_offset)).strftime("%Y-%m-%d")
    future = [e for e in cal if e.get("date", "") >= today_str]
    next_earnings = future[0] if future else None
    if not next_earnings and cal:
        # 没有未来财报日期 — 不用过期的cal[-1]，
        # 标记降级让LLM知道财报日历不可用
        return {
            "next_earnings": None,
            "all_upcoming": [],
            "_source": "finnhub/calendar/earnings",
            "_quality": "degraded",
            "_degraded_reason": "无未来财报日期(API仅返回已过期记录)",
        }
    return {
        "next_earnings": next_earnings,
```

## lines 739-780 (matched: _finnhub_get)
```python
    yesterday_str = (et_today - timedelta(days=1)).strftime("%Y-%m-%d")

    data = _finnhub_get("calendar/earnings", {"from": yesterday_str, "to": today_str})
    if "_error" in data or not isinstance(data, dict):
        return []

    cal = data.get("earningsCalendar", [])
    surprises = []
    for e in cal:
        sym = e.get("symbol", "")
        if not sym:
            continue
        actual = e.get("epsActual")
        estimate = e.get("epsEstimate")
        if actual is not None and estimate is not None:
            surprise_pct = ((actual - estimate) / max(abs(estimate), 0.01)) * 100 if estimate != 0 else 0
        else:
            surprise_pct = 0
        surprises.append({
            "code": f"US.{sym}" if market == "US" else sym,
            "name": sym,
            "eps_actual": actual,
            "eps_estimate": estimate,
            "surprise_pct": round(surprise_pct, 1),
            "_source": "earnings_surprise",
        })

    # 按surprise绝对值排序, 取top
    surprises.sort(key=lambda x: abs(x.get("surprise_pct", 0)), reverse=True)
    return surprises[:limit]


def collect_insider_movers(market: str = "US", limit: int = 20) -> List[dict]:
    """Finnhub 内部人异动: 近7天有内部人交易(买入)的标的.

    Finnhub /stock/insider-transactions 需要按symbol查,
    但 /calendar/gradings 或其他全市场接口有限.
    最短路径: 用 insider-sentiment 全市场接口(如果有)或用news关键词.
    实际: Finnhub免费版无全市场insider API, 用community_sentiment的post_count作为热度代理.
    """
    # Finnhub免费版无全市场insider API, 返回空但标注
    # 如果后续有付费API可在此扩展
```

## lines 1065-1106 (matched: _finnhub_get)
```python
    
    # 2. 获取同行业peer (Finnhub peers API)
    peers_data = _finnhub_get("stock/peers", {"symbol": bare})
    if "_error" in peers_data:
        return {"_error": "peers API failed", "_detail": str(peers_data.get("_error"))}
    
    # Finnhub返回的是symbol列表
    peer_symbols = peers_data if isinstance(peers_data, list) else peers_data.get("peers", [])
    if not peer_symbols:
        return {"_error": "no peers found", "industry": industry}
    
    # 3. 取top 5 peer的近期表现(用snapshot)
    import concurrent.futures
    
    peers_info = []
    
    def _get_peer_snapshot(peer_sym):
        """获取peer的快照+5日涨跌"""
        us_sym = f"US.{peer_sym}" if not peer_sym.startswith("US.") else peer_sym
        snap = collect_snapshot(us_sym)
        if "_error" in snap:
            return None
        # 简单取5日K线算涨跌
        klines = collect_kline(us_sym, "1d", 5)
        chg_5d = 0
        if len(klines) >= 2:
            first_close = klines[0].get("close", 0)
            last_close = klines[-1].get("close", 0)
            if first_close > 0:
                chg_5d = round((last_close - first_close) / first_close * 100, 2)
        # [v0.70 审计修复 2026-08-26] change_pct/pe_ttm/market_cap 均为错配键(恒0) —
        #   改读真键: 涨跌幅由 last/prev_close 现算, PE=pe_ttm_ratio, 市值=total_market_val
        _p_lp = float(snap.get("last_price", 0) or 0)
        _p_pc = float(snap.get("prev_close", 0) or snap.get("prev_close_price", 0) or 0)
        return {
            "symbol": peer_sym,
            "price": _p_lp,
            "chg_pct": round((_p_lp - _p_pc) / _p_pc * 100, 2) if (_p_lp > 0 and _p_pc > 0) else 0,
            "chg_5d_pct": chg_5d,
            "pe": snap.get("pe_ttm_ratio", 0) or snap.get("pe_ratio", 0),
            "market_cap": snap.get("total_market_val", 0),
        }
```


# DA 编排 — pipeline.py 关键段

# /root/.hermes/skills/deep-stock-analysis/scripts/pipeline.py (2843 lines)
## lines 1-60
```python
#!/usr/bin/env python3
"""
Pipeline — 管线编排器
=====================
5阶段模块化, 每阶段可独立运行。

Signal → Filter → Output → Execute → Result
市场扫描   筛选    深度分析  适配FTSeek  (FTSeek追踪IC)

Signal追踪/IC由FTSeek已有的signal_ic_tracker + playbook/ic_tracker负责。
本管线不重复IC计算, 只输出适配器格式供FTSeek消费。
"""

import json
import re
import time
import sys
import os
import hashlib
import math
from typing import Dict, List

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import config

# ────────────────────────────────────────────────────────────────────────
# [DEB-20260901] 结构化入场语义 — 确定性兜底解析 (双轨之"兜底轨")
#   LLM 直出字段优先; 缺失时从 entry_strategy/thesis 文本抽取。保守原则:
#   文本模式不明确 → None (FTSeek 侧按 BREAKOUT=现状行为处理, 零行为变更)。
#   消费方: FTSeek Phase B 影子 (pullback_limit_shadow) + 未来 archetype 路由。
# ────────────────────────────────────────────────────────────────────────
_CHASE_CAP_PATTERNS = (
    re.compile(r"禁止.*?[\$￥]?\s?([\d,]+(?:\.\d+)?)\s*(?:以上|上方)?\s*追涨"),
    re.compile(r"禁止追涨.*?[\$￥]?\s?([\d,]+(?:\.\d+)?)"),
    re.compile(r"禁止.*?追高.*?[\$￥]?\s?([\d,]+(?:\.\d+)?)"),
)
_FIRST_LEG_PRICE = re.compile(r"第[1一]批[^。;；\n]*?[\$￥@]\s?([\d,]+(?:\.\d+)?)")
_PULLBACK_WORDS = ("低吸", "回踩", "承接", "企稳接", "回调至", "回调企稳", "试多", "支撑区")
_BREAKOUT_WORDS = ("突破追入", "放量突破", "突破后追入", "放量上攻")
_FADE_WORDS = ("等反弹", "反弹至", "反弹做空", "逢反弹", "企稳后顺势做空", "逢高做空")
_VALID_ARCHS = ("BREAKOUT", "PULLBACK_LIMIT", "FADE_SHORT")


def _derive_entry_semantics(entry_strategy, thesis, direction,
                            llm_archetype=None, llm_confirmation=None, llm_chase_cap=None):
    """返回 (entry_archetype, confirmation, chase_cap_price)。LLM 值优先, 文本兜底。"""
    text = " ".join(str(x or "") for x in (entry_strategy, thesis))
    # chase_cap: "禁止在$X以上追涨" / "禁止追涨>$X"
    chase_cap = llm_chase_cap if isinstance(llm_chase_cap, (int, float)) else None
    if chase_cap is None:
        for _pat in _CHASE_CAP_PATTERNS:
            _m = _pat.search(text)
            if _m:
                try:
                    chase_cap = float(_m.group(1).replace(",", ""))
                    break
                except Exception:
                    pass
    # archetype
```

## lines 1327-1368 (matched: def run)
```python
        self.ue_only = ue_only

    def run_full(self, scan_result: Dict = None) -> Dict:
        results = {"pipeline_start": time.time(), "stages": {}}

        # ── Stage 1: Signal (市场扫描) ──────────────
        if scan_result is None:
            print("\n" + "="*60, file=sys.stderr)
            print("STAGE 1: SIGNAL — 市场扫描", file=sys.stderr)
            print("="*60, file=sys.stderr)

            try:
                import market_scanner
                scanner = market_scanner.MarketScanner(self.market, self.scan_limit)
                scan_result = scanner.scan(self.top_n * 2)
            except Exception as e:
                scan_result = {"error": str(e), "market_condition": f"扫描失败: {e}",
                              "candidates_total": 0, "top_bullish": [], "top_bearish": [], "all_scored": []}
                print(f"[Pipeline] 扫描失败: {e}", file=sys.stderr)
        else:
            print("[Pipeline] fresh_event: 复用当日市场上下文, 跳过 Stage1 重扫", file=sys.stderr)
        results["stages"]["signal"] = {
            "market_condition": scan_result.get("market_condition", ""),
            "candidates_total": scan_result.get(
                "candidates_total", scan_result.get("candidate_count", 0)),
        }
        print(f"[Pipeline] 扫描完成: {results['stages']['signal']['candidates_total']}只候选", file=sys.stderr)

        # ── Stage 2: Filter (筛选) ──────────────────
        print("\n" + "="*60, file=sys.stderr)
        print("STAGE 2: FILTER — 筛选", file=sys.stderr)
        print("="*60, file=sys.stderr)

        # ── [P1.4 2026-09-12] Stage2 裁决收敛到单一可测入口 `_select_deep_candidates()` ──
        #   先过滤后预算 / 完整分桶队列 / 补位 / 跨来源去重 / 漏斗落盘 全在该函数内,
        #   run_full 只消费结果 ⇒ 契约测试可直接注入 scan_result, 无需跑 LLM/触网。
        _selection = _select_deep_candidates(
            scan_result, self.extra_symbols,
            _load_ue_short_candidates(window=self.ue_window),
            top_n=self.top_n, output_dir=self.output_dir,
            ue_only=self.ue_only)
        deep_candidates = _selection["deep_candidates"]
```

## lines 2392-2433 (matched: def run)
```python
            print(f"[Bridge] Obsidian写入失败(不影响): {_obs_e}", file=sys.stderr)

    def run_scan_only(self) -> Dict:
        import market_scanner
        scanner = market_scanner.MarketScanner(self.market, self.scan_limit)
        return scanner.scan(self.top_n)

    def run_analyze_only(self, symbol: str, synth_mode: str = "llm") -> Dict:
        """synth_mode: 'llm' (默认,调LLM) | 'rule' (纯规则,不调LLM)"""
        import data_collector, deep_analyzer
        print(f"[Pipeline] 采集 {symbol} 全量数据...", file=sys.stderr)
        data = data_collector.collect_all(symbol)
        print(f"[Pipeline] 数据质量: {data.get('_quality', {})}", file=sys.stderr)

        synth = 'rule' if synth_mode == 'rule' else None
        analyzer = deep_analyzer.DeepAnalyzer(data, synth_module=synth)
        result = analyzer.analyze()

        # ── Signal Gate (先于ic_writer, 让gate数据进factors_json) ──
        try:
            import signal_gate
            gate_result = signal_gate.evaluate_signal(result)
            result["_signal_gate"] = gate_result
            print(f"[Pipeline] Signal Gate: {gate_result['decision']} "
                  f"score={gate_result['quality_score']}", file=sys.stderr)
        except Exception as e:
            print(f"[Pipeline] Signal Gate失败(不影响输出): {e}", file=sys.stderr)

        # 写入 FTSeek signal_snapshots.db (IC追踪, 不碰运行代码)
        # 2026-08-03 A/B隔离: --no-db-write 时跳过 (不写生产 signal_snapshots)
        if self.db_write:
            try:
                import ic_writer
                decision_id = ic_writer.write_signal_snapshot(result)
                result["_ic_decision_id"] = decision_id
            except Exception as e:
                result["_ic_decision_id"] = None
                print(f"[Pipeline] IC写入失败: {e}", file=sys.stderr)
        else:
            result["_ic_decision_id"] = None

        # Obsidian per-symbol 深度分析归档 (best-effort, 不阻塞pipeline)
```

## lines 195-236 (matched: def _da)
```python


def _da_batch_id() -> str:
    deps = _ftseek_screening_deps()
    now = _et_now()
    if deps:
        try:
            return str(deps["make_batch_id"]("DA", now))
        except Exception:
            pass
    return f"DA:{now.strftime('%Y%m%dT%H%M%S')}"


def _canon_symbol(value) -> str:
    """跨来源去重用的规范化标的 (与 FTSeek `normalize_symbol` 同源)。"""
    deps = _ftseek_screening_deps()
    if deps:
        try:
            return deps["canonical_symbol"](value)
        except Exception:
            pass
    sym = str(value or "").upper().strip()
    if not sym:
        return ""
    return sym if sym.startswith("US.") else f"US.{sym}"


def _da_screening_thresholds() -> Dict:
    """读 FTSeek 单一配置真源 (`get_playbook_config()` + `get_admission_config()`);
    读不到回退常量 —— 不猜阈值, 不新增一串 DA 侧永久开关。

    DA 侧只有扫描快照价, 没有实时盘口 / 借券数据 ⇒ 点差门与借券门恒不启用
    (缺数 ≠ 拒绝; 执行端 admission + quote gate 仍全量复核, 盘前快照不当实时行情用)。
    """
    out = {
        "version": "da_ue_v1",
        "min_price": _DA_SCREEN_MIN_PRICE_FALLBACK,
        "low_atr_enabled": True,
        "min_atr_ratio": _DA_SCREEN_MIN_ATR_PCT_FALLBACK / 100.0,
        "max_scan_age_hours": 3.0,
        "max_quote_age_sec": 90.0,
        "max_spread_ratio": 0.05,
```

## lines 371-412 (matched: def _da)
```python


def _da_direction_hint(cand: Dict, fallback: str = "") -> str:
    hint = str(cand.get("ue_direction") or cand.get("direction_hint")
               or cand.get("_scan_side") or fallback or "").upper()
    return {"BEARISH": "SHORT", "BULLISH": "LONG", "SELL_SHORT": "SHORT",
            "BUY": "LONG"}.get(hint, hint)


def _screen_da_candidates(cands: List[Dict], stage: str = "da_main_pool",
                          batch_id: str = None, direction_hint: str = "") -> List[Dict]:
    """逐个资格判定并附 `candidate_screening` — **只判定不截断** (预算由调用方消费)。

    输出 = 输入顺序不变的浅拷贝列表, 每行带与 FTSeek UE 侧**同一 schema** 的
    `candidate_screening` (symbol/price/status/reasons/missing_fields/policy_version/
    candidate_id/batch_id/source/observed_at + DA 侧 ATR 口径审计字段), 因此
    `select_candidates()` / `record_candidate_batch()` / 候选漏斗 JSONL 直接可消费。
    `symbol` 本身**不改写** (DA 下游 collect_all / bridge 依赖原格式)。
    """
    rows: List[Dict] = []
    deps = _ftseek_screening_deps()
    batch_id = batch_id or _da_batch_id()
    if os.environ.get("DA_CANDIDATE_SCREEN", "1") == "0" or not deps:
        for cand in cands or []:
            cand = dict(cand)
            cand["candidate_screening"] = {
                "symbol": _canon_symbol(cand.get("symbol")),
                "status": "UNKNOWN", "reasons": [],
                "missing_fields": ["screening_disabled_or_deps_unavailable"],
                "stage": stage, "batch_id": batch_id, "source": "DA",
                "policy_version": None,
            }
            rows.append(cand)
        return rows
    policy = _da_screening_thresholds()
    evaluate = deps["evaluate_candidate"]
    observed_at = _et_now().isoformat()
    missing_price = [str(c.get("symbol") or "") for c in (cands or [])
                     if _candidate_ref_price(c) is None]
    snapshot_rows = {}
    if missing_price:
        try:
```

## lines 1609-1650 (matched: deep_analysis)
```python
                        "tier": gate_result.get("tier", ""),
                        "position_size_hint": gate_result.get("strategy_hint", {}).get("position_size_hint", ""),
                        "source": "deep_analysis",
                        "reasons": gate_result.get("reasons", []),
                        "decision_conf": gate_result.get("decision_conf"),
                        "shadow_llm_self_tier": gate_result.get("shadow_llm_self_tier", ""),
                        "shadow_llm_self_decision": gate_result.get("shadow_llm_self_decision", ""),
                        "llm_self_conf": gate_result.get("llm_self_conf"),
                        # 挑战者 v2 观测 (DEB-20260814 §十一): AGREE可达性+防锚定效果统计
                        "challenger_verdict": (analysis.get("_llm_output", {}).get("_debate") or {}).get("challenger_verdict", ""),
                        "challenger_direction": (analysis.get("_llm_output", {}).get("_debate") or {}).get("challenger_direction", ""),
                        "challenger_independent_direction": (analysis.get("_llm_output", {}).get("_debate") or {}).get("challenger_independent_direction"),
                        "challenger_anchor_divergence": (analysis.get("_llm_output", {}).get("_debate") or {}).get("challenger_anchor_divergence", False),
                        # P0 影子双写 (DEB-20260814): 四态细分 conf 的 record-only 对照,
                        #   {conf,tier,decision} — live 不受影响, 供 §9.5 判据裁决。
                        "p0_shadow": gate_result.get("p0_shadow"),
                        # ── [DEB-20260825 第13轮 P6] DA 决策台账补字段 (零行为变更, 纯观测) ──
                        #   动因: 本行此前只有 gate 侧 quality_score/decision, 缺 DA 自身的
                        #   score/rating/skip_reason/tripwire数/五维验证不通过维度/prompt 指纹
                        #   → 第11轮的验证项 (SKIP 理由分布 + SHORT 占比) 只能靠对 reasons
                        #   文本正则抠 "score=X/10", 且 prompt 改版前后无法切分 (口径七律#2)。
                        #   勘误: 第12轮曾断言"SKIP 决策零落盘", 实为**误判** — 本 jsonl 一直
                        #   逐候选写入含 SKIP (v2 窗 84/195 行), 详见第13轮 §勘误。
                        #   已知缺口: checkpoint 命中的标的在上游 continue, 不写本行 (同日重跑才发生)。
                        "rating": analysis.get("rating", ""),
                        "da_score": analysis.get("score"),
                        "skip_reason": str(_lo_g.get("skip_reason") or "")[:300],
                        # [第14轮 P8] score_band 观测: 新契约三档(HIGH/MEDIUM/LOW)与旧数字score对照,
                        #   判据=score分布方差是否从1.268(旧)显著上升。_score_band_applied=实际生效档。
                        "score_band": _lo_g.get("_score_band_applied") or str(_lo_g.get("score_band") or ""),
                        "da_score_legacy": _lo_g.get("score_legacy"),
                        "tripwire_count": len([_t for _t in (_plan_g.get("tripwires") or [])
                                               if isinstance(_t, dict)
                                               and str(_t.get("metric") or "").strip()
                                               and str(_t.get("threshold") or "").strip()]),
                        # [第14轮 P10] 机器可核销失效条件数 (本股可判定), 判据=30天后tripwire_checker核销率>0
                        "invalidation_trigger_count": len([
                            _t for _t in (_plan_g.get("invalidation_triggers") or [])
                            if isinstance(_t, dict)
                            and str(_t.get("metric") or "") in ("收盘价","量比","5日涨跌","盘前涨跌幅")
                            and str(_t.get("operator") or "") in ("<",">")]),
                        "challenger_score": _dbg_g.get("challenger_score"),
```

## lines 1799-1840 (matched: deep_analysis)
```python
    def _write_ftseek_data_channels(self, scan_result: Dict, analyses: List[Dict],
                                    selected_symbols: List[str] = None):
        """把deep_analysis的规划输出写入FTSeek已读取的数据文件.

        3个文件通道 (全部best-effort, 失败只log不阻塞):
        1. data/daily_active_pool.json — scan候选追加, factor_engine自动读(0行FTSeek改动)
        2. data/dynamic_blacklist.json — avoid列表, risk_stage读(冻结后加3行)
        3. data/deep_sector_rotation.json — 板块轮动, plan_validator读(冻结后加3行)

        [P1.4 2026-09-12] `selected_symbols` = Stage2 过滤后**实际提交深分析**的合格
        名单。DailyActivePool 只消费它 + 合格 analyses; `scan_result` 降级为纯
        metadata 来源 (score/strategy_type 展示), **不再作为准入来源**。
        """
        import json as _json
        from datetime import datetime, timezone, timedelta
        _et_offset = timedelta(hours=-4) if 3 <= datetime.now().month <= 11 else timedelta(hours=-5)
        _et_tz = timezone(_et_offset)
        _now_et = datetime.now(_et_tz)
        _today_str = _now_et.strftime("%Y-%m-%d")
        # 2026-08-03 (任务#5): expires_at 必须带 ET 偏移 — 消费端 is_expired() 把 naive
        #   时间戳当 UTC (daily_active_pool.py), 裸 "T16:30:00" 会提前 4h (12:30 ET) 过期.
        _eod_str = _now_et.replace(hour=16, minute=30, second=0, microsecond=0).isoformat()  # ET收盘后过期

        data_dir = self.output_dir  # 2026-08-03 A/B隔离: 原硬编码 /opt/ftseek/FTSeek-US/data

        # ── 1. daily_active_pool.json: scan候选追加 ──
        try:
            pool_path = f"{data_dir}/daily_active_pool.json"
            pool = _json.loads(open(pool_path).read()) if os.path.exists(pool_path) else {"entries": [], "summary": {}}
            existing_codes = {e.get("code", "") for e in pool.get("entries", [])}

            # [P1.4 2026-09-12] 不再从 scanner top_bullish/top_bearish **重拼**活跃池 ——
            #   修前: pool = scanner Top (含已被资格判定拒绝的 DA 候选) + BUY/SELL_SHORT
            #   analyses ⇒ 最终候选裁决被绕过, 已拒标的重新出现在 factor_engine 读取的
            #   daily_active_pool 里 (DEB-20260910 问题④: "写 DailyActivePool 时重新拼
            #   scanner Top, 未必遵守最终候选裁决")。
            #   修后: pool = selected_symbols (实际提交深分析的合格名单) + 合格 analyses。
            #   scan_result 只用于取 metadata 展示, 不做准入; 未传 selected_symbols 时
            #   只用 analyses —— **绝不回退到 scanner Top**。
            _meta_rows = (scan_result.get("all_scored")
                          or (scan_result.get("bullish_all", [])
                              + scan_result.get("bearish_all", [])
```

## lines 1855-1896 (matched: deep_analysis)
```python
                    existing_codes.add(sym)

            # 追加deep_analysis候选(bridge候选) — 确保FTSeek watchlist包含bridge标的
            for a in analyses:
                if a.get("action") in ("BUY", "SELL_SHORT"):
                    sym = a.get("symbol", "")
                    if sym and sym not in existing_codes:
                        scan_candidates.append(sym)
                        existing_codes.add(sym)

            for sym in scan_candidates:
                # 找对应的分析结果取metadata
                _a = next((a for a in analyses if a.get("symbol") == sym), None)
                _s = _meta_by_symbol.get(str(sym))
                pool["entries"].append({
                    "code": sym,
                    "source": "SCAN",
                    "added_at": _now_et.isoformat(),
                    "expires_at": _eod_str,
                    "metadata": {
                        "strategy_type": (_a or _s or {}).get("strategy_type", ""),
                        "score": (_a or _s or {}).get("score", 0),
                    },
                })

            pool["summary"] = {
                "total": len(pool["entries"]),
                "breakdown": pool.get("summary", {}).get("breakdown", {}),
                "max_pool_size": 50,
                "scan_added": len(scan_candidates),
            }

            if self.pool_write:
                with open(pool_path, "w") as f:
                    _json.dump(pool, f, ensure_ascii=False, default=str)
                print(f"[Pipeline] daily_active_pool.json: +{len(scan_candidates)} scan候选 → {len(pool['entries'])}总", file=sys.stderr)
            else:
                print(f"[Pipeline] --no-pool-write: 跳过 daily_active_pool 落盘 (A/B 隔离)", file=sys.stderr)
        except Exception as e:
            print(f"[Pipeline] daily_active_pool写入失败(不影响): {e}", file=sys.stderr)

        # ── 2. dynamic_blacklist.json: avoid列表 ──
```

## lines 2135-2176 (matched: deep_analysis)
```python
                    #   cand块漏拷 → 8/14首跑 0/18 落库。补齐, 供盘后复盘核销+IC审计。
                    "tripwires": vr.get("tripwires", []),
                    "strategy_id": "deep_analysis",
                    "decision_id": a.get("_ic_decision_id", ""),
                    "strategy_type": a.get("strategy_type", ""),
                    "position_size_hint": a.get("position_size_hint", ""),
                    "ue_direction": str(a.get("_ue_direction") or "").upper(),
                    "ue_confidence": a.get("_ue_confidence"),
                    "ue_scanned_at": a.get("_ue_scanned_at", ""),
                    "ue_batch_id": a.get("_ue_batch_id", ""),
                    "ue_evidence": a.get("_ue_evidence", ""),
                    "ue_causal_chain": a.get("_ue_causal_chain", ""),
                    "ue_evidence_citation_state": a.get("_ue_evidence_citation_state", ""),
                    "ue_confidence_basis": a.get("_ue_confidence_basis", ""),
                    "ue_confidence_calibration": a.get("_ue_confidence_calibration") or {},
                    "ue_counter_evidence": a.get("_ue_counter_evidence", ""),
                    "ue_event_phase": a.get("_ue_event_phase", ""),
                    "ue_move_basis": a.get("_ue_move_basis", ""),
                    "ue_transmission_confirmation_required": a.get(
                        "_ue_transmission_confirmation_required", False),
                    # UE→DA 候选管道溯源 (2026-08-03): "ue_candidate"=DA确认UE浮现的候选,
                    #   ""=DA自选。字面量不含 universe_expansion → scorecard 仍归 DA 源。
                    "origin": "ue_candidate" if a.get("_ue_origin") else "",
                    # v0.39: 辩论结果写入bridge
                    "debate_outcome": _debate_outcome,
                    "debate": _debate if _debate else None,
                    "anchor_dev_pct": _anchor_dev,
                    # DEB-20260819 §十七-L1/L2: 股性卡实际注入规则列表透传
                    #   (语义=注入标记; 判分分组用文本引用cited vs 注入未引用)
                    "pangan_injected": (_llm_out.get("pangan_injected") or []),
                }
                candidates.append(cand)

                # ── P0修复: 同时写instruction到DB ──
                # 绕过run_playbook_generate的幂等检查, 让auto_execute直接消费
                # 2026-08-03 A/B隔离: --no-db-write 跳过 (候选已进 bridge 文件, 此处仅跳 DB 直写)
                if not self.db_write:
                    continue
                try:
                    import sys as _sys
                    _sys.path.insert(0, "/opt/ftseek/FTSeek-US")
                    from app.playbook.db import PlaybookDB
```

## lines 2220-2261 (matched: deep_analysis)
```python
                        "spy_trend_at_generation": _regime_fields.get("spy_trend_at_generation"),
                        "raw_instruction": {
                            "source": "deep_analysis",
                            "strategy_type": a.get("strategy_type", ""),
                            "position_size_hint": a.get("position_size_hint", ""),
                            "ue_direction": str(a.get("_ue_direction") or "").upper(),
                            "ue_confidence": a.get("_ue_confidence"),
                            "ue_scanned_at": a.get("_ue_scanned_at", ""),
                            "ue_batch_id": a.get("_ue_batch_id", ""),
                            "ue_evidence": a.get("_ue_evidence", ""),
                            "ue_causal_chain": a.get("_ue_causal_chain", ""),
                            "ue_evidence_citation_state": a.get("_ue_evidence_citation_state", ""),
                            "ue_confidence_basis": a.get("_ue_confidence_basis", ""),
                            "ue_confidence_calibration": a.get("_ue_confidence_calibration") or {},
                            "ue_counter_evidence": a.get("_ue_counter_evidence", ""),
                            "ue_event_phase": a.get("_ue_event_phase", ""),
                            "ue_move_basis": a.get("_ue_move_basis", ""),
                            "ue_transmission_confirmation_required": a.get(
                                "_ue_transmission_confirmation_required", False),
                            # UE→DA 管道溯源落库 (2026-08-03): scorecard 可读
                            #   raw_instruction_json LIKE '%ue_candidate%' 切 DA·SHORT 的 UE 确认子集
                            "origin": "ue_candidate" if a.get("_ue_origin") else "",
                            "entry_strategy": vr["entry_strategy"],
                            # [DEB-20260901] 结构化入场语义落库 (Phase B 影子从
                            #   raw_instruction_json 读取; 双轨新增, 零行为变更)
                            "entry_archetype": _arch,
                            "confirmation": _conf_struct,
                            "chase_cap_price": _chase_cap,
                            "invalidation": vr["invalidation"],
                            # [r15 P10接线] 机器可核销失效条件落库 — tripwire_checker
                            #   从 raw_instruction_json.invalidation_triggers 读.
                            "invalidation_triggers": vr.get("invalidation_triggers", []),
                            # P2 落库断点修复 (2026-08-14): 与 cand 块同步透传
                            "tripwires": vr.get("tripwires", []),
                            "debate_outcome": _debate_outcome,
                            "score": vr["score"],
                            # [丙2 PROPOSAL-20260905 P1, 2026-09-05] score_band 落库 —
                            #   band 是打分区分度的真正载体 (HIGH/MEDIUM·0.9/MEDIUM·0.8 单调),
                            #   此前只在 checkpoint 里, 指令级不可 join → 修覆写后无法回测档位表现。
                            "score_band": (_llm_out.get("_score_band_applied")
                                           or _llm_out.get("score_band")),
                            # [丙1 配套] 挑战者影子分 (不再覆写 score) + "本会覆写"标记.
```

## lines 128-169 (matched: data_collector)
```python
_DA_SCREEN_MIN_PRICE_FALLBACK = 5.0        # 配置读不到时的回退 (与原 _PREFILTER_MIN_PRICE 同值)
_DA_SCREEN_MIN_ATR_PCT_FALLBACK = 3.0      # 2.9 → 3.0 (见上, 显式统一到执行端配置)
_DA_SCREEN_SOURCE = "da.data_collector.collect_kline"
_DA_SCREEN_WINDOW = "da_premarket"
_DEPS_STATE: Dict[str, object] = {"loaded": False, "deps": None}
_ATR_CACHE: Dict[str, Dict] = {}           # 单轮进程内缓存: (symbol|asof_date) → detail


def _ftseek_screening_deps():
    """惰性导入 FTSeek 仓内共用件; 任何失败 fail-open (返回 None, 不阻断 DA 主流程)。

    DA 是仓外 skill (`/root/.hermes/skills/deep-stock-analysis/scripts/`), 不能假设
    FTSeek 可导入。导入失败 ⇒ 初筛退化为"只标注 UNKNOWN, 不拒绝", 由执行端原有
    风险门 (admission / 低 ATR / quote gate) 全量兜底 —— 不静默改判定口径。
    """
    if _DEPS_STATE["loaded"]:
        return _DEPS_STATE["deps"]
    _DEPS_STATE["loaded"] = True
    try:
        # append (不是 insert(0)): FTSeek 仓根有 `config/` **包**, 而本 skill 用同名
        #   `config.py` 模块; 插到最前会让后续 `import config` 解析到仓库包, 静默换掉
        #   DA 自己的配置。本 skill 目录已在 sys.path[0] (文件头 insert), append 后
        #   DA 模块名恒优先, `app.*` 仍能解析。
        if _FTSEEK_REPO not in sys.path:
            sys.path.append(_FTSEEK_REPO)
        from app.core import atr_provider as _ap
        from app.playbook import candidate_eligibility as _ce
        from app.playbook import ue_batch as _ub
        _DEPS_STATE["deps"] = {
            "compute_screening_atr": _ap.compute_screening_atr,
            "evaluate_candidate": _ce.evaluate_candidate,
            # 2026-10-11 移除 "source_time": _ce._source_time —— 零消费者死注入。
            #   它由未登记 WIP 引入, 唯一用途是把 DA 扫描快照时戳换算成 quote_age_sec 灌进
            #   entry 级 90s 门 (见 _screen_da_candidates 内 source_asof 处批注)。随该特性一并回退。
            "load_candidate_policy": _ce.load_candidate_policy,
            "canonical_symbol": _ce.canonical_symbol,
            "select_candidates": _ce.select_candidates,
            "make_batch_id": _ce.make_batch_id,
            "record_candidate_batch": _ce.record_candidate_batch,
            "fingerprint": _ce._fingerprint,
            "latest_valid_batch": _ub.latest_valid_batch,
        }
```

## lines 282-323 (matched: data_collector)
```python
    """原始日线筛选 ATR + 比例 (纯函数口径, 复用 FTSeek `compute_screening_atr`)。

    - 取数走 DA 自有 `data_collector.collect_kline` — **不新建第二条行情连接**;
    - 15 根已完成日线 → 14 个 TR, rolling-mean 取末值; 交易日截断 (当日未完成 bar
      排除)、同日去重、拆股/复权断点 (>1.6x) 不跨算;
    - 分母 = 调用方候选快照价优先, 缺则 `ref_close` (末根已完成日线收盘),
      口径写在 `denominator` 里供审计;
    - 任何缺数/异常 → status=UNKNOWN + reason, **不外推、不返回 0、不编造 ATR**;
    - 单轮进程内按 (symbol, asof_date) 缓存: 主池/extras/UE/异动多阶段重叠标的零重复取数。
    """
    bare = str(sym or "").replace("US.", "")
    detail = {"atr": None, "status": "UNKNOWN", "reason": "deps_unavailable",
              "basis": None, "source": _DA_SCREEN_SOURCE, "adjustment": None,
              "bars_asof": None, "bars_used": 0, "ref_close": None,
              "denominator": None, "ratio": None, "ratio_pct": None}
    deps = _ftseek_screening_deps()
    if not deps or not bare:
        if not bare:
            detail["reason"] = "invalid_symbol"
        return detail
    now = asof if asof is not None else _et_now()
    try:
        from app.core.atr_provider import screening_asof_date
        asof_date = screening_asof_date(now)
    except Exception:
        asof_date = str(now)[:10]
    cache_key = f"US.{bare}|{asof_date}"
    hit = _ATR_CACHE.get(cache_key)
    if hit is not None:
        detail = dict(hit)
    else:
        try:
            import data_collector as _dc
            bars = _dc.collect_kline(f"US.{bare}", "1d", 40) or []
        except Exception as _ke:
            detail["reason"] = f"kline_unavailable:{type(_ke).__name__}"
            _ATR_CACHE[cache_key] = detail
            return dict(detail)
        try:
            got = deps["compute_screening_atr"](bars, now, symbol=f"US.{bare}",
                                                source=_DA_SCREEN_SOURCE)
        except Exception as _ae:
```

## lines 1088-1129 (matched: data_collector)
```python
    全部输入可注入 (scan_result / extra_symbols / ue_candidates / top_n /
    output_dir / batch_id), 因此契约测试不需要跑 scanner、LLM 或触网;
    行情取数仍走 `_fetch_screening_atr` → `data_collector.collect_kline`
    (测试 monkeypatch 该函数即可)。

    预算边界全部沿用旧值: 主池 top_n+2 / extras 全量(只去重) /
    UE ≤ _UE_FEED_CAP / 盘前异动 ≤ 2。变的是**顺序**: 资格判定前置, 淘汰补位。

    Returns:
        {"deep_candidates", "selected_symbols", "screened", "batch_id", "filter"}
        — `selected_symbols` = **过滤后**实际提交深分析的名单 (修前记过滤前);
          `screened` = 全阶段候选漏斗原始行 (candidate_screening + selection_status)。
    """
    # 2026-07-18 (用户): 不再人为设定多/空配额 (原 bullish[:top_n] + bearish[:2] 非对称),
    #   改为合并两桶后按"方向信念强度(conviction)"统一择优取前 N。
    #   bullish 桶按高分排序、bearish 桶按低分入桶(score<=4), 故原始 score 非对称,
    #   用 conviction = score(多) / 10-score(空) 归一为"方向强度", 让信号本身决定多空占比。
    # ── [P1.4 2026-09-12, DEB-20260910] 先过滤后预算 — 三处修正 ──
    #   ① 池源: scanner 展示用的 top_bullish/top_bearish (已 [:top_n*2] 截断)
    #      → 完整分桶队列 bullish_all/bearish_all。展示 TopN 与选择解耦,
    #      最强空头不再在进池前就被丢掉 (配合 market_scanner BUCKET_ORDER_VERSION
    #      = bearish_conviction_v1 的桶内方向强度排序)。scanner 未提供完整队列时
    #      (旧版 market_scanner) 回退 top_* ⇒ 行为不劣化。
    #   ② 顺序: 资格判定 (价格地板 + 低 ATR 门) 从"取 K **之后**"前置到"取 K
    #      **之前**", 淘汰从剩余合格队列补位 ⇒ 深分析数不再低于预算。
    #   ③ 审计: selected_symbols 记**过滤后**实际提交深分析的名单 (修前记过滤前,
    #      与 Stage3 真正烧 LLM 的集合不符)。
    #   预算边界全部不变: 主池 top_n+2 / extras 全量 / UE ≤ _UE_FEED_CAP / 异动 ≤ 2。
    _reset_screening_cache()
    _screen_batch_id = batch_id or _da_batch_id()
    _pool = []
    if not ue_only:
        for _c in (scan_result.get("bullish_all") or scan_result.get("top_bullish") or []):
            _c = dict(_c); _c["_scan_side"] = "bullish"
            _c["_conviction"] = float(_c.get("score", 5) or 5)
            _pool.append(_c)
        for _c in (scan_result.get("bearish_all") or scan_result.get("top_bearish") or []):
            _c = dict(_c); _c["_scan_side"] = "bearish"
            _c["_conviction"] = 10.0 - float(_c.get("score", 5) or 5)
            _pool.append(_c)
    # 稳定 tie-break: conviction 相同按 (原序, symbol) —— 同分候选不再随重跑换人,
    #   使"实际提交深分析名单"可复现 (checkpoint 命中率与 P4 排序 A/B 归因都依赖它)。
```

## lines 1395-1436 (matched: data_collector)
```python
            _sym = _cand["symbol"]
            try:
                import data_collector, deep_analyzer
                _data = data_collector.collect_all(_sym)
                if _sector_ctx_base:
                    _own = _symbol_sectors.get(_sym, "")
                    _data["sector_context"] = ((f"本股所属板块: {_own}\n" if _own else "")
                                               + _sector_ctx_base)
                if _scan_market_condition:
                    _market_context = _data.get("market_context")
                    if not isinstance(_market_context, dict):
                        _market_context = {}
                    else:
                        _market_context = dict(_market_context)
                    _market_context["scan_condition"] = _scan_market_condition
                    _data["market_context"] = _market_context
                # UE→DA 候选管道: UE 的因果链作为"待验证假设"注入材料, 方向仍由 LLM 独立判定
                if _cand.get("_source") == "ue_candidate" and _cand.get("ue_causal_chain"):
                    _ue_extra = []
                    # §十二 (2026-08-14): 事件驱动必需字段透传 — 催化剂时效/失效条件/期望波幅
                    if _cand.get("ue_horizon_hours"):
                        _ue_extra.append(f"催化剂时效: 约 {_cand['ue_horizon_hours']} 小时 (超时效事件失效, 逻辑不再成立)")
                    if _cand.get("ue_expected_move_pct") is not None:
                        _ue_extra.append(f"UE 期望波幅: {_cand['ue_expected_move_pct']:+.1f}%")
                    if _cand.get("ue_invalidation"):
                        _ue_extra.append(f"UE 失效条件: {_cand['ue_invalidation']}")
                    if _cand.get("ue_self_tier"):
                        _ue_extra.append(f"UE 事件层级: {_cand['ue_self_tier']}")
                    if _cand.get("ue_evidence_citation_state"):
                        _ue_extra.append(f"UE 引证关联: {_cand['ue_evidence_citation_state']}")
                    if _cand.get("ue_confidence_basis"):
                        _ue_extra.append(f"UE 置信依据: {_cand['ue_confidence_basis']}")
                    if _cand.get("ue_counter_evidence"):
                        _ue_extra.append(f"UE 反证: {_cand['ue_counter_evidence']} (必须正面回应)")
                    if _cand.get("ue_event_phase"):
                        _ue_extra.append(f"UE 事件阶段: {_cand['ue_event_phase']}")
                    if _cand.get("ue_move_basis"):
                        _ue_extra.append(f"UE 波幅依据: {_cand['ue_move_basis']}")
                    if _cand.get("ue_critique"):
                        _ue_extra.append(f"UE 自评最弱环: {_cand['ue_critique']} (独立核验此点)")
                    _sc = _cand.get("ue_short_check") or {}
                    if _sc.get("provided"):
```

## lines 2535-2576 (matched: def main)
```python


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Pipeline — 深度分析管线")
    parser.add_argument("--mode", choices=["full", "scan", "analyze"],
                        default="scan", help="运行模式")
    parser.add_argument("--symbol", help="标的代码 (analyze模式)")
    parser.add_argument("--market", default="US")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--top", type=int, default=5)
    parser.add_argument("--symbols", help="额外标的列表 (逗号分隔, 如 US.TSLA,US.NVDA)")
    parser.add_argument("--ue-window", choices=["da_feed", "premarket"], default="da_feed",
                        help="UE候选消费窗口: da_feed=常规早批; premarket=当日新鲜事件增量批")
    parser.add_argument("--fresh-event", action="store_true",
                        help="当日UE新鲜事件增量批: 复用主DA市场上下文, 只分析UE候选")
    parser.add_argument("--fresh-context-dir", default="/opt/ftseek/FTSeek-US/data",
                        help="fresh-event读取主DA opportunity_scan的市场上下文目录")
    parser.add_argument("--format", choices=["json", "text"], default="text")
    parser.add_argument("--synth", choices=["llm", "rule"], default="llm", help="合成模式: llm(默认) | rule(纯规则)")
    # 2026-08-03 A/B 隔离 (防 deepseek 测试污染生产复盘): 三件套 — 文件重定向 + DB写入开关 + pool开关.
    #   完整隔离用法: FTSEEK_DB=/tmp/ab.db FTSEEK_DB_FILE=/tmp/ab.db \
    #     python3.11 pipeline.py --mode full --output-dir /tmp/ab_out --no-db-write --no-pool-write
    #   (FTSEEK_DB → ic_writer 等直连 sqlite3; FTSEEK_DB_FILE → FTSeek db_pool/PlaybookDB)
    parser.add_argument("--output-dir", default="",
                        help="文件输出目录 (bridge/pool/blacklist/sector/gate-jsonl), 默认生产 data/")
    parser.add_argument("--no-db-write", action="store_true",
                        help="跳过 PlaybookDB 指令直写 + IC snapshot + 飞书推送 + Obsidian 归档")
    parser.add_argument("--no-pool-write", action="store_true",
                        help="跳过 daily_active_pool.json 落盘")
    parser.add_argument("--llm-model", default="",
                        help="覆盖分析模型 (默认 config.LLM_MODEL=qwen3.7-max; A/B 用)")
    # 2026-09-07 交易日守卫 (SKILL.md pitfall -6 / handoff「下一步」0-假日, 用户裁"按建议执行"):
    #   cron `0 20 * * 1-5` 只排周末不排美股假日 → 假日照跑会白烧 LLM + 写陈旧指令
    #   + (修复前) 把 IC 快照 trade_date 写成非交易日导致静默丢样。
    #   逃逸口 --force: 人工在假日做单股复盘/调试时用。cron 不传 --force。
    parser.add_argument("--force", action="store_true",
                        help="跳过美股交易日守卫 (假日/周末强制运行; cron 不应使用)")
    args = parser.parse_args()

    if args.llm_model:
        config.LLM_MODEL = args.llm_model
```


# 信号门控 — signal_gate.py 头部

# /root/.hermes/skills/deep-stock-analysis/scripts/signal_gate.py (570 lines)
## lines 1-80
```python
#!/usr/bin/env python3
"""
Signal Gate — 信号质量门禁 + 策略推荐
=====================================
盘前对deep_analysis输出做最终质量检查，基于strategy_memory的经验。

职责:
  1. 信号质量评分：用个股历史+pattern匹配给信号打质量分
  2. 策略推荐：根据赢/亏pattern推荐持仓时长、退出方式
  3. 风险控制：检查方向冲突、极端条件、仓位集中度
  4. 门禁决策：PASS / WARN / BLOCK

不做:
  - 不替代LLM的个股分析（只做质量检查+策略适配）
  - 不碰FTSeek退出系统（只推荐，不修改退出规则）
  - 不做方向门禁（不因为"LONG弱"就一律BLOCK BUY）
"""

import json
import os
import sys
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import strategy_memory


def _load_claim_ic_for_gate() -> Optional[Dict]:
    """读取claim IC缓存供Gate检查论据有效性"""
    try:
        from alpha_backfill import load_claim_ic
        return load_claim_ic()
    except Exception:
        return None


def _load_regime_for_gate() -> Optional[str]:
    """读取当前市场 regime (复用 FTSeek MarketRegimeGate, 与 bridge 同源).

    2026-08-04 接线 (回测实证): 入场侧 score/conf/quality_score 对成交质量
    均无区分度 (WR 50-60% 无单调), 但 regime 有: cautious 61笔 WR=54% PnL=-$341
    是最大亏损源 (risk_off 67% +$15 / UNKNOWN 45% +$83). 故 gate 用 regime 做
    软降权, 替代无效的 score 门槛调整.
    """
    try:
        import sys as _sys
        _sys.path.insert(0, "/opt/ftseek/FTSeek-US")
        from app.playbook.market_regime import MarketRegimeGate as _MRG
        _rc = _MRG().assess()
        return _rc.regime.value
    except Exception:
        return None


def _infer_claim_type(thesis: str, source_type: str, evidence: str = "") -> str:
    """从论点文本推断claim_type（复用ic_writer._classify_claim逻辑）"""
    try:
        from ic_writer import _classify_claim
        return _classify_claim(thesis, source_type, evidence)
    except ImportError:
        return "unclassified"


def evaluate_signal(analysis: Dict[str, Any]) -> Dict[str, Any]:
    """对deep_analysis的输出做质量评估+策略推荐。

    analysis: pipeline analyze的输出dict

    Returns:
        {
            "quality_score": 0-100,
            "decision": "PASS" / "WARN" / "BLOCK",
            "reasons": [str, ...],
            "strategy_hint": {
                "recommended_hold": "short(<2h)" / "mid(2-12h)" / "long(12h+)",
                "recommended_exit": "EOD_FORCE_CLOSE" / "TRAILING_STOP" / ...,
                "position_size_hint": "normal" / "small" / "tiny",
                "risk_notes": [str, ...],
            },
```

## lines 529-570 (matched: def )
```python


def format_gate_report(gate_result: Dict, symbol: str) -> str:
    """格式化gate报告（人类可读）"""
    lines = []
    decision = gate_result["decision"]
    emoji = "✅" if decision == "PASS" else "⚠️" if decision == "WARN" else "🚫"

    lines.append(f"{emoji} Signal Gate: {symbol} | {decision} | 质量分={gate_result['quality_score']}")

    if gate_result["reasons"]:
        lines.append("理由:")
        for r in gate_result["reasons"]:
            lines.append(f"  • {r}")

    hint = gate_result.get("strategy_hint", {})
    if hint:
        lines.append(f"策略推荐: 持仓={hint.get('recommended_hold','')} "
                     f"退出={hint.get('recommended_exit','')} "
                     f"仓位={hint.get('position_size_hint','')}")
        if hint.get("risk_notes"):
            lines.append("风险提示:")
            for r in hint["risk_notes"]:
                lines.append(f"  ⚠️ {r}")

    return "\n".join(lines)


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Signal Gate")
    parser.add_argument("--symbol", required=True)
    parser.add_argument("--action", choices=["summary", "hint"], default="hint")
    args = parser.parse_args()

    if args.action == "hint":
        h = strategy_memory.get_strategy_hint(symbol=args.symbol)
        print(strategy_memory.format_hint_for_prompt(h, symbol=args.symbol))


if __name__ == "__main__":
    main()
```


# DA 配置 — config.py 头部

# /root/.hermes/skills/deep-stock-analysis/scripts/config.py (166 lines)
## lines 1-100
```python
#!/usr/bin/env python3
"""
Config — 所有路径/密钥/模型配置的唯一定义
=================================================
任何模块需要路径或密钥，都从这里取，不硬编码。
改路径只改这一个文件。
"""

import os

# ── Python 环境 ──────────────────────────────────
# futu-api 10.05.6508 在 FTSeek venv 的 python3.11 下可用
PYTHON = "/opt/ftseek/FTSeek-US/venv/bin/python3.11"
OPEND_HOST = "127.0.0.1"
OPEND_PORT = "11111"

# ── 脚本路径 ──────────────────────────────────────
FUTUAPI_SCRIPTS = "/root/.hermes/skills/futuapi/scripts/quote"
ANOMALY_SCRIPTS = {
    "technical": "/root/.hermes/skills/futu-technical-anomaly/scripts/handle_technical_anomaly.py",
    "capital": "/root/.hermes/skills/futu-capital-anomaly/scripts/handle_capital_anomaly.py",
    "derivatives": "/root/.hermes/skills/futu-derivatives-anomaly/scripts/handle_derivatives_anomaly.py",
}
SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPTS_DIR = os.path.join(SKILL_DIR, "scripts")
DATA_DIR = os.path.join(SKILL_DIR, "data")

# ── HTTP API ──────────────────────────────────────
NEWS_BASE = "https://ai-news-search.futunn.com"
FINNHUB_BASE = "https://finnhub.io/api/v1"

# ── FTSeek 环境文件 (密钥来源) ────────────────────
FTSEEK_ENV = "/opt/ftseek/FTSeek-US/.env.us"
# 2026-08-03 (A/B 隔离): FTSEEK_DB 环境变量可覆盖 — A/B 测试指向临时库,
#   避免 ic_writer 等直连 sqlite3 模块污染生产. 默认不变.
FTSEEK_DB = os.environ.get("FTSEEK_DB", "") or "/opt/ftseek/FTSeek-US/data/ftseek_us.db"


def _read_env(key: str, filepath: str = "") -> str:
    """从环境变量或 .env 文件读取密钥"""
    val = os.environ.get(key, "")
    if val:
        return val
    fp = filepath or FTSEEK_ENV
    try:
        with open(fp) as f:
            for line in f:
                if f"{key}=" in line:
                    return line.split("=", 1)[1].strip()
    except Exception:
        pass
    return ""


# ── 密钥 ──────────────────────────────────────────
DASHSCOPE_KEY = _read_env("DASHSCOPE_API_KEY")
FINNHUB_KEY = _read_env("FINNHUB_API_KEY")

# ── LLM 配置 ──────────────────────────────────────
DASHSCOPE_URL = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"
# 2026-08-03 (改进批 任务#3): LLM_MODEL 环境变量可覆盖 — A/B 换模型不再改源码;
#   生产默认 qwen3.7-max 不变 (与 hermes 全局 qwen3.8-max 的口径差异见 handoff).
# 2026-08-13 (统一配置获取): 改走 _read_env — 进程环境变量 > .env.us 文件 > 默认值.
#   修复缺口: os.environ.get 只读进程环境, DA cron(hermes终端,不经systemd)下写
#   .env.us 不生效; 现与密钥类同源, 配置位置统一为 /opt/ftseek/FTSeek-US/.env.us
LLM_MODEL = _read_env("LLM_MODEL") or "qwen3.7-max"
# 2026-08-13 (统一配置获取): 挑战者模型原硬编码在 llm_synth.py, 违背本文件
#   "所有模型配置唯一定义" 契约, 收归此处. 同 LLM_MODEL 模式.
#   分析师=综合策略师, 挑战者=风险审查官(天生偏空, 魔鬼代言人).
# 2026-08-27 (用户裁决): 默认挑战者 deepseek-v4-pro → deepseek-v4-pro-0813
#   (DashScope 日期快照, 固定版本防基别名静默升级)。原诉求 deepseek-v4-pro-0831
#   经 DashScope+官方API 双端实测不存在 (404/400); 0813 为现有唯一 pro 快照。
#   归因不受污染: 每条决策落 challenger_model/challenger_model_reported,
#   可按模型切分新旧样本。回滚: config.py.bak-challenger-0813-20260827。
#   [本节已被 2026-09-14 决定取代 — 保留原文供回滚对照, 现行值见下方赋值行]
# 2026-09-14 (用户裁决): DA 挑战者 deepseek-v4-pro-0813 → deepseek-v4.1-flash.
#   诉求: "DA 和 UE 中 deepseek 版本改 deepseek-v4.1-flash". UE 侧在
#   /opt/ftseek/FTSeek-US/config/unified_config.yaml (新增实体 deepseek-v41-flash,
#   只挂 universe_expansion_reasoning.fallback), 本文件是 DA 侧唯一改动点
#   (全文件仅此一处 deepseek — 已核).
#   ⚠️ 两点与 08-27 决定的口径差异 (记录在案, 非阻塞):
#   ① 无版本快照可 pin. 08-27 特意选日期快照 (-0813) 防"基别名静默升级";
#      DashScope 现无 deepseek-v4.1-flash 的日期快照变体 (实查 /models 只有裸名),
#      故本次无法沿用 pin 策略 — 上游换版对本地不可观测. 代偿: 归因字段仍在
#      (challenger_model/challenger_model_reported), 且 model_reported 回显会让
#      上游偷换暴露在 da_gate_decisions.jsonl 里 (基别名升级会改 reported 值).
#   ② 档位: pro→flash 降档 (v4.1 为 09-10 新代次, 但价/速属 flash 档). 挑战者位
#      的职责是"证伪+反证", 已按生产同构实弹核过不劣化 (见下).
#   生产同构实弹 (2026-09-14, 零生产写入直连 DashScope, 输入 8.0k tok ≈ 生产实测
#   7736-9490, enable_thinking=False + json_object + max_tokens=2000 + temp=0.4):
#     · v4.1-flash: 延迟 7.3s / finish=stop / JSON 合法 / 契约 12 键全在 / 无思考 token
#     · v4-pro-0813(现役): 延迟 16.9s / 同上全过
#     ⇒ 速度 2.3x, 契约满足无退化, 60s timeout 余量充足.
#   陷阱 (留给后来者): 挑战者调用点 (llm_synth.py _run_challenger) 不传 extra_body
#     ⇒ _call_llm 默认 enable_thinking=False ⇒ max_tokens=2000 全留给正文.
#     实弹反证: 若把 enable_thinking 打开, 两模型都会把 2000 全烧进 reasoning
#     (实测 completion=2000 且 reasoning=2000, content 为空 → JSON 解析必败 →
#     静默退化"同模型自我辩论"). 将来若要给挑战者开思考, 必须同时抬 max_tokens
#     (分析师现为 thinking_budget=2048 + max_tokens=6000).
#   回滚: config.py.bak-challenger-v41flash-20260914 (本行改回 -0813 即可).
```

## lines 154-166 (matched: LLM_TEMPERATURE)
```python
#   死配置 → 单一旋钮。llm_synth 三处调用点温度字面量 (0.2 契约修补 / 0.3 分析师 /
#   0.4 挑战者链, 原施工单第4处冷路径已被 9/18 挑战者链重构合并) 全部收口到本键,
#   _call_llm 缺省值同源。运行时回滚: .env.us 写 DA_LLM_TEMPERATURE=0.3, 无需改代码。
#   代码回滚: config.py.bak-temp0-20260921 / llm_synth.py.bak-temp0-20260921。
LLM_TEMPERATURE = float(_read_env("DA_LLM_TEMPERATURE") or "0.0")
LLM_MAX_TOKENS = 3000
LLM_TIMEOUT = 60

# ── 数据时效性 ────────────────────────────────────
DEFAULT_KLINE_DAYS = 60
DEFAULT_ANOMALY_DAYS = 7
DEFAULT_NEWS_SIZE = 10
DEFAULT_SENTIMENT_SIZE = 30
```
