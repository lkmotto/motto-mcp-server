"""Cockpit handler modules — LLM client and chat orchestration."""

from .deepseek import DIRECTOR_PERSONA, call_deepseek, _extract_text
from .deepseek import DEEPSEEK_BASE_URL, DEEPSEEK_DEFAULT_MODEL, DEEPSEEK_TIMEOUT_S

__all__ = [
    "DIRECTOR_PERSONA",
    "DEEPSEEK_BASE_URL",
    "DEEPSEEK_DEFAULT_MODEL",
    "DEEPSEEK_TIMEOUT_S",
    "call_deepseek",
    "_extract_text",
]
