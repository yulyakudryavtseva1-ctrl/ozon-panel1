# Схема базы данных (PostgreSQL) — Этап 1

Черновая схема для Этапа 1 из `docs/architecture.md`: мультитенантный фундамент
(Organization → Users → Clients → Marketplace Accounts) заложен сразу, но
выполняются на этом этапе только чтение данных и AI-рекомендации — таблицы для
Action Engine (`actions`) присутствуют в схеме с самого начала (чтобы не менять
структуру БД на Этапе 2), но код, который их реально исполняет, появится позже.

Все таблицы, которые содержат данные конкретного клиента, обязательно имеют
`client_id` (прямо или через связанные таблицы) — это основа для изоляции данных
между клиентами (§62 ТЗ: «Client A не может получить Client B»); на уровне
приложения каждый запрос к БД обязан фильтроваться по `client_id` из текущей
сессии пользователя, а не полагаться только на то, что фронтенд его не запросит.

## Тенантность и доступ

### organizations
Верхний уровень — агентство/аккаунт Юли.
- `id`, `name`, `created_at`

### users
Люди, у которых есть вход в систему.
- `id`, `organization_id`, `email`, `password_hash`, `name`,
  `role` (`owner` | `admin` | `manager` | `analyst` | `viewer`), `created_at`

Права на Этапе 1 — простые, по роли целиком (owner/admin — всё, manager — свои
клиенты без управления пользователями, analyst — только просмотр и рекомендации,
viewer — только просмотр). Отдельная таблица с гранулярными правами
(`clients.read`, `finance.write` и т.п., как в исходном ТЗ) — сознательно не
делается на Этапе 1: это усложнение имеет смысл, когда появится второй-третий
администратор с разными зонами ответственности, не раньше.

### clients
Клиент агентства — то, кем управляет Юля (включая её саму как первого «клиента»).
- `id`, `organization_id`, `name`, `created_at`

### marketplace_accounts
Кабинет на конкретной площадке, принадлежащий клиенту.
- `id`, `client_id`, `marketplace` (`ozon` | `wb`), `display_name`,
  `credentials_encrypted` (зашифровано на уровне приложения, ключ шифрования —
  переменная окружения на сервере, не в базе и не в git), `status`
  (`ok` | `cached` | `not_configured` | `unavailable` — тот же принцип
  независимости кабинетов, что и в текущем статическом сайте), `created_at`

Важно: ключи маркетплейсов клиента вводит сам клиент (или Юля от его имени) через
форму в самом приложении, когда она появится — это не то же самое, что нынешние
GitHub Secrets, и я по-прежнему не должна вводить эти значения сама.

## Каталог и продажи

### products
- `id`, `marketplace_account_id`, `external_id` (offer_id для Ozon / nmId для WB),
  `name`, `category`, `created_at`, `updated_at`

### skus
- `id`, `product_id`, `sku_code`, `created_at`

### sku_costs
История себестоимости — замена ручного `costs.json`, но с историей вместо одного
плоского значения (важно: цена закупки меняется со временем, а прибыль за прошлые
периоды должна считаться по себестоимости, которая действовала тогда).
- `id`, `sku_id`, `cost_amount`, `valid_from`, `valid_to` (null = действует по
  сейчас)

### orders / order_items
- `orders`: `id`, `marketplace_account_id`, `external_order_id`, `order_date`,
  `status`, `total_amount`, `created_at`
- `order_items`: `id`, `order_id`, `sku_id`, `quantity`, `price`

### returns
- `id`, `marketplace_account_id`, `sku_id`, `return_date`, `quantity`, `reason`

## Расходы площадки

### marketplace_commissions
- `id`, `marketplace_account_id`, `period_date`, `amount`

### logistics_costs
- `id`, `marketplace_account_id`, `period_date`, `amount`

### storage_costs
- `id`, `marketplace_account_id`, `period_date`, `amount`

(Три отдельные таблицы вместо одной общей `expenses` — потому что у каждой свой
источник и своя логика синхронизации; на уровне Profit Engine они всё равно
суммируются в одну статью «расходы площадки», как сейчас на сайте.)

## Реклама

### advertising_campaigns
- `id`, `marketplace_account_id`, `external_campaign_id`, `name`, `status`

### advertising_daily_stats
- `id`, `campaign_id`, `date`, `spend`, `clicks`, `impressions`, `orders_count`

## Остатки и цены

### inventory_snapshots
- `id`, `sku_id`, `snapshot_date`, `quantity_available`, `warehouse`

### sku_prices
История цен — нужна и для профита за прошлые периоды, и для будущего Action
Engine (сравнить цену до/после изменения).
- `id`, `sku_id`, `price`, `valid_from`

## Синхронизация

### sync_jobs
- `id`, `marketplace_account_id`, `dataset` (orders/returns/finance/ads/stocks/...),
  `status`, `started_at`, `finished_at`, `records_processed`, `error`

Дизайн синхронизации обязан учитывать уже подтверждённые вживую лимиты API:
Ozon `/v1/analytics/data` — не больше ~2 запросов/сек, Wildberries Statistics
API — ~1 запрос/мин на метод. Это должно быть заложено в код планировщика
(интервалы между запросами, а не просто generic retry), а не открыто заново.

## Profit Engine

### profit_daily
Единственный источник финансовой правды для дашборда — агрегат по клиенту за
день, а не то, что дашборд сам пересчитывает на лету из сырых заказов при каждом
открытии страницы.
- `id`, `client_id`, `date`, `revenue`, `discounts`, `commission`, `logistics`,
  `advertising`, `returns_amount`, `storage`, `cost_of_goods`, `other_expenses`,
  `net_profit`

Формула: `net_profit = revenue − discounts − commission − logistics −
advertising − returns_amount − storage − cost_of_goods − other_expenses`.

Если для проданного SKU нет записи в `sku_costs` на нужную дату — товар не
участвует в `cost_of_goods`, а `profit_daily` для этого дня помечается как
неполный (отдельное поле `is_partial boolean`) вместо того, чтобы тихо занижать
себестоимость нулём — тот же принцип «честность вместо угадывания», что и в
текущем сайте.

## AI, задачи, действия — схема готова, исполнение появится позже

### ai_recommendations
- `id`, `client_id`, `sku_id` (nullable), `agent_type`, `severity`, `title`,
  `description`, `reason`, `expected_impact`, `status`, `created_at`

### tasks
- `id`, `client_id`, `sku_id` (nullable), `created_by`, `assigned_to`, `source`
  (`manual` | `ai` | `system`), `title`, `description`, `priority`, `status`
  (`todo` | `in_progress` | `completed` | `rejected` | `cancelled`), `due_date`,
  `completed_at`, `created_at`

### actions
Таблица нужна с Этапа 1 (чтобы не переделывать схему на Этапе 2), но реального
выполнения (`EXECUTE` → запрос в API маркетплейса) в коде Этапа 1 нет — только
`ANALYZE`/`RECOMMEND` пишут сюда черновики со статусом, который никогда не
доходит до `EXECUTING` без кода Этапа 2.
- `id`, `client_id`, `marketplace_account_id`, `sku_id` (nullable), `type`
  (`change_price` | `change_ad_bid` | ...), `payload`, `status`
  (`pending` | `approval_required` | `approved` | `executing` | `completed` |
  `rejected` | `failed` | `cancelled`), `requested_by`, `approved_by`,
  `executed_at`, `result`, `error`, `idempotency_key`, `created_at`

### audit_logs
Пишется с Этапа 1, на все значимые операции (создание клиента, подключение
кабинета, синхронизация, создание задачи, AI-рекомендация) — не только на
будущие Action Engine-действия.
- `id`, `organization_id`, `user_id`, `client_id`, `action`, `entity_type`,
  `entity_id`, `before`, `after`, `ip`, `created_at`

## Что осознанно не проектируется сейчас

- Гранулярная таблица прав (`permissions`) — см. раздел про `users.role` выше.
- `raw_marketplace_data` как отдельный слой «сырых» ответов API — на масштабе
  Этапа 1 разумнее хранить сырой ответ только во временном логе синхронизации
  (`sync_jobs.error`/отдельный лог-файл) для отладки, а не заводить постоянную
  таблицу под это; вернуться к вопросу, если понадобится реплей исторических
  данных.
- Партиционирование/индексы под большие объёмы — добавляются по факту, когда
  реальные объёмы данных это потребуют (§63 ТЗ верно указывает на это как на
  будущую задачу, не задачу первого дня).

## Следующий документ

`docs/connectors.md` — как устроены Ozon/WB-коннекторы поверх этой схемы
(интерфейс `MarketplaceConnector`, где именно в коде используются уже известные
особенности API Ozon/WB, найденные в текущем статическом проекте).
