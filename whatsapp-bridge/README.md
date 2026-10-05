# Personal WhatsApp bridge (experimental)

This local sidecar uses [`whatsmeow`](https://github.com/tulir/whatsmeow) to link one WhatsApp Web companion session per Autopilot channel. The QR string is returned to the authenticated Autopilot API, which lets the frontend render it. New direct text messages are forwarded to the existing WhatsApp ingestion pipeline; outgoing replies use the same channel's session.

The bridge binds only to `127.0.0.1`. Its control endpoints require a bearer token, and its inbound callback uses a separate bearer token. It stores WhatsApp device credentials under one SQLite file per channel, with the data directory restricted to the current OS user. It does not store conversation history and intentionally ignores group, broadcast, media-only, and self-sent messages. Keep this folder on a local disk with restricted access and back it up only as carefully as an account credential. The SQLite session database is not encrypted by the bridge.

## Run locally

Install Go 1.26 or newer and a C compiler (the SQLite driver is CGO based). Create two independent random secrets and set only environment variables, never source control:

```powershell
$env:WHATSAPP_BRIDGE_TOKEN = '<random-control-token>'
$env:WHATSAPP_BRIDGE_CALLBACK_TOKEN = '<different-random-callback-token>'
$env:WHATSAPP_BRIDGE_CALLBACK_URL = 'http://127.0.0.1:8000/api/v1/channels/webhook/whatsapp/personal'
$env:WHATSAPP_BRIDGE_DATA_DIR = '.\data\whatsapp-personal'
go run .
```

Set matching `WHATSAPP_PERSONAL_BRIDGE_TOKEN` and `WHATSAPP_PERSONAL_CALLBACK_TOKEN` in the backend environment. `WHATSAPP_PERSONAL_BRIDGE_URL` defaults to `http://127.0.0.1:8092`. Start the bridge and backend on the same machine, open the personal-account QR connection in Autopilot, and scan it from WhatsApp's linked-device screen.

The bridge is a prototype for local testing. `whatsmeow` implements an unofficial WhatsApp Web protocol; availability and account policy are controlled by WhatsApp, so this must not be presented as an official WhatsApp integration. The project links the upstream library and does not copy code from the `whatsapp-mcp` repository. The `whatsmeow` library is MPL-2.0; see its upstream repository for license terms.
