"""
Map the raw OCR result produced by ocr_engine.OCREngine.read_pdf()
into the downstream invoice JSON schema.

Only fields that can be read from (or derived from) the invoice image
are populated. Fields that require external master data / ERP lookups
(vendor codes, Nav item numbers, serial / barcode, sf_items, entity /
template configuration, posting groups, etc.) are left blank so they
can be filled in a later enrichment step.
"""

import difflib
import os
import re
from datetime import date, datetime, timedelta

import buyer_order
import vendor_code as vendor_code_lib


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

    # dd-Mon-yy  or  dd-Mon-yyyy (the day-month separator is optional, not
    # just tolerant of different characters - an informal invoice
    # sometimes glues them with no gap at all, e.g. "14July.26")
    match = re.match(
        r"(\d{1,2})[-/. ]*([A-Za-z]{3,})[-/. ]+(\d{2,4})",
        value
    )

    if match:
        day, mon, year = match.groups()
        mon = MONTHS.get(mon[:3].lower())
        # An unreadable day ("0-Jul-20" from a poor scan) or an unknown
        # month word is left blank so it surfaces as a missing Invoice Date
        # instead of a made-up "00-07-2020" / January.
        if not mon or not 1 <= int(day) <= 31:
            return ""
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}-{mon}-{year}"

    # dd-mm-yyyy / dd/mm/yy / dd.mm.yyyy
    match = re.match(
        r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2,4})",
        value
    )

    if match:
        day, mon, year = match.groups()
        if not (1 <= int(day) <= 31 and 1 <= int(mon) <= 12):
            return ""
        if len(year) == 2:
            year = "20" + year
        return f"{int(day):02d}-{int(mon):02d}-{year}"

    # Mon dd, yyyy (American-style, month spelled out FIRST - e.g. a
    # generic small-business invoice template's "Invoice Date: July 10,
    # 2026") - checked separately from the "dd-Mon-yyyy" pattern above
    # since the day/month order is reversed; that pattern's leading
    # \d{1,2} group can never match a spelled-out month name, so it
    # correctly never misfires on this format either way.
    match = re.match(
        r"([A-Za-z]{3,})[-/. ]+(\d{1,2})[,]?[-/. ]+(\d{2,4})",
        value
    )

    if match:
        mon, day, year = match.groups()
        mon = MONTHS.get(mon[:3].lower())
        if mon:
            if len(year) == 2:
                year = "20" + year
            return f"{int(day):02d}-{mon}-{year}"

    # yyyy-mm-dd / yyyy/mm/dd (ISO-style, common on computer-generated GST
    # e-invoices, e.g. "2026-08-22") - a 4-digit year FIRST, so it must be
    # checked separately: it never matches the dd-mm-yyyy pattern above
    # (that one's leading \d{1,2} group can only ever consume 2 of the 4
    # digits, leaving no separator where it expects one, so it correctly
    # never misfires on this format either way).
    match = re.match(
        r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})\b",
        value
    )

    if match:
        year, mon, day = match.groups()
        return f"{int(day):02d}-{int(mon):02d}-{year}"

    # Doesn't match either recognized date shape — likely OCR noise from a
    # garbled scan (e.g. "03.0.7") rather than a genuine date in some other
    # format. Passing that through as if it were a real date is worse than
    # leaving Document Date blank, since it would also silently corrupt the
    # Due Date computed from it.
    return ""


# Fallback when no usable payment-terms days can be resolved from either
# Service First or the PDF.
DEFAULT_PAYMENT_TERMS_DAYS = 30

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


_NOT_APPLICABLE_RE = re.compile(r"^\s*n\s*/?\s*a\.?\s*$|^\s*not\s+applicable\s*$", re.IGNORECASE)


def _parse_state(value):
    """'Tamil Nadu, Code : 33' -> ('Tamil Nadu', '33')."""

    if not value:
        return "", ""

    # A seller whose own template prints "State Name : Not Applicable |
    # 600033" (e.g. "Zaco Computer.pdf", the pincode tacked on after a
    # "|") - real printed text, but not a real state name; passing it
    # through as-is would otherwise block every fallback below it (all
    # gated on the state name still being blank) from ever running, and
    # go on to surface as the literal "state" everywhere downstream
    # instead of being recognized as genuinely unstated.
    if _NOT_APPLICABLE_RE.match(re.sub(r"\s*\|.*$", "", value)):
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


# {lowercased state name: (state code, real-cased name)} - built once from
# STATE_CODES for _state_from_text's reverse (name -> code) lookup below.
_STATE_NAME_TO_CODE = {
    name.lower(): (code, name) for code, (name, _short) in STATE_CODES.items()
}

# {2-letter short code: (state code, full name)} - e.g. "MH" -> ("27",
# "Maharashtra"). Some vendor letterheads give their own address as
# "City (XX)" with the state abbreviated rather than spelled out (e.g.
# SHWETMANI ENTERPRISES's own "Jalgaon (MH)") - see _state_from_text.
_STATE_SHORT_TO_CODE = {
    short.upper(): (code, name) for code, (name, short) in STATE_CODES.items() if short
}


def _state_from_text(text):
    """(state name, state code) for the first recognized Indian state name
    found anywhere in `text` (word-boundary, case-insensitive), or None if
    none matches. Last-resort fallback for a non-GST vendor whose address
    states its own city/state in plain prose with no separate "State:"
    field or GSTIN to derive it from otherwise (e.g. "Bhiwandi,
    Maharashtra 421101").
    Sample: _state_from_text("Tal:, Bhiwandi, Maharashtra 421101") -> ("Maharashtra", "27")
    """
    low = (text or "").lower()
    for state_low, (code, real_name) in _STATE_NAME_TO_CODE.items():
        if re.search(r"\b" + re.escape(state_low) + r"\b", low):
            return real_name, code

    # "City (XX)" - a 2-letter state short code in parentheses, e.g.
    # "Jalgaon (MH)". Checked against the real (unlowercased) text since
    # the short codes are meaningful only as uppercase.
    short_match = re.search(r"\(([A-Za-z]{2})\)", text or "")
    if short_match and short_match.group(1).upper() in _STATE_SHORT_TO_CODE:
        code, real_name = _STATE_SHORT_TO_CODE[short_match.group(1).upper()]
        return real_name, code

    # Tolerate a small vendor typo / OCR slip in a single-word state name
    # (e.g. "Udaipur,Rajsthan 313001" - missing the "a" in "Rajasthan") -
    # word-for-word similarity against each single-word state name, rather
    # than requiring an exact match. Multi-word state names (e.g. "Tamil
    # Nadu") aren't attempted here - too easy to false-positive matching
    # against ordinary two-word address text one word at a time.
    words = re.findall(r"[a-z]+", low)
    for state_low, (code, real_name) in _STATE_NAME_TO_CODE.items():
        if " " in state_low or len(state_low) < 6:
            continue
        for w in words:
            if len(w) >= 6 and difflib.SequenceMatcher(None, w, state_low).ratio() >= 0.85:
                return real_name, code

    # Last resort: a handful of major metro city names, each unambiguously
    # in one state, for an address that states only "City, PIN" with no
    # state name anywhere (e.g. SHWETMANI ENTERPRISES's own "Chennai, 600
    # 017" - a city name alone, common on a simple small-business invoice
    # template with no dedicated State field). Deliberately a short,
    # conservative list of state capitals/major metros only - anything
    # smaller/ambiguous is left unmatched rather than guessed.
    for city_low, state_code in _MAJOR_CITY_TO_STATE_CODE.items():
        if re.search(r"\b" + re.escape(city_low) + r"\b", low):
            return STATE_CODES[state_code][0], state_code
    return None


# {lowercased major metro city: state code} - each city is unambiguously in
# exactly one state, so a bare city name (no state stated) can still
# resolve a real state via STATE_CODES. See _state_from_text's last tier.
_MAJOR_CITY_TO_STATE_CODE = {
    "chennai": "33", "coimbatore": "33", "madurai": "33",
    "mumbai": "27", "pune": "27", "nagpur": "27",
    "bangalore": "29", "bengaluru": "29",
    "hyderabad": "36", "delhi": "07", "new delhi": "07",
    # Some address blocks print "NEWDELHI" glued as one word (no space) -
    # "new delhi"/"delhi" above are word-boundary matches, so neither
    # substring-matches "newdelhi" at all (e.g. "421 FREE..pdf": "Plot No
    # 53 block R2 Mohan Garedn NEWDELHI 110059", no GSTIN or State field
    # anywhere for the seller to derive it from otherwise).
    "newdelhi": "07",
    "kolkata": "19", "ahmedabad": "24", "surat": "24",
    "jaipur": "08", "lucknow": "09", "kanpur": "09",
    "bhopal": "23", "indore": "23", "patna": "10",
    "chandigarh": "04", "kochi": "32", "cochin": "32",
    "thiruvananthapuram": "32", "guwahati": "18",
}


def _pincode(*texts):
    """First 6-digit run found across the given texts."""

    for text in texts:
        if not text:
            continue
        match = re.search(r"\b(\d{6})\b", text)
        if match:
            return match.group(1)
        # A PIN printed with a gap in the middle ("Chennai-600 017").
        split = re.search(r"\b([1-8]\d{2})\s(\d{3})\b", text)
        if split:
            return split.group(1) + split.group(2)
    return ""


# Indian PIN code -> GST state code, by the PIN's own first 2 digits
# (India Post's own postal-circle numbering). Last resort for a non-GST
# vendor whose address states a PIN code but no state name/city
# _state_from_text can recognize (e.g. "421 FREE..pdf"/"701 FRE..pdf": two
# different freelancers' own invoices, each with only "<street>, <city>
# <pincode>" and no GSTIN at all to derive a state from otherwise). Only
# the broad, unambiguous majority-state prefixes are listed - a handful of
# real PIN ranges straddle two states (e.g. Uttarakhand's own pins sit
# inside UP's 24x-26x block) and are deliberately left out rather than
# guessed at 2-digit precision; those addresses still fall through to
# blank, same as an unrecognized prefix.
_PINCODE_PREFIX_TO_STATE_CODE = {
    "11": "07",  # Delhi
    "12": "06", "13": "06",  # Haryana
    "14": "03", "15": "03",  # Punjab
    "17": "02",  # Himachal Pradesh
    "18": "01",  # Jammu and Kashmir
    "20": "09", "21": "09", "22": "09", "23": "09",  # Uttar Pradesh
    "28": "09",
    "30": "08", "31": "08", "32": "08", "33": "08", "34": "08",  # Rajasthan
    "36": "24", "37": "24", "38": "24", "39": "24",  # Gujarat
    "40": "27", "41": "27", "42": "27", "43": "27", "44": "27",  # Maharashtra
    "45": "23", "46": "23", "47": "23", "48": "23",  # Madhya Pradesh
    "49": "22",  # Chhattisgarh
    "50": "36", "51": "36", "52": "36",  # Telangana
    "53": "37",  # Andhra Pradesh
    "56": "29", "57": "29", "58": "29", "59": "29",  # Karnataka
    "60": "33", "61": "33", "62": "33", "63": "33", "64": "33",  # Tamil Nadu
    "67": "32", "68": "32", "69": "32",  # Kerala
    "70": "19", "71": "19", "72": "19", "73": "19", "74": "19",  # West Bengal
    "75": "21", "76": "21", "77": "21",  # Odisha
    "78": "18",  # Assam
    "80": "10", "81": "10", "82": "10", "84": "10", "85": "10",  # Bihar
    "83": "20",  # Jharkhand
}


def _header_pincodes(text):
    """Standalone 6-digit numbers printed in the page text ahead of the
    item table (the first line with an item-table column caption), i.e. the
    letterhead / Bill-to / Ship-to area - never HSN codes, bank or tax
    figures further down."""
    header = []
    for line in (text or "").splitlines():
        if re.search(r"\b(?:description|hsn|sac)\b", line, re.IGNORECASE):
            break
        header.append(line)
    return set(re.findall(
        r"(?<![\d/])(?<!\d[.,])\d{6}(?!\d|/|[.,]\d)", "\n".join(header)))


def _state_from_pincode(pincode):
    """(state name, GST state code) from a 6-digit Indian PIN code's own
    first 2 digits, or None if the prefix isn't one of the unambiguous
    ones above.
    Sample: _state_from_pincode('201301') -> ('Uttar Pradesh', '09')"""
    code = _PINCODE_PREFIX_TO_STATE_CODE.get((pincode or "")[:2])
    if code and code in STATE_CODES:
        return STATE_CODES[code][0], code
    return None


_CITY_STOPWORDS = {
    "state", "code", "india", "new", "road", "floor", "1st", "2nd", "3rd",
    # column captions / footer words that can trail an address block
    "service", "services", "invoice", "delivery", "dated", "date", "details",
    "address", "note",
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


def _any_known_city(tokens):
    """Like _match_known_city, but tries every possible end position, not
    just the very tail - a fallback for when trailing non-address junk
    after the real city (no genuine PIN to cut at) defeats the tail
    anchor, e.g. '... New Delhi Pin' with a literal word instead of a
    real PIN. Walks end positions right to left, widest-first at each, so
    a later, longer match ('New Delhi') is always preferred over an
    earlier or shorter one ('Nehru Place', or bare 'Delhi') - the same
    right-anchored preference _match_known_city has, just not limited to
    only the true last token."""
    n = len(tokens)
    for end in range(n, 0, -1):
        for width in range(min(_KNOWN_CITY_MAX_WORDS, end), 0, -1):
            chunk = [t.strip(".:-–() ") for t in tokens[end - width:end]]
            if not all(re.fullmatch(r"[A-Za-z][A-Za-z.'-]*", c) for c in chunk):
                continue
            phrase = " ".join(c.rstrip(".") for c in chunk).lower()
            if phrase in _KNOWN_CITIES:
                return " ".join(w.capitalize() for w in phrase.split())
    return ""


def _first_city_pos(line):
    """Character position where a known city/locality name (the same
    gazetteer _city() matches against) begins in `line`, or -1 if none
    found. Everything from that point on - the city itself, state, PIN,
    any contact info trailing it - belongs in the dedicated City/Pincode
    columns, not Address 2, so callers cut the line there."""
    tokens = list(re.finditer(r"[A-Za-z][A-Za-z.'-]*", line))
    n = len(tokens)
    for i in range(n):
        for width in range(min(_KNOWN_CITY_MAX_WORDS, n - i), 0, -1):
            # A trailing "-" glued onto the token itself ("CHENNAI-600002"
            # split on the digit run leaves "CHENNAI-") must not survive
            # into the lookup, or it'll never match the plain gazetteer key.
            phrase = " ".join(
                t.group().rstrip(".-").lower() for t in tokens[i:i + width]
            )
            if phrase in _KNOWN_CITIES:
                return tokens[i].start()
    return -1


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
        # City and PIN glued with no space at all ("CHENNAI-600008",
        # "MUMBAI-400703") - .strip() above can't separate them since the
        # digits aren't at a token edge. Keep the alpha prefix (the city)
        # so it's still available to match below, but cut right after it.
        glued = re.match(r"^([A-Za-z][A-Za-z.'-]*?)-?(\d{6})$", t0)
        if glued:
            tokens[i] = glued.group(1)
            cut = i + 1
            break
    search = tokens[:cut] or tokens
    # Drop stray punctuation-only tokens (a lone "-" separator left behind
    # once the PIN itself is cut off) so they don't block a match at the
    # tail - e.g. "... T Nagar - 600017" must search from "Nagar", not "-".
    search = [t for t in search if re.search(r"[A-Za-z0-9]", t)]

    known = _match_known_city(search) or _any_known_city(search)
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
        # A city the OCR/typist misspelled by a letter or two ("Chennae")
        # resolves to the gazetteer spelling when it's an unambiguous near
        # match, instead of being kept as printed.
        if len(cleaned) >= 6:
            close = difflib.get_close_matches(cleaned.lower(), _KNOWN_CITIES, n=1, cutoff=0.85)
            if close:
                return " ".join(w.capitalize() for w in close[0].split())
        return cleaned
    return ""


# A line that's actually contact info (phone/GST/email/website) or a
# landmark/intercom note rather than part of the postal address itself -
# GST invoice address blocks frequently run a "Mobile: ...", "GSTIN: ...",
# "Near <landmark>", "Intercom-<no>" bit right after the real street
# address, which _address_lines() used to glue straight into Address 2,
# inflating it well past BC's field length and mixing non-address data in.
_CONTACT_LINE_RE = re.compile(
    r"@"                                                   # email
    r"|\bwww\.|https?://"                                  # website
    r"|\bgstin\b|\bgst\s*no\.?"                             # GST label
    r"|\b\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]Z[A-Z\d]\b"        # GSTIN itself
    r"|\b(?:ph|phone|mobile|mob|contact|tel|fax)\b\.?\s*:?" # phone label
    r"|\bpan(?:\s*/\s*it)?\b\s*(?:no\.?)?\s*[:\-]?\s*[A-Z]{5}\d{4}[A-Z]\b"  # PAN label + number
    r"|\bcin\b\s*[:#\-]"                                    # CIN registration number
    r"|\budyam\b"                                           # Udyam label
    r"|\budyam-[a-z]{2}-\d{2}-\d{7}\b"                      # Udyam reg. no.
    r"|\bnear\b"                                            # landmark ("Near Casino Theatre")
    r"|\bintercom\b"                                        # intercom number
    r"|\bdays\b"                                            # payment-terms fragment ("... 30 Days Credit")
    r"|\biso\s*certified\b|\biso\s*9001\b"                  # ISO certification boilerplate
    r"|\b\d[\d\-]{7,13}\d\b",                               # bare phone number, labelled or not
    re.IGNORECASE,
)


def _strip_contact(line):
    """Cut a line at the first phone/GST/email/website/Udyam/landmark/
    intercom marker OR the start of a recognized city/locality name,
    whichever comes first - the City/Pincode columns already carry that
    information, so Address 2 shouldn't repeat city/state/PIN or whatever
    trails them ('...Nehru Place New Delhi-110019 ISO Certified -
    9001:2015 Days' keeps only 'Nehru Place'). Returns "" if the cut point
    is at (or before) the first real character, i.e. the line is nothing
    but that noise start to finish."""
    contact_cut = -1
    m = _CONTACT_LINE_RE.search(line)
    if m:
        contact_cut = m.start()
        # If the marker starts mid-word (the "@" inside "admin@x.com"),
        # back up to the start of that word so a stray fragment ("admin")
        # isn't left dangling in the kept prefix. Any of the usual
        # separators counts as a word boundary, not just a space - real
        # addresses are as often comma-separated ("...400001,UDYAM...")
        # as space-separated.
        if contact_cut > 0 and not line[contact_cut - 1] in " ,.-":
            wb = max(line.rfind(c, 0, contact_cut) for c in " ,.-")
            contact_cut = wb + 1 if wb != -1 else 0

    # _first_city_pos already returns the start of a clean token (the regex
    # it scans with requires a letter first), so it never needs the same
    # backing-up treatment.
    city_cut = _first_city_pos(line)

    positions = [p for p in (contact_cut, city_cut) if p != -1]
    if not positions:
        return line
    return line[:min(positions)].strip(" ,.-")


def _cap(text, limit=50):
    """Hard-cap a string to `limit` characters - Business Central's
    Address/Address 2 fields are Text50, so a value any longer gets
    rejected or silently truncated downstream regardless. This is a
    safety net after the noise-stripping above (which already removes
    most of what pushes a line past 50), not a substitute for it. Breaks
    at the last space inside the limit when there is one, so a long line
    isn't cut mid-word."""
    text = text or ""
    if len(text) <= limit:
        return text
    cut = text.rfind(" ", 0, limit)
    return (text[:cut] if cut > 0 else text[:limit]).rstrip(" ,.-")


def _looks_garbled(text):
    """True when `text` is mostly OCR noise rather than real words - a run
    of 4+ of the same letter in a row (e.g. "TAAAAAAAH"), which genuine
    name/address text never contains, but a low-confidence OCR read of
    faint/small print sometimes hallucinates in its place.
    Sample: _looks_garbled('TAAAAAAAH IAAAAA') -> True"""
    return bool(re.search(r"([A-Za-z])\1{3,}", text or ""))


_PIN_IN_LINE_RE = re.compile(r"\b\d{6}\b|\b\d{3}\s\d{3}\b|\bpin\b", re.IGNORECASE)


_ENTITY_WORDS = {"private", "pvt", "limited", "ltd", "llp", "inc", "corp",
                 "corporation", "co", "company"}


def _is_label_only_line(line):
    """A caption with no value after it ("/Invoice From:", "Bill To:",
    "sent to:") or nothing but a company-suffix ("PRIVATE LIMITED") - a
    line that names nothing about WHERE."""
    s = (line or "").strip()
    if re.fullmatch(r"[\W_]*[A-Za-z/&. ]{0,25}:\s*", s):
        return True
    words = re.findall(r"[a-z]+", s.lower())
    return bool(words) and all(w in _ENTITY_WORDS for w in words)


def _same_party(line, name):
    """True when `line` is (a copy of) the party's own company name - a
    letterhead prints it above the address, and OCR sometimes folds that
    line into the address block ("HEWLETT PACKARD ENTERPRISE INDIA" under
    "Hewlett Packard Enterprise", "PRECISION INFOMATIC (MADRAS)S" under
    "Precision Infomatic (M) Pvt Ltd")."""
    a = re.sub(r"[^a-z0-9]", "", (line or "").lower())
    b = re.sub(r"[^a-z0-9]", "", (name or "").lower())
    if len(a) < 8 or len(b) < 8:
        return False
    if a in b or b in a:
        return True
    return difflib.SequenceMatcher(None, a, b).ratio() >= 0.75


def _strip_postal_tail(piece, state_name=""):
    """Remove what has its own column from an address piece: the PIN code
    ("600017", "600 017", "PIN Code 382424"), the state name, a "(GJ - 24)"
    style state tag and the country - "TAMIL NADU -600017" or "Gujarat
    (GJ - 24). PIN Code 382424." leave nothing behind, while a genuine
    street piece passes through untouched."""
    p = piece or ""
    p = re.sub(r"\bpin\b(?:\s*code\b)?\.?\s*:?", "", p, flags=re.IGNORECASE)
    p = re.sub(r"\b\d{6}\b|\b\d{3}\s\d{3}\b", "", p)
    p = re.sub(r"\(\s*[A-Za-z]{2}\s*-?\s*\d{1,2}\s*\)", "", p)
    p = re.sub(r"\bindia\b", "", p, flags=re.IGNORECASE)
    if state_name:
        p = re.sub(r"\b" + re.escape(state_name) + r"\b", "", p, flags=re.IGNORECASE)
    return p.strip(" ,.-–")


def _address_lines(address, state_name="", name=""):
    """Split a possibly multi-line address into (line1, line2), trimming
    away any phone/GST/email/website/Udyam contact info found on a line
    rather than an actual address line, and capping each to BC's 50-char
    Address/Address 2 field limit."""

    pieces, city = _address_pieces(address, state_name, name)
    if not pieces and not city:
        return "", ""
    # Compared on letters/digits only, so "T Nagar" isn't added again after
    # a piece that already says "T.Nagar" / "T-NAGAR".
    squash = lambda s: re.sub(r"[^a-z0-9]", "", (s or "").lower())
    if city and squash(city) not in squash(" ".join(pieces)):
        pieces.append(city)

    line1 = ""
    idx = 0
    while idx < len(pieces):
        candidate = f"{line1}, {pieces[idx]}" if line1 else pieces[idx]
        if len(candidate) > 50 and line1:
            break
        line1 = candidate
        idx += 1
        if len(line1) >= 50:
            break
    line2 = ""
    for piece in pieces[idx:]:
        candidate = f"{line2}, {piece}" if line2 else piece
        if len(candidate) > 50 and line2:
            break
        line2 = candidate

    return _cap(line1), _cap(line2)


def _address_pieces(address, state_name="", name=""):
    """(street/area pieces, city) of an address - the printed lines cut at
    the PIN code line, with contact/landmark noise, PIN, state and country
    stripped. `pieces` is empty when the address states nothing but a city
    (and PIN/state), i.e. no street or area of its own."""
    if not address:
        return [], ""

    # OCR sometimes glues neighbouring words into one long token with no
    # space ("TowerNear", "StationNehru") - split a lowercase->Capitalised
    # seam inside any 9+ letter token so landmark/contact cutting below can
    # see the word boundary. 9+ keeps ordinary names ("McDonald") intact.
    address = re.sub(
        r"[A-Za-z]{9,}",
        lambda m: re.sub(r"(?<=[a-z])(?=[A-Z][a-z])", " ", m.group()),
        address,
    )
    raw_lines = [ln.strip() for ln in address.split("\n") if ln.strip()]

    # The postal address ends on the line carrying its PIN code - whatever
    # follows is contact/bank/registration text ("Bank Details.", "Account
    # Number: ...", "India", a repeat of the address from the next page).
    for i, ln in enumerate(raw_lines):
        if _PIN_IN_LINE_RE.search(ln):
            raw_lines = raw_lines[:i + 1]
            break

    lines = [s for s in (_strip_contact(ln) for ln in raw_lines) if s]
    lines = [s for s in lines if not _is_label_only_line(s) and not _same_party(s, name)]

    # _strip_contact cuts a line at its own city name on the assumption
    # there's real street content earlier on that same line worth keeping
    # instead ("...Road, Chennai" -> "Road"). Some simple invoice templates
    # print ONLY "Chennai 600017" as the whole address - nothing else was
    # ever stated to fall back to - so stripping the city there empties
    # the address entirely, discarding the only address content that
    # exists. Fall back to the untouched raw lines rather than lose real
    # (if minimal) address data just because it happens to start with a
    # recognized city name.
    if not lines and raw_lines:
        lines = raw_lines

    # Address 1 alone is what most screens show, and a lone street line
    # ("C-202, PARMESHWAR-3", "5th Main Road") says nothing about WHERE - so
    # the city is added as the last piece, and every piece is packed into
    # Address 1 up to BC's 50-char limit (overflow spills into Address 2)
    # rather than each source line getting a slot of its own.
    pieces = []
    carry = ""
    for p in lines:
        p = _strip_postal_tail(p, state_name)
        p = re.sub(r"\s*,(?:\s*,)+", ",", p)
        # A lone letter glued straight onto a comma ("HABIBULLAH RD,E") is
        # an OCR artifact; ", T" (comma + space) is a split place name and
        # is handled by the carry-over below instead.
        p = re.sub(r",[A-Za-z]$", "", p).strip(" ,.-")
        if not p:
            continue
        # A lone "No"/"No." is the wrapped caption of a door number, not a place.
        if re.fullmatch(r"(?i)no\.?", p):
            continue
        # A place name split across two printed lines ("...Habibullah Road,
        # T" / "Nagar") - a 1-2 letter tail belongs with the next piece.
        if carry:
            p = f"{carry} {p}"
            carry = ""
        if (len(p) <= 2 and p.isalpha()) or re.search(r"[\s,.][A-Za-z]$", p):
            carry = p
            continue
        pieces.append(p)
    if carry:
        # Nothing followed to complete a trailing lone letter ("...ROAD,
        # T" / "...RD,E") - a dangling OCR remnant, not a real word.
        carry = re.sub(r"[\s,.]+[A-Za-z]$", "", carry).strip(" ,.-")
        if carry:
            pieces.append(carry)
    return pieces, _city(" ".join(raw_lines), state_name)


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


def _reconcile_line(qty, rate, amount, tax_pct, allow_misread=True):
    """Make one line's Quantity / Rate / Amount agree with each other,
    using only the line's OWN numbers plus its GST rate. Returns
    (qty, rate, amount), unchanged unless a specific, provable misread is
    found:
      - Amount includes the line's GST (Amount == qty*rate*(1+gst)): the
        pre-tax qty*rate is the taxable Line Amount / GST Base Amount.
      - Rate includes the GST (Amount == qty*rate/(1+gst)): Amount is the
        taxable value, so the pre-tax unit price is Amount/qty.
      - Rate is some other number on the row (a tax amount, a date
        fragment - never explained by tax): qty*rate falls well SHORT of
        Amount, which a discount can never cause (a discount only lowers
        Amount). Either Quantity was misread (Amount is an exact whole
        multiple of Rate) or the Rate was (then Amount/qty).
    A qty*rate that exceeds Amount without a tax explanation is left alone -
    that is an ordinary line discount."""
    if amount == 0 and rate and rate > 0:
        # A line the invoice itself prices at 0.00 (a free / bundled item)
        # has a 0 unit cost - whatever else sat under the Rate column
        # (an end date, a tax cell) was never its price.
        return qty, 0.0, amount
    if not qty or qty <= 0 or not amount or amount <= 0:
        return qty, rate, amount
    if rate is None or rate <= 0:
        return qty, rate, amount
    tol = max(0.5, 0.0005 * amount)
    if abs(qty * rate - amount) <= tol:
        return qty, rate, amount
    # The line's own GST rate when known, and every standard slab besides -
    # a GST cell printed without a "%" ("18") leaves the rate unknown here,
    # but the numbers themselves still expose a tax-inclusive figure.
    slabs = [tax_pct] if tax_pct and 0 < tax_pct <= 40 else []
    slabs += [s for s in (5, 12, 18, 28) if s not in slabs]
    tol_t = max(tol, 0.001 * amount)
    for s in slabs:
        f = 1 + s / 100.0
        if abs(qty * rate * f - amount) <= tol_t:
            return qty, rate, round(qty * rate, 2)
        if abs(qty * rate / f - amount) <= tol_t:
            return qty, round(amount / qty, 2), amount
    if allow_misread and qty * rate < amount * 0.98:
        ratio = amount / rate
        whole = round(ratio)
        if qty == 1 and 2 <= whole <= 100 and abs(ratio - whole) <= 0.005:
            return float(whole), rate, amount
        return qty, round(amount / qty, 2), amount
    return qty, rate, amount


def _plausible_rate(value):
    """A number that could genuinely be a GST rate (India's slabs top out
    at 28%; nothing legitimate reaches even 40%). The tax-summary table
    parser (see ocr_engine.py's own build) assigns a bare, unlabelled
    number to the Rate column purely by its x-position when a layout has
    no separate Rate/Amount columns of its own (e.g. a plain "OUTPUT IGST
    1012.5" line, just the rupee amount) - without this check, that
    amount is trusted outright as if it were a percentage."""
    return value is not None and 0 < value <= 40


def _tax_rate(tax_total):
    """Pull the GST rate (e.g. 18.0) from the first tax-summary row."""

    for row in tax_total:
        igst = _num(row.get("IGST_Rate_%"))
        if _plausible_rate(igst):
            return igst
        # Intra-state: the item's GST rate is CGST + SGST (e.g. 9% + 9% = 18%),
        # not a single component.
        cgst = _num(row.get("CGST_Rate_%"))
        sgst = _num(row.get("SGST_Rate_%"))
        if _plausible_rate(cgst) and _plausible_rate(sgst):
            return cgst + sgst
        if _plausible_rate(cgst):
            return cgst
        if _plausible_rate(sgst):
            return sgst
    return None


def _tax_amount(tax_total):
    """Total GST amount actually printed on the invoice (IGST, or
    CGST+SGST summed) - used to reconcile whether freight was taxed (see
    the items loop below). None if the summary table has no usable
    amount at all.

    A table with more than one HSN/rate group usually also prints its
    own "Total" row restating the combined figure - use ONLY that row
    when present, rather than summing every row, or a table that
    already includes its own total would get double-counted (a
    single-group table's "Total" row just repeats the one HSN row's own
    figures, so this still gives the right answer either way)."""

    total_row = next(
        (row for row in tax_total if (row.get("HSN") or "").strip().lower() == "total"),
        None,
    )
    if total_row is not None:
        tax_total = [total_row]

    total = 0.0
    found = False
    for row in tax_total:
        igst = _num(row.get("IGST_Amount"))
        if igst:
            total += igst
            found = True
            continue
        cgst = _num(row.get("CGST_Amount"))
        sgst = _num(row.get("SGST_Amount"))
        if cgst or sgst:
            total += (cgst or 0) + (sgst or 0)
            found = True
    return total if found else None


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


# A page-footer note bleeding into the last item's description because the
# table continues past a page break ("...LENOVO continued to page number 2
# This is a Computer Generated Invoice") - generic boilerplate seen across
# vendors, not any one template's quirk.
_ITEM_FOOTER_RE = re.compile(
    r"continued\s+to\s+page\s*(?:no\.?|number)?\s*\d+"
    r"|this\s+is\s+an?\s+computer[\s-]*generated\s+invoice",
    re.IGNORECASE,
)


# A per-visit/per-service-call "Case ID" (e.g. "INS/2026/07/07-868791") on
# a freelancer/service-call invoice's own case-log table (column header
# literally "CASE ID" - see "421 FREE..pdf"/"701 FRE..pdf") - that table's
# rows are mostly case metadata (Name, Call Status, Pan Card, ...) with no
# real per-line description of the WORK the way a normal item table has,
# so once that metadata is filtered out (see ocr_engine.py's EXCLUDE_KW)
# what's left is fragments of the case IDs themselves - the only
# meaningful content actually describing what was billed. Only applied
# when the invoice's own table genuinely has a "CASE ID" column - never a
# blanket rule for ordinary item descriptions, which can legitimately
# contain a part/serial number in this same rough shape.
# No trailing \b - a poor scan sometimes glues the ID straight onto the
# next word with no space at all (e.g. "INS/2026/07/08-869083th July",
# an ordinal-day marker bleeding in from the Date column right next to
# it) - \d{4,8} already stops at the first non-digit either way, so
# nothing but the ID's own digits is ever captured.
_CASE_ID_RE = re.compile(r"\b[A-Za-z]{2,6}/\d{4}/\d{2}/\d{2}-\d{4,8}")


def _case_ids_as_description(desc, has_case_id_column):
    if not has_case_id_column:
        return None
    ids = []
    for m in _CASE_ID_RE.finditer(desc or ""):
        if m.group(0) not in ids:
            ids.append(m.group(0))
    return ",".join(ids) if ids else None


def _clean_item_description(desc, serial):
    """Strip OCR noise from an item's own description: a leading echo of
    this row's serial number ('1. ', '2 ') - matched against the row's
    actual SI so a genuine leading measurement ('18.5" TFT...') is never
    mistaken for one - a trailing GST rate ('...ROLLER 18%', already
    captured as its own field before this runs), and page-footer
    boilerplate that bled in across a page break."""
    desc = str(desc or "").strip()
    if not desc:
        return desc
    desc = re.sub(rf"^{re.escape(str(serial))}\.?\s+", "", desc)
    m = _ITEM_FOOTER_RE.search(desc)
    if m:
        desc = desc[:m.start()]
    desc = re.sub(r"\s*\d{1,2}(?:\.\d+)?\s*%\s*$", "", desc)
    return desc.strip(" ,.-")


_CGST_SGST_ROW_RE = re.compile(
    r"(\d{1,2}(?:\.\d+)?)\s*%\s+[\d,]+\.\d{2}\s+(\d{1,2}(?:\.\d+)?)\s*%\s+[\d,]+\.\d{2}"
)


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

    # A tax-summary TABLE ROW under "CGST Rate Amount | SGST Rate Amount"
    # column headers ("998713 900.00 9% 81.00 9% 81.00 162.00") - no GST
    # keyword sits on the row itself, but two equal plausible percentages
    # each followed by their own amount are the CGST and SGST halves.
    for m in _CGST_SGST_ROW_RE.finditer(text):
        r1, r2 = float(m.group(1)), float(m.group(2))
        if r1 == r2 and _plausible_rate(r1 + r2):
            return r1 + r2

    return _rate_near(text, "gst")


_AMOUNT_RE = re.compile(r"[\d,]+\.\d{2}")


def _gst_amount_from_text(text):
    """Best-effort total GST amount read straight from the OCR text (the
    last currency-shaped number - i.e. with 2 decimal places, so it's
    never confused with a bare rate like "18" - on a line mentioning
    IGST/CGST+SGST/GST). Used when the structured tax-summary table has
    no usable amount either (some layouts print the rate/amount as a
    plain text line - e.g. "Output IGST@18%  18 %  219.60" - not a table
    this code recognises)."""

    if not text:
        return None

    def _amount_near(keyword):
        for line in text.splitlines():
            if keyword in line.lower():
                nums = _AMOUNT_RE.findall(line)
                if nums:
                    return _num(nums[-1])
        return None

    igst = _amount_near("igst")
    if igst:
        return igst

    cgst = _amount_near("cgst")
    sgst = _amount_near("sgst")
    if cgst and sgst:
        return cgst + sgst
    if cgst or sgst:
        return cgst or sgst

    gst_amt = _amount_near("gst")
    if gst_amt:
        return gst_amt
    # "Total Tax Amount INR 360.00" - the total states itself as TAX with no
    # GST/IGST keyword on the line.
    return _amount_near("total tax amount")


# A right-scan for Invoice No. can occasionally run past its own value into
# a date sitting further along the same OCR row with no label in between to
# stop at (e.g. "Invioce No: 1413    03.08.2026", where the trailing date
# belongs to a different column entirely) - strip a trailing date pattern
# rather than keeping it glued onto the number.
_TRAILING_DATE_RE = re.compile(r"\s+\d{1,2}[./-]\d{1,2}[./-]\d{2,4}\s*$")


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

    seller_addr = fields.get("Seller Address", "")
    buyer_addr = fields.get("Buyer Address", "")
    consignee_addr = fields.get("Consignee Address", "")

    # A garbled buyer address line (OCR noise, not real words) is never
    # content worth keeping - treated as if nothing was read at all, so it
    # falls through to the normal missing-address handling (parked for a
    # human to check) rather than either inventing a replacement value or
    # passing the garbage through as if it were genuine.
    if buyer_addr and _looks_garbled(buyer_addr):
        buyer_addr = ""

    if not seller_state_name and seller_state_code in STATE_CODES:
        seller_state_name = STATE_CODES[seller_state_code][0]
    if not buyer_state_name and buyer_state_code in STATE_CODES:
        buyer_state_name = STATE_CODES[buyer_state_code][0]
    if not consignee_state_name and consignee_state_code in STATE_CODES:
        consignee_state_name = STATE_CODES[consignee_state_code][0]

    # Last-resort fallback for a non-GST vendor (no GSTIN to derive a state
    # code from at all) whose template also has no dedicated "State:"
    # field of its own for one party - e.g. C I T's own address line
    # "Tal:, Bhiwandi, Maharashtra 421101" states the seller's state
    # in-line, with no separate "State:" label anywhere for the seller
    # (only the buyer's own gets one). Only tried when everything else
    # above came up empty; a plain state-name search of the address text.
    if not seller_state_name and seller_addr:
        seller_state_name, seller_state_code = (
            _state_from_text(seller_addr) or (seller_state_name, seller_state_code)
        )
    if not buyer_state_name and buyer_addr:
        buyer_state_name, buyer_state_code = (
            _state_from_text(buyer_addr) or (buyer_state_name, buyer_state_code)
        )
    if not consignee_state_name and consignee_addr:
        consignee_state_name, consignee_state_code = (
            _state_from_text(consignee_addr) or (consignee_state_name, consignee_state_code)
        )

    # Still nothing (no GSTIN, no recognizable state/city name anywhere in
    # the address) but a PIN code is stated - e.g. "701 FRE..pdf"'s own
    # "Gt 23 nearby Technosphire Public school, 93 Noida 201301" (a city
    # not in _MAJOR_CITY_TO_STATE_CODE's short list), or a seller whose own
    # "State Name" field just says "Not Applicable" with only a PIN code
    # printed alongside it ("Zaco Computer.pdf"'s own "State Name : Not
    # Applicable | 600033" - genuinely no readable GSTIN either) - the
    # state name's own raw text is searched too, not just the address, so
    # this case isn't missed. Tried last since it only covers PIN prefixes
    # that map to one state unambiguously - see _state_from_pincode's own
    # docstring.
    if not seller_state_name and (seller_addr or fields.get("Seller State Name")):
        seller_state_name, seller_state_code = (
            _state_from_pincode(_pincode(seller_addr, fields.get("Seller State Name", "")))
            or (seller_state_name, seller_state_code)
        )
    if not buyer_state_name and (buyer_addr or fields.get("Buyer State Name")):
        buyer_state_name, buyer_state_code = (
            _state_from_pincode(_pincode(buyer_addr, fields.get("Buyer State Name", "")))
            or (buyer_state_name, buyer_state_code)
        )
    if not consignee_state_name and (consignee_addr or fields.get("Consignee State Name")):
        consignee_state_name, consignee_state_code = (
            _state_from_pincode(_pincode(consignee_addr, fields.get("Consignee State Name", "")))
            or (consignee_state_name, consignee_state_code)
        )

    # Last-resort buyer-state fallback: the buyer on every invoice this
    # tool processes is Precision Techserve's own Chennai office, which
    # never moves - unlike the seller (genuinely different per vendor,
    # so it gets no such default), a blank buyer state here only ever
    # means the vendor's layout didn't print a Buyer GSTIN/state/address
    # at all, never that the buyer was actually in some other state.
    if not buyer_state_code:
        buyer_state_code = "33"
    if not buyer_state_name:
        buyer_state_name = STATE_CODES[buyer_state_code][0]

    seller_a1, seller_a2 = _address_lines(
        seller_addr, seller_state_name, fields.get("Seller Name", ""))
    buyer_a1, buyer_a2 = _address_lines(
        buyer_addr, buyer_state_name, fields.get("Buyer Name", ""))
    consignee_a1, consignee_a2 = _address_lines(
        consignee_addr, consignee_state_name,
        fields.get("Consignee Name", "") or fields.get("Buyer Name", ""))

    seller_pin = _pincode(seller_addr, seller_a1, seller_a2)
    buyer_pin = _pincode(buyer_addr)
    consignee_pin = _pincode(consignee_addr)

    # A PIN code the party's own address block lost (a bare "110092" line
    # under the letterhead, or none printed there at all) is still read off
    # the page's header text when it is unambiguous: exactly one 6-digit
    # number ahead of the item table whose first two digits belong to THAT
    # party's own state, and which no other party already owns.
    header_pins = _header_pincodes(result.get("Text") or "")

    def _pin_from_header(state_code, taken):
        picks = {
            p for p in header_pins
            if p not in taken
            and _PINCODE_PREFIX_TO_STATE_CODE.get(p[:2]) == state_code
        }
        return next(iter(picks)) if len(picks) == 1 else ""

    if not seller_pin and seller_state_code:
        seller_pin = _pin_from_header(
            seller_state_code, {buyer_pin, consignee_pin})
    if not buyer_pin and buyer_state_code:
        buyer_pin = _pin_from_header(buyer_state_code, {seller_pin})
    if not consignee_pin and consignee_state_code:
        consignee_pin = _pin_from_header(consignee_state_code, {seller_pin})

    _, seller_short = STATE_CODES.get(seller_state_code, ("", ""))
    _, buyer_short = STATE_CODES.get(buyer_state_code, ("", ""))

    consignee_city = _city(consignee_addr, consignee_state_name)

    order_no = fields.get("Buyer's Order No.", "")

    # SERVICE invoices never call Service First (see service_api.py), so
    # the NAV vendor code has no automatic source for them - the user
    # instead writes "Nav Vendor Code : <code>" by hand on a paper, which
    # is then scanned into the PDF alongside the invoice, read here like
    # any other labelled field off that scan. For PART this is overwritten
    # by the real SF value once enrich_invoice runs; harmless if a PART
    # template happens to print the same label too.
    #
    # Handwriting is often poorly OCR'd - the code's own fixed shape (see
    # vendor_code.py) lets a garbled reading usually be corrected against
    # the vendor's own (already-extracted) name rather than trusted as-is.
    vendor_code_raw = fields.get("Vendor Code", "").strip()
    # The "Code" half of the caption is itself frequently OCR-garbled
    # beyond recognition ("Cxde", "cde", ...), which keeps the labelled-
    # field anchor above (needs literal "code") from ever firing at all -
    # same gap buyer_order.py's free-text PO scan fills for a garbled
    # SPRPUR prefix. Only tried when the anchor came up empty.
    if not vendor_code_raw:
        vendor_code_raw = vendor_code_lib.find_vendor_code_line(result.get("Text", ""))
    vendor_code, vendor_code_confidence = vendor_code_lib.correct_vendor_code(
        vendor_code_raw, fields.get("Seller Name", "")
    )
    vendor_code_doubtful = vendor_code_confidence == "doubtful"

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

    # A PO number is a structured code, never genuinely containing internal
    # whitespace - an anchor-extracted value that already "looks like" a
    # valid SPRPUR PO (e.g. "SPRPUR/2026/09/04 - 86476", spaces around the
    # trailing hyphen from how the PDF prints it) skips the free-text
    # rescan above entirely, so it would otherwise keep those spaces as-is.
    # buyer_order.find_buyer_order_no's own matches are already whitespace-
    # stripped (see its _normalise); strip here too so every path agrees.
    order_no = re.sub(r"\s+", "", order_no)

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
    if rate is None:
        # No percentage printed anywhere, only the tax AMOUNT (e.g. "Output
        # IGST - TN | 3,060.00" under a 17,000.00 line): the invoice's own
        # arithmetic gives the rate. Only accepted when it lands on a real
        # GST slab, so a stray number can't invent one.
        stated_tax = _tax_amount(tax_total)
        if stated_tax is None:
            stated_tax = _gst_amount_from_text(result.get("Text", ""))
        taxable = sum(
            _num(it.get("Amount")) or 0 for it in ocr_items if not it.get("charge")
        )
        if stated_tax and taxable:
            implied = stated_tax / taxable * 100
            slab = min((0.1, 0.25, 3, 5, 12, 18, 28, 40), key=lambda s: abs(s - implied))
            if slab in (3, 5, 12, 18, 28) and abs(slab - implied) <= 0.3:
                rate = float(slab)

    # Intra-state (seller & buyer in the same state) GST is split into equal
    # CGST + SGST halves. Some layouts print / parse only one half — promote a
    # recognised half-rate to its full slab so the item's GST % is the total.
    _HALF_TO_FULL = {2.5: 5.0, 6.0: 12.0, 9.0: 18.0, 14.0: 28.0}
    if (rate and seller_state_code and seller_state_code == buyer_state_code):
        rate = _HALF_TO_FULL.get(rate, rate)

    rate_str = f"{rate:g}%" if rate else ""

    # Only this kind of case-log template (a "CASE ID" table column of its
    # own) gets its item description rebuilt from the case IDs - see
    # _case_ids_as_description's own docstring. An ordinary item table
    # never has this column, so this never fires for one.
    has_case_id_column = "case id" in (result.get("Text", "") or "").lower()

    no_gst_charged = (
        not str(fields.get("Seller GSTIN/UIN", "") or "").strip()
        and _tax_amount(tax_total) is None
        and _gst_amount_from_text(result.get("Text", "")) is None
        and not rate
    )

    # ---- items -----------------------------------------------------------

    items = []
    for i, it in enumerate(ocr_items, start=1):

        try:
            serial = int(it.get("SI"))
        except (TypeError, ValueError):
            serial = i

        qty_n = _num(it.get("Quantity"))
        amt_n = _num(it.get("Amount"))

        # A service/labour line often has no explicit Quantity at all -
        # either the cell is genuinely blank while Amount/Rate are filled
        # in (e.g. Ideal Systems' "Service Charges Received" leaves both
        # Quantity and Rate blank), or the invoice's table has no Qty
        # column whatsoever (e.g. TPMGuru's "L1 Engineer" table only has
        # Price/Unit and Amount). That's not missing data, it's the
        # invoice stating "one instance of this service" - the same
        # convention a Charge (freight) line already gets unconditionally
        # (see ocr_engine's charge-row handling: Quantity=1, Rate=Amount).
        # Only kicks in when Quantity itself wasn't read at all and Amount
        # was, so a genuine multi-unit line whose Quantity merely failed
        # to extract despite a real Qty column being present is never
        # silently masked - it stays flagged, since guessing wrong there
        # (unlike the safe "1" here) would corrupt real quantity data.
        if qty_n is None and amt_n is not None:
            qty_n = 1.0
            if not it.get("Rate"):
                it["Rate"] = amt_n
        raw_desc = it.get("Description", "")
        is_charge = bool(it.get("charge"))

        # The invoice-level rate covers layouts with a proper tax-summary
        # table or a general "IGST/CGST+SGST NN%" line in the OCR text. Some
        # layouts instead print the rate trailing the item's own description
        # ("... PICKUP ROLLER 18%") with nothing recognisable elsewhere on
        # the page - fall back to that per item when the invoice-level rate
        # came back blank, rather than leaving GST % empty. Reads the raw
        # description (before _clean_item_description below strips that
        # same "18%" out as noise).
        #
        # A freight/courier charge line never gets this invoice-level rate
        # at all - that rate is the GOODS tax table's rate, which routinely
        # differs from (or simply doesn't apply to) freight. Only a
        # percentage actually printed on the charge line's own text counts;
        # when the PDF doesn't state one there, the correct reading is 0%,
        # not silently inheriting whatever the items were taxed at.
        if is_charge:
            # ocr_engine's charge-row parser captures a rate printed as its
            # own cell on that row (e.g. "Freight Outward | 9968 | 18 | % |
            # 120.00") separately, since that number gets excluded from the
            # description text there (it's a valid number, not label text)
            # - prefer it over re-scanning the description for the same
            # thing, which would only find it if it happened to survive
            # into the text.
            item_rate = it.get("ChargeRatePercent") or _rate_near(raw_desc, "") or 0
        else:
            # TaxRatePercent (see ocr_engine's is_gst3_breakdown) is this
            # exact item's own row-level CGST+SGST+IGST rate, read straight
            # off a grouped tax-breakdown table with no separate tax-
            # summary block for the invoice-level `rate` fallback to find
            # (e.g. R-Logic/SAP Business One's own layout) - preferred over
            # the invoice-level rate when present, since it's specific to
            # this line rather than assumed to apply to every line.
            item_rate = it.get("TaxRatePercent")
            if not item_rate:
                item_rate = rate if rate else _rate_near(raw_desc, "")
        # (keyword="" matches every line - the description is what we're
        # scanning here, not a labelled tax-summary line.)

        desc = _clean_item_description(raw_desc, serial)
        case_desc = _case_ids_as_description(raw_desc, has_case_id_column)
        if case_desc:
            desc = case_desc

        rate_n = _num(it.get("Rate"))
        # A charge line only gets the tax-inclusive check (its Rate/Amount
        # follow their own "1 unit at the stated amount" convention).
        qty_n, rate_n, amt_n = _reconcile_line(
            qty_n, rate_n, amt_n, _num(item_rate), allow_misread=not is_charge)
        it = dict(it)
        it["Rate"] = rate_n
        it["Amount"] = amt_n

        items.append({
            "sl_no": serial,
            "Description": desc,
            "hsn": it.get("HSN") or "",
            # Unit price: use whatever OCR read from the table's own Rate
            # column, or - when that column wasn't recognised/positioned
            # cleanly - derive it from Amount / Quantity. Amount = Rate x
            # Quantity always holds for a line item, so this is an exact
            # recovery, not a guess.
            "rate": _money(it.get("Rate")) or (
                _money(amt_n / qty_n)
                if amt_n is not None and qty_n
                else ""
            ),
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
            # item_rate can be a legitimate 0 for a charge line (see above) -
            # `item_rate else ...` would wrongly collapse that back to blank
            # (0 is falsy), so charge lines get an explicit "0" rather than
            # falling through to the same "" an Item line's true unknown
            # rate uses.
            "TaxPercentage": (
                item_rate if item_rate else ("0" if (is_charge or no_gst_charged) else "")
            ),
            # A vendor with no GSTIN that states no tax anywhere charged
            # none (an unregistered vendor can't) - that's a genuine 0%,
            # so GST Group Code ("required", "System") still resolves for
            # its lines instead of sitting blank forever.
            "HSN_Percentage_Description": (
                f"Goods {item_rate:g}%" if item_rate
                else ("Goods 0%" if (no_gst_charged and not is_charge) else "")
            ),
            "PartSpecification": desc,
            # Seed from the PDF's own table HSN/SAC column so it's used
            # whenever the Service First HSN lookup doesn't return a match
            # for this line (enrich_items overwrites it when SF does).
            "ProductNo": it.get("HSN") or "",
            "HSN_Type": "Goods",
            "Nav_Item_No": "",
        })

    # ---- Freight/courier GST %: reconcile against the invoice's own
    # stated tax amount, not just what's printed on the charge line's own
    # row -------------------------------------------------------------
    # A rate printed right on the freight row (or its absence) can be
    # right, wrong, or lost to OCR/layout noise - but the invoice's own
    # arithmetic never lies. Test both possibilities against the actual
    # tax amount the PDF states: goods taxed alone, or goods+freight
    # taxed together. Whichever reproduces the real number decides every
    # charge line's rate for this invoice, overriding the per-line
    # guess above (only when the check is actually possible - an
    # invoice with no parseable tax-summary amount, or no freight line
    # at all, keeps the per-line result as-is).
    actual_tax = _tax_amount(tax_total)
    if actual_tax is None:
        actual_tax = _gst_amount_from_text(result.get("Text", ""))
    if rate and actual_tax is not None:
        goods_total = sum(
            _num(it2["Amount"]) or 0 for it2 in items if not it2["_charge"]
        )
        freight_total = sum(
            _num(it2["Amount"]) or 0 for it2 in items if it2["_charge"]
        )
        if freight_total:
            candidate_0 = round(goods_total * rate / 100, 2)
            candidate_full = round((goods_total + freight_total) * rate / 100, 2)
            diff_0 = abs(actual_tax - candidate_0)
            diff_full = abs(actual_tax - candidate_full)
            # A real match reproduces the stated tax almost exactly - only
            # off by paisa-level rounding, never rupees. Require that
            # before trusting either candidate; a table with its own
            # parsing quirks (e.g. a CGST+SGST layout that only captured
            # one half, so actual_tax reads roughly half of the true
            # figure) can otherwise still be numerically "closer" to one
            # candidate than the other by sheer chance, which isn't a
            # genuine reconciliation - falling through to keep the
            # per-line result (the safer 0-unless-stated default) is
            # better than trusting a coincidence.
            TOLERANCE = 2.0
            resolved_rate = None
            if diff_0 <= TOLERANCE or diff_full <= TOLERANCE:
                resolved_rate = rate if diff_full < diff_0 else 0
            if resolved_rate is not None:
                for it2 in items:
                    if it2["_charge"]:
                        it2["TaxPercentage"] = resolved_rate if resolved_rate else "0"
                        it2["HSN_Percentage_Description"] = (
                            f"Goods {resolved_rate:g}%" if resolved_rate else ""
                        )

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
    # A Ship-to block that states nothing but a city (its street lines were
    # lost or never printed there, e.g. "CHENNAI / 600017 Tamil Nadu") says
    # nothing about WHERE - the Bill-to block's full address is the better
    # Ship-to in that case, same as when there's no consignee block at all.
    if buyer_addr:
        consignee_street, _ = _address_pieces(
            consignee_addr, consignee_state_name,
            fields.get("Consignee Name", "") or fields.get("Buyer Name", ""))
        buyer_street, _ = _address_pieces(
            buyer_addr, buyer_state_name, fields.get("Buyer Name", ""))
        if not consignee_street and buyer_street:
            consignee_a1, consignee_a2 = buyer_a1, buyer_a2
            consignee_pin = consignee_pin or buyer_pin
    if not str(consignee_gstin_v).strip():
        consignee_gstin_v = buyer_gstin_v

    # ---- assembled schema ------------------------------------------------

    data = {
        "invoice_no": _TRAILING_DATE_RE.sub("", fields.get("Invoice No.", "") or "").strip(),
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
        # Nav_VendorCode and Pay_to_Vendor_No are always the same value -
        # for PART, Service First's own response maps to both (see
        # service_api.py's _HEADER_FROM_SF); SERVICE has no SF at all, so
        # both get the one hand-written code directly.
        "Nav_VendorCode": vendor_code,
        "Pay_to_Vendor_No": vendor_code,
        "vendor_code_doubtful": vendor_code_doubtful,
        "Location_Code": "",
        "Location_State_Code": "",
        "GST_Order_Address_State": "",
        "Buy_from_Vendor_No": "",
        "Vendor_Invoice_No": "",
        "Currency_Code": "",
        "Gen_Bus_Posting_Group": "",
        "GST_Vendor_Type": "",
        "PaymentTermsName": "",
        # 1-based, inclusive - which of the source PDF's own physical pages
        # this invoice actually came from (see ocr_engine.py's _merge_group).
        # Lets the PDF viewer/download show this invoice's whole span for a
        # genuine multi-page invoice, not just a single (wrong) page - see
        # database.py's _invoice_list and app.py's /api/invoices/pdf.
        "_page_start": result.get("PageStart") or 1,
        "_page_end": result.get("PageEnd") or 1,
    }

    return data
