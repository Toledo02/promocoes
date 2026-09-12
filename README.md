# Achados & Promoções — agente de curadoria de ofertas

Agente Python config-driven que lê canais públicos de promoção do Telegram, filtra o ruído,
deduplica, limita repetição por campanha e por produto, e envia um resumo enxuto pelo Telegram
1–2 vezes ao dia. O link publicado é sempre o do **post no canal de origem** — é lá que está o
cupom.

Nasceu da seção `🛒 ACHADOS & PROMOÇÕES` do [Jornal Matinal](PROMOCOES_PROJETO.md), que virou
projeto próprio para poder varrer muitos canais em vez dos três que cabiam num jornal
generalista. A especificação original está em [PROMOCOES_PROJETO.md](PROMOCOES_PROJETO.md).

**Não é** comparador de preço, não é bot de afiliados (nenhum link é reescrito) e não é serviço
24/7: roda como job único via cron, sem webhook e sem processo vivo.

## Estrutura

```
Promocoes/
├── config/
│   ├── .env              # Segredos (não versionado)
│   ├── .env.example      # Template de variáveis
│   ├── config.yaml       # Canais, filtros, janelas
│   └── settings.py       # Carregador de config
├── scrapers/
│   └── promotions.py     # Leitura dos canais (t.me/s/<canal>)
├── core/
│   ├── digest.py         # Corte final + template da mensagem
│   ├── history.py        # Histórico de 30 dias + anti-repetição
│   ├── ai_engine.py      # LLM opcional (só reescreve o texto)
│   ├── telegram_sender.py
│   └── utils.py
├── tests/                # Testes das funções puras, sem rede
└── main.py               # Orquestrador
```

## Instalação

```bash
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -r requirements.txt
cp config/.env.example config/.env
```

| Variável | Descrição |
|----------|-----------|
| `TELEGRAM_BOT_TOKEN` | Token do BotFather |
| `TELEGRAM_CHAT_ID` | Canal/chat de destino |
| `LLM_API_KEY` | **Opcional.** Só usada com `formatting.use_llm: true` |

Sem chave de API o agente funciona igual: a mensagem é montada pelo template em Python, que é o
caminho padrão.

## Execução

```bash
python main.py                            # coleta, monta e envia
python main.py --dry-run                  # monta e imprime, sem enviar
python main.py --no-llm                   # só coleta e imprime o payload cru
python main.py --no-llm --only promotions # o mesmo, restrito a uma fonte
```

Testes (sem rede):

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Logs diários em `logs/promocoes_YYYYMMDD.log` (data no fuso de São Paulo).

## Como a mensagem é montada

Cada oferta ocupa até três linhas, e tudo que é número vem copiado do post:

```
• Echo Dot 5ª geração — por R$ 229 (menor preço já visto)
  cupom ECHO20
  [Ver no canal] @promobit
```

O nome do produto vai em negrito, o cupom em negrito, e o link aponta para o post no canal.

Com `formatting.use_llm` ligado (o padrão), o modelo faz mais que reescrever: ele recebe **todos**
os candidatos do dia — não só os 8 que vão ao ar — e escolhe quais publicar, comparando desconto,
preço final e o quanto o post identifica um produto real (vs. banner de loja tipo "tudo abaixo de
R$ X"). Quando dois candidatos são o mesmo produto vindo de canais diferentes, ele mantém o de
melhor preço e descarta o outro — na prática isso já pegou dois posts idênticos de um mesmo
produto (ex.: a mesma fralda anunciada em dois canais) e manteve só um. Preço, cupom e link
continuam vindo do post, verbatim: o modelo nunca calcula, só compara. Se a API falhar, ou a
resposta citar um link que não veio nos candidatos (oferta inventada), a mensagem sai pelo
template — que aí sim seleciona por ordem de chegada, sem julgamento nenhum.

## Configuração (`config/config.yaml`)

**Regra de ouro:** canais, janelas, ruído e rodapés ficam no YAML — não no código.

```yaml
promotions:
  telegram_channels: ["ofertasdecomputador", "promotop", ...]
  max_age_hours: 24
  per_channel: 8            # teto por canal antes do round-robin
  candidate_pool: 30        # candidatos coletados
  max_items: 8              # ofertas publicadas
  max_per_coupon: 2         # teto por campanha
  noise_patterns: [...]     # descartam a mensagem inteira
  strip_patterns: [...]     # recortam o rodapé fixo do canal
```

Os canais são intercalados em **round-robin** antes do corte: concatenar e truncar faria o
primeiro canal ocupar todos os slots. Canal muito prolífico precisa de `per_channel` baixo.

## As quatro anti-repetições

Elas não são intercambiáveis — cada uma pega um caso que as outras não pegam:

| Onde | O que compara | Pega |
|---|---|---|
| `scrapers/promotions._interleave` | chave exata do texto, dentro da leva | o mesmo post copiado literalmente entre canais |
| `core/history.filter_seen_offers` | chave exata contra o que já foi enviado | o repost do mesmo achado amanhã |
| `core/history.filter_published_items` | nomes próprios contra o texto dos envios recentes | o mesmo produto reescrito por outro canal |
| mesmo produto na leva atual | o LLM compara os candidatos (regra do prompt); sem LLM, `core/digest.dedupe_offers` por nome próprio | o mesmo produto chegando por dois canais agora |

A comparação por nome próprio é por proporção, não exata ("Echo Dot 5ª geração" × "Echo Dot 5"),
e tem duas travas contra falso positivo: nomes presentes em *todos* os envios recentes são pano
de fundo e são ignorados ("Amazon"), e pelo menos dois nomes precisam coincidir de fato. Sem LLM
(ou se a API falhar), a última linha vira mecânica: mantém sempre a primeira ocorrência, porque
comparar preço em Python sem NLP não dá para fazer direito — só o modelo faz isso.

## Resiliência

| Cenário | Comportamento |
|---------|---------------|
| Um canal fora do ar | Os outros seguem; vai só para o log (`status=partial`) |
| **Todos** os canais fora | `status=error` e alerta no Telegram |
| Zero ofertas depois dos filtros | Não envia; alerta só se nos últimos dias sempre houve |
| Canal sem entregar nada há vários envios | Alerta de silêncio crônico (`chronic_silence`) |
| LLM ligado retorna 503 | Backoff 10s → 30s → 90s, modelos de reserva, depois o template |
| Telegram rejeita o HTML | Reenvia sem marcação |
| Mensagem acima de 4096 chars | Dividida sem cortar tags no meio |

Alerta é sobre **resultado**, não sobre fonte: um canal que falha mas é coberto pelos outros
fica no log. Alertar todo dia sobre algo que não muda o resultado treina você a ignorar os
alertas — e foi por isso que se acrescentou o alerta de silêncio crônico, que é o caso em que a
falha some justamente por não atrapalhar hoje.

## Histórico

`logs/history.json` guarda 30 dias (`history.retention_days`), podados a cada gravação, e é
escrito **só após envio bem-sucedido** — uma mensagem que não chegou não pode suprimir a oferta
de amanhã. Cada envio grava o texto, as chaves das ofertas publicadas e os canais que ficaram em
silêncio. Dois envios no mesmo dia são dois registros: gravando um por dia, o das 18h apagaria o
das 8h.

## Como achar e validar um canal novo

O valor do projeto está em ter **muitos** canais bons.

1. **Achar:** buscar no Telegram por "promoção", "ofertas", "desconto", "achados"; nichos
   (hardware, livros, games); indicações nos próprios posts.
2. **Confirmar a prévia:** abrir `https://t.me/s/<canal>` no navegador. Se carregar as
   mensagens, serve. Se redirecionar para `t.me/<canal>` sem conteúdo, o canal é privado ou
   desativou a prévia — descartar.
3. **Pegar o nome canônico:** o handle pode ser apelido. `t.me/s/promobit` responde 200 servindo
   `ofertasdecomputador`, e `t.me/s/promocoes` serve `nerdofertas`. Use o nome que aparece no
   `data-post` (o scraper credita por ele), senão o mesmo canal entra duas vezes na lista.
4. **Validar o parsing:** `python main.py --no-llm --only promotions` com o canal na lista e
   conferir `text`, `link`, `published` e `coupon`. Canal que posta por imagem rende pouco — o
   filtro de comprimento mínimo já o esvazia.
5. **Cadenciar:** canal prolífico precisa de `per_channel` baixo para não afogar os outros.
6. **Aparar o rodapé:** se o canal cola um bloco fixo em todo post ("Assine o Prime", "me paga
   um café"), acrescente um `strip_pattern`.
7. **Validar na VPS**, não só localmente.

Manter a lista **curada**: canal que só repassa afiliado sem cupom, ou que posta muito sorteio,
entra no `noise_patterns` ou sai da lista.

## Deploy na VPS (cron)

```bash
crontab -e
```

```
0 8,18 * * * cd /home/ubuntu/promocoes && /home/ubuntu/promocoes/.venv/bin/python main.py >> /home/ubuntu/promocoes/logs/cron.log 2>&1
```

Manhã e fim de tarde, que é quando as campanhas saem. Certifique-se de que `config/.env` existe
na VPS.

> **Atenção:** a VPS recebe respostas diferentes das da sua máquina. Valide canal novo **lá**,
> com `python main.py --no-llm --only promotions`. Quando algo falhar com código estranho, leia
> o corpo da resposta antes de teorizar.
