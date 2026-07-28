# Backend — AI-сотрудник в едином окне

FastAPI-бэкенд SaaS-платформы. Стек и архитектура: см. вики проекта (`wiki/concepts/tech-stack.md`, `system-architecture.md`, `data-model.md`).

## Стек
Python 3.12 · FastAPI · SQLAlchemy 2 (async) + asyncpg · PostgreSQL · Alembic · Redis + ARQ · Qdrant · httpx · JWT/argon2 · structlog · uv.

## Структура
```
app/
  main.py            # сборка приложения, подключение роутеров
  core/              # config, security (JWT/argon2), logging
  db/                # engine, session, Base + миксины
  models/            # SQLAlchemy-модели (по data-model)
  schemas/           # Pydantic-схемы запросов/ответов
  api/v1/routes/     # эндпоинты по ресурсам
  services/          # бизнес-логика: rag/, channels/, confidence, knowledge
  workers/           # фоновые задачи ARQ
alembic/             # миграции
tests/
```

## Локальный запуск (когда появятся зависимости)
```bash
uv sync
docker compose up -d            # postgres, redis, qdrant, minio
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```
Конфиг — через `.env` (см. `.env.example`). AI-слой и каналы на старте работают на заглушках (`MockLLM`, локальные эмбеддинги).

## Безопасность и роли

При `APP_ENV`, отличном от `local` и `test`, приложение не запустится с:

- дефолтным/коротким `SECRET_KEY` (минимум 32 символа);
- wildcard `CORS_ORIGINS=*`;
- `EMAIL_DEV_MODE=true` или SQLite;
- `EMAIL_SEND_ENABLED=true` без `SMTP_HOST`.

Менеджеры могут работать с диалогами, но просмотр и подключение каналов, изменение AI/workspace-настроек, список команды, email outbox и диагностика интеграций доступны только `owner|admin`.

Основной Telegram-контур использует личный аккаунт через MTProto: API запускает OTP/2FA-вход, шифрует session string, а отдельный процесс `python -m app.workers.telegram_listener` слушает входящие сообщения. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` хранятся только в окружении backend. Старый Bot API webhook сохранён для обратной совместимости тестового MVP, но не является каноническим подключением.

## Embeddings и переиндексация

Для локальных semantic embeddings используйте `EMBEDDING_PROVIDER=local-ml`, модель `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`, размерность `384` и новую Qdrant collection. Модель скачивается один раз в `EMBEDDING_CACHE_DIR` и исполняется на CPU через ONNX/FastEmbed.

Если Docker/Qdrant-сервер не запущен, задайте `QDRANT_URL=local`: backend
использует встроенное хранилище Qdrant и сохраняет его в `QDRANT_LOCAL_PATH`.

OpenAI-compatible embeddings use `EMBEDDING_PROVIDER=openai-compatible` and the
`EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, and
`EMBEDDING_DIMENSION` variables. `EMBEDDING_DIMENSION` must match the selected model
and the Qdrant collection. After changing the model or dimension, point
`QDRANT_COLLECTION` at a new collection (recommended) and run:

```bash
uv run python -m app.db.reindex
# optionally limit the rebuild: --tenant-id <uuid>
```

## Быстрый локальный запуск без Docker
Если на машине нет Docker/PostgreSQL, можно поднять песочницу на SQLite:

```powershell
$env:DATABASE_URL="sqlite+aiosqlite:///./local-demo.sqlite3"
$env:SECRET_KEY="local-dev-secret-key-that-is-long-enough"
$env:CORS_ORIGINS="http://localhost:3000"
$env:LLM_PROVIDER="mock"
$env:TELEGRAM_DELIVERY_ENABLED="false"

.\.venv\Scripts\python.exe -m app.db.local_demo --reset
.\.venv\Scripts\python.exe -m uvicorn app.main:app --host 127.0.0.1 --port 8000
```

Демо-логин после seed:

```text
owner.demo@example.com
demo-password
```

Этот режим только для ручного localhost-теста. Production/dev-infra путь остаётся через PostgreSQL + Alembic.

## База данных

Первая миграция `20260623_0001_initial_schema` создаёт схему MVP: tenant/user/channel/customer/conversation/message, базу знаний, эскалации, тарифы и счётчики использования.
Для дедупликации уже зафиксированы уникальные ограничения на входящие вебхуки, внешние identity клиентов, внешние сообщения внутри диалога и usage-counter за период.
