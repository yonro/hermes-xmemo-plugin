"""Prefetch preserves its baseline contract even for time-related prompts."""
from unittest.mock import MagicMock

import pytest

from test_proactive_tools import XMemoMemoryProvider
from hermes_xmemo.xmemo.cache import XMemoLocalCache


# Executed HEAD (30fe1bc) queue_prefetch + cache.py to obtain these golden values.
@pytest.mark.parametrize("query,golden", [
    ("Summarize last week decisions", "dde70373e9be0222a48a94c91e24257e94810973e5aadbed95a23445dfe3bfad"),
    ("Summarize yesterday decisions", "4bcbb5cdf48c03650385571813d6a04c1121c49907514653e475adfa8ccae5ba"),
    ("总结上周的项目决策", "215113d889e4d34d20cd95839cf1d615e4484b24a8d39c2ce6b9288cb9511ac9"),
])
def test_prefetch_no_temporal_inference(tmp_path, query, golden):
    provider = XMemoMemoryProvider()
    provider._config = {"api_key": "test", "prefetch_max_items": 3, "prefetch_max_tokens": 1200}
    cache = XMemoLocalCache(tmp_path / "prefetch.sqlite")
    provider._local_cache = MagicMock(wraps=cache)
    client = MagicMock()
    client.recall_context.return_value = {"context_text": "prefetched", "items": []}
    provider._get_client = lambda: client
    provider.queue_prefetch(query, session_id="test")
    threads = list(provider._prefetch_threads.values())
    assert len(threads) == 1
    threads[0].join(timeout=5)
    assert not threads[0].is_alive()
    client.recall_context.assert_called_once_with(
        query=query, bucket="%", scope=None, max_items=3, max_tokens=1200, prefer_working=True,
    )
    params = {"bucket": "%", "scope": None, "memory_type": "auto", "max_items": 3,
              "max_tokens": 1200, "prefer_working": True, "base_url": "https://xmemo.dev"}
    provider._local_cache.put_cached_recall.assert_called_once_with(
        "recall_context", query, params, client.recall_context.return_value,
    )
    assert cache._hash_signature("recall_context", query, params) == golden
    assert cache.get_cached_recall("recall_context", query, params)["context_text"] == "prefetched"
    assert "prefetched" in provider.prefetch(query, session_id="test")
