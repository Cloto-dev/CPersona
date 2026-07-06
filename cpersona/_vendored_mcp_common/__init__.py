"""Common utilities shared across Magic Gateway Protocol (MGP) Python servers.

Foundation layer (Phase 2 Step 2-α) ported from
``cloto-mcp-servers/servers/common/``:

- :mod:`mcp_common.validation` — graceful-degradation argument validators
  (``validate_bool`` / ``validate_str`` / ``validate_int`` / ``validate_dict`` /
  ``validate_float`` / ``validate_list``).
- :mod:`mcp_common.isolation` — γ-semantics two-tier isolation helpers
  shared by CPersona & CScheduler (``gamma_clause`` / ``coerce_for_*`` /
  ``extract_axes_for_*``).
- :mod:`mcp_common.no_persist` — session no-persist mode helpers
  (``pause`` / ``resume`` / ``status`` / ``is_paused`` /
  ``make_skipped_response``).
- :mod:`mcp_common.mgp_utils` — MGP capability builder and stream-notification
  emitters (``MgpCapabilities`` / ``send_mgp_stream_chunk`` /
  ``write_mgp_*``).

Network / cache layer (Phase 2 Step 2-β):

- :mod:`mcp_common.embedding_client` — vector embedding client with TTL-based
  LRU cache for single-text queries (``EmbeddingClient`` /
  ``pack_embedding`` / ``unpack_embedding``).
- :mod:`mcp_common.semantic_cache` — in-memory semantic cache that matches
  cached results via embedding similarity (``SemanticCache``).
- :mod:`mcp_common.search` — search provider abstraction with a SearXNG →
  Tavily → DuckDuckGo fallback chain (``SearchProvider`` /
  ``SearXNGProvider`` / ``TavilyProvider`` / ``DuckDuckGoProvider`` /
  ``ChainProvider`` / ``create_search_provider``).

MCP SDK tooling (Phase 2 Step 2-γ):

- :mod:`mcp_common.mcp_utils` — decorator-based MCP tool registration
  with auto-validated parameter extraction (``ToolRegistry`` /
  ``ToolRegistry.tool`` / ``ToolRegistry.auto_tool`` /
  ``run_mcp_server`` / ``install_mgp_validation_filter``).

MGP streaming middleware (Phase 2 Step 2-δ):

- :mod:`mcp_common.mcp_stream_interceptor` — async pump that sits between
  the raw stdio read stream and the MCP ``ServerSession``, intercepting
  the custom MGP §12.7 ``mgp/stream/cancel`` requests and §12.9
  ``notifications/mgp.stream.gap`` notifications that would otherwise be
  dropped by the SDK's closed Pydantic unions (``mgp_message_interceptor``
  / ``_handle_mgp_cancel`` / ``_handle_mgp_gap``).

LLM provider + streaming (Phase 2 Step 2-ε):

- :mod:`mcp_common.llm_provider` — OpenAI-compatible HTTP client with
  SSE streaming, MGP §12 stream support (``mgp/stream/cancel`` /
  ``notifications/mgp.stream.gap`` retransmission), tool-call orchestration,
  and the ``create_llm_mcp_server`` / ``run_server`` entry points
  (``ProviderConfig`` / ``StreamState`` / ``LlmApiError`` /
  ``call_llm_api_streaming`` / ``handle_think_with_tools`` /
  ``handle_think_with_tools_streaming`` / ``load_llm_provider_config``).
  This module owns the ``StreamState`` dataclass referenced by the
  ``TYPE_CHECKING`` block in :mod:`mcp_common.mcp_stream_interceptor`;
  the forward reference left in Step 2-δ now resolves cleanly.

Phase 2 module migration from ``cloto-mcp-servers/servers/common/`` is
complete with this sub-PR.
"""

__version__ = "0.5.2"
