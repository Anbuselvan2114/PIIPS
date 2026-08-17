"""
Background invoice-processing jobs for PIIPS.

A job scans the configured PDF folder, runs each PDF through the OCR
engine, maps the result into the downstream invoice schema and writes
one <pdf>.json per file. Progress is tracked in memory so the frontend
can poll it and drive a live progress bar.

Processing runs on a worker thread (PaddleOCR is CPU bound and not
thread-safe, so only one job runs at a time).
"""

import json
import os
import shutil
import threading
import traceback
import uuid
from datetime import datetime

from ocr_engine import OCREngine
from invoice_schema import build_invoice_json, resolve_payment_terms, add_days_to_date
from format_model import FormatModel
import template_store
import service_api


class ProcessingJob:

    def __init__(
        self,
        job_id,
        source_folder,
        output_folder,
        unknown_folder="",
        mirror_folder="",
        trained_folder="",
        mode="process",
        started_by=None,
    ):

        self.job_id = job_id
        self.source_folder = source_folder
        self.output_folder = output_folder
        self.unknown_folder = unknown_folder
        self.mirror_folder = mirror_folder   # server New_Format (training src)
        self.trained_folder = trained_folder  # archive after training
        self.mode = mode                 # process | train
        self.export_file = ""            # generated Excel (process mode)
        self.db_saved = {}               # {table: rows_inserted} (process mode)
        self.batch_name = ""             # batch saved to the DB (process mode)
        self._doc_seq = 0                # running Document No. sequence
        # Purchase-tracker attribution: who clicked Start, and when.
        self.started_by = started_by
        self.started_at = datetime.now()

        self.status = "pending"          # pending | running | completed | failed
        self.total = 0
        self.processed = 0
        self.current_file = ""
        self.stage = ""                  # Extracting | Syncing | Processing (per file)
        self.error = None
        self.results = []                # per-file result records
        self.errors = []                 # validation errors (shown on dashboard)

        self._lock = threading.Lock()

    # -- progress snapshots ------------------------------------------------

    def _percent(self):
        if self.total <= 0:
            return 0
        return round(self.processed / self.total * 100)

    def status_dict(self):
        """Lightweight snapshot for polling (no extracted JSON payload).
        Sample: job.status_dict()"""

        with self._lock:
            return {
                "job_id": self.job_id,
                "mode": self.mode,
                "status": self.status,
                "total": self.total,
                "processed": self.processed,
                "percent": self._percent(),
                "current_file": self.current_file,
                "stage": self.stage,
                "error": self.error,
                "export_file": self.export_file,
                "batch_name": self.batch_name,
                "results": [
                    {
                        "file": r["file"],
                        "status": r["status"],
                        "error": r.get("error"),
                        "output": r.get("output"),
                        "format": r.get("format"),
                        "format_status": r.get("format_status"),
                        "moved_to": r.get("moved_to"),
                        "batch": r.get("batch"),
                        "reason": r.get("reason"),
                        "existing_batch": r.get("existing_batch"),
                    }
                    for r in self.results
                ],
                "errors": list(self.errors),
            }

    def result_dict(self):
        """Full snapshot including each file's extracted JSON.
        Sample: job.result_dict()"""

        with self._lock:
            return {
                "job_id": self.job_id,
                "status": self.status,
                "total": self.total,
                "processed": self.processed,
                "output_folder": self.output_folder,
                "results": list(self.results),
            }


class JobManager:

    def __init__(self):
        self._jobs = {}
        self._active = {}          # mode -> job_id (one active job per mode)
        self._lock = threading.Lock()

    def get(self, job_id):
        """Return the job with this id, or None. Sample: job_manager.get('a1b2c3d4e5f6')"""
        return self._jobs.get(job_id)

    def active_job(self, mode=None):
        """Active job for a given mode, or any running job if mode is None.
        Sample: job_manager.active_job('process')"""
        with self._lock:
            if mode:
                jid = self._active.get(mode)
                return self._jobs.get(jid) if jid else None
            for jid in self._active.values():
                job = self._jobs.get(jid)
                if job and job.status in ("pending", "running"):
                    return job
        return None

    def start(
        self,
        source_folder,
        output_folder,
        unknown_folder="",
        mirror_folder="",
        trained_folder="",
        mode="process",
        started_by=None,
    ):
        """
        Create and launch a job. A train job and a process job may run at
        the same time, but not two of the same mode. Returns
        (job, error_message).

        Sample: job_manager.start('D:/PIIPS/Input', 'D:/PIIPS/Output', unknown_folder='D:/PIIPS/New_Format', mode='process', started_by=7)
        """

        with self._lock:

            current = self._jobs.get(self._active.get(mode))

            if current and current.status in ("pending", "running"):
                return None, f"A {mode} job is already running. Please wait for it to finish."

            job_id = uuid.uuid4().hex[:12]
            job = ProcessingJob(
                job_id,
                source_folder,
                output_folder,
                unknown_folder,
                mirror_folder,
                trained_folder,
                mode,
                started_by=started_by,
            )

            self._jobs[job_id] = job
            self._active[mode] = job_id

        thread = threading.Thread(
            target=self._run,
            args=(job,),
            daemon=True,
        )
        thread.start()

        return job, None

    # -- worker ------------------------------------------------------------

    def _run(self, job):

        with job._lock:
            job.status = "running"

        try:

            accepted = (".pdf",) + OCREngine.IMAGE_EXTS

            # Process mode scans Input recursively (PDFs live in
            # <Input>/<entity>/<template>/ subfolders); train mode scans the
            # flat New_Format folder.
            source_files = []
            if job.mode == "process":
                for root, _dirs, files in os.walk(job.source_folder):
                    for f in files:
                        if f.lower().endswith(accepted):
                            source_files.append(os.path.join(root, f))
            else:
                for f in os.listdir(job.source_folder):
                    if f.lower().endswith(accepted):
                        source_files.append(os.path.join(job.source_folder, f))
            source_files.sort()

            with job._lock:
                job.total = len(source_files)

            os.makedirs(job.output_folder, exist_ok=True)
            if job.unknown_folder:
                os.makedirs(job.unknown_folder, exist_ok=True)

            # Process mode: assign this run's batch name and load the set of
            # invoices already saved in earlier batches (for de-duplication).
            invoice_map = {}
            if job.mode == "process":
                job.batch_name = "PIIPS_Batch_" + datetime.now().strftime("%Y%m%d_%H%M%S")
                try:
                    import config_store
                    if (config_store.load_config().get("db_connection") or "").strip():
                        import database
                        invoice_map = database.get_processed_invoices()
                except Exception:  # noqa: BLE001
                    traceback.print_exc()

            ocr = OCREngine()
            fmt_model = FormatModel()

            for src_path in source_files:

                filename = os.path.basename(src_path)

                with job._lock:
                    job.current_file = filename
                    job.stage = "Extracting"

                try:

                    ocr_result = ocr.read_pdf(src_path)

                    if job.mode == "train":

                        # Learn in memory only; the model is written once at
                        # the end so training updates format_model.json
                        # atomically (a failed run leaves it untouched).
                        fmt_name, learn_status = fmt_model.learn(
                            ocr_result,
                            filename,
                            persist=False,
                        )

                        records = [{
                            "file": filename,
                            "status": "success",
                            "format": fmt_name,
                            "format_status": learn_status,   # learned | exists
                        }]

                    else:

                        # Predict against trained formats (matched once per
                        # file, off its primary/first invoice — a PDF with
                        # multiple invoices is assumed to be one vendor).
                        fmt_name, score = fmt_model.match(ocr_result)

                        # Most PDFs contain exactly one invoice; some (e.g. a
                        # batch of service-call reports saved as one file)
                        # contain several distinct ones — ocr_engine already
                        # split those into separate groups. Process each as
                        # its own invoice. An unrecognized format (fmt_name
                        # is None) still goes through this same loop rather
                        # than being parked with no DB record at all — it's
                        # saved with its own NEW TEMPLATE status below so
                        # it's visible/trackable, while still being copied
                        # to New_Format for training exactly as before.
                        invoice_groups = ocr_result.get("Invoices") or [ocr_result]
                        multi = len(invoice_groups) > 1
                        rel = os.path.relpath(src_path, job.source_folder).replace("\\", "/")
                        records = []

                        for group_idx, invoice_ocr in enumerate(invoice_groups):

                            data = build_invoice_json(invoice_ocr, src_path)

                            data["Template_Name"] = fmt_name
                            data["Template"] = fmt_name

                            # GST Vendor Type is derived, not extracted:
                            # a seller with a readable GSTIN is GST
                            # Registered, otherwise Unregistered.
                            data["GST_Vendor_Type"] = (
                                "Registered" if (data.get("seller_gstin") or "").strip()
                                else "UnRegistered"
                            )

                            inv_no = (data.get("invoice_no") or "").strip()

                            # Skip if this invoice is already saved (any
                            # batch) - matched by the extracted Vendor
                            # Invoice No., not the PDF's filename, so a
                            # re-upload under a different filename is
                            # still caught. Moved out of Input so it
                            # doesn't get picked up and OCR'd again on
                            # every future run.
                            if inv_no in invoice_map:
                                moved_to = (
                                    None if multi
                                    else config_store.move_pdf_to_status(
                                        filename, "DUPLICATE", src_path=src_path
                                    )
                                )
                                if job.mirror_folder and moved_to:
                                    self._copy(moved_to, job.mirror_folder)
                                records.append({
                                    "file": filename,
                                    "rel": rel,
                                    "status": "skipped",
                                    "format": fmt_name,
                                    "format_status": "already_processed",
                                    "reason": (
                                        f"Invoice {inv_no} already processed → moved to DUPLICATE"
                                        if moved_to else
                                        f"Invoice {inv_no} already processed"
                                    ),
                                    "existing_batch": invoice_map[inv_no],
                                    "moved_to": moved_to,
                                })
                                continue

                            # Static values from the business template chosen
                            # by the PDF's path: <Input>/<entity>/<name>/...
                            static, tkey = template_store.static_for_path(
                                job.source_folder, src_path
                            )
                            invoice_type = template_store.invoice_type_for_path(
                                job.source_folder, src_path
                            )

                            # ---- BC relationship keys (Header/Line/Reservation) ----
                            job._doc_seq += 1
                            po_fmt = static.get("PO_Number_Format", "") or ""
                            doc_type = (
                                static.get("Purchase Header", {}).get("Document Type")
                                or static.get("Purchase Line", {}).get("Document Type")
                                or "Order"
                            )
                            doc_no = (
                                f"{po_fmt}{job._doc_seq:06d}" if po_fmt
                                else (data.get("invoice_no") or f"DOC{job._doc_seq:06d}")
                            )
                            data["Document Type"] = doc_type
                            data["Document No."] = doc_no
                            # Kept separately (not just parsed back out of
                            # Document No.) so a Dashboard renumber later
                            # can rebuild "prefix + new sequence" even for
                            # a header whose invoice_no fallback doesn't
                            # look like "PREFIX000123" at all.
                            data["PO_Number_Format"] = po_fmt

                            # ---- Service First: reservation (sf_items) +
                            # HSN enrichment + validation. Sets data["sf_items"]
                            # and vendor/header fields; returns the tracker
                            # verdict (status / is_active / is_synced / reason).
                            with job._lock:
                                job.stage = "Syncing"
                            if fmt_name is None or not inv_no:
                                # Either the format itself is unrecognized,
                                # or no Vendor Invoice No. could be read off
                                # this PDF at all - neither leaves anything
                                # trustworthy to hand to Service First or to
                                # generate a real Document No. from. Park it
                                # as NEW TEMPLATE instead of guessing;
                                # new_format=True below still triggers the
                                # same New_Format training-copy as the
                                # per-item "SF doesn't know this part" case.
                                reason = (
                                    "Unrecognized invoice format — needs training"
                                    if fmt_name is None else
                                    "Vendor Invoice No. not extracted — needs training"
                                )
                                verdict = {"status": "NEW TEMPLATE", "is_active": False,
                                           "is_synced": False, "new_format": True,
                                           "reason": reason}
                            elif invoice_type == "SERVICE":
                                # SERVICE invoices never carry a Buyer Order
                                # No. and have no Service First / reservation
                                # concept — skip the SF calls and the
                                # PO-driven verdict logic; ready for Load as-is.
                                verdict = {"status": "READY TO LOAD", "is_active": True, "is_synced": False}
                            else:
                                verdict = service_api.enrich_invoice(data)

                            # SF's item-master lookup (GetHSNDetails) never
                            # recognized a line's description at all — the
                            # invoice still saves as NEW TEMPLATE below,
                            # but also copy the source PDF into New_Format
                            # so it shows up on the Training screen.
                            if verdict.get("new_format") and job.unknown_folder:
                                try:
                                    self._copy(src_path, job.unknown_folder)
                                    if job.mirror_folder:
                                        self._copy(src_path, job.mirror_folder)
                                except Exception:  # noqa: BLE001 - best-effort
                                    traceback.print_exc()

                            # ---- Payment Terms Code / Due Date ----
                            # data["PaymentTermsName"] comes from Service
                            # First (GRN only, when SF returned rows);
                            # data["payment_terms_code"] already holds
                            # whatever the PDF itself states (Mode/Terms
                            # of Payment), set earlier in
                            # build_invoice_json. Resolve the final code
                            # + day count per the business rule, then
                            # derive Due Date from Document Date.
                            terms_code, terms_days = resolve_payment_terms(
                                data.get("PaymentTermsName", ""),
                                data.get("payment_terms_code", ""),
                            )
                            data["payment_terms_code"] = terms_code
                            data["Due_Date"] = add_days_to_date(
                                data.get("invoice_date", ""), terms_days
                            )

                            for it in data.get("items", []):
                                line_no = it.get("Line_No", "")
                                it["Document Type"] = doc_type
                                it["Document No."] = doc_no
                                it["Source Type"] = "39"
                                it["Source Subtype"] = doc_type
                                it["Source ID"] = doc_no
                                it["Source Ref. No."] = line_no

                            # Write the JSON into Output mirroring the Input
                            # template structure: <Output>/<entity>/<name>/<file>.json
                            # A file split into multiple invoices gets a
                            # suffixed name for the 2nd+ one so they don't
                            # overwrite each other.
                            rel_dir_parts = [p for p in os.path.dirname(rel).split("/") if p]
                            out_dir = os.path.join(job.output_folder, *rel_dir_parts)
                            os.makedirs(out_dir, exist_ok=True)
                            suffix = f"_{group_idx + 1}" if multi else ""
                            out_path = os.path.join(
                                out_dir,
                                os.path.splitext(filename)[0] + suffix + ".json",
                            )

                            with open(out_path, "w", encoding="utf-8") as fp:
                                json.dump(data, fp, indent=4, ensure_ascii=False)

                            # Attach static after writing the clean JSON so
                            # these internal keys stay out of the per-PDF file.
                            data["_static"] = static
                            data["_template_key"] = tkey
                            # So excel_export._link_override can special-case
                            # PART invoices' freight/charge lines without a
                            # separate lookup.
                            data["_invoice_type"] = invoice_type

                            rec = {
                                "file": filename,
                                # Path relative to the Input root, matching
                                # the upload log's RelPath so the tracker can
                                # resolve who initiated (uploaded) this file.
                                "rel": rel,
                                "status": "success",
                                "output": out_path,
                                "format": fmt_name,
                                "format_status": "matched" if fmt_name else "unmatched",
                                "batch": job.batch_name,
                                "data": data,
                                # Service First verdict -> per-invoice tracker
                                # status / IsActive / IsSynced, applied in
                                # _save_to_db.
                                "_status": verdict["status"],
                                "_isactive": verdict["is_active"],
                                "_synced": verdict.get("is_synced", False),
                                "_invoice_type": invoice_type,
                            }
                            if verdict.get("reason"):
                                rec["reason"] = verdict["reason"]
                            records.append(rec)

                            # Mark this invoice seen so a duplicate later in
                            # the same run is also skipped.
                            if inv_no:
                                invoice_map[inv_no] = job.batch_name

                except Exception as exc:  # noqa: BLE001 - report, keep going

                    traceback.print_exc()

                    moved_to = ""
                    if job.mode == "process":
                        # The file couldn't even be read (corrupt/unsupported
                        # PDF/image) — move it out of Input so it doesn't
                        # keep clogging the INITIATED queue and get retried
                        # forever on every future run.
                        try:
                            import config_store
                            moved_to = config_store.move_pdf_to_status(
                                filename, "UNSUPPORTED", src_path=src_path
                            )
                        except Exception:  # noqa: BLE001 - file move is best-effort
                            traceback.print_exc()

                    records = [{
                        "file": filename,
                        "status": "error",
                        "error": str(exc),
                        "moved_to": moved_to,
                    }]

                with job._lock:
                    for record in records:
                        job.results.append(record)
                    job.processed += 1

            # Commit the learned model once, only after a full training run.
            if job.mode == "train":
                fmt_model.save()

                # Archive the trained PDFs: move each file that was actually
                # learned/merged (not the no-GSTIN failures) from New_Format
                # to the Trained_format folder.
                if job.trained_folder:
                    for r in job.results:
                        if r.get("status") != "success" or not r.get("format"):
                            continue
                        moved = self._move(
                            os.path.join(job.source_folder, r["file"]),
                            job.trained_folder,
                        )
                        r["moved_to"] = moved

            # After a processing run, save the rows into the database as a
            # named batch. The Excel is generated on demand at download time.
            if job.mode == "process":
                with job._lock:
                    job.stage = "Processing"
                self._save_to_db(job)
                self._reset_moved_files(job)
                self._resync_pending(job)
                self._move_by_status(job)

            with job._lock:
                job.status = "completed"
                job.current_file = ""
                job.stage = ""

        except Exception as exc:  # noqa: BLE001 - fatal job error

            traceback.print_exc()

            with job._lock:
                job.status = "failed"
                job.error = str(exc)

    @staticmethod
    def _move(src_path, dest_folder):
        """Move a file into dest_folder, overwriting a same-named target."""

        if not dest_folder:
            return ""

        os.makedirs(dest_folder, exist_ok=True)

        dest = os.path.join(dest_folder, os.path.basename(src_path))

        if os.path.abspath(src_path) == os.path.abspath(dest):
            return dest

        if os.path.exists(dest):
            os.remove(dest)

        shutil.move(src_path, dest)
        return dest

    @staticmethod
    def _copy(src_path, dest_folder):
        """Copy a file into dest_folder, overwriting a same-named target."""

        if not dest_folder or not src_path:
            return ""

        os.makedirs(dest_folder, exist_ok=True)

        dest = os.path.join(dest_folder, os.path.basename(src_path))

        if os.path.abspath(src_path) == os.path.abspath(dest):
            return dest

        shutil.copy2(src_path, dest)
        return dest

    @staticmethod
    def _save_to_db(job):
        """Save each sheet's rows (mapped + static) into the DB tables.
        Best-effort: a DB/connection failure does not fail the run."""
        try:
            import config_store
            if not (config_store.load_config().get("db_connection") or "").strip():
                return

            import excel_export
            import database

            matched = [
                r for r in job.results
                if r.get("status") == "success" and r.get("data")
            ]
            if not matched:
                return

            invoices = [r["data"] for r in matched]
            grouped = excel_export.build_rows_grouped(invoices)

            # Purchase tracker: one row per header. InitiatedBy comes from
            # whoever uploaded each file (resolved from the input-file log by
            # relative path); StartedBy/At is who clicked Start; status is
            # 'processed'. `initiators` is aligned with `matched` (== groups).
            rels = [r.get("rel") or r.get("file") for r in matched]
            init_map = database.get_initiators(rels)
            initiators = [
                init_map.get(rel, {"id": None, "at": None}) for rel in rels
            ]
            tracker = {
                "started_by": job.started_by,
                "started_at": job.started_at.isoformat() if job.started_at else None,
                "status": "EXTRACTED",
                "initiators": initiators,
                "rels": rels,
                # Per-invoice Service First verdict (aligned with `matched`).
                "statuses": [r.get("_status", "EXTRACTED") for r in matched],
                "isactives": [r.get("_isactive", True) for r in matched],
                # IsSynced = the part was received in Service First (reservation).
                "synced": [bool(r.get("_synced")) for r in matched],
                "buyer_orders": [(r.get("data") or {}).get("buyer_order_no") for r in matched],
                "jsons": [r.get("output") for r in matched],
                # File name and matched format/template, stored on the tracker.
                "filenames": [r.get("file") for r in matched],
                "formats": [r.get("format") for r in matched],
                "invoice_types": [r.get("_invoice_type") for r in matched],
            }

            # Final data-completeness gate, checked for every invoice except
            # BUYER ORDER NO DOESN'T EXIST (that one has its own manual-entry
            # workflow — Buyer Order Entry — and can't have real Reservation
            # Entry data without a PO to look up in SF in the first place)
            # and NEW TEMPLATE (unrecognized format — nothing to check until
            # it's trained):
            #   - all mandatory Header/Line/Reservation fields filled ->
            #     READY TO LOAD, regardless of what Service First said
            #     (PENDING IN SF / DATA MISMATCH verdicts are provisional,
            #     not final, until the data itself is checked).
            #   - anything missing -> DATA MISMATCH, with exactly which
            #     field(s).
            for i, group in enumerate(grouped["groups"]):
                if tracker["statuses"][i] in ("BUYER ORDER NO DOESN'T EXIST", "NEW TEMPLATE"):
                    continue
                # InvoiceNo is a mandatory column but lives outside the
                # sheet-mapped header row (it's stored separately — see
                # save_grouped/usp_SaveInvoiceBatch), so check it alongside.
                header_for_check = dict(group["header"])
                header_for_check["InvoiceNo"] = group.get("invoice_no", "")
                missing = excel_export.missing_required_fields(
                    header_for_check, group["lines"], group["reservations"]
                )
                if missing:
                    tracker["statuses"][i] = "DATA MISMATCH"
                    tracker["isactives"][i] = False
                    # Keep a more specific reason Service First already gave
                    # (e.g. "the pdf is a new format", "Nav item No not
                    # mapped in SF") instead of clobbering it with the
                    # generic field list — it already explains this same
                    # gap and is more actionable. Only fall back to the
                    # generic message when nothing more specific was set.
                    if not matched[i].get("reason"):
                        matched[i]["reason"] = "Missing required field(s): " + ", ".join(missing)
                else:
                    tracker["statuses"][i] = "READY TO LOAD"
                    tracker["isactives"][i] = True
                    matched[i]["reason"] = ""
                # _move_by_status (which runs after this) files PDFs by
                # r["_status"], set from the original SF verdict before this
                # gate ran — keep it in sync so the file lands in the same
                # folder the DB says it's in.
                matched[i]["_status"] = tracker["statuses"][i]

            job.db_saved = database.save_grouped(grouped, job.batch_name, tracker)

        except Exception:  # noqa: BLE001 - DB save is best-effort
            traceback.print_exc()

    @staticmethod
    def _resync_pending(job):
        """Re-attempt Service First for records still pending in SF (from this
        or earlier runs); promote any whose part is now available."""
        try:
            import config_store
            if not (config_store.load_config().get("db_connection") or "").strip():
                return
            import database
            result = database.resync_pending()
            if result.get("promoted"):
                print(f"Re-synced {result['promoted']} pending record(s)")
            with job._lock:
                for reason in result.get("errors", []):
                    job.errors.append({"file": "(re-sync)", "invoice_no": "", "reason": reason})
        except Exception:  # noqa: BLE001 - best-effort
            traceback.print_exc()

    @staticmethod
    def _move_by_status(job):
        """Move each processed PDF from Input into the folder named after its
        tracker status (a sibling of Input under the Folder Path), so files are
        organised by outcome (Ready to Load / pending in SF / ...)."""
        try:
            import config_store
            import database
            config_store.ensure_status_folders(database.STATUS_VALUES)
            for r in job.results:
                if r.get("status") != "success" or not r.get("data"):
                    continue
                status = r.get("_status") or "EXTRACTED"
                rel = r.get("rel") or r.get("file")
                src = os.path.join(job.source_folder, rel.replace("/", os.sep)) if rel else None
                config_store.move_pdf_to_status(r.get("file"), status, src_path=src)
        except Exception:  # noqa: BLE001 - best-effort file organisation
            traceback.print_exc()

    @staticmethod
    def _reset_moved_files(job):
        """Files whose format wasn't trained get moved to New_Format during a
        run. Reset their input-file log entry to StatusID = 0 so any user can
        upload the same PDF again once the format has been trained."""
        try:
            import config_store
            if not (config_store.load_config().get("db_connection") or "").strip():
                return
            import database
            rels = [
                r.get("rel") for r in job.results
                if r.get("status") == "unknown" and r.get("rel")
            ]
            if rels:
                database.reset_input_files(rels)
        except Exception:  # noqa: BLE001 - best-effort
            traceback.print_exc()


# Shared singleton used by the API layer
job_manager = JobManager()
