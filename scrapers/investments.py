"""Indicadores de referência do Banco Central, para a seção de sugestão de investimentos.

A seção existe para responder "onde eu colocaria dinheiro hoje?" com número em vez de palpite.
Por isso a fonte é a série oficial do BCB (SGS, pública e sem chave) e **todo cálculo derivado
acontece aqui, em Python** — juro real, poupança anualizada e a comparação poupança × CDI são
exatamente o tipo de aritmética que o modelo erra e ninguém confere.

O que vai ao prompt é uma "ideia do dia" já redigida (`idea_of_the_day`), escolhida em Python
(`core/history.apply_investment_idea`) entre as afirmações prontas de `_investment_ideas` — a
menos usada nos últimos dias, para não repetir a mesma toda manhã. O modelo só reescreve a frase;
não inventa número, não calcula rendimento e não recomenda ativo específico.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from core.utils import ScraperResult, format_number_pt_br, http_get_json

logger = logging.getLogger(__name__)

SGS_URL = "https://api.bcb.gov.br/dados/serie/bcdata.sgs.{code}/dados/ultimos/1"

# Séries do SGS usadas quando o config.yaml não define as suas. O `unit` diz como o valor
# chega: "a.a." é taxa anual, "a.m." é mensal, "12m" é acumulado em doze meses.
DEFAULT_SERIES: dict[str, dict[str, Any]] = {
    "selic": {"code": 432, "label": "Selic (meta)", "unit": "a.a."},
    "cdi": {"code": 4389, "label": "CDI", "unit": "a.a."},
    "ipca_12m": {"code": 13522, "label": "IPCA (12 meses)", "unit": "12m"},
    "poupanca": {"code": 195, "label": "Poupança", "unit": "a.m."},
}

UNIT_SUFFIX = {"a.a.": "% a.a.", "a.m.": "% a.m.", "12m": "% em 12 meses"}


def _to_float(value: Any) -> float | None:
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None


async def _fetch_series(settings, key: str, spec: dict[str, Any]) -> dict[str, Any] | None:
    code = spec.get("code")
    if code is None:
        return None

    body = await http_get_json(SGS_URL.format(code=code), settings, params={"formato": "json"})
    if not isinstance(body, list) or not body:
        raise ValueError(f"série {code} vazia")

    last = body[-1]
    value = _to_float(last.get("valor"))
    if value is None:
        raise ValueError(f"série {code} sem valor numérico")

    unit = spec.get("unit", "a.a.")
    return {
        "key": key,
        "label": spec.get("label", key),
        "value": value,
        "date": last.get("data"),
        "unit": unit,
        "display": f"{format_number_pt_br(value, 2)}{UNIT_SUFFIX.get(unit, '%')}",
    }


def _annualize(monthly_percent: float) -> float:
    return ((1 + monthly_percent / 100) ** 12 - 1) * 100


def _real_rate(nominal_percent: float, inflation_percent: float) -> float:
    """Juro real pela fórmula de Fisher, não pela subtração.

    13,90 − 4,64 dá 9,26 e está errado: o correto é 8,84. A diferença é pequena o bastante para
    passar despercebida e grande o bastante para o número do jornal ficar errado todo dia.
    """
    return ((1 + nominal_percent / 100) / (1 + inflation_percent / 100) - 1) * 100


def _derive(indicators: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Métricas que só existem cruzando duas séries."""
    derived: dict[str, Any] = {}

    cdi = (indicators.get("cdi") or {}).get("value")
    ipca = (indicators.get("ipca_12m") or {}).get("value")
    poupanca_mensal = (indicators.get("poupanca") or {}).get("value")

    if cdi is not None and ipca is not None:
        real = _real_rate(cdi, ipca)
        derived["juro_real"] = {
            "value": round(real, 2),
            "display": f"{format_number_pt_br(real, 2)}% a.a. acima da inflação",
        }

    if poupanca_mensal is not None:
        anual = _annualize(poupanca_mensal)
        entry: dict[str, Any] = {
            "value": round(anual, 2),
            "display": f"{format_number_pt_br(anual, 2)}% a.a. equivalentes",
        }
        if cdi:
            share = anual / cdi * 100
            entry["percent_of_cdi"] = round(share, 0)
            entry["display"] += f" — cerca de {format_number_pt_br(share, 0)}% do CDI"
        derived["poupanca_anual"] = entry

    return derived


def _build_stock_candidate(entry: dict[str, Any], closes: list[float]) -> dict[str, Any] | None:
    """Monta o candidato de ação/ETF pronto para o prompt, a partir dos fechamentos de ~1 ano.

    Preço, variação do dia e faixa de 12 meses são calculados aqui, em Python — mesmo princípio
    de `_decorate` em finance.py: o modelo só copia string pronta, nunca calcula ele mesmo.
    """
    if len(closes) < 2:
        return None

    current, previous = closes[-1], closes[-2]
    try:
        pct = (current - previous) / previous * 100
    except ZeroDivisionError:
        return None
    sign = "+" if pct > 0 else "-" if pct < 0 else ""
    low, high = min(closes), max(closes)

    return {
        "ticker": entry["ticker"],
        "name": entry.get("name", entry["ticker"]),
        "class": entry.get("class", "ação"),
        "display": (
            f"R$ {format_number_pt_br(current, 2)} "
            f"({sign}{format_number_pt_br(abs(pct), 2)}%)"
        ),
        "range_display": (
            f"mín. R$ {format_number_pt_br(low, 2)} / máx. R$ {format_number_pt_br(high, 2)} "
            "em 12 meses"
        ),
    }


def _fetch_stock_pool_sync(pool: list[dict[str, Any]]) -> dict[str, list[float]]:
    """Um único download em lote para o pool inteiro — não N chamadas sequenciais.

    yfinance já é a fonte mais lenta da cadeia de cotações (finance.py); buscar ação por ação
    multiplicaria esse custo pelo tamanho do pool.
    """
    import yfinance as yf

    symbols = [entry["yahoo_symbol"] for entry in pool]
    history = yf.download(
        symbols, period="1y", group_by="ticker", auto_adjust=True, progress=False, threads=True
    )

    closes: dict[str, list[float]] = {}
    for symbol in symbols:
        try:
            series = history[symbol]["Close"] if len(symbols) > 1 else history["Close"]
        except (KeyError, TypeError):
            continue
        closes[symbol] = [float(value) for value in series.dropna().tolist()]
    return closes


async def _fetch_stocks(pool: list[dict[str, Any]]) -> dict[str, list[float]]:
    return await asyncio.to_thread(_fetch_stock_pool_sync, pool)


def _investment_ideas(
    indicators: dict[str, dict[str, Any]], derived: dict[str, Any]
) -> list[dict[str, str]]:
    """Afirmações prontas, com os números já formatados, cada uma com um `id` estável.

    O `id` não muda de um dia para o outro (só os números da frase mudam), então serve de chave
    para o rodízio em `core/history.apply_investment_idea`. É o mesmo princípio do campo `display`
    das cotações, aplicado a frases: o modelo só reescreve, nunca produz um número que não esteja
    aqui.
    """
    ideas: list[dict[str, str]] = []

    selic = indicators.get("selic")
    cdi = indicators.get("cdi")
    ipca = indicators.get("ipca_12m")
    juro_real = derived.get("juro_real")
    poupanca = derived.get("poupanca_anual")

    if selic:
        ideas.append({
            "id": "selic",
            "text": (
                f"A Selic está em {selic['display']}, então pós-fixados atrelados ao CDI "
                f"(Tesouro Selic, CDB de liquidez diária) acompanham essa taxa."
            ),
        })
    if cdi and juro_real and ipca:
        ideas.append({
            "id": "juro_real",
            "text": (
                f"O CDI roda a {cdi['display']} contra uma inflação de {ipca['display']}: sobram "
                f"{format_number_pt_br(juro_real['value'], 2)}% ao ano de juro real."
            ),
        })
    elif cdi:
        ideas.append({"id": "cdi", "text": f"O CDI roda a {cdi['display']}."})

    if poupanca:
        # A partir do valor, não do `display`: aquele já traz a comparação com o CDI embutida e
        # reaproveitá-lo aqui repetia "60% do CDI" duas vezes na mesma frase.
        share = poupanca.get("percent_of_cdi")
        comparison = (
            f", ou {format_number_pt_br(float(share), 0)}% do CDI — perde de um CDB de 100% do "
            "CDI com a mesma liquidez"
            if share
            else ""
        )
        ideas.append({
            "id": "poupanca",
            "text": (
                f"A poupança rende o equivalente a "
                f"{format_number_pt_br(poupanca['value'], 2)}% a.a.{comparison}."
            ),
        })

    if ipca:
        ideas.append({
            "id": "inflacao",
            "text": (
                f"Qualquer aplicação que renda menos que a inflação ({ipca['display']}) está "
                "perdendo poder de compra."
            ),
        })

    if juro_real and juro_real["value"] >= 5:
        ideas.append({
            "id": "ipca_plus",
            "text": (
                "Com juro real alto, títulos indexados à inflação (Tesouro IPCA+) travam esse "
                "ganho real por prazos longos, ao custo de oscilação no meio do caminho."
            ),
        })

    return ideas


async def fetch(settings) -> ScraperResult:
    section = "investments"
    cfg = settings.get("investments") or {}

    if not cfg.get("enabled", True):
        return ScraperResult(section=section, status="ok", data={})

    series_cfg = cfg.get("series") or DEFAULT_SERIES

    tasks = {
        key: asyncio.create_task(_fetch_series(settings, key, spec))
        for key, spec in series_cfg.items()
        if isinstance(spec, dict)
    }

    indicators: dict[str, dict[str, Any]] = {}
    errors: list[str] = []

    for key, task in tasks.items():
        try:
            result = await task
            if result:
                indicators[key] = result
        except Exception as exc:
            logger.warning("Série %s do BCB falhou: %s", key, exc)
            errors.append(f"{key}: {exc}")

    if not indicators:
        return ScraperResult(
            section=section,
            status="error",
            error="; ".join(errors) or "nenhum indicador do BCB disponível",
        )

    derived = _derive(indicators)
    ideas = _investment_ideas(indicators, derived)
    data: dict[str, Any] = {
        "indicators": indicators,
        "derived": derived,
        # Pool de ideias; `core/history.apply_investment_idea` escolhe uma e a move para
        # `idea_of_the_day`, removendo o resto antes de o payload chegar ao modelo.
        "ideas": ideas,
    }

    logger.info(
        "investments: %s indicadores, %s ideias no pool",
        len(indicators),
        len(ideas),
    )

    stock_errors: list[str] = []
    stock_pool = cfg.get("stock_pool") or []
    if stock_pool:
        try:
            closes = await _fetch_stocks(stock_pool)
            candidates = [
                candidate
                for entry in stock_pool
                if (candidate := _build_stock_candidate(entry, closes.get(entry["yahoo_symbol"], [])))
            ]
            if candidates:
                data["stocks"] = {
                    "candidates": candidates,
                    "display_limit": int(cfg.get("stocks_shown", 3)),
                }
                logger.info("investments: %s ações/ETFs no pool de destaques", len(candidates))
            else:
                stock_errors.append("stock_pool: nenhuma cotação de ação disponível")
        except Exception as exc:
            logger.warning("Stock pool fetch failed: %s", exc)
            stock_errors.append(f"stock_pool: {exc}")

    # Uma série ou o pool de ações faltando não degrada a seção — as outras seguram os pontos de
    # apoio, e alertar sobre isso todo dia treinaria você a ignorar os alertas (ver "Alerta é
    # sobre resultado").
    all_errors = errors + stock_errors
    if all_errors:
        logger.info("Dados ausentes hoje em investments: %s", "; ".join(all_errors))

    return ScraperResult(
        section=section,
        status="partial" if stock_errors else "ok",
        data=data,
        error="; ".join(stock_errors) if stock_errors else None,
    )
