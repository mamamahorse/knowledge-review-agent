"""User-selected response language, separate from reusable task instructions."""

import json
from typing import Literal

ResponseLanguage = Literal["zh", "en"]
LANGUAGE_NAMES = {"zh": "中文", "en": "English"}


def validate_language(language: ResponseLanguage | None) -> None:
    if language is not None and language not in LANGUAGE_NAMES:
        raise ValueError("Response language must be Chinese or English.")


def preference_messages(language: ResponseLanguage | None) -> list[dict]:
    validate_language(language)
    if language is None:
        return []
    return [{"role": "user", "content": json.dumps({
        "user_preferences": {"response_language": LANGUAGE_NAMES[language]},
    }, ensure_ascii=False)}]
