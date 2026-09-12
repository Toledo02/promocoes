"""Promoções: leitura dos canais públicos de promoção do Telegram.

A fonte é `https://t.me/s/<canal>`, a prévia web que o Telegram publica para canais públicos:
HTML simples, sem bot, sem token e sem entrar no canal. Curadoria humana é o que separa um
achado de um item de catálogo, e é por isso que a fonte são canais e não vitrines de loja.

O scraper devolve um **pool** de candidatos maior que o publicado. O corte final acontece
depois do histórico (`core/digest.select_offers`): sem essa folga, o segundo envio do dia
sairia pela metade, porque tudo que a manhã já mostrou é removido.
"""

from __future__ import annotations

import asyncio
import logging
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from itertools import zip_longest
from typing import Any
from urllib.parse import urlparse

from bs4 import BeautifulSoup

from core.utils import BROWSER_HEADERS, ScraperResult, http_get_text, offer_key

logger = logging.getLogger(__name__)

TELEGRAM_PREVIEW_URL = "https://t.me/s/{channel}"

# Limite do texto guardado no payload. O post inteiro é descrição de frete e parcelamento; o
# recorte que vai à mensagem é menor ainda (`formatting.max_offer_chars`).
MAX_TEXT_CHARS = 400

# "Cupom: BLACK20", "código de desconto ABC123". O código costuma vir em caixa alta; a palavra
# que o anuncia, não. O grupo é capturado com IGNORECASE e validado depois, porque flag por
# grupo — `(?i:...)` — só existe a partir do Python 3.11.
_COUPON_RE = re.compile(
    r"(?:cupom|cupons|c[oó]digo)s?\s*(?:de\s+desconto\s*)?[:\-–]?\s*([\w][\w.\-]{2,23})",
    re.IGNORECASE,
)

# Palavras que caem no grupo quando a mensagem não traz código nenhum ("cupom de desconto na
# página", "cupom disponível no grupo").
_COUPON_STOPWORDS = {
    "DE", "DO", "DA", "NO", "NA", "EM", "DESCONTO", "PROMOCIONAL", "EXCLUSIVO", "DISPONIVEL",
    "DISPONÍVEL", "APLICADO", "AUTOMATICO", "AUTOMÁTICO", "ACIMA", "PARA", "COM", "SEM",
}

_URL_RE = re.compile(r"https?://\S+")
_HASHTAG_RE = re.compile(r"(?:^|\s)#\w+")
# Sequências decorativas: "🔥🔥🔥🔥", "!!!!", "———". Uma repetição basta para o mesmo efeito.
_REPEAT_RE = re.compile(r"([^\w\s])\1{2,}")


def _coupon(text: str) -> str | None:
    """Código de desconto citado na mensagem, quando ele aparece na prévia."""
    for match in _COUPON_RE.finditer(text):
        code = match.group(1).strip(".-")
        if len(code) < 3 or code.upper() in _COUPON_STOPWORDS:
            continue
        # Código é caixa alta ou tem dígito; texto corrido em minúsculas não é código.
        if code == code.upper() or any(char.isdigit() for char in code):
            return code.upper()
    return None


def _clean_text(text: str) -> str:
    """Tira do post o que não é a oferta.

    Três coisas atrapalham a leitura e, pior, a deduplicação: a URL crua que o canal cola no
    meio da frase (a mesma oferta com outro parâmetro de afiliado vira outra chave), a fileira
    de hashtags no fim, e as sequências de emoji repetido. Nada disso é informação — o link
    canônico já vem do `data-post` e o preço continua no texto.
    """
    cleaned = _URL_RE.sub(" ", text or "")
    cleaned = _HASHTAG_RE.sub(" ", cleaned)
    cleaned = _REPEAT_RE.sub(r"\1", cleaned)
    return " ".join(cleaned.split()).strip(" -–—|•")


def _message_datetime(node: Any) -> datetime | None:
    time_el = node.select_one("time[datetime]")
    if not time_el:
        return None
    try:
        return datetime.fromisoformat(time_el["datetime"].replace("Z", "+00:00"))
    except (KeyError, ValueError):
        return None


def _external_link(text_el: Any) -> str | None:
    """Primeiro link que sai do Telegram — normalmente o da loja."""
    for anchor in text_el.select("a[href]"):
        href = anchor.get("href", "")
        host = urlparse(href).netloc.lower()
        if href.startswith("http") and not host.endswith("t.me") and "telegram" not in host:
            return href
    return None


def _parse_channel(
    html: str,
    channel: str,
    cutoff: datetime,
    noise: list[str],
    min_length: int = 25,
) -> list[dict[str, Any]]:
    soup = BeautifulSoup(html, "lxml")
    messages: list[dict[str, Any]] = []

    for node in soup.select("div.tgme_widget_message"):
        text_el = node.select_one(".tgme_widget_message_text")
        if not text_el:
            continue

        published = _message_datetime(node)
        if published and published < cutoff:
            continue

        text = _clean_text(text_el.get_text(" ", strip=True))
        if len(text) < min_length or any(pattern in text.lower() for pattern in noise):
            continue

        post = node.get("data-post")
        permalink = f"https://t.me/{post}" if post else None
        store_url = _external_link(text_el)

        messages.append(
            {
                "channel": channel,
                "text": text[:MAX_TEXT_CHARS],
                # O link publicado é o da mensagem no canal, não o da loja: boa parte das
                # ofertas só funciona com o cupom, e o cupom está no post — mandar direto para
                # a loja faz a pessoa pagar o preço cheio. A URL da loja fica no payload como
                # referência e não vai à mensagem.
                "link": permalink or store_url,
                "link_type": "canal" if permalink else "loja",
                "store_url": store_url,
                "coupon": _coupon(text),
                "published": published.isoformat() if published else None,
            }
        )

    # Mais recentes primeiro: a prévia vem em ordem cronológica.
    messages.reverse()
    return messages


def _interleave(per_channel: list[list[dict[str, Any]]], total: int) -> list[dict[str, Any]]:
    """Round-robin entre canais, com deduplicação.

    Mesma razão dos feeds RSS do jornal: concatenar e truncar faz o primeiro canal ocupar todos
    os slots. E os canais copiam uns aos outros — o mesmo achado sai em três deles na mesma
    hora, com o texto quase idêntico.
    """
    items: list[dict[str, Any]] = []
    seen: set[str] = set()

    for row in zip_longest(*per_channel):
        for item in row:
            if item is None:
                continue
            key = offer_key(item["text"])
            if key in seen:
                continue
            seen.add(key)
            items.append(item)
            if len(items) >= total:
                return items
    return items


async def _read_channel(channel: str, settings, cutoff, noise, per_channel, min_length):
    html = await http_get_text(
        TELEGRAM_PREVIEW_URL.format(channel=channel), settings, headers=BROWSER_HEADERS
    )
    found = _parse_channel(html, channel, cutoff, noise, min_length)[:per_channel]
    if not found:
        logger.info("Canal %s sem ofertas novas na janela", channel)
    return found


async def fetch(settings) -> ScraperResult:
    section = "promotions"
    cfg = settings.get("promotions") or {}

    channels = [str(c).strip().lstrip("@") for c in (cfg.get("telegram_channels") or []) if str(c).strip()]
    if not channels:
        return ScraperResult(
            section=section,
            status="error",
            error="Nenhum canal em promotions.telegram_channels",
        )

    max_age_hours = int(cfg.get("max_age_hours", 24))
    per_channel = int(cfg.get("per_channel", 8))
    pool = int(cfg.get("candidate_pool", 30))
    min_length = int(cfg.get("min_text_length", 25))
    noise = [str(p).lower() for p in (cfg.get("noise_patterns") or [])]
    cutoff = datetime.now(timezone.utc) - timedelta(hours=max_age_hours)

    # Em paralelo: são dezenas de canais no alvo do projeto, e em série o cron gastaria mais
    # tempo esperando rede do que fazendo qualquer outra coisa.
    results = await asyncio.gather(
        *(
            _read_channel(channel, settings, cutoff, noise, per_channel, min_length)
            for channel in channels
        ),
        return_exceptions=True,
    )

    per_channel_items: list[list[dict[str, Any]]] = []
    errors: list[str] = []
    failed: list[str] = []

    for channel, result in zip(channels, results):
        if isinstance(result, BaseException):
            logger.warning("Falha ao ler o canal %s: %s", channel, result)
            errors.append(f"@{channel}: {result}")
            failed.append(channel)
        elif result:
            per_channel_items.append(result)

    offers = _interleave(per_channel_items, pool)
    logger.info(
        "promotions: %s ofertas de %s canais (%s falharam)",
        len(offers),
        len(channels) - len(failed),
        len(failed),
    )

    data = {
        "offers": offers,
        "count": len(offers),
        "channels_read": [c for c in channels if c not in failed],
        "channels_failed": failed,
    }

    # Todos os canais fora do ar é falha de resultado; alguns fora, com outros entregando, é o
    # caso `partial` — o alerta correspondente é decisão do orquestrador, não daqui.
    if failed and len(failed) == len(channels):
        return ScraperResult(section=section, status="error", data=data, error="; ".join(errors))

    return ScraperResult(
        section=section,
        status="partial" if errors else "ok",
        data=data,
        error="; ".join(errors) if errors else None,
    )
