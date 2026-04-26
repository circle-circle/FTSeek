"""
金融新闻推送系统配置
"""
import os
import json
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
CONFIG_DIR = BASE_DIR / "news_push" / "config"
CONFIG_DIR.mkdir(exist_ok=True)

# 加载 .env 文件（确保 QWEN_ENABLED 等环境变量被读取）
try:
    from dotenv import load_dotenv as _load_dotenv
    _env_path = BASE_DIR.parent / '.env'
    if _env_path.exists():
        _load_dotenv(_env_path)
except ImportError:
    pass

# ============================================================
#  新闻源配置（2026-03-23 更新：替换失效源，补充可访问源）
#
#  实测状态：
#   ✅ CNBC (Markets/Asia/Finance), WSJ (Markets/Intl), FED, AAStocks EN
#   ❌ Reuters (网络不可达), HKEX RSS (404已下线), SCMP (网络不可达)
#   ❌ Sina 港股 (404), Eastmoney (无响应)
# ============================================================

# RSS 源列表
RSS_FEEDS = {
    # 财经类 (高优先级) - 国际
    "finance": [
        "https://www.cnbc.com/id/100003114/device/rss/rss.html",  # CNBC Markets ✅
        "https://www.cnbc.com/id/10000664/device/rss/rss.html",   # CNBC Finance ✅
        "https://feeds.a.dj.com/rss/RSSMarketsMain.xml",          # WSJ Markets ✅
        "https://feeds.a.dj.com/rss/RSSWorldNews.xml",            # WSJ World ✅
    ],

    # 宏观/政策类
    "macro": [
        "https://www.federalreserve.gov/feeds/press_all.xml",     # 美联储 ✅
    ],

    # 快讯类（原路透社已不可达，改用 CNBC Asia）
    "flash": [
        "https://www.cnbc.com/id/100727362/device/rss/rss.html",  # CNBC Asia ✅
        # Reuters 已从大陆服务器不可访问，已移除
        # "https://feeds.reuters.com/reuters/businessNews",  # ❌ 网络不可达
        # "https://feeds.reuters.com/reuters/topNews",       # ❌ 网络不可达
    ],

    # 港股专项（原 HKEX RSS 已下线，改用 AAStocks）
    "hk_stocks": [
        "https://www.aastocks.com/en/stocks/news/aafn-news-headline.aspx",  # AAStocks ✅
        # HKEX RSS 已 404，已移除
        # "https://www.hkex.com.hk/chi/newsconsul/hkexnews/listco/rss/listcorss_c.xml",  # ❌ 404
        # "https://rss.scmp.com/rss/13",          # ❌ 网络不可达
    ],

    # 港股财经（原东方财富/新浪港股无响应，暂时禁用）
    "hk_finance": [
        # "https://feed.eastmoney.com/news/1,10001,ajaxformat.html",  # ❌ 无响应
        # "https://rss.sina.com.cn/finance/hkstock/hkstocknews.xml",  # ❌ 404
    ],

    # 富途快讯 (RSSHub) - 2026-04-18 增加备用节点
    # 主节点: rsshub.rssforever.com (经常超时)
    # 备用 1: rsshub.ktachibana.party
    # 备用 2: rss.shab.fun
    "futu_flash": [
        "https://rsshub.rssforever.com/futunn/live",
        "https://rsshub.ktachibana.party/futunn/live",
        # "https://rss.shab.fun/futunn/live",  # ❌ 已失效 (DNS 解析失败)
    ],

    # 科技类 (默认关闭)
    "tech": [],
}

# 来源优先级 (数字越小优先级越高)
SOURCE_PRIORITY = {
    "cls": 1,           # 财联社 (最高)
    "blockbeats": 2,    # BlockBeats
    "hkex": 1,          # 港交所 (最高)
    "10jqka": 2,        # 同花顺
    "tencent": 2,       # 腾讯财经
    "eastmoney": 2,     # 东方财富
    "wallstreetcn": 3,  # 华尔街见闻
    "sina": 3,          # 新浪财经
    "reuters": 3,       # 路透社（当前不可达，保留配置）
    "scmp": 3,          # 南华早报（当前不可达，保留配置）
    "aastocks": 2,      # 阿斯达克（港股专项，提升至2）
    "cnbc": 4,          # CNBC（通用）
    "cnbc_asia": 3,     # CNBC Asia（亚太专项，高于通用）
    "sec": 2,           # SEC 公告
    "fed": 3,           # 美联储
    "hkex_news": 2,     # 港交所新闻
    "wsj": 4,           # 华尔街日报
    "futu_rsshub": 2,   # 富途快讯 (RSSHub) - 2026-04-12 新增，高优先级
}

# 分类开关
CATEGORY_ENABLED = {
    "finance": True,    # 财经类推送
    "flash": True,      # 快讯推送
    "macro": True,      # 宏观政策
    "hk_stocks": True,  # 港股专项 (开启)
    "hk_finance": False, # 港股财经 (禁用，源已失效)
    "futu_flash": True, # 富途快讯 (开启) - 2026-04-12 新增
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
        # 中文财报/业绩
        "财报", "业绩", "盈利", "亏损", "营收", "净利润", "利润",
        "收购", "重组", "合并", "分拆", "IPO", "上市",
        "增持", "减持", "回购", "分红", "派息",
        "监管", "调查", "诉讼", "处罚",
        "突破", "新高", "新低", "暴涨", "暴跌",
        "港交所", "公告", "停牌", "复牌",
        # 港股专项
        "美团", "泡泡玛特", "中芯", "腾讯", "阿里",
        # 英文
        "earnings", "revenue", "profit", "loss",
        "acquisition", "merger", "buyback",
        "Meituan", "SMIC", "semiconductor",
    ],
    
    # 必须排除的关键词 (任一匹配即过滤)
    "must_exclude": [
        "广告", "推广", "赞助", "营销", "宣传",
    ],
}

# 股票代码监控列表
WATCH_STOCKS = json.loads(os.getenv("NEWS_WATCH_STOCKS", """
[
    {"code": "HK.00700", "name": "腾讯控股", "enabled": true, "aliases": ["腾讯", "00700", "Tencent", "马化腾", "微信"]},
    {"code": "HK.09988", "name": "阿里巴巴", "enabled": true, "aliases": ["阿里", "09988", "Alibaba", "马云", "淘宝"]},
    {"code": "HK.09992", "name": "泡泡玛特", "enabled": true, "aliases": ["泡泡玛特", "09992", "POP MART"]},
    {"code": "US.AAPL", "name": "苹果", "enabled": true, "aliases": ["苹果", "AAPL", "iPhone", "Mac"]},
    {"code": "US.TSLA", "name": "特斯拉", "enabled": true, "aliases": ["特斯拉", "TSLA", "Elon Musk", "马斯克"]},
    {"code": "US.NVDA", "name": "英伟达", "enabled": true, "aliases": ["英伟达", "NVDA", "NVIDIA", "黄仁勋"]},
    {"code": "US.MSFT", "name": "微软", "enabled": true, "aliases": ["微软", "MSFT", "Microsoft", "Windows", "Office"]}
]
"""))

# 股票别名映射 (用于新闻匹配)
STOCK_ALIASES = {}
for stock in WATCH_STOCKS:
    if stock.get('enabled', True):
        STOCK_ALIASES[stock['code']] = stock.get('aliases', []) + [stock['name'], stock['code']]

# 股票代码别名映射 (用于新闻内容匹配)
STOCK_ALIASES = {
    # ─── 港股 - 互联网/电商 (6只) ───
    "HK.00700": ["腾讯", "Tencent", "0700", "00700", "00700.HK", "腾讯控股", "微信", "WeChat"],
    "HK.09988": ["阿里", "Alibaba", "9988", "09988", "09988.HK", "阿里巴巴", "淘宝", "BABA"],
    "HK.03690": ["美团", "Meituan", "3690", "03690", "03690.HK", "外卖", "美团点评", "王兴"],
    "HK.09618": ["京东", "JD", "9618", "09618", "09618.HK", "京东集团", "刘强东"],
    "HK.01024": ["快手", "Kuaishou", "1024", "01024", "01024.HK", "快手科技", "Kwai"],
    "HK.09888": ["百度", "Baidu", "9888", "09888", "09888.HK", "百度集团", "李彦宏", "文心一言"],

    # ─── 港股 - 半导体/芯片 (3只) ───
    "HK.00981": ["中芯国际", "SMIC", "981", "00981", "00981.HK", "中芯", "中芯半导体"],
    "HK.01347": ["华虹半导体", "Hua Hong", "1347", "01347", "01347.HK", "华虹", "华虹宏力"],
    "HK.02382": ["舜宇光学", "Sunny Optical", "2382", "02382", "02382.HK", "舜宇", "舜宇科技"],

    # ─── 港股 - 新能源车/汽车 (4只) ───
    "HK.01810": ["小米", "Xiaomi", "1810", "01810", "01810.HK", "小米集团", "雷军", "小米汽车"],
    "HK.01211": ["比亚迪", "BYD", "1211", "01211", "01211.HK", "比亚迪股份", "王传福"],
    "HK.02015": ["理想汽车", "Li Auto", "2015", "02015", "02015.HK", "理想", "李想"],
    "HK.09868": ["小鹏汽车", "Xpeng", "9868", "09868", "09868.HK", "小鹏", "何小鹏"],

    # ─── 港股 - 金融/保险 (4只) ───
    "HK.02318": ["中国平安", "Ping An", "2318", "02318", "02318.HK", "平安", "平安保险", "马明哲"],
    "HK.01299": ["友邦保险", "AIA", "1299", "01299", "01299.HK", "友邦", "AIA集团"],
    "HK.00388": ["港交所", "HKEx", "388", "00388", "00388.HK", "香港交易所", "HKEX", "陈翊庭"],
    "HK.00939": ["建设银行", "CCB", "939", "00939", "00939.HK", "建行", "建设银行", "CCB"],

    # ─── 港股 - 医药/生物 (3只) ───
    "HK.02269": ["药明生物", "WuXi Biologics", "2269", "02269", "02269.HK", "药明", "WuXi"],
    "HK.01801": ["信达生物", "Innovent", "1801", "01801", "01801.HK", "信达", "Innovent Bio"],
    "HK.06618": ["京东健康", "JD Health", "6618", "06618", "06618.HK", "京东健康"],

    # ─── 港股 - 消费/零售 (4只) ───
    "HK.09992": ["泡泡玛特", "Pop Mart", "9992", "09992", "09992.HK", "POPMART", "泡泡"],
    "HK.02020": ["安踏", "ANTA", "2020", "02020", "02020.HK", "安踏体育", "丁世忠"],
    "HK.01876": ["百威亚太", "Budweiser APAC", "1876", "01876", "01876.HK", "百威", "百威啤酒"],
    "HK.09926": ["康方生物", "Akeso", "9926", "09926", "09926.HK", "康方", "Akeso Bio"],

    # ─── 港股 - 能源/资源 (3只) ───
    "HK.00883": ["中海油", "CNOOC", "883", "00883", "00883.HK", "中国海洋石油", "CNOOC"],
    "HK.00857": ["中石油", "PetroChina", "857", "00857", "00857.HK", "中国石油", "CNPC"],
    "HK.01378": ["中国宏桥", "China Hongqiao", "1378", "01378", "01378.HK", "宏桥", "中国铝"],

    # ─── 港股 - AI/游戏/旅游 (3只) ───
    "HK.00772": ["阅文集团", "China Literature", "772", "00772", "00772.HK", "阅文", "ChinaLit", "起点"],
    "HK.09961": ["携程", "Trip.com", "9961", "09961", "09961.HK", "携程集团", "梁建章", "Trip"],
    "HK.00708": ["恒大汽车", "Evergrande Auto", "708", "00708", "00708.HK", "恒驰", "恒大汽车"],

    # ─── 美股 (跨市场映射) ───
    "US.AAPL": ["苹果", "Apple", "AAPL", "iPhone", "Mac"],
    "US.TSLA": ["特斯拉", "Tesla", "TSLA", "Elon Musk", "马斯克"],
    "US.NVDA": ["英伟达", "NVIDIA", "NVDA", "黄仁勋"],
    "US.MSFT": ["微软", "Microsoft", "MSFT", "Windows", "Office"],
    "US.BABA": ["阿里巴巴", "BABA", "Alibaba US", "阿里巴巴美股"],
    "US.TCEHY": ["腾讯", "TCEHY", "Tencent ADR", "腾讯美股"],
    "US.JD": ["京东", "JD", "JD.com", "京东美股"],
    "US.NIO": ["蔚来", "NIO", "蔚来汽车", "李斌"],
    "US.LI": ["理想", "LI", "Li Auto US", "理想美股"],
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
#  钉钉推送配置 (统一配置源 2026-04-13 修复)
#  
#  P2 修复: 优先使用 app/config.py 的统一配置源，避免多套配置不一致
#  如果 app/config.py 不可用，回退到环境变量
# ============================================================

def _get_dingtalk_webhook():
    """统一获取钉钉 Webhook URL"""
    try:
        from app import config as main_config
        if getattr(main_config, 'DINGTALK_WEBHOOK_URL', ''):
            return main_config.DINGTALK_WEBHOOK_URL
    except Exception:
        pass
    return os.getenv("DINGTALK_WEBHOOK_URL", "")

def _get_dingtalk_secret():
    """统一获取钉钉 Secret"""
    try:
        from app import config as main_config
        if getattr(main_config, 'DINGTALK_SECRET', ''):
            return main_config.DINGTALK_SECRET
    except Exception:
        pass
    return os.getenv("DINGTALK_SECRET", "")

DINGTALK_WEBHOOK_URL = _get_dingtalk_webhook()
DINGTALK_SECRET = _get_dingtalk_secret()

# 推送时间控制 (避免打扰)
PUSH_TIME_WINDOW = {
    "start_hour": int(os.getenv("PUSH_START_HOUR", "9")),    # 9:00 开始
    "end_hour": int(os.getenv("PUSH_END_HOUR", "22")),       # 22:00 结束
    "weekend_push": os.getenv("WEEKEND_PUSH", "false").lower() == "true",  # 周末是否推送
}

# ============================================================
#  缓存配置 (2026-03-17 优化：缩短 TTL 提升时效性)
# ============================================================

CACHE_DIR = BASE_DIR / "data" / "news_cache"
CACHE_DIR.mkdir(exist_ok=True)

CACHE_CONFIG = {
    "ttl_seconds": int(os.getenv("NEWS_CACHE_TTL", "300")),  # 缓存 5 分钟 (原 1 小时)
    "max_items": int(os.getenv("NEWS_CACHE_MAX", "1000")),    # 最多缓存 1000 条
}

# 新闻采集间隔配置 (新增 2026-03-17)
COLLECTION_INTERVAL = {
    "fast_mode": 60,       # 快速模式: 1 分钟采集一次 (交易时段)
    "normal_mode": 300,    # 正常模式: 5 分钟采集一次
    "slow_mode": 900,      # 慢速模式: 15 分钟采集一次 (非交易时段)
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
