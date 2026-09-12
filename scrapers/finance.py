"""Finance data: multi-asset quotes filled asset-by-asset across several sources."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable

from core.utils import (
    ScraperResult,
    format_number_pt_br,
    http_get_json,
)

logger = logging.getLogger(__name__)

AWESOMEAPI_URL = "https://economia.awesomeapi.com.br/json/last/{pairs}"
HG_BRASIL_URL = "https://api.hgbrasil.com/finance"

ASSET_PAIRS = [
    ("USD-BRL", "usd_brl", "USD"),
    ("EUR-BRL", "eur_brl", "EUR"),
    ("ARS-BRL", "ars_brl", "ARS"),
    ("BTC-BRL", "btc_brl", "BTC"),
]

ASSET_KEYS = ["usd_brl", "eur_brl", "ars_brl", "btc_brl", "ibovespa"]

YFINANCE_TICKERS = {
    "usd_brl": "USDBRL=X",
    "eur_brl": "EURBRL=X",
    "ars_brl": "ARSBRL=X",
    "ibovespa": "^BVSP",
    # BTC-BRL foi descontinuado no Yahoo ("possibly delisted"); é derivado em _derive_btc_brl.
}


def _has_value(quote: dict[str, Any] | None) -> bool:
    if not quote:
        return False
    return quote.get("bid") is not None or quote.get("points") is not None


# --------------------------------------------------------------------------- fontes


def _quote_from_awesome(body: dict[str, Any], pair: str) -> dict[str, Any]:
    key = pair.replace("-", "")
    quote = body.get(key) or {}
    return {
        "pair": pair,
        "bid": quote.get("bid"),
        "ask": quote.get("ask"),
        "variation_percent": quote.get("pctChange"),
        "timestamp": quote.get("create_date") or quote.get("timestamp"),
        "source": "awesomeapi",
    }


async def _fetch_awesomeapi(settings, missing: list[str]) -> dict[str, Any]:
    wanted = [(pair, key) for pair, key, _ in ASSET_PAIRS if key in missing]
    if not wanted:
        return {}

    url = AWESOMEAPI_URL.format(pairs=",".join(pair for pair, _ in wanted))
    headers = {}
    if settings.awesomeapi_token:
        headers["x-api-key"] = settings.awesomeapi_token

    body = await http_get_json(url, settings, headers=headers)
    return {key: _quote_from_awesome(body, pair) for pair, key in wanted}


async def _fetch_hg_brasil(settings, missing: list[str]) -> dict[str, Any]:
    body = await http_get_json(HG_BRASIL_URL, settings, params={"key": "free"})
    results = body.get("results") or {}
    currencies = results.get("currencies") or {}
    bitcoin = results.get("bitcoin") or {}
    ibov = (results.get("stocks") or {}).get("IBOVESPA") or {}

    quotes: dict[str, Any] = {}
    for _, key, currency_code in ASSET_PAIRS:
        if currency_code == "BTC":
            # A chave "free" costuma devolver o bloco bitcoin vazio; nesse caso o ativo fica
            # faltando e a próxima fonte da cadeia preenche.
            source = next(iter(bitcoin.values()), {}) if isinstance(bitcoin, dict) else {}
            quotes[key] = {
                "pair": "BTC-BRL",
                "bid": source.get("buy") if isinstance(source, dict) else None,
                "ask": source.get("sell") if isinstance(source, dict) else None,
                "source": "hgbrasil",
            }
        else:
            currency = currencies.get(currency_code) or {}
            quotes[key] = {
                "pair": f"{currency_code}-BRL",
                "bid": currency.get("buy"),
                "ask": currency.get("sell"),
                "variation_percent": currency.get("variation"),
                "source": "hgbrasil",
            }

    quotes["ibovespa"] = {
        "symbol": "IBOVESPA",
        "points": ibov.get("points"),
        "variation_percent": ibov.get("variation"),
        "source": "hgbrasil",
    }
    return quotes


def _fetch_yfinance_sync(keys: list[str]) -> dict[str, Any]:
    import yfinance as yf

    pair_labels = {key: pair for pair, key, _ in ASSET_PAIRS}
    quotes: dict[str, Any] = {}

    for key in keys:
        ticker = YFINANCE_TICKERS.get(key)
        if not ticker:
            continue
        history = yf.Ticker(ticker).history(period="5d")
        if history.empty:
            continue

        close = float(history.iloc[-1]["Close"])
        previous = float(history.iloc[-2]["Close"]) if len(history) >= 2 else None

        if key == "ibovespa":
            quotes[key] = {
                "symbol": "IBOVESPA",
                "points": round(close, 2),
                "previous_close": round(previous, 2) if previous else None,
                "source": "yfinance",
            }
        else:
            quotes[key] = {
                "pair": pair_labels.get(key, ticker),
                "bid": round(close, 4),
                "ask": round(close, 4),
                "previous_close": round(previous, 4) if previous else None,
                "source": "yfinance",
            }
    return quotes


async def _fetch_yfinance(settings, missing: list[str]) -> dict[str, Any]:
    # Só busca o que falta: cada ticker é uma chamada de rede síncrona.
    return await asyncio.to_thread(_fetch_yfinance_sync, missing)


def _yfinance_close_sync(ticker: str) -> float | None:
    import yfinance as yf

    history = yf.Ticker(ticker).history(period="5d")
    return None if history.empty else float(history.iloc[-1]["Close"])


async def _derive_btc_brl(quotes: dict[str, Any]) -> None:
    """Deriva BTC-BRL de BTC-USD x USD-BRL.

    O par direto saiu do Yahoo e a AwesomeAPI — única outra fonte de BTC — responde 429 na VPS,
    então sem esta derivação o Bitcoin fica permanentemente ausente em produção.
    """
    usd = quotes.get("usd_brl") or {}
    if not usd.get("bid"):
        return

    try:
        btc_usd = await asyncio.to_thread(_yfinance_close_sync, "BTC-USD")
    except Exception as exc:
        logger.warning("BTC-USD fetch failed: %s", exc)
        return

    if btc_usd is None:
        return

    quotes["btc_brl"] = {
        "pair": "BTC-BRL",
        "bid": round(btc_usd * float(usd["bid"]), 2),
        "source": "derivado de BTC-USD x USD-BRL",
    }
    logger.info("BTC-BRL derivado de BTC-USD x USD-BRL")


# Ordem por confiabilidade observada em produção, não por preferência:
# a AwesomeAPI responde 429 na VPS desde 11/06/2026 e ficou por último.
SOURCES: list[tuple[str, Callable[..., Awaitable[dict[str, Any]]]]] = [
    ("HG Brasil", _fetch_hg_brasil),
    ("yfinance", _fetch_yfinance),
    ("AwesomeAPI", _fetch_awesomeapi),
]


async def _fetch_quotes(settings) -> tuple[dict[str, Any], list[str]]:
    """Preenche ativo a ativo, e não tudo-ou-nada.

    Antes, bastava o USD vir preenchido para a função retornar — se a fonte não trouxesse o BTC,
    ninguém consultava as demais e o jornal saía com "Bitcoin: cotação indisponível".
    """
    quotes: dict[str, Any] = {}

    for label, fetcher in SOURCES:
        missing = [key for key in ASSET_KEYS if not _has_value(quotes.get(key))]
        if not missing:
            break

        try:
            fresh = await fetcher(settings, missing)
        except Exception as exc:
            # Só log: uma fonte que falha mas é coberta pela seguinte não degrada o jornal, e
            # alertar sobre ela todo dia (a AwesomeAPI está bloqueada há 47 dias) treinaria
            # o leitor a ignorar os alertas. O que importa é o que ficou faltando no fim.
            logger.warning("%s fetch failed: %s", label, exc)
            continue

        filled = [key for key in missing if _has_value(fresh.get(key))]
        for key in filled:
            quotes[key] = fresh[key]
        if filled:
            logger.info("%s forneceu: %s", label, ", ".join(filled))

    if not _has_value(quotes.get("btc_brl")):
        await _derive_btc_brl(quotes)

    still_missing = [key for key in ASSET_KEYS if not _has_value(quotes.get(key))]
    errors = [f"sem cotação para: {', '.join(still_missing)}"] if still_missing else []

    return quotes, errors


# --------------------------------------------------------------------------- formatação


def _fill_variation(quote: dict[str, Any]) -> None:
    """Calcula a variação a partir do fechamento anterior quando a fonte não a informa."""
    if quote.get("variation_percent") is not None or not quote.get("previous_close"):
        return
    try:
        previous = float(quote["previous_close"])
        quote["variation_percent"] = round((float(quote["bid"]) - previous) / previous * 100, 2)
    except (TypeError, ValueError, ZeroDivisionError):
        pass


def _invert_variation(variation: Any) -> float | None:
    """Variação do par invertido: se ARS-BRL sobe p%, o quanto 1 BRL compra em ARS cai.

    Sem isso o peso apareceria subindo justamente nos dias em que enfraqueceu.
    """
    if variation is None:
        return None
    try:
        value = float(variation)
    except (TypeError, ValueError):
        return None
    return round(-value / (1 + value / 100), 2)


def _variation_suffix(quote: dict[str, Any]) -> str:
    """' (+0,10%)'. Sem isso o dólar parece congelado: 5,1288 e 5,116 arredondam para o
    mesmo 'R$ 5,12' em dias seguidos, e a variação é justamente a informação do dia."""
    variation = quote.get("variation_percent")
    if variation is None:
        return ""
    try:
        value = float(variation)
    except (TypeError, ValueError):
        return ""
    # O sinal sai daqui, não da formatação do número: -0.0 (que surge ao inverter uma variação
    # zero) é >= 0 em Python e produzia "+-0,00%".
    sign = "+" if value > 0 else "-" if value < 0 else ""
    return f" ({sign}{format_number_pt_br(abs(value), 2)}%)"


def _decorate(quotes: dict[str, Any]) -> None:
    """Acrescenta o texto pronto para exibição a cada ativo.

    Formatação numérica e cálculo de variação são feitos aqui, em Python: entregar float cru ao
    modelo produzia "R$ 0.0034278" e percentuais inventados por aritmética dele.
    """
    for key, decimals in (("usd_brl", 2), ("eur_brl", 2), ("btc_brl", 0)):
        quote = quotes.get(key) or {}
        if quote.get("bid") is None:
            continue
        _fill_variation(quote)
        quote["display"] = (
            f"R$ {format_number_pt_br(float(quote['bid']), decimals)}{_variation_suffix(quote)}"
        )

    ars = quotes.get("ars_brl") or {}
    if ars.get("bid"):
        try:
            # 0,0034 BRL por peso não diz nada a ninguém; o inverso é legível.
            _fill_variation(ars)
            inverted = dict(ars, variation_percent=_invert_variation(ars.get("variation_percent")))
            ars["display"] = (
                f"1 BRL = {format_number_pt_br(1 / float(ars['bid']), 2)} ARS"
                f"{_variation_suffix(inverted)}"
            )
        except (ValueError, ZeroDivisionError):
            pass

    ibov = quotes.get("ibovespa") or {}
    points = ibov.get("points")
    if points is not None:
        variation = ibov.get("variation_percent")
        if variation is None and ibov.get("previous_close"):
            try:
                previous = float(ibov["previous_close"])
                variation = round((float(points) - previous) / previous * 100, 2)
                ibov["variation_percent"] = variation
            except (ValueError, ZeroDivisionError):
                variation = None

        ibov["display"] = f"{format_number_pt_br(float(points), 0)} pts{_variation_suffix(ibov)}"


async def fetch(settings) -> ScraperResult:
    section = "finance"
    data: dict[str, Any] = {}
    errors: list[str] = []

    quotes, quote_errors = await _fetch_quotes(settings)
    _decorate(quotes)
    errors.extend(quote_errors)
    data.update(quotes)

    has_quotes = any(_has_value(data.get(key)) for key in ASSET_KEYS)
    if not has_quotes:
        return ScraperResult(section=section, status="error", error="; ".join(errors) or "No finance data")

    return ScraperResult(
        section=section,
        status="partial" if errors else "ok",
        data=data,
        error="; ".join(errors) if errors else None,
    )
