# Changelog

## 2.2

### Invoice extraction fixes (surfaced during GST e-invoice training)

- **Seller GSTIN regex boundary** — stripping spaces before matching could
  eat the word boundary the GSTIN regex needed, silently dropping a
  correctly-printed GSTIN. Now tries the original, space-intact text first.
- **Three-column header (Bill To / Ship To / Invoice Details) dropping data**
  — when a vendor's Invoice Details column has no literal
  "Invoice Details"/"Details" caption (just starts straight with e.g.
  "Inv No."), the whole column — including Invoice No., Dated, and Buyer's
  Order No. — was silently discarded. Now falls back to detecting the
  column boundary structurally, and the header row's own values are no
  longer lost.
- **Wrapped value continuations** — a value that line-wraps in a narrow
  column (e.g. an Invoice No. split across two lines) is now stitched back
  together instead of being read as only its first half.
- **Bare "Invoice :" label** — some vendors label the invoice number with
  no "No." at all; this is now recognized.
- **Anchor-offset bug** — when a matched label phrase straddled two OCR
  tokens, the wrong field's value (e.g. a date instead of an invoice
  number) could be returned. Fixed.
- **Item description word order** — a number glued directly onto the next
  word with no space (e.g. "1000Base-T") could get OCR'd as two
  overlapping boxes and end up spliced into the wrong position in the
  description. Row words are now sorted by box center, not left edge.
- **Item description spacing** — touching/overlapping OCR word boxes are
  now joined with no space instead of always inserting one, so a wrapped
  word like "1000Base-T" no longer gains a stray space.
- **Stray "days" fragment removed** — a bare "days" left over from a
  Warranty column split by OCR no longer leaks into the item description.
- **Freight/courier/shipping charges misclassified as parts** — a charge
  line that carries its own explicit serial number (not just the
  no-serial fallback case) is now correctly tagged as a charge, so it's
  excluded from the Part Description Mapping suggestion list instead of
  being offered as a selectable part description.
- **Charge lines missing Quantity/Direct Unit Cost** — a charge line whose
  row only ever states a flat amount now defaults Quantity to 1 and Rate
  to that amount, instead of failing mandatory-field validation.
- **Duplicate empty charge line** — a charge label that wraps onto a second
  line (e.g. "Freight Charges" / "Shipping") is now merged into the
  original charge line's description instead of opening a second, empty
  charge item.
- **ISO date parsing** — invoice dates printed as `yyyy-mm-dd` (common on
  computer-generated e-invoices) were not recognized, silently blanking
  both Invoice Date and the Due Date computed from it. Now handled.
- **PDF download filename** — downloading/saving an invoice PDF from the
  viewer could lose its real file name on some browsers; the server now
  sends both a plain and an RFC 5987 `Content-Disposition` filename so the
  real name is always kept.

### Part Description Mapping screen

- Renamed the "Invoice Part Description" column to
  "Invoice Part Description (IN SF)" and "Specification" to
  "SF Part Specification" for clarity.
- Purchase Details / Part Details columns now wrap instead of forcing
  horizontal scroll.
- A part whose Service First description already matches the invoice's own
  description is no longer listed, even if its PO's overall status is
  DATA MISMATCH for an unrelated reason — this screen now only surfaces a
  genuine PDF/SF description mismatch.

### Dashboard

- The Status Breakdown popup's invoice list no longer forces horizontal
  scroll — Invoice No., File, Vendor, Invoice Type, Status, and Batch
  columns wrap to fit on one screen.

### Other

- Added a per-tab single-tab guard: opening PIIPS in a second tab or
  window shows a blocking screen instead of running two copies at once
  (a convenience, not a security control).
