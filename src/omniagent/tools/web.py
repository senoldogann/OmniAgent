"""Web ve haber araması için sağlayıcıdan bağımsız yardımcılar."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import re
from typing import Any, Callable, Dict, List, Optional, Tuple

from .types import ToolError


SearchClientFactory = Callable[[], Any]


def search_web(
    query: str,
    category: str = "auto",
    freshness_days: Optional[int] = None,
    *,
    client_factory: SearchClientFactory,
) -> str:
    """Metin/haber araması yapar; güncel sorgularda haber yolunu ve metin fallback'ini kullanır."""
    cleaned_query = query.strip()
    if not cleaned_query:
        raise ToolError("Web arama sorgusu boş olamaz.", "INVALID_QUERY", False)

    normalized_category = category.strip().casefold()
    if normalized_category not in {"auto", "text", "news"}:
        raise ToolError(f"Geçersiz web arama kategorisi: {category}", "INVALID_QUERY", False)
    if freshness_days is not None and not 1 <= freshness_days <= 30:
        raise ToolError("freshness_days 1-30 arasında olmalı.", "INVALID_QUERY", False)

    inferred_days = freshness_days
    if inferred_days is None:
        match = re.search(
            r"(?:last\s+(\d+)\s+days?|son\s+(\d+)\s+g[üu]n)",
            cleaned_query,
            re.IGNORECASE,
        )
        if match:
            inferred_days = int(next(group for group in match.groups() if group is not None))

    looks_like_news = bool(
        re.search(
            r"\b(?:news|breaking|latest|haber(?:ler|leri)?|g[üu]ndem)\b",
            cleaned_query,
            re.IGNORECASE,
        )
    ) or inferred_days is not None
    mode = (
        "news"
        if normalized_category == "news"
        or (normalized_category == "auto" and looks_like_news)
        else "text"
    )
    timelimit = (
        "d"
        if inferred_days is not None and inferred_days <= 1
        else "w"
        if inferred_days is not None and inferred_days <= 7
        else "m"
        if inferred_days is not None
        else None
    )

    search_query = cleaned_query
    if mode == "news":
        search_query = re.sub(
            r"(?:last\s+\d+\s+days?|son\s+\d+\s+g[üu]n)",
            " ",
            search_query,
            flags=re.IGNORECASE,
        )
        search_query = re.sub(
            r"\b(?:summary|özet|ozet)\b", " ", search_query, flags=re.IGNORECASE,
        )
        if inferred_days is not None:
            search_query = re.sub(
                r"\b(?:january|february|march|april|may|june|july|august|september|"
                r"october|november|december|ocak|şubat|subat|mart|nisan|mayıs|mayis|"
                r"haziran|temmuz|ağustos|agustos|eylül|eylul|ekim|kasım|kasim|aralık|aralik)"
                r"\s+\d{1,2}(?:\s*[-–]\s*\d{1,2})?(?:st|nd|rd|th)?[,]?\s+\d{4}\b",
                " ",
                search_query,
                flags=re.IGNORECASE,
            )
            search_query = re.sub(
                r"\b20\d{2}[-/.]\d{1,2}[-/.]\d{1,2}\b", " ", search_query,
            )
        search_query = re.sub(r"\s+", " ", search_query).strip() or cleaned_query

    def fresh_news(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        if inferred_days is None:
            return items
        cutoff = datetime.now(timezone.utc) - timedelta(days=inferred_days)
        fresh: List[Tuple[datetime, Dict[str, Any]]] = []
        undated: List[Dict[str, Any]] = []
        for item in items:
            raw_date = str(item.get("date", "")).strip()
            if not raw_date:
                undated.append(item)
                continue
            try:
                moment = datetime.fromisoformat(raw_date.replace("Z", "+00:00"))
                if moment.tzinfo is None:
                    moment = moment.replace(tzinfo=timezone.utc)
                else:
                    moment = moment.astimezone(timezone.utc)
            except ValueError:
                undated.append(item)
                continue
            if moment >= cutoff:
                fresh.append((moment, item))
        fresh.sort(key=lambda pair: pair[0], reverse=True)
        return [item for _, item in fresh] or undated

    try:
        with client_factory() as client:
            if mode == "news":
                kwargs: Dict[str, Any] = {
                    "max_results": 12,
                    "backend": "bing,duckduckgo,yahoo",
                }
                if timelimit is not None:
                    kwargs["timelimit"] = timelimit
                results = fresh_news(list(client.news(search_query, **kwargs)))
                if not results:
                    results = list(client.text(search_query, **kwargs))
            else:
                kwargs = {"max_results": 8, "backend": "auto"}
                if timelimit is not None:
                    kwargs["timelimit"] = timelimit
                results = list(client.text(search_query, **kwargs))
    except Exception as error:
        raise ToolError(
            f"Web araması başarısız: {error}", "WEB_SEARCH_FAILED", True
        ) from error

    if not results:
        raise ToolError(
            f"Web araması boş sonuç döndü: {cleaned_query}", "WEB_SEARCH_EMPTY", True
        )

    clipped = []
    for result in results:
        url = str(result.get("url") or result.get("href") or "")
        clipped.append(
            {
                "title": str(result.get("title", "")),
                "url": url,
                "href": url,
                "body": str(result.get("body", ""))[:400],
                "date": str(result.get("date", "")),
                "source": str(result.get("source", "")),
            }
        )
    return json.dumps(clipped, ensure_ascii=False, indent=2)
