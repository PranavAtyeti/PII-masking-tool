# Privy document processing audit

**Audit date:** 19 November 2026  
**Scope:** Static review of the checked-out repository and official public provider/product documentation. No application source was changed and no performance tests were run. Findings below describe code paths, not observed production throughput.

## Executive summary

Privy is a React/Vite frontend and FastAPI/PostgreSQL application that masks spreadsheet content locally, stores masked CSV and encrypted token mappings, and streams answers from Gemini or Groq. The implementation has real Gemini File Search and Code Execution integrations. Gemini retrieval uses a reusable indexed store; Gemini calculation requests attach a masked CSV projection to a remote Code Execution tool. Groq receives inline masked CSV context through chat completions. Chat history is persisted locally and bounded to the last 32 messages when replayed. Gemini also has a server-side state continuation cache for retrieval conversations.

It is not yet a Word/document processor, a general spreadsheet workbook processor, or an exact large-data analysis system across all providers. Excel is read into one pandas DataFrame through `read_excel` without selecting sheet names, so only the default first sheet is represented; formulas/styles/merged-cell structure and workbook relationships are not modeled. The full masked DataFrame is retained, but Groq requests truncate it to an estimated 90,000 tokens per file and 110,000 total. Gemini Code Execution can see a whole-row projection only when a narrow question-column resolver recognizes the requested columns. Its routing is regex-based. A configured 100,000-row limit exists but upload does not enforce it. The frontend file limit is 10 MiB; FastAPI's request body ceiling is 50 MiB.

The largest risks are incomplete privacy coverage and inconsistent scope/retention: users can disable masking per column; final leak checks cover a limited set of recognizable patterns; filenames are sent in Gemini prompts; masked CSV is stored as ordinary PostgreSQL text; and Gemini provider file/search data has a different deletion lifecycle from local chat data. Mask mappings and messages do receive application-level AES-GCM protection. A full correctness/security deployment gate is still needed.

**Relative to ChatGPT and Claude:** Privy has a differentiated local masking boundary and a Gemini-specific persistent retrieval/calculation path, but is far behind on supported formats, workbook fidelity, multi-provider tool execution, local persistence operations, large-scale exactness, citations, and operational scale. Consumer product documentation describes capabilities, not their private internals; this report makes no claims about undocumented internals.

## A. Existing architecture and execution trace

### Stack verified from source

- Frontend: React, TypeScript, Vite, Tailwind (`frontend/package.json`, `frontend/src/App.tsx`).
- Backend: Python, FastAPI, pandas, Presidio/spaCy, SQLAlchemy, PostgreSQL (`backend/requirements.txt`, `backend/app/main.py`).
- Authentication: custom local HS JWT access tokens, opaque refresh sessions, Argon2 password hashes and guest sessions (`backend/app/auth.py`, `backend/app/routers/auth.py`). This conflicts with README references to Auth0.
- Providers: Groq OpenAI-compatible chat completions and Gemini native Interactions API (`backend/app/llm.py`).
- Word extraction, DOCX libraries, chunking, citation metadata, pgvector, and app-side tool execution are not present in the source tree.

### Upload to answer

1. **Select and preview:** `frontend/src/App.tsx` and `frontend/src/components/ChatInput.tsx` accept CSV/XLS/XLSX. `frontend/src/uploadLimits.ts` caps a file at 10 MiB, up to 10 files/chat. `frontend/src/api.ts` calls `/api/upload/{chat_id}/preview`; `backend/app/routers/upload.py::preview_file` reads the entire upload into memory, calls `_read_dataframe`, and returns row/column suggestions without persisting.
2. **Parse:** `backend/app/routers/upload.py::_read_dataframe` uses `pd.read_csv` or `pd.read_excel`. There is no `sheet_name=None`; pandas returns the first worksheet. The uploaded raw bytes and DataFrame are held in memory. There is no Word parser.
3. **Detect and mask locally:** `classify_dataframe_columns` in `backend/app/detection.py` uses column-name hints and sample-value heuristics. `backend/app/masking.py::mask_dataframe` performs per-cell structured matching, then free-text structured patterns and optional Presidio/spaCy NER. Tokens are chat-scoped and reused through `mapping_store.get_or_create_tokens`. Disabled columns stay raw by design. User question text is also mapped/masked before it is sent (`routers/messages.py::_generate_impl`).
4. **Validate and persist:** upload checks structured-cell masking (`find_leaked_values`) and scans serialized CSV for a set of high-confidence PII patterns (`security_scan.py::scan_for_unmasked_pii`). It serializes the complete masked DataFrame (`build_masked_context`) and a separate 200-row UI preview. `mapping_store.set_chat_file` stores `masked_csv`, filename, row count and column JSON in `ChatFile` PostgreSQL `Text` columns. No original uploaded bytes are retained by this path. `backend/app/storage.py` and `encryption.py` provide a GCS encrypted-file adapter, but the upload flow does not call it (requirements comment says cloud storage is on hold).
5. **Construct request:** `routers/messages.py::_generate_impl` loads messages/files, masks question/history, selects provider, builds instructions/context in `_build_prompt`, checks known residual patterns, then streams. Groq path (`_build_prompt`, `context_limits.py::limit_file_context`) puts masked CSV inline, capped by approximate characters/4 token budgets (90k/file, 110k request defaults). Gemini omits CSV inline and passes Gemini resource identifiers/tools.
6. **Gemini file lifecycle:** `gemini_files.py::get_or_upload_gemini_file` hashes and validates masked CSV, caches provider IDs on the backend filesystem, uploads a temporary Files API object and imports it to a File Search store. It reuses cache IDs, rechecks store existence periodically, and refreshes temporary Files API objects after expiry. Upload can pre-create this when Gemini is active. File Search indexing is provider-side; source is masked CSV only.
7. **Gemini routing/tool execution:** `_requires_code_execution` uses regexes to classify numeric questions; otherwise File Search is used. For code requests `_build_calculation_projection` reads the complete locally stored CSV but selects columns by exact names and a small alias list. It then uploads this masked projection as a Gemini Files API resource. `llm.py::_gemini_interaction_payload` requests Gemini Code Execution and includes the projection as a document input. Calculation code runs in Google's provider-managed environment, not in Privy's backend. Retrieval requests invoke Gemini File Search over persistent stores. There are no app-defined tools (`inspect_schema`, `execute_groupby`, etc.) and Groq currently gets no tools.
8. **History and persistence:** PostgreSQL stores chat/messages/files. Message bodies are encrypted in `mapping_store.add_message`; mappings are AES-GCM ciphertext with a keyed HMAC blind index (`crypto.py`, `mapping_store.py`). The most recent 32 messages are masked/replayed in the prompt. For Gemini, `gemini_interactions.py` stores interaction IDs and fingerprints in a local JSON file (20-hour application TTL), and `llm.py` uses `previous_interaction_id` only when the cache is valid. This cache is per filesystem/process, not a shared Postgres cache. Code Execution deliberately skips interaction reuse in `messages.py` (sets interaction ID to `None` for that mode).
9. **Stream, unmask, save:** `llm.py::stream_llm` streams provider text. `masking.py::stream_unmask` buffers partial token spans and restores token values from this chat's mapping before yielding SSE chunks. The final displayed answer is stored encrypted as a chat message. There is no LLM-created mapping import: only preexisting local mappings are used, though the model can emit a known token if it has seen it.
10. **Errors/limits:** upload parser errors become 400; masking residuals 422; Gemini file errors are caught and surfaced; provider 401/403/429/5xx, timeout and network errors map to safe messages. Retryability is surfaced but the backend does not automatically retry a failed inference. There is provider file polling with a timeout and 1.5s polling. Request size middleware checks `Content-Length` against 50 MiB but doesn't itself count streamed body bytes if header is absent. Quota handling is only user messaging; no per-user budget, concurrency admission, usage ledger or circuit breaker was found.

### Local vs provider execution

| Operation | Where it happens |
|---|---|
| File read, pandas parse, PII scan/mask, token mapping, context formatting | Privy backend (local to deployment) |
| Masked file, prompt, selected recent history | Sent to selected provider; Groq inline, Gemini via file/store APIs plus question |
| Gemini file embedding/indexing and File Search | Google Gemini service |
| Gemini Code Execution calculations | Provider-managed remote execution environment |
| Groq calculation/retrieval | Model response from inline context only; no execution/retrieval tool in current app path |
| Unmask and chat persistence | Privy backend/PostgreSQL |

## B. Competitor comparison

| Capability | Privy (verified in this repo) | ChatGPT (publicly documented) | Claude (publicly documented) |
|---|---|---|---|
| Spreadsheet types | CSV/XLS/XLSX; first worksheet only | Data Analysis supports common spreadsheet types including XLS/XLSX/CSV; Python-based analysis is documented | CSV and XLSX documented; XLSX depends on Analysis tool being enabled |
| Workbook fidelity | One DataFrame, no explicit multi-sheet/formula/table handling | Help docs recommend clean tabular layout; specific internals/workbook preservation for Data Analysis are not disclosed | Non-PDF docs are text-extracted; the public upload page does not promise formula/workbook fidelity |
| Word/DOCX | Unsupported | Document upload/summarization supported, exact extraction internals vary/undocumented | DOCX supported; non-PDF documents are text-only according to upload docs |
| Retrieval/context | Gemini File Search; Groq bounded inline CSV | Product docs describe file analysis and code execution; exact retrieval architecture is not public | Project knowledge bases retain files subject to total context window; exact retrieval internals are not public |
| Exact numeric analysis | Gemini remote Code Execution on recognized requests/projection; Groq has no exact calculator | ChatGPT Data Analysis documents Python calculations and a secure code environment; docs warn large/complex files may not be fully analyzed | XLSX analysis capability is documented when tool enabled, but exact internals are not public |
| Follow-ups/persistence | PostgreSQL chat/files, 32-message local history; Gemini state IDs on local disk; Groq resends context | Follow-up in a conversation is documented; plan-specific file behavior applies | Chats/projects and project knowledge bases documented; file text must fit context |
| App-side tools | None | User-visible Data Analysis can run code; internal exact flow not disclosed | Public Claude app analysis; internal tool implementation not disclosed |
| Privacy | Local masking, encrypted mappings/messages; masked data still leaves backend; masked CSV at rest in DB is plaintext | Product privacy depends on account/workspace controls; not a like-for-like local masking boundary | Product privacy depends on plan/workspace controls; not a like-for-like local masking boundary |
| Scale limits (public) | 10 MiB per frontend file, 10/chat; server request ceiling 50 MiB; Groq context budget | File FAQ documents up to ~50 MB spreadsheets (depending on row size); other upload caps apply | 30 MB/file, up to 20/chat; project content must fit context |
| Public internals | Repository code is inspectable | OpenAI documents Python environment and constraints, but not all internal architecture | Anthropic documents accepted formats and context limits, not all internal architecture |

Product comparison is subject to plans, accounts, workspaces, enabled tools and changing published limits. ChatGPT docs say Python-based analysis can calculate/transform data, and warn a file may not be fully analyzed if large/complex. Claude docs list DOCX/XLSX support and note project corpus must fit its context window. None of those product claims establish that all rows/sheets are always analyzed in every request.

## C. Gap analysis

### Critical — resolve before production deployment

- **Masked data is only as safe as detector coverage and user settings.** Disabled columns are deliberately not masked; NER is probabilistic/English configured; residual checks are narrow pattern scans. This is not a hard guarantee that PII never leaves the service. Either make sensitive-field masking mandatory or gate outbound use with explicit, auditable policy and strong structured validation.
- **No server-side upload size enforcement at the upload handler.** Frontend 10 MiB can be bypassed; middleware only trusts `Content-Length`. Guest file-size setting exists but is not applied. Add streamed byte counting and decompression/row/cell limits before production.
- **Plaintext masked dataset at rest.** `chat_files.masked_csv` is ordinary PostgreSQL text. Masked content can still be confidential business data; file encryption adapter is unused. Define encryption, access control, backup and deletion policies.
- **Deletion lifecycle is incomplete.** Deleting a chat/file removes local rows, but chat deletion path does not clean Gemini File Search stores or interaction IDs; file delete looks up `GEMINI_API_KEY` only (may not be the active/admin configured key), and cache/resource cleanup is best effort. Add durable lifecycle/outbox cleanup and reconciliation.
- **No reproducible accuracy or privacy suite for core upload/message paths.** Existing visible tests cover one masking batching case. Deployment needs regression coverage for leakage, row completeness, provider boundary, tenant isolation, multi-turn mapping and cleanup.

### High — major correctness, privacy or scale concern

- **Not a workbook/document model.** First worksheet only, DOCX unsupported, formulas and sheet links are not represented. Questions about other tabs, cross-tab joins, formula values or Word are unsupported or silently answered from incomplete data.
- **Large Excel support contradicts configured limits.** `MAX_ROWS_PER_FILE=100000` is declared but unused in upload. The actual UI limit is 10 MiB; a 100k-row file can exceed context or memory, and 500k/1m files have no safe explicit server behavior.
- **Groq numerical correctness is not guaranteed.** The app sends a truncated text prefix when context exceeds budgets; complete-data aggregation is therefore impossible. No deterministic local tool exists. Prompt warnings cannot make omitted rows available.
- **Gemini exact-analysis routing can miss or misroute.** Regex determines tool mode, and aliases/column extraction are limited. A row-count question phrased as “records” may route to File Search; an unrecognized column can lead to a preemptive “missing column” answer. It handles multiple files as a concatenated projection only if all have the referenced columns; no true workbook sheet model.
- **Code mode doesn't reuse state.** Retrieval mode can reuse Gemini interactions, but code mode always starts without prior interaction ID; projection files are re-uploaded on numeric turns. Upload-time main-file preparation still happens even though calculations use the separate projection.
- **Provider cache state is local JSON.** Multi-worker/container deployments can miss or race caches, and local cache loses continuity after replacement/restart. Chat-wide state updates can race between concurrent questions.
- **Metadata leakage.** Original filenames are embedded in prompts for both paths and Gemini upload source metadata. Filenames may themselves include people, account numbers, projects, or customer names.
- **Masking can distort analytics.** Amount header hints are masked as AMOUNT; no clear numeric-preserving pseudonymization is present. PAN/account fields masked with per-value tokens cannot support meaningful numerical operations. Column-level masking rules need to distinguish identifiers vs numeric measures.
- **No citations/source locators.** Gemini File Search results are not converted into user-visible stable row references, and DOCX page/heading provenance is absent.

### Medium — performance/maintainability

- Masking runs nested Python loops across all cells, object-casts a full DataFrame, then serializes the complete result; peak memory can be several times input size. NER can analyze many unique text cells synchronously.
- Token estimate is characters/4, not provider tokenizer. Groq context budget can still exceed model limits (system/history/output tokens omitted from the allowance) or leave much less data than intended.
- File Search readiness polls synchronously in request flow; concurrent uploads can occupy workers. No async/background job queue or admission control.
- Recent history limit is by message count, not tokens. A few long messages can overrun context. Provider usage/token reporting is not surfaced as an auditable cost ledger.
- README architecture/setup is stale: it claims Auth0 despite local auth and describes DOCX elsewhere? It also mentions files/limits that don't fully map to executable enforcement. Update docs after architecture decisions.

### Low — optional improvement

- Add explicit row IDs/offsets, better type summaries, formula provenance, charts and exportable analysis tables.
- Add context cache/token-cost metrics and a user-facing “data used” summary showing sheets, rows and any exclusions.

## D. Large-file and question capability assessment

These are source-based estimates, not benchmarks. All sizes are conditional on the 10 MiB frontend and 50 MiB HTTP limits, shape, Python memory, and provider quotas.

| Rows | Current likely behavior |
|---:|---|
| 5,000 | Likely parse/store/mask if upload is under 10 MiB. Groq may receive all rows only if under approximate context budgets. Gemini File Search can index the masked CSV; calculation requires recognized columns and remote Code Execution. |
| 50,000 | No code row cap enforced, but Python object/cell loops and PostgreSQL text storage can be costly. Groq will frequently truncate; Gemini search/calculation can receive all serialized rows, with provider constraints and latency to validate. |
| 100,000 | A nominal row limit is configured but not applied. File byte limit likely becomes practical blocker; no assurance of safe memory/latency. Groq truncation makes exact aggregate unsupported. |
| 500,000 | No safe supported path. Frontend blocks >10 MiB; direct API can bypass. Large in-memory parse/mask/serialize/DB/provider operation risks resource exhaustion. |
| 1,000,000 | No safe supported path. Same issues with substantially higher O(rows × columns) processing and data storage; must reject until a streaming/columnar pipeline is implemented and benchmarked. |

| Question | Current assessment |
|---|---|
| Record count | Row count metadata is local and exact after parsing, but provider prompt has columns/row count; only questions matching `how many/count/number of rows` explicitly avoid code mode. “Records” may invoke File Search and be approximate. |
| Average salary | Gemini calculation only when routing and column resolution recognize salary; projection contains full rows for selected columns, then remote Python computes. Groq approximate/truncated prompt only. |
| Department highest average salary | Gemini can in principle group the complete projection if it selects salary+department; it uses constrained column resolver and model-generated remote code, so validate exactness. Groq cannot guarantee complete rows. |
| Total debit vs credit | Gemini can in principle compute both if exact columns/aliases resolve. Groq cannot guarantee completeness past truncation. |
| Top 10 customers by transaction amount | Gemini only if both columns resolve; column names with uncommon aliases can fail. No stable row IDs/citations. Groq incomplete at scale. |
| Unusual transaction patterns | LLM may interpret patterns from retrieval/context, but no defined statistical/outlier method or full-data deterministic workflow is enforced. Treat as exploratory, require method disclosure. |
| Monthly totals | Gemini can calculate if date/amount columns are selected; resolver has no general date alias/type understanding. Not guaranteed. |
| Compare worksheets | Unsupported: only first worksheet is parsed. |

### Word: 100, 500, 1,000 pages

All three are unsupported by this code: there is no DOCX upload accept filter, parser, chunker, retrieval index or page/paragraph/table metadata. For a future implementation, page count will depend on page density and document structure, not page number alone. Preserve paragraph order/headings/tables, assign stable chunk IDs and source locators, mask before indexing, and retrieve relevant chunks for question-answering. Summaries across 1,000 pages need hierarchical/map-reduce summaries with source references, then targeted retrieval; sending a whole book in every turn is not the right architecture.

## E. Recommended architecture

Preserve React, FastAPI and PostgreSQL. Keep the LLM responsible for interpreting natural language and deciding among a small set of declared tools. Keep execution deterministic and allowlisted in the backend; never accept arbitrary Python/SQL from the model.

1. **Ingestion service:** stream upload to a bounded temporary object; validate content signature, size, decompressed size, row/cell count, workbook sheets and parser safety. Parse each sheet separately. Keep raw bytes only if needed under encrypted short-retention policy; otherwise discard after masking.
2. **Schema and masked canonical data:** persist masked tables in a queryable representation (PostgreSQL tables or encrypted Parquet/object storage plus DuckDB). Store schema (names, logical types, sheet names, row counts, formula/cache presence), stable row IDs and masking policy. Preserve numeric columns needed for aggregation; tokenize identity fields only. Do not silently drop sheets/formulas.
3. **LLM tool loop:** provide schema/metadata tools, `search_columns`, `filter_dataset`, `execute_aggregation/groupby/top_n`, `search_document`, `retrieve_document_chunks`, and `compare_document_sections`. The LLM chooses tool(s); validate JSON arguments against Pydantic schemas, tenant/chat/file scope, column allowlists, row/result bounds, operation timeout and query plan. Execute only fixed operations through pandas/DuckDB/Postgres. Return minimal result rows/totals and provenance to the LLM. Never expose raw PII to the execution response or model.
4. **Word/document indexing:** extract DOCX paragraphs/headings/tables with offsets and stable IDs; mask locally before storing/indexing. Chunk at semantic section boundaries with modest overlap; maintain keyword and vector indexes with metadata/source anchors. Use provider File Search only for Gemini if its data retention and isolation policy fits, or local retrieval for provider neutrality.
5. **Provider adapters:** Gemini: use File Search for narrative retrieval; use app-side tools for exact structured calculations to keep execution controlled and reusable. Treat Gemini Files objects as 48-hour references and File Search store as separately managed, deleteable state. Persist provider resource IDs in PostgreSQL and reconcile. Groq: implement local function-calling loop, because Groq documents tool requests but the app is responsible for executing local functions; send compact results instead of full files. Do not use Groq batch file APIs for interactive file analysis.
6. **Conversation state:** store chat, file binding, tool outputs/provenance and provider IDs in PostgreSQL with TTL and optimistic concurrency. Use bounded summaries of older turns, with current user question and selected evidence; ensure each request sends only needed context. Use idempotency keys to prevent double posts.
7. **Security/lifecycle:** no original filenames in prompts; substitute generated file labels. Enforce masking on all enabled sensitive fields and validate every outbound text/file/tool result. Encrypt masked content and mappings at rest; isolate tenant keys/rows; prevent user-controlled raw text from becoming SQL. Deletion should queue provider/local cleanup and verify completion. Redact telemetry and enforce strict log allowlists.

This is an LLM-first application with a small deterministic data access layer, not a standalone local analytics product. Gemini remote Code Execution is useful but should not be the sole correctness boundary for exact financial/HR totals.

## F. Optimization roadmap

| Phase | Change / likely files | Why and expected benefit | Risk / validation |
|---|---|---|---|
| 0. Production guardrails | `routers/upload.py`, `main.py`, `context_limits.py`, `security_scan.py`, `mapping_store.py`, deployment config | Enforce byte/row/cell/decompression limits server-side; eliminate filename leakage; enforce/record masking policy; complete cleanup; encrypt file data at rest. Prevent resource exhaustion and reduce privacy uncertainty. | Compatibility with existing chats; migration/re-encryption. Test oversize, malformed, zip bombs, missing headers, filenames with PII, exact deletion across provider failure. |
| 1. Make spreadsheet ingestion explicit | `routers/upload.py`, `models.py`, migrations, new ingestion module, frontend types/UI | Parse all sheets and report names/types/counts; identify formulas/cached values; stable row identifiers; replace implicit first-sheet behavior. | Formula cells without cached values; workbook edge cases. Fixture workbooks with formulas, hidden sheets, tables, blanks and mixed types. |
| 2. Add safe backend tools | `routers/messages.py`, `llm.py`, new tool schemas/executor, provider adapters | LLM-directed schema/search/filter/group/aggregate tools. Exact full-data answers independent of prompt truncation and reusable for both providers. | Hallucinated/invalid tool args; query injection; oversized results. Fuzz validation, exact comparison against independent oracle and multi-tenant controls. |
| 3. Persistent retrieval and conversation | `models.py`, migrations, `gemini_files.py`, `gemini_interactions.py`, `mapping_store.py` | Move provider caches into DB, support multi-worker deployments, include idempotency and lifecycle cleanup. Stable follow-ups without repeated full content. | Stale references/provider expiration; concurrent turns. Test restart, multi-worker, file replace/delete, expiry and retry. |
| 4. DOCX support | frontend accept/types, `routers/upload.py`, extraction/index modules, DB schema | Heading/table-aware extraction, privacy masking, chunk search and cited passages. Enable 100/500/1,000-page classes. | Extraction fidelity, table ordering, false masking. Gold set with paragraphs, headings, tables, repeated names, cross-section questions. |
| 5. Cost and scale | `llm.py`, provider adapters, metrics/logging, background jobs | Token usage accounting, cache stable prefixes, async index jobs, streaming, admission control, response/result caching. Reduce latency/cost and worker exhaustion. | Provider cache behavior differs. Compare billed tokens and latency with/without caching; load tests with concurrency. |

## G. Reproducible benchmark plan

### Dataset fixtures

- Generate CSV/XLSX sizes of 5k, 50k, 100k, 500k, and 1M rows with a deterministic seed. Columns: `record_id`, `customer_id`, `customer_name`, `department`, `salary`, `debit`, `credit`, `transaction_amount`, `transaction_date`, `note`. Include nulls, duplicate IDs, quoted commas/newlines, Unicode, mixed numeric formats, and PII canaries in names/emails/notes.
- XLSX variants: 1, 3, and 10 sheets; include a formula column with cached and missing cached values, tables, hidden sheet, repeated headers, and a cross-sheet lookup.
- DOCX fixtures with known answer key at approx. 100, 500, 1,000 pages, repeated section headings, tables, page breaks, footnotes/hyperlinks, and unique canary PII. Include exact paragraph/table source IDs.
- Keep an oracle generated independently from the app: row count, null counts, exact averages, sum debit/credit, department means, top 10, monthly totals, and controlled outlier labels. Fix timezone and rounding.

### Questions and expected answers

Ask the representative Excel questions listed in section D plus: “Which sheets are available?”, “Compare the total debit and credit by month across every sheet”, and a multi-turn follow-up (“Which department was highest? Now compare it with the prior year”). The expected answer is the oracle result computed over every designated row and sheet, with explicit null/duplicate treatment. Word questions: summary; locate named policy clause; quote a specific record; compare sections; find a table fact; reconcile a claim across distant chapters. Expected answer key includes exact target text/facts and chunk/page anchors.

### Measurements and acceptance criteria

- **Correctness:** exact numeric equality for integer sums/counts; decimal tolerance defined per currency precision; top-k set equality; retrieval answer F1 plus citation/anchor precision; no claims of unseen rows/sheets.
- **Completeness:** assert the operation reports processed row/sheet/page counts equal to fixture metadata; fail if sampling/truncation is implicit.
- **Privacy:** scan every outbound prompt, upload byte stream, retrieved chunk, tool result and log for canary originals, variants, filenames and metadata; confirm only tokens/approved aggregates cross provider boundary. Test disabled sensitive columns explicitly as expected block or visible opt-in policy.
- **Cost:** log provider-reported input/output tokens, cached tokens, retrieval/index embedding cost, and request count per question/follow-up; compare cumulative total for 10-turn conversations.
- **Latency:** record parse, classify/mask, persist, index-ready, retrieval, tool calculation, first-token and total time; report p50/p95 by size and provider.
- **Memory/scale:** process RSS and peak memory per worker, temporary storage, DB growth; test concurrent uploads/queries at 1/5/20/50 clients, limits and backpressure.
- **Persistence:** ask 10 follow-ups without re-upload; restart API and test; switch provider/model; expire Gemini Files ref; replace/delete file and chat; verify no stale access and user isolation.
- **Suggested correctness gate:** 100% exact results for supported calculations and no raw PII observed at any provider boundary. For retrieval, predefine minimum answer/citation accuracy on the gold set (e.g. ≥95%) and require abstention on missing evidence.

## H. Provider/API notes and deployment readiness

- **Gemini architecture:** File Search is a hosted retrieval/indexing feature; the repository uses provider File Search for retrieval and Code Execution with a file URI for calculation. Gemini's Files API uploads are temporary (official docs say 48 hours); do not use that ID as durable document storage. Server-side `previous_interaction_id` can preserve interaction history, but it is separate from file/store lifecycle and API interaction settings still need to be included on each turn. Keep local durable source as authority.
- **Groq architecture:** the current path is inline chat completion. Groq publicly documents local tool calling: the model returns a structured call, then the application executes it and sends results back. Its API file endpoint is for batch JSONL, not persistent interactive document analysis. Prompt caching is documented as automatic for common prefixes, but it does not fix repeated payload size or guarantee an exact cache hit.
- **No assumed Gemini code environment file access:** code execution is sent a document URI as an explicit input in the current payload; the application does not mount its own backend filesystem into Google's environment. That is the boundary to preserve and verify with provider integration tests.
- **Privacy policy review:** masked values remain potentially sensitive business data. Review provider retention controls, region, DPA/contract, account tier, audit and retention settings before enabling any provider upload. Google AI Developer API docs note interaction objects can be stored when `store=true`; app currently sets that. Groq says inference data is not retained by default, with limited reliability/abuse retention; ZDR is available in its controls. These published policies do not substitute for customer-specific legal/security review.
- **Documentation mismatch:** `README.md` says Auth0 but code implements local JWT auth; it describes GCS/encryption dependencies as planned/on hold. Reconcile before onboarding/deployment.
- **Not deployment-ready for large/multi-tenant sensitive workloads** until critical gates are closed: server upload bounds, privacy policy for opt-out columns, data-at-rest encryption for masked datasets, provider/resource deletion, exact supported-format contract, concurrency and cost limits, and end-to-end tests/benchmarks.

## Sources

Local sources: `backend/app/routers/upload.py`, `backend/app/routers/messages.py`, `backend/app/llm.py`, `backend/app/gemini_files.py`, `backend/app/gemini_interactions.py`, `backend/app/masking.py`, `backend/app/detection.py`, `backend/app/security_scan.py`, `backend/app/context_limits.py`, `backend/app/mapping_store.py`, `backend/app/models.py`, `backend/app/auth.py`, `backend/app/crypto.py`, `backend/app/storage.py`, `frontend/src/uploadLimits.ts`, `frontend/src/App.tsx`, `README.md`.

Official/public docs consulted:

- [OpenAI: Data analysis with ChatGPT](https://help.openai.com/en/articles/8437071-advanced-data-analysis-chatgpt)
- [OpenAI: File Uploads FAQ](https://help.openai.com/en/articles/8555545-file-uploads-faq)
- [OpenAI: ChatGPT capabilities overview](https://help.openai.com/en/articles/9260256-chatgpt-capabilities-overview)
- [Anthropic: document types for Claude](https://support.anthropic.com/en/articles/8241126-what-kinds-of-documents-can-i-upload-to-claude-ai)
- [Google: Gemini API Interactions](https://ai.google.dev/gemini-api/docs/interactions-overview)
- [Google: Gemini multi-turn interactions](https://ai.google.dev/gemini-api/docs/get-started)
- [Google: Gemini Files/document processing](https://ai.google.dev/gemini-api/docs/generate-content/document-processing)
- [Google: Gemini File Search documents API](https://ai.google.dev/api/file-search/documents)
- [Google: Gemini API pricing/tool costs](https://ai.google.dev/gemini-api/docs/pricing)
- [Groq: tool use overview](https://console.groq.com/docs/tool-use/overview)
- [Groq: local tool calling](https://console.groq.com/docs/tool-use/local-tool-calling)
- [Groq: data retention controls](https://console.groq.com/docs/your-data)
- [Groq: prompt caching](https://console.groq.com/docs/prompt-caching)
