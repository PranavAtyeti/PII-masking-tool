"""
ner_detection.py
-----------------
"Intelligence" layer on top of the regex detector: uses Microsoft Presidio
(spaCy-based NER under the hood) to catch PII that regex can't -- mainly
names and locations embedded in free text ("call Rohan about the Baner
office lease"), rather than sitting alone and format-perfect in a cell.

Design principle (integrity): regex is NOT replaced. Structured, unambiguous
formats (email, PAN, Aadhaar, card numbers) are still caught by regex in
detection.py because regex is *more* reliable for those -- a PAN either
matches ABCDE1234F or it doesn't, no probabilistic judgment needed. NER adds
coverage for the fuzzy cases regex was never going to catch; it doesn't
subtract coverage anywhere regex already had it.

A confidence threshold guards against over-flagging real business text as PII.
"""

import re
from functools import lru_cache

# Presidio's DATE_TIME recognizer flags relative/vague time expressions
# ("yesterday", "next quarter", "3pm") at the same flat confidence as actual
# dates -- the confidence score alone can't tell them apart. None of these
# are privacy-sensitive (they're not dates of birth), and masking them
# strips ordinary time context the model needs to answer well, so they're
# filtered out here by content instead.
_VAGUE_DATE_WORDS = {
    "today", "yesterday", "tomorrow", "tonight",
    "morning", "evening", "afternoon", "night",
    "this week", "next week", "last week",
    "this month", "next month", "last month",
    "this quarter", "next quarter", "last quarter",
    "this year", "next year", "last year",
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
}
_CLOCK_TIME_RE = re.compile(r"^\d{1,2}(:\d{2})?\s?(am|pm)$", re.IGNORECASE)


def _is_vague_date_mention(entity_text: str) -> bool:
    t = entity_text.strip().lower()
    return t in _VAGUE_DATE_WORDS or bool(_CLOCK_TIME_RE.match(t))

# Presidio entity -> our internal type vocabulary (matches detection.py)
PRESIDIO_TO_INTERNAL = {
    "PERSON": "PERSON",
    "EMAIL_ADDRESS": "EMAIL",
    "PHONE_NUMBER": "PHONE",
    "LOCATION": "ADDRESS",
    "CREDIT_CARD": "ID",
    "US_SSN": "ID",
    "IP_ADDRESS": "IP",
    "DATE_TIME": "DOB",
}

CONFIDENCE_THRESHOLD = 0.6  # below this, treat as "not confident enough" -> don't mask


@lru_cache(maxsize=1)
def _get_analyzer():
    """
    Lazily build the Presidio analyzer once (spaCy model load is the slow part,
    ~1-2s, so we do it a single time per process, not per cell).
    """
    from presidio_analyzer import AnalyzerEngine
    from presidio_analyzer.nlp_engine import NlpEngineProvider

    nlp_config = {
        "nlp_engine_name": "spacy",
        "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}],
    }
    provider = NlpEngineProvider(nlp_configuration=nlp_config)
    nlp_engine = provider.create_engine()
    return AnalyzerEngine(nlp_engine=nlp_engine, supported_languages=["en"])


def analyze_text(text: str, min_confidence: float = CONFIDENCE_THRESHOLD):
    """
    Runs NER over a piece of free text. Returns a list of
    (entity_text, internal_type, confidence) tuples above the threshold.
    Used for free-text columns (notes/comments) where regex can't parse
    PII embedded mid-sentence.
    """
    if not text or not str(text).strip():
        return []

    analyzer = _get_analyzer()
    results = analyzer.analyze(text=str(text), language="en")

    findings = []
    for r in results:
        if r.score < min_confidence:
            continue
        internal_type = PRESIDIO_TO_INTERNAL.get(r.entity_type)
        if not internal_type:
            continue
        entity_text = str(text)[r.start:r.end]
        if internal_type == "DOB" and _is_vague_date_mention(entity_text):
            continue
        findings.append((entity_text, internal_type, r.score))
    return findings


def classify_cell_with_ner(value, min_confidence: float = CONFIDENCE_THRESHOLD):
    """
    Whole-cell version: returns internal_type or None, used the same way
    detection.classify_cell() is used, but for cells that look like short
    free text rather than a clean structured value (so it's worth the
    extra NER pass instead of only relying on regex).
    """
    findings = analyze_text(value, min_confidence)
    if not findings:
        return None
    # if NER found something covering most of the cell, trust the whole-cell type
    best = max(findings, key=lambda f: f[2])
    return best[1]
