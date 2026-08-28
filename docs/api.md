# API — Этап 1

Продолжение `docs/database.md`, `docs/connectors.md`, `docs/profit-engine.md`.
REST API поверх этой схемы — то, через что интерфейс (и в будущем — AI Core)
читает и меняет данные. Ниже — только эндпоинты, нужные Этапу 1 (просмотр,
управление клиентами/кабинетами, ручной запуск синхронизации, AI-рекомендации
без исполнения). Action Engine (`POST /api/v1/actions/...`) и Admin-панель —
отдельные документы, пишутся при переходе к Этапу 2, не раньше.

## Формат ответа

Единый на все эндпоинты (§36 ТЗ, идея верная, переносится без изменений):

Успех: `{ "success": true, "data": {...}, "meta": {...} }`
Ошибка: `{ "success": false, "error": { "code": "...", "message": "..." } }`

Коды ошибок (§37 ТЗ): `400 VALIDATION_ERROR`, `401 UNAUTHORIZED`,
`403 FORBIDDEN`, `404 NOT_FOUND`, `409 CONFLICT`, `422 BUSINESS_RULE_ERROR`,
`429 RATE_LIMITED`, `500 INTERNAL_ERROR`, `502 MARKETPLACE_ERROR` (отдельный
код специально для ошибок, пришедших от Ozon/WB, а не от нашего кода — это
разница важна для диагностики, как показал опыт текущего проекта).

Каждый запрос обязан фильтроваться по `client_id` из текущей сессии
пользователя на уровне кода эндпоинта — не полагаться на то, что фронтенд не
запросит чужого (см. `docs/database.md`, раздел про тенантность).

## AUTH

```
POST /api/v1/auth/register
POST /api/v1/auth/login
POST /api/v1/auth/logout
GET  /api/v1/auth/me
```

## ORGANIZATION / CLIENTS

```
GET    /api/v1/clients
POST   /api/v1/clients
GET    /api/v1/clients/:id
PATCH  /api/v1/clients/:id
```

Удаление клиента на Этапе 1 не реализуется (мягкое архивирование — да, если
понадобится; жёсткое удаление финансовых данных — отдельный вопрос, требующий
отдельного решения, не по умолчанию).

## MARKETPLACE ACCOUNTS

```
GET    /api/v1/clients/:clientId/accounts
POST   /api/v1/clients/:clientId/accounts       — подключить кабинет (ключи вводит сама Юля/клиент)
GET    /api/v1/accounts/:id
PATCH  /api/v1/accounts/:id                      — обновить ключи, переименовать
DELETE /api/v1/accounts/:id
POST   /api/v1/accounts/:id/test-connection
POST   /api/v1/accounts/:id/sync                 — ручной запуск синхронизации
GET    /api/v1/accounts/:id/sync-status
```

## PRODUCTS / SKU

```
GET /api/v1/accounts/:id/products
GET /api/v1/skus/:id
GET /api/v1/skus/:id/orders
GET /api/v1/skus/:id/price-history
GET /api/v1/skus/:id/inventory
```

## ORDERS / RETURNS

```
GET /api/v1/accounts/:id/orders?date_from=&date_to=
GET /api/v1/accounts/:id/returns?date_from=&date_to=
```

## ADVERTISING

```
GET /api/v1/accounts/:id/advertising/campaigns
GET /api/v1/accounts/:id/advertising/daily-stats?date_from=&date_to=
```

## PROFIT

Единственная точка правды для цифр на дашборде — читает `profit_daily`, не
пересчитывает на лету (см. `docs/profit-engine.md`).

```
GET /api/v1/clients/:id/profit?date_from=&date_to=
GET /api/v1/clients/:id/profit/summary            — агрегат за период для карточек дашборда
```

## AI / RECOMMENDATIONS

Только рекомендации на Этапе 1 — без `create-task`/`approve`, ведущих к
исполнению (это часть Action Engine, Этап 2).

```
POST /api/v1/clients/:id/ai/analyze                — запустить анализ вручную
GET  /api/v1/clients/:id/ai/recommendations
GET  /api/v1/ai/recommendations/:id
```

## TASKS

```
GET    /api/v1/clients/:id/tasks
POST   /api/v1/clients/:id/tasks
PATCH  /api/v1/tasks/:id
POST   /api/v1/tasks/:id/complete
```

## AUDIT (только чтение)

```
GET /api/v1/clients/:id/audit
```

Пишется во все перечисленные выше эндпоинты с побочными эффектами (создание
клиента, подключение кабинета, ручной запуск синхронизации, создание задачи) —
не только в будущие Action Engine-операции.

## Что осознанно не входит в Этап 1

- `POST /api/v1/actions/...` и весь Action Engine — Этап 2 (`docs/action-engine.md`,
  пишется позже).
- `/api/v1/admin/...` — админ-панель имеет смысл, когда есть что администрировать
  (больше одного администратора/клиента), не раньше.
- `/api/v1/reports` (генерация PDF/XLSX-отчётов) — полезная, но не блокирующая
  функция; данные для неё уже доступны через `/profit` и `/ai/recommendations`,
  сама генерация файлов может подождать.

## Следующий документ

`docs/ai-agents.md` — как устроен AI Core на Этапе 1: какие данные видит,
через какие инструменты, как формулирует рекомендации без прямого доступа к
базе данных.
