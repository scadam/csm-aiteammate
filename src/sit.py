"""
Sensitive Information Type (SIT) detection.

A real, dependency-free scanner that mirrors a useful subset of Microsoft
Purview's built-in sensitive information types (credit card with Luhn check, SSN,
ITIN, IBAN, email, phone, IPv4, UK National Insurance number). It is used in two
places:

* to **classify and tag** content the agent handles (so Gainsight/Snowflake data
  is marked Confidential and any embedded SITs are visible), and
* to populate the **Governance/Technical** page on the sponsor dashboard with the
  SITs seen in prompts and responses.

Purview's ``processContent`` API performs the authoritative server-side DLP/DSPM
evaluation (see :mod:`src.purview`); this local scanner exists because Purview
exposes **no API to read SIT analytics back out**, so the dashboard computes its
own view while the real policy enforcement happens in Purview.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# Sensitivity labels (mirror a typical Purview label taxonomy).
LABEL_PUBLIC = "Public"
LABEL_GENERAL = "General"
LABEL_CONFIDENTIAL = "Confidential"
LABEL_HIGHLY_CONFIDENTIAL = "Highly Confidential"


@dataclass(frozen=True)
class SitMatch:
    sit: str            # the sensitive information type name
    count: int          # number of occurrences
    confidence: str     # High | Medium | Low
    redacted: str       # a single redacted sample for display


def _luhn_ok(digits: str) -> bool:
    total, alt = 0, False
    for ch in reversed(digits):
        d = ord(ch) - 48
        if alt:
            d *= 2
            if d > 9:
                d -= 9
        total += d
        alt = not alt
    return total % 10 == 0


def _redact(value: str, keep: int = 4) -> str:
    v = re.sub(r"\s|-", "", value)
    if len(v) <= keep:
        return "•" * len(v)
    return "•" * (len(v) - keep) + v[-keep:]


# (name, regex, confidence, optional validator)
_PATTERNS = [
    ("Credit Card Number", re.compile(r"\b(?:\d[ -]*?){13,16}\b"), "High", "luhn"),
    ("U.S. Social Security Number (SSN)", re.compile(r"\b\d{3}-\d{2}-\d{4}\b"), "High", None),
    ("U.S. ITIN", re.compile(r"\b9\d{2}-[7-8]\d-\d{4}\b"), "High", None),
    ("IBAN", re.compile(r"\b[A-Z]{2}\d{2}[A-Z0-9]{11,30}\b"), "High", None),
    ("UK National Insurance Number", re.compile(r"\b[A-CEGHJ-PR-TW-Z]{2}\d{6}[A-D]\b"), "High", None),
    ("Email Address", re.compile(r"\b[\w.+-]+@[\w-]+\.[\w.-]+\b"), "Medium", None),
    ("Phone Number", re.compile(r"(?<!\d)(?:\+?\d{1,3}[ -]?)?(?:\(?\d{2,4}\)?[ -]?){2,4}\d{2,4}(?!\d)"), "Low", None),
    ("IP Address", re.compile(r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b"), "Medium", None),
]


def detect(text: str) -> list[SitMatch]:
    """Return the sensitive information types found in ``text`` (deduped by type)."""
    if not text:
        return []
    found: dict[str, list[str]] = {}
    confidences: dict[str, str] = {}
    for name, pattern, confidence, validator in _PATTERNS:
        for m in pattern.findall(text):
            raw = m if isinstance(m, str) else "".join(m)
            digits = re.sub(r"\D", "", raw)
            if validator == "luhn":
                if len(digits) < 13 or not _luhn_ok(digits):
                    continue
            if name == "Phone Number" and len(digits) < 9:
                continue
            found.setdefault(name, []).append(raw.strip())
            confidences[name] = confidence
    out = []
    for name, hits in found.items():
        out.append(SitMatch(sit=name, count=len(hits), confidence=confidences[name],
                            redacted=_redact(hits[0])))
    out.sort(key=lambda s: ({"High": 0, "Medium": 1, "Low": 2}[s.confidence], -s.count))
    return out


def classify(text: str, base_label: str = LABEL_GENERAL) -> str:
    """Derive a sensitivity label: any high-confidence SIT => Highly Confidential."""
    sits = detect(text)
    if any(s.confidence == "High" for s in sits):
        return LABEL_HIGHLY_CONFIDENTIAL
    if sits:
        return LABEL_CONFIDENTIAL
    return base_label


def summarise(matches: list[SitMatch]) -> dict:
    """A compact dict for telemetry/audit (types + counts, never raw values)."""
    return {
        "sitCount": sum(m.count for m in matches),
        "types": [{"sit": m.sit, "count": m.count, "confidence": m.confidence} for m in matches],
    }
