"""Shared utilities for scrapers and orchestrator."""

from __future__ import annotations

import html
import logging
import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

import httpx

TIMEZONE = ZoneInfo("America/Sao_Paulo")

# APIs públicas atrás de Cloudflare recusam User-Agent genérico ou disfarçado de navegador e
# exigem identificação do cliente com um contato.
USER_AGENT = "AchadosBot/1.0 (+https://github.com/Toledo02/Promocoes)"
DEFAULT_HEADERS = {"User-Agent": USER_AGENT}

# Alvos de scraping de HTML fazem o oposto: tendem a recusar tráfego declaradamente automatizado.
# A prévia do Telegram (t.me/s/<canal>) é um deles.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
BROWSER_HEADERS = {"User-Agent": BROWSER_USER_AGENT}


@dataclass
class ScraperResult:
    section: str
    status: str  # ok | partial | error
    data: dict[str, Any] = field(default_factory=dict)
    error: str | None = None

    def to_payload(self) -> dict[str, Any]:
        payload = dict(self.data)
        if self.status == "error":
            payload["_error"] = self.error or "Unknown error"
        elif self.status == "partial" and self.error:
            payload["_warning"] = self.error
        return payload


def setup_logging(settings) -> logging.Logger:
    log_cfg = settings.logging_config
    level_name = log_cfg.get("level", "INFO")
    level = getattr(logging, level_name.upper(), logging.INFO)
    log_dir = settings.project_root / log_cfg.get("directory", "logs")
    log_dir.mkdir(parents=True, exist_ok=True)

    # Data no fuso do agente, não no do servidor: numa VPS em UTC o arquivo de log viraria o dia
    # no meio da tarde.
    log_file = log_dir / f"promocoes_{now_local():%Y%m%d}.log"
    logging.basicConfig(
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_file, encoding="utf-8"),
            logging.StreamHandler(),
        ],
        force=True,
    )

    # O httpx registra a URL completa de cada requisição em nível INFO, o que grava o token do bot
    # do Telegram em texto puro dentro do arquivo de log.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)

    return logging.getLogger("promocoes")


def request_timeout(settings) -> int:
    return int(settings.get("orchestrator", "request_timeout_seconds", default=15))


def _client_timeout(settings) -> httpx.Timeout:
    return httpx.Timeout(request_timeout(settings))


async def http_get(
    url: str,
    settings,
    *,
    headers: dict[str, str] | None = None,
    params: dict[str, Any] | None = None,
) -> httpx.Response:
    merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
    async with httpx.AsyncClient(timeout=_client_timeout(settings), follow_redirects=True) as client:
        response = await client.get(url, headers=merged_headers, params=params)
        response.raise_for_status()
        return response


async def http_get_text(url: str, settings, **kwargs: Any) -> str:
    response = await http_get(url, settings, **kwargs)
    return response.text


_TAG_RE = re.compile(r"<[^>]+>")


def strip_html(text: str, *, keep_newlines: bool = False) -> str:
    """Remove tags e resolve entidades HTML.

    Usado no fallback de texto puro do Telegram — nesse caso com `keep_newlines`, senão a
    mensagem inteira viraria um parágrafo só.
    """
    if not text:
        return ""
    unescaped = html.unescape(_TAG_RE.sub(" ", text))
    if keep_newlines:
        return "\n".join(" ".join(line.split()) for line in unescaped.splitlines())
    return " ".join(unescaped.split())


def offer_key(text: str) -> str:
    """Chave de deduplicação de uma oferta: só letras e dígitos do começo do texto.

    Usada em dois momentos e por isso mora aqui, longe dos dois: o scraper descarta o repost
    do mesmo post em canais diferentes, e o histórico registra o que já foi ao ar. Precisa ser
    a mesma chave nos dois, senão a oferta publicada hoje não é reconhecida amanhã.
    """
    return re.sub(r"[^\w]", "", (text or "").lower(), flags=re.UNICODE)[:60]


def now_local() -> datetime:
    return datetime.now(TIMEZONE)


def format_date_pt_br(dt: datetime) -> str:
    weekdays = (
        "Segunda-feira",
        "Terça-feira",
        "Quarta-feira",
        "Quinta-feira",
        "Sexta-feira",
        "Sábado",
        "Domingo",
    )
    months = (
        "Janeiro",
        "Fevereiro",
        "Março",
        "Abril",
        "Maio",
        "Junho",
        "Julho",
        "Agosto",
        "Setembro",
        "Outubro",
        "Novembro",
        "Dezembro",
    )
    return f"{weekdays[dt.weekday()]}, {dt.day} de {months[dt.month - 1]} de {dt.year}"
