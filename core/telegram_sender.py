"""Telegram Bot API sender with HTML sanitising, tag-aware splitting and plain-text fallback."""

from __future__ import annotations

import logging
import re
from typing import Any

import httpx

from core.utils import strip_html

logger = logging.getLogger(__name__)

TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
MAX_MESSAGE_LENGTH = 4096

# Subset de HTML aceito pelo Telegram. Qualquer outra tag derruba a mensagem inteira.
ALLOWED_TAGS = {"b", "strong", "i", "em", "u", "ins", "s", "strike", "del", "code", "pre", "a"}

# Tags de bloco viram quebra de linha em vez de sumir: removê-las coladas juntaria as palavras
# vizinhas ("<p>um</p><p>dois</p>" -> "umdois").
BLOCK_TAGS = {
    "p", "div", "li", "ul", "ol", "tr", "td", "th", "table", "section",
    "article", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6",
}

_TAG_PATTERN = re.compile(r"</?([a-zA-Z][a-zA-Z0-9]*)([^>]*)>")
_BR_PATTERN = re.compile(r"</?br\s*/?>", re.IGNORECASE)
# "&" que não inicia uma entidade já válida (&amp; &#39; &nbsp;).
_BARE_AMP = re.compile(r"&(?!#?\w+;)")


def sanitize_html(text: str) -> str:
    """Deixa a mensagem no subset HTML que o Telegram entende.

    O modelo reincide em três coisas que a API rejeita: tags de layout (<br>, <p>, <ul>), `&` cru
    dentro de URLs, e `<` solto em texto. Qualquer uma derruba a mensagem toda para texto puro,
    então é mais barato corrigir aqui do que confiar no prompt.
    """
    if not text:
        return ""

    text = _BR_PATTERN.sub("\n", text)

    # Guarda as tags permitidas antes de escapar o resto, senão elas próprias seriam escapadas.
    kept: list[str] = []

    def _stash(match: re.Match[str]) -> str:
        name = match.group(1).lower()
        if name in BLOCK_TAGS:
            return "\n"
        if name not in ALLOWED_TAGS:
            return ""
        kept.append(_BARE_AMP.sub("&amp;", match.group(0)))
        return f"\x00{len(kept) - 1}\x00"

    text = _TAG_PATTERN.sub(_stash, text)
    text = _BARE_AMP.sub("&amp;", text)
    text = text.replace("<", "&lt;").replace(">", "&gt;")

    for index, raw in enumerate(kept):
        text = text.replace(f"\x00{index}\x00", raw)

    # As substituições acima podem gerar sequências de linhas em branco.
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _open_tags(text: str) -> list[tuple[str, str]]:
    """Tags abertas e ainda não fechadas, na ordem de abertura: [(nome, tag_completa)]."""
    stack: list[tuple[str, str]] = []
    for match in _TAG_PATTERN.finditer(text):
        name = match.group(1).lower()
        if name not in ALLOWED_TAGS:
            continue
        if match.group(0).startswith("</"):
            for index in range(len(stack) - 1, -1, -1):
                if stack[index][0] == name:
                    stack.pop(index)
                    break
        else:
            stack.append((name, match.group(0)))
    return stack


def _raw_split(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.split("\n"):
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks


def _split_message(text: str, limit: int = MAX_MESSAGE_LENGTH) -> list[str]:
    """Divide respeitando as tags: fecha o que ficou aberto e reabre no pedaço seguinte.

    Cortar no meio de um <b> ou <a href> torna os dois pedaços inválidos e faz a mensagem
    inteira cair para texto puro.
    """
    if len(text) <= limit:
        return [text]

    # Margem para as tags de fechamento/reabertura acrescentadas abaixo.
    chunks = _raw_split(text, limit - 200)

    balanced: list[str] = []
    carry: list[tuple[str, str]] = []
    for chunk in chunks:
        body = "".join(raw for _, raw in carry) + chunk
        carry = _open_tags(body)
        closing = "".join(f"</{name}>" for name, _ in reversed(carry))
        balanced.append(body + closing)
    return balanced


def inspect_message(text: str) -> dict[str, Any]:
    """Diagnóstico de como a mensagem sairia — usado pelo --dry-run."""
    sanitized = sanitize_html(text)
    chunks = _split_message(sanitized)

    problems: list[str] = []
    if any(len(chunk) > MAX_MESSAGE_LENGTH for chunk in chunks):
        problems.append("pedaço acima do limite do Telegram")
    if any(_open_tags(chunk) for chunk in chunks):
        problems.append("tag HTML aberta em algum pedaço")
    if "*" in sanitized:
        problems.append("asterisco literal (o modelo usou Markdown)")

    return {
        "sanitized": sanitized,
        "chunk_sizes": [len(chunk) for chunk in chunks],
        "links": len(re.findall(r"<a href", sanitized)),
        "problems": problems,
    }


def send_message(text: str, settings, parse_mode: str | None = "HTML") -> list[dict[str, Any]]:
    token = settings.telegram_bot_token
    chat_id = settings.telegram_chat_id

    if not token or not chat_id:
        raise ValueError("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in config/.env")

    url = TELEGRAM_API.format(token=token)
    responses: list[dict[str, Any]] = []

    for chunk in _split_message(text):
        payload: dict[str, Any] = {
            "chat_id": chat_id,
            "text": chunk,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode

        response = httpx.post(url, json=payload, timeout=30)
        body = response.json()

        if not response.is_success or not body.get("ok"):
            logger.error("Telegram API error: %s", body)
            raise RuntimeError(f"Telegram send failed: {body}")

        responses.append(body)

    return responses


def send_alert(text: str, settings) -> list[dict[str, Any]]:
    """Envia um aviso operacional (falhas de fonte, pipeline abortado).

    Sem parse_mode de propósito: o aviso carrega mensagens de erro cruas, cheias de <, > e &,
    que quebrariam o parser HTML justamente quando algo já deu errado.
    """
    return send_message(text, settings, parse_mode=None)


def send_digest(text: str, settings) -> list[dict[str, Any]]:
    try:
        return send_message(sanitize_html(text), settings, parse_mode="HTML")
    except RuntimeError:
        logger.warning("HTML send failed; retrying as plain text")
        # Reenviar o mesmo texto sem parse_mode faria as tags aparecerem literalmente na mensagem.
        return send_message(strip_html(text, keep_newlines=True), settings, parse_mode=None)
