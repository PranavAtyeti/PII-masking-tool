"""Provider-neutral prompt assembly and token-budget accounting."""

from __future__ import annotations

import os
from dataclasses import dataclass

from .context_limits import MAX_TOTAL_FILE_CONTEXT_TOKENS, estimate_tokens, truncate_text_to_tokens

DEFAULT_PROVIDER_OVERHEAD_TOKENS = int(os.getenv("PRIVY_PROVIDER_OVERHEAD_TOKENS", "256"))
if DEFAULT_PROVIDER_OVERHEAD_TOKENS < 0:
    raise RuntimeError("PRIVY_PROVIDER_OVERHEAD_TOKENS must be zero or greater.")


@dataclass(frozen=True)
class BuiltPrompt:
    user_prompt: str
    file_context: str
    history: str
    estimated_input_tokens: int
    reserved_output_tokens: int
    context_window_tokens: int | None
    context_limited: bool
    token_estimate_method: str = "approx_characters_divided_by_four"


class PromptBudgetExceeded(ValueError):
    pass


def build_prompt(
    *,
    system_prompt: str,
    file_context: str,
    history: str,
    question: str,
    requested_output_tokens: int,
    model_capabilities: dict,
    context_label: str = "MASKED DATA FROM ATTACHED FILES",
) -> BuiltPrompt:
    """Keep the current question and instructions, shrinking context first."""
    context_window = model_capabilities.get("context_window_tokens")
    model_output_limit = model_capabilities.get("max_output_tokens")
    output_reserve = min(
        requested_output_tokens,
        model_output_limit if isinstance(model_output_limit, int) else requested_output_tokens,
    )

    question_section = f"CURRENT USER QUESTION:\n{question}"
    fixed_text = system_prompt + question_section
    fixed_tokens = estimate_tokens(fixed_text) + DEFAULT_PROVIDER_OVERHEAD_TOKENS
    if context_window is not None:
        input_budget = max(0, context_window - output_reserve)
        if fixed_tokens > input_budget:
            raise PromptBudgetExceeded("The system instructions and current question exceed the configured model context window.")
        remaining = input_budget - fixed_tokens
    else:
        input_budget = fixed_tokens + MAX_TOTAL_FILE_CONTEXT_TOKENS
        remaining = MAX_TOTAL_FILE_CONTEXT_TOKENS

    history_budget = min(
        estimate_tokens(history),
        max(0, remaining // 4),
    )
    limited_history, history_truncated = truncate_text_to_tokens(history, history_budget)
    remaining -= estimate_tokens(limited_history) if limited_history else 0

    file_budget = min(MAX_TOTAL_FILE_CONTEXT_TOKENS, max(0, remaining))
    limited_file_context, file_truncated = truncate_text_to_tokens(file_context, file_budget)

    context_limited = history_truncated or file_truncated

    def render_prompt() -> str:
        sections = []
        if context_limited:
            sections.append(
                "CONTEXT BUDGET: Some conversation history or workbook content was omitted to fit the configured context budget. Do not claim to have reviewed omitted content."
            )
        if limited_file_context:
            sections.append(f"{context_label}:\n{limited_file_context}")
        if limited_history:
            sections.append(limited_history)
        sections.append(question_section)
        return "\n\n".join(sections)

    user_prompt = render_prompt()
    estimated_tokens = estimate_tokens(system_prompt + user_prompt) + DEFAULT_PROVIDER_OVERHEAD_TOKENS
    while estimated_tokens > input_budget:
        context_limited = True
        excess = estimated_tokens - input_budget
        if limited_file_context:
            current_budget = estimate_tokens(limited_file_context)
            limited_file_context, was_truncated = truncate_text_to_tokens(
                limited_file_context,
                max(0, current_budget - excess - 2),
            )
            file_truncated = file_truncated or was_truncated
        elif limited_history:
            current_budget = estimate_tokens(limited_history)
            limited_history, was_truncated = truncate_text_to_tokens(
                limited_history,
                max(0, current_budget - excess - 2),
            )
            history_truncated = history_truncated or was_truncated
        else:
            raise PromptBudgetExceeded("The fixed prompt and context notice exceed the configured model context window.")
        user_prompt = render_prompt()
        estimated_tokens = estimate_tokens(system_prompt + user_prompt) + DEFAULT_PROVIDER_OVERHEAD_TOKENS

    return BuiltPrompt(
        user_prompt=user_prompt,
        file_context=limited_file_context,
        history=limited_history,
        estimated_input_tokens=estimated_tokens,
        reserved_output_tokens=output_reserve,
        context_window_tokens=context_window,
        context_limited=context_limited,
    )
