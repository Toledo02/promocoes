"""Histórico dos envios.

Guarda dois tipos de informação, com finalidades distintas — e é a diferença entre elas que
justifica haver duas anti-repetições:

* as **chaves** das ofertas que foram ao ar, para a comparação exata. Aqui, ao contrário do
  jornal de onde este projeto nasceu, o que é oferecido *é* o que é publicado: a mensagem lista
  todas as ofertas selecionadas, uma por linha. Então registrar o conjunto é registrar o
  publicado, e o repost literal do mesmo post amanhã é barrado sem margem para erro.
* o **texto** dos envios recentes, para a comparação por nome próprio. Ela alcança o que a
  chave exata não alcança: o mesmo produto anunciado por outro canal, com outra redação, outro
  emoji e outra ordem de palavras.

Retenção fixa em dias para o arquivo não crescer indefinidamente.
"""

from __future__ import annotations

import json
import logging
import re
import unicodedata
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from core.utils import now_local, offer_key

logger = logging.getLogger(__name__)

DEFAULT_RETENTION_DAYS = 30

# Quantos envios recentes a anti-repetição por texto consulta. São envios, não dias: com dois
# disparos diários, 4 cobre ontem e hoje.
DEFAULT_SENDS_IN_LOOKBACK = 4

# Idade máxima desses envios. `recent_sends` devolve os N mais recentes sem olhar a data, então
# depois de uma interrupção do cron o envio de um mês atrás entraria como se fosse o de agora.
DEFAULT_LOOKBACK_DAYS = 2

# Por quantos dias uma oferta já publicada continua bloqueada pela chave exata.
DEFAULT_REPEAT_WINDOW_DAYS = 3


def load(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Histórico ilegível (%s); começando vazio", exc)
        return {}


def save(path: Path, history: dict[str, Any], retention_days: int = DEFAULT_RETENTION_DAYS) -> None:
    pruned = prune(history, retention_days)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pruned, ensure_ascii=False, indent=2), encoding="utf-8")


def prune(history: dict[str, Any], retention_days: int = DEFAULT_RETENTION_DAYS) -> dict[str, Any]:
    """Descarta dias além da janela de retenção, e chaves que não sejam datas.

    O "hoje" é sempre o do fuso do agente, nunca o do servidor: `record` grava a data com
    `now_local()`, e numa VPS em UTC comparar com `date.today()` faria a janela deslizar meio dia.
    """
    cutoff = now_local().date() - timedelta(days=retention_days)
    kept: dict[str, Any] = {}
    for key, value in history.items():
        try:
            if datetime.strptime(key, "%Y-%m-%d").date() >= cutoff:
                kept[key] = value
        except ValueError:
            continue
    return dict(sorted(kept.items()))


def record(
    history: dict[str, Any],
    day: date,
    text: str,
    offer_keys: list[str] | None = None,
    silent_channels: list[str] | None = None,
    time_label: str | None = None,
) -> dict[str, Any]:
    """Acrescenta um envio ao dia — não substitui.

    O agente roda duas vezes por dia. Gravando um envio por dia, o das 18h apagaria o das 8h e
    as ofertas da manhã voltariam à tona amanhã, que é exatamente o que o histórico existe para
    impedir.

    `silent_channels` é o que alimenta `chronic_silence`: sem guardar quem não entregou hoje,
    não há como saber amanhã que um canal parou de entregar semanas atrás.
    """
    history = dict(history)
    key = day.isoformat()
    entry = dict(history.get(key) or {})
    sends = list(entry.get("sends") or [])
    sends.append(
        {
            "time": time_label or now_local().strftime("%H:%M"),
            "text": text,
            "offers": list(offer_keys or []),
            "silent": list(silent_channels or []),
        }
    )
    entry["sends"] = sends
    history[key] = entry
    return history


def _sends(history: dict[str, Any], days: int) -> list[tuple[str, dict[str, Any]]]:
    """(dia, envio) do mais recente para o mais antigo, dentro da janela de dias."""
    cutoff = (now_local().date() - timedelta(days=days)).isoformat()
    rows: list[tuple[str, dict[str, Any]]] = []
    for day, entry in sorted(history.items(), reverse=True):
        if day < cutoff or not isinstance(entry, dict):
            continue
        for send in reversed(entry.get("sends") or []):
            if isinstance(send, dict):
                rows.append((day, send))
    return rows


def recent_sends(
    history: dict[str, Any],
    limit: int = DEFAULT_SENDS_IN_LOOKBACK,
    days: int = DEFAULT_LOOKBACK_DAYS,
) -> list[tuple[str, str]]:
    """(data, texto) dos envios mais recentes, do mais novo para o mais antigo."""
    rows = [(day, send.get("text", "")) for day, send in _sends(history, days) if send.get("text")]
    return rows[:limit]


def published_keys(
    history: dict[str, Any], window_days: int = DEFAULT_REPEAT_WINDOW_DAYS
) -> set[str]:
    """Chaves das ofertas que já foram ao ar na janela."""
    return {
        str(key)
        for _, send in _sends(history, window_days)
        for key in (send.get("offers") or [])
        if key
    }


def filter_seen_offers(
    payload: dict[str, Any],
    history: dict[str, Any],
    window_days: int = DEFAULT_REPEAT_WINDOW_DAYS,
) -> None:
    """Tira as ofertas cuja chave exata já foi publicada.

    É a trava barata e sem falso positivo: os canais republicam o mesmo post ao longo do dia e o
    mesmo achado circula entre canais com o texto idêntico. Não substitui a comparação por nome
    próprio (`filter_published_items`), que é quem pega a mesma oferta reescrita.
    """
    data = payload.get("promotions")
    if not isinstance(data, dict) or not data.get("offers"):
        return

    seen = published_keys(history, window_days)
    if not seen:
        return

    offers = data["offers"]
    kept = [offer for offer in offers if offer_key(offer.get("text", "")) not in seen]
    if len(kept) != len(offers):
        logger.info(
            "%s ofertas já publicadas nos últimos %s dias (chave exata)",
            len(offers) - len(kept),
            window_days,
        )
    data["offers"] = kept


# Tokens que começam frase ou ligam oração: aparecem em maiúscula sem identificar produto nenhum.
# Inclui o vocabulário fixo do anúncio de promoção, que abre quase toda mensagem em maiúscula
# ("Cupom", "Frete", "Menor preço") e faria duas ofertas sem nada em comum parecerem a mesma.
_NOT_ENTITIES = {
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
_WORD_RE = re.compile(r"[\wÀ-ÿ'’-]+")


def _fold(text: str) -> str:
    """Minúsculas e sem acento, para comparar oferta de canal com texto já enviado."""
    folded = unicodedata.normalize("NFKD", (text or "").lower())
    return "".join(char for char in folded if not unicodedata.combining(char))


def _entities(title: str) -> set[str]:
    """Nomes próprios do texto — marca, modelo e loja, que é o que identifica o produto.

    Comparar palavra a palavra não funciona: "Echo Dot 5ª geração por R$ 229 com frete grátis" e
    "SÓ HOJE! Echo Dot 5 saindo por 229 reais na Amazon" são a mesma oferta e quase não
    compartilham palavra comum. Nome próprio atravessa a reescrita do canal; o resto, não.

    O genitivo é aparado porque "Levi's" e "Levis" precisam bater.
    """
    entities: set[str] = set()
    for token in _ENTITY_RE.findall(title or ""):
        folded = re.sub(r"['’]s$", "", _fold(token)).strip("'’-")
        if len(folded) >= 3 and folded not in _NOT_ENTITIES:
            entities.add(folded)
    return entities


def _terms(text: str) -> set[str]:
    return set(_WORD_RE.findall(_fold(text)))


# Seção filtrada por cobertura, com o campo da lista, o texto que identifica o item e o mínimo
# a preservar. O mínimo é 0 de propósito, ao contrário do jornal: lá, ficar sem a seção Mundo
# era pior que repetir; aqui, reenviar o achado de ontem é o pior resultado possível — e quando
# não sobra oferta nenhuma o agente simplesmente não envia.
COVERAGE_SECTIONS: dict[str, tuple[str, str, int]] = {
    "promotions": ("offers", "text", 0),
}

# Só o começo do texto identifica o produto; o resto é frete, parcelamento e hashtag.
_OFFER_TEXT_LIMIT = 110


def filter_published_items(
    payload: dict[str, Any],
    history: dict[str, Any],
    limit: int = DEFAULT_SENDS_IN_LOOKBACK,
    days: int = DEFAULT_LOOKBACK_DAYS,
    min_entities: int = 2,
    min_ratio: float = 0.6,
    sections: dict[str, tuple[str, str, int]] | None = None,
) -> None:
    """Tira as ofertas que os envios recentes já anunciaram, comparando nomes próprios.

    A comparação é por proporção (`min_ratio`), não exata: o canal A escreve "Echo Dot 5ª
    geração" e o canal B escreve "Echo Dot 5", e verbo em início de frase entra em maiúscula
    como se fosse nome ("Baixou de novo o Kindle").

    Duas proteções contra falso positivo, herdadas do jornal:

    * nomes presentes em **todos** os envios recentes são pano de fundo e são ignorados —
      "Amazon" e "Mercado" saem em toda mensagem e não identificam oferta alguma;
    * pelo menos `min_entities` nomes precisam coincidir de fato, não só a proporção: com um
      nome só, "Samsung" apagaria qualquer oferta da marca pelo resto da semana.

    A terceira proteção do jornal — nunca esvaziar a seção — não vem junto, e por isso o mínimo
    em `COVERAGE_SECTIONS` é 0. Ver o comentário lá.
    """
    sends = [_terms(text) for _, text in recent_sends(history, limit, days) if text]
    if not sends:
        return

    background = set.intersection(*sends) if len(sends) > 1 else set()

    for section, (list_field, text_field, min_keep) in (sections or COVERAGE_SECTIONS).items():
        data = payload.get(section)
        if not isinstance(data, dict) or not data.get(list_field):
            continue

        original = data[list_field]
        kept: list[dict[str, Any]] = []
        dropped: list[dict[str, Any]] = []

        for item in original:
            text = str(item.get(text_field, ""))[:_OFFER_TEXT_LIMIT]
            marks = _entities(text) - background
            best = max((len(marks & send) for send in sends), default=0)
            if (
                len(marks) >= min_entities
                and best >= min_entities
                and best / len(marks) >= min_ratio
            ):
                dropped.append(item)
            else:
                kept.append(item)

        if not dropped:
            continue

        if len(kept) < min_keep:
            # Recompõe na ordem original: o round-robin já equilibrou os canais.
            restored = dropped[: min_keep - len(kept)]
            kept = [item for item in original if item in kept or item in restored]
            dropped = [item for item in dropped if item not in restored]

        logger.info(
            "%s: %s itens já anunciados nos últimos %s envios (%s)",
            section,
            len(dropped),
            len(sends),
            "; ".join(str(item.get(text_field, ""))[:50] for item in dropped[:3]),
        )
        data[list_field] = kept
        if "count" in data:
            data["count"] = len(kept)


def has_recent_sends(history: dict[str, Any], days: int = 3) -> bool:
    """Houve envio nos últimos dias?

    Serve ao alerta de resultado vazio: zero ofertas depois dos filtros só é sintoma quando
    normalmente há ofertas. Na primeira execução, ou depois de uma pausa, não é.
    """
    return bool(_sends(history, days))


def chronic_silence(
    history: dict[str, Any],
    today_silent: list[str],
    days: int = 3,
    min_sends: int = 3,
) -> list[str]:
    """Canais que não entregam nada há vários envios seguidos.

    A cadeia de canais é boa demais para o próprio bem: com dez fontes, uma que morreu não faz
    falta nenhuma no resultado e por isso não dispara o alerta de resultado. No jornal que deu
    origem a este projeto, foi assim que dois feeds ficaram 404 por três semanas. A conta é de
    interseção — só entra o canal que ficou de fora de **todos** os envios da janela, o de hoje
    incluído —, então um canal que teve um dia ruim não vira alerta.
    """
    rounds = [set(send.get("silent") or []) for _, send in _sends(history, days)]
    rounds.append(set(today_silent or []))
    if len(rounds) < min_sends:
        return []
    return sorted(set.intersection(*rounds))
