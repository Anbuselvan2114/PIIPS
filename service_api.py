"""
Service First / NAV backend integration for PIIPS.

Supplies two things the OCR alone cannot:

  * Reservation Entries (sf_items) — the serial / part rows already received
    in Service First for an invoice's purchase order, fetched from
    /api/purchase/GetSparePurchaseItem (keyed by the invoice's buyer_order_no).
  * HSN / item master details — /api/Purchase/GetHSNDetails, used to enrich
    each line item with its Navision item no., HSN type and tax percentage.

The base URL comes from config (sf_api_url); when it isn't configured the
lookups are skipped and the invoice is processed without reservation rows.

`enrich_invoice(data)` runs both lookups for one invoice dict, applies the
Service First defaults, and returns a small verdict:

    {"status": "processed" | "pending in SF", "is_active": bool,
     "errors": [reason, ...]}

which the processor uses to stamp tbl_Purchase_Tracker and to surface errors
on the dashboard.
"""

import re
from datetime import datetime

import requests
from rapidfuzz import fuzz

import config_store


# One reusable session for all backend calls.
_session = requests.Session()

_SPARE_PATH = "/api/purchase/GetSparePurchaseItem"
_HSN_PATH = "/api/Purchase/GetHSNDetails"

_TIMEOUT = 60


def _base_url():
    return (config_store.load_config().get("sf_api_url") or "").rstrip("/")


def _clean(value):
    return str(value or "").strip().lower()


# Vendor invoice wording and Navision's own catalog description are never
# byte-identical ("1. HP LJ P1505 PICKUP ROLLER 18%" vs. Nav's "PICKUP
# ROLLER - HP1505") - word order and vendor-added noise (numbering, GST %,
# unit) differ even for the same part. token_set_ratio scores on the words
# each shares regardless of order/duplication, so it tolerates that noise
# while still catching a genuinely different part description.
_DESC_MISMATCH_THRESHOLD = 45


def _description_mismatch(nav_desc, pdf_desc):
    """True if `nav_desc` (Nav_Part_Description) doesn't look like it
    describes the same part as `pdf_desc` (the PDF's own item line). A
    blank nav_desc always counts as a mismatch - nothing to compare."""
    nav_desc = str(nav_desc or "").strip()
    pdf_desc = str(pdf_desc or "").strip()
    if not nav_desc:
        return True
    if not pdf_desc:
        return False  # nothing on our side to compare against - don't flag
    return fuzz.token_set_ratio(nav_desc.upper(), pdf_desc.upper()) < _DESC_MISMATCH_THRESHOLD


# ---------------------------------------------------------------------------
# Backend calls
# ---------------------------------------------------------------------------

def get_spare_purchase_items(order_numbers):
    """{order_no_lower: [reservation_row, ...]} for the given PO numbers.
    Returns {} on any error or when no API URL is configured.
    Sample: get_spare_purchase_items(['SPRPUR/2026/04/27-83650'])"""
    base = _base_url()
    if not base:
        return {}

    payload = [
        {"SpareRequestOrderNumber": str(n).strip()}
        for n in (order_numbers or []) if n and str(n).strip()
    ]
    if not payload:
        return {}

    try:
        resp = _session.post(base + _SPARE_PATH, json=payload, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - backend optional
        print(f"Spare purchase API error: {exc}")
        return {}

    if isinstance(data, dict):
        rows = data.get("result", data)
    else:
        rows = data
    if not isinstance(rows, list):
        return {}

    grouped = {}
    for row in rows:
        if isinstance(row, dict):
            key = _clean(row.get("SpareRequestOrderNumber"))
            if key:
                grouped.setdefault(key, []).append(row)
    return grouped


def get_hsn_details(hsn_items):
    """{(po_lower, part_lower): row} of HSN/item-master details. Returns {}
    on error or when no API URL is configured.
    Sample: get_hsn_details([{'PartSpecification': 'Oil Filter', 'PurchaseOrderNo': 'SPRPUR/2026/04/27-83650'}])"""
    base = _base_url()
    if not base or not hsn_items:
        return {}

    try:
        resp = _session.post(base + _HSN_PATH, json=hsn_items, timeout=_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # noqa: BLE001 - backend optional
        print(f"HSN details API error: {exc}")
        return {}

    if isinstance(data, dict):
        rows = (data.get("PartViewModelList") or data.get("Data")
                or data.get("Items") or [])
    elif isinstance(data, list):
        rows = data
    else:
        rows = []

    hsn_map = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        po = _clean(row.get("PurchaseOrderNo"))
        part = _clean(row.get("PartSpecification"))
        if po and part:
            hsn_map[(po, part)] = row
    return hsn_map


def apply_sf_item_defaults(rows):
    """Fill the Service First reservation defaults on each row (in place).
    Sample: apply_sf_item_defaults([{'Nav_Item_No': 'ITM-1001'}])"""
    today = datetime.now().strftime("%Y-%m-%d")
    for row in rows:
        row.setdefault("Positive", "TRUE")
        row.setdefault("Suppressed_Action_Msg", 1)
        row.setdefault("Quantity_Base", 1)
        row.setdefault("Quantity_Invoiced_Base", 1)
        row.setdefault("Qty_per_Unit_of_Measure", 1)
        row.setdefault("Quantity", 1)
        row["Creation Date"] = today
        row["Expected Receipt Date"] = today
    return rows


# ---------------------------------------------------------------------------
# Header field mapping from the first reservation row
# ---------------------------------------------------------------------------

_HEADER_FROM_SF = {
    "Nav_VendorCode": "Nav_VendorCode",
    "Pay_to_Vendor_No": "Nav_VendorCode",
    "Location_Code": "Location_Code",
    "Location_State_Code": "Location_State_Code",
    "GST_Order_Address_State": "GST_Order_Address_State",
    "Buy_from_Vendor_No": "Buy_from_Vendor_No",
    "Currency_Code": "Currency_Code",
    "Gen_Bus_Posting_Group": "Gen_Bus_Posting_Group",
    "PaymentTermsName": "PaymentTermsName",
}


def _hsn_lookup_items(data, order_no):
    """{'PartSpecification', 'PurchaseOrderNo'} rows this invoice needs
    looked up in GetHSNDetails - no API call, just what to ask for. Split
    out from _apply_hsn_map so a whole batch's items can be combined into
    one GetHSNDetails call instead of one per invoice."""
    items = []
    for item in data.get("items", []) or []:
        if item.get("_charge"):
            continue  # freight / courier lines aren't Navision parts
        desc = str(item.get("Description", "")).strip()
        if desc:
            items.append({"PartSpecification": desc, "PurchaseOrderNo": order_no})
    return items


def _apply_hsn_map(data, order_no, hsn_map):
    """Enrich this invoice's line items (item no., HSN, tax) from an
    already-fetched {(po_lower, part_lower): row} map - no API call."""
    # Only blank a not-yet-confirmed No. when SF integration is actually
    # active - build_invoice_json's PDF-HSN fallback is the real, intended
    # value when SF isn't configured at all (nothing to defer to), not a
    # placeholder standing in for an unconfirmed match.
    sf_active = bool(_base_url())
    items = data.get("items", []) or []
    for item in items:
        if item.get("_charge"):
            continue
        info = hsn_map.get((_clean(order_no), _clean(item.get("Description"))))
        # GetHSNDetails is the authoritative item-master lookup: no match at
        # all, or a match whose own Nav_Item_No is blank, means SF doesn't
        # recognize this line's description -> flag it as a new template
        # needing manual review/retraining, regardless of any later
        # GetSparePurchaseItem positional fallback in enrich_invoice.
        missing = not (info and str(info.get("Nav_Item_No") or "").strip())
        item["_hsn_nav_item_no_missing"] = missing
        if missing and sf_active:
            # SF couldn't confirm this line at all - don't leave the PDF's
            # raw HSN/SAC code sitting in No. looking like a real match;
            # blank it so a saved No. always means SF actually confirmed it.
            item["ProductNo"] = ""
        if not info:
            continue
        # Only overwrite a field when SF actually has a value for it — an SF
        # match whose own record is missing a piece (no code, no tax rate,
        # ...) must not blank out what build_invoice_json/the PDF already
        # seeded it with. TaxPercentage in particular drives the "GST %"
        # column, and SF's item master frequently has the Nav_Item_No but
        # not a rate, which used to silently wipe out the PDF-derived rate.
        new_product_no = info.get("ProductNo") or info.get("Nav_Item_No", "")
        if new_product_no:
            item["ProductNo"] = new_product_no
        item["Nav_Item_No"] = info.get("Nav_Item_No", "")
        if info.get("PartSpecification"):
            item["PartSpecification"] = info["PartSpecification"]
        if info.get("Nav_Part_Description"):
            item["Nav_Part_Description"] = info["Nav_Part_Description"]
        if info.get("HSN_Type"):
            item["HSN_Type"] = info["HSN_Type"]
        if info.get("HSN_Percentage_Description"):
            item["HSN_Percentage_Description"] = info["HSN_Percentage_Description"]
        if info.get("TaxPercentage"):
            item["TaxPercentage"] = info["TaxPercentage"]
            item["GST_%"] = info["TaxPercentage"]


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def fetch_sf_batch(invoices):
    """
    One-shot batched Service First lookup for a whole processing run - a
    single GetSparePurchaseItem call covering every invoice's PO, and a
    single GetHSNDetails call covering every invoice's items, instead of
    two HTTP round trips per invoice. `invoices` is an iterable of invoice
    `data` dicts (only buyer_order_no/items are read). Returns
    (spare_map, hsn_map), both pass straight into apply_sf_batch.

    Sample: fetch_sf_batch([{'buyer_order_no': 'SPRPUR/2026/04/27-83650', 'items': []}])
    """
    order_nos = []
    hsn_items = []
    for data in invoices:
        order_no = (data.get("buyer_order_no") or "").strip()
        if order_no:
            order_nos.append(order_no)
            hsn_items.extend(_hsn_lookup_items(data, order_no))

    spare_map = get_spare_purchase_items(order_nos) if order_nos else {}
    hsn_map = get_hsn_details(hsn_items) if hsn_items else {}
    return spare_map, hsn_map


def apply_sf_batch(data, spare_map, hsn_map):
    """
    Apply the results of a prior fetch_sf_batch() to one invoice and
    evaluate it - the batched-run equivalent of enrich_invoice(), with no
    HTTP calls of its own. Returns the same verdict shape enrich_invoice
    does.

    Sample: apply_sf_batch(data, spare_map, hsn_map)
    """
    order_no = (data.get("buyer_order_no") or "").strip()
    rows = spare_map.get(_clean(order_no), []) if order_no else []
    return _apply_sf_to_invoice(data, order_no, rows, hsn_map)


def enrich_invoice(data):
    """
    Populate data["sf_items"] (reservation rows) from Service First for this
    invoice's buyer_order_no, enrich its line items with HSN / vendor details,
    then evaluate the invoice. Returns the verdict from `evaluate_invoice`:

        {"status", "is_active", "is_synced", "errors", "reason"}

    Makes its own HTTP calls for just this one invoice - for a whole batch
    of invoices, use fetch_sf_batch()/apply_sf_batch() instead so the run
    makes one GetSparePurchaseItem/GetHSNDetails call in total rather than
    one pair per invoice.

    Sample: enrich_invoice({'buyer_order_no': 'SPRPUR/2026/04/27-83650', 'seller_gstin': '33AAAAA0000A1Z5', 'items': []})
    """
    order_no = (data.get("buyer_order_no") or "").strip()

    rows = []
    if _base_url() and order_no:
        rows = get_spare_purchase_items([order_no]).get(_clean(order_no), [])

    hsn_items = _hsn_lookup_items(data, order_no) if rows else []
    hsn_map = get_hsn_details(hsn_items) if hsn_items else {}

    return _apply_sf_to_invoice(data, order_no, rows, hsn_map)


def _apply_sf_to_invoice(data, order_no, rows, hsn_map):
    """Shared by enrich_invoice() and apply_sf_batch(): given the
    GetSparePurchaseItem rows for this invoice's PO and the (possibly
    much larger, batch-wide) GetHSNDetails map, enrich `data` in place
    and return its verdict. No HTTP calls."""
    # GetHSNDetails/GetHSNDetails-derived fields (item code, GST %,
    # description) don't depend on GetSparePurchaseItem succeeding - the
    # two are separate calls (see fetch_sf_batch) and GetSparePurchaseItem
    # is, in practice, the far less reliable of the two (frequent
    # timeouts). Apply HSN data unconditionally so a missing/failed
    # reservation lookup never discards HSN data that was fetched fine -
    # the invoice can still fail its verdict below for lacking reservation
    # data, but its Purchase Line fields won't be blanked out for it.
    _apply_hsn_map(data, order_no, hsn_map)

    if rows:
        apply_sf_item_defaults(rows)
        data["sf_items"] = rows

        # Vendor / header fields come from the first reservation row.
        first = rows[0]
        for dest, src in _HEADER_FROM_SF.items():
            data[dest] = first.get(src, "")

        # Link each reservation row to its Purchase Line item so
        # Reservation Entry.Source Ref. No. = Purchase Line.Line No. for
        # the matching Item No./No. (join key: Nav_Item_No). Preferred
        # match: by Nav_Item_No, when the HSN lookup above resolved it.
        items = [it for it in data.get("items", []) if not it.get("_charge")]
        by_nav = {
            _clean(it.get("Nav_Item_No")): it
            for it in items if _clean(it.get("Nav_Item_No"))
        }
        matched = set()
        for sf in rows:
            key = _clean(sf.get("Nav_Item_No"))
            it = by_nav.get(key)
            if it is not None:
                sf["Line_No"] = it.get("Line_No", "")
                matched.add(id(it))

        # Any SF reservation row not yet matched by Nav_Item_No still needs
        # a Line_No so Reservation Entry.Source Ref. No. can point at
        # *some* Purchase Line row - pair the remainder positionally
        # (same sequence on the PO) purely for that cross-reference.
        # Reservation Entry rows are per-unit (see _link_override's
        # Quantity note), so a line item with Quantity > 1 legitimately
        # has multiple reservation rows for the same item — expand each
        # unmatched item by its own quantity before pairing.
        #
        # Deliberately does NOT backfill Nav_Item_No/ProductNo from this
        # pairing (unlike the confirmed Nav_Item_No match above) - a
        # same-position SF row is not a confirmed identity match, only a
        # sequence coincidence, and No. must only ever hold a value SF
        # actually confirmed by description (see _apply_hsn_map).
        unmatched_sf = [sf for sf in rows if not sf.get("Line_No")]
        expanded_items = []
        for it in items:
            if id(it) in matched:
                continue
            try:
                qty = max(1, int(float(it.get("Quantity") or 1)))
            except (TypeError, ValueError):
                qty = 1
            expanded_items.extend([it] * qty)
        if unmatched_sf and len(unmatched_sf) == len(expanded_items):
            for sf, it in zip(unmatched_sf, expanded_items):
                sf["Line_No"] = it.get("Line_No", "")
    else:
        data.setdefault("sf_items", [])

    return evaluate_invoice(data)


def evaluate_invoice(data):
    """
    Decide the tracker verdict for one extracted invoice. Returns:

        {"status", "is_active", "is_synced", "errors": [...], "reason": str}

    is_synced is 1 when the invoice's part was received in Service First
    (reservation rows exist), 0 otherwise. Any failing check sets is_active
    False and a single human-readable `reason`; checks are ordered from the
    most fundamental (can't identify the seller) downward.

    Sample: evaluate_invoice({'seller_gstin': '33AAAAA0000A1Z5', 'buyer_order_no': 'SPRPUR/2026/04/27-83650', 'sf_items': [], 'items': []})
    """
    seller_gst = str(data.get("seller_gstin") or "").strip()
    order_no = str(data.get("buyer_order_no") or "").strip()
    sf_items = data.get("sf_items") or []
    is_synced = bool(sf_items)

    # tbl_status names are stored UPPER CASE — keep every status string here
    # upper case too (also matches the per-status folder names).
    def fail(reason, status="PENDING IN SF"):
        return {"status": status, "is_active": False, "is_synced": is_synced,
                "errors": [reason], "reason": reason}

    # 16 — seller GSTIN could not be captured from the PDF.
    if not seller_gst:
        return fail("seller gst could nt be captured")

    # 8 — no Buyer's Order No. on the PDF (nothing to look up in SF).
    if not order_no:
        return fail("Buyer order no is empty in pdf", status="BUYER ORDER NO DOESN'T EXIST")

    # 8b — a SPRPUR PO was found but OCR left it garbled (e.g. a handwritten
    # note). Park it in the same review queue so a user can confirm the value
    # before it's trusted for the Service First lookup.
    if data.get("buyer_order_doubtful"):
        return fail("Buyer order no format is doubtful — please verify",
                    status="BUYER ORDER NO DOESN'T EXIST")

    # Service-First-dependent checks (only when the backend is configured).
    if _base_url():
        # Part not yet received in Service First.
        if not sf_items:
            return fail("Part not yet received in Service First")

        # 1 & 2 — a Purchase Line "No." (Navision item no.) is empty. Freight /
        # courier charge lines aren't Navision parts, so skip them here.
        items = data.get("items") or []
        for it in items:
            if it.get("_charge"):
                continue
            # GetHSNDetails (the item-master lookup) never resolved this
            # line's description at all -> SF has no idea what this part
            # is. The invoice's own FORMAT is still recognized (this only
            # runs once fmt_model already matched it) - it's SF's item
            # catalog that's incomplete, not the template, so this is a
            # DATA MISMATCH (Purchase Line's [No.] stays blank), not a NEW
            # TEMPLATE. Still flagged for retraining copy below, same as
            # the genuine "PENDING IN SF" case, since a human should look at
            # why SF doesn't know this part.
            if it.get("_hsn_nav_item_no_missing"):
                msg = ("The field [No.] is blank in Purchase Line table — "
                       "Service First doesn't recognize this item")
                return {"status": "DATA MISMATCH", "is_active": False,
                        "is_synced": is_synced, "errors": [msg],
                        "reason": msg, "new_format": True}
            # SF resolved a Nav_Item_No/ProductNo above but it (or the raw
            # HSN lookup) ended up literally blank or the string "null" -
            # a mandatory, Service-First-sourced Purchase Line field with
            # nothing in it, same class of gap as the two checks around
            # this one, so it's DATA MISMATCH too rather than the generic
            # PENDING IN SF.
            nav = str(it.get("ProductNo") or it.get("Nav_Item_No") or "").strip()
            if not nav or nav.lower() == "null":
                msg = "The field [No.] is blank in Purchase Line table"
                return {"status": "DATA MISMATCH", "is_active": False,
                        "is_synced": is_synced, "errors": [msg],
                        "reason": msg, "new_format": True}
            # SF resolved a Nav_Item_No, but its own Nav_Part_Description is
            # either blank or describes something else entirely - trusting
            # it (or the blank) would load the wrong part into BC, so this
            # needs a human to confirm before it's ready.
            if _description_mismatch(it.get("Nav_Part_Description"), it.get("Description")):
                msg = ("Nav Part Description is empty or doesn't match the "
                       "PDF's item description")
                return {"status": "DATA MISMATCH", "is_active": False,
                        "is_synced": is_synced, "errors": [msg],
                        "reason": msg, "new_format": True}

        # Purchase Line is fine — now check the Reservation Entry side: every
        # row GetSparePurchaseItem returned must itself carry a Nav_Item_No
        # and a Nav_VendorCode, or SF hasn't finished mapping this part/
        # vendor yet.
        for sf in sf_items:
            if not str(sf.get("Nav_Item_No") or "").strip():
                return fail("Nav item No not mapped in SF")
            if not str(sf.get("Nav_VendorCode") or "").strip():
                return fail("NAV Vendor Code not Mapped in SF")

        return {"status": "READY TO LOAD", "is_active": True, "is_synced": True,
                "errors": [], "reason": ""}

    # No SF backend configured: extraction-level checks passed.
    return {"status": "EXTRACTED", "is_active": True, "is_synced": is_synced,
            "errors": [], "reason": ""}
