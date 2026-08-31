"""High-precision deterministic PII checks for Privy's final model-bound payload.

This module deliberately favors precision over broad recall. It is a final safety
barrier after the normal masking/detection pipeline, not a replacement for it.
"""

import re
from collections import Counter

EMAIL_RE = re.compile(r"\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
PAN_RE = re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b", re.IGNORECASE)
AADHAAR_RE = re.compile(r"(?<!\d)[2-9]\d{3}[ -]?\d{4}[ -]?\d{4}(?!\d)")
CARD_RE = re.compile(r"(?<!\d)(?:\d[ -]?){13,19}(?!\d)")
PHONE_RE = re.compile(r"(?<!\d)(?:\+\d{1,3}[ -]?)?[6-9]\d{9}(?!\d)")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _valid_luhn(value: str) -> bool:
    digits = _digits(value)
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


def _valid_ip(value: str) -> bool:
    try:
        parts = value.split(".")
        return len(parts) == 4 and all(0 <= int(part) <= 255 for part in parts)
    except ValueError:
        return False


def scan_for_unmasked_pii(text: str) -> list[dict[str, int | str]]:
    """Return high-confidence PII type/count findings without returning values."""
    if not text:
        return []

    counts: Counter[str] = Counter()

    if EMAIL_RE.search(text):
        counts["EMAIL"] += len(EMAIL_RE.findall(text))

    if PAN_RE.search(text):
        counts["PAN"] += len(PAN_RE.findall(text))

    aadhaar_matches = AADHAAR_RE.findall(text)
    counts["AADHAAR"] += sum(1 for m in aadhaar_matches if len(_digits(m)) == 12)

    card_matches = CARD_RE.findall(text)
    counts["CARD_NUMBER"] += sum(1 for m in card_matches if _valid_luhn(m))

    counts["PHONE"] += len(PHONE_RE.findall(text))

    ip_matches = IP_RE.findall(text)
    counts["IP_ADDRESS"] += sum(1 for m in ip_matches if _valid_ip(m))

    return [
        {"type": pii_type, "count": count}
        for pii_type, count in counts.items()
        if count > 0
    ]
