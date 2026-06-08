"""
search.py — Web search via DuckDuckGo (free, no API key).

Public API:
    web_search(query, max_results=5)  →  dict with 'results' list or 'error'

Each result: {"title": str, "url": str, "snippet": str}
"""

_MAX = 5
_TIMEOUT = 10


def web_search(query: str, max_results: int = _MAX) -> dict:
    """
    Search the web and return up to max_results results.
    Returns {"results": [...]} or {"error": "..."}.
    Each result has keys: title, url, snippet.
    """
    if not query or not query.strip():
        return {"error": "Empty search query."}
    try:
        from ddgs import DDGS
    except ImportError:
        return {"error": "Search package not installed (pip install ddgs)."}

    try:
        results = []
        with DDGS() as ddgs:
            for r in ddgs.text(query.strip(), max_results=max_results):
                results.append({
                    "title":   r.get("title", ""),
                    "url":     r.get("href",  ""),
                    "snippet": r.get("body",  ""),
                })
        if not results:
            return {"results": [], "note": "No results found."}
        return {"results": results}
    except Exception as exc:
        return {"error": f"Search failed: {type(exc).__name__}: {exc}"}
