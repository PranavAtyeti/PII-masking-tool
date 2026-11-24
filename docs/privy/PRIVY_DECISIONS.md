# Privy Architectural Decisions

Last updated: 2026-11-24

Questions 1-44 were answered explicitly by the user in the project discovery. Questions 45-55 delegated option selection to the assistant and were accepted by the user. The exact discovery wording and selection history beyond this summary are not all present in a permanent repository artifact; rows below preserve the known decisions without claiming unsupported details.

Status labels: **Implemented**, **Partially implemented**, or **Planned**.

| # | Selected option | Explanation | Implementation implications | Status |
|---|---|---|---|---|
| 1 | Existing React/TypeScript/Vite/Tailwind frontend | Preserve the current frontend. | No frontend framework replacement. | Implemented |
| 2 | FastAPI backend | Preserve the current API framework. | Continue FastAPI routers, middleware, and streaming responses. | Implemented |
| 3 | PostgreSQL | Preserve authoritative relational persistence. | Use SQLAlchemy/Alembic; no alternate analytics database. | Implemented |
| 4 | Custom JWT authentication | Preserve existing local authentication. | Continue refresh rotation and ownership validation. | Implemented |
| 5 | Gemini and Groq providers | Preserve both providers. | Provider-neutral orchestration and provider-specific adapters. | Implemented |
| 6 | Local-first masking | Sensitive data is detected and masked locally before provider calls. | Keep regex/Presidio/NER and token mappings backend-controlled. | Implemented |
| 7 | Local mask/unmask state | The original values are kept locally and restored for display. | Never persist provider prompts or mappings to provider resources. | Implemented |
| 8 | Mask/Share/Exclude policies | Every controlled value or document has one of three actions. | Validate policies before every retrieval/tool/provider call. | Implemented |
| 9 | Sensitive Share confirmation | Sharing sensitive data requires explicit authorization. | Fail closed for unconfirmed Share. | Implemented |
| 10 | CSV, XLS, and XLSX support | Preserve workbook ingestion. | Maintain complete storage and bounded 200-row preview. | Implemented |
| 11 | DOCX support | Extract paragraphs, headings, and table rows locally. | Retrieve bounded excerpts and provide stable citations. | Implemented |
| 12 | Full-dataset storage | Store complete eligible workbook/document data locally. | Retrieval samples cannot become exact calculation sources. | Implemented |
| 13 | 200-row UI preview | Preserve preview behavior. | Preview pagination limits do not limit storage or tools. | Implemented |
| 14 | Hybrid AI and local computation | LLM orchestration plus deterministic backend computation. | Add tools rather than a local dataframe chatbot. | Partially implemented |
| 15 | LLM-driven orchestration | Providers interpret and route user requests. | Local tools execute authoritative calculations. | Implemented |
| 16 | Advanced analytics | Support exact filtering, grouping, aggregation, and comparison. | Continue the bounded tool registry. | Partially implemented |
| 17 | Multi-step tool execution | Permit bounded provider-tool rounds. | Enforce round and call limits. | Implemented |
| 18 | Verifiable exact results | Tool results may be marked exact. | Do not label retrieval samples as exact. | Implemented |
| 19 | Shared privacy projection | One policy engine governs retrieval and tools. | Reuse existing policy validation and projection helpers. | Implemented |
| 20 | Adaptive processing | Later phases may adjust retrieval depth and context size. | Preserve privacy and explicit context-budget semantics. | Planned |
| 21 | Caching | Later caching must be bounded and version-aware. | No plaintext persistent cache is permitted. | Planned |
| 22 | Standardized analytical tools | Use provider-independent named tools and strict arguments. | Preserve `_TOOL_REGISTRY` and safe-result validation. | Implemented |
| 23 | Verified results | Add exact/provenance/status metadata to analytical results. | Shared additive result contract is attached to existing tool results. | Implemented |
| 24 | Bounded autonomous investigation | Limit steps, calls, depth, and data volume. | Stop on failure, budget exhaustion, or ambiguity. | Planned |
| 25 | Multi-file analysis | Support multiple attached files and later joins. | Preserve ownership and revision checks. | Partially implemented |
| 26 | Sandboxed Python execution | Use isolation for future generated-code execution. | Not implemented; do not run unrestricted generated code. | Planned |
| 27 | Static code validation | Future sandbox code must pass restricted AST/import validation. | Not implemented. | Planned |
| 28 | Restricted filesystem/network sandbox | Generated code must have no arbitrary filesystem or network access. | Not implemented. | Planned |
| 29 | Persistent analytical workspace | Later versions may save derived analysis and assets. | Requires approved schema and encryption lifecycle. | Planned |
| 30 | Version-aware analysis | Results must reference source and policy versions. | File revisions exist; derived-result versioning is planned. | Partially implemented |
| 31 | Adaptive visualization | Choose useful charts based on verified results and intent. | Planned; do not invent charts for exactness-sensitive answers. | Planned |
| 32 | Intelligent provider failover | Provider fallback is a later reliability capability. | Preserve privacy and quota/error normalization. | Planned |
| 33 | Background execution | Long-running tasks require durable bounded execution. | No application worker infrastructure is currently implemented. | Planned |
| 34 | Privacy-preserving evaluation | Test and benchmark data must retain de-identification controls. | Use synthetic fixtures and no real customer data. | Implemented |
| 35 | Versioned backward-compatible evolution | Additions must preserve APIs and previous encrypted data behavior. | Migration and compatibility review required for every batch. | Partially implemented |
| 36 | No Auth0 | The existing custom authentication architecture remains authoritative. | Do not add Auth0. | Implemented |
| 37 | No Streamlit | Streamlit is not reintroduced. | Keep React/FastAPI. | Implemented |
| 38 | No DuckDB or Polars replacement | pandas remains the current dataframe implementation. | Do not replace it without explicit approval. | Implemented |
| 39 | No GCS migration in current stages | GCS storage remains optional, not the primary lifecycle. | Do not start Stage 3 storage migration. | Implemented |
| 40 | AES-256-GCM envelope encryption | Encrypt stored sensitive application data with generated DEKs. | Preserve local and Cloud KMS providers. | Implemented |
| 41 | Cloud KMS production | Production key wrapping fails closed. | Cloud credentials and key resource must be configured. | Implemented |
| 42 | Local key provider development | Local development must work without GCP credentials. | Requires a valid local encryption key. | Implemented |
| 43 | Bounded context budgets | Prompt/file/history budgets are model-aware. | Preserve prompt budgeting and context truncation labels. | Implemented |
| 44 | Privacy-safe observability | Logs and usage metadata exclude sensitive prompt/content fields. | Extend existing redaction rather than ad hoc logging. | Implemented |
| 45 | Standardized analytical foundation | Establish provider-neutral shared table/file abstractions. | Existing workbook/DOCX sources reuse the current registry with shared identity and result provenance. | Partially implemented |
| 46 | Verified cross-file joins | Joins require approved keys and privacy-preserved result types. | Bounded discovery and unambiguous one-to-one/one-to-many execution are available; ambiguous joins await backend-verifiable user confirmation. | Partially implemented |
| 47 | Adaptive caching with invalidation | Cache only bounded, privacy-safe derived metadata/results. | Must be version-aware; no plaintext persistent cache. | Planned |
| 48 | Sandboxed Python execution | Provide a restricted execution mode for approved calculations. | Requires isolation, static validation, resource limits, and network/filesystem restrictions. | Planned |
| 49 | Persistent workspace assets | Save approved analysis assets in an encrypted, versioned workspace. | Requires workspace schema and deletion lifecycle. | Planned |
| 50 | Adaptive visualization | Generate visualizations from verified, privacy-filtered result sets. | Restrict chart inputs and retain source/version metadata. | Planned |
| 51 | Provider-aware failover | Use provider capability and quota metadata to choose or fail over safely. | Retry only with bounded, idempotent provider calls. | Planned |
| 52 | Background long-running jobs | Move long-running approved analysis to a durable queue/workflow. | Requires job identity, retries, cancellation, and cleanup. | Planned |
| 53 | Privacy-preserving evaluation | Evaluate accuracy and cost using synthetic or authorized datasets. | Never require real PII in standard tests. | Implemented |
| 54 | Workspace asset lifecycle | Define creation, encryption, retention, revocation, and deletion of workspace assets. | Planned for Phase 5. | Planned |
| 55 | Backward-compatible versioned evolution | Use additive schemas, migrations, revisions, and compatibility tests. | No destructive architecture changes or migration assumptions. | Partially implemented |
