"""Testes das funções puras — sem rede, determinísticas.

Rodar com:  python -m pytest -q
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from core.digest import _split_headline, _truncate, format_digest
from core.telegram_sender import _open_tags, _split_message, sanitize_html
from core.utils import format_date_pt_br, offer_key, strip_html
from scrapers.promotions import _clean_text, _coupon, _interleave, _parse_channel


# --------------------------------------------------------------------------- datas


def test_format_date_pt_br_segunda():
    # 27/07/2026 é uma segunda-feira.
    assert format_date_pt_br(datetime(2026, 7, 27)) == "Segunda-feira, 27 de Julho de 2026"


def test_format_date_pt_br_domingo():
    assert format_date_pt_br(datetime(2026, 7, 26)) == "Domingo, 26 de Julho de 2026"


# --------------------------------------------------------------------------- HTML


def test_strip_html_colapsa_por_padrao():
    assert strip_html("<p>um</p>\n<p>dois</p>") == "um dois"


def test_strip_html_preserva_linhas_quando_pedido():
    assert strip_html("<b>a</b>\n\n<i>b</i>", keep_newlines=True) == "a\n\nb"


def test_sanitize_converte_br_em_quebra():
    assert sanitize_html("um<br>dois") == "um\ndois"


def test_sanitize_escapa_e_comercial_em_url():
    saida = sanitize_html('<a href="https://x.com/?a=1&b=2">link</a>')
    assert "&amp;b=2" in saida


def test_sanitize_nao_duplica_entidade_existente():
    assert sanitize_html("Casas &amp; Bahia") == "Casas &amp; Bahia"


def test_sanitize_escapa_menor_que_solto():
    assert sanitize_html("preço < 100") == "preço &lt; 100"


def test_sanitize_preserva_tags_validas():
    assert sanitize_html("<b>oferta</b> <i>@canal</i>") == "<b>oferta</b> <i>@canal</i>"


def test_sanitize_bloco_vira_quebra_e_nao_cola_palavras():
    assert sanitize_html("<p>um</p><p>dois</p>") == "um\n\ndois"


def test_sanitize_remove_tag_desconhecida_inline():
    assert sanitize_html("<span>oferta</span>") == "oferta"


# --------------------------------------------------------------------------- split


def test_split_curto_nao_divide():
    assert _split_message("oferta") == ["oferta"]


def test_split_respeita_limite():
    texto = "\n".join(f"linha {i}" * 20 for i in range(200))
    for pedaco in _split_message(texto, 500):
        assert len(pedaco) <= 500


def test_split_nao_deixa_tag_aberta():
    texto = "\n".join(f"<b>linha {i}</b>" for i in range(200))
    for pedaco in _split_message(texto, 400):
        assert not _open_tags(pedaco)


def test_split_reabre_tag_no_pedaco_seguinte():
    texto = "<b>" + "\n".join(f"linha {i}" for i in range(200)) + "</b>"
    pedacos = _split_message(texto, 400)
    assert len(pedacos) > 1
    assert pedacos[1].startswith("<b>")


# --------------------------------------------------------------------------- chave da oferta


def test_offer_key_ignora_pontuacao_e_caixa():
    assert offer_key("Echo Dot 5ª geração — R$ 229!") == offer_key("echo dot 5ª geração r$ 229")


def test_offer_key_distingue_produtos_diferentes():
    assert offer_key("Echo Dot 5 por R$ 229") != offer_key("Kindle 11 por R$ 429")


# --------------------------------------------------------------------------- cupom


@pytest.mark.parametrize(
    "texto,esperado",
    [
        ("Fone JBL por R$ 199 com cupom BLACK20", "BLACK20"),
        ("Use o código de desconto: ECHO15 no carrinho", "ECHO15"),
        ("cupom ap10off no app", "AP10OFF"),
        ("Cadeira por R$ 899, cupom de desconto na página", None),
        ("Sem código nenhum aqui, só o preço", None),
    ],
)
def test_coupon(texto, esperado):
    assert _coupon(texto) == esperado


def test_coupon_ignora_stopword_maiuscula():
    # "cupom DE desconto" cai no grupo e não é código nenhum.
    assert _coupon("Aproveite o cupom DE desconto especial") is None


# --------------------------------------------------------------------------- limpeza do post


def test_clean_text_remove_url_crua():
    # A mesma oferta com outro parâmetro de afiliado viraria outra chave de deduplicação.
    limpo = _clean_text("Echo Dot por R$ 229 https://amzn.to/abc?tag=canal1")
    assert limpo == "Echo Dot por R$ 229"


def test_clean_text_remove_hashtags():
    assert _clean_text("Notebook Dell por R$ 2.999 #promo #achado") == "Notebook Dell por R$ 2.999"


def test_clean_text_colapsa_emoji_repetido():
    assert _clean_text("🔥🔥🔥🔥 Oferta!!!! 🔥") == "🔥 Oferta! 🔥"


def test_clean_text_preserva_o_preco():
    texto = "Smart TV 50'' 4K por R$ 1.899,00 à vista"
    assert "R$ 1.899,00" in _clean_text(texto)


# --------------------------------------------------------------------------- prévia do Telegram

AGORA = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)
CORTE = AGORA - timedelta(hours=24)


def _mensagem(post: str, texto: str, quando: datetime) -> str:
    return (
        f'<div class="tgme_widget_message" data-post="{post}">'
        f'<div class="tgme_widget_message_text">{texto}</div>'
        f'<time datetime="{quando.isoformat().replace("+00:00", "Z")}">hoje</time>'
        f"</div>"
    )


def _pagina(*mensagens: str) -> str:
    return f"<html><body>{''.join(mensagens)}</body></html>"


def test_parse_channel_extrai_texto_link_e_data():
    html = _pagina(_mensagem("promobit/1", "Echo Dot 5ª geração por R$ 229 na Amazon", AGORA))
    [oferta] = _parse_channel(html, "promobit", CORTE, [])

    assert oferta["channel"] == "promobit"
    assert oferta["text"] == "Echo Dot 5ª geração por R$ 229 na Amazon"
    assert oferta["link"] == "https://t.me/promobit/1"
    assert oferta["link_type"] == "canal"
    assert oferta["published"] == AGORA.isoformat()


def test_parse_channel_publica_o_post_e_nao_a_loja():
    # Publicar a loja faz a pessoa pagar o preço cheio: o cupom está no post.
    texto = 'Fone JBL por R$ 199, cupom JBL10 <a href="https://amazon.com.br/dp/1">aqui</a>'
    html = _pagina(_mensagem("promobit/2", texto, AGORA))
    [oferta] = _parse_channel(html, "promobit", CORTE, [])

    assert oferta["link"] == "https://t.me/promobit/2"
    assert oferta["store_url"] == "https://amazon.com.br/dp/1"
    assert oferta["coupon"] == "JBL10"


def test_parse_channel_cai_para_a_loja_sem_permalink():
    texto = 'Monitor LG 27 por R$ 899 <a href="https://kabum.com.br/p/1">ver</a>'
    html = _pagina(
        '<div class="tgme_widget_message">'
        f'<div class="tgme_widget_message_text">{texto}</div>'
        "</div>"
    )
    [oferta] = _parse_channel(html, "promobit", CORTE, [])

    assert oferta["link"] == "https://kabum.com.br/p/1"
    assert oferta["link_type"] == "loja"


def test_parse_channel_ignora_link_interno_do_telegram():
    texto = 'Air Fryer por R$ 299 <a href="https://t.me/outrocanal">nosso outro canal</a>'
    html = _pagina(_mensagem("promobit/3", texto, AGORA))
    [oferta] = _parse_channel(html, "promobit", CORTE, [])

    assert oferta["store_url"] is None


def test_parse_channel_descarta_post_velho():
    html = _pagina(
        _mensagem("promobit/4", "Oferta de ontem retrasado por R$ 100", AGORA - timedelta(days=3))
    )
    assert _parse_channel(html, "promobit", CORTE, []) == []


def test_parse_channel_descarta_ruido_configurado():
    html = _pagina(
        _mensagem("promobit/5", "Participe do grupo de whatsapp e receba as ofertas", AGORA),
        _mensagem("promobit/6", "Notebook Acer i5 por R$ 2.499 na Kabum", AGORA),
    )
    ofertas = _parse_channel(html, "promobit", CORTE, ["participe do grupo"])

    assert [o["text"] for o in ofertas] == ["Notebook Acer i5 por R$ 2.499 na Kabum"]


def test_parse_channel_descarta_post_curto_demais():
    # Canal que posta por imagem deixa três palavras de legenda.
    html = _pagina(_mensagem("promobit/7", "Corram!", AGORA))
    assert _parse_channel(html, "promobit", CORTE, []) == []


def test_parse_channel_inverte_para_o_mais_recente_primeiro():
    html = _pagina(
        _mensagem("promobit/8", "Primeira oferta do dia por R$ 10", AGORA - timedelta(hours=5)),
        _mensagem("promobit/9", "Última oferta do dia por R$ 20", AGORA),
    )
    ofertas = _parse_channel(html, "promobit", CORTE, [])

    assert ofertas[0]["text"].startswith("Última")


def test_parse_channel_ignora_mensagem_sem_texto():
    html = _pagina('<div class="tgme_widget_message" data-post="x/1"></div>')
    assert _parse_channel(html, "promobit", CORTE, []) == []


# --------------------------------------------------------------------------- round-robin


def _oferta(canal: str, texto: str, cupom: str | None = None) -> dict:
    return {"channel": canal, "text": texto, "coupon": cupom, "link": f"https://t.me/{canal}/1"}


def test_interleave_distribui_entre_canais():
    # Concatenar e truncar faria o primeiro canal ocupar todos os slots.
    a = [_oferta("a", f"Oferta A{i} por R$ {i}") for i in range(5)]
    b = [_oferta("b", f"Oferta B{i} por R$ {i}") for i in range(5)]
    canais = [item["channel"] for item in _interleave([a, b], 4)]

    assert canais == ["a", "b", "a", "b"]


def test_interleave_deduplica_o_mesmo_achado_em_canais_diferentes():
    a = [_oferta("a", "Echo Dot 5 por R$ 229")]
    b = [_oferta("b", "Echo Dot 5 por R$ 229!"), _oferta("b", "Kindle 11 por R$ 429")]
    textos = [item["text"] for item in _interleave([a, b], 10)]

    assert textos == ["Echo Dot 5 por R$ 229", "Kindle 11 por R$ 429"]


def test_interleave_respeita_canal_menor():
    a = [_oferta("a", f"Oferta A{i} por R$ {i}") for i in range(3)]
    b = [_oferta("b", "Oferta B0 por R$ 0")]

    assert len(_interleave([a, b], 10)) == 4


# --------------------------------------------------------------------------- linha da oferta


def test_truncate_corta_na_palavra():
    assert _truncate("um dois três quatro cinco", 12) == "um dois…"


def test_truncate_nao_mexe_no_que_cabe():
    assert _truncate("Echo Dot por R$ 229", 100) == "Echo Dot por R$ 229"


@pytest.mark.parametrize(
    "texto,cabeca",
    [
        ("Echo Dot 5ª geração por R$ 229 na Amazon", "Echo Dot 5ª geração"),
        ("Cadeira Gamer ThunderX3 - modelo novo por R$ 899", "Cadeira Gamer ThunderX3 - modelo novo"),
        ("Notebook Dell Inspiron: 16GB RAM e SSD de 512GB", "Notebook Dell Inspiron"),
    ],
)
def test_split_headline_separa_o_produto(texto, cabeca):
    assert _split_headline(texto)[0] == cabeca


def test_split_headline_desiste_quando_nao_ha_quebra_confiavel():
    texto = "Promoção relâmpago em toda a loja hoje"
    assert _split_headline(texto) == (texto, "")


# --------------------------------------------------------------------------- mensagem


AS_OITO = datetime(2026, 9, 12, 8, 0)


def test_format_digest_tem_cabecalho_e_regua():
    mensagem = format_digest([_oferta("promobit", "Echo Dot 5 por R$ 229")], AS_OITO)

    assert mensagem.startswith("<b>Sábado, 12 de Setembro de 2026 — 08:00</b>")
    assert "━━━━━━━━━━━━━━━\n<b>🛒 ACHADOS & PROMOÇÕES</b>" in mensagem


def test_format_digest_usa_bullet_e_nao_asterisco():
    mensagem = format_digest([_oferta("promobit", "Echo Dot 5 por R$ 229")], AS_OITO)

    assert "• <b>Echo Dot 5</b> — por R$ 229" in mensagem
    assert "*" not in mensagem


def test_format_digest_destaca_o_cupom():
    oferta = _oferta("promobit", "Fone JBL por R$ 199 com cupom JBL10", cupom="JBL10")
    assert "cupom <b>JBL10</b>" in format_digest([oferta], AS_OITO)


def test_format_digest_liga_para_o_post_do_canal():
    oferta = {**_oferta("promobit", "Echo Dot 5 por R$ 229"), "link_type": "canal"}
    mensagem = format_digest([oferta], AS_OITO)

    assert '<a href="https://t.me/promobit/1">[Ver no canal]</a>' in mensagem
    assert "<i>@promobit</i>" in mensagem


def test_format_digest_muda_o_rotulo_quando_o_link_e_da_loja():
    oferta = {**_oferta("promobit", "Echo Dot 5 por R$ 229"), "link_type": "loja"}
    assert "[Ver oferta]" in format_digest([oferta], AS_OITO)


def test_format_digest_omite_a_linha_de_link_quando_nao_ha_link():
    oferta = {"channel": "promobit", "text": "Echo Dot 5 por R$ 229", "link": None}
    assert "<a href" not in format_digest([oferta], AS_OITO)
