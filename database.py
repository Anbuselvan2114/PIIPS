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


# Ordered status values for tbl_status.
STATUS_VALUES = [
    "INITIATED",
    "UNSUPPORTED",
    "DUPLICATE",
    "EXTRACTED",
    "BUYER ORDER NO DOESN'T EXIST",
    "SF PROCESSED",
    "PENDING IN SF",
    "DATA MISMATCH",      # renamed from "INCOMPLETE DATA" - see migration below
    "NEW TEMPLATE",       # unrecognized-format PDFs, previously untracked entirely
    "READY TO LOAD",
    "LOADED",
    "POSTED",
    "COMPLETED",
    "EXCLUDED",
]

# User type values for tbl_UserType. "Accounts" runs the Load/Post/Complete
# lifecycle (see ROLE_MENUS in the frontend App.jsx).
USER_TYPE_VALUES = ["User", "Admin", "Super Admin", "Accounts"]

# Invoice type values for tbl_InvoiceType (formerly the free-text "GRN" /
# "NON-GRN" strings baked into template/folder paths — PART = GRN, SERVICE =
# NON-GRN).
INVOICE_TYPE_VALUES = ["PART", "SERVICE"]

# VALUES(...) list for seeding tbl_status (single quotes escaped).
_STATUS_SEED_VALUES = ", ".join(
    "('" + v.replace("'", "''") + "')" for v in STATUS_VALUES
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
    deliberately saved blank, not the auto-generated Document No. — a
    download's renumbering (excel_export.renumber_batch) must never end up
    disagreeing with what's actually stored. The proposed number goes into
    Last_Updated_No instead (self-healing column, see usp_SaveInvoiceBatch);
    it becomes real/permanent only once the invoice is marked Loaded (see
    advance_status). This mutates `data`'s groups in place — safe here since
    the caller (processor.py's _save_to_db) builds `data` fresh for this
    call and doesn't read it again afterward; in particular the mandatory-
    field completeness check (ready vs incomplete) already ran on the real
    numbers *before* this function is called, so blanking them now doesn't
    affect that classification — only the DB write.
    """
    for g in data.get("groups", []):
        header = g.get("header") or {}
        original_no = (header.get("No.") or "").strip()
        if original_no:
            header["Last_Updated_No"] = original_no
        header["No."] = ""
        for line in g.get("lines") or []:
            line["Document No."] = ""
        for res in g.get("reservations") or []:
            res["Source ID"] = ""

    cols = data["columns"]
    ph_cols = list(cols["Purchase Header"])
    pl_cols = list(cols["Purchase Line"])
    re_cols = list(cols["Reservation Entry"])
    header_cols = ph_cols + ["InvoiceNo", "Last_Updated_No", "PO_Number_Format"]   # BatchName now lives on the tracker

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
        header["Last_Updated_No"] = g["header"].get("Last_Updated_No")
        header["PO_Number_Format"] = g.get("po_number_format")
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


def resync_pending():
    """
    Re-attempt Service First for records still "pending in SF" (status 5).
    Reloads each record's extracted JSON, re-calls the API, and — when the
    part is now available — rebuilds that header's reservation rows and
    promotes the tracker (Ready to Load / sf processed) with a fresh
    SyncedDatetime. Returns {"promoted": n, "errors": [reason, ...]}.

    Sample: resync_pending()
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
                "EXEC dbo.usp_ReplaceReservation ?, ?, ?, ?, ?, ?",
                header_id, json.dumps(re_cols), json.dumps(group["reservations"]),
                verdict["status"], 1 if verdict["is_active"] else 0, 1,
            )
            conn.commit()
        finally:
            conn.close()

        promoted += 1
        errors.extend(verdict["errors"])

    return {"promoted": promoted, "errors": errors}


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
    {invoice_no: earliest_batch} for every invoice already saved in
    tbl_Purchase_Header (any batch). Used to skip re-processing an invoice
    that is already in the database. Backfills InvoiceNo from the mapped
    'Vendor Invoice No.' column for rows saved before InvoiceNo existed.

    Sample: get_processed_invoices()
    """
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute("EXEC dbo.usp_GetProcessedInvoices")
        return {r[0]: r[1] for r in cur.fetchall()}
    finally:
        conn.close()


def list_batches():
    """Batches (newest first), each with a per-status count map keyed by status
    name (one entry per StatusID that occurs in the batch's tracker rows).
    Sample: list_batches()
    Returns [{batch, created, headers, exportable, counts: {status: n, ...}}]."""
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

        return [by_batch[b] for b in order]
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


def fetch_batch(batch_name, sheet_cols):
    """
    Read a batch's rows from the 3 tables into {sheet: {columns, rows}}
    ready for excel_export.build_workbook_from_sheets. `sheet_cols` is the
    effective {sheet: [columns]} mapping. usp_FetchBatch returns three
    result sets (header, line, reservation), each with only the columns that
    actually exist on the table — restricted to headers not yet advanced
    past READY TO LOAD (see usp_FetchBatch's @idset).

    [No.] / [Document No.] / [Source ID] are deliberately blank until the
    invoice is marked Loaded (see advance_status) — a download must show
    the working number instead, so this falls back to the header's
    Last_Updated_No (propagated onto its lines/reservations) whenever
    those columns are blank. Each header row also carries an internal "Id"
    key (not part of the declared column list, so it's invisible to the
    exported Excel) so the caller can write back Last_Updated_No after a
    renumbered download — see update_last_updated_no().

    [Entry No.] on Reservation Entry follows the same pattern, but per-row
    (it isn't shared across a header's rows the way No. is): each
    reservation row falls back to its own Last_Updated_Entry_No when
    [Entry No.] is blank, and carries an internal "Id" for the write-back —
    see update_last_updated_entry_no().

    Sample: fetch_batch('PIIPS_Batch_20260722_101500', {'Purchase Header': ['InvoiceNo'], 'Purchase Line': ['Description'], 'Reservation Entry': ['Serial No.']})
    """
    ensure_menu_schema()

    header_cols = list(sheet_cols.get("Purchase Header", []))
    line_cols = list(sheet_cols.get("Purchase Line", []))
    res_cols = list(sheet_cols.get("Reservation Entry", []))

    # Internal-only columns needed for the Last_Updated_No fallback/write-
    # back below — usp_FetchBatch includes whatever's requested that
    # actually exists as a real column, so this piggybacks on the same
    # dynamic-column mechanism without any SQL changes.
    header_req = header_cols + ["Id", "No.", "Last_Updated_No", "PO_Number_Format"]
    line_req = line_cols + ["Purchase_Header_ID", "Document No."]
    res_req = res_cols + ["Id", "Purchase_Header_ID", "Source ID", "Entry No.", "Last_Updated_Entry_No"]

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

        last_no_by_header = {}
        for row in ph_r:
            effective = (row.get("No.") or "").strip() or (row.get("Last_Updated_No") or "").strip()
            row["No."] = effective
            if row.get("Id") is not None:
                last_no_by_header[row["Id"]] = effective
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
            if not (row.get("Document No.") or "").strip():
                row["Document No."] = last_no_by_header.get(row.get("Purchase_Header_ID"), "")
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
                    row["GST Group Code"] = f"Service {rate:g}%" if rate else "Service"

        for row in re_r:
            if not (row.get("Source ID") or "").strip():
                row["Source ID"] = last_no_by_header.get(row.get("Purchase_Header_ID"), "")
            if not (row.get("Entry No.") or "").strip():
                row["Entry No."] = (row.get("Last_Updated_Entry_No") or "").strip()
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


def update_last_updated_no(id_no_pairs):
    """Bulk-update tbl_Purchase_Header.Last_Updated_No — called after a
    batch download so a custom Document No./renumbering (excel_export.
    renumber_batch) stays in sync with what will be finalized into [No.]
    once the invoice is marked Loaded. `id_no_pairs`: [(header_id, no), ...].
    Sample: update_last_updated_no([(42, 'PO-2627-00002')])"""
    pairs = [(int(i), n) for i, n in (id_no_pairs or []) if i is not None]
    if not pairs:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE dbo.tbl_Purchase_Header SET Last_Updated_No = ? WHERE Id = ?",
            [(n, i) for i, n in pairs],
        )
        conn.commit()
    finally:
        conn.close()


def update_last_updated_entry_no(id_no_pairs):
    """Bulk-update tbl_Reservation_Entry.Last_Updated_Entry_No — the
    Reservation Entry row-level counterpart to update_last_updated_no(),
    called after a batch download so a renumbered [Entry No.] stays in
    sync with what will be finalized once the invoice is marked Loaded.
    `id_no_pairs`: [(reservation_row_id, entry_no), ...].
    Sample: update_last_updated_entry_no([(7, '1001')])"""
    pairs = [(int(i), n) for i, n in (id_no_pairs or []) if i is not None]
    if not pairs:
        return
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.executemany(
            "UPDATE dbo.tbl_Reservation_Entry SET Last_Updated_Entry_No = ? WHERE Id = ?",
            [(n, i) for i, n in pairs],
        )
        conn.commit()
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
        sql = (
            "SELECT h.Id, h.InvoiceNo, pt.BatchName, pt.FileName, pt.TemplateFormat, "
            "s.StatusName, pt.IsActive, pt.IsSynced, ISNULL(pt.IsExcluded, 0), "
            f"{vendor}, {gst}, {ddate}, pt.Id, pt.SourceJson "
            "FROM dbo.tbl_Purchase_Tracker pt "
            "JOIN dbo.tbl_Purchase_Header h ON h.Id = pt.Purchase_Header_ID "
            "LEFT JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
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
                rows.append({
                    "field": n, "value": v, "missing": not v.strip(),
                    "source": source,
                })
            return rows

        header_wanted = excel_export.REQUIRED_HEADER_FIELDS + ["InvoiceNo"]
        header_cols = _existing_cols(cur, "tbl_Purchase_Header", header_wanted)
        header_values = {}
        if header_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in header_cols)} "
                "FROM dbo.tbl_Purchase_Header WHERE Id = ?",
                header_id,
            )
            row = cur.fetchone()
            if row:
                header_values = dict(zip(header_cols, row))

        # "No." and "HSN/SAC Code" are only mandatory conditionally (on the
        # line's own Type — see excel_export.required_fields_for_line_type),
        # so always fetch both and pick the right one per line below.
        line_wanted = excel_export.REQUIRED_LINE_FIELDS + ["No.", "HSN/SAC Code"]
        line_cols = _existing_cols(cur, "tbl_Purchase_Line", line_wanted)
        line_rows = []
        if line_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in line_cols)} "
                "FROM dbo.tbl_Purchase_Line WHERE Purchase_Header_ID = ?",
                header_id,
            )
            line_rows = [dict(zip(line_cols, r)) for r in cur.fetchall()]

        res_cols = _existing_cols(cur, "tbl_Reservation_Entry", excel_export.REQUIRED_RESERVATION_FIELDS)
        res_rows = []
        if res_cols:
            cur.execute(
                f"SELECT {', '.join(_q(c) for c in res_cols)} "
                "FROM dbo.tbl_Reservation_Entry WHERE Purchase_Header_ID = ?",
                header_id,
            )
            res_rows = [dict(zip(res_cols, r)) for r in cur.fetchall()]
            # Same forced recompute as fetch_batch() - a row saved before
            # the Source Subtype fix still has the old "Order" text stored.
            for rr in res_rows:
                if "Source Subtype" in rr:
                    rr["Source Subtype"] = "1"

        return {
            "header": field_rows("Purchase Header", header_wanted, header_values),
            "lines": [
                {"label": ln.get("Description", "") or f"Line {i + 1}",
                 "fields": field_rows(
                     "Purchase Line",
                     excel_export.required_fields_for_line_type(ln.get("Type")), ln)}
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
    Returns {'count': <rows changed>, 'files': [<FileName>, ...]} so the caller
    can relocate each PDF into its new status folder.

    Advancing to LOADED also finalizes [No.] (tbl_Purchase_Header) /
    [Document No.] (tbl_Purchase_Line) / [Source ID] (tbl_Reservation_Entry)
    from Last_Updated_No — see fetch_batch/update_last_updated_no — the
    working Document No. only becomes real/permanent once an invoice is
    actually loaded. Only blank values are touched, so this never
    overwrites an already-finalized number (e.g. a re-run)."""
    ids = [int(h) for h in (header_ids or [])]
    froms = [s for s in (from_statuses or []) if s]
    if not ids or not froms or not to_status:
        return {"count": 0, "files": []}

    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        id_ph = ", ".join("?" for _ in ids)
        from_ph = ", ".join("?" for _ in froms)

        # Files + header ids of the rows that actually qualify (for the
        # folder move and the Load finalize step below — captured before
        # the UPDATE changes their status out from under a later lookup).
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

        cur.execute(
            "UPDATE pt "
            "SET pt.StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = ?), "
            "    pt.LastModifiedById = ?, pt.LastModifiedDatetime = GETDATE() "
            "FROM dbo.tbl_Purchase_Tracker pt "
            "JOIN dbo.tbl_status s ON s.StatusId = pt.StatusID "
            f"WHERE pt.Purchase_Header_ID IN ({id_ph}) AND s.StatusName IN ({from_ph})",
            to_status, user_id, *ids, *froms)
        count = cur.rowcount if cur.rowcount is not None else len(files)

        if to_status == "LOADED" and qualifying_ids and _existing_cols(cur, "tbl_Purchase_Header", ["Last_Updated_No"]):
            qid_ph = ", ".join("?" for _ in qualifying_ids)
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Header "
                "SET [No.] = Last_Updated_No "
                f"WHERE Id IN ({qid_ph}) AND (([No.] IS NULL) OR ([No.] = ''))",
                *qualifying_ids)
            if _existing_cols(cur, "tbl_Purchase_Line", ["Document No."]):
                cur.execute(
                    "UPDATE l SET l.[Document No.] = h.Last_Updated_No "
                    "FROM dbo.tbl_Purchase_Line l "
                    "JOIN dbo.tbl_Purchase_Header h ON h.Id = l.Purchase_Header_ID "
                    f"WHERE l.Purchase_Header_ID IN ({qid_ph}) "
                    "AND (l.[Document No.] IS NULL OR l.[Document No.] = '')",
                    *qualifying_ids)
            if _table_exists(cur, "tbl_Reservation_Entry") and _existing_cols(cur, "tbl_Reservation_Entry", ["Source ID"]):
                cur.execute(
                    "UPDATE r SET r.[Source ID] = h.Last_Updated_No "
                    "FROM dbo.tbl_Reservation_Entry r "
                    "JOIN dbo.tbl_Purchase_Header h ON h.Id = r.Purchase_Header_ID "
                    f"WHERE r.Purchase_Header_ID IN ({qid_ph}) "
                    "AND (r.[Source ID] IS NULL OR r.[Source ID] = '')",
                    *qualifying_ids)
            # [Entry No.] is per-row (not shared across a header's rows like
            # No./Source ID), so it finalizes from its own Last_Updated_Entry_No.
            if _table_exists(cur, "tbl_Reservation_Entry") and len(_existing_cols(
                cur, "tbl_Reservation_Entry", ["Entry No.", "Last_Updated_Entry_No"]
            )) == 2:
                cur.execute(
                    "UPDATE r SET r.[Entry No.] = r.Last_Updated_Entry_No "
                    "FROM dbo.tbl_Reservation_Entry r "
                    f"WHERE r.Purchase_Header_ID IN ({qid_ph}) "
                    "AND (r.[Entry No.] IS NULL OR r.[Entry No.] = '')",
                    *qualifying_ids)

        conn.commit()
        return {"count": count, "files": files, "header_ids": qualifying_ids}
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


def set_excluded(header_id, exclude, user_id=None):
    """Include / exclude one invoice. Excluding sets the tracker status to
    'Excluded' (remembering the prior status), flags IsExcluded and stamps
    ExcludedByID / ExcludedDatetime; including restores the prior status.
    Returns {file_name, new_status} so the caller can relocate the PDF.
    Sample: set_excluded(42, True, 7)"""
    ensure_menu_schema()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT StatusID, PriorStatusID, FileName FROM dbo.tbl_Purchase_Tracker "
            "WHERE Purchase_Header_ID = ?", header_id)
        row = cur.fetchone()
        if not row:
            return None
        file_name = row[2]

        if exclude:
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Tracker "
                "SET PriorStatusID = CASE WHEN ISNULL(IsExcluded,0)=1 THEN PriorStatusID ELSE StatusID END, "
                "    StatusID = (SELECT StatusId FROM dbo.tbl_status WHERE StatusName = 'EXCLUDED'), "
                "    IsExcluded = 1, ExcludedByID = ?, ExcludedDatetime = GETDATE(), "
                "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
                "WHERE Purchase_Header_ID = ?", user_id, user_id, header_id)
            new_status = "EXCLUDED"
        else:
            cur.execute(
                "UPDATE dbo.tbl_Purchase_Tracker "
                "SET StatusID = ISNULL(PriorStatusID, StatusID), IsExcluded = 0, "
                "    ExcludedByID = NULL, ExcludedDatetime = NULL, "
                "    LastModifiedById = ?, LastModifiedDatetime = GETDATE() "
                "WHERE Purchase_Header_ID = ?", user_id, header_id)
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
        @InvoiceType    NVARCHAR(20) = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_SaveTemplate @Entity='SPR', @Name='Bosch', @TemplateKey='SPR\PART\Bosch', @PONumberFormat='SPRPUR/2026/', @Static=@StaticVals, @UserId=7, @InvoiceType='PART'
        DECLARE @TemplateId INT;
        SELECT @TemplateId = Id FROM dbo.tbl_Template WHERE TemplateKey = @TemplateKey;

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
               SET Entity = @Entity, Name = @Name, PONumberFormat = @PONumberFormat,
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

        -- Working Document No. (see Load's finalize step below): the caller
        -- deliberately leaves [No.]/[Document No.]/[Source ID] blank at
        -- save time (a download's renumbering must not disagree with what
        -- got saved here) and sends the proposed number as Last_Updated_No
        -- instead, self-healing the column on first use like InvoiceNo.
        IF COL_LENGTH('dbo.tbl_Purchase_Header', 'Last_Updated_No') IS NULL
            ALTER TABLE dbo.tbl_Purchase_Header ADD Last_Updated_No NVARCHAR(200) NULL;

        -- The template's PO number prefix at the time this header was
        -- saved, kept separately from Last_Updated_No so a later Dashboard
        -- renumber can rebuild "prefix + new sequence" even when the
        -- invoice_no fallback used for Last_Updated_No doesn't look like
        -- "PREFIX000123" at all.
        IF COL_LENGTH('dbo.tbl_Purchase_Header', 'PO_Number_Format') IS NULL
            ALTER TABLE dbo.tbl_Purchase_Header ADD PO_Number_Format NVARCHAR(100) NULL;

        -- Same idea for Reservation Entry's [Entry No.] — a working value
        -- (Last_Updated_Entry_No) tracked per row, finalized into the
        -- real column only once the invoice is marked Loaded.
        IF COL_LENGTH('dbo.tbl_Reservation_Entry', 'Last_Updated_Entry_No') IS NULL
            ALTER TABLE dbo.tbl_Reservation_Entry ADD Last_Updated_Entry_No NVARCHAR(50) NULL;

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
        IF OBJECT_ID('dbo.tbl_Purchase_Tracker') IS NOT NULL
        BEGIN
            INSERT INTO dbo.tbl_Purchase_Tracker
                (Purchase_Header_ID, InitiatedByID, InitiatedDatetime,
                 StartedByID, StartedDatetime, BatchName, StatusID, IsActive,
                 SyncedByID, SyncedDatetime, IsSynced, FileName, TemplateFormat,
                 InvoiceTypeID, BuyerOrderNo, SourceJson,
                 CreatedById, CreatedDatetime)
            SELECT m.HeaderId, s._InitById, s._InitAt,
                   @StartedByID, @StartedDatetime, s._BatchName,
                   st.StatusId, ISNULL(s._IsActive, 1),
                   CASE WHEN ISNULL(s._Synced, 0) = 1 THEN 1 ELSE NULL END,
                   CASE WHEN ISNULL(s._Synced, 0) = 1 THEN GETDATE() ELSE NULL END,
                   ISNULL(s._Synced, 0),
                   s._FileName, s._Format,
                   it.InvoiceTypeId, s._BuyerOrderNo, s._SourceJson,
                   @StartedByID, GETDATE()
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
        @SyncedById INT
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_ReplaceReservation @HeaderId=42, @Cols='["Serial No.","Nav_Item_No"]', @Rows='[{"Serial No.":"SN123","Nav_Item_No":"ITM-1001"}]', @StatusName='READY TO LOAD', @IsActive=1, @SyncedById=1

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
               LastModifiedDatetime = GETDATE()
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
        SELECT s.StatusId, s.StatusName, COUNT(pt.Id) AS Cnt
        FROM dbo.tbl_status s
        LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.StatusID = s.StatusId
        GROUP BY s.StatusId, s.StatusName
        ORDER BY s.StatusId;
    END
    """,
    # ---- User Management -------------------------------------------------
    # Password hashing stays in Python (stdlib PBKDF2); the procedures only
    # persist the already-hashed value. Users are soft-deleted via IsActive
    # (usp_SetUserActive), never physically removed.
    """
    CREATE OR ALTER PROCEDURE dbo.usp_CreateUser
        @UserName     NVARCHAR(100),
        @UserTypeID   INT,
        @PasswordHash NVARCHAR(256),
        @Email        NVARCHAR(200) = NULL,
        @CreatedById  INT = NULL
    AS
    BEGIN
        SET NOCOUNT ON;
        -- Sample: EXEC dbo.usp_CreateUser @UserName='jsmith', @UserTypeID=1, @PasswordHash='pbkdf2_sha256$100000$abcd$ef01', @Email='jsmith@precisionit.co.in', @CreatedById=7
        IF EXISTS (SELECT 1 FROM dbo.tbl_user WHERE UserName = @UserName)
        BEGIN
            SELECT -1 AS Status;   -- already exists
            RETURN;
        END
        INSERT INTO dbo.tbl_user
            (UserName, UserTypeID, Password, Email, MustChangePassword, IsActive, CreatedById, CreatedDatetime)
        VALUES (@UserName, @UserTypeID, @PasswordHash, @Email, 1, 1, @CreatedById, GETDATE());
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
                   ROW_NUMBER() OVER (PARTITION BY l.RelPath
                                      ORDER BY l.InitiatedDatetime DESC, l.Id DESC) AS rn
            FROM dbo.tbl_InputFile_Log l
            JOIN wanted w ON w.RelPath = l.RelPath
        )
        -- StatusID = 0 means the file was reset (moved to New_Format) and may
        -- be uploaded again by anyone, so it is NOT treated as a duplicate.
        SELECT r.RelPath, r.FileName, r.InitiatedByID, u.UserName AS InitiatedByName,
               r.InitiatedDatetime
        FROM ranked r
        LEFT JOIN dbo.tbl_user u ON u.UserId = r.InitiatedByID
        WHERE r.rn = 1 AND ISNULL(r.StatusID, -1) <> 0;
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
        FROM dbo.tbl_SheetColumn
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
        FROM dbo.tbl_FieldMapping
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
        FROM dbo.tbl_Template
        WHERE IsActive = 1;

        SELECT sv.TemplateId, sv.SheetName, sv.ColumnName, sv.StaticValue
        FROM dbo.tbl_TemplateStaticValue sv
        JOIN dbo.tbl_Template t ON t.Id = sv.TemplateId AND t.IsActive = 1
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
        SELECT UserTypeId, UserTypeName FROM dbo.tbl_UserType ORDER BY UserTypeId;
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
        FROM dbo.tbl_user u
        LEFT JOIN dbo.tbl_UserType t ON u.UserTypeID = t.UserTypeId
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
        SELECT pt.BatchName, MIN(h.CreatedAt) AS created, COUNT(*) AS headers,
               SUM(CASE WHEN pt.IsActive = 1 AND ISNULL(pt.IsExcluded, 0) = 0
                        THEN 1 ELSE 0 END) AS exportable
        FROM dbo.tbl_Purchase_Tracker pt
        JOIN dbo.tbl_Purchase_Header h ON h.Id = pt.Purchase_Header_ID
        WHERE pt.BatchName IS NOT NULL
        GROUP BY pt.BatchName
        ORDER BY MIN(h.CreatedAt) DESC;

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
        SELECT h.InvoiceNo, MIN(pt.BatchName) AS BatchName
        FROM dbo.tbl_Purchase_Header h
        LEFT JOIN dbo.tbl_Purchase_Tracker pt ON pt.Purchase_Header_ID = h.Id
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


def save_template(entity, name, template_key, po_format, static, user_id=None, invoice_type=None):
    """Upsert one template header and MERGE its static values in bulk.
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
            "EXEC dbo.usp_SaveTemplate ?, ?, ?, ?, ?, ?, ?",
            entity, name, template_key, (po_format or None), rows, user_id, invoice_type,
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

        cur.execute("SELECT StatusId, StatusName FROM tbl_status ORDER BY StatusId")
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


def create_user(username, user_type_id, email, created_by=None):
    """Create a user with a system-generated temporary password (raises if
    the name exists). The admin never chooses a password - one is always
    auto-generated here and returned so the caller can email it; it is not
    persisted anywhere in plaintext. Sample: create_user('jsmith', 1, 'jsmith@precisionit.co.in', 7)"""
    init_user_table()
    ensure_menu_schema()
    temp_password = generate_temp_password()
    conn = get_connection()
    try:
        cur = conn.cursor()
        cur.execute(
            "EXEC dbo.usp_CreateUser ?, ?, ?, ?, ?",
            username, user_type_id, hash_password(temp_password), email, created_by,
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
