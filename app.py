"""
app.py
------
Privy: Streamlit UI. Upload an Excel/CSV file (optional), auto-detect PII
columns (smart recognizer, user-adjustable), mask, chat with an LLM, unmask
the response before displaying it. Chat also works without any file
uploaded -- in that case the same PII-masking safeguard is applied to your
typed messages instead of spreadsheet cells.

Run:
    streamlit run app.py

See .env.example for provider configuration.
"""

import os
import re
import uuid

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv

from detection import classify_dataframe_columns, classify_cell, scan_free_text_structured, TYPE_LABEL
import mapping_store as store
import ner_detection

load_dotenv()
store.init_db()

st.set_page_config(page_title="Privy", page_icon="🔒", layout="wide")

# Provider-agnostic LLM config. LLM_BASE_URL is deploy-time only (set in
# .env, not exposed in the admin UI -- a bad base URL breaks the whole app,
# unlike a bad API key). Defaults to Groq's OpenAI-compatible endpoint, but
# any OpenAI-compatible /chat/completions endpoint works here unchanged.
LLM_BASE_URL = os.environ.get("LLM_BASE_URL", "https://api.groq.com/openai/v1/chat/completions")
LLM_MODEL_DEFAULT = os.environ.get("LLM_MODEL", "openai/gpt-oss-20b")
LLM_API_KEY_ENV = os.environ.get("LLM_API_KEY", "")
DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")

COMMON_MODELS = [
    "openai/gpt-oss-20b",
    "openai/gpt-oss-120b",
    "qwen/qwen3.6-27b",
]


# ---------------------------------------------------------------------------
# Styling -- a Streamlit-native "modern chat app" theme. Selectors target
# Streamlit's data-testid attributes, which are the most stable hooks
# available, but Streamlit's internal DOM can still shift between versions --
# if a future Streamlit upgrade changes these, the app still works, it just
# reverts to Streamlit's default look until the selectors below are updated.
# ---------------------------------------------------------------------------
def inject_css():
    st.markdown(
        """
        <style>
        @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@500;600;700&family=Inter:wght@400;500;600&family=JetBrains+Mono:wght@400;500&display=swap');

        :root {
            --accent: #12B886;
            --accent-2: #6C63FF;
            --ink: #1A1D23;
            --bg: #F6F7F9;
            --surface: #FFFFFF;
            --border: #E4E7EC;
        }

        html, body, [class*="css"] { font-family: 'Inter', sans-serif; }
        h1, h2, h3 { font-family: 'Space Grotesk', sans-serif !important; }
        code, .stCodeBlock, [data-testid="stChatMessageContent"] code {
            font-family: 'JetBrains Mono', monospace !important;
        }

        .stApp { background: var(--bg); }

        /* Sidebar */
        [data-testid="stSidebar"] {
            background: linear-gradient(180deg, #14162B 0%, #1B1E38 100%);
        }
        [data-testid="stSidebar"] * { color: #F0F1F7 !important; }
        [data-testid="stSidebar"] input, [data-testid="stSidebar"] textarea {
            color: #1A1D23 !important;
        }
        [data-testid="stSidebar"] hr { border-color: rgba(255,255,255,0.14); }

        /* Sidebar buttons and expanders keep Streamlit's default WHITE
           background unless told otherwise -- forcing light text onto that
           (previous rule above) left it nearly invisible: light-on-white,
           not light-on-navy. Giving them an explicit dark background fixes
           the actual contrast problem rather than just the text color. */
        [data-testid="stSidebar"] .stButton>button {
            background: rgba(255,255,255,0.07) !important;
            border: 1px solid rgba(255,255,255,0.20) !important;
            color: #F5F6FA !important;
        }
        [data-testid="stSidebar"] .stButton>button:hover {
            background: rgba(255,255,255,0.14) !important;
            border-color: var(--accent) !important;
            color: #FFFFFF !important;
        }
        [data-testid="stSidebar"] [data-testid="stExpander"] {
            background: rgba(255,255,255,0.05) !important;
            border: 1px solid rgba(255,255,255,0.16) !important;
            border-radius: 10px;
        }
        [data-testid="stSidebar"] [data-testid="stExpander"] summary {
            background: transparent !important;
        }
        [data-testid="stSidebar"] [data-testid="stExpander"] summary:hover {
            background: rgba(255,255,255,0.05) !important;
        }

        /* Buttons */
        .stButton>button {
            border-radius: 10px;
            border: 1px solid var(--border);
            font-weight: 500;
            transition: all 0.15s ease;
        }
        .stButton>button:hover {
            border-color: var(--accent);
            color: var(--accent);
        }
        button[kind="primary"] {
            background: var(--accent) !important;
            border-color: var(--accent) !important;
        }

        /* Chat bubbles */
        [data-testid="stChatMessage"] {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 14px;
            padding: 4px 6px;
            margin-bottom: 10px;
            box-shadow: 0 1px 2px rgba(16,24,40,0.04);
        }
        [data-testid="stChatMessage"]:has([data-testid*="user" i]) {
            border-left: 3px solid var(--accent-2);
        }
        [data-testid="stChatMessage"]:has([data-testid*="assistant" i]) {
            border-left: 3px solid var(--accent);
        }

        /* Expanders */
        [data-testid="stExpander"] {
            border-radius: 12px;
            border: 1px solid var(--border);
            background: var(--surface);
        }

        /* Base text contrast (sidebar already forces its own via !important) */
        .stApp, .stApp p, .stApp span, .stApp label, .stApp li,
        .stApp div { color: var(--ink); }

        /* Metric cards -- value/label render in their own testid'd elements
           that the generic selector above doesn't reliably reach, which was
           leaving the numbers nearly invisible against the white card */
        [data-testid="stMetric"] {
            background: var(--surface);
            border: 1px solid var(--border);
            border-radius: 12px;
            padding: 10px 14px;
        }
        [data-testid="stMetricValue"] { color: var(--ink) !important; }
        [data-testid="stMetricLabel"] { color: #667085 !important; }

        /* Captions (st.caption) -- keep readable grey, not too pale */
        [data-testid="stCaptionContainer"], .stCaption, small { color: #5B6472 !important; }

        /* Chat input -- placeholder text was too light to read against white */
        [data-testid="stChatInput"] textarea { color: var(--ink) !important; }
        [data-testid="stChatInput"] textarea::placeholder { color: #8A93A3 !important; opacity: 1; }

        /* Section headers */
        .rr-section-title {
            display: flex; align-items: center; gap: 8px;
            font-family: 'Space Grotesk', sans-serif; font-weight: 600;
            font-size: 1.2rem; color: var(--ink);
            margin: 6px 0 2px 0;
        }

        /* Suggestion chips: st.markdown can't actually wrap subsequent
           st.button calls (Streamlit renders them as separate sibling
           elements regardless of the HTML written here), so equal-height
           buttons are handled below via the column-stretch rule instead --
           this class is kept only in case it's useful for non-widget content. */
        .rr-chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 14px 0; }

        /* Make buttons placed in st.columns() stretch to match the tallest
           button in their row, so a longer label that wraps to two lines
           (e.g. a suggestion chip) doesn't leave the row looking ragged
           next to single-line buttons beside it. */
        [data-testid="stHorizontalBlock"] { align-items: stretch; }
        [data-testid="column"] [data-testid="stButton"] { height: 100%; }
        [data-testid="column"] [data-testid="stButton"] button {
            height: 100%; white-space: normal; line-height: 1.3;
        }

        /* Chat-app layout: a centered, narrow column instead of a full-width
           dashboard -- this is the single biggest lever for making the app
           feel like ChatGPT/Claude rather than an admin panel. padding-top
           is deliberately generous (not a small value like 1.5rem) because
           Streamlit's own fixed toolbar (the bar with the "Deploy" button)
           overlaps anything closer to the top -- too little clearance here
           renders the topbar partially hidden underneath it. */
        [data-testid="stMainBlockContainer"] {
            max-width: 760px;
            margin: 0 auto;
            padding-top: 4rem;
        }

        /* Slim top bar (replaces the old big gradient hero) */
        .rr-topbar {
            display: flex; align-items: center; justify-content: space-between;
            margin-bottom: 14px;
        }
        .rr-topbar-title {
            display: flex; align-items: center; gap: 8px;
            font-family: 'Space Grotesk', sans-serif; font-weight: 600;
            font-size: 1.05rem; color: var(--ink);
        }
        .rr-privacy-pill {
            display: inline-flex; align-items: center; gap: 6px;
            font-size: 0.78rem; color: #0F7B57;
            background: #E4F7EF; border-radius: 20px; padding: 4px 10px;
        }

        /* Per-message "kept private" badge -- plain language, no jargon,
           quiet enough not to compete with the actual answer text */
        .rr-privacy-badge {
            display: inline-flex; align-items: center; gap: 5px;
            font-size: 0.74rem; color: #0F7B57;
            background: #E4F7EF; border-radius: 20px; padding: 2px 9px;
            margin-top: 4px;
        }
        </style>
        """,
        unsafe_allow_html=True,
    )


def section_header(icon: str, title: str):
    st.markdown(
        f'<div class="rr-section-title"><span>{icon}</span><span>{title}</span></div>',
        unsafe_allow_html=True,
    )


inject_css()


# ---------------------------------------------------------------------------
# LLM call -- provider-agnostic. Works with any OpenAI-compatible
# /chat/completions endpoint (Groq, Together, Fireworks, OpenRouter,
# self-hosted vLLM, etc.) -- swapping providers is changing LLM_BASE_URL /
# LLM_API_KEY / LLM_MODEL, not code.
# ---------------------------------------------------------------------------
def call_llm(system_prompt: str, user_prompt: str, api_key: str, model: str,
             temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 400) -> str:
    """
    Only the masked/tokenized text you pass in ever leaves this machine --
    raw values are swapped back in locally after the response comes back.
    """
    if not api_key:
        raise RuntimeError("No API key is set")

    response = requests.post(
        LLM_BASE_URL,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        },
        timeout=60,
    )
    response.raise_for_status()
    data = response.json()
    return data["choices"][0]["message"]["content"].strip()


# A second, server-side gate: block anything that still looks like raw PII
# from ever reaching the model, in case the detector above missed something.
RAW_PII_PATTERNS = [
    re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+"),
    re.compile(r"\b(?:\d[ -]?){13,16}\b"),
    re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"),
]


def looks_unmasked(text: str) -> bool:
    return any(p.search(text) for p in RAW_PII_PATTERNS)


def find_leaked_values(df: pd.DataFrame, masked_df: pd.DataFrame, col_types: dict,
                        enabled_columns: set) -> list:
    """
    Hard last-line check, narrowly scoped to atomic/structured fields only --
    names, emails, phone numbers, ID numbers, and anything else
    classify_column()/classify_cell() assigns a type to. For those columns,
    masking is deterministic (regex/header-driven), so if a cell's masked
    value still equals its raw value, that's a genuine masking bug (a code
    path that skipped it, a stale cached dataframe, whatever the cause) and
    is worth blocking on.

    Free-text/unclassified columns (col_types.get(col) is falsy) are
    intentionally never scanned here, regardless of whether they're enabled.
    Masking inside long prose is NER-driven and best-effort by design (see
    mask_dataframe) -- NER missing an entity in a paragraph is an accepted
    limitation, not something a hard gate should block a whole request over.
    Likewise, disabled (unticked) columns are never scanned: unticking a
    column is an explicit, intentional choice to send it raw, so its content
    must never factor into this decision, including as an explanation for a
    leak found elsewhere.

    Returns a list of (column, raw_value) for cells that failed to mask.
    """
    if df is None or masked_df is None:
        return []
    leaks = []
    for col in df.columns:
        if col not in enabled_columns:
            continue
        if not col_types.get(col):  # unclassified/free-text -> out of scope entirely
            continue
        for i, raw_val in df[col].items():
            if pd.isna(raw_val) or str(raw_val).strip() == "":
                continue
            masked_val = masked_df.at[i, col] if i in masked_df.index else None
            if str(masked_val) == str(raw_val):
                leaks.append((col, str(raw_val).strip()))
    return leaks


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------
defaults = {
    "session_id": lambda: str(uuid.uuid4()),
    "token_counters": lambda: {},
    "df": lambda: None,
    "col_types": lambda: {},
    "column_enabled": lambda: {},
}
for key, factory in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = factory()

SESSION_ID = st.session_state.session_id

# Chat persistence. chat_history in session_state is just a display cache
# for the active chat_id -- the source of truth is mapping_store.db, so a
# browser refresh (which wipes session_state) reloads the same chat instead
# of losing it. No auth yet, so "most recent chat" is app-wide, not
# per-user -- revisit once auth exists.
if "active_chat_id" not in st.session_state:
    existing_chats = store.list_chats()
    if existing_chats:
        st.session_state.active_chat_id = existing_chats[0]["chat_id"]
    else:
        st.session_state.active_chat_id = store.create_chat()
    st.session_state.chat_history = store.get_chat_messages(st.session_state.active_chat_id)


def get_active_llm_config():
    """
    Single source of truth for which key/model the app actually uses:
    admin-set global config (DB) > .env > hardcoded default. Deliberately
    NOT cached in session_state -- these are meant to be global (admin sets
    once, applies to everyone), so every session needs to see a change
    immediately, not just the session that made it.
    """
    api_key = store.get_admin_config("llm_api_key", LLM_API_KEY_ENV)
    model = store.get_admin_config("llm_model", LLM_MODEL_DEFAULT)
    return api_key, model


# ---------------------------------------------------------------------------
# Masking helpers
# ---------------------------------------------------------------------------
def _looks_like_free_text(value: str) -> bool:
    """Heuristic: is this cell prose (worth an NER pass) rather than a
    single structured token (email/phone/name-only) that regex already handles?"""
    s = str(value).strip()
    return len(s) > 20 and len(s.split()) >= 4


def _col_tag(col) -> str:
    """Short, token-safe tag derived from a column name, used to keep tokens
    from different columns of the same PII type distinguishable
    (e.g. Aadhaar vs PAN vs AccountNumber vs CustomerID all being "ID" type
    would otherwise collapse into one ambiguous ID_1, ID_2... sequence)."""
    tag = re.sub(r"[^A-Za-z0-9]+", "", str(col)).upper()
    return tag[:14] or "COL"


def mask_free_text_cell(text: str, min_confidence: float, use_ner: bool = True) -> str:
    """
    Masks PII spans found inside a piece of free text, leaving the rest
    intact. Used both for free-text spreadsheet cells and for the raw text
    a user types into chat -- same integrity principle either way: the
    model still sees surrounding context, just not the PII buried in it.

    Two layers, run every time regardless of the use_ner toggle:
    1. Deterministic regex (email/phone/ID/IP) -- doesn't depend on any
       confidence threshold. Added after testing showed a real phone
       number embedded in a sentence (e.g. "call her at 9876543210")
       scored only 0.40 confidence from Presidio's NER phone recognizer --
       below the app's own 0.6 default -- and nothing else caught it.
    2. NER (names/locations/etc, only if use_ner is True) -- inherently
       best-effort for the fuzzy cases regex can't define a format for.
    """
    findings = scan_free_text_structured(text)
    if use_ner:
        findings = findings + ner_detection.analyze_text(text, min_confidence)
    if not findings:
        return text
    out = text
    seen = sorted({(f[0], f[1]) for f in findings}, key=lambda x: -len(x[0]))
    for entity_text, internal_type in seen:
        token = store.get_or_create_token(
            SESSION_ID, internal_type, entity_text, st.session_state.token_counters
        )
        out = out.replace(entity_text, token)
    return out


def _replace_known_values(text: str, known_values: dict) -> str:
    """
    Deterministic pass: replaces any occurrence of a value we ALREADY know is
    sensitive (because it's a real cell value seen elsewhere in this same
    sheet, e.g. a city from the Home City column) wherever it appears in free
    text, using the SAME token already assigned to that value.

    This exists because NER confidence isn't reliable for every phrasing --
    e.g. "the Chennai office" uses a city name adjectivally, a grammatical
    role general-purpose NER models are weaker at tagging than "in Chennai".
    For values we already positively know are sensitive from elsewhere in
    the sheet, we don't need to depend on NER catching them a second time.
    Longest values are replaced first so e.g. "Priya Nair" doesn't get
    partially clobbered by a shorter unrelated match.
    """
    if not known_values:
        return text
    for raw_value in sorted(known_values.keys(), key=len, reverse=True):
        if len(raw_value) < 3:
            continue  # skip trivially short known values, too many false positives
        token = known_values[raw_value]
        text = re.sub(r"\b" + re.escape(raw_value) + r"\b", token, text, flags=re.IGNORECASE)
    return text


def mask_dataframe(df: pd.DataFrame, col_types: dict, use_ner: bool, ner_confidence: float,
                    disabled_columns: set = frozenset()) -> pd.DataFrame:
    masked = df.copy()
    known_values = {}  # normalized value -> token, built while masking structured columns

    # Pass 1: structured/whole-cell columns. Deterministic (regex/header-driven),
    # and also builds the known-value index pass 2 uses below.
    for col in df.columns:
        if col in disabled_columns:
            continue
        col_type = col_types.get(col)
        for i, val in df[col].items():
            if pd.isna(val) or str(val).strip() == "":
                continue
            cell_type = col_type or classify_cell(val)
            if cell_type:
                token_key = f"{cell_type}_{_col_tag(col)}"
                token = store.get_or_create_token(
                    SESSION_ID, token_key, val, st.session_state.token_counters
                )
                masked.at[i, col] = token
                known_values[str(val).strip().lower()] = token

    # Pass 2: free-text columns. Known-value literal replace first (catches
    # anything already seen elsewhere in the sheet, regardless of NER
    # confidence), then NER for anything genuinely novel (e.g. "Andheri",
    # which only ever appears in a note, never in a structured column).
    for col in df.columns:
        if col in disabled_columns:
            continue
        col_type = col_types.get(col)
        for i, val in df[col].items():
            if pd.isna(val) or str(val).strip() == "":
                continue
            cell_type = col_type or classify_cell(val)
            if cell_type:
                continue  # already handled in pass 1
            if _looks_like_free_text(val):
                text = _replace_known_values(str(val), known_values)
                text = mask_free_text_cell(text, ner_confidence, use_ner)
                masked.at[i, col] = text
    st.session_state["_known_values"] = known_values
    return masked


def unmask_text(text: str) -> str:
    """
    Swaps tokens back to their real values. Matching is case-insensitive and
    whitespace-tolerant on purpose: smaller free models sometimes reproduce a
    token as "[person_name_1]" or "[ PERSON_NAME_1 ]" instead of exactly
    "[PERSON_NAME_1]" -- a harmless formatting drift that would otherwise
    silently break a plain, case-sensitive string replace and leave the raw
    token visible in the answer. A bracketed pattern that doesn't match any
    known token (e.g. the model invented one) is left as-is rather than
    guessed at.
    """
    reverse_map = store.get_reverse_map(SESSION_ID)
    if not reverse_map:
        return text
    lookup = {tok.upper(): orig for tok, orig in reverse_map.items()}

    def _replace(match):
        raw = match.group(0)
        normalized = "[" + re.sub(r"\s+", "", raw[1:-1]).upper() + "]"
        return str(lookup.get(normalized, raw))

    return re.sub(r"\[\s*[A-Za-z0-9_]+\s*\]", _replace, text)


def build_masked_context(masked_df: pd.DataFrame, max_rows=200) -> tuple[str, bool]:
    """Returns (csv_text, was_truncated). Callers must tell the model (and the
    user) when was_truncated is True -- otherwise a partial view gets treated
    as the complete dataset, which is a common source of wrong answers on
    larger files."""
    truncated = len(masked_df) > max_rows
    limited = masked_df.head(max_rows)
    return limited.to_csv(index=False), truncated


# ---------------------------------------------------------------------------
# Shared question handler -- works whether or not a spreadsheet is loaded
# ---------------------------------------------------------------------------
def process_question(question: str, use_ner: bool, ner_confidence: float, concise: bool = True):
    chat_id = st.session_state.active_chat_id
    is_first_message = not st.session_state.chat_history

    st.session_state.chat_history.append({"role": "user", "content": question})
    store.add_message(chat_id, "user", question)
    if is_first_message:
        title = " ".join(question.strip().split())
        if len(title) > 40:
            title = title[:40].rstrip() + "…"
        store.rename_chat(chat_id, title or "New chat")

    df = st.session_state.df
    known_values = st.session_state.get("_known_values", {})
    masked_question = _replace_known_values(question, known_values)
    masked_question = mask_free_text_cell(masked_question, ner_confidence, use_ner)

    length_instruction = (
        "Be concise: lead with the direct answer in 1-3 sentences, no preamble, "
        "no restating the question, no closing summary. Only go longer if the "
        "question explicitly asks for detail or a breakdown."
        if concise else
        "Give a complete, clearly explained answer, using more than a few "
        "sentences where it genuinely helps understanding."
    )
    max_tokens = 250 if concise else 800

    if df is not None:
        masked_df = st.session_state.get("_masked_df")
        masked_context, truncated = build_masked_context(masked_df)
        row_note = (
            f"You are seeing the first 200 of {len(masked_df)} total rows -- "
            "do not imply or assume you have the full dataset."
            if truncated else
            f"You are seeing all {len(masked_df)} rows of the dataset."
        )
        system_prompt = (
            "You are analyzing a spreadsheet where sensitive values have been "
            "replaced with placeholder tokens like [PERSON_NAME_1], [EMAIL_EMAIL_1], "
            "[ID_AADHAAR_2]. The part before the number hints at both the data type "
            "and the column it came from. Never claim to know the real values behind "
            "tokens. Answer using only the masked data provided -- do not guess or "
            "fill in values that aren't there. " + row_note + " When asked to "
            "identify a specific person (e.g. 'who earns the most'), refer to them "
            "using the token from the column that actually represents their name "
            "(tokens starting with PERSON_), not an ID/account/customer-number token, "
            "even if both appear in the same row. Always reproduce tokens exactly as "
            "given, including the surrounding brackets (e.g. [PERSON_NAME_1]) -- never "
            "paraphrase, reformat, or drop the brackets, or the value cannot be "
            "restored for display. If a question requires exact arithmetic across many "
            "rows (sums, medians, averages, counts), work through the rows carefully "
            "and show your row-by-row reasoning briefly before giving the final answer, "
            "rather than estimating. " + length_instruction
        )
        user_prompt = f"MASKED DATA (CSV):\n{masked_context}\n\nQUESTION: {masked_question}"
        payload_to_check = masked_context + masked_question
        truncation_warning = truncated
        leaked = find_leaked_values(
            df, masked_df, st.session_state.col_types,
            st.session_state.get("_masked_columns", set()),
        )
    else:
        system_prompt = (
            "You are a helpful, general-purpose assistant. Any personal information "
            "the user typed (names, emails, phone numbers, etc.) may have already "
            "been replaced with placeholder tokens like [PERSON_NAME_1] before "
            "reaching you, as a privacy safeguard -- if you see a token like that, "
            "treat it as a stand-in for that piece of information and use it "
            "naturally in your answer exactly as written, brackets included, rather "
            "than commenting on it or guessing what it says. " + length_instruction
        )
        user_prompt = masked_question
        payload_to_check = masked_question
        truncation_warning = False
        leaked = []

    if leaked:
        leaked_cols = sorted({col for col, _ in leaked})
        # up to 3 example values per column so this is diagnosable at a glance,
        # instead of just naming the column and leaving you to guess why
        examples_by_col = {}
        for col, val in leaked:
            examples_by_col.setdefault(col, [])
            if val not in examples_by_col[col] and len(examples_by_col[col]) < 3:
                examples_by_col[col].append(val)
        example_lines = "; ".join(
            f"{col}: {', '.join(examples_by_col[col])}" for col in leaked_cols
        )
        answer = (
            "Request blocked: masking failed for a structured field in "
            f"column(s) {', '.join(leaked_cols)} — its value is still raw in "
            f"the data about to be sent. Example(s): {example_lines}. Nothing "
            "was sent to the model. This is a masking bug, not a setting to "
            "adjust -- please report it."
        )
    elif looks_unmasked(payload_to_check):
        answer = (
            "Request blocked: content still looks like it contains unmasked "
            "personal data. Nothing was sent to the model."
        )
    else:
        try:
            active_api_key, active_model = get_active_llm_config()

            if is_first_message:
                # Best-effort: upgrade the truncated fallback title (set
                # above) to a short LLM-written one. Uses the already-masked
                # question -- the titling call must obey the same "nothing
                # raw leaves this machine" rule as the main answer. Silent
                # on failure: a plain-answer question shouldn't fail just
                # because the title polish call did.
                try:
                    title_raw = call_llm(
                        "Write a short title (3-6 words) summarizing the topic of "
                        "the user's message below. Plain text only -- no quotes, "
                        "no punctuation at the end, no preamble like 'Title:'.",
                        masked_question,
                        active_api_key, active_model,
                        temperature=0.3, max_tokens=16,
                    )
                    title = unmask_text(" ".join(title_raw.strip().split()))
                    title = title.strip(" \"'.")[:60]
                    if title:
                        store.rename_chat(chat_id, title)
                except Exception:
                    pass

            raw_text = call_llm(
                system_prompt, user_prompt,
                active_api_key, active_model,
                max_tokens=max_tokens,
            )
            answer = unmask_text(raw_text)
        except RuntimeError as e:
            answer = f"{e}. Ask an admin to set it up in Settings."
        except requests.exceptions.ConnectionError:
            answer = "Couldn't reach the AI service. Check your internet connection and try again."
        except Exception as e:
            answer = f"Couldn't reach the model: {e}"

    if truncation_warning:
        st.session_state["_last_truncation_warning"] = True
        st.session_state["_last_truncation_warning_rows"] = len(st.session_state.df)

    masked_count = len(set(re.findall(r"\[\s*[A-Za-z0-9_]+\s*\]", payload_to_check)))
    st.session_state.chat_history.append(
        {"role": "assistant", "content": answer, "masked_count": masked_count}
    )
    store.add_message(chat_id, "assistant", answer, masked_count)


def _export_chat_text(chat_id: str, title: str) -> str:
    """Plain-text transcript of a chat, always read fresh from the DB so it
    works for any chat in the list, not just the currently active one."""
    messages = store.get_chat_messages(chat_id)
    lines = [title, "=" * len(title), ""]
    for m in messages:
        speaker = "You" if m["role"] == "user" else "Privy"
        lines.append(f"{speaker}: {m['content']}")
        lines.append("")
    return "\n".join(lines)


def _safe_filename(title: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", title).strip("_")
    return safe[:50] or "chat"


# ---------------------------------------------------------------------------
# Sidebar -- part A: connection + detection settings. Runs before the file
# section below because use_ner/ner_confidence/concise are needed there.
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🔒 Privy")
    st.caption("Personal data stays on this device")

    if st.button("+ New chat", use_container_width=True):
        st.session_state.active_chat_id = store.create_chat()
        st.session_state.chat_history = []
        st.rerun()

    past_chats = store.list_chats()
    if past_chats:
        st.caption("Recent chats")
        for chat in past_chats[:20]:
            is_active = chat["chat_id"] == st.session_state.active_chat_id
            label = chat["title"] or "New chat"
            col_select, col_menu = st.columns([5, 1])
            if col_select.button(
                label,
                key=f"chat_{chat['chat_id']}",
                use_container_width=True,
                disabled=is_active,
            ):
                st.session_state.active_chat_id = chat["chat_id"]
                st.session_state.chat_history = store.get_chat_messages(chat["chat_id"])
                st.rerun()

            with col_menu.popover("⋮", use_container_width=True):
                renaming_key = f"_renaming_{chat['chat_id']}"
                is_renaming = st.session_state.get(renaming_key, False)

                if is_renaming:
                    new_title = st.text_input(
                        "Rename",
                        value=label,
                        key=f"rename_input_{chat['chat_id']}",
                        label_visibility="collapsed",
                        placeholder="Chat name",
                    )
                    col_save, col_cancel = st.columns(2)
                    if col_save.button(
                        "Save", key=f"rename_save_{chat['chat_id']}", use_container_width=True
                    ):
                        cleaned = new_title.strip()
                        if cleaned and cleaned != label:
                            store.rename_chat(chat["chat_id"], cleaned)
                        st.session_state[renaming_key] = False
                        st.rerun()
                    if col_cancel.button(
                        "Cancel", key=f"rename_cancel_{chat['chat_id']}", use_container_width=True
                    ):
                        st.session_state[renaming_key] = False
                        st.rerun()
                else:
                    if st.button(
                        "Rename", key=f"rename_btn_{chat['chat_id']}", use_container_width=True
                    ):
                        st.session_state[renaming_key] = True
                        st.rerun()

                    st.download_button(
                        "Export as .txt",
                        data=_export_chat_text(chat["chat_id"], label),
                        file_name=f"{_safe_filename(label)}.txt",
                        mime="text/plain",
                        key=f"export_{chat['chat_id']}",
                        use_container_width=True,
                    )

                    if st.button(
                        "Delete", key=f"del_{chat['chat_id']}", use_container_width=True
                    ):
                        store.delete_chat(chat["chat_id"])
                        if is_active:
                            # Deleted the chat you were looking at -- fall
                            # back to the next most recent one, or a fresh
                            # chat if that was the last one left.
                            remaining = store.list_chats()
                            if remaining:
                                st.session_state.active_chat_id = remaining[0]["chat_id"]
                                st.session_state.chat_history = store.get_chat_messages(remaining[0]["chat_id"])
                            else:
                                st.session_state.active_chat_id = store.create_chat()
                                st.session_state.chat_history = []
                        st.rerun()

    st.markdown("")

    active_api_key, active_model = get_active_llm_config()
    key_is_set = bool(active_api_key)
    status_dot = "🟢" if key_is_set else "🔴"
    status_text = "Connected" if key_is_set else "Not connected"

    with st.expander(f"⚙️ Settings   —   {status_dot} {status_text}", expanded=not key_is_set):
        st.caption(
            "Connected — an admin has set this up" if key_is_set
            else "Not connected — ask an admin to set up the API key below"
        )

        st.divider()
        concise = st.toggle("Concise answers", value=True,
                             help="Short, direct answers (1-3 sentences) with no preamble. "
                                  "Turn off for fuller explanations.")
        use_ner = st.toggle("Catch names in sentences", value=True,
                             help="Catches names/places embedded in free text, not just in dedicated "
                                  "fields. Emails, phone numbers and ID numbers are always caught "
                                  "regardless of this toggle.")
        ner_confidence = st.slider("Detection sensitivity", 0.3, 0.95, 0.6, 0.05,
                                    help="Higher = fewer false positives, but may miss some personal "
                                         "data. Lower = catches more, but may flag ordinary text.")

        st.divider()
        with st.expander("🔑 Admin"):
            if not ADMIN_PASSWORD:
                st.caption("Set ADMIN_PASSWORD in .env to enable admin access.")
            elif not st.session_state.get("_admin_unlocked"):
                pw = st.text_input("Admin password", type="password", key="_admin_pw_input")
                if st.button("Unlock"):
                    if pw and pw == ADMIN_PASSWORD:
                        st.session_state["_admin_unlocked"] = True
                        st.rerun()
                    else:
                        st.error("Incorrect password")
            else:
                st.caption("Unlocked for this session. Changes apply to everyone using this app.")
                new_key = st.text_input("API key", value=active_api_key, type="password")
                model_options = sorted(set(COMMON_MODELS + [active_model]))
                new_model = st.selectbox("Model", options=model_options,
                                          index=model_options.index(active_model))
                col_save, col_lock = st.columns(2)
                if col_save.button("Save", use_container_width=True):
                    store.set_admin_config("llm_api_key", new_key)
                    store.set_admin_config("llm_model", new_model)
                    st.success("Saved.")
                    st.rerun()
                if col_lock.button("Lock", use_container_width=True):
                    st.session_state["_admin_unlocked"] = False
                    st.rerun()


# ---------------------------------------------------------------------------
# Top bar + file attach. The checklist of what gets hidden now lives right
# here, next to the upload, instead of buried in a separate sidebar section
# -- it auto-expands the first time a file is uploaded, then collapses to a
# one-line summary with a "Review" button that reopens the same checklist.
# ---------------------------------------------------------------------------
st.markdown(
    '<div class="rr-topbar"><div class="rr-topbar-title">🔒 Privy</div></div>',
    unsafe_allow_html=True,
)

popover_label = (
    f"📎 {st.session_state['_uploaded_file_name']}"
    + (f" · {st.session_state['_mapped_count']} kept private" if st.session_state.get("_mapped_count") else "")
    if st.session_state.df is not None and st.session_state.get("_uploaded_file_name")
    else "📎 Attach a file"
)
with st.popover(popover_label):
    uploaded = st.file_uploader("Upload a spreadsheet", type=["xlsx", "xls", "csv"],
                                 label_visibility="collapsed")
    # file_uploader keeps returning the same file on every rerun until the user
    # removes it -- comparing file_id lets us process a given upload exactly once.
    if uploaded is not None and st.session_state.get("_uploaded_file_id") != uploaded.file_id:
        if uploaded.name.endswith(".csv"):
            new_df = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
        else:
            new_df = pd.read_excel(uploaded, dtype=str, keep_default_na=False)
        st.session_state.df = new_df
        st.session_state.col_types = classify_dataframe_columns(new_df)
        st.session_state.column_enabled = {
            col: (t is not None) for col, t in st.session_state.col_types.items()
        }
        st.session_state["_uploaded_file_id"] = uploaded.file_id
        st.session_state["_uploaded_file_name"] = uploaded.name
        st.session_state["_review_open"] = True  # force a look at what's about to be hidden

    df = st.session_state.df

    if df is not None:
        col_types = st.session_state.col_types
        st.caption(f"{st.session_state.get('_uploaded_file_name', 'Your file')} · reviewed automatically")

        if st.session_state.get("_review_open"):
            st.markdown("**What gets hidden**")
            st.caption(
                "Untick anything that shouldn't be hidden, or tick something "
                "the recognizer missed."
            )
            for col in df.columns:
                suggested_type = col_types.get(col)
                current = st.session_state.column_enabled.get(col, suggested_type is not None)
                sub = TYPE_LABEL.get(suggested_type, "not flagged") if suggested_type else "not flagged"
                new_val = st.checkbox(f"{col}  ·  _{sub}_", value=current, key=f"colchk_{col}")
                st.session_state.column_enabled[col] = new_val
            if st.button("Looks good", use_container_width=True):
                st.session_state["_review_open"] = False
                st.rerun()
        else:
            enabled_count = sum(1 for c in df.columns if st.session_state.column_enabled.get(c))
            st.caption(f"{enabled_count} of {len(df.columns)} fields will be hidden")
            if st.button("Review what's hidden", use_container_width=True):
                st.session_state["_review_open"] = True
                st.rerun()

        st.divider()
        if st.button("Remove file"):
            st.session_state.df = None
            st.session_state.col_types = {}
            st.session_state.column_enabled = {}
            st.session_state.pop("_masked_df", None)
            st.session_state.pop("_masked_columns", None)
            st.session_state.pop("_disabled_columns", None)
            st.session_state.pop("_uploaded_file_id", None)
            st.session_state.pop("_uploaded_file_name", None)
            st.session_state.pop("_review_open", None)
            st.rerun()

df = st.session_state.df


# ---------------------------------------------------------------------------
# Masking runs here, right after the checklist above is read, so both the
# file chip below and the sidebar preview see up-to-date results.
# ---------------------------------------------------------------------------
mapped_count = 0
if df is not None:
    col_types = st.session_state.col_types
    disabled_columns = {
        col for col in df.columns
        if not st.session_state.column_enabled.get(col, col_types.get(col) is not None)
    }
    manual_enabled_untyped = {
        col for col in df.columns
        if col_types.get(col) is None and st.session_state.column_enabled.get(col) and col not in disabled_columns
    }
    effective_col_types = {
        col: (None if col in manual_enabled_untyped else col_types.get(col))
        for col in df.columns
    }
    masked_df = mask_dataframe(df, effective_col_types, use_ner, ner_confidence, disabled_columns)
    st.session_state["_masked_df"] = masked_df
    st.session_state["_masked_columns"] = set(df.columns) - disabled_columns
    st.session_state["_disabled_columns"] = disabled_columns
    mapped_count = store.session_entry_count(SESSION_ID)
    st.session_state["_mapped_count"] = mapped_count
else:
    st.session_state.pop("_masked_df", None)
    st.session_state.pop("_masked_columns", None)
    st.session_state.pop("_disabled_columns", None)
    st.session_state.pop("_mapped_count", None)


# ---------------------------------------------------------------------------
# Sidebar -- part B: "Data & privacy". Deeper technical detail only --
# the raw/masked preview and the local token map. The checklist itself now
# lives in the attach popover above (single source of truth), not here.
# ---------------------------------------------------------------------------
if df is not None:
    with st.sidebar:
        with st.expander("🔍 Data & privacy", expanded=False):
            show_masked = st.toggle("Show hidden version", value=True)
            st.dataframe(st.session_state["_masked_df"] if show_masked else df,
                         use_container_width=True, height=240)

            with st.expander(f"Local record ({mapped_count} entries — never sent anywhere)"):
                reverse_map = store.get_reverse_map(SESSION_ID)
                if reverse_map:
                    map_df = pd.DataFrame([{"token": t, "original": o} for t, o in reverse_map.items()])
                    st.dataframe(map_df, use_container_width=True, height=200)
                else:
                    st.caption("Nothing hidden yet.")

            if st.button("Clear this file's local record", use_container_width=True):
                store.clear_session(SESSION_ID)
                st.session_state.token_counters = {}
                st.rerun()


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
if st.session_state.pop("_last_truncation_warning", None):
    total_rows = st.session_state.get("_last_truncation_warning_rows", 0)
    st.warning(
        f"This file has {total_rows} rows — only the first 200 were used for the "
        "last answer, so it may be incomplete."
    )

if not st.session_state.chat_history:
    st.markdown('<div class="rr-chip-row">', unsafe_allow_html=True)
    suggestions = (
        ["What looks sensitive in this file?", "Summarize this data", "Any patterns worth flagging?"]
        if df is not None else
        ["What can this app do?", "Draft a polite follow-up email", "Explain how my privacy is protected"]
    )
    chip_cols = st.columns(len(suggestions))
    for i, s in enumerate(suggestions):
        if chip_cols[i].button(s, key=f"chip_{i}"):
            process_question(s, use_ner, ner_confidence, concise)
            st.rerun()
    st.markdown('</div>', unsafe_allow_html=True)

for msg in st.session_state.chat_history:
    with st.chat_message(msg["role"]):
        st.write(msg["content"])
        if msg["role"] == "assistant" and msg.get("masked_count"):
            n = msg["masked_count"]
            label = "1 personal detail kept private" if n == 1 else f"{n} personal details kept private"
            st.markdown(f'<span class="rr-privacy-badge">🛡️ {label}</span>', unsafe_allow_html=True)

placeholder = "Ask about this file..." if df is not None else "Message Privy"
question = st.chat_input(placeholder)
if question:
    process_question(question, use_ner, ner_confidence, concise)
    st.rerun()

st.markdown(
    '<p style="text-align:center; font-size:0.72rem; color:#98A2B3; margin-top:6px;">'
    'Your files never leave this device. Only hidden text is sent to the AI.</p>',
    unsafe_allow_html=True,
)
