"""
Shared search provider abstraction for Cloto MCP servers.
Fallback chain: SearXNG (self-hosted) → Tavily (cloud API) → DuckDuckGo (zero-config).
"""

import os
import sys
from abc import ABC, abstractmethod

import httpx

# ============================================================
# Configuration (read once at import time)
# ============================================================

PROVIDER = os.environ.get("CLOTO_SEARCH_PROVIDER", "auto")
SEARXNG_URL = os.environ.get("SEARXNG_URL", "http://localhost:8080")
TAVILY_API_KEY = os.environ.get("TAVILY_API_KEY", "")
REQUEST_TIMEOUT = int(os.environ.get("CLOTO_SEARCH_TIMEOUT", "15"))


# ============================================================
# Provider Abstraction
# ============================================================


class SearchProvider(ABC):
    name: str = "unknown"

    @abstractmethod
    async def search(
        self,
        query: str,
        max_results: int,
        language: str,
        time_range: str | None,
    ) -> list[dict]: ...


class SearXNGProvider(SearchProvider):
    """Self-hosted SearXNG — no API key, unlimited queries, full privacy."""

    name = "searxng"

    def __init__(self, base_url: str):
        self.base_url = base_url.rstrip("/")
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def search(
        self,
        query: str,
        max_results: int,
        language: str,
        time_range: str | None,
    ) -> list[dict]:
        params: dict = {
            "q": query,
            "format": "json",
            "pageno": 1,
            "language": language,
        }
        if time_range:
            params["time_range"] = time_range

        resp = await self.client.get(f"{self.base_url}/search", params=params)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for r in data.get("results", [])[:max_results]:
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                }
            )
        return results


class TavilyProvider(SearchProvider):
    """Tavily — AI-optimized search, 1000 free queries/month."""

    name = "tavily"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self.client = httpx.AsyncClient(timeout=REQUEST_TIMEOUT)

    async def aclose(self) -> None:
        await self.client.aclose()

    async def search(
        self,
        query: str,
        max_results: int,
        language: str,
        time_range: str | None,
    ) -> list[dict]:
        payload: dict = {
            "query": query,
            "max_results": max_results,
            "api_key": self.api_key,
        }
        if time_range:
            day_map = {"day": 1, "week": 7, "month": 30, "year": 365}
            if time_range in day_map:
                payload["days"] = day_map[time_range]

        resp = await self.client.post("https://api.tavily.com/search", json=payload)
        resp.raise_for_status()
        data = resp.json()

        results = []
        for r in data.get("results", [])[:max_results]:
            results.append(
                {
                    "title": r.get("title", ""),
                    "url": r.get("url", ""),
                    "snippet": r.get("content", ""),
                }
            )
        return results


class DuckDuckGoProvider(SearchProvider):
    """DuckDuckGo via HTML scraping — zero-config, no API key, no external deps.

    The `ddgs` package (v9+) switched its backend to Brave Search, breaking
    DuckDuckGo functionality. This provider scrapes DuckDuckGo's HTML endpoint
    directly, which is stable and does not require any third-party library.
    """

    name = "duckduckgo"

    async def search(
        self,
        query: str,
        max_results: int,
        language: str,
        time_range: str | None,
    ) -> list[dict]:
        import re
        from html import unescape
        from urllib.parse import parse_qs, urlparse

        params: dict = {"q": query}
        if language and language != "en":
            params["kl"] = language

        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT, follow_redirects=True, proxy=None
        ) as client:
            resp = await client.get(
                "https://html.duckduckgo.com/html/",
                params=params,
                headers={"User-Agent": "Mozilla/5.0 (compatible; ClotoCore/0.6)"},
            )
            resp.raise_for_status()

        html = resp.text
        results: list[dict] = []

        # Parse result blocks: <a class="result__a" href="...">title</a>
        for match in re.finditer(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>',
            html,
            re.DOTALL,
        ):
            if len(results) >= max_results:
                break
            raw_url = unescape(match.group(1))
            title = re.sub(r"<[^>]+>", "", match.group(2)).strip()
            title = unescape(title)

            # Resolve DuckDuckGo redirect URLs (//duckduckgo.com/l/?uddg=<actual_url>)
            url = raw_url
            if "uddg=" in raw_url:
                parsed = parse_qs(urlparse(raw_url).query)
                if "uddg" in parsed:
                    url = parsed["uddg"][0]

            # Extract snippet from nearby result__snippet
            snippet = ""
            snippet_match = re.search(
                r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
                html[match.end() : match.end() + 2000],
                re.DOTALL,
            )
            if snippet_match:
                snippet = re.sub(r"<[^>]+>", "", snippet_match.group(1)).strip()
                snippet = unescape(snippet)

            if url and url.startswith("http"):
                results.append({"title": title, "url": url, "snippet": snippet})

        return results


class ChainProvider(SearchProvider):
    """Try providers in order, falling back on failure."""

    name = "chain"

    def __init__(self, providers: list[SearchProvider]):
        self.providers = providers

    async def aclose(self) -> None:
        for p in self.providers:
            if hasattr(p, "aclose"):
                await p.aclose()

    async def search(
        self,
        query: str,
        max_results: int,
        language: str,
        time_range: str | None,
    ) -> list[dict]:
        last_error: Exception | None = None
        for p in self.providers:
            try:
                return await p.search(query, max_results, language, time_range)
            except Exception as e:
                print(f"Provider {p.name} failed: {e}", file=sys.stderr)
                last_error = e
        raise last_error or RuntimeError("No search providers available")


def create_search_provider() -> SearchProvider:
    """Build provider (or chain) from CLOTO_SEARCH_PROVIDER env var.

    Supported values:
      "auto"    — SearXNG → Tavily (if key set) → DuckDuckGo
      "searxng" — SearXNG only
      "tavily"  — Tavily only
      "ddg"     — DuckDuckGo only
    """
    if PROVIDER == "auto":
        chain: list[SearchProvider] = [SearXNGProvider(SEARXNG_URL)]
        if TAVILY_API_KEY:
            chain.append(TavilyProvider(TAVILY_API_KEY))
        chain.append(DuckDuckGoProvider())
        return ChainProvider(chain)
    elif PROVIDER == "searxng":
        return SearXNGProvider(SEARXNG_URL)
    elif PROVIDER == "tavily":
        if not TAVILY_API_KEY:
            print("WARNING: TAVILY_API_KEY not set, search will fail", file=sys.stderr)
        return TavilyProvider(TAVILY_API_KEY)
    elif PROVIDER == "ddg":
        return DuckDuckGoProvider()
    else:
        raise ValueError(f"Unknown search provider: {PROVIDER}")
