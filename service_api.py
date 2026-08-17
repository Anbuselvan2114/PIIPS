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


def _enrich_items(data, order_no):
    """Enrich line items from the HSN API (item no., HSN, tax)."""
    items = data.get("items", []) or []
    hsn_items = []
    for item in items:
        if item.get("_charge"):
            continue  # freight / courier lines aren't Navision parts
        desc = str(item.get("Description", "")).strip()
        if desc:
            hsn_items.append({"PartSpecification": desc, "PurchaseOrderNo": order_no})
    if not hsn_items:
        return
    hsn_map = get_hsn_details(hsn_items)
    for item in items:
        if item.get("_charge"):
            continue
        info = hsn_map.get((_clean(order_no), _clean(item.get("Description"))))
        # GetHSNDetails is the authoritative item-master lookup: no match at
        # all, or a match whose own Nav_Item_No is blank, means SF doesn't
        # recognize this line's description -> flag it as a new template
        # needing manual review/retraining, regardless of any later
        # GetSparePurchaseItem positional fallback in enrich_invoice.
        item["_hsn_nav_item_no_missing"] = not (info and str(info.get("Nav_Item_No") or "").strip())
        if not info:
            continue
        # Only overwrite ProductNo when SF actually has one — an SF match
        # with no code must not blank out the PDF's own HSN/SAC value that
        # build_invoice_json already seeded it with.
        new_product_no = info.get("ProductNo") or info.get("Nav_Item_No", "")
        if new_product_no:
            item["ProductNo"] = new_product_no
        item["Nav_Item_No"] = info.get("Nav_Item_No", "")
        item["PartSpecification"] = info.get("PartSpecification", "")
        item["Nav_Part_Description"] = info.get("Nav_Part_Description", "")
        item["HSN_Type"] = info.get("HSN_Type", "")
        item["HSN_Percentage_Description"] = info.get("HSN_Percentage_Description", "")
        item["TaxPercentage"] = info.get("TaxPercentage", "")
        item["GST_%"] = info.get("TaxPercentage", "")


# ---------------------------------------------------------------------------
# Orchestration for one invoice
# ---------------------------------------------------------------------------

def enrich_invoice(data):
    """
    Populate data["sf_items"] (reservation rows) from Service First for this
    invoice's buyer_order_no, enrich its line items with HSN / vendor details,
    then evaluate the invoice. Returns the verdict from `evaluate_invoice`:

        {"status", "is_active", "is_synced", "errors", "reason"}

    Sample: enrich_invoice({'buyer_order_no': 'SPRPUR/2026/04/27-83650', 'seller_gstin': '33AAAAA0000A1Z5', 'items': []})
    """
    order_no = (data.get("buyer_order_no") or "").strip()

    rows = []
    if _base_url() and order_no:
        rows = get_spare_purchase_items([order_no]).get(_clean(order_no), [])

    if rows:
        apply_sf_item_defaults(rows)
        data["sf_items"] = rows

        # Vendor / header fields come from the first reservation row.
        first = rows[0]
        for dest, src in _HEADER_FROM_SF.items():
            data[dest] = first.get(src, "")

        _enrich_items(data, order_no)

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

        # Fallback: the HSN lookup can fail to resolve any Nav_Item_No at
        # all (external API unavailable/no match), leaving both sides of
        # that join key blank. Fall back to positional matching (same
        # sequence on the PO) and backfill Nav_Item_No onto the Purchase
        # Line item too, so No. and Item No. still agree. Reservation
        # Entry rows are per-unit (see _link_override's Quantity note), so
        # a line item with Quantity > 1 legitimately has multiple
        # reservation rows for the same item — expand each unmatched item
        # by its own quantity before pairing positionally.
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
                nav_no = sf.get("Nav_Item_No", "")
                if nav_no and not it.get("Nav_Item_No"):
                    it["Nav_Item_No"] = nav_no
                    it["ProductNo"] = it.get("ProductNo") or nav_no
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
            # is. Treat the PDF as an unrecognized/new template: park the
            # invoice as NEW TEMPLATE (Purchase Line's [No.] stays
            # blank) and flag it for retraining, rather than the generic
            # "PENDING IN SF" used when SF simply hasn't received the part
            # yet.
            if it.get("_hsn_nav_item_no_missing"):
                msg = ("The field [No.] is blank in Purchase Line table — "
                       "the pdf is a new format")
                return {"status": "NEW TEMPLATE", "is_active": False,
                        "is_synced": is_synced, "errors": [msg],
                        "reason": msg, "new_format": True}
            nav = str(it.get("ProductNo") or it.get("Nav_Item_No") or "").strip()
            if not nav or nav.lower() == "null":
                return fail("Nav Part Description not entered in SF GRN")

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
