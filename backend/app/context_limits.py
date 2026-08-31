"""Application-level guardrails for file context sent to the LLM.

These limits are intentionally provider-agnostic. They protect Privy from
building unexpectedly large prompts without pretending that an exact model
context window is known here.
"""

import math
import os

# Hard ingestion limit: files with more rows than this are rejected.
MAX_ROWS_PER_FILE = int(os.getenv("PRIVY_MAX_ROWS_PER_FILE", "100000"))

# Approximate token budgets for masked file context.
# Estimation uses ~4 UTF-8 characters per token as a conservative application
# heuristic. The model provider may tokenize differently.
MAX_CONTEXT_TOKENS_PER_FILE = int(
    os.getenv("PRIVY_MAX_CONTEXT_TOKENS_PER_FILE", "12000")
)
MAX_TOTAL_FILE_CONTEXT_TOKENS = int(
    os.getenv("PRIVY_MAX_TOTAL_FILE_CONTEXT_TOKENS", "30000")
)


def estimate_tokens(text: str) -> int:
    """Return a provider-independent token estimate for plain/CSV text."""
    if not text:
        return 0
    return max(1, math.ceil(len(text) / 4))


def truncate_text_to_tokens(text: str, max_tokens: int) -> tuple[str, bool]:
    """Trim text to an approximate token budget.

    For CSV-like content, keep the header and as many complete rows as fit so
    the model receives structurally valid context instead of a partial row.
    """
    if max_tokens <= 0:
        return "", bool(text)

    if estimate_tokens(text) <= max_tokens:
        return text, False

    lines = text.splitlines(keepends=True)
    if not lines:
        return "", bool(text)

    budget_chars = max_tokens * 4
    header = lines[0]
    if len(header) >= budget_chars:
        # Keep a shortened header rather than emitting arbitrary partial data.
        return header[:budget_chars], True

    out = [header]
    used = len(header)
    for line in lines[1:]:
        if used + len(line) > budget_chars:
            break
        out.append(line)
        used += len(line)

    return "".join(out), True


def limit_file_context(
    filename: str,
    masked_csv: str,
    remaining_total_tokens: int,
) -> tuple[str, int, bool]:
    """Apply both per-file and remaining chat-wide context limits."""
    budget = min(MAX_CONTEXT_TOKENS_PER_FILE, max(0, remaining_total_tokens))
    limited, truncated = truncate_text_to_tokens(masked_csv, budget)
    return limited, estimate_tokens(limited), truncated
