# Privy

Privy is a privacy-focused AI chat application for conversations and spreadsheet analysis. It detects and tokenizes sensitive data before sending a prompt to a configured LLM provider, then restores the original values only in the response shown to the user.

The supported application is a React frontend, a FastAPI backend, and PostgreSQL.

## How it works

1. A user starts a chat and can attach CSV, XLS, or XLSX files.
2. The backend reads the file, identifies likely sensitive columns, and masks enabled values locally.
3. Structured values such as email addresses, phone numbers, PANs, Aadhaar-like numbers, card numbers, and IP addresses are found with deterministic patterns. Optional Presidio/spaCy NER adds coverage for names and locations in free text.
4. Tokenized file context, message text, and recent chat history are sent to the selected model. A residual-PII check blocks payloads that still contain high-confidence structured PII.
5. The streamed model response is unmasked for display. Token mappings, chats, messages, and file metadata are stored in PostgreSQL.

Privy is a mitigation layer, not a certified anonymization or compliance solution. Review sensitive free text carefully: NER can miss entities, especially outside English or with unusual phrasing.

## Architecture

```text
React + Vite + Auth0
          |
          | /api
          v
FastAPI
  |-- authentication and guest sessions
  |-- chat, upload, and streaming-message APIs
  |-- regex + optional Presidio/spaCy masking
  |-- model-provider client (Groq or Gemini)
          |
          +--> PostgreSQL
          |
          +--> configured LLM provider
```

## Requirements

- Python 3.10+
- Node.js 18+
- PostgreSQL
- An Auth0 single-page application and API
- At least one LLM provider API key: Groq or Gemini
- The spaCy `en_core_web_sm` model when NER is enabled

## Setup

### 1. Configure PostgreSQL

Create a database and user, then set a connection string such as:

```text
DATABASE_URL=postgresql+psycopg://privy:privy@localhost:5432/privy
```

### 2. Configure the backend

From the repository root on Windows:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r backend\requirements.txt -r backend\requirements-postgres.txt
python -m spacy download en_core_web_sm
Copy-Item backend\.env.example backend\.env
```

Edit `backend/.env` and set the PostgreSQL URL, Auth0 settings, and at least one provider key. Then apply the schema migrations:

```powershell
Set-Location backend
alembic upgrade head
uvicorn app.main:app --reload --port 8000
```

The API health endpoint is available at `http://localhost:8000/api/health`.

### 3. Configure the frontend

In a second terminal:

```powershell
Set-Location frontend
npm ci
Copy-Item .env.example .env
npm run dev
```

Set these values in `frontend/.env`:

```text
VITE_AUTH0_DOMAIN=your-tenant.us.auth0.com
VITE_AUTH0_CLIENT_ID=your_auth0_spa_client_id
VITE_AUTH0_AUDIENCE=https://privy-api
```

The development UI runs at `http://localhost:5173` and proxies `/api` requests to FastAPI on port 8000.

## Backend environment variables

`backend/.env.example` contains safe placeholders for the required settings.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL SQLAlchemy connection URL. |
| `AUTH0_DOMAIN` | Auth0 tenant domain for JWT validation. |
| `AUTH0_AUDIENCE` | Auth0 API audience. |
| `ALLOWED_ORIGINS` | Comma-separated frontend origins allowed by CORS. |
| `LLM_PROVIDER` | Default provider: `groq` or `gemini`. |
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq credentials and default model. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini credentials and default model. |
| `MODEL_TEMPERATURE` | Default generation temperature. |
| `PRIVY_GUEST_MAX_FILES`, `PRIVY_GUEST_MAX_FILE_SIZE_MB` | Guest attachment limits. |
| `PRIVY_MAX_CONTEXT_TOKENS_PER_FILE`, `PRIVY_MAX_TOTAL_FILE_CONTEXT_TOKENS` | Per-file and per-request masked-data context budgets. Defaults: 40,000 and 100,000 tokens. |
| `PRIVY_MAX_HISTORY_MESSAGES` | Recent turns preserved for conversational continuity. Default: 32. |
| `PRIVY_CONCISE_MAX_OUTPUT_TOKENS`, `PRIVY_DETAILED_MAX_OUTPUT_TOKENS` | Response budgets. Defaults: 600 and 4,000 tokens. |

Never commit populated `.env` files or provider credentials. An administrator can manage the active model configuration in the application; the API never returns a full stored key.

## Privacy behavior

- Masking happens in the backend before provider calls.
- Token-to-original mappings are kept in PostgreSQL and scoped to a chat.
- Column suggestions can be reviewed before a file is persisted as masked context.
- NER is an optional **Enhanced free-text PII scan** when attaching a file with selected non-structured columns. It is disabled by default to keep large uploads responsive; regex detection remains active even when NER is disabled.
- Large attachments are stored as masked CSV context and limited again before inclusion in a model request. The model must not be assumed to see every row when context is truncated.

## Project structure

```text
frontend/                   React, Vite, Tailwind, Auth0 UI
backend/
  app/                      FastAPI application and masking pipeline
  migrations/               Alembic PostgreSQL migrations
  requirements.txt          Backend runtime dependencies
  requirements-postgres.txt PostgreSQL, SQLAlchemy, and Alembic dependencies
  .env.example              Safe backend configuration template
  .env.auth0.example        Auth0 configuration reference
  .env.postgres.example     PostgreSQL configuration reference
```

## Development checks

```powershell
Set-Location frontend
npm run build
```

The repository currently has limited automated test coverage. Add tests for masking round trips, upload validation, authentication boundaries, and database persistence before relying on changes in production.

## License

MIT — see [LICENSE](LICENSE).
