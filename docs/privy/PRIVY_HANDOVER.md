# Privy AI Coding Handover

Last updated: 2026-11-24

Use [the architecture](./PRIVY_ARCHITECTURE.md), [roadmap](./PRIVY_ROADMAP.md), [decisions](./PRIVY_DECISIONS.md), [security](./PRIVY_SECURITY.md), and [testing](./PRIVY_TESTING.md) as the source of truth.

## Project purpose

Privy is a privacy-focused AI chat application for workbook and DOCX analysis. It locally masks and policies sensitive data, stores complete eligible content in PostgreSQL, uses bounded local tools for exact calculations, and sends only privacy-approved context to Gemini or Groq.

## Technology stack

- React, TypeScript, Vite, Tailwind CSS.
- FastAPI.
- PostgreSQL with SQLAlchemy/Alembic.
- Custom local JWT authentication.
- Gemini and Groq.
- pandas, regex, Presidio, spaCy.
- AES-256-GCM envelope encryption.
- Local development key provider and Cloud KMS production provider.
- Optional GCS ciphertext adapter.
- CSV, XLS, XLSX, and DOCX support.

## Non-negotiable constraints

- Keep Privy LLM-first.
- Do not introduce Auth0 or Streamlit.
- Do not replace pandas with DuckDB or Polars without approval.
- Do not weaken masking or privacy-policy enforcement.
- Do not execute unrestricted generated code.
- Keep excluded data out of all provider and tool payloads.
- Do not create persistent plaintext workbook/document caches.
- Preserve Gemini and Groq integrations.
- Preserve complete storage and the 200-row preview.
- No commit, push, deployment, or database migration unless explicitly requested.
- Add migrations only when required.

## Current implementation status

Stages 1A through 2D functionality is established in the existing codebase. Prompt 3 orchestration and Prompt 4 deterministic analytical capabilities are implemented and locally verified.

Recent DOCX improvements were implemented locally:

- Better repeated-header and data-first table handling.
- Broader bounded retrieval for complete-table questions.
- Structured DOCX table tools using the existing tool registry.
- Exact local calculations over complete eligible DOCX tables.
- Privacy fail-closed checks for excluded and sensitive Share documents.
- Frontend duplicate-send protection and refresh retry cleanup.
- Synthetic DOCX extraction, retrieval, privacy, and calculation regressions.
- Bounded `execute_analysis_plan` operations through the existing tool registry, with dependency validation, request-scoped output references, partial success, and capability metadata.
- Chained joins that reuse a complete request-scoped result to add more authorized source tables, preserving source and policy revisions through three-or-more-source workflows.
- Complete-dataset profiles and quality metrics; composable typed filters and stable sorts; multi-column grouping, aggregates, ranking and comparisons; calendar trends; fixed-operation Decimal/date derived columns.
- Request-scoped filtered and calculated outputs that feed later registered plan steps, with explicit precision/exactness, missing-data, result-size, and memory reporting.

Eleven code-review comments were reportedly resolved in the local worktree. The fixes and resolution should be rechecked by the next agent if they depend on the final implementation.

## Test status

Previously reported:

- Backend: 83 tests passed.
- Frontend TypeScript/build: passed.
- Python compilation: passed.
- Alembic: single head `0008_persistent_turns`.
- `git diff --check`: passed.

These are reported local results, not fresh verification.

Prompt 2 verification performed locally:

- Backend: 89 tests passed; the focused join/confirmation/DOCX group passed 36 tests.
- Frontend TypeScript and Vite production build passed.
- Python compilation and `git diff --check` passed.
- Alembic has one head, `0009_join_confirmations`; the migration was not applied.
- Live PostgreSQL, Gemini, Groq, and Cloud KMS behavior remains unverified.

Prompt 3 focused tool verification:

- The analytical tool suite has 18 tests, including plan validation, partial success, confirmation pause, and a three-source chained join with an exact downstream aggregate.
- Fresh verification: backend suite 93 tests passed; frontend TypeScript and Vite production build passed; Python compilation and `git diff --check` passed; Alembic reports one head, `0009_join_confirmations`.
- No live provider verification was performed; the existing mocked Gemini/Groq shared tool-loop tests remain in the suite.

Prompt 4 verification:

- Focused workbook tool suite: 26 tests passed; full backend suite: 101 tests passed.
- Python compilation and `git diff --check` passed. Alembic reports one head, `0009_join_confirmations`.
- No migration was created or applied. Frontend files were unchanged; live PostgreSQL and provider behavior remain unverified.

The informal 28-record DOCX experiment is not a project acceptance blocker; synthetic fixtures define acceptance.

Independent Codex review is optional and should not be repeated unless a new implementation batch introduces meaningful risk.

## Deployment status

The application has a deployment configuration, but no live Render, PostgreSQL, Gemini, Groq, or Cloud KMS verification is confirmed.

Do not claim deployment success. Any deployment or migration execution requires explicit approval.

## Outstanding issues

- Add provider-loop failure injection and cross-request follow-up context tests for plans.
- Add cancellation and memory-limit cleanup coverage for chained plans.
- Chained join ambiguity currently fails closed and does not create a new confirmation workflow for the joined-result-to-source relationship.
- Add frontend integration/browser coverage for the confirmation prompt and repeat-question approval flow.
- Expand regression coverage for memory ceilings and cleanup on tool failure/cancellation.
- Verify mixed spreadsheet/DOCX tool privacy in live provider calls.
- Complete frontend authentication and duplicate-submit browser tests.
- Verify live Cloud KMS and production blind-index behavior.
- Verify live provider tool calling and usage reporting.
- Add full Phase 1 acceptance matrix.
- Establish sandbox only in an explicit later phase.

## Next recommended task

Continue Phase 3 verification:

1. Verify migration `0009_join_confirmations` against an isolated PostgreSQL database when one is available; it was not run here.
2. Add provider-loop failure injection and cancellation/resource-limit tests for plans.
3. Add cross-request follow-up context tests and browser coverage for explicit approval followed by confirmed analysis.
4. Keep persistent analytical workspace and secure sandbox work deferred to their planned phases.

## Documentation update rule

After every implementation batch:

1. Update the nearest existing architecture/roadmap/decision/security/testing/hand-over section.
2. Mark only verified behavior as implemented.
3. Mark provider/PostgreSQL/KMS/Render behavior unverified unless live evidence exists.
4. Update test counts only after running them.
5. Record migration changes and backward compatibility.
6. Do not add future work to current implementation status.
7. Do not commit or push unless explicitly requested.
