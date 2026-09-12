"""Unified async RSS collector driven by config categories."""

from __future__ import annotations

import logging
import re
import unicodedata
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Any

import feedparser

from core.utils import ScraperResult, http_get, strip_html

logger = logging.getLogger(__name__)


def _normalize_title(title: str) -> str:
    """Chave de deduplicação: minúsculas, sem acento e sem pontuação.

    Sem remover acento, a mesma notícia sindicalizada em dois portais escapa da deduplicação
    por uma diferença de grafia.
    """
    folded = unicodedata.normalize("NFKD", title.lower().strip())
    folded = "".join(char for char in folded if not unicodedata.combining(char))
    return re.sub(r"[^\w\s]", "", re.sub(r"\s+", " ", folded))


def _entry_datetime(entry: Any) -> datetime | None:
    """Data de publicação em UTC, ou None se o feed não informar."""
    for key in ("published_parsed", "updated_parsed"):
        parsed = entry.get(key)
        if not parsed:
            continue
        try:
            return datetime(*parsed[:6], tzinfo=timezone.utc)
        except (TypeError, ValueError):
            continue
    return None


async def _fetch_feed(
    url: str, max_entries: int, max_age_hours: int, settings
) -> list[dict[str, Any]]:
    response = await http_get(url, settings)
    parsed = feedparser.parse(response.content)

    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)
    items: list[dict[str, Any]] = []
    stale = 0

    for entry in parsed.entries:
        if len(items) >= max_entries:
            break

        published = _entry_datetime(entry)
        # Um feed parado há dias não deve contribuir notícia velha para o jornal *matinal*.
        # Entradas sem data são mantidas: não dá para provar que estão velhas.
        if published and published < cutoff:
            stale += 1
            continue

        items.append(
            {
                "title": entry.get("title", "").strip(),
                "link": entry.get("link", ""),
                "published": entry.get("published", entry.get("updated", "")),
                "summary": strip_html(entry.get("summary", ""))[:400],
                "source_feed": url,
            }
        )

    if stale:
        logger.debug("%s: %s entradas descartadas por idade (> %sh)", url, stale, max_age_hours)
    return items


def _interleave(per_feed: list[list[dict[str, Any]]], max_items: int) -> list[dict[str, Any]]:
    """Intercala os feeds em round-robin: um item de cada por rodada.

    Truncar a lista concatenada faria os primeiros feeds consumirem todos os slots — foi assim
    que TabNews e CNN Brasil ficaram fora do jornal sem ninguém perceber.
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in zip_longest(*per_feed):
        for item in row:
            if item is None:
                continue
            key = _normalize_title(item.get("title", ""))
            if not key or key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= max_items:
                return items
    return items


async def fetch(settings, category: str = "tech") -> ScraperResult:
    section = f"{category}_news" if category in {"tech", "world"} else category
    rss_cfg = settings.get("rss") or {}
    feeds = (settings.get("rss_feeds") or {}).get(category, [])

    if not feeds:
        return ScraperResult(
            section=section,
            status="error",
            error=f"No RSS feeds configured for category '{category}'",
        )

    entries_per_feed = int(rss_cfg.get("entries_per_feed", 5))
    max_items = int(rss_cfg.get("max_items_per_category", 15))
    max_age_hours = int(rss_cfg.get("max_age_hours", 30))

    per_feed: list[list[dict[str, Any]]] = []
    errors: list[str] = []
    empty_feeds: list[str] = []

    for feed_url in feeds:
        try:
            items = await _fetch_feed(feed_url, entries_per_feed, max_age_hours, settings)
            if items:
                per_feed.append(items)
            else:
                empty_feeds.append(feed_url)
        except Exception as exc:
            logger.warning("RSS fetch failed for %s: %s", feed_url, exc)
            errors.append(f"{feed_url}: {exc}")

    # Um feed que responde 200 mas não entrega nada falha tão silenciosamente quanto um 404.
    if empty_feeds:
        errors.append(f"sem itens recentes: {', '.join(empty_feeds)}")

    all_items = _interleave(per_feed, max_items)

    if not all_items:
        return ScraperResult(section=section, status="error", error="; ".join(errors) or "No RSS entries")

    by_feed = {}
    for item in all_items:
        by_feed[item["source_feed"]] = by_feed.get(item["source_feed"], 0) + 1
    logger.info("%s: %s itens de %s feeds %s", section, len(all_items), len(per_feed), by_feed)

    status = "partial" if errors else "ok"
    return ScraperResult(
        section=section,
        status=status,
        data={"category": category, "items": all_items, "count": len(all_items)},
        error="; ".join(errors) if errors else None,
    )
