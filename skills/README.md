# FTSeek 每日复盘技能

**位置**: `/opt/ftseek/FTSeek/skills/ftseek-daily-review/`

## 使用方式

```bash
cd /opt/ftseek/FTSeek

# 今日复盘
python3 skills/ftseek-daily-review/scripts/run_review.py

# 指定日期
python3 skills/ftseek-daily-review/scripts/run_review.py --date 2026-03-24

# JSON输出
python3 skills/ftseek-daily-review/scripts/run_review.py --output json
```

## 技能结构

```
/opt/ftseek/FTSeek/skills/ftseek-daily-review/
├── SKILL.md              # 技能说明
├── scripts/
│   └── run_review.py     # 复盘脚本
└── references/
    └── analysis_guide.md # 分析指南
```

## 功能

- 盈亏汇总分析
- 问题根因诊断
- 优化建议生成
- 待办事项跟踪