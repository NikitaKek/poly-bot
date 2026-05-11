````markdown
# Codex Handoff: Polymarket BTCUSDT 15m Paper Trading Bot

## 1. Контекст проекта

Мы строим paper-trading бота для Polymarket 15-minute BTCUSDT prediction markets.

Целевой рынок:

- BTCUSDT 15-minute markets
- Binary prediction market
- Outcomes: `YES` / `NO`
- Один токен в конце рынка выплачивает `1`, второй — `0`

Проект находится в репозитории:

```text
NikitaKek/poly-bot
````

Главная цель текущего этапа — не запуск реальной торговли, а построение безопасной инфраструктуры:

```text
market data
paper execution
order management
position tracking
risk management
logging
tests
strategy diagnostics
```

Важно:

```text
Торговля пока только paper-only.
Нельзя добавлять real order routing.
Нельзя добавлять private keys.
Нельзя отправлять реальные ордера.
```

---

## 2. Первоначальная идея стратегии

Изначальная идея была арбитражная:

```text
Если купить аутсайдера по 0.15,
а затем купить противоположный токен дешевле 0.85,
то можно получить guaranteed profit.
```

Пример:

```text
BUY NO  @ 0.15
BUY YES @ 0.83
```

Тогда:

```text
0.15 + 0.83 = 0.98
```

Теоретическая прибыль:

```text
1.00 - 0.98 = 0.02
```

Но важное условие:

```text
YES_price + NO_price + fees + slippage < 1
```

Если:

```text
0.15 + 0.85 = 1.00
```

то это не прибыль, а ноль до комиссий, спреда и проскальзывания.

---

## 3. Почему чистый арбитраж недостаточен

Чистый арбитраж вида:

```text
YES_ask + NO_ask < 1
```

видят все боты.

Проблемы:

```text
latency
очередь в стакане
частичные исполнения
adverse selection
резкие движения BTC
невозможность быстро захеджироваться
слишком оптимистичный paper trading
```

Поэтому стратегия была переосмыслена как inventory-based market making.

---

## 4. Предлагаемая целевая стратегия

Целевая стратегия — inventory-based market maker для 15m BTC рынков.

Идея:

```text
Бот не пытается постоянно угадывать направление BTC.
Он котирует YES и NO лимитными ордерами,
пытается зарабатывать на spread и краткосрочном mean reversion,
но строго контролирует inventory.
```

Базовый цикл:

```text
1. Найти активный BTC 15m market.
2. Получить реальные bid/ask YES и NO.
3. Рассчитать fair price.
4. Построить лимитные котировки.
5. Применить inventory skew.
6. Проверить risk limits.
7. Выставить paper orders.
8. Получить paper fills.
9. Обновить positions, PnL и equity.
10. Логировать snapshot → decision → order → fill → position.
```

---

## 5. Где потенциальный edge

Потенциальный edge не в одной формуле, а в комбинации:

### 5.1 Spread capture

Бот пытается покупать ближе к bid и продавать ближе к ask.

Пример:

```text
BUY YES  @ 0.42
SELL YES @ 0.45
profit = 0.03 * size
```

### 5.2 Mean reversion

15m BTC markets часто эмоционально реагируют на короткие импульсы BTC.

Пример:

```text
YES 0.55 → 0.78 → 0.66
```

Бот может зарабатывать, если не ловит движение слишком поздно и контролирует inventory.

### 5.3 Inventory skew

Если у бота слишком много YES:

```text
меньше покупать YES
активнее продавать YES
```

Если слишком много NO:

```text
меньше покупать NO
активнее продавать NO
```

Простая формула:

```text
imbalance = yes_position - no_position
skew = imbalance / max_inventory_imbalance
```

### 5.4 Avoiding toxic markets

Бот должен переставать торговать, если:

```text
BTC резко движется
рынок близко к expiry
orderbook пустой
spread слишком узкий
spread слишком широкий
market data stale
inventory imbalance слишком большой
```

### 5.5 Execution discipline

Edge также в дисциплине исполнения:

```text
correct accounting
no naked shorts
market-specific positions
market-specific orders
cancel stale orders
kill switch
structured logging
session summaries
```

---

## 6. Текущий статус проекта

В проекте уже есть:

```text
polymarket_bot/main.py
polymarket_bot/config.py
polymarket_bot/models.py
polymarket_bot/market_data.py
polymarket_bot/polymarket_market_data.py
polymarket_bot/order_manager.py
polymarket_bot/paper_exchange.py
polymarket_bot/position_manager.py
polymarket_bot/risk_manager.py
polymarket_bot/logger.py
tests/
```

Уже реализовано:

```text
mock market data
public Polymarket market data
Gamma market discovery
CLOB orderbook reading
paper limit order simulation
position manager
risk manager
basic tests
```

Текущий режим:

```text
real Polymarket data
paper-only execution
no real order routing
```

---

## 7. Что было исправлено ранее

Раньше в логах была проблема:

```text
yes=-55
no=-5
realized_pnl=1474.93
```

Это означало, что paper bot разрешал naked short.

Было принято решение:

```text
SELL YES запрещён, если YES position = 0
SELL NO запрещён, если NO position = 0
position не должен уходить ниже 0
realized_pnl считается только при продаже уже купленного inventory
```

Правильная формула realized PnL:

```text
realized_pnl += (sell_price - avg_cost[token]) * filled_size
```

Главная метрика:

```text
equity = cash + yes_position * yes_mark + no_position * no_mark
```

Не `cash`, а именно `equity`.

---

## 8. Анализ последних логов

Был загружен лог:

```text
bot.log.3
```

По нему было найдено:

```text
~32 335 строк
~1 960 position snapshots
~641 paper fills
~7 разных 15m BTC markets
```

Общий результат по видимой части сессии:

```text
equity: 1025.53 → 969.59
delta: -55.94
```

Минимальная equity:

```text
947.06
```

Максимальная просадка:

```text
~78.47
```

Вывод:

```text
Текущая стратегия в таком виде убыточна.
```

Но главная проблема не только в стратегии, а в архитектуре market lifecycle и accounting для rolling 15m markets.

---

## 9. Критические проблемы, найденные в логах

### 9.1 Глобальные YES/NO позиции

Сейчас позиции хранятся как:

```text
yes_position
no_position
yes_avg_cost
no_avg_cost
```

Но каждый 15m market имеет отдельные token IDs.

Проблема:

```text
YES одного 15m market ≠ YES другого 15m market
NO одного 15m market ≠ NO другого 15m market
```

Нужен market-specific ledger.

Правильно:

```text
positions_by_condition_id[condition_id]
```

Каждый market должен иметь отдельные:

```text
market_slug
condition_id
yes_token_id
no_token_id
yes_position
no_position
yes_avg_cost
no_avg_cost
realized_pnl
unrealized_pnl
equity
```

---

### 9.2 Старые ордера могут исполняться на новом market

В логах видно, что перед сменой рынка оставались open orders, а после market rollover они могли матчиться уже на новом snapshot.

Проблема:

```text
Order знает только Token.YES / Token.NO,
но не знает condition_id / token_id.
```

Нужно добавить в Order:

```text
condition_id
market_slug
token_id
outcome YES/NO
```

PaperExchange должен матчить order только если:

```text
order.condition_id == snapshot.condition_id
order.token_id == current snapshot token_id
```

---

### 9.3 Нет settlement logic

В конце 15m рынка один токен должен стать `1`, другой `0`.

Сейчас нет полноценной логики:

```text
settle winning token
write losing token to zero
realize final PnL
archive market ledger
reset current market
```

Для начала можно сделать forced exit:

```text
за 90 секунд до expiry — не открывать новые позиции
за 60 секунд до expiry — cancel all open orders
по возможности закрыть inventory
```

---

### 9.4 Торговля слишком близко к expiry

В логах бот торгует почти в последние секунды рынка.

Это токсично:

```text
одна сторона летит к 1
другая летит к 0
orderbook становится односторонним
fills становятся adverse
```

Нужно правило:

```text
do not open new positions when time_to_expiry < 90 seconds
cancel all orders when time_to_expiry < 60 seconds
```

---

### 9.5 Слабый inventory skew

RiskManager часто отклоняет ордера из-за inventory imbalance.

Это значит, что стратегия сама не адаптируется к inventory, а просто упирается в risk manager.

RiskManager должен быть аварийным тормозом, а не основным механизмом стратегии.

Нужно, чтобы QuoteEngine заранее делал:

```text
if imbalance слишком отрицательный:
    disable BUY NO
    prefer SELL NO
    allow BUY YES
    avoid SELL YES

if imbalance слишком положительный:
    disable BUY YES
    prefer SELL YES
    allow BUY NO
    avoid SELL NO
```

---

### 9.6 Текущая стратегия ловит adverse selection

Текущая стратегия примерно:

```text
BUY YES = best_bid - offset
BUY NO = best_bid - offset
SELL YES = best_ask + offset
SELL NO = best_ask + offset
```

Проблема:

```text
если цена резко падает, BUY исполняется, и бот остаётся с плохим inventory
если цена резко растёт, SELL исполняется, и бот продаёт слишком рано
```

То есть бот часто получает fills именно тогда, когда рынок двигается против него.

---

## 10. Почему пока нельзя честно оценивать стратегию

Текущий PnL смешивает:

```text
разные 15m markets
старые orders
новые snapshots
глобальные YES/NO positions
отсутствие settlement
near-expiry trading
adverse fills
```

Поэтому сначала нужно исправить lifecycle и observability, а уже потом оценивать edge.

---

## 11. Следующий приоритет: улучшить логирование

Перед дальнейшей оптимизацией стратегии нужно улучшить observability.

Цель логирования:

```text
session → market → snapshot → strategy_decision → order → fill → position → summary
```

Сейчас все логи слишком плоские и смешаны в одном файле.

Нужно добавить:

```text
session-level logging
market-level logging
structured JSONL logs
CSV exports
session summary
```

---

## 12. Предлагаемая структура логов

```text
logs/
  sessions/
    <session_id>/
      bot.log
      events.jsonl
      summary.json
      config.json

      markets/
        btc-updown-15m-1778413500.log
        btc-updown-15m-1778414400.log

      csv/
        snapshots.csv
        orders.csv
        fills.csv
        positions.csv
        risk_rejections.csv
```

---

## 13. session_id

Каждый запуск должен иметь уникальный session_id.

Формат:

```text
YYYYMMDD_HHMMSS_<market_data_mode>_paper
```

Пример:

```text
20260510_145101_polymarket_paper
```

Все события должны содержать:

```text
session_id
mode=paper
market_data_mode
strategy_version
config_hash, если возможно
```

---

## 14. Structured JSONL events

Нужно добавить файл:

```text
events.jsonl
```

Одна строка = один JSON object.

Базовые поля каждого event:

```text
ts
session_id
event
level
iteration
market_slug
condition_id
yes_token_id
no_token_id
```

Event types:

```text
session_started
session_finished
market_selected
market_rollover
snapshot
strategy_decision
order_placed
order_cancelled
fill
position
risk_rejection
market_data_unavailable
forced_exit
error
```

---

## 15. Что логировать

### 15.1 Market selected

```text
question
market_slug
condition_id
yes_token_id
no_token_id
expiry_time
market_data_mode
```

### 15.2 Snapshot

```text
yes_bid
yes_ask
yes_bid_size
yes_ask_size
no_bid
no_ask
no_bid_size
no_ask_size
yes_mid
no_mid
yes_ask_plus_no_ask
yes_bid_plus_no_bid
time_to_expiry
```

### 15.3 Strategy decision

```text
fair_yes
fair_no
base_spread
quote_offset
inventory_skew
proposed_quotes
skipped_quotes
skip_reasons
```

Пример:

```text
skip BUY NO because inventory_imbalance is too negative
```

### 15.4 Orders

При order_placed:

```text
order_id
market_slug
condition_id
token_id
outcome
side
price
size
reason
```

При order_cancelled:

```text
order_id
market_slug
condition_id
outcome
side
price
remaining_size
cancel_reason
```

### 15.5 Fills

```text
fill_id или order_id
market_slug
condition_id
token_id
outcome
side
price
size
notional
time_to_expiry
```

### 15.6 Positions

```text
market_slug
condition_id
cash
equity
realized_pnl
unrealized_pnl
yes_position
no_position
yes_avg_cost
no_avg_cost
inventory_imbalance
```

Если будет market-specific ledger:

```text
current_market_equity
total_equity
```

### 15.7 Risk rejections

```text
reason
token/outcome
side
price
size
current_position
current_cash
inventory_imbalance
max_inventory_imbalance
max_position_per_token
```

---

## 16. Summary JSON

В конце сессии создать:

```text
summary.json
```

Поля:

```text
session_id
started_at
ended_at
duration_seconds
market_data_mode
starting_cash
ending_cash
ending_equity
realized_pnl
unrealized_pnl
max_equity
min_equity
max_drawdown
total_orders
total_fills
total_risk_rejections
markets_seen
markets_traded
final_positions
```

---

## 17. Текущий рекомендуемый Codex task

Сначала не менять стратегию.

Сначала улучшить observability/logging.

После этого будет проще понять:

```text
стратегия реально убыточна
или проблема в market lifecycle
или проблема в execution simulation
или проблема в accounting
или проблема в near-expiry behavior
```

---

# Codex Prompt: Improve Logging / Observability

```text
Улучши систему логирования в текущем Polymarket paper trading bot.

Проблема:
Сейчас все логи пишутся в один общий файл, из-за чего сложно отделять разные запуски, разные 15m рынки, ордера, fills, позиции и risk rejections. Нужно сделать логирование удобным для анализа стратегии.

Цель:
Добавить session-level, market-level и structured JSONL logging, не меняя торговую логику.

Требования:

1. Session ID

При каждом запуске бота генерировать уникальный session_id.

Формат:
YYYYMMDD_HHMMSS_<market_data_mode>_paper

Например:
20260510_145101_polymarket_paper

Все текстовые и JSONL логи должны содержать session_id.

2. Структура папок логов

Для каждого запуска создавать отдельную папку:

logs/sessions/<session_id>/

Внутри:

logs/sessions/<session_id>/bot.log
logs/sessions/<session_id>/events.jsonl
logs/sessions/<session_id>/summary.json
logs/sessions/<session_id>/config.json
logs/sessions/<session_id>/markets/
logs/sessions/<session_id>/csv/

3. Human-readable bot.log

Оставить обычный читаемый лог, но добавить в каждую строку контекст:

- session_id
- event
- iteration
- market_slug, если известен
- condition_id, если известен

Пример:

2026-05-10 15:01:02 | INFO | session=20260510_145101_polymarket_paper | event=snapshot | market=btc-updown-15m-1778414400 | YES 0.470/0.490 | NO 0.520/0.540

4. JSONL event logger

Добавить отдельный модуль или класс StructuredEventLogger.

Он должен писать events.jsonl, где каждая строка — валидный JSON object.

Обязательные поля каждого event:

- ts
- session_id
- event
- level
- iteration, если есть
- market_slug, если есть
- condition_id, если есть

5. Типы событий

Добавить structured events для:

- session_started
- session_finished
- market_selected
- market_rollover
- snapshot
- strategy_decision
- order_placed
- order_cancelled
- fill
- position
- risk_rejection
- market_data_unavailable
- forced_exit
- error

6. Market selected event

При выборе рынка логировать:

- question
- market_slug
- condition_id
- yes_token_id
- no_token_id
- expiry_time, если доступно
- market_data_mode

7. Snapshot event

На каждый snapshot логировать:

- yes_bid
- yes_ask
- yes_bid_size
- yes_ask_size
- no_bid
- no_ask
- no_bid_size
- no_ask_size
- yes_mid
- no_mid
- yes_ask_plus_no_ask
- yes_bid_plus_no_bid
- time_to_expiry, если доступно

8. Order events

При order_placed логировать:

- order_id
- market_slug
- condition_id
- token_id
- outcome YES/NO
- side BUY/SELL
- price
- size
- reason, если есть

При order_cancelled логировать:

- order_id
- market_slug
- condition_id
- outcome
- side
- price
- remaining_size
- cancel_reason

9. Fill events

При fill логировать:

- fill_id или order_id
- market_slug
- condition_id
- token_id
- outcome
- side
- price
- size
- notional
- time_to_expiry, если есть

10. Position events

После применения fills и update marks логировать:

- market_slug
- condition_id
- cash
- equity
- realized_pnl
- unrealized_pnl
- yes_position
- no_position
- yes_avg_cost
- no_avg_cost
- inventory_imbalance

Если будет реализован market-specific ledger, логировать и current_market_equity, и total_equity.

11. Risk rejection events

В RiskManager при reject логировать structured event:

- reason
- token/outcome
- side
- price
- size
- current_position
- current_cash
- inventory_imbalance
- max_inventory_imbalance
- max_position_per_token

12. Strategy decision events

Перед выставлением ордеров логировать:

- fair_yes
- fair_no
- base_spread
- quote_offset
- inventory_skew
- proposed_quotes
- skipped_quotes
- skip_reasons

Например:
skip BUY NO because inventory_imbalance is too negative.

13. CSV exports

Дополнительно писать CSV-файлы:

logs/sessions/<session_id>/csv/snapshots.csv
logs/sessions/<session_id>/csv/orders.csv
logs/sessions/<session_id>/csv/fills.csv
logs/sessions/<session_id>/csv/positions.csv
logs/sessions/<session_id>/csv/risk_rejections.csv

CSV можно писать append-режимом через стандартный csv module.

14. Session summary

В конце запуска создать summary.json:

- session_id
- started_at
- ended_at
- duration_seconds
- market_data_mode
- starting_cash
- ending_cash
- ending_equity
- realized_pnl
- unrealized_pnl
- max_equity
- min_equity
- max_drawdown
- total_orders
- total_fills
- total_risk_rejections
- markets_seen
- markets_traded
- final_positions

15. Не ломать существующий logger

Сохрани совместимость с текущими вызовами logger.info/warning/error.
Новый structured logger должен быть дополнительным, а не заменять полностью обычный лог.

16. Tests

Добавь unit tests:

- session_id создаётся в правильном формате
- создаётся структура папок logs/sessions/<session_id>/
- events.jsonl содержит валидные JSON строки
- snapshot event содержит YES/NO bid/ask
- fill event содержит order_id, side, price, size, notional
- summary.json создаётся в конце сессии
- CSV файлы создаются и содержат header

17. README

Обнови README:
- объясни структуру логов
- покажи пример bot.log
- покажи пример events.jsonl
- покажи как анализировать fills через Python
- покажи где искать summary.json

Важно:
- не добавлять real order routing
- не добавлять private keys
- не менять торговую стратегию в этом PR
- задача только про observability/logging
```

---

## 18. Следующие задачи после logging

После улучшения логирования следующие приоритеты:

```text
1. Market-specific accounting.
2. Market-specific orders.
3. Cancel all orders on market rollover.
4. Forced exit или settlement.
5. Stop trading near expiry.
6. Proper inventory skew.
7. Toxicity filters.
8. Fair price engine через midpoint/microprice.
9. Более реалистичный paper execution.
10. Анализ стратегии через events.jsonl и CSV.
```

---

## 19. Короткий вывод

Текущая версия проекта уже полезна как инфраструктурный MVP, но текущая стратегия показывает убыточность на реальных Polymarket data.

Однако вывод не должен быть:

```text
market making не работает
```

Правильный вывод:

```text
текущая реализация ещё не является корректным market-making bot для rolling 15m markets
```

Сначала нужно улучшить:

```text
observability
market lifecycle
market-specific accounting
expiry handling
inventory skew
```

Только после этого можно честно оценивать наличие edge.

```
```
