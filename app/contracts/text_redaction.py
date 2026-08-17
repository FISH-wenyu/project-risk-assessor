"""Redact sensitive values out of anything derived from contract text.

Contract bodies carry far more identifying data than contract metadata ever
did: unified social credit codes, bank accounts, national ID numbers, phone
numbers, emails, addresses and named individuals. Once text extraction is
switched on, every one of those can reach a finding, a stored summary, an audit
row or an LLM payload unless it is stripped first.

Design rules:

- **Redact, never drop.** A finding that silently lost its evidence is worse
  than one that shows `[已脱敏:银行账号]`, because the reader cannot tell that
  anything was removed.
- **Order matters.** The longest and most specific patterns run first, so a
  bank account is not partially eaten by the generic long-number rule.
- **Fail loud on the caller side.** This module only sanitises; the caller is
  responsible for never emitting raw text that has not been through it.

This is intentionally conservative: over-redacting a contract clause costs
readability, under-redacting leaks personal data into local storage and,
potentially, into an LLM prompt.
"""

from __future__ import annotations

import re
from typing import Any

REDACTION_VERSION = "contract-redaction-v1"

# Ordered most-specific first. Each entry is (label, compiled pattern).
_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    # Unified social credit code: 18 chars, digits and uppercase letters.
    ("统一社会信用代码", re.compile(r"(?<![0-9A-Z])[0-9A-Z]{18}(?![0-9A-Z])")),
    # Mainland ID: 17 digits + check digit (may be X). Before the generic
    # long-number rule, which would otherwise swallow it.
    ("身份证号", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)")),
    # Bank account / card: 16-23 digits, often spaced in groups.
    ("银行账号", re.compile(r"(?<!\d)(?:\d[ -]?){16,23}\d(?!\d)")),
    ("邮箱", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # Mainland mobile.
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")),
    # Landline with area code.
    ("联系电话", re.compile(r"(?<!\d)0\d{2,3}[- ]?\d{7,8}(?!\d)")),
)

# Labelled fields whose VALUE is sensitive even when it does not match a shape
# above, e.g. "法定代表人：张三" or "开户银行：某某支行".
_LABELLED_FIELDS = (
    "法定代表人",
    "授权代表",
    "委托代理人",
    "开户银行",
    "开户行",
    "账户名称",
    "联系人",
    "经办人",
    "签署人",
)
_LABELLED_RE = re.compile(
    r"(?P<label>" + "|".join(_LABELLED_FIELDS) + r")\s*[:：]\s*"
    # Value runs until a separator, a blank fill, or end of line.
    r"(?P<value>[^\s，,；;。\n_＿]{1,30})"
)


def redact_text(value: str) -> str:
    """Replace sensitive values with a labelled marker.

    Returns text safe to store locally, show in a finding, or include in a
    sanitised LLM summary.
    """
    if not value:
        return ""
    text = str(value)
    for label, pattern in _PATTERNS:
        text = pattern.sub(f"[已脱敏:{label}]", text)
    text = _LABELLED_RE.sub(
        lambda m: f"{m.group('label')}：[已脱敏]", text
    )
    return text


def redact_mapping(payload: dict[str, Any], *, keys: tuple[str, ...]) -> dict[str, Any]:
    """Redact selected string keys of a dict, leaving the rest untouched."""
    cleaned = dict(payload)
    for key in keys:
        if isinstance(cleaned.get(key), str):
            cleaned[key] = redact_text(cleaned[key])
    return cleaned


def contains_sensitive(value: str) -> bool:
    """True when the text still holds something the patterns would redact.

    Used by tests as a tripwire: a finding must never satisfy this.
    """
    if not value:
        return False
    text = str(value)
    return any(pattern.search(text) for _label, pattern in _PATTERNS)


# ------------------------------------------------------------- tabular data
#
# A spreadsheet gives one thing prose does not: a header row that NAMES each
# column. That matters because the pattern-based redaction above cannot touch
# personal names - a Chinese name has no distinguishing shape, and any regex
# broad enough to catch 张伟 also catches half the vocabulary in a contract.
#
# Enabling `.xlsx` on 2026-08-14 made this concrete: the one spreadsheet in the
# source data is a contact roster (name, mobile, job title), and while the
# mobile numbers were redacted, the names were not, so they could travel with a
# clause fragment into an LLM payload.
#
# The header row solves it without guessing at names: if a column is called
# 姓名, everything under it is a name, whatever it looks like.
PERSON_COLUMN_HEADERS = (
    "姓名", "名字", "联系人", "负责人", "经办人", "签署人", "法定代表人",
    "授权代表", "委托代理人", "手机", "手机号", "电话", "联系电话", "邮箱",
    "email", "身份证", "身份证号", "开户行", "开户银行", "账号", "银行账号",
)

# Above this share of data rows carrying a person column, the sheet is a roster
# of people rather than a contract artefact such as a payment schedule. The
# distinction changes what the document IS, so it is reported rather than
# silently analysed as contract text.
ROSTER_ROW_RATIO = 0.60
SIGNAL_PERSONAL_ROSTER = "spreadsheet_looks_like_a_personal_roster"


def _looks_like_person_header(cell: str) -> bool:
    text = str(cell or "").strip().lower()
    if not text or len(text) > 12:
        return False
    return any(header.lower() in text for header in PERSON_COLUMN_HEADERS)


def redact_table(rows: list[list[str]]) -> tuple[list[list[str]], list[str]]:
    """Redact personal data from a spreadsheet, on the CELL GRID.

    Takes rows of cells rather than joined text, because the separators do not
    survive normalisation and the grid is the whole point: it is the only
    reliable way to know that a bare string is somebody's name.

    Two rules, in order:

    1. **Header columns.** If a row near the top names a column 姓名 or 手机,
       everything under it is redacted whatever it looks like.
    2. **Rows carrying contact data.** A row containing a phone, email, ID or
       bank number is a record ABOUT A PERSON, and the other cells in that row
       are what identify them. Measured need: the real spreadsheet in the
       source data has no header row at all, so rule 1 alone did nothing to it.

    Rule 2 is what makes this safe by default. A contract artefact - a payment
    schedule, a price list, a milestone table - has no phone numbers in its
    rows, so it passes through untouched.
    """
    if not rows:
        return [], []

    person_columns: set[int] = set()
    header_index = -1
    for index, cells in enumerate(rows[:5]):
        found = {i for i, cell in enumerate(cells) if _looks_like_person_header(cell)}
        if found:
            person_columns, header_index = found, index
            break

    redacted_rows = 0
    data_rows = 0
    out: list[list[str]] = []
    for index, cells in enumerate(rows):
        if index == header_index:
            out.append(list(cells))
            continue
        if not any(str(cell).strip() for cell in cells):
            out.append(list(cells))
            continue
        data_rows += 1

        # Rule 2: does this row identify a person?
        row_is_personal = any(_holds_contact_data(str(cell)) for cell in cells)

        new_cells = []
        touched = False
        for column, cell in enumerate(cells):
            text = str(cell)
            if not text.strip():
                new_cells.append(text)
                continue
            if row_is_personal or column in person_columns:
                new_cells.append(REDACTED_PERSON)
                touched = True
            else:
                new_cells.append(text)
        redacted_rows += touched
        out.append(new_cells)

    signals: list[str] = []
    if data_rows and (redacted_rows / data_rows) >= ROSTER_ROW_RATIO:
        signals.append(SIGNAL_PERSONAL_ROSTER)
    return out, signals


REDACTED_PERSON = "[已脱敏:个人信息]"

# Patterns that mark a row as being ABOUT a person rather than about money or
# dates. Deliberately narrower than the full redaction set: a contract amount
# is not evidence of a person, but a mobile number is.
_CONTACT_PATTERNS = tuple(
    pattern for label, pattern in _PATTERNS
    if label in ("手机号", "邮箱", "身份证号", "银行账号", "联系电话")
)


def _holds_contact_data(value: str) -> bool:
    return any(pattern.search(value) for pattern in _CONTACT_PATTERNS)
