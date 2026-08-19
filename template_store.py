"""
Business templates for PIIPS.

A template is identified by an entity (PI / PT / PB), an invoice type
(PART / SERVICE) and a name (e.g. "Services_Chennai", "trichy/service"). It
holds a PO number format and the static/constant values written into each
Excel sheet's columns for that template.

On save, the folder  <pdf_folder>/<entity>/<invoice_type>/<name>  must
already exist (the name may contain "/" for nested folders); otherwise the
save is rejected.

Stored in SQL Server (tbl_Template + tbl_TemplateStaticValue). The in-memory
shape used by the API and the processor is unchanged:

    { "Static_Values": { "PT\\PART\\Services_Chennai": {
        "PO_Number_Format": "PO-2627-",
        "Purchase Header": { "Document Type": "Order", ... },
        "Purchase Line":   { ... },
        "Reservation Entry": { ... }
    } } }
"""

import os

import config_store


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

ENTITIES = ["PI", "PT", "PB"]

INVOICE_TYPES = ["PART", "SERVICE"]

SHEETS = ["Purchase Header", "Purchase Line", "Reservation Entry"]


def load():
    """{"Static_Values": {key: entry}} for active templates, from the DB.
    Returns an empty set if the database is unavailable."""
    try:
        import database
        return {"Static_Values": database.get_templates_data()}
    except Exception:  # noqa: BLE001 - DB optional at read time
        return {"Static_Values": {}}


def folder_for(entity, invoice_type, name):
    """
    Absolute folder path for a template (under the Input folder), and the
    configured Input base.
    e.g. <folder_path>/Input/<entity>/<invoice_type>/<name>.
    """
    folders = config_store.folders(create=False)
    base = folders.get("input", "") if folders else ""
    parts = [p for p in name.replace("\\", "/").split("/") if p]
    folder = os.path.join(base, entity, invoice_type, *parts) if base else ""
    return folder, base


def save_template(entity, invoice_type, name, po_format, static, user_id=None):
    entity = (entity or "").strip()
    invoice_type = (invoice_type or "").strip()
    name = (name or "").strip().strip("/\\ ")

    if entity not in ENTITIES:
        raise ValueError(f"Entity must be one of {', '.join(ENTITIES)}.")
    if invoice_type not in INVOICE_TYPES:
        raise ValueError(f"Invoice type must be one of {', '.join(INVOICE_TYPES)}.")
    if not name:
        raise ValueError("Template name is required.")

    folder, base = folder_for(entity, invoice_type, name)
    if not base:
        raise ValueError("Folder Path is not configured. Save it under Configuration first.")
    # Create the template's folder under Input (<Input>/<entity>/<invoice_type>/<name>).
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError as exc:
        raise ValueError(f"Could not create the template folder: {folder} ({exc})")

    key = f"{entity}\\{invoice_type}\\{name}"

    clean_static = {
        sheet: {k: v for k, v in (static or {}).get(sheet, {}).items() if k}
        for sheet in SHEETS
    }

    import database
    database.save_template(entity, name, key, po_format or "", clean_static, user_id, invoice_type)
    return key, folder


def entity_type_name_from_relpath(rel):
    """
    Parse (entity, invoice_type, name) from a path already relative to the
    Input root, e.g. "PT/PART/Services_Chennai/inv1.pdf" (same shape as
    tbl_InputFile_Log.RelPath). Returns (None, None, None) if not resolvable
    (e.g. fewer than 4 path segments) — independent of whether a template is
    actually defined for that path.
    """
    if not rel:
        return None, None, None
    parts = [p for p in rel.replace("\\", "/").split("/") if p]
    if len(parts) < 4:            # need entity / invoice_type / name / file
        return None, None, None

    entity, invoice_type = parts[0], parts[1]
    name = "/".join(parts[2:-1])   # everything between invoice_type and filename
    return entity, invoice_type, name


def _split_path(input_folder, pdf_path):
    """
    Parse (entity, invoice_type, name) from a PDF located under
    <input_folder>/<entity>/<invoice_type>/<name>/<file>. Returns
    (None, None, None) if not resolvable — independent of whether a
    template is actually defined for that path.
    """
    if not input_folder:
        return None, None, None
    try:
        rel = os.path.relpath(pdf_path, input_folder)
    except ValueError:
        return None, None, None
    return entity_type_name_from_relpath(rel)


def invoice_type_for_path(input_folder, pdf_path):
    """Invoice type (PART/SERVICE) parsed directly from a PDF's path, or
    None if it can't be resolved."""
    return _split_path(input_folder, pdf_path)[1]


def static_for_path(input_folder, pdf_path):
    """
    Given a PDF located under <input_folder>/<entity>/<invoice_type>/<name>/...,
    return (static_entry, key) for that template, or ({}, None) if the PDF
    is not inside an entity/invoice_type/template subfolder or the template
    isn't defined.
    """
    entity, invoice_type, name = _split_path(input_folder, pdf_path)
    if entity is None:
        return {}, None

    key = f"{entity}\\{invoice_type}\\{name}"
    entry = load()["Static_Values"].get(key)
    return (entry or {}), (key if entry else None)


def delete_template(key, user_id=None):
    """Soft-delete (deactivate) a template. Returns True if one was found."""
    import database
    return database.delete_template(key, user_id)
