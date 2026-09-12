# Jornal Matinal — Agente de Clipping Pessoal

Agente Python config-driven que coleta dados de múltiplas fontes, consolida via LLM (Google Gemini)
e envia um jornal matinal personalizado pelo Telegram.

## Estrutura

```
Jornal/
├── config/
│   ├── .env              # Segredos (não versionado)
│   ├── .env.example      # Template de variáveis
│   ├── config.yaml       # Fontes, URLs, times, produtos
│   └── settings.py       # Carregador de config
├── scrapers/             # Coletores de dados
├── core/                 # IA e Telegram
├── tests/                # Testes das funções puras
├── main.py               # Orquestrador
└── PLANO.md              # Backlog de melhorias com diagnóstico
```

## Requisitos

- Python 3.10+
- VPS Linux (ex.: Oracle Cloud Infrastructure) ou máquina local

## Instalação

```bash
cd /path/to/Jornal
python -m venv .venv
source .venv/bin/activate   # Linux/macOS
# .venv\Scripts\activate    # Windows

pip install -r requirements.txt
cp config/.env.example config/.env
```

Edite `config/.env`:

| Variável | Descrição |
|----------|-----------|
| `LLM_API_KEY` | Chave do Gemini ([AI Studio](https://aistudio.google.com/apikey)) |
| `LLM_MODEL` | Modelo (padrão: `gemini-3.6-flash`; os `pro` dão 429 no plano gratuito) |
| `LLM_FALLBACK_MODELS` | Modelos tentados se o principal der 503 |
| `TELEGRAM_BOT_TOKEN` | Token do BotFather |
| `TELEGRAM_CHAT_ID` | ID do chat de destino |
| `FOOTBALL_DATA_TOKEN` | Jogos e placares ([football-data.org](https://www.football-data.org/client/register), grátis) |
| `AWESOMEAPI_TOKEN` | Opcional; a AwesomeAPI é a última fonte da cadeia de cotações |

Os nomes `OPENAI_*` continuam aceitos por compatibilidade (herança de quando o projeto usava
OpenAI), mas os `LLM_*` têm precedência.

## Execução

```bash
python main.py                      # pipeline completo: coleta, gera e envia
python main.py --dry-run            # gera e imprime no terminal, sem enviar
python main.py --no-llm             # só coleta e imprime o payload (não gasta requisição)
python main.py --only weather,finance   # roda um subconjunto de scrapers
```

Testes:

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

Logs diários em `logs/journal_YYYYMMDD.log` (data no fuso de São Paulo).

## Configuração dinâmica (`config/config.yaml`)

**Regra de ouro:** URLs, feeds, seletores, times e produtos ficam no YAML — não no código.

```yaml
rss_feeds:
  tech:
    - "https://techcrunch.com/feed/"
    - "https://seu-novo-feed.com/rss"   # basta adicionar aqui

weather:
  city: "Curitiba"
  lat: -25.4284
  lon: -49.2733
```

Os feeds são intercalados em round-robin antes do corte por `max_items_per_category`, então cada
fonte contribui proporcionalmente. Entradas mais velhas que `max_age_hours` são descartadas.

## Resiliência

| Cenário | Comportamento |
|---------|---------------|
| Um scraper falha | Jornal enviado com as demais seções, **e um alerta no Telegram** |
| Uma fonte de cotação falha | Os ativos faltantes são buscados na fonte seguinte, um a um |
| Menos de `min_sections_for_send` seções | Pipeline aborta e avisa pelo Telegram |
| LLM retorna 503 | Backoff 10s → 30s → 90s, depois tenta os modelos de reserva |
| LLM falha de vez | Fallback em texto puro |
| Telegram rejeita o HTML | Reenvia sem marcação, com as tags removidas |
| Mensagem acima de 4096 chars | Dividida sem cortar tags no meio |

Falhas parciais geram alerta **quando faltam dados no jornal final**. Uma fonte que falha mas é
coberta pelo fallback fica só no log: alertar todo dia sobre algo que não afeta o resultado
treinaria você a ignorar os alertas.

## Histórico

`logs/history.json` guarda 30 dias (`history.retention_days`), podados a cada gravação. Serve a
três coisas: os 3 jornais mais recentes vão ao prompt para a regra anti-repetição, as métricas
numéricas alimentam o contexto comparativo — `R$ 5,60 (+0,10%) — maior valor em 30 dias` — e os
títulos já publicados alimentam o rodízio de jogos (`history.repeat_window_days`), que é o que
impede a mesma oferta de voltar todo dia.

## Módulos de dados

1. **Clima** — Open-Meteo, entregue por tópico (agora, hoje, máx/mín, chuva, sol, vento, UV), com
   emoji de condição montado em Python
2. **Economia** — HG Brasil → yfinance → AwesomeAPI, preenchendo ativo a ativo
3. **Investimentos** — séries do Banco Central (SGS): Selic, CDI, IPCA e poupança, com juro real
   calculado em Python e uma "ideia do dia" rotacionada pelo histórico
4. **Tech** — RSS
5. **Mundo** — RSS (LLM seleciona 3 fatos globais)
6. **Curitiba & Paraná** — RSS local (Gazeta do Povo PR, Tribuna PR), até 3 fatos
7. **Cultura Pop** — RSS
8. **GitHub Trending** — scraping
9. **Gaming** — giveaways da GamerPower + ofertas do CheapShark, filtradas por nota, volume de
   avaliações e faixa de preço, com rodízio entre dias
10. **Futebol** — jogos via football-data.org + notícias filtradas do GE

## Deploy na VPS (cron)

```bash
crontab -e
```

```
55 5 * * * cd /home/ubuntu/jornal && /home/ubuntu/jornal/.venv/bin/python main.py >> /home/ubuntu/jornal/logs/cron.log 2>&1
```

Certifique-se de que `config/.env` existe na VPS.

> **Atenção:** a VPS recebe respostas diferentes das da sua máquina. O CheapShark exige
> User-Agent descritivo e a AwesomeAPI aplica cota por IP. Valide scrapers **na VPS**, não só
> localmente — use `python main.py --no-llm --only gaming`.
