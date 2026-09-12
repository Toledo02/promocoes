"""Weather data collector via Open-Meteo API."""

from __future__ import annotations

import logging
from typing import Any

from core.utils import ScraperResult, format_number_pt_br, http_get_json, now_local

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Códigos WMO devolvidos pelo Open-Meteo. Traduzir aqui em vez de deixar o número no payload:
# o modelo não tem como saber o que significa "weather_code: 61".
WMO_CODES = {
    0: "Céu limpo",
    1: "Predominantemente limpo",
    2: "Parcialmente nublado",
    3: "Nublado",
    45: "Nevoeiro",
    48: "Nevoeiro com geada",
    51: "Garoa fraca",
    53: "Garoa moderada",
    55: "Garoa intensa",
    56: "Garoa congelante",
    57: "Garoa congelante intensa",
    61: "Chuva fraca",
    63: "Chuva moderada",
    65: "Chuva forte",
    66: "Chuva congelante",
    67: "Chuva congelante forte",
    71: "Neve fraca",
    73: "Neve moderada",
    75: "Neve forte",
    77: "Grãos de neve",
    80: "Pancadas de chuva isoladas",
    81: "Pancadas de chuva",
    82: "Pancadas de chuva fortes",
    85: "Pancadas de neve",
    86: "Pancadas de neve fortes",
    95: "Tempestade",
    96: "Tempestade com granizo",
    99: "Tempestade com granizo forte",
}


def _wmo_emoji(code: int | None) -> str:
    """Emoji da condição, montado em Python: deixado a cargo do modelo, ele varia o ícone de um
    dia para o outro para o mesmo código (hoje 🌦️, amanhã 🌧️ sem a chuva ter mudado)."""
    if code is None:
        return ""
    if code <= 1:
        return "☀️"
    if code == 2:
        return "⛅"
    if code == 3:
        return "☁️"
    if code in (45, 48):
        return "🌫️"
    if 51 <= code <= 57:
        return "🌦️"
    if 61 <= code <= 67 or 80 <= code <= 82:
        return "🌧️"
    if 71 <= code <= 77 or 85 <= code <= 86:
        return "🌨️"
    if 95 <= code <= 99:
        return "⛈️"
    return ""


def _celsius(value: float | None) -> str | None:
    """'18,4°C'. Pelo mesmo motivo das cotações: entregue como float, o modelo escreve
    '18.4°C' com ponto decimal no meio de um texto em português."""
    return None if value is None else f"{format_number_pt_br(float(value), 1)}°C"


def _uv_label(index: float | None) -> str | None:
    if index is None:
        return None
    for limit, label in ((2.9, "baixo"), (5.9, "moderado"), (7.9, "alto"), (10.9, "muito alto")):
        if index <= limit:
            return label
    return "extremo"


def _rain_summary(
    probability: int | None,
    volume: float | None,
    window: str | None,
    all_day: bool = False,
) -> str | None:
    """Uma linha só com tudo que importa sobre chuva, já montada em Python.

    A seção é lida por tópico, então cada tópico precisa chegar pronto: pedir ao modelo que
    junte probabilidade, volume e janela é pedir que ele reformate números.
    """
    if probability is None and volume is None and window is None:
        return None

    parts: list[str] = []
    if probability is not None:
        parts.append(f"{probability}% de chance")
    if volume:
        parts.append(f"{format_number_pt_br(float(volume), 1)} mm previstos")
    if all_day:
        parts.append("chuva praticamente o dia todo")
    if window:
        # "mais forte" quando chove o dia inteiro: aí a janela é o pico, não o começo.
        parts.append(f"{'mais forte' if all_day else 'mais provável'} entre {window}")

    if not parts:
        return None
    if probability == 0 and not window:
        return "sem chuva prevista"
    return ", ".join(parts)


def _rain_blocks(
    hours: list[int | None], probabilities: list[int | None], threshold: int, start_hour: int = 0
) -> list[tuple[int, int, int]]:
    """Intervalos contínuos acima do limiar, como (início, fim, pico), a partir de `start_hour`.

    Horas já passadas são descartadas: o jornal chega às 5h55 e "chuva provável entre 0h e 1h"
    descreve a madrugada que a pessoa dormiu.
    """
    blocks: list[tuple[int, int, int]] = []
    start: int | None = None
    end = peak = 0

    for hour, probability in zip(hours, probabilities):
        if hour is None or hour < start_hour:
            continue
        if probability is not None and probability >= threshold:
            if start is None:
                start, peak = hour, probability
            end, peak = hour, max(peak, probability)
        elif start is not None:
            blocks.append((start, end, peak))
            start = None

    if start is not None:
        blocks.append((start, end, peak))
    return blocks


def _rain_window(
    hours: list[int | None],
    probabilities: list[int | None],
    threshold: int,
    start_hour: int = 0,
    max_window_hours: int = 6,
    narrow_margin: int = 20,
) -> str | None:
    """A janela de chuva que vale avisar — a mais intensa do que resta do dia, não a primeira.

    Dois erros que esta função já cometeu, os dois observados com dados reais de Curitiba
    (08/08/2026: 47% à 0h, 42% à 1h, depois 83-100% das 13h às 17h):

    * devolvia o **primeiro** bloco acima do limiar e parava no primeiro buraco, então travava no
      resmungo de 47% da madrugada e não mencionava a chuva de verdade da tarde;
    * não olhava a hora, então anunciava uma janela que já tinha passado quando o jornal chegou.

    Quando o bloco vencedor é longo demais para servir de aviso ("chuva entre 5h e 23h" não ajuda
    ninguém), ele é estreitado para o miolo — as horas dentro de `narrow_margin` pontos do pico.
    """
    blocks = _rain_blocks(hours, probabilities, threshold, start_hour)
    if not blocks:
        return None

    # Maior pico primeiro; empatado, o bloco mais longo; ainda empatado, o mais cedo.
    start, end, peak = max(blocks, key=lambda b: (b[2], b[1] - b[0], -b[0]))

    if end - start + 1 > max_window_hours:
        core = _rain_blocks(hours, probabilities, max(threshold, peak - narrow_margin), start_hour)
        if core:
            start, end, _ = max(core, key=lambda b: (b[2], b[1] - b[0], -b[0]))

    return f"{start}h" if start == end else f"{start}h-{end}h"


def _rains_most_of_day(
    hours: list[int | None], probabilities: list[int | None], threshold: int, start_hour: int = 0
) -> bool:
    """Verdadeiro quando a maior parte do que resta do dia está acima do limiar.

    Com a janela estreitada para o pico, sem isto se perderia a diferença entre "chove das 13h às
    17h" e "chove o dia todo, mais forte das 13h às 17h".
    """
    remaining = [
        probability
        for hour, probability in zip(hours, probabilities)
        if hour is not None and hour >= start_hour and probability is not None
    ]
    if len(remaining) < 6:
        return False
    return sum(1 for probability in remaining if probability >= threshold) / len(remaining) >= 0.7


async def fetch(settings) -> ScraperResult:
    section = "weather"
    weather_cfg = settings.get("weather") or {}
    city = weather_cfg.get("city", "Unknown")
    lat = weather_cfg.get("lat")
    lon = weather_cfg.get("lon")
    rain_threshold = int(weather_cfg.get("rain_threshold_percent", 40))

    if lat is None or lon is None:
        return ScraperResult(
            section=section,
            status="error",
            error="weather.lat and weather.lon are required in config.yaml",
        )

    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m",
        "daily": (
            "temperature_2m_max,temperature_2m_min,precipitation_probability_max,"
            "precipitation_sum,sunrise,sunset,uv_index_max,weather_code"
        ),
        "hourly": "precipitation_probability",
        "timezone": "America/Sao_Paulo",
        "forecast_days": 1,
    }

    try:
        payload = await http_get_json(OPEN_METEO_URL, settings, params=params)
        daily = payload.get("daily") or {}
        current = payload.get("current") or {}
        hourly = payload.get("hourly") or {}

        if not daily.get("time"):
            return ScraperResult(section=section, status="error", error="Open-Meteo returned empty forecast")

        hours = [int(stamp[11:13]) for stamp in hourly.get("time", [])]
        probabilities = hourly.get("precipitation_probability", [])
        uv_max = (daily.get("uv_index_max") or [None])[0]

        wind = current.get("wind_speed_10m")
        temp_min = daily["temperature_2m_min"][0]
        temp_max = daily["temperature_2m_max"][0]

        daily_code = (daily.get("weather_code") or [None])[0]
        current_code = current.get("weather_code")
        condition = WMO_CODES.get(daily_code)
        condition_now = WMO_CODES.get(current_code)

        rain_probability = (daily.get("precipitation_probability_max") or [None])[0]
        rain_mm = (daily.get("precipitation_sum") or [None])[0]
        # A partir da hora atual: o jornal é lido de manhã e não adianta avisar sobre a chuva
        # que caiu de madrugada.
        current_hour = now_local().hour
        rain_window = _rain_window(hours, probabilities, rain_threshold, current_hour)
        rain_all_day = _rains_most_of_day(hours, probabilities, rain_threshold, current_hour)
        sunrise = (daily.get("sunrise") or [""])[0][-5:] or None
        sunset = (daily.get("sunset") or [""])[0][-5:] or None
        uv_label = _uv_label(uv_max)

        data: dict[str, Any] = {
            "city": city,
            "date": daily["time"][0],
            "condition": condition,
            "condition_now": condition_now,
            # Linha pronta para o bullet "Hoje", com o emoji da condição do dia.
            "today_summary": (
                f"{_wmo_emoji(daily_code)} {condition}".strip() if condition else None
            ),
            # Emoji para o bullet "Agora" começar por ele.
            "condition_now_emoji": _wmo_emoji(current_code),
            "temp_now": _celsius(current.get("temperature_2m")),
            "feels_like": _celsius(current.get("apparent_temperature")),
            "wind": None if wind is None else f"{format_number_pt_br(float(wind), 0)} km/h",
            "temp_range": f"{_celsius(temp_min)} a {_celsius(temp_max)}",
            "temp_min": _celsius(temp_min),
            "temp_max": _celsius(temp_max),
            # Numéricos crus para o histórico comparativo; o texto acima é para o LLM copiar.
            "temp_min_c": temp_min,
            "temp_max_c": temp_max,
            "rain_probability_percent": rain_probability,
            "rain_mm": rain_mm,
            "rain_window": rain_window,
            "rain_all_day": rain_all_day,
            # Cada tópico da seção já montado: o modelo copia a linha, não recalcula nada.
            "rain_summary": _rain_summary(rain_probability, rain_mm, rain_window, rain_all_day),
            "sunrise": sunrise,
            "sunset": sunset,
            "sun_summary": (
                f"nascer {sunrise} · pôr {sunset}" if sunrise and sunset else sunrise or sunset
            ),
            "uv_index_max": uv_max,
            "uv_label": uv_label,
            "uv_summary": (
                None
                if uv_max is None
                else f"{uv_label} ({format_number_pt_br(float(uv_max), 1)})"
            ),
        }
        return ScraperResult(section=section, status="ok", data=data)

    except Exception as exc:
        logger.warning("Weather fetch failed: %s", exc)
        return ScraperResult(section=section, status="error", error=str(exc))
