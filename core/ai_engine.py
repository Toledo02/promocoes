"""Camada LLM — opcional.

O caminho padrão é o template de `core/digest.py`: o projeto tem que rodar sem chave de API. O
modelo entra só quando `formatting.use_llm` está ligado, e faz uma coisa só — reescrever o
texto do post, que vem cheio de "CORRAM", emoji repetido e caixa alta. Preço, cupom e link
continuam sendo copiados do Python, porque é exatamente aí que modelo erra: no jornal que deu
origem a este projeto, ele chegou a inventar uma variação percentual que ninguém pediu.

Se a API falhar, o fallback é o mesmo template — não uma versão degradada dele.
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from typing import Any

from core.digest import (
    DEFAULT_MAX_OFFER_CHARS,
    SECTION_RULE,
    SECTION_TITLE,
    format_digest,
)
from core.utils import format_date_pt_br, now_local

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = f"""You rewrite Brazilian deal-channel posts into a short Telegram digest in
pt-BR. You are an editor, not a shopper and not an analyst.

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
3. ONE offer per entry in "offers", in the SAME ORDER, none added, none dropped, none merged.
   Each offer is a block of up to three lines:
   • <b>product name</b> — price and the one detail that matters
     cupom <b>CODE</b>
     <a href="LINK">[Ver no canal]</a>
   Bullets use "•" and nothing else. Separate blocks with one blank line.
4. NUMBERS ARE COPIED, NEVER COMPUTED. Write every price exactly as it appears in the offer
   text — same currency, same digits, same separators. Never convert, never round, never
   compute a discount, an instalment or a percentage, and never claim "menor preço" unless the
   post says so.
5. The coupon line appears only when the offer has a "coupon"; copy the code character by
   character, in caps, inside <b>. Never invent a code and never guess one from the text.
6. The link is the offer's "link" field, unchanged — it points to the post in the channel,
   where the coupon is. Never link to a store, never rewrite a URL, never add a parameter.
   Use the label [Ver no canal] when "link_type" is "canal" and [Ver oferta] otherwise. After
   the link, add the source channel as <i>@channel</i>.
7. Cut the noise from the post: "CORRAM", "IMPERDÍVEL", repeated emoji, hashtags, shipping
   boilerplate. Keep the product name recognisable — model, size, capacity and generation are
   part of the name, not noise. One line, at most 20 words, no closing remark.
8. Never invent an offer, a store, a stock warning or a deadline that is not in the payload.
"""

# Erros que não melhoram com nova tentativa: modelo inexistente, chave inválida, prompt malformado.
PERMANENT_ERROR_MARKERS = ("400", "401", "403", "404", "INVALID_ARGUMENT", "PERMISSION_DENIED")


def _build_user_prompt(offers: list[dict[str, Any]], moment: datetime) -> str:
    meta = {
        "header": f"<b>{format_date_pt_br(moment)} — {moment:%H:%M}</b>",
        "offer_count": len(offers),
        "instruction": (
            "Rewrite each offer as one bullet. Copy prices, coupons and links verbatim."
        ),
    }
    # Só os campos que a mensagem usa. `store_url` fica de fora de propósito: dar a URL da loja
    # ao modelo é convidá-lo a publicá-la, e o cupom está no post do canal.
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
        "Monte o resumo de achados com estas ofertas.\n\n"
        f"Metadata:\n{json.dumps(meta, ensure_ascii=False, indent=2)}\n\n"
        f"offers:\n{json.dumps(trimmed, ensure_ascii=False, indent=2)}"
    )


def _looks_valid(content: str, offers: list[dict[str, Any]]) -> bool:
    """Barreira mínima antes de aceitar a resposta.

    O modelo às vezes devolve um resumo em prosa, ou come metade das ofertas. Nesse caso o
    template é melhor do que a resposta, e o template está sempre disponível.
    """
    if SECTION_TITLE not in content:
        return False
    bullets = content.count("•")
    return bullets >= max(1, len(offers) - 1)


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
    offers: list[dict[str, Any]],
    settings,
    now: datetime | None = None,
) -> str:
    """Mensagem final: template por padrão, modelo quando pedido e disponível."""
    moment = now or now_local()
    max_chars = int(settings.get("formatting", "max_offer_chars", default=DEFAULT_MAX_OFFER_CHARS))
    template = format_digest(offers, moment, max_chars)

    if not settings.get("formatting", "use_llm", default=False):
        return template
    if not settings.llm_api_key:
        logger.warning("formatting.use_llm ligado sem LLM_API_KEY; usando o template")
        return template
    if not offers:
        return template

    user_prompt = _build_user_prompt(offers, moment)

    # Um 503 do Gemini é sobrecarga do modelo e costuma durar minutos, não segundos: o intervalo
    # fixo de 10s gastava as três tentativas em menos de um minuto e caía no fallback.
    delays = [10, 30, 90]
    models = [settings.llm_model, *settings.llm_fallback_models]

    for model in models:
        for attempt, delay in enumerate(delays, start=1):
            try:
                content = _generate_once(model, user_prompt, settings)
                if not _looks_valid(content, offers):
                    logger.warning("Resposta de %s fora do formato; usando o template", model)
                    return template
                if model != settings.llm_model:
                    logger.warning("Resumo gerado pelo modelo de reserva %s", model)
                return content
            except Exception as exc:
                if _is_permanent(exc):
                    logger.error("Erro permanente em %s, sem retry: %s", model, exc)
                    break
                logger.warning("Tentativa %s/%s falhou em %s: %s", attempt, len(delays), model, exc)
                if attempt < len(delays):
                    logger.info("Aguardando %ss antes de tentar novamente", delay)
                    time.sleep(delay)

    logger.error("Todas as tentativas de geração falharam; usando o template")
    return template
