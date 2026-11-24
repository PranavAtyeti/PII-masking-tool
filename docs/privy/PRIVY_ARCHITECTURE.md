# Privy Architecture

Last updated: 2026-11-24

This document describes the current Privy implementation and separates it from planned roadmap work. It is intended as a source of truth for GitHub Copilot and OpenAI Codex.

Related documents:

- [Roadmap](./PRIVY_ROADMAP.md)
- [Architectural decisions](./PRIVY_DECISIONS.md)
- [Security](./PRIVY_SECURITY.md)
- [Testing](./PRIVY_TESTING.md)
- [Handover](./PRIVY_HANDOVER.md)

## Product vision

Privy is a privacy-focused, LLM-first spreadsheet and document chat application. It keeps masking, privacy policy enforcement, stored data, exact calculations, and unmasking under backend control while allowing Gemini or Groq to provide conversational and tool-orchestrated answers.

Privy is not an unrestricted local analytics engine. Local computation is deterministic and bounded, and the LLM remains responsible for orchestration and user-facing interpretation.

## Architectural principles

1. Privacy-approved data is projected locally before every provider call.
2. Original sensitive values remain local and are restored only for authorized display.
3. Excluded values are not included in prompts, retrieval, tools, uploaded provider resources, or provider results.
4. Exact calculations use bounded backend tools or complete stored datasets, not an incomplete retrieval sample.
5. Provider adapters expose provider-neutral contracts.
6. Persistent state is encrypted or bounded, chat-scoped, and ownership-checked.
7. Unknown model capabilities are not assumed.
8. Future phases must remain backward-compatible and versioned.

## Current technology stack

### Implemented

- React, TypeScript, Vite, and Tailwind CSS frontend.
- FastAPI backend.
- PostgreSQL persistence.
- Custom JWT authentication with refresh-token sessions.
- Gemini and Groq provider integrations.
- pandas, regex, Presidio, and spaCy for data processing and PII detection.
- AES-256-GCM envelope encryption.
- Local development key provider and production Cloud KMS key provider.
- CSV, XLS, XLSX, and DOCX ingestion.
- Server-Sent Events for streaming responses.
- Optional GCS ciphertext storage adapter. The adapter is implemented but is not the primary current PostgreSQL document lifecycle.

### Not implemented

- Auth0.
- Streamlit.
- DuckDB or Polars replacement.
- Unrestricted generated-code execution.
- Persistent plaintext analytical caches.
- Production sandboxed Python workspace, persistent analytics workspace, background worker infrastructure, and advanced visualization/report generation as described in later roadmap phases.

## Frontend

The frontend is a React application served by Vite. It provides chat, file attachment and privacy review, streamed responses, sources, usage metadata, model selection, authentication, and settings.

The frontend:

- Uses short-lived access tokens and HttpOnly refresh cookies for local authentication.
- Deduplicates refresh calls through a shared refresh promise.
- Retries an authenticated request once after successful refresh.
- Prevents rapid duplicate submissions with `sendingRef`.
- Displays provider-reported or estimated usage without exposing prompt contents.

Frontend implementation status is established locally, but live browser refresh behavior and deployment behavior remain to be verified.

## Backend

FastAPI owns:

- Authentication and guest sessions.
- Chat and conversation-turn lifecycle.
- Upload validation and parsing.
- PII masking and privacy-policy projection.
- Workbook and DOCX persistence.
- LLM prompt budgeting.
- Provider streaming and normalized usage metadata.
- Bounded local workbook, DOCX-table, join, and derived-result tool execution.
- Document retrieval and citations.
- Health and readiness reporting.

The backend health endpoint is a database readiness probe that returns HTTP 503 when the database probe fails.

## Database

PostgreSQL is the authoritative persistence store. SQLAlchemy and Alembic manage application schema and migrations.

The migration line has one current head: `0008_persistent_turns`. The migration depends on `0007_encrypted_workbook_document`.

Stored application entities include users, chats, chat files, chat messages, token mappings, authentication sessions, guest sessions, and conversation-turn lifecycle records. Sensitive text fields use application-layer encryption.

## Authentication

Privy uses custom local JWT authentication. Access tokens are short-lived. Refresh tokens are opaque random values whose SHA-256 hashes are stored server-side. Refresh rotates the server-side session and returns a new HttpOnly cookie.

Authentication is chat and file ownership checked for ordinary local users. Guest sessions have separate limits and ownership rules.

## File ingestion

### Excel and CSV

Supported formats are CSV, XLS, and XLSX. Spreadsheet parsing:

- Preserves complete stored worksheets.
- Normalizes duplicate and blank headers.
- Retains source-row metadata.
- Enforces row, cell, file, archive, and request limits.
- Produces a 200-row frontend preview.
- Persists complete eligible workbook data.

### DOCX

DOCX parsing is bounded and non-executing. It validates the ZIP archive, reads only supported XML components, extracts ordered headings, paragraphs, tables, headers, and footers, and stores table rows with source identities and heading paths.

Nested tables are flattened with a warning. Page numbers are not inferred. External relationships and embedded objects are not extracted.

Recent locally implemented improvements add header detection, repeated-header handling, structured DOCX table framing, and bounded exact tools.

DOCX exact calculations use complete eligible table rows. Synthetic fixtures define project acceptance; the informal 28-record experiment is not an acceptance blocker.

## Masking and unmasking

PII detection combines:

- Column-name heuristics.
- Deterministic regex for structured formats.
- Optional Presidio/spaCy NER for free text.
- Chat-scoped token mappings.

Masking replaces eligible values with tokens. The provider receives privacy-approved tokens or explicitly confirmed Share values. The response is unmasked locally before display.

The protected-value badge counts distinct token patterns in the relevant payload, not every occurrence or every mapping. This semantic is not changed without migration or acceptance documentation.

## Privacy policies

Every privacy-controlled column or document content choice is Share, Mask, or Exclude.

- Share sends original values; sensitive Share requires explicit confirmation.
- Mask sends local tokens and preserves local unmasking.
- Exclude removes values from retrieval, prompts, tool schemas, tool frames, provider uploads, and provider payloads.

Persisted policies are loaded and validated again at use time. Legacy policy formats remain readable but are treated as legacy and fail closed where a modern privacy policy is required.

## Encryption

The application uses AES-256-GCM envelope encryption.

- Each ciphertext uses a generated data encryption key.
- The data key is wrapped by the configured key provider.
- Envelope metadata identifies the local or KMS key.
- Legacy encrypted or plaintext compatibility is explicitly handled in existing code.

Local development uses `PRIVY_ENCRYPTION_KEY` and a local key provider. Production uses Cloud KMS and requires `PRIVY_KMS_KEY_NAME`; production missing or unavailable KMS configuration must fail closed.

The blind-index key `PRIVY_BLIND_INDEX_KEY` is a separate stable key for HMAC-based token lookup and is required in production.

## LLM orchestration

Prompt assembly is provider-neutral. It:

- Keeps system instructions and the current question.
- Reserves requested output tokens.
- Applies model context and output limits where configured.
- Shrinks conversation history and file context first.
- Explicitly marks context as limited.

The provider registry stores provider capabilities and model catalog entries. Unknown model limits remain unknown unless explicitly configured with verified metadata.

Gemini and Groq are stream-capable in the current implementation. Gemini also supports provider resource concepts, but workbook content is not uploaded to Gemini File Search or Code Execution in the current chat tool path.

## Provider adapters

Provider-specific adapters implement provider-neutral streaming and completion contracts. Usage normalization distinguishes:

- Provider-reported input tokens.
- Provider-reported output tokens.
- Cached input tokens.
- Reasoning/thought tokens.
- Tool-use tokens.
- Total reported tokens.
- Estimated input/output tokens when actual provider usage is unavailable.

Usage metadata is provider/model metadata only and must not contain prompts or sensitive values.

## Local analytical execution

The current implementation has bounded workbook tools and newly introduced structured DOCX table tools. These tools support:

- Schema discovery.
- Column profiling.
- Filtering.
- Counting.
- Summation.
- Average.
- Minimum and maximum.
- Distinct counts.
- Grouping and comparison.

Tools operate over complete stored eligible data. DOCX retrieval excerpts are contextual or citeable evidence, not the authoritative source for exact document-table calculations.

Tool execution is request-scoped and enforced for call count, round count, argument size, result size, execution time, row limits, group limits, privacy policy, and ownership/revision.

The shared result contract adds operation/version, exactness status, source/file revisions, source format, worksheet/table and permitted column identities, a digest of the applicable privacy policy, safe filter descriptors, record counts, warnings, bounded-execution metadata, and partial-result limitations. Existing workbook and DOCX result fields remain at the top level for compatibility. Join discovery profiles only policy-eligible columns and reports compatible inferred types, null counts, uniqueness, overlap, and observed cardinality without key values. It considers structured workbook sheets and DOCX tables; narrative paragraphs are never tables. Join execution supports one-to-one, one-to-many, and many-to-one inner joins for a unique compatible Share-permitted key pair. It validates ownership, revisions, source policies, output size, and amplification; excludes null keys and reports null/unmatched counts. Many-to-many joins fail closed.

Ambiguous relationships require an explicit UI confirmation. A short-lived database record binds the authenticated user, chat, both file/table/column identities, file revisions, and privacy-policy revisions. Confirmation and one-time consumption are authenticated and atomic. Execution revalidates the binding and source access. The opaque confirmation ID is delivered only in a backend-to-UI event, never in the LLM tool result.

The join result is held as a complete in-memory frame scoped to the current tool context, with a 32 MiB aggregate memory ceiling. `aggregate_joined_data` operates over the complete frame; `join_tables` returns only a 25-row safe sample plus exact counts and a request-scoped result reference. A subsequent join may append one new authorized source to the complete prior result, with unique Share-permitted key names, full source lineage, revision and policy revalidation, and a prohibition on repeating a source file. Repeating this operation supports three or more source tables; ambiguous chained keys fail closed. References cannot be used in another request, user, or chat. The stream cleanup hook clears derived frames on completion, error, or cancellation. Limits are 20,000 source rows, 10,000 output rows, 32 MiB aggregate intermediate memory, six tool calls, and three provider tool rounds.

`execute_analysis_plan` adds bounded sequential orchestration inside the same tool registry. A plan is limited to five registered operations, dependency depth three, and 30 seconds; the existing request tool-call and provider-round ceilings still apply. The backend rejects unknown capabilities, forward/circular dependencies, undeclared references, excessive depth, invalid step arguments, and unavailable result fields. Dependent steps use validated outputs; unrelated steps may continue after a failure. Only bounded step summaries and safe provenance are returned to the provider. Complete intermediate frames remain local to `ToolContext`, and the existing join confirmation token remains backend-verifiable and one-time.

Advanced deterministic analytics use the same registry and plan runner: whole-dataset profiling and quality counts, typed filtering with stable sorting, multi-column grouping with up to four additional aggregates and bounded ranking, selected-group comparisons with absolute and percentage change, chronological date trends, and fixed-operation cross-column calculations. Filtered and calculated frames can feed later plan steps through validated request-scoped references. Numeric aggregates use Decimal; derived division and percentage change disclose rounding and exactness, and zero-baseline percentage change is null. Trend periods are day, Monday-based week, month, quarter, or year; missing and invalid dates are counted separately. Group cardinality, samples, output size, execution time, and intermediate memory are bounded. Profiling returns statistics and quality descriptors without raw cell values. All operations honor the current privacy projection and source revision/policy checks.

Custom calculations are limited to an explicit operation set; arbitrary code execution is not implemented. No persistent analytical results or plaintext caches are created.

## Document retrieval and citations

DOCX retrieval ranks bounded lexical sections and table rows. It returns only privacy-approved excerpts, stable source references, and heading paths. Citations identify source IDs and never infer page numbers.

Retrieval is not a vector database or persistent semantic index. It is a bounded local lexical process over encrypted stored document representations.

Broad retrieval may include more table rows for complete-table questions, but exact values and calculations must come from structured tools where available.

## Analytical workspace

No persistent analytical workspace or derived assets are implemented. Request-scoped plans, joins, and local calculations over complete intermediate results are available. Conversation follow-up still relies on prior chat text and fresh authorized tool calls; complete analytical frames are not persisted or reused across requests. Ambiguous initial joins use short-lived confirmation state only; chained joins with ambiguous keys stop and require a unique relationship. Later phases may add persistent derived assets, saved analyses, version-aware notebooks or reports, and workspace organization. Such state must remain privacy-bound and must not create plaintext caches.

Migration `0009_join_confirmations` adds only the short-lived confirmation records. This is required to make approval survive the discovery request and to enforce expiry and one-time consumption atomically across backend workers. No joined data or plaintext analytics result is persisted. The migration file was added but not executed against a database in this task.

## File versioning and caching

Current implementation:

- Chat files have persisted update revisions.
- Tool contexts refresh files when revision timestamps change.
- Request context revisions include attached-file revisions.
- Conversation-turn idempotency and stale-turn recovery are persistent.
- There is no persistent plaintext workbook or DOCX cache.

Planned capabilities include explicit workbook/document versions, cross-file join versions, derived result versions, invalidation manifests, and cache retention policies.

## Background execution

Not implemented as an application worker system. Conversation turns persist interrupted/in-progress state and support retry recovery. The system does not currently provide a durable long-running job queue or background task worker.

Phase 6 or later work must introduce background execution only after an approved bounded design.

## Visualization and report generation

Not implemented. Provider-adaptive or local charting and report generation are future roadmap work. Any implementation must preserve exact tool results, privacy policy projection, and source attribution.

## External storage

PostgreSQL currently stores application entities and ciphertext representations. An optional GCS ciphertext adapter exists in `storage.py`; it encrypts bytes before GCS upload and decrypts after download in application memory.

GCS is not enabled as a mandatory architectural dependency for the current workbook/DOCX lifecycle. The optional adapter requires separate GCS configuration and package support.
