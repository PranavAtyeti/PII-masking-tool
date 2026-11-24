# Privy Roadmap

Last updated: 2026-11-24

Status labels:

- **Completed**: implemented and locally verified.
- **Partially completed**: some implementation exists, but acceptance or live verification remains incomplete.
- **Pending**: planned and not established as implemented.
- **Blocked**: dependency or approved decision is absent.

## Phase 1 — Existing system verification

**Status: Partially completed**

### Objectives
- Establish evidence-based acceptance for the current spreadsheet, DOCX, privacy, provider, and deployment behavior.
- Confirm current local code against synthetic fixtures.

### Scope
- Existing upload/security pipeline.
- 200-row workbook preview and complete storage.
- Mask/Share/Exclude controls.
- DOCX extraction and retrieval.
- Exact local tools.
- Authentication and conversation lifecycle.
- Deployment configuration and migration readiness.

### Dependencies
- Synthetic DOCX fixture.
- Local PostgreSQL or isolated test database.
- Mock Gemini/Groq test adapters.
- Live provider verification only when credentials and approval exist.

### Deliverables
- Phase 1 acceptance matrix.
- Synthetic DOCX fixtures.
- Privacy and exact-calculation regression suite.
- Deployment configuration checklist.

### Acceptance criteria
- All file types and privacy choices pass regression tests.
- Complete eligible data is stored and exact tools use complete stored data.
- No live deployment claim is made until live verification exists.

### Testing requirements
- 83 reported tests remain a baseline, not a replacement for new acceptance tests.
- Backend suite, frontend build, compilation, Alembic heads, and diff checks.
- Synthetic DOCX fixtures.

## Phase 2 — Shared analytical foundation

**Status: Partially completed**

### Objectives
- Establish a single versioned, privacy-aware analytical foundation for spreadsheets and document tables.

### Scope
- Shared file and table abstractions.
- Standardized tool registry.
- Multi-file file/table joins.
- Full-dataset exact execution.
- Verification and result provenance.
- Dataset schema and column metadata.

### Dependencies
- Phase 1 acceptance baseline.
- Stable privacy-policy and tool interfaces.
- Migration strategy only if persistent joins/workspace state are introduced.

### Deliverables
- Shared analytical schema.
- Standardized discovery, filter, aggregate, group, compare, join, and profile tools.
- Verified-result envelope.
- Multi-file join validation.
- Shared source identity and safe result provenance for existing workbook and DOCX tools.
- Bounded join discovery/execution across workbook and DOCX table sources, one-time confirmation for ambiguous relationships, and request-scoped full joined-result aggregation.

### Acceptance criteria
- Same privacy projection governs spreadsheets and DOCX tables.
- Exact results are not derived from retrieval samples.
- Join keys and excluded/masked columns are validated.
- Existing analytical tool results include provenance and exact/estimated status where applicable.

### Testing requirements
- Unit, discovery, confirmation lifecycle, cross-format join, full-result aggregation, privacy, authorization, and fixture accuracy tests.

## Phase 3 — Advanced analytical intelligence

**Status: Pending**

### Objectives
- Support adaptive multi-step investigation and bounded autonomous reasoning.

### Scope
- Query decomposition.
- Adaptive processing plans.
- Multi-step tool execution.
- Bounded investigation depth.
- Ambiguity detection.
- Follow-up and correction workflows.

### Dependencies
- Completed Phase 2 shared tools.
- Provider tool-call reliability.
- Verified result metadata.

### Deliverables
- Investigation planner.
- Tool-round policies.
- Evidence and source lineage.
- Stopping and escalation rules.

### Acceptance criteria
- Investigation is bounded and privacy-preserving.
- Tool plans terminate on contradictions or provider failures.
- Results remain traceable to complete data or retrieved sources.

### Testing requirements
- Mocked provider tool-loop tests.
- Multi-step planning tests.
- Loop, budget, quota, and failure tests.

## Phase 4 — Reliability and optimization

**Status: Partially completed**

### Objectives
- Optimize cost and latency while improving production reliability.

### Scope
- Prompt/context efficiency.
- Usage observability.
- Provider retry and failover.
- Database round-trip optimization.
- Bounded caching and resource cleanup.
- Readiness and deployment monitoring.

### Dependencies
- Stable provider usage normalization.
- Persistent turn lifecycle.
- Approved cache and failover design.

### Deliverables
- Cost and latency instrumentation.
- Provider failover policy.
- Bounded invalidation strategy.
- Production diagnostics.

### Acceptance criteria
- No security or data reduction regression.
- Provider failover does not leak PII or resend excluded data.
- Retry behavior is idempotent and bounded.
- Any cache claims are backed by measurements.

### Testing requirements
- Quota/rate-limit tests.
- Interrupt and stale-turn tests.
- Cache invalidation tests.
- Benchmark and token-cost measurement.

## Phase 5 — Persistent analytical workspace

**Status: Pending**

### Objectives
- Preserve approved analytical context, versions, derived assets, and reports.

### Scope
- Persistent workspace records.
- Version-aware files and joins.
- Saved query/result provenance.
- Optional GCS adapter if an approved external-storage decision is made.
- Encryption and deletion lifecycle.

### Dependencies
- Phase 2 analytical foundation.
- Explicit workspace schema decision.
- KMS/storage retention and deletion requirements.

### Deliverables
- Versioned workspace model.
- Asset lifecycle.
- Result and source persistence.
- Workspace authorization.

### Acceptance criteria
- Every derived result identifies source versions.
- No plaintext sensitive caches are introduced.
- Workspace access honors tenant/user/chat/file ownership.
- Deletion and retention are auditable.

### Testing requirements
- Version invalidation.
- Ownership and deletion tests.
- Encryption/persistence tests.
- Optional GCS ciphertext tests.

## Phase 6 — Advanced user experience

**Status: Pending**

### Objectives
- Improve usability for complex analytical work.

### Scope
- Adaptive visualization.
- Rich report generation.
- Background execution UX.
- Investigation progress.
- Citation and provenance UI.
- Multi-file workspace navigation.

### Dependencies
- Phase 4 reliability.
- Phase 5 workspace.
- Approved visualization/report formats.

### Deliverables
- Interactive result views.
- Report/export capabilities.
- Background task status and retry UX.
- Workspace history UI.

### Acceptance criteria
- Visualizations match verified exact results.
- Sensitive data remains masked until authorized.
- Long-running tasks are cancelable and retry-safe.

### Testing requirements
- Frontend build and interaction tests.
- Background lifecycle tests.
- Large-result rendering tests.
- Privacy review tests.

## Phase 7 — Advanced intelligence and automation

**Status: Pending**

### Objectives
- Support higher-level, bounded automation and provider-aware optimization.

### Scope
- Intelligent provider failover.
- Adaptive processing and caching.
- Automated anomaly detection.
- Scheduled or trigger-based analysis.
- Privacy-preserving evaluation.
- Versioned backward-compatible evolution.

### Dependencies
- Phases 2-6.
- Security approval for automation.
- Production observability and retention policies.

### Deliverables
- Provider policy engine.
- Automation scheduler if explicitly approved.
- Evaluation framework.
- Evolution/version compatibility policy.

### Acceptance criteria
- Automation has human-visible scope and cancellation.
- Provider and cache decisions do not weaken privacy.
- Runtime behavior is measurable and auditable.

### Testing requirements
- Failure injection.
- Provider and quota tests.
- Audit and authorization tests.
- Backward compatibility tests.

