"""Parse money written the way contracts actually write it.

Evidence for why this exists, from the two 2026-08-12 samples:

- The project document writes `人民币 1536 万元整`  -> 万 units.
- The contract writes `¥15,360,000.00` and `壹仟伍佰叁拾陆万元整` -> yuan,
  plus the Chinese financial (capital) numerals used on every formal contract.
- `contract_record.total_amount` is plain yuan.

Checking the column definitions established that all DATABASE money
columns are yuan. That finding does not extend to contract body
text, which mixes 万/亿 and capital numerals freely. Comparing a text amount to
a database amount without normalising here would be an order-of-magnitude bug.

Everything returns Decimal yuan, so a text amount and a database amount are
directly comparable.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation

# Capital (financial) and ordinary Chinese digits. Contracts use the capital
# forms precisely because they are hard to alter after signing.
_DIGITS = {
    "零": 0, "〇": 0, "壹": 1, "一": 1, "贰": 2, "二": 2, "两": 2,
    "叁": 3, "三": 3, "肆": 4, "四": 4, "伍": 5, "五": 5,
    "陆": 6, "六": 6, "柒": 7, "七": 7, "捌": 8, "八": 8, "玖": 9, "九": 9,
}
# Units below 万.
_SMALL_UNITS = {"拾": 10, "十": 10, "佰": 100, "百": 100, "仟": 1000, "千": 1000}
# Section units. 万 and 亿 multiply everything accumulated before them.
_SECTION_UNITS = {"万": 10**4, "萬": 10**4, "亿": 10**8, "億": 10**8}

_CHINESE_AMOUNT_CHARS = set(_DIGITS) | set(_SMALL_UNITS) | set(_SECTION_UNITS)

# Arabic amount, e.g. "¥15,360,000.00", "1536万元", "12800 元", "人民币 1536 万元".
#
# A currency marker is REQUIRED by default, and that is not fussiness. Run
# without it over the real 2026-08-12 sample and three of eight "amounts" are
# junk: 28172020 from the standard number `TB/T 2817-2020`, 20260812 from the
# signing date, and 1200 from a quantity in 件. Any rule comparing "the largest
# amount in the document" against a contract total would have been wrecked by
# the standard number alone.
_ARABIC_AMOUNT_RE = re.compile(
    r"(?P<prefix>[¥￥]|人民币|RMB)?\s*"
    r"(?P<number>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
    r"\s*(?P<unit>亿元|億元|亿|億|万元|萬元|万|萬)?"
    r"\s*(?P<currency>元|圆)?"
    # `整` / `正` closes a formal amount ("壹佰元整"). Without it here, a
    # fullmatch on `人民币 1536 万元整` fails and the amount is silently lost.
    r"\s*(?:整|正)?",
    re.IGNORECASE,
)

# A run of Chinese numeral characters followed by 元 (optionally 整/正).
_CHINESE_AMOUNT_RE = re.compile(
    r"(?P<body>[" + "".join(_CHINESE_AMOUNT_CHARS) + r"]{2,})\s*元(?:整|正)?"
)

_PERCENT_RE = re.compile(r"(?P<number>\d+(?:\.\d+)?)\s*%")


def parse_chinese_amount(text: str) -> Decimal | None:
    """Convert Chinese numerals to a Decimal, e.g. 壹仟伍佰叁拾陆万 -> 15360000.

    Returns None when the string is not a well-formed Chinese numeral, rather
    than guessing. A wrong amount is far worse than no amount.
    """
    if not text:
        return None
    total = Decimal(0)      # completed 万/亿 sections
    section = Decimal(0)    # current section below the next 万/亿
    digit: Decimal | None = None
    seen_any = False

    for char in text:
        if char in _DIGITS:
            digit = Decimal(_DIGITS[char])
            seen_any = True
        elif char in _SMALL_UNITS:
            # "拾" with no preceding digit means one ten, as in 拾贰 = 12.
            section += (digit if digit is not None else Decimal(1)) * _SMALL_UNITS[char]
            digit = None
            seen_any = True
        elif char in _SECTION_UNITS:
            multiplier = Decimal(_SECTION_UNITS[char])
            section += digit or Decimal(0)
            if section == 0:
                section = Decimal(1)
            if multiplier == 10**8:
                # 亿 closes everything accumulated so far.
                total = (total + section) * multiplier
            else:
                total += section * multiplier
            section = Decimal(0)
            digit = None
            seen_any = True
        else:
            return None
    if not seen_any:
        return None
    return total + section + (digit or Decimal(0))


def parse_amount_to_yuan(raw: str, *, require_currency: bool = False) -> Decimal | None:
    """Parse one amount token into yuan, honouring a 万/亿 suffix.

    `require_currency` is False here because the caller has already decided the
    token is an amount. Scanning free text is the dangerous direction, so
    `find_amounts_in_yuan` requires a marker by default instead.
    """
    text = raw.strip()
    match = _ARABIC_AMOUNT_RE.fullmatch(text)
    if match and match.group("number"):
        if require_currency and not _has_currency_marker(match):
            return None
        return _arabic_match_to_yuan(match)
    body = text.rstrip("元圆整正").strip()
    return parse_chinese_amount(body)


def _has_currency_marker(match: re.Match[str]) -> bool:
    """True when the number is explicitly money, not a date, code or quantity."""
    if match.group("prefix") or match.group("currency"):
        return True
    # `万元` / `亿元` carry their own 元; a bare `万` does not, and a bare 万
    # after a number can just as easily be a count.
    unit = match.group("unit") or ""
    return unit.endswith(("元", "圆"))


def _arabic_match_to_yuan(match: re.Match[str]) -> Decimal | None:
    try:
        value = Decimal(match.group("number").replace(",", ""))
    except InvalidOperation:
        return None
    unit = match.group("unit") or ""
    if unit.startswith(("万", "萬")):
        return value * 10**4
    if unit.startswith(("亿", "億")):
        return value * 10**8
    return value


def find_amounts_in_yuan(text: str, *, require_currency: bool = True) -> list[Decimal]:
    """Every Arabic amount in the text, normalised to yuan, in order.

    Keep `require_currency` on unless you have a specific reason: without it,
    dates, standard numbers and quantities all parse as money. See the regex
    comment for the measured false positives.
    """
    found: list[Decimal] = []
    for match in _ARABIC_AMOUNT_RE.finditer(text):
        if not match.group("number"):
            continue
        if require_currency and not _has_currency_marker(match):
            continue
        value = _arabic_match_to_yuan(match)
        if value is not None:
            found.append(value)
    return found


def find_chinese_amounts_in_yuan(text: str) -> list[Decimal]:
    """Every `<Chinese numerals>元` amount in the text, normalised to yuan."""
    found: list[Decimal] = []
    for match in _CHINESE_AMOUNT_RE.finditer(text):
        value = parse_chinese_amount(match.group("body"))
        # Require a plausible amount: a lone 零元 or a stray character run that
        # happens to parse is more likely a false positive than a real figure.
        if value is not None and value > 0:
            found.append(value)
    return found


def find_percentages(text: str) -> list[Decimal]:
    values: list[Decimal] = []
    for match in _PERCENT_RE.finditer(text):
        try:
            values.append(Decimal(match.group("number")))
        except InvalidOperation:
            continue
    return values
