# Changelog

Every release from 2.2 onward gets its own dated section below, added at the
time of the version bump — this file is the one place to check "what changed
in which version." The app didn't track version numbers incrementally before
that: `2.1` was the version string set on the very first commit and never
bumped again until 2.2, so everything from the initial commit through just
before 2.2 is grouped under **2.1** below as a retrospective summary (by
theme, not a literal commit-by-commit log) rather than a series of real
sub-versions.

## 2.2 — 2026-08-30

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

---

## 2.1 — Initial release (retrospective summary)

Everything below shipped under the `2.1` version string, from the project's
first commit up through the point 2.2 branched off. Grouped by theme for
reference, not in strict chronological order.

### Core extraction / OCR / GST logic

- Address parsing: stripped landmark ("Near ..."), intercom, ISO-certification
  boilerplate, and unlabeled phone numbers out of Address 2; fixed glued PIN
  code parsing; capped address fields to 50 characters.
- Item descriptions: stripped serial-number echoes, trailing GST%, page-footer
  noise, and trailing summary-section text that had been leaking in; kept
  genuine bare dimension specs (e.g. `18.5"`) from being dropped; recognized a
  no-serial, no-own-row-HSN item line instead of merging it into the next
  charge line; recovered Quantity/Rate/Amount from a garbled scanned-image
  continuation row.
- Dates: recognized dot-separated dates and dense 4-digit HSN table layouts;
  recovered buyer block and document date on labels-optional layouts.
- Invoice No.: recognized the "Invioce No:" vendor typo; stripped a stray
  trailing date that could glue onto it.
- Buyer order number: found the buyer order serial across a two-column page's
  interleaved OCR text.
- Seller/Pay-to Name: fixed extraction when a title splits across columns.
- GST/tax: added a per-item GST % fallback; reworked freight/courier GST % to
  reconcile against the invoice's own tax total instead of inheriting the
  item tax rate; fixed GST Group Code collapsing "Service 0%" to just
  "Service"; fixed GST Group Type/Code and No. for freight/charge lines on
  PART invoices; made GST Group Code/Type/Base Amount mandatory.
- Format/status handling: renamed INCOMPLETE DATA to DATA MISMATCH; added the
  NEW TEMPLATE status, later reserved strictly for unrecognized formats (not
  Service First item-lookup misses); a blank Purchase Line No. now correctly
  moves an invoice to DATA MISMATCH instead of PENDING IN SF; a `None`-source
  field can no longer cause a false DATA MISMATCH and is hidden from the
  Fields drill-down; rejected scanned/photocopied PDFs outright instead of
  OCR-extracting garbage from them.
- Service First integration: stopped trusting unconfirmed item-code matches
  from Service First; stopped letting a Service First call blank out a good
  field when SF's own data was missing; batched Service First calls once per
  run (later chunked so one large run can't time out everything) instead of
  once per invoice; flagged Nav Part Description mismatches; refreshed the PO
  Number Format from the current template at batch download.
- Duplicate/re-upload handling: gave duplicate invoices a real tracker row;
  let a re-upload through when the file's linked invoice was Excluded;
  re-processing an Excluded invoice now updates its existing record instead
  of duplicating it.
- Unit cost: derived item unit cost from Amount / Quantity when OCR missed
  the Rate column.

### Workflow / lifecycle / Dashboard

- Reworked batch numbering and status lifecycle; added Viewer role gaps
  cleanup and assorted fixes.
- Added a Navision Document No. column and an Accounts/Admin/Super Admin
  reject action on the Post page; styled the per-row Reject button as a
  danger action.
- Ordered the Dashboard batches table by batch, not a header's CreatedAt.
- Archived Posted/Completed invoices into `ALL_INVOICES` under the Folder
  Path.
- Showed the Dashboard "Fields" drill-down button to every role (including
  Admin).
- Added the Part Description Mapping menu (screen that 2.2 later refined).

### Admin, security, and users

- Added user email notifications, forced password change on first login, and
  mail server settings; showed the account type in the welcome email; fixed
  email sign-in links.
- Turned "My password" into an admin-driven Change Password / assignment
  form.
- Added site-wide Announcements (Super Admin).
- Added a pre-login Database Configuration bootstrap screen; switched
  display queries to NOLOCK.
- Stored `config.json`'s `db_connection` as plain text instead of
  DPAPI-encrypted.
- Restored the self-lockout guard.

### UI/UX

- General UI/UX polish: modern confirm dialogs, password visibility toggle,
  friendlier error messages.
- Replaced the PIIPS logo with a new monogram mark.
- Renamed "Precision Intelligence" to "Precision Intelligent" throughout.

### Deployment / ops / repo hygiene

- Added a deploy script, `requirements.txt`, and the UAT Deployment Guide;
  later refreshed the deploy runbook (UAT/live robocopy + NSSM setup steps)
  and fixed IIS WebDAV 405 errors.
- Defaulted a fresh `config.json`'s `folder_path` to `E:\PIIPS_UPloads`.
- Fixed `.gitignore` negation for the tracked Deployment Guide PDF; kept
  `PIIPS.sql` out of git and out of Publish deployments; made Publish push
  already-committed-but-unpushed commits too; removed sample invoice PDFs
  and generated manuals from the repo.
