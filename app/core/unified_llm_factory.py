#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
FTSeek Unified LLM Factory
SSOT Pattern: Single client configuration and lifecycle management
"""
import os
import sys
import logging
from typing import Optional, Dict, Any

from openai import AsyncOpenAI

logger = logging.getLogger(__name__)

# Singleton instance
_client_instance: Optional[AsyncOpenAI] = None
_client_config: Dict[str, Any] = {}

def get_llm_client(config: Optional[Dict] = None) -> AsyncOpenAI:
    """
    Get the unified LLM client (SSOT).
    Reads from provided config dict or falls back to unified_config.yaml.
    
    Args:
        config: Configuration dict containing 'llm' section
        
    Returns:
        Configured AsyncOpenAI client
    """
    global _client_instance, _client_config
    
    # Check if we need to reinitialize
    if config and config != _client_config:
        _initialize_client(config)
    elif not _client_instance:
        # Load from YAML if no config provided
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            from config.config_center import get_config_center
            config_center = get_config_center()
            cfg = config_center.get('llm', {})
            if cfg:
                _initialize_client({'llm': cfg})
        except Exception as e:
            logger.debug(f"ConfigCenter not available, using env defaults: {e}")
            if not _client_instance:
                _initialize_client({})
    
    return _client_instance

def _initialize_client(config: Dict[str, Any]):
    """Internal initialization logic"""
    global _client_instance, _client_config
    
    llm_cfg = config.get('llm', {})
    
    # SSOT: Read from config, fallback to env vars for compatibility
    # 支持环境变量占位符 (如 ${BAILIAN_API_KEY})
    def _resolve_env(val):
        if isinstance(val, str) and val.startswith('${') and val.endswith('}'):
            env_name = val[2:-1]
            return os.environ.get(env_name, val)
        return val
    
    api_key = _resolve_env(llm_cfg.get('api_key', '')) or os.environ.get('BAILIAN_API_KEY', '')
    
    if not api_key:
        logger.warning("LLM API Key not configured, client will fail on first call")
        api_key = 'placeholder'  # Allow instantiation, fail later with clear error
    
    endpoint = _resolve_env(llm_cfg.get('endpoint', ''))
    if not endpoint:
        # DashScope default
        endpoint = os.environ.get('BAILIAN_ENDPOINT', 'https://dashscope.aliyuncs.com/compatible-mode/v1')

    # Normalize: strip trailing /chat/completions if present, SDK appends its own
    base_url = endpoint.rstrip('/')
    if base_url.endswith('/chat/completions'):
        base_url = base_url[:-len('/chat/completions')]

    try:
        _client_instance = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
        )
        _client_config = config
        logger.info(f"✅ Unified LLM Client initialized: {endpoint}")
    except Exception as e:
        logger.error(f"❌ Failed to initialize Unified LLM Client: {e}")
        raise

def get_llm_config() -> Dict[str, Any]:
    """Get the current LLM configuration"""
    try:
        from config.config_center import get_config_center
        config_center = get_config_center()
        return config_center.get('llm', {})
    except:
        return {}
