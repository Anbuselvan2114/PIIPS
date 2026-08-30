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
- The Fields drill-down popup (Dashboard → DATA MISMATCH → Fields) no
  longer shows an "HSN_Type" row (redundant with the row directly above
  it); the field previously labeled "ProductNo" is now shown as
  "HSN/SAC Code", matching the actual saved column name; and it now also
  shows "Buyer Order No" in the Purchase Header section (previously only
  visible elsewhere in the app, not in this popup).

### Business rules

- Default payment terms, used only when neither Service First nor the PDF
  itself states a usable payment-terms value, changed from 45 days to
  30 days — affects both the Payment Terms Code shown on the invoice and
  the Due Date computed from it in that fallback case.
- HSN/SAC Code is now conditionally mandatory for an "Item" (part) line —
  missing exactly when the PDF itself had an HSN that Service First failed
  to confirm; if the PDF never printed one either, there was nothing for
  SF to confirm in the first place and it's not flagged. Stays fully
  optional for a "Charge (Item)" (freight/courier) line, since many
  vendors never print one on those. Source of truth is unchanged for
  either line type: a part's HSN still only ever comes from Service First
  (left blank when SF can't confirm the part, by design), a charge's HSN
  still comes from the PDF (SF is never queried for charges at all). Also
  fixed a duplicate this requirement briefly introduced: "HSN/SAC Code"
  was showing up as two separate rows in the Fields popup for the same
  value — now shown once.

### Other

- Added a per-tab single-tab guard: opening PIIPS in a second tab or
  window shows a blocking screen instead of running two copies at once
  (a convenience, not a security control).
- The app version now shows in the sidebar, next to "Invoice Processing
  Suite" — fetched from a new `/api/version` endpoint so it can never
  drift out of sync with the backend's own version string.
- Added a copyright footer — "© 2026 Precision Techserve Madras Pvt. Ltd.
  All Rights Reserved." plus the app name and version — to the Login
  screen and to the bottom of every page once logged in (pinned level with
  the sidebar's Logout button).

### Viewer role

- Added a fully read-only "Viewer" role. It sees the same invoice-
  processing screens as everyone else (Dashboard, File Explorer, Buyer
  Order Entry, Part Description Mapping, Load, Post, Complete) with every
  mutating action blocked (server-side too, not just hidden in the UI),
  but does not see the Manual screen or any Setup/Mapping/Admin screen
  (Folder/Database/API Configuration, Template, Create Field, Field
  Mapping, Model Training, User Management, Mail Server Setting,
  Announcement, Publish) - those configure the app itself, not data.
- A Viewer account is set up differently from every other role: a Super
  Admin assigns its username and password directly (no email required, no
  auto-generated temp password, no forced change on first login - the
  chosen password is meant to be the permanent one), instead of the normal
  emailed-temp-password flow. Its password isn't held to the usual
  complexity policy either, since it's an admin-assigned internal
  credential (e.g. matching an employee code) rather than a self-service
  one.
- Activating or deactivating a Viewer account sends no email (there's
  usually no email on file for one, and no self-service flow that would
  need notifying); an Admin/Super-Admin-triggered password change for any
  account, Viewer included, still emails the new credentials as before.
- Every outbound email now shows which environment sent it ("Local
  (test)" / "UAT" / "Live") in its footer, read from web.config's own
  `environment` <appSettings> key (`config_store.current_environment()`,
  defaulting to "Local (test)" when absent - the normal case for a
  developer's own machine). `deploy.py`'s publish step now tags the
  staged web.config it copies to `<published_root>/<environment>` with
  the right value automatically.
- Moved "Load" from the Accounts nav group into "Buyer Order Entry" and
  "Part Description Mapping"'s group, which was also renamed from
  "Review" to "Review & Update" to describe all three together.

### Business rules

- A mandatory field whose source is the PDF itself (not Service First,
  System, or a Template value) being missing now routes an invoice to
  NEW TEMPLATE instead of DATA MISMATCH, and copies it into New_Format
  for retraining - e.g. Quantity, Direct Unit Cost, or Line Amount not
  being read off the PDF signals the extraction/template needs work, not
  a one-off data gap. A missing Service-First-sourced field (like `No.`,
  when SF simply doesn't recognize the part) still correctly stays
  DATA MISMATCH. `excel_export.missing_required_fields()` now reports
  each missing field's sheet and source so the two cases can be told
  apart; verified against the full trained-format set with no new false
  positives.

### Viewer role - closing the remaining write endpoints

- Two mutating endpoints were reachable by a Viewer despite the role
  being meant as fully read-only: `POST /api/input/upload` (File
  Explorer's "Upload PDFs" button) and `POST /api/part-description-
  update/save` (Part Description Mapping's "Update" button) had no
  `_require_not_viewer` check. Both now block a Viewer with the same
  403 every other write endpoint already returns. Dashboard's Start,
  Buyer Order Entry's Save, and Load/Post/Complete's advance and reject
  actions were already correctly blocked - only these two were missed
  when the role was first added.
- The default "PBV0030" Viewer account is now seeded automatically on
  every app startup (`database.ensure_default_viewer()`, called
  alongside `ensure_default_super_admin()`), the same always-available
  way "Sadmin" already was. Publish only ever copies code - Local, UAT,
  and Live each have their own separate database, so a user account
  created by hand on one was never going to appear on the others; this
  makes PBV0030 exist everywhere the same way Sadmin already did,
  without a manual re-creation step per environment. Its seeded default
  password is `PIIPS@2026` (was the username itself); Forgot Password
  now works for it too, once it has an email on file to send a new
  temporary password to (it had none before, by design, since normal
  Viewer setup skips email entirely).

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
