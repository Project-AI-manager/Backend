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

## База данных

Первая миграция `20260623_0001_initial_schema` создаёт схему MVP: tenant/user/channel/customer/conversation/message, базу знаний, эскалации, тарифы и счётчики использования.
Для дедупликации уже зафиксированы уникальные ограничения на входящие вебхуки, внешние identity клиентов, внешние сообщения внутри диалога и usage-counter за период.
