# 2414 Donor Chat

Technical documentation for the `gloo-2414` application. The application name presented to users is **2414 Donor Chat**.

## Overview

2414 Donor Chat is a FastAPI-powered conversational assistant for 24:14 World. It helps users:

- Ask questions grounded in the organisation's Gloo knowledge collection.
- Discover ministries by category, location, name, or ministry code.
- Learn about available donation funds and receive donation links.
- Continue multi-turn ministry and donation conversations through a session ID.
- Use a browser-based conversation tester served by the same application.

The service combines deterministic routing and validation with Gloo's search-grounded chat APIs. Ministry records are read from `app/Ministry_details.csv`; fund definitions and routing rules are maintained in Python modules.

## Architecture

### Main components

```text
Browser / API client
        |
        v
FastAPI application (backend/main.py)
        |
        +--> Request ID middleware and logging
        +--> Conversation store (in-memory session state)
        +--> Deterministic input guards and intent routing
        |       +--> Ministry lookup (CSV-backed)
        |       +--> Donation/fund flow
        |       +--> General RAG flow
        |
        +--> Gloo data search API
        +--> Gloo chat completions API
        +--> Optional Loki logging
```

### Request flow

1. The client calls `POST /api/chat` with a message and optional `session_id`.
2. Middleware creates or propagates an `X-Request-ID` and logs request start/completion.
3. The service obtains the conversation state and appends the user message to bounded history.
4. Early deterministic routes handle donation selections, unsupported creation requests, underspecified input, and gibberish where possible.
5. Remaining requests go through intent classification. The classifier may use the Gloo chat endpoint; deterministic routing is used as a fallback if classification fails.
6. The selected flow performs one of the following:
   - Ministry lookup against the local CSV data.
   - Donation/fund guidance and construction of safe donation URLs.
   - Gloo search followed by a Gloo chat completion using retrieved context.
7. The response is normalized into the public response shape, conversation state is updated, and the assistant message is stored.
8. The client receives the response, including suggestions, links, citations when enabled, and the session ID.

For grounded RAG requests, the backend calls `https://platform.ai.gloo.com/ai/v1/data/search` and then `https://platform.ai.gloo.com/ai/v2/chat/completions`. Search requests use the `GlooProd` collection and a tenant derived from `PUBLISHER_NAME`.

## Modules

| Module | Responsibility |
| --- | --- |
| `app/backend/main.py` | FastAPI application, middleware, endpoint definitions, routing, ministry and donation conversation flows, and static frontend serving. |
| `app/backend/rag_service.py` | Gloo authentication, search-grounded chat, chat-completion helpers, URL/citation normalization, response shaping, and donation constants. |
| `app/backend/intent_service.py` | Deterministic intent detection, optional LLM-based intent classification, slot validation, and fallback behavior. |
| `app/backend/ministry_service.py` | Loads and searches `app/Ministry_details.csv`; matches ministry names, codes, categories, and locations. |
| `app/backend/fund_service.py` | Defines supported fund profiles and resolves fund names, codes, and interest-based suggestions. |
| `app/backend/conversation_service.py` | Thread-safe in-memory session store, conversation slots, bounded history, TTL eviction, and per-session locks. |
| `app/backend/prompts.py` | System prompts and canonical welcome, clarification, and unsupported-request messages. |
| `app/backend/app_loggers.py` | Console logging, request IDs, and optional asynchronous Loki logging. |
| `app/frontend/static/index.html` | Browser conversation tester; sends same-origin requests to `/api/welcome` and `/api/chat`. |
| `app/uploaders/` | Utilities for uploading ministry data and verifying uploads; not part of the request path. |

## Security model

- Gloo credentials are read from environment variables and sent as a Bearer token in outbound requests. Secrets must not be committed to source control or exposed in the frontend.
- Required startup configuration is validated in `rag_service.py`: `GLOO_API_KEY` (or legacy `API_KEY`), `PUBLISHER_ID`, and `PUBLISHER_NAME`.
- Pydantic validates the chat request body. The backend also bounds stored message content to 4,000 characters and keeps only the latest 10 history messages per session.
- Outbound resource links are normalized and restricted to valid HTTP(S) URLs. The frontend opens links with `noopener noreferrer`.
- CORS is configured for `https://2414ai.thearkofrevival.com`; local development through the same-origin tester does not require CORS.
- Requests carry a response `X-Request-ID` for tracing. If Loki is configured, logs are queued and pushed with a two-second timeout so logging does not block requests.

Current limitations to account for in production:

- The API endpoints do not implement user authentication or authorization.
- Conversation state is process-local. Multiple replicas or worker processes will not share sessions.
- Request messages are logged by the current implementation; review logging and redact sensitive data before exposing the service to sensitive inputs.
- The Dockerfile health check targets `/health`, while the application exposes `/api/health`; align these paths before relying on the container health check.

## Caching strategy

There is no general-purpose response cache for Gloo search results, chat completions, or API responses. Each eligible RAG request performs fresh upstream calls.

The application does maintain bounded in-memory conversation state:

- Session idle TTL: **30 minutes** (`SESSION_TTL_SECONDS`).
- Maximum sessions per process: **10,000** (`SESSION_MAX_COUNT`). Oldest sessions are trimmed when the cap is exceeded.
- Maximum stored history: **10 messages** (`MAX_HISTORY_MESSAGES`).
- Maximum stored message content: **4,000 characters** (`MAX_MESSAGE_CONTENT_LENGTH`).
- Per-session locks serialize concurrent turns for the same session; different sessions can run concurrently.

This state is an interaction cache, not a durable database. A process restart, deployment, or request routed to another replica can lose the conversation. Redis or another shared store is required for durable, multi-worker session continuity.

## API reference

Base URL for local development: `http://127.0.0.1:8000`

### `GET /api`

Returns a basic service description.

Example response:

```json
{
  "message": "Gloo Grounded RAG API Running",
  "publisher_name": "..."
}
```

### `GET /api/health`

Returns application health and whether the Gloo API key is configured.

```json
{
  "status": "ok",
  "publisher_name": "...",
  "api_key_configured": true
}
```

### `GET /api/ready`

Returns `{ "ready": true }`.

### `GET /api/live`

Returns `{ "live": true }`.

### `GET /api/welcome`

Returns the welcome message and initial quick replies. The response includes `id`, `sender`, `timestamp`, `message`, and `suggestions`.

### `POST /api/chat`

Request body:

```json
{
  "message": "Help me find a ministry to support",
  "session_id": "optional-client-session-id"
}
```

`question` may be used as an alias for `message`, and `sessionId` as an alias for `session_id`.

Typical response body:

```json
{
  "id": "...",
  "sender": "bot",
  "timestamp": "2026-10-30T00:00:00+00:00",
  "message": "...",
  "status": "answered",
  "suggestions": [{"label": "...", "message": "..."}],
  "urls": ["https://..."],
  "citations": [],
  "links": [{"label": "...", "url": "https://..."}],
  "session_id": "...",
  "ministries": []
}
```

`status` can indicate an answered response, a clarification request, an unsupported request, or a no-result outcome. `citations` are included only when `EXPOSE_CITATIONS` is enabled.

### `GET /`

Serves the browser conversation tester from `app/frontend/static/index.html` (or a colocated `index.html` when present).

### `GET /favicon.ico`

Returns `204 No Content`.

Interactive API documentation is also available from FastAPI at `/docs` and `/openapi.json` when the service is running.

## Configuration and environment variables

Create a `.env` file in `app/` for local development, or inject these values through the deployment environment.

| Variable | Required | Description |
| --- | --- | --- |
| `GLOO_API_KEY` | Yes | Bearer token for Gloo APIs. `API_KEY` is accepted as a legacy fallback. |
| `API_KEY` | Fallback | Legacy name used only when `GLOO_API_KEY` is not set. |
| `PUBLISHER_ID` | Yes | Gloo publisher identifier. |
| `PUBLISHER_NAME` | Yes | Publisher name; also used to derive the Gloo search tenant. |
| `MODEL` | No | Gloo model name. Defaults to `gloo-openai-gpt-5.6-luna`. |
| `EXPOSE_CITATIONS` | No | Set to `true`, `1`, `yes`, or `on` to include grounded citation titles and links. Defaults to `false`. |
| `LOKI_URL` | No | Loki endpoint for asynchronous log shipping. |
| `LOKI_USERNAME` | No | Optional Loki username. |
| `LOKI_PASSWORD` | No | Optional Loki password. |

The local ministry dataset is `app/Ministry_details.csv`. Donation links and the donor registration/tutorial URLs are defined in `rag_service.py`.

## Running the app

### Local command (PowerShell)

```powershell
cd ark-ml-model/gloo-2414/app
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -r requirements.txt

$env:GLOO_API_KEY = "replace-with-gloo-api-key"
$env:PUBLISHER_ID = "replace-with-publisher-id"
$env:PUBLISHER_NAME = "replace-with-publisher-name"

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

Open `http://127.0.0.1:8000/` for the conversation tester or `http://127.0.0.1:8000/docs` for Swagger UI.

### Local command (macOS/Linux)

```bash
cd ark-ml-model/gloo-2414/app
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt

export GLOO_API_KEY="replace-with-gloo-api-key"
export PUBLISHER_ID="replace-with-publisher-id"
export PUBLISHER_NAME="replace-with-publisher-name"

python -m uvicorn backend.main:app --reload --host 127.0.0.1 --port 8000
```

### Container command

The image uses Python 3.11, installs the requirements and Playwright Chromium, exposes port `8000`, and starts with:

```bash
uvicorn backend.main:app --host 0.0.0.0 --port 8000
```

The Helm chart is under `build/`. Its local values configure one replica, resource requests/limits, the image, and the Kubernetes node selector; secrets and required application environment variables should be supplied by the deployment platform.

## Daily website PDF crawler

`app/uploaders/crawler.py` replaces the former crawler manifest/upload workflow. It does not use Qdrant, FastEmbed, embeddings, or local text chunking. Each run combines the four configured listing pages with `/sitemap.xml` (including sitemap indexes), then rechecks every URL in SQLite state. A SHA-256 hash of deterministic semantic HTML content controls version creation. A version is committed only after the verified Gloo upload succeeds; prior PDFs and history are retained.

The current deployment has no shared database, so the default single-host state is `app/uploaders/crawl_state.sqlite3`. Do not put this SQLite file on a shared volume for multiple replicas; migrate `StateStore` to the shared database before scaling the crawler horizontally. The process lock is the adjacent `.lock` file.

Commands:

```bash
cd ark-ml-model/gloo-2414/app
python uploaders/crawler.py --mode full
python uploaders/crawler.py --mode daily
python uploaders/crawler.py --mode single --url https://www.2414world.com/blog/example
python uploaders/crawler.py --mode daily --dry-run
```

The upload adapter intentionally uses only the contract already present in `app/uploaders/web_script.py`: `POST https://platform.ai.gloo.com/ingestion/v2/files`, Bearer authentication, `publisher_id`, `producer_id`, and multipart `files`. The returned JSON is retained as a normalized storage identifier when it contains an `id`, `file_id`, `item_id`, or `producer_id`. No undocumented Gloo metadata or storage-listing semantics are assumed.

The example daily cron entry is `deploy/2414world-crawler.cron`, scheduled at 02:00 in `Asia/Calcutta`; install it in the deployment environment rather than on a developer workstation. Redirect stdout/stderr to a managed rotating log location in production.
