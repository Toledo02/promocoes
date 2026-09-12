"""Orquestrador do agente de promoções.

Job único, disparado pelo cron 1–2 vezes ao dia. Pipeline linear:

    load_settings → fetch (canais em paralelo) → filtros do histórico → generate_digest
                  (escolhe + formata; LLM por padrão, template como fallback)
                  → sanitize_html → send → grava o histórico só se enviou

`generate_digest` também decide **quais** ofertas publicar quando o LLM está ligado — não é só
formatação. Por isso `main.py` não corta o pool antes de chamá-la: cortar em `max_items` aqui
seria fazer a escolha por ordem de chegada e nunca deixar o modelo comparar desconto/preço entre
os candidatos, que é exatamente o que se pediu para ele fazer.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from typing import Awaitable, Callable

from config.settings import load_settings
from core import digest, history as history_store
from core.ai_engine import generate_digest
from core.telegram_sender import inspect_message, send_alert, send_digest
from core.utils import ScraperResult, now_local, setup_logging, strip_html
from scrapers import promotions

ScraperFn = Callable[..., Awaitable[ScraperResult]]

# Uma fonte hoje. A lista existe porque o recorte do projeto é crescer em fontes (outros canais
# já entram pelo config; uma fonte de outro tipo entraria aqui).
SCRAPERS: list[tuple[str, ScraperFn]] = [
    ("promotions", promotions.fetch),
]

HISTORY_FILE = "history.json"


async def _run_scraper(name: str, fn: ScraperFn, settings) -> ScraperResult:
    logger = logging.getLogger("promocoes")
    try:
        result = await fn(settings)
        logger.info("Scraper %s finished with status=%s", name, result.status)
        return result
    except Exception as exc:
        logger.warning("Scraper %s raised exception: %s", name, exc)
        return ScraperResult(section=name, status="error", error=str(exc))


async def _collect_data(settings, only: list[str] | None = None) -> dict[str, ScraperResult]:
    logger = logging.getLogger("promocoes")
    timeout = int(settings.orchestrator.get("scraper_timeout_seconds", 30))

    selected = [(name, fn) for name, fn in SCRAPERS if not only or name in only]
    if only:
        unknown = set(only) - {name for name, _ in SCRAPERS}
        if unknown:
            logger.warning("Scrapers desconhecidos ignorados: %s", ", ".join(sorted(unknown)))

    tasks = {
        name: asyncio.create_task(_run_scraper(name, fn, settings), name=name)
        for name, fn in selected
    }

    results: dict[str, ScraperResult] = {}
    for name, task in tasks.items():
        try:
            results[name] = await asyncio.wait_for(task, timeout=timeout)
        except asyncio.TimeoutError:
            logger.warning("Scraper %s timed out after %ss", name, timeout)
            task.cancel()
            results[name] = ScraperResult(
                section=name, status="error", error=f"Timeout after {timeout}s"
            )
        except Exception as exc:
            logger.warning("Scraper %s future failed: %s", name, exc)
            results[name] = ScraperResult(section=name, status="error", error=str(exc))

    return results


def _build_payload(results: dict[str, ScraperResult]) -> dict:
    return {name: result.to_payload() for name, result in results.items()}


def _clean_error(text: str | None, limit: int = 200) -> str:
    """Colapsa o erro em uma linha e corta: mensagens de httpx trazem URLs e quebras de linha."""
    collapsed = " ".join((text or "erro não informado").split())
    return collapsed if len(collapsed) <= limit else f"{collapsed[:limit]}…"


def _failed_sections(results: dict[str, ScraperResult]) -> list[tuple[str, str]]:
    return [
        (name, _clean_error(result.error))
        for name, result in results.items()
        if result.status == "error"
    ]


def _notify(text: str, settings) -> None:
    """Avisa pelo Telegram sem nunca derrubar o pipeline por causa do aviso."""
    logger = logging.getLogger("promocoes")
    try:
        send_alert(text, settings)
        logger.info("Alerta enviado ao Telegram")
    except Exception as exc:
        logger.error("Falha ao enviar o alerta: %s", exc)


def _silent_channels(payload: dict) -> list[str]:
    """Canais que não entregaram nada nesta rodada — por erro ou por virem vazios.

    Erro e silêncio contam junto porque, do lado de cá, dão no mesmo: um canal que responde 200
    com uma página vazia há três semanas é tão inútil quanto um que responde 404, e é o tipo de
    falha que a cadeia de fallback esconde justamente por não quebrar nada.
    """
    data = payload.get("promotions") or {}
    read = data.get("channels_read") or []
    delivered = {offer.get("channel") for offer in (data.get("offers") or [])}
    empty = [channel for channel in read if channel not in delivered]
    return sorted({*(data.get("channels_failed") or []), *empty})


def _preview(message: str, offers: list[dict], alerts: list[str]) -> None:
    """Mostra a mensagem como ela será lida, mais o que só se vê no formato de transporte."""
    report = inspect_message(message)
    sizes = report["chunk_sizes"]

    print()
    print("=" * 72)
    # Renderizado, não o HTML cru: "&amp;" chega ao Telegram como "&" e assustaria à toa.
    print(strip_html(report["sanitized"], keep_newlines=True))
    print("=" * 72)
    print()
    print(
        f"  {len(offers)} ofertas, {sum(sizes)} caracteres, "
        f"{len(sizes)} mensagem(ns) {sizes}, {report['links']} links"
    )
    print(f"  formatação: {'; '.join(report['problems']) if report['problems'] else 'ok'}")

    if alerts:
        print()
        print("  Alerta que seria enviado:")
        for line in "\n".join(alerts).splitlines():
            print(f"    {line}")


async def _run_pipeline(args: argparse.Namespace) -> int:
    settings = load_settings()
    logger = setup_logging(settings)
    logger.info("Iniciando o agente de promoções%s", " [DRY RUN]" if args.dry_run else "")

    results = await _collect_data(settings, args.only)
    payload = _build_payload(results)

    if args.no_llm:
        print(_dump_payload(payload))
        return 0

    failures = _failed_sections(results)
    for name, error in failures:
        logger.error("Fonte %s falhou: %s", name, error)

    promo_cfg = settings.promotions
    history_cfg = settings.config.get("history") or {}
    history_file = settings.project_root / "logs" / history_cfg.get("file", HISTORY_FILE)
    retention_days = int(history_cfg.get("retention_days", history_store.DEFAULT_RETENTION_DAYS))
    sends_in_lookback = int(
        history_cfg.get("sends_in_lookback", history_store.DEFAULT_SENDS_IN_LOOKBACK)
    )
    lookback_days = int(history_cfg.get("lookback_days", history_store.DEFAULT_LOOKBACK_DAYS))
    repeat_window = int(
        history_cfg.get("repeat_window_days", history_store.DEFAULT_REPEAT_WINDOW_DAYS)
    )

    history = history_store.load(history_file)
    collected = len((payload.get("promotions") or {}).get("offers") or [])
    # Antes dos filtros: um canal cujas ofertas foram todas barradas por repetição entregou,
    # e chamá-lo de silencioso mandaria procurar defeito onde não há.
    silent = _silent_channels(payload)

    history_store.filter_seen_offers(payload, history, repeat_window)
    history_store.filter_published_items(payload, history, sends_in_lookback, lookback_days)

    pool = (payload.get("promotions") or {}).get("offers") or []
    logger.info("%s candidatos coletados, %s restantes após o histórico", collected, len(pool))

    min_items = int(settings.orchestrator.get("min_items_for_send", 1))
    if len(pool) < min_items:
        # Sem candidato para julgar, nem vale chamar o LLM: nada muda o resultado.
        logger.warning("Nada a enviar: %s candidatos (mínimo %s)", len(pool), min_items)
        alerts = _build_alerts(failures, collected, history, pool, silent, settings)
        if args.dry_run:
            _preview("", [], alerts)
        elif alerts:
            _notify("\n\n".join(alerts), settings)
        return 0

    message, offers = generate_digest(
        pool,
        settings,
        max_items=int(promo_cfg.get("max_items", 8)),
        max_per_coupon=int(promo_cfg.get("max_per_coupon", 2)),
    )
    logger.info("%s ofertas publicadas de %s candidatos (%s chars)", len(offers), len(pool), len(message))

    alerts = _build_alerts(failures, collected, history, offers, silent, settings)

    if len(offers) < min_items:
        # Pool tinha candidato, mas nenhuma ganhou do julgamento (LLM) ou sobreviveu à
        # deduplicação (template) até o mínimo — mesmo critério de "não manda nada hoje".
        logger.warning("Nada a enviar: %s ofertas publicadas (mínimo %s)", len(offers), min_items)
        if args.dry_run:
            _preview("", [], alerts)
        elif alerts:
            _notify("\n\n".join(alerts), settings)
        return 0

    if args.dry_run:
        _preview(message, offers, alerts)
        return 0

    try:
        send_digest(message, settings)
        logger.info("Resumo enviado ao Telegram")
    except Exception as exc:
        logger.error("Falha ao enviar o resumo: %s", exc)
        _notify(f"🔥 Achados & Promoções não pôde ser enviado\n\n{_clean_error(str(exc), 500)}", settings)
        return 1

    # Só grava após envio bem-sucedido: uma mensagem que não chegou não pode suprimir a oferta
    # amanhã.
    try:
        updated = history_store.record(
            history,
            now_local().date(),
            message,
            digest.offer_keys(offers),
            silent,
        )
        history_store.save(history_file, updated, retention_days)
        logger.info("Histórico atualizado (%s dias)", len(updated))
    except Exception as exc:
        logger.warning("Falha ao gravar o histórico: %s", exc)

    if alerts:
        _notify("\n\n".join(alerts), settings)

    return 0


def _build_alerts(
    failures: list[tuple[str, str]],
    collected: int,
    history: dict,
    offers: list[dict],
    silent: list[str],
    settings,
) -> list[str]:
    """Alerta é sobre resultado, não sobre fonte.

    Um canal fora do ar que os outros cobrem vai só para o log: alertar todo dia sobre algo que
    não muda o resultado treina o leitor a ignorar os alertas. Sobram três casos em que o
    resultado *é* pior — e o terceiro existe porque a falha silenciosa é a que dura semanas.
    """
    logger = logging.getLogger("promocoes")
    alerts: list[str] = []

    for name, error in failures:
        alerts.append(f"❌ {name}: {error}")

    # Só quando a coleta foi bem: leva vazia já vira `error` no scraper, e dois alertas sobre
    # o mesmo fato é o começo de se ignorar os dois.
    if not failures and not offers and history_store.has_recent_sends(history):
        alerts.append(
            "⚠️ Nenhuma oferta sobrou depois dos filtros, e nos últimos dias sempre houve. "
            f"Coletados: {collected} candidatos."
        )

    chronic = history_store.chronic_silence(
        history,
        silent,
        days=int(settings.get("history", "silence_alert_days", default=3)),
        min_sends=int(settings.get("history", "silence_alert_sends", default=3)),
    )
    if chronic:
        alerts.append(
            "🔇 Canais sem entregar nada há vários envios seguidos (conferir se ainda existem "
            "ou se a prévia foi desativada): " + ", ".join(f"@{c}" for c in chronic)
        )

    for alert in alerts:
        logger.warning("Alerta: %s", " ".join(alert.split()))
    return alerts


def _dump_payload(payload: dict) -> str:
    import json

    return json.dumps(payload, ensure_ascii=False, indent=2)


def _use_utf8_stdout() -> None:
    """O console do Windows usa cp1252 e estoura ao imprimir acentos e emoji.

    Só afeta os modos de inspeção (--dry-run / --no-llm); o envio ao Telegram sempre foi UTF-8.
    """
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass


def _parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Coleta e envia o resumo de promoções.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Monta a mensagem e imprime no terminal, sem enviar ao Telegram.",
    )
    parser.add_argument(
        "--no-llm",
        action="store_true",
        help="Só coleta e imprime o payload cru, sem filtros nem mensagem. Não gasta requisição.",
    )
    parser.add_argument(
        "--only",
        type=lambda value: [name.strip() for name in value.split(",") if name.strip()],
        help=f"Roda apenas as fontes indicadas. Opções: {', '.join(n for n, _ in SCRAPERS)}",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if args.dry_run or args.no_llm:
        _use_utf8_stdout()
    try:
        return asyncio.run(_run_pipeline(args))
    except Exception as exc:
        logging.getLogger("promocoes").exception("Pipeline quebrou")
        if not args.dry_run and not args.no_llm:
            try:
                _notify(
                    f"🔥 Achados & Promoções quebrou\n\n{_clean_error(str(exc), 500)}",
                    load_settings(),
                )
            except Exception:
                logging.getLogger("promocoes").error("Não foi possível avisar sobre a quebra")
        return 1


if __name__ == "__main__":
    sys.exit(main())
