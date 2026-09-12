# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Agente Python que coleta dados de várias fontes (clima, câmbio, RSS, GitHub, gaming, futebol),
consolida via Gemini e envia um jornal matinal em pt-BR pelo Telegram. Roda como job único
via cron às 05:55, não como serviço.

[PLANO.md](PLANO.md) mantém o backlog de melhorias com o diagnóstico que originou cada item — vale
consultar antes de mexer em algo que pareça estranho, a causa provavelmente está documentada lá.

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp config/.env.example config/.env

python main.py                        # pipeline completo (envia ao Telegram)
python main.py --dry-run              # gera e imprime, sem enviar
python main.py --no-llm               # só coleta e imprime o payload, sem gastar requisição
python main.py --only gaming,finance  # subconjunto de scrapers
python -m pytest -q                   # 127 testes, sem rede
```

Ao iterar em scrapers use `--no-llm --only <scraper>`: não consome cota do Gemini nem envia mensagem.

## Arquitetura

Pipeline linear em [main.py](main.py): `load_settings` → scrapers em paralelo → `generate_journal`
(LLM) → `sanitize_html` → `send_journal`.

**Contrato dos scrapers.** Cada um expõe `async def fetch(settings) -> ScraperResult` e é registrado
em `SCRAPERS` ([main.py](main.py)). `ScraperResult` ([core/utils.py](core/utils.py)) tem `status` =
`ok` | `partial` | `error`; `to_payload()` injeta `_error`/`_warning`, que o prompt sabe interpretar.
Scrapers **nunca propagam exceção** — capturam e devolvem `status="error"`.

**Observabilidade não é opcional aqui.** Seções `error`/`partial` geram alerta no Telegram
(`_collect_issues` → `send_alert`). Isso existe porque dois feeds ficaram 404 por três semanas e a
AwesomeAPI 47 dias, sem ninguém notar: a cadeia de fallback é boa o bastante para esconder falhas.
Ao adicionar uma fonte, garanta que a falha dela apareça no `status` — mas só quando o resultado
final for pior por causa dela (ver "Alerta é sobre resultado" abaixo).

**Camada LLM.** [core/ai_engine.py](core/ai_engine.py) usa `google-genai`, com backoff 10s→30s→90s e
fallback para os modelos de `llm_fallback_models`. Erros 4xx não são repetidos (`_is_permanent`).
Se tudo falhar, `_fallback_journal` produz um resumo em **texto puro** — sem marcação, para
atravessar o sanitizador intacto.

**Envio.** [core/telegram_sender.py](core/telegram_sender.py) faz `sanitize_html` (mantém só o
subset do Telegram, converte tags de bloco em quebra, escapa `&`/`<`/`>`), divide acima de 4096
fechando e reabrindo tags no corte, e em caso de erro reenvia com as tags removidas.

## Pontos de atenção

**Prod ≠ local — isto é a regra mais importante do repositório.** A VPS recebe respostas diferentes
das da sua máquina. O CheapShark exige User-Agent descritivo (por isso `USER_AGENT` em
[core/utils.py](core/utils.py) **não** imita navegador) e a AwesomeAPI aplica cota por IP e responde
429 lá desde 11/06/2026. Validar scraper só localmente não prova nada; rode `--no-llm --only X` na
VPS. Quando algo falhar com código estranho, **leia o corpo da resposta antes de teorizar** — o 400
do CheapShark parecia bloqueio de IP e a resposta dizia exatamente qual era o problema.

**Dois User-Agents, de propósito.** `USER_AGENT` (descritivo) para APIs e feeds; `BROWSER_HEADERS`
para os alvos de scraping de HTML — GE Globo e GitHub Trending. As duas famílias de site querem
coisas opostas.

**O `SYSTEM_PROMPT` é a camada de apresentação.** Seções, ordem, emojis, onde links são permitidos e
formatação vivem no prompt ([core/ai_engine.py](core/ai_engine.py)), não em código. Restrições que
já custaram bug: só `<b>`, `<i>` e `<a href>`; bullets com `•` (o modelo recorre a `*` do Markdown se
não mandarem o contrário, e `*` aparece literal em HTML); a regra anti-repetição é escopada às
seções de notícia — Clima e Economia são exemplos de conteúdo que **deve** repetir todo dia.

**Título de seção não tem tamanho de fonte.** O Telegram não expõe nada equivalente a `<h2>`: o
subset dele tem só negrito, itálico, sublinhado, tachado, código e link. O que faz um cabeçalho
parecer cabeçalho é a convenção do prompt — régua `━━━━━━━━━━━━━━━`, título em CAIXA ALTA dentro de
`<b>`, linha em branco antes do conteúdo. Tag de heading no output é convertida em quebra de linha
pelo sanitizador, então pedir `<h2>` ao modelo não falha: só apaga o título.

**Seções lidas por tópico.** Clima e Investimentos são consultados de relance, não lidos como
parágrafo — cada fato tem seu bullet com rótulo em negrito, e as strings chegam prontas do Python
(`rain_summary`, `sun_summary`, `uv_summary`, `today_summary`, `idea_of_the_day`). O emoji de
condição do clima (`_wmo_emoji`, ☀️/⛅/☁️/🌧️/⛈️…) também é montado em Python e não escolhido pelo
modelo: o mesmo código WMO tem que dar sempre o mesmo ícone. As setas 🔺/🔻 da máxima vêm coladas
no `vs_ontem` por `enrich_payload`.

**Números vêm formatados de Python, não do LLM.** Cada ativo em `finance` carrega um campo `display`
já em pt-BR, e o prompt manda copiá-lo literalmente. Modelo formatando número produz `R$ 0.0034278`,
e pior: em produção ele chegou a calcular uma variação percentual do IBOVESPA que ninguém pediu.

**A sugestão de investimento não é opinião do modelo.** [investments.py](scrapers/investments.py) lê
as séries do SGS do Banco Central (Selic, CDI, IPCA, poupança — públicas, sem chave) e calcula em
Python o juro real (Fisher, não subtração: 13,90 − 4,64 dá 9,26 e o certo é 8,85) e a anualização da
poupança. `_investment_ideas` monta um pool de ~5 afirmações prontas, cada uma com um `id` estável;
`core/history.apply_investment_idea` escolhe a menos usada nos últimos dias e a move para
`idea_of_the_day` (removendo o resto do payload). O modelo só reescreve essa frase — o prompt proíbe
que produza número, projeção ou rendimento próprio, cite ativo específico, corretora ou emissor.
Não há mais os bullets de perfil (Conservador/Moderado/Arrojado) nem o disclaimer — foram removidos
na Rodada 5 por serem sempre iguais / a pedido (uso pessoal).

**Duas anti-repetições, e elas não são intercambiáveis.** A escolha entre uma e outra depende de
o payload ser igual ou não ao que se publica:

* **Jogos** — `apply_repeat_policy` compara *conjuntos de títulos*, porque o payload é trimado para
  exatamente o que vai ao ar (4 ofertas), então registrar o oferecido é registrar o publicado.
* **Notícias** — `filter_published_items` compara *nomes próprios contra o texto dos
  jornais recentes*. Aqui o payload traz 15 candidatos e o modelo publica 3, então guardar os
  títulos oferecidos apagaria 12 matérias que nunca saíram. A comparação é por proporção
  (`min_ratio`), não exata: o feed diz "EUA" onde o jornal escreveu "Estados Unidos", e verbo em
  início de frase entra em maiúscula como se fosse nome ("Morre pai de Messi"). Só o nome próprio
  atravessa a tradução — metade dos feeds é em inglês e o jornal sai em português.
  Três travas contra apagar notícia legítima: nomes presentes em *todos* os jornais recentes são
  pano de fundo e são ignorados ("Brasil", "Athletico"); pelo menos dois nomes precisam coincidir
  de fato; e a seção nunca cai abaixo do mínimo — ficar sem a seção Mundo é pior que repetir.

**Janela de chuva olha para frente e para o pico.** `_rain_window` recebe a hora atual e descarta o
que já passou: o jornal chega às 5h55 e avisar sobre a chuva das 0h é descrever a madrugada que a
pessoa dormiu. Entre os blocos que restam vence o de maior pico, não o primeiro — em 08/08/2026 os
47% da madrugada escondiam os 100% das 15h. Bloco longo demais é estreitado para o miolo, porque
"chuva entre 5h e 23h" não é aviso; `rain_all_day` preserva a diferença entre "chove à tarde" e
"chove o dia todo, mais forte à tarde".

**403 da football-data.org não é falha, é plano.** A Seleção joga competições fora do plano
gratuito, então o 403 é definitivo e chegava todo dia como `partial` — disparando justamente o
alerta que a Fase 1 criou para não ser ignorado. `_is_out_of_plan` degrada o time para só-notícias
em silêncio. Outros códigos HTTP continuam sendo falha.

**Rodízio de jogos é decisão de conjunto, resolvida em Python.** A regra anti-repetição do prompt
compara texto e não dá conta de listas: os mesmos jogos voltavam todo dia com outra redação.
`apply_repeat_policy` ([core/history.py](core/history.py)) usa os títulos já publicados, guardados em
`highlights`. Ofertas pagas repetidas são descartadas e substituídas pelas próximas da fila — por
isso o scraper devolve `candidate_pool` (16) candidatos e só `max_deals` (4) são exibidos. Giveaways
repetidos ficam quando estão acabando: "termina amanhã" é justamente a informação que só serve no dia
em que o item já apareceu antes. Prazos são calculados em Python (`_days_until`), nunca pelo modelo.

**A cadeia de cotações preenche ativo a ativo**, não tudo-ou-nada
([finance.py](scrapers/finance.py)): HG Brasil → yfinance → AwesomeAPI, cada fonte cobrindo o que
faltou. `BTC-BRL` saiu do Yahoo e é derivado de `BTC-USD × USD-BRL`.

**Config-driven é regra.** URLs, feeds, seletores CSS, times, coordenadas e produtos ficam em
[config/config.yaml](config/config.yaml). Nos feeds RSS, os itens são intercalados em round-robin
antes do corte — concatenar e truncar fazia os primeiros feeds consumirem todos os slots.

**`news_rss.py` é genérico por categoria.** Uma seção nova de notícias (ex.: `local`, a
`📍 CURITIBA & PARANÁ`) é só: `rss_feeds.<cat>` no config, uma entrada em `SCRAPERS`
([main.py](main.py)), as regras no `SYSTEM_PROMPT` e a seção em `COVERAGE_SECTIONS`
([core/history.py](core/history.py)) e no `_fallback_journal`. Não há lógica por categoria dentro
do scraper além do nome. O `local` usa Gazeta do Povo PR + Tribuna PR; o g1 PR ficou de fora porque
a Globo bloqueia a validação — testar na VPS antes de somar. A triagem de obituário/horóscopo/
policial é feita pelo prompt, não por blocklist em Python.

**Scrapers de HTML são frágeis por natureza.** Futebol (GE) e GitHub Trending dependem de seletores
CSS. Nunca use seletor amplo como `"div, section"`: ele casa com a página inteira e injeta o body
todo no payload do LLM. Prefira seletores específicos com alternativas por vírgula, limite de
tamanho por bloco, e uma lista de ruído quando o site mistura chamadas de engajamento ao feed.

**Nomes `OPENAI_*` são herança.** `Settings` usa `llm_*` e o `.env` aceita `LLM_*`, `GEMINI_*` ou
`OPENAI_*` nessa ordem de precedência ([config/settings.py](config/settings.py)). `_resolve_model`
ignora nomes que não sejam do Gemini, porque o `.env.example` antigo distribuía `gpt-4o-mini`.

**Emoji não vai para o log.** O console do Windows usa cp1252 e quebra no `StreamHandler`; os
alertas montam o emoji só na hora de enviar (`_format_issues`). Pelo mesmo motivo, `--dry-run` e
`--no-llm` chamam `_use_utf8_stdout`.

**Estado local em `logs/`:** `history.json` ([core/history.py](core/history.py)) guarda 30 dias com
poda automática — o texto dos jornais alimenta a anti-repetição (3 últimos vão ao prompt) e as
métricas alimentam o contexto comparativo ("maior valor em 30 dias"). Só é gravado após envio
bem-sucedido: um jornal que não chegou não pode suprimir as notícias dele amanhã. Migra sozinho o
`last_journal.txt` da versão anterior. `football_teams.json` cacheia os ids resolvidos da API.

**Alerta é sobre resultado, não sobre fonte.** Uma fonte que falha mas é coberta pelo fallback vai
só para o log — a AwesomeAPI responde 429 todo dia e alertar sobre isso treinaria o leitor a
ignorar os alertas. `_fetch_quotes` só reporta o que ficou faltando no fim.

**A seção "Achados & Promoções" saiu do projeto** (Rodada 5 do [PLANO.md](PLANO.md)) para virar um
projeto à parte — a especificação está em [PROMOCOES_PROJETO.md](PROMOCOES_PROJETO.md). Este repo
não coleta mais promoção nem cupom.

**Futebol separa jogo de notícia.** A API football-data.org dá adversário, data e placar; o GE dá
notícia, filtrada por `relevance_keywords` ([config.yaml](config/config.yaml)). Sem o token a seção
degrada para só notícias, e é omitida quando não há nada relevante — antes ela repetia as mesmas
manchetes por dias.
