# API базы знаний

Задача `task_109` переводит базу знаний из заглушек в рабочий MVP-контракт без
ML/Qdrant-индексации.

## Документы

### `GET /api/v1/knowledge/documents`

Возвращает документы текущей компании из JWT `tenant_id`.

Ответ:

```json
[
  {
    "id": "uuid",
    "title": "FAQ Telegram",
    "source_type": "manual",
    "storage_url": null,
    "status": "ready",
    "version": 1,
    "chunks_count": 1,
    "created_at": "2026-06-29T12:00:00Z",
    "updated_at": "2026-06-29T12:00:00Z"
  }
]
```

### `POST /api/v1/knowledge/documents`

Создаёт ручной текстовый документ и сразу нарезает его на чанки в БД.

Тело:

```json
{
  "title": "FAQ Telegram",
  "source_type": "manual",
  "text": "Telegram подключается через токен бота.",
  "tags": { "topic": "telegram" }
}
```

На этом этапе `source_type` поддерживает `manual`, `txt`, `md`, `url`.
Файловая загрузка, S3 и Qdrant-индексация остаются следующим шагом.

## Кандидаты автообучения

### `GET /api/v1/knowledge/candidates`

Возвращает кандидатов текущей компании.

### `POST /api/v1/knowledge/candidates/{candidate_id}/approve`

Подтверждает кандидата, создаёт новый документ базы знаний и один чанк вида
`Вопрос → Ответ`. Повторный approve идемпотентно возвращает уже созданный документ.
