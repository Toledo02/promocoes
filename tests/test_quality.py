"""Testes das regras de seleção — o que vai ao ar e o que é cortado.

Separado de `test_parsers.py`, que cobre parsing e formatação. Aqui a pergunta é outra: dado o
que os canais trouxeram e o que já foi enviado, o que sobra? Também sem rede.

Rodar com:  python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from config.settings import Settings
from core import history
from core.ai_engine import _looks_valid, generate_digest
from core.digest import format_digest, offer_keys, select_offers
from core.telegram_sender import inspect_message
from core.utils import now_local, offer_key
from main import _silent_channels


def _envio(texto: str, dias_atras: int = 0, ofertas=None, silenciosos=None, hora="08:00") -> dict:
    dia = (now_local().date() - timedelta(days=dias_atras)).isoformat()
    return {
        dia: {
            "sends": [
                {
                    "time": hora,
                    "text": texto,
                    "offers": list(ofertas or []),
                    "silent": list(silenciosos or []),
                }
            ]
        }
    }


def _oferta(texto: str, canal: str = "promobit", cupom: str | None = None) -> dict:
    return {
        "channel": canal,
        "text": texto,
        "coupon": cupom,
        "link": f"https://t.me/{canal}/1",
        "link_type": "canal",
    }


def _payload(*ofertas: dict) -> dict:
    return {"promotions": {"offers": list(ofertas), "count": len(ofertas)}}


# --------------------------------------------------------------------------- histórico


def test_prune_respeita_a_janela():
    antigo = (now_local().date() - timedelta(days=40)).isoformat()
    recente = now_local().date().isoformat()
    podado = history.prune({antigo: {}, recente: {}}, retention_days=30)

    assert list(podado) == [recente]


def test_prune_descarta_chave_invalida():
    assert history.prune({"ontem": {}}, retention_days=30) == {}


def test_record_nao_apaga_o_envio_anterior():
    # Dois disparos por dia: gravando por dia, o das 18h apagaria o das 8h e as ofertas da
    # manhã voltariam à tona amanhã.
    hoje = now_local().date()
    depois_da_manha = history.record({}, hoje, "manhã", ["k1"], time_label="08:00")
    depois_da_tarde = history.record(depois_da_manha, hoje, "tarde", ["k2"], time_label="18:00")

    sends = depois_da_tarde[hoje.isoformat()]["sends"]
    assert [s["text"] for s in sends] == ["manhã", "tarde"]


def test_recent_sends_do_mais_novo_para_o_mais_antigo():
    historico = {**_envio("ontem", dias_atras=1), **_envio("hoje")}
    assert [texto for _, texto in history.recent_sends(historico, 5, 3)] == ["hoje", "ontem"]


def test_recent_sends_ignora_envio_velho_demais():
    assert history.recent_sends(_envio("antigo", dias_atras=10), 5, 2) == []


def test_published_keys_junta_os_envios_da_janela():
    historico = {**_envio("manhã", ofertas=["k1"]), **_envio("ontem", 1, ofertas=["k2"])}
    assert history.published_keys(historico, 3) == {"k1", "k2"}


def test_has_recent_sends():
    assert history.has_recent_sends(_envio("hoje")) is True
    assert history.has_recent_sends({}) is False


# --------------------------------------------------------------------------- repetição exata


def test_filtro_exato_remove_a_oferta_ja_publicada():
    ja_foi = "Echo Dot 5ª geração por R$ 229"
    payload = _payload(_oferta(ja_foi), _oferta("Kindle 11 por R$ 429"))
    history.filter_seen_offers(payload, _envio("qualquer", ofertas=[offer_key(ja_foi)]), 3)

    assert [o["text"] for o in payload["promotions"]["offers"]] == ["Kindle 11 por R$ 429"]


def test_filtro_exato_alcanca_o_repost_com_outra_pontuacao():
    ja_foi = "Echo Dot 5ª geração por R$ 229"
    payload = _payload(_oferta("ECHO DOT 5ª GERAÇÃO POR R$ 229!!"))
    history.filter_seen_offers(payload, _envio("x", ofertas=[offer_key(ja_foi)]), 3)

    assert payload["promotions"]["offers"] == []


def test_filtro_exato_sem_historico_nao_mexe_em_nada():
    payload = _payload(_oferta("Echo Dot 5 por R$ 229"))
    history.filter_seen_offers(payload, {}, 3)

    assert len(payload["promotions"]["offers"]) == 1


def test_filtro_exato_ignora_publicacao_fora_da_janela():
    ja_foi = "Echo Dot 5ª geração por R$ 229"
    payload = _payload(_oferta(ja_foi))
    history.filter_seen_offers(payload, _envio("x", 10, ofertas=[offer_key(ja_foi)]), 3)

    assert len(payload["promotions"]["offers"]) == 1


# --------------------------------------------------------------------------- repetição reescrita


def test_filtro_por_nome_alcanca_a_mesma_oferta_com_outra_redacao():
    enviado = "• Echo Dot 5ª geração — por R$ 229 na Amazon"
    payload = _payload(_oferta("SÓ HOJE Echo Dot 5 saindo na Amazon por 229 reais", canal="pelando"))
    history.filter_published_items(payload, _envio(enviado), 4, 2)

    assert payload["promotions"]["offers"] == []


def test_filtro_por_nome_preserva_outro_produto_da_mesma_loja():
    payload = _payload(_oferta("Kindle Paperwhite 12 na Amazon por R$ 599"))
    history.filter_published_items(payload, _envio("• Echo Dot 5ª geração — R$ 229 na Amazon"), 4, 2)

    assert len(payload["promotions"]["offers"]) == 1


def test_filtro_por_nome_exige_mais_de_um_nome_coincidindo():
    # Com um nome só, "Samsung" apagaria qualquer oferta da marca pelo resto da semana.
    payload = _payload(_oferta("Samsung lançou monitor curvo de 34 polegadas por R$ 2.199"))
    history.filter_published_items(payload, _envio("• Galaxy Buds da Samsung — R$ 399"), 4, 2)

    assert len(payload["promotions"]["offers"]) == 1


def test_filtro_por_nome_ignora_o_que_aparece_em_todos_os_envios():
    # "Amazon" e "Mercado" saem em toda mensagem e não identificam oferta alguma.
    hoje = now_local().date()
    historico = history.record({}, hoje, "Amazon Mercado Livre Echo", time_label="08:00")
    historico = history.record(historico, hoje, "Amazon Mercado Livre Kindle", time_label="12:00")

    payload = _payload(_oferta("Air Fryer Mondial na Amazon e no Mercado Livre por R$ 299"))
    history.filter_published_items(payload, historico, 4, 2)

    assert len(payload["promotions"]["offers"]) == 1


def test_filtro_por_nome_ignora_envio_antigo_demais():
    payload = _payload(_oferta("Echo Dot 5 na Amazon por R$ 229"))
    history.filter_published_items(payload, _envio("Echo Dot 5ª geração na Amazon", 10), 4, 2)

    assert len(payload["promotions"]["offers"]) == 1


def test_filtro_por_nome_mantem_o_count_coerente():
    enviado = "• Echo Dot 5ª geração — R$ 229 na Amazon"
    payload = _payload(_oferta("Echo Dot 5 na Amazon por R$ 229"), _oferta("Kindle 11 por R$ 429"))
    history.filter_published_items(payload, _envio(enviado), 4, 2)

    assert payload["promotions"]["count"] == len(payload["promotions"]["offers"]) == 1


def test_filtro_por_nome_sem_historico_nao_mexe_em_nada():
    payload = _payload(_oferta("Echo Dot 5 na Amazon por R$ 229"))
    history.filter_published_items(payload, {}, 4, 2)

    assert len(payload["promotions"]["offers"]) == 1


# --------------------------------------------------------------------------- corte final


def test_select_offers_respeita_o_teto_de_itens():
    ofertas = [_oferta(f"Produto {i} por R$ {i}0") for i in range(12)]
    assert len(select_offers(ofertas, max_items=8, max_per_coupon=2)) == 8


def test_select_offers_limita_a_campanha_por_cupom():
    # Num jornal real, 3 dos 4 achados eram o mesmo OFERTA8DO8.
    ofertas = [_oferta(f"Produto {i} por R$ {i}0", cupom="OFERTA8DO8") for i in range(5)]
    escolhidas = select_offers(ofertas, max_items=8, max_per_coupon=2)

    assert len(escolhidas) == 2


def test_select_offers_deixa_passar_outra_campanha():
    ofertas = [
        _oferta("A por R$ 10", cupom="X"),
        _oferta("B por R$ 20", cupom="X"),
        _oferta("C por R$ 30", cupom="X"),
        _oferta("D por R$ 40", cupom="Y"),
    ]
    assert [o["text"] for o in select_offers(ofertas, 8, 2)] == [
        "A por R$ 10",
        "B por R$ 20",
        "D por R$ 40",
    ]


def test_select_offers_nao_limita_oferta_sem_cupom():
    ofertas = [_oferta(f"Produto {i} por R$ {i}0") for i in range(5)]
    assert len(select_offers(ofertas, max_items=8, max_per_coupon=2)) == 5


def test_offer_keys_bate_com_a_chave_do_historico():
    # Se as duas divergirem, a oferta publicada hoje não é reconhecida amanhã.
    ofertas = [_oferta("Echo Dot 5 por R$ 229")]
    assert offer_keys(ofertas) == [offer_key("Echo Dot 5 por R$ 229")]


# --------------------------------------------------------------------------- mensagem final

AS_OITO = datetime(2026, 9, 12, 8, 0)


def test_mensagem_atravessa_o_sanitizador_sem_problema():
    ofertas = [_oferta(f"Produto {i} por R$ {i}00", cupom=f"CUP{i}") for i in range(8)]
    relatorio = inspect_message(format_digest(ofertas, AS_OITO))

    assert relatorio["problems"] == []
    assert relatorio["links"] == 8


def test_mensagem_escapa_o_texto_do_canal():
    # O texto vem de terceiros: `<` e `&` crus derrubam a mensagem inteira no Telegram.
    ofertas = [_oferta("Casas & Bahia <b>hackeada</b> por R$ 10")]
    sanitizada = inspect_message(format_digest(ofertas, AS_OITO))["sanitized"]

    assert "&amp;" in sanitizada
    assert "&lt;b&gt;" in sanitizada


def test_mensagem_de_oito_ofertas_cabe_em_um_envio():
    ofertas = [_oferta(f"Produto {i} bem descrito por R$ {i}00", cupom=f"CUP{i}") for i in range(8)]
    assert len(inspect_message(format_digest(ofertas, AS_OITO))["chunk_sizes"]) == 1


# --------------------------------------------------------------------------- canais silenciosos


def test_silent_channels_junta_falha_e_silencio():
    payload = {
        "promotions": {
            "offers": [_oferta("Echo Dot 5 por R$ 229", canal="promobit")],
            "channels_read": ["promobit", "pelando"],
            "channels_failed": ["hardmob_promo"],
        }
    }
    assert _silent_channels(payload) == ["hardmob_promo", "pelando"]


def test_silent_channels_vazio_quando_todos_entregaram():
    payload = {
        "promotions": {
            "offers": [_oferta("A por R$ 1", canal="a"), _oferta("B por R$ 2", canal="b")],
            "channels_read": ["a", "b"],
            "channels_failed": [],
        }
    }
    assert _silent_channels(payload) == []


def test_chronic_silence_exige_um_minimo_de_envios():
    # Um dia ruim não é alerta.
    assert history.chronic_silence(_envio("x", silenciosos=["morto"]), ["morto"], 3, 3) == []


def test_chronic_silence_acusa_quem_faltou_em_todos_os_envios():
    hoje = now_local().date()
    historico = history.record({}, hoje, "m", silent_channels=["morto", "eventual"], time_label="08:00")
    historico = history.record(historico, hoje, "t", silent_channels=["morto"], time_label="18:00")

    assert history.chronic_silence(historico, ["morto", "outro"], 3, 3) == ["morto"]


def test_chronic_silence_nao_acusa_quem_voltou_hoje():
    hoje = now_local().date()
    historico = history.record({}, hoje, "m", silent_channels=["voltou"], time_label="08:00")
    historico = history.record(historico, hoje, "t", silent_channels=["voltou"], time_label="18:00")

    assert history.chronic_silence(historico, [], 3, 3) == []


# --------------------------------------------------------------------------- LLM opcional


def _settings(**formatting) -> Settings:
    return Settings(config={"formatting": formatting})


def test_sem_llm_configurado_o_resumo_e_o_template():
    ofertas = [_oferta("Echo Dot 5 por R$ 229")]
    assert generate_digest(ofertas, _settings(use_llm=False), AS_OITO) == format_digest(
        ofertas, AS_OITO
    )


def test_use_llm_sem_chave_cai_no_template():
    ofertas = [_oferta("Echo Dot 5 por R$ 229")]
    assert generate_digest(ofertas, _settings(use_llm=True), AS_OITO) == format_digest(
        ofertas, AS_OITO
    )


def test_looks_valid_rejeita_resposta_em_prosa():
    assert _looks_valid("Hoje tem várias ofertas boas, confira no canal.", [{}, {}]) is False


def test_looks_valid_aceita_resposta_no_formato():
    resposta = "<b>Hoje</b>\n\n━━━━━━━━━━━━━━━\n<b>🛒 ACHADOS & PROMOÇÕES</b>\n\n• um\n\n• dois"
    assert _looks_valid(resposta, [{}, {}]) is True
