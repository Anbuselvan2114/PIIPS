"""
Anchor-based header field extraction.

Instead of assuming one fixed invoice layout, this finds each field by its
label ("anchor") and reads the value either to the RIGHT of the label (same
row, e.g. "Invoice No. : NT/1659") or, if the right cell is another label /
empty, BELOW it (next rows in the same column band, e.g. the Tally grid).
The same code therefore handles both the colon/right-value layouts and the
value-below grid layouts.

Returns a dict using the same field keys that invoice_schema.build_invoice_json
consumes (Invoice No., Dated, Buyer Name, Buyer GSTIN/UIN, ...).
"""

import re


# Fields whose value sits to the right of (or below) a label anchor.
RIGHT_FIELDS = {
    "Invoice No.": [
        "invoice no", "invoice number", "invoice #", "inv no", "bill no",
        "invioce no",  # genuine vendor-template typo seen on a real invoice, not an OCR artifact
        "invoice :",  # some vendors label it bare "Invoice :" with no "No."/"No" at all
    ],
    "Dated": ["dated", "invoice date", "date"],
    "Buyer's Order No.": [
        "p.o. no", "p.o no", "po no", "buyer's order no",
        "buyer's order", "order no", "purchase order",
    ],
    "Place of Supply": ["place of supply"],
    "Reference No. & Date.": ["reference no", "other references"],
    "Mode/Terms of Payment": ["mode/terms of payment", "terms of payment"],
    # Generic (non-Tally) billing-statement layouts label the parties
    # directly instead of using a "Buyer (Bill To)" section header — these
    # phrases are specific enough not to collide with that section marker.
    "Buyer Name": ["customer name"],
    "Seller Name": ["vendor name"],
}

# Left-column section markers -> which party the following lines describe.
SECTION_MARKERS = [
    ("Consignee", ["consignee", "ship to", "shipped to", "shipping addres"]),
    ("Buyer", ["party details", "customer detail", "details of receiver",
               "bill to", "billed to", "buyer (bill to)", "buyer"]),
]

# Markers only recognized as a WHOLE line (after stripping trailing
# punctuation), never as a substring — "to" alone is far too short/common
# to safely match inside arbitrary text (e.g. "Total"), but informal/
# freelancer invoices commonly address the recipient with a standalone
# "To," line before their name/address, the same way a letter would.
EXACT_SECTION_MARKERS = [
    ("Buyer", ["to"]),
]

# Phrases that identify a token/row as a label (so it is never taken as a
# value, and so party-name detection skips them).
LABEL_WORDS = [
    "invoice no", "invoice number", "invioce no", "dated", "invoice date", "place of supply",
    # bare "Date" as its own grid-column header (e.g. "Invoice No. | Date"
    # with the values in the row below) — without this, a right-scan for
    # "Invoice No." keeps going past its own column into "Date"'s and
    # returns that label text itself as the (wrong) Invoice No. value.
    "date",
    "p.o. no", "po no", "buyer's order", "order no", "purchase order",
    "reference no", "other references", "mode/terms", "terms of payment",
    "gstin", "uin", "state name", "state code", "hsn", "sac", "description",
    "qty", "quantity", "rate", "amount", "unit", "price", "code", "tax invoice",
    # Other generic GST document-type titles besides "Tax Invoice" (e.g. a
    # combined invoice/dispatch document) - a document-type label, not a
    # company name, wherever it prints as the page's own top line.
    "commercial invoice", "delivery challan",
    # GST goods invoices print 3 copies captioned "Original"/"Duplicate"/
    # "Triplicate" (for Recipient/Transporter/Supplier respectively) - all
    # 3 are document-copy markers, never company name/address content,
    # wherever they print as the page's own top line. "duplicate" and
    # "triplicate" alone were already here; "original" (not just the
    # "original copy" phrase) was the missing third.
    "original", "original copy", "duplicate", "triplicate", "contect person",
    "contact person", "shipping addres", "shipping address", "address",
    "party details", "consignee", "bill to", "ship to", "s.n.", "sl.no",
    "e-mail", "email", "tel", "phone", "msme", "bank", "ifsc", "terms",
    "declaration", "authorised", "authorized", "signatory", "grand total",
    "tax rate", "taxable", "total tax", "due date", "name of product",
    "service", "particulars",
    # dispatch / delivery labels — these are field captions, never values,
    # so a blank "Buyer's Order No." must not swallow the next label below it.
    "despatch", "dispatch", "despatched", "dispatched", "document no",
    "delivery note", "supplier's ref", "supplier ref", "e-way", "eway",
    "other reference", "reference no",
    # GST e-Invoice QR-code block (IRN/Ack No./Ack Date/"e-Invoice" caption)
    # sits above the seller's letterhead on this layout — without these,
    # "IRN : <64-char hash>" becomes the very first unclaimed left-column
    # line and gets captured as the Seller Name/Address instead.
    "irn", "ack no", "ack date", "e-invoice",
]

GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9])\b")
DATE_RE = re.compile(r"\d{1,2}[-/][A-Za-z0-9]{2,}[-/]\d{2,4}")


def _gstin_from_text(text):
    """Return a GSTIN found in `text`, reconstructing the common OCR split
    where the 2-digit state code is separated from the 13-char core
    (e.g. 'GSTIN/UIN  AABCP8005C2ZZ 33'). Returns '' if none found.

    Tries the ORIGINAL text first (spaces intact) before the space-stripped
    version: GSTIN_RE's trailing \\b needs a word/non-word transition right
    after the 15th character, and blindly stripping every space can glue a
    real GSTIN straight onto the next word with no separator left at all
    (e.g. "...9257H2ZC INVOICE :..." -> "...9257H2ZCINVOICE:..." - now "C"
    is followed by "I", both word characters, so \\b never matches even
    though the source text plainly had a GSTIN there). Stripped-text search
    stays as the fallback for the OCR-garbled case this was written for
    (the state code split off with its OWN stray space, e.g.
    "GSTIN/UIN  AABCP8005C2ZZ 33")."""
    g = GSTIN_RE.search(text) or GSTIN_RE.search(text.replace(" ", ""))
    if g:
        return g.group(1)
    alnum = re.sub(r"[^A-Z0-9]", "", text.upper()).replace("GSTIN", "").replace("UIN", "")
    core = re.search(r"[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", alnum)
    if core:
        rest = alnum.replace(core.group(0), "")
        st = re.search(r"\d{2}", rest)
        return (st.group(0) if st else "") + core.group(0)
    return ""


def _row_text(row):
    return " ".join(w["text"].strip() for w in sorted(row, key=lambda w: w["x"])).strip()


def _row_text_with_spans(row):
    """Like _row_text, but also returns each word's (start, end) character
    span within the joined string, so a phrase match can be mapped back to
    the specific word it ends in."""
    srow = sorted(row, key=lambda w: w["x"])
    parts, spans = [], []
    pos = 0
    for w in srow:
        t = w["text"].strip()
        if not t:
            continue
        start = pos
        spans.append((start, start + len(t), w))
        parts.append(t)
        pos = start + len(t) + 1  # +1 for the joining space
    return " ".join(parts), spans


def _is_label(text):
    low = text.lower()
    return any(lbl in low for lbl in LABEL_WORDS)


def _label_span(text):
    """(start, end) of the earliest LABEL_WORDS phrase in `text`
    (case-insensitive), or None if none is present."""
    low = text.lower()
    best = None
    for lbl in LABEL_WORDS:
        i = low.find(lbl)
        if i != -1 and (best is None or i < best[0]):
            best = (i, i + len(lbl))
    return best


def _looks_like_phone_line(text):
    """A line that's a bare phone/mobile number with no label word at all
    (e.g. "M +91 9900716651 ; 044-45015154") — mostly digits/phone
    punctuation rather than real name/address text. A genuine address line
    with a pincode has far more letters than digits, so requiring digits to
    both meet a phone-length minimum AND outnumber letters keeps this from
    matching normal address text."""
    digits = sum(1 for c in text if c.isdigit())
    letters = sum(1 for c in text if c.isalpha())
    return digits >= 7 and digits > letters


_URL_RE = re.compile(r"^[\w.-]+\.(com|in|co\.in|org|net|io|co)$", re.IGNORECASE)


def _looks_like_url_line(text):
    """A line that IS a website URL on its own (e.g. 'www.careinfotech.co.in')
    — never real name/address content. Requires the whole trimmed line to be
    domain-shaped (not just contain a domain-like substring), so a genuine
    address/email line isn't mistaken for one."""
    return bool(_URL_RE.match(text.strip().rstrip(".,;")))


_HASH_FRAGMENT_RE = re.compile(r"^[0-9a-fA-F]{10,}$")


def _looks_like_hash_fragment(text):
    """A line that's a bare hex-hash fragment — e.g. a GST e-Invoice IRN
    (a 64-char hex hash) wrapped across two text-layer/OCR lines, whose
    second half carries no "IRN"/"Ack No." label of its own — never real
    name/address content. Requires the whole line, with ALL whitespace
    removed (not just leading/trailing) and a line-wrap hyphen stripped,
    to be a long run of hex characters with both digits and letters, so a
    genuine short code/word isn't mistaken for one. Internal whitespace is
    stripped too because some PDF generators render a hex hash as two+
    separate OCR/text-layer words on the same row (split at an odd offset,
    sometimes even out of numeric order) rather than one glued token."""
    t = re.sub(r"\s+", "", text).rstrip("-")
    if not _HASH_FRAGMENT_RE.match(t):
        return False
    return any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def _clean_value(text):
    return text.strip().lstrip(":").strip(" :-.–")


def _anchor_glued_value(anchor_word, offset):
    """
    Some PDFs render a label and its value as ONE OCR token with no space
    (e.g. "Invoice No.D/2026-27/341" or "Date:15-07-2026"), or split the
    matched phrase itself across two adjacent tokens with the tail landing
    glued to the value (e.g. label token "INVOICE" ends the phrase "invoice
    :", and the very next token is ": ACSPLTN2627/1748" — the anchor, since
    the phrase's END falls inside IT). Either way `offset` is where the
    matched phrase's end falls inside anchor_word's own text (computed by
    the caller from the row-wide match position vs. this token's own span
    start) — whatever follows at that offset is the value. Returns "" when
    that leaves nothing usable (offset at/past the token's own end — the
    phrase's end wasn't actually inside this token, e.g. the fallback
    `srow[0]` anchor, which the caller signals with offset=None).
    """
    if offset is None:
        return ""
    text = anchor_word["text"]
    if offset <= 0 or offset >= len(text):
        return ""
    return _clean_value(text[offset:])


def _value_right_or_below(rows, ri, anchor_word, offset):
    """
    Given the row index and the anchor word that matched, return the value:
    tokens to the right on the same row (minus another label), else tokens in
    the same column band on the next few rows.
    """

    row = sorted(rows[ri], key=lambda w: w["x"])
    ax = anchor_word["x"]

    # --- label and value OCR-glued into the anchor's own token, no space ---
    glued = _anchor_glued_value(anchor_word, offset)
    if glued:
        return glued

    # --- value to the right on the same row (stop at the next label) ---
    # A label isn't always in our vocabulary (LABEL_WORDS is necessarily
    # incomplete), but in Tally/GST-style layouts a label token is reliably
    # followed by its own bare ":" before its value — so a token immediately
    # followed by a lone ":" must itself be a label for a different field,
    # regardless of whether we recognize its text.
    right_row = [w for w in row if w["x"] > anchor_word["right"] - 1]
    collected = []
    for idx, w in enumerate(right_row):
        nxt = right_row[idx + 1] if idx + 1 < len(right_row) else None
        if nxt is not None and nxt["text"].strip() == ":":
            break
        if _is_label(w["text"]):
            # This word may be a genuine value glued to the NEXT field's
            # label with no gap wide enough to land in its own box (e.g.
            # "1424 Dated:" - value "1424" immediately followed by the
            # next label, merged into one token upstream) - keep whatever
            # precedes the label match rather than discarding the value
            # along with it. A label sitting at the very start (offset 0,
            # e.g. a bare "Dated:" token) still has nothing to keep here.
            span = _label_span(w["text"])
            if span is not None and span[0] > 0:
                lead = w["text"][:span[0]].strip()
                if lead:
                    collected.append(lead)
            break
        collected.append(w["text"])
    right_text = _clean_value(" ".join(collected))
    if right_text:
        return right_text

    # --- value below, in the same (bounded) column band ---
    band_lo, band_hi = ax - 40, ax + 220
    for nri in range(ri + 1, min(ri + 4, len(rows))):
        nrow = sorted(rows[nri], key=lambda w: w["x"])
        cell = [w for w in nrow if band_lo <= w["x"] <= band_hi]
        text = _clean_value(" ".join(w["text"] for w in cell))
        if not text:
            continue
        if _is_label(text):
            break
        return text

    return ""


# A single-word label that continues onto the NEXT physical row, e.g. a
# narrow "Invoice Details" column that wraps "Invoice No. :" as "Invoice" /
# "No. :" on two separate lines. Every RIGHT_FIELDS phrase for these fields
# needs both halves on ONE line to ever match (see _merge_wrapped_labels);
# without merging them, the row-by-row scan below never sees "invoice no"
# as a substring and the field is silently left empty.
_WRAPPED_LABEL_CONTINUATIONS = {
    "invoice": ("no", "number", "#"),
    "purchase": ("order",),
    "reference": ("no", "number"),
}


def _merge_wrapped_labels(rows):
    """Splice a row that is JUST one of _WRAPPED_LABEL_CONTINUATIONS's keys
    with the next row, when that next row starts with the expected
    continuation word - into one virtual row (word spans combined) so the
    normal phrase-matching loop in extract() sees them as a single line,
    the same as an unwrapped layout would. Deliberately narrow (whole-row
    text must be EXACTLY the bare label word, not just contain it) so an
    unrelated two-line sequence is never merged and its values
    misattributed."""
    merged = []
    skip = False
    for i, row in enumerate(rows):
        if skip:
            skip = False
            continue
        text = _row_text(row).strip().lower().rstrip(":.")
        continuations = _WRAPPED_LABEL_CONTINUATIONS.get(text)
        if continuations and i + 1 < len(rows):
            next_text = _row_text(rows[i + 1]).strip().lower()
            if any(next_text.startswith(c) for c in continuations):
                merged.append(row + rows[i + 1])
                skip = True
                continue
        merged.append(row)
    return merged


def _merge_wrapped_values(rows):
    """Splice a bare value-continuation fragment - e.g. "UG392" wrapping the
    tail of "Inv No. :ZCPLM2627A-" printed on the row above, in a narrow
    Invoice-Details column - back onto the specific word it wraps, so the
    normal right-of-anchor value scan in extract() sees the whole value as
    one token instead of just its first half.

    A fragment row's own x position (not the previous row's LAST word) picks
    the merge target: two unrelated fields (e.g. "Inv No. :X- Date :Y") can
    share one row, so blindly extending the last word would staple a wrap
    onto the wrong value. The target is whichever word's cell the fragment
    visually falls under - the closest word starting AT OR BEFORE the
    fragment's x (a label+value glued into one token, as in "Order No.
    :SPRPUR/2026", can start well to the left of where its value visually
    ends, so a plain nearest-by-distance match would miss it entirely).
    Deliberately narrow - the fragment must be a single word with no ':' of
    its own (so it can never be a fresh field's row) and not a recognized
    label - so an unrelated one-word row is never misattributed."""
    merged = []
    for row in rows:
        srow = sorted(row, key=lambda w: w["x"])
        text = " ".join(w["text"].strip() for w in srow).strip()
        words = text.split()
        if merged and len(words) == 1 and ":" not in text and not _is_label(text):
            prev_row = merged[-1]
            frag_x = srow[0]["x"]
            before = [w for w in prev_row if w["x"] <= frag_x]
            target = (
                max(before, key=lambda w: w["x"]) if before
                else min(prev_row, key=lambda w: abs(w["x"] - frag_x)) if prev_row
                else None
            )
            if target is not None and abs(target["x"] - frag_x) <= 300:
                new_target = dict(target, text=target["text"].rstrip("-") + text)
                merged[-1] = [w for w in prev_row if w is not target] + [new_target]
                continue
        merged.append(row)
    return merged


def _split_three_column_header(rows):
    """Detect a layout where Bill To / Ship To / Invoice Details print as
    separate columns SIDE BY SIDE on one row - e.g. "Bill to: Ship to:
    Invoice Details:" - rather than the more common Tally layout where
    "Buyer (Bill To)"/"Consignee (Ship To)" are separate STACKED section
    markers within one left column, or a plain 2-column (party block /
    invoice metadata) split.

    Row-grouping bands text by Y-position across the WHOLE page width, so
    with 3 unequal-height columns side by side, a row typically ends up
    containing fragments of BOTH the Bill To and Ship To (and Invoice
    Details) columns interleaved together, and a narrow label that wraps
    onto 2 lines in one column (e.g. "Invoice" / "No. :") lands on
    different rows than its own value. Without isolating each column, the
    normal party-block scan sees "bill to" and "ship to" together on the
    same row and applies whichever section it checks first to everything
    (silently discarding the other party's block), and the wrapped
    "Invoice"/"No. :" label never matches as one phrase.

    Returns (marker_ri, bill_rows, ship_rows, detail_rows, bill_x, ship_x) -
    each *_rows a list of per-row word-lists narrowed to just that column,
    in original vertical order - or None if this row shape isn't present.
    """
    for ri, row in enumerate(rows):
        text, spans = _row_text_with_spans(row)
        low = text.lower()
        bill_idx = low.find("bill to")
        ship_idx = low.find("ship to")
        if bill_idx == -1 or ship_idx == -1:
            continue

        def _x_at(char_idx):
            return next((w["x"] for start, end, w in spans if start <= char_idx < end), None)

        bill_x = _x_at(bill_idx)
        ship_x = _x_at(ship_idx)
        if bill_x is None or ship_x is None or ship_x <= bill_x:
            continue
        details_idx = next(
            (low.find(p) for p in ("invoice details", "invoice detail", "details")
             if low.find(p) != -1),
            -1,
        )
        details_x = _x_at(details_idx) if details_idx != -1 else None
        if details_x is None:
            # No explicit "Invoice Details"/"Details" caption on the marker
            # row - some layouts start the third column directly with its
            # first real sub-label instead (e.g. "Bill To, Ship To Inv No.
            # :... Date :..."). Fall back to the first word on this row that
            # starts strictly after the "Ship To" phrase ends - the true
            # start of column 3 either way, captioned or not.
            ship_end = ship_idx + len("ship to")
            details_x = next(
                (w["x"] for start, end, w in spans if start >= ship_end and w["x"] > ship_x),
                None,
            )
        mid = (bill_x + ship_x) / 2
        ship_hi = ((ship_x + details_x) / 2) if (details_x and details_x > ship_x) else float("inf")

        bill_rows, ship_rows, detail_rows = [], [], []
        # The marker row itself often already carries the first Invoice
        # Details value alongside "Bill To"/"Ship To" (e.g. "Inv No.
        # :ZCPLM2627A-" on the very same line) - without capturing its own
        # column-3 words here too, that value is silently lost entirely
        # (this row is otherwise skipped: `header_rows[:marker_ri]` stops
        # BEFORE it, and the scan below only covers rows AFTER it).
        if ship_hi != float("inf"):
            marker_dw = [w for w in row if w["x"] >= ship_hi]
            if marker_dw:
                detail_rows.append(marker_dw)
        for later in rows[ri + 1:]:
            bw = [w for w in later if bill_x - 1 <= w["x"] < mid]
            sw = [w for w in later if mid <= w["x"] < ship_hi]
            dw = [w for w in later if w["x"] >= ship_hi] if ship_hi != float("inf") else []
            if bw:
                bill_rows.append(bw)
            if sw:
                ship_rows.append(sw)
            if dw:
                detail_rows.append(dw)

        return ri, bill_rows, ship_rows, detail_rows, bill_x, ship_x
    return None


def _marker_row(label, x, sample_row):
    y = sample_row[0].get("y", 0) if sample_row else 0
    return [{"text": label, "x": x, "y": y}]


def extract(header_rows, footer_rows, page_width):
    fields = {}
    three_col = _split_three_column_header(header_rows)
    if three_col is not None:
        marker_ri, bill_rows, ship_rows, detail_rows, bill_x, ship_x = three_col
        # Invoice Details' own words, isolated from Bill To/Ship To, feed
        # the ordinary right-field anchor scan below exactly like a normal
        # single-column layout would - including _merge_wrapped_labels
        # picking up an "Invoice"/"No. :" 2-line wrap that's now a clean,
        # uninterleaved sequence.
        rows = _merge_wrapped_labels(header_rows[:marker_ri] + _merge_wrapped_values(detail_rows))
    else:
        rows = _merge_wrapped_labels(header_rows)
    divider = page_width * 0.42
    # Rows whose LEFT-hand text (the half the party-block pass reads) was
    # already consumed by a labeled anchor there — e.g. "Customer Name: Acme
    # Ltd" shouldn't additionally become the Seller Name once "Customer Name"
    # claimed it. A row where the anchor is on the RIGHT (the common Tally
    # case: seller letterhead on the left, "Invoice No. / Dated" on the
    # right of the SAME row) must not be claimed — the two halves are
    # unrelated content that only happen to share a row.
    claimed_rows = set()

    # First row (not claimed on its left side) where "Invoice No." or
    # "Dated" was anchored on the RIGHT half of the page - the common
    # Tally-style layout prints this metadata alongside the buyer block,
    # not the seller's own letterhead. Used below only as a last-resort
    # structural marker when a document has no textual "Bill To"/"Ship
    # To" label at all.
    meta_row = None

    # ------------------------------------------------------------------
    # Right-value / below-value anchored fields
    # ------------------------------------------------------------------
    for ri, row in enumerate(rows):
        srow = sorted(row, key=lambda w: w["x"])
        if not srow:
            continue
        row_text, spans = _row_text_with_spans(row)
        low = row_text.lower()
        # Skip the GST e-Invoice QR-code block (IRN/Ack No./Ack Date) here
        # too, same as the party-block pass below — "Ack Date : Aug 6,
        # 2026, 6:01:00 PM" contains "date" (one of "Dated"'s own RIGHT_
        # FIELDS phrases) and, being near the top of the page, would
        # otherwise claim "Dated" before the invoice's real Invoice No./
        # Dated row is ever reached.
        if low.startswith(("irn", "ack no", "ack date", "e-invoice")):
            continue
        for field, phrases in RIGHT_FIELDS.items():
            if field in fields:
                continue
            for ph in phrases:
                idx = low.find(ph)
                if idx != -1:
                    # The anchor is the word whose own span contains the end
                    # of the matched phrase — found by exact phrase position,
                    # not by "any word containing the phrase's last word",
                    # which mis-anchors when two labels share a short common
                    # suffix on the same row (e.g. "Invoice No." vs.
                    # "e-Way Bill No." both contain "no").
                    match_end = idx + len(ph)
                    anchor_span = next(
                        ((start, w) for start, end, w in spans if start < match_end <= end),
                        None,
                    )
                    if anchor_span is not None:
                        anchor_start, anchor = anchor_span
                        offset = match_end - anchor_start
                    else:
                        anchor = srow[0]
                        offset = None
                    val = _value_right_or_below(rows, ri, anchor, offset)
                    if val:
                        fields[field] = val
                        if anchor["x"] < divider:
                            claimed_rows.add(ri)
                        elif field in ("Invoice No.", "Dated") and meta_row is None:
                            meta_row = ri
                    break

    # Invoice No. fallback for the isolated Invoice Details column (3-column
    # header - see _split_three_column_header): OCR's row-banding can pair
    # "Invoice"'s value on the SAME row as "Invoice" itself, while "No. :"
    # (the rest of the wrapped label) lands on the NEXT row entirely (e.g.
    # "Invoice RSI/26-27/1399" / "No. :") - "invoice no" then never appears
    # as one phrase anywhere, so the anchor loop above finds nothing. Only
    # tried within the isolated detail column (never the whole page) and
    # only accepts a non-date value, so it can't collide with an "Invoice"
    # + bare-date row (that's Invoice DATE, not Invoice No.) or misfire on
    # an ordinary "Tax Invoice" title elsewhere on the page.
    if "Invoice No." not in fields and three_col is not None:
        for row in detail_rows:
            text = _row_text(row)
            m = re.match(r"^invoice\s+(\S.*)$", text, re.IGNORECASE)
            if m and not DATE_RE.fullmatch(m.group(1).strip()):
                fields["Invoice No."] = _clean_value(m.group(1))
                break

    # Dated fallback: some layouts print the document date right next to
    # the Invoice No. value with no "Date"/"Dated" label of its own (e.g.
    # "Inv No.PW/GST/26-27-041   04/08/2026") - the label-anchored pass
    # above never finds a "Dated" anchor there at all. When that's happened
    # (no "Dated" field, but an Invoice No. row was recorded), look for a
    # bare date-shaped token on that same row.
    if "Dated" not in fields and meta_row is not None:
        row_text = _row_text(rows[meta_row])
        inv_no = fields.get("Invoice No.", "")
        for m in DATE_RE.finditer(row_text):
            if m.group(0) not in inv_no:
                fields["Dated"] = m.group(0)
                break

    # Seller Name fallback: some letterheads put the company's own brand
    # name as the very first line of the page wherever the logo sits
    # (often right-aligned/centered), outside the left-column address
    # block the pass below scans — so it's never reached there. Only a
    # short, plain, title-like first line qualifies (not a label/title
    # keyword, phone line or URL), so this can't misfire on ordinary
    # Tally-style layouts where the top line is "TAX INVOICE" (a
    # recognized label). Uses setdefault so an anchor-claimed Seller Name
    # (e.g. "Vendor Name:") always takes priority, and runs BEFORE the
    # party-block pass below so that pass treats the true first address
    # line as Address, not a second (silently dropped) Name.
    if rows:
        first_text = _row_text(rows[0])
        if (first_text and not _is_label(first_text)
                and not _looks_like_phone_line(first_text)
                and not _looks_like_url_line(first_text)
                and len(first_text.split()) <= 4
                and not any(c.isdigit() for c in first_text)):
            fields.setdefault("Seller Name", first_text)

    # ------------------------------------------------------------------
    # Party blocks: Seller (top), then Buyer / Consignee by marker.
    # ------------------------------------------------------------------
    def _party_pass(forced_switch_row=None, rows_override=None):
        """One left-column scan, classifying each line as Seller/Buyer/
        Consignee content. Returns (fields, any_marker_switch). Seeded
        fresh from the outer `fields` each call so a retry (see below)
        starts clean rather than compounding a failed first attempt.
        `forced_switch_row` is only used on that retry, to force the
        Seller->Buyer switch at the invoice-metadata row when the
        document has no textual section marker at all. `rows_override`
        (see _side_by_side_party_rows) replaces the normal row scan and
        divider cut entirely - its indices don't correspond to the real
        page, so claimed_rows (a same-row-title-banner edge case that
        cannot occur in a synthesized column split) is not applied there."""
        out = dict(fields)
        section = "Seller"
        # A party's Name may already be set by the anchored-field pass
        # above (e.g. "Vendor Name"/"Customer Name" on a claimed row) —
        # start that party as already-named so the next unclaimed line
        # becomes its Address, not a second (silently dropped) Name.
        named = {p: bool(out.get(f"{p} Name")) for p in ("Seller", "Buyer", "Consignee")}
        any_switch = False

        scan_rows = rows_override if rows_override is not None else rows
        for ri, row in enumerate(scan_rows):
                if rows_override is None and ri in claimed_rows:
                    continue

                # A page-wide document-title row (e.g. "Invoice Cum Delivery
                # Challan (ORIGINAL FOR RECIPIENT)") can straddle the left/
                # right divider - "Invoice Cum" left of it, "Delivery
                # Challan (ORIGINAL FOR RECIPIENT)" right of it - so neither
                # half alone contains a recognizable label phrase even
                # though the row AS A WHOLE plainly is one. Checked against
                # the full, unsliced row text (not just the left column
                # used below), but ONLY for the page's very first row - a
                # title banner can only ever be the first line of the page,
                # while every later Seller-block row legitimately pairs
                # left-column address text with right-column invoice-
                # metadata labels ("Invoice No." / "Dated") on the SAME
                # row in the common Tally layout, which would otherwise
                # false-positive as a title too.
                if ri == 0 and section == "Seller" and not named["Seller"]:
                    whole = _row_text(row)
                    if whole and _is_label(whole):
                        continue

                # A rows_override row is already narrowed to one column's
                # words (or is a single synthetic marker word) - use it as
                # given rather than re-slicing it by the page-wide divider,
                # which has no meaning for a synthesized column split.
                left = (
                    sorted(row, key=lambda w: w["x"]) if rows_override is not None
                    else sorted([w for w in row if w["x"] < divider], key=lambda w: w["x"])
                )
                if not left:
                    continue
                text = " ".join(w["text"].strip() for w in left).strip()
                if not text:
                    continue
                low = text.lower()

                # section change?
                switched = False
                for name, marks in SECTION_MARKERS:
                    if any(m in low for m in marks):
                        section = name
                        switched = True
                        break
                if not switched:
                    stripped = low.strip(" :,-.")
                    for name, marks in EXACT_SECTION_MARKERS:
                        if stripped in marks:
                            section = name
                            switched = True
                            break
                if switched:
                    any_switch = True
                    continue

                # Structural fallback for a missing section-header row: some
                # PDFs' born-digital text layer omits "Billed to"/"Shipped
                # to" entirely (rendered as an image/graphic, not real
                # text), so the marker check above never fires — but the
                # Buyer/Consignee block's first content row still follows
                # right after the Seller block. Detect it structurally
                # instead: its right-side column (a mirrored Ship-to copy)
                # duplicates this row's left-side text — a shape no genuine
                # Seller-block metadata row has (those pair a left LABEL
                # with a right VALUE, never identical text on both sides).
                if rows_override is None and section == "Seller" and named["Seller"]:
                    right = sorted([w for w in row if w["x"] >= divider], key=lambda w: w["x"])
                    right_text = " ".join(w["text"].strip() for w in right).strip()
                    if right_text and re.sub(r"\s+", "", low) == re.sub(r"\s+", "", right_text.lower()):
                        section = "Buyer"
                        any_switch = True
                    elif forced_switch_row is not None and ri >= forced_switch_row:
                        # Last resort, only tried on retry: no textual or
                        # structural marker exists anywhere in this
                        # document, so treat the row carrying the Invoice
                        # No./Dated metadata as the seller/buyer boundary -
                        # the common Tally-style layout prints that
                        # metadata alongside the buyer block, not the
                        # seller's own letterhead.
                        section = "Buyer"

                # GSTIN on this line -> party gstin (see _gstin_from_text
                # for why the original, un-stripped text is tried first)
                val = _gstin_from_text(text)
                if val:
                    out.setdefault(f"{section} GSTIN/UIN", val)
                gstin_pos = min(
                    (p for p in (low.find("gstin"), low.find("uin")) if p != -1),
                    default=-1,
                )
                if gstin_pos == -1:
                    if val:
                        continue
                else:
                    # A "GSTIN"/"UIN" label (with or without its own value on
                    # this same row - the value can instead wrap onto the
                    # NEXT row, e.g. "...Road,T.Nagar,Chennai,TamilNadu,
                    # 600017,GSTIN:" / "33AABCP8005C2ZZ") never itself
                    # belongs to name/address content, but genuine address
                    # text can precede it on the same row - keep that
                    # lead-in instead of discarding the whole row (which
                    # would silently drop the city/state/PIN it carries),
                    # same as the generic label-trim pass below does for
                    # other labels found partway through a row.
                    lead = text[:gstin_pos].strip(" :,-.")
                    if not lead:
                        continue
                    text = lead
                    low = text.lower()

                # State. Matches "state name"/"state code" (Tally-style), a bare
                # "State" label (word-bounded — \b so "Estate" in an address line
                # is never mistaken for it), or "place of supply".
                if (
                    "state name" in low or "state code" in low
                    or "place of supply" in low
                    or re.search(r"\bstate\b", low)
                ):
                    out.setdefault(
                        f"{section} State Name",
                        _clean_value(text.split(":", 1)[-1]),
                    )
                    continue

                # Skip a bare phone/mobile number line (no label word at all, so
                # _label_start below wouldn't catch it) — never real name/address
                # content.
                if _looks_like_phone_line(text):
                    continue

                # Skip a bare website URL line (e.g. "www.careinfotech.co.in") —
                # never real name/address content either.
                if _looks_like_url_line(text):
                    continue

                # Skip a bare hex-hash fragment (e.g. the wrapped second half of an
                # IRN, with no "IRN"/"Ack No." label of its own on that line).
                if _looks_like_hash_fragment(text):
                    continue

                # Skip the GST e-Invoice QR-code block lines entirely (label AND
                # value) — unlike other one-line labels below (e.g. "Address:Plot
                # No 53..." where the tail after the label IS the real value), the
                # IRN hash / Ack No. / Ack Date values are never name/address
                # content, so nothing after these labels should be kept either.
                # Same for "Reverse Charge : N" / "Credit Days : 0" — standard
                # Tally/GST metadata captions, never part of the seller's address.
                if low.startswith(("irn", "ack no", "ack date", "e-invoice",
                                    "reverse charge", "credit days")):
                    continue

                # Skip pure labels / titles / contact lines. A label phrase can
                # appear PARTWAY through an address-continuation line (e.g.
                # "...Maharashtra 421101, Phone: 8983834716 email: ...") — trim at
                # that point and keep the genuine address text before it, instead
                # of discarding real address/state/pincode content along with the
                # incidental label that happens to trail it on the same OCR row.
                # When the label sits at the very start (e.g. "Address:Plot No 53
                # block R2..." glued into one OCR token, no separate value line),
                # keep whatever follows the label instead of dropping the row
                # outright — that tail IS the value.
                span = _label_span(text)
                if span is not None:
                    start, end = span
                    if start == 0:
                        text = text[end:].lstrip(" :,-.")
                    else:
                        text = text[:start].rstrip(" ,;:-")
                    if not text:
                        continue
                    low = text.lower()

                # First non-label line = party name; rest = address
                if not named[section]:
                    out.setdefault(f"{section} Name", text)
                    named[section] = True
                else:
                    key = f"{section} Address"
                    out[key] = (out.get(key, "") + "\n" + text).strip() if out.get(key) else text

        return out, any_switch

    if three_col is not None:
        marker_row_src = header_rows[marker_ri]
        side_by_side_rows = (
            header_rows[:marker_ri]
            + [_marker_row("Bill to", bill_x, marker_row_src)] + bill_rows
            + [_marker_row("Ship to", ship_x, marker_row_src)] + ship_rows
        )
        party_fields, any_switch = _party_pass(rows_override=side_by_side_rows)
    else:
        party_fields, any_switch = _party_pass()
    if not any_switch and not party_fields.get("Buyer Name") and meta_row is not None:
        # No textual or structural section marker fired anywhere in this
        # document - retry once, using the invoice-metadata row as the
        # seller/buyer boundary instead.
        party_fields, _ = _party_pass(forced_switch_row=meta_row)
    fields.update(party_fields)

    # (Seller GSTIN fallback is applied in ocr_engine after all pages/bands
    # are merged, where the full invoice text is available.)

    # ------------------------------------------------------------------
    # Amount in words (footer)
    # ------------------------------------------------------------------
    for row in footer_rows:
        text = _row_text(row)
        low = text.lower()
        if "only" not in low:
            continue
        if low.startswith(("rupees", "inr", "indian rupees")):
            fields.setdefault("Amount Chargeable (in words)", text)
            break
        # Some layouts label this "In Words:" rather than starting the line
        # with the currency name outright, and may glue the next summary
        # field onto the SAME row (e.g. "In Words: ... Rupees Only Total:
        # 15,340.00") - cut at "only" so that trailing fragment is dropped.
        idx = low.find("in words")
        if idx != -1:
            only_end = low.find("only", idx) + len("only")
            value = re.sub(
                r"^in\s*words\s*:?\s*", "", text[idx:only_end], flags=re.IGNORECASE
            ).strip()
            if value:
                fields.setdefault("Amount Chargeable (in words)", value)
                break

    # "Place of Supply" implies the buyer's state when not stated separately.
    if not fields.get("Buyer State Name") and fields.get("Place of Supply"):
        fields["Buyer State Name"] = fields["Place of Supply"]

    return fields
