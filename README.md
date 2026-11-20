# Privy

Privy is a privacy-focused AI chat application for conversations and spreadsheet analysis. It detects and tokenizes sensitive data before sending a prompt to a configured LLM provider, then restores the original values only in the response shown to the user.

The supported application is a React frontend, a FastAPI backend, and PostgreSQL.

## How it works

1. A user starts a chat and can attach CSV, XLS, or XLSX files. Excel uploads include every worksheet.
2. The backend preserves worksheet order and source rows, normalizes blank or duplicate headers, identifies likely sensitive columns, and masks selected values locally.
3. Structured values such as email addresses, phone numbers, PANs, Aadhaar-like numbers, card numbers, and IP addresses are found with deterministic patterns. Optional Presidio/spaCy NER adds coverage for names and locations in free text.
4. The selected Share/Mask/Exclude policy is persisted with the file. A shared privacy projection enforces that policy for local workbook tools and both providers: excluded columns are unavailable, masked values remain tokenized, and sensitive Share choices require explicit confirmation.
5. The streamed model response is unmasked for display. Token mappings, chats, messages, and file metadata are stored in PostgreSQL.

Privy is a mitigation layer, not a certified anonymization or compliance solution. Review sensitive free text carefully: NER can miss entities, especially outside English or with unusual phrasing.

## Architecture

```text
React + Vite + custom JWT authentication
          |
          | /api
          v
FastAPI
  |-- authentication and guest sessions
  |-- chat, upload, and streaming-message APIs
  |-- regex + optional Presidio/spaCy masking
  |-- provider-independent, bounded workbook tools
  |-- Groq and Gemini streaming adapters
          |
          +--> PostgreSQL
          |
          +--> configured LLM provider
```

## Requirements

- Python 3.10+
- Node.js 18+
- PostgreSQL
- A configured `PRIVY_JWT_SECRET` for local JWT authentication
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

Edit `backend/.env` and set the PostgreSQL URL, `PRIVY_JWT_SECRET`, and at least one provider key. Then apply the schema migrations:

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
npm run dev
```

The development UI runs at `http://localhost:5173` and proxies `/api` requests to FastAPI on port 8000.

## Backend environment variables

`backend/.env.example` contains safe placeholders for the required settings.

| Variable | Purpose |
| --- | --- |
| `DATABASE_URL` | PostgreSQL SQLAlchemy connection URL. |
| `PRIVY_JWT_SECRET`, `PRIVY_JWT_ALGORITHM` | Signing secret and algorithm for local JWT authentication. |
| `ALLOWED_ORIGINS` | Comma-separated frontend origins allowed by CORS. |
| `LLM_PROVIDER` | Default provider: `groq` or `gemini`. |
| `GROQ_API_KEY`, `GROQ_MODEL` | Groq credentials and default model. |
| `GEMINI_API_KEY`, `GEMINI_MODEL` | Gemini credentials and default model. |
| `MODEL_TEMPERATURE` | Default generation temperature. |
| `PRIVY_GUEST_MAX_FILES`, `PRIVY_GUEST_MAX_FILE_SIZE_MB` | Guest attachment limits. |
| `PRIVY_MAX_FILE_SIZE_BYTES`, `PRIVY_MAX_FILES_PER_CHAT` | Per-file byte limit and maximum attachments per chat. Defaults: 10 MiB and 10 files. |
| `PRIVY_MAX_ROWS_PER_FILE`, `PRIVY_MAX_CELLS_PER_FILE` | Hard spreadsheet parsing limits. Defaults: 100,000 rows and 2,000,000 cells. |
| `PRIVY_MAX_XLSX_ZIP_ENTRIES`, `PRIVY_MAX_XLSX_UNCOMPRESSED_BYTES`, `PRIVY_MAX_XLSX_ENTRY_BYTES`, `PRIVY_MAX_XLSX_COMPRESSION_RATIO` | XLSX archive-bomb protections. |
| `PRIVY_MAX_CONTEXT_TOKENS_PER_FILE`, `PRIVY_MAX_TOTAL_FILE_CONTEXT_TOKENS` | Per-file and per-request masked-data context budgets. Defaults: 90,000 and 110,000 tokens. |
| `PRIVY_MAX_HISTORY_MESSAGES` | Recent turns preserved for conversational continuity. Default: 32. |
| `PRIVY_CONCISE_MAX_OUTPUT_TOKENS`, `PRIVY_DETAILED_MAX_OUTPUT_TOKENS` | Response budgets. Defaults: 2,048 and 16,384 tokens. |
| `PRIVY_MODEL_METADATA` | Optional JSON keyed by registered model ID to configure verified `context_window_tokens`, `max_output_tokens`, and known capability overrides. Unconfigured limits remain unknown. |
| `PRIVY_PROVIDER_OVERHEAD_TOKENS` | Approximate allowance for provider request overhead during prompt budgeting. Default: 256. |
| `PRIVY_STREAM_USAGE_REPORTING` | Request usage data in Groq-compatible streaming responses when supported. Default: true. |
| `PRIVY_MAX_TOOL_CALLS_PER_REQUEST`, `PRIVY_MAX_TOOL_ROUNDS` | Maximum local workbook tool calls and provider tool-call rounds. |
| `PRIVY_MAX_FILTER_CONDITIONS`, `PRIVY_MAX_FILTER_COLUMNS`, `PRIVY_MAX_TOOL_ROWS`, `PRIVY_MAX_TOOL_GROUPS` | Bounds for local filter, row, and grouping results. |
| `PRIVY_MAX_TOOL_RESULT_BYTES`, `PRIVY_MAX_TOOL_ARGUMENT_BYTES`, `PRIVY_MAX_TOOL_EXECUTION_SECONDS` | Limits for tool payload size and execution duration. |

Never commit populated `.env` files or provider credentials. An administrator can manage the active model configuration in the application; the API never returns a full stored key.

## Privacy behavior

- Masking happens in the backend before provider calls.
- Prompt construction budgets the current question and recent history centrally. Workbook schemas and exact calculations are requested through bounded local tools; complete worksheet rows are not serialized into the provider prompt.
- Token-to-original mappings are kept in PostgreSQL and scoped to a chat.
- Column suggestions can be reviewed before a file is persisted as masked context.
- Workbook privacy review applies to every worksheet, including sheets with repeated headers; hidden and empty sheets are identified in the review.
- CSV identifiers are read as text so leading zeroes are preserved. XLSX formula cells are retained as formulas; Privy does not recalculate formulas.
- Upload limits can be configured with `PRIVY_MAX_FILE_SIZE_BYTES`, `PRIVY_MAX_FILES_PER_CHAT`, `PRIVY_MAX_ROWS_PER_FILE`, `PRIVY_MAX_CELLS_PER_FILE`, and the `PRIVY_MAX_XLSX_*` archive settings. Files exceeding limits are rejected rather than truncated.
- NER is an optional **Enhanced free-text PII scan** when attaching a file with selected non-structured columns. It is disabled by default to keep large uploads responsive; regex detection remains active even when NER is disabled.
- Complete workbook documents are stored locally in encrypted PostgreSQL fields. Local tools use the full stored dataset, while only compact, privacy-checked tool results are returned to the selected model.
- Workbook files are not uploaded to Gemini File Search or Code Execution. Gemini and Groq receive the same provider-independent tool definitions and results.

## Conversation context and tool-assisted analysis

Privy keeps up to 32 recent messages for continuity by default. Local workbook interactions are limited by tool-call, round, argument, result, row, group, and execution-time budgets. Output budgets are up to 2,048 tokens for concise answers and 16,384 for detailed answers; configured model context limits are checked again before each tool-call round.

These are application budgets, not a guarantee that every configured model supports the resulting request. Lower the values in `backend/.env` if a provider reports context-window, latency, or cost issues.

For spreadsheet questions, the model can request schema discovery, column profiles, filtering, exact aggregations, grouping, and group comparisons through the shared local tool registry. Calculations run against the complete stored dataset; tool arguments and results are validated and bounded before the provider interaction continues. The provider remains responsible for understanding the question and explaining the result.

## Project structure

```text
frontend/                   React, Vite, Tailwind, custom JWT UI
backend/
  app/                      FastAPI application and masking pipeline
    tools.py                 Policy-aware local workbook tool registry and executor
    workbook.py              Spreadsheet ingestion and privacy projection
    provider_registry.py     Provider capabilities and model metadata
    logging_utils.py          Structured, PII-safe timing and event logging
  migrations/               Alembic PostgreSQL migrations
  requirements.txt          Backend runtime dependencies
  requirements-postgres.txt PostgreSQL, SQLAlchemy, and Alembic dependencies
  .env.example              Safe backend configuration template
  .env.postgres.example     PostgreSQL configuration reference
```

## Development checks

```powershell
Set-Location frontend
npm run build
```

Run the backend tests with `python -m unittest discover -s backend\\tests` and the frontend production/type-check build with `npm run build` from `frontend`.

## License

MIT — see [LICENSE](LICENSE).
