"""Shared utilities for scrapers and orchestrator."""

from __future__ import annotations

import html
import logging
import re
import unicodedata
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


# Tokens que começam frase ou ligam oração: aparecem em maiúscula sem identificar produto
# nenhum. Inclui o vocabulário fixo do anúncio de promoção, que abre quase toda mensagem em
# maiúscula ("Cupom", "Frete", "Menor preço") e faria duas ofertas sem nada em comum parecerem
# a mesma.
NOT_ENTITIES = {
    "a", "o", "as", "os", "um", "uma", "de", "do", "da", "dos", "das", "em", "no", "na", "nos",
    "nas", "por", "para", "com", "sem", "sobre", "apos", "ate", "entre", "contra", "durante",
    "que", "quem", "como", "quando", "onde", "mais", "menos", "novo", "nova", "novos", "novas",
    "veja", "confira", "saiba", "entenda", "the", "and", "for", "with", "from", "this", "that",
    "cupom", "cupons", "codigo", "desconto", "descontos", "oferta", "ofertas", "promocao",
    "promocoes", "preco", "precos", "menor", "maior", "frete", "gratis", "gratuito", "compre",
    "aproveite", "corram", "ultimas", "unidades", "link", "loja", "site", "app", "hoje",
    "somente", "apenas", "usando", "pix", "boleto", "cartao", "parcelado", "avista", "reais",
    "achado", "achados", "baixou", "caiu", "leve", "pague", "ganhe", "receba", "voce", "seu",
    "sua", "agora", "ainda", "muito", "todos", "todas", "acabou", "volta", "voltou", "chegou",
}

_ENTITY_RE = re.compile(r"\b[A-ZÀ-ÖØ-Þ][\wÀ-ÿ'’-]{2,}\b")


def fold(text: str) -> str:
    """Minúsculas e sem acento, para comparar textos de origens diferentes."""
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(char for char in folded if not unicodedata.combining(char))


def entities(text: str) -> set[str]:
    """Nomes próprios do texto — marca, modelo e loja, que é o que identifica o produto.

    Comparar palavra a palavra não funciona: "Echo Dot 5ª geração por R$ 229 com frete grátis"
    e "SÓ HOJE! Echo Dot 5 saindo por 229 reais na Amazon" são a mesma oferta e quase não
    compartilham palavra comum. Nome próprio atravessa a reescrita do canal; o resto, não.

    O genitivo é aparado porque "Levi's" e "Levis" precisam bater.

    Mora aqui, e não junto de quem filtra, porque duas etapas distintas fazem a mesma pergunta
    com ela: se a oferta já foi publicada num envio anterior (`core/history`) e se duas ofertas
    da mesma leva são o mesmo produto vindo de canais diferentes (`core/digest`).
    """
    found: set[str] = set()
    for token in _ENTITY_RE.findall(text or ""):
        folded = re.sub(r"['’]s$", "", fold(token)).strip("'’-")
        if len(folded) >= 3 and folded not in NOT_ENTITIES:
            found.add(folded)
    return found


def same_item(a: set[str], b: set[str], min_entities: int = 2, min_ratio: float = 0.6) -> bool:
    """Dois conjuntos de nomes próprios descrevem o mesmo produto?

    Proporção, não igualdade: um canal escreve "Echo Dot 5ª geração" e o outro "Echo Dot 5".
    E pelo menos `min_entities` nomes precisam coincidir de fato — com um só, "Samsung"
    bastaria para casar duas ofertas sem relação.
    """
    if not a or not b:
        return False
    common = len(a & b)
    return common >= min_entities and common / len(a) >= min_ratio


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
