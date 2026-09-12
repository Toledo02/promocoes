"""Futebol: jogos via football-data.org e notícias relevantes via GE Globo Esporte.

Divisão de responsabilidades: a API entrega o que o scraping nunca deu — adversário, data e
horário do próximo jogo, e o placar do último. O GE continua servindo às notícias, mas agora
filtradas por relevância: contratação, lesão, desfalque e escalação passam; o resto não.

A seção inteira é omitida quando não há jogo próximo nem notícia relevante, em vez de repetir
as mesmas manchetes por dias.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
from bs4 import BeautifulSoup

from core.utils import BROWSER_HEADERS, ScraperResult, TIMEZONE, http_get_json, http_get_text

logger = logging.getLogger(__name__)

FOOTBALL_DATA_BASE = "https://api.football-data.org/v4"

WEEKDAYS_PT = ("segunda", "terça", "quarta", "quinta", "sexta", "sábado", "domingo")


# --------------------------------------------------------------------------- API de jogos


def _api_headers(settings) -> dict[str, str]:
    return {"X-Auth-Token": settings.football_data_token}


async def _resolve_team_ids(settings, names: list[str], competitions: list[str], cache_path: Path) -> dict[str, int]:
    """Descobre os IDs dos times pelo nome, uma vez, e guarda em cache.

    Evita obrigar você a garimpar IDs numéricos na documentação da API.
    """
    cache: dict[str, int] = {}
    if cache_path.exists():
        try:
            cache = {k: int(v) for k, v in json.loads(cache_path.read_text(encoding="utf-8")).items()}
        except (json.JSONDecodeError, OSError, ValueError, TypeError):
            cache = {}

    missing = [name for name in names if name not in cache]
    if not missing:
        return cache

    for code in competitions:
        if not missing:
            break
        try:
            body = await http_get_json(
                f"{FOOTBALL_DATA_BASE}/competitions/{code}/teams",
                settings,
                headers=_api_headers(settings),
            )
        except Exception as exc:
            logger.warning("Não foi possível listar times de %s: %s", code, exc)
            continue

        for team in body.get("teams", []):
            candidates = {
                str(team.get("name", "")).lower(),
                str(team.get("shortName", "")).lower(),
                str(team.get("tla", "")).lower(),
            }
            for name in list(missing):
                if name.lower() in candidates or any(name.lower() in c for c in candidates if c):
                    cache[name] = int(team["id"])
                    missing.remove(name)
                    logger.info("Time '%s' resolvido para id %s em %s", name, cache[name], code)

    if missing:
        logger.warning(
            "Sem id para: %s. Defina 'api_id' em config.yaml se a API não os encontrar.",
            ", ".join(missing),
        )

    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("Falha ao gravar o cache de times: %s", exc)

    return cache


def _format_kickoff(utc_date: str) -> str | None:
    try:
        moment = datetime.fromisoformat(utc_date.replace("Z", "+00:00")).astimezone(TIMEZONE)
    except (ValueError, AttributeError):
        return None
    return f"{WEEKDAYS_PT[moment.weekday()]} {moment:%d/%m} às {moment:%Hh%M}"


def _team_label(side: dict[str, Any], own_id: int | None, own_label: str | None) -> str | None:
    """Nome do time no placar.

    Para o *seu* time usa o nome do config: a API registra o Athletico como "CA Paranaense",
    e "Paranaense x Corinthians" não é como você chama o jogo.
    """
    if own_id and own_label and side.get("id") == own_id:
        return own_label
    return side.get("shortName") or side.get("name")


def _format_match(
    match: dict[str, Any], finished: bool, own_id: int | None = None, own_label: str | None = None
) -> str | None:
    home = _team_label(match.get("homeTeam") or {}, own_id, own_label)
    away = _team_label(match.get("awayTeam") or {}, own_id, own_label)
    competition = (match.get("competition") or {}).get("name", "")
    if not home or not away:
        return None

    if finished:
        full_time = (match.get("score") or {}).get("fullTime") or {}
        home_goals, away_goals = full_time.get("home"), full_time.get("away")
        placar = f"{home} {home_goals} x {away_goals} {away}" if home_goals is not None else f"{home} x {away}"
        when = _format_kickoff(match.get("utcDate", ""))
        return f"{placar} ({competition}{', ' + when if when else ''})"

    when = _format_kickoff(match.get("utcDate", ""))
    return f"{home} x {away} — {competition}{', ' + when if when else ''}"


def _is_out_of_plan(exc: Exception) -> bool:
    """403 da football-data.org significa "seu plano não cobre isso", não "deu erro".

    A Seleção Brasileira joga Copa e amistosos, competições fora do plano gratuito, então o 403
    é a resposta certa e definitiva — não vai mudar amanhã. Tratá-lo como falha fazia a seção
    voltar `partial` todo santo dia e disparar o alerta de fontes com falha, que é exatamente o
    mecanismo que a Fase 1 criou para não ser ignorado. Alerta é sobre resultado, não sobre fonte:
    o time degrada para só-notícias em silêncio, igual ao que já acontece sem token.
    """
    return isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code == 403


async def _team_matches(settings, team_id: int, status: str, limit: int) -> list[dict[str, Any]]:
    params = {"status": status, "limit": limit}
    body = await http_get_json(
        f"{FOOTBALL_DATA_BASE}/teams/{team_id}/matches",
        settings,
        headers=_api_headers(settings),
        params=params,
    )
    matches = body.get("matches", [])
    # SCHEDULED vem em ordem crescente; FINISHED interessa do mais recente para trás.
    return matches[-limit:][::-1] if status == "FINISHED" else matches[:limit]


# --------------------------------------------------------------------------- notícias GE


def _is_relevant(title: str, keywords: list[str], noise: list[str]) -> bool:
    lowered = title.lower()
    if any(pattern in lowered for pattern in noise):
        return False
    return any(keyword in lowered for keyword in keywords)


async def _team_news(settings, slug: str, ge_cfg: dict[str, Any]) -> list[str]:
    base_url = ge_cfg.get("base_url", "https://ge.globo.com").rstrip("/")
    selectors = ge_cfg.get("selectors") or {}
    keywords = [str(k).lower() for k in (ge_cfg.get("relevance_keywords") or [])]
    noise = [str(n).lower() for n in (ge_cfg.get("noise_patterns") or [])]
    limit = int(selectors.get("max_headlines", 4))

    html = await http_get_text(f"{base_url}/{slug.strip('/')}/", settings, headers=BROWSER_HEADERS)
    soup = BeautifulSoup(html, "lxml")

    headlines: list[str] = []
    seen: set[str] = set()
    for element in soup.select(selectors.get("headlines", ".feed-post-link")):
        title = " ".join(element.get_text(" ", strip=True).split())
        if not title or len(title) < 15 or len(title) > 300 or title in seen:
            continue
        # Sem filtro de relevância a seção virava um apanhado de manchetes evergreen que
        # se repetiam por dias — foi o que motivou a troca.
        if keywords and not _is_relevant(title, keywords, noise):
            continue
        seen.add(title)
        headlines.append(title)
        if len(headlines) >= limit:
            break
    return headlines


# --------------------------------------------------------------------------- entrada


async def fetch(settings) -> ScraperResult:
    section = "football"
    football_cfg = settings.get("football") or {}
    teams_cfg = football_cfg.get("teams") or []
    ge_cfg = (football_cfg.get("sources") or {}).get("ge") or {}

    if not teams_cfg:
        return ScraperResult(section=section, status="error", error="No football teams configured")

    names = [team.get("name") for team in teams_cfg if team.get("name")]
    ids: dict[str, int] = {}

    if settings.football_data_token:
        cache_path = settings.project_root / "logs" / "football_teams.json"
        competitions = football_cfg.get("competitions") or ["BSA"]
        needing_lookup = [t["name"] for t in teams_cfg if t.get("name") and not t.get("api_id")]
        if needing_lookup:
            ids = await _resolve_team_ids(settings, needing_lookup, competitions, cache_path)
    else:
        logger.info("FOOTBALL_DATA_TOKEN ausente; a seção terá apenas notícias")

    team_data: list[dict[str, Any]] = []
    errors: list[str] = []

    for team in teams_cfg:
        name = team.get("name")
        if not name:
            continue

        entry: dict[str, Any] = {"team": name, "next_matches": [], "last_matches": [], "news": []}
        team_id = team.get("api_id") or ids.get(name)

        if settings.football_data_token and team_id:
            for status, key, limit in (("SCHEDULED", "next_matches", 2), ("FINISHED", "last_matches", 1)):
                try:
                    matches = await _team_matches(settings, int(team_id), status, limit)
                    entry[key] = [
                        formatted
                        for match in matches
                        if (formatted := _format_match(match, status == "FINISHED", int(team_id), name))
                    ]
                except Exception as exc:
                    if _is_out_of_plan(exc):
                        logger.info(
                            "%s (%s): fora do plano da football-data.org; seguindo só com notícias",
                            name,
                            status.lower(),
                        )
                        continue
                    logger.warning("Jogos de %s (%s) falharam: %s", name, status, exc)
                    errors.append(f"{name} ({status.lower()}): {exc}")

        if team.get("ge_slug"):
            try:
                entry["news"] = await _team_news(settings, team["ge_slug"], ge_cfg)
            except Exception as exc:
                logger.warning("Notícias de %s falharam: %s", name, exc)
                errors.append(f"{name} (notícias): {exc}")

        if entry["next_matches"] or entry["last_matches"] or entry["news"]:
            team_data.append(entry)
        else:
            logger.info("Sem jogo próximo nem notícia relevante para %s", name)

    logger.info("football: %s times com conteúdo", len(team_data))

    if not team_data and errors:
        return ScraperResult(section=section, status="error", error="; ".join(errors))

    # Lista vazia sem erro é legítima: significa "nada relevante hoje", e o prompt omite a seção.
    return ScraperResult(
        section=section,
        status="partial" if errors else "ok",
        data={"teams": team_data},
        error="; ".join(errors) if errors else None,
    )
