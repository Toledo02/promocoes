"""Camada LLM — opcional.

O caminho padrão, sem chave de API, é o template de `core/digest.py`: ordem de chegada, corta em
`max_items`. Com `formatting.use_llm` ligado, o modelo assume duas coisas que a heurística de
Python não faz bem — **qual** oferta publicar (maior desconto, preço final, produto reconhecível
vs. banner de loja) e reescrever o texto do post, que vem cheio de "CORRAM", emoji repetido e
caixa alta. O que o modelo nunca faz, ligado ou não: preço, cupom e link continuam sendo
copiados do post, verbatim — é exatamente aí que modelo erra. No jornal que deu origem a este
projeto, ele chegou a inventar uma variação percentual que ninguém pediu.

Como a seleção agora é do modelo, a validação de resposta (`_looks_valid`) não pode mais
conferir "uma linha por oferta de entrada" — o normal é ele descartar a maioria dos candidatos.
Em vez disso, cada link que aparece na resposta é conferido contra os links dos candidatos
enviados: um link que não veio no payload é uma oferta inventada, e a resposta inteira é
descartada em favor do template. Esse casamento por link também é como o pipeline sabe **quais**
candidatos foram de fato publicados, para gravar no histórico (`_resolve_chosen`) — só essas
entram na anti-repetição de amanhã, nunca o pool inteiro que foi oferecido ao modelo.

Se a API falhar, ou a resposta não passar na validação, o fallback é o mesmo template — não uma
versão degradada dele.
"""

from __future__ import annotations

import json
import logging
import re
import time
from datetime import datetime
from typing import Any

from core.digest import (
    DEFAULT_MAX_OFFER_CHARS,
    SECTION_RULE,
    SECTION_TITLE,
    format_digest,
    select_offers,
)
from core.utils import format_date_pt_br, now_local

logger = logging.getLogger(__name__)

DEFAULT_MAX_ITEMS = 8
DEFAULT_MAX_PER_COUPON = 2

SYSTEM_PROMPT = f"""You curate a short Telegram digest of deals in pt-BR, from raw posts of
Brazilian deal channels. You are an editor AND the person deciding what is worth showing — not
a shopper, not an analyst, and never the source of a price.

Rules:
1. Output ONLY the final message, in the HTML subset Telegram accepts: <b>, <i> and
   <a href="URL">. No other tag exists. Never use Markdown asterisks (*) or underscores (_) —
   they render literally. Never use <br>, <p>, <ul> or heading tags. Do not wrap the output in
   code fences.
2. Start with the EXACT header block given in metadata.header, copied verbatim, followed by a
   blank line, then this section header, exactly like this and nothing else on those lines:

   {SECTION_RULE}
   <b>{SECTION_TITLE}</b>

   then one blank line before the first offer.
3. SELECTION. "offers" holds up to metadata.candidate_count candidates — deliberately more than
   should be published. Choose at most metadata.max_items of them: the best ones, ordered from
   best deal first to least-best last. If fewer than metadata.max_items are genuinely worth
   showing, output fewer (down to 1) — never pad the count with a weak offer, and never invent
   one to reach it.
4. HOW TO JUDGE "BEST", in rough order of importance:
   a. A bigger discount or percentage off actually stated in the text beats a smaller one.
   b. A specific, recognisable product (brand + model) beats a vague store-wide banner that
      names no real item ("as melhores ofertas", "tudo abaixo de R$ X") — banners like that are
      usually not worth including at all unless nothing better is available.
   c. A real product-specific coupon beats a generic app-wide promotion, when otherwise similar.
   d. Variety: do not fill the digest with near-duplicates of one category (five sneakers, three
      diaper packs) when the candidate list has other genuinely good, different offers.
   When two or more candidates clearly describe the SAME product (same brand and model), keep
   only the ONE with the best final price or discount and drop the rest — never show the same
   product twice. Never use the same coupon code in more than metadata.max_per_coupon of your
   chosen offers.
5. OUTPUT FORMAT. Each chosen offer is one block, in this exact shape:
   • <b>product name</b> — price and the one detail that matters
     cupom <b>CODE</b>
     <a href="LINK">[Ver no canal]</a>
   Bullets use "•" and nothing else. Separate blocks with one blank line.
6. NUMBERS ARE COPIED, NEVER COMPUTED. Write every price exactly as it appears in the offer
   text — same currency, same digits, same separators. Never convert, never round, never
   compute a discount, an instalment or a percentage, and never claim "menor preço" unless the
   post says so. Comparing candidates to rank them is fine; changing a number is not.
7. The coupon line appears only when the CHOSEN offer has a "coupon"; copy the code character by
   character, in caps, inside <b>. Never invent a code and never guess one from the text.
8. The link is the chosen offer's OWN "link" field, unchanged — it points to the post in the
   channel, where the coupon is. Never link to a store, never rewrite a URL, never add a
   parameter, and never reuse one offer's link for another. Use the label [Ver no canal] when
   "link_type" is "canal" and [Ver oferta] otherwise. After the link, add the source channel as
   <i>@channel</i>.
9. Cut the noise from the post: "CORRAM", "IMPERDÍVEL", repeated emoji, hashtags, shipping
   boilerplate. Keep the product name recognisable — model, size, capacity and generation are
   part of the name, not noise. One line, at most 20 words, no closing remark.
10. Never invent an offer, a store, a stock warning, a deadline or a link that is not among the
    candidates given.
"""

# Erros que não melhoram com nova tentativa: modelo inexistente, chave inválida, prompt malformado.
PERMANENT_ERROR_MARKERS = ("400", "401", "403", "404", "INVALID_ARGUMENT", "PERMISSION_DENIED")

_HREF_RE = re.compile(r'href="([^"]+)"')


def _build_user_prompt(
    offers: list[dict[str, Any]], moment: datetime, max_items: int, max_per_coupon: int
) -> str:
    meta = {
        "header": f"<b>{format_date_pt_br(moment)} — {moment:%H:%M}</b>",
        "candidate_count": len(offers),
        "max_items": max_items,
        "max_per_coupon": max_per_coupon,
        "instruction": (
            f"Select at most {max_items} of these {len(offers)} candidates — the best deals, "
            "best first. Rewrite each as one bullet. Copy prices, coupons and links verbatim."
        ),
    }
    # Só os campos que a mensagem usa e a seleção precisa julgar. `store_url` fica de fora de
    # propósito: dar a URL da loja ao modelo é convidá-lo a publicá-la, e o cupom está no post
    # do canal.
    trimmed = [
        {
            "text": offer.get("text", ""),
            "coupon": offer.get("coupon"),
            "link": offer.get("link"),
            "link_type": offer.get("link_type"),
            "channel": offer.get("channel"),
        }
        for offer in offers
    ]
    return (
        "Curadoria do resumo de achados a partir destes candidatos.\n\n"
        f"Metadata:\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"offers:\n{json.dumps(trimmed, ensure_ascii=False, indent=2)}"
    )


def _extract_links(content: str) -> list[str]:
    """Links na ordem em que aparecem na resposta, sem repetir."""
    seen: list[str] = []
    for href in _HREF_RE.findall(content):
        if href not in seen:
            seen.append(href)
    return seen


def _resolve_chosen(content: str, offers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Ofertas realmente publicadas, na ordem da resposta — é isso que vai para o histórico.

    Nunca o pool inteiro que foi oferecido ao modelo: gravar candidato que ele descartou
    suprimiria essa oferta amanhã sem ela nunca ter ido ao ar.
    """
    by_link = {offer["link"]: offer for offer in offers if offer.get("link")}
    return [by_link[link] for link in _extract_links(content) if link in by_link]


def _looks_valid(
    content: str,
    offers: list[dict[str, Any]],
    max_items: int,
    max_per_coupon: int = DEFAULT_MAX_PER_COUPON,
) -> bool:
    """Barreira antes de aceitar a resposta.

    Como a seleção agora é do modelo, não dá mais para exigir "uma linha por oferta de entrada"
    — o normal é ele descartar a maioria. O que continua verificável: o título apareceu, a
    contagem de blocos está dentro do pedido, e **todo link citado veio de fato num dos
    candidatos** — um link fora da lista é oferta inventada, o problema mais caro que existe
    aqui, e nenhuma outra checagem pega isso.
    """
    if SECTION_TITLE not in content:
        return False

    bullets = content.count("•")
    if not 1 <= bullets <= max_items:
        return False

    links = _extract_links(content)
    known = {offer["link"] for offer in offers if offer.get("link")}
    if any(link not in known for link in links):
        return False

    chosen = _resolve_chosen(content, offers)
    coupon_counts: dict[str, int] = {}
    for offer in chosen:
        coupon = offer.get("coupon")
        if coupon:
            coupon_counts[coupon] = coupon_counts.get(coupon, 0) + 1
    if any(count > max_per_coupon for count in coupon_counts.values()):
        return False

    return True


def _generate_once(model: str, user_prompt: str, settings) -> str:
    from google import genai
    from google.genai import types

    client = genai.Client(api_key=settings.llm_api_key)
    response = client.models.generate_content(
        model=model,
        contents=user_prompt,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_PROMPT,
            temperature=settings.llm_temperature,
        ),
    )
    content = (response.text or "").strip()
    if not content:
        raise ValueError("Resposta vazia do Gemini")
    return content


def _is_permanent(error: Exception) -> bool:
    message = str(error)
    return any(marker in message for marker in PERMANENT_ERROR_MARKERS)


def generate_digest(
    candidates: list[dict[str, Any]],
    settings,
    max_items: int = DEFAULT_MAX_ITEMS,
    max_per_coupon: int = DEFAULT_MAX_PER_COUPON,
    now: datetime | None = None,
) -> tuple[str, list[dict[str, Any]]]:
    """Mensagem final e as ofertas de fato publicadas (para o histórico).

    `candidates` é o pool bruto pós-filtros de histórico — ainda com duplicata do mesmo produto
    entre canais, ainda sem teto de cupom aplicado. Quem resolve isso é o próprio modelo (rule 4
    do prompt) quando `use_llm` está ligado, porque a decisão de qual duplicata manter depende
    de comparar preço, que é julgamento de texto livre. Sem LLM (ou se ele falhar), a mensagem
    sai de `select_offers`: dedup + teto por ordem de chegada, sem julgamento nenhum disponível.
    """
    moment = now or now_local()
    max_chars = int(settings.get("formatting", "max_offer_chars", default=DEFAULT_MAX_OFFER_CHARS))

    def _template() -> tuple[str, list[dict[str, Any]]]:
        chosen = select_offers(candidates, max_items, max_per_coupon)
        return format_digest(chosen, moment, max_chars), chosen

    if not settings.get("formatting", "use_llm", default=False):
        return _template()
    if not settings.llm_api_key:
        logger.warning("formatting.use_llm ligado sem LLM_API_KEY; usando o template")
        return _template()
    if not candidates:
        return _template()

    user_prompt = _build_user_prompt(candidates, moment, max_items, max_per_coupon)

    # Um 503 do Gemini é sobrecarga do modelo e costuma durar minutos, não segundos: o intervalo
    # fixo de 10s gastava as três tentativas em menos de um minuto e caía no fallback.
    delays = [10, 30, 90]
    models = [settings.llm_model, *settings.llm_fallback_models]

    for model in models:
        for attempt, delay in enumerate(delays, start=1):
            try:
                content = _generate_once(model, user_prompt, settings)
                if not _looks_valid(content, candidates, max_items, max_per_coupon):
                    logger.warning("Resposta de %s fora do formato; usando o template", model)
                    return _template()
                chosen = _resolve_chosen(content, candidates)
                if not chosen:
                    logger.warning("Resposta de %s sem link reconhecível; usando o template", model)
                    return _template()
                if model != settings.llm_model:
                    logger.warning("Resumo gerado pelo modelo de reserva %s", model)
                logger.info(
                    "%s de %s candidatos escolhidos pelo modelo (%s)",
                    len(chosen), len(candidates), model,
                )
                return content, chosen
            except Exception as exc:
                if _is_permanent(exc):
                    logger.error("Erro permanente em %s, sem retry: %s", model, exc)
                    break
                logger.warning("Tentativa %s/%s falhou em %s: %s", attempt, len(delays), model, exc)
                if attempt < len(delays):
                    logger.info("Aguardando %ss antes de tentar novamente", delay)
                    time.sleep(delay)

    logger.error("Todas as tentativas de geração falharam; usando o template")
    return _template()
