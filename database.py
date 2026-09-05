"""
SQL Server access for PIIPS.

Connects using the connection string saved in config.json (db_connection),
which is in .NET SqlClient format; it's converted to a pyodbc connection
string here (ODBC Driver 17 for SQL Server).
"""

import binascii
import hashlib
import json
import os
import re
import secrets
import string

import pyodbc

import config_store
import secret_store


ODBC_DRIVER = "ODBC Driver 17 for SQL Server"

# A Data Mismatch/Excluded/New Template invoice left unresolved this long
# auto-parks as Manually Updated (see usp_ExpireStaleUnresolved /
# expire_stale_unresolved) - permanently, never reprocessable again.
# Unsupported gets the same treatment by filesystem age instead, since it
# never gets a database row at all (see config_store.expire_stale_files).
STALE_STATUS_EXPIRY_DAYS = 10


# Ordered status values for tbl_status.
STATUS_VALUES = [
    "NEW TEMPLATE",       # unrecognized-format PDFs, previously untracked entirely
    "DUPLICATE",
    "UNSUPPORTED",
    "INITIATED",
    "EXTRACTED",
    "BUYER ORDER NO DOESN'T EXIST",
    "SF PROCESSED",
    "PENDING IN SF",
    "DATA MISMATCH",      # renamed from "INCOMPLETE DATA" - see migration below
    "READY TO LOAD",
    "LOADED",
    "POSTED",
    "COMPLETED",
    "EXCLUDED",
    "REJECTED BY ACCOUNTS",
    "MANUALLY UPDATED",   # a Data Mismatch left unresolved past the retry
                           # window (see usp_ExpireStaleUnresolved) -
                           # permanently parked, never reprocessable again
]

# User type values for tbl_UserType. "Accounts" runs the Load/Post/Complete
# lifecycle; "Viewer" can see every menu/page but never change anything -
# every mutating endpoint rejects it via app.py's _require_not_viewer
# (see ROLE_MENUS in the frontend App.jsx).
USER_TYPE_VALUES = ["User", "Admin", "Super Admin", "Accounts", "Viewer"]

# Invoice type values for tbl_InvoiceType (formerly the free-text "GRN" /
# "NON-GRN" strings baked into template/folder paths — PART = GRN, SERVICE =
# NON-GRN).
INVOICE_TYPE_VALUES = ["PART", "SERVICE"]

# VALUES(...) list for seeding tbl_status (single quotes escaped).
_STATUS_SEED_VALUES = ", ".join(
    "('" + v.replace("'", "''") + "')" for v in STATUS_VALUES
)

# (StatusName, position) pairs for syncing tbl_status.DisplayOrder to
# STATUS_VALUES' own order on every startup - see the DisplayOrder DDL
# step below (decouples display order from StatusId, an IDENTITY frozen
# at whenever a status was first seeded on a given database).
_STATUS_DISPLAY_ORDER_VALUES = ", ".join(
    "('" + v.replace("'", "''") + f"', {i})" for i, v in enumerate(STATUS_VALUES)
)

# VALUES(...) list for seeding tbl_InvoiceType (single quotes escaped).
_INVOICE_TYPE_SEED_VALUES = ", ".join(
    "('" + v.replace("'", "''") + "')" for v in INVOICE_TYPE_VALUES
)


def _conn_val(raw, *keys):
    """Read a value from a .NET-style connection string by key (any alias)."""
    for key in keys:
        m = re.search(rf"{re.escape(key)}\s*=\s*([^;]+)", raw or "", re.IGNORECASE)
        if m:
            return m.group(1).strip()
    return ""


def parse_dotnet_connection(raw):
    """Break a .NET-style connection string into its parts. Windows auth is
    inferred when no User Id is present (matching how the pyodbc string is
    built). The password is included for internal use — never return it to a
    client.
    Sample: parse_dotnet_connection('Data Source=SQLSVR01;Initial Catalog=PIIPS;Integrated Security=True;')"""
    server = _conn_val(raw, "Data Source", "Server", "Addr", "Address")
    database = _conn_val(raw, "Initial Catalog", "Database")
    uid = _conn_val(raw, "User Id", "Uid", "User")
    pwd = _conn_val(raw, "Password", "Pwd")
    return {
        "server": server,
        "database": database,
        "uid": uid,
        "password": pwd,
        "has_password": bool(pwd),
        "trusted": not bool(uid),
    }


def build_dotnet_connection(server, database, uid="", pwd="", trusted=False):
    """Build a .NET-style connection string (the format stored in config).
    Sample: build_dotnet_connection('SQLSVR01', 'PIIPS', 'appuser', 'S3cret!', False)"""
    parts = [f"Data Source={server}", f"Initial Catalog={database}"]
    if trusted or not uid:
        parts.append("Integrated Security=True")
    else:
        parts += [f"User Id={uid}", f"Password={pwd or ''}"]
    return ";".join(parts) + ";"


def _pyodbc_from_dotnet(raw):
    """Convert a .NET-style connection string into a pyodbc string."""
    p = parse_dotnet_connection(raw)
    if not p["server"] or not p["database"]:
        raise RuntimeError("db_connection is not configured (server/database missing).")

    parts = [
        f"DRIVER={{{ODBC_DRIVER}}}",
        f"SERVER={p['server']}",
        f"DATABASE={p['database']}",
        "TrustServerCertificate=yes",
    ]
    if p["uid"]:
        parts += [f"UID={p['uid']}", f"PWD={p['password']}"]
    else:
        parts.append("Trusted_Connection=yes")

    return ";".join(parts) + ";"


def _pyodbc_connection_string():
    """Convert the configured .NET-style db_connection into a pyodbc string."""
    return _pyodbc_from_dotnet(config_store.load_config().get("db_connection", "") or "")


def test_connection(dotnet_conn, timeout=8):
    """Try to connect with the given .NET-style string. Returns (ok, error).
    Sample: test_connection('Data Source=SQLSVR01;Initial Catalog=PIIPS;Integrated Security=True;', 8)"""
    try:
        conn = pyodbc.connect(_pyodbc_from_dotnet(dotnet_conn), timeout=timeout)
        cur = conn.cursor()
        cur.execute("SELECT 1")
        cur.fetchone()
        conn.close()
        return True, ""
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)


def get_connection(timeout=15):
    """Open a pyodbc connection to the configured database. Sample: get_connection(15)"""
    return pyodbc.connect(_pyodbc_connection_string(), timeout=timeout)


def _q(name):
    """Safely bracket-quote a SQL identifier."""
    return "[" + str(name).replace("]", "]]") + "]"


def _ensure_table(cur, table, columns, extra=None):
    """Create the table if missing (Id + CreatedAt), then add any missing
    data columns (NVARCHAR(MAX)) plus any `extra` columns {name: ddl}."""
    cur.execute("SELECT 1 FROM sys.tables WHERE name = ?", table)
    if not cur.fetchone():
        cur.execute(
            f"CREATE TABLE {_q(table)} "
            f"(Id INT IDENTITY(1,1) PRIMARY KEY, "
            f"CreatedAt DATETIME NOT NULL DEFAULT GETDATE())"
        )

    cur.execute("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?)", table)
    # Compare case-insensitively — SQL Server identifiers are case-insensitive,
    # so a column stored as 'Purchase_Header_ID' must not be re-added because
    # the requested name differs only in case.
    existing = {r[0].lower() for r in cur.fetchall()}
    for col in columns:
        if col.lower() not in existing:
            cur.execute(f"ALTER TABLE {_q(table)} ADD {_q(col)} NVARCHAR(MAX) NULL")
    for col, ddl in (extra or {}).items():
        if col.lower() not in existing:
            cur.execute(f"ALTER TABLE {_q(table)} ADD {_q(col)} {ddl}")


def _norm(v):
    if v is None or v == "":
        return None
    if isinstance(v, (int, float)):
        return v
    return str(v)


def save_grouped(data, batch_name=None, tracker=None):
    """
    Bulk-insert Header/Line/Reservation rows for one processing batch through
    dbo.usp_SaveInvoiceBatch. Every row is handed to the procedure as JSON in
    a single call (not one INSERT per row); the procedure captures each
    inserted Purchase Header Id and stamps it onto that invoice's lines and
    reservations, preserving the parent link. Returns {table: rows_inserted}.

    `tracker` (optional) drives the purchase-tracker rows (one per header):
        {"started_by": int|None, "started_at": iso-str|None,
         "status": "processed",
         "initiators": [ {"id": int|None, "at": iso-str|None} | None, ... ]}
    The initiators list is aligned with `data["groups"]` (one entry per
    invoice/header).

    Sample: save_grouped(grouped, 'PIIPS_Batch_20260722_101500', tracker)

    [No.] (header) / [Document No.] (line) / [Source ID] (reservation) are
    deliberately saved blank — a real, unique Document No. is only minted
    at Excel-download time (see fetch_batch/_assign_document_numbers),
    resolving PO_Number_Format live from the template rather than storing
    it on the header at all. This mutates `data`'s groups in place — safe
    here since the caller (processor.py's _save_to_db) builds `data` fresh
    for this call and doesn't read it again afterward; in particular the
    mandatory-field completeness check (ready vs incomplete) already ran
    before this function is called, so blanking them now doesn't affect
    that classification — only the DB write.
    """
    for g in data.get("groups", []):
        header = g.get("header") or {}
        header["No."] = ""
        for line in g.get("lines") or []:
            line["Document No."] = ""
        for res in g.get("reservations") or []:
            res["Source ID"] = ""

    cols = data["columns"]
    ph_cols = list(cols["Purchase Header"])
    pl_cols = list(cols["Purchase Line"])
    re_cols = list(cols["Reservation Entry"])
    header_cols = ph_cols + ["InvoiceNo"]   # BatchName now lives on the tracker

    tracker = tracker or {}
    initiators = tracker.get("initiators") or []
    rels = tracker.get("rels") or []
    statuses = tracker.get("statuses") or []
    isactives = tracker.get("isactives") or []
    synced = tracker.get("synced") or []
    buyer_orders = tracker.get("buyer_orders") or []
    jsons = tracker.get("jsons") or []
    filenames = tracker.get("filenames") or []
    formats = tracker.get("formats") or []
    invoice_types = tracker.get("invoice_types") or []

    counts = {"tbl_Purchase_Header": 0, "tbl_Purchase_Line": 0, "tbl_Reservation_Entry": 0}

    # Build the JSON payloads, linking child rows to their header via _gid
    # (the group index) so the procedure can resolve the FK after insert.
    headers, lines, reservations = [], [], []
    for gid, g in enumerate(data.get("groups", [])):
        header = {c: _norm(g["header"].get(c)) for c in ph_cols}
        header["InvoiceNo"] = g.get("invoice_no")
        header["_BatchName"] = batch_name       # -> tracker.BatchName
        header["_gid"] = gid
        init = initiators[gid] if gid < len(initiators) else None
        header["_InitById"] = (init or {}).get("id")
        header["_InitAt"] = (init or {}).get("at")
        header["_rel"] = rels[gid] if gid < len(rels) else None
        header["_StatusName"] = statuses[gid] if gid < len(statuses) else tracker.get("status")
        active = isactives[gid] if gid < len(isactives) else True
        header["_IsActive"] = 1 if active else 0
        header["_Synced"] = 1 if (synced[gid] if gid < len(synced) else False) else 0
        header["_BuyerOrderNo"] = buyer_orders[gid] if gid < len(buyer_orders) else None
        header["_SourceJson"] = jsons[gid] if gid < len(jsons) else None
        header["_FileName"] = filenames[gid] if gid < len(filenames) else None
        header["_Format"] = formats[gid] if gid < len(formats) else None
        header["_InvoiceType"] = invoice_types[gid] if gid < len(invoice_types) else None
        headers.append(header)
        for line in g["lines"]:
            row = {c: _norm(line.get(c)) for c in pl_cols}
            row["_gid"] = gid
            lines.append(row)
        for res in g["reservations"]:
            row = {c: _norm(res.get(c)) for c in re_cols}
            row["_gid"] = gid
            reservations.append(row)

    if not headers:
        return counts

    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        # DDL (idempotent): create the tables and add any user-defined
        # columns. Schema changes stay in Python; only the row inserts run
        # through the procedure.
        _ensure_table(cur, "tbl_Purchase_Header", ph_cols,
                      extra={"InvoiceNo": "NVARCHAR(200) NULL"})
        _ensure_table(cur, "tbl_Purchase_Line", pl_cols, extra={"Purchase_Header_ID": "INT NULL"})
        _ensure_table(cur, "tbl_Reservation_Entry", re_cols, extra={"Purchase_Header_ID": "INT NULL"})
        conn.commit()

        cur.execute(
            "EXEC dbo.usp_SaveInvoiceBatch ?, ?, ?, ?, ?, ?, ?, ?, ?",
            json.dumps(header_cols), json.dumps(pl_cols), json.dumps(re_cols),
            json.dumps(headers), json.dumps(lines), json.dumps(reservations),
            tracker.get("started_by"), tracker.get("started_at"),
            tracker.get("status"),
        )
        conn.commit()
    finally:
        conn.close()

    counts["tbl_Purchase_Header"] = len(headers)
    counts["tbl_Purchase_Line"] = len(lines)
    counts["tbl_Reservation_Entry"] = len(reservations)
    return counts


def resync_pending(batch_name=None):
    """
    Re-attempt Service First for records still "pending in SF" (status 5).
    Reloads each record's extracted JSON, re-calls the API, and — when the
    part is now available — rebuilds that header's reservation rows and
    promotes the tracker (Ready to Load / sf processed) with a fresh
    SyncedDatetime. `batch_name`, when given, also moves each promoted
    record into that batch (same as reprocess_reworkable_header does for a
    re-uploaded Data Mismatch/New Template/Excluded file - a record that
    finally clears here belongs to the run that cleared it, not left
    behind in whatever batch it was originally saved under). Returns
    {"promoted": n, "errors": [reason, ...]}.

    Sample: resync_pending(batch_name='PIIPS_Batch_20260902_120000')
    """
    ensure_menu_schema()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetPendingSync")
        targets = [(r[0], r[1], r[2]) for r in cur.fetchall()]
    finally:
        conn.close()
    if not targets:
        return {"promoted": 0, "errors": []}

    import service_api
    import excel_export
    import template_store

    output_folder = (config_store.folders(create=False) or {}).get("output", "")

    promoted, errors = 0, []
    for header_id, _po, json_path in targets:
        if not json_path or not os.path.isfile(json_path):
            continue
        try:
            with open(json_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            continue

        verdict = service_api.enrich_invoice(data)
        if not data.get("sf_items"):
            continue  # still not received in SF — leave pending

        # Re-attach the template's static values (derived from the output
        # path <Output>/<entity>/<name>/...) so reservation constants apply.
        static, _ = template_store.static_for_path(output_folder, json_path)
        data["_static"] = static

        grouped = excel_export.build_rows_grouped([data])
        group = grouped["groups"][0] if grouped["groups"] else {"reservations": []}
        re_cols = grouped["columns"]["Reservation Entry"]

        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "EXEC dbo.usp_ReplaceReservation ?, ?, ?, ?, ?, ?, ?",
                header_id, json.dumps(re_cols), json.dumps(group["reservations"]),
                verdict["status"], 1 if verdict["is_active"] else 0, 1, batch_name,
            )
            conn.commit()
        finally:
            conn.close()

        promoted += 1
        errors.extend(verdict["errors"])

    return {"promoted": promoted, "errors": errors}


def expire_stale_unresolved(days=None):
    """Park every Data Mismatch/Excluded/New Template invoice that's sat
    unresolved for more than `days` (default STALE_STATUS_EXPIRY_DAYS) as
    Manually Updated - permanently: it drops out of batch status entirely
    (_BATCH_IGNORED_STATUSES) and, since Manually Updated is deliberately
    never in _REPROCESSABLE_STATUSES, a later re-upload of the same invoice
    falls through to DUPLICATE instead of merging in place. Unsupported is
    NOT covered here - see config_store.expire_stale_files for that (it
    never gets a database row at all). Returns the FileName of every row
    expired (the caller - processor.py's _expire_stale_unresolved - moves
    each PDF into the Manually Updated folder to match; this function only
    ever touches the DB).

    Sample: expire_stale_unresolved()"""
    ensure_menu_schema()
    days = STALE_STATUS_EXPIRY_DAYS if days is None else days
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_ExpireStaleUnresolved ?", days)
        filenames = [r[0] for r in cur.fetchall() if r[0]]
        conn.commit()
        return filenames
    finally:
        conn.close()


def apply_manual_buyer_order(header_id, order_no, user_id=None):
    """Set a manually-entered Buyer's Order No. on a parked invoice and
    re-validate it (Buyer Order Entry menu).
    Sample: apply_manual_buyer_order(42, 'SPRPUR/2026/04/27-83650', 7)

    Reloads the invoice's extracted JSON, writes the PO, clears the doubtful
    flag, re-runs Service First (enrich_invoice), rebuilds reservation rows when
    the part is now available, and updates the tracker status / IsActive so a
    clean invoice becomes active (status = 1). Returns
    {file_name, new_status, is_active, reason}, or None if the invoice row is
    missing."""
    order_no = (order_no or "").strip()
    if not order_no:
        return None
    ensure_menu_schema()

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT SourceJson, FileName FROM dbo.tbl_Purchase_Tracker "
            "WHERE Purchase_Header_ID = ?", header_id)
        row = cur.fetchone()
    finally:
        conn.close()
    if not row:
        return None
    json_path, file_name = row[0], row[1]

    import service_api
    import excel_export
    import template_store

    data = None
    if json_path and os.path.isfile(json_path):
        try:
            with open(json_path, "r", encoding="utf-8") as fp:
                data = json.load(fp)
        except (OSError, json.JSONDecodeError):
            data = None
    if data is None:
        return {"file_name": file_name, "new_status": None,
                "is_active": False, "reason": "Extracted JSON not found"}

    # Apply the user-supplied PO and clear the doubtful flag, then re-validate.
    data["buyer_order_no"] = order_no
    data["buyer_order_doubtful"] = False
    verdict = service_api.enrich_invoice(data)

    # Persist the corrected JSON so a later re-sync sees the PO.
    try:
        with open(json_path, "w", encoding="utf-8") as fp:
            json.dump(data, fp, indent=4, ensure_ascii=False)
    except OSError:
        pass

    # If the part is now received in SF, rebuild this header's reservation rows.
    if data.get("sf_items"):
        output_folder = (config_store.folders(create=False) or {}).get("output", "")
        static, _ = template_store.static_for_path(output_folder, json_path)
        data["_static"] = static
        grouped = excel_export.build_rows_grouped([data])
        group = grouped["groups"][0] if grouped["groups"] else {"reservations": []}
        re_cols = grouped["columns"]["Reservation Entry"]
        conn = get_connection()
        try:
            cur = conn.cursor()
            cur.execute(
                "EXEC dbo.usp_ReplaceReservation ?, ?, ?, ?, ?, ?",
                header_id, json.dumps(re_cols), json.dumps(group["reservations"]),
                verdict["status"], 1 if verdict["is_active"] else 0, 1,
            )
            conn.commit()
        finally:
            conn.close()

    # Update the tracker PO / status / IsActive / IsSynced and the header column.
    res = set_buyer_order_no(header_id, order_no, verdict, user_id)
    return {
        "file_name": (res or {}).get("file_name", file_name),
        "new_status": verdict["status"],
        "is_active": verdict["is_active"],
        "reason": verdict.get("reason", ""),
    }


def status_id(name):
    """StatusId for a status name (case-insensitive), or None.
    Sample: status_id('READY TO LOAD')"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT StatusId FROM dbo.tbl_status WHERE StatusName = ?", name)
        r = cur.fetchone()
        return r[0] if r else None
    finally:
        conn.close()


def status_counts():
    """[{"status_id", "status", "count"}] — count of purchase headers grouped
    by their tracker status (for the dashboard pie chart).
    Sample: status_counts()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_StatusCounts")
        return [{"status_id": r[0], "status": r[1], "count": r[2]} for r in cur.fetchall()]
    finally:
        conn.close()


def log_input_files(files, initiated_by=None):
    """Record uploaded files (who copied them in File Explorer) with status
    'initiated'. `files` is a list of {"RelPath":.., "FileName":..}. Bulk
    insert via dbo.usp_LogInputFiles (one call).
    Sample: log_input_files([{'RelPath': 'SPR/Bosch/inv1.pdf', 'FileName': 'inv1.pdf'}], 7)"""
    files = [f for f in (files or []) if f.get("FileName")]
    if not files:
        return
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_LogInputFiles ?, ?", json.dumps(files), initiated_by)
        conn.commit()
    finally:
        conn.close()


def get_initiators(relpaths):
    """{relpath: {"id": InitiatedByID, "at": iso-datetime}} — the most recent
    uploader for each requested relative path.
    Sample: get_initiators(['SPR/Bosch/inv1.pdf', 'SPR/Bosch/inv2.pdf'])"""
    relpaths = [p for p in (relpaths or []) if p]
    if not relpaths:
        return {}
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetInitiators ?", json.dumps(relpaths))
        out = {}
        for relpath, uid, at in cur.fetchall():
            out[relpath] = {"id": uid, "at": at.isoformat() if at is not None else None}
        return out
    finally:
        conn.close()


def get_initiated_files(relpaths):
    """{relpath: {"file", "by_id", "by_name", "at"}} for the requested paths
    that were already initiated (uploaded) before. Used to skip duplicates on
    upload and show who initiated each one.
    Sample: get_initiated_files(['SPR/Bosch/inv1.pdf'])"""
    relpaths = [p for p in (relpaths or []) if p]
    if not relpaths:
        return {}
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetInitiatedFiles ?", json.dumps(relpaths))
        out = {}
        for relpath, fname, uid, uname, at in cur.fetchall():
            out[relpath] = {
                "file": fname,
                "by_id": uid,
                "by_name": uname,
                "at": at.isoformat() if at is not None else None,
            }
        return out
    finally:
        conn.close()


def reset_input_files(relpaths):
    """Set StatusID = 0 on the input-file log rows for the given relative
    paths (files moved to New_Format), so any user can upload them again.
    Sample: reset_input_files(['SPR/Bosch/inv1.pdf'])"""
    relpaths = [p for p in (relpaths or []) if p]
    if not relpaths:
        return
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_ResetInputFiles ?", json.dumps(relpaths))
        conn.commit()
    finally:
        conn.close()


def get_processed_invoices():
    """
    {(invoice_no, buyer_order_no_lower): {"batch": earliest_batch,
        "header_id": int, "reprocessable": bool}}
    for every invoice already saved in tbl_Purchase_Header (any batch) -
    keyed on the combination of Invoice No. and Buyer's Order No. together
    (not Invoice No. alone - different vendors, or the same vendor's
    invoices against different POs, can coincidentally reuse an invoice
    number). Used to skip re-processing an invoice that is already in the
    database - unless its only existing record is in one of
    _REPROCESSABLE_STATUSES (Excluded/Pending In SF/Data Mismatch/New
    Template), in which case the caller re-processes it into that same
    header instead of flagging a duplicate (see processor.py and
    reprocess_reworkable_header). Backfills InvoiceNo from the mapped
    'Vendor Invoice No.' column for rows saved before InvoiceNo existed.

    Sample: get_processed_invoices()
    """
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetProcessedInvoices")
        out = {}
        for inv_no, batch, header_id, is_reprocessable, buyer_order_no in cur.fetchall():
            po = str(buyer_order_no or "").strip().lower()
            out[(inv_no, po)] = {
                "batch": batch, "header_id": header_id,
                "reprocessable": bool(is_reprocessable),
            }
        return out
    finally:
        conn.close()



# Statuses an existing header may be re-processed FROM (in place, same
# header/batch/No.) instead of the re-upload being parked as a DUPLICATE -
# see reprocess_reworkable_header. All four represent an invoice that never
# reached a real, final outcome the first time - Excluded is a deliberate
# drop (a real re-inclusion - see _mark_batch_reincluded); Pending In SF
# just means the part hadn't reached Service First yet; Data Mismatch and
# New Template mean the data/format wasn't usable last time - in every
# case, a fresh upload deserves a fresh look rather than being told it's a
# duplicate of itself. This re-processing only ever happens when a USER
# deliberately re-uploads that exact file and starts a run - nothing here
# pulls a file back in on its own (see processor.py's main loop, which is
# the only caller, driven by whatever the user just uploaded).
# MANUALLY UPDATED is deliberately NOT in this list - it's what Data
# Mismatch/Excluded/New Template becomes once usp_ExpireStaleUnresolved
# parks it as permanently unresolved (Pending In SF is NOT swept by that
# expiry - a re-upload can still merge into it as before); unlike the
# four statuses below, a re-upload of a Manually Updated invoice must
# fall through to DUPLICATE, never merge back in.
# KEEP IN SYNC with usp_GetProcessedInvoices' own copy of this list.
_REPROCESSABLE_STATUSES = ("EXCLUDED", "PENDING IN SF", "DATA MISMATCH", "NEW TEMPLATE")


def reprocess_reworkable_header(existing_header_id, new_header_id):
    """After a fresh save_grouped() insert created `new_header_id` for an
    invoice whose only prior record (`existing_header_id`) is in one of
    _REPROCESSABLE_STATUSES, merge the newly-inserted row's data back onto
    the EXISTING Header and Tracker rows in place (same Id - the invoice a
    user has been looking at stays the same record, genuinely updated, not
    replaced by a new one), replace its Line/Reservation children with the
    freshly-inserted ones (re-parented onto the existing header - counts
    can legitimately differ from before, so there's no stable old-row-to-
    new-row identity to update against), then discard the now-empty
    temporary new_header_id shell. Re-checks the existing tracker's status
    itself (not just trusting a caller's earlier read) so this can never
    silently overwrite a live invoice under any other status. The existing
    header's [No.] is preserved as-is (not overwritten by the new insert's
    blank one) - re-processing brings in fresh extracted data, not a fresh
    Document No.; whatever it already had (even blank) is left exactly as
    it is, to be minted fresh at the next download of whichever batch it
    ends up in regardless (see fetch_batch/_assign_document_numbers, which
    always re-mints on every download). BatchName, however, IS overwritten
    - a reprocessed invoice moves into the batch of the run that just
    reprocessed it (e.g. re-uploaded and re-Started alongside genuinely new
    files), not left behind in whatever batch it belonged to before.
    Sample: reprocess_reworkable_header(42, 57)"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT s.StatusName FROM dbo.tbl_Purchase_Tracker pt "
            "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
            "WHERE pt.Purchase_Header_ID = ?",
            existing_header_id,
        )
        row = cur.fetchone()
        existing_status = (row[0] or "").strip().upper() if row else ""
        if not row or existing_status not in _REPROCESSABLE_STATUSES:
            return False

        # ---- Header: copy every real data column from new -> existing,
        # EXCEPT [No.] - re-processing brings in fresh extracted data, not
        # a fresh Document No.; whatever No. this invoice already had (even
        # blank) is left exactly as it is, to be minted at the next
        # download of whichever batch it ends up in (see fetch_batch) ----
        cur.execute(
            "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_Purchase_Header')")
        h_cols = [r[0] for r in cur.fetchall() if r[0] not in ("Id", "CreatedAt", "No.")]
        if h_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in h_cols)} FROM dbo.tbl_Purchase_Header WHERE Id = ?",
                new_header_id,
            )
            new_row = cur.fetchone()
            if new_row:
                set_clause = ", ".join(f"{_q(c)} = ?" for c in h_cols)
                cur.execute(
                    f"UPDATE dbo.tbl_Purchase_Header SET {set_clause} WHERE Id = ?",
                    *new_row, existing_header_id,
                )

        # ---- Line / Reservation: existing children replaced wholesale by
        # the freshly-inserted ones, re-parented onto the existing header ----
        for child_table in ("tbl_Purchase_Line", "tbl_Reservation_Entry"):
            cur.execute(f"DELETE FROM dbo.{child_table} WHERE Purchase_Header_ID = ?", existing_header_id)
            cur.execute(
                f"UPDATE dbo.{child_table} SET Purchase_Header_ID = ? WHERE Purchase_Header_ID = ?",
                existing_header_id, new_header_id,
            )

        # ---- Tracker: copy the new tracker's fields onto the EXISTING
        # tracker row in place (clears IsExcluded/PriorStatusID, adopts the
        # fresh verdict/status), then drop the temporary new tracker.
        # BatchName IS included here (not excluded) - a reprocessed invoice
        # moves into the batch of the run that just reprocessed it, same as
        # any genuinely new file in that same run, rather than staying
        # associated with whatever batch it belonged to before ----
        cur.execute(
            "SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('dbo.tbl_Purchase_Tracker')")
        t_cols = [r[0] for r in cur.fetchall()
                  if r[0] not in ("Id", "Purchase_Header_ID", "CreatedById", "CreatedDatetime")]
        if t_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in t_cols)} FROM dbo.tbl_Purchase_Tracker "
                "WHERE Purchase_Header_ID = ?",
                new_header_id,
            )
            new_row = cur.fetchone()
            if new_row:
                set_clause = ", ".join(f"{_q(c)} = ?" for c in t_cols)
                cur.execute(
                    f"UPDATE dbo.tbl_Purchase_Tracker SET {set_clause} WHERE Purchase_Header_ID = ?",
                    *new_row, existing_header_id,
                )

        cur.execute(
            "UPDATE dbo.tbl_InputFile_Log SET Purchase_Header_ID = ? WHERE Purchase_Header_ID = ?",
            existing_header_id, new_header_id,
        )
        cur.execute("DELETE FROM dbo.tbl_Purchase_Tracker WHERE Purchase_Header_ID = ?", new_header_id)
        cur.execute("DELETE FROM dbo.tbl_Purchase_Header WHERE Id = ?", new_header_id)
        # No _mark_batch_reincluded call here: since the invoice now moves
        # into the NEW batch entirely (see the BatchName note above), its
        # OLD batch no longer contains it at all - marking the old batch
        # "reincluded" wouldn't reflect anything real about it anymore,
        # and the new batch is freshly Created and already
        # displays accurately as such.
        conn.commit()
        return True
    finally:
        conn.close()


# Any invoice reaching one of these means the batch's Document No.
# sequence may already be partly committed (Loaded into Navision, or
# deliberately dropped) - see _batch_status_and_lock/batch_is_locked.
# Excluded is deliberately NOT here: it's ignored entirely (see
# _BATCH_IGNORED_STATUSES) and never locks the batch on its own - the
# batch's lock state is always driven by its currently-included invoices.
_BATCH_LOCK_STATUSES = ("LOADED", "POSTED", "COMPLETED", "REJECTED BY ACCOUNTS")


# Statuses ignored entirely when deciding whether a batch is "cleared" (see
# _batch_is_cleared) or computing its status label (see
# _batch_status_and_lock). Each is either inert (Excluded/Duplicate never
# need further action - a duplicate is already handled under a different
# header) or off on its own separate resolution path (New Template needs
# retraining, Data Mismatch needs a data fix, Pending In SF needs Service
# First to catch up, Manually Updated is permanently parked and will never
# move again - see usp_ExpireStaleUnresolved) that shouldn't hold up this
# batch's own status label, or any batch behind it, indefinitely - e.g. 9
# real invoices Loaded plus 1 still Pending In SF must show the batch as
# LOADED, not stuck looking unresolved forever waiting on a part SF may
# take days to receive.
_BATCH_IGNORED_STATUSES = (
    "EXCLUDED", "NEW TEMPLATE", "DUPLICATE", "DATA MISMATCH", "PENDING IN SF",
    "MANUALLY UPDATED",
)


def _batch_is_cleared(counts):
    """True if every invoice in a batch NOT in _BATCH_IGNORED_STATUSES has
    reached LOADED/POSTED/COMPLETED, OR none of them have reached that far
    YET (still sitting at Ready To Load, say) - a batch that hasn't started
    doesn't hold up a later batch either. Only PARTIAL progress - some
    invoices Loaded/Posted/Completed, others not - counts as not cleared,
    since that's real, unfinished work for this batch specifically."""
    counted_total = sum(c for s, c in counts.items() if s not in _BATCH_IGNORED_STATUSES)
    cleared = counts.get("LOADED", 0) + counts.get("POSTED", 0) + counts.get("COMPLETED", 0)
    return cleared == 0 or cleared == counted_total


def _batch_status_and_lock(counts, downloaded, ever_reincluded=False):
    """(batch_status, locked) derived from a batch's {status: count} map and
    whether it's ever been downloaded. Precedence: Excluded > Completed >
    Loaded/Posted (uniform) > In Progress > Downloaded > Created. The label
    is always computed from the batch's currently-INCLUDED invoices -
    Excluded ones are ignored entirely (see _BATCH_IGNORED_STATUSES) and
    never lock the batch or move its label off of what the remaining
    invoices would show on their own.
    - Created: nothing downloaded yet, nothing moved on.
    - Downloaded: the Excel has been pulled at least once, nothing moved on
      (an Excluded invoice sitting alongside all-Ready-To-Load invoices
      does NOT change this - it's ignored).
    - Loaded / Posted: every invoice outside _BATCH_IGNORED_STATUSES has
      reached that SAME single stage (see _batch_is_cleared) - e.g. one
      Loaded invoice plus any number of Excluded/Data Mismatch/Duplicate/
      New Template invoices still counts as "Loaded".
    - In Progress: at least one invoice (outside _BATCH_IGNORED_STATUSES)
      has reached Loaded/Posted/Completed/Rejected but NOT every one has
      reached the SAME stage (see batch_is_locked/_batch_is_cleared) - real,
      partial progress. No more downloading or Document No./Entry No.
      renumbering for the WHOLE batch, since part of its sequence may
      already be committed. A batch where NOTHING has moved past Ready To
      Load yet is NOT "in progress" even if it has Excluded/Data Mismatch/
      Duplicate rows sitting in it - those don't represent real work
      started on this batch.
    - Excluded: every invoice in the batch is Excluded (none Completed) -
      the whole batch was dropped, not finished.
    - Completed: every invoice is Completed or Excluded (at least one
      actually Completed).
    A batch that has ever had a re-inclusion (ever_reincluded - see
    _batch_ever_reincluded, set by both the manual Include button and
    reprocessing an Excluded invoice back to a real status) is ALWAYS shown
    as at least Downloaded, taking priority even over "In Progress" - e.g.
    a batch already Loaded except for one invoice that got re-included and
    is sitting at Ready To Load again still shows Downloaded, not In
    Progress, since the point of including a file back in is exactly to
    download the batch again and mint IT a fresh number - the already-
    Loaded invoices elsewhere still keep the batch 'locked' (returned
    separately), so nothing about them gets renumbered by that download."""
    total = sum(counts.values())
    locked = any(counts.get(s, 0) > 0 for s in _BATCH_LOCK_STATUSES)
    if total > 0 and counts.get("EXCLUDED", 0) == total:
        return "EXCLUDED", locked

    completed_or_excluded = counts.get("COMPLETED", 0) + counts.get("EXCLUDED", 0)
    if total > 0 and completed_or_excluded == total:
        return "COMPLETED", locked

    counted_total = sum(c for s, c in counts.items() if s not in _BATCH_IGNORED_STATUSES)
    reached = {s for s in ("LOADED", "POSTED", "COMPLETED") if counts.get(s, 0) > 0}
    if counted_total > 0 and len(reached) == 1 and _batch_is_cleared(counts):
        return next(iter(reached)), locked
    if ever_reincluded:
        return "DOWNLOADED", locked
    if locked:
        return "IN PROGRESS", locked
    if downloaded:
        return "DOWNLOADED", locked
    return "CREATED", locked


def list_batches():
    """Batches (newest first), each with a per-status count map keyed by status
    name (one entry per StatusID that occurs in the batch's tracker rows),
    plus a derived batch_status ('CREATED' / 'DOWNLOADED' / 'LOADED' / 'POSTED' /
    'COMPLETED' / 'EXCLUDED' - see _batch_status_and_lock), 'locked' (Document No./Entry No.
    renumbering must be disabled - see batch_is_locked), and
    'blocked_by' (names of EARLIER, not-yet-cleared batches that must reach
    Loaded/Posted/Completed - see _batch_is_cleared - before THIS batch may
    be downloaded at all; batches must download in order so a later
    batch's Document Nos. never get ahead of an earlier one still pending).
    Sample: list_batches()
    Returns [{batch, created, headers, exportable, counts, batch_status, locked, blocked_by}]."""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_ListBatches")

        # Result set 1: per-batch summary (ordered newest first).
        order, by_batch = [], {}
        for r in cur.fetchall():
            batch = r[0]
            by_batch[batch] = {
                "batch": batch,
                "created": str(r[1]) if r[1] is not None else "",
                "headers": r[2] or 0,
                "exportable": r[3] or 0,
                "counts": {},
            }
            order.append(batch)

        # Result set 2: per (batch, status) tracker-row counts.
        cur.nextset()
        for r in cur.fetchall():
            batch, _sid, sname, cnt = r[0], r[1], r[2], r[3]
            if batch in by_batch and sname:
                by_batch[batch]["counts"][sname] = cnt or 0

        downloaded = set()
        if _table_exists(cur, "tbl_BatchDownload") and order:
            placeholders = ", ".join("?" for _ in order)
            cur.execute(
                f"SELECT BatchName FROM dbo.tbl_BatchDownload WHERE BatchName IN ({placeholders})",
                *order)
            downloaded = {r[0] for r in cur.fetchall()}

        reincluded = set()
        if _table_exists(cur, "tbl_BatchReIncluded") and order:
            placeholders = ", ".join("?" for _ in order)
            cur.execute(
                f"SELECT BatchName FROM dbo.tbl_BatchReIncluded WHERE BatchName IN ({placeholders})",
                *order)
            reincluded = {r[0] for r in cur.fetchall()}

        for batch in order:
            row = by_batch[batch]
            row["batch_status"], row["locked"] = _batch_status_and_lock(
                row["counts"], batch in downloaded, batch in reincluded)

        # `order` is BatchName DESC (newest first), so everything AFTER a
        # batch in this list was created BEFORE it - exactly the "earlier
        # batches" a download must wait on. Batches must clear in creation
        # order, so this is computed once here rather than left to the
        # caller to get right.
        cleared = {b: _batch_is_cleared(by_batch[b]["counts"]) for b in order}
        for i, batch in enumerate(order):
            earlier = order[i + 1:]
            by_batch[batch]["blocked_by"] = [b for b in earlier if not cleared[b]]

        # Returned oldest-first: the batch that needs attention first (to
        # unblock everything newer) leads the list.
        return [by_batch[b] for b in reversed(order)]
    finally:
        conn.close()


def is_batch_locked(batch_name):
    """Public wrapper for batch_is_locked() - opens its own connection.
    Sample: is_batch_locked('PIIPS_Batch_20260722_101500')"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        return batch_is_locked(conn.cursor(), batch_name)
    finally:
        conn.close()


def mark_batch_downloaded(batch_name):
    """Record that a batch's Excel was just downloaded (for the Dashboard's
    Batch Status column - see list_batches). Upserts one row per batch.
    Sample: mark_batch_downloaded('PIIPS_Batch_20260722_101500')"""
    if not batch_name:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        if not _table_exists(cur, "tbl_BatchDownload"):
            return
        cur.execute(
            "UPDATE dbo.tbl_BatchDownload SET LastDownloadedAt = GETDATE(), "
            "DownloadCount = DownloadCount + 1 WHERE BatchName = ?",
            batch_name)
        if cur.rowcount == 0:
            cur.execute(
                "INSERT INTO dbo.tbl_BatchDownload "
                "(BatchName, FirstDownloadedAt, LastDownloadedAt, DownloadCount) "
                "VALUES (?, GETDATE(), GETDATE(), 1)",
                batch_name)
        conn.commit()
    finally:
        conn.close()


def list_statuses():
    """Ordered [(StatusId, StatusName), ...] from tbl_status — used to render a
    stable column per status in the dashboard batches table.
    Sample: list_statuses()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        if not _table_exists(cur, "tbl_status"):
            return []
        cur.execute("SELECT StatusId, StatusName FROM dbo.tbl_status ORDER BY StatusId")
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


# Same pattern as excel_export._DOC_NO_SUFFIX_RE - splits a doc number into
# its prefix and trailing numeric sequence (e.g. "PO-2627-000010" ->
# ("PO-2627-", "000010")) so a changed PO_Number_Format can be re-applied
# to an existing sequence without disturbing the sequence itself.
_DOC_NO_SUFFIX_RE = re.compile(r"^(.*?)(\d+)$")


def _next_doc_no_seq(cur, po_fmt, exclude_header_ids=None):
    """Next sequence number for a PO_Number_Format prefix, continuing from
    the highest number already used in tbl_Purchase_Header.[No.] under that
    exact prefix - never restarting at 1 for a fresh batch/run - so two
    separate downloads that happen to share a prefix can never mint the
    same Document No.

    `exclude_header_ids` (the header(s) about to be re-minted in this very
    call - see _assign_document_numbers, which re-mints every header in the
    batch on every download, even ones that already have a number) are left
    out of the "already used" scan. Without this, a batch's own current
    numbers would always count against itself, so a re-download could never
    reuse the range an Excluded sibling just freed (see set_excluded, which
    clears an Excluded invoice's [No.] entirely) - the remaining included
    invoices would just keep climbing to ever-higher numbers on every
    re-download instead of closing the gap and staying in their original
    order."""
    like_escaped = po_fmt.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    exclude_ids = {h for h in (exclude_header_ids or []) if h is not None}
    cur.execute(
        "SELECT Id, [No.] FROM dbo.tbl_Purchase_Header WHERE [No.] LIKE ? ESCAPE '\\'",
        like_escaped + "%",
    )
    best = 0
    for header_id, no in cur.fetchall():
        if header_id in exclude_ids or not no or not no.startswith(po_fmt):
            continue
        suffix = no[len(po_fmt):]
        if suffix.isdigit():
            best = max(best, int(suffix))
    return best + 1


def _batch_ever_reincluded(cur, batch_name):
    """True if this batch has ever had a previously-Excluded invoice
    Included back in (see set_excluded/_mark_batch_reincluded). Once that
    happens the batch is shown as at least Downloaded (not locked) even if
    every remaining invoice reverts to a pre-Loaded status (e.g. Ready To
    Load) - a re-inclusion is a real action taken on the batch, so it
    shouldn't quietly look freshly Created again.
    Sample: _batch_ever_reincluded(cur, 'PIIPS_Batch_20260722_101500')"""
    if not _table_exists(cur, "tbl_BatchReIncluded"):
        return False
    cur.execute("SELECT 1 FROM dbo.tbl_BatchReIncluded WHERE BatchName = ?", batch_name)
    return cur.fetchone() is not None


def _mark_batch_reincluded(cur, batch_name):
    """Record that batch_name has had a re-inclusion - see
    _batch_ever_reincluded. Idempotent; call within an already-open
    transaction (the caller commits)."""
    if not batch_name or not _table_exists(cur, "tbl_BatchReIncluded"):
        return
    cur.execute("SELECT 1 FROM dbo.tbl_BatchReIncluded WHERE BatchName = ?", batch_name)
    if cur.fetchone() is None:
        cur.execute(
            "INSERT INTO dbo.tbl_BatchReIncluded (BatchName, MarkedAt) VALUES (?, GETDATE())",
            batch_name)


def batch_is_locked(cur, batch_name):
    """True once at least one invoice in this batch has reached a status in
    _BATCH_LOCK_STATUSES (Loaded/Posted/Completed/Rejected). Excluded never
    locks the batch on its own - it's ignored entirely (see
    _BATCH_IGNORED_STATUSES) so the batch's lock state is always driven by
    its currently-INCLUDED invoices. From the locked point some of the
    batch's Document Nos. may already be committed in Navision (or
    deliberately dropped), so its [No.] sequence is frozen: fetch_batch
    must never renumber it on a re-download, and the whole batch can no
    longer be downloaded or have its Document No./Entry No. renumbered
    (see download_batch) - see also _batch_status_and_lock, which derives
    the same condition from counts already in hand. A re-inclusion
    (_batch_ever_reincluded) does NOT lock the batch - the point of
    including a file back in is to be able to download the batch again and
    mint it a fresh number.
    Sample: batch_is_locked(cur, 'PIIPS_Batch_20260722_101500')"""
    placeholders = ", ".join("?" for _ in _BATCH_LOCK_STATUSES)
    cur.execute(
        "SELECT COUNT(*) FROM dbo.tbl_Purchase_Tracker pt "
        "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
        f"WHERE pt.BatchName = ? AND s.StatusName IN ({placeholders})",
        batch_name, *_BATCH_LOCK_STATUSES)
    return cur.fetchone()[0] > 0


def _po_number_formats_for_headers(cur, header_ids):
    """{header_id: PO_Number_Format} resolved LIVE from each header's
    template, via its source file's path (tbl_InputFile_Log.RelPath ->
    template_store) - PO_Number_Format is never stored on
    tbl_Purchase_Header, so this always reflects the template's current
    setting, not a stale value frozen at processing time. A header whose
    template can't be resolved (deleted since, or no matching file log
    row) is simply absent from the result."""
    header_ids = [h for h in header_ids if h is not None]
    if not header_ids:
        return {}
    placeholders = ",".join("?" * len(header_ids))
    cur.execute(
        f"SELECT Purchase_Header_ID, RelPath FROM tbl_InputFile_Log "
        f"WHERE Purchase_Header_ID IN ({placeholders})",
        *header_ids,
    )
    relpath_by_header = {r[0]: r[1] for r in cur.fetchall() if r[1]}
    if not relpath_by_header:
        return {}
    import template_store
    current_templates = get_templates_data()
    result = {}
    for header_id, rel in relpath_by_header.items():
        entity, invoice_type, name = template_store.entity_type_name_from_relpath(rel)
        if entity is None:
            continue
        current = current_templates.get(f"{entity}\\{invoice_type}\\{name}")
        if current is None:
            continue
        result[header_id] = current.get("PO_Number_Format", "") or ""
    return result


def _find_no_collision(cur, id_to_no, batch_name=None):
    """Check a proposed {header_id: no} mapping against every header
    ALREADY in the table before it's written. Returns (no, other_batch)
    for the first Document No. that's already in use by a header in a
    DIFFERENT batch, or None if the whole mapping is safe to write. Used by
    both the default per-download minting (_assign_document_numbers) and a
    user's custom renumber (write_document_numbers) so a duplicate Document
    No. across two batches is refused outright rather than silently
    written - see the callers for what happens on a collision.

    Pass `batch_name` (the batch the caller is writing) so a match that
    belongs to that SAME batch is never treated as a collision. Without
    this, re-downloading a batch that has an Excluded invoice (which
    permanently keeps its own old Document No. - usp_FetchBatch's @idset
    stops offering it for re-minting once Excluded) could see its own
    sibling's frozen number and refuse the whole re-download, reporting
    that batch as colliding "with another batch" that is, confusingly,
    itself."""
    for header_id, no in id_to_no.items():
        if not no:
            continue
        cur.execute(
            "SELECT TOP 1 pt.BatchName FROM dbo.tbl_Purchase_Header h "
            "LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.Purchase_Header_ID = h.Id "
            "WHERE h.[No.] = ? AND h.Id <> ?",
            no, header_id)
        row = cur.fetchone()
        if row and (not batch_name or (row[0] or "") != batch_name):
            return no, row[0]
    return None


def _no_collision_error(no, other_batch):
    return ValueError(
        f"The No. '{no}' is already processed with another batch"
        + (f" ('{other_batch}')" if other_batch else "")
        + " - download aborted, nothing was changed. Refresh and try again."
    )


def _assign_document_numbers(cur, header_rows, batch_name=None):
    """Mint a FRESH, real, globally-unique Document No. for every header
    row passed in - every call re-mints, even a header that already has
    one (see fetch_batch, which skips calling this entirely once the batch
    is locked) - continuing each header's live template PO_Number_Format's
    sequence from whatever's already used OUTSIDE this same set of headers,
    and writes it directly to tbl_Purchase_Header.[No.] (propagated onto
    that header's tbl_Purchase_Line.[Document No.] / tbl_Reservation_Entry.
    [Source ID] rows). Mutates each row's "No."/"PO_Number_Format" in
    place; PO_Number_Format is resolved live here, never stored.

    Headers are numbered in ascending Id (original creation) order - so
    excluding one invoice and re-downloading closes the gap it leaves and
    keeps the rest in their original relative order, rather than each
    header just climbing to an ever-higher number on every re-download
    (see _next_doc_no_seq, which excludes this same header set from its
    own "already used" scan so a batch's own current numbers never block
    reusing the range an Excluded sibling just freed - see set_excluded).

    Computes every header's new number FIRST and checks the whole set for
    a collision against another batch's already-persisted [No.] (see
    _find_no_collision, passed this same `batch_name` so a match that's
    actually one of this batch's OWN other headers is never treated as a
    collision) before writing anything - raises ValueError and writes
    nothing at all if one is found (a concurrent download's race, or two
    headers whose no-template fallback happens to collide), rather than
    silently letting two batches share a Document No."""
    header_rows = sorted(header_rows, key=lambda r: r.get("Id") or 0)
    header_ids = [row.get("Id") for row in header_rows]
    po_fmt_by_header = _po_number_formats_for_headers(cur, header_ids)
    seq_by_prefix = {}
    proposed = {}
    for row in header_rows:
        header_id = row.get("Id")
        if header_id is None:
            continue
        po_fmt = po_fmt_by_header.get(header_id, "")
        row["PO_Number_Format"] = po_fmt
        if po_fmt:
            if po_fmt not in seq_by_prefix:
                seq_by_prefix[po_fmt] = _next_doc_no_seq(cur, po_fmt, header_ids)
            new_no = f"{po_fmt}{seq_by_prefix[po_fmt]:06d}"
            seq_by_prefix[po_fmt] += 1
        else:
            # No template prefix on record - fall back to the invoice's own
            # number so it's at least deterministic, never blank.
            new_no = (row.get("InvoiceNo") or f"DOC{header_id}").strip()
        proposed[header_id] = new_no

    collision = _find_no_collision(cur, proposed, batch_name)
    if collision:
        raise _no_collision_error(*collision)

    for row in header_rows:
        header_id = row.get("Id")
        if header_id not in proposed:
            continue
        new_no = proposed[header_id]
        row["No."] = new_no
        cur.execute("UPDATE dbo.tbl_Purchase_Header SET [No.] = ? WHERE Id = ?", new_no, header_id)
        if _existing_cols(cur, "tbl_Purchase_Line", ["Document No."]):
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Line SET [Document No.] = ? WHERE Purchase_Header_ID = ?",
                new_no, header_id)
        if _table_exists(cur, "tbl_Reservation_Entry") and _existing_cols(cur, "tbl_Reservation_Entry", ["Source ID"]):
            cur.execute(
                "UPDATE dbo.tbl_Reservation_Entry SET [Source ID] = ? WHERE Purchase_Header_ID = ?",
                new_no, header_id)


def write_document_numbers(id_to_no, batch_name=None):
    """Bulk-write an explicit {header_id: no} mapping straight to
    tbl_Purchase_Header.[No.] (used for a user-chosen custom renumber, see
    excel_export.renumber_batch), propagating each value onto that header's
    Purchase_Line.[Document No.] and Reservation_Entry.[Source ID] rows the
    same way. Checks the whole mapping against every other header's
    already-persisted [No.] first (see _find_no_collision) and writes
    nothing at all - raising ValueError instead - if a custom-chosen number
    collides with one another batch already has. Pass `batch_name` (the
    batch being renumbered) so a match against one of its OWN other headers
    is never treated as a collision.
    Sample: write_document_numbers({42: 'PO-2627-000010'}, 'PIIPS_Batch_20260722_101500')"""
    pairs = [(int(i), n) for i, n in (id_to_no or {}).items() if i is not None and n]
    if not pairs:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        collision = _find_no_collision(cur, dict(pairs), batch_name)
        if collision:
            raise _no_collision_error(*collision)
        cur.executemany(
            "UPDATE dbo.tbl_Purchase_Header SET [No.] = ? WHERE Id = ?",
            [(n, i) for i, n in pairs])
        cur.executemany(
            "UPDATE dbo.tbl_Purchase_Line SET [Document No.] = ? WHERE Purchase_Header_ID = ?",
            [(n, i) for i, n in pairs])
        if _table_exists(cur, "tbl_Reservation_Entry"):
            cur.executemany(
                "UPDATE dbo.tbl_Reservation_Entry SET [Source ID] = ? WHERE Purchase_Header_ID = ?",
                [(n, i) for i, n in pairs])
        conn.commit()
    finally:
        conn.close()


def _find_entry_no_collision(cur, id_to_entry_no, batch_name=None):
    """Reservation Entry counterpart to _find_no_collision: checks a
    proposed {reservation_row_id: entry_no} mapping against every
    reservation row ALREADY in the table. Returns (entry_no, other_batch)
    for the first Entry No. already in use by a row in a DIFFERENT batch,
    or None if the whole mapping is safe to write. Pass `batch_name` so a
    match belonging to that SAME batch (e.g. an Excluded sibling's
    permanently-frozen Entry No.) is never treated as a collision - see
    _find_no_collision's docstring for why."""
    for row_id, entry_no in id_to_entry_no.items():
        if not entry_no:
            continue
        cur.execute(
            "SELECT TOP 1 pt.BatchName FROM dbo.tbl_Reservation_Entry r "
            "LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.Purchase_Header_ID = r.Purchase_Header_ID "
            "WHERE r.[Entry No.] = ? AND r.Id <> ?",
            str(entry_no), row_id)
        row = cur.fetchone()
        if row and (not batch_name or (row[0] or "") != batch_name):
            return entry_no, row[0]
    return None


def _entry_no_collision_error(entry_no, other_batch):
    return ValueError(
        f"The Entry No. '{entry_no}' is already processed with another batch"
        + (f" ('{other_batch}')" if other_batch else "")
        + " - download aborted, nothing was changed. Refresh and try again."
    )


def _next_entry_no_seq(cur, exclude_row_ids=None):
    """Next sequential integer for Entry No., continuing from the highest
    already used in tbl_Reservation_Entry.[Entry No.] - never restarting at
    1 for a fresh batch/run, so two separate downloads can never mint the
    same Entry No. Unlike Document No. this has no prefix, just a plain
    running integer.

    `exclude_row_ids` (the reservation rows about to be re-minted in this
    very call) are left out of the scan - see _next_doc_no_seq's own
    docstring for why: without this, a batch's own current numbers would
    always count against itself and could never reuse the range an
    Excluded sibling's row just freed (see set_excluded)."""
    exclude_ids = {r for r in (exclude_row_ids or []) if r is not None}
    cur.execute(
        "SELECT Id, [Entry No.] FROM dbo.tbl_Reservation_Entry "
        "WHERE [Entry No.] IS NOT NULL AND [Entry No.] <> ''")
    best = 0
    for row_id, v in cur.fetchall():
        if row_id in exclude_ids:
            continue
        try:
            best = max(best, int(str(v).strip()))
        except (TypeError, ValueError):
            continue
    return best + 1


def _assign_entry_numbers(cur, res_rows, batch_name=None):
    """Mint a FRESH, real, globally-unique Entry No. for every reservation
    row passed in - every call re-mints, even a row that already has one
    (see fetch_batch, which skips calling this entirely once the batch is
    locked) - continuing the running sequence from whatever's already used
    outside this same set of rows (see _next_entry_no_seq), and writes it
    directly to tbl_Reservation_Entry.[Entry No.]. Rows are numbered in
    ascending Id order, same reasoning as _assign_document_numbers: excluding
    one invoice and re-downloading closes the gap it leaves instead of
    every remaining row climbing to an ever-higher number each time.
    Mutates each row's "Entry No." in place. Computes every row's new
    number first and checks the whole set for a collision against another
    batch's already-persisted Entry No. (see _find_entry_no_collision,
    passed this same `batch_name` so a match that's one of this batch's OWN
    other rows is never treated as a collision) before writing anything -
    raises ValueError and writes nothing at all if one is found, rather
    than silently letting two batches share an Entry No."""
    res_rows = sorted(res_rows, key=lambda r: r.get("Id") or 0)
    row_ids = [row.get("Id") for row in res_rows]
    seq = None
    proposed = {}
    for row in res_rows:
        row_id = row.get("Id")
        if row_id is None:
            continue
        if seq is None:
            seq = _next_entry_no_seq(cur, row_ids)
        proposed[row_id] = str(seq)
        seq += 1

    collision = _find_entry_no_collision(cur, proposed, batch_name)
    if collision:
        raise _entry_no_collision_error(*collision)

    for row_id, entry_no in proposed.items():
        cur.execute("UPDATE dbo.tbl_Reservation_Entry SET [Entry No.] = ? WHERE Id = ?", entry_no, row_id)
    for row in res_rows:
        row_id = row.get("Id")
        if row_id in proposed:
            row["Entry No."] = proposed[row_id]


def write_entry_numbers(id_to_entry_no, batch_name=None):
    """Bulk-write an explicit {reservation_row_id: entry_no} mapping
    straight to tbl_Reservation_Entry.[Entry No.] (used for a user-chosen
    custom renumber, see excel_export.renumber_batch). Checks the whole
    mapping against every other row's already-persisted Entry No. first
    (see _find_entry_no_collision) and writes nothing at all - raising
    ValueError instead - if a custom-chosen number collides with one
    another batch already has. Pass `batch_name` (the batch being
    renumbered) so a match against one of its OWN other rows is never
    treated as a collision.
    Sample: write_entry_numbers({7: '1001'}, 'PIIPS_Batch_20260722_101500')"""
    pairs = [(int(i), str(n)) for i, n in (id_to_entry_no or {}).items() if i is not None and n]
    if not pairs:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        collision = _find_entry_no_collision(cur, dict(pairs), batch_name)
        if collision:
            raise _entry_no_collision_error(*collision)
        cur.executemany(
            "UPDATE dbo.tbl_Reservation_Entry SET [Entry No.] = ? WHERE Id = ?",
            [(n, i) for i, n in pairs])
        conn.commit()
    finally:
        conn.close()


def precheck_number_collisions(id_to_no, id_to_entry_no, batch_name=None):
    """Check a proposed Document No. mapping AND a proposed Entry No.
    mapping for collisions in one shared connection, before either
    write_document_numbers or write_entry_numbers runs. A custom renumber
    sets both at once via two separate calls that each commit
    independently - without this upfront check, a Document No. write could
    succeed and commit, only for the paired Entry No. write to then hit a
    collision and abort, leaving the Document No. change persisted despite
    the overall request failing with "nothing was changed". Pass
    `batch_name` (the batch being renumbered) so a match against one of its
    OWN other rows is never treated as a collision. Raises ValueError on
    the first collision found (of either kind); returns None if both
    mappings are safe to write.
    Sample: precheck_number_collisions({42: 'PO-2627-000010'}, {7: '1001'}, 'PIIPS_Batch_20260722_101500')"""
    no_pairs = [(int(i), n) for i, n in (id_to_no or {}).items() if i is not None and n]
    entry_pairs = [(int(i), str(n)) for i, n in (id_to_entry_no or {}).items() if i is not None and n]
    if not no_pairs and not entry_pairs:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        if no_pairs:
            collision = _find_no_collision(cur, dict(no_pairs), batch_name)
            if collision:
                raise _no_collision_error(*collision)
        if entry_pairs:
            collision = _find_entry_no_collision(cur, dict(entry_pairs), batch_name)
            if collision:
                raise _entry_no_collision_error(*collision)
    finally:
        conn.close()


def fetch_batch(batch_name, sheet_cols):
    """
    Read a batch's rows from the 3 tables into {sheet: {columns, rows}}
    ready for excel_export.build_workbook_from_sheets. `sheet_cols` is the
    effective {sheet: [columns]} mapping. usp_FetchBatch returns three
    result sets (header, line, reservation), each with only the columns that
    actually exist on the table — restricted to headers not yet advanced
    past READY TO LOAD (see usp_FetchBatch's @idset).

    Every download re-mints and re-writes a fresh [No.] directly to
    tbl_Purchase_Header.[No.] for every header in the batch (propagated
    onto tbl_Purchase_Line.[Document No.] / tbl_Reservation_Entry.
    [Source ID]) - see _assign_document_numbers, which resolves each
    header's PO_Number_Format LIVE from its template (never stored) and
    continues that prefix's sequence from whatever's already used across
    the whole table, so it can never collide with a number minted by an
    earlier, separate download. EXCEPTION: once any invoice in this batch
    has reached LOADED/POSTED/COMPLETED/REJECTED BY ACCOUNTS, the whole
    batch's numbering is frozen (see batch_is_locked) - some of its numbers
    may already be committed in Navision, so a re-download must leave every
    [No.] exactly as it is. The rows are then re-read straight from the
    table so the export reflects exactly what's now persisted.
    advance_status's Load step never generates [No.] - it only
    duplicate-checks it.

    [Entry No.] on Reservation Entry works exactly the same way now: every
    download re-mints and re-writes a fresh one directly to
    tbl_Reservation_Entry.[Entry No.] (see _assign_entry_numbers), under
    the same batch_is_locked exception as [No.].

    Sample: fetch_batch('PIIPS_Batch_20260722_101500', {'Purchase Header': ['InvoiceNo'], 'Purchase Line': ['Description'], 'Reservation Entry': ['Serial No.']})
    """
    ensure_menu_schema()

    header_cols = list(sheet_cols.get("Purchase Header", []))
    line_cols = list(sheet_cols.get("Purchase Line", []))
    res_cols = list(sheet_cols.get("Reservation Entry", []))

    # Internal-only columns needed below — usp_FetchBatch includes whatever's
    # requested that actually exists as a real column, so this piggybacks on
    # the same dynamic-column mechanism without any SQL changes. Deduped so
    # a column already in the caller's own requested list isn't asked for
    # twice.
    header_req = list(dict.fromkeys(header_cols + ["Id", "No.", "InvoiceNo"]))
    line_req = list(dict.fromkeys(line_cols + ["Purchase_Header_ID", "Document No."]))
    res_req = list(dict.fromkeys(res_cols + ["Id", "Purchase_Header_ID", "Source ID", "Entry No."]))

    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_FetchBatch ?, ?, ?, ?",
            batch_name,
            json.dumps(header_req),
            json.dumps(line_req),
            json.dumps(res_req),
        )

        def read_rows():
            names = [d[0] for d in (cur.description or [])]
            fetched = cur.fetchall()
            if names == ["_empty"]:
                return []
            return [dict(zip(names, r)) for r in fetched]

        ph_r = read_rows()
        cur.nextset()
        pl_r = read_rows()
        cur.nextset()
        re_r = read_rows()

        # A re-download of a batch that already has at least one invoice
        # past Ready to Load (or Rejected) must never touch [No.] again —
        # see batch_is_locked. Otherwise mint fresh numbers for everything
        # still in the batch, then re-read all three result sets fresh
        # from the database so the export reflects exactly what's now
        # persisted (rather than an in-memory guess).
        if not batch_is_locked(cur, batch_name):
            _assign_document_numbers(cur, ph_r, batch_name)
            _assign_entry_numbers(cur, re_r, batch_name)
            conn.commit()

            cur.execute(
                "EXEC dbo.usp_FetchBatch ?, ?, ?, ?",
                batch_name,
                json.dumps(header_req),
                json.dumps(line_req),
                json.dumps(res_req),
            )
            ph_r = read_rows()
            cur.nextset()
            pl_r = read_rows()
            cur.nextset()
            re_r = read_rows()

        for row in ph_r:
            # Re-applied here (not just at save time in excel_export.py's
            # _link_override) so a download is correct even for invoices
            # saved before this prefix-strip existed - re.sub is a no-op on
            # a value that's already clean.
            if "Consignment Note No." in row:
                row["Consignment Note No."] = re.sub(
                    r"^[S5]PRPUR/?", "", row.get("Consignment Note No.") or "",
                    flags=re.IGNORECASE)

        # A freight/courier/forwarding line has no Navision item master
        # entry - BC books it as a fixed service line. Re-applied here (not
        # just at save time in excel_export.py's _freight_line_override) so
        # a download is correct even for a batch saved before this fix, same
        # reasoning as Consignment Note No. above.
        part_header_ids = set()
        if any(row.get("Type") == "Charge (Item)" for row in pl_r):
            cur.execute(
                "SELECT pt.Purchase_Header_ID FROM tbl_Purchase_Tracker pt "
                "JOIN tbl_InvoiceType it ON it.InvoiceTypeId = pt.InvoiceTypeID "
                "WHERE pt.BatchName = ? AND it.InvoiceTypeName = 'PART'",
                batch_name,
            )
            part_header_ids = {r[0] for r in cur.fetchall()}

        for row in pl_r:
            if (row.get("Type") == "Charge (Item)"
                    and row.get("Purchase_Header_ID") in part_header_ids):
                if "No." in row:
                    row["No."] = "FRIEGHT IN"
                if "GST Group Type" in row:
                    row["GST Group Type"] = "Service"
                if "GST Group Code" in row:
                    try:
                        rate = float(row.get("GST %") or 0)
                    except (TypeError, ValueError):
                        rate = 0
                    # `if rate else "Service"` would collapse a genuine
                    # 0% (falsy) back down to the bare fallback, losing
                    # the rate a freight line with no GST is legitimately
                    # supposed to show ("Service 0%", not just "Service").
                    row["GST Group Code"] = f"Service {rate:g}%"

        for row in re_r:
            # Forced here too (not just at save time), same reasoning as
            # Consignment Note No. above - a row saved before this fix
            # still has the old "Order" text rather than BC's numeric
            # Document Type option value.
            if "Source Subtype" in row:
                row["Source Subtype"] = "1"

        return {
            "Purchase Header": {"columns": header_cols, "rows": ph_r},
            "Purchase Line": {"columns": line_cols, "rows": pl_r},
            "Reservation Entry": {"columns": res_cols, "rows": re_r},
        }
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Invoice lists for the dashboard pop-ups (by status / by batch) and the
# per-invoice include / exclude action.
# ---------------------------------------------------------------------------

def _hdr_col(cur, name):
    cur.execute("SELECT COL_LENGTH('dbo.tbl_Purchase_Header', ?)", name)
    return cur.fetchone()[0] is not None


def _invoice_type_from_source_json(path):
    """'PART'/'SERVICE' parsed from a SourceJson path
    (.../Output/<entity>/<invoice_type>/<name>/<file>.json), or '' if it
    can't be resolved (e.g. path too short, or predates the invoice-type
    folder structure)."""
    if not path:
        return ""
    parts = re.split(r"[\\/]+", path)
    try:
        i = parts.index("Output")
    except ValueError:
        return ""
    return parts[i + 2] if len(parts) > i + 2 else ""


def _invoice_list(where_sql, params):
    """One row per tracked invoice matching `where_sql` (a few display columns
    for the dashboard pop-ups)."""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        if not _table_exists(cur, "tbl_Purchase_Tracker"):
            return []
        vendor = "h.[Buy-from Vendor Name]" if _hdr_col(cur, "Buy-from Vendor Name") else "CAST(NULL AS NVARCHAR(400))"
        gst = "h.[Vendor GST Reg. No.]" if _hdr_col(cur, "Vendor GST Reg. No.") else "CAST(NULL AS NVARCHAR(50))"
        ddate = "h.[Document Date]" if _hdr_col(cur, "Document Date") else "CAST(NULL AS NVARCHAR(50))"
        docno = "h.[No.]" if _hdr_col(cur, "No.") else "CAST(NULL AS NVARCHAR(50))"
        # NOLOCK: pure display (Dashboard pop-ups, Lifecycle page listings) -
        # never gates a write decision itself (advance_status/lifecycle_advance
        # re-check fresh data at submit time), so a dirty/stale read here is
        # harmless and keeps this off the numbering/status write path.
        sql = (
            "SELECT h.Id, h.InvoiceNo, pt.BatchName, pt.FileName, pt.TemplateFormat, "
            "s.StatusName, pt.IsActive, pt.IsSynced, ISNULL(pt.IsExcluded, 0), "
            f"{vendor}, {gst}, {ddate}, pt.Id, pt.SourceJson, {docno}, pt.RejectRemark "
            "FROM dbo.tbl_Purchase_Tracker pt WITH (NOLOCK) "
            "JOIN dbo.tbl_Purchase_Header h WITH (NOLOCK) ON h.Id = pt.Purchase_Header_ID "
            "LEFT JOIN dbo.tbl_status s WITH (NOLOCK) ON s.StatusId = pt.StatusID "
            f"WHERE {where_sql} ORDER BY h.Id"
        )
        cur.execute(sql, *params)
        out = []
        for r in cur.fetchall():
            out.append({
                "header_id": r[0], "invoice_no": r[1], "batch": r[2],
                "file_name": r[3], "format": r[4], "status": r[5],
                "is_active": bool(r[6]) if r[6] is not None else None,
                "is_synced": bool(r[7]) if r[7] is not None else None,
                "is_excluded": bool(r[8]),
                "vendor": r[9], "vendor_gst": r[10],
                "doc_date": str(r[11]) if r[11] else "",
                "tracker_id": r[12],
                "invoice_type": _invoice_type_from_source_json(r[13]),
                "navision_doc_no": r[14] or "",
                "reject_remark": r[15] or "",
            })
        return out
    finally:
        conn.close()


def _table_exists(cur, table):
    cur.execute("SELECT 1 FROM sys.tables WHERE name = ?", table)
    return cur.fetchone() is not None


def invoices_by_status(status_id):
    """Invoices whose tracker status is `status_id` (pie-slice pop-up).
    Sample: invoices_by_status(5)"""
    return _invoice_list("pt.StatusID = ?", [int(status_id)])


def invoices_by_batch(batch_name):
    """Every invoice in a batch (Dashboard batches table's per-status
    drill-down pop-up). Deliberately NOT filtered by IsActive: every
    non-terminal status (BUYER ORDER NO DOESN'T EXIST, PENDING IN SF,
    DATA MISMATCH) is saved with IsActive=0 by evaluate_invoice() - it
    marks "not yet validated/ready", not "hide this row". Filtering on it
    here used to make the popup disagree with the status column's own
    count (e.g. count shows 3, popup opens to 0) for exactly the statuses
    someone clicking in is most likely trying to review.
    Sample: invoices_by_batch('PIIPS_Batch_20260722_101500')"""
    return _invoice_list("pt.BatchName = ?", [batch_name])


def invoices_by_statuses(status_names, active_only=False):
    """Invoices whose tracker status is one of `status_names` (Buyer Order Entry
    and the Load/Post/Complete lifecycle pages). Excluded invoices are skipped;
    `active_only` additionally restricts to IsActive = 1 (the lifecycle pages
    only act on active invoices).
    Sample: invoices_by_statuses(['READY TO LOAD', 'LOADED'], active_only=True)"""
    names = [n for n in (status_names or []) if n]
    if not names:
        return []
    placeholders = ", ".join("?" for _ in names)
    where = f"s.StatusName IN ({placeholders}) AND ISNULL(pt.IsExcluded, 0) = 0"
    if active_only:
        where += " AND pt.IsActive = 1"
    return _invoice_list(where, names)


# The menu keys each role can see BEFORE a Super Admin has ever saved the
# "Screen Access" menu - i.e. what every existing deployment already
# behaves like today. Used only to seed tbl_RoleMenu the first time it's
# empty (see get_role_menus), so nothing changes for anyone until a Super
# Admin actually visits that menu and saves something. Keep in sync with
# frontend/src/App.jsx's MENU list if a menu key is ever renamed - this is
# a one-time seed, not read on every request, so a stale key here only
# matters for a brand new deployment's first run.
_ROLE_MENU_DEFAULTS = {
    "admin": ["dashboard", "input", "manual", "buyerorder", "partdescupdate",
              "load", "post", "complete",
              "configuration", "apiconfig", "template", "createfield", "users"],
    "user": ["dashboard", "input", "manual", "buyerorder", "partdescupdate", "load"],
    "accounts": ["dashboard", "input", "manual", "post", "complete"],
    "viewer": ["dashboard", "input", "buyerorder", "partdescupdate", "load", "post", "complete"],
}


def get_role_menus():
    """{role_name: [menu_key, ...]} for every configurable role (never
    'super admin'/'developer' - see tbl_RoleMenu's own comment). Seeds the
    table from _ROLE_MENU_DEFAULTS the first time it's empty.
    Sample: get_role_menus()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM dbo.tbl_RoleMenu")
        if cur.fetchone()[0] == 0:
            for role, keys in _ROLE_MENU_DEFAULTS.items():
                for key in keys:
                    cur.execute(
                        "INSERT INTO dbo.tbl_RoleMenu (RoleName, MenuKey) VALUES (?, ?)",
                        role, key)
            conn.commit()
        cur.execute("SELECT RoleName, MenuKey FROM dbo.tbl_RoleMenu ORDER BY RoleName, Id")
        out = {}
        for role, key in cur.fetchall():
            out.setdefault(role, []).append(key)
        return out
    finally:
        conn.close()


def save_role_menus(mapping, user_id=None):
    """Replace the whole role -> menu-keys mapping (Screen Access menu's
    Save button). `mapping` is {role_name: [menu_key, ...]}; a 'super
    admin'/'developer' entry, if present, is silently dropped - that role
    always sees every menu and is never stored. Returns the mapping as
    actually saved (via get_role_menus).
    Sample: save_role_menus({'admin': ['dashboard', 'input']}, user_id=7)"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM dbo.tbl_RoleMenu")
        for role, keys in (mapping or {}).items():
            role = (role or "").strip().lower()
            if not role or role in ("super admin", "developer"):
                continue
            for key in keys or []:
                key = (key or "").strip()
                if key:
                    cur.execute(
                        "INSERT INTO dbo.tbl_RoleMenu (RoleName, MenuKey) VALUES (?, ?)",
                        role, key)
        conn.commit()
    finally:
        conn.close()
    return get_role_menus()


def buyer_order_nos_for_status(status_name):
    """Distinct, non-blank Buyer's Order Nos for invoices currently at
    `status_name` - feeds the Service First GetPurchaseLineSpecification-
    MismatchRecord lookup for the Part Description Update menu.
    Sample: buyer_order_nos_for_status('DATA MISMATCH')"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT DISTINCT pt.BuyerOrderNo "
            "FROM dbo.tbl_Purchase_Tracker pt WITH (NOLOCK) "
            "JOIN dbo.tbl_Status s WITH (NOLOCK) ON s.StatusId = pt.StatusID "
            "WHERE s.StatusName = ? AND ISNULL(pt.IsExcluded, 0) = 0 "
            "AND pt.BuyerOrderNo IS NOT NULL AND pt.BuyerOrderNo <> ''",
            status_name,
        )
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def invoice_details_by_buyer_order(order_nos, status_name=None):
    """{BuyerOrderNo: {"descriptions": [...], "invoices": [{"InvoiceNo",
    "FileName"}, ...]}} for the given POs - PIIPS's own invoice(s) for each
    PO (Purchase Details column) and their Purchase Line Description text
    (Part Description Update screen's autocomplete), matching a Service
    First row's PurchaseOrderNo against what's actually in PIIPS for that
    same PO. "descriptions" excludes "Charge (Item)" lines (Freight
    Outward/Courier) - those aren't real parts, so they're never valid
    matches for a Service First part row and shouldn't be offered as a
    selectable description.
    A PO can carry multiple tracker rows across earlier re-uploads/re-runs
    (superseded ones parked DUPLICATE) - pass `status_name` (the same status
    the caller scoped `order_nos` to) to only surface the invoice(s) that
    are still actually at that status, instead of every stale sibling ever
    filed under this PO.
    Sample: invoice_details_by_buyer_order(['SPRPUR/2026/04/27-83650'], 'DATA MISMATCH')"""
    order_nos = [str(n).strip() for n in (order_nos or []) if n and str(n).strip()]
    if not order_nos:
        return {}
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        placeholders = ", ".join("?" for _ in order_nos)
        where = f"pt.BuyerOrderNo IN ({placeholders})"
        params = list(order_nos)
        if status_name:
            where += " AND s.StatusName = ?"
            params.append(status_name)
        cur.execute(
            "SELECT DISTINCT pt.BuyerOrderNo, h.InvoiceNo, pt.FileName, "
            "  pl.[Description], pl.[Type] "
            "FROM dbo.tbl_Purchase_Tracker pt WITH (NOLOCK) "
            "JOIN dbo.tbl_Purchase_Header h WITH (NOLOCK) ON h.Id = pt.Purchase_Header_ID "
            "JOIN dbo.tbl_Status s WITH (NOLOCK) ON s.StatusId = pt.StatusID "
            "LEFT JOIN dbo.tbl_Purchase_Line pl WITH (NOLOCK) "
            "  ON pl.Purchase_Header_ID = pt.Purchase_Header_ID "
            f"WHERE {where}",
            params,
        )
        by_po = {}
        for po, invoice_no, file_name, desc, line_type in cur.fetchall():
            entry = by_po.setdefault(po, {"descriptions": [], "invoices": []})
            if desc and (line_type or "").strip() != "Charge (Item)":
                if desc not in entry["descriptions"]:
                    entry["descriptions"].append(desc)
            pair = {"InvoiceNo": invoice_no or "", "FileName": file_name or ""}
            if pair not in entry["invoices"]:
                entry["invoices"].append(pair)
        return by_po
    finally:
        conn.close()


def _existing_cols(cur, table, wanted):
    """Subset of `wanted` that actually exist as columns on `table` (case-
    insensitive), preserving `wanted`'s order."""
    cur.execute("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID(?)", f"dbo.{table}")
    have = {r[0].lower() for r in cur.fetchall()}
    return [c for c in wanted if c.lower() in have]


def get_invoice_field_check(header_id):
    """Field-by-field mandatory-data breakdown for one invoice: every
    required Purchase Header / Purchase Line / Reservation Entry column,
    its current stored value, and whether it's missing — for the Dashboard's
    'DATA MISMATCH' drill-down (exactly which field(s) are missing and
    what's currently in them).
    Sample: get_invoice_field_check(29)"""
    import excel_export

    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()

        field_mapping = excel_export.load_mapping()

        def field_rows(sheet, names, values):
            rows = []
            for n in names:
                v = values.get(n)
                v = "" if v is None else str(v)
                source = excel_export.field_source(sheet, n, field_mapping)
                # field_source() calls any unmapped column "Template" since
                # that's where a value for it *would* come from - but if no
                # static value was actually ever set (this invoice's actual
                # stored value is blank), nothing really populated it, so
                # show that honestly as "None" rather than implying a
                # template value exists when it doesn't.
                if source == "Template" and not v.strip():
                    source = "None"
                # A "None" field has nothing configured to populate it at
                # all - not shown here at all (nothing to review or act
                # on), and never counts toward DATA MISMATCH either (see
                # excel_export.missing_required_fields).
                if source == "None":
                    continue
                missing = not v.strip()
                # Purchase Line's own "Description" column is the invoice's
                # (PDF's) item description - displayed as "Invoice Part
                # Description" in the Fields popup to sit clearly alongside
                # "SF Part Description" (Service First's own item-master
                # description for the same line, see with_nav_part_description
                # below). Internal lookups below still key off the real
                # "Description" column name (`n`), only the popup label changes.
                display_field = "Invoice Part Description" if (sheet == "Purchase Line" and n == "Description") else n
                row_out = {
                    "field": display_field, "value": v, "missing": missing,
                    "source": source,
                }
                # Purchase Line "No." (Nav Item No.) only ever comes from
                # Service First's GetHSNDetails, keyed on an exact
                # (PO, item description) match (see service_api._apply_hsn_map)
                # - so a missing "No." from that source always means SF
                # didn't recognize this line's description, not some other
                # data problem. Surfacing the reason directly saves a trip
                # into the API logs to find out why.
                if sheet == "Purchase Line" and n == "No." and missing and source == "Service First":
                    row_out["reason"] = "Invoice Part Description and SF Part Description are different"
                rows.append(row_out)
            return rows

        # REQUIRED_HEADER_FIELDS already ends with "InvoiceNo" - appending
        # it again here used to show it twice in the Fields popup.
        header_wanted = excel_export.REQUIRED_HEADER_FIELDS
        header_cols = _existing_cols(cur, "tbl_Purchase_Header", header_wanted)
        header_values = {}
        if header_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in header_cols)} "
                "FROM dbo.tbl_Purchase_Header WITH (NOLOCK) WHERE Id = ?",
                header_id,
            )
            row = cur.fetchone()
            if row:
                header_values = dict(zip(header_cols, row))

        # Buyer Order No lives on the tracker, not tbl_Purchase_Header
        # itself (it drives matching/reprocessing, not an exported column) -
        # shown here purely for visibility, not part of REQUIRED_HEADER_FIELDS
        # (its own absence is handled by the separate "BUYER ORDER NO
        # DOESN'T EXIST" status/workflow, not this mandatory-field check).
        cur.execute(
            "SELECT BuyerOrderNo FROM dbo.tbl_Purchase_Tracker WHERE Purchase_Header_ID = ?",
            header_id,
        )
        tracker_row = cur.fetchone()
        buyer_order_no = (tracker_row[0] if tracker_row else "") or ""

        # "No." and "HSN/SAC Code" are only mandatory conditionally (on the
        # line's own Type — see excel_export.required_fields_for_line_type),
        # so always fetch both and pick the right one per line below.
        line_wanted = excel_export.REQUIRED_LINE_FIELDS + ["No.", "HSN/SAC Code"]
        line_cols = _existing_cols(cur, "tbl_Purchase_Line", line_wanted)
        line_rows = []
        if line_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in line_cols)} "
                "FROM dbo.tbl_Purchase_Line WITH (NOLOCK) WHERE Purchase_Header_ID = ?",
                header_id,
            )
            line_rows = [dict(zip(line_cols, r)) for r in cur.fetchall()]

        # Nav_Part_Description (Service First's GetHSNDetails item-master
        # description) isn't a saved tbl_Purchase_Line column - it only ever
        # lived in the extracted JSON at process time (see
        # service_api._apply_hsn_map / _description_mismatch, the actual
        # DATA MISMATCH check for this). Reload it from the tracker's
        # SourceJson (same lookup as apply_manual_buyer_order) and key it by
        # cleaned Description so it can be shown alongside the PDF's own
        # Description for comparison - not a mandatory field, just extra
        # context for why SF's description didn't match.
        nav_part_desc_by_desc = {}
        # The PDF's own raw HSN reading (before Service First's GetHSNDetails
        # match either confirms it or blanks it - see service_api._hsn_lookup_
        # items) is never saved as a Purchase Line column, only kept in this
        # same source JSON under "hsn" - needed below so a genuinely blank
        # PDF HSN (nothing to confirm in the first place) isn't flagged as
        # "missing" the same way a PDF HSN that SF failed to confirm is.
        pdf_hsn_by_desc = {}
        cur.execute(
            "SELECT SourceJson FROM dbo.tbl_Purchase_Tracker WITH (NOLOCK) "
            "WHERE Purchase_Header_ID = ?", header_id,
        )
        json_row = cur.fetchone()
        json_path = json_row[0] if json_row else None
        if json_path and os.path.isfile(json_path):
            try:
                with open(json_path, "r", encoding="utf-8") as fp:
                    source_data = json.load(fp)
                for item in source_data.get("items", []) or []:
                    key = str(item.get("Description") or "").strip().lower()
                    if key:
                        nav_part_desc_by_desc[key] = str(item.get("Nav_Part_Description") or "")
                        pdf_hsn_by_desc[key] = str(item.get("hsn") or "")
            except (OSError, json.JSONDecodeError):
                pass

        def with_nav_part_description(ln, rows):
            # Freight Outward / Courier lines are "Charge (Item)" rows, not
            # "Item" - they're never looked up in GetHSNDetails at all (see
            # service_api._hsn_lookup_items/_apply_hsn_map skipping "_charge"
            # items), so Nav_Part_Description has no meaning for them.
            if ln.get("Type") != "Item":
                return rows
            desc_key = str(ln.get("Description") or "").strip().lower()
            if desc_key not in nav_part_desc_by_desc:
                return rows
            v = nav_part_desc_by_desc[desc_key]
            new_field = {
                "field": "SF Part Description", "value": v, "missing": not v.strip(),
                "source": "Service First",
            }
            idx = next((i for i, f in enumerate(rows) if f["field"] == "Invoice Part Description"), None)
            if idx is None:
                return rows + [new_field]
            result = rows[:idx + 1] + [new_field] + rows[idx + 1:]
            # "No." otherwise lands at the very end (required_fields_for_line_type
            # appends it last) - move it to sit right after SF Part
            # Description instead, so the two SF-sourced item-match fields
            # are shown together.
            no_idx = next((i for i, f in enumerate(result) if f["field"] == "No."), None)
            if no_idx is not None:
                no_field = result.pop(no_idx)
                result.insert(idx + 2, no_field)
            return result

        def with_hsn_fields(ln, rows):
            # ProductNo (the item's HSN Number, not a Navision item no. -
            # despite the name) and HSN_Type are already saved on the line
            # as "HSN/SAC Code" / "GST Group Type" (see the field mapping:
            # HSN/SAC Code <- ProductNo, GST Group Type <- HSN_Type). For an
            # "Item" line that value came from Service First's GetHSNDetails
            # match; for a "Charge (Item)" (Freight Outward/Courier) line SF
            # is never queried at all (see service_api._hsn_lookup_items
            # skipping "_charge" items), so whatever's stored there is the
            # PDF's own extracted value instead - shown here under the SF
            # field names either way so both line types can be compared
            # against what Service First actually calls them.
            source = "Service First" if ln.get("Type") == "Item" else "PDF"
            product_no = str(ln.get("HSN/SAC Code") or "")
            missing = not product_no.strip()
            # For a part (Item line), a blank result only counts as
            # "missing" when the PDF itself actually had an HSN that SF
            # failed to confirm - if the PDF never printed one either,
            # there was nothing for SF to confirm in the first place, so
            # it's not a real data problem (Charge lines never touch this:
            # their HSN already comes straight from the PDF, so a blank
            # value there always means the PDF genuinely had none).
            if missing and ln.get("Type") == "Item":
                desc_key = str(ln.get("Description") or "").strip().lower()
                missing = bool(pdf_hsn_by_desc.get(desc_key, "").strip())
            extra = [
                {"field": "HSN/SAC Code", "value": product_no,
                 "missing": missing, "source": source},
            ]
            idx = next((i for i, f in enumerate(rows) if f["field"] == "No."), None)
            if idx is None:
                idx = next((i for i, f in enumerate(rows)
                            if f["field"] in ("SF Part Description", "Invoice Part Description")),
                           len(rows) - 1)
            return rows[:idx + 1] + extra + rows[idx + 1:]

        res_cols = _existing_cols(cur, "tbl_Reservation_Entry", excel_export.REQUIRED_RESERVATION_FIELDS)
        res_rows = []
        if res_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in res_cols)} "
                "FROM dbo.tbl_Reservation_Entry WITH (NOLOCK) WHERE Purchase_Header_ID = ?",
                header_id,
            )
            res_rows = [dict(zip(res_cols, r)) for r in cur.fetchall()]
            # Same forced recompute as fetch_batch() - a row saved before
            # the Source Subtype fix still has the old "Order" text stored.
            for rr in res_rows:
                if "Source Subtype" in rr:
                    rr["Source Subtype"] = "1"

        header_rows_out = field_rows("Purchase Header", header_wanted, header_values)
        buyer_order_row = {
            "field": "Buyer Order No", "value": buyer_order_no,
            "missing": not buyer_order_no.strip(), "source": "PDF",
        }
        # Shown right before InvoiceNo (REQUIRED_HEADER_FIELDS' own last
        # entry) so the two invoice-identifying fields sit together.
        invoiceno_idx = next(
            (i for i, r in enumerate(header_rows_out) if r["field"] == "InvoiceNo"), None)
        header_rows_out = (
            header_rows_out[:invoiceno_idx] + [buyer_order_row] + header_rows_out[invoiceno_idx:]
            if invoiceno_idx is not None else header_rows_out + [buyer_order_row]
        )

        return {
            "header": header_rows_out,
            "lines": [
                {"label": ln.get("Description", "") or f"Line {i + 1}",
                 # "HSN/SAC Code" is excluded here even though it's part of
                 # required_fields_for_line_type (needed there for the
                 # mandatory-field gate) - with_hsn_fields below already
                 # adds this exact same column itself (with the missing-
                 # ness rule refined for a part's PDF-vs-SF comparison), so
                 # including it here too would show the value twice.
                 "fields": with_hsn_fields(ln, with_nav_part_description(ln, field_rows(
                     "Purchase Line",
                     [f for f in excel_export.required_fields_for_line_type(ln.get("Type"))
                      if f != "HSN/SAC Code"],
                     ln)))}
                for i, ln in enumerate(line_rows)
            ],
            "reservations": [
                {"label": rs.get("Item No.", "") or f"Reservation {i + 1}",
                 "fields": field_rows("Reservation Entry", excel_export.REQUIRED_RESERVATION_FIELDS, rs)}
                for i, rs in enumerate(res_rows)
            ],
        }
    finally:
        conn.close()


def set_buyer_order_no(header_id, order_no, verdict, user_id=None):
    """Persist a manually-keyed Buyer's Order No. and the recomputed verdict for
    one invoice (Buyer Order Entry menu).
    Sample: set_buyer_order_no(42, 'SPRPUR/2026/04/27-83650',
            {'status': 'READY TO LOAD', 'is_active': True, 'is_synced': True}, 7)
    Updates the header's Buyer's Order No. column (when present) and the
    tracker's BuyerOrderNo / StatusID / IsActive / IsSynced / SyncedDatetime.
    Returns {file_name, new_status}."""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            "SELECT FileName FROM dbo.tbl_Purchase_Tracker WHERE Purchase_Header_ID = ?",
            header_id)
        row = cur.fetchone()
        if not row:
            return None
        file_name = row[0]

        # Header PO column only exists when the field mapping produced it.
        if _hdr_col(cur, "Buyer's Order No."):
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Header SET [Buyer's Order No.] = ? WHERE Id = ?",
                order_no, header_id)

        status_name = (verdict or {}).get("status")
        is_active = 1 if (verdict or {}).get("is_active") else 0
        is_synced = 1 if (verdict or {}).get("is_synced") else 0

        cur.execute(
            "UPDATE dbo.tbl_Purchase_Tracker "
            "SET BuyerOrderNo = ?, "
            "    StatusID = ISNULL((SELECT StatusId FROM dbo.tbl_status "
            "                       WHERE StatusName = ?), StatusID), "
            "    IsActive = ?, IsSynced = ?, "
            "    SyncedDatetime = CASE WHEN ? = 1 THEN GETDATE() ELSE SyncedDatetime END, "
            "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
            "WHERE Purchase_Header_ID = ?",
            order_no, status_name, is_active, is_synced, is_synced,
            user_id, header_id)
        conn.commit()
        return {"file_name": file_name, "new_status": status_name}
    finally:
        conn.close()


def advance_status(header_ids, from_statuses, to_status, user_id=None):
    """Bulk-advance the tracker status of selected invoices (Load / Post /
    Complete). Only rows currently in one of `from_statuses` are moved.
    Sample: advance_status([12, 13], ['READY TO LOAD'], 'LOADED', 7)
    Returns {'count': <rows changed>, 'files': [<FileName>, ...],
    'header_ids': [...], 'duplicate_no': [{'header_id', 'no'}, ...]} so the
    caller can relocate each PDF into its new status folder and alert on
    any header that got blocked.

    [No.] is no longer generated/finalized here - it becomes real back at
    Excel-download time (see fetch_batch/_assign_document_numbers).
    Advancing to LOADED instead guards against a duplicate: any qualifying
    header whose current (non-blank) [No.] already exists on a DIFFERENT
    header is left exactly where it is - excluded from this advance - and
    reported back in 'duplicate_no' so the caller can alert the user,
    rather than silently letting two invoices share one Document No."""
    ids = [int(h) for h in (header_ids or [])]
    froms = [s for s in (from_statuses or []) if s]
    if not ids or not froms or not to_status:
        return {"count": 0, "files": [], "header_ids": [], "duplicate_no": []}

    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        id_ph = ", ".join("?" for _ in ids)
        from_ph = ", ".join("?" for _ in froms)

        # Files + header ids of the rows that actually qualify (for the
        # folder move below and, on Load, the duplicate check) — captured
        # before the UPDATE changes their status out from under a later
        # lookup.
        cur.execute(
            "SELECT pt.FileName, pt.Purchase_Header_ID FROM dbo.tbl_Purchase_Tracker pt "
            "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
            f"WHERE pt.Purchase_Header_ID IN ({id_ph}) AND s.StatusName IN ({from_ph})",
            *ids, *froms)
        qualifying = cur.fetchall()
        # Kept positionally paired (files[i] <-> qualifying_ids[i]) - a row
        # with no FileName is dropped from both together, not just files,
        # so callers that zip the two (e.g. the ALL_INVOICES archive copy)
        # can't get misaligned.
        files, qualifying_ids = [], []
        for fname, hid in qualifying:
            if fname:
                files.append(fname)
                qualifying_ids.append(hid)

        duplicate_no = []
        if to_status == "LOADED" and qualifying_ids and _hdr_col(cur, "No."):
            qid_ph = ", ".join("?" for _ in qualifying_ids)
            cur.execute(
                f"SELECT Id, [No.] FROM dbo.tbl_Purchase_Header "
                f"WHERE Id IN ({qid_ph}) AND [No.] IS NOT NULL AND [No.] <> ''",
                *qualifying_ids)
            for hid, no in cur.fetchall():
                cur.execute(
                    "SELECT COUNT(*) FROM dbo.tbl_Purchase_Header WHERE [No.] = ? AND Id <> ?",
                    no, hid)
                if cur.fetchone()[0] > 0:
                    duplicate_no.append({"header_id": hid, "no": no})

        if duplicate_no:
            blocked = {d["header_id"] for d in duplicate_no}
            ids = [i for i in ids if i not in blocked]
            keep = [i for i, hid in enumerate(qualifying_ids) if hid not in blocked]
            files = [files[i] for i in keep]
            qualifying_ids = [qualifying_ids[i] for i in keep]

        count = 0
        if ids:
            id_ph = ", ".join("?" for _ in ids)
            cur.execute(
                "UPDATE pt "
                "SET pt.StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = ?), "
                "    pt.LastModifiedById = ?, pt.LastModifiedDatetime = GETDATE() "
                "FROM dbo.tbl_Purchase_Tracker pt "
                "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
                f"WHERE pt.Purchase_Header_ID IN ({id_ph}) AND s.StatusName IN ({from_ph})",
                to_status, user_id, *ids, *froms)
            count = cur.rowcount if cur.rowcount is not None else len(files)

        conn.commit()
        return {"count": count, "files": files, "header_ids": qualifying_ids,
                "duplicate_no": duplicate_no}
    finally:
        conn.close()


def reject_invoice(header_id, remark, user_id=None):
    """Accounts/Admin/Super Admin action on the Post page: reject one LOADED
    invoice back to REJECTED BY ACCOUNTS with a required remark explaining
    why. Stays
    IsActive=1 so it reappears on the Load page (alongside READY TO LOAD)
    for another attempt, with the remark visible there.
    Sample: reject_invoice(8, "Wrong GST rate on line 2", 11)"""
    remark = (remark or "").strip()
    if not remark:
        raise ValueError("A remark is required to reject an invoice.")
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pt.Id, pt.FileName, s.StatusName "
            "FROM dbo.tbl_Purchase_Tracker pt "
            "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
            "WHERE pt.Purchase_Header_ID = ?", header_id)
        row = cur.fetchone()
        if not row:
            return None
        tracker_id, file_name, status_name = row
        if status_name != "LOADED":
            raise ValueError(f"Invoice is '{status_name}', not LOADED — it can't be rejected from here.")
        cur.execute(
            "UPDATE dbo.tbl_Purchase_Tracker "
            "SET StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = 'REJECTED BY ACCOUNTS'), "
            "    IsActive = 1, RejectRemark = ?, RejectedByID = ?, RejectedDatetime = GETDATE(), "
            "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
            "WHERE Id = ?",
            remark, user_id, user_id, tracker_id)
        conn.commit()
        return {"file_name": file_name}
    finally:
        conn.close()


def invoices_by_header_ids(header_ids):
    """Invoices for a specific set of header ids, in the same shape as
    invoices_by_status/invoices_by_batch (used to build the invoice-no +
    vendor-name filename for the ALL_INVOICES archive copy).
    Sample: invoices_by_header_ids([12, 13])"""
    ids = [int(h) for h in (header_ids or [])]
    if not ids:
        return []
    ph = ", ".join("?" for _ in ids)
    return _invoice_list(f"h.Id IN ({ph})", ids)


def _current_batch_status(cur, batch_name):
    """This ONE batch's status label ('CREATED'/'DOWNLOADED'/'IN PROGRESS'/
    'LOADED'/'POSTED'/'COMPLETED'/'EXCLUDED'), same derivation
    list_batches/the Dashboard uses (see _batch_status_and_lock) - used to
    gate Include/Exclude purely on the invoice's OWN batch, never any
    other batch's."""
    cur.execute(
        "SELECT s.StatusName, COUNT(*) FROM dbo.tbl_Purchase_Tracker pt "
        "LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
        "WHERE pt.BatchName = ? GROUP BY s.StatusName",
        batch_name)
    counts = {sname: cnt for sname, cnt in cur.fetchall()}
    downloaded = False
    if _table_exists(cur, "tbl_BatchDownload"):
        cur.execute("SELECT 1 FROM dbo.tbl_BatchDownload WHERE BatchName = ?", batch_name)
        downloaded = cur.fetchone() is not None
    ever_reincluded = _batch_ever_reincluded(cur, batch_name)
    status, _locked = _batch_status_and_lock(counts, downloaded, ever_reincluded)
    return status


def set_excluded(header_id, exclude, user_id=None):
    """Include / exclude one invoice. Excluding sets the tracker status to
    'Excluded' (remembering the prior status), flags IsExcluded and stamps
    ExcludedByID / ExcludedDatetime; including restores the prior status.
    Returns {file_name, new_status} so the caller can relocate the PDF.

    Re-including is gated purely on this invoice's OWN batch's current
    status (see _current_batch_status) - allowed while it's CREATED or
    DOWNLOADED, refused with a ValueError otherwise (IN PROGRESS/LOADED/
    POSTED/COMPLETED/EXCLUDED). Excluding gets one extra allowance: it's
    also permitted while the batch is IN PROGRESS, as long as THIS
    invoice's own current status hasn't itself advanced past READY TO
    LOAD (i.e. isn't in _BATCH_LOCK_STATUSES) - the batch-level lock
    exists because some OTHER invoice in it may already be committed
    downstream (Navision), which says nothing about whether pulling
    THIS still-unadvanced one out is safe. No OTHER batch's status is
    ever considered either way.
    Sample: set_excluded(42, True, 7)"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT pt.StatusID, pt.PriorStatusID, pt.FileName, pt.BatchName, s.StatusName "
            "FROM dbo.tbl_Purchase_Tracker pt "
            "LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
            "WHERE pt.Purchase_Header_ID = ?", header_id)
        row = cur.fetchone()
        if not row:
            return None
        file_name = row[2]
        batch_name = row[3]
        own_status = (row[4] or "").upper()

        if batch_name:
            status = _current_batch_status(cur, batch_name)
            allowed = status in ("CREATED", "DOWNLOADED") or (
                exclude and own_status not in _BATCH_LOCK_STATUSES
            )
            if not allowed:
                if exclude:
                    detail = (
                        "This is only allowed while a batch is still Created or "
                        "Downloaded, or - for excluding only - while this "
                        "invoice's own status hasn't advanced past Ready To Load."
                    )
                else:
                    detail = "This is only allowed while a batch is still Created or Downloaded."
                action = "exclude" if exclude else "re-include"
                raise ValueError(
                    f"Can't {action} this invoice — its batch '{batch_name}' "
                    f"is already {status}. {detail}"
                )

        if exclude:
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Tracker "
                "SET PriorStatusID = CASE WHEN ISNULL(IsExcluded,0)=1 THEN PriorStatusID ELSE StatusID END, "
                "    StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = 'EXCLUDED'), "
                "    IsExcluded = 1, ExcludedByID = ?, ExcludedDatetime = GETDATE(), "
                "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
                "WHERE Purchase_Header_ID = ?", user_id, user_id, header_id)
            # Release this invoice's minted Document No./Entry No. entirely
            # rather than letting it hold them forever - usp_FetchBatch's
            # @idset stops offering an Excluded header for re-minting, so a
            # frozen [No.] left in place would sit there indefinitely and
            # (before _find_no_collision became batch-aware) could even
            # make a later re-download of this SAME batch look like it
            # collided with "another batch" that was, confusingly, itself.
            # Clearing it also means a future re-Include (see the `else`
            # branch below/reprocess_reworkable_header) gets a genuinely
            # fresh number next download, same as any other blank-No. invoice.
            cur.execute("UPDATE dbo.tbl_Purchase_Header SET [No.] = NULL WHERE Id = ?", header_id)
            if _existing_cols(cur, "tbl_Purchase_Line", ["Document No."]):
                cur.execute(
                    "UPDATE dbo.tbl_Purchase_Line SET [Document No.] = NULL "
                    "WHERE Purchase_Header_ID = ?", header_id)
            if _table_exists(cur, "tbl_Reservation_Entry"):
                res_cols = [c for c in ("Source ID", "Entry No.")
                            if _existing_cols(cur, "tbl_Reservation_Entry", [c])]
                if res_cols:
                    set_clause = ", ".join(f"{_q(c)} = NULL" for c in res_cols)
                    cur.execute(
                        f"UPDATE dbo.tbl_Reservation_Entry SET {set_clause} "
                        "WHERE Purchase_Header_ID = ?", header_id)
            new_status = "EXCLUDED"
        else:
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Tracker "
                "SET StatusID = ISNULL(PriorStatusID, StatusID), IsExcluded = 0, "
                "    ExcludedByID = NULL, ExcludedDatetime = NULL, "
                "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
                "WHERE Purchase_Header_ID = ?", user_id, header_id)
            _mark_batch_reincluded(cur, batch_name)
            cur.execute(
                "SELECT s.StatusName FROM dbo.tbl_Purchase_Tracker pt "
                "LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
                "WHERE pt.Purchase_Header_ID = ?", header_id)
            nr = cur.fetchone()
            new_status = nr[0] if nr else None
        conn.commit()
        return {"file_name": file_name, "new_status": new_status}
    finally:
        conn.close()


# ===========================================================================
# Menu storage: Create Field, Field Mapping, Template
# ---------------------------------------------------------------------------
# These three menus previously persisted to columns.json / mapping.json /
# templates.json. They are now stored in SQL Server tables. Rules:
#   * Every table carries CreatedById / CreatedDatetime / ModifiedById /
#     ModifiedDatetime and an IsActive flag (soft delete — rows are never
#     physically removed).
#   * All writes (insert / update / soft-delete) go through stored
#     procedures that MERGE a table-valued parameter, so only genuinely
#     changed rows are written and nothing is inserted row-by-row in a loop.
#   * Reads are plain SELECTs of the active rows.
# ===========================================================================

_MENU_TABLE_DDL = [
    # Create Field: one active row per (sheet, column), order via SortOrder.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_SheetColumn')
    CREATE TABLE tbl_SheetColumn (
        Id               INT IDENTITY(1,1) PRIMARY KEY,
        SheetName        NVARCHAR(150) NOT NULL,
        ColumnName       NVARCHAR(200) NOT NULL,
        SortOrder        INT NOT NULL DEFAULT 0,
        IsActive         BIT NOT NULL DEFAULT 1,
        CreatedById      INT NULL,
        CreatedDatetime  DATETIME NOT NULL DEFAULT GETDATE(),
        ModifiedById     INT NULL,
        ModifiedDatetime DATETIME NULL,
        CONSTRAINT UQ_SheetColumn UNIQUE (SheetName, ColumnName)
    )
    """,
    # Field Mapping: Excel column -> JSON field (spec may be "=literal").
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_FieldMapping')
    CREATE TABLE tbl_FieldMapping (
        Id               INT IDENTITY(1,1) PRIMARY KEY,
        SheetName        NVARCHAR(150) NOT NULL,
        ColumnName       NVARCHAR(200) NOT NULL,
        JsonField        NVARCHAR(200) NULL,
        IsActive         BIT NOT NULL DEFAULT 1,
        CreatedById      INT NULL,
        CreatedDatetime  DATETIME NOT NULL DEFAULT GETDATE(),
        ModifiedById     INT NULL,
        ModifiedDatetime DATETIME NULL,
        CONSTRAINT UQ_FieldMapping UNIQUE (SheetName, ColumnName)
    )
    """,
    # Template header (entity + invoice type + name + PO format), keyed by
    # "entity\\invoice_type\\name".
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_Template')
    CREATE TABLE tbl_Template (
        Id               INT IDENTITY(1,1) PRIMARY KEY,
        Entity           NVARCHAR(20) NOT NULL,
        Name             NVARCHAR(400) NOT NULL,
        TemplateKey      NVARCHAR(500) NOT NULL,
        PONumberFormat   NVARCHAR(100) NULL,
        IsActive         BIT NOT NULL DEFAULT 1,
        CreatedById      INT NULL,
        CreatedDatetime  DATETIME NOT NULL DEFAULT GETDATE(),
        ModifiedById     INT NULL,
        ModifiedDatetime DATETIME NULL,
        CONSTRAINT UQ_Template UNIQUE (TemplateKey)
    )
    """,
    # Invoice type (PART / SERVICE) added to a table created before it
    # existed; existing rows are backfilled by migrate_template_invoice_type.
    "IF COL_LENGTH('dbo.tbl_Template','InvoiceType') IS NULL "
    "ALTER TABLE dbo.tbl_Template ADD InvoiceType NVARCHAR(20) NULL",
    # Template static values (child of tbl_Template) — normalized one row
    # per (template, sheet, column).
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_TemplateStaticValue')
    CREATE TABLE tbl_TemplateStaticValue (
        Id               INT IDENTITY(1,1) PRIMARY KEY,
        TemplateId       INT NOT NULL,
        SheetName        NVARCHAR(150) NOT NULL,
        ColumnName       NVARCHAR(200) NOT NULL,
        StaticValue      NVARCHAR(MAX) NULL,
        IsActive         BIT NOT NULL DEFAULT 1,
        CreatedById      INT NULL,
        CreatedDatetime  DATETIME NOT NULL DEFAULT GETDATE(),
        ModifiedById     INT NULL,
        ModifiedDatetime DATETIME NULL,
        CONSTRAINT FK_TemplateStatic_Template
            FOREIGN KEY (TemplateId) REFERENCES tbl_Template(Id),
        CONSTRAINT UQ_TemplateStatic UNIQUE (TemplateId, SheetName, ColumnName)
    )
    """,
    # ---- Status / purchase-header base (needed for the tracker FKs) ------
    # tbl_status and a base tbl_Purchase_Header must exist before the tracker
    # table can reference them. tbl_Purchase_Header is normally created lazily
    # during processing (_ensure_table); create a minimal base here so the FK
    # can be declared. init_status_table also seeds tbl_status; seed here too
    # so 'initiated'/'processed' exist regardless of call order.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_status')
    CREATE TABLE tbl_status (
        StatusId   INT IDENTITY(1,1) PRIMARY KEY,
        StatusName NVARCHAR(150) NOT NULL UNIQUE
    )
    """,
    # Rename in place (UPDATE, not delete+reinsert) so every existing
    # tracker row's StatusID keeps pointing at the same row - only its
    # display name changes. Must run before the seed INSERT below, or the
    # seed would insert "DATA MISMATCH" as a brand new row (since it
    # doesn't exist yet under that name) while old rows stay orphaned on
    # the never-updated "INCOMPLETE DATA" name.
    "IF EXISTS (SELECT 1 FROM dbo.tbl_status WHERE StatusName = 'INCOMPLETE DATA') "
    "AND NOT EXISTS (SELECT 1 FROM dbo.tbl_status WHERE StatusName = 'DATA MISMATCH') "
    "UPDATE dbo.tbl_status SET StatusName = 'DATA MISMATCH' WHERE StatusName = 'INCOMPLETE DATA'",
    f"""
    INSERT INTO tbl_status (StatusName)
    SELECT v FROM (VALUES {_STATUS_SEED_VALUES}) t(v)
    WHERE NOT EXISTS (SELECT 1 FROM tbl_status s WHERE s.StatusName = t.v)
    """,
    # Status names are always upper case (normalise any legacy mixed-case row).
    "UPDATE dbo.tbl_status SET StatusName = UPPER(StatusName) "
    "WHERE StatusName COLLATE Latin1_General_BIN <> UPPER(StatusName)",
    # DisplayOrder decouples the Dashboard's "Status breakdown" bar order
    # (and anywhere else that wants a logical, not historical, order) from
    # StatusId - StatusId is an IDENTITY, permanently fixed to whenever a
    # status was first seeded on THIS database, so a status added later
    # can never sort between two earlier ones by StatusId alone, no matter
    # where it sits in STATUS_VALUES. Re-synced from STATUS_VALUES' own
    # order on every startup, so re-ordering that Python list is always
    # enough going forward - no StatusId renumbering, ever.
    "IF COL_LENGTH('dbo.tbl_status', 'DisplayOrder') IS NULL "
    "ALTER TABLE dbo.tbl_status ADD DisplayOrder INT NULL",
    f"""
    UPDATE s SET s.DisplayOrder = v.ord
    FROM dbo.tbl_status s
    JOIN (VALUES {_STATUS_DISPLAY_ORDER_VALUES}) v(name, ord) ON v.name = s.StatusName
    """,
    # ---- Invoice type (PART / SERVICE) — needed for the tracker FK --------
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_InvoiceType')
    CREATE TABLE tbl_InvoiceType (
        InvoiceTypeId   INT IDENTITY(1,1) PRIMARY KEY,
        InvoiceTypeName NVARCHAR(20) NOT NULL UNIQUE
    )
    """,
    f"""
    INSERT INTO tbl_InvoiceType (InvoiceTypeName)
    SELECT v FROM (VALUES {_INVOICE_TYPE_SEED_VALUES}) t(v)
    WHERE NOT EXISTS (SELECT 1 FROM tbl_InvoiceType i WHERE i.InvoiceTypeName = t.v)
    """,
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_Purchase_Header')
    CREATE TABLE tbl_Purchase_Header (
        Id        INT IDENTITY(1,1) PRIMARY KEY,
        CreatedAt DATETIME NOT NULL DEFAULT GETDATE()
    )
    """,
    # InvoiceNo must exist before usp_GetProcessedInvoices is (re)created
    # below — it references the column directly (not dynamic SQL), and SQL
    # Server only defers name resolution for objects that don't exist yet,
    # not for missing columns on a table that already exists. Without this,
    # CREATE PROCEDURE fails to compile on a genuinely fresh database (this
    # never surfaced before because every database this ran against already
    # had real invoices processed, which self-heals the column — see
    # save_grouped/_ensure_table).
    "IF COL_LENGTH('dbo.tbl_Purchase_Header','InvoiceNo') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Header ADD InvoiceNo NVARCHAR(200) NULL",
    # ---- Retire the old table names (were empty; renamed) ----------------
    "IF OBJECT_ID('dbo.tbl_purchaseTracker') IS NOT NULL DROP TABLE dbo.tbl_purchaseTracker",
    "IF OBJECT_ID('dbo.tbl_inputFileLog') IS NOT NULL DROP TABLE dbo.tbl_inputFileLog",
    # ---- Input-file upload log (who copied files in File Explorer) -------
    # One row per uploaded file. The uploader becomes the purchase tracker's
    # InitiatedBy; once the file is processed into a header, the log row is
    # linked to that header (Purchase_Header_ID) and stamped with its
    # Vendor Invoice No.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_InputFile_Log')
    CREATE TABLE tbl_InputFile_Log (
        Id                  INT IDENTITY(1,1) PRIMARY KEY,
        FileName            NVARCHAR(400) NOT NULL,
        RelPath             NVARCHAR(1000) NULL,
        InitiatedByID       INT NULL,
        InitiatedDatetime   DATETIME NOT NULL DEFAULT GETDATE(),
        [Vendor Invoice No.] NVARCHAR(200) NULL,
        Purchase_Header_ID   INT NULL
            CONSTRAINT FK_InputFile_Log_Header REFERENCES tbl_Purchase_Header(Id),
        -- Not FK-constrained: StatusID holds tbl_status ids for normal states
        -- and the sentinel 0 = "reset" (moved to New_Format, re-uploadable).
        StatusID            INT NULL
    )
    """,
    # ---- Purchase tracker (lifecycle per purchase header) ----------------
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_Purchase_Tracker')
    CREATE TABLE tbl_Purchase_Tracker (
        Id                   INT IDENTITY(1,1) PRIMARY KEY,
        Purchase_Header_ID    INT NOT NULL
            CONSTRAINT FK_Purchase_Tracker_Header REFERENCES tbl_Purchase_Header(Id),
        InitiatedByID        INT NULL,
        InitiatedDatetime    DATETIME NULL,
        StartedByID          INT NULL,
        StartedDatetime      DATETIME NULL,
        BatchName            NVARCHAR(200) NULL,  -- the run's batch (moved off header)
        StatusID             INT NULL
            CONSTRAINT FK_Purchase_Tracker_Status REFERENCES tbl_status(StatusId),
        IsActive             BIT NOT NULL DEFAULT 1,
        SyncedByID           INT NULL,          -- who triggered the API sync (always 1)
        SyncedDatetime       DATETIME NULL,     -- when the Service First API was called
        BuyerOrderNo         NVARCHAR(200) NULL, -- PO, used to re-sync pending records
        SourceJson           NVARCHAR(1000) NULL, -- extracted JSON path, reloaded on re-sync
        IsSynced             BIT NULL,           -- part received in SF (1) or not (0)
        FileName             NVARCHAR(400) NULL,  -- source PDF file name
        TemplateFormat       NVARCHAR(100) NULL,  -- matched format / template name
        InvoiceTypeID        INT NULL
            CONSTRAINT FK_Purchase_Tracker_InvoiceType REFERENCES tbl_InvoiceType(InvoiceTypeId),
        CreatedById          INT NULL,
        CreatedDatetime      DATETIME NOT NULL DEFAULT GETDATE(),
        LastModifiedById     INT NULL,
        LastModifiedDatetime DATETIME NULL
    )
    """,
    # Add the tracker columns to a table created before they existed.
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','IsSynced') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD IsSynced BIT NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','FileName') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD FileName NVARCHAR(400) NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','TemplateFormat') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD TemplateFormat NVARCHAR(100) NULL",
    # Include / Exclude (a user can drop an invoice from the Excel export).
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','IsExcluded') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD IsExcluded BIT NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','ExcludedByID') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD ExcludedByID INT NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','ExcludedDatetime') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD ExcludedDatetime DATETIME NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','PriorStatusID') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD PriorStatusID INT NULL",
    # Accounts can reject a LOADED invoice back to REJECTED BY ACCOUNTS with
    # a required remark - see reject_invoice().
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','RejectRemark') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD RejectRemark NVARCHAR(500) NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','RejectedByID') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD RejectedByID INT NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','RejectedDatetime') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD RejectedDatetime DATETIME NULL",
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','InvoiceTypeID') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD InvoiceTypeID INT NULL "
    "CONSTRAINT FK_Purchase_Tracker_InvoiceType REFERENCES dbo.tbl_InvoiceType(InvoiceTypeId)",
    # ---- Move BatchName from the header to the tracker -------------------
    "IF COL_LENGTH('dbo.tbl_Purchase_Tracker','BatchName') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD BatchName NVARCHAR(200) NULL",
    # Backfill the tracker from any existing header BatchName, then drop the
    # header column (dynamic SQL so it compiles whether or not the column is
    # still present). Backfill must precede the drop.
    "IF COL_LENGTH('dbo.tbl_Purchase_Header','BatchName') IS NOT NULL "
    "AND COL_LENGTH('dbo.tbl_Purchase_Tracker','BatchName') IS NOT NULL "
    "EXEC('UPDATE pt SET pt.BatchName = h.BatchName "
    "FROM dbo.tbl_Purchase_Tracker pt "
    "JOIN dbo.tbl_Purchase_Header h ON h.Id = pt.Purchase_Header_ID "
    "WHERE pt.BatchName IS NULL AND h.BatchName IS NOT NULL')",
    "IF COL_LENGTH('dbo.tbl_Purchase_Header','BatchName') IS NOT NULL "
    "ALTER TABLE dbo.tbl_Purchase_Header DROP COLUMN BatchName",
    # ---- Retired: Publish now stages locally, tracked in config.json's
    # publish_status instead of a per-server registration table.
    "IF OBJECT_ID('dbo.tbl_DeployServer') IS NOT NULL DROP TABLE dbo.tbl_DeployServer",
    # ---- Retired: Document No. is minted fresh at every Excel download
    # (see fetch_batch/_assign_document_numbers) - no staging column, and
    # PO_Number_Format is resolved live from the template every time
    # (see _po_number_formats_for_headers) rather than stored.
    "IF COL_LENGTH('dbo.tbl_Purchase_Header','Last_Updated_No') IS NOT NULL "
    "ALTER TABLE dbo.tbl_Purchase_Header DROP COLUMN Last_Updated_No",
    "IF COL_LENGTH('dbo.tbl_Purchase_Header','PO_Number_Format') IS NOT NULL "
    "ALTER TABLE dbo.tbl_Purchase_Header DROP COLUMN PO_Number_Format",
    # ---- Retired: Entry No. is minted fresh at every Excel download too
    # (see fetch_batch/_assign_entry_numbers) - no staging column.
    "IF COL_LENGTH('dbo.tbl_Reservation_Entry','Last_Updated_Entry_No') IS NOT NULL "
    "ALTER TABLE dbo.tbl_Reservation_Entry DROP COLUMN Last_Updated_Entry_No",
    # ---- Remembers whether a batch's Excel has ever been downloaded, for
    # the Dashboard's Batch Status column (Downloaded / Loaded / Posted /
    # Completed) - see database.mark_batch_downloaded/batch_download_info.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_BatchDownload')
    CREATE TABLE tbl_BatchDownload (
        BatchName          NVARCHAR(200) NOT NULL PRIMARY KEY,
        FirstDownloadedAt  DATETIME NOT NULL,
        LastDownloadedAt   DATETIME NOT NULL,
        DownloadCount      INT NOT NULL DEFAULT 1
    )
    """,
    # ---- Remembers whether a batch has ever had an Excluded invoice
    # Included back in - see database._batch_ever_reincluded/
    # _mark_batch_reincluded. Once marked, the batch is shown as at least
    # DOWNLOADED (not locked) even if every remaining invoice reverts to a
    # pre-Loaded status.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_BatchReIncluded')
    CREATE TABLE tbl_BatchReIncluded (
        BatchName  NVARCHAR(200) NOT NULL PRIMARY KEY,
        MarkedAt   DATETIME NOT NULL
    )
    """,
    # ---- Which menu keys each (non-Super-Admin) role can see - the "Screen
    # Access" menu's own storage. Presence of a (RoleName, MenuKey) row means
    # that role can see that menu; Super Admin/Developer are never rows here
    # - they always see every menu, unconfigurable, so a Super Admin editing
    # this table can never lock everyone (including themselves) out. Seeded
    # from _ROLE_MENU_DEFAULTS the first time it's empty - see get_role_menus.
    """
    IF NOT EXISTS (SELECT 1 FROM sys.tables WHERE name = 'tbl_RoleMenu')
    CREATE TABLE tbl_RoleMenu (
        Id       INT IDENTITY(1,1) PRIMARY KEY,
        RoleName NVARCHAR(50) NOT NULL,
        MenuKey  NVARCHAR(50) NOT NULL,
        CONSTRAINT UQ_RoleMenu UNIQUE (RoleName, MenuKey)
    )
    """,
]

# Table-valued parameter types used by the save procedures for bulk MERGE.
_MENU_TYPE_DDL = [
    ("SheetColumnTVP",
     "CREATE TYPE dbo.SheetColumnTVP AS TABLE "
     "(SheetName NVARCHAR(150), ColumnName NVARCHAR(200), SortOrder INT)"),
    ("FieldMappingTVP",
     "CREATE TYPE dbo.FieldMappingTVP AS TABLE "
     "(SheetName NVARCHAR(150), ColumnName NVARCHAR(200), JsonField NVARCHAR(200))"),
    ("TemplateStaticTVP",
     "CREATE TYPE dbo.TemplateStaticTVP AS TABLE "
     "(SheetName NVARCHAR(150), ColumnName NVARCHAR(200), StaticValue NVARCHAR(MAX))"),
]

_MENU_PROC_DDL = [
    # ---- Create Field ----------------------------------------------------
    # Upsert the columns of each sheet present in @Rows; any active column of
    # those sheets that is no longer in @Rows is soft-deleted. Only rows whose
    # order/active flag actually changed are updated.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SaveSheetColumns
        @Rows   dbo.SheetColumnTVP READONLY,
        @UserId INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveSheetColumns @Rows=@SheetCols /* ('Purchase Header','InvoiceNo',1) */, @UserId=7
        MERGE dbo.tbl_SheetColumn AS t
        USING @Rows AS s
            ON t.SheetName = s.SheetName AND t.ColumnName = s.ColumnName
        WHEN MATCHED AND (t.SortOrder <> s.SortOrder OR t.IsActive = 0) THEN
            UPDATE SET t.SortOrder = s.SortOrder, t.IsActive = 1,
                       t.ModifiedById = @UserId, t.ModifiedDatetime = GETDATE()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (SheetName, ColumnName, SortOrder, CreatedById, CreatedDatetime, IsActive)
            VALUES (s.SheetName, s.ColumnName, s.SortOrder, @UserId, GETDATE(), 1)
        WHEN NOT MATCHED BY SOURCE
             AND t.SheetName IN (SELECT DISTINCT SheetName FROM @Rows)
             AND t.IsActive = 1 THEN
            UPDATE SET t.IsActive = 0,
                       t.ModifiedById = @UserId, t.ModifiedDatetime = GETDATE();
    END
    """,
    # ---- Field Mapping ---------------------------------------------------
    # A blank/NULL JsonField means "unmapped" -> soft-delete the row if it
    # exists, insert nothing otherwise. Non-blank values are upserted.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SaveFieldMapping
        @Rows   dbo.FieldMappingTVP READONLY,
        @UserId INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveFieldMapping @Rows=@FieldMap /* ('Purchase Header','InvoiceNo','invoice_no') */, @UserId=7
        -- SQL Server allows only one WHEN MATCHED ... UPDATE, so a single
        -- clause handles both cases via CASE: a blank incoming value
        -- deactivates the row; a non-blank value (re)activates + updates it.
        MERGE dbo.tbl_FieldMapping AS t
        USING @Rows AS s
            ON t.SheetName = s.SheetName AND t.ColumnName = s.ColumnName
        WHEN MATCHED AND (
                 (NULLIF(LTRIM(RTRIM(s.JsonField)), '') IS NULL AND t.IsActive = 1)
              OR (NULLIF(LTRIM(RTRIM(s.JsonField)), '') IS NOT NULL
                    AND (ISNULL(t.JsonField, '') <> s.JsonField OR t.IsActive = 0))
             ) THEN
            UPDATE SET
                t.JsonField = CASE
                    WHEN NULLIF(LTRIM(RTRIM(s.JsonField)), '') IS NULL THEN t.JsonField
                    ELSE s.JsonField END,
                t.IsActive = CASE
                    WHEN NULLIF(LTRIM(RTRIM(s.JsonField)), '') IS NULL THEN 0
                    ELSE 1 END,
                t.ModifiedById = @UserId, t.ModifiedDatetime = GETDATE()
        WHEN NOT MATCHED BY TARGET
             AND NULLIF(LTRIM(RTRIM(s.JsonField)), '') IS NOT NULL THEN
            INSERT (SheetName, ColumnName, JsonField, CreatedById, CreatedDatetime, IsActive)
            VALUES (s.SheetName, s.ColumnName, s.JsonField, @UserId, GETDATE(), 1);
    END
    """,
    # ---- Template (header + static values) -------------------------------
    # Upsert the header, then MERGE its static values: insert new, update
    # changed, soft-delete any active value no longer supplied.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SaveTemplate
        @Entity         NVARCHAR(20),
        @Name           NVARCHAR(400),
        @TemplateKey    NVARCHAR(500),
        @PONumberFormat NVARCHAR(100) = NULL,
        @Static         dbo.TemplateStaticTVP READONLY,
        @UserId         INT = NULL,
        @InvoiceType    NVARCHAR(20) = NULL,
        @OldTemplateKey NVARCHAR(500) = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveTemplate @Entity='SPR', @Name='Bosch', @TemplateKey='SPR\PART\Bosch', @PONumberFormat='SPRPUR/2026/', @Static=@StaticVals, @UserId=7, @InvoiceType='PART'
        -- Renaming a template (Template Edit screen's Template name field)
        -- passes @OldTemplateKey = the key being edited, @TemplateKey = the
        -- new one - looked up by the OLD key so the SAME row (and its
        -- already-associated tbl_TemplateStaticValue rows, FK'd by
        -- TemplateId, not by key) gets its Name/TemplateKey updated in
        -- place, rather than this becoming a brand new template that
        -- leaves the old one behind. A plain (non-rename) save always
        -- passes @OldTemplateKey = NULL, so COALESCE just falls back to
        -- the unchanged @TemplateKey lookup.
        DECLARE @TemplateId INT;
        SELECT @TemplateId = Id FROM dbo.tbl_Template
         WHERE TemplateKey = COALESCE(@OldTemplateKey, @TemplateKey);

        IF @TemplateId IS NULL
        BEGIN
            INSERT INTO dbo.tbl_Template
                (Entity, Name, TemplateKey, PONumberFormat, InvoiceType, CreatedById, CreatedDatetime, IsActive)
            VALUES (@Entity, @Name, @TemplateKey, @PONumberFormat, @InvoiceType, @UserId, GETDATE(), 1);
            SET @TemplateId = SCOPE_IDENTITY();
        END
        ELSE
        BEGIN
            UPDATE dbo.tbl_Template
               SET Entity = @Entity, Name = @Name, TemplateKey = @TemplateKey, PONumberFormat = @PONumberFormat,
                   InvoiceType = @InvoiceType,
                   IsActive = 1, ModifiedById = @UserId, ModifiedDatetime = GETDATE()
             WHERE Id = @TemplateId;
        END

        MERGE dbo.tbl_TemplateStaticValue AS t
        USING (SELECT SheetName, ColumnName, StaticValue FROM @Static) AS s
            ON t.TemplateId = @TemplateId
           AND t.SheetName = s.SheetName AND t.ColumnName = s.ColumnName
        WHEN MATCHED AND (ISNULL(t.StaticValue, '') <> ISNULL(s.StaticValue, '')
                          OR t.IsActive = 0) THEN
            UPDATE SET t.StaticValue = s.StaticValue, t.IsActive = 1,
                       t.ModifiedById = @UserId, t.ModifiedDatetime = GETDATE()
        WHEN NOT MATCHED BY TARGET THEN
            INSERT (TemplateId, SheetName, ColumnName, StaticValue, CreatedById, CreatedDatetime, IsActive)
            VALUES (@TemplateId, s.SheetName, s.ColumnName, s.StaticValue, @UserId, GETDATE(), 1)
        WHEN NOT MATCHED BY SOURCE
             AND t.TemplateId = @TemplateId AND t.IsActive = 1 THEN
            UPDATE SET t.IsActive = 0,
                       t.ModifiedById = @UserId, t.ModifiedDatetime = GETDATE();

        SELECT @TemplateId AS TemplateId;
    END
    """,
    # ---- Template soft-delete -------------------------------------------
    """
    CREATE OR ALTER PROCEDURE dbo.usp_DeleteTemplate
        @TemplateKey NVARCHAR(500),
        @UserId      INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_DeleteTemplate @TemplateKey='SPR\Bosch', @UserId=7
        DECLARE @TemplateId INT;
        SELECT @TemplateId = Id FROM dbo.tbl_Template
         WHERE TemplateKey = @TemplateKey AND IsActive = 1;

        IF @TemplateId IS NULL
        BEGIN
            SELECT 0 AS Deleted;
            RETURN;
        END

        UPDATE dbo.tbl_Template
           SET IsActive = 0, ModifiedById = @UserId, ModifiedDatetime = GETDATE()
         WHERE Id = @TemplateId;
        UPDATE dbo.tbl_TemplateStaticValue
           SET IsActive = 0, ModifiedById = @UserId, ModifiedDatetime = GETDATE()
         WHERE TemplateId = @TemplateId AND IsActive = 1;

        SELECT 1 AS Deleted;
    END
    """,
    # ---- Dashboard / invoice processing: bulk insert -------------------
    # All Header/Line/Reservation rows for a batch arrive as JSON and are
    # inserted set-based (no row-by-row loop). Because the target tables
    # have user-defined columns (Create Field), the column list is supplied
    # per call and the INSERTs are built with dynamic SQL over OPENJSON.
    # The header MERGE captures each new Id into #hmap keyed by the source
    # group index (_gid); lines/reservations join #hmap to set the FK.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SaveInvoiceBatch
        @HeaderCols      NVARCHAR(MAX),
        @LineCols        NVARCHAR(MAX),
        @ResCols         NVARCHAR(MAX),
        @Headers         NVARCHAR(MAX),
        @Lines           NVARCHAR(MAX),
        @Reservations    NVARCHAR(MAX),
        @StartedByID     INT = NULL,
        @StartedDatetime DATETIME2 = NULL,
        @StatusName      NVARCHAR(150) = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveInvoiceBatch @HeaderCols='["InvoiceNo"]', @LineCols='["Description"]', @ResCols='["Serial No."]', @Headers='[{"InvoiceNo":"INV-001","_gid":0}]', @Lines='[{"Description":"Oil Filter","_gid":0}]', @Reservations='[]', @StartedByID=7, @StartedDatetime='2026-07-22T10:15:00', @StatusName='EXTRACTED'

        -- [No.]/[Document No.]/[Source ID]/[Entry No.] are all deliberately
        -- left blank at save time - a real, unique Document No./Entry No.
        -- is only minted at Excel-download time (see database.fetch_batch/
        -- _assign_document_numbers/_assign_entry_numbers), resolving
        -- PO_Number_Format live from the template rather than storing it
        -- on the header at all.

        DECLARE @sql NVARCHAR(MAX), @cols NVARCHAR(MAX), @with NVARCHAR(MAX);

        CREATE TABLE #hmap (_gid INT PRIMARY KEY, HeaderId INT);

        -- Purchase Header: insert all, capture (source _gid -> new Id).
        SELECT
            @cols = STRING_AGG(QUOTENAME([value]), N', '),
            @with = STRING_AGG(
                QUOTENAME([value]) + N' NVARCHAR(MAX) ' +
                -- Only escape the double-quote that would terminate the
                -- path's quoted key segment. STRING_ESCAPE(..., 'json') was
                -- used here before, but it also escapes '/' to '\/' (valid
                -- JSON) — and OPENJSON's path parser does NOT unescape that
                -- back to '/' when matching, so ANY column name containing
                -- a slash (e.g. "Ship-to Country/Region Code",
                -- "HSN/SAC Code") silently came back NULL on every insert.
                QUOTENAME(N'$."' + REPLACE([value], '"', '\"') + N'"', N''''),
                N', ')
        FROM OPENJSON(@HeaderCols);

        IF @cols IS NOT NULL
        BEGIN
            SET @sql = N'
            MERGE dbo.tbl_Purchase_Header AS t
            USING (SELECT [_gid], ' + @cols + N'
                   FROM OPENJSON(@Headers)
                   WITH ([_gid] INT ''$._gid'', ' + @with + N')) AS s
            ON 1 = 0
            WHEN NOT MATCHED THEN
                INSERT (' + @cols + N') VALUES (' + @cols + N')
            OUTPUT s.[_gid], inserted.Id INTO #hmap(_gid, HeaderId);';
            EXEC sp_executesql @sql, N'@Headers NVARCHAR(MAX)', @Headers = @Headers;
        END

        -- Purchase tracker: one row per new header (1:1). InitiatedBy/At come
        -- from each header's source file; the status name and IsActive are
        -- per-invoice (carried in the JSON as _StatusName / _IsActive) so the
        -- Service First outcome can flag each invoice independently.
        -- LastModifiedDatetime is stamped here too (same as CreatedDatetime)
        -- rather than left NULL until some later action touches the row -
        -- usp_ExpireStaleUnresolved measures "how long has this sat
        -- unresolved" off it, and a row nobody has ever touched needs a
        -- real starting timestamp to age from.
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL
        BEGIN
            INSERT INTO dbo.tbl_Purchase_Tracker
                (Purchase_Header_ID, InitiatedByID, InitiatedDatetime,
                 StartedByID, StartedDatetime, BatchName, StatusID, IsActive,
                 SyncedByID, SyncedDatetime, IsSynced, FileName, TemplateFormat,
                 InvoiceTypeID, BuyerOrderNo, SourceJson,
                 CreatedById, CreatedDatetime, LastModifiedById, LastModifiedDatetime)
            SELECT m.HeaderId, s._InitById, s._InitAt,
                   @StartedByID, @StartedDatetime, s._BatchName,
                   st.StatusId, ISNULL(s._IsActive, 1),
                   CASE WHEN ISNULL(s._Synced, 0) = 1 THEN 1 ELSE NULL END,
                   CASE WHEN ISNULL(s._Synced, 0) = 1 THEN GETDATE() ELSE NULL END,
                   ISNULL(s._Synced, 0),
                   s._FileName, s._Format,
                   it.InvoiceTypeId, s._BuyerOrderNo, s._SourceJson,
                   @StartedByID, GETDATE(), @StartedByID, GETDATE()
            FROM OPENJSON(@Headers)
                 WITH ([_gid] INT '$._gid',
                       [_InitById] INT '$._InitById',
                       [_InitAt] DATETIME2 '$._InitAt',
                       [_BatchName] NVARCHAR(200) '$._BatchName',
                       [_StatusName] NVARCHAR(150) '$._StatusName',
                       [_IsActive] BIT '$._IsActive',
                       [_Synced] BIT '$._Synced',
                       [_FileName] NVARCHAR(400) '$._FileName',
                       [_Format] NVARCHAR(100) '$._Format',
                       [_InvoiceType] NVARCHAR(20) '$._InvoiceType',
                       [_BuyerOrderNo] NVARCHAR(200) '$._BuyerOrderNo',
                       [_SourceJson] NVARCHAR(1000) '$._SourceJson') s
            JOIN #hmap m ON m._gid = s.[_gid]
            LEFT JOIN dbo.tbl_status st
                   ON st.StatusName = ISNULL(s._StatusName, @StatusName)
            LEFT JOIN dbo.tbl_InvoiceType it
                   ON it.InvoiceTypeName = s._InvoiceType;
        END

        -- Link each input-file log row to the header it produced, stamping
        -- the Purchase_Header_ID (relationship) and its Vendor Invoice No.
        IF OBJECT_ID('dbo.tbl_InputFile_Log') IS NOT NULL
        BEGIN
            UPDATE l
               SET l.Purchase_Header_ID = m.HeaderId,
                   l.[Vendor Invoice No.] = s.InvoiceNo
            FROM dbo.tbl_InputFile_Log l
            JOIN OPENJSON(@Headers)
                 WITH ([_gid] INT '$._gid',
                       [_rel] NVARCHAR(1000) '$._rel',
                       [InvoiceNo] NVARCHAR(200) '$.InvoiceNo') s
                 ON l.RelPath = s.[_rel]
            JOIN #hmap m ON m._gid = s.[_gid];
        END

        -- Purchase Line: insert with resolved Purchase_Header_ID.
        SELECT
            @cols = STRING_AGG(QUOTENAME([value]), N', '),
            @with = STRING_AGG(
                QUOTENAME([value]) + N' NVARCHAR(MAX) ' +
                -- Only escape the double-quote that would terminate the
                -- path's quoted key segment. STRING_ESCAPE(..., 'json') was
                -- used here before, but it also escapes '/' to '\/' (valid
                -- JSON) — and OPENJSON's path parser does NOT unescape that
                -- back to '/' when matching, so ANY column name containing
                -- a slash (e.g. "Ship-to Country/Region Code",
                -- "HSN/SAC Code") silently came back NULL on every insert.
                QUOTENAME(N'$."' + REPLACE([value], '"', '\"') + N'"', N''''),
                N', ')
        FROM OPENJSON(@LineCols);

        IF @cols IS NOT NULL
        BEGIN
            SET @sql = N'
            INSERT INTO dbo.tbl_Purchase_Line (' + @cols + N', [Purchase_Header_ID])
            SELECT ' + @cols + N', m.HeaderId
            FROM OPENJSON(@Lines)
                 WITH ([_gid] INT ''$._gid'', ' + @with + N') AS s
            JOIN #hmap m ON m._gid = s.[_gid];';
            EXEC sp_executesql @sql, N'@Lines NVARCHAR(MAX)', @Lines = @Lines;
        END

        -- Reservation Entry: insert with resolved Purchase_Header_ID.
        SELECT
            @cols = STRING_AGG(QUOTENAME([value]), N', '),
            @with = STRING_AGG(
                QUOTENAME([value]) + N' NVARCHAR(MAX) ' +
                -- Only escape the double-quote that would terminate the
                -- path's quoted key segment. STRING_ESCAPE(..., 'json') was
                -- used here before, but it also escapes '/' to '\/' (valid
                -- JSON) — and OPENJSON's path parser does NOT unescape that
                -- back to '/' when matching, so ANY column name containing
                -- a slash (e.g. "Ship-to Country/Region Code",
                -- "HSN/SAC Code") silently came back NULL on every insert.
                QUOTENAME(N'$."' + REPLACE([value], '"', '\"') + N'"', N''''),
                N', ')
        FROM OPENJSON(@ResCols);

        IF @cols IS NOT NULL
        BEGIN
            SET @sql = N'
            INSERT INTO dbo.tbl_Reservation_Entry (' + @cols + N', [Purchase_Header_ID])
            SELECT ' + @cols + N', m.HeaderId
            FROM OPENJSON(@Reservations)
                 WITH ([_gid] INT ''$._gid'', ' + @with + N') AS s
            JOIN #hmap m ON m._gid = s.[_gid];';
            EXEC sp_executesql @sql, N'@Reservations NVARCHAR(MAX)', @Reservations = @Reservations;
        END

        DROP TABLE #hmap;
    END
    """,
    # ---- Expire stale unresolved rows (each Start) -------------------------
    # A Data Mismatch/Excluded/New Template invoice nobody has resolved
    # (re-uploaded/fixed, re-included, retrained) within STALE_STATUS_
    # EXPIRY_DAYS is parked permanently as Manually Updated - it stops
    # counting toward batch status (_BATCH_IGNORED_STATUSES) and, since
    # Manually Updated is deliberately never added to
    # _REPROCESSABLE_STATUSES, a later re-upload of the same invoice now
    # falls through to DUPLICATE instead of merging in place (Excluded's
    # own IsExcluded/PriorStatusID columns are left as-is when this fires -
    # the frontend only offers Include/re-inclusion while the tracker's
    # CURRENT status is literally "EXCLUDED", so once StatusID moves off
    # of it that option is already gone). Age is measured off
    # LastModifiedDatetime (stamped at insert - see usp_SaveInvoiceBatch -
    # and refreshed by any later touch, e.g. reprocess_reworkable_header or
    # set_excluded), COALESCEd onto CreatedDatetime only as a safety net
    # for rows saved before that stamp-at-insert fix shipped. Unsupported
    # is NOT handled here - it never gets a Purchase_Header/Tracker row at
    # all (a pure exception + file move - see processor.py), so it's swept
    # separately by filesystem age instead (config_store.expire_stale_files).
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ExpireStaleUnresolved
        @Days INT
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ExpireStaleUnresolved @Days=10
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NULL OR OBJECT_ID('dbo.tbl_status') IS NULL
        BEGIN
            SELECT CAST(NULL AS NVARCHAR(400)) AS FileName WHERE 1 = 0;
            RETURN;
        END
        DECLARE @StaleIds TABLE (StatusId INT);
        INSERT INTO @StaleIds
        SELECT StatusId FROM dbo.tbl_status
         WHERE StatusName IN ('DATA MISMATCH', 'EXCLUDED', 'NEW TEMPLATE');
        DECLARE @ManuallyUpdatedId INT = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = 'MANUALLY UPDATED');
        IF NOT EXISTS (SELECT 1 FROM @StaleIds) OR @ManuallyUpdatedId IS NULL
        BEGIN
            SELECT CAST(NULL AS NVARCHAR(400)) AS FileName WHERE 1 = 0;
            RETURN;
        END

        -- FileName of every row this expires, so the caller can also move
        -- each PDF into the Manually Updated folder (this UPDATE only ever
        -- touches the DB row - a PDF from an EARLIER run isn't part of the
        -- current job's file list, so nothing else would move it).
        DECLARE @Expired TABLE (FileName NVARCHAR(400));

        UPDATE dbo.tbl_Purchase_Tracker
           SET StatusID = @ManuallyUpdatedId,
               IsActive = 0,
               LastModifiedDatetime = GETDATE()
        OUTPUT inserted.FileName INTO @Expired
         WHERE StatusID IN (SELECT StatusId FROM @StaleIds)
           AND COALESCE(LastModifiedDatetime, CreatedDatetime) <= DATEADD(day, -@Days, GETDATE());

        SELECT FileName FROM @Expired;
    END
    """,
    # ---- Re-sync pending reservations (each Start) -----------------------
    # Records still "pending in SF" (no reservation received yet) that should
    # be re-attempted against the API on the next Start.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetPendingSync
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetPendingSync
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS INT) AS HeaderId,
                         CAST(NULL AS NVARCHAR(200)) AS BuyerOrderNo,
                         CAST(NULL AS NVARCHAR(1000)) AS SourceJson;
            RETURN;
        END
        SELECT pt.Purchase_Header_ID, pt.BuyerOrderNo, pt.SourceJson
        FROM dbo.tbl_Purchase_Tracker pt
        JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID
        WHERE s.StatusName = 'PENDING IN SF';
    END
    """,
    # Replace one header's reservation rows and update its tracker verdict
    # (used when a pending record's part is now available in Service First).
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ReplaceReservation
        @HeaderId   INT,
        @Cols       NVARCHAR(MAX),   -- JSON array of reservation column names
        @Rows       NVARCHAR(MAX),   -- JSON array of {col: value}
        @StatusName NVARCHAR(150),
        @IsActive   BIT,
        @SyncedById INT,
        @BatchName  NVARCHAR(300) = NULL   -- pass the CURRENT run's batch to
                                            -- move a promoted record into it
                                            -- (resync_pending); NULL (the
                                            -- default) leaves BatchName as-is
                                            -- for callers outside a Start run
                                            -- (e.g. apply_manual_buyer_order)
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ReplaceReservation @HeaderId=42, @Cols='["Serial No.","Nav_Item_No"]', @Rows='[{"Serial No.":"SN123","Nav_Item_No":"ITM-1001"}]', @StatusName='READY TO LOAD', @IsActive=1, @SyncedById=1, @BatchName='PIIPS_Batch_20260902_120000'

        IF OBJECT_ID('dbo.tbl_Reservation_Entry') IS NOT NULL
            DELETE FROM dbo.tbl_Reservation_Entry WHERE Purchase_Header_ID = @HeaderId;

        -- Internal var names avoid colliding (case-insensitively) with the
        -- @Cols / @Rows parameters.
        DECLARE @collist NVARCHAR(MAX), @withdef NVARCHAR(MAX), @sql NVARCHAR(MAX);
        SELECT @collist = STRING_AGG(QUOTENAME(c.name), N', '),
               @withdef = STRING_AGG(
                   QUOTENAME(c.name) + N' NVARCHAR(MAX) ' +
                   QUOTENAME(N'$."' + STRING_ESCAPE(c.name, 'json') + N'"', N''''),
                   N', ')
        FROM OPENJSON(@Cols) j
        JOIN sys.columns c ON c.object_id = OBJECT_ID('dbo.tbl_Reservation_Entry')
                           AND c.name = j.value;

        IF @collist IS NOT NULL AND OBJECT_ID('dbo.tbl_Reservation_Entry') IS NOT NULL
        BEGIN
            SET @sql = N'INSERT INTO dbo.tbl_Reservation_Entry (' + @collist + N', [Purchase_Header_ID])
                         SELECT ' + @collist + N', @hid
                         FROM OPENJSON(@Rows) WITH (' + @withdef + N') s';
            EXEC sp_executesql @sql, N'@Rows NVARCHAR(MAX), @hid INT',
                               @Rows = @Rows, @hid = @HeaderId;
        END

        UPDATE dbo.tbl_Purchase_Tracker
           SET StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = @StatusName),
               IsActive = @IsActive,
               SyncedByID = @SyncedById,
               SyncedDatetime = GETDATE(),
               LastModifiedById = @SyncedById,
               LastModifiedDatetime = GETDATE(),
               BatchName = ISNULL(@BatchName, BatchName)
         WHERE Purchase_Header_ID = @HeaderId;
    END
    """,
    # ---- Dashboard: count of purchase headers by status -----------------
    """
    CREATE OR ALTER PROCEDURE dbo.usp_StatusCounts
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_StatusCounts
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS INT) AS StatusId,
                         CAST(NULL AS NVARCHAR(150)) AS StatusName,
                         CAST(0 AS INT) AS Cnt;
            RETURN;
        END
        -- Every status appears, even with a 0 count (for the bar chart).
        -- Ordered by DisplayOrder (a logical order re-synced from
        -- STATUS_VALUES on every startup - see its own DDL comment), not
        -- StatusId (an IDENTITY frozen at whenever each status was first
        -- seeded, so a status added later can never sort where it
        -- logically belongs by StatusId alone).
        -- NOLOCK: pure display (Dashboard tiles), never gates a write decision.
        SELECT s.StatusId, s.StatusName, COUNT(pt.Id) AS Cnt
        FROM dbo.tbl_status s WITH (NOLOCK)
        LEFT JOIN dbo.tbl_Purchase_Tracker pt WITH (NOLOCK) ON pt.StatusID = s.StatusId
        GROUP BY s.StatusId, s.StatusName, s.DisplayOrder
        ORDER BY ISNULL(s.DisplayOrder, s.StatusId);
    END
    """,
    # ---- User Management -------------------------------------------------
    # Password hashing stays in Python (stdlib PBKDF2); the procedures only
    # persist the already-hashed value. Users are soft-deleted via IsActive
    # (usp_SetUserActive), never physically removed.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_CreateUser
        @UserName          NVARCHAR(100),
        @UserTypeID        INT,
        @PasswordHash      NVARCHAR(256),
        @Email             NVARCHAR(200) = NULL,
        @CreatedById       INT = NULL,
        @MustChangePassword BIT = 1
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_CreateUser @UserName='jsmith', @UserTypeID=1, @PasswordHash='pbkdf2_sha256$100000$abcd$ef01', @Email='jsmith@precisionit.co.in', @CreatedById=7
        -- @MustChangePassword defaults to 1 (the normal auto-generated-
        -- and-emailed-temp-password flow) - a Viewer account, whose
        -- password a Super Admin assigns directly as a persistent
        -- credential rather than a temp one, passes 0.
        IF EXISTS (SELECT 1 FROM dbo.tbl_user WHERE UserName = @UserName)
        BEGIN
            SELECT -1 AS Status;   -- already exists
            RETURN;
        END
        INSERT INTO dbo.tbl_user
            (UserName, UserTypeID, Password, Email, MustChangePassword, IsActive, CreatedById, CreatedDatetime)
        VALUES (@UserName, @UserTypeID, @PasswordHash, @Email, @MustChangePassword, 1, @CreatedById, GETDATE());
        SELECT 0 AS Status;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ResetPassword
        @UserName     NVARCHAR(100),
        @PasswordHash NVARCHAR(256),
        @ForceChange  BIT = 0,
        @ModifiedById INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ResetPassword @UserName='jsmith', @PasswordHash='pbkdf2_sha256$100000$abcd$ef01', @ForceChange=1, @ModifiedById=7
        UPDATE dbo.tbl_user
           SET Password = @PasswordHash,
               MustChangePassword = @ForceChange,
               LastModifiedById = @ModifiedById,
               LastModifiedDatetime = GETDATE()
         WHERE UserName = @UserName;
        SELECT @@ROWCOUNT AS Affected;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SetUserActive
        @UserId       INT,
        @IsActive     BIT,
        @ModifiedById INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SetUserActive @UserId=12, @IsActive=0, @ModifiedById=7
        UPDATE dbo.tbl_user
           SET IsActive = @IsActive,
               LastModifiedById = @ModifiedById,
               LastModifiedDatetime = GETDATE()
         WHERE UserId = @UserId;
        SELECT @@ROWCOUNT AS Affected;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SetUserType
        @UserId       INT,
        @UserTypeID   INT,
        @ModifiedById INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SetUserType @UserId=12, @UserTypeID=1, @ModifiedById=7
        UPDATE dbo.tbl_user
           SET UserTypeID = @UserTypeID,
               LastModifiedById = @ModifiedById,
               LastModifiedDatetime = GETDATE()
         WHERE UserId = @UserId;
        SELECT @@ROWCOUNT AS Affected;
    END
    """,
    # ---- Mail server settings ---------------------------------------------
    # Single-row table (SettingID, UserName, EmailID, Password, SMTPHost,
    # SMTPPort). Password is stored DPAPI-encrypted (secret_store) - these
    # procedures only move the already-encrypted string, never touch it.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetMailSettings
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetMailSettings
        IF OBJECT_ID('dbo.tbl_MailSettings') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS INT) AS SettingID,
                         CAST(NULL AS NVARCHAR(100)) AS UserName,
                         CAST(NULL AS NVARCHAR(200)) AS EmailID,
                         CAST(NULL AS NVARCHAR(500)) AS Password,
                         CAST(NULL AS NVARCHAR(200)) AS SMTPHost,
                         CAST(NULL AS INT) AS SMTPPort;
            RETURN;
        END
        SELECT TOP 1 SettingID, UserName, EmailID, Password, SMTPHost, SMTPPort
        FROM dbo.tbl_MailSettings
        ORDER BY SettingID;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_SaveMailSettings
        @SettingID     INT,
        @UserName      NVARCHAR(100),
        @EmailID       NVARCHAR(200),
        @PasswordEnc   NVARCHAR(500),
        @SMTPHost      NVARCHAR(200),
        @SMTPPort      INT
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveMailSettings @SettingID=31, @UserName='SF APP', @EmailID='sfapp@precisionit.co.in', @PasswordEnc='enc:dpapi:...', @SMTPHost='mail.precisionit.co.in', @SMTPPort=587
        UPDATE dbo.tbl_MailSettings
           SET UserName = @UserName,
               EmailID  = @EmailID,
               Password = @PasswordEnc,
               SMTPHost = @SMTPHost,
               SMTPPort = @SMTPPort
         WHERE SettingID = @SettingID;
        SELECT @@ROWCOUNT AS Affected;
    END
    """,
    # ---- Purchase tracker: input-file upload log -------------------------
    # Records who uploaded each file in File Explorer, with status
    # 'initiated'. Bulk insert from a JSON array (one call per upload).
    """
    CREATE OR ALTER PROCEDURE dbo.usp_LogInputFiles
        @Files         NVARCHAR(MAX),   -- [{"RelPath":..,"FileName":..}, ...]
        @InitiatedByID INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_LogInputFiles @Files='[{"RelPath":"SPR/Bosch/inv1.pdf","FileName":"inv1.pdf"}]', @InitiatedByID=7
        DECLARE @sid INT = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = 'INITIATED');
        INSERT INTO dbo.tbl_InputFile_Log
            (FileName, RelPath, InitiatedByID, InitiatedDatetime, StatusID)
        SELECT j.FileName, j.RelPath, @InitiatedByID, GETDATE(), @sid
        FROM OPENJSON(@Files)
             WITH (RelPath NVARCHAR(1000) '$.RelPath',
                   FileName NVARCHAR(400) '$.FileName') j
        WHERE j.FileName IS NOT NULL;
    END
    """,
    # ---- Purchase tracker: resolve initiators for a set of files ---------
    # For each requested relative path, return the most recent uploader.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetInitiators
        @Paths NVARCHAR(MAX)             -- JSON array of relative-path strings
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetInitiators @Paths='["SPR/Bosch/inv1.pdf","SPR/Bosch/inv2.pdf"]'
        IF OBJECT_ID('dbo.tbl_InputFile_Log') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS NVARCHAR(1000)) AS RelPath,
                         CAST(NULL AS INT) AS InitiatedByID,
                         CAST(NULL AS DATETIME) AS InitiatedDatetime;
            RETURN;
        END
        ;WITH wanted AS (SELECT value AS RelPath FROM OPENJSON(@Paths)),
        ranked AS (
            SELECT l.RelPath, l.InitiatedByID, l.InitiatedDatetime,
                   ROW_NUMBER() OVER (PARTITION BY l.RelPath
                                      ORDER BY l.InitiatedDatetime DESC, l.Id DESC) AS rn
            FROM dbo.tbl_InputFile_Log l
            JOIN wanted w ON w.RelPath = l.RelPath
        )
        SELECT RelPath, InitiatedByID, InitiatedDatetime FROM ranked WHERE rn = 1;
    END
    """,
    # ---- Upload dedup: which of these files were already initiated --------
    # For each requested relative path that already exists in the log, return
    # who initiated it and when (with the user name) so the upload UI can show
    # a result table and skip re-loading duplicates.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetInitiatedFiles
        @Paths NVARCHAR(MAX)             -- JSON array of relative-path strings
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetInitiatedFiles @Paths='["SPR/Bosch/inv1.pdf"]'
        IF OBJECT_ID('dbo.tbl_InputFile_Log') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS NVARCHAR(1000)) AS RelPath,
                         CAST(NULL AS NVARCHAR(400)) AS FileName,
                         CAST(NULL AS INT) AS InitiatedByID,
                         CAST(NULL AS NVARCHAR(100)) AS InitiatedByName,
                         CAST(NULL AS DATETIME) AS InitiatedDatetime;
            RETURN;
        END
        ;WITH wanted AS (SELECT value AS RelPath FROM OPENJSON(@Paths)),
        ranked AS (
            SELECT l.RelPath, l.FileName, l.InitiatedByID, l.InitiatedDatetime, l.StatusID,
                   s.StatusName,
                   ROW_NUMBER() OVER (PARTITION BY l.RelPath
                                      ORDER BY l.InitiatedDatetime DESC, l.Id DESC) AS rn
            FROM dbo.tbl_InputFile_Log l
            JOIN wanted w ON w.RelPath = l.RelPath
            LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.Purchase_Header_ID = l.Purchase_Header_ID
            LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID
        )
        -- StatusID = 0 means the file was reset (moved to New_Format) and may
        -- be uploaded again by anyone, so it is NOT treated as a duplicate.
        -- Same for a file whose linked invoice is currently in any of
        -- _REPROCESSABLE_STATUSES (Excluded/Pending In SF/Data Mismatch/New
        -- Template) - KEEP THIS LIST IN SYNC with database._REPROCESSABLE_
        -- STATUSES. Re-uploading one of these is a deliberate correction
        -- (see processor.py/reprocess_reworkable_header), not a duplicate,
        -- so it must reach processing rather than being silently skipped
        -- here before it ever gets that far.
        SELECT r.RelPath, r.FileName, r.InitiatedByID, u.UserName AS InitiatedByName,
               r.InitiatedDatetime
        FROM ranked r
        LEFT JOIN dbo.tbl_user u ON u.UserId = r.InitiatedByID
        WHERE r.rn = 1 AND ISNULL(r.StatusID, -1) <> 0
          AND ISNULL(r.StatusName, '') NOT IN ('EXCLUDED', 'PENDING IN SF', 'DATA MISMATCH', 'NEW TEMPLATE');
    END
    """,
    # ---- Reset input-file log entries (moved to New_Format) --------------
    # Set StatusID = 0 for the given relative paths so any user may upload the
    # same PDF again (used after a run moves untrained-format PDFs away).
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ResetInputFiles
        @Paths NVARCHAR(MAX)             -- JSON array of relative-path strings
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ResetInputFiles @Paths='["SPR/Bosch/inv1.pdf"]'
        IF OBJECT_ID('dbo.tbl_InputFile_Log') IS NULL RETURN;
        UPDATE l
           SET l.StatusID = 0
        FROM dbo.tbl_InputFile_Log l
        JOIN OPENJSON(@Paths) j ON j.value = l.RelPath;
    END
    """,
    # ---- Reads: menu (Create Field / Field Mapping / Template) -----------
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetSheetColumns
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetSheetColumns
        SELECT SheetName, ColumnName
        FROM dbo.tbl_SheetColumn WITH (NOLOCK)
        WHERE IsActive = 1
        ORDER BY SheetName, SortOrder, Id;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetFieldMapping
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetFieldMapping
        SELECT SheetName, ColumnName, JsonField
        FROM dbo.tbl_FieldMapping WITH (NOLOCK)
        WHERE IsActive = 1 AND JsonField IS NOT NULL AND JsonField <> '';
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetTemplates
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetTemplates
        SELECT Id, TemplateKey, PONumberFormat
        FROM dbo.tbl_Template WITH (NOLOCK)
        WHERE IsActive = 1;

        SELECT sv.TemplateId, sv.SheetName, sv.ColumnName, sv.StaticValue
        FROM dbo.tbl_TemplateStaticValue sv WITH (NOLOCK)
        JOIN dbo.tbl_Template t WITH (NOLOCK) ON t.Id = sv.TemplateId AND t.IsActive = 1
        WHERE sv.IsActive = 1;
    END
    """,
    # ---- Reads: User Management ------------------------------------------
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ListUserTypes
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ListUserTypes
        IF OBJECT_ID('dbo.tbl_UserType') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS INT) AS UserTypeId,
                         CAST(NULL AS NVARCHAR(100)) AS UserTypeName;
            RETURN;
        END
        SELECT UserTypeId, UserTypeName FROM dbo.tbl_UserType WITH (NOLOCK) ORDER BY UserTypeId;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ListUsers
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ListUsers
        SELECT u.UserId, u.UserName, u.UserTypeID, t.UserTypeName, u.IsActive,
               u.Email, u.MustChangePassword,
               u.CreatedById, u.CreatedDatetime,
               u.LastModifiedById, u.LastModifiedDatetime
        FROM dbo.tbl_user u WITH (NOLOCK)
        LEFT JOIN dbo.tbl_UserType t WITH (NOLOCK) ON u.UserTypeID = t.UserTypeId
        ORDER BY u.UserId;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetUserByName
        @UserName NVARCHAR(100)
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetUserByName @UserName='jsmith'
        SELECT u.UserId, u.UserName, u.UserTypeID, t.UserTypeName,
               u.Password, u.IsActive, u.Email, u.MustChangePassword
        FROM dbo.tbl_user u
        LEFT JOIN dbo.tbl_UserType t ON u.UserTypeID = t.UserTypeId
        WHERE u.UserName = @UserName;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetUserByEmail
        @Email NVARCHAR(200)
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetUserByEmail @Email='jsmith@precisionit.co.in'
        SELECT u.UserId, u.UserName, u.UserTypeID, t.UserTypeName,
               u.Password, u.IsActive, u.Email, u.MustChangePassword
        FROM dbo.tbl_user u
        LEFT JOIN dbo.tbl_UserType t ON u.UserTypeID = t.UserTypeId
        WHERE u.Email = @Email;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetUserRole
        @UserId INT
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetUserRole @UserId=12
        SELECT t.UserTypeName, u.IsActive
        FROM dbo.tbl_user u
        LEFT JOIN dbo.tbl_UserType t ON u.UserTypeID = t.UserTypeId
        WHERE u.UserId = @UserId;
    END
    """,
    # ---- Reads: batches / processed invoices -----------------------------
    """
    CREATE OR ALTER PROCEDURE dbo.usp_ListBatches
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ListBatches
        -- Two result sets:
        --   1) one row per batch (created / total headers / exportable count);
        --   2) one row per (batch, status) with the tracker-row count for that
        --      status — the dashboard renders a column per status from this.
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NULL
           OR COL_LENGTH('dbo.tbl_Purchase_Tracker', 'BatchName') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS NVARCHAR(200)) AS BatchName,
                         CAST(NULL AS DATETIME) AS created,
                         CAST(0 AS INT) AS headers,
                         CAST(0 AS INT) AS exportable;
            SELECT TOP 0 CAST(NULL AS NVARCHAR(200)) AS BatchName,
                         CAST(0 AS INT) AS StatusId,
                         CAST(NULL AS NVARCHAR(150)) AS StatusName,
                         CAST(0 AS INT) AS Cnt;
            RETURN;
        END

        -- 1) Per-batch summary. `exportable` = invoices that would appear in
        -- the Excel (active AND not excluded); the UI hides Download when 0.
        -- Ordered by BatchName itself (newest first), not MIN(h.CreatedAt) -
        -- a header re-processed into an existing record (see
        -- database.reprocess_reworkable_header) deliberately keeps its
        -- original CreatedAt, so that column no longer reflects when the
        -- batch containing it was actually run. BatchName is always
        -- "PIIPS_Batch_YYYYMMDD_HHMMSS" (job.batch_name in processor.py),
        -- which sorts correctly as a plain string without that problem.
        SELECT pt.BatchName, MIN(h.CreatedAt) AS created, COUNT(*) AS headers,
               SUM(CASE WHEN pt.IsActive = 1 AND ISNULL(pt.IsExcluded, 0) = 0
                        THEN 1 ELSE 0 END) AS exportable
        FROM dbo.tbl_Purchase_Tracker pt
        JOIN dbo.tbl_Purchase_Header h ON h.Id = pt.Purchase_Header_ID
        WHERE pt.BatchName IS NOT NULL
        GROUP BY pt.BatchName
        ORDER BY pt.BatchName DESC;

        -- 2) Count of tracker rows per StatusID, per batch. Deliberately
        -- NOT filtered by IsActive - see invoices_by_batch()'s docstring
        -- for why (every non-terminal status sets IsActive=0, so filtering
        -- on it here would undercount exactly the statuses users most need
        -- an accurate count for).
        SELECT pt.BatchName, s.StatusId, s.StatusName, COUNT(*) AS Cnt
        FROM dbo.tbl_Purchase_Tracker pt
        LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID
        WHERE pt.BatchName IS NOT NULL
        GROUP BY pt.BatchName, s.StatusId, s.StatusName;
    END
    """,
    """
    CREATE OR ALTER PROCEDURE dbo.usp_GetProcessedInvoices
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_GetProcessedInvoices
        IF OBJECT_ID('dbo.tbl_Purchase_Header') IS NULL
        BEGIN
            SELECT TOP 0 CAST(NULL AS NVARCHAR(200)) AS InvoiceNo,
                         CAST(NULL AS NVARCHAR(200)) AS BatchName;
            RETURN;
        END
        -- InvoiceNo itself is guaranteed to exist (added in _MENU_TABLE_DDL
        -- before this procedure is ever created). Backfill it from the
        -- mapped 'Vendor Invoice No.' for rows saved before it existed.
        IF COL_LENGTH('dbo.tbl_Purchase_Header', 'Vendor Invoice No.') IS NOT NULL
            EXEC('UPDATE dbo.tbl_Purchase_Header SET InvoiceNo = [Vendor Invoice No.] '
               + 'WHERE InvoiceNo IS NULL AND [Vendor Invoice No.] IS NOT NULL');
        -- BatchName now lives on the tracker (informational for dedup).
        -- HeaderId/IsReprocessable let the caller re-process an invoice
        -- whose only existing record is in one of database.
        -- _REPROCESSABLE_STATUSES (Excluded/Pending In SF/Data Mismatch/
        -- New Template), instead of flagging it as a duplicate (see
        -- database.get_processed_invoices/reprocess_reworkable_header) -
        -- KEEP THIS LIST IN SYNC with _REPROCESSABLE_STATUSES. Picks one
        -- representative header per invoice no. (MIN Id) since an invoice
        -- no. is expected to map to a single real header in practice.
        -- BuyerOrderNo comes from the tracker (a fixed schema column,
        -- always present) rather than the header's own optional, Excel-
        -- mapped "Buyer's Order No." column (may not exist depending on
        -- field mapping) - part of the (InvoiceNo, vendor, PO) match key.
        SELECT h.InvoiceNo, MIN(pt.BatchName) AS BatchName,
               MIN(h.Id) AS HeaderId,
               MAX(CASE WHEN s.StatusName IN (
                       'EXCLUDED', 'PENDING IN SF', 'DATA MISMATCH', 'NEW TEMPLATE'
                   ) THEN 1 ELSE 0 END) AS IsReprocessable,
               MIN(pt.BuyerOrderNo) AS BuyerOrderNo
        FROM dbo.tbl_Purchase_Header h
        LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.Purchase_Header_ID = h.Id
        LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID
        WHERE h.InvoiceNo IS NOT NULL AND h.InvoiceNo <> ''
        GROUP BY h.InvoiceNo;
    END
    """,
    # ---- Read: a saved batch's rows (dynamic columns) --------------------
    # Selects the intersection of the requested columns (JSON arrays) with
    # the columns that actually exist on each table, for the given batch.
    # Emits exactly three result sets: header, line, reservation.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_FetchBatch
        @BatchName  NVARCHAR(200),
        @HeaderCols NVARCHAR(MAX),
        @LineCols   NVARCHAR(MAX),
        @ResCols    NVARCHAR(MAX)
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_FetchBatch @BatchName='PIIPS_Batch_20260722_101500', @HeaderCols='["InvoiceNo"]', @LineCols='["Description"]', @ResCols='["Serial No."]'
        DECLARE @sql NVARCHAR(MAX), @cols NVARCHAR(MAX);

        -- The batch's exportable headers: BatchName now lives on the tracker,
        -- only active, non-excluded invoices are exported, and — once an
        -- invoice has advanced past Ready to Load — it's already been
        -- finalized (see advance_status's Load step) and has nothing left
        -- to (re-)download, so it's excluded here too.
        DECLARE @idset NVARCHAR(600) =
            N'(SELECT pt.Purchase_Header_ID FROM dbo.tbl_Purchase_Tracker pt '
          + N'LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID '
          + N'WHERE pt.BatchName = @b AND pt.IsActive = 1 AND ISNULL(pt.IsExcluded, 0) = 0 '
          + N'AND ISNULL(s.StatusName, '''') NOT IN (''LOADED'', ''POSTED'', ''COMPLETED''))';

        -- Header
        IF OBJECT_ID('dbo.tbl_Purchase_Header') IS NULL
            SELECT TOP 0 CAST(NULL AS INT) AS _empty;
        ELSE
        BEGIN
            SELECT @cols = STRING_AGG(QUOTENAME(c.name), N', ')
            FROM OPENJSON(@HeaderCols) j
            JOIN sys.columns c ON c.object_id = OBJECT_ID('dbo.tbl_Purchase_Header')
                               AND c.name = j.value;
            IF @cols IS NULL
                SELECT TOP 0 CAST(NULL AS INT) AS _empty;
            ELSE
            BEGIN
                SET @sql = N'SELECT ' + @cols +
                           N' FROM dbo.tbl_Purchase_Header WHERE Id IN ' + @idset +
                           N' ORDER BY Id';
                EXEC sp_executesql @sql, N'@b NVARCHAR(200)', @b = @BatchName;
            END
        END

        -- Line
        IF OBJECT_ID('dbo.tbl_Purchase_Line') IS NULL
            SELECT TOP 0 CAST(NULL AS INT) AS _empty;
        ELSE
        BEGIN
            SELECT @cols = STRING_AGG(QUOTENAME(c.name), N', ')
            FROM OPENJSON(@LineCols) j
            JOIN sys.columns c ON c.object_id = OBJECT_ID('dbo.tbl_Purchase_Line')
                               AND c.name = j.value;
            IF @cols IS NULL
                SELECT TOP 0 CAST(NULL AS INT) AS _empty;
            ELSE
            BEGIN
                SET @sql = N'SELECT ' + @cols +
                    N' FROM dbo.tbl_Purchase_Line WHERE Purchase_Header_ID IN ' + @idset +
                    N' ORDER BY Purchase_Header_ID, Id';
                EXEC sp_executesql @sql, N'@b NVARCHAR(200)', @b = @BatchName;
            END
        END

        -- Reservation
        IF OBJECT_ID('dbo.tbl_Reservation_Entry') IS NULL
            SELECT TOP 0 CAST(NULL AS INT) AS _empty;
        ELSE
        BEGIN
            SELECT @cols = STRING_AGG(QUOTENAME(c.name), N', ')
            FROM OPENJSON(@ResCols) j
            JOIN sys.columns c ON c.object_id = OBJECT_ID('dbo.tbl_Reservation_Entry')
                               AND c.name = j.value;
            IF @cols IS NULL
                SELECT TOP 0 CAST(NULL AS INT) AS _empty;
            ELSE
            BEGIN
                SET @sql = N'SELECT ' + @cols +
                    N' FROM dbo.tbl_Reservation_Entry WHERE Purchase_Header_ID IN ' + @idset +
                    N' ORDER BY Purchase_Header_ID, Id';
                EXEC sp_executesql @sql, N'@b NVARCHAR(200)', @b = @BatchName;
            END
        END
    END
    """,
]


_menu_schema_ready = False


def _table_type_exists(cur, name):
    cur.execute(
        "SELECT 1 FROM sys.types WHERE name = ? AND is_table_type = 1", name
    )
    return cur.fetchone() is not None


# Idempotent renames applied to existing databases: table case-normalisation
# (tbl_user -> tbl_User, tbl_status -> tbl_Status) and unifying the
# purchase-header foreign-key column to Purchase_Header_ID across every table
# that has it. A case-sensitive check (or the underscore difference) makes
# each rename fire exactly once; fresh installs already use the final names.
_CS = "COLLATE Latin1_General_CS_AS"

_RENAME_MIGRATIONS = [
    f"IF EXISTS (SELECT 1 FROM sys.tables WHERE name='tbl_user' {_CS}) "
    "EXEC sp_rename 'dbo.tbl_user', 'tbl_User'",
    f"IF EXISTS (SELECT 1 FROM sys.tables WHERE name='tbl_status' {_CS}) "
    "EXEC sp_rename 'dbo.tbl_status', 'tbl_Status'",
    # No-underscore variant -> standard name (Tracker, InputFile_Log)
    "IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.columns "
    "WHERE object_id=OBJECT_ID('dbo.tbl_Purchase_Tracker') AND name='Purchase_headerid') "
    "EXEC sp_rename 'dbo.tbl_Purchase_Tracker.Purchase_headerid', 'Purchase_Header_ID', 'COLUMN'",
    "IF OBJECT_ID('dbo.tbl_InputFile_Log') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.columns "
    "WHERE object_id=OBJECT_ID('dbo.tbl_InputFile_Log') AND name='Purchase_headerid') "
    "EXEC sp_rename 'dbo.tbl_InputFile_Log.Purchase_headerid', 'Purchase_Header_ID', 'COLUMN'",
    # Case-only variant -> standard name (Line, Reservation)
    f"IF OBJECT_ID('dbo.tbl_Purchase_Line') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.columns "
    f"WHERE object_id=OBJECT_ID('dbo.tbl_Purchase_Line') AND name='Purchase_header_id' {_CS}) "
    "EXEC sp_rename 'dbo.tbl_Purchase_Line.Purchase_header_id', 'Purchase_Header_ID', 'COLUMN'",
    f"IF OBJECT_ID('dbo.tbl_Reservation_Entry') IS NOT NULL AND EXISTS (SELECT 1 FROM sys.columns "
    f"WHERE object_id=OBJECT_ID('dbo.tbl_Reservation_Entry') AND name='Purchase_header_id' {_CS}) "
    "EXEC sp_rename 'dbo.tbl_Reservation_Entry.Purchase_header_id', 'Purchase_Header_ID', 'COLUMN'",
    # Drop the tbl_status FK on the input-file log so StatusID can be set to
    # the sentinel 0 (reset / re-uploadable) for files moved to New_Format.
    "IF OBJECT_ID('dbo.FK_InputFile_Log_Status', 'F') IS NOT NULL "
    "ALTER TABLE dbo.tbl_InputFile_Log DROP CONSTRAINT FK_InputFile_Log_Status",
    # Add IsActive to the purchase tracker (0 flags a validation/SF failure).
    "IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL "
    "AND COL_LENGTH('dbo.tbl_Purchase_Tracker', 'IsActive') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD IsActive BIT NOT NULL "
    "CONSTRAINT DF_Purchase_Tracker_IsActive DEFAULT 1 WITH VALUES",
    # Add the sync / re-sync columns to the purchase tracker.
    "IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL "
    "AND COL_LENGTH('dbo.tbl_Purchase_Tracker', 'SyncedByID') IS NULL "
    "ALTER TABLE dbo.tbl_Purchase_Tracker ADD "
    "SyncedByID INT NULL, SyncedDatetime DATETIME NULL, "
    "BuyerOrderNo NVARCHAR(200) NULL, SourceJson NVARCHAR(1000) NULL",
    # Rename the 'Developer' user type to 'Super Admin' (keeps the same id,
    # so existing users of that type keep their access).
    "IF OBJECT_ID('dbo.tbl_UserType') IS NOT NULL "
    "UPDATE dbo.tbl_UserType SET UserTypeName = 'Super Admin' "
    "WHERE UserTypeName = 'Developer'",
]


def _apply_rename_migrations(cur):
    for stmt in _RENAME_MIGRATIONS:
        cur.execute(stmt)


def ensure_menu_schema(force=False):
    """Create the tables, table types and stored procedures used by the
    menus (Create Field / Field Mapping / Template), User Management and the
    invoice bulk-save path, if missing. Idempotent and cached — after the
    first success it is a no-op.
    Sample: ensure_menu_schema(force=True)"""
    global _menu_schema_ready
    if _menu_schema_ready and not force:
        return

    conn = get_connection()
    try:
        cur = conn.cursor()
        for ddl in _MENU_TABLE_DDL:
            cur.execute(ddl)
        conn.commit()

        # Normalise legacy table/column names BEFORE (re)creating the
        # procedures, whose static references use the final names.
        _apply_rename_migrations(cur)
        conn.commit()

        # Table types can't be wrapped in IF; check the catalog first.
        for type_name, type_ddl in _MENU_TYPE_DDL:
            if not _table_type_exists(cur, type_name):
                cur.execute(type_ddl)
        conn.commit()

        for proc_ddl in _MENU_PROC_DDL:
            cur.execute(proc_ddl)
        conn.commit()
    finally:
        conn.close()

    _menu_schema_ready = True


# ---- Create Field (columns) -----------------------------------------------

def get_sheet_columns():
    """{sheet: [column, ...]} of active columns in display order.
    Sample: get_sheet_columns()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetSheetColumns")
        out = {}
        for sheet, col in cur.fetchall():
            out.setdefault(sheet, []).append(col)
        return out
    finally:
        conn.close()


def save_sheet_columns(columns, user_id=None):
    """Bulk upsert {sheet: [columns]}; columns dropped from a sheet are
    soft-deleted. Returns the effective active columns.
    Sample: save_sheet_columns({'Purchase Header': ['InvoiceNo', 'Document Date']}, 7)"""
    ensure_menu_schema()
    rows = [
        (sheet, col, i)
        for sheet, cols in (columns or {}).items()
        for i, col in enumerate(cols or [])
        if col
    ]
    if not rows:
        return get_sheet_columns()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_SaveSheetColumns ?, ?", rows, user_id)
        conn.commit()
    finally:
        conn.close()
    return get_sheet_columns()


# ---- Field Mapping --------------------------------------------------------

def get_field_mapping():
    """{sheet: {column: json_field}} of active, non-blank mappings.
    Sample: get_field_mapping()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetFieldMapping")
        out = {}
        for sheet, col, field in cur.fetchall():
            out.setdefault(sheet, {})[col] = field
        return out
    finally:
        conn.close()


def save_field_mapping(mapping, user_id=None):
    """Bulk upsert {sheet: {column: json_field}}; blank values unmap
    (soft-delete) the column. Returns the effective active mapping.
    Sample: save_field_mapping({'Purchase Header': {'InvoiceNo': 'invoice_no'}}, 7)"""
    ensure_menu_schema()
    rows = [
        (sheet, col, (field or "").strip() or None)
        for sheet, cells in (mapping or {}).items()
        for col, field in (cells or {}).items()
        if col
    ]
    if not rows:
        return get_field_mapping()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_SaveFieldMapping ?, ?", rows, user_id)
        conn.commit()
    finally:
        conn.close()
    return get_field_mapping()


# ---- Template (header + normalized static values) -------------------------

def get_templates_data():
    """{key: {"PO_Number_Format": ..., sheet: {col: value}}} for active
    templates — the shape the Template menu and the processor expect.
    Sample: get_templates_data()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetTemplates")

        temps, id_to_key = {}, {}
        for tid, key, po in cur.fetchall():
            temps[key] = {"PO_Number_Format": po or ""}
            id_to_key[tid] = key

        if cur.nextset():
            for tid, sheet, col, val in cur.fetchall():
                key = id_to_key.get(tid)
                if key is not None:
                    temps[key].setdefault(sheet, {})[col] = val if val is not None else ""
        return temps
    finally:
        conn.close()


def save_template(entity, name, template_key, po_format, static, user_id=None, invoice_type=None, old_template_key=None):
    """Upsert one template header and MERGE its static values in bulk.
    `old_template_key`, when given and different from `template_key`, is a
    RENAME: the row is found by the OLD key and its Name/TemplateKey are
    updated in place (see usp_SaveTemplate's own comment) instead of this
    becoming a new template.
    Sample: save_template('SPR', 'Bosch', 'SPR\\PART\\Bosch', 'SPRPUR/2026/', {'Purchase Header': {'Location_Code': 'CHN'}}, 7, 'PART')"""
    ensure_menu_schema()
    rows = [
        (sheet, col, (val if val not in (None, "") else None))
        for sheet, vals in (static or {}).items()
        for col, val in (vals or {}).items()
        if col
    ]
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_SaveTemplate ?, ?, ?, ?, ?, ?, ?, ?",
            entity, name, template_key, (po_format or None), rows, user_id, invoice_type,
            old_template_key if old_template_key and old_template_key != template_key else None,
        )
        conn.commit()
    finally:
        conn.close()


def delete_template(template_key, user_id=None):
    """Soft-delete a template (header + static values). Returns True if a
    template was deactivated.
    Sample: delete_template('SPR\\Bosch', 7)"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_DeleteTemplate ?, ?", template_key, user_id)
        row = cur.fetchone()
        conn.commit()
        return bool(row and row[0])
    finally:
        conn.close()


def migrate_menu_json(base_dir):
    """One-time seed of the menu tables from the legacy JSON files, then
    delete the files. Safe to call repeatedly: MERGE is idempotent and a
    missing file is skipped.
    Sample: migrate_menu_json('D:/WorkSpace/Projects/Python/PIIPS/data')"""
    import json

    ensure_menu_schema()

    cpath = os.path.join(base_dir, "columns.json")
    if os.path.exists(cpath):
        with open(cpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            save_sheet_columns(data, None)
        os.remove(cpath)

    mpath = os.path.join(base_dir, "mapping.json")
    if os.path.exists(mpath):
        with open(mpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            save_field_mapping(data, None)
        os.remove(mpath)

    tpath = os.path.join(base_dir, "templates.json")
    if os.path.exists(tpath):
        with open(tpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        static_values = (
            data.get("Static_Values", {}) if isinstance(data, dict) else {}
        )
        for key, entry in (static_values or {}).items():
            entity, _, name = key.partition("\\")
            # Legacy keys never carried an invoice type; default to PART
            # (the only invoice type processed historically) and extend
            # the key to the new entity\invoice_type\name shape.
            key = f"{entity}\\PART\\{name}"
            po = (entry or {}).get("PO_Number_Format", "")
            static = {
                sheet: (entry or {}).get(sheet, {})
                for sheet in ("Purchase Header", "Purchase Line", "Reservation Entry")
            }
            save_template(entity, name, key, po, static, None, "PART")
        os.remove(tpath)


def migrate_template_invoice_type():
    """One-time backfill for templates saved before the InvoiceType column
    existed: default them to PART (the only invoice type processed so far),
    extend TemplateKey to entity\\PART\\name, and physically move their real
    Input/Output folders on disk to include the PART segment. Idempotent
    (only rows with InvoiceType IS NULL are touched) and best-effort: a
    problem moving one template's folder must not block the others or the
    caller.
    Sample: migrate_template_invoice_type()"""
    import shutil
    import config_store

    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Id, Entity, Name FROM dbo.tbl_Template "
            "WHERE InvoiceType IS NULL"
        )
        rows = cur.fetchall()
        if not rows:
            return

        folders = config_store.folders(create=False)
        bases = [folders[k] for k in ("input", "output") if folders.get(k)]

        for tid, entity, name in rows:
            parts = [p for p in (name or "").replace("\\", "/").split("/") if p]
            for base in bases:
                try:
                    old_folder = os.path.join(base, entity, *parts)
                    new_folder = os.path.join(base, entity, "PART", *parts)
                    if os.path.isdir(old_folder) and not os.path.exists(new_folder):
                        os.makedirs(os.path.dirname(new_folder), exist_ok=True)
                        shutil.move(old_folder, new_folder)
                except OSError:
                    import traceback
                    traceback.print_exc()

            new_key = f"{entity}\\PART\\{name}"
            try:
                cur.execute(
                    "UPDATE dbo.tbl_Template SET InvoiceType = 'PART', "
                    "TemplateKey = ? WHERE Id = ?",
                    new_key, tid,
                )
                conn.commit()
            except Exception:  # noqa: BLE001 - keep migrating the rest
                import traceback
                traceback.print_exc()
    finally:
        conn.close()


def init_status_table():
    """Create tbl_status if missing and seed the status values (idempotent).
    Sample: init_status_table()"""
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.tables WHERE name = 'tbl_status'
            )
            CREATE TABLE tbl_status (
                StatusId   INT IDENTITY(1,1) PRIMARY KEY,
                StatusName NVARCHAR(150) NOT NULL UNIQUE
            )
            """
        )
        conn.commit()

        for name in STATUS_VALUES:
            cur.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tbl_status WHERE StatusName = ?)
                    INSERT INTO tbl_status (StatusName) VALUES (?)
                """,
                name, name,
            )
        conn.commit()

        # DisplayOrder decouples the Dashboard's "Status breakdown" bar
        # order (and anywhere else that wants a logical, not historical,
        # order) from StatusId - StatusId is an IDENTITY, permanently
        # fixed to whenever a status was first seeded on THIS database, so
        # a status added later can never sort between two earlier ones by
        # StatusId alone, no matter where it sits in STATUS_VALUES.
        # Re-synced from STATUS_VALUES' own order on every startup, so
        # re-ordering that Python list is always enough going forward.
        cur.execute(
            "IF COL_LENGTH('dbo.tbl_status', 'DisplayOrder') IS NULL "
            "ALTER TABLE dbo.tbl_status ADD DisplayOrder INT NULL"
        )
        conn.commit()
        for i, name in enumerate(STATUS_VALUES):
            cur.execute(
                "UPDATE tbl_status SET DisplayOrder = ? WHERE StatusName = ?",
                i, name,
            )
        conn.commit()

        cur.execute("SELECT StatusId, StatusName FROM tbl_status ORDER BY DisplayOrder")
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def init_usertype_table():
    """Create tbl_UserType if missing and seed the user types (idempotent).
    Sample: init_usertype_table()"""
    conn = get_connection()
    try:
        cur = conn.cursor()

        cur.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.tables WHERE name = 'tbl_UserType'
            )
            CREATE TABLE tbl_UserType (
                UserTypeId   INT IDENTITY(1,1) PRIMARY KEY,
                UserTypeName NVARCHAR(100) NOT NULL UNIQUE
            )
            """
        )
        conn.commit()

        for name in USER_TYPE_VALUES:
            cur.execute(
                """
                IF NOT EXISTS (SELECT 1 FROM tbl_UserType WHERE UserTypeName = ?)
                    INSERT INTO tbl_UserType (UserTypeName) VALUES (?)
                """,
                name, name,
            )
        conn.commit()

        cur.execute("SELECT UserTypeId, UserTypeName FROM tbl_UserType ORDER BY UserTypeId")
        return [(r[0], r[1]) for r in cur.fetchall()]
    finally:
        conn.close()


def init_user_table():
    """Create tbl_User if missing and add the audit / status columns.
    Sample: init_user_table()"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.tables WHERE name = 'tbl_user'
            )
            CREATE TABLE tbl_User (
                UserId     INT IDENTITY(1,1) PRIMARY KEY,
                UserName   NVARCHAR(100) NOT NULL UNIQUE,
                UserTypeID INT NOT NULL
                    CONSTRAINT FK_tbl_user_UserType
                    REFERENCES tbl_UserType(UserTypeId),
                Password   NVARCHAR(256) NOT NULL
            )
            """
        )
        conn.commit()

        cur.execute("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('tbl_user')")
        existing = {r[0] for r in cur.fetchall()}

        adds = {
            "IsActive": "BIT NOT NULL DEFAULT 1",
            "CreatedById": "INT NULL",
            "CreatedDatetime": "DATETIME NULL DEFAULT GETDATE()",
            "LastModifiedById": "INT NULL",
            "LastModifiedDatetime": "DATETIME NULL",
            "Email": "NVARCHAR(200) NULL",
            "MustChangePassword": "BIT NOT NULL DEFAULT 0",
        }
        for col, ddl in adds.items():
            if col not in existing:
                cur.execute(f"ALTER TABLE tbl_user ADD {_q(col)} {ddl}")
        conn.commit()

        cur.execute("SELECT name FROM sys.columns WHERE object_id = OBJECT_ID('tbl_user')")
        return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


def init_mail_settings_table():
    """Create tbl_MailSettings if missing and seed the one SMTP settings
    row (idempotent - never overwrites an existing row, only inserts when
    the table is empty). Sample: init_mail_settings_table()"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.tables WHERE name = 'tbl_MailSettings'
            )
            CREATE TABLE tbl_MailSettings (
                SettingID INT IDENTITY(31,1) PRIMARY KEY,
                UserName  NVARCHAR(100) NULL,
                EmailID   NVARCHAR(200) NULL,
                Password  NVARCHAR(500) NULL,
                SMTPHost  NVARCHAR(200) NULL,
                SMTPPort  INT NULL
            )
            """
        )
        conn.commit()

        cur.execute("SELECT COUNT(*) FROM tbl_MailSettings")
        if cur.fetchone()[0] == 0:
            cur.execute(
                "INSERT INTO tbl_MailSettings (UserName, EmailID, Password, SMTPHost, SMTPPort) "
                "VALUES (?, ?, ?, ?, ?)",
                "SF APP", "sfapp@precisionit.co.in", secret_store.protect(""),
                "mail.precisionit.co.in", 587,
            )
            conn.commit()
    finally:
        conn.close()


# The one account guaranteed to always exist, with a fixed (not auto-
# generated) password and no email/forced-change requirement, so it can
# never be locked out by mail-server misconfiguration. Any number of other
# Super Admins may also exist (created normally, with email + a generated
# password like everyone else) - this is just the always-available default.
DEFAULT_SUPER_ADMIN_USERNAME = "Sadmin"
DEFAULT_SUPER_ADMIN_PASSWORD = "Sadmin@2026"


def ensure_default_super_admin():
    """Seed the default 'Sadmin' account if it doesn't exist yet. Idempotent.
    Sample: ensure_default_super_admin()"""
    init_user_table()
    init_usertype_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tbl_user WHERE UserName = ?", DEFAULT_SUPER_ADMIN_USERNAME)
        if cur.fetchone():
            return
        cur.execute("SELECT UserTypeId FROM tbl_UserType WHERE UserTypeName = 'Super Admin'")
        row = cur.fetchone()
        if not row:
            return
        cur.execute(
            "INSERT INTO tbl_user (UserName, UserTypeID, Password, Email, MustChangePassword, IsActive, CreatedDatetime) "
            "VALUES (?, ?, ?, NULL, 0, 1, GETDATE())",
            DEFAULT_SUPER_ADMIN_USERNAME, row[0], hash_password(DEFAULT_SUPER_ADMIN_PASSWORD),
        )
        conn.commit()
    finally:
        conn.close()


# The default Viewer account, seeded the same always-available way as
# Sadmin above - so it exists on every environment (Local/UAT/Live each
# have their own separate database; nothing about a user account travels
# with a Publish, which only ever copies code) without a Super Admin
# having to manually recreate it on each one.
DEFAULT_VIEWER_USERNAME = "PBV0030"
DEFAULT_VIEWER_PASSWORD = "PIIPS@2026"


def ensure_default_viewer():
    """Seed the default 'PBV0030' Viewer account if it doesn't exist yet.
    Idempotent. Sample: ensure_default_viewer()"""
    init_user_table()
    init_usertype_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("SELECT 1 FROM tbl_user WHERE UserName = ?", DEFAULT_VIEWER_USERNAME)
        if cur.fetchone():
            return
        cur.execute("SELECT UserTypeId FROM tbl_UserType WHERE UserTypeName = 'Viewer'")
        row = cur.fetchone()
        if not row:
            return
        cur.execute(
            "INSERT INTO tbl_user (UserName, UserTypeID, Password, Email, MustChangePassword, IsActive, CreatedDatetime) "
            "VALUES (?, ?, ?, NULL, 0, 1, GETDATE())",
            DEFAULT_VIEWER_USERNAME, row[0], hash_password(DEFAULT_VIEWER_PASSWORD),
        )
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Announcements (Super Admin only) - site-wide notices shown to every
# logged-in user until EndDateTime passes or a Super Admin stops one early.
# ---------------------------------------------------------------------------

def init_announcement_table():
    """Create tbl_Announcement if missing. Sample: init_announcement_table()"""
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            """
            IF NOT EXISTS (
                SELECT 1 FROM sys.tables WHERE name = 'tbl_Announcement'
            )
            CREATE TABLE tbl_Announcement (
                Id              INT IDENTITY(1,1) PRIMARY KEY,
                Title           NVARCHAR(200) NOT NULL,
                BodyText        NVARCHAR(MAX) NULL,
                ImagePath       NVARCHAR(500) NULL,
                VideoUrl        NVARCHAR(500) NULL,
                EndDateTime     DATETIME NOT NULL,
                IsActive        BIT NOT NULL DEFAULT 1,
                CreatedById     INT NULL,
                CreatedDatetime DATETIME NOT NULL DEFAULT GETDATE(),
                StoppedById     INT NULL,
                StoppedDatetime DATETIME NULL
            )
            """
        )
        conn.commit()
    finally:
        conn.close()


def create_announcement(title, body_text, image_path, video_url, end_datetime, created_by=None):
    """Create an announcement. Sample:
    create_announcement('Maintenance', 'Down 10-11 PM', None, None, '2026-08-20 22:00:00', 11)"""
    init_announcement_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO tbl_Announcement "
            "(Title, BodyText, ImagePath, VideoUrl, EndDateTime, IsActive, CreatedById, CreatedDatetime) "
            "OUTPUT INSERTED.Id "
            "VALUES (?, ?, ?, ?, ?, 1, ?, GETDATE())",
            title, body_text, image_path, video_url, end_datetime, created_by,
        )
        new_id = cur.fetchone()[0]
        conn.commit()
        return new_id
    finally:
        conn.close()


def list_announcements():
    """Every announcement, newest first (Super Admin management view).
    Sample: list_announcements()"""
    init_announcement_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Id, Title, BodyText, ImagePath, VideoUrl, EndDateTime, IsActive, "
            "CreatedById, CreatedDatetime, StoppedById, StoppedDatetime "
            "FROM tbl_Announcement ORDER BY Id DESC"
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["IsActive"] = bool(d["IsActive"])
            for k in ("EndDateTime", "CreatedDatetime", "StoppedDatetime"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            out.append(d)
        return out
    finally:
        conn.close()


def get_active_announcements():
    """Announcements every logged-in user should currently see: IsActive
    and not yet past EndDateTime. Sample: get_active_announcements()"""
    init_announcement_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT Id, Title, BodyText, ImagePath, VideoUrl, EndDateTime "
            "FROM tbl_Announcement WHERE IsActive = 1 AND EndDateTime > GETDATE() "
            "ORDER BY Id DESC"
        )
        cols = [d[0] for d in cur.description]
        out = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["EndDateTime"] = str(d["EndDateTime"])
            out.append(d)
        return out
    finally:
        conn.close()


def stop_announcement(announcement_id, user_id=None):
    """Deactivate an announcement immediately (Super Admin). Sample:
    stop_announcement(3, 11)"""
    init_announcement_table()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tbl_Announcement SET IsActive = 0, StoppedById = ?, StoppedDatetime = GETDATE() "
            "WHERE Id = ?",
            user_id, announcement_id,
        )
        conn.commit()
        return cur.rowcount
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Password hashing (stdlib PBKDF2-SHA256)
# ---------------------------------------------------------------------------

def hash_password(password):
    """Hash a plaintext password with PBKDF2-SHA256 for storage. Sample: hash_password('S3cret!')"""
    salt = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 100_000)
    return "pbkdf2_sha256$100000$" + binascii.hexlify(salt).decode() + "$" + binascii.hexlify(dk).decode()


def verify_password(password, stored):
    """Check a plaintext password against a stored PBKDF2 hash. Sample: verify_password('S3cret!', stored_hash)"""
    try:
        _algo, iters, salt_hex, hash_hex = stored.split("$")
        salt = binascii.unhexlify(salt_hex)
        dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(iters))
        return binascii.hexlify(dk).decode() == hash_hex
    except (ValueError, AttributeError):
        return False


_SPECIAL_CHARS = "!@#$%^&*()-_=+[]{};:,.<>?/"
_PASSWORD_MIN_LEN = 5


def validate_password_policy(password):
    """Raise ValueError with a specific message unless `password` has at
    least one uppercase, one lowercase, one digit, one special character,
    and is at least 5 characters long. Sample: validate_password_policy('Ab1!x')"""
    password = password or ""
    if len(password) < _PASSWORD_MIN_LEN:
        raise ValueError(f"Password must be at least {_PASSWORD_MIN_LEN} characters long.")
    if not any(c.isupper() for c in password):
        raise ValueError("Password must contain at least one uppercase letter.")
    if not any(c.islower() for c in password):
        raise ValueError("Password must contain at least one lowercase letter.")
    if not any(c.isdigit() for c in password):
        raise ValueError("Password must contain at least one number.")
    if not any(c in _SPECIAL_CHARS for c in password):
        raise ValueError("Password must contain at least one special character.")


def generate_temp_password(length=10):
    """A random password that satisfies validate_password_policy by
    construction. Sample: generate_temp_password()"""
    length = max(length, _PASSWORD_MIN_LEN)
    required = [
        secrets.choice(string.ascii_uppercase),
        secrets.choice(string.ascii_lowercase),
        secrets.choice(string.digits),
        secrets.choice(_SPECIAL_CHARS),
    ]
    pool = string.ascii_letters + string.digits + _SPECIAL_CHARS
    required += [secrets.choice(pool) for _ in range(length - len(required))]
    secrets.SystemRandom().shuffle(required)
    return "".join(required)


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

def list_user_types():
    """List the available user types as [{"id", "name"}]. Sample: list_user_types()"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_ListUserTypes")
        return [{"id": r[0], "name": r[1]} for r in cur.fetchall()]
    finally:
        conn.close()


def list_users():
    """List all users with their type, status and audit fields. Sample: list_users()"""
    init_user_table()
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_ListUsers")
        cols = [d[0] for d in cur.description]
        users = []
        for r in cur.fetchall():
            d = dict(zip(cols, r))
            d["IsActive"] = bool(d.get("IsActive"))
            for k in ("CreatedDatetime", "LastModifiedDatetime"):
                if d.get(k) is not None:
                    d[k] = str(d[k])
            users.append(d)
        return users
    finally:
        conn.close()


def create_user(username, user_type_id, email, created_by=None, password=None):
    """Create a user (raises if the name exists). Normally a system-
    generated temporary password is used and returned so the caller can
    email it. `password`: an admin-assigned password instead of an
    auto-generated one - used for a Viewer account, which is set up
    directly by a Super Admin rather than through the email-a-temp-
    password flow (see api_create_user). Whichever password was actually
    used is returned; it is not persisted anywhere in plaintext.
    Sample: create_user('jsmith', 1, 'jsmith@precisionit.co.in', 7)"""
    init_user_table()
    ensure_menu_schema()
    temp_password = password or generate_temp_password()
    # An admin-assigned password (Viewer accounts) is meant to be the
    # persistent credential the admin just chose, not a placeholder - don't
    # force a change on first login the way the emailed-temp-password flow
    # does.
    must_change = password is None
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_CreateUser ?, ?, ?, ?, ?, ?",
            username, user_type_id, hash_password(temp_password), email, created_by,
            1 if must_change else 0,
        )
        row = cur.fetchone()
        conn.commit()
        if row and row[0] == -1:
            raise ValueError(f"User '{username}' already exists.")
    finally:
        conn.close()
    return temp_password


def get_user(username):
    """Fetch one user (incl. password hash) by name, or None. Sample: get_user('jsmith')"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetUserByName ?", username)
        r = cur.fetchone()
        if not r:
            return None
        return {
            "UserId": r[0], "UserName": r[1], "UserTypeID": r[2],
            "UserTypeName": r[3], "Password": r[4], "IsActive": bool(r[5]),
            "Email": r[6], "MustChangePassword": bool(r[7]),
        }
    finally:
        conn.close()


def get_user_by_email(email):
    """Fetch one user by email, or None. Sample: get_user_by_email('jsmith@precisionit.co.in')"""
    if not email:
        return None
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetUserByEmail ?", email)
        r = cur.fetchone()
        if not r:
            return None
        return {
            "UserId": r[0], "UserName": r[1], "UserTypeID": r[2],
            "UserTypeName": r[3], "Password": r[4], "IsActive": bool(r[5]),
            "Email": r[6], "MustChangePassword": bool(r[7]),
        }
    finally:
        conn.close()


def get_user_by_id(user_id):
    """Fetch one user by id, or None. Sample: get_user_by_id(12)"""
    if not user_id:
        return None
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT u.UserId, u.UserName, u.UserTypeID, t.UserTypeName, "
            "u.Password, u.IsActive, u.Email, u.MustChangePassword "
            "FROM dbo.tbl_user u "
            "LEFT JOIN dbo.tbl_UserType t ON u.UserTypeID = t.UserTypeId "
            "WHERE u.UserId = ?",
            user_id,
        )
        r = cur.fetchone()
        if not r:
            return None
        return {
            "UserId": r[0], "UserName": r[1], "UserTypeID": r[2],
            "UserTypeName": r[3], "Password": r[4], "IsActive": bool(r[5]),
            "Email": r[6], "MustChangePassword": bool(r[7]),
        }
    finally:
        conn.close()


def get_user_role(user_id):
    """Return {"role": UserTypeName, "active": bool} for a user id, or None.
    Sample: get_user_role(12)"""
    if not user_id:
        return None
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetUserRole ?", user_id)
        r = cur.fetchone()
        if not r:
            return None
        return {"role": r[0], "active": bool(r[1])}
    finally:
        conn.close()


def get_username_and_role(user_id):
    """{"username", "role"} for a user id, or None. Used to stamp who
    downloaded a manual PDF. Sample: get_username_and_role(12)"""
    if not user_id:
        return None
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT u.UserName, ut.UserTypeName FROM dbo.tbl_User u "
            "LEFT JOIN dbo.tbl_UserType ut ON ut.UserTypeId = u.UserTypeID "
            "WHERE u.UserId = ?",
            user_id,
        )
        r = cur.fetchone()
        if not r:
            return None
        return {"username": r[0], "role": r[1] or ""}
    finally:
        conn.close()


def authenticate(username, password):
    """Return the user dict on success, or None on failure/inactive.
    Sample: authenticate('jsmith', 'S3cret!')"""
    u = get_user(username)
    if not u or not u["IsActive"]:
        return None
    if not verify_password(password, u["Password"] or ""):
        return None
    return {
        "user_id": u["UserId"],
        "username": u["UserName"],
        "user_type": u["UserTypeName"],
        "user_type_id": u["UserTypeID"],
        "must_change_password": u["MustChangePassword"],
    }


def reset_password(username, new_password, force_change=False, modified_by=None):
    """Set a new hashed password for a user, validated against the password
    policy. `force_change=True` also flags the account so the user must set
    their own password on next login (used by user creation and the
    forgot-password flow; a normal self-service change passes False to
    clear the flag). Sample: reset_password('jsmith', 'N3wPass!1', True, 7)"""
    validate_password_policy(new_password)
    init_user_table()
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_ResetPassword ?, ?, ?, ?",
            username, hash_password(new_password), 1 if force_change else 0, modified_by,
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


def set_user_active(user_id, is_active, modified_by=None):
    """Activate or deactivate (soft-delete) a user. Sample: set_user_active(12, False, 7)"""
    init_user_table()
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_SetUserActive ?, ?, ?",
            user_id, 1 if is_active else 0, modified_by,
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


def set_user_type(user_id, user_type_id, modified_by=None):
    """Change an existing user's role (see app.py's /api/users/change-type
    for who's allowed to call this on whom). Sample: set_user_type(12, 1, 7)"""
    init_user_table()
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_SetUserType ?, ?, ?",
            user_id, user_type_id, modified_by,
        )
        row = cur.fetchone()
        conn.commit()
        return row[0] if row else 0
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Mail server settings
# ---------------------------------------------------------------------------

def get_mail_settings():
    """The single SMTP settings row, with Password decrypted, or None if
    not yet initialized. Sample: get_mail_settings()"""
    init_mail_settings_table()
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetMailSettings")
        r = cur.fetchone()
        if not r:
            return None
        return {
            "SettingID": r[0], "UserName": r[1], "EmailID": r[2],
            "Password": secret_store.unprotect(r[3] or ""),
            "SMTPHost": r[4], "SMTPPort": r[5],
        }
    finally:
        conn.close()


def save_mail_settings(username, email, password, smtp_host, smtp_port):
    """Update the single SMTP settings row. `password=None` keeps the
    current (encrypted) password unchanged. Sample:
    save_mail_settings('SF APP', 'sfapp@precisionit.co.in', 'S3cret!', 'mail.precisionit.co.in', 587)"""
    init_mail_settings_table()
    ensure_menu_schema()
    current = get_mail_settings()
    if password is None:
        password_enc = secret_store.protect(current["Password"] if current else "")
    else:
        password_enc = secret_store.protect(password)
    setting_id = current["SettingID"] if current else 31
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_SaveMailSettings ?, ?, ?, ?, ?, ?",
            setting_id, username, email, password_enc, smtp_host, smtp_port,
        )
        conn.commit()
    finally:
        conn.close()


if __name__ == "__main__":
    for sid, sname in init_status_table():
        print(f"{sid:>3}  {sname}")
