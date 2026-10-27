"""Local PII redaction applied before persistence, outbound model calls and logs.

Two detectors, chosen with CAPSTONE_PII_DETECTOR:
- `regex` (default): deterministic patterns for structured identifiers. No dependencies.
- `presidio`: the same patterns plus Microsoft Presidio with a LOCAL spaCy model for entities
  that need NER (people's names, IBANs). Text never leaves this process; there is deliberately
  no remote detection API. Requires the `privacy` extra; if configured but missing, startup fails.

Only allowlisted entity types are redacted. Neither detector is complete: redaction reduces
exposure, it does not guarantee it, so it is one control among access scoping and review.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass, field
import os
import re
import threading

TOKENS = {"EMAIL_ADDRESS": "[EMAIL]", "PHONE_NUMBER": "[PHONE]", "US_SSN": "[SSN]",
          "CREDIT_CARD": "[CARD]", "IBAN_CODE": "[IBAN]", "PERSON": "[PERSON]"}
# Minimum Presidio confidence per entity. Its phone recogniser scores real international
# numbers ~0.4 and order IDs too, so phones come from the deterministic patterns instead.
PRESIDIO_THRESHOLDS = {"PERSON": 0.85, "IBAN_CODE": 0.8, "CREDIT_CARD": 0.8, "US_SSN": 0.8, "EMAIL_ADDRESS": 0.9}

EMAIL = re.compile(r"(?<![\w.+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}\b")
# North American numbers, and international numbers written with a leading +.
PHONE_NANP = re.compile(r"(?<![\w-])(?:\+?1[\s.-]?)?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}(?![\w-])")
PHONE_INTL = re.compile(r"(?<![\w-])\+\d{1,3}(?:[\s.-]?\(?\d{1,4}\)?){2,5}(?![\w-])")
SSN = re.compile(r"(?<![\w-])\d{3}-\d{2}-\d{4}(?![\w-])")
CARD = re.compile(r"(?<![\w-])(?:\d[ -]?){12,18}\d(?![\w-])")


@dataclass
class Redaction:
    text: str
    counts: dict = field(default_factory=dict)


def _luhn(digits: str) -> bool:
    total, parity = 0, len(digits) % 2
    for i, ch in enumerate(digits):
        d = int(ch)
        if i % 2 == parity:
            d = d * 2 - 9 if d > 4 else d * 2
        total += d
    return total % 10 == 0


def _part_of_identifier(text: str, start: int) -> bool:
    """`ORD-2024-000123`, `v1.2.3-555`: a digit run glued to a longer token is an identifier, not PII."""
    return start > 0 and (text[start - 1].isalnum() or text[start - 1] in "-_/")


def _pattern_spans(text: str) -> list[tuple[int, int, str]]:
    spans = [(m.start(), m.end(), "EMAIL_ADDRESS") for m in EMAIL.finditer(text)]
    spans += [(m.start(), m.end(), "US_SSN") for m in SSN.finditer(text)]
    for m in CARD.finditer(text):
        digits = re.sub(r"\D", "", m.group())
        if 13 <= len(digits) <= 19 and _luhn(digits):
            spans.append((m.start(), m.end(), "CREDIT_CARD"))
    for pattern in (PHONE_NANP, PHONE_INTL):
        for m in pattern.finditer(text):
            if 8 <= len(re.sub(r"\D", "", m.group())) <= 15:
                spans.append((m.start(), m.end(), "PHONE_NUMBER"))
    return spans


def _apply(text: str, spans: list[tuple[int, int, str]]) -> Redaction:
    """Replace non-overlapping spans (longest first wins), right to left."""
    chosen: list[tuple[int, int, str]] = []
    for start, end, kind in sorted(spans, key=lambda s: (s[0], -(s[1] - s[0]))):
        if not chosen or start >= chosen[-1][1]:
            chosen.append((start, end, kind))
    counts = Counter(kind for _, _, kind in chosen)
    for start, end, kind in reversed(chosen):
        text = text[:start] + TOKENS[kind] + text[end:]
    return Redaction(text, dict(counts))


class RegexRedactor:
    name = "regex"

    def redact(self, text: str) -> Redaction:
        if not isinstance(text, str):
            raise TypeError("Expected text")
        return _apply(text, _pattern_spans(text))


class PresidioRedactor:
    """Deterministic patterns plus local Presidio NER. Loads the spaCy model once per process."""
    name = "presidio"

    def __init__(self, entities: set[str] | None = None):
        from presidio_analyzer import AnalyzerEngine
        from presidio_analyzer.nlp_engine import NlpEngineProvider
        self.entities = sorted(entities or set(PRESIDIO_THRESHOLDS))
        unknown = set(self.entities) - set(PRESIDIO_THRESHOLDS)
        if unknown:
            raise ValueError(f"Entities not allowlisted: {sorted(unknown)}")
        nlp = NlpEngineProvider(nlp_configuration={
            "nlp_engine_name": "spacy", "models": [{"lang_code": "en", "model_name": "en_core_web_sm"}]}).create_engine()
        self.analyzer = AnalyzerEngine(nlp_engine=nlp, supported_languages=["en"])

    def redact(self, text: str) -> Redaction:
        if not isinstance(text, str):
            raise TypeError("Expected text")
        spans = _pattern_spans(text)
        for r in self.analyzer.analyze(text, language="en", entities=self.entities):
            if r.score >= PRESIDIO_THRESHOLDS[r.entity_type] and not _part_of_identifier(text, r.start):
                spans.append((r.start, r.end, r.entity_type))
        return _apply(text, spans)


_instance = None
_lock = threading.Lock()


def get_redactor():
    """Process-wide redactor from CAPSTONE_PII_DETECTOR; a configured detector that cannot load fails closed."""
    global _instance
    with _lock:
        if _instance is None:
            kind = os.getenv("CAPSTONE_PII_DETECTOR", "regex")
            if kind == "regex":
                _instance = RegexRedactor()
            elif kind == "presidio":
                entities = os.getenv("CAPSTONE_PII_ENTITIES")
                _instance = PresidioRedactor({e.strip() for e in entities.split(",")} if entities else None)
            else:
                raise RuntimeError("CAPSTONE_PII_DETECTOR must be regex or presidio")
        return _instance


def reset_redactor() -> None:
    """For tests and configuration reloads."""
    global _instance
    with _lock:
        _instance = None
