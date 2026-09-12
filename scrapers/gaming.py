"""Free game giveaways (GamerPower) and paid deals (CheapShark)."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import Any

from core.utils import ScraperResult, http_get_json, now_local

logger = logging.getLogger(__name__)

CHEAPSHARK_URL = "https://www.cheapshark.com/api/1.0/deals"
STORES_URL = "https://www.cheapshark.com/api/1.0/stores"
GAMERPOWER_URL = "https://www.gamerpower.com/api/giveaways"

FALLBACK_STORES = {
    "1": "Steam",
    "2": "GamersGate",
    "3": "GreenManGaming",
    "7": "GOG",
    "11": "Humble Store",
    "25": "Epic Games Store",
    "32": "Microsoft Store",
    "34": "Fanatical",
}


def _worth_usd(text: str | None) -> float:
    """'$19.99' -> 19.99. Serve para ordenar os giveaways pelo que vale mais a pena."""
    match = re.search(r"[\d.]+", (text or "").replace(",", ""))
    return float(match.group()) if match else 0.0


def _days_until(end_date: str | None) -> int | None:
    """Dias que faltam para o giveaway acabar, contados em Python.

    O modelo recebe a data crua e escreve "termina amanhã" olhando para um calendário que ele não
    tem — o mesmo tipo de alucinação temporal que já apareceu no futebol (item 6.4 do PLANO).
    """
    if not end_date or end_date == "N/A":
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
        try:
            return (datetime.strptime(end_date, fmt).date() - now_local().date()).days
        except ValueError:
            continue
    return None


def _ends_in_label(days: int | None) -> str | None:
    if days is None:
        return None
    if days <= 0:
        return "último dia"
    if days == 1:
        return "termina amanhã"
    return f"termina em {days} dias"


async def _fetch_free_games(settings) -> list[dict[str, Any]]:
    """Giveaways ativos de jogos.

    O CheapShark ordenado por desconto quase nunca traz algo de graça — a seção prometia
    "Games Grátis" e entregava sete títulos obscuros a US$ 0,51, os mesmos por semanas.
    """
    gaming_cfg = settings.get("gaming") or {}
    gp_cfg = gaming_cfg.get("gamerpower") or {}
    if not gp_cfg.get("enabled", True):
        return []

    giveaways = await http_get_json(
        GAMERPOWER_URL, settings, params={"type": gp_cfg.get("type", "game")}
    )

    platform_filter = [p.lower() for p in (gp_cfg.get("platforms") or [])]
    max_items = int(gp_cfg.get("max_items", 5))
    # Um giveaway de US$ 0,99 ocupa a mesma linha de um de US$ 25 e não vale o clique: é o que
    # enchia a seção de títulos obscuros repetidos por semanas.
    min_worth = float(gp_cfg.get("min_worth_usd", 0))

    selected: list[dict[str, Any]] = []
    for giveaway in giveaways:
        if str(giveaway.get("status", "")).lower() != "active":
            continue

        platforms = str(giveaway.get("platforms", ""))
        if platform_filter and not any(p in platforms.lower() for p in platform_filter):
            continue

        worth_value = _worth_usd(giveaway.get("worth"))
        days_left = _days_until(giveaway.get("end_date"))
        # O que está acabando entra mesmo abaixo do piso de valor: aviso de último dia é
        # justamente o que só serve se chegar hoje.
        if worth_value < min_worth and not (days_left is not None and days_left <= 1):
            continue

        selected.append(
            {
                "id": giveaway.get("id"),
                "title": giveaway.get("title", "").replace(" Giveaway", "").strip(),
                "worth": giveaway.get("worth"),
                "worth_value": worth_value,
                "platforms": platforms,
                "ends_at": giveaway.get("end_date") if giveaway.get("end_date") != "N/A" else None,
                "days_left": days_left,
                "ends_in": _ends_in_label(days_left),
                "url": giveaway.get("open_giveaway_url") or giveaway.get("gamerpower_url"),
            }
        )

    # Os que acabam primeiro no topo e, empatados, os mais valiosos: um jogo de US$ 20 de graça
    # interessa mais que um de US$ 2, mas nenhum dos dois interessa depois que expirou.
    selected.sort(key=lambda item: (item["days_left"] is None, item["days_left"] or 0, -item["worth_value"]))
    return selected[:max_items]


async def _fetch_store_map(settings) -> dict[str, str]:
    try:
        stores = await http_get_json(STORES_URL, settings)
        return {
            store.get("storeID"): store.get("storeName")
            for store in stores
            if store.get("storeID") and store.get("storeName")
        }
    except Exception as exc:
        logger.warning("Failed to fetch CheapShark stores dynamically: %s", exc)
        return FALLBACK_STORES


async def _fetch_deals(settings) -> list[dict[str, Any]]:
    gaming_cfg = settings.get("gaming") or {}
    cheapshark_cfg = gaming_cfg.get("cheapshark") or {}
    if not cheapshark_cfg.get("enabled", True):
        return []

    store_map = await _fetch_store_map(settings)

    # "Deal Rating" é a nota própria do CheapShark, que pondera desconto *e* qualidade do jogo.
    # `sortBy=Savings` ordenava só pelo desconto e por isso devolvia sempre o mesmo shovelware:
    # o topo dos 96% off é estático porque ninguém compra aqueles jogos e o preço não muda.
    params: dict[str, Any] = {
        "sortBy": cheapshark_cfg.get("sort_by", "Deal Rating"),
        "pageSize": 60,
        "onSale": 1,
    }
    min_rating = cheapshark_cfg.get("min_steam_rating")
    if min_rating is not None:
        params["steamRating"] = min_rating
    # AAA=1 restringe a jogos de preço cheio a partir de US$ 29 — o corte mais eficaz contra
    # títulos obscuros, porque shovelware nunca lançou nessa faixa.
    if cheapshark_cfg.get("aaa_only", True):
        params["AAA"] = 1
    if "max_price" in cheapshark_cfg:
        params["upperPrice"] = cheapshark_cfg["max_price"]

    deals = await http_get_json(CHEAPSHARK_URL, settings, params=params)

    min_savings = float(cheapshark_cfg.get("min_savings_percent", 60))
    max_deals = int(cheapshark_cfg.get("max_deals", 5))
    # Devolve mais candidatos do que serão exibidos: `apply_repeat_policy` remove os que já
    # saíram nos últimos dias, e sem folga a seção encolheria em vez de trazer outros.
    pool = max(max_deals, int(cheapshark_cfg.get("candidate_pool", max_deals * 3)))
    # Nota alta com 40 avaliações é ruído; é o número de avaliações que separa "jogo bom" de
    # "jogo que ninguém jogou".
    min_reviews = int(cheapshark_cfg.get("min_steam_reviews", 0))
    min_metacritic = int(cheapshark_cfg.get("min_metacritic", 0))

    results: list[dict[str, Any]] = []
    seen_titles: set[str] = set()

    for deal in deals:
        # O mesmo jogo aparece uma vez por loja; como a lista já vem ordenada, a primeira
        # ocorrência é a melhor oferta.
        title_key = (deal.get("title") or "").strip().lower()
        if not title_key or title_key in seen_titles:
            continue

        try:
            sale_price = float(deal.get("salePrice") or 0.0)
            normal_price = float(deal.get("normalPrice") or 0.0)
            savings = float(deal.get("savings") or 0.0)
            rating = int(deal.get("steamRatingPercent") or 0)
            reviews = int(deal.get("steamRatingCount") or 0)
            metacritic = int(deal.get("metacriticScore") or 0)
        except (TypeError, ValueError):
            continue

        if sale_price > 0.0 and savings < min_savings:
            continue
        if min_reviews and reviews < min_reviews:
            continue
        # Metacritic 0 significa "sem nota", não "nota péssima": o CheapShark devolve 0 para
        # jogo nunca avaliado, e descartá-lo por isso eliminaria indie bom.
        if min_metacritic and metacritic and metacritic < min_metacritic:
            continue

        seen_titles.add(title_key)
        results.append(
            {
                "title": deal.get("title"),
                "is_free": sale_price == 0.0,
                "sale_price_usd": f"{sale_price:.2f}",
                "normal_price_usd": f"{normal_price:.2f}",
                "savings_percent": f"{savings:.0f}",
                "steam_rating_percent": rating or None,
                "steam_rating_text": deal.get("steamRatingText") or None,
                "steam_rating_count": reviews or None,
                "metacritic": metacritic or None,
                "store": store_map.get(deal.get("storeID"), f"Loja {deal.get('storeID')}"),
                "url": f"https://www.cheapshark.com/redirect?dealID={deal.get('dealID')}",
            }
        )
        if len(results) >= pool:
            break

    return results


async def fetch(settings) -> ScraperResult:
    section = "gaming"
    cheapshark_cfg = (settings.get("gaming") or {}).get("cheapshark") or {}
    data: dict[str, Any] = {
        "free_games": [],
        "deals": [],
        # Consumido e removido por `apply_repeat_policy`: quantas ofertas sobrevivem ao corte
        # de repetição é decisão de lá, mas quantas cabem na seção é configuração daqui.
        "deals_display_limit": int(cheapshark_cfg.get("max_deals", 5)),
    }
    errors: list[str] = []

    try:
        data["free_games"] = await _fetch_free_games(settings)
    except Exception as exc:
        logger.warning("GamerPower fetch failed: %s", exc)
        errors.append(f"GamerPower: {exc}")

    try:
        data["deals"] = await _fetch_deals(settings)
    except Exception as exc:
        logger.warning("CheapShark fetch failed: %s", exc)
        errors.append(f"CheapShark: {exc}")

    logger.info(
        "gaming: %s jogos grátis, %s ofertas", len(data["free_games"]), len(data["deals"])
    )

    if errors and not data["free_games"] and not data["deals"]:
        return ScraperResult(section=section, status="error", error="; ".join(errors))

    # Zero ofertas qualificadas é um resultado legítimo, não uma falha: alertar sobre isso
    # todo dia treinaria você a ignorar os alertas.
    return ScraperResult(
        section=section,
        status="partial" if errors else "ok",
        data=data,
        error="; ".join(errors) if errors else None,
    )
