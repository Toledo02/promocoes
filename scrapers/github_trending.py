"""GitHub trending repositories.

Vivia dentro do scraper de gaming, mas o conteúdo alimenta a seção de Tecnologia do jornal —
o payload agora bate com a seção que o consome.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from bs4 import BeautifulSoup

from core.utils import BROWSER_HEADERS, ScraperResult, http_get_text

logger = logging.getLogger(__name__)


async def fetch(settings) -> ScraperResult:
    section = "github_trending"
    github_cfg = settings.get("github") or {}
    url = github_cfg.get("trending_url", "https://github.com/trending")
    max_repos = int(github_cfg.get("max_repos", 5))

    try:
        html = await http_get_text(url, settings, headers=BROWSER_HEADERS)
    except Exception as exc:
        logger.warning("GitHub trending fetch failed: %s", exc)
        return ScraperResult(section=section, status="error", error=str(exc))

    soup = BeautifulSoup(html, "lxml")
    repos: list[dict[str, str]] = []

    for article in soup.select("article.Box-row"):
        title_el = article.select_one("h2 a")
        if not title_el:
            continue

        # O <a> traz dono e repositório em nós separados, o que produzia "dono /repo".
        name = re.sub(r"\s+", "", title_el.get_text())
        description = article.select_one("p")

        repos.append(
            {
                "name": name,
                "url": f"https://github.com{title_el.get('href', '')}",
                "description": description.get_text(strip=True) if description else "",
            }
        )
        if len(repos) >= max_repos:
            break

    if not repos:
        return ScraperResult(
            section=section, status="error", error="Nenhum repositório extraído (layout mudou?)"
        )

    return ScraperResult(section=section, status="ok", data={"repos": repos})
