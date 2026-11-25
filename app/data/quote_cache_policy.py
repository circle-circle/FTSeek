"""Request-local data cache publication policy; ordinary calls remain unchanged."""
from contextvars import ContextVar


DATA_CACHE_PUBLICATION_ALLOWED: ContextVar[bool] = ContextVar(
    "data_cache_publication_allowed", default=True,
)
