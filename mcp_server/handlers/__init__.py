"""Cockpit handler modules — LLM client and chat orchestration."""

from .deepseek import (
    DEEPSEEK_BASE_URL,
    DEEPSEEK_DEFAULT_MODEL,
    DEEPSEEK_TIMEOUT_S,
    DIRECTOR_PERSONA,
    _extract_text,
    call_deepseek,
)

__all__ = [
    "DIRECTOR_PERSONA",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_TIMEOUT_S",
    "call_deepseek",
    "_extract_text",
]
