"""
masking.py
----------
Core PII masking pipeline. Ported from the original Streamlit app's inline
functions -- same logic, but Streamlit-free: every function takes its
session_id and token counters explicitly instead of reading
st.session_state, so it behaves the same whether called from a request
handler, a test, or anything else.

"session_id" here = chat_id. Each chat gets its own token namespace in
mapping_store, the same way each browser session did in the Streamlit
version -- a chat is the closest equivalent unit ("one continuous
conversation about one file"), so token numbering (PERSON_1, PERSON_2...)
stays stable and consistent across every question asked within that chat.
"""

import re
import pandas as pd

from . import mapping_store as store
from . import ner_detection
from .detection import classify_cell, scan_free_text_structured

# Second, server-side gate: block anything that still looks like raw PII
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
    """Hard last-line check, narrowly scoped to atomic/structured fields only
    -- see the original app.py docstring for the full reasoning. Unchanged
    from the Streamlit version; this function never touched session_state."""
    if df is None or masked_df is None:
        return []
    leaks = []
    for col in df.columns:
        if col not in enabled_columns:
            continue
        if not col_types.get(col):
            continue
        for i, raw_val in df[col].items():
            if pd.isna(raw_val) or str(raw_val).strip() == "":
                continue
            masked_val = masked_df.at[i, col] if i in masked_df.index else None
            if str(masked_val) == str(raw_val):
                leaks.append((col, str(raw_val).strip()))
    return leaks


def _looks_like_free_text(value: str) -> bool:
    s = str(value).strip()
    return len(s) > 20 and len(s.split()) >= 4


def _col_tag(col) -> str:
    tag = re.sub(r"[^A-Za-z0-9]+", "", str(col)).upper()
    return tag[:14] or "COL"


def mask_free_text_cell(text: str, session_id: str, counters: dict,
                         min_confidence: float, use_ner: bool = True) -> str:
    """Masks PII spans found inside free text, leaving the rest intact. Used
    both for free-text spreadsheet cells and for a chat question's raw text."""
    findings = scan_free_text_structured(text)
    if use_ner:
        findings = findings + ner_detection.analyze_text(text, min_confidence)
    if not findings:
        return text
    out = text
    seen = sorted({(f[0], f[1]) for f in findings}, key=lambda x: -len(x[0]))
    for entity_text, internal_type in seen:
        token = store.get_or_create_token(session_id, internal_type, entity_text, counters)
        out = out.replace(entity_text, token)
    return out


def _replace_known_values(text: str, known_values: dict) -> str:
    if not known_values:
        return text
    for raw_value in sorted(known_values.keys(), key=len, reverse=True):
        if len(raw_value) < 3:
            continue
        token = known_values[raw_value]
        text = re.sub(r"\b" + re.escape(raw_value) + r"\b", token, text, flags=re.IGNORECASE)
    return text


def mask_dataframe(df: pd.DataFrame, col_types: dict, session_id: str, counters: dict,
                    use_ner: bool, ner_confidence: float,
                    disabled_columns: set = frozenset()) -> tuple[pd.DataFrame, dict]:
    """Same two-pass approach as the original. Returns (masked_df,
    known_values) -- the caller doesn't need to persist known_values itself;
    mapping_store.get_known_values(session_id) reconstructs the same dict
    from the DB on a later request (see that function's docstring)."""
    # Cast to object dtype up front, not just .copy(). Any column can end
    # up holding a string token (e.g. a phone-number column pandas infers
    # as int64) -- newer pandas (confirmed on 3.0.2) raises TypeError on
    # `.at[i, col] = "[TOKEN]"` against a numeric-dtype column instead of
    # silently converting it, which crashed every upload where a masked
    # column happened to be numeric. astype(object) up front sidesteps
    # that entirely; to_csv() output is unaffected for ordinary values.
    masked = df.astype(object)
    known_values = {}

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
                token = store.get_or_create_token(session_id, token_key, val, counters)
                masked.at[i, col] = token
                known_values[str(val).strip().lower()] = token

    for col in df.columns:
        if col in disabled_columns:
            continue
        col_type = col_types.get(col)
        for i, val in df[col].items():
            if pd.isna(val) or str(val).strip() == "":
                continue
            cell_type = col_type or classify_cell(val)
            if cell_type:
                continue
            if _looks_like_free_text(val):
                text = _replace_known_values(str(val), known_values)
                text = mask_free_text_cell(text, session_id, counters, ner_confidence, use_ner)
                masked.at[i, col] = text

    return masked, known_values


def unmask_text(text: str, session_id: str) -> str:
    """Swaps tokens back to their real values. See app.py's original
    docstring for why matching is case/whitespace-tolerant."""
    reverse_map = store.get_reverse_map(session_id)
    if not reverse_map:
        return text
    lookup = {tok.upper(): orig for tok, orig in reverse_map.items()}

    def _replace(match):
        raw = match.group(0)
        normalized = "[" + re.sub(r"\s+", "", raw[1:-1]).upper() + "]"
        return str(lookup.get(normalized, raw))

    return re.sub(r"\[\s*[A-Za-z0-9_]+\s*\]", _replace, text)


_TOKEN_PATTERN = re.compile(r"\[\s*[A-Za-z0-9_]+\s*\]")


def stream_unmask(chunks, session_id: str):
    """Same job as unmask_text(), but for a live stream of text deltas
    instead of one complete string.

    The problem unmask_text() doesn't have to deal with: a provider can
    split a single token across two separate chunks (e.g. one chunk ends
    "...[PERSON_" and the next starts "1] said..."). Naively unmasking each
    chunk independently would miss that token entirely -- half of it isn't
    a match against the pattern in either chunk alone.

    Fix: buffer text and only emit the portion up to the last unresolved
    "[" (a "[" with no ']' after it yet, i.e. a token that might still be
    mid-arrival). Everything before that point is guaranteed not to contain
    a partial token, so it's safe to unmask and yield immediately -- keeping
    the response feeling live rather than waiting for the whole thing.
    Whatever's left in the buffer when the chunk stream ends (e.g. the
    provider was cut off mid-token by a token limit) is flushed as-is at
    the end, same as unmask_text() would leave an unmatched bracket alone.
    """
    reverse_map = store.get_reverse_map(session_id)
    lookup = {tok.upper(): orig for tok, orig in reverse_map.items()} if reverse_map else {}

    def _replace(match):
        raw = match.group(0)
        normalized = "[" + re.sub(r"\s+", "", raw[1:-1]).upper() + "]"
        return str(lookup.get(normalized, raw))

    def _unmask(s: str) -> str:
        return _TOKEN_PATTERN.sub(_replace, s) if lookup else s

    buffer = ""
    for chunk in chunks:
        buffer += chunk
        last_open = buffer.rfind("[")
        if last_open == -1 or "]" in buffer[last_open:]:
            safe_end = len(buffer)  # no dangling open bracket -- all safe
        else:
            safe_end = last_open  # hold back from the open bracket onward
        if safe_end > 0:
            yield _unmask(buffer[:safe_end])
            buffer = buffer[safe_end:]
    if buffer:
        yield _unmask(buffer)


def build_masked_context(masked_df: pd.DataFrame, max_rows: int = 200) -> tuple[str, bool]:
    """Returns (csv_text, was_truncated)."""
    truncated = len(masked_df) > max_rows
    limited = masked_df.head(max_rows)
    return limited.to_csv(index=False), truncated


def count_masked_tokens(payload: str) -> int:
    """Number of distinct [TOKEN] patterns in a piece of text -- used for
    the "N kept private" badge on a message."""
    return len(set(_TOKEN_PATTERN.findall(payload)))
