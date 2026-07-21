# ARQ workers

Run a worker against the configured Redis database:

```bash
uv run arq app.workers.worker.WorkerSettings
```

The worker exposes two stable jobs:

- `process_inbound_message(message_id: str)` loads a persisted Telegram inbound
  message, runs retrieval/LLM decisioning and writes an AI reply or escalation.
- `reindex_document(document_id: str)` replaces the current document points in
  Qdrant and returns the indexed chunk count.

Producer code should use `app.workers.queue.enqueue_inbound_message` and
`enqueue_document_reindex`. Both assign deterministic ARQ job ids, so duplicate
delivery while a job/result is retained does not enqueue duplicate work. Worker
functions are also retry-safe at the database/vector level: completed inbound
decisions are detected by stable message ids, and reindex deletes then upserts
stable chunk ids.

The existing Telegram HTTP route still calls the extracted inbound processor
synchronously to preserve its response contract. Wiring the producer helper to
the API lifespan Redis pool is the remaining switch to make webhook responses
fully asynchronous. Telegram delivery is at-least-once: a process failure after
the Bot API accepted a message but before the database commit can resend it on
retry; a durable outbox is required for exactly-once delivery.
