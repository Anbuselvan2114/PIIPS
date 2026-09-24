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

import difflib
import re


# Fields whose value sits to the right of (or below) a label anchor.
RIGHT_FIELDS = {
    "Invoice No.": [
        "invoice no", "invoice number", "invoice #", "inv no", "bill no",
        "invioce no",  # genuine vendor-template typo seen on a real invoice, not an OCR artifact
        "invoice :",  # some vendors label it bare "Invoice :" with no "No."/"No" at all
        "invoice#",  # a vendor template with no space before the "#" - "invoice #" above doesn't substring-match this
        "invno",  # a vendor template glues "Inv" and "No." together with no space - "inv no" above doesn't substring-match this
        "billing doc. no", "billing doc no",  # HPE's own template labels it "Billing Doc. No." instead of any "Invoice ..." phrase - the fuller phrase (not just "billing doc") is needed so the anchor consumes the WHOLE label including its own trailing "No.", not just "Doc." with "No." left dangling as if it were the value's first word
    ],
    "Dated": ["dated", "invoice date", "date"],
    "Buyer's Order No.": [
        "p.o. no", "p.o no", "po no", "buyer's order no",
        "buyer's order", "order no", "purchase order",
        "p.o.#",  # same no-space-before-"#" vendor template as "invoice#" above
    ],
    "Place of Supply": ["place of supply"],
    "Reference No. & Date.": ["reference no", "other references"],
    "Mode/Terms of Payment": ["mode/terms of payment", "terms of payment"],
    # Generic (non-Tally) billing-statement layouts label the parties
    # directly instead of using a "Buyer (Bill To)" section header — these
    # phrases are specific enough not to collide with that section marker.
    "Buyer Name": ["customer name"],
    "Seller Name": ["vendor name"],
    # SERVICE invoices never go through Service First, so there is no
    # automatic source for the NAV vendor code at all - it is instead
    # hand-written onto the scanned PDF itself (the operational parallel
    # to a hand-written SPRPUR buyer's order no. on a PART invoice, see
    # buyer_order.py), so it's read here as an ordinary labelled field.
    "Vendor Code": ["nav vendor code", "vendor code", "vendor no"],
}

# Left-column section markers -> which party the following lines describe.
SECTION_MARKERS = [
    ("Consignee", ["consignee", "ship to", "shipped to", "shipping addres"]),
    ("Buyer", ["party details", "customer detail", "details of receiver",
               "bill to", "billed to", "buyer (bill to)", "buyer",
               "billing address"]),
]

# Stricter subset of SECTION_MARKERS, used ONLY for the right-half-of-a-
# shared-row check further down (e.g. "BUYER DETAILS" landing a few
# columns right of the seller's own trailing GSTIN on one row). Bare
# "buyer" is deliberately dropped here - on the LEFT it's safely scoped to
# the party-block column, but checked against arbitrary RIGHT-side text it
# also matches inside "Buyer's Order No.", an entirely ordinary Tally
# invoice-metadata label that legitimately sits to the right of many
# ordinary Seller-block rows, and would false-trigger the switch on them.
_RIGHT_SIDE_SECTION_MARKERS = [
    ("Consignee", ["consignee", "ship to", "shipped to", "shipping addres"]),
    ("Buyer", ["party details", "customer detail", "details of receiver",
               "buyer details", "bill to", "billed to", "buyer (bill to)"]),
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
    "nav vendor code", "vendor code", "vendor no",
    "reference no", "other references", "mode/terms", "terms of payment",
    "gstin", "uin", "state name", "state code", "hsn", "sac", "description",
    "qty", "quantity", "rate", "amount", "unit", "price", "code", "tax invoice",
    # A GST-metadata caption ("Document Type Code:INV") that can land left
    # of an unusually wide page's own divider, gluing onto an unrelated
    # 2-line letterhead's own 2nd line the same way "TAX INVOICE" glues
    # onto its 1st (see the Seller Name continuation-merge in _party_pass) -
    # checked ahead of the bare "code" entry above so it wins the earliest-
    # position tie-break and the whole caption is stripped, not just "code".
    "document type",
    # PAN (Permanent Account Number) is standard GST-invoice metadata,
    # printed on many vendor letterheads right alongside GSTIN - never
    # address content, but with no label recognition here it silently
    # became a trailing Address line (e.g. "...Nehru\nTPMGuru\nPAN No.
    # AAHCT7371D" for TPM Guru's own letterhead).
    "pan no", "pan:",
    # Other generic GST document-type titles besides "Tax Invoice" (e.g. a
    # combined invoice/dispatch document) - a document-type label, not a
    # company name, wherever it prints as the page's own top line.
    "commercial invoice", "delivery challan",
    # A bare "INVOICE" document-title word with no "Tax"/"Commercial"
    # prefix at all (e.g. a simple non-GST vendor template's own top-right
    # "INVOICE" banner, sharing a row with the seller's letterhead name on
    # the left) - same category as "tax invoice" above, just the shorter
    # form some templates use.
    "invoice",
    # GST goods invoices print 3 copies captioned "Original"/"Duplicate"/
    # "Triplicate" (for Recipient/Transporter/Supplier respectively) - all
    # 3 are document-copy markers, never company name/address content,
    # wherever they print as the page's own top line. "duplicate" and
    # "triplicate" alone were already here; "original" (not just the
    # "original copy" phrase) was the missing third.
    #
    # These 3 are usually printed with their own "for <role>" qualifier
    # right after ("ORIGINAL FOR RECIPIENT", "DUPLICATE FOR TRANSPORTER",
    # "TRIPLICATE FOR SUPPLIER") - the combined phrases are listed FIRST so
    # _label_span's earliest-position tie-break (first match wins a tied
    # start) strips the whole caption in one pass; without them, only the
    # bare "original"/"duplicate"/"triplicate" word gets stripped and
    # leaves the "for <role>" half behind, e.g. "FOR RECIPIENT" wrongly
    # read as a standalone company name when it happens to fall on the
    # page's own top line with no genuine letterhead text otherwise
    # recognized there.
    "original for recipient", "duplicate for transporter",
    "triplicate for supplier",
    "original", "original copy", "duplicate", "triplicate",
    "contect person",
    "contact person", "shipping addres", "shipping address", "billing address",
    # "Shipped From:" / "Ship From:" captions the seller's own block (the
    # company name follows on the next line) - a label, not a name.
    "shipped from", "ship from",
    # A generic small-business invoice template ("Customer Name:"/"Street
    # Address:"/"City/Prov/Postal:", one field per row) labels its address
    # lines this way rather than a bare "Address:" - matched here, ahead of
    # the shorter bare "address" below, so _label_span's earliest-position
    # rule picks the FULL "street address"/"city/prov/postal" phrase
    # instead of just the trailing "address" fragment (which would
    # otherwise treat "Street" as real content preceding a noise suffix,
    # the opposite of what's actually happening here - "Street Address:"
    # IS the label, "Street" is not separate content).
    "street address", "city/prov/postal",
    # Same template's "Email Address:" row ties with the bare "email"
    # phrase below at the same starting position (both start at char 0) -
    # _label_span's earliest-position rule only breaks ties by whichever
    # phrase it reaches FIRST in this list, so "email address" must be
    # listed ahead of plain "email"/"e-mail" to win that tie and strip the
    # whole label instead of leaving a dangling "Address: NA".
    "email address",
    "address",
    # A "Seller Details" section caption (a "Buyer Details"/"Bill To"
    # counterpart with no dedicated marker of its own, since Seller is
    # already the default starting section - nothing switches INTO it,
    # but it still needs to be recognized as a label, not content, when
    # a vendor's own template prints one) - without this, "SELLER
    # DETAILS" itself becomes the Seller Name.
    "seller details",
    # Amazon-style marketplace invoices caption the seller block "Sold
    # By :" instead of a name/address label at all - same category as
    # "Seller Details" above (recognized as a label so it isn't taken as
    # the Seller Name itself), just different vendor-template wording.
    "sold by",
    # A generic "Details Of Receiver :" template labels its own Buyer Name
    # line "Client name :" rather than any of the Tally-style phrasings
    # above - without this it glues onto the front of the real name
    # ("Client name : Precision Techserve Pvt. Ltd" instead of just the
    # name).
    "client name",
    "party details", "consignee", "bill to", "ship to", "s.n.", "sl.no",
    "e-mail", "email", "tel", "phone", "msme", "bank", "ifsc", "terms",
    "declaration", "authorised", "authorized", "signatory", "grand total",
    "tax rate", "taxable", "total tax", "due date", "name of product",
    # A grid-style header (e.g. R-Logic's "Invoice Number | Invoice Date |
    # Order Number | Customer Ref No", values one row below, column-
    # aligned) has every column's OWN label sharing that same row with
    # "Invoice Date"'s. The "value to the right on this row" scan (see
    # _value_right_or_below) stops at the next recognized label - without
    # these, "Order Number"/"Customer Ref No" aren't recognized as labels
    # at all, so they get collected as if they were Invoice Date's own
    # VALUE text ("Order Number Customer Ref No"), and the real value
    # sitting one row down is never reached.
    "order number", "customer ref",
    # Bare "service" alone is too broad - it's a substring of "Services",
    # extremely common in real company names ("R-Logic Technology
    # SERVICES India Pvt. Ltd."), and was truncating those names at the
    # false-positive match. Scoped to the actual table-header phrasing
    # this was for instead ("Item-Service Code", "Name of Product /
    # Service" table captions - "name of product" alone, right above,
    # already covers one variant of that same header).
    "item-service", "product / service", "particulars",
    # dispatch / delivery labels — these are field captions, never values,
    # so a blank "Buyer's Order No." must not swallow the next label below it.
    "despatch", "dispatch", "despatched", "dispatched", "document no",
    "delivery note", "supplier's ref", "supplier ref", "e-way", "eway",
    "other reference", "reference no",
    # GST e-Invoice QR-code block (IRN/Ack No./Ack Date caption) sits above
    # the seller's letterhead on this layout — without these, "IRN : <64-
    # char hash>" becomes the very first unclaimed left-column line and
    # gets captured as the Seller Name/Address instead. "e-invoice" itself
    # is deliberately NOT here (only in the explicit low.startswith(...)
    # checks below, which already cover its real job of skipping a row
    # that STARTS with it) - as a generic substring match here, it also
    # matched a seller's own letterhead row that merely shares its line
    # with an unrelated "e-Invoice" badge elsewhere on the same row (e.g.
    # "Zaco Computers Pvt Ltd" ... "e-Invoice"), wrongly disqualifying the
    # whole row as a title banner and losing the real company name.
    "irn", "ack no", "ack date",
]

# The 14th character of a GSTIN is always the literal "Z" (a fixed part of
# the numbering scheme, not vendor-specific data) - "2" is accepted there
# too since poor handwriting/scan quality OCRs it that way often enough in
# practice (the same Z<->2 confusion vendor_code.py's own confusion table
# already tracks for a different field); _gstin_from_text normalizes it
# back to "Z" in its result either way.
GSTIN_RE = re.compile(r"\b(\d{2}[A-Z]{5}\d{4}[A-Z][A-Z0-9][Z2][A-Z0-9])\b")
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
        val = g.group(1)
        return val[:13] + "Z" + val[14:]  # the 14th char is always literal "Z" - see GSTIN_RE's own note
    alnum = re.sub(r"[^A-Z0-9]", "", text.upper()).replace("GSTIN", "").replace("UIN", "")
    core = re.search(r"[A-Z]{5}\d{4}[A-Z][A-Z0-9][Z2][A-Z0-9]", alnum)
    if core:
        val = core.group(0)
        val = val[:11] + "Z" + val[12:]  # same fixed "Z" position within the 13-char core
        rest = alnum.replace(core.group(0), "")
        st = re.search(r"\d{2}", rest)
        return (st.group(0) if st else "") + val
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


def _is_garbled_tax_invoice_title(low):
    """A poor scan's OCR-garbled "Tax Invoice" title banner ("Tax
    Involco", "Tax Involce") - none of LABEL_WORDS' literal "tax invoice"
    substring-matches it, so the banner was taken as the seller's own name
    (pushing the real name down into the address). Only a short line that
    STARTS with "tax" and is still very close to the real phrase counts, so
    an ordinary company name is never mistaken for one."""
    low = low.strip()
    return (
        len(low) <= 14
        and low.startswith("tax")
        and difflib.SequenceMatcher(None, low, "tax invoice").ratio() >= 0.8
    )


_TITLE_WORDS = {
    "tax", "invoice", "bill", "of", "supply", "cash", "memo", "challan",
    "delivery", "cum", "original", "duplicate", "triplicate", "for",
    "recipient", "transporter", "supplier", "copy", "proforma", "credit",
    "debit", "note", "e", "and", "the", "retail", "sales", "service",
}
_TITLE_ANCHOR_WORDS = {"invoice", "challan", "bill", "memo", "proforma", "note"}


def _is_document_title(text):
    """A line made up ONLY of document-title vocabulary ("Invoice Cum
    Delivery Challan", "Tax Invoice / Bill of Supply", "Cash Memo") - a
    title banner, never a company name. Needs at least one strong title
    word so a name that merely contains "Service" or "Sales" isn't caught."""
    words = re.findall(r"[a-z]+", (text or "").lower())
    return (bool(words)
            and all(w in _TITLE_WORDS for w in words)
            and any(w in _TITLE_ANCHOR_WORDS for w in words))


def _restore_garbled_invoice_label(low):
    """A poor scan reads the "Invoice" of "Invoice No." as some other
    seven-letter word ("Involce No.", "InvoIce No.") so the literal "invoice
    no" phrase never appears. Any 7-letter word right before "no" that is
    still very close to "invoice" is put back - same length, so every
    character position later matched against the ORIGINAL row text stays
    valid."""
    def fix(m):
        w = m.group(1)
        if w != "invoice" and difflib.SequenceMatcher(None, w, "invoice").ratio() >= 0.75:
            return "invoice" + m.group(2)
        return m.group(0)
    return re.sub(r"\b([a-z]{7})(\s+no\b)", fix, low)


def _is_label(text):
    low = text.lower()
    return (any(lbl in low for lbl in LABEL_WORDS)
            or _is_garbled_tax_invoice_title(low)
            or _is_document_title(low))


def _label_span(text):
    """(start, end) of the earliest LABEL_WORDS phrase in `text`
    (case-insensitive), or None if none is present."""
    low = text.lower()
    # "PIN Code 382424" is part of the ADDRESS, not the bare "code" label
    # (LABEL_WORDS' own "code" entry) - masked to the same length first so
    # the positions stay valid, otherwise the row is cut right after "PIN"
    # and the pincode itself is lost.
    low = re.sub(r"(\bpin\s*)code\b", lambda m: m.group(1) + "xxxx", low)
    best = None
    for lbl in LABEL_WORDS:
        i = low.find(lbl)
        # A label found in the MIDDLE of a word ("rate" inside "Corporate",
        # cutting "Swastik Disa Corporate Park" down to "Corpo") is just
        # part of that word - keep searching for one that starts a word.
        while i > 0 and low[i - 1].isalpha() and lbl[0].isalpha():
            i = low.find(lbl, i + 1)
        if i != -1 and (best is None or i < best[0]):
            best = (i, i + len(lbl))
    return best


_ENDS_WITH_CITY_PINCODE_RE = re.compile(r"[A-Za-z]{3,}\s+\d{6}$")


def _looks_like_phone_line(text):
    """A line that's a bare phone/mobile number with no label word at all
    (e.g. "M +91 9900716651 ; 044-45015154") — mostly digits/phone
    punctuation rather than real name/address text. A genuine address line
    with a pincode has far more letters than digits, so requiring digits to
    both meet a phone-length minimum AND outnumber letters keeps this from
    matching normal address text - except a SHORT address line that's
    little more than a building number plus "<City> <6-digit-PIN>" (e.g.
    "93 Noida 201301") can still tip digits > letters despite genuinely
    being an address, not a phone number - a bare phone number is never
    shaped like "<word> <exactly 6 digits>" at its own end, so that shape
    is excluded regardless of the digit/letter ratio."""
    if _ENDS_WITH_CITY_PINCODE_RE.search(text.strip()):
        return False
    # "Mumbai-400086. Ph.: 25009000" - a city + exactly-6-digit PIN with the
    # letterhead's phone number trailing on the same line is still an
    # address line (the phone gets trimmed off later as a label).
    if re.search(r"[A-Za-z]{3,}[\s\-,.]*(?<!\d)\d{6}(?!\d)", text):
        return False
    # A door/plot/survey number ("HO: 17/2/1/286", "1417/38-N") is a string
    # of short digit groups split by "/" or "-" - a real phone number
    # always has at least one substantial unbroken run (even a landline's
    # "044-45015154" has 8, a spaced mobile's groups are 5 each), so a
    # line whose longest digit run is only 1-3 digits can't be one.
    runs = re.findall(r"\d+", text)
    if not runs or max(len(r) for r in runs) < 4:
        return False
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


_BARE_DATE_RE = re.compile(
    r"^\s*(\d{1,2})\s*[-/. ]\s*(\d{1,2}|[A-Za-z]{3,9})\s*[-/. ]\s*(\d{4}|\d{2})\s*$"
)
_MONTH_ABBRS = {"jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"}


def _is_bare_date(text):
    """A value that is nothing but a REAL date ("0-Jul-20", "13/08/2026").
    Day/month ranges are checked so an invoice number that merely has date
    shape ("26/27/195", "69/26-27") is never mistaken for one."""
    m = _BARE_DATE_RE.match(text or "")
    if not m:
        return False
    day, mon = m.group(1), m.group(2)
    if mon.isdigit():
        return 1 <= int(day) <= 31 and 1 <= int(mon) <= 12
    return int(day) <= 31 and mon[:3].lower() in _MONTH_ABBRS


def _value_right_or_below(rows, ri, anchor_word, offset, reject=None):
    """
    Given the row index and the anchor word that matched, return the value:
    tokens to the right on the same row (minus another label), else tokens in
    the same column band on the next few rows. `reject`, when given, is a
    predicate a same-row candidate must NOT satisfy to be accepted - it then
    falls through to the value below instead (an Invoice No. label whose
    right-hand neighbour is really the DATE column's own value).
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
    if right_text and not (reject and reject(right_text)):
        return right_text

    # --- value below, in the same (bounded) column band ---
    # A narrower value (e.g. a date) sitting under a wider label can start
    # a bit further left than the label's own x (R-Logic's "01-September-
    # 2026" sits ~44px left of its own "Invoice Date" label) - widened
    # from -40 to -60 to comfortably cover that drift while still safely
    # short of the previous grid column, which real invoices space far
    # wider apart than that (R-Logic's own neighboring columns sit ~300px+
    # apart).
    band_lo, band_hi = ax - 60, ax + 220
    for nri in range(ri + 1, min(ri + 4, len(rows))):
        nrow = sorted(rows[nri], key=lambda w: w["x"])
        cell = [w for w in nrow if band_lo <= w["x"] <= band_hi]
        text = _clean_value(" ".join(w["text"] for w in cell))
        if not text:
            # Label column and value column printed a full column apart on
            # staggered rows ("Invoice No. :" one row, its value
            # "2627PSI26040069" on the NEXT row, out in the value column
            # 350px to the right, with nothing under the label itself):
            # a single non-label word just past the band on the very next
            # row is that value.
            if nri == ri + 1:
                far = [w for w in nrow if band_hi < w["x"] <= ax + 700
                       and w["text"].strip()]
                if len(far) == 1 and not _is_label(far[0]["text"]):
                    far_text = _clean_value(far[0]["text"])
                    if far_text and not (reject and reject(far_text)):
                        return far_text
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
        # Some vendors caption this exact same Buyer/Consignee-block-plus-
        # metadata-column shape with a different phrase pair than the
        # common Tally "Bill To"/"Ship To" - e.g. R-Logic's SAP Business
        # One template prints "Billing Address : .../Delivery Address:
        # ..." side by side, with a third, unrelated Customer Code/Contact
        # Details column further right. Same physical layout, same fix -
        # try each known pair in turn; first one where BOTH phrases are
        # found on this row wins.
        bill_idx = ship_idx = -1
        for bill_phrase, ship_phrase in (
            ("bill to", "ship to"),
            ("billing address", "delivery address"),
        ):
            bill_idx = low.find(bill_phrase)
            ship_idx = low.find(ship_phrase)
            if bill_idx != -1 and ship_idx != -1:
                break
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
            # starts strictly after the ship-marker phrase ends (whichever
            # of the phrase-pair options above actually matched - NOT
            # hardcoded to "ship to"'s own length, which would
            # miscalculate the boundary for "delivery address" and any
            # future pair with a different length) - the true start of
            # column 3 either way, captioned or not.
            ship_end = ship_idx + len(ship_phrase)
            details_x = next(
                (w["x"] for start, end, w in spans if start >= ship_end and w["x"] > ship_x),
                None,
            )
        if details_x is None:
            # Still nothing on the marker row itself - some layouts (e.g.
            # R-Logic's) don't reveal column 3 until a few rows further
            # down (its own metadata block starts blank on the marker row,
            # then "Customer Name :"/"Contact Details :" etc. appear later
            # at a consistent x far past the ship column's own natural
            # width). Look ahead a bounded number of rows for the
            # left-most word sitting meaningfully right of ship_x - a
            # margin (not just "> ship_x") so a ship-column value merely
            # drifting a little right on its own wrapped line is never
            # mistaken for a whole new column.
            margin = max(200, (ship_x - bill_x) / 2)
            candidates = [
                w["x"] for later in rows[ri:ri + 10] for w in later
                if w["x"] > ship_x + margin
            ]
            if candidates:
                details_x = min(candidates)
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
        # Same problem, but for the bill/ship marker's OWN value, when it's
        # glued onto the very same OCR word as the marker phrase itself
        # rather than sitting in a later, separate word/row - e.g.
        # "Billing Address : PRECISION TECHSERVE PRIVATE" is one merged
        # token, so bill_x's own word never gets picked up by the
        # position-only scan below (which only ever looks at LATER rows).
        # Without this, only a wrapped continuation line (if any) becomes
        # the party's Name, silently dropping the marker row's own real
        # value entirely.
        for idx, phrase, dest in ((bill_idx, bill_phrase, bill_rows),
                                   (ship_idx, ship_phrase, ship_rows)):
            found = next(((s, w) for s, e, w in spans if s <= idx < e), None)
            if found is None:
                continue
            word_start, src_w = found
            local_end = idx - word_start + len(phrase)
            val = src_w["text"][local_end:].strip(" :,-.")
            if val:
                dest.append([{"text": val, "x": src_w["x"], "y": src_w.get("y", 0)}])
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


def _find_marker(rows, start_ri, phrase):
    """First row at or after `start_ri` whose text contains `phrase`,
    returning (row_index, x_of_the_matched_word) - or None. Used by
    _split_stacked_address_columns below, where (unlike
    _split_three_column_header's side-by-side pair) the two markers never
    share a row, so each is located independently."""
    for ri in range(start_ri, len(rows)):
        text, spans = _row_text_with_spans(rows[ri])
        idx = text.lower().find(phrase)
        if idx == -1:
            continue
        found = next(((s, w) for s, e, w in spans if s <= idx < e), None)
        if found is None:
            continue
        return ri, found[1]["x"]
    return None


def _split_stacked_address_columns(rows):
    """Detect a marketplace-style layout where the Buyer ("Billing
    Address") and Consignee ("Shipping Address") blocks print STACKED one
    after another in a single right-hand column, running in PARALLEL
    alongside an unrelated Seller block ("Sold By :") that spans the same
    row range on the left - e.g. Amazon invoices. Unlike
    _split_three_column_header's side-by-side columns (both markers share
    one row), these two markers are on different rows entirely, so the
    normal party-block scan (one `section` value shared by the whole row)
    can never see "Billing Address" and "Shipping Address" as anything but
    ordinary Seller-block content miles down the page.

    Returns (bill_ri, bill_x, ship_ri, ship_x) - the row index and x
    position of each marker phrase - or None if this shape isn't present.
    """
    bill = _find_marker(rows, 0, "billing address")
    if bill is None:
        return None
    ship = _find_marker(rows, bill[0] + 1, "shipping address")
    if ship is None:
        return None
    return bill[0], bill[1], ship[0], ship[1]


def extract(header_rows, footer_rows, page_width):
    fields = {}
    three_col = _split_three_column_header(header_rows)
    if three_col is not None:
        marker_ri, bill_rows, ship_rows, detail_rows, bill_x, ship_x = three_col
        # The marker row itself can carry real content of its own to the
        # LEFT of the Bill To column - e.g. "Invoice Date : 13/08/2026
        # Bill to: Ship To :", the date squeezed onto the same row as the
        # 3-column markers rather than its own line above them. None of
        # bill_rows/ship_rows/detail_rows cover it (all 3 are narrowed to
        # their own column's x-range, starting at bill_x or later), and
        # slicing header_rows[:marker_ri] excludes the marker row entirely
        # - so without this, that leading text is silently dropped. Kept
        # as its own row, positioned right where the marker row itself
        # was, so the ordinary right-field anchor scan below still finds it.
        marker_leading = [w for w in header_rows[marker_ri] if w["x"] < bill_x]
        # Invoice Details' own words, isolated from Bill To/Ship To, feed
        # the ordinary right-field anchor scan below exactly like a normal
        # single-column layout would - including _merge_wrapped_labels
        # picking up an "Invoice"/"No. :" 2-line wrap that's now a clean,
        # uninterleaved sequence.
        rows = _merge_wrapped_labels(
            header_rows[:marker_ri]
            + ([marker_leading] if marker_leading else [])
            + _merge_wrapped_values(detail_rows)
        )
        stacked = None
    else:
        rows = _merge_wrapped_labels(header_rows)
        # Only tried when the side-by-side 3-column shape above wasn't
        # found - see _split_stacked_address_columns's own docstring.
        stacked = _split_stacked_address_columns(rows)
    divider = page_width * 0.42
    # Rows whose LEFT-hand text (the half the party-block pass reads) was
    # already consumed by a labeled anchor there — e.g. "Customer Name: Acme
    # Ltd" shouldn't additionally become the Seller Name once "Customer Name"
    # claimed it. A row where the anchor is on the RIGHT (the common Tally
    # case: seller letterhead on the left, "Invoice No. / Dated" on the
    # right of the SAME row) must not be claimed — the two halves are
    # unrelated content that only happen to share a row.
    claimed_rows = set()

    # Every row where a RIGHT_FIELDS anchor matched anywhere (left or right
    # of the divider) - used below to tell a genuine right-column metadata
    # row (already claimed as some field's value) apart from a row that's
    # merely unlucky enough to sit past the divider for structural reasons
    # (see the right/center-aligned-letterhead fallback in _party_pass).
    right_field_rows = set()

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
        low = _restore_garbled_invoice_label(row_text.lower())
        # Skip the GST e-Invoice QR-code block (IRN/Ack No./Ack Date) here
        # too, same as the party-block pass below — "Ack Date : Aug 6,
        # 2026, 6:01:00 PM" contains "date" (one of "Dated"'s own RIGHT_
        # FIELDS phrases) and, being near the top of the page, would
        # otherwise claim "Dated" before the invoice's real Invoice No./
        # Dated row is ever reached.
        if low.startswith(("irn", "ack no", "ack date", "e-invoice")):
            continue
        # A vendor template that labels the invoice number with a bare "#"
        # (e.g. "# : 25077") rather than any recognized phrase - unlike
        # "P.O.#"/"Invoice#" etc. (already matched via their own longer,
        # unambiguous RIGHT_FIELDS phrases), a standalone "#" is only safe
        # to treat as this label when it's the row's OWN first character -
        # a blanket substring match would also fire on "P.O.#" (which
        # contains "#" too, several characters in) and steal its value
        # instead of Buyer's Order No.'s.
        # ...as its own label ("#", "# :", "#: 25077") - never a street/
        # unit number that merely starts with "#" ("#7087th FLOOR CTC PARK
        # LANE"), which used to be taken for the label and steal the NEXT
        # line ("SECUNDERABAD-500003") as the Invoice No.
        if ("Invoice No." not in fields
                and (srow[0]["text"].strip() == "#" or re.match(r"^#\s*:", low))):
            val = _value_right_or_below(rows, ri, srow[0], len(srow[0]["text"]))
            if val:
                fields["Invoice No."] = val
                right_field_rows.add(ri)
                if srow[0]["x"] < divider:
                    claimed_rows.add(ri)
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
                    val = _value_right_or_below(
                        rows, ri, anchor, offset,
                        reject=_is_bare_date if field == "Invoice No." else None,
                    )
                    if val and not (field == "Invoice No." and _is_bare_date(val)):
                        fields[field] = val
                        right_field_rows.add(ri)
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

    # "DT" short-form Date label, glued directly to its value with no space
    # (e.g. an informal freelancer invoice's own "DT14July.26") - not
    # joined into RIGHT_FIELDS' "Dated" phrase list above because that's a
    # plain substring search, and bare "dt" is too easy to false-match
    # inside ordinary words ("width", "breadth"); matched here instead
    # with a proper word-boundary regex, scoped to every row (not just
    # meta_row, since this label's own row may not be the one carrying
    # "Invoice No.").
    if "Dated" not in fields:
        m = re.search(
            r"\bDT\s*[:\-]?\s*(\d{1,2}\s*[A-Za-z]{3,9}\.?\s*\d{2,4})\b",
            "\n".join(_row_text(row) for row in rows),
            re.IGNORECASE,
        )
        if m:
            fields["Dated"] = m.group(1).strip()

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
        # Prefer the LEFT-of-divider slice of row 0, not the whole
        # unsliced row: a company name that's genuinely left-aligned (the
        # common case) can share its row with an unrelated right-side
        # document-title word ("BABA INFOTECH SOLUTION" ... "INVOICE") that
        # isn't a recognized label phrase on its own and so wouldn't be
        # caught by the _is_label check below - using the full row text
        # would glue that title word onto the real company name. Only
        # falls back to the full row when the left side is empty, so a
        # genuinely right/center-aligned letterhead (e.g. Printer World,
        # where the company name itself sits well past the divider) is
        # still found.
        left0 = [w for w in rows[0] if w["x"] < divider]
        first_text = _row_text(left0) if left0 else _row_text(rows[0])
        if (first_text and not _is_label(first_text)
                # The slice left of the divider can be just the tail of a
                # longer title ("Invoice" | "Cum Delivery Challan") - judge
                # the whole row too.
                and not _is_document_title(_row_text(rows[0]))
                and not _looks_like_phone_line(first_text)
                and not _looks_like_url_line(first_text)
                and len(first_text.split()) <= 4
                and not any(c.isdigit() for c in first_text)
                # A genuine company name always has at least one letter -
                # without this, a stray punctuation-only artifact sitting
                # above the real letterhead (e.g. a lone "." rendered as
                # the page's very first line) passes every other check
                # here and wins the Seller Name outright.
                and any(c.isalpha() for c in first_text)):
            # Some 2-line logos stack the company name across rows 0 and 1
            # (e.g. "Hewlett Packard" / "Enterprise" - the actual two-line
            # HPE wordmark) - same left-aligned/centered spot, same short
            # plain-text shape as row 0's own check, so tried again against
            # row 1 immediately below it. Row 1 stays party-block content
            # otherwise (a genuine next line of it might be the real
            # street address instead), so this only actually fires when
            # BOTH checks pass. Claiming it here (not just appending the
            # text) keeps the party-block pass below from ALSO treating it
            # as the first line of Seller Address once "Name" is set.
            if len(rows) > 1 and 1 not in claimed_rows:
                left1 = [w for w in rows[1] if w["x"] < divider]
                second_text = _row_text(left1) if left1 else _row_text(rows[1])
                if (second_text and not _is_label(second_text)
                        and not _looks_like_phone_line(second_text)
                        and not _looks_like_url_line(second_text)
                        and len(second_text.split()) <= 2
                        and not any(c.isdigit() for c in second_text)
                        and any(c.isalpha() for c in second_text)):
                    first_text = f"{first_text} {second_text}"
                    claimed_rows.add(1)
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
        # A section marker seen on the RIGHT half of some row (see below) -
        # applied at the START of the NEXT row's processing, not this row's
        # own, since this row's LEFT half may still genuinely belong to the
        # OLD section (e.g. the seller's own trailing GSTIN sharing a row
        # with a "BUYER DETAILS" caption a few columns further right).
        pending_switch = None

        scan_rows = rows_override if rows_override is not None else rows
        # A row consumed as the PREVIOUS row's own Name continuation (see
        # the 2-line-logo merge below) - unlike claimed_rows (which indexes
        # the real page's own `rows`/`header_rows` and so is only
        # meaningful when rows_override is None), this indexes whichever
        # sequence THIS call is actually scanning, so it applies the same
        # way regardless of rows_override.
        consumed_as_continuation = set()
        # x-position of each section's own company/person name row, used
        # below to recognize logo artwork printed far to the left of the
        # letterhead's real text column.
        name_x = {}
        for ri, row in enumerate(scan_rows):
                if ri in consumed_as_continuation:
                    continue
                if rows_override is None and ri in claimed_rows:
                    continue

                if pending_switch:
                    section = pending_switch
                    any_switch = True
                    pending_switch = None

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
                if ri == 0 and section == "Seller":
                    whole = _row_text(row)
                    if not named["Seller"]:
                        # A row that is nothing but a document title
                        # ("Invoice Cum Delivery Challan (ORIGINAL FOR
                        # RECIPIENT)") - skipped outright; stripping just
                        # the one label phrase it happens to contain would
                        # leave the rest ("Cum Delivery Challan") behind to
                        # be taken as the Seller Name.
                        if whole and _is_document_title(re.sub(r"\([^)]*\)", "", whole)):
                            continue
                        if whole and _is_label(whole):
                            # The label can sit ENTIRELY in the right half
                            # of this same row (already excluded from
                            # `text` by the ordinary left-of-divider slice
                            # above), cleanly separate from a genuine
                            # letterhead on the left - e.g. "R-Logic
                            # Technology Services India Pvt. Ltd." (left)
                            # sharing its row with "TAX INVOICE" (right,
                            # well past the divider). Discarding the WHOLE
                            # row in that case loses the real Seller Name
                            # entirely, leaving the block shifted down by
                            # one line (the first genuine ADDRESS line gets
                            # mistaken for the Name instead). Only actually
                            # discard when stripping the matched label
                            # leaves nothing substantial behind either side
                            # - the genuinely-straddling-title case this
                            # check exists for (see its own docstring
                            # above) - not just "a label phrase appears
                            # somewhere in this row".
                            span = _label_span(whole)
                            remainder = (
                                whole[:span[0]] + " " + whole[span[1]:]
                            ).strip(" :,-.") if span else ""
                            if len(re.sub(r"[^A-Za-z]", "", remainder)) < 10:
                                continue
                    elif whole and out.get("Seller Name") == whole:
                        # The "Seller Name fallback" above already consumed
                        # this exact row (its own unsliced text became
                        # Seller Name before this pass even started) -
                        # falling through below would treat it as
                        # unclaimed content and duplicate the name as the
                        # first Address line too. A Seller Name already
                        # set for some OTHER reason (e.g. a "Vendor Name:"
                        # anchor claimed elsewhere) means row 0 is still
                        # genuine, unclaimed content - let it fall through
                        # normally in that case.
                        continue

                # A rows_override row is already narrowed to one column's
                # words (or is a single synthetic marker word) - use it as
                # given rather than re-slicing it by the page-wide divider,
                # which has no meaning for a synthesized column split.
                left = (
                    sorted(row, key=lambda w: w["x"]) if rows_override is not None
                    else sorted([w for w in row if w["x"] < divider], key=lambda w: w["x"])
                )
                # A company logo's own stylised text (OCR'd as short garbled
                # fragments - "Sw", "Sureworhs", "info systen") often sits
                # far to the LEFT of the letterhead's real text column,
                # sharing rows with genuine address lines, and gets glued
                # onto the front of them ("Sw HO: 17/2/1/286",
                # "Sureworhs Bengaluru 560062"). Once this section's own
                # name row fixed where that column starts, a short word
                # sitting well left of it is artwork, not address text.
                # Skipped on a section-marker line, whose caption
                # ("Consignee (Ship to)") legitimately starts further left.
                if (rows_override is None and section == "Seller"
                        and section in name_x and left):
                    row_low = " ".join(w["text"] for w in left).lower()
                    if not any(m in row_low for _n, marks in SECTION_MARKERS for m in marks):
                        # Only a leading run that is short, sits well left
                        # of the name column AND is set apart from the rest
                        # of the row by a wide gap - a centered letterhead's
                        # long line also starts left of a short name line,
                        # but its words run on contiguously.
                        k = 0
                        while (k < len(left)
                               and left[k]["x"] < name_x[section] - 150
                               and len(left[k]["text"].strip()) <= 12
                               and not _is_label(left[k]["text"])):
                            k += 1
                        if 0 < k < len(left):
                            last = left[k - 1]
                            gap = left[k]["x"] - last.get("right", last["x"] + 60)
                            if gap >= 40:
                                left = left[k:]
                # A lone single LETTER set well apart from the rest of its row
                # ("R      Flat No.1417/38-N ...") is a stray logo/bullet
                # artifact, never the start of a real address line - real
                # single-letter starts ("C-202", "A 12") are glued or close.
                if rows_override is None and len(left) >= 2:
                    w0, w1 = left[0], left[1]
                    t0 = w0["text"].strip()
                    if (len(t0) == 1 and t0.isalpha()
                            and w1["x"] - w0.get("right", w0["x"] + 20) >= 25):
                        left = left[1:]
                # A right-aligned letterhead whose logo artwork ("TPMGuru")
                # sits alone left of the divider on the same row as a real
                # address line ("Place,New Delhi-110019") that starts right
                # of it: the divider slice keeps only the logo word, losing
                # the address tail (and its PIN) entirely. Take the row's
                # right-hand text instead when the left part is just that
                # short, widely-separated artwork word.
                logo_rest_used = False
                if (rows_override is None and section == "Seller" and left
                        and not any_switch and ri != 0 and ri not in right_field_rows
                        and len(left) == 1 and len(left[0]["text"].strip()) <= 12
                        and not _is_label(left[0]["text"])):
                    rest = sorted([w for w in row if w["x"] >= divider], key=lambda w: w["x"])
                    rest_text = " ".join(w["text"].strip() for w in rest)
                    if (rest and re.search(r"[A-Za-z]{3,}", rest_text)
                            and not _is_label(rest_text)
                            and re.search(r"\b\d{3}\s?\d{3}\b", rest_text)
                            and rest[0]["x"] - left[0].get("right", left[0]["x"] + 60) >= 40):
                        left = rest
                        logo_rest_used = True
                if (not left and rows_override is None and section == "Seller"
                        and not any_switch and ri != 0 and ri not in right_field_rows):
                    # Row 0 is deliberately excluded - it already went through
                    # its own title-banner-vs-letterhead disambiguation above
                    # (and, if that decided it's not a title, the dedicated
                    # "Seller Name fallback" earlier in extract() already
                    # handles a right/center-aligned company-name-only first
                    # line). Falling through past that to land here as an
                    # ordinary content row would wrongly treat leftover title
                    # text (e.g. "Tax Invoice/Bill of Supply/Cash Memo" after
                    # "Tax Invoice" is stripped) as a genuine Seller Name.
                    #
                    # A right/center-aligned seller letterhead (name, address,
                    # GSTIN, email all centered around the page's midline
                    # rather than sitting at the true left margin) straddles
                    # the fixed left/right divider line-by-line - a longer
                    # line starts further left than a short one (e.g. the
                    # company name itself), so some lines land entirely past
                    # the divider even though they're genuine Seller content,
                    # not real right-column invoice metadata (which WOULD
                    # have matched a RIGHT_FIELDS phrase above and landed in
                    # right_field_rows). Only tried pre-switch, while still
                    # in the Seller block - once Buyer/Consignee starts, the
                    # right column reliably carries real per-row metadata
                    # (PO No., dates) that must stay excluded.
                    left = sorted(row, key=lambda w: w["x"])
                    used_letterhead_fallback = True
                else:
                    used_letterhead_fallback = False
                # The same stray-letter rule again for a row that only
                # reached `left` through the letterhead fallback just above
                # (TPM Guru's "R   Flat No.1417/38-N ..." sits entirely
                # right of the divider).
                if used_letterhead_fallback and len(left) >= 2:
                    w0, w1 = left[0], left[1]
                    t0 = w0["text"].strip()
                    if (len(t0) == 1 and t0.isalpha()
                            and w1["x"] - w0.get("right", w0["x"] + 20) >= 25):
                        left = left[1:]
                if not left:
                    continue
                text = " ".join(w["text"].strip() for w in left).strip()
                if not text:
                    continue
                low = text.lower()

                # section change? A caption like "Buyer (if other than
                # consignee)" matches BOTH markers - "buyer" at the start
                # (the actual subject) and "consignee" buried in the
                # parenthetical qualifier - so whichever phrase occurs
                # EARLIEST in the text wins, not whichever section happens
                # to be checked first in the list above. Without this, the
                # Consignee entry's blanket "consignee" substring always
                # won on a row like that regardless of what the row is
                # actually captioning, silently re-triggering Consignee
                # (a no-op, since it's already active) and discarding the
                # row's real content - including the Buyer's own Name,
                # often printed on that same caption line.
                switched = False
                best_pos, best_name, best_marker = None, None, None
                for name, marks in SECTION_MARKERS:
                    for m in marks:
                        pos = low.find(m)
                        if pos != -1 and (best_pos is None or pos < best_pos):
                            best_pos, best_name, best_marker = pos, name, m
                # A Seller-block row can carry the seller's own GSTIN on its
                # left and the NEXT block's caption further right on the same
                # line ("GSTIN No. 33AAGCS1406H1ZR   Ship to-"): the GSTIN
                # belongs to the block being left, not the one starting.
                if best_name is not None and best_pos and section == "Seller":
                    lead_gstin = _gstin_from_text(text[:best_pos])
                    if lead_gstin:
                        out.setdefault("Seller GSTIN/UIN", lead_gstin)
                        text = text[best_pos:]
                        low = text.lower()
                        best_pos = 0
                if best_name is not None:
                    section = best_name
                    switched = True
                if not switched:
                    stripped = low.strip(" :,-.")
                    for name, marks in EXACT_SECTION_MARKERS:
                        if stripped in marks:
                            section = name
                            switched = True
                            break
                if switched:
                    any_switch = True
                    # The winning marker phrase can share its row with real
                    # content for the section it just switched INTO (e.g.
                    # "Buyer (if other than consignee)\nPRECISION TECHSERVE
                    # PVT LTD" clustered as one row by the text layout) -
                    # strip the marker phrase itself, and an immediately-
                    # following parenthetical qualifier (never real content
                    # either, e.g. "(if other than consignee)"), and let
                    # whatever remains fall through to the normal per-row
                    # handling below under the NEW section, instead of
                    # unconditionally discarding the whole row the way a
                    # bare marker-only row correctly should be. Not
                    # applicable to an EXACT_SECTION_MARKERS match (bare
                    # "To,") - that only ever matches when the WHOLE row is
                    # the marker, so there is never a real remainder.
                    if best_marker is None:
                        continue
                    remainder = text[best_pos + len(best_marker):]
                    # The marker phrase can be a PREFIX of a longer word in
                    # the actual text (e.g. marker "customer detail"
                    # matching inside "Customer Details:") - strip any
                    # lowercase letters immediately continuing that same
                    # word (the "s" in "Details") first, so that leftover
                    # word-fragment doesn't itself become a spurious one-
                    # character "name" once real content is sought below.
                    word_tail = re.match(r"^[a-z]+", remainder)
                    if word_tail:
                        remainder = remainder[word_tail.end():]
                    remainder = remainder.strip(" :,-.")
                    paren = re.match(r"^\([^)]*\)\s*", remainder)
                    if paren:
                        remainder = remainder[paren.end():].strip(" :,-.")
                    if not remainder or not any(c.isalpha() for c in remainder):
                        # Nothing usable after the marker - some layouts
                        # print it the other way round instead, the row's
                        # real content FIRST and the caption trailing after
                        # it (e.g. "M/s.Precision Techserve Pvt Ltd., Buyer
                        # (Bill to)" - the same company's name introducing
                        # its own "Buyer" caption, not a blank marker-only
                        # row). Whatever sits before the marker match is
                        # this new section's own content in that case, not
                        # the old section's trailing data - the marker
                        # match itself already means "everything from here
                        # is the new section", so use it instead of
                        # discarding the row outright.
                        before = text[:best_pos].strip(" :,-.")
                        if before and any(c.isalpha() for c in before):
                            remainder = before
                        else:
                            continue
                    text = remainder
                    low = text.lower()

                # A section marker on the RIGHT half of this SAME row (past
                # the divider) - e.g. "BUYER DETAILS" landing a few columns
                # right of the seller's own trailing GSTIN on one shared
                # row. The left-only check above never sees it, and without
                # this every later row stays wrongly stuck in the OLD
                # section forever (the real buyer's own name/address/GSTIN
                # all get misfiled as more Seller content). Deferred to the
                # NEXT row (applied at the top of the loop) rather than
                # switched here, so THIS row's own left-side text - which
                # may still genuinely belong to the OLD section - keeps its
                # normal classification below.
                if rows_override is None and not used_letterhead_fallback and not logo_rest_used:
                    right_only =[w for w in row if w["x"] >= divider]
                    if right_only:
                        right_low = " ".join(w["text"].strip() for w in right_only).strip().lower()
                        for name, marks in _RIGHT_SIDE_SECTION_MARKERS:
                            if any(m in right_low for m in marks):
                                pending_switch = name
                                break

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
                if (rows_override is None and section == "Seller" and named["Seller"]
                        and not used_letterhead_fallback and not logo_rest_used):
                    right =sorted([w for w in row if w["x"] >= divider], key=lambda w: w["x"])
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
                # is never mistaken for it), "place of supply", or a
                # marketplace invoice's "place of delivery" (same GST
                # concept, different wording - e.g. Amazon's Consignee
                # block). Not itself a separate field worth keeping - just
                # needs to stop it falling through as bogus Address text.
                if (
                    "state name" in low or "state code" in low
                    or "place of supply" in low or "place of delivery" in low
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
                # Same again for PAN (Permanent Account Number) - its value is
                # its own piece of metadata, not a continuation of the address
                # the way "Address:123 Main St" glued together would be.
                if low.startswith(("irn", "ack no", "ack date", "e-invoice",
                                    "reverse charge", "credit days",
                                    "pan no", "pan:", "pan ")):
                    continue
                # CIN (Corporate Identity Number) - registration metadata,
                # "CIN: U72200DL2001PTC110044" / "CIN#U72200..." - never address.
                if re.match(r"cin\s*[:#\-]", low):
                    continue

                # A multi-page invoice's own "(Page 1 of 3)" pagination
                # marker, printed on its own line near a corner - never
                # name/address content, wherever it happens to land relative
                # to the real letterhead (e.g. it can sit right above the
                # genuine Seller Name, becoming the wrongly-picked "first
                # unclaimed line" for that section instead).
                if re.fullmatch(r"\(?\s*page\s+\d+\s+of\s+\d+\s*\)?", low):
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
                        # "Contact Person.: Mr.Ramesh 9940681023" - a person to
                        # call, not part of the party's name or address.
                        if low[start:end] in ("contact person", "contect person"):
                            continue
                        text = text[end:].lstrip(" :,-.")
                        # "Shipped From:" (label) sharing its row with the right
                        # column's "IRN No : <hash>" leaves the IRN line as
                        # the "remainder" - registration metadata, not a value.
                        if re.match(r"(?i)(irn|ack)\b", text):
                            continue
                        # A glued "<label>: <value>" row (nothing before the
                        # label to keep) whose value is a BARE date - e.g. a
                        # redundant "Invoice Date : 24.07.2026" repeated
                        # inside the Consignee block - is metadata, not real
                        # Name/Address content, even though it survived the
                        # label strip above. The genuine Invoice Date was
                        # already captured by the RIGHT_FIELDS anchor pass;
                        # this is just a second copy that would otherwise
                        # become a bogus trailing Address line.
                        # Not DATE_RE itself (that one's tuned for the
                        # dash/slash "Dated" fallback scan above and
                        # deliberately excludes dot-separated dates, which
                        # would otherwise misparse a dotted decimal amount
                        # elsewhere) - this is only a plain "is the leftover
                        # text just a date" check, so dots are fine too
                        # (e.g. "24.07.2026").
                        if re.fullmatch(r"\d{1,2}[-/.]\S+[-/.]\d{2,4}", text.strip()):
                            continue
                    else:
                        text = text[:start].rstrip(" ,;:-")
                    if not text:
                        continue
                    low = text.lower()

                # The item table's own column captions ("SN | CGST | SGST",
                # "Qty Rate Amount") can sit in a header row right below the
                # party block and, once sliced by the divider, leave a line
                # made of nothing but those caption words - never address
                # text ("22, 1st Floor, Habibullah Road T.Nagar / CGST SGST").
                if re.fullmatch(
                    r"(?i)[\W_]*(?:(?:s\.?\s*n\.?o?|sl\.?|cgst|sgst|igst|utgst|gst|"
                    r"qty|rate|amt|amount|tax|hsn|sac|item|description|total|%)[\W_]*)+",
                    text,
                ):
                    continue

                # An e-invoice's "IRN No : <hash>" / "Ack No : ..." registration
                # line is metadata wherever it ends up (here it can be left
                # over once a "Shipped From:" caption sharing its row is
                # stripped) - never a party name or address line.
                if re.match(r"(?i)(irn|ack)\b", text):
                    continue

                # A bare customer/account code ("Bill To- C000568") captions the
                # block but is not the company name - the name follows.
                if not named[section] and re.fullmatch(r"[A-Za-z]{1,3}\d{4,10}", text.strip()):
                    continue

                # First non-label line = party name; rest = address
                if not named[section]:
                    if not any(c.isalpha() for c in text):
                        # Not a real name candidate (e.g. a stray "."
                        # rendering artifact sitting above the real
                        # letterhead, with no label/GSTIN/etc. of its own
                        # to otherwise catch it) - a genuine company/person
                        # name always has at least one letter. Skip it
                        # entirely rather than consuming the "first content
                        # line" slot, so the next genuine line still
                        # becomes the Name instead of Address.
                        continue
                    out.setdefault(f"{section} Name", text)
                    named[section] = True
                    name_x[section] = left[0]["x"]
                    # Some 2-line logos stack the company name across two
                    # consecutive rows (e.g. "Hewlett Packard" / "Enterprise"
                    # - the actual two-line HPE wordmark, or "Anakage
                    # Technologies Private" / "Limited"), each independently
                    # sharing its own row with unrelated right-column
                    # metadata that happens to also land left of THIS
                    # page's own (unusually wide) divider - "Document Type
                    # Code:INV" glued onto "Enterprise" the same way "TAX
                    # INVOICE" glued onto "Hewlett Packard" above. Peeking at
                    # the very next row and applying the identical left-
                    # slice + mid-row-label-truncation this row itself just
                    # went through catches it - a short, plain, label-free
                    # result is a name continuation, not the real address
                    # (which starts on whichever row comes after that).
                    # Uses consumed_as_continuation (not claimed_rows) to
                    # skip that row on its own turn below - this runs the
                    # same way whether or not rows_override is set (a
                    # 3-column document's own Seller-block scan runs
                    # THROUGH rows_override too, via _side_by_side_party_rows
                    # below, so gating this on rows_override is None would
                    # silently skip every 3-column document).
                    nxt_ri = ri + 1
                    if nxt_ri < len(scan_rows) and nxt_ri not in consumed_as_continuation:
                        nxt_left = sorted(
                            [w for w in scan_rows[nxt_ri] if w["x"] < divider],
                            key=lambda w: w["x"],
                        )
                        nxt_text = _row_text(nxt_left) if nxt_left else ""
                        nxt_span = _label_span(nxt_text)
                        if nxt_span is not None:
                            nxt_text = nxt_text[:nxt_span[0]].rstrip(" ,;:-") if nxt_span[0] > 0 else ""
                        if (nxt_text and not _is_label(nxt_text)
                                and not _looks_like_phone_line(nxt_text)
                                and not _looks_like_url_line(nxt_text)
                                and len(nxt_text.split()) <= 2
                                # A genuine continuation word is a real,
                                # recognizable word ("Enterprise", "Limited")
                                # - a bare short fragment (e.g. "JE", an
                                # unrelated 2-character OCR artifact seen
                                # sitting on its own line above a Zaco
                                # invoice's real address) is far more often
                                # OCR noise than a real word this short.
                                and len(nxt_text) >= 4
                                and not any(c.isdigit() for c in nxt_text)
                                and any(c.isalpha() for c in nxt_text)):
                            out[f"{section} Name"] = f"{out[f'{section} Name']} {nxt_text}"
                            consumed_as_continuation.add(nxt_ri)
                            if rows_override is None:
                                claimed_rows.add(nxt_ri)
                else:
                    # A right/center-aligned letterhead sometimes repeats
                    # its own name as a small separate logo/watermark line,
                    # positioned well apart from the real flowing address
                    # text (e.g. TPM Guru's own "TPMGuru" sitting at the
                    # page's far left margin while the actual letterhead
                    # block sits centered well to the right) - never real
                    # address content, but with nothing else to catch it,
                    # it silently became a trailing Address line. A genuine
                    # address line never exactly reproduces (whitespace
                    # aside) a piece of the company's own already-captured
                    # Name, so that's the signal used here instead of
                    # position, which isn't available this far downstream.
                    name_compact = re.sub(r"\s+", "", out.get(f"{section} Name", "")).lower()
                    text_compact = re.sub(r"\s+", "", text).lower()
                    if text_compact and name_compact and text_compact in name_compact:
                        continue
                    # A bare 1-2 letter fragment with nothing else on its own
                    # line (e.g. Zaco's own "JE", sitting alone just above
                    # its real address - purpose unclear, but too short to
                    # be genuine address content on its own) - real address
                    # lines this short only ever appear as PART of a longer
                    # line, never as the row's entire content.
                    if len(text) <= 2 and text.isalpha():
                        continue
                    key = f"{section} Address"
                    out[key] = (out.get(key, "") + "\n" + text).strip() if out.get(key) else text

        return out, any_switch

    if three_col is not None:
        marker_row_src = header_rows[marker_ri]
        # The prefix (everything before the Bill To/Ship To marker row) is
        # the ordinary, unsliced Seller letterhead block - unlike bill_rows/
        # ship_rows below (each already narrowed to just one column's
        # words), it was never column-split. But every row fed into
        # rows_override is treated as "already narrowed, don't re-slice" by
        # _party_pass's own left-computation below, so a prefix row sharing
        # its line with unrelated right-side content (e.g. "Zaco Computers
        # Pvt Ltd" ... "e-Invoice") would keep that content glued on, and a
        # row already claimed by the anchor pass above (e.g. "IRN : <hash>")
        # would leak back in as Address text. Apply the same left-of-divider
        # slice and claimed_rows exclusion the ordinary (non-3-column) path
        # already gets, so only genuine Seller-block content survives.
        prefix_rows = [
            [w for w in row if w["x"] < divider]
            for ri, row in enumerate(header_rows[:marker_ri])
            if ri not in claimed_rows
        ]
        side_by_side_rows = (
            prefix_rows
            + [_marker_row("Bill to", bill_x, marker_row_src)] + bill_rows
            + [_marker_row("Ship to", ship_x, marker_row_src)] + ship_rows
        )
        party_fields, any_switch = _party_pass(rows_override=side_by_side_rows)
    elif stacked is not None:
        bill_ri, bill_x, ship_ri, ship_x = stacked
        # Unlike the 3-column case, the Seller block here isn't confined to
        # a prefix before a marker row - it runs down the LEFT half of the
        # very same rows the Billing/Shipping column occupies on the
        # RIGHT, for the whole span. So the whole page's left-of-divider
        # words become the Seller pass content (skipping any row already
        # claimed by an anchor, same as the ordinary non-split path), and
        # the right-of-divider words are what get split at the two
        # markers into a Billing block then a Shipping block.
        seller_rows = [
            [w for w in row if w["x"] < divider]
            for ri, row in enumerate(rows)
            if ri not in claimed_rows and ri not in right_field_rows
        ]
        bill_rows, ship_rows = [], []
        for ri, row in enumerate(rows):
            if ri in right_field_rows:
                continue
            rw = [w for w in row if w["x"] >= divider]
            if not rw:
                continue
            if bill_ri < ri < ship_ri:
                bill_rows.append(rw)
            elif ri > ship_ri:
                ship_rows.append(rw)
        stacked_rows = (
            seller_rows
            + [_marker_row("Billing Address", bill_x, rows[bill_ri])] + bill_rows
            + [_marker_row("Shipping Address", ship_x, rows[ship_ri])] + ship_rows
        )
        party_fields, any_switch = _party_pass(rows_override=stacked_rows)
    else:
        party_fields, any_switch = _party_pass()
    if not any_switch and not party_fields.get("Buyer Address") and meta_row is not None:
        # No textual or structural section marker fired anywhere in this
        # document - retry once, using the invoice-metadata row as the
        # seller/buyer boundary instead.
        #
        # Checked against "Buyer Address", not "Buyer Name": a template
        # whose Buyer Name label also happens to match a RIGHT_FIELDS
        # phrase (e.g. "Buyer Name": ["customer name"] matching "Customer
        # Name:") gets that ONE field claimed and set directly in `fields`
        # by the anchor pass above, entirely independent of whether the
        # party-block section ever actually switched - _party_pass seeds
        # its own working copy from `fields`, so party_fields.get("Buyer
        # Name") comes back truthy even though the switch that would also
        # capture the Buyer's real Address/GSTIN/State never happened,
        # silently losing all of it into the Seller's own block instead
        # (see SHWETMANI ENTERPRISES's "Customer Name:" template). Buyer
        # Address only ever comes from that switch, never from a
        # RIGHT_FIELDS anchor, so it's the reliable signal here.
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
    # ...but only when it doesn't contradict the buyer's OWN GSTIN: a
    # GSTIN's first two digits ARE its state code, and a vendor's "Place of
    # Supply" can carry a different state entirely (At Sales-1125: "Jammu
    # and Kashmir (01)" against a buyer GSTIN starting 33) - the GSTIN
    # then decides, via the ordinary GSTIN-derived fallback downstream.
    if not fields.get("Buyer State Name") and fields.get("Place of Supply"):
        pos = fields["Place of Supply"]
        code = re.search(r"\((\d{2})\)|code\s*:?\s*(\d{2})|\b(\d{2})\s*-", pos, re.IGNORECASE)
        bg = re.sub(r"[^0-9A-Z]", "", str(fields.get("Buyer GSTIN/UIN") or "").upper())
        pos_code = next((g for g in code.groups() if g), "") if code else ""
        if not (pos_code and len(bg) >= 2 and bg[:2].isdigit() and bg[:2] != pos_code):
            fields["Buyer State Name"] = pos

    _split_embedded_buyer_block(fields)

    return fields


_PIN_LINE_RE = re.compile(r"\b\d{6}\b")


def _split_embedded_buyer_block(fields):
    """A letterhead-only layout with no "Bill To"/"Buyer" caption at all
    (Printer World: the seller's centered letterhead, then the buyer's
    company name and address printed at the left margin below it) leaves
    the buyer's whole block glued onto the end of the SELLER's address,
    with nothing left for Buyer Name / Buyer Address. A postal address ends
    at the line carrying its PIN code, so a SECOND PIN-terminated block
    after the first one, with no buyer/consignee address found anywhere
    else, is that missing buyer block. Only ever fires when the seller
    address holds two PIN codes and no Buyer/Consignee Address exists."""
    if fields.get("Buyer Address") or fields.get("Consignee Address"):
        return
    lines = [ln for ln in (fields.get("Seller Address") or "").split("\n") if ln.strip()]
    pin_rows = [i for i, ln in enumerate(lines) if _PIN_LINE_RE.search(ln)]
    if len(pin_rows) < 2:
        return
    first = pin_rows[0]
    # Contact lines trailing the seller's own PIN line ("Email: ...") belong
    # to the seller, not the buyer block that follows.
    j = first + 1
    while j < len(lines) and ("@" in lines[j] or _looks_like_url_line(lines[j])
                              or _looks_like_phone_line(lines[j])):
        j += 1
    tail = lines[j:]
    if len(tail) < 2:
        return
    buyer_name = ""
    if not any(c.isdigit() for c in tail[0]) and any(c.isalpha() for c in tail[0]):
        buyer_name, tail = tail[0], tail[1:]
    fields["Seller Address"] = "\n".join(lines[:j])
    if buyer_name and not fields.get("Buyer Name"):
        fields["Buyer Name"] = buyer_name
    fields["Buyer Address"] = "\n".join(tail)
