# Privy Security

Last updated: 2026-11-24

This document describes mandatory security principles and distinguishes controls already present from planned safeguards.

## Core principle

Privy is a privacy mitigation layer. It is not a guarantee of complete anonymization or compliance certification. PII detection can miss entities, especially in unstructured text.

## Existing local-first privacy controls

- PII detection is performed in the backend using regex, column heuristics, optional Presidio, and spaCy.
- Sensitive values are replaced with chat-scoped tokens before provider payload construction.
- Original values are restored only locally for display.
- Share, Mask, and Exclude choices are persisted and validated again before use.
- Sensitive Share requires explicit confirmation.
- Excluded columns and document content are removed from provider prompts and tool contexts.
- Outbound text has a final privacy validation barrier.
- Masked document and workbook content is not uploaded to Gemini or Groq as raw complete data in the current tool path.

## Existing encryption

- Stored sensitive text uses AES-256-GCM envelope encryption.
- Each record uses a generated data encryption key.
- Data keys are wrapped by a local development provider or Cloud KMS provider.
- Token lookup uses a separate keyed HMAC blind index.
- Legacy plaintext compatibility is intentionally limited and documented.

Local development:

- Requires `PRIVY_ENCRYPTION_KEY`.
- Uses `LocalDevelopmentKeyProvider`.
- May use `PRIVY_BLIND_INDEX_KEY`; otherwise it falls back to the development encryption key for the blind index.

Production:

- Requires `PRIVY_ENVIRONMENT=production`.
- Requires `PRIVY_KMS_KEY_NAME`.
- Cloud KMS operations fail closed.
- Requires `PRIVY_BLIND_INDEX_KEY`.
- Requires a production JWT secret of at least 32 bytes.

## Ownership isolation

Existing controls include:

- Chat ownership checks against the authenticated user.
- File ownership checks before retrieval and tool execution.
- Chat-scoped token mappings.
- Request identity and chat-turn ownership checks.
- Conversation-turn uniqueness and stale/busy state protection.
- Refresh sessions tied to local users.
- Guest-session separation.

The application must not expose or mutate a chat/file without proving ownership in the same request path.

## Tool privacy

Every tool call must:

- Confirm chat and file ownership.
- Refresh authorized file state and detect revisions.
- Load and validate the privacy policy.
- Reject excluded or unconfirmed sensitive Share values.
- Return only bounded privacy-safe result fields.
- Run final PII validation over serialized tool output.
- Enforce call, round, argument, result, row, group, and time limits.

Join discovery considers policy-eligible columns in workbook sheets and structured DOCX tables; it does not convert document paragraphs into tables. Matching names are candidate signals, not proof of a relationship; type compatibility, null frequency, uniqueness, overlap, cardinality, and bounded source sizes also inform its output. Join execution rechecks ownership, revisions, policies, key types, cardinality, output size, and amplification. It accepts one-to-one, one-to-many, and many-to-one joins and rejects many-to-many joins. Ambiguous joins require an authenticated UI confirmation bound to the exact source/table/key and policy revisions. The one-time record is consumed atomically; provider tool arguments cannot create or broaden consent.

Complete joined frames remain in request-scoped process memory. Their total memory is capped at 32 MiB, sources at 20,000 rows, and join outputs at 10,000 rows. The provider receives only a bounded sample or exact aggregate result. Random result references are held only by the active tool context, access checks repeat before derived operations, and cleanup clears frames at stream termination. No plaintext analytical result is persisted.

The structured DOCX tool implementation must fail closed for uneven tables and excluded documents.

## Sandboxed execution

Unrestricted generated-code execution is not implemented and is prohibited.

Planned sandbox requirements:

- Static syntax and AST validation.
- Restricted imports and attribute access.
- No arbitrary filesystem access.
- No network access.
- Bounded memory, CPU, process, output, and wall-clock resources.
- Execution identity and workspace isolation.
- Privacy-policy validation before and after execution.
- No persistence of plaintext generated datasets or source copies.

## Upload and parser security

Existing controls include:

- Streamed request-body limits.
- Per-file and per-chat limits.
- Spreadsheet row/cell limits.
- XLSX ZIP metadata checks.
- DOCX ZIP, entry, ratio, XML, section, and text limits.
- Malformed/unsupported format rejection.
- No arbitrary archive extraction.
- No silent truncation for rejected oversized uploads.

## Data handling

- Sensitive extracted documents are persisted encrypted.
- No plaintext persistent workbook or DOCX cache is created by the current implementation.
- Provider usage metadata must contain token counts and provider/model labels only.
- Application logs redact prompt, content, original, token, result, and credential fields.
- Raw exception and provider response bodies must not be returned to users.

## Data retention and deletion

Existing configuration includes temporary and abandoned upload/provider-resource retention settings.

Deletion must:

- Validate ownership.
- Remove application file state.
- Clean provider resources through the existing lifecycle when applicable.
- Respect provider cleanup failures.
- Avoid displaying raw sensitive values in diagnostics.

Formal workspace asset retention, evidence, and deletion manifests are planned.

## Provider failover

No automatic failover implementation is documented as complete.

Planned failover rules:

- Keep privacy projection outside provider selection.
- Retry only bounded idempotent operations.
- Never retry with excluded or unapproved sensitive data.
- Normalize quota, timeout, context, authentication, and provider errors.
- Do not retry provider requests indefinitely.
- Preserve request identity and conversation-turn state.

## Auditability

Logs should record event names, request IDs, provider/model names, durations, status categories, and bounded counts. Logs must not record:

- Prompts or questions.
- Assistant answers.
- Original PII.
- Token originals or mappings.
- Provider secrets.
- Authentication tokens.
- Raw workbook/document contents.
- Raw tool results.

## Planned security work

- Synthetic multi-table DOCX privacy fixtures.
- Mixed-file DOCX/workbook tool isolation matrix.
- Sandboxed Python execution.
- Persistent workspace asset lifecycle.
- Stronger sensitive-document classification than regex/NER sampling.
- Live provider, PostgreSQL, and Cloud KMS verification.

