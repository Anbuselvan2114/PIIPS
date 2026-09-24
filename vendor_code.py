"""
NAV Vendor Code correction/validation for SERVICE invoices.

Since SERVICE never calls Service First (see service_api.py), the vendor
code has no automatic source - it's hand-written onto the scanned page as
"NAV Vendor Code : <code>" before upload (see anchor_extract.py's "Vendor
Code" label), as a letter block (either the vendor name's own first word in
full, e.g. "ZACO0215" for "Zaco Computers...", or two 3-letter blocks, each
the start of one of the name's own words, e.g. "SARTEC03" for "SARASWATHI
TECHNOLOGIES") followed by a run of digits. Not a single fixed shape -
correct_vendor_code() tries every plausible letter-block candidate derived
from the vendor's own name and keeps whichever fits best.

Poor handwriting is frequently misread letter-for-letter by OCR (P<->9,
U/O/D<->0, I/L<->1, S<->5, G<->6, B<->8, Z<->2, ...). Since the vendor's own
name is already known and reliably extracted, the true letter block can
usually be reconstructed from it directly rather than trusted blindly from
the OCR reading.
"""

import difflib
import re


# The "Code" part of the "NAV Vendor Code :" caption is itself frequently
# OCR-garbled beyond recognition (seen as "Cxde", "cde", ...) on a poor
# scan, same as the code's own value - the label anchor in anchor_extract.py
# (which needs "code" to actually read "code") then never fires at all,
# leaving the field blank even though a value IS printed. "vendor" alone
# survives far more reliably, so find_vendor_code_line() scans the raw page
# text line by line instead: any line starting with "vendor" (allowing a
# short "NAV "-style prefix before it) and ending in "<junk>[:;]<value>" is
# almost certainly this caption, wherever the middle word gets read as.
# No trailing \b after "vendor" - the garbled "code" word is sometimes
# glued straight onto it with no gap at all (e.g. "Vendorcde:"). Matched
# against each line only after it's been stripped (see find_vendor_code_line)
# - the free OCR text pads a right-aligned line with however many leading
# spaces stand in for its real x position on the page (routinely 80-100+
# for a caption stamped near the top-right corner), which would otherwise
# blow straight through the "vendor" must appear within the first ~20
# characters budget below.
_LABEL_LINE_RE = re.compile(r"(?i)^.{0,20}\bvendor\b(.{0,15}?)[:;]\s*(\S.*)$")

# "vendor" also legitimately labels several OTHER fields on the same page
# (Vendor Name, Vendor Address, Vendor GSTIN, ...) - _LABEL_LINE_RE's own
# loose "vendor<junk>[:;]" shape (needed since "code" itself is often the
# garbled half - see its own comment) matches those too, e.g. a plain
# "FreeLancer Vendor Name:Jagdish Chandra" letterhead line reads as if
# "Jagdish Chandra" were the raw vendor CODE. Rejected here by checking
# the middle text isn't one of these other, unambiguous field labels -
# "no" is deliberately absent (RIGHT_FIELDS' own "vendor no" alias is a
# genuine Vendor Code label, not a different field).
_NOT_VENDOR_CODE_RE = re.compile(
    r"(?i)\b(name|address|gstin|uin|gst|pan|email|e-mail|phone|mobile|"
    r"state|city|pin\s*code|invoice)\b"
)


def _is_vendor_code_label(middle):
    return not _NOT_VENDOR_CODE_RE.search(middle)

# Fallback for when "vendor" itself is the garbled half, not just "code"
# (real cases: "NAV Verdorcode: HEWeNT27J" - n/r swapped and the space
# dropped; "NAV Venbr ce; SoR^NFOo8i3" - missing letters). "NAV" survives
# intact in both, so anchor on that word boundary instead and only accept
# the text between it and the separator when it's still recognizably
# close to "vendor code" (see the ratio check below) - guards against
# some unrelated "NAV ...:" caption elsewhere on the page being mistaken
# for this one.
_NAV_FUZZY_LINE_RE = re.compile(r"(?i)^.{0,20}\bnav\b(.{0,20}?)[:;]\s*(\S.*)$")
_NAV_FUZZY_RATIO_MIN = 0.6


def find_vendor_code_line(text):
    """The raw value half of a "<junk>Vendor<junk>[:;] <code>" line found
    anywhere in the free OCR `text`, or '' if no such line exists.
    Sample: find_vendor_code_line('   NAv Vendor cde;SoR1NFOo813') -> 'SoR1NFOo813'"""
    for line in (text or "").splitlines():
        m = _LABEL_LINE_RE.match(line.strip())
        if m and _is_vendor_code_label(m.group(1)):
            return m.group(2).strip()
    for line in (text or "").splitlines():
        m = _NAV_FUZZY_LINE_RE.match(line.strip())
        if m:
            label_bit = m.group(1).strip().lower()
            ratio = difflib.SequenceMatcher(None, label_bit, "vendor code").ratio()
            if ratio >= _NAV_FUZZY_RATIO_MIN:
                return m.group(2).strip()
    return ""


# The label's own ":" separator is sometimes OCR'd as a digit rather than
# punctuation (seen as "1") when the anchor-based extractor - not the
# free-text fallback above, which requires a literal ":"/";" - reads the
# value from tokens sitting to the right of a cleanly-read "Vendor Code"
# label; that misread colon then arrives as its own leading token glued
# onto the front of the real value (e.g. "1 ZACD021S" for a true
# "ZACO0215"). Stripped before anything else - a real code never starts
# with an isolated single character set off by its own space.
_LEADING_COLON_RE = re.compile(r"^\s*[1:;]\s+")

# Letters/digits handwriting or OCR commonly confuses for one another.
# Each group maps to one canonical digit (its first character) - used both
# to compare the OCR'd letter block against a vendor-name-derived
# candidate under confusion-tolerant equality, and to normalize the
# trailing digit run back to clean digits. "U"/"D" are grouped with "O"/"0"
# (both round-shaped, easily read for one another), and "J"/"F" with
# "1"/"I"/"L", on the strength of real observed/confirmed cases (a
# handwritten "U" read by OCR as lowercase "o"; a single-stroke "1" read
# as "J" in one case and lowercase "f" in another; a "D" read where a "O"
# was written) - tune further as more real examples turn up.
_CONFUSION_GROUPS = ["0OUD", "1ILJF^|!", "2Z", "5S", "6G", "8B", "9P"]

_CANON_MAP = {ch: group[0] for group in _CONFUSION_GROUPS for ch in group}


def _canon(ch):
    """The canonical digit for one confusable character, or the character
    itself (upper-cased) when it isn't part of any confusion group.
    Sample: _canon('o') -> '0'; _canon('R') -> 'R'"""
    return _CANON_MAP.get(ch.upper(), ch.upper())


def _confusable_score(a, b):
    """How many of two equal-length strings' characters agree once each
    is mapped through _canon - the letter-block match score.
    Sample: _confusable_score('T9MGoR', 'TPMGUR') -> 6"""
    return sum(1 for x, y in zip(a, b) if _canon(x) == _canon(y))


def _is_subsequence(sub, word):
    """True when every character of `sub`, IN ORDER, can be found
    somewhere within `word` - not necessarily contiguous (letters of
    `word` in between are simply skipped) - under the same confusion-
    tolerant character equality as _confusable_score. Requires `sub`'s
    OWN first character to canon-match `word`'s own first character - a
    vendor's shorthand for one of its name's words always seems to start
    on that word's own initial letter (every real example seen so far
    does - "SAR" for SARASWATHI, "TEC" for TECHNOLOGIES, "ANK" for
    ANAKAGE, ...), so requiring it keeps this from validating some
    coincidental 3-letter run buried in the middle of an unrelated word.

    Not every vendor's code takes a strict 3-letter PREFIX of its word -
    e.g. "ANK" for "Anakage" (A-N-A-K-A-G-E: A, N, then K, skipping the
    2nd "A") - so a literal-prefix-only check would wrongly call this
    "not the same word" and flag an already-correct code doubtful for no
    reason.
    Sample: _is_subsequence('ANK', 'ANAKAGE') -> True (A,N,then K - skips the 2nd A)
    Sample: _is_subsequence('ANK', 'TPMGURU') -> False (no 'A' first)"""
    if not sub or not word or _canon(sub[0]) != _canon(word[0]):
        return False
    pos = 0
    for c in sub:
        while pos < len(word) and _canon(word[pos]) != _canon(c):
            pos += 1
        if pos >= len(word):
            return False
        pos += 1
    return True


def _digits_only(text):
    """Every character mapped through _canon to its confusion group's
    canonical digit; '' returned in place of any character that still
    isn't a digit afterwards, so the caller can tell a clean numeric run
    apart from one that didn't resolve.
    Sample: _digits_only('U0I') -> '001'"""
    return "".join(_canon(c) if _canon(c).isdigit() else "?" for c in text)


def _vendor_words(vendor_name):
    """Every >=3-letter word in the vendor name, upper-cased, in order.
    Sample: _vendor_words('Zaco Computers Pvt Ltd') -> ['ZACO', 'COMPUTERS', 'PVT', 'LTD']"""
    return [w.upper() for w in re.findall(r"[A-Za-z]+", vendor_name or "") if len(w) >= 3]


def vendor_word_prefixes(vendor_name):
    """(word1_prefix, [word2_prefix, word3_prefix, ...]) - the first 3
    letters of the vendor name's own first word, and of its 2nd/3rd words
    as candidates for the code's second 3-letter block. '' / [] when the
    vendor name has fewer than 2 usable (>=3 letter) words.
    Sample: vendor_word_prefixes('SARASWATHI TECHNOLOGIES')
            -> ('SAR', ['TEC'])"""
    words = _vendor_words(vendor_name)
    if len(words) < 2:
        return "", []
    return words[0][:3], [w[:3] for w in words[1:3]]


def _letter_block_candidates(vendor_name):
    """Every plausible letter-block shape for this vendor's code, longest/
    most-specific first: the first word in FULL (e.g. "ZACO"), then word1's
    own 3-letter prefix combined with EVERY prefix length of word 2-or-3
    from 3 up to that word's own full length (e.g. "SUR"+"INFO" as well as
    "SUR"+"INF" - "Sureworks Infotech..."'s own code takes 4 letters of
    "Infotech", not 3). Not a single fixed shape - different vendors' own
    codes follow different ones, and even the "2nd word" half isn't always
    exactly 3 letters."""
    words = _vendor_words(vendor_name)
    if not words:
        return []
    w1 = words[0]
    candidates = [w1]
    if len(w1) >= 3:
        for w in words[1:3]:
            for n in range(3, len(w) + 1):
                candidates.append(w1[:3] + w[:n])
    return candidates


def _confusable_prefix_candidate(stripped, vendor_name):
    """The longest run at the very start of `stripped` that matches the
    vendor name's own first word under confusion-tolerant equality (same
    _canon comparison as everything else here) - e.g. "BRISKI" for a raw
    "BRISK101" against "Briskinfosec" (a portmanteau word with no natural
    3-letter or whole-word split; confirmed correct - the code takes 6
    letters, "BRISK" + "I", not the shorter 5-letter "BRISK" a stricter
    literal-only comparison would have stopped at, since raw "1" and
    word1's own "I" agree once mapped through the same confusion class
    already used everywhere else in this module). Built from word1's OWN
    characters, not the raw reading's, at every position - so the "1" in
    the raw code that agreed with "I" comes back as the letter "I" it
    actually is, not left as a digit. '' when under 3 chars.

    An earlier version of this function deliberately used STRICT literal
    equality here, reasoning that confusion-tolerance would "over-match"
    into the digit run - that reasoning was wrong: BRISKI01 (confirmed by
    direct instruction) IS the over-match it was built to avoid, so
    consistency with the rest of the module's confusion tolerance turned
    out to be correct after all, not the exception."""
    words = _vendor_words(vendor_name)
    if not words:
        return ""
    w1 = words[0]
    n = 0
    for x, y in zip(stripped, w1):
        if _canon(x) != _canon(y):
            break
        n += 1
    return w1[:n] if n >= 3 else ""


def correct_vendor_code(raw_code, vendor_name):
    """
    Correct a hand-written NAV vendor code read off a scan, using the
    vendor's own name as a reference for what its letter block should be.
    Returns (corrected_code, confidence) with confidence in
    {"confident", "doubtful", "none"}.

    Sample inputs:
        ('T9MGoRU0 I', 'TPM GURU PRIVATE LIMITED') -> ('TPMGUR001', 'confident')
        ('1 ZACD021S', 'Zaco Computers Pvt Ltd')    -> ('ZACO0215', 'confident')
        ('', 'TPM GURU PRIVATE LIMITED')            -> ('', 'none')
        ('ABCDEF12', 'Some Unrelated Name')         -> ('ABCDEF12', 'doubtful')

    Every letter-block candidate from the vendor's own name (see
    _letter_block_candidates) is scored against the OCR'd head under
    confusion-tolerant comparison; the best-fitting one wins by mismatch
    RATIO (not raw count, so a short candidate isn't unfairly penalized
    against a longer one) and must score a PERFECT match - zero characters
    left disagreeing once the confusion classes are applied - to be
    trusted; a single genuine mismatch means the candidate doesn't really
    describe what's printed, so the RAW reading is kept instead of being
    overwritten by a wrong guess. A confident result also needs the
    remaining run to resolve to clean digits once the same confusion
    classes are applied. Anything less is returned as read (only the
    digit-class normalization applied to the tail, if any) but flagged
    doubtful for a human to confirm - never silently guessed.
    """
    if not raw_code or not raw_code.strip():
        return "", "none"

    raw_code = _LEADING_COLON_RE.sub("", raw_code)
    stripped = re.sub(r"\s+", "", raw_code).upper()
    if len(stripped) < 4:
        return raw_code.strip(), "doubtful"

    candidates = _letter_block_candidates(vendor_name)
    prefix = _confusable_prefix_candidate(stripped, vendor_name)
    if prefix:
        candidates.append(prefix)

    # Not every vendor's code takes a strict 3-letter PREFIX of each word -
    # some pick 3 letters from within it, in order but not contiguous (see
    # _is_subsequence's own note, e.g. "ANK" for "Anakage": A, N, then K,
    # skipping the 2nd A). When the raw reading's own first 6 characters
    # validate this way against (word1, word2-or-3), trust the reading
    # AS READ rather than "correcting" it toward a plain prefix that isn't
    # actually what this vendor's code follows.
    words = _vendor_words(vendor_name)
    if len(words) >= 2 and len(stripped) >= 6:
        head6 = stripped[:6]
        if _is_subsequence(head6[:3], words[0]) and any(
            _is_subsequence(head6[3:6], w) for w in words[1:3]
        ):
            candidates.append(head6)

    best_cand, best_len, best_mismatches = None, 0, None
    for cand in candidates:
        n = len(cand)
        if n > len(stripped):
            continue
        score = _confusable_score(stripped[:n], cand)
        mismatches = n - score
        # Ratio comparison via cross-multiplication (avoids float
        # precision issues, exact for these small integers). A SHORTER
        # candidate that happens to be a prefix of a longer one (e.g.
        # "TPM" vs "TPMGUR") can tie it on ratio alone - prefer the
        # LONGER one then, since it accounts for more of the evidence and
        # is the more specific/informative match.
        better = (
            best_mismatches is None
            or mismatches * best_len < best_mismatches * n
            or (mismatches * best_len == best_mismatches * n and n > best_len)
        )
        if better:
            best_cand, best_len, best_mismatches = cand, n, mismatches

    # Strictly 0 mismatches, not "close enough" - every one of the
    # confusion cases already validated (P<->9, U/O/D<->0, ...) scores 0
    # mismatches under _confusable_score's canon-equality (that's the
    # whole point of the confusion table). Any ACTUAL mismatch left after
    # that means the candidate doesn't really describe what's printed -
    # e.g. "Anakage" -> word1-prefix candidate "ANA" against a raw "ANK"
    # scores 1 mismatch (A vs K, not a real confusion pair) and must NOT
    # be trusted enough to overwrite an already-correct raw reading with
    # a wrong "corrected" one.
    head_confident = best_cand is not None and best_mismatches == 0
    if head_confident:
        corrected_head, head_len = best_cand, best_len
    else:
        # No confident candidate - assume the more common 6-letter shape
        # for where the letters end and the trailing digit run begins.
        head_len = min(6, len(stripped))
        corrected_head = stripped[:head_len]

    corrected_tail = _digits_only(stripped[head_len:])
    tail_confident = bool(corrected_tail) and "?" not in corrected_tail

    corrected = corrected_head + corrected_tail.replace("?", "")
    confidence = "confident" if (head_confident and tail_confident) else "doubtful"
    return corrected, confidence
