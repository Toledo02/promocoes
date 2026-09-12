"""Escolha e formatação do que vai ao ar.

Duas etapas que o LLM não faz — e não deve fazer: qual oferta entra (`select_offers`) e como a
linha é montada (`format_digest`). Preço, cupom e link são copiados do post; o modelo, quando
ativado, só reescreve o texto ao redor (ver `core/ai_engine.py`).
"""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime
from typing import Any

from core.utils import format_date_pt_br, now_local, offer_key

SECTION_RULE = "━━━━━━━━━━━━━━━"
SECTION_TITLE = "🛒 ACHADOS & PROMOÇÕES"

DEFAULT_MAX_OFFER_CHARS = 180

# Onde o nome do produto termina e o preço começa. A ordem é de preferência, não de posição:
# "Cadeira Gamer ThunderX3 - modelo novo por R$ 899" deve quebrar no preço, não no travessão.
_HEAD_SEPARATORS = (" por R$", " R$", " — ", " – ", " - ", ": ")

# Faixa em que a quebra produz um rótulo utilizável: abaixo disso não é nome de produto, acima
# disso é a frase inteira em negrito.
_HEAD_MIN, _HEAD_MAX = 8, 70


def select_offers(
    offers: list[dict[str, Any]],
    max_items: int = 8,
    max_per_coupon: int = 2,
) -> list[dict[str, Any]]:
    """Corte final: teto por campanha e teto de itens, preservando a ordem do round-robin.

    O teto por cupom fica aqui, e não no scraper, porque tem que valer sobre o que é
    **publicado**. Aplicado ao pool, uma campanha gastaria as duas vagas com ofertas que o
    histórico depois removeria, e a mensagem sairia sem nenhuma.
    """
    chosen: list[dict[str, Any]] = []
    by_coupon: Counter[str] = Counter()

    for offer in offers:
        coupon = offer.get("coupon")
        if coupon and by_coupon[coupon] >= max_per_coupon:
            continue
        if coupon:
            by_coupon[coupon] += 1
        chosen.append(offer)
        if len(chosen) >= max_items:
            break

    return chosen


def offer_keys(offers: list[dict[str, Any]]) -> list[str]:
    """Chaves das ofertas publicadas, para o histórico."""
    return [offer_key(offer.get("text", "")) for offer in offers if offer.get("text")]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;.:-–—")
    return f"{cut}…"


def _split_headline(text: str) -> tuple[str, str]:
    """Separa "nome do produto" do resto, para o nome ir em negrito.

    Sem nada em negrito, oito linhas de post de canal viram um bloco cinza que ninguém varre
    com o olho. Quando a quebra não sai confiável, a linha fica inteira sem negrito — rótulo
    errado em negrito é pior que rótulo nenhum.
    """
    for separator in _HEAD_SEPARATORS:
        index = text.find(separator)
        if _HEAD_MIN <= index <= _HEAD_MAX:
            head = text[:index].strip(" -–—:")
            tail = text[index:].strip(" -–—:")
            if head and tail:
                return head, tail
    return text, ""


def _escape(text: str) -> str:
    """O texto vem de terceiros: `<`, `>` e `&` crus derrubam a mensagem inteira no Telegram."""
    return html.escape(text or "", quote=False)


def _offer_lines(offer: dict[str, Any], max_chars: int) -> list[str]:
    head, tail = _split_headline(_truncate(offer.get("text", ""), max_chars))
    lines = [f"• <b>{_escape(head)}</b> — {_escape(tail)}" if tail else f"• {_escape(head)}"]

    coupon = offer.get("coupon")
    if coupon:
        lines.append(f"  cupom <b>{_escape(coupon)}</b>")

    link = offer.get("link")
    if link:
        label = "[Ver no canal]" if offer.get("link_type") == "canal" else "[Ver oferta]"
        channel = offer.get("channel")
        suffix = f" <i>@{_escape(channel)}</i>" if channel else ""
        lines.append(f'  <a href="{html.escape(str(link), quote=True)}">{label}</a>{suffix}')

    return lines


def format_digest(
    offers: list[dict[str, Any]],
    now: datetime | None = None,
    max_offer_chars: int = DEFAULT_MAX_OFFER_CHARS,
) -> str:
    """Monta a mensagem em Python — o caminho padrão, que roda sem chave de API.

    A convenção de cabeçalho é a do jornal e existe porque o Telegram não tem tamanho de fonte:
    régua, título em CAIXA ALTA dentro de <b> e uma linha em branco antes do conteúdo é o que
    faz um cabeçalho parecer cabeçalho.
    """
    moment = now or now_local()
    lines = [
        f"<b>{format_date_pt_br(moment)} — {moment:%H:%M}</b>",
        "",
        SECTION_RULE,
        f"<b>{SECTION_TITLE}</b>",
        "",
    ]

    for offer in offers:
        lines.extend(_offer_lines(offer, max_offer_chars))
        lines.append("")

    return "\n".join(lines).strip()
