# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Visão geral

Agente Python que lê canais públicos de promoção do Telegram, filtra, deduplica e envia um
resumo de achados em pt-BR pelo Telegram. Roda como job único via cron 1–2 vezes ao dia
(sugestão: 08h e 18h), não como serviço.

O projeto é um fork do **Jornal Matinal**, de onde vêm o esqueleto (`main.py`, `core/utils.py`,
`core/telegram_sender.py`, `core/history.py`) e boa parte das lições registradas aqui. A
especificação que originou o recorte está em [PROMOCOES_PROJETO.md](PROMOCOES_PROJETO.md) — vale
consultar antes de mudar escopo. Os scrapers do jornal (clima, câmbio, RSS, futebol…) não fazem
parte deste projeto; se ainda estiverem em `scrapers/`, são resíduo do fork e podem sair.

## Comandos

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp config/.env.example config/.env

python main.py                            # pipeline completo (envia ao Telegram)
python main.py --dry-run                  # monta e imprime, sem enviar
python main.py --no-llm                   # só coleta e imprime o payload cru
python main.py --no-llm --only promotions # o mesmo, restrito a uma fonte
python -m pytest -q                       # 101 testes, sem rede
```

Ao iterar em canais use `--dry-run`: mostra a mensagem final, os alertas que seriam enviados e o
diagnóstico de formatação, sem enviar nada e sem gravar histórico.

## Arquitetura

Pipeline linear em [main.py](main.py): `load_settings` → `fetch` (canais em paralelo) → filtros
do histórico → `generate_digest` (escolhe **e** formata) → `sanitize_html` → `send_digest` →
grava o histórico **só se enviou**.

**Contrato do scraper.** `async def fetch(settings) -> ScraperResult`, registrado em `SCRAPERS`
([main.py](main.py)). `ScraperResult` ([core/utils.py](core/utils.py)) tem `status` = `ok` |
`partial` | `error`; scrapers **nunca propagam exceção**. `partial` quando um canal falhou e
outros entregaram; `error` só quando todos falharam.

**O scraper devolve um pool, não a lista final.** `candidate_pool` (30) candidatos, `max_items`
(8) publicados. A folga existe porque o corte final acontece *depois* do histórico: sem ela, o
segundo envio do dia sairia pela metade, já que tudo que a manhã mostrou é removido. Mesmo
princípio do `candidate_pool` das ofertas de jogos no jornal.

**Quem escolhe o que é publicado é o LLM, não o Python — quando ele está ligado.**
`main.py` não corta o pool em `max_items` antes de chamar `generate_digest`: cortar ali seria
escolher por ordem de chegada e nunca deixar o modelo comparar desconto/preço entre candidatos,
que é o pedido original (issue de 12/09/2026 — "deixar que a LLM decida quais são as melhores
ofertas baseada em relevância"). `core/ai_engine.generate_digest` recebe o pool inteiro
pós-histórico (ainda com produto duplicado entre canais, ainda sem teto de cupom) e devolve
`(mensagem, ofertas_escolhidas)` — a tupla importa porque só as `ofertas_escolhidas` vão para o
histórico (`digest.offer_keys`); gravar o pool inteiro suprimiria amanhã candidato que o modelo
nunca publicou. Regras do prompt que fazem esse trabalho: maior desconto/menor preço declarado
no texto > produto reconhecível > banner de loja vago; mesmo produto em dois candidatos → fica
só o de melhor preço; mesmo cupom não passa de `max_per_coupon` escolhas.
`core/digest.select_offers` (dedup + teto por ordem de chegada, sem julgamento nenhum) é só o
critério do template — usado com `use_llm: false` ou quando a API falha.

## Pontos de atenção

**Prod ≠ local.** A VPS recebe respostas diferentes das da sua máquina. Validar canal só
localmente não prova nada; rode `--no-llm --only promotions` lá. Quando algo falhar com código
estranho, **leia o corpo da resposta antes de teorizar**.

**Dois User-Agents, de propósito.** `USER_AGENT` (descritivo) para APIs; `BROWSER_HEADERS` para
alvos de scraping de HTML — e `t.me/s/<canal>` é um deles. As duas famílias de site querem
coisas opostas.

**Nome de canal tem apelido.** `t.me/s/promobit` responde 200 servindo o conteúdo de
`ofertasdecomputador`, e `t.me/s/promocoes` serve `nerdofertas`. Por isso o campo `channel` sai
do `data-post` da mensagem, não do config: atribuir a oferta ao apelido publicaria um crédito
que não bate com o link, e a mesma fonte entraria duas vezes na lista sem ninguém notar.

**O link publicado é o do post, nunca o da loja.** Boa parte das ofertas só vale com o cupom, e
o cupom está no post. `store_url` fica no payload como referência e não é passado nem ao
template nem ao modelo — dar a URL da loja ao LLM é convidá-lo a publicá-la.

**Quatro anti-repetições, e elas não são intercambiáveis.** Duas comparam chave exata, duas
comparam nome próprio; duas olham para a leva atual, duas para o histórico:

* `_interleave` (scraper) — chave exata dentro da leva: o post copiado literalmente entre canais.
* `filter_seen_offers` (histórico) — chave exata contra o publicado: o repost de amanhã.
* `filter_published_items` (histórico) — nomes próprios contra o **texto** dos envios recentes:
  o mesmo produto reescrito por outro canal.
* mesmo produto dentro da leva atual (o achado chegando por dois canais na mesma hora, que é o
  corriqueiro): com o LLM ligado é **regra do prompt**, não código — o modelo compara os dois e
  mantém o de melhor preço (visto na prática: dois posts de Pantene e dois de fralda Pampers,
  mesmo produto por canais diferentes, o modelo manteve um de cada). Sem LLM, quem resolve é
  `core/digest.dedupe_offers` por nome próprio (`core/utils.entities`/`same_item`), mantendo
  sempre a primeira ocorrência — não tem como comparar preço em código sem NLP, então nem tenta.

A comparação por nome próprio é por proporção, não exata, e tem duas travas contra falso
positivo: nomes presentes em *todos* os envios recentes são pano de fundo e são ignorados
("Amazon", "Mercado"), e pelo menos dois nomes precisam coincidir. Diferente do jornal, aqui
**não** existe a trava de "nunca esvaziar a seção": reenviar o achado de ontem é o pior
resultado possível, e sem oferta nenhuma o agente simplesmente não envia.

**Histórico grava por envio, não por dia.** São dois disparos diários: gravando um por dia, o
das 18h apagaria o das 8h e as ofertas da manhã voltariam à tona amanhã — exatamente o que o
histórico existe para impedir. Só grava após envio bem-sucedido.

**`noise_patterns` e `strip_patterns` fazem coisas diferentes.** O primeiro descarta a mensagem
inteira (sorteio, captação de seguidor); o segundo recorta um trecho — o rodapé fixo que o canal
cola em todo post ("Assine o Amazon Prime", "você me paga um café", "PEGAR OFERTA 👇"). Sem o
segundo, o rodapé ocupa metade do texto útil e empurra o nome do produto para fora do recorte de
`max_offer_chars`. Os dois são config, não código: canal novo traz rodapé novo.

**A URL crua no meio do texto atrapalha a deduplicação**, não só a leitura: a mesma oferta com
outro parâmetro de afiliado vira outra chave. `_clean_text` remove URL, hashtag, emoji repetido
e as pontas sem palavra ("➡️" na frente, "🛒 👇 |" no fim) antes de qualquer comparação.

**Título de seção não tem tamanho de fonte.** O Telegram só tem negrito, itálico, sublinhado,
tachado, código e link. O que faz um cabeçalho parecer cabeçalho é a convenção herdada do
jornal — régua `━━━━━━━━━━━━━━━`, título em CAIXA ALTA dentro de `<b>`, linha em branco antes do
conteúdo. Tag de heading é convertida em quebra de linha pelo sanitizador, então pedir `<h2>` ao
modelo não falha: só apaga o título.

**Número nenhum é calculado.** O preço é copiado do post, verbatim, e o prompt do LLM proíbe
recalcular, converter ou arredondar. No jornal que deu origem a isto, o modelo chegou a inventar
uma variação percentual que ninguém pediu.

**O texto vem de terceiros — escape sempre.** `<`, `>` e `&` crus derrubam a mensagem inteira no
Telegram. `core/digest._escape` escapa o conteúdo dinâmico antes de montar a linha, e
`sanitize_html` é a segunda rede.

**O LLM é opcional** (`formatting.use_llm`, ligado desde 12/09/2026). O import do `google-genai`
é tardio, então o agente roda sem o pacote instalado quando desligado. Quando ligado, o modelo
faz duas coisas: escolhe quais candidatos publicar (ver acima) e reescreve o texto da oferta.
Erros 4xx não são repetidos (`_is_permanent`).

**A validação da resposta do LLM mudou de "uma linha por oferta" para "todo link é rastreável".**
Antes o modelo recebia exatamente as ofertas a publicar, então `_looks_valid` conferia que a
contagem de bullets batia com a de entrada. Agora ele recebe o pool inteiro e descarta a
maioria de propósito — essa checagem não existe mais. O que ficou, e é o que importa: todo
`href` da resposta precisa bater com o `link` de algum candidato enviado (`_extract_links`) —
um link fora da lista é oferta inventada, o erro mais caro que existe aqui — e nenhum cupom
aparece em mais candidatos escolhidos que `max_per_coupon`. Qualquer uma das duas falhando cai
no template. `core/ai_engine._resolve_chosen` casa os links da resposta com os candidatos para
saber **quais** foram publicados de fato — é essa lista, não o pool, que vai para o histórico.

**Alerta é sobre resultado, não sobre fonte.** Um canal fora do ar que os outros cobrem vai só
para o log. Alerta acontece em três casos: todos os canais fora, zero ofertas depois dos filtros
num dia em que normalmente há, e **silêncio crônico** — canal que não entrega nada há vários
envios seguidos (`chronic_silence`). O terceiro existe porque, com muitos canais, um canal morto
não faz falta no resultado e por isso nunca dispararia os outros dois: no jornal, foi assim que
dois feeds ficaram 404 por três semanas.

**Emoji não vai para o log.** O console do Windows usa cp1252 e quebra no `StreamHandler`; por
isso `--dry-run` e `--no-llm` chamam `_use_utf8_stdout` e os alertas montam o emoji só na hora de
enviar.

**Config-driven é regra.** Canais, janelas, tetos, ruído e rodapés ficam em
[config/config.yaml](config/config.yaml). O processo de validar um canal novo está no README.

**Estado local em `logs/`:** `history.json` guarda 30 dias com poda automática.
