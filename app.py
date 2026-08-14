"""
app.py
------
Streamlit UI: upload an Excel/CSV file (optional), auto-detect PII columns
(smart recognizer, user-adjustable), mask, chat with Groq's free-tier cloud
model, unmask the response before displaying it. Chat also works without any
file uploaded -- in that case the same PII-masking safeguard is applied to
your typed messages instead of spreadsheet cells.

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

from detection import classify_dataframe_columns, classify_cell, TYPE_LABEL
import mapping_store as store
import ner_detection

load_dotenv()
store.init_db()

st.set_page_config(page_title="Redact / Relay", page_icon="🔒", layout="wide")

GROQ_MODEL_DEFAULT = os.environ.get("GROQ_MODEL", "llama-3.1-8b-instant")
GROQ_API_KEY_ENV = os.environ.get("GROQ_API_KEY", "")
DEFAULT_TEMPERATURE = float(os.environ.get("MODEL_TEMPERATURE", "0.1"))

COMMON_GROQ_MODELS = [
    "llama-3.1-8b-instant",
    "llama-3.3-70b-versatile",
    "gemma2-9b-it",
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

        /* Hero header -- solid dark navy instead of a gradient that trailed
           into a pale teal, which was washing out the light-grey subtitle text */
        .rr-hero {
            padding: 18px 22px;
            border-radius: 16px;
            background: linear-gradient(120deg, #14162B 0%, #1B1E38 100%);
            margin-bottom: 18px;
        }
        .rr-hero h1 {
            color: #FFFFFF !important;
            margin: 0 0 4px 0;
            font-size: 1.6rem;
        }
        .rr-hero p {
            color: #DDE0EE !important;
            margin: 0;
            font-size: 0.92rem;
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

        /* Suggestion chips */
        .rr-chip-row { display: flex; gap: 8px; flex-wrap: wrap; margin: 6px 0 14px 0; }
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
# Groq call
# ---------------------------------------------------------------------------
def call_groq(system_prompt: str, user_prompt: str, api_key: str, model: str,
              temperature: float = DEFAULT_TEMPERATURE, max_tokens: int = 400) -> str:
    """
    Calls Groq's free-tier cloud API (OpenAI-compatible endpoint). Only the
    masked/tokenized text you pass in ever leaves this machine -- raw values
    are swapped back in locally after the response comes back.
    """
    if not api_key:
        raise RuntimeError("No Groq API key is set")

    response = requests.post(
        "https://api.groq.com/openai/v1/chat/completions",
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
    "chat_history": lambda: [],
    "df": lambda: None,
    "col_types": lambda: {},
    "column_enabled": lambda: {},
    "groq_api_key": lambda: GROQ_API_KEY_ENV,
    "groq_model": lambda: GROQ_MODEL_DEFAULT,
}
for key, factory in defaults.items():
    if key not in st.session_state:
        st.session_state[key] = factory()

SESSION_ID = st.session_state.session_id


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


def mask_free_text_cell(text: str, min_confidence: float) -> str:
    """
    Masks only the PII spans found inside a piece of free text, leaving the
    rest intact. Used both for free-text spreadsheet cells and for the raw
    text a user types into chat -- same integrity principle either way: the
    model still sees surrounding context, just not the PII buried in it.
    """
    findings = ner_detection.analyze_text(text, min_confidence)
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
                if use_ner:
                    text = mask_free_text_cell(text, ner_confidence)
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
    st.session_state.chat_history.append({"role": "user", "content": question})

    df = st.session_state.df
    known_values = st.session_state.get("_known_values", {})
    masked_question = _replace_known_values(question, known_values)
    if use_ner:
        masked_question = mask_free_text_cell(masked_question, ner_confidence)

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
            raw_text = call_groq(
                system_prompt, user_prompt,
                st.session_state.groq_api_key, st.session_state.groq_model,
                max_tokens=max_tokens,
            )
            answer = unmask_text(raw_text)
        except RuntimeError as e:
            answer = f"{e}. Add a free key from https://console.groq.com/keys in the sidebar."
        except requests.exceptions.ConnectionError:
            answer = "Couldn't reach Groq. Check your internet connection and try again."
        except Exception as e:
            answer = f"Couldn't reach the model: {e}"

    if truncation_warning:
        st.session_state["_last_truncation_warning"] = True
        st.session_state["_last_truncation_warning_rows"] = len(st.session_state.df)

    st.session_state.chat_history.append({"role": "assistant", "content": answer})


# ---------------------------------------------------------------------------
# Sidebar
# ---------------------------------------------------------------------------
with st.sidebar:
    st.markdown("### 🔒 Redact / Relay")
    st.caption("Local PII masking · Groq for answers")
    st.markdown("")

    key_is_set = bool(st.session_state.groq_api_key)
    status_dot = "🟢" if key_is_set else "🔴"
    status_text = "Connected" if key_is_set else "No API key set"

    with st.expander(f"⚙️ Settings   —   {status_dot} {status_text}", expanded=not key_is_set):
        st.markdown("**Groq API key**")
        key_input = st.text_input(
            "Groq API key", value=st.session_state.groq_api_key, type="password",
            label_visibility="collapsed", placeholder="gsk_...",
            help="Overrides GROQ_API_KEY from .env for this session only -- never written to disk. "
                 "Get a free key at https://console.groq.com/keys",
        )
        st.session_state.groq_api_key = key_input
        st.caption("Using session key" if key_input != GROQ_API_KEY_ENV and key_input
                   else ("Using key from .env" if GROQ_API_KEY_ENV else "Paste a key to get started"))

        st.markdown("**Model**")
        model_options = sorted(set(COMMON_GROQ_MODELS + [st.session_state.groq_model]))
        model_input = st.selectbox(
            "Model", options=model_options, index=model_options.index(st.session_state.groq_model),
            label_visibility="collapsed",
        )
        st.session_state.groq_model = model_input

    with st.expander("🎛️ Detection settings", expanded=False):
        use_ner = st.toggle("Use NER", value=True,
                             help="Catches names/locations embedded in sentences -- both in spreadsheet "
                                  "free-text columns and in chat messages you type. Structured values "
                                  "(email, phone, ID) are always caught by regex regardless of this toggle.")
        ner_confidence = st.slider("NER confidence threshold", 0.3, 0.95, 0.6, 0.05,
                                    help="Higher = fewer false positives, but may miss some PII. "
                                         "Lower = catches more, but may flag ordinary text.")
        st.divider()
        concise = st.toggle("Concise answers", value=True,
                             help="Short, direct answers (1-3 sentences) with no preamble. "
                                  "Turn off for fuller explanations.")

    st.divider()
    bcol1, bcol2 = st.columns(2)
    if bcol1.button("🗑️ Chat", use_container_width=True, help="Clear the conversation"):
        st.session_state.chat_history = []
        st.rerun()
    if bcol2.button("🗑️ Mapping", use_container_width=True, help="Clear the local PII token mapping"):
        store.clear_session(SESSION_ID)
        st.session_state.token_counters = {}
        st.rerun()



# ---------------------------------------------------------------------------
# Header
# ---------------------------------------------------------------------------
st.markdown(
    """
    <div class="rr-hero">
        <h1>🔒 Redact / Relay</h1>
        <p>Ask questions about a spreadsheet, or just chat — personal data never
        leaves this machine unmasked.</p>
    </div>
    """,
    unsafe_allow_html=True,
)


# ---------------------------------------------------------------------------
# Optional file upload
# ---------------------------------------------------------------------------
with st.expander("📄 Add a spreadsheet (optional)", expanded=(st.session_state.df is None)):
    uploaded = st.file_uploader("Upload a spreadsheet", type=["xlsx", "xls", "csv"],
                                 label_visibility="collapsed")
    # file_uploader keeps returning the same file on every rerun until the user
    # removes it -- without this guard, processing it below would call st.rerun()
    # on every single rerun, looping forever and never reaching the rest of the
    # page. Comparing file_id lets us process a given upload exactly once.
    if uploaded is not None and st.session_state.get("_uploaded_file_id") != uploaded.file_id:
        if uploaded.name.endswith(".csv"):
            df = pd.read_csv(uploaded, dtype=str, keep_default_na=False)
        else:
            df = pd.read_excel(uploaded, dtype=str, keep_default_na=False)
        st.session_state.df = df
        st.session_state.col_types = classify_dataframe_columns(df)
        st.session_state.column_enabled = {
            col: (t is not None) for col, t in st.session_state.col_types.items()
        }
        st.session_state["_uploaded_file_id"] = uploaded.file_id
        st.rerun()
    if st.session_state.df is not None and st.button("Remove spreadsheet"):
        st.session_state.df = None
        st.session_state.col_types = {}
        st.session_state.column_enabled = {}
        st.session_state.pop("_masked_df", None)
        st.session_state.pop("_masked_columns", None)
        st.session_state.pop("_disabled_columns", None)
        st.session_state.pop("_uploaded_file_id", None)
        st.rerun()

df = st.session_state.df


# ---------------------------------------------------------------------------
# Data panel (only when a spreadsheet is loaded)
# ---------------------------------------------------------------------------
if df is not None:
    col_types = st.session_state.col_types
    detected = sorted({TYPE_LABEL[t] for t in col_types.values() if t})

    with st.container(border=True):
        section_header("📊", "Your data")

        with st.expander("🧠 Smart-detected columns — review and adjust before masking", expanded=False):
            st.caption(
                "The recognizer suggests which columns look sensitive based on header names "
                "and sample values. Untick anything that shouldn't be masked, or tick a column "
                "the recognizer missed."
            )
            review_cols = st.columns(4)
            for idx, col in enumerate(df.columns):
                suggested_type = col_types.get(col)
                with review_cols[idx % 4]:
                    current = st.session_state.column_enabled.get(col, suggested_type is not None)
                    sub = TYPE_LABEL.get(suggested_type, "not flagged") if suggested_type else "not flagged"
                    new_val = st.checkbox(f"{col}  ·  _{sub}_", value=current, key=f"colchk_{col}")
                    st.session_state.column_enabled[col] = new_val

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

        mcol1, mcol2, mcol3, mcol4 = st.columns([1, 1, 1.4, 1])
        mcol1.metric("Rows", len(df))
        mcol2.metric("Tokenized values", mapped_count)
        mcol3.metric("Types detected", len(detected))
        show_masked = mcol4.toggle("Show masked", value=True)
        if detected:
            st.caption("Detected: " + ", ".join(detected))

        st.dataframe(masked_df if show_masked else df, use_container_width=True, height=320)

        with st.expander(f"🔐 Local token map ({mapped_count} entries — never sent anywhere)"):
            reverse_map = store.get_reverse_map(SESSION_ID)
            if reverse_map:
                map_df = pd.DataFrame([{"token": t, "original": o} for t, o in reverse_map.items()])
                st.dataframe(map_df, use_container_width=True, height=200)
            else:
                st.caption("Nothing masked yet.")
else:
    st.session_state.pop("_masked_df", None)
    st.session_state.pop("_masked_columns", None)
    st.session_state.pop("_disabled_columns", None)


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
with st.container(border=True):
    section_header("💬", "Chat")
    if df is not None:
        st.caption("Only masked tokens + your question are sent to Groq — never raw spreadsheet values.")
    else:
        st.caption("Chatting without a spreadsheet — personal details you type are still masked before being sent to Groq.")

    if st.session_state.pop("_last_truncation_warning", None):
        total_rows = st.session_state.get("_last_truncation_warning_rows", 0)
        st.warning(
            f"Only the first 200 of {total_rows} rows were sent to the model for the last "
            "question — its answer about the full dataset may be incomplete."
        )

    if not st.session_state.chat_history:
        st.markdown('<div class="rr-chip-row">', unsafe_allow_html=True)
        suggestions = (
            ["Which columns look sensitive?", "Summarize this data", "Any patterns worth flagging?"]
            if df is not None else
            ["What can this app do?", "Draft a polite follow-up email", "Explain PII masking simply"]
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

placeholder = ("e.g. Which rows have salaries above the median?" if df is not None
               else "Ask me anything...")
question = st.chat_input(placeholder)
if question:
    process_question(question, use_ner, ner_confidence, concise)
    st.rerun()
