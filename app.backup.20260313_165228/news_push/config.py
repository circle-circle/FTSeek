"""
金融新闻推送系统配置
"""
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "news_push" / "config"
CONFIG_DIR.mkdir(exist_ok=True)

# ============================================================
#  新闻源配置
# ============================================================

# RSS 源列表 (来自 Situation-aggregation-monitoring)
RSS_FEEDS = {
    # 财经类 (高优先级)
    "finance": [
        "https://feed.mix.sina.com.cn/api/roll/get?page=1&page_size=20",
        "https://api-one.wallstcn.com/apiv1/contents?limit=20&category=global",
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",
    ],
    
    # 快讯类 (最高优先级)
    "flash": [
        "https://www.cls.cn/api/telegraph",  # 财联社电报
        "https://www.blockbeats.com/api/news/flash",  # BlockBeats 快讯
    ],
    
    # 宏观/政策类
    "macro": [
        "https://www.federalreserve.gov/feeds/press.xml",
        "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&output=atom",
    ],
    
    # 科技类 (默认关闭)
    "tech": [
        "https://news.ycombinator.com/rss",
        "https://arstechnica.com/feed/",
    ],
}

# 来源优先级 (数字越小优先级越高)
SOURCE_PRIORITY = {
    "cls": 1,           # 财联社 (最高)
    "blockbeats": 2,    # BlockBeats
    "wallstreetcn": 3,  # 华尔街见闻
    "sina": 4,          # 新浪财经
    "cnbc": 5,          # CNBC
    "sec": 2,           # SEC 公告
    "fed": 3,           # 美联储
    "hkex": 2,          # 港交所
}

# 分类开关
CATEGORY_ENABLED = {
    "finance": True,    # 财经类推送
    "flash": True,      # 快讯推送
    "macro": True,      # 宏观政策
    "tech": False,      # 科技类 (默认不推送)
    "crypto": False,    # 加密货币 (默认不推送)
}

# ============================================================
#  推送规则配置
# ============================================================

# 关键词过滤
PUSH_KEYWORDS = {
    # 必须包含的关键词 (任一匹配即推送)
    "must_include": [
        "财报", "业绩", "盈利", "亏损", "营收", "净利润",
        "收购", "重组", "合并", "分拆", "IPO",
        "增持", "减持", "回购", "分红",
        "监管", "调查", "诉讼", "处罚",
        "突破", "新高", "新低", "暴涨", "暴跌",
        "earnings", "revenue", "profit", "loss",
        "acquisition", "merger", "IPO", "buyback",
    ],
    
    # 必须排除的关键词 (任一匹配即过滤)
    "must_exclude": [
        "广告", "推广", "赞助", "合作",
        "广告", "营销", "宣传",
    ],
}

# 股票代码监控列表
WATCH_STOCKS = json.loads(os.getenv("NEWS_WATCH_STOCKS", """
[
    {"code": "HK.00700", "name": "腾讯控股", "enabled": true},
    {"code": "HK.09988", "name": "阿里巴巴", "enabled": true},
    {"code": "HK.09992", "name": "泡泡玛特", "enabled": true},
    {"code": "US.AAPL", "name": "苹果", "enabled": true},
    {"code": "US.TSLA", "name": "特斯拉", "enabled": true},
    {"code": "US.NVDA", "name": "英伟达", "enabled": true},
    {"code": "US.MSFT", "name": "微软", "enabled": true}
]
"""))

# 股票代码别名映射 (用于新闻内容匹配)
STOCK_ALIASES = {
    "HK.00700": ["腾讯", "Tencent", "0700", "00700.HK"],
    "HK.09988": ["阿里", "Alibaba", "9988", "09988.HK", "阿里巴巴"],
    "HK.09992": ["泡泡玛特", "Pop Mart", "9992"],
    "US.AAPL": ["苹果", "Apple", "AAPL"],
    "US.TSLA": ["特斯拉", "Tesla", "TSLA"],
    "US.NVDA": ["英伟达", "NVIDIA", "NVDA", "伟达"],
    "US.MSFT": ["微软", "Microsoft", "MSFT"],
}

# ============================================================
#  频率控制配置
# ============================================================

# 推送限流
RATE_LIMIT = {
    "max_per_hour": int(os.getenv("NEWS_MAX_PER_HOUR", "5")),      # 每小时最多 5 条
    "max_per_day": int(os.getenv("NEWS_MAX_PER_DAY", "30")),        # 每日最多 30 条
    "cooldown_seconds": int(os.getenv("NEWS_COOLDOWN_SECONDS", "300")),  # 冷却时间 5 分钟
    "merge_window_seconds": int(os.getenv("NEWS_MERGE_WINDOW", "60")),   # 合并窗口 60 秒
}

# ============================================================
#  大模型配置 (Qwen 系列)
# ============================================================

QWEN_CONFIG = {
    "enabled": os.getenv("QWEN_ENABLED", "false").lower() == "true",
    "api_key": os.getenv("QWEN_API_KEY", ""),
    "endpoint": os.getenv("QWEN_ENDPOINT", "https://dashscope.aliyuncs.com/api/v1"),
    "model": os.getenv("QWEN_MODEL", "qwen-max"),  # qwen-max / qwen-plus / qwen-turbo
    "max_tokens": int(os.getenv("QWEN_MAX_TOKENS", "500")),
    "temperature": float(os.getenv("QWEN_TEMPERATURE", "0.3")),
}

# ============================================================
#  钉钉推送配置
# ============================================================

DINGTALK_WEBHOOK_URL = os.getenv("DINGTALK_WEBHOOK_URL", "")
DINGTALK_SECRET = os.getenv("DINGTALK_SECRET", "")

# 推送时间控制 (避免打扰)
PUSH_TIME_WINDOW = {
    "start_hour": int(os.getenv("PUSH_START_HOUR", "9")),    # 9:00 开始
    "end_hour": int(os.getenv("PUSH_END_HOUR", "22")),       # 22:00 结束
    "weekend_push": os.getenv("WEEKEND_PUSH", "false").lower() == "true",  # 周末是否推送
}

# ============================================================
#  缓存配置
# ============================================================

CACHE_DIR = BASE_DIR / "data" / "news_cache"
CACHE_DIR.mkdir(exist_ok=True)

CACHE_CONFIG = {
    "ttl_seconds": int(os.getenv("NEWS_CACHE_TTL", "3600")),  # 缓存 1 小时
    "max_items": int(os.getenv("NEWS_CACHE_MAX", "1000")),    # 最多缓存 1000 条
}

# ============================================================
#  日志配置
# ============================================================

LOG_LEVEL = os.getenv("NEWS_LOG_LEVEL", "INFO")
LOG_FILE = BASE_DIR / "logs" / "news_push.log"

# ============================================================
#  数据目录 (导出给其他模块使用)
# ============================================================

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)

# ============================================================
#  存储模块专项配置
# ============================================================

# 存储模块关键词
STORAGE_KEYWORDS = [
    "存储芯片", "DRAM", "NAND Flash", "HBM", "存储模块",
    "美光", "三星", "海力士", "长江存储", "长鑫存储",
    "兆易创新", "北京君正", "西部数据", "闪迪", "SK 海力士",
    "storage", "memory chip", "semiconductor", "NAND",
]

# 存储模块相关股票
STORAGE_STOCKS = [
    "US.MU",      # 美光科技
    "US.WDC",     # 西部数据
    "US.STX",     # 希捷科技
    "HK.00981",   # 中芯国际
    "SH.603986",  # 兆易创新
    "SZ.300223",  # 北京君正
    "SZ.002156",  # 通富微电
]

# 存储模块新闻单独推送开关
STORAGE_NEWS_PUSH = True
