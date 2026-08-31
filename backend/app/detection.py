"""
detection.py
------------
PII detection: column-name heuristics + per-cell regex classification.
"""

import re

COLUMN_HINTS = {
    # NOTE: "customer"/"client"/"employee" alone were removed as standalone
    # PERSON keywords -- they used to substring-match inside identifier
    # columns like "CustomerID", wrongly tagging IDs as names. They're kept
    # only as part of explicit "X name" phrases below.
    "PERSON": ["name", "full name", "first name", "last name",
               "customer name", "client name", "employee name", "contact person"],
    "EMAIL": ["email", "e-mail"],
    "PHONE": ["phone", "mobile", "cell", "telephone", "contact no"],
    "ADDRESS": ["address", "location", "street", "city", "residence"],
    "ID": ["pan", "aadhaar", "ssn", "passport", "id number", "id",
           "account no", "account number", "card number", "bank account",
           "account id", "tax id", "national id", "government id"],
    "DOB": ["dob", "date of birth", "birth date"],
    "AMOUNT": ["salary", "income", "wage", "compensation", "ctc"],
}

# Words that mark a header as "identifier-shaped" -- if present, an
# unqualified name-ish word elsewhere shouldn't be enough to call it PERSON.
IDENTIFIER_MARKER_WORDS = {"id", "no", "number", "code"}

# High-precision, format-specific patterns only. Safe to run blindly on any
# cell in any column (mask_dataframe does exactly that as a fallback for
# columns with no column-level type) because a false positive here requires
# the value to accidentally look like an email/phone/ID/IP -- rare for
# ordinary business data.
CELL_PATTERNS = [
    ("EMAIL", re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")),
    ("PHONE", re.compile(r"^(\+?\d{1,3}[\s-]?)?\d{10}$")),
    ("ID", re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")),               # PAN
    ("ID", re.compile(r"^\d{4}\s?\d{4}\s?\d{4}$")),            # Aadhaar
    ("ID", re.compile(r"^(\d{4}[- ]?){3}\d{4}$")),              # card
    ("IP", re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")),
]

# Deliberately NOT in CELL_PATTERNS above. "Two or three Title Case words"
# is far too broad to apply blindly to a single cell in an arbitrary column
# -- it matches "New York", "Under Review", "Not Applicable", "High
# Priority" just as readily as an actual name. It's only safe to use where
# it's checked against MANY cells in a column and requires a consistent
# majority (see classify_dataframe_columns' min_hit_ratio gate below), not
# as a one-off per-cell classifier.
NAME_SHAPE_PATTERN = re.compile(r"^[A-Z][a-z]+(\s[A-Z][a-z]+){1,2}$")

TYPE_LABEL = {
    "PERSON": "Name", "EMAIL": "Email", "PHONE": "Phone", "ADDRESS": "Address",
    "ID": "ID number", "DOB": "Date of birth", "AMOUNT": "Amount", "IP": "IP address",
}


def _split_header_words(header: str):
    """
    Splits a header into lowercase words, handling camelCase ("CustomerID"
    -> "customer id") and separators (_, -) so word-boundary matching below
    doesn't get fooled by concatenated identifiers.
    """
    s = str(header)
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", s)   # camelCase split
    s = re.sub(r"[_\-]+", " ", s)
    return s.lower().split()


def classify_column(header: str):
    words = _split_header_words(header)
    word_set = set(words)
    joined = " ".join(words)

    def hint_match(value_type):
        for kw in COLUMN_HINTS[value_type]:
            if re.search(r"\b" + re.escape(kw) + r"\b", joined):
                return True
        return False

    # Identifier-shaped headers (contain "id"/"no"/"number"/"code" as a word)
    # get checked against ID hints first, and are NOT eligible for the
    # generic PERSON match unless "name" is explicitly present -- this is
    # what stops "CustomerID" from being tagged as a name column.
    if word_set & IDENTIFIER_MARKER_WORDS:
        if hint_match("ID"):
            return "ID"
        if "name" in word_set:
            return "PERSON"
        # any other id/no/number/code-shaped header defaults to ID as the
        # safer choice (better to mask an identifier unnecessarily than miss one)
        return "ID"

    for value_type in COLUMN_HINTS:
        if hint_match(value_type):
            return value_type
    return None


def _is_valid_luhn(value: str) -> bool:
    digits = re.sub(r"\D", "", value)
    if not 13 <= len(digits) <= 19:
        return False
    total = 0
    parity = len(digits) % 2
    for i, ch in enumerate(digits):
        n = int(ch)
        if i % 2 == parity:
            n *= 2
            if n > 9:
                n -= 9
        total += n
    return total % 10 == 0


def classify_cell(value, include_name_shape: bool = False):
    """
    include_name_shape=False (the default) only checks high-precision
    format-specific patterns -- safe to call on a single cell with no other
    context, which is how mask_dataframe uses this as a per-cell fallback.

    include_name_shape=True additionally checks NAME_SHAPE_PATTERN ("Rohan
    Mehta"-style Title Case). Only pass True from a call site that gates on
    a consistent majority across many cells (see classify_dataframe_columns)
    -- a single cell matching this pattern is not reliable evidence on its
    own (see NAME_SHAPE_PATTERN's docstring above for why).
    """
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for value_type, pattern in CELL_PATTERNS:
        if pattern.match(s):
            return value_type
    if _is_valid_luhn(s):
        return "ID"
    if include_name_shape and NAME_SHAPE_PATTERN.match(s):
        return "PERSON"
    return None


# Same idea as CELL_PATTERNS but written for re.finditer over a whole
# sentence/paragraph rather than re.match against a whole cell -- e.g. PHONE
# needs (?<!\d)/(?!\d) boundaries here since it's finding a number embedded
# among other digits/words, not validating that an entire cell is a number.
FREE_TEXT_PATTERNS = [
    ("EMAIL", re.compile(r"[^\s@]+@[^\s@]+\.[^\s@]+")),
    ("ID", re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b")),                      # PAN
    ("ID", re.compile(r"(?<!\d)(?:\d{4}[-\s]?){3}\d{4}(?!\d)")),        # card / Aadhaar
    ("IP", re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")),
    ("PHONE", re.compile(r"(?<!\d)(?:\+\d{1,3}[-\s]?)?\d{5}[-\s]?\d{5}(?!\d)")),
]


def scan_free_text_structured(text: str):
    """
    Deterministic, confidence-independent pass over free text for
    high-precision structured patterns (email/phone/ID/IP). Runs regardless
    of whether NER is enabled or what confidence threshold is set -- these
    patterns are specific enough that they don't need a probabilistic
    judgment call the way names/locations do.

    Returns a list of (matched_text, internal_type) tuples. Longer/more
    specific patterns (card numbers, 16 digits) are checked before PHONE
    (10 digits) so a 16-digit card number isn't also partially re-matched
    as a phone number; callers additionally sort all findings by length
    before replacing, which resolves any remaining overlap safely.
    """
    s = str(text)
    findings = []
    claimed = []  # (start, end) spans already matched by an earlier pattern

    def _overlaps(start, end):
        return any(start < c_end and end > c_start for c_start, c_end in claimed)

    for value_type, pattern in FREE_TEXT_PATTERNS:
        for m in pattern.finditer(s):
            if _overlaps(m.start(), m.end()):
                continue
            findings.append((m.group().strip(), value_type))
            claimed.append((m.start(), m.end()))
    return findings


def classify_dataframe_columns(df, sample_size=25, min_hit_ratio=0.3, min_name_uniqueness=0.7):
    """
    Returns {column_name: value_type or None}.
    Uses header hints first; falls back to majority-vote cell sampling.

    The name-shape vote (Title Case, 2-3 words) additionally requires the
    sampled values to be mostly unique before it's trusted. Shape alone
    isn't enough: category/status columns ("Under Review", "High Priority",
    "North Zone") are just as Title-Case-shaped as real names, but unlike
    names they're drawn from a small repeated set of values rather than
    being distinct per row. This distinguishes them without needing a
    hand-maintained list of business phrases, which could never be complete.
    """
    col_types = {}
    n_rows = len(df)
    for col in df.columns:
        col_type = classify_column(col)
        if not col_type:
            sample = df[col].dropna().astype(str).head(sample_size)
            votes = {}
            for v in sample:
                t = classify_cell(v, include_name_shape=True)
                if t:
                    votes[t] = votes.get(t, 0) + 1
            if votes:
                best_type, best_count = max(votes.items(), key=lambda kv: kv[1])
                enough_hits = best_count >= max(2, int(n_rows * min_hit_ratio))
                if best_type == "PERSON" and enough_hits and len(sample) > 0:
                    uniqueness = sample.nunique() / len(sample)
                    enough_hits = enough_hits and uniqueness >= min_name_uniqueness
                if enough_hits:
                    col_type = best_type
        col_types[col] = col_type
    return col_types
