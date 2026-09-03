# Changelog

Every release from 2.2 onward gets its own dated section below, added at the
time of the version bump — this file is the one place to check "what changed
in which version." The app didn't track version numbers incrementally before
that: `2.1` was the version string set on the very first commit and never
bumped again until 2.2, so everything from the initial commit through just
before 2.2 is grouped under **2.1** below as a retrospective summary (by
theme, not a literal commit-by-commit log) rather than a series of real
sub-versions.

## 2.2.1 — 2026-09-03

Purchase Invoice Mapping — a new step between Load and Loaded requiring the
vendor's real Purchase Invoice No. before an invoice is truly Loaded — is
the headline feature of this release; everything else below is work that
either supports it directly or was found while building it.

### Block a batch's download while it has a missing Buyer Order No.

- `GET /api/batches/download` now refuses to export a batch that has any
  invoice still parked at BUYER ORDER NO DOESN'T EXIST - there's nothing
  usable for Navision on that invoice yet, and minting it a Document No.
  now would just need redoing once the PO is filled in. Returns a clear
  400 telling the user how many invoices are missing a PO and to fill it
  in first. `Dashboard.jsx`'s `canDownload`/Download-button tooltip gets
  the same check client-side, for a proactive disabled state instead of
  only failing after the click - matches the existing pattern already
  used for locked/blocked_by batches.
  Verified live: a batch with 2 Ready To Load invoices + 1 Buyer Order
  No Doesn't Exist invoice is correctly refused (400, exact invoice
  count in the message); fixing that one invoice's PO immediately
  unblocks the download, which then succeeds and produces a real .xlsx.

### Allow excluding a not-yet-advanced invoice from an In Progress batch

- `set_excluded` previously gated BOTH exclude and re-include purely on
  the batch's own status (Created/Downloaded only) - meaning once any
  ONE invoice in a batch reached Loaded/Purchase Invoice Pending/etc.,
  every OTHER invoice in that same batch got permanently stuck if it
  itself never advanced (e.g. was parked at Buyer Order No Doesn't
  Exist when the batch was downloaded, then only became Ready To Load
  afterward - too late for that download's Document No. minting, and
  its batch was already locked by the time it was fixed). That invoice
  could never get a Document No., never appear on the Load screen, and
  - since a batch only "clears" once every non-ignored invoice reaches
  the same terminal stage - permanently blocked every later batch from
  ever being downloaded too.
  Excluding now has one extra allowance: permitted while the batch is
  In Progress, as long as THIS invoice's own current status hasn't
  itself advanced past Ready To Load (checked against
  `_BATCH_LOCK_STATUSES`) - the batch-level lock exists because some
  OTHER invoice may already be committed downstream, which says nothing
  about whether pulling this still-unadvanced one out is safe.
  Re-including is deliberately NOT relaxed - it stays Created/Downloaded
  only, unchanged.
  Verified live with an isolated 20-invoice batch (19 at Purchase
  Invoice Pending, 1 stuck at Ready To Load): excluding the stuck one
  now succeeds; excluding one of the 19 already-advanced invoices still
  correctly fails with the (updated) error message; re-including the
  freshly-excluded invoice while the batch is still In Progress still
  correctly fails, unchanged.

### Security fix — CORS allowed any origin with credentials

- `allow_origins=["*"]` combined with `allow_credentials=True` (app.py)
  is a spec-disallowed combination - Starlette reflects the Origin
  header back, letting credentialed cross-site requests reach the whole
  API. Checked how the frontend actually uses this before fixing it:
  production is same-origin already (`api.js`'s own comment - the React
  build is served behind the same IIS site that reverse-proxies to this
  backend, `API_BASE` defaults to `""`), and no `fetch()` call anywhere
  in `api.js` ever sets `credentials: "include"` (auth here is a
  `user_id` in the request body/query, not a cookie/session) - so
  `allow_credentials=True` was protecting nothing. Now `allow_origins`
  is an explicit list - the two local Vite dev ports (5173, 3000), live
  (`https://piips.precisionit.co.in:8010`), and uat
  (`http://10.0.1.210:8080`) - and `allow_credentials` is `False`,
  matching actual usage instead of a wildcard that served no real
  purpose. Verified live: a preflight from each of the four real origins
  gets a proper `Access-Control-Allow-Origin` back; one from an
  arbitrary origin gets rejected outright (400, no CORS headers at all);
  the `Access-Control-Allow-Credentials` header is confirmed absent from
  every response.

### Security fix — Training screen actions had no server-side authorization

- `DELETE /api/formats` (wipes every trained vendor format), `POST
  /api/train`, and `POST /api/backups/restore` (silently discards
  everything learned since the chosen backup) had no authorization check
  at all - reachable by anyone who could hit the API directly, even
  though the Training screen these three actions live on is already
  Super Admin/Developer-only in the frontend's `ROLE_MENUS`. Found during
  a full-project review; the three unmatched endpoints were the only gap
  left in an otherwise-covered set (every other privileged endpoint
  already calls `_require_not_viewer` or stricter).
  Now all three call `_require_developer` (Super Admin only - the same
  guard `GET /api/db-config` already uses), matching what the UI already
  implied rather than adding a new access tier. `RestoreModel` gained a
  `user_id` field; `train_start`/`clear_formats` take it as a query
  param, same pattern as `get_db_config`. Frontend: `Training.jsx` didn't
  even accept the `user` prop App.jsx already passes to every page - now
  threads `user?.user_id` through Train/Restore; `clearFormats()` has no
  UI call site today (unused button), so only the backend guard applies
  there for now.
  Verified live: all three reject with 403 for an unauthenticated caller
  and for a Viewer; a Super Admin caller passes the check (confirmed via
  a safe not-found case on restore, without triggering the real
  destructive operations against the live model).

### Purchase Invoice No. surfaced on the Post/Complete screens; Dashboard column tweaks

- `_invoice_list` (backs Buyer Order Entry, the Lifecycle pages, and the
  Dashboard batch drill-down) now also selects `[Purchase Invoice No]`
  as `purchase_invoice_no`. Post and Complete show it as its own column
  right after Navision Document No.; the Batch column (not meaningful
  once an invoice already has a real Document No.) was dropped from
  those two stages only - Load keeps it. Verified with an isolated
  LOADED test header: the Post-stage API correctly returns the saved
  Purchase Invoice No.
- Dashboard batches table: added a Purchase Invoice Pending status
  column right after Loaded (`BATCH_STATUS_COLS` in `Dashboard.jsx` -
  the column gets the same clickable drill-down every other status
  column already has, no extra code needed).

### New "Purchase Invoice Pending" step between Load and Loaded, and the "Purchase Invoice Mapping" menu

- The Load lifecycle stage's real target status is now **PURCHASE INVOICE
  PENDING**, not LOADED directly (`app.py`'s `_LIFECYCLE["load"]`) - an
  invoice only reaches LOADED once its Purchase Invoice No. is mapped.
  READY TO LOAD itself is unchanged (still the ordinary extraction-complete
  gate); only what clicking "Mark as Loaded" leads to has changed.
  New column `[Purchase Invoice No]` on `tbl_Purchase_Header` (user-entered
  only - nothing else writes it). New menu, Purchase Invoice Mapping
  (`frontend/src/PurchaseInvoiceMapping.jsx`, modelled on Buyer Order
  Entry), lists every invoice at PURCHASE INVOICE PENDING with its
  Navision Document No. (the header's existing `[No.]` column) alongside
  an editable Purchase Invoice No. field; saving a non-blank value
  promotes the invoice straight to LOADED via `advance_status` (reused
  from the Load lifecycle stage itself), so a clashing Document No. on
  another invoice is still caught and blocks the promotion with a 409 -
  the Purchase Invoice No. is saved regardless, just not the promotion.
  Backend: `database.purchase_invoice_mapping_items()` /
  `set_purchase_invoice_no()`, `GET/POST /api/purchase-invoice-mapping/*`
  (save blocked for Viewer via `_require_not_viewer`, same as every other
  mutating endpoint; requires a non-blank value, matching Buyer Order
  Entry's own validation). Menu sits between Load and Post. Visible to
  Super Admin/Developer/Admin/User/Viewer (read-only for Viewer); not
  Accounts, matching Buyer Order Entry's own role scoping. Purchase
  Invoice Pending is also added to `_BATCH_LOCK_STATUSES` - an invoice
  sitting there already went through Load, so the batch's Document No.
  sequence may already be partly committed, same reasoning as LOADED
  itself.
  - Verified end-to-end with isolated test data: clicking "Mark as
    Loaded" (`POST /api/lifecycle/advance`) correctly lands invoices at
    PURCHASE INVOICE PENDING (not LOADED); they then appear in Purchase
    Invoice Mapping's list; saving a Purchase Invoice No. promotes to
    LOADED; a colliding Document No. on a second invoice correctly
    returns 409 and blocks the promotion while still saving the value;
    frontend production build passes with no errors.
  - The Load screen only shows invoices that already have a Navision
    Document No. minted (that's a pre-existing, unrelated filter -
    `[No.]` isn't minted until a batch is actually downloaded, see
    `fetch_batch`/`_assign_document_numbers`) - a batch that's never
    been downloaded shows nothing on Load, same as before this change.

### New `DisplayOrder` column on `tbl_status` - decouples display order from StatusId

- The Dashboard's "Status breakdown" bar chart (and `usp_StatusCounts`
  generally) ordered by raw `StatusId`, an IDENTITY permanently frozen at
  whenever a status was first seeded on a given database - a status added
  later (like Purchase Invoice Pending, added long after Loaded/Posted
  already existed on this dev database) could never sort where it
  logically belongs no matter where it sits in `STATUS_VALUES`. Added a
  `DisplayOrder INT` column, re-synced from `STATUS_VALUES`' own list
  order on every startup (both the live `ensure_menu_schema` DDL path and
  the CLI-only `init_status_table`), so reordering that Python list is
  now always enough - no StatusId renumbering, ever. `usp_StatusCounts`
  orders by `ISNULL(DisplayOrder, StatusId)`. `/api/stats/status-counts`
  (the Dashboard's data source) had its own bug in the same family: it
  force-pinned INITIATED/UNSUPPORTED (synthetic, folder-based counts -
  they have no real tracker rows) to the very front of the list
  regardless of DisplayOrder; now re-inserted at the position they
  already held in the DisplayOrder-sorted list instead. Verified via
  `/api/stats/status-counts` after several live reorders (Purchase
  Invoice Pending between Loaded and Posted; New Template, Duplicate and
  Unsupported each repositioned ahead of Initiated) - each one landed
  exactly where `STATUS_VALUES` placed it, including the previously-
  pinned Initiated/Unsupported.

## 2.2 — 2026-08-30

### New "MANUALLY UPDATED" status — stale-invoice auto-expiry

- A Data Mismatch, Excluded, or New Template invoice nobody resolves
  (re-uploads/fixes, re-includes, retrains) within 10 days
  (`database.STALE_STATUS_EXPIRY_DAYS`) now auto-parks as a new
  **MANUALLY UPDATED** status - permanently: it's deliberately never added
  to `_REPROCESSABLE_STATUSES`, so a later re-upload of that same invoice
  falls through to DUPLICATE instead of merging back in place, and it's
  added to `_BATCH_IGNORED_STATUSES` so it never holds up its batch's
  status label (same treatment Data Mismatch/New Template/Pending In SF
  already got). An Excluded invoice keeps its IsExcluded/PriorStatusID
  columns as-is when this fires - the frontend only offers Include/
  re-inclusion while the tracker's CURRENT status is literally "EXCLUDED",
  so once StatusID moves off of it that option is already gone. Runs
  automatically at the end of every Process/Start job
  (`processor.py`'s `_expire_stale_unresolved`, alongside the existing
  Pending In SF resync) via a new stored procedure,
  `usp_ExpireStaleUnresolved` - like the Pending In SF resync, this only
  ever fires as a side effect of someone clicking Start; there's no
  background scheduler in this codebase, so a batch nobody revisits
  won't auto-expire on its own.
  - Age is measured off `LastModifiedDatetime`, COALESCEd onto
    `CreatedDatetime` as a safety net for older rows. This surfaced a
    real pre-existing gap: `usp_SaveInvoiceBatch`'s tracker INSERT never
    stamped `LastModifiedDatetime` at all (only `CreatedDatetime`), so
    it sat NULL forever for any row nobody manually touched afterward -
    silently defeating any age check keyed off it. Now stamped at
    insert, same as `CreatedDatetime`.
  - **Unsupported is handled separately** - it never gets a
    Purchase_Header/Tracker row at all (a pure exception + file move, no
    database presence to age off), so it's swept by filesystem modified
    time instead: a new `config_store.expire_stale_files(status_name,
    days, target_status)` moves any PDF sitting in a status folder longer
    than the same 10-day window, called for UNSUPPORTED specifically
    from the same `_expire_stale_unresolved` step.
  - The DB-only status flip doesn't move the file on its own (these are
    older records from a past run, not part of the current job's own
    result list - see `_move_by_status`), so
    `expire_stale_unresolved()` also returns each expired row's FileName
    and the caller moves that PDF into the Manually Updated folder to
    match.
  - Verified directly: a stale row of each of the three DB-tracked
    statuses (11 days since last touch) expires, a fresh Data Mismatch
    row (2 days) doesn't; a stale Unsupported file (11-day-old mtime)
    moves via the filesystem sweep while a fresh one stays put;
    batch-status computation confirmed to ignore Manually Updated rows
    entirely (a batch with some Loaded and some Manually Updated still
    shows as LOADED, not stuck IN PROGRESS).

### Batch membership for auto-resynced Pending In SF records

- A record promoted out of PENDING IN SF by `_resync_pending` (runs
  automatically at the end of every Process/Start job, re-attempting
  Service First for anything still pending - see processor.py) stayed
  in whichever batch it was originally saved under, even though the
  equivalent case for a re-uploaded Data Mismatch/New Template/Excluded
  invoice already moves it into the batch of the run that cleared it
  (`reprocess_reworkable_header`). `usp_ReplaceReservation` now takes an
  optional `@BatchName` and `resync_pending()`/`_resync_pending` pass the
  current job's batch name through, so a Pending In SF record that
  clears during a Start now moves into THAT run's batch the same way,
  by updating its existing tracker row in place - no new row inserted.
  `@BatchName` defaults to NULL and is left unpassed by
  `apply_manual_buyer_order` (the Buyer Order Entry menu action, not a
  Start run), so that caller's behaviour - leave the record in its
  existing batch - is unchanged.

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

### Scanned/photocopied PDF support (PART and Service)

- An invoice with no embedded text layer (a scan or photocopy) used to
  be rejected outright, moved to UNSUPPORTED, every time, regardless of
  type. A new Super Admin-only toggle in Folder Configuration ("Allow
  scanned/photocopied invoices to be processed") switches this to OCR-
  extracting it instead, using the same PaddleOCR path already used for
  plain image files (.png/.jpg) — this infrastructure already existed,
  it just wasn't reachable for a scanned PDF page before. Off by default
  (unchanged behavior); a genuine handheld photo is still rejected
  either way, since OCR off an actual photo (vs. a flat scan) stays
  unreliable regardless of the setting. Applies to normal processing
  only, not Train, which still expects a clean file. Verified end-to-
  end for both invoice types: with the toggle on, previously-rejected
  scans of both PART and Service invoices now correctly extract and
  match their trained formats; with it off, the exact same files are
  rejected to UNSUPPORTED exactly as before.
- Fixed a routing bug this same feature exposed: a scanned invoice whose
  format DID match an already-trained one, but whose OCR misread its own
  Quantity/Rate/Amount table, was landing at NEW TEMPLATE ("needs
  training") - misleading, since retraining an already-recognized
  format doesn't fix one document's own OCR noise. `ocr_engine.read_pdf`
  now reports `IsScanned`; the mandatory-field gate only routes a
  missing PDF-sourced field to NEW TEMPLATE for a born-digital page -
  the same gap on a scanned one now correctly stays DATA MISMATCH
  instead. Verified against the exact case that surfaced it (a scanned
  "IDEAL SYSTEMS" invoice matching its already-trained format11).
- Train now also honors the toggle (originally normal processing only) -
  a scanned file sitting in New_Format is OCR'd and learned/merged like
  any other instead of being stuck there as permanently unsupported.
  This closed out the last of this session's genuinely-unrecoverable
  scans: all 11 remaining scanned files in New_Format (including
  scan1-3.pdf, unsupported since the very start of this session) trained
  successfully in one run - 9 merged into already-known formats, 2
  learned as new ones.
- Fixed a second, earlier routing bug in the same family: a scanned
  invoice whose format DID match an already-trained one, but whose
  Vendor Invoice No. itself couldn't be OCR'd off the page, was still
  landing at NEW TEMPLATE ("needs training") - same problem as the
  Quantity/Rate/Amount case above, just caught earlier in the pipeline
  where the format match hadn't been checked yet. Since the template is
  already known, retraining it fixes nothing; this is this one scan's
  own OCR noise, the same class of problem as any other unreadable
  file. Now routed to UNSUPPORTED instead (the same file-move, no-DB-row
  path every other unreadable file already takes) whenever the format
  matched, the invoice number is missing, and the page was scanned - an
  unrecognized format or a PART invoice with no item lines still route
  to NEW TEMPLATE as before. Verified against invoice 169.pdf (matched
  format53, PaddleOCR misread "Invoice No." as "Involce No." on a poor-
  quality scan) - now lands in UNSUPPORTED with no tracker row created.

### Item-table detection fix (born-digital PDFs)

- `find_table` (the scan that locates where an invoice's item table starts)
  scored keyword matches one row at a time. A layout whose column header
  wraps onto two stacked lines (e.g. "Sl. No Description Unit Qty Net Tax
  Tax Tax Total" / "Price Amount Rate Type Amount Amount") never reached
  the match threshold on either line alone, so the table was invisible to
  the pipeline end to end - a PART invoice (an Amazon marketplace invoice,
  IN-8362.pdf) landed at NEW TEMPLATE with "No item line found", even
  though every other part of its layout was already recognised (format40).
  `find_table` now also tries combining a row with the next one when
  neither alone reaches the threshold - but only when the next row
  doesn't already qualify by itself, so a genuine single-row header
  sitting just above an unrelated short label line (e.g. a "SN | CGST |
  SGST" sub-heading) isn't backdated a row early and dragged into the
  table, which as first written this fix would have quietly done for one
  sample file (`NMBK - 43.pdf`) - caught by re-running the full 290-file
  sample set before and after and diffing file-by-file, not just
  comparing aggregate counts. Note: this only fixes *detecting* the
  table for this layout - IN-8362.pdf's own item row still has its
  Quantity/Rate/Amount figures glued into one text block by a separate,
  unrelated tolerance setting in `_pdf_text_boxes` (tuned for how every
  other supported layout's phrase-level OCR-style boxes behave); fixing
  that safely needs its own careful pass, so that file still needs a
  human to complete it, it just no longer wrongly claims to need
  retraining.
- A related attempt - loosening the item-table cleaner's bare
  "igst"/"cgst"/"sgst" row filter so a real item row that happens to
  name its own tax type inline wouldn't be discarded - was tried and
  **reverted**. The first version gated on row length; that let through
  a tax sub-table's own multi-word column-header row (no real item, just
  verbose labels), corrupting item parsing for 4 other sample files. A
  second version gated on "does the row contain any digit" instead;
  that made things *worse* system-wide, since a genuine standalone tax
  line almost always contains a rate or amount (a digit) - it now kept
  rows the original code correctly dropped, spiking NEW TEMPLATE across
  57 sample files in one run. Getting this right needs the table's
  column positions (to check whether the row actually has a value under
  the Description column), which the cleaner doesn't have access to at
  the point it currently runs - left as still-open work rather than
  shipped as an unsafe heuristic. Caught before merging by rerunning the
  full 290-file sample set after every change and diffing the file-level
  result, not just trusting the aggregate pass/fail counts.
- Buyer state (`buyer_state` / the "State", "GST Order Address State",
  "Ship-to Address" Purchase Header columns) now falls back to Tamil
  Nadu when a vendor's layout prints no Buyer GSTIN and no labelled
  Buyer State field at all - this app only ever processes Precision
  Techserve's own inbound invoices, and Precision Techserve's own
  registered address never moves, so a blank buyer-side state here only
  ever means the vendor didn't print one, never that the buyer was
  actually somewhere else. A parallel idea - guessing the *seller's*
  state from free text in its (often small/unregistered vendor) address
  block when no GSTIN is printed either - was tried and **reverted**:
  for one sample vendor (SHWETMANI ENTERPRISES) the "Seller Address"
  field itself was already mis-anchored (it had captured unrelated
  invoice-body text, not the real address), and the text-search matched
  "GOA" inside "5. CAPRI, GOA, Break-Fix..." - silently writing the
  wrong state (Goa) instead of the vendor's real Maharashtra. A wrong
  GST state code is worse than a correctly-flagged NEW TEMPLATE, so this
  stays an open gap for small/unregistered vendors rather than a risky
  guess.

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
