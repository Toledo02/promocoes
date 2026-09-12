# Plano de melhorias — Jornal Matinal

Levantamento feito em 27/07/2026 sobre o commit `040ae1a` (v2.1), com base em duas execuções reais
(local v2.0 às 20:27 e VPS v2.1 às 05:55) e em testes isolados dos scrapers.

As fases estão em ordem de execução recomendada. Cada item tem o arquivo afetado e um critério de
pronto. Marque `[x]` conforme implementarmos.

---

## Fase 0 — Segurança (fazer antes de qualquer código)

- [x] **0.1 Revogar o token do bot.** Feito em 28/07. Confirmado via `getMe`: o bot
  @Jornal_matinal_bot responde com um segredo diferente do que vazou (o id 8947249677 não muda no
  revoke, só a parte secreta). O token antigo está morto.
- [x] **0.2 Silenciar o `httpx`.** Feito em `setup_logging` ([core/utils.py](core/utils.py)) —
  `httpx` e `httpcore` em WARNING. Verificado no mesmo arquivo de log: a execução anterior à
  correção gravou 18 linhas de requisição e 1 token; a posterior, zero e zero.
- [ ] **0.3 Apagar os logs antigos** que contêm o token, local e na VPS
  (`rm ~/jornal/logs/journal_*.log`). Fazer depois de 0.1.

**Pronto quando:** nenhum arquivo em `logs/` contém `api.telegram.org/bot<token>`.

---

## Fase 1 — Observabilidade

Motivação concreta: os feeds do Omelete e do JovemNerd retornam **404 desde 04/07** e o jornal
continuou chegando bonito por três semanas. A falha existe, mas é invisível.

- [x] **1.1 Notificar falhas pelo Telegram.** Feito. `_collect_issues` / `_format_issues` em
  [main.py](main.py) e `send_alert` em [core/telegram_sender.py](core/telegram_sender.py). O aviso
  vai em texto puro (sem `parse_mode`), porque mensagens de erro carregam `<`, `>` e `&` que
  quebrariam o parser HTML justamente quando algo já deu errado.
- [x] **1.2 Notificar crash do pipeline.** Feito, em três pontos: seções insuficientes, falha no
  envio do jornal, e exceção não tratada em `main()`. O `_notify` nunca propaga exceção — um aviso
  que falha não pode derrubar o pipeline.
- [x] **1.3 Auditar o log de prod.** Feito em 27/07 — resultado abaixo.

**Pronto quando:** derrubar um feed de propósito gera aviso no Telegram no mesmo dia.

### Resultado da auditoria (47 dias de log, 11/06 a 27/07)

| Falha | Desde | Frequência | Item |
|---|---|---|---|
| AwesomeAPI `429 Too Many Requests` | 11/06 | **todo dia, sem exceção** | 5.6 |
| Omelete + JovemNerd `404` | 04/07 | todo dia | 2.2 |
| CheapShark `400 Bad Request` | **10/07** | todo dia | 7.1 |
| Gemini `503 UNAVAILABLE` | 11/06 | ~7 dias esparsos | 9.1 |
| Telegram `can't parse entities` | 11/06 | 4 ocorrências, cessou em 13/06 | Fase 4 |

Três conclusões que mudam o diagnóstico:

1. **A AwesomeAPI nunca funcionou em prod.** 429 em *todas* as execuções desde 11/06. Com 1
   requisição por dia é impossível ser estouro de cota real — é bloqueio por reputação da faixa de
   IP da Oracle Cloud. Ou seja, a fonte primária de cotação está morta há 47 dias e o jornal vem
   silenciosamente do HG Brasil / yfinance.
2. **O 400 do CheapShark não é culpa dos parâmetros.** A v2.1 subiu em 04/07 e o scraper funcionou
   normalmente de 04/07 a 09/07 — o erro começa em **10/07**, sem nenhuma mudança no código.
   Foi o CheapShark que passou a rejeitar a VPS.
3. **Duas APIs distintas bloqueando o mesmo servidor** apontam para causa única: o IP da VPS.
   Confirmar com o teste da Fase 7.1 antes de decidir a correção.

Em 01/07 as 3 tentativas do Gemini falharam e o **jornal de fallback foi realmente enviado** — com
os asteriscos de Markdown e o JSON cru descritos no item 4.4.

---

## Fase 2 — RSS: distribuição e fontes

Contagem real de itens por feed **depois** do corte de `max_items_per_category: 15`:

```
tech         →  8 TechCrunch |  7 Verge      |  0 TabNews
world        →  8 G1         |  7 BBC        |  0 CNN Brasil
pop_culture  →  0 Omelete    |  0 JovemNerd  |  8 IGN  |  7 Polygon
```

- [x] **2.1 Intercalar em round-robin antes de truncar.** Hoje `all_items[:max_items]`
  ([news_rss.py:71](scrapers/news_rss.py#L71)) corta em ordem de chegada, então os dois primeiros
  feeds consomem todos os slots. Coletar 1 item de cada feed por rodada até atingir o limite.
  **Fazer antes do item 2.2** — com 4 feeds em pop_culture, corrigir só as URLs apenas inverte quem
  fica de fora.
- [x] **2.2 Trocar os feeds 404.** Omelete e JovemNerd derrubaram o RSS público (testei `/rss`,
  `/rss.xml`, `/feed`, `/feed/rss`, com e sem `www` — todos 404). Substitutos validados:
  - `https://br.ign.com/feed.xml` → 40 entradas, pt-BR (troca direta do IGN em inglês)
  - `https://www.legiaodosherois.com.br/feed` → 10 entradas
- [x] **2.3 Filtrar por recência.** Nenhum filtro de data hoje: um feed parado há dias contribui
  notícia velha para o jornal *matinal*. Usar `published_parsed` e descartar acima de ~30h.
- [x] **2.4 Remover HTML dos `summary`.** Vão crus para o Gemini
  ([news_rss.py:33](scrapers/news_rss.py#L33)), gastando tokens com tags e entidades.
- [x] **2.5 Revisar `entries_per_feed` / `max_items_per_category`** para o cenário de 4 feeds.

**Pronto quando:** todo feed configurado contribui itens, e nenhum fica em 0 sem aviso.

---

## Fase 3 — Prompt

- [x] **3.1 Escopar a regra anti-repetição.** A regra 11
  ([ai_engine.py:53](core/ai_engine.py#L53)) proíbe repetir notícia de ontem sem dizer a quais
  seções se aplica. Clima e Economia são repetitivos por natureza — há risco real do modelo suprimir
  a cotação do dólar por "já ter saído ontem". Listar explicitamente as seções sujeitas (Mundo,
  Tecnologia, Cultura Pop, Futebol) e isentar Clima, Economia e Neste Dia na História.
- [x] **3.2 Definir o caractere de bullet.** A regra 1 proíbe asteriscos e a regra 4 exige bullet
  points; sem alternativa dada, o modelo usa `*` do Markdown, que com `parse_mode="HTML"` aparece
  literal na mensagem (visível na saída real: `*   Dólar (USD): Compra 5.1288`). Mandar usar `•`.
- [x] **3.3 Travar a lista de seções:** proibir adicionar, renomear, remover ou reordenar.
- [x] **3.4 Corrigir o limite de tamanho.** A regra 8 pede 1500–2500 caracteres; a saída real deu
  3557 com 8 seções, e prod tem 9. Ou ajustar para uma faixa realista, ou impor corte por seção.

**Pronto quando:** nenhum `*` literal na mensagem e as seções saem sempre iguais e na mesma ordem.

---

## Fase 4 — Envio ao Telegram

- [x] **4.1 Split ciente de HTML.** `_split_message` ([core/telegram_sender.py:16](core/telegram_sender.py#L16))
  quebra em 4096 sem noção de marcação: pode cortar no meio de uma tag e invalidar o chunk seguinte.
  Ainda não estourou porque o jornal tem ~3,5k, mas cresceu de 8 para 9 seções.
- [x] **4.2 Remover tags no retry de texto puro.** Quando o envio HTML falha, o mesmo texto é
  reenviado sem `parse_mode` ([telegram_sender.py:78](core/telegram_sender.py#L78)) — então a
  mensagem chega com `<b>` e `<a href>` visíveis. Passar por um strip antes.
- [x] **4.3 Escapar `&`, `<`, `>`** em conteúdo dinâmico. URLs com `&` quebram o parser HTML do
  Telegram e derrubam a mensagem inteira para o fallback.
- [x] **4.4 Corrigir o fallback do LLM.** `_fallback_journal` ([ai_engine.py:89](core/ai_engine.py#L89))
  monta o texto com asteriscos de Markdown mas é enviado como HTML, e ainda despeja JSON cru
  (com `&` nas URLs). Gerar texto puro e enviar com `parse_mode=None` direto.

**Pronto quando:** um jornal de 8k caracteres com links é entregue íntegro em dois chunks.

---

## Fase 5 — Economia

- [x] **5.1 Fallback por ativo.** `_fetch_quotes` ([finance.py:143](scrapers/finance.py#L143))
  retorna assim que `usd_brl.bid` existe, sem verificar os demais ativos. Em prod isso produz
  "Bitcoin: Cotação indisponível": a AwesomeAPI está fora (5.6), o HG Brasil assume mas sua chave
  `free` não devolve o campo `bitcoin`, e como o USD veio preenchido ninguém consulta o yfinance —
  que **tem** `BTC-BRL` e resolveria. Preencher ativo a ativo com a próxima fonte da cadeia.
- [x] **5.2 Formatar números em Python.** Hoje vão floats crus para o modelo (`329233`,
  `175334.45`, `0.0034278`). Formatação numérica é justamente onde LLM erra. Entregar string pronta
  em pt-BR: `R$ 329.233`, `175.334 pts`, `R$ 5,13`.
- [x] **5.3 Calcular a variação do IBOVESPA em Python.** Quando a fonte é o yfinance, o payload traz
  `previous_close` e `last_close` mas nenhum campo de variação — e a saída de prod exibiu
  "variação de -1.52%", ou seja, aritmética feita pelo modelo. Dado financeiro não deve depender
  disso.
- [x] **5.4 Repensar o ARS.** `0.0034278` com 7 casas não informa nada; inverter para `1 BRL = X ARS`
  ou remover.
- [x] **5.5 Validar o scrape do InfoMoney** (`h2 a, h3 a`) — conferir se traz manchete ou menu.
- [x] **5.6 Rebaixar ou remover a AwesomeAPI.** Diagnóstico fechado: `{"code":"QuotaExceeded"}`,
  cota do plano gratuito contada por IP — não é UA nem bloqueio. Persiste desde 11/06 atravessando a
  virada de mês, então o contador daquele IP não reseta sozinho. Como é a primeira da cadeia, todo
  dia se gasta uma requisição inútil e um WARNING antes de cair para o HG Brasil.
  Duas saídas:
  - **Rebaixar para o fim da cadeia ou remover** (recomendado). O HG Brasil sustenta a seção sozinho
    há 47 dias em prod — só não sabíamos.
  - **Obter um `AWESOMEAPI_TOKEN` gratuito**, que vincula a cota à conta em vez do IP. ⚠️ Antes de
    concluir que resolve, conferir o método de autenticação: o código envia o token no header
    `x-api-key` ([finance.py:51](scrapers/finance.py#L51)), mas a AwesomeAPI documenta o parâmetro
    de query `?token=`. Se o nome estiver errado, o token é ignorado e o 429 continua.

- [x] **5.7 Reavaliar a ordem da cadeia por confiabilidade real.** A ordem atual assume que a
  AwesomeAPI é a melhor fonte, mas em prod ela nunca respondeu. E o HG Brasil é chamado com
  `key=free` ([finance.py:61](scrapers/finance.py#L61)) — chave de demonstração, também sujeita a
  limite, ou seja, o mesmo tipo de fragilidade que já nos mordeu. O yfinance é o único sem cota nem
  chave. Considerar promovê-lo, aceitando que é mais lento (5 chamadas sequenciais, ver 5.8).

- [x] **5.8 Paralelizar o yfinance.** `_fetch_yfinance_sync` busca ticker por ticker em sequência
  ([finance.py:105](scrapers/finance.py#L105)); `yf.download` faz em lote.

---

## Fase 6 — Futebol

- [x] **6.1 Substituir o scraping do GE por uma API.** ✅ Feito com football-data.org. Os campos se chamam `next_match`/`last_match`
  mas o scraper só extrai blocos de texto solto: nas duas execuções a Seleção saiu como "não há
  informações sobre jogos". Avaliar football-data.org ou api-futebol.com.br.
- [x] **6.2 Paliativo imediato:** remover o fallback `match_items: "div, section"`
  ([config.yaml:57](config/config.yaml#L57)), que casa com todas as divs da página e injeta lixo no
  payload.
- [x] **6.3 Atualizar o slug do Athletico.** O log mostra dois redirects 301 encadeados:
  `/futebol/times/atletico-pr/` → `/pr/futebol/times/atletico-pr/` → `/pr/futebol/times/athletico-pr/`.
  Apontar direto para a URL final.
- [x] **6.4 Passar datas explícitas.** O modelo escreveu "venceu o Internacional ontem" inferindo de
  texto sem data — risco de alucinação temporal.

---

## Fase 7 — Gaming

- [x] **7.1 O CheapShark exige User-Agent descritivo.** Causa confirmada em 27/07 pelo corpo da
  resposta 400 (servida por Cloudflare):
  ```json
  {"error": "Missing or generic User-Agent header detected. Please identify your client
   with a descriptive User-Agent (e.g., 'MyApp/1.0 (contact@example.com)')."}
  ```
  O UA atual, `Mozilla/5.0 (compatible; DailyJournalBot/2.0)`
  ([core/utils.py:13](core/utils.py#L13)), se disfarça de navegador — é justamente o padrão que a
  regra deles rejeita. Bate com a linha do tempo: funcionou até 09/07, quebrou em 10/07, quando eles
  ligaram a proteção. **Não é bloqueio de IP.**

  Correção: trocar `USER_AGENT` por algo identificável, no formato que eles pedem —
  `JornalMatinal/2.1 (+https://github.com/Toledo02/Jornal)`. Prefira a URL do repositório ao e-mail
  pessoal: cumpre a exigência de contato sem expor endereço pessoal a todo site que o agente acessa.

  **Validado na VPS em 27/07: esse UA retorna `200`.**

  ✅ **Aplicado** em [core/utils.py](core/utils.py). Confirmar na VPS após o deploy.

- [x] **7.1a Usar UA diferente para APIs e para scraping de HTML.** Feito. `BROWSER_HEADERS` em
  [core/utils.py](core/utils.py), aplicado nos quatro alvos de HTML: InfoMoney
  ([finance.py](scrapers/finance.py)), GE Globo ([football.py](scrapers/football.py)), Mercado Livre
  e Buscapé ([promotions.py](scrapers/promotions.py)), GitHub Trending
  ([gaming.py](scrapers/gaming.py)). APIs e feeds RSS seguem com o UA descritivo.
  Testado localmente: gaming, finance e football continuam `status=ok`.
  **Falta validar na VPS** — é lá que o comportamento difere.

- [x] **7.1b AwesomeAPI tem causa distinta.** Testada com o mesmo UA descritivo: continua `429`, e o
  corpo é claro — `{"code":"QuotaExceeded"}`. Não é User-Agent nem bloqueio: é cota de plano
  gratuito, contada por IP. Tratado no item 5.6.

- [x] **7.1c Plano B — desnecessário**, o User-Agent resolveu. Item original: substituir o CheapShark. Candidatas alinhadas com a
  seção "Ofertas & Games Grátis" (validar cada uma **na VPS**):
  - GamerPower (`gamerpower.com/api/giveaways`) — jogos grátis e giveaways, exatamente o tema
  - Epic Games free games (endpoint público de `freeGamesPromotions`)
  - IsThereAnyDeal (exige chave gratuita)
- [x] **7.2 Filtrar shovelware.** `sortBy=Savings` seleciona o maior desconto, que é justamente o
  jogo que ninguém compra. Retorno real: das 7 ofertas, só o Destiny 2 prestava — o resto era
  *Ship Graveyard Simulator*, *3 Stars of Destiny*, *Asguaard*. O CheapShark aceita `steamRating` e
  `metacritic`; `steamRating=70` cortaria quase tudo isso.
- [x] **7.3 Mover `github_trending` para fora do gaming.** Desde a v2.1 o scraper de gaming é só
  ofertas, mas os repos continuam nele ([gaming.py:129](scrapers/gaming.py#L129)) e são consumidos
  pela seção de Tecnologia.
- [x] **7.4 Corrigir o nome do repo.** Sai como `permissionlesstech /bitchat`, com espaço antes da
  barra ([gaming.py:102](scrapers/gaming.py#L102)).

---

## Fase 8 — DX e manutenção

- [x] **8.1 `--dry-run`** — imprime o jornal no stdout sem enviar. Hoje a única forma de validar é
  esperar o cron ou disparar de verdade no Telegram.
- [x] **8.2 `--only weather,finance`** — roda um subconjunto de scrapers.
- [x] **8.3 Pinar versões** no `requirements.txt`, hoje todo sem versão. Um release quebrado de
  `feedparser` ou `yfinance` derruba o cron num dia qualquer.
- [x] **8.4 Testes dos parsers puros:** `_parse_price`, `_split_message`, `_normalize_title`,
  `format_date_pt_br` e o round-robin da Fase 2.1. São determinísticos e sem rede.
- [x] **8.5 Renomear `openai_*` → `llm_*`** em `Settings` ([config/settings.py](config/settings.py))
  e atualizar o `.env.example`, que ainda manda `OPENAI_MODEL=gpt-4o-mini` — quem seguir o exemplo
  do repo cai direto no fallback.
- [x] **8.6 Timezone dos logs.** `setup_logging` usa `datetime.now()` local
  ([core/utils.py:40](core/utils.py#L40)) enquanto o `ai_engine` fixa `America/Sao_Paulo`; numa VPS
  em UTC o arquivo de log vira o dia antes do jornal.
- [x] **8.7 Atualizar o README**, que ainda descreve setup da OpenAI e a estrutura da v2.0.
- [x] **8.8 Resolver a seção de promoções.** `product_names: []` está vazio
  ([config.yaml:62](config/config.yaml#L62)), então a seção 🛒 aparece todo dia dizendo que não há
  nada. Popular a lista ou remover a seção do prompt.
- [x] **8.9 Reavaliar `min_sections_for_send: 1`** — com 8 scrapers, envia um jornal de uma seção só.

---

## Fase 9 — Resiliência do LLM

- [x] **9.1 Melhorar a estratégia de retry do Gemini.** O log registra `503 UNAVAILABLE` em ~7 dias
  distintos, e em 01/07 as três tentativas falharam dentro de ~60s, resultando no envio do jornal de
  fallback. O `retry_delay` é fixo em 10s ([ai_engine.py:129](core/ai_engine.py#L129)): curto demais
  para sobrecarga de modelo, que costuma durar minutos. Usar backoff exponencial (10s → 30s → 90s).
- [x] **9.2 Fallback de modelo.** Antes de desistir e cair no template, tentar um modelo alternativo
  (ex.: `gemini-2.0-flash`). Um 503 é do modelo específico, não da conta.
- [x] **9.3 Não repetir retry em erro permanente.** Hoje um 400 (modelo inexistente, chave inválida)
  gasta os mesmos 3 ciclos com 20s de espera de um 503 transitório. Distinguir 4xx de 5xx.
- [x] **9.4 `time.sleep` bloqueante.** `generate_journal` é síncrona mas chamada de dentro do loop
  async ([main.py:105](main.py#L105)); o sleep trava o event loop. Inofensivo hoje (os scrapers já
  terminaram), mas quebra se algo passar a rodar em paralelo.

---

## Notas de contexto

- **Prod ≠ local.** CheapShark e AwesomeAPI recusam a VPS e atendem a sua máquina normalmente.
  Toda validação de scraper que dependa de rede precisa acontecer *na VPS*, não só localmente.
- **Ler o corpo do erro antes de teorizar.** O 400 do CheapShark foi atribuído a bloqueio de IP com
  base em três testes de User-Agent que davam o mesmo código — mas as três variantes testadas eram
  igualmente "genéricas" pela regra deles, então o resultado uniforme não significava nada. O corpo
  da resposta explicava a causa em uma frase. Custa 30 segundos e evita reescrever o scraper à toa.
- **Falhas parciais são invisíveis hoje.** `status="partial"` só aparece no log. Foi assim que o 404
  dos feeds brasileiros passou três semanas despercebido, e o 429 da AwesomeAPI, 47 dias — é o que a
  Fase 1 resolve.
- **A cadeia de fallback funcionou bem demais.** Ela salvou o jornal todos os dias, e por isso
  escondeu que a fonte primária estava morta. Resiliência sem observabilidade vira dívida silenciosa:
  por isso a Fase 1 vem antes de qualquer correção de conteúdo.


---

## Rodada 2 — qualidade dos dados (28/07/2026)

Motivada pela leitura do jornal em produção: os dados chegavam, mas pouco processados.

- [x] **R2.1 Jogos de futebol via football-data.org.** A seção repetia as mesmas manchetes por
  dias porque as páginas de time do GE são feed de notícia, não agenda. Agora a API dá adversário,
  data e horário (convertido para o fuso de Brasília) e o placar do último jogo; o GE segue como
  fonte de notícia, filtrada por `relevance_keywords`. A seção é omitida quando não há nada.
  ⚠️ A API registra o Athletico como "CA Paranaense", então a resolução por nome falha — os ids
  ficam fixos no config (Athletico 1768, Seleção 764), confirmados na API.
- [x] **R2.2 Promoções por canais do Telegram.** `https://t.me/s/<canal>` expõe as últimas
  mensagens de canais públicos sem bot e sem token. Substitui o monitoramento de produto como
  fonte principal — o Mercado Livre passou a redirecionar para um muro de verificação e sua API
  pública exige OAuth; o Zoom devolve o mesmo catálogo do Buscapé, que ficou como fonte
  secundária opcional.
- [x] **R2.3 Jogos realmente gratuitos (GamerPower).** A seção prometia "Games Grátis" e trazia
  sete títulos obscuros a US$ 0,51 — os mesmos por semanas, porque o ranking de maior desconto do
  CheapShark é quase estático. Agora os giveaways vêm primeiro, ordenados por valor.
- [x] **R2.4 Clima com o que a API já dava de graça.** Condição, sensação térmica, vento,
  nascer/pôr do sol, índice UV e **janela de chuva por hora** — "chuva provável entre 16h e 19h"
  é o dado que muda o dia de quem lê às 6h.
- [x] **R2.5 Variação percentual nas moedas.** O campo já vinha do HG Brasil e era descartado.
  Sem ele o dólar parecia congelado, porque 5,1288 e 5,116 arredondam para o mesmo "R$ 5,12".
  A variação do ARS é invertida junto com a cotação (1 BRL = X ARS), senão o peso apareceria
  subindo nos dias em que enfraqueceu.
- [x] **R2.6 Histórico de 30 dias** ([core/history.py](core/history.py)), podado a cada gravação.
  Guarda o texto dos jornais (os 3 últimos vão ao prompt, contra 1 antes) e as métricas numéricas,
  que alimentam o contexto comparativo: "maior valor em 30 dias". Migra sozinho o
  `last_journal.txt` da versão anterior.
- [x] **R2.7 Alerta sobre resultado, não sobre fonte.** A AwesomeAPI falha todo dia e o BTC é
  derivado sem problema — alertar sobre isso diariamente treinaria o leitor a ignorar os alertas,
  que é exatamente o que a Fase 1 veio resolver. Agora só alerta o que faltou no jornal final.
- [x] **R2.8 Modelo `gemini-3.6-flash`.** Comparado com o mesmo payload: sintetiza as manchetes em
  texto próprio (o 2.5-flash copiava os títulos crus, com os "veja" e "confira" do original), sai
  26% mais curto e 15% mais rápido. Todos os modelos `pro` respondem 429 na cota gratuita.

### Ainda em aberto

- [ ] Revogar já foi feito; **apagar os logs antigos** da VPS que contêm o token antigo (item 0.3).
- [x] Avaliar `min_steam_rating` mais alto: mesmo com 70, as ofertas pagas do CheapShark ainda
  são de jogos obscuros. Os giveaways da GamerPower cobrem a seção, então talvez valha reduzir o
  peso das ofertas pagas. → resolvido na Rodada 3 (item R3.4), por outro caminho: o problema não
  era a nota, era a ordenação.

---

## Rodada 3 — leitura e relevância (08/08/2026)

Motivada pela leitura diária: a informação chegava correta, mas em bloco, e duas seções traziam
sempre o mesmo conteúdo.

- [x] **R3.1 Clima por tópico.** A seção era um parágrafo corrido, e achar a chance de chuva exigia
  ler a frase inteira. Agora é um bullet por tópico (agora, máxima e mínima, chuva, sol, vento, UV),
  com as strings montadas em Python — `rain_summary`, `sun_summary` e `uv_summary` em
  [weather.py](scrapers/weather.py). Mesma razão do campo `display` das cotações: texto pronto não
  dá margem para o modelo reformatar número.

- [x] **R3.2 Cabeçalho de seção com cara de cabeçalho.** ⚠️ **O Telegram não tem tamanho de fonte.**
  O subset HTML dele é `<b> <i> <u> <s> <code> <pre> <a>` e mais nada — não existe `<h2>`, `<h3>`,
  `<font>` nem CSS, e uma tag de heading que chegue ao sanitizador vira quebra de linha
  ([telegram_sender.py](core/telegram_sender.py), `BLOCK_TAGS`), ou seja, o título sumiria.
  O que dá para fazer, e foi feito: régua `━━━━━━━━━━━━━━━`, título em CAIXA ALTA dentro de `<b>` e
  uma linha em branco antes do conteúdo. Visualmente separa as seções; tecnicamente a fonte continua
  do mesmo tamanho, porque não há como mudá-la.

- [x] **R3.3 Seção de sugestão de investimentos.** Novo scraper
  [investments.py](scrapers/investments.py), lendo as séries do SGS do Banco Central (públicas, sem
  chave, sem cota): Selic 432, CDI 4389, IPCA 12m 13522, poupança 195. Juro real e anualização da
  poupança são calculados em Python — o juro real usa Fisher, não subtração (13,90 − 4,64 daria
  9,26; o correto é 8,85, e o erro é pequeno o bastante para nunca ser notado no jornal).
  O prompt recebe `talking_points` já redigidos e tem regra explícita: nada de calcular rendimento,
  citar ativo específico, corretora ou emissor, ou mandar comprar — só classe de ativo, mais o
  disclaimer no fim da seção.

- [x] **R3.4 Filtro de jogos.** Duas causas distintas, tratadas separado:
  - **Irrelevância** era a ordenação. `sortBy=Savings` ordena por desconto puro, e o topo dessa
    lista é estático justamente porque ninguém compra aqueles jogos. Trocado por
    `sortBy=Deal Rating` (a nota do próprio CheapShark, que pondera desconto e qualidade) mais
    `AAA=1` (preço cheio ≥ US$ 29), `steamRating=75`, `min_steam_reviews: 1000` e
    `min_metacritic: 70`. Antes: *Ship Graveyard Simulator*, *3 Stars of Destiny*, *Asguaard*.
    Depois: The Witcher 3, Disco Elysium, Civilization VI, BioShock Infinite.
    Nota alta sozinha não bastava — 95% de aprovação com 40 votos é ruído, e era assim que jogo
    obscuro passava pelo filtro de nota que existia desde a Fase 7.2.
  - **Repetição** não era resolvível no prompt: o modelo compara texto, não listas, e o mesmo jogo
    voltava reescrito. Agora `apply_repeat_policy` ([core/history.py](core/history.py)) guarda os
    títulos publicados em `highlights` e faz o rodízio em Python. Oferta paga repetida é descartada
    e substituída (o scraper devolve 16 candidatos para exibir 4); giveaway repetido fica só quando
    está acabando, porque "termina amanhã" é útil exatamente no dia em que o item já apareceu antes.
    Os prazos (`_days_until`) são calculados em Python, pelo mesmo motivo do item 6.4.
  - Na GamerPower, `min_worth_usd: 5` corta o giveaway de US$ 0,99 — que ocupa a mesma linha de um
    de US$ 25 —, com exceção de quem está no último dia.

- [x] **R3.5 Promoção aponta para o canal, não para a loja.** Boa parte das ofertas dos canais só
  vale com cupom, e o cupom está no post. O link ia direto para a loja, então quem clicava pagava o
  preço cheio. Agora o payload traz `link` (o permalink do post) como campo canônico, `store_url`
  como referência, e `_coupon` extrai o código quando ele aparece na prévia — o prompt manda
  destacá-lo em negrito. Validado em produção: `OFERTA8DO8`, `INFLU350`, `BELEZA8DO8`.

### Consequência a acompanhar (Rodada 3)

O jornal cresceu de ~3.5k para ~5.8k caracteres — uma seção nova e mais estrutura por seção — e
passou a sair em **duas mensagens** do Telegram. O split é ciente de HTML (Fase 4.1) e não quebra
marcação, mas a leitura fica pior. O prompt já foi apertado (limite por seção, um fato por bullet,
teto de 5000) e derrubou ~22%; se incomodar, os candidatos a corte são GitHub Trending (5 repos) e
Cultura Pop.

---

## Rodada 4 — relevância e ruído (08/08/2026)

Feita na sequência da Rodada 3, a partir do que os testes daquele dia expuseram.

- [x] **R4.1 A janela de chuva apontava para o passado — e para a chuva errada.** Confirmado com os
  dados reais de Curitiba em 08/08: `[00h:47, 01h:42, 02h:31, ..., 13h:83, 14h:94, 15h:100, 16h:95,
  17h:85, ...]`. `_rain_window` devolvia o **primeiro** bloco acima do limiar e parava no primeiro
  buraco, então travava no resmungo de 47% da madrugada; o jornal daquele dia imprimiu
  "mais provável entre 0h-1h" — uma janela que já tinha passado quando a mensagem chegou, e que
  ignorava a chuva de verdade da tarde. Corrigido em três frentes
  ([weather.py](scrapers/weather.py)): descarta horas anteriores à atual, escolhe o bloco de maior
  pico e estreita o bloco longo demais para o miolo ("chuva entre 5h e 23h" não é aviso).
  `rain_all_day` preserva a diferença entre "chove à tarde" e "chove o dia todo, mais forte à
  tarde". Hoje sai: *"100% de chance, 3,1 mm previstos, chuva praticamente o dia todo, mais forte
  entre 15h-17h"*.

- [x] **R4.2 O futebol garantia um alerta por dia.** O 403 da Seleção não é intermitente: o plano
  gratuito da football-data.org não cobre as competições dela, então a resposta é definitiva. A
  seção voltava `partial` todo dia e disparava o "🩺 fontes com falha" — o mecanismo que a Fase 1
  criou justamente para não ser ignorado. `_is_out_of_plan` ([football.py](scrapers/football.py))
  trata 403 como falta de cobertura e degrada o time para só-notícias em silêncio, igual ao que já
  acontecia sem token. Outros códigos HTTP continuam sendo falha. Verificado: `status=ok`, sem
  alerta, dados do Athletico intactos.

- [x] **R4.3 Anti-repetição de notícia em Python.** Mesma fraqueza estrutural dos jogos: a regra 12
  do prompt pede ao modelo que compare a matéria de hoje com três jornais em prosa, e ele compara
  mal. **Mas a solução dos jogos não servia aqui** — lá o payload é trimado para exatamente o que
  vai ao ar, então registrar o oferecido é registrar o publicado; nas notícias o payload traz 15
  candidatos e o modelo publica 3, e guardar os títulos oferecidos apagaria 12 matérias que nunca
  saíram. `filter_published_items` ([core/history.py](core/history.py)) compara os **nomes próprios
  do título contra o texto dos jornais recentes**: substantivo comum não sobrevive à tradução
  (metade dos feeds é em inglês), nome próprio sobrevive.
  Exigir coincidência exata deixava o filtro inerte — o título diz "EUA" onde o jornal escreveu
  "Estados Unidos", e verbo em início de frase entra em maiúscula como se fosse nome ("Morre pai de
  Messi"). Por isso a correspondência é por proporção. Validado com o RSS real: removeu as duas
  matérias sobre o Estreito de Ormuz e as duas sobre a morte do pai do Messi (uma delas em inglês),
  sem falso positivo.

- [x] **R4.4 Uma campanha tomava a seção de promoções.** 3 dos 4 achados do jornal eram o mesmo
  cupom `OFERTA8DO8`. `max_per_coupon: 2` ([config.yaml](config/config.yaml)) limita por campanha, e
  as promoções entraram no `filter_published_items` — o produto anunciado ontem sai, outro produto
  da mesma loja fica.

- [x] **R4.5 Testes do caminho de fallback.** `_fallback_journal` é o que roda no pior dia (em
  01/07 as três tentativas do Gemini falharam e ele foi realmente enviado) e não tinha teste nenhum.
  Novo módulo [tests/test_quality.py](tests/test_quality.py), separado do `test_parsers.py` porque a
  pergunta é outra: não "isto formata certo?", mas "dado o que as fontes trouxeram e o que já foi
  publicado, o que sobra?". A propriedade central testada é que o fallback atravessa o
  `sanitize_html` **intacto** — foi ela que o item 4.4 quebrou.

- [x] **R4.6 Fuso do histórico.** `prune` e `_highlight_counts` usavam `date.today()` (do servidor)
  enquanto `record` grava com `now_local()`. Com cron às 5h55 as datas coincidem, mas a janela
  deslizaria meio dia numa VPS em UTC. Unificado em `now_local()`.

- [x] **R4.7 Jornal antigo passando por recente.** Descoberto ao medir o payload: `recent_journals`
  devolve os N mais recentes **sem olhar a data**, então depois de uma interrupção do cron o jornal
  de um mês atrás entraria como se fosse o de ontem — e agora que ele suprime notícia, isso deixou
  de ser inofensivo. `filter_published_items` limita por idade além de por quantidade.

### Medição: cortar o payload de notícias não vale a pena

Item levantado e **descartado com número**. Payload real de 08/08, 42.885 caracteres (~10,7k tokens):

| seção | chars | % |
|---|---|---|
| pop_culture | 8.319 | 19,4% |
| tech_news | 8.184 | 19,1% |
| world_news | 7.957 | 18,6% |
| gaming | 7.486 | 17,5% |
| promotions | 4.754 | 11,1% |
| demais (finance, investments, football, github, weather) | 6.031 | 14,1% |

As notícias são 57% do payload e os `summary` são 40% desse bloco — mas a mediana de um summary é
190 caracteres e só 5 de 45 encostam no teto de 400. Cortar em 220 economizaria 5,2% do payload;
cortar em 150, agressivo o bastante para prejudicar o contexto, economizaria 8,7%. Para um modelo de
1M de contexto na cota gratuita, não paga o risco. E depois do R4.3 ter mais candidatos passou a
valer **mais**, não menos: o filtro consome candidatos antes do modelo escolher.

---

## Rodada 5 — relevância, legibilidade e foco pessoal (28/08/2026)

Motivada pela leitura diária de quem recebe o jornal. Diagnóstico por seção: **Clima** e **Economia**
são lidos de relance mas pouco escaneáveis; a frase de manchete da Economia é sempre genérica
("o mercado reage a movimentações corporativas e à postura defensiva de analistas") e não informa
nada; **Ideias de investimento** repete os mesmos perfis Conservador/Moderado/Arrojado todo dia;
**Mundo** traz o global mas nada da cidade onde o leitor mora (Curitiba); **Achados & Promoções**
está sem produto configurado e cabe melhor num projeto dedicado; as seções de notícia lida por
inteiro (Tech, Mundo, Cultura Pop) são bloco de texto sem ponto de ancoragem visual.

Ordem de execução: R5.6 → R5.2 → R5.3 → R5.4 → R5.5 → R5.1 → R5.7. Contagem de seções: 10 → 10
(sai Achados, entra Curitiba).

**Resultado medido (`--dry-run`, 28/08):** 5.937 caracteres, 2 mensagens [3.728 / 2.209], 10 links,
formatação ok. Praticamente empatado com os ~5,8k de antes — a seção Curitiba e o bullet "Hoje" do
clima compensaram a saída de Achados e dos 3 bullets de perfil. Continua em 2 mensagens; reavaliar o
teto na Rodada 6 se incomodar, com este número de base.

### Decisões deliberadas

- **Duas mensagens do Telegram seguem aceitas.** O teto do prompt fica em 4500–5000; a decisão de
  apertar para caber numa bolha só se toma **com medição real** (`--dry-run` depois de tudo), na
  Rodada 6 — o mesmo critério da "Medição" da Rodada 4.
- **Sem arquivo de preferências.** Não há knob suficiente para justificar uma camada nova; o que
  valer parametrizar vira chave no `config.yaml` que já existe.
- **Interatividade via serviço ou poll `getUpdates` está fora de escopo** e não entra no backlog —
  contradiz "job único via cron, não serviço".
- **Futebol fica como está** — já é condicional e curto (5 linhas de prompt); o "sempre igual" é o
  Athletico jogar toda semana, não um defeito.

---

- [x] **R5.6 Remover a seção `🛒 ACHADOS & PROMOÇÕES` deste repo.** `product_names: []` está vazio
  desde sempre e o leitor quer isso como projeto dedicado varrendo muitos grupos (ver R5.7).
  **Intacta:** a seção `🎮 OFERTAS & GAMES GRÁTIS` inteira — jogos grátis da GamerPower **e** os 3
  "deals" AAA do CheapShark (pós-R3.4). O `git` guarda o histórico; nada de dead code "por precaução".
  ✅ **Feito em 28/08.** Removidos: `scrapers/promotions.py`, a entrada em `SCRAPERS` e o import
  ([main.py](main.py)), a seção e as 3 regras no `SYSTEM_PROMPT`
  ([core/ai_engine.py](core/ai_engine.py)), `"promotions"` de `COVERAGE_SECTIONS`
  ([core/history.py](core/history.py)), o bloco `promotions:` do [config.yaml](config/config.yaml),
  `promotions_history.json` do `.gitignore`, os testes `test_parse_price` / `test_slugify` /
  `test_coupon` / `test_filtro_alcanca_as_promocoes`, e as menções no README e no CLAUDE.md.
  `_fallback_journal` já não referenciava a seção. `pytest -q`: 115 passam; `grep -ri promotion` em
  `scrapers/`, `core/` e `config/` não acha nada.

- [x] **R5.2 Economia sem linha de manchete.** A frase de fechamento sobre `headlines` é sempre vaga
  porque compete por uma manchete boa com os feeds de Mundo/Tech e perde. Economia passa a ser só os
  5 bullets de cotação com o `display` formatado em Python.
  ✅ **Feito em 28/08.** Removidos: `_scrape_target` e os imports `BeautifulSoup` / `BROWSER_HEADERS`
  / `http_get_text` ([scrapers/finance.py](scrapers/finance.py)), o bloco `finance.scrape_targets` do
  [config.yaml](config/config.yaml), o campo `headlines` do payload, a linha de manchete do bloco
  ECONOMIA e "InfoMoney" das notas do CLAUDE.md. `fetch` agora falha só quando não há cotação nenhuma.

- [x] **R5.3 Ideias de investimento sem perfis.** A seção fica: **Referências** + **Destaques da
  bolsa** (rotação já existente) + **1 bullet "Ideia do dia"**.
  ✅ **Feito em 28/08.** `_talking_points` → `_investment_ideas`, agora retorna dicts `{id, text}`
  com `id` estável ([investments.py](scrapers/investments.py)); o payload leva o pool em `ideas`.
  Novo `history.apply_investment_idea` (janela `investment_idea_window_days: 5`) escolhe a menos
  usada por `_highlight_counts("investment_idea")`, move para `idea_of_the_day` e tira o pool do
  payload; `extract_highlights` registra o `id` escolhido; chamado no [main.py](main.py) junto do
  rodízio de ações. Removidos do prompt: bullets de perfil, a linha do disclaimer, a regra 9 sobre
  "profile bullet"; `idea_of_the_day` entrou nas fontes numéricas permitidas. Removidos do
  scraper/config: `profiles`, `disclaimer`. `_fallback_journal` mostra a ideia, sem disclaimer.
  5 testes novos.

- [x] **R5.4 Clima escaneável com emoji de condição.**
  ✅ **Feito em 28/08.** `_wmo_emoji` em [weather.py](scrapers/weather.py) (0-1 ☀️, 2 ⛅, 3 ☁️,
  45-48 🌫️, 51-57 🌦️, 61-67/80-82 🌧️, 71-77/85-86 🌨️, 95-99 ⛈️); payload expõe
  `condition_now_emoji` e `today_summary` ("🌧️ Chuva moderada") a partir do `weather_code` diário
  que antes era ignorado. Setas 🔺/🔻 coladas no `vs_ontem` por `enrich_payload` (limiar 3 °C, que
  já existia). Prompt: bullet "Agora" abre pelo emoji, **novo bullet "Hoje"** copia `today_summary`;
  `_fallback_journal` também mostra "Hoje". Teste parametrizado de `_wmo_emoji`. Verificado com
  `--no-llm`: `today_summary` = "🌦️ Garoa fraca".

- [x] **R5.5 Rótulo em negrito nas seções de notícia.**
  ✅ **Feito em 28/08.** Nova regra "NEWS SECTIONS" no `SYSTEM_PROMPT`: cada bullet de Tech, Mundo,
  Curitiba e Cultura Pop abre com o sujeito em `<b>…:</b>` (rótulo de 1-3 palavras); válvula de
  rótulo temático quando não há sujeito único; `<b>` proibido no meio da frase; não vale para os
  repos do GitHub Trending nem para o resumo do futebol. A regra 9 que mandava "não abrir bullet
  com rótulo em negrito" foi reescrita para o contrário.

- [x] **R5.1 Nova seção `📍 CURITIBA & PARANÁ`.** Posição: **depois de MUNDO, antes de CULTURA POP**.
  Feeds: **Gazeta do Povo – PR** (`gazetadopovo.com.br/feed/rss/parana.xml`) + **Tribuna do Paraná**
  (`tribunapr.com.br/feed/`). g1 PR ficou de fora (Globo bloqueia validação; testar na VPS antes de
  somar como terceiro).
  ✅ **Feito em 28/08.** `rss_feeds.local` no [config.yaml](config/config.yaml), entrada `local` em
  `SCRAPERS` ([main.py](main.py)) — o `news_rss.fetch(category="local")` já resolve para a seção
  `local` sem código novo. No `SYSTEM_PROMPT`: título na lista de seções, regra de conteúdo
  ("até 3 fatos"; priorizar política municipal/estadual, economia, obras, trânsito, eventos;
  ignorar obituário/horóscopo/loteria/policial de rotina; omitir se nada), entrada nas NEWS
  SECTIONS (rótulo em negrito), na regra de links e na anti-repetição (regra 12). `local` em
  `COVERAGE_SECTIONS` (mínimo 2) e no `_fallback_journal`. Teste de cobertura para a seção.
  Verificado com `--no-llm`: 9 itens de Curitiba/PR, conteúdo regional.

- [x] **R5.7 `PROMOCOES_PROJETO.md` — doc de handoff.**
  ✅ **Feito em 28/08.** [PROMOCOES_PROJETO.md](PROMOCOES_PROJETO.md) na raiz, 10 seções: objetivo
  e recorte (não é comparador de preço, não é bot de afiliados, não é serviço 24/7), arquitetura
  como fork (tabela do que reaproveitar do Jornal), contrato do scraper, leitura via
  `https://t.me/s/<canal>` com os pontos que o código antigo já resolveu, filtros/limites em
  Python (`max_per_coupon`, `filter_published_items`), schema de config, formato da mensagem
  (template Python padrão, LLM opcional), deploy cron, "como achar e validar um canal novo" +
  candidatos, e critério de pronto. Aponta para o `scrapers/promotions.py` no histórico do git
  como referência.
