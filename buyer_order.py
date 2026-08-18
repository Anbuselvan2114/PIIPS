"""
Buyer's Order No. (PO) detection for PIIPS.

Some vendors don't print the buyer's PO in a fixed field — instead it is
written (often by hand) somewhere on the page in the SPRPUR form:

    SPRPUR/2026/04/27-83650
            |    |  |    |
        prefix  yyyy mm dd  serial

`find_buyer_order_no(text)` scans free OCR text for such a token and reports
whether the match is trustworthy ("confident"), garbled and in need of a
human check ("doubtful"), or absent ("none"). The anchor extractor only reads
labelled fields, so this fills the gap for handwritten / stray POs.

OCR frequently mangles handwriting (S->5, O->0, l->1, Z->2, B->8, letters in
the date groups). A "doubtful" match is returned upper-cased and lightly
repaired but is never trusted automatically — the caller parks the invoice so
a user can confirm it.
"""

import re


# Prefix, allowing the common OCR confusion of a leading 'S' read as '5'.
_PREFIX = r"[S5]PRPUR"

# Strict, trustworthy PO: SPRPUR/YYYY/MM/DD-NNNNN (spaces around separators
# tolerated; serial 4-6 digits to allow for the sample range).
_STRICT_RE = re.compile(
    _PREFIX + r"\s*/\s*\d{4}\s*/\s*\d{2}\s*/\s*\d{2}\s*-\s*\d{4,6}",
    re.IGNORECASE,
)

# Loose PO: the same skeleton (prefix, three '/'-separated groups, a '-', a
# trailing group) but each group is any run of letters/digits — so a mangled
# date or serial still matches and can be flagged doubtful.
_LOOSE_RE = re.compile(
    _PREFIX + r"\s*/\s*[A-Za-z0-9]{1,6}\s*/\s*[A-Za-z0-9]{1,4}\s*/\s*"
    r"[A-Za-z0-9]{1,4}\s*-\s*[A-Za-z0-9]{1,8}",
    re.IGNORECASE,
)

# Prefix + a clean date + trailing hyphen, serial not required here - used
# to re-anchor when the serial doesn't immediately follow the hyphen (see
# _serial_after below).
_ANCHOR_RE = re.compile(
    _PREFIX + r"\s*/\s*\d{4}\s*/\s*\d{2}\s*/\s*\d{2}\s*-",
    re.IGNORECASE,
)


def _serial_after(text, pos):
    """A 4-6 digit serial starting right at `pos` (only whitespace before
    it), or - when a facing column's own text sits there instead - the
    last 4-6 digit run on the very next physical line.

    A two-column page (seller block | "Reference No. & Date." box) gets
    flattened into one row of plain text per physical row, so the row
    that starts "SPRPUR/2026/08/03-" often ends there, and the row below
    it interleaves the OTHER column's own text ahead of the real serial:
    "GSTIN:29LLZPS1935B1ZA                    85734" - naive whitespace-
    only bridging stops at "GSTIN", this looks past it instead.
    """
    tail = text[pos:]
    same_line = tail.split("\n", 1)[0]
    m = re.match(r"\s*(\d{4,6})\b", same_line)
    if m:
        return m.group(1)
    parts = tail.split("\n", 2)
    if len(parts) >= 2:
        digits = re.findall(r"\d{4,6}", parts[1])
        if digits:
            return digits[-1]
    return None


def _normalise(token):
    """Strip internal whitespace and upper-case a matched PO token.
    Sample: _normalise('sprpur/ 2026 /04/27- 83650') -> 'SPRPUR/2026/04/27-83650'
    """
    return re.sub(r"\s+", "", token).upper()


def _is_strict(token):
    """True when a whitespace-free token matches the trustworthy PO shape.
    Sample: _is_strict('SPRPUR/2026/04/27-83650') -> True
    """
    return bool(re.fullmatch(_PREFIX + r"/\d{4}/\d{2}/\d{2}-\d{4,6}", token,
                             re.IGNORECASE))


def looks_like_po(value):
    """True when `value` is already a clean SPRPUR PO (used to decide whether
    the anchor-extracted value needs to be re-scanned).
    Sample: looks_like_po('SPRPUR/2026/04/27-83650') -> True
    """
    return _is_strict(_normalise(value or ""))


def find_buyer_order_no(text):
    """
    Scan free OCR `text` for a SPRPUR buyer's order number.

    Sample inputs:
        "... Buyer Order SPRPUR/2026/04/27-83650 ..."  -> ("SPRPUR/2026/04/27-83650", "confident")
        "hand note SPRPUR/205/ann/20-8z45"             -> ("SPRPUR/205/ANN/20-8Z45", "doubtful")
        "no po written here"                           -> ("", "none")

    Returns (value, confidence) with confidence in {"confident","doubtful","none"}.
    A confident match is a clean SPRPUR/YYYY/MM/DD-NNNNN. A doubtful match has
    the right skeleton but a mangled group; it is returned (upper-cased) for a
    user to verify. When nothing matches, ("", "none").
    """
    if not text:
        return "", "none"

    # Trustworthy match first.
    m = _STRICT_RE.search(text)
    if m:
        return _normalise(m.group(0)), "confident"

    # The strict/loose forms above both assume the serial sits right after
    # the hyphen with only whitespace between - a two-column page can
    # interleave the facing column's own text there instead once OCR
    # flattens rows into plain text. The prefix+date+hyphen is unambiguous
    # on its own, so anchor on just that and look separately for the
    # serial (same line, or the last digit run on the next physical line).
    am = _ANCHOR_RE.search(text)
    if am:
        serial = _serial_after(text, am.end())
        if serial:
            return _normalise(am.group(0) + serial), "confident"

    # Skeleton match with garbled groups -> doubtful (needs human confirmation).
    m = _LOOSE_RE.search(text)
    if m:
        return _normalise(m.group(0)), "doubtful"

    return "", "none"
