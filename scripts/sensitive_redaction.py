"""Local-only reversible redaction for advisory Provider requests.

Only the local process keeps the replacement map. The online/web Provider sees
stable fake values; the returned text is restored after it comes back to this
machine. Unsafe input fails closed instead of being sent in plaintext.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re


class SensitiveDataError(ValueError):
    """The text contains sensitive material that cannot be safely mapped."""


@dataclass(frozen=True)
class RedactionResult:
    text: str
    replacements: int
    categories: tuple[str, ...] = ()
    _reverse: dict[str, str] = field(default_factory=dict, repr=False, compare=False)

    def restore(self, value: str) -> str:
        restored = str(value or "")
        for fake, original in sorted(
            self._reverse.items(),
            key=lambda item: len(item[0]),
            reverse=True,
        ):
            restored = restored.replace(fake, original)
        return restored

    def audit(self) -> dict[str, object]:
        return {
            "replacements": self.replacements,
            "categories": list(self.categories),
            "mapping_retained_locally": bool(self._reverse),
        }


_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "private_key",
        re.compile(
            r"-----BEGIN (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"
            r".*?"
            r"-----END (?:RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----",
            re.DOTALL,
        ),
    ),
    (
        "known_api_key",
        re.compile(
            r"\b(?:sk-[A-Za-z0-9_-]{16,}|"
            r"gh[pousr]_[A-Za-z0-9]{20,}|"
            r"AIza[0-9A-Za-z_-]{20,}|"
            r"AKIA[0-9A-Z]{16})\b"
        ),
    ),
    (
        "authorization",
        re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    ),
    (
        "credential_assignment",
        re.compile(
            r"(?i)\b(api[_-]?key|access[_-]?token|secret|password|passwd)"
            r"(\s*[:=]\s*)"
            r"([^\s,;'\"`]{6,})"
        ),
    ),
    (
        "email",
        re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b"),
    ),
    (
        "phone",
        re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)"),
    ),
)


def _fake_value(category: str, index: int) -> str:
    examples = {
        "private_key": "<AGENTRELAY_FAKE_PRIVATE_KEY>",
        "known_api_key": f"AGENTRELAY_FAKE_API_KEY_{index}",
        "authorization": f"Bearer AGENTRELAY_FAKE_TOKEN_{index}",
        "credential_assignment": f"AGENTRELAY_FAKE_VALUE_{index}",
        "email": f"agentrelay-user-{index}@example.invalid",
        "phone": f"1380000{index:04d}",
    }
    return examples.get(category, f"AGENTRELAY_FAKE_{category.upper()}_{index}")


def redact_text(value: str) -> RedactionResult:
    """Replace recognizable secrets and return a local-only reverse map."""
    text = str(value or "")
    if "-----BEGIN" in text and "PRIVATE KEY-----" in text:
        if "-----END" not in text:
            raise SensitiveDataError(
                "检测到不完整的私钥内容，为避免泄露，本次咨询已停止发送"
            )

    reverse: dict[str, str] = {}
    categories: list[str] = []
    replacement_count = 0
    candidates: list[tuple[int, int, int, str, re.Match[str]]] = []
    for priority, (category, pattern) in enumerate(_PATTERNS):
        for match in pattern.finditer(text):
            candidates.append((
                match.start(),
                priority,
                match.end(),
                category,
                match,
            ))

    # Work from matches in the original text. Sequential regex substitution can
    # redact an already-generated fake value (for example a known key inside an
    # ``api_key=`` assignment), which makes the reverse mapping incomplete.
    candidates.sort(key=lambda item: (item[0], item[1], item[2]))
    pieces: list[str] = []
    cursor = 0
    for start, _priority, end, category, match in candidates:
        if start < cursor:
            continue
        replacement_count += 1
        fake = _fake_value(category, replacement_count)
        original = match.group(3 if category == "credential_assignment" else 0)
        reverse[fake] = original
        categories.append(category)
        if category == "credential_assignment":
            replacement = f"{match.group(1)}{match.group(2)}{fake}"
        else:
            replacement = fake
        pieces.append(text[cursor:start])
        pieces.append(replacement)
        cursor = end
    pieces.append(text[cursor:])

    return RedactionResult(
        text="".join(pieces),
        replacements=replacement_count,
        categories=tuple(dict.fromkeys(categories)),
        _reverse=reverse,
    )
