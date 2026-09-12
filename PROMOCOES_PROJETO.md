# Projeto "Achados & Promoções" — especificação para implementar

Doc de handoff. A seção `🛒 ACHADOS & PROMOÇÕES` saiu do **Jornal Matinal** na Rodada 5 (ver
[PLANO.md](PLANO.md), R5.6) para virar um projeto próprio: bot dedicado, histórico próprio,
varrendo **muitos** canais de promoção em vez dos 3 que cabiam num jornal generalista.

Este arquivo é auto-suficiente: clone o Jornal Matinal como ponto de partida, cole este doc na
raiz do fork e mande implementar. O código original da seção está no histórico do git do Jornal
(commit anterior à Rodada 5, arquivo `scrapers/promotions.py`) e serve de referência —
não de ponto de chegada, porque o recorte aqui é outro.

---

## 1. Objetivo e recorte

**É:** um agente que lê canais públicos de promoção do Telegram, filtra o ruído, deduplica,
limita repetição por campanha e por produto, e entrega um resumo enxuto — 1 a 2 vezes ao dia —
num canal/chat do Telegram, com o link apontando para **o post no canal de origem** (onde está o
cupom), não para a loja.

**Não é:**

- **Comparador de preço.** Monitorar o preço de um produto específico entre execuções (o que o
  Jornal fazia via Buscapé, opcional) é escopo à parte e de baixo valor — Mercado Livre e Magalu
  bloqueiam, o Zoom devolve o mesmo catálogo do Buscapé. Se entrar, entra depois e isolado.
- **Bot de afiliados.** Sem reescrever links, sem tag de afiliado — o link é o do post original.
- **Serviço 24/7.** Assim como o Jornal, roda como job único via cron. Sem webhook, sem processo
  vivo, sem responder mensagem.

**Público:** uso pessoal (uma pessoa). Sem multi-tenant, sem painel.

---

## 2. Arquitetura — fork do Jornal Matinal

Reaproveite o esqueleto do Jornal **inteiro** e jogue fora o que não serve. O que fica:

| Componente do Jornal | Papel aqui | Mudança |
|---|---|---|
| `main.py` (pipeline linear) | orquestrador | 1 scraper só (ou 1 por "fonte"), sem LLM obrigatório |
| `core/utils.py` — `ScraperResult`, `http_get_text`, `BROWSER_HEADERS`, `USER_AGENT`, `setup_logging`, `now_local` | infra HTTP + contrato | copiar como está |
| `core/telegram_sender.py` — `sanitize_html`, `_split_message`, `send_message`, `send_alert` | envio + saneamento do subset HTML do Telegram | copiar como está |
| `core/history.py` — `load`/`save`/`prune`, `filter_published_items`, `_entities`/`_terms`/`_fold` | histórico 30 dias + anti-repetição por nome próprio | copiar; adaptar `COVERAGE_SECTIONS` |
| `config/settings.py` | loader de `config.yaml` + `.env` | copiar; trocar as chaves de secret |
| `config/config.yaml` | config-driven | manter só `promotions`, `history`, `orchestrator`, `logging` |
| `scrapers/promotions.py` (do git antigo) | **referência** da parte de canais | reimplementar a partir dela |
| `tests/` (estrutura, `test_parsers.py` / `test_quality.py`) | testes sem rede | manter o padrão |

O que **não** vem: todos os outros scrapers, `core/ai_engine.py` inteiro (ver §7 sobre o resumo),
`football_teams.json`, o enrich de métricas.

### Fluxo

```
load_settings → fetch_offers (paralelo por canal) → dedup + filtros (Python)
             → filter_published_items (anti-repetição contra o histórico)
             → format_message (template Python OU LLM opcional)
             → sanitize_html → send  → grava histórico só se enviou
```

O LLM é **opcional** (ver §7). Sem ele, um template Python monta a mensagem. Isso mantém o
projeto rodável sem chave de API.

---

## 3. Contrato do scraper

Igual ao do Jornal:

```python
async def fetch(settings) -> ScraperResult
```

- `ScraperResult.status` ∈ `ok` | `partial` | `error`.
- **Nunca propaga exceção** — captura e devolve `status="error"`.
- `partial` quando um canal falhou mas outros entregaram.
- **Alerta é sobre resultado, não sobre fonte** (regra herdada do Jornal): um canal fora do ar
  que os outros cobrem vai só para o log. Só alerta quando o resultado final ficou pior — p.ex.
  todos os canais fora, ou zero ofertas depois dos filtros num dia em que normalmente há.

`fetch` devolve, no `data`:

```python
{
  "offers": [
    {
      "channel": "promobit",
      "text": "Echo Dot 5ª geração por R$ 229 (menor preço já visto)",   # <= 400 chars
      "link": "https://t.me/promobit/12345",   # CANÔNICO: o post no canal
      "link_type": "canal",                    # "canal" | "loja"
      "store_url": "https://www.amazon.com.br/...",  # referência, NÃO publicar
      "coupon": "ECHO20",                       # ou None
      "published": "2026-08-28T09:12:00+00:00",
    },
    ...
  ]
}
```

---

## 4. Leitura dos canais — `https://t.me/s/<canal>`

A prévia web pública que o Telegram publica para **canais públicos**: HTML simples, sem bot, sem
token, sem entrar no canal. É o mecanismo que o Jornal já usava e que **funciona em produção**.

Pontos que o código do Jornal já resolveu (reaproveitar):

- Seletor das mensagens: `div.tgme_widget_message`; texto em `.tgme_widget_message_text`;
  timestamp em `time[datetime]` (ISO, com `Z` → `+00:00`); permalink de `data-post`
  (`https://t.me/<post>`).
- **Ordem:** a prévia vem cronológica; inverter para "mais recente primeiro".
- **`link` canônico = permalink do post**, com fallback para o primeiro link externo do texto
  (`_external_link`: primeiro `a[href]` cujo host não seja `t.me`/`telegram`). Publicar
  `store_url` faz a pessoa pagar o preço cheio quando a oferta depende de cupom.
- **`_coupon(text)`**: regex `(?:cupom|cupons|c[oó]digo)s?\s*(?:de\s+desconto\s*)?[:\-–]?\s*([\w][\w.\-]{2,23})`,
  valida que o código é caixa-alta ou tem dígito, descarta stopwords (`DE`, `DO`, `DESCONTO`…).
- **Ruído:** lista `noise_patterns` no config (`sorteio`, `participe do grupo`,
  `grupo de whatsapp`, `clique aqui para receber`…); descartar mensagem que casa qualquer um, e
  mensagens com menos de ~25 caracteres.
- **`BROWSER_HEADERS`** (não o `USER_AGENT` descritivo) — o `t.me/s/` é alvo de scraping de HTML.
- **Round-robin entre canais** antes do corte (mesmo motivo dos feeds RSS do Jornal: concatenar
  e truncar faz o primeiro canal tomar todos os slots).

---

## 5. Filtros e limites (Python, não prompt)

Na ordem:

1. **Idade:** `max_age_hours` (24h é razoável para 1–2 envios/dia).
2. **Ruído:** `noise_patterns` + comprimento mínimo.
3. **Dedup:** chave = `re.sub(r"[^\w]", "", text.lower())[:60]`.
4. **Teto por campanha:** `max_per_coupon` (2). O mesmo cupom em 5 produtos tomava a seção
   inteira — num jornal real, 3 dos 4 achados eram o mesmo `OFERTA8DO8`.
5. **Anti-repetição contra o histórico:** `filter_published_items` com
   `COVERAGE_SECTIONS = {"offers": ("offers", "text", <mínimo>)}` e `_OFFER_TEXT_LIMIT ≈ 110`
   (só o começo da oferta identifica o produto; o resto é emoji/hashtag/frete). Compara nomes
   próprios do texto da oferta contra o texto dos envios recentes: o produto anunciado ontem sai,
   outro produto da mesma loja fica. As 3 travas anti-falso-positivo do Jornal valem aqui também.
6. **Corte final:** `max_items` (com 1–2 envios/dia e mais canais, algo como 6–10).

---

## 6. Config (`config/config.yaml`)

```yaml
promotions:
  telegram_channels:          # começar com estes 3; a intenção é crescer muito (ver §9)
    - "promobit"
    - "promocoesdodia"
    - "hardmob_promo"
  max_age_hours: 24
  per_channel: 8
  max_items: 8
  max_per_coupon: 2
  noise_patterns:
    - "sorteio"
    - "participe do grupo"
    - "entre no nosso"
    - "clique aqui para receber"
    - "grupo de whatsapp"

history:
  file: "history.json"
  retention_days: 30
  journals_in_prompt: 3        # aqui: quantos envios recentes a anti-repetição consulta

orchestrator:
  scraper_timeout_seconds: 30
  min_items_for_send: 1        # abaixo disso, não envia (evita "nada hoje")

logging:
  level: "INFO"
  directory: "logs"
```

`.env`: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (o canal/chat de destino), e
`LLM_API_KEY` **só se** for usar o resumo por LLM.

---

## 7. Formato da mensagem

**Padrão: template em Python.** Uma mensagem com régua de seção (convenção do Jornal —
`━━━━━━━━━━━━━━━`, título em CAIXA ALTA dentro de `<b>`, linha em branco) e um bloco por oferta:

```
• <b>Echo Dot 5ª geração</b> — R$ 229 (menor preço já visto)
  cupom <b>ECHO20</b>
  <a href="https://t.me/promobit/12345">[Ver no canal]</a>
```

Regras herdadas do sanitizador do Telegram: só `<b>`, `<i>`, `<a href>`; bullets com `•`; escapar
`&`/`<`/`>` em conteúdo dinâmico; dividir acima de 4096 fechando/reabrindo tags.

**Opcional: LLM.** Se quiser que o texto seja reescrito/enxugado (tirar "corram", "imperdível",
emoji em excesso dos posts), reaproveite `core/ai_engine.py` com um `SYSTEM_PROMPT` mínimo:
copiar preço verbatim, nunca recalcular, destacar cupom em negrito, link só o do canal, 1 linha
por oferta. Mantenha o `_fallback_journal` equivalente (template puro) para o dia em que a API
falhar. Sem chave configurada → template direto.

---

## 8. Deploy

Cron, igual ao Jornal. Sugestão: `0 8,18 * * *` (manhã e fim de tarde — quando as campanhas
saem). Uma VPS qualquer; o `t.me/s/` não aplica cota por IP como a AwesomeAPI. Testar cada canal
novo **na VPS** (`--no-llm --only promotions`), não só localmente.

Estado local em `logs/`: `history.json` (30 dias, podado a cada gravação, gravado **só após
envio bem-sucedido** — um envio que não chegou não pode suprimir a oferta amanhã).

---

## 9. Como achar e validar um canal novo

O valor do projeto está em ter **muitos** canais. Processo:

1. **Achar:** buscar no Telegram por "promoção", "ofertas", "desconto", "achados"; nichos
   (`hardware`, `livros`, `pelando`, `gamer`, categorias do Promobit); indicações de outros
   canais nos próprios posts.
2. **Confirmar que é público e tem prévia:** abrir `https://t.me/s/<canal>` no navegador. Se
   carregar as mensagens, serve. Se redirecionar para `t.me/<canal>` sem conteúdo, o canal é
   privado ou desativou a prévia — descartar.
3. **Validar o parsing:** rodar o scraper só com esse canal e conferir que `text`, `link`,
   `published` e `coupon` saem corretos. Canais que postam muito por imagem (texto curto ou
   vazio) rendem pouco — o filtro de comprimento mínimo já os esvazia.
4. **Cadenciar:** canal muito prolífico (dezenas de posts/dia) precisa de `per_channel` baixo
   para não afogar os outros no round-robin.
5. **Adicionar** em `telegram_channels` e validar na VPS.

**Candidatos a avaliar (preencher com o que passar no processo acima):**

- `pelando` / canais do Pelando por categoria
- `promobit` por categoria (o Promobit tem canais temáticos)
- canais de hardware/PC (`pcgamerbr`, `hardmob_promo` já está)
- canais de livros, games (`gamedealsbrasil` e similares)
- canais de cartão/cashback quando a oferta depende de cupom empilhável

Manter a lista **curada**: canal que só repassa afiliado sem cupom, ou que posta muito sorteio,
entra no `noise_patterns` ou sai da lista.

---

## 10. Critério de pronto

- `python main.py --no-llm --only promotions` na VPS devolve `status=ok` com ofertas reais de
  todos os canais configurados.
- Duas execuções seguidas no mesmo dia não repetem a mesma oferta (anti-repetição).
- Um cupom que aparece em 5 posts rende no máximo `max_per_coupon` linhas.
- O link publicado é sempre o do post no canal.
- `pytest -q` verde, sem rede.
- Derrubar todos os canais de propósito gera alerta no Telegram no mesmo dia.
