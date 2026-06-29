# Карта API по страницам продукта

- **Дата:** 2026-06-24
- **Ветка:** `task_103`
- **Репозиторий:** backend
- **Статус:** проектирование API-контракта до реализации

Этот документ описывает, какие API нужны страницам фронта и зачем. Он не является финальной OpenAPI-спецификацией, но должен стать рабочей картой для реализации backend endpoints в `task/103`.

## Общие правила API

### Базовый формат

- REST API: `/api/v1/...`
- Документация FastAPI/OpenAPI: `/openapi.json`
- Авторизация: `Authorization: Bearer <access_token>`
- Access/refresh: JWT.
- Тенантность: backend должен брать `tenant_id` из JWT/пользователя, а не доверять `tenant_id` из frontend-запросов.
- Все списки должны поддерживать пагинацию: `limit`, `offset` или cursor.
- Все изменяющие запросы должны принимать JSON body, а не query-параметры.
- Ответы должны иметь Pydantic response models, чтобы frontend генерировал стабильные типы через OpenAPI.

### Базовая форма ошибок

Пока можно использовать стандартный FastAPI `HTTPException`, но для фронта желательно прийти к единому формату:

```json
{
  "code": "conversation_not_found",
  "message": "Диалог не найден",
  "details": {}
}
```

## Текущий факт по backend

Сейчас в коде уже есть роутеры:

- `auth`
- `users`
- `conversations`
- `knowledge`
- `ml`
- `channels`
- `analytics`
- `settings`

Но многие endpoints пока являются заглушками `NotImplementedError`, а часть рабочих endpoints возвращает `dict` / `list[dict]` без точных схем.

Главный вывод: **каркас API есть, но контракт ещё не утверждён полностью**. Перед массовой интеграцией фронта нужно зафиксировать request/response schemas.

## Страницы и нужные API

### 1. Публичный лендинг

Маршрут фронта: `/`

Назначение:

- объяснить продукт;
- показать ценность AI-сотрудника;
- привести пользователя к регистрации или входу.

API для MVP:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/health` | Простая проверка доступности backend при dev/demo | Есть |

Публичный лендинг не должен зависеть от авторизованного API. В будущем можно добавить публичный endpoint для feature flags или тарифов, но для MVP это не обязательно.

### 2. Регистрация

Маршрут фронта: `/register`

Назначение:

- создать компанию-клиента;
- создать первого пользователя с ролью owner;
- выдать JWT пару.

Нужные API:

| Метод | Endpoint | Request | Response | Зачем | Статус |
|---|---|---|---|---|---|
| `POST` | `/api/v1/auth/register` | `company_name`, `email`, `password`, `full_name` | `access_token`, `refresh_token`, `token_type` | Создать tenant + owner user | Endpoint есть, реализация заглушка |

Решения для реализации:

- При регистрации создавать:
  - `tenant`;
  - `user` с ролью `owner`;
  - дефолтный `tenant_ai_config`;
  - при необходимости trial subscription / usage counter.
- Пароль хранить только как hash.
- `access_token` должен содержать минимум `sub`, `tenant_id`, `role`, `type=access`.
- `refresh_token` лучше хранить/валидировать через таблицу `refresh_token`.

### 3. Вход

Маршрут фронта: `/login`

Назначение:

- авторизовать пользователя;
- выдать JWT пару;
- позволить frontend открывать защищённые страницы.

Нужные API:

| Метод | Endpoint | Request | Response | Зачем | Статус |
|---|---|---|---|---|---|
| `POST` | `/api/v1/auth/login` | `email`, `password` | `access_token`, `refresh_token`, `token_type` | Вход пользователя | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/auth/refresh` | `refresh_token` | `access_token`, `refresh_token`, `token_type` | Обновление сессии | Endpoint есть, реализация заглушка |

Важно:

- Сейчас большинство endpoints требует Bearer JWT, поэтому без реализации auth фронт будет получать `401`.
- Это блокер для настоящей интеграции страниц кабинета.

### 4. Онбординг

Маршрут фронта: `/onboarding`

Назначение:

- быстро довести пользователя до первого полезного результата;
- настроить компанию, первый канал, базу знаний и AI-поведение.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/users/me` | Получить текущего пользователя, tenant, роль | Endpoint есть, ответ пока payload JWT |
| `GET` | `/api/v1/settings/ai` | Получить дефолтные AI-настройки | Есть, но без response model |
| `PUT` | `/api/v1/settings/ai` | Сохранить автоответы, порог уверенности, system prompt | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/channels` | Подключить первый канал, сначала web-chat | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/knowledge/documents` | Загрузить первый документ базы знаний | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/knowledge/ask` | Проверить вопрос в playground после загрузки знаний | Есть, использует ML flow |

Не хватает:

- endpoint для workspace/company profile: например `GET/PUT /api/v1/settings/workspace`;
- нормальный request body для подключения канала;
- загрузка файла через `multipart/form-data` или signed upload flow.

### 5. Диалоги / Inbox

Маршрут фронта: `/inbox`

Назначение:

- главное рабочее место менеджера;
- видеть обращения из всех каналов;
- читать тред;
- видеть AI-черновик/решение;
- отправлять ответ или эскалировать.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/conversations` | Список диалогов с фильтрами | Есть, возвращает пустой список |
| `GET` | `/api/v1/conversations/{conversation_id}` | Детали диалога: сообщения, клиент, канал, AI-контекст | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/conversations/{conversation_id}/reply` | Отправить ответ менеджера в канал | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/conversations/{conversation_id}/escalate` | Перевести диалог в ручную обработку | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/ml/answer` | Получить AI-ответ/черновик по текущему сообщению | Есть |

Фильтры для `GET /conversations`:

- `status`: `open`, `closed`, `escalated`, `auto`;
- `channel_type`: `web`, `avito`, `vk`, `max`;
- `assignee_user_id`;
- `unread_only`;
- `q` для поиска по клиенту/тексту;
- `limit`, `offset`.

Рекомендуемые новые/уточнённые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `POST` | `/api/v1/conversations/{conversation_id}/assign` | Назначить менеджера |
| `POST` | `/api/v1/conversations/{conversation_id}/close` | Закрыть диалог |
| `POST` | `/api/v1/conversations/{conversation_id}/ai-draft` | Получить AI-черновик именно в контексте диалога |
| `WS` | `/api/v1/conversations/stream` | Live-обновления inbox |

Важно:

- `reply` должен принимать body, например `{ "text": "...", "send_to_channel": true }`.
- `ml/answer` сейчас универсальный, но для inbox лучше иметь endpoint, который сам достаёт историю диалога, клиента, канал и tenant context из БД.

### 6. База знаний

Маршрут фронта: `/knowledge`

Назначение:

- управлять документами;
- смотреть статус индексации;
- тестировать ответы;
- подтверждать кандидатов автообучения.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/knowledge/documents` | Список документов | Есть, возвращает пустой список |
| `POST` | `/api/v1/knowledge/documents` | Загрузка документа | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/knowledge/ask` | Playground: вопрос → ответ + источники | Есть |
| `GET` | `/api/v1/knowledge/candidates` | Очередь кандидатов автообучения | Есть, возвращает пустой список |
| `POST` | `/api/v1/knowledge/candidates/{candidate_id}/approve` | Подтвердить кандидата в базу знаний | Endpoint есть, реализация заглушка |

Рекомендуемые новые/уточнённые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `GET` | `/api/v1/knowledge/documents/{document_id}` | Детали документа |
| `DELETE` | `/api/v1/knowledge/documents/{document_id}` | Удаление документа и его чанков |
| `POST` | `/api/v1/knowledge/documents/{document_id}/reindex` | Переиндексация |
| `GET` | `/api/v1/knowledge/documents/{document_id}/chunks` | Просмотр чанков |
| `POST` | `/api/v1/knowledge/candidates/{candidate_id}/reject` | Отклонить кандидата |
| `PUT` | `/api/v1/knowledge/candidates/{candidate_id}` | Отредактировать кандидата перед подтверждением |

Важно:

- Загрузка документа должна запускать фоновую задачу индексации.
- Ответ документа должен содержать `status`: `uploaded`, `processing`, `ready`, `failed`.
- Playground должен возвращать `answer`, `confidence`, `decision`, `sources`.

### 7. Каналы

Маршрут фронта: `/channels`

Назначение:

- подключать web-chat, Avito, VK, позже MAX;
- видеть статус интеграций;
- диагностировать ошибки.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/channels` | Список подключённых каналов | Есть, возвращает пустой список |
| `POST` | `/api/v1/channels` | Подключить канал | Endpoint есть, реализация заглушка |
| `POST` | `/api/v1/channels/webhook/{channel_type}` | Приём webhook от канала | Есть, возвращает `ok` |

Рекомендуемые новые/уточнённые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `GET` | `/api/v1/channels/{channel_id}` | Детали канала |
| `PUT` | `/api/v1/channels/{channel_id}` | Изменить настройки канала |
| `DELETE` | `/api/v1/channels/{channel_id}` | Отключить канал |
| `POST` | `/api/v1/channels/{channel_id}/test` | Проверить подключение |
| `GET` | `/api/v1/channels/web-widget/config` | Получить публичную конфигурацию web-chat виджета |

Важно:

- `POST /channels` должен принимать body, например `{ "type": "web", "name": "...", "settings": {} }`.
- Секреты внешних каналов нельзя возвращать во frontend.
- Webhook endpoints должны иметь проверку подписи/секрета для внешних каналов.

### 8. Аналитика

Маршрут фронта: `/analytics`

Назначение:

- показать ценность продукта;
- отслеживать автоответы, эскалации, скорость ответа, лимиты.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/analytics/overview` | KPI карточки | Есть, возвращает нули |

Рекомендуемые новые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `GET` | `/api/v1/analytics/timeseries` | Графики по дням/неделям |
| `GET` | `/api/v1/analytics/escalations` | Причины и темы эскалаций |
| `GET` | `/api/v1/analytics/knowledge-growth` | Рост базы знаний |

Параметры:

- `from`;
- `to`;
- `channel_type`;
- `manager_id`.

Минимальный `overview` response:

```json
{
  "auto_reply_rate": 0.62,
  "avg_response_sec": 18,
  "dialogs_used": 1240,
  "dialogs_limit": 2000,
  "escalations_count": 84,
  "knowledge_documents_count": 17
}
```

### 9. Настройки

Маршрут фронта: `/settings`

Назначение:

- управлять AI-поведением;
- управлять командой;
- видеть тариф и лимиты;
- редактировать компанию.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/settings/ai` | Настройки AI | Есть, но без response model |
| `PUT` | `/api/v1/settings/ai` | Сохранить настройки AI | Endpoint есть, реализация заглушка |
| `GET` | `/api/v1/settings/billing` | Тариф и лимиты | Есть, но пока trial/нули |
| `GET` | `/api/v1/users` | Список команды | Есть, возвращает пустой список |

Рекомендуемые новые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `GET` | `/api/v1/settings/workspace` | Данные компании |
| `PUT` | `/api/v1/settings/workspace` | Обновить компанию |
| `POST` | `/api/v1/users/invite` | Пригласить сотрудника |
| `PUT` | `/api/v1/users/{user_id}` | Изменить имя/роль/статус |
| `DELETE` | `/api/v1/users/{user_id}` | Удалить/деактивировать пользователя |

Минимальный `settings/ai` response:

```json
{
  "auto_reply_enabled": false,
  "confidence_threshold": 80,
  "llm_provider": "mock",
  "embedding_model": "local",
  "system_prompt": "Отвечай кратко, дружелюбно и только по базе знаний.",
  "business_hours": {
    "timezone": "Europe/Moscow",
    "days": [1, 2, 3, 4, 5],
    "from": "09:00",
    "to": "18:00"
  }
}
```

### 10. Профиль

Маршрут фронта: `/profile`

Назначение:

- показать текущего пользователя;
- дать изменить имя/пароль;
- дать выйти из аккаунта.

Нужные API:

| Метод | Endpoint | Зачем | Статус |
|---|---|---|---|
| `GET` | `/api/v1/users/me` | Текущий пользователь | Есть, но возвращает JWT payload |

Рекомендуемые новые endpoints:

| Метод | Endpoint | Зачем |
|---|---|---|
| `PUT` | `/api/v1/users/me` | Обновить профиль |
| `POST` | `/api/v1/users/me/change-password` | Смена пароля |
| `POST` | `/api/v1/auth/logout` | Отозвать refresh token |

### 11. Legal pages

Маршруты фронта:

- `/legal/privacy`
- `/legal/terms`

API для MVP не нужен. Это статические страницы.

## Приоритет реализации backend API

### Шаг 1 — авторизация

Без auth frontend не сможет нормально ходить в защищённые endpoints.

Сделать:

- `POST /auth/register`;
- `POST /auth/login`;
- `POST /auth/refresh`;
- расширить JWT payload: `sub`, `tenant_id`, `role`, `type`;
- `GET /users/me` должен возвращать профиль из БД, а не сырой payload.

### Шаг 2 — точные схемы ответов

Сделать Pydantic schemas для:

- user/profile;
- tenant/workspace;
- settings AI;
- billing;
- channel;
- conversation list item;
- conversation detail;
- message;
- knowledge document;
- knowledge candidate;
- analytics overview.

### Шаг 3 — settings + onboarding

Самый короткий путь к рабочему кабинету:

- `GET/PUT /settings/ai`;
- `GET/PUT /settings/workspace`;
- `GET /settings/billing`;
- `POST /channels` для web-chat;
- `GET /channels`.

### Шаг 4 — inbox

Ключевая ценность продукта:

- список диалогов;
- детали диалога;
- отправка ответа;
- эскалация;
- AI draft по диалогу.

### Шаг 5 — knowledge

Нужна для качества AI:

- документы;
- upload;
- reindex;
- playground;
- candidates approve/reject.

### Шаг 6 — analytics

После появления реальных диалогов и сообщений:

- overview;
- timeseries;
- эскалации;
- usage.

## Что нужно поправить в уже существующих endpoints

1. `POST /api/v1/auth/*` — реализовать, сейчас заглушки.
2. `GET /api/v1/users/me` — вернуть нормальный профиль, а не JWT payload.
3. `POST /api/v1/conversations/{id}/reply` — принимать JSON body, не query `text`.
4. `POST /api/v1/channels` — принимать JSON body, не query `type`.
5. `PUT /api/v1/settings/ai` — добавить request schema.
6. Почти всем endpoints добавить response models.
7. Спискам добавить пагинацию и фильтры.
8. Для tenant isolation убрать доверие к `tenant_id` из frontend body там, где tenant можно взять из JWT.

## Минимальный контракт для первого рабочего цикла frontend ↔ backend

Чтобы фронт начал честно работать с backend, достаточно такого набора:

| Страница | Минимум API |
|---|---|
| `/register` | `POST /auth/register` |
| `/login` | `POST /auth/login`, `POST /auth/refresh` |
| `/onboarding` | `GET /users/me`, `GET/PUT /settings/ai`, `POST /channels`, `POST /knowledge/documents` |
| `/inbox` | `GET /conversations`, `GET /conversations/{id}`, `POST /conversations/{id}/reply`, `POST /conversations/{id}/escalate` |
| `/knowledge` | `GET/POST /knowledge/documents`, `POST /knowledge/ask`, `GET /knowledge/candidates` |
| `/channels` | `GET/POST /channels` |
| `/analytics` | `GET /analytics/overview` |
| `/settings` | `GET/PUT /settings/ai`, `GET /settings/billing`, `GET /users` |
| `/profile` | `GET /users/me` |

Именно этот набор стоит считать основой `task_103`.
