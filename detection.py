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
           "account no", "account number", "card number"],
    "DOB": ["dob", "date of birth", "birth date"],
    "AMOUNT": ["salary", "income", "wage", "compensation", "ctc"],
}

# Words that mark a header as "identifier-shaped" -- if present, an
# unqualified name-ish word elsewhere shouldn't be enough to call it PERSON.
IDENTIFIER_MARKER_WORDS = {"id", "no", "number", "code"}

CELL_PATTERNS = [
    ("EMAIL", re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")),
    ("PHONE", re.compile(r"^(\+?\d{1,3}[\s-]?)?\d{10}$")),
    ("ID", re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")),               # PAN
    ("ID", re.compile(r"^\d{4}\s?\d{4}\s?\d{4}$")),            # Aadhaar
    ("ID", re.compile(r"^(\d{4}[- ]?){3}\d{4}$")),              # card
    ("IP", re.compile(r"^\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}$")),
    ("PERSON", re.compile(r"^[A-Z][a-z]+(\s[A-Z][a-z]+){1,2}$")),  # "Rohan Mehta"
]

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


def classify_cell(value):
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    for value_type, pattern in CELL_PATTERNS:
        if pattern.match(s):
            return value_type
    return None


def classify_dataframe_columns(df, sample_size=25, min_hit_ratio=0.3):
    """
    Returns {column_name: value_type or None}.
    Uses header hints first; falls back to majority-vote cell sampling.
    """
    col_types = {}
    n_rows = len(df)
    for col in df.columns:
        col_type = classify_column(col)
        if not col_type:
            sample = df[col].dropna().astype(str).head(sample_size)
            votes = {}
            for v in sample:
                t = classify_cell(v)
                if t:
                    votes[t] = votes.get(t, 0) + 1
            if votes:
                best_type, best_count = max(votes.items(), key=lambda kv: kv[1])
                if best_count >= max(2, int(n_rows * min_hit_ratio)):
                    col_type = best_type
        col_types[col] = col_type
    return col_types
