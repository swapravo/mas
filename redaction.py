"""Strip personal data from text before a model sees it (and check text before it goes out).

Regex rules for phone numbers, national ID numbers, PAN and email, plus an optional list of
names (resident, owner) to blank out. A starting point: a real deployment would add Presidio
and a measured fake-data test set (see docs/how-build-plan.md).
"""
import re

_RULES = [
    (re.compile(r"\b\d{4}[\s-]?\d{4}[\s-]?\d{4}\b"), "[id]"),                      # 12-digit national ID
    (re.compile(r"\b[A-Z]{5}\d{4}[A-Z]\b"), "[id]"),                                # PAN
    (re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "[email]"),
    (re.compile(r"(?<!\d)(?:\+?91[\s-]?)?[6-9]\d{4}[\s-]?\d{5}(?!\d)"), "[phone]"),  # Indian mobile
]


def redact(text: str, names: list[str] | None = None) -> str:
    for pat, token in _RULES:
        text = pat.sub(token, text)
    for name in names or []:
        for part in {name, *name.split()}:
            if len(part) >= 3:
                text = re.sub(rf"\b{re.escape(part)}\b", "[name]", text, flags=re.I)
    return text


def contains_pii(text: str, names: list[str] | None = None) -> bool:
    return redact(text, names) != text
