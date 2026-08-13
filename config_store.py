"""
Simple JSON-file backed configuration store for PIIPS.

Holds the single working Folder Path (the base for Input, its per-template
subfolders, New_Format and the extracted output) and the SQL Server
connection string.
"""

import json
import os
import re
import shutil
import traceback
import xml.etree.ElementTree as ET

import secret_store


BASE_DIR = os.path.dirname(os.path.abspath(__file__))

CONFIG_FILE = os.path.join(BASE_DIR, "config.json")

WEBCONFIG_FILE = os.path.join(BASE_DIR, "web.config")


DEFAULT_CONFIG = {
    # Single working folder. Input (and its per-template subfolders),
    # New_Format and the extracted output all live under here.
    "folder_path": "",
    # SQL Server connection string. Kept in config.json (git-ignored),
    # never hard-coded here, so the credential is not committed to source.
    "db_connection": "",
    # Base URL of the Service First / NAV backend that supplies reservation
    # (spare-purchase) and HSN details. e.g. "http://10.0.1.x:port".
    "sf_api_url": "",
}

# Legacy keys that used to hold folder paths; migrated into folder_path.
_LEGACY_FOLDER_KEYS = ("output_folder", "pdf_folder")


# ---------------------------------------------------------------------------
# web.config <appSettings> holds the Folder Path (authoritative when present)
# ---------------------------------------------------------------------------

def webconfig_get(key):
    """Read an <appSettings> value from web.config, or '' if absent."""
    if not os.path.exists(WEBCONFIG_FILE):
        return ""
    try:
        root = ET.parse(WEBCONFIG_FILE).getroot()
    except ET.ParseError:
        return ""
    for add in root.findall("./appSettings/add"):
        if add.get("key") == key:
            return add.get("value") or ""
    return ""


def webconfig_set(key, value):
    """Set an <appSettings> value in web.config, preserving the rest."""
    if not os.path.exists(WEBCONFIG_FILE):
        return
    try:
        tree = ET.parse(WEBCONFIG_FILE)
        root = tree.getroot()
    except ET.ParseError:
        return

    appsettings = root.find("appSettings")
    if appsettings is None:
        appsettings = ET.SubElement(root, "appSettings")

    add = next(
        (a for a in appsettings.findall("add") if a.get("key") == key),
        None,
    )
    if add is None:
        add = ET.SubElement(appsettings, "add")
        add.set("key", key)
    add.set("value", value)

    try:
        ET.indent(tree, space="  ")
    except AttributeError:
        pass
    tree.write(WEBCONFIG_FILE, encoding="utf-8", xml_declaration=True)


def load_config():
    """Return the saved configuration merged over the defaults. The Folder
    Path comes from web.config when present (authoritative)."""

    config = dict(DEFAULT_CONFIG)

    if os.path.exists(CONFIG_FILE):

        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as file:
                saved = json.load(file)

            if isinstance(saved, dict):
                config.update(saved)

        except (json.JSONDecodeError, OSError):
            # Corrupt / unreadable file -> fall back to defaults
            pass

    # Migrate the old two-folder config (output_folder / pdf_folder) into the
    # single folder_path, then drop the legacy keys.
    if not (config.get("folder_path") or "").strip():
        for key in _LEGACY_FOLDER_KEYS:
            if (config.get(key) or "").strip():
                config["folder_path"] = config[key]
                break
    for key in _LEGACY_FOLDER_KEYS:
        config.pop(key, None)

    # web.config wins for the Folder Path
    wc_path = webconfig_get("folder_path") or webconfig_get("pdf_folder")
    if wc_path:
        config["folder_path"] = wc_path

    # db_connection is stored encrypted at rest (DPAPI); hand callers the
    # decrypted value. A decryption failure is treated as "not configured"
    # rather than crashing the app.
    try:
        config["db_connection"] = secret_store.unprotect(config.get("db_connection") or "")
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        config["db_connection"] = ""

    return config


def save_config(updates):
    """Merge `updates` into the current config and persist it. The Folder
    Path is also written to web.config."""

    config = load_config()

    for key, value in (updates or {}).items():
        if value is not None:
            config[key] = value

    # Persist with the connection string encrypted at rest; keep the
    # in-memory copy plaintext for the caller.
    on_disk = dict(config)
    on_disk["db_connection"] = secret_store.protect(config.get("db_connection") or "")

    with open(CONFIG_FILE, "w", encoding="utf-8") as file:
        json.dump(on_disk, file, indent=4, ensure_ascii=False)

    if updates and updates.get("folder_path"):
        webconfig_set("folder_path", updates["folder_path"])

    return config


def ensure_secret_encrypted():
    """One-time migration: if config.json holds a plaintext db_connection,
    rewrite that single field encrypted (DPAPI). Idempotent — a value that is
    already protected is left untouched. Only that field is rewritten so the
    rest of the file is preserved verbatim."""
    if not os.path.exists(CONFIG_FILE):
        return
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as file:
            raw = json.load(file)
    except (json.JSONDecodeError, OSError):
        return

    dbc = raw.get("db_connection") or ""
    if dbc and not secret_store.is_protected(dbc):
        raw["db_connection"] = secret_store.protect(dbc)
        with open(CONFIG_FILE, "w", encoding="utf-8") as file:
            json.dump(raw, file, indent=4, ensure_ascii=False)


def folders(create=True):
    """
    Resolve the working folders under the single Folder Path:

        <folder_path>/Input        PDFs to process (Dashboard -> Start), with
                                   per-template subfolders <entity>/<name>
        <folder_path>/New_Format   PDFs to train (Training -> Train), and where
                                   Start moves PDFs of untrained formats
        <folder_path>              extracted JSON / Excel output

    Returns {} if the Folder Path is not configured. Creates Input and
    New_Format when `create` is True.
    """

    config = load_config()
    base = (config.get("folder_path") or "").strip()

    if not base:
        return {}

    result = {
        "base": base,
        "input": os.path.join(base, "Input"),
        "new_format": os.path.join(base, "New_Format"),
        # Server-local New_Format (next to the model file). This is the
        # training source; Start also mirrors unrecognized PDFs here.
        "server_new_format": os.path.join(BASE_DIR, "New_Format"),
        # Dedicated Output folder; extracted JSON is written under here
        # mirroring the Input template structure (<entity>/<name>).
        "output": os.path.join(base, "Output"),
    }

    # Trained PDFs are archived here after a successful training run.
    result["trained_format"] = os.path.join(base, "Trained_format")

    if create:
        # Server New_Format lives with the app; create it regardless.
        os.makedirs(result["server_new_format"], exist_ok=True)
        if os.path.isdir(base):
            os.makedirs(result["input"], exist_ok=True)
            os.makedirs(result["new_format"], exist_ok=True)
            os.makedirs(result["output"], exist_ok=True)
            os.makedirs(result["trained_format"], exist_ok=True)

    return result


# ---------------------------------------------------------------------------
# Per-status folders (siblings of Input under the Folder Path)
# ---------------------------------------------------------------------------

# Reserved subfolders under the Folder Path that are NOT invoice-status folders
# and must never be searched/overwritten when relocating a processed PDF.
_RESERVED_SUBDIRS = {"Input", "New_Format", "Output", "Trained_format"}


def _safe_folder(name):
    """A filesystem-safe folder name for a status value."""
    return re.sub(r'[<>:"/\\|?*]+', "_", str(name or "")).strip(" .") or "Unknown"


def status_folder(status_name, create=False):
    """Path of the folder for a tracker status, a sibling of Input under the
    Folder Path (e.g. <folder_path>/Ready to Load). '' if not configured."""
    base = (load_config().get("folder_path") or "").strip()
    if not base:
        return ""
    path = os.path.join(base, _safe_folder(status_name))
    if create:
        try:
            os.makedirs(path, exist_ok=True)
        except OSError:
            pass
    return path


def ensure_status_folders(status_names):
    """Create a folder for each status value; returns {status: path}."""
    return {s: status_folder(s, create=True) for s in (status_names or [])}


def find_pdf(filename):
    """Locate a PDF/image by name anywhere under the Folder Path (Input or any
    status folder), skipping Output/New_Format/Trained_format. '' if missing."""
    base = (load_config().get("folder_path") or "").strip()
    if not base or not filename:
        return ""
    filename = os.path.basename(filename)
    skip = {"New_Format", "Output", "Trained_format"}
    for root, dirs, files in os.walk(base):
        if root == base:
            dirs[:] = [d for d in dirs if d not in skip]
        if filename in files:
            return os.path.join(root, filename)
    return ""


def move_pdf_to_status(filename, status_name, src_path=None):
    """
    Move a processed PDF into its status folder under the Folder Path.

    If `src_path` is given and exists it is moved directly; otherwise the file
    is located by name across the Folder Path (skipping the Input/Output/
    New_Format/Trained_format reserved subfolders and the other status
    folders). Returns the destination path, or "" if nothing was moved.
    """
    base = (load_config().get("folder_path") or "").strip()
    if not base or not filename:
        return ""
    target = status_folder(status_name, create=True)
    if not target:
        return ""

    src = src_path if (src_path and os.path.isfile(src_path)) else None
    if not src:
        skip = {"New_Format", "Output", "Trained_format"}
        for root, dirs, files in os.walk(base):
            if root == base:  # don't descend into extraction/training folders
                dirs[:] = [d for d in dirs if d not in skip]
            if os.path.abspath(root) == os.path.abspath(target):
                continue
            if filename in files:
                src = os.path.join(root, filename)
                break

    if not src or not os.path.isfile(src):
        return ""
    dest = os.path.join(target, filename)
    if os.path.abspath(src) == os.path.abspath(dest):
        return dest
    try:
        if os.path.exists(dest):
            os.remove(dest)
        shutil.move(src, dest)
    except OSError:
        return ""
    return dest
