# Redact / Relay — Python Edition

![Python](https://img.shields.io/badge/python-3.10%2B-blue)
![Streamlit](https://img.shields.io/badge/UI-Streamlit-FF4B4B)
![License](https://img.shields.io/badge/license-MIT-green)
![Status](https://img.shields.io/badge/status-prototype-orange)

A local-first PII masking layer for chatting with an AI model — with or
without a spreadsheet. Personal data is detected and tokenized on your
machine before anything is sent to the cloud; only masked tokens and your
question ever leave your computer.

Built on free, open-source detection components (regex + Presidio/spaCy
NER) paired with Groq's free-tier hosted LLM for answers.

---

## Table of contents

- [How it works](#how-it-works)
- [Architecture](#architecture)
- [Requirements](#requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Running the app](#running-the-app)
- [Usage guide](#usage-guide)
- [Detection design](#detection-design)
- [Security & privacy model](#security--privacy-model)
- [Improving answer quality](#improving-answer-quality)
- [Troubleshooting](#troubleshooting)
- [Project structure](#project-structure)
- [Known limitations](#known-limitations)
- [Roadmap ideas](#roadmap-ideas)
- [Contributing](#contributing)
- [License](#license)

---

## How it works

**With a spreadsheet:**

1. **Parse locally.** The uploaded file is read entirely in-process with
   pandas — nothing is uploaded anywhere at this stage.
2. **Suggest sensitive columns.** A smart recognizer classifies each column
   using header-name hints and a sample of its values (`detection.py`).
   Every suggestion is shown as a checkbox — you have final say on what gets
   masked.
3. **Mask.** Enabled columns are tokenized: structured values (email,
   phone, PAN, Aadhaar, card numbers) via regex; free text (a "Notes"
   column, say) via a local NER model (Presidio/spaCy) that catches names,
   locations, and other entities embedded mid-sentence without disturbing
   the rest of the text. Every unique value gets a stable token (e.g.
   `[PERSON_NAME_1]`), stored in a local SQLite table (`mapping_store.py`).
4. **Ask.** Your question plus the masked (tokenized) data are sent to
   Groq's free hosted API.
5. **Unmask.** The response comes back still containing tokens; those are
   swapped back to their original values locally before being displayed.

**Without a spreadsheet:**

The same masking pipeline runs on what you type into chat — any names,
emails, phone numbers, etc. in your message are tokenized the same way
before the message is sent, and un-tokenized in the reply. You get a
general-purpose assistant with the same privacy safeguard, no file needed.

**Either way, a defense-in-depth gate applies:** immediately before
anything is sent out, a second, independent regex check (`looks_unmasked`)
scans the exact outgoing payload and blocks the request outright if it
still resembles raw PII — a backstop that doesn't depend on the column
checkboxes or the NER pass having worked correctly.

## Architecture

```
  Spreadsheet (optional) ──▶ pandas parses locally
            │
            ▼
     detection.py  (header hints + regex)
            │
            ▼
   ner_detection.py  (Presidio / spaCy, local)
            │
            ▼
  mapping_store.py  (SQLite: token <-> original)
            │
            ▼
   masked data / masked chat message + question
            │
            ▼
        Groq (cloud, free tier)   ◀── only tokens leave the machine
            │
            ▼
   unmask_text() in app.py ──▶ displayed to you
```

`app.py` is the Streamlit UI and orchestrates all of the above; the data
panel (spreadsheet review/masking) only appears when a file is loaded, but
the chat panel and the masking safeguard are always active.

## Requirements

- Python 3.10+ (uses the `tuple[str, bool]` style type hints)
- A free [Groq](https://console.groq.com) API key

## Installation

```bash
cd pii-mask-python
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt
python -m spacy download en_core_web_sm

cp .env.example .env
```

Dependencies (see `requirements.txt` for exact version floors):

| Package | Purpose |
|---|---|
| `streamlit` | Web UI |
| `pandas` | Spreadsheet parsing |
| `openpyxl` | `.xlsx` support for pandas |
| `python-dotenv` | Loads `.env` |
| `presidio-analyzer` | NER-based PII detection |
| `spacy` | NLP model backing Presidio |
| `requests` | HTTP calls to Groq |

## Configuration

Get a free key at https://console.groq.com/keys (no credit card required),
then either:

- Paste it into `GROQ_API_KEY` in `.env`, **or**
- Paste it directly into the sidebar's **⚙️ Settings** panel once the app is
  running — this is the easiest way to swap keys on the fly. A key entered
  there overrides `.env` for that session only and is never written to disk.
  The sidebar shows a 🟢/🔴 status indicator so you can see at a glance
  whether a key is active.

| Variable | Default | Description |
|---|---|---|
| `GROQ_API_KEY` | *(empty)* | Your Groq key. Can also be set/changed live in the sidebar. |
| `GROQ_MODEL` | `llama-3.1-8b-instant` | Any chat model available on your Groq account. Also changeable live in the sidebar (`llama-3.1-8b-instant`, `llama-3.3-70b-versatile`, `gemma2-9b-it` are offered by default; type a different model name in `.env` if you want another as the startup default). |
| `MODEL_TEMPERATURE` | `0.1` | Sampling temperature. Kept low by default since this is a data-QA/assistant tool, not a creative one — lower values reduce hallucinated/inconsistent answers. |

**Response length** is controlled separately, live in the sidebar's
**Detection settings** panel: the **Concise answers** toggle (on by default)
caps the model at ~250 output tokens and instructs it to lead with a direct
1–3 sentence answer, no preamble. Turn it off for fuller, more explained
responses (up to ~800 tokens).

## Running the app

```bash
streamlit run app.py
```

Opens at http://localhost:8501.

## Usage guide

1. **Set your Groq API key** in the sidebar's **⚙️ Settings** panel (or via
   `.env`). It's expanded by default until a key is set.
2. **Just start chatting** — no file required. Personal details you type
   are masked before being sent, the same way spreadsheet cells are.
3. **Optionally add a spreadsheet** via the "Add a spreadsheet" panel
   (`.csv`, `.xls`, `.xlsx`). Once uploaded:
   - Review the **smart-detected columns** panel. Every column the
     recognizer flagged is pre-ticked with its guessed type. Untick
     anything that shouldn't be masked; tick anything it missed. Changes
     apply immediately to both the preview table and what gets sent to the
     model.
   - Ask questions about the data in the same chat panel, e.g. *"Which rows
     have salaries above the median?"*
4. **Adjust detection settings** in the sidebar any time: toggle NER on/off,
   move the confidence threshold (higher = fewer false positives but may
   miss subtler mentions; lower = catches more but risks flagging ordinary
   text), and toggle **Concise answers** for shorter vs. fuller responses.
5. **Inspect or clear the token map** via the "Local token map" expander
   and the *Clear PII mapping* button — this data stays in
   `mapping_store.db` on your machine and is never transmitted. *Clear
   chat* resets the conversation without touching the mapping.
6. **Remove a spreadsheet** at any time via the button in the upload panel
   to drop back into plain chat mode.

## Detection design

Two layers work together rather than one replacing the other:

- **Regex (`detection.py`)** handles structured, format-perfect values —
  email addresses, 10-digit phone numbers, PAN, Aadhaar, and card numbers —
  where a deterministic pattern is strictly more reliable than a
  probabilistic model.
- **NER (`ner_detection.py`, via Presidio/spaCy)** handles free text, where
  PII is embedded in prose rather than sitting alone in a cell or message
  (e.g. *"call Rohan at his Baner office about the Q3 invoice"*). Only the
  specific entity spans found are tokenized; the rest of the text is left
  intact so the model retains context. This same function runs on both
  spreadsheet free-text cells and chat messages.
- **Column-header heuristics** (`COLUMN_HINTS` in `detection.py`) give a
  fast first guess per column, with explicit handling so that
  identifier-shaped headers like `CustomerID` aren't mistaken for name
  columns.
- Cell sampling (`classify_dataframe_columns`) is a fallback for columns
  whose header gives no hint: it classifies a sample of cells and only
  commits to a type if a clear majority match.
- **Pre-send audit is scoped to structured fields only.** Right before
  anything is sent, `find_leaked_values()` in `app.py` does a cell-by-cell
  comparison of the raw vs. masked dataframe, but *only* for columns that
  are both currently enabled and classified as a structured type (name,
  email, phone, ID, etc. — anything `col_types.get(col)` is truthy for).
  If such a cell's masked value still equals its raw value, that's a real
  masking bug and the request is blocked. Free-text/unclassified columns
  (e.g. a "Notes" field) and any column you've unticked are never scanned
  by this check at all — masking inside long prose is NER-driven and
  best-effort by design (see the NER bullet above), so it's deliberately
  not a hard gate; an unticked column is your explicit choice to send it
  raw, and its content is never used to explain a leak elsewhere either.

## Security & privacy model

- **Tokens only, never raw values, leave the machine.** Whether you're
  chatting freely or asking about a spreadsheet, only masked tokens and
  your question are sent to Groq. The token↔value mapping never leaves
  your machine.
- **Defense in depth.** Before anything is sent, `find_leaked_values()`
  cross-checks every enabled, structured-type cell against its raw value
  and blocks the request if masking silently failed on one — see
  [Detection design](#detection-design) for its exact (narrow) scope.
  Separately, `looks_unmasked()` re-scans the entire outgoing payload for
  email-, card-, and PAN-shaped patterns as a broader catch-all, and blocks
  the request outright if anything still looks like raw PII.
- **Session isolation.** Each browser session gets its own `session_id`;
  mappings never leak between different uploads, chats, or users, and are
  wiped on demand with *Clear PII mapping*.
- **API key handling.** A key entered in the sidebar lives only in that
  session's memory (Streamlit `session_state`) — it is never written to
  `.env` or disk by the app.
- **Nothing persisted outside your machine.** `mapping_store.db` is
  `.gitignore`d and only ever read/written locally.

This is a mitigation layer, not a certified anonymization guarantee — see
[Known limitations](#known-limitations) for what it does *not* protect
against.

## Improving answer quality

If Groq's answers seem wrong or inconsistent, check these first — roughly
in order of impact:

1. **Model choice.** `llama-3.1-8b-instant` is fast but, like any small
   model, noticeably weaker at exact arithmetic over many rows (medians,
   sums, "above-average" filters). Switch to `llama-3.3-70b-versatile` in
   the sidebar for meaningfully better analysis at a small speed cost.
2. **Temperature.** `MODEL_TEMPERATURE` defaults to `0.1` for more
   deterministic answers. Raise it only if answers feel too terse or
   robotic for your use case.
3. **Row limits.** Only the first 200 rows of a spreadsheet are sent per
   question (`build_masked_context`). The app warns you in the chat and
   tells the model explicitly when this truncation happens, so answers
   about the full dataset are flagged as based on a partial view.
4. **Ask narrower questions.** "List the 5 highest values in column X" is
   answered more reliably by a small/fast model than "analyze trends across
   the dataset," which needs more holistic reasoning over the whole table.

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| `No Groq API key is set` | No key in `.env` or the sidebar's Settings panel | Add a free key from https://console.groq.com/keys in the sidebar |
| A token like `[PERSON_NAME_1]` shows up literally in the answer instead of the real value | The model reproduced the token with different casing or spacing (e.g. `[person_name_1]`) | Fixed as of this version — `unmask_text()` now matches case-insensitively and ignores stray whitespace inside brackets. If it still happens, the model likely invented a token that was never assigned (a hallucination) — rephrase the question |
| Responses feel too long | Default mode favors completeness | Turn on **Concise answers** in the sidebar's Detection settings — caps output and instructs the model to lead with the direct answer |
| "Couldn't reach Groq" | No internet, or Groq is down | Check connectivity; retry |
| A column you expect to be masked isn't | Header didn't match any hint and cell sampling didn't find a majority pattern | Manually tick the column in the "Smart-detected columns" panel |
| "Request blocked" message on every question | `looks_unmasked()` is matching something in your masked data/message that only coincidentally looks like an email/card/PAN pattern | Check the flagged text; if it's a false positive, this is a known trade-off of a conservative safety gate |
| "Request blocked: masking failed for a structured field..." | A genuine masking bug — an enabled, structured-type column (name/email/phone/ID/etc.) still holds its raw value where a token should be | This is not a setting to adjust; please report it. Note this check no longer covers free-text columns or unticked columns at all — see [Detection design](#detection-design) — so a name or city missed inside a "Notes" column, or raw content in a column you deliberately unticked, will *not* trigger this and is expected/accepted behavior |
| Answers ignore later rows in a large file | The 200-row cap — see [Improving answer quality](#improving-answer-quality) | Ask about the data in narrower batches, or raise the cap in `build_masked_context` if you're comfortable editing the code |
| `en_core_web_sm` not found | spaCy model wasn't downloaded | `python -m spacy download en_core_web_sm` |
| Text is faint/unreadable in sidebar buttons or expanders ("Settings", "Chat", "Mapping") | Those elements kept Streamlit's default *white* background; forcing light text onto that (an earlier fix) left it light-on-white, not light-on-navy | Fixed as of this version — sidebar buttons/expanders now get an explicit dark, translucent background matching the sidebar |
| Text is faint/unreadable elsewhere (metrics, chat input placeholder, header banner) | Streamlit was auto-switching to a dark theme based on your OS/browser, which clashed with the app's fixed light-theme CSS | Fixed as of this version — `.streamlit/config.toml` now pins the theme explicitly so it no longer depends on system settings |
| Styling looks like default Streamlit, not the custom theme | A Streamlit upgrade changed internal `data-testid` attributes the CSS in `inject_css()` targets | Inspect the new DOM with browser devtools and update the selectors in `app.py`'s `inject_css()` |

## Project structure

```
pii-mask-python/
├── app.py               # Streamlit UI, styling, chat orchestration, masking pipeline, Groq calls
├── detection.py          # Header-hint + regex column/cell classification
├── ner_detection.py       # Presidio/spaCy NER layer for free text (cells and chat messages)
├── mapping_store.py      # SQLite-backed token <-> original value store
├── .streamlit/
│   └── config.toml       # Pins the UI theme explicitly (avoids OS/browser dark-mode contrast issues)
├── requirements.txt
├── .env.example
└── .gitignore
```

## Known limitations

- **Not a certified anonymization tool.** This reduces exposure of obvious
  PII; it does not guarantee full anonymization or compliance with a
  specific regulatory standard (GDPR, HIPAA, DPDP Act, etc.) out of the box.
- **NER quality depends on the base model, and misses on free text are not
  blocked.** `en_core_web_sm` is a small, fast spaCy model — it can miss
  uncommon names, non-English text, or unusual phrasing in long free-text
  columns (e.g. a "Notes" field). Unlike structured fields, this is treated
  as an accepted limitation rather than a hard gate (see
  [Detection design](#detection-design)): a missed entity in prose will not
  block the request. If a free-text column in your data regularly contains
  sensitive content, review it directly (the "Show masked" toggle in the
  data table) rather than relying on the block to catch a miss. Larger
  spaCy models would improve recall at the cost of speed/memory.
- **Cloud-only inference.** With the local Ollama option removed, questions
  and masked data always go over the internet to Groq, even though raw PII
  does not. If fully offline inference is a hard requirement for your use
  case, that trade-off is worth knowing about upfront.
- **Row cap per question.** Only the first 200 rows are sent per question;
  very large files require asking about them in batches.
- **No automated test suite yet** — see [Roadmap ideas](#roadmap-ideas).
- **Single-machine, single-process SQLite store** — not designed for
  concurrent multi-user deployment as-is.

## Roadmap ideas

- Unit tests for `detection.py` classification rules and the masking/
  unmasking round-trip in `mapping_store.py`.
- Configurable row cap with automatic chunking for large files.
- Optional larger spaCy/transformer NER model for higher recall.
- Structured aggregate computation (sums/medians/filters) done in pandas
  rather than left to the LLM, for exact answers to quantitative questions.
- Per-message indicator of which parts of a chat message were masked before
  sending, for extra transparency.

## Contributing

Issues and pull requests are welcome. For anything beyond a small fix,
please open an issue first to discuss the approach — in particular, changes
to `detection.py`'s classification rules or the masking pipeline in `app.py`
should include a short note on what test data confirms the change doesn't
regress existing detection.

## License

MIT — see [LICENSE](LICENSE).
