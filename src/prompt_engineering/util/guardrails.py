"""Deterministic output-side guardrails.

The prompts instruct the model to mask PII, refuse injection. It does NOT rewrite the
data-quality findings; it only scans free-text fields (``value`` / ``reason``)
for leaks the model may have forgotten to mask, and records which guardrail
categories were seen.
"""
from __future__ import annotations

import re
from typing import Any, Iterable

ALLOWED_GUARDRAILS = frozenset(
    {
        "malformed_input",
        "injection_attempt",
        "pii_masked",
        "offensive_content",
    }
)

_SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
_CREDIT_CARD_RE = re.compile(r"\b(?:\d[ -]?){12,18}\d\b")
_PHONE_RE = re.compile(
    r"\b(?:\+?\d{1,3}[ -]?)?(?:\(?\d{3}\)?[ -]?)\d{3}[ -]?\d{4}\b"
)

_INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous",
    "disregard the above",
    "print your system prompt",
    "reveal your system prompt",
    "you are now",
)

def _mask_ssn(match: re.Match[str]) -> str:
    s = match.group(0)
    return "***-**-" + s[-4:]

def _mask_card(match: re.Match[str]) -> str:
    s = re.sub(r"[ -]", "", match.group(0))
    return "**** **** **** " + s[-4:]

def _mask_phone(match: re.Match[str]) -> str:
    s = re.sub(r"\D", "", match.group(0))
    return "***-***-" + s[-4:]

def mask_pii(text: str) -> tuple[str, bool]:
    if not isinstance(text, str) or not text:
        return text, False
    original = text
    text = _SSN_RE.sub(_mask_ssn, text)
    text = _CREDIT_CARD_RE.sub(_mask_card, text)
    text = _PHONE_RE.sub(_mask_phone, text)
    return text, text != original

def detect_injection(text: str) -> bool:
    """Return True if the string contains a known injection marker."""
    if not isinstance(text, str) or not text:
        return False
    lowered = text.lower()
    return any(marker in lowered for marker in _INJECTION_MARKERS)

def sanitize_response(payload: dict[str, Any]) -> tuple[dict[str, Any], set[str]]:
    detected: set[str] = set()
    if not isinstance(payload, dict):
        return payload, detected

    reported = payload.get("guardrail_triggered")
    if isinstance(reported, str) and reported in ALLOWED_GUARDRAILS:
        detected.add(reported)

    rows = payload.get("row_errors")
    if not isinstance(rows, list):
        return payload, detected

    for row in rows:
        if not isinstance(row, dict):
            continue
        errors = row.get("errors")
        if not isinstance(errors, list):
            continue
        for err in errors:
            if not isinstance(err, dict):
                continue
            for key in ("value", "reason"):
                raw = err.get(key)
                if not isinstance(raw, str):
                    continue
                masked, was_masked = mask_pii(raw)
                if was_masked:
                    err[key] = masked
                    detected.add("pii_masked")
                if detect_injection(err.get(key, "")):
                    detected.add("injection_attempt")

    return payload, detected

def merge_guardrails(
    reported: Any, detected: Iterable[str]
) -> str | None:
    """Pick the single most severe guardrail tag to persist on the payload."""
    severity = [
        "malformed_input",
        "injection_attempt",
        "offensive_content",
        "pii_masked",
    ]
    candidates: set[str] = set(detected)
    if isinstance(reported, str) and reported in ALLOWED_GUARDRAILS:
        candidates.add(reported)
    for tag in severity:
        if tag in candidates:
            return tag
    return None