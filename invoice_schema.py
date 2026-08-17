"""
Map the raw OCR result produced by ocr_engine.OCREngine.read_pdf()
into the downstream invoice JSON schema.

Only fields that can be read from (or derived from) the invoice image
are populated. Fields that require external master data / ERP lookups
(vendor codes, Nav item numbers, serial / barcode, sf_items, entity /
template configuration, posting groups, etc.) are left blank so they
can be filled in a later enrichment step.
"""

import os
import re
from datetime import date, datetime, timedelta

import buyer_order


# ---------------------------------------------------------------------------
# GST state code -> (state name, 2-letter short code)
# ---------------------------------------------------------------------------

STATE_CODES = {
    "01": ("Jammu and Kashmir", "JK"),
    "02": ("Himachal Pradesh", "HP"),
    "03": ("Punjab", "PB"),
    "04": ("Chandigarh", "CH"),
    "05": ("Uttarakhand", "UK"),
    "06": ("Haryana", "HR"),
    "07": ("Delhi", "DL"),
    "08": ("Rajasthan", "RJ"),
    "09": ("Uttar Pradesh", "UP"),
    "10": ("Bihar", "BR"),
    "11": ("Sikkim", "SK"),
    "12": ("Arunachal Pradesh", "AR"),
    "13": ("Nagaland", "NL"),
    "14": ("Manipur", "MN"),
    "15": ("Mizoram", "MZ"),
    "16": ("Tripura", "TR"),
    "17": ("Meghalaya", "ML"),
    "18": ("Assam", "AS"),
    "19": ("West Bengal", "WB"),
    "20": ("Jharkhand", "JH"),
    "21": ("Odisha", "OD"),
    "22": ("Chhattisgarh", "CG"),
    "23": ("Madhya Pradesh", "MP"),
    "24": ("Gujarat", "GJ"),
    "25": ("Daman and Diu", "DD"),
    "26": ("Dadra and Nagar Haveli", "DN"),
    "27": ("Maharashtra", "MH"),
    "28": ("Andhra Pradesh (Old)", "AP"),
    "29": ("Karnataka", "KA"),
    "30": ("Goa", "GA"),
    "31": ("Lakshadweep", "LD"),
    "32": ("Kerala", "KL"),
    "33": ("Tamil Nadu", "TN"),
    "34": ("Puducherry", "PY"),
    "35": ("Andaman and Nicobar Islands", "AN"),
    "36": ("Telangana", "TS"),
    "37": ("Andhra Pradesh", "AP"),
    "38": ("Ladakh", "LA"),
}


MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04",
    "may": "05", "jun": "06", "jul": "07", "aug": "08",
    "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _normalize_date(value):
    """'10-Nov-25' / '10-11-2025' -> 'DD-MM-YYYY' (best effort)."""

    if not value:
        return ""

    value = value.strip()

    # dd-Mon-yy  or  dd-Mon-yyyy
    match = re.match(
        r"(\d{1,2})[-/ ]+([A-Za-z]{3,})[-/ ]+(\d{2,4})",
        value
    )

    if match:
        day, mon, year = match.groups()
        mon = MONTHS.get(mon[:3].lower(), "01")
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}-{mon}-{year}"

    # dd-mm-yyyy / dd/mm/yy
    match = re.match(
        r"(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})",
        value
    )

    if match:
        day, mon, year = match.groups()
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}-{int(mon):02d}-{year}"

    # Doesn't match either recognized date shape — likely OCR noise from a
    # garbled scan (e.g. "03.0.7") rather than a genuine date in some other
    # format. Passing that through as if it were a real date is worse than
    # leaving Document Date blank, since it would also silently corrupt the
    # Due Date computed from it.
    return ""


# Fallback when no usable payment-terms days can be resolved from either
# Service First or the PDF.
DEFAULT_PAYMENT_TERMS_DAYS = 45

_ADVANCE_TERMS_RE = re.compile(r"100\s*%\s*advance", re.IGNORECASE)
_DAYS_RE = re.compile(r"(\d+)\s*days?", re.IGNORECASE)


def payment_terms_days(terms_text):
    """Number of days implied by a payment-terms phrase (e.g. '30 DAYS' -> 30,
    '100% ADVANCE PAYMENT' -> 0), or None if the phrase is blank or doesn't
    resolve to a fixed number of days (e.g. 'AGAINST INVOICE')."""
    text = (terms_text or "").strip()
    if not text:
        return None
    if _ADVANCE_TERMS_RE.search(text):
        return 0
    match = _DAYS_RE.search(text)
    if match:
        return int(match.group(1))
    return None


def resolve_payment_terms(sf_terms, pdf_terms):
    """
    Resolve (Payment Terms Code, days) from Service First's PaymentTermsName
    and the PDF's own extracted payment-terms text:
      - '100% ADVANCE PAYMENT' -> 0 days
      - '<N> DAYS' -> N days
      - 'AGAINST INVOICE' -> use the PDF's own terms text instead (applying
        the same rules to it)
      - anything else / nothing usable on either side -> default to
        DEFAULT_PAYMENT_TERMS_DAYS
    """
    sf_terms = (sf_terms or "").strip()
    pdf_terms = (pdf_terms or "").strip()

    if sf_terms and "against invoice" in sf_terms.lower():
        days = payment_terms_days(pdf_terms)
        return (pdf_terms or sf_terms), (DEFAULT_PAYMENT_TERMS_DAYS if days is None else days)

    days = payment_terms_days(sf_terms)
    if days is not None:
        return sf_terms, days

    # SF terms absent/unrecognized -> fall back to the PDF's own value.
    days = payment_terms_days(pdf_terms)
    if days is not None:
        return pdf_terms, days

    return (sf_terms or pdf_terms or f"{DEFAULT_PAYMENT_TERMS_DAYS} DAYS"), DEFAULT_PAYMENT_TERMS_DAYS


def add_days_to_date(date_str, days):
    """'DD-MM-YYYY' + N days -> 'DD-MM-YYYY'; '' if date_str isn't usable."""
    if not date_str:
        return ""
    try:
        dt = datetime.strptime(date_str, "%d-%m-%Y")
    except ValueError:
        return ""
    return (dt + timedelta(days=days)).strftime("%d-%m-%Y")


def _parse_state(value):
    """'Tamil Nadu, Code : 33' -> ('Tamil Nadu', '33')."""

    if not value:
        return "", ""

    code = ""
    code_match = (
        re.search(r"code\s*:?\s*(\d{1,2})", value, re.IGNORECASE)
        or re.search(r"\((\d{1,2})\)", value)   # "Tamilnadu (33)"
    )
    if code_match:
        code = code_match.group(1).zfill(2)

    name = re.split(r",?\s*code|\(", value, flags=re.IGNORECASE)[0].strip(" ,:")

    return name, code


def _pincode(*texts):
    """First 6-digit run found across the given texts."""

    for text in texts:
        if not text:
            continue
        match = re.search(r"\b(\d{6})\b", text)
        if match:
            return match.group(1)
    return ""


_CITY_STOPWORDS = {
    "state", "code", "india", "new", "road", "floor", "1st", "2nd", "3rd",
    "gstin", "uin", "pin", "pincode",
    # contact-line noise that can trail an address
    "ext", "tel", "mob", "ph", "phone", "email", "contact", "fax", "no", "mob.",
    # state-name words commonly trailing an address (incl. OCR misspellings)
    "tamil", "nadu", "madu", "delhi", "karnataka", "kerala", "maharashtra",
    "telangana", "andhra", "pradesh", "gujarat", "punjab", "haryana", "bengal",
    "west", "rajasthan", "odisha", "orissa", "bihar", "goa",
}


# Reference gazetteer of Indian city/town/locality names, used to recognize
# a city that's actually two (or three) words - e.g. "T Nagar", "Navi
# Mumbai", "New Delhi", "Anna Nagar" - which the single-last-word fallback
# below would otherwise cut down to just "Nagar"/"Mumbai"/"Delhi". Lowercase,
# multi-word entries as space-joined phrases. Not exhaustive, but covers
# every state/UT capital, the major metros/tier-2 cities, and the common
# business-district localities of the largest metros (Chennai, Mumbai,
# Bengaluru, Delhi NCR, Hyderabad, Pune) that GST invoices frequently cite
# in place of the formal city name.
_KNOWN_CITIES = {
    # State / UT capitals & major metros
    "delhi", "new delhi", "mumbai", "bengaluru", "bangalore", "chennai",
    "kolkata", "hyderabad", "pune", "ahmedabad", "surat", "jaipur",
    "lucknow", "kanpur", "nagpur", "indore", "thane", "bhopal",
    "visakhapatnam", "vadodara", "ghaziabad", "ludhiana", "agra", "nashik",
    "faridabad", "meerut", "rajkot", "kalyan", "vasai", "varanasi",
    "srinagar", "aurangabad", "dhanbad", "amritsar", "navi mumbai",
    "allahabad", "prayagraj", "ranchi", "howrah", "coimbatore", "jabalpur",
    "gwalior", "vijayawada", "jodhpur", "madurai", "raipur", "kota",
    "guwahati", "chandigarh", "solapur", "hubli", "dharwad",
    "hubli-dharwad", "bareilly", "moradabad", "mysore", "mysuru",
    "gurgaon", "gurugram", "aligarh", "jalandhar", "tiruchirappalli",
    "trichy", "bhubaneswar", "salem", "warangal", "bhiwandi", "saharanpur",
    "gorakhpur", "guntur", "bikaner", "amravati", "noida", "greater noida",
    "jamshedpur", "bhilai", "cuttack", "firozabad", "kochi", "cochin",
    "nellore", "bhavnagar", "dehradun", "durgapur", "asansol", "rourkela",
    "nanded", "kolhapur", "ajmer", "akola", "gulbarga", "kalaburagi",
    "jamnagar", "ujjain", "siliguri", "jhansi", "ulhasnagar", "jammu",
    "sangli", "belgaum", "belagavi", "mangalore", "mangaluru",
    "tirunelveli", "malegaon", "gaya", "jalgaon", "udaipur", "davanagere",
    "kozhikode", "calicut", "kurnool", "rajahmundry", "bokaro", "bellary",
    "ballari", "patiala", "agartala", "bhagalpur", "muzaffarnagar",
    "latur", "dhule", "rohtak", "korba", "bhilwara", "berhampur",
    "muzaffarpur", "ahmednagar", "mathura", "kollam", "avadi", "kadapa",
    "sambalpur", "bilaspur", "shahjahanpur", "satara", "bijapur",
    "vijayapura", "rampur", "shivamogga", "shimoga", "chandrapur",
    "junagadh", "thrissur", "alwar", "bardhaman", "burdwan", "nizamabad",
    "parbhani", "tumkur", "tumakuru", "khammam", "panipat", "darbhanga",
    "aizawl", "dewas", "karnal", "bathinda", "jalna", "eluru", "barabanki",
    "purnia", "satna", "mau", "sonipat", "farrukhabad", "sagar", "durg",
    "imphal", "ratlam", "hapur", "arrah", "anantapur", "karimnagar",
    "etawah", "ambernath", "bharatpur", "begusarai", "gandhidham",
    "puducherry", "pondicherry", "sikar", "thoothukudi", "tuticorin",
    "rewa", "mirzapur", "raichur", "pali", "ramagundam", "haridwar",
    "vizianagaram", "tenali", "nagercoil", "sri ganganagar", "thanjavur",
    "bulandshahr", "katni", "sambhal", "singrauli", "nadiad", "secunderabad",
    "yamunanagar", "bidhannagar", "pallavaram", "bidar", "munger",
    "panchkula", "burhanpur", "kharagpur", "dindigul", "gandhinagar",
    "hospet", "hosapete", "ambattur", "vellore", "machilipatnam", "shimla",
    "udupi", "katihar", "mahbubnagar", "saharsa", "dibrugarh", "jorhat",
    "hazaribagh", "bhimavaram", "guntakal", "panvel", "deoghar", "ongole",
    "nandyal", "morena", "bhiwani", "porbandar", "palakkad", "anand",
    "purulia", "baharampur", "barmer", "morvi", "morbi", "orai", "bahraich",
    "sirsa", "danapur", "serampore", "guna", "jaunpur", "shivpuri",
    "surendranagar", "unnao", "chittoor", "lakhimpur", "hindupur",
    "bharuch", "arakkonam", "chittorgarh", "ratnagiri", "nagaon",
    "cuddalore", "erode", "kanchipuram", "tirupati", "karaikudi",
    "neyveli", "rajapalayam", "sivakasi", "namakkal", "krishnagiri",
    "hosur", "pollachi", "nagapattinam", "virudhunagar", "ariyalur",
    "perambalur", "villupuram",
    # Chennai localities/areas commonly cited in place of "Chennai" itself
    "t nagar", "anna nagar", "adyar", "guindy", "velachery", "porur",
    "tambaram", "west tambaram", "east tambaram", "chromepet", "egmore",
    "mylapore", "nungambakkam", "perambur", "kilpauk", "vadapalani",
    "saidapet", "triplicane", "royapettah", "alwarpet", "nandanam",
    "teynampet", "ashok nagar", "kk nagar", "kodambakkam", "choolaimedu",
    "aminjikarai", "purasawalkam", "villivakkam", "selaiyur", "medavakkam",
    "sholinganallur", "thoraipakkam", "perungudi", "navalur", "siruseri",
    "padi", "korattur", "mogappair", "west mambalam", "mambalam",
    "poonamallee", "redhills", "madhavaram", "manali", "ennore",
    "royapuram", "washermanpet", "tondiarpet", "chetpet", "vepery",
    "santhome", "besant nagar", "thiruvanmiyur", "kolathur",
    # Mumbai localities
    "andheri", "bandra", "borivali", "dadar", "malad", "goregaon",
    "kandivali", "vile parle", "juhu", "powai", "chembur", "ghatkopar",
    "kurla", "worli", "colaba", "lower parel", "vikhroli", "mulund",
    "bhandup", "vashi", "nerul", "kharghar", "airoli", "belapur",
    "mira road", "virar", "nalasopara", "dombivli",
    # Bengaluru localities
    "whitefield", "koramangala", "indiranagar", "jayanagar",
    "malleshwaram", "rajajinagar", "hsr layout", "btm layout",
    "electronic city", "marathahalli", "yelahanka", "hebbal",
    "banashankari", "jp nagar", "basavanagudi", "peenya",
    # Delhi NCR localities
    "dwarka", "rohini", "pitampura", "karol bagh", "lajpat nagar",
    "saket", "vasant kunj", "janakpuri", "rajouri garden",
    "connaught place", "chandni chowk", "mayur vihar", "preet vihar",
    "laxmi nagar", "okhla", "nehru place", "greater kailash", "hauz khas",
    "model town", "shalimar bagh",
    # Hyderabad localities
    "gachibowli", "madhapur", "kukatpally", "ameerpet", "begumpet",
    "banjara hills", "jubilee hills", "uppal", "lb nagar", "miyapur",
    "kondapur",
    # Pune localities
    "hinjewadi", "kothrud", "baner", "wakad", "aundh", "hadapsar",
    "viman nagar", "kharadi", "pimpri", "chinchwad", "wagholi", "katraj",
}
# Longest phrase (in words) present in the gazetteer, so the matcher below
# knows how wide a window to try first.
_KNOWN_CITY_MAX_WORDS = max(len(c.split()) for c in _KNOWN_CITIES)


def _match_known_city(tokens):
    """Look for the longest gazetteer entry ending at the tail of `tokens`,
    trying wider windows first so a two-word city/locality ('T Nagar',
    'Navi Mumbai') is preferred over the single trailing word it contains."""
    n = len(tokens)
    for width in range(min(_KNOWN_CITY_MAX_WORDS, n), 0, -1):
        chunk = [t.strip(".:-–() ") for t in tokens[n - width:]]
        if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", c) for c in chunk):
            continue
        phrase = " ".join(c.rstrip(".") for c in chunk).lower()
        if phrase in _KNOWN_CITIES:
            return " ".join(w.capitalize() for w in phrase.split())
    return ""


def _city(address, state_name=""):
    """Best-effort city from an address. Tries the known-city gazetteer
    first (so multi-word cities/localities like 'T Nagar' or 'Navi Mumbai'
    come back whole), then falls back to a single-word heuristic: the last
    plain alphabetic word sitting just before the PIN code ('... Chennai -
    600 017', '... CHENNAI 600 017'); everything after the PIN (India /
    phone / Ext) is ignored. Falls back to the last alphabetic word when no
    PIN is present ('22 HABIBULLAH ROAD T.NAGAR CHENNAI'), skipping
    punctuation, phone/ext tokens and state-name words ('... CHENNAI, TAMIL
    MADU -')."""
    if not address:
        return ""
    stop = set(_CITY_STOPWORDS)
    for w in re.split(r"[\s,]+", (state_name or "").lower()):
        if w:
            stop.add(w)

    tokens = [t for t in re.split(r"[\s,]+", address.replace("\n", " ")) if t]

    # Cut everything from the PIN code onward (a 6-digit run, or a split
    # "600 017" pair) so trailing India / phone / Ext tokens are excluded.
    cut = len(tokens)
    for i, t in enumerate(tokens):
        t0 = t.strip(".,-")
        t1 = tokens[i + 1].strip(".,-") if i + 1 < len(tokens) else ""
        if re.fullmatch(r"\d{6}", t0) or (
            re.fullmatch(r"\d{3}", t0) and re.fullmatch(r"\d{3}", t1)
        ):
            cut = i
            break
    search = tokens[:cut] or tokens
    # Drop stray punctuation-only tokens (a lone "-" separator left behind
    # once the PIN itself is cut off) so they don't block a match at the
    # tail - e.g. "... T Nagar - 600017" must search from "Nagar", not "-".
    search = [t for t in search if re.search(r"[A-Za-z0-9]", t)]

    known = _match_known_city(search)
    if known:
        return known

    for tok in reversed(search):
        cleaned = tok.strip(".:-–() ")
        if len(cleaned) < 3:
            continue
        # a plain place name is alphabetic; reject phone/ext (":"), digits, etc.
        if not re.fullmatch(r"[A-Za-z][A-Za-z.&'-]*[A-Za-z]", cleaned):
            continue
        if cleaned.lower() in stop:
            continue
        return cleaned
    return ""


# A line that's actually contact info (phone/GST/email/website) rather
# than part of the postal address itself - GST invoice address blocks
# frequently run a "Mobile: ...", "GSTIN: ...", "Email: ..." line right
# after the real street address, which _address_lines() used to glue
# straight into Address 2, inflating it well past BC's field length and
# mixing contact data into an address field.
_CONTACT_LINE_RE = re.compile(
    r"@"                                                   # email
    r"|\bwww\.|https?://"                                  # website
    r"|\bgstin\b|\bgst\s*no\.?"                             # GST label
    r"|\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]Z[A-Z\d]\b"        # GSTIN itself
    r"|\b(?:ph|phone|mobile|mob|contact|tel|fax)\b\.?\s*:?" # phone label
    r"|\budyam\b"                                           # Udyam label
    r"|\budyam-[a-z]{2}-\d{2}-\d{7}\b"                      # Udyam reg. no.
    r"|^\+?\d[\d\s\-]{8,14}\d$",                            # bare phone number
    re.IGNORECASE,
)


def _strip_contact(line):
    """Cut a line at the first phone/GST/email/website/Udyam marker, so
    real address text OCR'd onto the same line as contact info (a common
    layout - '...Mumbai - 400001 UDYAM : UDYAM-MH-19-0149863 ...email...')
    keeps its street/city/PIN instead of the whole line being discarded.
    Returns "" if the marker starts at (or before) the first real
    character, i.e. the line is contact info start to finish."""
    m = _CONTACT_LINE_RE.search(line)
    if not m:
        return line
    cut = m.start()
    # If the marker starts mid-word (the "@" inside "admin@x.com"), back up
    # to the start of that word so a stray fragment ("admin") isn't left
    # dangling in the kept prefix.
    if cut > 0 and not line[cut - 1].isspace():
        ws = line.rfind(" ", 0, cut)
        cut = ws + 1 if ws != -1 else 0
    return line[:cut].strip(" ,.-")


def _address_lines(address):
    """Split a possibly multi-line address into (line1, line2), trimming
    away any phone/GST/email/website/Udyam contact info found on a line
    rather than an actual address line."""

    if not address:
        return "", ""

    raw_lines = [ln.strip() for ln in address.split("\n") if ln.strip()]
    lines = [s for s in (_strip_contact(ln) for ln in raw_lines) if s]

    line1 = lines[0] if lines else ""
    line2 = " ".join(lines[1:]) if len(lines) > 1 else ""

    return line1, line2


def _num(value):
    """'1,150.00' / 1150.0 -> float (or None)."""

    if value is None or value == "":
        return None
    try:
        return float(str(value).replace(",", "").replace("%", "").strip())
    except (ValueError, TypeError):
        return None


def _money(value):
    """Normalize a numeric value to a 2-decimal string ('1150.00')."""

    n = _num(value)
    if n is None:
        return ""
    return f"{n:.2f}"


def _tax_rate(tax_total):
    """Pull the GST rate (e.g. 18.0) from the first tax-summary row."""

    for row in tax_total:
        igst = _num(row.get("IGST_Rate_%"))
        if igst:
            return igst
        # Intra-state: the item's GST rate is CGST + SGST (e.g. 9% + 9% = 18%),
        # not a single component.
        cgst = _num(row.get("CGST_Rate_%"))
        sgst = _num(row.get("SGST_Rate_%"))
        if cgst and sgst:
            return cgst + sgst
        if cgst or sgst:
            return cgst or sgst
    return None


def _rate_near(text, keyword):
    """First plausible percentage on a line that mentions `keyword`."""

    for line in text.splitlines():
        if keyword in line.lower():
            match = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", line)
            if match:
                value = float(match.group(1))
                if 0 < value <= 40:
                    return value
    return None


def _gst_rate_from_text(text):
    """
    Best-effort GST rate (%) read straight from the OCR text.

    Used as a fallback when the tax-summary table couldn't be parsed
    (some invoice layouts have no HSN/tax table this code recognises).
    For intra-state invoices the CGST and SGST components are summed.
    """

    if not text:
        return None

    igst = _rate_near(text, "igst")
    if igst:
        return igst

    cgst = _rate_near(text, "cgst")
    sgst = _rate_near(text, "sgst")
    if cgst and sgst:
        return cgst + sgst
    if cgst or sgst:
        return cgst or sgst

    return _rate_near(text, "gst")


# ---------------------------------------------------------------------------
# Main mapper
# ---------------------------------------------------------------------------

def build_invoice_json(result, pdf_path=""):
    """Map a raw OCR result into the downstream invoice JSON schema.
    Sample: build_invoice_json(ocr_result, 'D:/PIIPS/Input/SPR/Bosch/inv1.pdf')"""

    fields = result.get("Fields", {})
    ocr_items = result.get("Items", [])
    tax_total = result.get("TaxSummary", [])

    # ---- parties ---------------------------------------------------------

    seller_state_name, seller_state_code = _parse_state(
        fields.get("Seller State Name", "")
    )
    buyer_state_name, buyer_state_code = _parse_state(
        fields.get("Buyer State Name", "")
    )
    consignee_state_name, consignee_state_code = _parse_state(
        fields.get("Consignee State Name", "")
    )

    # State code fallback: the first 2 digits of a GSTIN are the state code.
    # Fill any state code/name the layout didn't print explicitly so the
    # downstream State / GST Order Address State / Location State Code columns
    # are populated consistently.
    def _state_from_gstin(g):
        g = re.sub(r"[^0-9A-Z]", "", (g or "").upper())
        return g[:2] if len(g) >= 2 and g[:2].isdigit() else ""

    if not seller_state_code:
        seller_state_code = _state_from_gstin(fields.get("Seller GSTIN/UIN", ""))
    if not buyer_state_code:
        buyer_state_code = _state_from_gstin(fields.get("Buyer GSTIN/UIN", ""))
    if not consignee_state_code:
        consignee_state_code = _state_from_gstin(fields.get("Consignee GSTIN/UIN", ""))
    if not seller_state_name and seller_state_code in STATE_CODES:
        seller_state_name = STATE_CODES[seller_state_code][0]
    if not buyer_state_name and buyer_state_code in STATE_CODES:
        buyer_state_name = STATE_CODES[buyer_state_code][0]
    if not consignee_state_name and consignee_state_code in STATE_CODES:
        consignee_state_name = STATE_CODES[consignee_state_code][0]

    seller_addr = fields.get("Seller Address", "")
    buyer_addr = fields.get("Buyer Address", "")
    consignee_addr = fields.get("Consignee Address", "")

    seller_a1, seller_a2 = _address_lines(seller_addr)
    buyer_a1, buyer_a2 = _address_lines(buyer_addr)
    consignee_a1, consignee_a2 = _address_lines(consignee_addr)

    seller_pin = _pincode(seller_addr, seller_a1, seller_a2)
    buyer_pin = _pincode(buyer_addr)
    consignee_pin = _pincode(consignee_addr)

    _, seller_short = STATE_CODES.get(seller_state_code, ("", ""))
    _, buyer_short = STATE_CODES.get(buyer_state_code, ("", ""))

    consignee_city = _city(consignee_addr, consignee_state_name)

    order_no = fields.get("Buyer's Order No.", "")

    # Some vendors don't print the PO in a labelled field — it's written (often
    # by hand) somewhere on the page in the SPRPUR form. When the anchored value
    # is missing or isn't a clean SPRPUR PO, scan the full page text for one.
    #  * a confident match fills the PO;
    #  * a doubtful (OCR-garbled) match fills it but flags buyer_order_doubtful
    #    so the invoice is parked for a user to confirm.
    buyer_order_doubtful = False
    if not order_no or not buyer_order.looks_like_po(order_no):
        found, confidence = buyer_order.find_buyer_order_no(result.get("Text", ""))
        if confidence == "confident":
            order_no = found
        elif confidence == "doubtful":
            order_no = found
            buyer_order_doubtful = True

    # Seller GSTIN must never be the buyer's / consignee's. When the seller
    # GSTIN is unreadable (some layouts cram it against another line) the only
    # GSTINs on the page are the buyer's — leave the field blank rather than
    # record a wrong Vendor GST Reg. No.
    _ng = lambda g: re.sub(r"[^A-Z0-9]", "", (g or "").upper())
    seller_gstin_v = fields.get("Seller GSTIN/UIN", "") or ""
    if seller_gstin_v and _ng(seller_gstin_v) in {
        _ng(fields.get("Buyer GSTIN/UIN")), _ng(fields.get("Consignee GSTIN/UIN"))
    } - {""}:
        seller_gstin_v = ""

    # Prefer the parsed tax table; fall back to reading the rate from the
    # OCR text for layouts whose tax table we don't recognise.
    rate = _tax_rate(tax_total)
    if rate is None:
        rate = _gst_rate_from_text(result.get("Text", ""))

    # Intra-state (seller & buyer in the same state) GST is split into equal
    # CGST + SGST halves. Some layouts print / parse only one half — promote a
    # recognised half-rate to its full slab so the item's GST % is the total.
    _HALF_TO_FULL = {2.5: 5.0, 6.0: 12.0, 9.0: 18.0, 14.0: 28.0}
    if (rate and seller_state_code and seller_state_code == buyer_state_code):
        rate = _HALF_TO_FULL.get(rate, rate)

    rate_str = f"{rate:g}%" if rate else ""

    # ---- items -----------------------------------------------------------

    items = []
    for i, it in enumerate(ocr_items, start=1):

        try:
            serial = int(it.get("SI"))
        except (TypeError, ValueError):
            serial = i

        qty_n = _num(it.get("Quantity"))
        amt_n = _num(it.get("Amount"))
        desc = it.get("Description", "")
        is_charge = bool(it.get("charge"))

        # The invoice-level rate covers layouts with a proper tax-summary
        # table or a general "IGST/CGST+SGST NN%" line in the OCR text. Some
        # layouts instead print the rate trailing the item's own description
        # ("... PICKUP ROLLER 18%") with nothing recognisable elsewhere on
        # the page - fall back to that per item when the invoice-level rate
        # came back blank, rather than leaving GST % empty.
        item_rate = rate if rate else _rate_near(desc, "")
        # (keyword="" matches every line - the description is what we're
        # scanning here, not a labelled tax-summary line.)

        items.append({
            "sl_no": serial,
            "Description": desc,
            "hsn": it.get("HSN") or "",
            "rate": _money(it.get("Rate")),
            "Unit": "",
            "discount": "",
            "amount": _money(it.get("Amount")),
            "Document No.": "",
            "Type": "Charge (Item)" if is_charge else "Item",
            "_charge": is_charge,
            "Line_No": serial * 10000,
            "PurchaseOrderNo": order_no.lower(),
            "Quantity": qty_n if qty_n is not None else "",
            "Amount": amt_n if amt_n is not None else "",
            "GST_Base_Amount": amt_n if amt_n is not None else "",
            "TaxPercentage": item_rate if item_rate else "",
            "HSN_Percentage_Description": (
                f"Goods {item_rate:g}%" if item_rate else ""
            ),
            "PartSpecification": desc,
            # Seed from the PDF's own table HSN/SAC column so it's used
            # whenever the Service First HSN lookup doesn't return a match
            # for this line (enrich_items overwrites it when SF does).
            "ProductNo": it.get("HSN") or "",
            "HSN_Type": "Goods",
            "Nav_Item_No": "",
        })

    # Consignee / Ship-to falls back to Buyer / Bill-to when the PDF has no
    # separate consignee block (item 12).
    buyer_name_v = fields.get("Buyer Name", "")
    buyer_gstin_v = fields.get("Buyer GSTIN/UIN", "")
    consignee_name_v = fields.get("Consignee Name", "")
    consignee_gstin_v = fields.get("Consignee GSTIN/UIN", "")
    if not str(consignee_name_v).strip():
        consignee_name_v = buyer_name_v
        consignee_state_name = consignee_state_name or buyer_state_name
        consignee_a1 = consignee_a1 or buyer_a1
        consignee_a2 = consignee_a2 or buyer_a2
        consignee_pin = consignee_pin or buyer_pin
        if not consignee_city:
            consignee_city = _city(buyer_addr, buyer_state_name)
    if not str(consignee_gstin_v).strip():
        consignee_gstin_v = buyer_gstin_v

    # ---- assembled schema ------------------------------------------------

    data = {
        "invoice_no": fields.get("Invoice No.", ""),
        "invoice_date": _normalize_date(fields.get("Dated", "")),
        "buyer_name": buyer_name_v,
        "buyer_gstin": buyer_gstin_v,
        "buyer_state": buyer_state_name,
        "consignee_name": consignee_name_v,
        "consignee_gstin": consignee_gstin_v,
        "consignee_state": consignee_state_name,
        "buyer_order_no": order_no,
        "buyer_order_doubtful": buyer_order_doubtful,
        "amount_in_words": fields.get("Amount Chargeable (in words)", ""),
        "seller_name": fields.get("Seller Name", ""),
        "seller_gstin": seller_gstin_v,
        "seller_state": seller_state_name,
        "payment_terms_code": fields.get("Mode/Terms of Payment", ""),
        "seller_pincode": seller_pin,
        "Pay_to_Post_Code": seller_pin,
        "buyer_pincode": buyer_pin,
        "Ship_to_Post_Code": consignee_pin,
        "pincode": seller_pin,
        "items": items,
        "Entity": "",
        "Template": "",
        "Template_Name": os.path.splitext(os.path.basename(pdf_path))[0],
        "File_Name": os.path.basename(pdf_path),
        "Status": "Open",
        "Posting_Date": date.today().strftime("%d-%m-%Y"),
        "buyer_address1": buyer_a1,
        "buyer_address2": buyer_a2,
        "consignee_address1": consignee_a1,
        "consignee_address2": consignee_a2,
        "seller_address1": seller_a1,
        "seller_address2": seller_a2,
        "Pay_to_Address1": seller_a1,
        "Pay_to_Address2": seller_a2,
        "Pay_to_Name": fields.get("Seller Name", ""),
        "Due_Date": "",
        "consignee_city": consignee_city,
        "buyer_state_GST_code": buyer_state_code,
        "seller_state_GST_code": seller_state_code,
        "buyer_state_short": buyer_short,
        "state": seller_short,
        "seller_state_short": seller_short,
        "sf_items": [],
        "Nav_VendorCode": "",
        "Pay_to_Vendor_No": "",
        "Location_Code": "",
        "Location_State_Code": "",
        "GST_Order_Address_State": "",
        "Buy_from_Vendor_No": "",
        "Vendor_Invoice_No": "",
        "Currency_Code": "",
        "Gen_Bus_Posting_Group": "",
        "GST_Vendor_Type": "",
        "PaymentTermsName": "",
    }

    return data
