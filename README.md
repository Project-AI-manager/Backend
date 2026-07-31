# Автопилот — Backend

Backend AI-менеджера «Автопилот»: FastAPI API, хранение данных, RAG, Telegram-интеграция, email, аналитика и фоновые процессы.

- Frontend: [Project-AI-manager/Frontend](https://github.com/Project-AI-manager/Frontend)
- Обзор проекта: [Project-AI-manager/Main](https://github.com/Project-AI-manager/Main)
- Публичный интерфейс: [автопилот.space](https://автопилот.space)

## Возможности

- JWT-аутентификация с access/refresh-токенами и Argon2;
- регистрация, подтверждение email и восстановление пароля;
- роли `owner`, `admin` и `manager` с разграничением доступа;
- клиенты, диалоги, сообщения, вложения и статусы прочтения;
- подключение личного Telegram-аккаунта через MTProto, OTP и 2FA;
- приём Telegram-сообщений отдельным listener-процессом или внутри API;
- отметка входящего сообщения прочитанным, typing-индикатор и задержка автоответа;
- документы базы знаний: PDF, DOCX, XLSX, TXT и Markdown;
- чанкинг, эмбеддинги, Qdrant-поиск и переиндексация;
- RAG-ответы по четырём наиболее релевантным фрагментам и истории диалога;
- оценка уверенности и перевод диалога менеджеру;
- кандидаты в базу знаний из ответов сотрудников;
- агрегированная аналитика и подробная XLSX-выгрузка;
- SMTP-письма и уведомления;
- health-check и диагностика внешних интеграций;
- статус прохождения продуктового обучения.

## Стек

- Python 3.12;
- FastAPI, Pydantic v2, Uvicorn/Gunicorn;
- SQLAlchemy 2 async, Alembic, PostgreSQL;
- SQLite для облегчённого локального режима;
- Redis и ARQ;
- Qdrant server или embedded local storage;
- FastEmbed, модель `sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2`;
- OpenAI-compatible API для LLM и облачных эмбеддингов;
- Telethon для Telegram MTProto;
- PyPDF, python-docx и openpyxl для документов;
- pytest, Ruff и mypy.

## Структура

```text
app/
├── api/v1/routes/     # HTTP-эндпоинты
├── core/              # конфигурация, безопасность и логирование
├── db/                # engine, сессии, миграционные утилиты и seed
├── models/            # SQLAlchemy-модели
├── schemas/           # Pydantic-схемы
├── services/          # бизнес-логика, RAG, каналы, email, аналитика
├── workers/           # ARQ и Telegram listener
└── main.py            # сборка FastAPI-приложения
alembic/               # миграции базы данных
tests/                 # unit- и integration-тесты
docker-compose.yml     # PostgreSQL, Redis, Qdrant и MinIO
```

API сгруппирован по разделам `auth`, `users`, `conversations`, `knowledge`, `channels`, `settings`, `analytics`, `email`, `integrations` и `ml`.

## Быстрый запуск без Docker

Подходит для разработки интерфейса и ручной проверки API. Используются SQLite, mock-LLM и локальные файлы.

```powershell
Copy-Item .env.example .env
uv sync

$env:DATABASE_URL="sqlite+aiosqlite:///./local-demo.sqlite3"
$env:SECRET_KEY="local-dev-secret-key-that-is-long-enough"
$env:CORS_ORIGINS="http://localhost:3000"
$env:LLM_PROVIDER="mock"
$env:QDRANT_URL="local"
$env:QDRANT_ENABLED="true"
$env:TELEGRAM_DELIVERY_ENABLED="false"

uv run python -m app.db.local_demo --reset
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

После seed доступен локальный демонстрационный пользователь:

```text
owner.demo@example.com
demo-password
```

Эти данные предназначены только для localhost. Не переносите демонстрационный пароль в общедоступное окружение.

## Запуск с локальной инфраструктурой

```powershell
Copy-Item .env.example .env
uv sync
docker compose up -d
uv run alembic upgrade head
uv run uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

`docker-compose.yml` поднимает PostgreSQL, Redis, Qdrant и MinIO. Перед первым использованием MinIO создайте bucket, совпадающий с `S3_BUCKET`, либо настройте другое S3-совместимое хранилище.

## Конфигурация

Все настройки читаются из окружения; полный перечень и безопасные локальные значения находятся в [.env.example](.env.example).

| Группа | Основные переменные |
| --- | --- |
| Приложение | `APP_ENV`, `SECRET_KEY`, `API_V1_PREFIX`, `CORS_ORIGINS` |
| Данные | `DATABASE_URL`, `REDIS_URL` |
| Векторы | `QDRANT_URL`, `QDRANT_COLLECTION`, `QDRANT_ENABLED` |
| Файлы | `S3_*`, `CONVERSATION_UPLOAD_DIR`, `CUSTOMER_AVATAR_DIR` |
| Модель | `LLM_PROVIDER`, `OPENAI_COMPATIBLE_*` |
| Эмбеддинги | `EMBEDDING_PROVIDER`, `EMBEDDING_*` |
| Telegram | `TELEGRAM_API_ID`, `TELEGRAM_API_HASH`, `TELEGRAM_*` |
| Email | `EMAIL_*`, `SMTP_*`, `APP_PUBLIC_URL` |
| JWT | `ACCESS_TOKEN_TTL_MIN`, `REFRESH_TOKEN_TTL_DAYS` |

Не коммитьте заполненный `.env`, API-ключи, Telegram session string, SMTP-пароли или production JWT secret.

## LLM и RAG

Локально можно использовать `LLM_PROVIDER=mock`. Для реальных ответов backend поддерживает OpenAI-compatible endpoint через `OPENAI_COMPATIBLE_BASE_URL`, `OPENAI_COMPATIBLE_API_KEY` и `OPENAI_COMPATIBLE_MODEL`.

Базовый RAG-процесс:

1. пользователь загружает документ;
2. backend извлекает и разбивает текст на фрагменты;
3. команда обновления базы строит эмбеддинги и записывает их в Qdrant;
4. для входящего вопроса находятся четыре ближайших фрагмента;
5. модель получает найденные знания и историю диалога;
6. confidence-логика разрешает автоответ или создаёт эскалацию.

### Локальные эмбеддинги

```dotenv
EMBEDDING_PROVIDER=local-ml
EMBEDDING_MODEL=sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2
EMBEDDING_DIMENSION=384
QDRANT_ENABLED=true
```

Модель скачивается один раз в `EMBEDDING_CACHE_DIR` и исполняется на CPU через ONNX/FastEmbed. Если сервер Qdrant недоступен, задайте `QDRANT_URL=local`: данные будут сохранены в `QDRANT_LOCAL_PATH`.

### OpenAI-compatible embeddings

Используйте `EMBEDDING_PROVIDER=openai-compatible` и заполните `EMBEDDING_BASE_URL`, `EMBEDDING_API_KEY`, `EMBEDDING_MODEL`, `EMBEDDING_DIMENSION`.

Размерность модели должна совпадать с Qdrant collection. После смены модели или размерности укажите новую `QDRANT_COLLECTION` и выполните полную переиндексацию:

```powershell
uv run python -m app.db.reindex
# Один workspace:
uv run python -m app.db.reindex --tenant-id <uuid>
```

## Telegram

Каноническая интеграция использует личный Telegram-аккаунт через MTProto. API проводит OTP/2FA-подключение и хранит session string в зашифрованном виде. `TELEGRAM_API_ID` и `TELEGRAM_API_HASH` доступны только backend.

В обычной конфигурации listener запускается отдельно:

```powershell
uv run python -m app.workers.telegram_listener
```

Для локального embedded-Qdrant можно включить `TELEGRAM_LISTENER_IN_PROCESS=true`, чтобы API и listener использовали один кешированный клиент. В таком режиме не запускайте второй listener-процесс.

Старый Bot API webhook оставлен для совместимости локальных тестов и не является основным production-сценарием.

## Email

В локальном режиме `EMAIL_DEV_MODE=true` позволяет проверить подтверждение адреса, восстановление пароля и уведомления без реальной отправки. Для SMTP задайте `EMAIL_SEND_ENABLED=true`, `SMTP_HOST`, порт и учётные данные. `APP_PUBLIC_URL` определяет ссылки, которые пользователь получает в письмах.

Unicode-домен `https://автопилот.space` поддерживается: техническое IDNA/Punycode-представление применяется только там, где этого требует протокол.

## Файлы и вложения

Файлы базы знаний и вложения диалогов проверяются по типу и размеру. Локально они могут храниться в каталогах из `CONVERSATION_UPLOAD_DIR` и `CUSTOMER_AVATAR_DIR`; для общей инфраструктуры предусмотрено S3-совместимое хранилище.

Не выставляйте каталоги загрузок как публичную статику без авторизационной проверки.

## API и диагностика

После запуска доступны:

- `GET /health` — базовый health-check;
- `/docs` — Swagger UI;
- `/openapi.json` — спецификация для frontend-клиента;
- `/api/v1/integrations/...` — проверки подключённых сервисов для owner/admin.

Если используется другой `API_V1_PREFIX`, пути API изменятся соответствующим образом.

## Проверки

```powershell
uv run ruff check .
uv run mypy app
uv run pytest
```

Миграции после изменения моделей:

```powershell
uv run alembic upgrade head
```

## Production-требования

В окружении, отличном от `local` и `test`, приложение отклоняет небезопасную конфигурацию, включая:

- короткий или стандартный `SECRET_KEY`;
- `CORS_ORIGINS=*`;
- SQLite;
- `EMAIL_DEV_MODE=true`;
- включённую отправку email без `SMTP_HOST`.

Production-развёртывание backend пока не завершено. Публичный frontend временно обращается к локальному backend через HTTPS-туннель; после перезапуска туннеля его новый URL нужно передать frontend через `NEXT_PUBLIC_API_URL`.
