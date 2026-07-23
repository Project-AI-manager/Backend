# Deployment topology

## Public pilot

The Vercel frontend cannot make a multi-user product out of `localhost:8000`:
each visitor's browser resolves localhost to that visitor's own computer, and
HTTPS-to-local-HTTP requests may be blocked by browser Private Network Access.

Use this split:

- frontend: Vercel;
- API + Telegram listener + ARQ worker: one persistent Linux VM/container host;
- PostgreSQL: managed service or persistent VM volume;
- Redis: managed service or persistent VM container;
- Qdrant: persistent VM volume;
- local-ML embeddings: model cache on the same persistent host.

The Telegram personal-account listener requires a long-running process, so the
backend must not be deployed as request-only serverless functions. A small
persistent host (for example Railway, Render background workers, Fly.io machine,
or a VPS/Yandex Cloud VM) is the correct pilot topology. Expose only the FastAPI
service through HTTPS; PostgreSQL, Redis, Qdrant, and the listener remain private.

Processes:

```text
web:      uvicorn app.main:app --host 0.0.0.0 --port $PORT
arq:      arq app.workers.worker.WorkerSettings
telegram: python -m app.workers.telegram_listener
```

Set Vercel `NEXT_PUBLIC_API_URL=https://api.example.ru`, and add the Vercel
production/preview origins to backend `CORS_ORIGINS`.

## Local development

Run frontend at `http://localhost:3000`, backend at `http://127.0.0.1:8000`, and
the Telegram listener as a second backend process. A temporary HTTPS tunnel is
acceptable for demonstrations, but not as production infrastructure.
