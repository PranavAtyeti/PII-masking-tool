# Privy Testing

Last updated: 2026-11-24

## Backend testing

Backend tests are Python `unittest` suites under `backend/tests/`.

Existing suites cover:

- Workbook parsing and privacy policies.
- Masking, token mapping, and encryption.
- Conversation-turn idempotency and stale-turn recovery.
- Provider usage normalization and tool loops.
- DOCX parsing, retrieval, privacy, and persistence.
- KMS and local key behavior.
- Upload and parser security.
- Provider error categories and tool limits.

Use the standard unittest discovery command:

```powershell
Set-Location backend
python -m unittest discover -s tests
```

Tests use mocks for external providers and do not require live API keys.

## Frontend testing

There is no separate automated frontend test runner currently established. Current verification is TypeScript compilation and Vite production build:

```powershell
Set-Location frontend
npm run build
```

Future frontend coverage should include authentication refresh, simultaneous 401 handling, duplicate submission, stream abort, source/citation display, and usage metadata.

## Excel and DOCX fixtures

Excel fixtures should cover:

- CSV/XLS/XLSX parsing.
- Multi-sheet workbooks.
- Duplicate and blank headers.
- Large permitted datasets.
- Malformed or oversized files.
- Privacy policies and 200-row preview behavior.

DOCX fixtures should cover:

- Headings and paragraphs.
- Tables and repeated headers.
- Data-first tables.
- Uneven and malformed tables.
- Multiple tables and sections.
- Headers and footers.
- Citations and source references.
- Mask, Share, and Exclude policies.
- Sensitive Share confirmation.
- Duplicate PII values.

The informal 28-record DOCX experiment is not a project acceptance requirement. Synthetic fixtures define acceptance and must not be described as the original experiment.

## Analytical accuracy tests

Tests must verify synthetic exact tool values against complete stored data, not against retrieval excerpts. Join discovery and execution tests cover compatibility, null counts, uniqueness, overlap, one-to-one, one-to-many, and many-to-one cardinality, many-to-many rejection, output limits, excluded keys, authorization, and provenance. Confirmation tests cover owner/chat binding, expiry, reuse, stale source revisions, changed privacy policies, and exact-candidate matching. Cross-format tests include DOCX-table-to-workbook joins and prove narrative sections are not exposed as tables. Derived calculations use the complete in-memory join result, not the bounded LLM sample; cleanup is verified.

## Privacy and authorization tests

Required tests include:

- Mask, Share, and Exclude projection.
- Sensitive Share confirmation.
- Excluded values absent from prompts/tools/results.
- Chat and file ownership.
- Encrypted persistence and blind-index lookup.
- PII absent from logs and usage metadata.
- KMS/local key behavior.
- Mixed workbook/DOCX tool isolation.

## Provider tests

Use mocked providers to verify:

- Gemini and Groq streaming contracts.
- Tool calls and multi-round limits.
- Usage normalization.
- Timeout, quota, context, authentication, and provider failures.
- Retry bounds and idempotency.
- Interrupted and stale turns.
- No PII or excluded data in provider payloads.

Live provider tests are separate and require API credentials.

## Sandbox tests

Sandboxed generated-code execution is not implemented. Future tests must include:

- AST/import validation.
- Network and filesystem escape attempts.
- Resource exhaustion.
- Timeout and output limits.
- Privacy-policy leakage checks.
- Result validation and source provenance.

## Performance tests

Planned benchmarks include:

- Upload and parse time.
- DOCX extraction and retrieval time.
- Prompt construction time.
- Tool execution time.
- Provider response time.
- Input/output/cached/reasoning/tool tokens.
- Large-permitted workbook and DOCX processing.

No benchmark claim should be made without a measured result.

## Migration verification

Use:

```powershell
Set-Location backend
alembic history
alembic heads
```

For PostgreSQL-compatible verification, use an isolated database and `alembic upgrade head`.

Do not delete or recreate persistent databases for routine validation.

## Reported baseline

The following values are previously reported from earlier local sessions. They are not being claimed as newly executed verification in this documentation task.

- Backend: 83 tests passed.
- Frontend TypeScript/build: passed.
- Python compilation: passed.
- Alembic: single head `0008_persistent_turns`.
- `git diff --check`: passed.
- Live PostgreSQL, Gemini, Groq, and Cloud KMS were not confirmed in the latest documentation pass.

## Prompt 2 verification

- Backend: 89 tests passed with `python -m unittest discover -s tests`.
- Focused join, confirmation, and DOCX suites: 36 tests passed.
- Frontend TypeScript and Vite build: passed.
- Python compilation passed.
- Alembic reports one head: `0009_join_confirmations`.
- `git diff --check` passed.
- The migration was not executed; live PostgreSQL and provider behavior remain unverified.
