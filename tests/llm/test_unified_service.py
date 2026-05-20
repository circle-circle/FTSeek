"""
LLM 模块单元测试 (UnifiedLLMService + 新增模块)

覆盖:
1. UnifiedLLMService 配置加载、路由、缓存、预算
2. json_schema_guard 校验逻辑
3. feedback_loop 数据读写
4. quality_evaluator 评分逻辑
5. prompt_ab_testing 版本管理
"""

import asyncio
import json
import os
import sys
import tempfile
import time
from pathlib import Path
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

# 确保项目路径在 sys.path
BASE_DIR = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BASE_DIR))


# ============================================================
# 1. UnifiedLLMService 测试
# ============================================================

class TestUnifiedLLMService:
    """UnifiedLLMService 配置加载、路由、缓存、预算"""

    def test_init_defaults(self):
        """默认初始化"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            svc = UnifiedLLMService()
            assert svc.default_model == "qwen3.6-plus"
            assert svc.default_timeout == 120
            assert svc.max_retries == 2

    def test_yaml_config_loading(self):
        """YAML 配置加载: feature_routes, fallback_chain, budget, cache"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            svc = UnifiedLLMService()
            
            # feature_routes 应从 unified_config.yaml 加载
            assert "premarket_planner" in svc.feature_routes
            assert svc.feature_routes["premarket_planner"] == "qwen3.6-plus"
            assert "news_classifier" in svc.feature_routes
            assert svc.feature_routes["news_classifier"] == "qwen3.6-flash"
            
            # fallback_chain 应合并 (YAML 覆盖默认)
            assert "qwen3.6-plus" in svc.FALLBACK_CHAIN
            assert isinstance(svc.FALLBACK_CHAIN["qwen3.6-plus"], list)
            
            # budget 配置
            assert svc.budget_enabled is True
            assert svc.budget_daily_max > 0
            
            # cache 配置
            assert svc.cache_enabled is True
            assert svc.cache_ttl > 0

    def test_cache_key_consistency(self):
        """相同输入应生成相同 cache_key"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            svc = UnifiedLLMService()
            
            messages = [{"role": "user", "content": "test prompt"}]
            key1 = svc._cache_key(messages, "qwen3.6-plus", 0.3)
            key2 = svc._cache_key(messages, "qwen3.6-plus", 0.3)
            key3 = svc._cache_key(messages, "qwen3.6-flash", 0.3)
            
            assert key1 == key2, "相同输入应生成相同 key"
            assert key1 != key3, "不同模型应生成不同 key"

    def test_cache_put_get(self):
        """缓存读写: 写入后应能读取"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            with tempfile.TemporaryDirectory() as tmpdir:
                from app.llm.unified_service import UnifiedLLMService
                svc = UnifiedLLMService()
                svc.cache_dir = Path(tmpdir)
                svc.cache_dir.mkdir(parents=True, exist_ok=True)
                
                key = "test_cache_key_abc123"
                value = '{"result": "cached response"}'
                
                # 写入
                svc._cache_put(key, value)
                
                # 读取
                result = svc._cache_get(key)
                assert result == value

    def test_cache_expiry(self):
        """缓存过期: TTL 后应返回 None"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            with tempfile.TemporaryDirectory() as tmpdir:
                from app.llm.unified_service import UnifiedLLMService
                svc = UnifiedLLMService()
                svc.cache_dir = Path(tmpdir)
                svc.cache_ttl = 1  # 1秒 TTL
                
                key = "test_expiry_key"
                value = "test_value"
                
                svc._cache_put(key, value)
                assert svc._cache_get(key) == value  # 立即可读
                
                time.sleep(1.5)  # 等待过期
                assert svc._cache_get(key) is None  # 应过期

    def test_budget_check(self):
        """预算检查: 超限时应拒绝调用"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            svc = UnifiedLLMService()
            svc.budget_enabled = True
            svc.budget_daily_max = 1.0  # 1元上限
            
            # Mock _get_daily_cost 返回超限
            with patch.object(svc, '_get_daily_cost', return_value=1.5):
                assert svc._budget_exceeded() is True
            
            # Mock _get_daily_cost 返回正常
            with patch.object(svc, '_get_daily_cost', return_value=0.5):
                assert svc._budget_exceeded() is False

    def test_extract_json_valid(self):
        """JSON 提取: 有效 JSON"""
        from app.llm.unified_service import UnifiedLLMService
        raw = '{"key": "value", "num": 42}'
        result = UnifiedLLMService.extract_json(raw)
        assert result == {"key": "value", "num": 42}

    def test_extract_json_markdown_wrapped(self):
        """JSON 提取: markdown 代码块包裹"""
        from app.llm.unified_service import UnifiedLLMService
        raw = '```json\n{"key": "value"}\n```'
        result = UnifiedLLMService.extract_json(raw)
        assert result == {"key": "value"}

    def test_extract_json_with_prefix(self):
        """JSON 提取: 前缀文本"""
        from app.llm.unified_service import UnifiedLLMService
        raw = '这里是分析结果: {"score": 0.85, "action": "buy"}'
        result = UnifiedLLMService.extract_json(raw)
        assert result == {"score": 0.85, "action": "buy"}

    def test_extract_json_invalid(self):
        """JSON 提取: 无效 JSON 应返回空 dict"""
        from app.llm.unified_service import UnifiedLLMService
        raw = "这不是 JSON"
        result = UnifiedLLMService.extract_json(raw)
        assert result == {}

    def test_model_pricing(self):
        """模型定价表完整性"""
        from app.llm.unified_service import UnifiedLLMService
        assert "qwen3.6-plus" in UnifiedLLMService.MODEL_PRICING
        assert "qwen3.6-flash" in UnifiedLLMService.MODEL_PRICING
        assert "deepseek-v4-pro" in UnifiedLLMService.MODEL_PRICING
        
        for model, (inp, out) in UnifiedLLMService.MODEL_PRICING.items():
            assert inp >= 0, f"{model} input price should be non-negative"
            assert out >= 0, f"{model} output price should be non-negative"

    @pytest.mark.asyncio
    async def test_call_task_type_routing(self):
        """task_type 路由: premarket_planner 应使用 qwen3.6-plus"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            svc = UnifiedLLMService()
            svc.cache_enabled = False  # 禁用缓存
            
            # Mock _http_call 避免真实 API 调用，同时捕获调用参数
            captured_model = None
            
            async def mock_http_call(model, messages, timeout, temperature, max_tokens):
                nonlocal captured_model
                captured_model = model
                return ('{"result": "ok"}', {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            
            svc._http_call = mock_http_call
            
            messages = [{"role": "user", "content": "test routing"}]
            result = await svc.call(messages, task_type="premarket_planner")
            
            # 验证 _http_call 被调用时使用了正确的模型
            assert captured_model == "qwen3.6-plus", f"Expected qwen3.6-plus, got {captured_model}"


# ============================================================
# 2. json_schema_guard 测试
# ============================================================

class TestJsonSchemaGuard:
    """JSON Schema 校验逻辑"""

    def test_parse_valid_json(self):
        """校验有效 JSON"""
        from app.llm.json_schema_guard import parse_and_validate
        
        raw = '{"targets": [{"code": "00700"}], "score": 0.9}'
        schema = {"targets": list, "score": (int, float)}
        
        result = parse_and_validate(raw, schema)
        assert result is not None
        assert result["targets"] == [{"code": "00700"}]
        assert result["score"] == 0.9

    def test_parse_missing_field(self):
        """校验: 缺少必填字段"""
        from app.llm.json_schema_guard import parse_and_validate
        
        raw = '{"score": 0.9}'
        schema = {"targets": list}  # targets 缺失
        
        result = parse_and_validate(raw, schema)
        assert result is None

    def test_parse_wrong_type(self):
        """校验: 类型不匹配"""
        from app.llm.json_schema_guard import parse_and_validate
        
        raw = '{"targets": "not a list", "score": 0.9}'
        schema = {"targets": list}
        
        result = parse_and_validate(raw, schema)
        assert result is None

    def test_parse_invalid_json(self):
        """校验: 无效 JSON"""
        from app.llm.json_schema_guard import parse_and_validate
        
        raw = "not json at all"
        schema = {"targets": list}
        
        result = parse_and_validate(raw, schema)
        assert result is None

    def test_parse_with_markdown_wrapper(self):
        """校验: markdown 代码块包裹"""
        from app.llm.json_schema_guard import parse_and_validate
        
        raw = '```json\n{"targets": [1, 2, 3]}\n```'
        schema = {"targets": list}
        
        result = parse_and_validate(raw, schema)
        assert result is not None
        assert result["targets"] == [1, 2, 3]


# ============================================================
# 3. feedback_loop 测试
# ============================================================

class TestFeedbackLoop:
    """WinRateFeedbackLoop 数据读写"""

    def test_import_and_init(self):
        """导入和初始化"""
        from app.llm.feedback_loop import WinRateFeedbackLoop, get_feedback_loop
        
        loop = WinRateFeedbackLoop()
        assert loop is not None
        
        singleton = get_feedback_loop()
        assert singleton is not None

    def test_get_feedback_context_empty(self):
        """无历史数据时应返回空或 None"""
        from app.llm.feedback_loop import get_feedback_loop
        
        loop = get_feedback_loop()
        ctx = loop.get_feedback_context("NONEXIST_CODE", direction="long", days=30)
        # 无数据时返回空字符串或 None
        assert ctx is None or ctx == ""

    def test_get_historical_stats_empty(self):
        """无历史数据时应返回 None 或空"""
        from app.llm.feedback_loop import get_feedback_loop
        
        loop = get_feedback_loop()
        stats = loop.get_historical_stats("NONEXIST_CODE")
        assert stats is None or stats == {}


# ============================================================
# 4. quality_evaluator 测试
# ============================================================

class TestQualityEvaluator:
    """LLMQualityEvaluator 评分逻辑"""

    def test_import_and_init(self):
        """导入和初始化"""
        from app.llm.quality_evaluator import LLMQualityEvaluator, get_quality_evaluator
        
        ev = LLMQualityEvaluator()
        assert ev is not None
        
        singleton = get_quality_evaluator()
        assert singleton is not None

    def test_get_quality_trend(self):
        """质量趋势: 应返回日期+分数列表"""
        from app.llm.quality_evaluator import get_quality_evaluator
        
        ev = get_quality_evaluator()
        trend = ev.get_quality_trend(days=7)
        assert isinstance(trend, list)
        # 即使无数据也应返回结构化的空列表
        for entry in trend:
            assert "date" in entry
            assert "quality_score" in entry

    def test_get_model_comparison(self):
        """模型对比: 应返回各模型统计"""
        from app.llm.quality_evaluator import get_quality_evaluator
        
        ev = get_quality_evaluator()
        cmp = ev.get_model_comparison()
        assert isinstance(cmp, dict)
        # 如果有数据，应包含统计字段
        for model, stats in cmp.items():
            assert "calls" in stats
            assert "success_rate" in stats


# ============================================================
# 5. prompt_ab_testing 测试
# ============================================================

class TestPromptABTesting:
    """ABTestManager 版本管理"""

    def test_import_and_init(self):
        """导入和初始化"""
        from app.llm.prompt_ab_testing import ABTestManager
        
        mgr = ABTestManager()
        assert mgr is not None

    def test_register_and_get_version(self):
        """注册版本并获取"""
        from app.llm.prompt_ab_testing import ABTestManager
        
        mgr = ABTestManager()
        mgr.register_version(
            task_type="test_strategy",
            version_id="v1",
            content="system: you are a test analyst",
        )
        
        content = mgr.get_version_content("test_strategy", "v1")
        assert content == "system: you are a test analyst"

    def test_get_all_stats(self):
        """获取所有版本统计"""
        from app.llm.prompt_ab_testing import ABTestManager
        
        mgr = ABTestManager()
        stats = mgr.get_all_stats()
        assert isinstance(stats, dict)

    def test_register_duplicate(self):
        """重复注册同一版本"""
        from app.llm.prompt_ab_testing import ABTestManager
        
        mgr = ABTestManager()
        mgr.register_version(
            task_type="test_dup",
            version_id="v1",
            content="version 1",
        )
        # 再次注册应覆盖或忽略
        mgr.register_version(
            task_type="test_dup",
            version_id="v1",
            content="version 1 updated",
        )
        
        content = mgr.get_version_content("test_dup", "v1")
        # 应返回最新内容
        assert content == "version 1 updated" or content == "version 1"


# ============================================================
# 6. UnifiedLLMService + 新模块集成测试
# ============================================================

class TestIntegration:
    """端到端集成测试 (带 Mock)"""

    @pytest.mark.asyncio
    async def test_call_and_parse_with_schema(self):
        """call_and_parse: 带 schema 校验"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            
            svc = UnifiedLLMService()
            svc.cache_enabled = False  # 禁用缓存避免跨测试污染
            
            # Mock _http_call 返回有效 JSON
            mock_response = (
                '{"strategies": [{"code": "00700", "direction": "buy"}], "market_overview": "偏多", "overall_bias": "偏多", "risk_level": "低"}',
                {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}
            )
            
            with patch.object(svc, '_http_call', new_callable=AsyncMock, return_value=mock_response):
                messages = [{"role": "user", "content": "test prompt"}]
                schema = {"strategies": list, "market_overview": str}
                
                result = await svc.call_and_parse(
                    messages=messages,
                    task_type="premarket_planner",
                    schema_required=schema,
                )
                
                assert "strategies" in result
                assert isinstance(result["strategies"], list)
                assert result["market_overview"] == "偏多"

    @pytest.mark.asyncio
    async def test_call_and_parse_invalid_json(self):
        """call_and_parse: 无效 JSON 应返回空 dict"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            
            svc = UnifiedLLMService()
            svc.cache_enabled = False  # 禁用缓存避免跨测试污染
            
            mock_response = ("这不是 JSON", {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
            
            with patch.object(svc, '_http_call', new_callable=AsyncMock, return_value=mock_response):
                messages = [{"role": "user", "content": "test invalid json"}]
                result = await svc.call_and_parse(messages=messages)
                assert result == {}

    @pytest.mark.asyncio
    async def test_call_budget_rejection(self):
        """call: 预算超限时应返回 None"""
        with patch.dict(os.environ, {"BAILIAN_API_KEY": "test-key"}):
            from app.llm.unified_service import UnifiedLLMService
            
            svc = UnifiedLLMService()
            svc.budget_enabled = True
            svc.budget_daily_max = 1.0
            svc.cache_enabled = False  # 禁用缓存避免跨测试污染
            
            with patch.object(svc, '_get_daily_cost', return_value=2.0):
                messages = [{"role": "user", "content": "test"}]
                result = await svc.call(messages)
                assert result is None


# ============================================================
# 运行入口
# ============================================================

if __name__ == "__main__":
    pytest.main([__file__, "-v", "--tb=short"])
