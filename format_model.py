"""
Invoice format (template) learning and detection for PIIPS.

A "format" is identified by the **seller's GSTIN** — each vendor issues
one invoice template, and the GSTIN is constant across every invoice they
send, regardless of the invoice's data (amounts, dates, buyer, items).
This reliably separates one vendor's template from another's, which the
field-label set alone cannot (most GST invoices contain the same fields).

The set of field labels is still recorded per format for display, but
matching is by GSTIN.

Persisted to format_model.json:

    {
      "formats": [
        {
          "name": "AVS_ENTERPRISES",
          "seller_gstin": "07DMBPS3307A2ZH",
          "seller_name": "AVS ENTERPRISES",
          "source": "1077.pdf",
          "fields": ["amount", "buyer", "gstin", "hsn", ...]
        }
      ]
    }
"""

import json
import os
import re
import shutil
import threading
from datetime import datetime


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

MODEL_FILE = os.path.join(BASE_DIR, "format_model.json")

BACKUP_DIR = os.path.join(BASE_DIR, "model_backups")

# A PDF is accepted as a known format when its similarity to some stored
# format is at least this fraction (90%); otherwise it is moved to New_Format.
MATCH_THRESHOLD = 0.90


# Canonical invoice fields -> label phrases (recorded for display only).
FIELD_LABELS = {
    "invoice_no":     ["invoice no", "invoice number", "bill no", "tax invoice"],
    "invoice_date":   ["dated", "invoice date"],
    "consignee":      ["consignee", "ship to", "shipped to"],
    "buyer":          ["buyer", "bill to", "billed to"],
    "gstin":          ["gstin", "uin", "gst no"],
    "state":          ["state name", "state code"],
    "hsn":            ["hsn", "sac"],
    "description":    ["description of goods", "description", "goods and services",
                       "name of product", "particulars"],
    "quantity":       ["quantity", "qty"],
    "rate":           ["rate"],
    "per":            ["per"],
    "discount":       ["disc", "discount"],
    "amount":         ["amount"],
    "taxable":        ["taxable"],
    "igst":           ["igst"],
    "cgst":           ["cgst"],
    "sgst":           ["sgst"],
    "tax_amount":     ["tax amount"],
    "buyers_order":   ["buyer's order", "order no", "purchase order"],
    "dispatch":       ["dispatch", "dispatched through"],
    "destination":    ["destination"],
    "delivery_terms": ["terms of delivery", "mode/terms of payment"],
    "reference":      ["reference no", "other references"],
    "eway":           ["e-way", "eway"],
    "amount_words":   ["amount chargeable", "in words"],
    "bank":           ["bank name", "a/c no", "account no", "ifsc", "ifs code"],
    "declaration":    ["declaration"],
    "email":          ["e-mail", "email"],
    "phone":          ["phone", "mobile"],
    "pan":            ["pan"],
    "signatory":      ["authorised signatory", "authorized signatory"],
    "delivery_note":  ["delivery note"],
}


class FormatModel:

    def __init__(self, model_file=MODEL_FILE):
        self.model_file = model_file
        self._lock = threading.Lock()
        self.formats = self._load()

    # -- persistence -------------------------------------------------------

    def _load(self):
        if os.path.exists(self.model_file):
            try:
                with open(self.model_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data.get("formats", [])
            except (json.JSONDecodeError, OSError):
                pass
        return []

    def save(self):
        with open(self.model_file, "w", encoding="utf-8") as f:
            json.dump({"formats": self.formats}, f, indent=4, ensure_ascii=False)

    def clear(self):
        """Forget all trained formats."""
        with self._lock:
            self.formats = []
            self.save()

    # -- backup / restore --------------------------------------------------

    def backup(self):
        """
        Copy the current model file to model_backups/ with a timestamp.
        Returns the backup filename (or "" if there was no model yet).
        """
        if not os.path.exists(self.model_file):
            return ""

        os.makedirs(BACKUP_DIR, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        dest = os.path.join(BACKUP_DIR, f"backup_model_{stamp}.json")

        n = 1
        while os.path.exists(dest):
            dest = os.path.join(BACKUP_DIR, f"backup_model_{stamp}_{n}.json")
            n += 1

        shutil.copy2(self.model_file, dest)
        return os.path.basename(dest)

    @staticmethod
    def list_backups():
        """Available backups, newest first."""
        if not os.path.isdir(BACKUP_DIR):
            return []

        out = []
        for f in os.listdir(BACKUP_DIR):
            if not f.endswith(".json"):
                continue
            p = os.path.join(BACKUP_DIR, f)
            try:
                data = json.load(open(p, "r", encoding="utf-8"))
                count = len(data.get("formats", [])) if isinstance(data, dict) else 0
            except (json.JSONDecodeError, OSError):
                count = 0
            out.append({
                "name": f,
                "modified": datetime.fromtimestamp(
                    os.path.getmtime(p)
                ).strftime("%Y-%m-%d %H:%M:%S"),
                "formats": count,
            })

        return sorted(out, key=lambda x: x["name"], reverse=True)

    def restore(self, name):
        """
        Restore a backup by filename: copy it over the live model. Does NOT
        create a new backup, so the backup list/order is unchanged. Raises
        FileNotFoundError if the backup does not exist.
        """
        name = os.path.basename(name)
        src = os.path.join(BACKUP_DIR, name)

        if not os.path.isfile(src):
            raise FileNotFoundError(name)

        with self._lock:
            shutil.copy2(src, self.model_file)
            self.formats = self._load()

        return name

    # -- signature ---------------------------------------------------------

    @staticmethod
    def seller_gstin(ocr_result):
        """Normalized seller GSTIN — the format key."""
        fields = ocr_result.get("Fields", {}) or {}
        g = (fields.get("Seller GSTIN/UIN") or "").upper()
        return re.sub(r"[^A-Z0-9]", "", g)

    @staticmethod
    def _norm(text):
        """Uppercase, alphanumerics only — for stable name comparison."""
        return re.sub(r"[^A-Z0-9]", "", (text or "").upper())

    @classmethod
    def _party_gstins(cls, ocr_result):
        """The buyer's and consignee's GSTINs (normalized)."""
        fields = ocr_result.get("Fields", {}) or {}
        return {
            cls._norm(fields.get(k))
            for k in ("Buyer GSTIN/UIN", "Consignee GSTIN/UIN")
            if cls._norm(fields.get(k))
        }

    @classmethod
    def trusted_gstin(cls, ocr_result):
        """The seller GSTIN, but ONLY when it is genuinely the seller's — i.e.
        present and not equal to the buyer's/consignee's. Some layouts print
        the seller GSTIN in a spot OCR can't read (e.g. crammed against the
        UDYAM line); then the only GSTIN on the page is the buyer's, which must
        never be used as the seller's key."""
        g = cls.seller_gstin(ocr_result)
        return g if (g and g not in cls._party_gstins(ocr_result)) else ""

    @classmethod
    def seller_name_key(cls, ocr_result):
        """Normalized seller name — the fallback format key when the seller
        GSTIN is unreadable."""
        fields = ocr_result.get("Fields", {}) or {}
        return cls._norm(fields.get("Seller Name"))

    @staticmethod
    def field_set(ocr_result):
        """Field labels present (recorded for display / reference)."""
        text = (ocr_result.get("Text", "") or "").lower()
        return sorted(
            key
            for key, phrases in FIELD_LABELS.items()
            if any(p in text for p in phrases)
        )

    # -- matching ----------------------------------------------------------

    @staticmethod
    def _similarity(fields_a, fields_b):
        """Jaccard similarity between two field-label sets (0.0 - 1.0)."""
        a, b = set(fields_a or []), set(fields_b or [])
        union = a | b
        if not union:
            return 0.0
        return len(a & b) / len(union)

    def match(self, ocr_result):
        """
        Compare the PDF against every stored format and return
        (format_name, score), where score is 0.0 - 1.0.

        - An identical (trusted) seller GSTIN is a definitive match (1.0).
        - If the seller GSTIN is unreadable, an identical seller NAME is a
          definitive match (1.0) — this keeps one vendor as one format instead
          of splitting/collapsing onto whatever buyer GSTIN was on the page.
        - Otherwise the best field-set similarity is used; accepted only when
          the score is >= MATCH_THRESHOLD (90%).
        - When nothing reaches the threshold, returns (None, best_score) and
          the caller moves the PDF to New_Format.
        """

        gstin = self.trusted_gstin(ocr_result)
        name_key = self.seller_name_key(ocr_result)
        pdf_fields = self.field_set(ocr_result)

        best_name, best_score = None, 0.0
        for fmt in self.formats:
            if gstin and fmt.get("seller_gstin") and fmt["seller_gstin"] == gstin:
                return fmt["name"], 1.0
            # Name fallback only when THIS invoice has no trusted GSTIN, so a
            # GSTIN-keyed vendor is never matched by name alone.
            if (not gstin and name_key
                    and self._norm(fmt.get("seller_name")) == name_key):
                return fmt["name"], 1.0
            score = self._similarity(pdf_fields, fmt.get("fields", []))
            if score > best_score:
                best_name, best_score = fmt["name"], score

        if best_name is not None and best_score >= MATCH_THRESHOLD:
            return best_name, best_score

        return None, best_score

    # -- registration ------------------------------------------------------

    def _next_name(self):
        """Next sequential format name: format1, format2, ..."""
        nums = []
        for f in self.formats:
            m = re.fullmatch(r"format(\d+)", f.get("name", ""))
            if m:
                nums.append(int(m.group(1)))
        return f"format{(max(nums) + 1) if nums else 1}"

    def register(self, ocr_result, source="", persist=True):
        """
        Learn a NEW format from this OCR result. Returns its name.
        Set persist=False to append in memory only (caller saves later) —
        used so a training run commits atomically at the end.
        """

        fields = ocr_result.get("Fields", {}) or {}
        seller = (fields.get("Seller Name") or "").strip()
        # Store only a TRUSTED seller GSTIN; never the buyer's. When the seller
        # GSTIN is unreadable the format is keyed on the seller name instead.
        gstin = self.trusted_gstin(ocr_result)

        with self._lock:
            name = self._next_name()
            self.formats.append({
                "name": name,
                "seller_gstin": gstin,
                "seller_name": seller,
                "sources": [os.path.basename(source)] if source else [],
                "fields": self.field_set(ocr_result),
            })
            if persist:
                self.save()

        return name

    def _merge_into(self, fmt, ocr_result, source, persist):
        """Merge another same-format sample into an existing format."""
        with self._lock:
            merged = set(fmt.get("fields", [])) | set(self.field_set(ocr_result))
            fmt["fields"] = sorted(merged)

            src = os.path.basename(source)
            sources = fmt.setdefault("sources", [])
            if src and src not in sources:
                sources.append(src)

            # keep a seller name if we didn't have one yet
            if not fmt.get("seller_name"):
                fmt["seller_name"] = (
                    (ocr_result.get("Fields", {}) or {}).get("Seller Name") or ""
                ).strip()

            # Backfill a trusted GSTIN if a later sample of a name-keyed format
            # finally has a readable seller GSTIN.
            if not fmt.get("seller_gstin"):
                g = self.trusted_gstin(ocr_result)
                if g:
                    fmt["seller_gstin"] = g

            if persist:
                self.save()

    def learn(self, ocr_result, source="", persist=True):
        """
        Training helper. Keyed on the TRUSTED seller GSTIN, or the seller NAME
        when the GSTIN is unreadable. If the key matches a known format, MERGE
        this sample into it; otherwise register a new formatN.
        Returns (name, status): 'merged', 'learned', or 'no_gstin'.
        """

        gstin = self.trusted_gstin(ocr_result)
        name_key = self.seller_name_key(ocr_result)

        # Nothing to key on at all (no trusted GSTIN and no seller name).
        if not gstin and not name_key:
            return None, "no_gstin"

        for fmt in self.formats:
            if gstin and fmt.get("seller_gstin") and fmt["seller_gstin"] == gstin:
                self._merge_into(fmt, ocr_result, source, persist)
                return fmt["name"], "merged"
            if (not gstin and name_key
                    and self._norm(fmt.get("seller_name")) == name_key):
                self._merge_into(fmt, ocr_result, source, persist)
                return fmt["name"], "merged"

        return self.register(ocr_result, source, persist=persist), "learned"

    def names(self):
        return [f["name"] for f in self.formats]
