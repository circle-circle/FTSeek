"""
中国大陆新闻源单测 (2026-05-14)

覆盖:
- 财联社 / 华尔街见闻 / 东方财富 解析逻辑 (mock HTTP)
- 中文 ticker 抽取
- 紧急度推断
- 健康看板 import
"""
import time
import pytest


# ─── 财联社快讯 ─────────────────────────────────────────
def test_cailianpress_source_init():
    from app.news_v2.sources.cailianpress_source import CailianPressSource, NewsSource
    s = CailianPressSource(interval_seconds=30)
    assert s.source_id == NewsSource.CAILIANPRESS
    assert s.source_name == "财联社快讯"
    assert s.interval_seconds == 30


def test_cailianpress_extract_tickers_chinese_aliases():
    from app.news_v2.sources.cailianpress_source import CailianPressSource
    s = CailianPressSource()
    # 中文别名
    text = "英伟达发布新一代 GPU, 苹果跌 2%"
    tickers = s._extract_tickers(text)
    assert "NVDA" in tickers
    assert "AAPL" in tickers


def test_cailianpress_extract_tickers_english():
    from app.news_v2.sources.cailianpress_source import CailianPressSource
    s = CailianPressSource()
    text = "NVDA earnings beat, AMD down 3%"
    tickers = s._extract_tickers(text)
    assert "NVDA" in tickers
    assert "AMD" in tickers


def test_cailianpress_urgency_inference():
    from app.news_v2.sources.cailianpress_source import CailianPressSource
    from app.news_v2.models import NewsUrgency
    s = CailianPressSource()
    assert s._infer_urgency("美联储紧急降息 50bp") == NewsUrgency.HIGH
    assert s._infer_urgency("英伟达财报beat预期") == NewsUrgency.NORMAL
    assert s._infer_urgency("某科技公司发布新产品") == NewsUrgency.LOW


def test_cailianpress_parse_with_mock_data():
    from app.news_v2.sources.cailianpress_source import CailianPressSource
    s = CailianPressSource()
    # 模拟财联社返回结构
    telegrams = [
        {
            "id": "test-001",
            "title": "美联储:维持联邦基金利率不变",
            "content": "鲍威尔表示通胀仍是主要担忧",
            "ctime": time.time(),
        },
        {
            "id": "test-002",
            "title": "苹果发布Q4财报 EPS超预期",
            "content": "营收增长10%, 服务业务表现强劲",
            "ctime": time.time(),
        },
        # 不相关的 A 股新闻 (应被过滤)
        {
            "id": "test-003",
            "title": "茅台股价创新高",
            "content": "白酒板块走强",
            "ctime": time.time(),
        },
    ]
    items = s._parse(telegrams)
    # 仅前 2 条被过滤进 (有美股关键字)
    assert len(items) == 2
    assert items[0].source == "cailianpress"
    # AAPL 应在 related_stocks 列表 (格式 US.AAPL)
    aapl_items = [i for i in items if "US.AAPL" in i.related_stocks]
    assert len(aapl_items) >= 1


# ─── 华尔街见闻 ──────────────────────────────────────
def test_wallstreetcn_source_init():
    from app.news_v2.sources.wallstreetcn_source import WallstreetCNSource, NewsSource
    s = WallstreetCNSource(interval_seconds=60)
    assert s.source_id == NewsSource.WALLSTREETCN
    assert "us-stock" in s._channels


def test_wallstreetcn_parse_articles():
    from app.news_v2.sources.wallstreetcn_source import WallstreetCNSource
    s = WallstreetCNSource()
    articles = [
        {
            "resource": {
                "id": 12345,
                "title": "美联储议息会议鹰派表态 美股全线下挫",
                "content_short": "鲍威尔讲话后, 道指跌 300 点, 纳指跌 1.5%",
                "display_time": int(time.time()),
            }
        },
    ]
    items = s._parse_articles(articles, "fed")
    assert len(items) == 1
    from app.news_v2.models import NewsUrgency
    assert items[0].urgency == NewsUrgency.HIGH  # fed channel 强制 HIGH


# ─── 东方财富 ────────────────────────────────────────
def test_eastmoney_source_init():
    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource, NewsSource
    s = EastmoneyUSSource(interval_seconds=120, symbols=["NVDA", "AAPL"])
    assert s.source_id == NewsSource.EASTMONEY_US
    assert s._symbols == ["NVDA", "AAPL"]


def test_eastmoney_update_symbols():
    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource
    s = EastmoneyUSSource(symbols=["NVDA"])
    s.update_symbols(["NVDA", "AAPL", "TSLA"])
    assert len(s._symbols) == 3


def test_eastmoney_parse_ts():
    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource
    s = EastmoneyUSSource()
    # ISO 字符串
    ts = s._parse_ts("2026-05-14 10:30:00")
    assert ts is not None
    # Unix int
    ts2 = s._parse_ts(1715651400)
    assert ts2 == 1715651400
    # 毫秒
    ts3 = s._parse_ts(1715651400000)
    assert ts3 == 1715651400


def test_eastmoney_parse_articles_strip_html():
    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource
    s = EastmoneyUSSource(symbols=["NVDA"])
    # 用 now 附近的时间戳, 避免 24h 过滤
    articles = [
        {
            "Url": "https://example.com/1",
            "Title": "<em>NVDA</em> 财报<em>超预期</em>",
            "Content": "营收同比增长50%",
            "ShowTime": time.time() - 600,  # 10 min ago
        }
    ]
    items = s._parse_articles(articles, "NVDA")
    assert len(items) == 1
    # HTML 标记被剥离
    assert "<em>" not in items[0].title
    assert "NVDA 财报超预期" in items[0].title
    assert "US.NVDA" in items[0].related_stocks


def test_eastmoney_filter_old_news():
    """24h 之前的新闻应被过滤"""
    from app.news_v2.sources.eastmoney_us_source import EastmoneyUSSource
    s = EastmoneyUSSource(symbols=["NVDA"])
    old_ts = time.time() - 86400 * 2  # 2 天前
    articles = [
        {
            "Url": "https://example.com/old",
            "Title": "Very old news",
            "Content": "stale",
            "ShowTime": old_ts,
        }
    ]
    items = s._parse_articles(articles, "NVDA")
    assert len(items) == 0


# ─── 新闻源在 NewsSource 枚举中注册 ────────────────────
def test_new_source_ids_registered():
    from app.news_v2.models import NewsSource
    assert NewsSource.CAILIANPRESS == "cailianpress"
    assert NewsSource.WALLSTREETCN == "wallstreetcn"
    assert NewsSource.EASTMONEY_US == "eastmoney_us"


# ─── 健康看板可 import ──────────────────────────────
def test_health_dashboard_importable():
    """scripts/news_sources_health.py 可正常 import (无 syntax error)"""
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "news_sources_health", "scripts/news_sources_health.py"
    )
    mod = importlib.util.module_from_spec(spec)
    # 不实际运行 main(), 仅验证模块 load
    spec.loader.exec_module(mod)
    assert hasattr(mod, "cmd_snapshot")
    assert hasattr(mod, "cmd_probe")


# ─── yaml 配置 ────────────────────────────────────────
def test_yaml_cn_sources_default_disabled():
    """国产源默认 disabled, 模拟盘验证后再开"""
    import yaml
    with open("config/unified_config.yaml") as f:
        cfg = yaml.safe_load(f)
    cn = cfg.get("news_sources", {}).get("cn", {})
    assert "cailianpress" in cn
    assert "wallstreetcn" in cn
    assert "eastmoney_us" in cn
    # 默认全 disabled
    for name, c in cn.items():
        assert c.get("enabled") is False, f"{name} 默认应 disabled"
