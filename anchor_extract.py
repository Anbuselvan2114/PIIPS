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
    "original copy", "duplicate", "triplicate", "contect person",
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
    (e.g. 'GSTIN/UIN  AABCP8005C2ZZ 33'). Returns '' if none found."""
    g = GSTIN_RE.search(text.replace(" ", ""))
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
    name/address content. Requires the whole trimmed line (a line-wrap
    hyphen stripped) to be a long run of hex characters with both digits
    and letters, so a genuine short code/word isn't mistaken for one."""
    t = text.strip().rstrip("-")
    if not _HASH_FRAGMENT_RE.match(t):
        return False
    return any(c.isdigit() for c in t) and any(c.isalpha() for c in t)


def _clean_value(text):
    return text.strip().lstrip(":").strip(" :-.–")


def _anchor_glued_value(anchor_word, matched_phrase):
    """
    Some PDFs render a label and its value as ONE OCR token with no space
    (e.g. "Invoice No.D/2026-27/341" or "Date:15-07-2026"). In that case the
    anchor word IS the only token — there's nothing separate to its right —
    so return whatever follows the matched phrase inside that same token.
    Returns "" if the phrase isn't actually found inside the token's text
    (e.g. the anchor was chosen via the fallback `srow[0]`).
    """
    text = anchor_word["text"]
    idx = text.lower().find(matched_phrase)
    if idx == -1:
        return ""
    return _clean_value(text[idx + len(matched_phrase):])


def _value_right_or_below(rows, ri, anchor_word, matched_phrase):
    """
    Given the row index and the anchor word that matched, return the value:
    tokens to the right on the same row (minus another label), else tokens in
    the same column band on the next few rows.
    """

    row = sorted(rows[ri], key=lambda w: w["x"])
    ax = anchor_word["x"]

    # --- label and value OCR-glued into the anchor's own token, no space ---
    glued = _anchor_glued_value(anchor_word, matched_phrase)
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


def extract(header_rows, footer_rows, page_width):
    fields = {}
    rows = header_rows
    divider = page_width * 0.42
    # Rows whose LEFT-hand text (the half the party-block pass reads) was
    # already consumed by a labeled anchor there — e.g. "Customer Name: Acme
    # Ltd" shouldn't additionally become the Seller Name once "Customer Name"
    # claimed it. A row where the anchor is on the RIGHT (the common Tally
    # case: seller letterhead on the left, "Invoice No. / Dated" on the
    # right of the SAME row) must not be claimed — the two halves are
    # unrelated content that only happen to share a row.
    claimed_rows = set()

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
                    anchor = next(
                        (w for start, end, w in spans if start < match_end <= end),
                        None,
                    )
                    if anchor is None:
                        anchor = srow[0]
                    val = _value_right_or_below(rows, ri, anchor, ph)
                    if val:
                        fields[field] = val
                        if anchor["x"] < divider:
                            claimed_rows.add(ri)
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
    section = "Seller"
    # A party's Name may already be set by the anchored-field pass above
    # (e.g. "Vendor Name"/"Customer Name" on a claimed row) — start that
    # party as already-named so the next unclaimed line becomes its
    # Address, not a second (silently dropped) Name.
    named = {p: bool(fields.get(f"{p} Name")) for p in ("Seller", "Buyer", "Consignee")}

    for ri, row in enumerate(rows):
        if ri in claimed_rows:
            continue
        left = sorted([w for w in row if w["x"] < divider], key=lambda w: w["x"])
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
            continue

        # Structural fallback for a missing section-header row: some PDFs'
        # born-digital text layer omits "Billed to"/"Shipped to" entirely
        # (rendered as an image/graphic, not real text), so the marker
        # check above never fires — but the Buyer/Consignee block's first
        # content row still follows right after the Seller block. Detect it
        # structurally instead: its right-side column (a mirrored Ship-to
        # copy) duplicates this row's left-side text — a shape no genuine
        # Seller-block metadata row has (those pair a left LABEL with a
        # right VALUE, never identical text on both sides).
        if section == "Seller" and named["Seller"]:
            right = sorted([w for w in row if w["x"] >= divider], key=lambda w: w["x"])
            right_text = " ".join(w["text"].strip() for w in right).strip()
            if right_text and re.sub(r"\s+", "", low) == re.sub(r"\s+", "", right_text.lower()):
                section = "Buyer"

        # GSTIN on this line -> party gstin
        g = GSTIN_RE.search(text.replace(" ", ""))
        if "gstin" in low or "uin" in low or g:
            val = ""
            if g:
                val = g.group(1)
            else:
                # OCR sometimes splits the GSTIN (state code separated from
                # the 13-char core). Reconstruct: <state code> + <core>.
                alnum = re.sub(r"[^A-Z0-9]", "", text.upper())
                alnum = alnum.replace("GSTIN", "").replace("UIN", "")
                core = re.search(r"[A-Z]{5}\d{4}[A-Z][A-Z0-9]Z[A-Z0-9]", alnum)
                if core:
                    rest = alnum.replace(core.group(0), "")
                    st = re.search(r"\d{2}", rest)
                    val = (st.group(0) if st else "") + core.group(0)
            if val:
                fields.setdefault(f"{section} GSTIN/UIN", val)
            continue

        # State. Matches "state name"/"state code" (Tally-style), a bare
        # "State" label (word-bounded — \b so "Estate" in an address line
        # is never mistaken for it), or "place of supply".
        if (
            "state name" in low or "state code" in low
            or "place of supply" in low
            or re.search(r"\bstate\b", low)
        ):
            fields.setdefault(
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
            fields.setdefault(f"{section} Name", text)
            named[section] = True
        else:
            key = f"{section} Address"
            fields[key] = (fields.get(key, "") + "\n" + text).strip() if fields.get(key) else text

    # (Seller GSTIN fallback is applied in ocr_engine after all pages/bands
    # are merged, where the full invoice text is available.)

    # ------------------------------------------------------------------
    # Amount in words (footer)
    # ------------------------------------------------------------------
    for row in footer_rows:
        text = _row_text(row)
        low = text.lower()
        if low.startswith(("rupees", "inr", "indian rupees")) and "only" in low:
            fields.setdefault("Amount Chargeable (in words)", text)
            break

    # "Place of Supply" implies the buyer's state when not stated separately.
    if not fields.get("Buyer State Name") and fields.get("Place of Supply"):
        fields["Buyer State Name"] = fields["Place of Supply"]

    return fields
