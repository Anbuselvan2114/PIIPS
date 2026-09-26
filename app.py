from typing import List, Literal, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File, Form, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel
import os
import re
import shutil
import threading
import traceback
import uuid
from datetime import datetime
from urllib.parse import quote

import config_store
import security
from processor import job_manager
from format_model import FormatModel

ACCEPTED_EXTS = (".pdf", ".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".webp")

# The interactive API docs / OpenAPI schema are switched off: they publish
# every route (including the admin ones) to anyone who can reach the site.
app = FastAPI(
    title="Precision Intelligent Invoice Processing Suite",
    version="2.3",
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)


# Explicit origin allow-list: the local Vite dev server, plus the real
# live (piips.precisionit.co.in:8010) and uat (10.0.1.210:8080)
# deployments. allow_credentials is deliberately False: nothing in
# api.js's fetch() calls ever sends credentials cross-origin (no
# `credentials: "include"` anywhere - auth here is just a user_id in the
# request body/query, not a cookie/session), so allow_origins=["*"] +
# allow_credentials=True was a spec-disallowed combination protecting
# nothing.
# Token authentication + role/menu authorisation for every /api route (see
# security.py). Added BEFORE CORS so CORS stays outermost and answers
# preflights / decorates 401-403 responses itself.
app.add_middleware(security.AuthMiddleware)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173", "http://localhost:3000",
        "https://piips.precisionit.co.in:8010",
        "http://10.0.1.210:8080",
    ],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


BASE_DIR = os.path.dirname(os.path.abspath(__file__))


@app.on_event("startup")
def _bootstrap_menu_storage():
    """Create the menu tables/types/procedures and migrate any legacy JSON
    (columns.json / mapping.json / templates.json) into them, once. Best
    effort: a DB that isn't configured/reachable never blocks startup."""
    import traceback
    try:
        import config_store
        # Decrypt any legacy DPAPI-protected connection string in
        # config.json back to plain text (db_connection is human-readable
        # now - see config_store.save_config).
        config_store.ensure_secret_plaintext()

        import database
        if (config_store.load_config().get("db_connection") or "").strip():
            database.migrate_menu_json(BASE_DIR)
            database.migrate_template_invoice_type()
            database.migrate_line_type_to_template()
            database.init_mail_settings_table()
            database.ensure_default_super_admin()
            database.ensure_default_viewer()
    except Exception:  # noqa: BLE001 - never block startup on DB issues
        traceback.print_exc()


@app.on_event("startup")
def _warm_extraction_pool():
    """Start the parallel-extraction worker processes in the background so
    the first Start click isn't slowed by their ~30 s OCR-engine load."""
    threading.Thread(target=job_manager.warm_pool, daemon=True).start()


@app.on_event("startup")
def _run_part_description_migration():
    """Kicks off _run_part_description_migration_impl() in the background
    (see its own docstring) - a startup event handler blocks the app from
    ever becoming ready to serve requests until every handler returns, and
    on a real live database this can mean re-OCRing a real number of PDFs
    (each easily 10-40s for a scanned one) - left synchronous, a slow host-
    level startup timeout could kill the process before it ever finishes,
    over and over, without ever reaching the point where it marks itself
    done (see _warm_extraction_pool just above for the same reasoning)."""
    threading.Thread(target=_run_part_description_migration_impl, daemon=True).start()


def _run_part_description_migration_impl():
    """One-time data-quality migration, NOT a general cleanup: older
    ocr_engine.py extraction bugs (now fixed - see EXCLUDE_KW's hsn/sac,
    way bill, net amount, bank name, beneficiary, account no, ifsc code,
    branch, continued, contd, computer generated invoice, rounded off
    entries) saved corrupted text straight into tbl_Purchase_Line's own
    [Description] for invoices processed before those fixes existed - which
    then surfaces as a wrong/junk suggestion on the Part Description
    Mapping screen (Part_Description_Update_Items -> PdfDescriptions).
    Runs ONCE ever, guarded by config.json's part_description_migration_done
    (set only after a run actually completes with a real, non-ambiguous
    signal - see the "inconclusive" comment below): re-OCRs, with today's
    fixed extraction, ONLY the PDFs currently backing a row this screen
    itself would show right now (never a blanket table scan, and never an
    invoice the screen doesn't currently flag), and overwrites ONLY the
    [Description] column of the specific tbl_Purchase_Line row(s) that
    PDF's own re-extraction positionally matches (see
    database.fix_purchase_line_descriptions) - no other column, table,
    invoice, or tracker status is touched. Best-effort per PDF: one
    failure is logged and skipped, never aborts the rest of the run or
    blocks startup."""
    import traceback
    try:
        cfg = config_store.load_config()
        if cfg.get("part_description_migration_done"):
            return
        if not (cfg.get("db_connection") or "").strip():
            return  # DB not configured yet - nothing to migrate

        import database

        order_nos = database.buyer_order_nos_for_status("DATA MISMATCH")
        if not order_nos:
            # Nothing at DATA MISMATCH at all - determined entirely from
            # our own DB, no dependency on Service First being reachable,
            # so this is a safe, unambiguous "done".
            config_store.save_config({"part_description_migration_done": True})
            return

        # The EXACT same screen logic (order_nos -> SF mismatch records ->
        # drop-if-already-resolved) - reused as-is (not reimplemented) so
        # the candidate set is always byte-for-byte what a user would
        # currently see on the Part Description Mapping screen.
        items = part_description_update_items().get("items") or []
        if not items:
            # Ambiguous: could genuinely mean "every PO is already
            # resolved", or Service First being unreachable right now
            # (get_specification_mismatch_records swallows its own errors
            # and returns [] either way). Don't mark done on a guess - a
            # transient SF outage at the one moment this fires must not
            # look like "nothing to fix" forever. Retried on next startup.
            return

        candidates = {}
        for item in items:
            po = item.get("PurchaseOrderNo")
            for inv in item.get("PdfInvoices") or []:
                fname = inv.get("FileName")
                if po and fname:
                    candidates[(po, fname)] = True

        from ocr_engine import OCREngine
        from invoice_schema import build_invoice_json

        ocr = OCREngine()
        # Part Description Mapping is PART-only by definition (see this
        # function's own docstring/scope), so the PART toggle is the
        # correct one here regardless of SERVICE's own setting.
        allow_scanned = bool(cfg.get("allow_scanned_pdfs_part"))
        fixed_lines = 0
        fixed_invoices = 0
        for po, fname in candidates:
            try:
                header_id = database.purchase_header_id_for_invoice(po, fname, "DATA MISMATCH")
                if not header_id:
                    continue
                path = config_store.find_pdf(fname)
                if not path:
                    continue
                ocr_result = ocr.read_pdf(path, allow_scanned=allow_scanned)
                invoice_groups = ocr_result.get("Invoices") or [ocr_result]
                data = build_invoice_json(invoice_groups[0], path)
                n = database.fix_purchase_line_descriptions(header_id, data.get("items") or [])
                if n:
                    fixed_lines += n
                    fixed_invoices += 1
            except Exception:  # noqa: BLE001 - one bad PDF must not stop the rest
                traceback.print_exc()
                continue

        config_store.save_config({"part_description_migration_done": True})
        print(
            f"[part-description-migration] Corrected {fixed_lines} Purchase "
            f"Line description(s) across {fixed_invoices} invoice(s) "
            f"({len(candidates)} candidate(s) examined). Will not run again."
        )
    except Exception:  # noqa: BLE001 - never block startup
        traceback.print_exc()


@app.on_event("startup")
def _run_line_description_continuation_fix_migration():
    """Kicks off _run_line_description_continuation_fix_migration_impl() in
    the background - same reasoning as _run_part_description_migration's own
    wrapper just above (a startup event handler blocks the app from ever
    becoming ready until every handler returns; this one re-OCRs a real
    number of PDFs and calls Service First per invoice on a real live
    database, easily long enough for a host-level startup timeout to kill
    the process before it ever finishes and marks itself done)."""
    threading.Thread(target=_run_line_description_continuation_fix_migration_impl, daemon=True).start()


def _run_line_description_continuation_fix_migration_impl():
    """One-time data-quality migration, NOT a general cleanup: a table
    layout where each item prints its serial + full values on one row with
    a plain spec line right below it (e.g. "1 MOTHERBOARD ... 9,800.00" /
    "HP 280 PRO G6 MICROTOWER PC RCTO") had that spec line wrongly
    reassigned to the NEXT item's Description instead of staying on the
    item it's printed under - fixed in ocr_engine.py, but every invoice
    processed before that fix already has the wrong text saved in
    tbl_Purchase_Line. Runs ONCE ever, guarded by config.json's
    line_description_continuation_fix_done: re-OCRs, with today's fixed
    extraction, every PDF currently at DATA MISMATCH or PENDING IN SF
    (database.data_mismatch_headers - never a blanket table scan; PENDING IN
    SF is included too since an invoice parked there for an unrelated reason
    - Service First just hasn't received the part yet - can still be
    carrying the same bad Description from before the fix existed), and for
    any whose freshly re-extracted Description(s) differ from what's stored
    AND whose item count is unchanged (a different item count means today's
    fix also changed WHICH rows exist, not just their text - too different
    to positionally match, skipped rather than guessed at), overwrites just
    those Description(s) (database.fix_purchase_line_descriptions - PDF-
    sourced data only) and re-validates the invoice (Service First for PART,
    the same mandatory-field gate a NAV vendor code correction uses for
    SERVICE), rebuilding only its Reservation Entry rows and status/IsActive
    (database.revalidate_header_after_description_fix - Service First-
    sourced data only) so it moves to whatever status is now actually
    correct - which may still be DATA MISMATCH or PENDING IN SF, just for a
    different, genuine reason. Never touches BatchName, [No.], or
    [Entry No.] - those stay exactly as already assigned. Best-effort per
    PDF: one failure is logged and skipped, never aborts the rest of the
    run or blocks startup."""
    import json
    import traceback
    try:
        cfg = config_store.load_config()
        if cfg.get("line_description_continuation_fix_done"):
            return
        if not (cfg.get("db_connection") or "").strip():
            return  # DB not configured yet - nothing to migrate

        import database
        import excel_export
        import service_api
        import template_store

        candidates = database.data_mismatch_headers(("DATA MISMATCH", "PENDING IN SF"))
        if not candidates:
            # Nothing at either status at all - determined entirely from
            # our own DB, a safe, unambiguous "done".
            config_store.save_config({"line_description_continuation_fix_done": True})
            return

        from ocr_engine import OCREngine
        from invoice_schema import build_invoice_json

        ocr = OCREngine()
        output_folder = (config_store.folders(create=False) or {}).get("output", "")
        fixed_lines = 0
        fixed_invoices = 0
        for c in candidates:
            header_id, fname, json_path = c["header_id"], c["file_name"], c["source_json"]
            try:
                if not json_path or not os.path.isfile(json_path):
                    continue
                path = config_store.find_pdf(fname)
                if not path:
                    continue
                allow_scanned = bool(cfg.get(
                    f"allow_scanned_pdfs_{c['invoice_type'].lower()}"
                    if c["invoice_type"] in ("PART", "SERVICE") else "allow_scanned_pdfs_part"
                ))
                ocr_result = ocr.read_pdf(path, allow_scanned=allow_scanned)
                invoice_groups = ocr_result.get("Invoices") or [ocr_result]
                fresh = build_invoice_json(invoice_groups[0], path)
                new_items = fresh.get("items") or []

                with open(json_path, "r", encoding="utf-8") as fp:
                    data = json.load(fp)
                old_items = data.get("items") or []
                if len(old_items) != len(new_items):
                    continue  # today's fix also changed the item count - skip, don't guess
                if all(
                    (o.get("Description") or "").strip() == (n.get("Description") or "").strip()
                    for o, n in zip(old_items, new_items)
                ):
                    continue  # nothing this migration is meant to touch changed

                data["items"] = new_items
                static, _ = template_store.static_for_path(output_folder, json_path)
                data["_static"] = static
                with open(json_path, "w", encoding="utf-8") as fp:
                    json.dump(data, fp, indent=4, ensure_ascii=False)

                if c["invoice_type"] == "SERVICE":
                    field_mapping = excel_export.load_mapping()
                    grouped = excel_export.build_rows_grouped([data])
                    group = grouped["groups"][0] if grouped["groups"] else {"header": {}, "lines": [], "reservations": []}
                    header_for_check = dict(group.get("header", {}))
                    header_for_check["InvoiceNo"] = data.get("invoice_no", "")
                    missing = excel_export.missing_required_fields(
                        header_for_check, group.get("lines", []), group.get("reservations", []),
                        field_mapping, invoice_type="SERVICE",
                    )
                    missing_names = sorted({m["field"] for m in missing})
                    verdict = (
                        {"status": "DATA MISMATCH", "is_active": False, "is_synced": False,
                         "reason": "Missing required field(s): " + ", ".join(missing_names)}
                        if missing_names else
                        {"status": "READY TO LOAD", "is_active": True, "is_synced": False}
                    )
                else:
                    verdict = service_api.enrich_invoice(data)

                n = database.fix_purchase_line_descriptions(header_id, new_items)
                if not n:
                    continue
                fixed_lines += n
                fixed_invoices += 1
                database.revalidate_header_after_description_fix(header_id, data, verdict)
                database.log_event(
                    "STATUS_CHANGED", header_id=header_id, to_status=verdict["status"],
                    detail="Purchase Line Description corrected (continuation-line extraction "
                           "fix) and re-validated during startup migration")
            except Exception:  # noqa: BLE001 - one bad PDF must not stop the rest
                traceback.print_exc()
                continue

        config_store.save_config({"line_description_continuation_fix_done": True})
        print(
            f"[line-description-continuation-fix-migration] Corrected {fixed_lines} Purchase "
            f"Line description(s) across {fixed_invoices} invoice(s) "
            f"({len(candidates)} DATA MISMATCH/PENDING IN SF invoice(s) examined). Will not run again."
        )
    except Exception:  # noqa: BLE001 - never block startup
        traceback.print_exc()


@app.get("/health")
def health():
    return {"status": "Healthy"}


@app.get("/api/version")
def version():
    """The running app's version - shown in the sidebar footer so the
    version string only ever needs to change in one place (app.py's own
    FastAPI `version=`)."""
    return {"version": app.version}


# ==========================================================================
# Configuration API
# ==========================================================================

class ConfigModel(BaseModel):
    folder_path: str
    sf_api_url: Optional[str] = None


def _public_config(cfg):
    """Config safe to send to the browser: never expose the connection
    string (it carries the DB password) or the token-signing secret. Report
    only whether a connection is set."""
    public = {k: v for k, v in cfg.items() if k not in ("db_connection", "auth_secret")}
    public["db_configured"] = bool((cfg.get("db_connection") or "").strip())
    return public


@app.get("/api/config")
def get_config():
    return _public_config(config_store.load_config())


class ScannedPdfsModel(BaseModel):
    enabled: bool
    invoice_type: Literal["PART", "SERVICE"]
    user_id: Optional[int] = None


@app.post("/api/config/scanned-pdfs")
def set_scanned_pdfs(payload: ScannedPdfsModel):
    """Super Admin toggle: whether a scanned/photocopied invoice of the
    given type (PART or SERVICE, no embedded text layer) gets OCR-extracted
    instead of rejected outright. Kept separate per invoice type so turning
    it on for one doesn't also start OCR'ing scanned invoices of the other
    - see config_store.DEFAULT_CONFIG's own comment for the full
    rationale."""
    _require_developer(payload.user_id)
    key = f"allow_scanned_pdfs_{payload.invoice_type.lower()}"
    config_store.save_config({key: bool(payload.enabled)})
    return {"ok": True, key: bool(payload.enabled)}


def _writable(path):
    """Probe real write access by creating and removing a temp file."""
    probe = os.path.join(path, f".piips_write_test_{os.getpid()}.tmp")
    try:
        with open(probe, "w") as f:
            f.write("ok")
        os.remove(probe)
        return True
    except OSError:
        return False


def _is_network_path(path):
    """True for a UNC path (\\\\server\\share) or a drive letter mapped to a
    network share (DRIVE_REMOTE) — i.e. not on the server machine itself."""
    if path.startswith("\\\\") or path.startswith("//"):
        return True
    drive = os.path.splitdrive(path)[0]
    if len(drive) == 2 and drive[1] == ":":
        try:
            import ctypes
            DRIVE_REMOTE = 4
            return ctypes.windll.kernel32.GetDriveTypeW(drive + "\\") == DRIVE_REMOTE
        except Exception:  # noqa: BLE001 - if we can't tell, don't block
            return False
    return False


def _validate_dir(path, label, must_be_local=False):
    """
    Validate a folder path and return it, or raise HTTPException(400) with a
    clear message. Distinguishes: missing value, network-not-allowed,
    non-existent (ask the user to create it), not-a-folder, and
    exists-but-no-access (permission denied).
    """

    path = (path or "").strip()

    if not path:
        raise HTTPException(status_code=400, detail=f"{label} is required.")

    is_network = _is_network_path(path)

    if must_be_local and is_network:
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} must be a folder on the server machine, not a "
                f"network path: {path}"
            ),
        )

    if not os.path.exists(path):
        where = " on the server machine" if must_be_local else \
                " (check the network share, mapping and spelling)"
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} does not exist: {path}. Please create the folder"
                f"{where} and try again."
            ),
        )

    if not os.path.isdir(path):
        raise HTTPException(
            status_code=400,
            detail=f"{label} is not a folder: {path}",
        )

    if not os.access(path, os.R_OK):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} exists but access is denied — you do not have "
                f"read permission: {path}. Grant the service/app account "
                f"access and try again."
            ),
        )

    if not _writable(path):
        raise HTTPException(
            status_code=400,
            detail=(
                f"{label} exists but access is denied — you do not have "
                f"write permission: {path}. Grant the service/app account "
                f"write access and try again."
            ),
        )

    return path


@app.post("/api/config")
def update_config(payload: ConfigModel):

    # The Folder Path must be on the server machine (not a UNC/network share
    # or a network-mapped drive), exist, and be writable; Input, New_Format
    # and the template subfolders are created inside it.
    folder_path = _validate_dir(payload.folder_path, "Folder Path", must_be_local=True)

    updates = {"folder_path": folder_path}
    if payload.sf_api_url is not None:
        updates["sf_api_url"] = payload.sf_api_url.strip()
    config = config_store.save_config(updates)

    # Create Input / New_Format (and Trained_format) under the Folder Path.
    folders = config_store.folders(create=True)

    # Create a subfolder for every defined template under BOTH Input
    # (<Input>/<entity>/<name>, where uploads land) and Output (where the
    # generated JSON is written, mirroring the same structure).
    import template_store
    input_base = folders.get("input", "")
    output_base = folders.get("output", "")
    created_templates = []
    for key in (template_store.load().get("Static_Values") or {}):
        entity, invoice_type, name = key.split("\\", 2)
        parts = [p for p in name.replace("\\", "/").split("/") if p]
        for root in (input_base, output_base):
            if root:
                try:
                    os.makedirs(os.path.join(root, entity, invoice_type, *parts), exist_ok=True)
                except OSError:
                    pass
        created_templates.append(key)

    result = _public_config(config)
    result["folders"] = folders
    result["templates_created"] = created_templates

    return result


# ==========================================================================
# API Configuration  (Service First / NAV backend URL)
# ==========================================================================

class ApiConfigModel(BaseModel):
    sf_api_url: Optional[str] = ""


@app.get("/api/api-config")
def get_api_config():
    return {"sf_api_url": config_store.load_config().get("sf_api_url") or ""}


@app.post("/api/api-config")
def save_api_config(payload: ApiConfigModel):
    url = (payload.sf_api_url or "").strip()
    config_store.save_config({"sf_api_url": url})
    return {"sf_api_url": url}


# ==========================================================================
# Database Configuration API  (Developer only)
# ==========================================================================
# Lets a Developer point the app at a SQL Server / change credentials from
# the UI. The connection string is stored encrypted at rest (config_store)
# and the password is never sent back to the browser. Access is enforced
# server-side by looking up the acting user's role (the app has no session
# layer, so the caller passes their user_id).

class DbConfigModel(BaseModel):
    user_id: Optional[int] = None
    server: str
    database: str
    auth: str = "sql"                     # "sql" | "windows"
    username: Optional[str] = ""
    password: Optional[str] = None        # blank/None = keep existing password


def _require_developer(user_id):
    """Raise 403 unless user_id is an active Super Admin. Requires a working DB
    (users live there); the very first connection is still bootstrapped via
    config.json."""
    import database
    try:
        info = database.get_user_role(user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not info or not info["active"] or (info["role"] or "").lower() != "super admin":
        raise HTTPException(status_code=403, detail="Super Admin access required.")


def _require_not_viewer(user_id):
    """Raise 403 if user_id is the read-only 'Viewer' role - it can see
    every page but never change anything. A missing/unknown user_id is NOT
    blocked here (many write endpoints are reachable before login in some
    flows); this only ever blocks an identified Viewer."""
    import database
    try:
        info = database.get_user_role(user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if info and (info["role"] or "").lower() == "viewer":
        raise HTTPException(status_code=403, detail="Viewers have read-only access.")


@app.get("/api/db-config")
def get_db_config(user_id: Optional[int] = None):
    import database
    # Bootstrap exception: with nothing configured yet, there's no way to
    # be logged in (auth itself needs a working DB), so the frontend's
    # pre-login setup screen (see App.jsx) must be able to read/save this
    # without a Super Admin session. Once a connection exists, every call
    # goes back through the normal Super-Admin-only gate.
    if (config_store.load_config().get("db_connection") or "").strip():
        _require_developer(user_id)
    raw = config_store.load_config().get("db_connection") or ""
    p = database.parse_dotnet_connection(raw)
    return {
        "server": p["server"],
        "database": p["database"],
        "auth": "windows" if p["trusted"] else "sql",
        "username": p["uid"],
        "password_set": p["has_password"],
        "db_configured": bool(raw.strip()),
    }


@app.post("/api/db-config")
def save_db_config(payload: DbConfigModel):
    import database
    # Same bootstrap exception as GET /api/db-config above - only skipped
    # while nothing is configured yet. The moment a real connection exists
    # (including the one this very call is about to save), changing it
    # again requires an authenticated Super Admin.
    if (config_store.load_config().get("db_connection") or "").strip():
        _require_developer(payload.user_id)

    server = (payload.server or "").strip()
    db_name = (payload.database or "").strip()
    if not server or not db_name:
        raise HTTPException(status_code=400, detail="Server and database are required.")

    windows = (payload.auth or "sql").lower() == "windows"
    username = (payload.username or "").strip()
    password = payload.password

    if not windows:
        if not username:
            raise HTTPException(
                status_code=400,
                detail="Username is required for SQL Server authentication.",
            )
        # A blank password means "keep the current one" (so the developer
        # doesn't have to re-type it just to change the server/db).
        if not password:
            existing = database.parse_dotnet_connection(
                config_store.load_config().get("db_connection") or ""
            )
            if existing["has_password"] and existing["uid"] == username:
                password = existing["password"]
            else:
                raise HTTPException(status_code=400, detail="Password is required.")

    conn_str = database.build_dotnet_connection(
        server, db_name,
        uid=("" if windows else username),
        pwd=("" if windows else password),
        trusted=windows,
    )

    ok, err = database.test_connection(conn_str)
    if not ok:
        raise HTTPException(status_code=400, detail=f"Could not connect: {err}")

    config_store.save_config({"db_connection": conn_str})

    # Make sure the target database has the app's tables/procedures.
    try:
        database.ensure_menu_schema(force=True)
    except Exception:  # noqa: BLE001 - connection already tested OK
        import traceback
        traceback.print_exc()

    return {"ok": True, "db_configured": True}


class PublishConfigModel(BaseModel):
    user_id: Optional[int] = None
    publish_root: str


class PublishModel(BaseModel):
    user_id: Optional[int] = None
    environment: str


_DEPLOY_ENVIRONMENTS = {"uat", "live"}


@app.get("/api/publish-config")
def get_publish_config(user_id: Optional[int] = None):
    _require_developer(user_id)
    cfg = config_store.load_config()
    return {
        "publish_root": cfg.get("publish_root") or "",
        "status": cfg.get("publish_status") or {},
    }


@app.post("/api/publish-config")
def save_publish_config(payload: PublishConfigModel):
    _require_developer(payload.user_id)
    root = (payload.publish_root or "").strip()
    if not root:
        raise HTTPException(status_code=400, detail="Root path is required.")
    config_store.save_config({"publish_root": root})
    return {"ok": True}


@app.post("/api/deploy/publish")
def publish_deploy(payload: PublishModel):
    import database
    import deploy
    from datetime import datetime, timezone

    _require_developer(payload.user_id)

    environment = (payload.environment or "").strip().lower()
    if environment not in _DEPLOY_ENVIRONMENTS:
        raise HTTPException(status_code=400, detail="environment must be 'uat' or 'live'.")

    root = (config_store.load_config().get("publish_root") or "").strip()
    who = database.get_username_and_role(payload.user_id) or {}

    def _record(status, log):
        cfg = config_store.load_config()
        publish_status = dict(cfg.get("publish_status") or {})
        publish_status[environment] = {
            "at": datetime.now(timezone.utc).isoformat(),
            "by": who.get("username") or f"user #{payload.user_id}",
            "status": status,
            "log": log,
        }
        config_store.save_config({"publish_status": publish_status})

    try:
        log = deploy.publish(environment, root or None)
    except deploy.PublishError as exc:
        _record("Failed", str(exc))
        raise HTTPException(status_code=500, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        _record("Failed", str(exc))
        raise HTTPException(status_code=500, detail=f"Publish failed: {exc}")

    _record("Success", log)
    return {"ok": True, "log": log}


def _require_folders():
    folders = config_store.folders(create=True)
    if not folders:
        raise HTTPException(
            status_code=400,
            detail="PDF folder is not configured. Save it under Configuration first.",
        )
    if not os.path.isdir(folders["base"]):
        raise HTTPException(
            status_code=400,
            detail=f"Configured folder does not exist: {folders['base']}",
        )
    return folders


def _conflict(error):
    active = job_manager.active_job()
    raise HTTPException(
        status_code=409,
        detail={
            "message": error,
            "job_id": active.job_id if active else None,
        },
    )


# ==========================================================================
# Processing API  (Dashboard -> Start: processes <pdf_folder>/Input)
# ==========================================================================

class StartModel(BaseModel):
    user_id: Optional[int] = None


@app.post("/api/process/start")
def process_start(payload: Optional[StartModel] = None):

    _require_not_viewer(payload.user_id if payload else None)
    folders = _require_folders()

    # Whoever clicks Start is the tracker's "started by".
    started_by = payload.user_id if payload else None

    # Matched formats are extracted; untrained formats are moved to the
    # New_Format folder (under the Folder Path), which is also the training
    # source — no separate mirror is needed now the Folder Path is local.
    job, error = job_manager.start(
        folders["input"],
        folders["output"],
        folders["new_format"],
        mode="process",
        started_by=started_by,
    )

    if error:
        _conflict(error)

    return {"job_id": job.job_id, "status": job.status, "mode": "process"}


@app.get("/api/job/active")
def active_job(mode: Optional[str] = None, brief: bool = False):
    """Active job (optionally for a specific mode: process | train) so the
    UI can resume progress after navigating away. Returns {"active": false}
    when there is none."""

    job = job_manager.active_job(mode)
    if not job:
        return {"active": False}
    return job.status_dict(brief=brief)


@app.get("/api/batches")
def list_batches():
    """List saved batches (newest first) from the database, plus the ordered
    status list so the dashboard can render one count column per status."""
    import database
    try:
        return {"batches": database.list_batches(),
                "statuses": [name for _id, name in database.list_statuses()]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


def _input_files_list():
    """Not-yet-processed files sitting in the Input folder — these are the
    'INITIATED' items (loaded via the PWA or dropped in via File Explorer,
    but not processed yet). Returns [{name, rel}]."""
    folders = config_store.folders(create=False) or {}
    input_dir = folders.get("input")
    out = []
    if input_dir and os.path.isdir(input_dir):
        for root, _dirs, files in os.walk(input_dir):
            for f in sorted(files):
                if f.lower().endswith(ACCEPTED_EXTS):
                    rel = os.path.relpath(os.path.join(root, f), input_dir).replace("\\", "/")
                    out.append({"name": f, "rel": rel})
    return out


def _unsupported_files_list():
    """Files Start couldn't even read (corrupt/unsupported PDF or image),
    moved out of Input into the UNSUPPORTED status folder. Returns
    [{name, rel}]."""
    folder = config_store.status_folder("UNSUPPORTED", create=False)
    out = []
    if folder and os.path.isdir(folder):
        for f in sorted(os.listdir(folder)):
            if f.lower().endswith(ACCEPTED_EXTS) and os.path.isfile(os.path.join(folder, f)):
                out.append({"name": f, "rel": f})
    return out


@app.get("/api/stats/status-counts")
def status_counts():
    """Purchase-header counts grouped by tracker status, PLUS an INITIATED
    bucket for files still waiting in the Input folder and an UNSUPPORTED
    bucket for files Start couldn't read (dashboard pie)."""
    import database
    try:
        counts = database.status_counts()
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    for name, files in (("INITIATED", _input_files_list),
                         ("UNSUPPORTED", _unsupported_files_list)):
        try:
            n = len(files())
            if n:
                sid = database.status_id(name) or 1
                # Re-inserted at the SAME position it already held in
                # `counts` (database.status_counts() lists every status,
                # even a real-tracker-row count of 0 - see
                # usp_StatusCounts, ordered by DisplayOrder) rather than
                # forced to the front, so this synthetic folder-based
                # count still respects DisplayOrder/STATUS_VALUES' order
                # like every other status.
                idx = next((i for i, c in enumerate(counts)
                            if (c.get("status") or "").upper() == name), 0)
                counts = [c for c in counts if (c.get("status") or "").upper() != name]
                counts.insert(idx, {"status_id": sid, "status": name, "count": n})
        except Exception:  # noqa: BLE001 - synthetic count is best-effort
            import traceback
            traceback.print_exc()
    return {"counts": counts}


@app.get("/api/invoices/by-status")
def invoices_by_status(status_id: int):
    """Invoices with a given tracker status (pie-slice pop-up). For the
    INITIATED/UNSUPPORTED statuses the tracker has no rows, so the
    corresponding folder is listed instead."""
    import database
    try:
        for name, files in (("INITIATED", _input_files_list),
                             ("UNSUPPORTED", _unsupported_files_list)):
            if status_id == (database.status_id(name) or -1):
                rows = [
                    {"header_id": None, "invoice_no": "—", "file_name": f["name"],
                     "vendor": "", "format": "", "status": name, "batch": "",
                     "is_active": None, "is_synced": None, "is_excluded": False,
                     "invoice_type": ""}
                    for f in files()
                ]
                if rows:
                    return {"invoices": rows}
        return {"invoices": database.invoices_by_status(status_id)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.get("/api/invoices/{header_id}/fields")
def invoice_field_check(header_id: int):
    """Field-by-field mandatory-data breakdown for one invoice (Dashboard
    'DATA MISMATCH' drill-down): every required column, its current
    value, and whether it's missing."""
    import database
    try:
        return database.get_invoice_field_check(header_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


def _inline_content_disposition(filename):
    """Build a Content-Disposition header carrying BOTH a plain ASCII
    `filename=` and an RFC 5987 `filename*=` for the same name. Starlette's
    own FileResponse(filename=...) emits ONLY filename*= (percent-encoded)
    whenever quote() changes the name at all - which a bare space already
    triggers, so any invoice file name with a space (the common case here,
    e.g. "Armtech - 1748.pdf") loses the plain fallback entirely. Most
    browsers handle filename*= fine, but anything that only understands the
    plain form then falls back to a name derived from the URL instead of the
    real file name - so both forms are always sent together here."""
    ascii_fallback = re.sub(r'[^\x20-\x7E]', "_", filename).replace('"', "'")
    return f"inline; filename=\"{ascii_fallback}\"; filename*=UTF-8''{quote(filename)}"


@app.get("/api/invoices/pdf")
def invoice_pdf(file: str, page: Optional[int] = None, page_end: Optional[int] = None):
    """Serve an invoice's PDF/image inline (clicking an invoice no./file
    name anywhere in the app opens it in the shared PdfModal viewer, which
    all route through this one endpoint). Located by file name across the
    Folder Path. The real file name is always what gets used if the user
    downloads/saves it from the viewer - see _inline_content_disposition.

    `page` (1-based, start of the range) and `page_end` (1-based,
    inclusive end - defaults to `page` when omitted) are passed whenever
    the caller knows which of the source PDF's own pages this specific
    invoice came from (see database._invoice_list's page_start/page_end,
    stored from ocr_engine.py's _merge_group). A single uploaded PDF can
    either print more than one invoice (e.g. 2 invoices, 1 per page,
    sharing one file_name) or one invoice spanning several pages - without
    this every one of those invoices' rows would open/download the
    identical full multi-page file starting at page 1, regardless of which
    invoice was actually clicked, or would only show that invoice's FIRST
    page and silently drop the rest of a genuine multi-page invoice. When
    given (and the file is a real multi-page PDF, not a single-page scan/
    image), that page range is extracted into its own PDF and served
    instead of the whole file - both viewing and downloading then show/
    save only that invoice's own pages."""
    path = config_store.find_pdf(file)
    if not path or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    media = _VIEW_MEDIA.get(os.path.splitext(path)[1].lower(), "application/octet-stream")
    name = os.path.basename(path)

    # No "page > 1" shortcut here: a file with 2+ invoices packed in still
    # needs page 1 isolated from page 2 onward just as much as any later
    # page does - skipping extraction specifically for page 1 was the bug
    # (every OTHER invoice in the file stayed visible/downloadable from
    # the "page 1" view). Only skips when the file turns out to have just
    # the one page total, where extracting would just reproduce the same
    # pages the full-file response already serves.
    if page and media == "application/pdf":
        end = page_end if (page_end and page_end >= page) else page
        try:
            import fitz  # pymupdf
            with fitz.open(path) as src:
                end = min(end, src.page_count)
                if src.page_count > 1 and 1 <= page <= src.page_count:
                    extracted = fitz.open()
                    extracted.insert_pdf(src, from_page=page - 1, to_page=end - 1)
                    data = extracted.tobytes()
                    extracted.close()
                    stem, ext = os.path.splitext(name)
                    page_label = f"page {page}" if end == page else f"pages {page}-{end}"
                    page_name = f"{stem} ({page_label}){ext}"
                    return Response(
                        content=data, media_type=media,
                        headers={"content-disposition": _inline_content_disposition(page_name)},
                    )
        except Exception:  # noqa: BLE001 - extraction is a nice-to-have; fall through to the full file
            traceback.print_exc()

    return FileResponse(
        path, media_type=media,
        headers={"content-disposition": _inline_content_disposition(name)},
    )


@app.get("/api/invoices/by-batch")
def invoices_by_batch(batch: str):
    """Invoices in a batch (batch eye-button pop-up)."""
    import database
    try:
        return {"invoices": database.invoices_by_batch(batch)}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


class ExcludeModel(BaseModel):
    header_id: int
    exclude: bool
    user_id: Optional[int] = None


@app.post("/api/invoices/exclude")
def invoices_exclude(payload: ExcludeModel):
    """Include / exclude an invoice from the Excel export. Excluding also sets
    the tracker status to 'Excluded' and moves the PDF to the Excluded folder;
    including restores the prior status and folder."""
    import database
    import config_store

    _require_not_viewer(payload.user_id)
    try:
        res = database.set_excluded(payload.header_id, payload.exclude, payload.user_id)
    except ValueError as exc:
        # A later batch already relies on this one staying cleared - see
        # database._later_batch_not_created.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not res:
        raise HTTPException(status_code=404, detail="Invoice not found")
    moved = ""
    if res.get("file_name") and res.get("new_status"):
        try:
            moved = config_store.move_pdf_to_status(res["file_name"], res["new_status"])
        except Exception:  # noqa: BLE001 - file move is best-effort
            import traceback
            traceback.print_exc()
    return {"ok": True, "new_status": res.get("new_status"), "moved_to": moved}


@app.get("/api/invoices/buyer-order-missing")
def invoices_buyer_order_missing():
    """Invoices parked at 'BUYER ORDER NO DOESN'T EXIST' (missing or doubtful
    PO) for the Buyer Order Entry menu."""
    import database
    try:
        return {"invoices": database.invoices_by_statuses(
            ["BUYER ORDER NO DOESN'T EXIST"])}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.get("/api/invoices/vendor-code-missing")
def invoices_vendor_code_missing():
    """SERVICE invoices parked at 'NAV VENDOR CODE DOESN'T EXIST' (missing
    from the scanned PDF) for the Vendor Code Entry menu."""
    import database
    try:
        return {"invoices": database.invoices_by_statuses(
            ["NAV VENDOR CODE DOESN'T EXIST"])}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


def _norm_desc(text):
    return re.sub(r"\s+", " ", (text or "").strip()).lower()


@app.get("/api/part-description-update/items")
def part_description_update_items():
    """Service First's own purchase-line records (SpareRequestID, PartNo,
    PartSpecification, Nav_Part_Description, pricing) for every Buyer's
    Order No currently sitting at DATA MISMATCH - Part Description Update
    menu, so a user can see what SF actually has on file for a PO's parts.
    Each row also carries PdfInvoices (PIIPS's own InvoiceNo/FileName for
    that PO - Purchase Details column) and PdfDescriptions (the invoice's
    own saved Purchase Line Description text(s) for that PO - the screen's
    autocomplete when typing a corrected SF description).

    A row is flagged Resolved (kept, not dropped) when its own
    Nav_Part_Description already matches one of the PDF's own descriptions
    (whitespace/case-insensitive) - every part on a PO is shown together so
    a user can see how many of an invoice's lines are already done vs still
    pending, rather than a resolved one silently vanishing just because its
    PO's overall status happens to be DATA MISMATCH for some unrelated
    reason (a missing field elsewhere, another part on the same PO, etc.).
    UNLESS that same description is also claimed by another, different
    part on the same PO (see DuplicateDescription below), in which case
    neither is ever marked Resolved: two distinct parts sharing one
    description means the "match" is ambiguous, not resolved - e.g. an
    invoice's own line reads "DELL3560-BEZEL" and Service First has BOTH
    that part AND an unrelated part "0RHGDM" saved under the identical
    description (someone updated two different parts to the same text) -
    without this override, whichever part actually corresponds to the PDF
    line would look "already matched" and hide the fact that a second,
    wrongly-duplicated part is sitting right behind it, unresolved.

    PdfDescriptions is always every one of the PO's own PDF descriptions,
    for EVERY row (not trimmed by which are already claimed elsewhere on
    the PO) - a user reviewing one part's suggestions still needs to see
    the invoice's other lines to tell them apart, even a currently-claimed
    one. Actually PICKING one already claimed by a different part is what's
    prevented, client-side (PartDescriptionUpdate.jsx's
    descriptionUsedElsewhereInPo, re-checked server-side in
    part_description_update_save before ever saving) - that's the real
    conflict guard, so trimming the dropdown itself would only have hidden
    information, never prevented anything trimming alone didn't already."""
    import database
    import service_api
    try:
        order_nos = database.buyer_order_nos_for_status("DATA MISMATCH")
        items = service_api.get_specification_mismatch_records(order_nos)
        details_by_po = database.invoice_details_by_buyer_order(order_nos, "DATA MISMATCH")

        # A (PurchaseOrderNo, normalized description) claimed by more than
        # one distinct PartNoMapID is a real data conflict - flag every
        # item in the group so the frontend can call it out, and so the
        # "already matches a PDF description" check below never applies to
        # any of them.
        ids_by_key = {}
        for item in items:
            nd = _norm_desc(item.get("Nav_Part_Description"))
            if not nd:
                continue
            key = (item.get("PurchaseOrderNo"), nd)
            ids_by_key.setdefault(key, set()).add(item.get("PartNoMapID"))
        dup_keys = {k for k, ids in ids_by_key.items() if len(ids) > 1}

        result = []
        for item in items:
            details = details_by_po.get(item.get("PurchaseOrderNo"), {})
            pdf_descriptions = details.get("descriptions", [])
            item["PdfInvoices"] = details.get("invoices", [])
            item["PdfDescriptions"] = pdf_descriptions
            nav_desc = _norm_desc(item.get("Nav_Part_Description"))
            key = (item.get("PurchaseOrderNo"), nav_desc)
            if key in dup_keys:
                item["DuplicateDescription"] = True
            elif nav_desc and any(_norm_desc(d) == nav_desc for d in pdf_descriptions):
                item["Resolved"] = True
            result.append(item)

        return {"items": result}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


class PartDescriptionSaveModel(BaseModel):
    part_no_map_id: int
    description: str
    purchase_order_no: Optional[str] = None
    user_id: Optional[int] = None


@app.post("/api/part-description-update/save")
def part_description_update_save(payload: PartDescriptionSaveModel, request: Request):
    """Push a corrected description for one Service First part back to SF -
    Part Description Mapping menu's Update button (UpdateInvoiceDescription-
    InPurchaseLine). part_no_map_id is stores_SparePurchaseLine.PartNoMapID
    (from a GetPurchaseLineSpecificationMismatchRecord row's own
    "PartNoMapID" field) - NOT its "PartID", a different column entirely on
    the same table.

    The same description must map to exactly one part per PO - the
    frontend already blocks this client-side (PartDescriptionUpdate.jsx's
    descriptionUsedElsewhereInPo), but that's trivially bypassed (a stale
    build, a direct API call), and this exact conflict has already
    happened in practice (two different real parts, e.g. "DELL3560-BEZEL"
    and "0RHGDM", ended up saved under the identical description) - so it
    is re-checked here too, against Service First's own current data,
    before ever pushing the update through. Requires purchase_order_no
    (the frontend already has it from the row being edited) - the check
    is skipped, not blocked, when it's not supplied, so this stays
    backward compatible with any older caller that doesn't send it yet."""
    import service_api
    _require_not_viewer(payload.user_id)
    # Collapsed to single spaces (never just .strip()) before it's ever
    # compared OR pushed to Service First - an embedded newline/double space
    # (e.g. a suggestion picked from PdfDescriptions whose own OCR text still
    # had one) makes PIIPS's own "already resolved" check pass (_norm_desc
    # collapses whitespace too) while the value actually saved to Service
    # First keeps it, so SF's OWN GetHSNDetails match against the PDF's real,
    # single-spaced text keeps failing forever after - the invoice looks
    # "✓ Updated" here but never actually leaves DATA MISMATCH (see
    # ATH Printer - 2117.pdf's "CAN DR 240 PICKUP ROLLER KIT \nIMPORT").
    desc = re.sub(r"\s+", " ", (payload.description or "")).strip()
    if payload.purchase_order_no and desc:
        norm = _norm_desc(desc)
        try:
            existing = service_api.get_specification_mismatch_records([payload.purchase_order_no])
        except Exception:  # noqa: BLE001 - a lookup failure must not silently allow a bad save through
            raise HTTPException(status_code=502, detail="Could not verify this description against "
                                                          "Service First's current data - try again.")
        for item in existing:
            if (item.get("PartNoMapID") != payload.part_no_map_id
                    and _norm_desc(item.get("Nav_Part_Description")) == norm):
                raise HTTPException(
                    status_code=409,
                    detail=f"\"{desc}\" is already assigned to part {item.get('PartNo') or 'another part'} "
                           f"on PO {payload.purchase_order_no} - each description can only map to one part.",
                )
    try:
        result = service_api.update_invoice_description(payload.part_no_map_id, desc)
        # Who / when for the matching Purchase Line rows (best effort).
        import database
        user_id = request.scope.get("state", {}).get("user_id")
        stamped = database.record_part_description_update(
            payload.purchase_order_no, desc, user_id, payload.part_no_map_id)
        # This description was exactly what a DATA MISMATCH invoice on this
        # PO was waiting on (Service First couldn't resolve its Nav Item No.
        # against the OLD description) - now that it's corrected, re-check
        # against Service First right away instead of leaving the invoice
        # parked until some later, unrelated run happens to revisit it.
        # Best-effort: a re-check failure must not fail the save itself,
        # which already succeeded against Service First.
        moved = []
        for header_id in stamped.get("header_ids", []):
            try:
                res = database.revalidate_data_mismatch_header(header_id, user_id)
                if res:
                    moved.append({"header_id": header_id, **res})
            except Exception:  # noqa: BLE001
                import traceback
                traceback.print_exc()
        return {"result": result, "revalidated": moved}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Service First update failed: {exc}")


class RevalidateAllModel(BaseModel):
    user_id: Optional[int] = None


@app.post("/api/part-description-update/revalidate-all")
def part_description_revalidate_all(payload: RevalidateAllModel):
    """Re-check EVERY current DATA MISMATCH/PENDING IN SF invoice against
    Service First right now (Part Description Mapping's "Recheck All"
    button) - the catch-up sweep for a description confirmed on Service
    First's own side before the Update button auto-rechecked (see
    part_description_update_save), or by any other means. Safe to run any
    time; each invoice re-validates independently."""
    import database
    _require_not_viewer(payload.user_id)
    try:
        return database.revalidate_all_data_mismatch(payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


class BuyerOrderModel(BaseModel):
    header_id: int
    buyer_order_no: str
    user_id: Optional[int] = None


@app.post("/api/invoices/buyer-order")
def invoices_set_buyer_order(payload: BuyerOrderModel):
    """Manually set a Buyer's Order No. on a parked invoice and re-validate it.
    When no other error remains the invoice becomes active (status = 1) and
    advances (Ready to Load / Extracted); the PDF moves to the new status
    folder."""
    import database
    _require_not_viewer(payload.user_id)
    order_no = (payload.buyer_order_no or "").strip()
    if not order_no:
        raise HTTPException(status_code=400, detail="Buyer order no is required")
    try:
        res = database.apply_manual_buyer_order(
            payload.header_id, order_no, payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not res:
        raise HTTPException(status_code=404, detail="Invoice not found")
    moved = ""
    if res.get("file_name") and res.get("new_status"):
        try:
            moved = config_store.move_pdf_to_status(res["file_name"], res["new_status"])
        except Exception:  # noqa: BLE001 - file move is best-effort
            import traceback
            traceback.print_exc()
    return {"ok": True, "new_status": res.get("new_status"),
            "is_active": res.get("is_active"), "reason": res.get("reason", ""),
            "moved_to": moved}


class VendorCodeModel(BaseModel):
    header_id: int
    vendor_code: str
    user_id: Optional[int] = None


@app.post("/api/invoices/vendor-code")
def invoices_set_vendor_code(payload: VendorCodeModel):
    """Manually set the NAV vendor code on a parked SERVICE invoice. Unlike
    Buyer Order (which re-runs Service First), SERVICE never calls SF at
    all, so filling the code is itself sufficient to become READY TO LOAD;
    the PDF moves to the new status folder."""
    import database
    _require_not_viewer(payload.user_id)
    vendor_code = (payload.vendor_code or "").strip()
    if not vendor_code:
        raise HTTPException(status_code=400, detail="Vendor code is required")
    try:
        res = database.apply_manual_vendor_code(
            payload.header_id, vendor_code, payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not res:
        raise HTTPException(status_code=404, detail="Invoice not found")
    moved = ""
    if res.get("file_name") and res.get("new_status"):
        try:
            moved = config_store.move_pdf_to_status(res["file_name"], res["new_status"])
        except Exception:  # noqa: BLE001 - file move is best-effort
            import traceback
            traceback.print_exc()
    return {"ok": True, "new_status": res.get("new_status"),
            "is_active": res.get("is_active"), "reason": res.get("reason", ""),
            "moved_to": moved}


@app.get("/api/role-menus")
def get_role_menus():
    """Which menu keys each configurable role (admin/user/accounts/viewer)
    can see - read by every logged-in client to build its own sidebar, so
    no auth gate here beyond being reachable at all (same as /api/config).
    Super Admin/Developer always see every menu, unconfigurable, and are
    never part of this mapping - see database.get_role_menus."""
    import database
    try:
        return {"mapping": database.get_role_menus()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


class RoleMenusModel(BaseModel):
    mapping: dict
    user_id: Optional[int] = None


@app.post("/api/role-menus")
def save_role_menus(payload: RoleMenusModel):
    """Replace the whole role->menu mapping - Screen Access menu's Save
    button. Super Admin only: this controls who can reach every other
    screen (including this one), so a lower-privileged role must never be
    able to grant itself more access."""
    import database
    _require_developer(payload.user_id)
    try:
        result = {"mapping": database.save_role_menus(payload.mapping, payload.user_id)}
        security.forget_user()
        return result
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


# Load / Post / Complete lifecycle. Each stage lists invoices at its source
# status(es) and advances the selected ones to its target status.
_LIFECYCLE = {
    "load":     {"from": ["READY TO LOAD", "REJECTED BY ACCOUNTS"], "to": "LOADED"},
    "post":     {"from": ["LOADED"],  "to": "POSTED"},
    "complete": {"from": ["POSTED"],  "to": "COMPLETED"},
}


_POST_ROLES = {"accounts", "admin", "super admin"}


def _require_post_access(user_id):
    """Raise 403 unless user_id is an active Accounts, Admin, or Super Admin
    user (the Post page's mark-as-posted and reject actions)."""
    import database
    try:
        info = database.get_user_role(user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not info or not info["active"] or (info["role"] or "").lower() not in _POST_ROLES:
        raise HTTPException(status_code=403, detail="Accounts, Admin, or Super Admin access required.")


@app.get("/api/lifecycle/invoices")
def lifecycle_invoices(stage: str):
    """Invoices eligible for a lifecycle stage (load | post | complete)."""
    stage = (stage or "").lower()
    if stage not in _LIFECYCLE:
        raise HTTPException(status_code=400, detail="Unknown stage")
    import database
    try:
        return {"invoices": database.invoices_by_statuses(
                    _LIFECYCLE[stage]["from"], active_only=True),
                "to": _LIFECYCLE[stage]["to"]}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


class LifecycleModel(BaseModel):
    stage: str
    header_ids: List[int]
    user_id: Optional[int] = None


@app.post("/api/lifecycle/advance")
def lifecycle_advance(payload: LifecycleModel):
    """Advance the selected invoices to a lifecycle stage's target status
    (Load→LOADED, Post→POSTED, Complete→COMPLETED) and move each PDF into its
    new status folder. Posted/Completed invoices are also archived into
    <Folder Path>/ALL_INVOICES as "<Invoice No.>_<Vendor Name>.pdf" (a copy,
    the original stays in its status folder). On Load, any invoice whose
    [No.] duplicates another invoice's is silently excluded from the
    advance (see database.advance_status) and reported back in
    'duplicate_no' so the caller can alert on it."""
    stage = (payload.stage or "").lower()
    if stage not in _LIFECYCLE:
        raise HTTPException(status_code=400, detail="Unknown stage")
    _require_not_viewer(payload.user_id)
    if stage == "post":
        _require_post_access(payload.user_id)
    spec = _LIFECYCLE[stage]
    import database
    try:
        res = database.advance_status(
            payload.header_ids, spec["from"], spec["to"], payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    files = res.get("files", [])
    for fname in files:
        try:
            config_store.move_pdf_to_status(fname, spec["to"])
        except Exception:  # noqa: BLE001 - file move is best-effort
            import traceback
            traceback.print_exc()

    if spec["to"] in ("POSTED", "COMPLETED") and files:
        try:
            meta = {m["file_name"]: m for m in database.invoices_by_header_ids(res.get("header_ids", []))}
            for fname in files:
                m = meta.get(fname)
                if not m:
                    continue
                inv_no = config_store._safe_folder(m.get("invoice_no") or "NoInvoiceNo")
                vendor = config_store._safe_folder(m.get("vendor") or "UnknownVendor")
                ext = os.path.splitext(fname)[1] or ".pdf"
                dest_name = f"{inv_no}_{vendor}{ext}"
                new_src = os.path.join(config_store.status_folder(spec["to"]), fname)
                config_store.copy_pdf_to_all_invoices(new_src, dest_name)
        except Exception:  # noqa: BLE001 - archive copy is best-effort
            import traceback
            traceback.print_exc()

    return {"ok": True, "count": res.get("count", 0), "to": spec["to"],
            "duplicate_no": res.get("duplicate_no", [])}


class RejectModel(BaseModel):
    header_id: int
    remark: str
    user_id: Optional[int] = None


@app.post("/api/lifecycle/reject")
def lifecycle_reject(payload: RejectModel):
    """Accounts/Admin/Super Admin: reject one LOADED invoice, on the Post
    page, back to REJECTED BY ACCOUNTS with a required remark. The invoice
    reappears on the Load page (alongside READY TO LOAD) with the remark
    visible, for another attempt."""
    import database
    _require_post_access(payload.user_id)
    remark = (payload.remark or "").strip()
    if not remark:
        raise HTTPException(status_code=400, detail="A remark is required to reject an invoice.")
    try:
        res = database.reject_invoice(payload.header_id, remark, payload.user_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not res:
        raise HTTPException(status_code=404, detail="Invoice not found")
    moved = ""
    if res.get("file_name"):
        try:
            moved = config_store.move_pdf_to_status(res["file_name"], "REJECTED BY ACCOUNTS")
        except Exception:  # noqa: BLE001 - file move is best-effort
            import traceback
            traceback.print_exc()
    return {"ok": True, "new_status": "REJECTED BY ACCOUNTS", "moved_to": moved}


@app.get("/api/batches/download")
def download_batch(request: Request, batch: str, doc_no: Optional[str] = None, entry_no: Optional[str] = None):
    """Rebuild the Excel for a batch from the 3 DB tables, on demand.
    Optional doc_no / entry_no override the Document No. / Entry No.
    sequence used in the export (Dashboard > Batches inputs) — see
    excel_export.renumber_batch."""
    import database
    import excel_export
    import tempfile

    name = (batch or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="batch is required")

    def _to_int(raw, field):
        raw = (raw or "").strip()
        if not raw:
            return None
        try:
            return int(raw)
        except ValueError:
            raise HTTPException(status_code=400, detail=f"{field} must be a whole number")

    start_doc_no = _to_int(doc_no, "doc_no")
    start_entry_no = _to_int(entry_no, "entry_no")

    locked = database.is_batch_locked(name)
    if locked:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch '{name}' has an invoice already Loaded, Excluded, "
                "Posted, Completed, or Rejected — it can no longer be "
                "downloaded or have its Document No./Entry No. renumbered."
            ),
        )

    all_batches = database.list_batches()

    # An invoice still parked at Buyer Order No Doesn't Exist has nothing
    # usable for Navision yet - block the whole batch's download rather
    # than silently exporting it without a PO, or worse, minting it a
    # Document No. now that it'll need redone once the PO is fixed later.
    this_batch = next((b for b in all_batches if b.get("batch") == name), None)
    missing_po = (this_batch or {}).get("counts", {}).get("BUYER ORDER NO DOESN'T EXIST", 0)
    if missing_po:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch '{name}' has {missing_po} invoice(s) with no Buyer "
                "Order No. — kindly fill in the Buyer Order No before "
                "downloading this batch."
            ),
        )

    # A Data Mismatch invoice is missing a required field - block the whole
    # batch's download the same way a missing Buyer Order No does, rather
    # than silently exporting around it (and minting Document Nos. for the
    # rest) while it sits unresolved. The batch stays CREATED until every
    # Data Mismatch in it is cleared - resolving one moves it to whatever
    # status it now deserves (possibly still Data Mismatch, for a different
    # field) via the same re-upload/reprocess path as New Template/Excluded.
    missing_data = (this_batch or {}).get("counts", {}).get("DATA MISMATCH", 0)
    if missing_data:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch '{name}' has {missing_data} invoice(s) at Data "
                "Mismatch — kindly resolve the missing field(s) before "
                "downloading this batch."
            ),
        )

    # Batches must clear in creation order: an earlier batch not yet fully
    # Loaded/Posted/Completed (ignoring its excluded invoices) blocks any
    # newer batch's download, so Document Nos. never get ahead of a batch
    # still pending - see database.list_batches's 'blocked_by'.
    blocked_by = next(
        (b.get("blocked_by", []) for b in all_batches if b.get("batch") == name),
        [],
    )
    if blocked_by:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch '{name}' can't be downloaded yet — the earlier "
                f"batch(es) {', '.join(blocked_by)} must be fully Loaded, "
                "Posted, or Completed first."
            ),
        )

    # Only one batch may be mid-flight (Downloaded or In Progress) at a
    # time - if some OTHER batch is already sitting there, it must be
    # taken to Loaded/Posted/Completed before a different batch may be
    # downloaded, even if this one wouldn't otherwise be blocked by
    # creation order.
    active_others = [
        b["batch"] for b in all_batches
        if b["batch"] != name and b.get("batch_status") in ("DOWNLOADED", "IN PROGRESS")
    ]
    if active_others:
        raise HTTPException(
            status_code=400,
            detail=(
                f"Batch(es) {', '.join(active_others)} still Downloaded/In "
                "Progress — take it to Loaded, Posted, or Completed before "
                f"downloading '{name}'."
            ),
        )

    try:
        sheet_data = database.fetch_batch(name, excel_export.sheet_columns())
    except ValueError as exc:
        # A Document No. collision against another batch (see
        # database._find_no_collision) - nothing was written, refuse the
        # download with a clear reason rather than a generic 500.
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if not sheet_data["Purchase Header"]["rows"]:
        raise HTTPException(status_code=404, detail=f"Batch '{name}' not found")

    # database.fetch_batch() already wrote a real [No.]/[Entry No.] directly
    # (see _assign_document_numbers/_assign_entry_numbers) - the batch being
    # locked was already rejected above, so a custom doc_no/entry_no here is
    # a deliberate user override on top of that, persisted the same way
    # (overwriting what fetch_batch just auto-assigned).
    if start_doc_no is not None or start_entry_no is not None:
        excel_export.renumber_batch(sheet_data, start_doc_no, start_entry_no)
        doc_pairs = {
            row.get("Id"): row.get("No.", "")
            for row in sheet_data["Purchase Header"]["rows"]
            if row.get("Id") is not None
        } if start_doc_no is not None else {}
        entry_pairs = {
            row.get("Id"): row.get("Entry No.", "")
            for row in sheet_data["Reservation Entry"]["rows"]
            if row.get("Id") is not None
        } if start_entry_no is not None else {}
        try:
            # Check BOTH mappings before writing EITHER - write_document_numbers
            # and write_entry_numbers each commit independently, so without
            # this upfront check a Document No. write could succeed and
            # commit only for the paired Entry No. write to then collide,
            # leaving the Document No. change persisted despite the overall
            # request failing with "nothing was changed".
            database.precheck_number_collisions(doc_pairs, entry_pairs, name)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))
        if start_doc_no is not None:
            try:
                database.write_document_numbers(doc_pairs, name)
            except ValueError as exc:
                # A custom-chosen number collides with another batch's -
                # nothing was written, refuse rather than export an Excel
                # with a Document No. that never actually got saved.
                raise HTTPException(status_code=400, detail=str(exc))
        if start_entry_no is not None:
            try:
                database.write_entry_numbers(entry_pairs, name)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))

    try:
        database.mark_batch_downloaded(name, user_id=request.scope.get("state", {}).get("user_id"))
    except Exception:  # noqa: BLE001 - best-effort, download still succeeds
        import traceback
        traceback.print_exc()

    safe = "".join(c for c in name if c.isalnum() or c in ("-", "_")) or "batch"
    out_path = os.path.join(tempfile.gettempdir(), f"{safe}.xlsx")
    excel_export.build_workbook_from_sheets(sheet_data, out_path)

    return FileResponse(
        out_path,
        filename=f"{safe}.xlsx",
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


# ==========================================================================
# User Manual download (role-specific PDF, stamped with who downloaded it)
# ==========================================================================

MANUALS_DIR = os.path.join(BASE_DIR, "manuals")
_MANUAL_FILES = {
    "user": "PIIPS_User_Manual_User.pdf",
    "accounts": "PIIPS_User_Manual_Accounts.pdf",
    "admin": "PIIPS_User_Manual_Admin.pdf",
    "super admin": "PIIPS_User_Manual_Super_Admin.pdf",
}
_DEVELOPER_MANUAL_FILE = "PIIPS_Developer_Manual.pdf"
_DEPLOYMENT_GUIDE_FILE = "PIIPS_UAT_Deployment_Guide.pdf"
# "developer" is a legacy alias for "super admin" (pre-rename accounts),
# matching how the frontend treats the two roles as equivalent everywhere.
_SUPER_ADMIN_ROLES = {"super admin", "developer"}


def _stamp_and_serve(src_path, who, out_name):
    import tempfile
    import fitz

    if not os.path.isfile(src_path):
        raise HTTPException(status_code=404, detail="Manual file is missing on the server")

    doc = fitz.open(src_path)
    try:
        stamp = (f"Downloaded by {who['username']} ({who['role']}) on "
                 f"{datetime.now().strftime('%Y-%m-%d %H:%M')}")
        for page in doc:
            page.insert_text(
                fitz.Point(28, page.rect.height - 18), stamp,
                fontsize=7.5, color=(0.4, 0.4, 0.4),
            )
        out_path = os.path.join(tempfile.gettempdir(), f"{out_name}_{who['username']}.pdf")
        doc.save(out_path, garbage=4, deflate=True)
    finally:
        doc.close()

    return FileResponse(out_path, filename=f"{out_name}.pdf", media_type="application/pdf")


@app.get("/api/manual/download")
def download_manual(user_id: int, kind: str = "user"):
    """A manual PDF, with a footer stamped on every page recording who
    downloaded it and when.

    kind='user' (default): the User Manual matching the requester's own
    role — there is no way to request a different role's manual than the
    caller's own.
    kind='developer': the full Developer Manual — Super Admin only.
    kind='deployment': the UAT/production Deployment Guide — Super Admin only.
    """
    import database

    who = database.get_username_and_role(user_id)
    if not who:
        raise HTTPException(status_code=404, detail="User not found")

    role_key = (who.get("role") or "").strip().lower()

    if kind in ("developer", "deployment"):
        if role_key not in _SUPER_ADMIN_ROLES:
            raise HTTPException(
                status_code=403,
                detail=f"The {'Developer Manual' if kind == 'developer' else 'Deployment Guide'} is Super Admin only",
            )
        fname = _DEVELOPER_MANUAL_FILE if kind == "developer" else _DEPLOYMENT_GUIDE_FILE
        out_name = "PIIPS_Developer_Manual" if kind == "developer" else "PIIPS_UAT_Deployment_Guide"
        src_path = os.path.join(MANUALS_DIR, fname)
        return _stamp_and_serve(src_path, who, out_name)

    fname = _MANUAL_FILES.get(role_key)
    if not fname:
        raise HTTPException(
            status_code=404, detail=f"No manual available for role '{who.get('role')}'"
        )
    src_path = os.path.join(MANUALS_DIR, fname)
    return _stamp_and_serve(src_path, who, f"PIIPS_User_Manual_{who['role'].replace(' ', '_')}")


@app.get("/api/process/status/{job_id}")
def process_status(job_id: str, brief: bool = False):

    job = job_manager.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Unknown job_id")

    return job.status_dict(brief=brief)


@app.get("/api/process/result/{job_id}")
def process_result(job_id: str):

    job = job_manager.get(job_id)

    if not job:
        raise HTTPException(status_code=404, detail="Unknown job_id")

    return job.result_dict()


# ==========================================================================
# Input folder API  (open in Explorer / upload / list) — per template subpath
# ==========================================================================

def _input_subdir(subpath):
    """Resolve <Input>/<subpath> safely (no traversal outside Input)."""
    folders = _require_folders()
    base = os.path.abspath(folders["input"])
    parts = [p for p in (subpath or "").replace("\\", "/").split("/")
             if p and p not in (".", "..")]
    target = os.path.abspath(os.path.join(base, *parts))
    if target != base and not target.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Invalid target folder")
    return target


@app.get("/api/input/files")
def input_files(subpath: str = ""):
    """List files in <Input>/<subpath> (subpath = selected template)."""

    folder = _input_subdir(subpath)
    os.makedirs(folder, exist_ok=True)

    items = []
    for name in sorted(os.listdir(folder)):
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            items.append({"name": name, "size": os.path.getsize(path)})

    return {"folder": folder, "files": items}


@app.post("/api/input/upload")
def upload_input(subpath: str = "", user_id: Optional[int] = None,
                 files: List[UploadFile] = File(...)):
    """Upload PDF/image files into <Input>/<subpath> (selected template).
    Each saved file is logged as 'initiated' by the uploading user so the
    purchase tracker can attribute InitiatedBy once the file is processed."""

    import database

    _require_not_viewer(user_id)
    folder = _input_subdir(subpath)
    os.makedirs(folder, exist_ok=True)

    # Relative path under Input, matching what the processor computes
    # (os.path.relpath(src, Input)) so the initiator can be resolved later.
    rel_base = "/".join(
        p for p in (subpath or "").replace("\\", "/").split("/") if p and p not in (".", "..")
    )

    def rel_for(name):
        return f"{rel_base}/{name}" if rel_base else name

    # Candidate (type-valid) files, and the ones rejected for bad type.
    candidates, results = [], []
    for f in files:
        name = os.path.basename(f.filename or "")
        if not name or not name.lower().endswith(ACCEPTED_EXTS):
            results.append({"file": f.filename or "", "status": "skipped",
                            "reason": "Unsupported file type"})
            continue
        candidates.append((name, rel_for(name), f))

    # Which of these were already initiated (uploaded) before, by anyone?
    already = {}
    if candidates:
        try:
            already = database.get_initiated_files([rel for _, rel, _ in candidates])
        except Exception:  # noqa: BLE001 - if the lookup fails, treat as none
            import traceback
            traceback.print_exc()

    saved, skipped, logged = [], [], []
    for name, rel, f in candidates:
        prev = already.get(rel)
        if prev:
            # Already initiated by someone earlier -> ignore, do not reload.
            who = prev.get("by_name") or (f"user {prev['by_id']}" if prev.get("by_id") else "another user")
            when = (prev.get("at") or "").replace("T", " ")[:19]
            results.append({
                "file": name, "status": "already_initiated",
                "reason": f"Already initiated by {who}" + (f" on {when}" if when else ""),
            })
            skipped.append(name)
            continue
        # New file -> load it and log the initiation.
        with open(os.path.join(folder, name), "wb") as out:
            shutil.copyfileobj(f.file, out)
        saved.append(name)
        logged.append({"FileName": name, "RelPath": rel})
        results.append({"file": name, "status": "loaded", "reason": "New file loaded"})

    if logged:
        try:
            database.log_input_files(logged, user_id)
        except Exception:  # noqa: BLE001 - logging must not fail the upload
            import traceback
            traceback.print_exc()

    return {"results": results, "saved": saved, "skipped": skipped}


# ==========================================================================
# Format training API
# ==========================================================================

@app.post("/api/train")
def train_start(user_id: Optional[int] = None):
    """Learn (merge) invoice formats from PDFs in the server New_Format folder.
    Super Admin only - the Training screen this action lives on is already
    restricted to Super Admin/Developer in ROLE_MENUS."""
    _require_developer(user_id)
    folders = _require_folders()

    # Back up the current model before training so a failed/partial run
    # can be rolled back.
    backup_name = FormatModel().backup()

    job, error = job_manager.start(
        folders["new_format"],
        folders["output"],
        trained_folder=folders["trained_format"],
        mode="train",
    )

    if error:
        _conflict(error)

    return {
        "job_id": job.job_id,
        "status": job.status,
        "mode": "train",
        "backup": backup_name,
    }


def _train_folder():
    """The New_Format folder (under the Folder Path) that training reads from."""
    folders = config_store.folders(create=True)
    return folders.get("new_format", "") if folders else ""


_VIEW_MEDIA = {
    ".pdf": "application/pdf", ".png": "image/png", ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".bmp": "image/bmp", ".webp": "image/webp",
}


@app.get("/api/train/files")
def train_files():
    """List the files in the New_Format folder that are waiting to be trained."""
    folder = _train_folder()
    items = []
    if folder and os.path.isdir(folder):
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if os.path.isfile(path) and name.lower().endswith(ACCEPTED_EXTS):
                items.append({"name": name, "size": os.path.getsize(path)})
    return {"folder": folder, "files": items}


@app.get("/api/train/file")
def train_file(name: str):
    """Serve a New_Format file inline so it can be viewed in the browser."""
    folder = _train_folder()
    safe = os.path.basename(name or "")
    if not safe:
        raise HTTPException(status_code=400, detail="name is required")
    if not folder:
        raise HTTPException(status_code=404, detail="File not found")
    path = os.path.abspath(os.path.join(folder, safe))
    # Only serve files directly inside the New_Format folder (no traversal).
    if os.path.dirname(path) != os.path.abspath(folder) or not os.path.isfile(path):
        raise HTTPException(status_code=404, detail="File not found")
    media = _VIEW_MEDIA.get(os.path.splitext(safe)[1].lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, content_disposition_type="inline")


@app.get("/api/formats")
def list_formats():
    """Return the learned invoice formats."""

    return {"formats": FormatModel().formats}


@app.delete("/api/formats")
def clear_formats(user_id: Optional[int] = None):
    """Forget all trained formats (start clean). Super Admin only - wipes
    every vendor's learned format at once, and the Training screen this
    lives on is already Super Admin/Developer-only in ROLE_MENUS."""
    _require_developer(user_id)
    model = FormatModel()
    model.clear()
    return {"formats": [], "message": "All trained formats cleared"}


class RestoreModel(BaseModel):
    name: str
    user_id: Optional[int] = None


@app.get("/api/backups")
def list_backups():
    """List available model backups (newest first), plus the current live
    model so the UI can show it alongside the backups."""
    from datetime import datetime
    from format_model import MODEL_FILE

    model = FormatModel()
    current = {"formats": len(model.formats), "modified": ""}
    if os.path.exists(MODEL_FILE):
        current["modified"] = datetime.fromtimestamp(
            os.path.getmtime(MODEL_FILE)
        ).strftime("%Y-%m-%d %H:%M:%S")

    return {"backups": FormatModel.list_backups(), "current": current}


@app.post("/api/backups/restore")
def restore_backup(payload: RestoreModel):
    """Restore a chosen model backup. Super Admin only - can silently
    discard every format learned since the chosen backup."""
    _require_developer(payload.user_id)
    model = FormatModel()
    try:
        model.restore(payload.name)
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Backup not found")

    return {
        "restored": payload.name,
        "formats": model.formats,
        "message": f"Restored {payload.name}",
    }


# ==========================================================================
# Field mapping API  (JSON fields -> Excel columns)
# ==========================================================================

class MappingModel(BaseModel):
    mapping: dict
    user_id: Optional[int] = None


@app.get("/api/mapping")
def get_mapping():
    """Excel columns per sheet, available JSON fields, and saved mapping."""
    import excel_export
    return {
        "columns": excel_export.sheet_columns(),
        "fields": excel_export.available_fields(),
        "mapping": excel_export.load_mapping(),
    }


@app.post("/api/mapping")
def save_mapping(payload: MappingModel):
    import excel_export
    _require_not_viewer(payload.user_id)
    return {"mapping": excel_export.save_mapping(payload.mapping, payload.user_id)}


# ==========================================================================
# Field (Excel column) management API  (create / reorder / delete)
# ==========================================================================

class FieldsModel(BaseModel):
    columns: dict
    user_id: Optional[int] = None


@app.get("/api/fields")
def get_fields():
    """Effective columns per sheet, plus the original template columns."""
    import excel_export
    import template_store

    columns = excel_export.sheet_columns()
    mapping = excel_export.load_mapping()

    # A column can be reused across many templates with no single "this
    # invoice's" value to check (unlike the Template edit screen, which
    # only ever looks at the one template being edited) - so "Template" is
    # downgraded to "None" here only when NOT ONE currently-saved template
    # has a non-blank static value for it. Otherwise every unmapped column
    # would show "Template" regardless of whether anything actually uses it.
    templates = template_store.load()["Static_Values"].values()

    def has_any_static(sheet, col):
        return any((t.get(sheet) or {}).get(col, "").strip() for t in templates)

    sources = {}
    for sheet, cols in columns.items():
        sources[sheet] = {}
        for col in cols:
            source = excel_export.field_source(sheet, col, mapping)
            if source == "Template" and not has_any_static(sheet, col):
                source = "None"
            sources[sheet][col] = source
    return {
        "columns": columns,
        "template": excel_export.template_columns(),
        "sources": sources,
    }


@app.post("/api/fields")
def save_fields(payload: FieldsModel):
    """Save customized column lists {sheet: [columns]} (order matters)."""
    import excel_export
    _require_not_viewer(payload.user_id)
    return {"columns": excel_export.save_columns(payload.columns, payload.user_id)}


# ==========================================================================
# Business templates API  (entity + name + static values, folder-checked)
# ==========================================================================

class TemplateModel(BaseModel):
    entity: str
    invoice_type: str
    name: str
    po_format: Optional[str] = ""
    static: dict = {}
    user_id: Optional[int] = None
    # Set only when renaming an existing template (Template Edit screen's
    # Template name field) - the key it's being renamed FROM.
    original_key: Optional[str] = None


class TemplateKeyModel(BaseModel):
    key: str
    user_id: Optional[int] = None


@app.get("/api/templates")
def get_templates():
    import template_store
    import excel_export

    columns = excel_export.sheet_columns()
    mapping = excel_export.load_mapping()
    sources = {
        sheet: {col: excel_export.field_source(sheet, col, mapping) for col in cols}
        for sheet, cols in columns.items()
    }
    return {
        "entities": template_store.ENTITIES,
        "invoice_types": template_store.INVOICE_TYPES,
        "sheets": template_store.SHEETS,
        "columns": columns,
        "mapping": mapping,
        "sources": sources,
        "templates": template_store.load()["Static_Values"],
    }


@app.post("/api/templates")
def save_template(payload: TemplateModel):
    import template_store
    _require_not_viewer(payload.user_id)
    try:
        key, folder = template_store.save_template(
            payload.entity, payload.invoice_type, payload.name, payload.po_format,
            payload.static, payload.user_id, original_key=payload.original_key,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return {"key": key, "folder": folder}


@app.post("/api/templates/delete")
def delete_template(payload: TemplateKeyModel):
    import template_store
    _require_not_viewer(payload.user_id)
    return {"deleted": template_store.delete_template(payload.key, payload.user_id)}


# ==========================================================================
# Authentication API
# ==========================================================================

class LoginModel(BaseModel):
    username: str
    password: str


class ForgotPasswordModel(BaseModel):
    # Several accounts can share one email address, so the form asks for the
    # username AND the email and matches the pair. `username_or_email` is the
    # older single-field form, still accepted.
    username: Optional[str] = ""
    email: Optional[str] = ""
    username_or_email: Optional[str] = ""


class ChangePasswordModel(BaseModel):
    user_id: int
    current_password: str
    new_password: str


def _base_url(request: Request):
    """The app's own public-facing base URL. Prefers the Origin/Referer
    header sent by the caller's browser - that reflects the actual URL the
    admin is signed in on (e.g. https://piips.precisionit.co.in:8010) -
    over request.base_url, which reflects Uvicorn's own view of itself
    behind the IIS reverse proxy (http://127.0.0.1:8000) and is wrong
    unless IIS forwards proxy headers that Uvicorn is configured to trust,
    which it isn't here. Falls back to request.base_url for non-browser
    callers with neither header. Works unmodified on localhost/UAT/Live -
    never hard-coded."""
    origin = request.headers.get("origin")
    if origin:
        return origin.rstrip("/")
    referer = request.headers.get("referer")
    if referer:
        from urllib.parse import urlsplit
        parts = urlsplit(referer)
        if parts.scheme and parts.netloc:
            return f"{parts.scheme}://{parts.netloc}"
    return str(request.base_url).rstrip("/")


def _client_ip(request: Request):
    """The caller's address for throttling. Behind the IIS reverse proxy every
    request arrives from the proxy's own (loopback/private) address, which
    would make ALL users share one throttle bucket - so when the direct peer
    is a proxy on this machine/LAN, take the first X-Forwarded-For hop."""
    peer = (request.client.host if request.client else "") or "unknown"
    forwarded = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded:
        import ipaddress
        try:
            addr = ipaddress.ip_address(peer)
            if addr.is_loopback or addr.is_private:
                return forwarded
        except ValueError:
            pass
    return peer


@app.post("/api/login")
def api_login(payload: LoginModel, request: Request):
    import database
    username = (payload.username or "").strip()
    # Slow password guessing: 10 attempts / 15 min per client + username.
    throttle_key = f"login:{_client_ip(request)}:{username.lower()}"
    if security.throttled(throttle_key, 10, 15 * 60):
        raise HTTPException(
            status_code=429,
            detail="Too many sign-in attempts. Please wait a few minutes and try again.",
        )
    try:
        user = database.authenticate(username, payload.password or "")
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not user:
        raise HTTPException(
            status_code=401,
            detail="Invalid username or password, or the account is inactive.",
        )
    security.clear_attempts(throttle_key)
    database.record_login(user["user_id"])
    return {**user, "token": security.issue_token(user["user_id"])}

@app.post("/api/logout")
def api_logout(request: Request):
    """Sign the caller out: stamp the logout time, mark them offline and make
    their token unusable."""
    import database
    state = request.scope.get("state", {})
    database.record_logout(state.get("user_id"))
    if state.get("token"):
        security.revoke_token(state["token"])
    return {"ok": True}



@app.post("/api/forgot-password")
def api_forgot_password(payload: ForgotPasswordModel, request: Request):
    """Self-service password reset: email a new auto-generated password to
    the account's registered address. Always returns the same generic
    response regardless of whether the account exists, so this endpoint
    can't be used to enumerate valid usernames/emails."""
    import database
    import mailer

    username = (payload.username or "").strip()
    email = (payload.email or "").strip()
    legacy = (payload.username_or_email or "").strip()
    if legacy and not username and not email:
        if "@" in legacy:
            email = legacy
        else:
            username = legacy
    name_or_email = username or email
    generic = {"ok": True, "message": "If that account exists, a new password has been emailed to it."}
    if not name_or_email:
        return generic
    # Nobody may keep resetting (and mailing) other people's passwords:
    # 5 requests / hour per client, and 3 / hour per target account.
    if (security.throttled(f"forgot:{_client_ip(request)}", 5, 3600)
            or security.throttled(f"forgot-target:{name_or_email.lower()}", 3, 3600)):
        raise HTTPException(
            status_code=429,
            detail="Too many password-reset requests. Please try again later.",
        )

    try:
        if username:
            # The account is named: an email given alongside it must be that
            # account's own, so one shared address can't reset a neighbour.
            user = database.get_user(username)
            if user and email and (user.get("Email") or "").strip().lower() != email.lower():
                user = None
        else:
            matches = database.get_users_by_email(email)
            if len(matches) > 1:
                # Same address on several accounts and no username given: don't
                # guess which one - mail the address its usernames so the owner
                # can ask again with the username.
                names = ", ".join(m["UserName"] for m in matches if m["IsActive"])
                if names:
                    try:
                        mailer.send_mail(
                            email, "Your PIIPS usernames",
                            "<p>Several PIIPS accounts use this email address: <b>"
                            + names + "</b>.</p><p>On the Forgot password screen, enter the "
                            "username you need together with this email address.</p>")
                    except mailer.MailError:
                        pass
                return generic
            user = matches[0] if matches else None
    except Exception:  # noqa: BLE001
        return generic

    if not user or not user["IsActive"] or not user.get("Email"):
        # The caller always gets the same reply (no account enumeration); the
        # reason is logged for whoever runs the server.
        print(f"[forgot-password] no reset sent: no active account with an email "
              f"for username={username!r} email={email!r}")
        return generic

    temp_password = database.generate_temp_password()
    try:
        database.reset_password(user["UserName"], temp_password, force_change=True)
    except Exception:  # noqa: BLE001
        return generic

    try:
        mailer.send_mail(
            user["Email"], "Your PIIPS password was reset",
            mailer.password_reset_email_html(user["UserName"], temp_password, _base_url(request)),
        )
    except mailer.MailError as exc:
        # Same reply either way - the password WAS reset - but say why the mail
        # never arrived (SMTP not configured / login refused / unreachable).
        print(f"[forgot-password] password reset for {user['UserName']!r} but the email "
              f"could not be sent: {exc}")

    return generic


@app.post("/api/change-password")
def api_change_password(payload: ChangePasswordModel):
    """Change a user's own password (requires the current one) - used both
    for the forced first-login change and a normal self-service change."""
    import database
    user = database.get_user_by_id(payload.user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found.")
    if not database.verify_password(payload.current_password or "", user["Password"] or ""):
        raise HTTPException(status_code=401, detail="Current password is incorrect.")
    if (payload.new_password or "").strip().lower() == (user["UserName"] or "").strip().lower():
        raise HTTPException(status_code=400, detail="Your new password can't be the same as your username.")
    try:
        database.reset_password(user["UserName"], payload.new_password, force_change=False)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    return {"ok": True}


# ==========================================================================
# User management API
# ==========================================================================

class UserCreateModel(BaseModel):
    username: str
    email: Optional[str] = None
    user_type_id: int
    created_by: Optional[int] = None
    # Viewer accounts only: a Super Admin assigns the password directly
    # instead of one being auto-generated and emailed - see api_create_user.
    password: Optional[str] = None


class UserActiveModel(BaseModel):
    user_id: int
    is_active: bool
    modified_by: Optional[int] = None


class UserResetPasswordModel(BaseModel):
    user_id: int          # the acting admin/super admin
    target_user_id: int   # whose password is being reset
    new_password: Optional[str] = None   # None = auto-generate


class UserChangeTypeModel(BaseModel):
    user_id: int          # the acting admin/super admin
    target_user_id: int   # whose role is being changed
    new_user_type_id: int


def _email_result(email_sent, email_error):
    return {"email_sent": email_sent, "email_error": email_error}


@app.get("/api/users")
def api_list_users():
    import database
    try:
        return {
            "users": database.list_users(),
            "user_types": database.list_user_types(),
        }
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.post("/api/users")
def api_create_user(payload: UserCreateModel, request: Request):
    """Create a user. Normally the admin never chooses a password - one is
    always auto-generated and emailed to the address given here. A Viewer
    account is the one exception: it's set up directly by a Super Admin
    with a password of their choosing, no email required, and no welcome
    email sent (see the Viewer-specific rules noted inline below)."""
    import database
    import mailer

    _require_not_viewer(payload.created_by)
    name = (payload.username or "").strip()
    email = (payload.email or "").strip()
    type_name = next(
        (t["name"] for t in database.list_user_types() if t["id"] == payload.user_type_id), ""
    )
    is_viewer = type_name.strip().lower() == "viewer"

    if not name:
        raise HTTPException(status_code=400, detail="Username is required.")
    if is_viewer:
        # A Viewer's password is assigned directly by the Super Admin
        # creating it, not auto-generated and emailed - there's no welcome
        # email to send, so no email address is required either. The usual
        # complexity policy is skipped too: a Viewer account is commonly
        # set up with a simple, memorable convention (e.g. the employee
        # code as-is), and it's read-only with no email recovery path
        # anyway, unlike a self-service account's own password.
        if not payload.password:
            raise HTTPException(status_code=400, detail="A password is required for a Viewer account.")
    else:
        if not email:
            raise HTTPException(status_code=400, detail="Username and email are required.")
        if "@" not in email or "." not in email.split("@")[-1]:
            raise HTTPException(status_code=400, detail="Enter a valid email address.")

    try:
        # Viewer: the Super Admin's own chosen password (kept). Everyone else:
        # the initial password is simply the username - the user MUST replace
        # it at their first login (must_change_password), so it only ever
        # works once, until they set their own under the full policy.
        temp_password = database.create_user(
            name, payload.user_type_id, email or None, payload.created_by,
            password=payload.password if is_viewer else name,
            force_change=None if is_viewer else True,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if is_viewer:
        return {"ok": True, "email_sent": False, "email_error": None}

    created = database.get_user(name)
    user_type_name = (created or {}).get("UserTypeName", "")

    email_sent, email_error = True, None
    try:
        mailer.send_mail(
            email, "Your PIIPS account is ready",
            mailer.welcome_email_html(name, temp_password, _base_url(request), user_type_name),
        )
    except mailer.MailError as exc:
        email_sent, email_error = False, str(exc)

    return {"ok": True, **_email_result(email_sent, email_error)}


@app.post("/api/users/active")
def api_set_user_active(payload: UserActiveModel, request: Request):
    import database
    import mailer

    _require_not_viewer(payload.modified_by)
    try:
        user = database.get_user_by_id(payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    if not payload.is_active:
        if payload.modified_by == payload.user_id:
            raise HTTPException(
                status_code=403,
                detail="You can't deactivate your own account.",
            )
        if user and user["UserName"] == database.DEFAULT_SUPER_ADMIN_USERNAME:
            raise HTTPException(
                status_code=403,
                detail=f"'{database.DEFAULT_SUPER_ADMIN_USERNAME}' is the account guaranteed to "
                       "always be available and can't be deactivated.",
            )

    try:
        database.set_user_active(payload.user_id, payload.is_active, payload.modified_by)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    security.forget_user(payload.user_id)

    # A Viewer account is set up by a Super Admin directly (no email on
    # file is even required for one - see api_create_user), so there's no
    # activation/deactivation notice to send either.
    is_viewer = (user or {}).get("UserTypeName", "").strip().lower() == "viewer"

    email_sent, email_error = False, None
    if user and user.get("Email") and not is_viewer:
        html = (
            mailer.activation_email_html(user["UserName"], _base_url(request))
            if payload.is_active
            else mailer.deactivation_email_html(user["UserName"], _base_url(request))
        )
        try:
            mailer.send_mail(user["Email"], "Your PIIPS account status changed", html)
            email_sent = True
        except mailer.MailError as exc:
            email_error = str(exc)

    return {"ok": True, **_email_result(email_sent, email_error)}


@app.post("/api/users/reset-password")
def api_admin_reset_password(payload: UserResetPasswordModel, request: Request):
    """Admin-triggered password reset/assignment for another user (distinct
    from the self-service /api/forgot-password). A Super Admin may target
    anyone, including themselves. An Admin may target only plain User/
    Accounts accounts - never themselves, another Admin, or a Super Admin.
    `new_password` lets the caller assign a specific password directly
    (e.g. the User Management "Change Password" form); omitted, one is
    auto-generated (the per-row "Reset password" action). Either way the
    target must change it on next login, and it's emailed to them."""
    import database
    import mailer

    caller_role = database.get_user_role(payload.user_id)
    if not caller_role or not caller_role["active"] or caller_role["role"].lower() not in ("admin", "super admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    is_super_admin = caller_role["role"].lower() == "super admin"

    target = database.get_user_by_id(payload.target_user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    if not is_super_admin:
        target_role = database.get_user_role(payload.target_user_id)
        target_role_name = (target_role["role"] or "").lower() if target_role else ""
        if (payload.target_user_id == payload.user_id
                or target_role_name in ("admin", "super admin")):
            raise HTTPException(
                status_code=403,
                detail="Admins can only change the password of a regular User/Accounts account "
                       "- not their own, another Admin's, or a Super Admin's.",
            )

    target_is_viewer = (target.get("UserTypeName") or "").strip().lower() == "viewer"
    if payload.new_password:
        if is_super_admin:
            # A Super Admin may give ANY user any password of their choosing
            # (no complexity rules - just not empty); the user replaces it under
            # the normal policy at their next login.
            if not payload.new_password.strip() or len(payload.new_password) > 128:
                raise HTTPException(status_code=400, detail="Enter a password (1-128 characters).")
        else:
            try:
                database.validate_password_policy(payload.new_password)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc))
        new_password = payload.new_password
        initial = is_super_admin
    else:
        # Back to the initial credential: password = username, change forced
        # at the next login.
        new_password = target["UserName"]
        initial = True

    try:
        # A Viewer account is a fixed credential the Super Admin hands out (no
        # forced change, same as when it is created); everyone else must
        # change the password at next login.
        database.reset_password(target["UserName"], new_password,
                                force_change=not (is_super_admin and payload.new_password and target_is_viewer),
                                modified_by=payload.user_id, check_policy=not initial)
    except ValueError as exc:      # e.g. Sadmin's password is fixed
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    email_sent, email_error = False, None
    if target.get("Email"):
        try:
            mailer.send_mail(
                target["Email"], "Your PIIPS password was reset",
                mailer.password_reset_email_html(target["UserName"], new_password, _base_url(request)),
            )
            email_sent = True
        except mailer.MailError as exc:
            email_error = str(exc)

    return {"ok": True, **_email_result(email_sent, email_error)}


@app.post("/api/users/change-type")
def api_admin_change_user_type(payload: UserChangeTypeModel):
    """Admin-triggered role change for an existing user, mirroring
    /api/users/reset-password's exact permission rule: a Super Admin may
    retarget anyone (including themselves) to any role. An Admin may only
    retarget a plain User/Accounts account - never themselves, another
    Admin, or a Super Admin - AND may only assign them another plain
    User/Accounts role (never promote anyone to Admin/Super Admin)."""
    import database

    caller_role = database.get_user_role(payload.user_id)
    if not caller_role or not caller_role["active"] or caller_role["role"].lower() not in ("admin", "super admin"):
        raise HTTPException(status_code=403, detail="Admin access required.")
    is_super_admin = caller_role["role"].lower() == "super admin"

    target = database.get_user_by_id(payload.target_user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found.")

    types = {t["id"]: t["name"] for t in database.list_user_types()}
    new_type_name = (types.get(payload.new_user_type_id) or "").lower()
    if not new_type_name:
        raise HTTPException(status_code=400, detail="Unknown user type.")

    if not is_super_admin:
        target_role_name = (target.get("UserTypeName") or "").lower()
        if (payload.target_user_id == payload.user_id
                or target_role_name in ("admin", "super admin")):
            raise HTTPException(
                status_code=403,
                detail="Admins can only change the role of a regular User/Accounts account "
                       "- not their own, another Admin's, or a Super Admin's.",
            )
        if new_type_name in ("admin", "super admin"):
            raise HTTPException(
                status_code=403,
                detail="Admins can only assign the User or Accounts role - promoting to "
                       "Admin or Super Admin requires a Super Admin.",
            )

    try:
        database.set_user_type(payload.target_user_id, payload.new_user_type_id, payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    security.forget_user(payload.target_user_id)

    return {"ok": True, "user_type_name": types[payload.new_user_type_id]}


class MailSettingsModel(BaseModel):
    user_id: Optional[int] = None
    username: str
    email: str
    password: Optional[str] = None   # blank/None = keep existing
    smtp_host: str
    smtp_port: int


@app.get("/api/mail-settings")
def get_mail_settings_api(user_id: Optional[int] = None):
    import database
    _require_developer(user_id)
    s = database.get_mail_settings()
    if not s:
        return {"username": "", "email": "", "smtp_host": "", "smtp_port": 587, "password_set": False}
    return {
        "username": s["UserName"], "email": s["EmailID"],
        "smtp_host": s["SMTPHost"], "smtp_port": s["SMTPPort"],
        "password_set": bool(s["Password"]),
    }


@app.post("/api/mail-settings")
def save_mail_settings_api(payload: MailSettingsModel):
    import database
    import smtplib

    _require_developer(payload.user_id)

    username = (payload.username or "").strip()
    email = (payload.email or "").strip()
    host = (payload.smtp_host or "").strip()
    if not username or not email or not host or not payload.smtp_port:
        raise HTTPException(status_code=400, detail="Username, email, SMTP host and port are required.")

    password = payload.password
    current = database.get_mail_settings()
    test_password = password if password else (current["Password"] if current else "")
    if not test_password:
        raise HTTPException(status_code=400, detail="Password is required.")

    try:
        with smtplib.SMTP(host, int(payload.smtp_port), timeout=15) as smtp:
            smtp.starttls()
            smtp.login(email, test_password)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=400, detail=f"Could not connect: {exc}")

    database.save_mail_settings(username, email, password, host, payload.smtp_port)
    return {"ok": True}


# ==========================================================================
# Announcements (Super Admin only) - site-wide notices for every user
# ==========================================================================

ANNOUNCEMENT_MEDIA_DIR = os.path.join(BASE_DIR, "announcement_media")
os.makedirs(ANNOUNCEMENT_MEDIA_DIR, exist_ok=True)
_ANNOUNCEMENT_IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")


@app.get("/api/announcements/active")
def api_active_announcements():
    """Announcements every logged-in user should currently see - no role
    gate, any authenticated session can call this."""
    import database
    try:
        return {"announcements": database.get_active_announcements()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.get("/api/announcements")
def api_list_announcements(user_id: Optional[int] = None):
    """All announcements, including expired/stopped (Super Admin management view)."""
    import database
    _require_developer(user_id)
    try:
        return {"announcements": database.list_announcements()}
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")


@app.post("/api/announcements")
def api_create_announcement(
    request: Request,
    title: str = Form(...),
    body_text: str = Form(""),
    video_url: str = Form(""),
    end_datetime: str = Form(...),   # "YYYY-MM-DDTHH:MM" from <input type="datetime-local">
    user_id: Optional[int] = Form(None),
    image: Optional[UploadFile] = File(None),
):
    """Create a site-wide announcement (Super Admin only). Content is
    flexible - plain text, an optional uploaded image (a screenshot, a
    QR code, whatever), and/or an optional video URL, all shown together
    in the notification banner every user sees until end_datetime passes
    or a Super Admin stops it early."""
    import database

    # Multipart forms aren't inspected by the auth middleware - take the
    # acting user from the verified token instead of the client-sent field.
    user_id = request.scope["state"]["user_id"]
    _require_developer(user_id)

    title = title.strip()
    if not title:
        raise HTTPException(status_code=400, detail="Title is required.")
    if not end_datetime:
        raise HTTPException(status_code=400, detail="End date/time is required.")
    end_dt = end_datetime.replace("T", " ")
    if len(end_dt) == 16:   # "YYYY-MM-DD HH:MM" -> add seconds
        end_dt += ":00"

    image_path = None
    if image is not None and image.filename:
        ext = os.path.splitext(image.filename)[1].lower()
        if ext not in _ANNOUNCEMENT_IMAGE_EXTS:
            raise HTTPException(status_code=400, detail=f"Unsupported image type: {ext or '(none)'}")
        stored_name = f"{uuid.uuid4().hex}{ext}"
        dest = os.path.join(ANNOUNCEMENT_MEDIA_DIR, stored_name)
        with open(dest, "wb") as f:
            shutil.copyfileobj(image.file, f)
        image_path = stored_name

    try:
        new_id = database.create_announcement(
            title, body_text.strip() or None, image_path, video_url.strip() or None, end_dt, user_id,
        )
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {"ok": True, "id": new_id}


class AnnouncementStopModel(BaseModel):
    user_id: Optional[int] = None


@app.post("/api/announcements/{announcement_id}/stop")
def api_stop_announcement(announcement_id: int, payload: AnnouncementStopModel):
    """Stop/remove an announcement immediately (Super Admin only)."""
    import database
    _require_developer(payload.user_id)
    try:
        n = database.stop_announcement(announcement_id, payload.user_id)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")
    if not n:
        raise HTTPException(status_code=404, detail="Announcement not found.")
    return {"ok": True}


if os.path.isdir(ANNOUNCEMENT_MEDIA_DIR):
    app.mount(
        "/announcement_media",
        StaticFiles(directory=ANNOUNCEMENT_MEDIA_DIR),
        name="announcement_media",
    )


# ==========================================================================
# Static frontend (React build)
# ==========================================================================
# Serve the built React app at the site root. The API routes above are
# registered first, so they take precedence; every other path falls
# through to the SPA (html=True serves index.html for "/").

FRONTEND_DIST = os.path.join(BASE_DIR, "frontend", "dist")

if os.path.isdir(FRONTEND_DIST):
    app.mount(
        "/",
        StaticFiles(directory=FRONTEND_DIST, html=True),
        name="frontend",
    )