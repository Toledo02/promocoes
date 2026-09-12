"""Formatação e seleção mecânica do que vai ao ar.

`dedupe_offers` resolve o que é regra de negócio, não julgamento de qualidade: mesmo produto
duas vezes na leva e teto por campanha. Isso nunca muda, LLM ligado ou não. Qual das ofertas
restantes é a "melhor" — maior desconto, preço final, produto reconhecível vs. banner genérico
— é julgamento sobre texto livre, e por isso é o LLM quem decide quando está ativado
(`core/ai_engine.generate_digest`); `select_offers` aqui é só o critério mecânico de
fallback (ordem de chegada) usado quando o LLM está desligado ou falha. `format_digest` monta a
linha; preço, cupom e link são sempre copiados do post, nunca calculados.
"""

from __future__ import annotations

import html
import logging
import re
from collections import Counter
from datetime import datetime
from typing import Any

from core.utils import entities, fold, format_date_pt_br, now_local, offer_key, same_item

logger = logging.getLogger(__name__)

SECTION_RULE = "━━━━━━━━━━━━━━━"
SECTION_TITLE = "🛒 ACHADOS & PROMOÇÕES"

DEFAULT_MAX_OFFER_CHARS = 180

# Só o começo do texto identifica o produto; o resto é frete, parcelamento e apelo. Mesmo corte
# que o histórico usa, pela mesma razão.
_ITEM_TEXT_LIMIT = 110

# Onde o nome do produto termina e o preço começa. A ordem é de preferência, não de posição:
# "Cadeira Gamer ThunderX3 - modelo novo por R$ 899" deve quebrar no preço, não no travessão.
_HEAD_SEPARATORS = (" por R$", " R$", " — ", " – ", " - ", ": ")

# Faixa em que a quebra produz um rótulo utilizável: abaixo disso não é nome de produto, acima
# disso é a frase inteira em negrito.
_HEAD_MIN, _HEAD_MAX = 8, 90

# Palavras que ligam o nome ao preço e ficam penduradas no fim do rótulo quando a quebra é no
# preço: "Smart TV 32 LG 🔥 Por" — R$ 1.099. O nome do produto termina antes delas.
_HEAD_TAIL_NOISE = {
    "de", "por", "a", "so", "somente", "apenas", "sai", "fica", "custa", "hoje", "agora", "ate",
    "cada", "leve", "no", "em", "the",
}


def dedupe_offers(
    offers: list[dict[str, Any]],
    max_per_coupon: int = 2,
) -> list[dict[str, Any]]:
    """Regra de negócio, não julgamento: mesmo produto e teto por campanha, sem cortar por
    quantidade — quem faz o corte final é `select_offers` (fallback) ou o LLM (padrão).

    O teto por cupom fica aqui, e não no scraper, porque tem que valer sobre o que é
    **publicado**. Aplicado ao pool bruto do scraper, uma campanha gastaria as duas vagas com
    ofertas que o histórico depois removeria, e a mensagem sairia sem nenhuma.

    A checagem de mesmo produto é a terceira anti-repetição do projeto, e a única que olha para
    dentro da leva atual: as duas do histórico comparam com o que já foi enviado e não veem o
    mesmo achado chegando por dois canais na mesma hora — que é o corriqueiro, porque os canais
    copiam uns aos outros. A deduplicação exata do scraper não pega esse caso: o texto é
    reescrito, o preço é arredondado e a chave muda. Mantém a primeira ocorrência — quando o LLM
    está ligado ele já resolveu isso a favor do melhor preço antes de chegar aqui (ver
    `core/ai_engine.py`); esta função só entra em ação quando o LLM está desligado ou falhou.
    """
    chosen: list[dict[str, Any]] = []
    chosen_marks: list[set[str]] = []
    by_coupon: Counter[str] = Counter()

    for offer in offers:
        marks = entities(offer.get("text", "")[:_ITEM_TEXT_LIMIT])
        if any(same_item(marks, other) for other in chosen_marks):
            logger.info("Mesmo produto já escolhido nesta leva: %s", offer.get("text", "")[:60])
            continue

        coupon = offer.get("coupon")
        if coupon and by_coupon[coupon] >= max_per_coupon:
            continue
        if coupon:
            by_coupon[coupon] += 1

        chosen.append(offer)
        chosen_marks.append(marks)

    return chosen


def select_offers(
    offers: list[dict[str, Any]],
    max_items: int = 8,
    max_per_coupon: int = 2,
) -> list[dict[str, Any]]:
    """Corte mecânico por ordem de chegada: `dedupe_offers` truncado em `max_items`.

    É o critério do template (LLM desligado) e o de segurança quando o LLM falha — nos dois
    casos não há julgamento de qualidade disponível, só ordem de chegada mesmo.
    """
    return dedupe_offers(offers, max_per_coupon)[:max_items]


def offer_keys(offers: list[dict[str, Any]]) -> list[str]:
    """Chaves das ofertas publicadas, para o histórico."""
    return [offer_key(offer.get("text", "")) for offer in offers if offer.get("text")]


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit(" ", 1)[0].rstrip(" ,;.:-–—")
    return f"{cut}…"


def _trim_head(head: str) -> str:
    """Tira do fim do rótulo o que não é nome de produto: emoji solto e a preposição do preço."""
    tokens = head.split()
    while tokens and (
        not re.search(r"\w", tokens[-1]) or fold(tokens[-1].strip(":,.-")) in _HEAD_TAIL_NOISE
    ):
        tokens.pop()
    return " ".join(tokens)


def _split_headline(text: str) -> tuple[str, str]:
    """Separa "nome do produto" do resto, para o nome ir em negrito.

    Sem nada em negrito, oito posts de canal viram um bloco cinza que ninguém varre com o olho.
    Quando a quebra não sai confiável, a linha fica inteira sem negrito — rótulo errado em
    negrito é pior que rótulo nenhum.
    """
    for separator in _HEAD_SEPARATORS:
        index = text.find(separator)
        if _HEAD_MIN <= index <= _HEAD_MAX:
            head = _trim_head(text[:index].strip(" -–—:"))
            tail = text[index:].strip(" -–—:")
            if len(head) >= _HEAD_MIN and tail:
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
