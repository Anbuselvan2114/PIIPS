
import os
import re
import sys
import threading
import time
import traceback

# The born-digital fast path yields real Unicode text (e.g. 'ī', curly quotes)
# that a Windows cp1252 console can't print — a bare print() would raise
# UnicodeEncodeError mid-extraction. Make the debug prints lossless-safe so
# they never abort processing. (Data written to JSON/DB is unaffected.)
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - older/redirected streams
        pass

import cv2
import numpy as np
from pdf2image import convert_from_path
from paddleocr import PaddleOCR

try:
    import fitz  # PyMuPDF — read born-digital PDF text layers (fast path)
except Exception:  # noqa: BLE001 - fall back to OCR-only if unavailable
    fitz = None

import anchor_extract

# Rasterisation DPI used for OCR; the text-layer fast path scales PDF point
# coordinates (72 dpi) to the same pixel space so all downstream thresholds
# (row grouping, column bands, the 42% divider) behave identically.
_OCR_DPI = 300
_PDF_POINT_SCALE = _OCR_DPI / 72.0
# Only trust a page's embedded text when it is clearly born-digital, so a
# thin/garbage scanner text layer still goes through OCR.
_MIN_TEXT_WORDS = 12
_MIN_TEXT_CHARS = 60

# ---- Photographed-page detection ------------------------------------------
# A flatbed/app scan is a clean, evenly-lit rectangle of paper: the border is
# bright and uniform, and illumination barely varies across the page. A
# handheld phone photo of a document usually shows some of the surrounding
# scene (desk, hand, shadow) at the edges and has a visible lighting gradient
# (one side/corner darker than the rest). Paper aspect ratios also cluster
# tightly around A4/Letter/Legal; a raw camera photo generally doesn't.
_PAPER_RATIOS = (210 / 297, 8.5 / 11, 8.5 / 14)   # A4, Letter, Legal (portrait)
_PAPER_RATIO_TOL = 0.06


def _looks_like_photo_page(image):
    """True when a rasterised page looks like a handheld photo rather than a
    clean scan/digital render: non-paper aspect ratio, a dark/non-uniform
    border (scene visible around the document), and/or an uneven lighting
    gradient across the page. Any 2 of the 3 signals trigger the flag, to
    keep genuine scans (which can trip a single signal, e.g. a slight skew)
    from being rejected."""
    if image is None or image.size == 0:
        return False

    h, w = image.shape[:2]
    if h == 0 or w == 0:
        return False

    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)

    signals = 0

    # 1) Aspect ratio far from any standard paper size.
    ratio = min(h, w) / max(h, w)
    if not any(abs(ratio - r) <= _PAPER_RATIO_TOL for r in _PAPER_RATIOS):
        signals += 1

    # 2) Border region (outer 4%) is dark and/or highly varied — real page
    #    background is bright, near-uniform paper; a photo often shows desk/
    #    hand/shadow right up to the image edge.
    m = max(4, int(round(min(h, w) * 0.04)))
    border_pixels = np.concatenate([
        gray[:m, :].ravel(), gray[-m:, :].ravel(),
        gray[:, :m].ravel(), gray[:, -m:].ravel(),
    ])
    if border_pixels.mean() < 170 or border_pixels.std() > 45:
        signals += 1

    # 3) Uneven lighting: split into a 3x3 grid of cell means (on a heavily
    #    down-scaled copy, so text/graphics average out and only the broad
    #    illumination gradient remains) and check the spread between cells.
    small = cv2.resize(gray, (30, 30), interpolation=cv2.INTER_AREA)
    cell_means = [
        small[r * 10:(r + 1) * 10, c * 10:(c + 1) * 10].mean()
        for r in range(3) for c in range(3)
    ]
    if (max(cell_means) - min(cell_means)) > 60:
        signals += 1

    return signals >= 2


# Indian GSTIN: 2-digit state code + 10-char PAN + entity/check chars (15 total).
_GSTIN_RE = re.compile(r"\d{2}[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]{2}")

# A physical-dimension spec with no unit word of its own (e.g. `18.5"` for a
# monitor's screen size) - digits then a bare inch/feet mark, nothing else.
_SIZE_SPEC_RE = re.compile(r'^\d+(\.\d+)?["\']$')


def _norm_gstin(value):
    """Uppercase, alphanumerics only — for comparing GSTIN values."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


def _find_gstin(text):
    """
    Extract a GSTIN from a line even when OCR dropped the ':' or split and
    reordered it (e.g. 'AABCP8005C2ZZ  GSTIN/UIN  33' -> '33AABCP8005C2ZZ').
    Returns "" if none found.
    """

    up = text.upper()

    match = _GSTIN_RE.search(up.replace(" ", ""))
    if match:
        return match.group(0)

    # Reassemble a split code + body, only on lines that mention GSTIN/UIN.
    low = up.lower()
    if "gstin" in low or "uin" in low:
        tokens = re.findall(r"[A-Z0-9]+", up)
        code = next((t for t in tokens if re.fullmatch(r"\d{2}", t)), "")
        body = next(
            (t for t in tokens
             if re.fullmatch(r"[A-Z]{5}\d{4}[A-Z]\d[A-Z\d]{2}", t)),
            "",
        )
        if code and body and _GSTIN_RE.fullmatch(code + body):
            return code + body

    return ""


class OCREngine:

    _ocr = None

    # PaddleOCR inference is not thread-safe; serialize it so a training
    # job and a processing job can run concurrently without corrupting the
    # shared model.
    _ocr_lock = threading.Lock()

    @classmethod
    def initialize(cls):

        if cls._ocr is not None:
            return

        print("Loading PaddleOCR...")

        start = time.perf_counter()

        cls._ocr = PaddleOCR(
            lang="en",

            # PaddleOCR 2.7.x parameters
            # Angle classification handles rotated scans / photos.
            use_angle_cls=True,

            use_gpu=False,

            cpu_threads=8,

            enable_mkldnn=True,

            show_log=False
        )

        print(
            f"PaddleOCR Loaded in {time.perf_counter()-start:.2f} sec"
        )
    def __init__(self):

        if OCREngine._ocr is None:
            OCREngine.initialize()
    # Standalone image formats accepted in addition to PDF
    IMAGE_EXTS = (
        ".png", ".jpg", ".jpeg",
        ".tif", ".tiff", ".bmp", ".webp",
    )

    def file_to_images(self, path):
        """
        Load a document as a list of BGR page images.

        Accepts any of:
          * born-digital PDF (with a text layer)
          * scanned PDF
          * photo PDF
          * a standalone image file (photo / scan export)

        Every input is rasterised to an image and OCR'd, so the presence
        or absence of an embedded text layer does not matter.
        """

        ext = os.path.splitext(path)[1].lower()

        # ---- PDF (digital, scanned or photo) ----
        if ext == ".pdf":

            pages = convert_from_path(path, dpi=300)

            images = []
            for page in pages:
                images.append(
                    cv2.cvtColor(np.array(page), cv2.COLOR_RGB2BGR)
                )
            return images

        # ---- Standalone image file ----
        if ext in self.IMAGE_EXTS:

            # np.fromfile keeps non-ASCII / Windows paths working,
            # which cv2.imread does not handle reliably.
            data = np.fromfile(path, dtype=np.uint8)

            img = cv2.imdecode(data, cv2.IMREAD_COLOR)

            if img is None:
                raise ValueError(
                    f"Could not decode image file: {path}"
                )

            return [img]

        raise ValueError(
            f"Unsupported file type '{ext}'. "
            f"Accepted: .pdf or {', '.join(self.IMAGE_EXTS)}"
        )
        
    def _pdf_text_boxes(self, page):
        """Word boxes for a born-digital PDF page, in the SAME schema as
        normalize_result, scaled to the 300-dpi pixel space the pipeline
        expects. Returns [] when the page has no usable text layer (so it
        falls back to OCR)."""
        if fitz is None:
            return []
        words = page.get_text("words") or []   # (x0,y0,x1,y1,word,block,line,n)
        text_len = sum(len((w[4] or "").strip()) for w in words)
        if len(words) < _MIN_TEXT_WORDS or text_len < _MIN_TEXT_CHARS:
            return []

        # Merge words into line segments (by block+line), splitting where the
        # horizontal gap between consecutive words is large — this reproduces
        # PaddleOCR's phrase-level detection boxes (a label and its value stay
        # separate), which the anchor/column extractor is tuned for. Word-level
        # boxes shifted values into the wrong columns.
        s = _PDF_POINT_SCALE
        groups = {}
        for x0, y0, x1, y1, word, block, line, *_ in words:
            if (word or "").strip():
                groups.setdefault((block, line), []).append((x0, y0, x1, y1, word.strip()))

        boxes = []
        for _key, ws in groups.items():
            ws.sort(key=lambda w: w[0])
            seg = []
            for w in ws:
                if seg:
                    prev = seg[-1]
                    gap = w[0] - prev[2]                 # x0(cur) - x1(prev), points
                    height = max(prev[3] - prev[1], 1)
                    if gap > 1.5 * height:               # big gap -> new segment/cell
                        boxes.append(self._seg_box(seg, s))
                        seg = []
                seg.append(w)
            if seg:
                boxes.append(self._seg_box(seg, s))
        return boxes

    @staticmethod
    def _seg_box(seg, s):
        """Build a normalize_result-shaped box from a run of words (scaled)."""
        left = min(w[0] for w in seg) * s
        top = min(w[1] for w in seg) * s
        right = max(w[2] for w in seg) * s
        bottom = max(w[3] for w in seg) * s
        text = " ".join(w[4] for w in seg)
        return {
            "text": text, "confidence": 1.0,
            "box": [[left, top], [right, top], [right, bottom], [left, bottom]],
            "x": left, "y": top, "left": left, "top": top,
            "right": right, "bottom": bottom,
            "width": right - left, "height": bottom - top,
            "center_x": left + (right - left) / 2,
            "center_y": top + (bottom - top) / 2,
        }

    @staticmethod
    def _render_pdf_page(pdf_path, page_no):
        """Rasterise a single PDF page at the OCR DPI (poppler, same as the
        full-document path) for pages that must be OCR'd."""
        pages = convert_from_path(
            pdf_path, dpi=_OCR_DPI, first_page=page_no, last_page=page_no
        )
        if not pages:
            return None
        return cv2.cvtColor(np.array(pages[0]), cv2.COLOR_RGB2BGR)

    def process_page(
        self,
        page_no,
        image,
        boxes=None
    ):

        start = time.perf_counter()

        try:

            # -----------------------------------------
            # OCR (skipped when caller supplies boxes from the PDF text
            # layer — the born-digital fast path). Everything downstream is
            # identical, so a text-layer page and an OCR'd page are processed
            # the exact same way.
            # -----------------------------------------

            if boxes is None:
                with OCREngine._ocr_lock:
                    result = OCREngine._ocr.ocr(
                        image,
                        det=True,
                        rec=True,
                        cls=True
                    )

                boxes = self.normalize_result(
                    result
                )


            # -----------------------------------------
            # No OCR Found
            # -----------------------------------------

            if not boxes:

                elapsed = round(
                    time.perf_counter() - start,
                    2
                )

                return {

                    "PageNo": page_no,

                    "Text": "",

                    "Boxes": [],

                    "Rows": [],

                    "Header": {
                        "Rows": [],
                        "Fields": {}
                    },

                    "Table": {
                        "Rows": [],
                        "Columns": {},
                        "Items": []
                    },

                    "Footer": {
                        "Rows": []
                    },

                    "Statistics": {

                        "TotalLines": 0,

                        "TotalWords": 0,

                        "Elapsed": elapsed
                    }
                }



            # -----------------------------------------
            # Sort Boxes
            # -----------------------------------------

            boxes.sort(

                key=lambda b: (

                    round(
                        b["center_y"] / 15
                    ),

                    b["x"]

                )

            )


            # -----------------------------------------
            # Group Rows
            # -----------------------------------------

            rows = self.group_rows(
                boxes
            )
            rows = self.merge_table_header_rows(rows)

            # -----------------------------------------
            # Detect Table
            # -----------------------------------------

            table_start, table_end = self.find_table(
                rows
            )


            if table_start == -1:


                header_rows = rows

                table_rows = []

                footer_rows = []


            else:


                header_rows = rows[
                    :table_start
                ]

                # Only the item-table body can contain a serial number
                # glued straight onto its description ("1Adaptor") - a
                # header/footer metadata row that merely happens to look
                # like "<digits> <Capitalized word>" (e.g. "1424 Dated:",
                # an invoice no. immediately followed by the next label)
                # would otherwise be mis-split too, landing a fake
                # description box at the table's hardcoded DESCRIPTION_START
                # x - which then gets misread as a LEFT-column word and can
                # wrongly claim/consume the whole header row it came from.
                table_rows = self.split_serial_description(
                    rows[table_start:table_end]
                )


                footer_rows = rows[
                    table_end:
                ]



            # -----------------------------------------
            # Detect Table Columns
            # -----------------------------------------

            columns = {}


            if table_rows:


                columns = self.detect_table_columns(
                    table_rows
                )



            # -----------------------------------------
            # Dynamic Header Extraction
            # -----------------------------------------

            page_width = max(
                (b["right"] for b in boxes),
                default=0
            )

            # Anchor-based extraction: reads each layout by its labels
            # (right-of / below the anchor), so it handles both the
            # colon/right-value and the value-below grid formats.
            header_fields = anchor_extract.extract(
                header_rows,
                footer_rows,
                page_width
            )



            # -----------------------------------------
            # Items
            # -----------------------------------------

            items = []


            if table_rows:

                print("\n========== TABLE ROWS ==========")

                for r in table_rows:

                    print(
                        " | ".join(
                            w["text"]
                            for w in r
                        )
                    )

                print("==============================")
                # Clean OCR table rows first

                table_rows = self.clean_table_rows(
                    table_rows
                )


                items = self.extract_items(

                    table_rows,

                    columns

                )


            # -----------------------------------------
            # Tax / HSN Summary
            # -----------------------------------------

            tax_summary = self.extract_tax_summary(
                footer_rows,
                page_width
            )


            # -----------------------------------------
            # Visual Text
            # -----------------------------------------

            page_text = self.create_ordered_text(
                rows
            )



            # -----------------------------------------
            # Statistics
            # -----------------------------------------

            elapsed = round(

                time.perf_counter() - start,

                2

            )


            print()

            print("=" * 70)

            print(f"PAGE : {page_no}")

            print("=" * 70)

            print(f"OCR Time     : {elapsed} sec")

            print(f"Words        : {len(boxes)}")

            print(f"Rows         : {len(rows)}")

            print(f"Header Rows  : {len(header_rows)}")

            print(f"Table Rows   : {len(table_rows)}")

            print(f"Footer Rows  : {len(footer_rows)}")

            print(f"Items        : {len(items)}")

            print("=" * 70)



            # -----------------------------------------
            # Return
            # -----------------------------------------

            return {

                "PageNo": page_no,

                "Text": page_text,

                "Boxes": boxes,

                "Rows": rows,


                "Header": {

                    "Rows": header_rows,

                    "Fields": header_fields

                },


                "Table": {

                    "Rows": table_rows,

                    "Columns": columns,

                    "Items": items

                },


                "TaxSummary": tax_summary,


                "Footer": {

                    "Rows": footer_rows

                },


                "Statistics": {

                    "TotalLines": len(rows),

                    "TotalWords": len(boxes),

                    "Elapsed": elapsed

                }

            }


        except Exception:


            traceback.print_exc()


            elapsed = round(

                time.perf_counter() - start,

                2

            )


            return {

                "PageNo": page_no,

                "Error": traceback.format_exc(),

                "Text": "",

                "Boxes": [],

                "Rows": [],

                "Header": {

                    "Rows": [],

                    "Fields": {}

                },

                "Table": {

                    "Rows": [],

                    "Columns": {},

                    "Items": []

                },

                "Footer": {

                    "Rows": []

                },

                "Statistics": {

                    "TotalLines": 0,

                    "TotalWords": 0,

                    "Elapsed": elapsed

                }

            }
    def group_rows(
        self,
        boxes
    ):

        if not boxes:
            return []

        # -----------------------------------------
        # Calculate Dynamic Line Height
        # -----------------------------------------

        avg_height = sum(
            box["height"]
            for box in boxes
        ) / len(boxes)

        line_threshold = max(
            10,
            avg_height * 0.70
        )

        # -----------------------------------------
        # Sort Top -> Bottom
        # -----------------------------------------

        boxes.sort(

            key=lambda x: (

                x["center_y"],

                x["x"]

            )

        )

        rows = []

        current_row = []

        current_y = None

        # -----------------------------------------
        # Group Words
        # -----------------------------------------

        for box in boxes:

            if current_y is None:

                current_y = box["center_y"]

                current_row.append(
                    box
                )

                continue

            if abs(

                box["center_y"]

                -

                current_y

            ) <= line_threshold:

                current_row.append(
                    box
                )

            else:

                current_row.sort(

                    key=lambda x: x["x"]

                )

                rows.append(
                    current_row
                )

                current_row = [
                    box
                ]

                current_y = box["center_y"]

        # -----------------------------------------
        # Last Row
        # -----------------------------------------

        if current_row:

            current_row.sort(

                key=lambda x: x["x"]

            )

            rows.append(
                current_row
            )

        return rows
    def print_rows(
        self,
        rows
    ):

        for i, row in enumerate(rows):

            print(
                "\nROW",
                i + 1
            )

            for word in row:

                print(
                    f'{word["text"]:<30} '
                    f'X={word["x"]:.0f} '
                    f'Y={word["y"]:.0f}'
                )
    def reorder_boxes(
        self,
        boxes
    ):

        if not boxes:
            return []

        # ----------------------------------
        # Calculate Average Height
        # ----------------------------------

        avg_height = sum(
            b["height"]
            for b in boxes
        ) / len(boxes)

        line_threshold = max(
            10,
            avg_height * 0.7
        )

        # ----------------------------------
        # Top -> Bottom
        # ----------------------------------

        boxes.sort(
            key=lambda b: b["center_y"]
        )

        rows = []

        current_row = []

        current_y = None

        for box in boxes:

            if current_y is None:

                current_y = box["center_y"]

            if abs(
                box["center_y"] -
                current_y
            ) <= line_threshold:

                current_row.append(box)

            else:

                current_row.sort(
                    key=lambda x: x["x"]
                )

                rows.append(current_row)

                current_row = [box]

                current_y = box["center_y"]

        if current_row:

            current_row.sort(
                key=lambda x: x["x"]
            )

            rows.append(current_row)

        return rows
            
    def create_ordered_text(self, rows):
        """
        Create readable OCR text with better invoice table alignment.
        """

        if not rows:
            return ""

        all_words = [w for row in rows for w in row]

        if not all_words:
            return ""

        page_width = max(w["right"] for w in all_words)

        TOTAL_CHARS = 170
        scale = TOTAL_CHARS / page_width

        output = []

        for row in rows:

            row.sort(key=lambda x: x["x"])

            chars = [" "] * TOTAL_CHARS

            last_position = 0

            for word in row:

                text = word["text"].strip()

                if not text:
                    continue

                # -------------------------
                # Place every word at its true horizontal
                # position so columns line up with the actual
                # invoice layout (headers and values alike).
                # -------------------------

                pos = int(word["x"] * scale)

                pos = max(0, min(pos, TOTAL_CHARS - 1))

                # Avoid overlap only by one character
                if pos <= last_position:
                    pos = last_position + 1

                for i, ch in enumerate(text):

                    index = pos + i

                    if index >= TOTAL_CHARS:
                        break

                    chars[index] = ch

                last_position = pos + len(text)

            line = "".join(chars).rstrip()

            if line.strip():
                output.append(line)

        return "\n".join(output)
    def _page_inputs(self, pdf_path):
        """Per-page (boxes, image) inputs. For a born-digital PDF page the
        text layer supplies boxes (image=None, no OCR); scanned pages and
        image files return (None, image) and are OCR'd as before."""
        ext = os.path.splitext(pdf_path)[1].lower()

        if ext == ".pdf" and fitz is not None:
            inputs = []
            try:
                doc = fitz.open(pdf_path)
            except Exception:  # noqa: BLE001 - unreadable -> OCR everything
                doc = None
            if doc is not None:
                try:
                    for page in doc:
                        boxes = self._pdf_text_boxes(page)
                        if boxes:
                            inputs.append((boxes, None))          # fast path
                        else:
                            inputs.append((None, self._render_pdf_page(pdf_path, page.number + 1)))
                finally:
                    doc.close()
                return inputs

        # Non-PDF image files (or fitz unavailable): OCR every rasterised page.
        return [(None, img) for img in self.file_to_images(pdf_path)]

    def read_pdf(
        self,
        pdf_path,
        allow_scanned=False,
    ):

        page_inputs = self._page_inputs(pdf_path)

        # By default this only processes born-digital PDFs (a real embedded
        # text layer). A page with no text layer (image is not None) had to
        # be rasterised for OCR, which means the source is a scan or a
        # photocopy (or a handheld photo) rather than an original digital
        # document — reject the whole file rather than OCR-extracting from
        # it, so it gets moved to UNSUPPORTED upstream instead of silently
        # producing lower-confidence data.
        #
        # `allow_scanned=True` (config_store's "allow_scanned_pdfs" toggle,
        # PART and SERVICE alike - see processor.py) skips that rejection:
        # `boxes` stays None for a rasterised page, so
        # process_page's own OCR path below runs PaddleOCR on it exactly
        # the way an image file (.png/.jpg) already always has. A genuine
        # handheld photo is still rejected either way - OCR quality on an
        # actual photo (as opposed to a flat scan/photocopy) is unreliable
        # regardless of this setting.
        rasterised = [image for _boxes, image in page_inputs if image is not None]
        if rasterised:
            photo_count = sum(1 for image in rasterised if _looks_like_photo_page(image))
            if photo_count * 2 > len(rasterised):
                raise ValueError(
                    "Document looks like a handheld photo, not a clean scan "
                    "(uneven lighting/background or non-paper aspect ratio) — "
                    "unsupported."
                )
            if not allow_scanned:
                raise ValueError(
                    "Document is a scanned or photocopied PDF, not an original "
                    "born-digital PDF (no embedded text layer) — unsupported."
                )

        result = {

            "FileName": pdf_path,

            "TotalPages": len(page_inputs),

            "Pages": [],

            "Text": "",

            "Fields": {},

            "Items": [],

            "TaxSummary": [],

            # True when at least one page had no embedded text layer and
            # had to be OCR'd (allow_scanned let it through instead of
            # being rejected above) - processor.py uses this to avoid
            # treating a missing PDF-sourced field as "the template needs
            # training" the way it would for a born-digital page: a
            # scanned page's OCR misreading one document's own numbers is
            # image-quality noise on that document, not a template gap
            # retraining could ever fix.
            "IsScanned": bool(rasterised),

        }


        # -----------------------------------------
        # Process Pages, grouped by invoice identity
        # -----------------------------------------
        # Most PDFs contain exactly one invoice, across one or more pages
        # (duplicate copies are skipped below; genuine continuation pages
        # have no header of their own and stay in the current group). Some
        # PDFs concatenate two or more DISTINCT invoices as consecutive
        # pages instead (e.g. a batch of service call reports saved as one
        # file). A new group starts whenever a page declares its OWN
        # "Invoice No." and it differs from the current group's — a page
        # with no Invoice No. of its own never triggers a split, so ordinary
        # multi-page single invoices are unaffected.
        groups = []

        def _new_group():
            return {"text": [], "fields": [], "items": [], "tax": [], "invoice_no": ""}

        for page_no, (boxes, image) in enumerate(
            page_inputs,
            start=1
        ):


            page = self.process_page(
                page_no,
                image,
                boxes=boxes
            )


            result["Pages"].append(
                page
            )


            # -------------------------------------
            # Skip duplicate copy pages
            # -------------------------------------
            # Many invoices print the SAME invoice several times — "ORIGINAL
            # FOR RECIPIENT", "DUPLICATE FOR TRANSPORTER", "TRIPLICATE FOR
            # SUPPLIER". Only the first (original) copy should contribute its
            # fields / items / tax; otherwise the line items are duplicated.
            if re.search(
                r"(duplicate|triplicate|quadruplicate)\s+(for|copy)"
                r"|transporter'?s?\s+copy|extra\s+copy",
                page.get("Text", "") or "",
                re.IGNORECASE,
            ):
                continue

            page_fields = (page.get("Header") or {}).get("Fields", {}) or {}
            page_invoice_no = (page_fields.get("Invoice No.") or "").strip()

            if not groups:
                groups.append(_new_group())
            elif (page_invoice_no and groups[-1]["invoice_no"]
                    and page_invoice_no != groups[-1]["invoice_no"]):
                groups.append(_new_group())

            group = groups[-1]
            if page_invoice_no and not group["invoice_no"]:
                group["invoice_no"] = page_invoice_no


            # -------------------------------------
            # Collect OCR Text
            # -------------------------------------

            if page.get("Text"):

                group["text"].append(
                    page["Text"]
                )



            # -------------------------------------
            # Collect Header Fields
            # -------------------------------------

            if page_fields:

                group["fields"].append(
                    page_fields
                )



            # -------------------------------------
            # Collect Items
            # -------------------------------------

            if page.get("Table"):


                items = page["Table"].get(
                    "Items",
                    []
                )


                if items:

                    group["items"].extend(
                        items
                    )



            # -------------------------------------
            # Collect Tax Summary
            # -------------------------------------

            if page.get("TaxSummary"):

                group["tax"].extend(
                    page["TaxSummary"]
                )


        if not groups:
            groups.append(_new_group())

        result["Invoices"] = [self._merge_group(g) for g in groups]

        # Back-compat top-level aliases: single-invoice consumers (format
        # training/matching, anything reading Fields/Items/Text/TaxSummary
        # directly) keep working unchanged off the first invoice.
        first = result["Invoices"][0]
        result["Text"] = first["Text"]
        result["Fields"] = first["Fields"]
        result["Items"] = first["Items"]
        result["TaxSummary"] = first["TaxSummary"]

        return result

    def _merge_group(self, group):
        """Merge one invoice group's per-page text/fields/items/tax into the
        single-invoice shape the rest of the pipeline expects (same logic
        read_pdf used to apply once across the whole file)."""

        text = "\n\n".join(group["text"])

        merged_fields = {}
        for fields in group["fields"]:
            for key, value in fields.items():
                if not value:
                    continue
                if key not in merged_fields:
                    merged_fields[key] = value
                elif value not in merged_fields[key]:
                    # Append multiline values
                    merged_fields[key] += "\n" + value

        # Seller GSTIN fallback: the per-page anchor pass only sees the
        # header/footer bands, so a seller GSTIN printed inside the item-table
        # band (or one the party pass mislabeled) can be missed. Scan this
        # group's merged text top-down for every GSTIN and take the first
        # that isn't the buyer's / consignee's. If the seller GSTIN is
        # unreadable (the only GSTINs are the buyer's/consignee's), leave
        # Seller GSTIN/UIN EMPTY — never fall back to a buyer GSTIN, which
        # would mislabel the vendor and split/collapse the trained format.
        # Keying by seller name is handled downstream (format_model).
        already = {
            _norm_gstin(merged_fields.get("Buyer GSTIN/UIN")),
            _norm_gstin(merged_fields.get("Consignee GSTIN/UIN")),
        }
        already.discard("")
        cur_seller = _norm_gstin(merged_fields.get("Seller GSTIN/UIN"))
        if cur_seller and cur_seller in already:
            merged_fields["Seller GSTIN/UIN"] = ""
        if not merged_fields.get("Seller GSTIN/UIN"):
            found = []
            for line in (text or "").splitlines():
                low = line.lower()
                if ("gstin" not in low and "uin" not in low
                        and not anchor_extract.GSTIN_RE.search(line.replace(" ", ""))):
                    continue
                val = anchor_extract._gstin_from_text(line)
                if val and val not in found:
                    found.append(val)
            seller = next((g for g in found if _norm_gstin(g) not in already), None)
            if seller:
                merged_fields["Seller GSTIN/UIN"] = seller

        return {
            "Text": text,
            "Fields": merged_fields,
            "Items": group["items"],
            "TaxSummary": group["tax"],
        }
    def extract_invoice_fields(
        self,
        header_rows,
        footer_rows,
        page_width
    ):
        """
        Extract header key/value fields from an Indian GST (Tally-style)
        invoice layout using the OCR geometry:

          * Right-side metadata grid  -> label on one line, value in the
            cell directly below, in two sub-columns.
          * Left-side party blocks    -> Seller / Consignee (Ship to) /
            Buyer (Bill to), each with name, GSTIN/UIN and State Name.
          * "In words" amounts        -> read from the footer rows.

        Column boundaries are derived from page_width, so no absolute
        pixel constants are hard-coded.
        """

        fields = {}

        if not header_rows or page_width <= 0:
            return fields

        left_cut = page_width * 0.45      # left block vs right block
        sub_cut = page_width * 0.67       # right block: two sub-columns

        RIGHT_LABELS = {
            "invoice no.",
            "dated",
            "delivery note",
            "mode/terms of payment",
            "reference no. & date.",
            "other references",
            "buyer's order no.",
            "dispatch doc no.",
            "delivery note date",
            "dispatched through",
            "destination",
            "terms of delivery",
        }

        # ------------------------------------------------------------------
        # Right-side metadata grid: label -> value in the row below.
        # Two sub-columns are tracked independently.
        # ------------------------------------------------------------------

        pending = {"L": None, "R": None}
        seen_labels = []

        for row in header_rows:

            for sub, low_x, high_x in (
                ("L", left_cut, sub_cut),
                ("R", sub_cut, float("inf"))
            ):

                cell_words = sorted(
                    (
                        w for w in row
                        if low_x <= w["x"] < high_x
                    ),
                    key=lambda w: w["x"]
                )

                if not cell_words:
                    continue

                text = " ".join(
                    w["text"].strip()
                    for w in cell_words
                ).strip()

                if not text:
                    continue

                if text.lower() in RIGHT_LABELS:

                    pending[sub] = text.rstrip(":").strip()

                    if pending[sub] not in seen_labels:
                        seen_labels.append(pending[sub])

                elif pending[sub]:

                    fields.setdefault(pending[sub], text)

                    pending[sub] = None

        # Keep the full field structure visible even when a cell is blank.
        for label in seen_labels:
            fields.setdefault(label, "")

        # ------------------------------------------------------------------
        # Left-side party blocks: Seller / Consignee / Buyer.
        # ------------------------------------------------------------------

        section = "Seller"
        named = {"Seller": False, "Consignee": False, "Buyer": False}

        for row in header_rows:

            cell_words = sorted(
                (w for w in row if w["x"] < left_cut),
                key=lambda w: w["x"]
            )

            if not cell_words:
                continue

            text = " ".join(
                w["text"].strip()
                for w in cell_words
            ).strip()

            if not text:
                continue

            low = text.lower()

            # Section markers
            if "consignee" in low and "ship to" in low:
                section = "Consignee"
                continue

            if "buyer" in low and "bill to" in low:
                section = "Buyer"
                continue

            # GSTIN (colon optional; may be split/reordered by OCR)
            gstin = _find_gstin(text)
            if gstin:
                fields.setdefault(f"{section} GSTIN/UIN", gstin)
                continue

            # State Name (colon optional)
            sm = re.match(r"\s*state\s*name\b[\s:]*", text, flags=re.IGNORECASE)
            if sm:
                fields.setdefault(
                    f"{section} State Name",
                    text[sm.end():].strip(" :"),
                )
                continue

            # Other colon-delimited fields (PH, EMAIL ID, E-Mail, ...)
            if ":" in text:

                label, _, value = text.partition(":")

                label = label.strip()
                value = value.strip()

                if not label:
                    continue

                fields.setdefault(label, value)
                continue

            # Non-colon text -> first line is the party name,
            # following lines are the address.
            if not named[section]:

                fields.setdefault(f"{section} Name", text)
                named[section] = True

            else:

                addr_key = f"{section} Address"

                if addr_key in fields:
                    fields[addr_key] += "\n" + text
                else:
                    fields[addr_key] = text

        # ------------------------------------------------------------------
        # Footer "in words" amounts.
        # ------------------------------------------------------------------

        # Markers that OCR may merge onto the in-words line; the amount in
        # words ends before any of these.
        cut_markers = [
            "previous balance",
            "e. & o.e",
            "e.& o.e",
            "e & o.e",
            "e&oe",
            "e. & o. e",
            "company's",
            "declaration",
        ]

        def _trim_in_words(value):
            low_v = value.lower()
            cut = len(value)
            for marker in cut_markers:
                pos = low_v.find(marker)
                if pos > 0:
                    cut = min(cut, pos)
            return value[:cut].strip(" .,-")

        for index, row in enumerate(footer_rows):

            text = " ".join(
                w["text"].strip()
                for w in row
            ).strip()

            low = text.lower()

            if "amount chargeable" in low and "in words" in low:

                if index + 1 < len(footer_rows):

                    value = " ".join(
                        w["text"].strip()
                        for w in footer_rows[index + 1]
                    ).strip()

                    fields.setdefault(
                        "Amount Chargeable (in words)",
                        _trim_in_words(value)
                    )

            if "tax amount" in low and "in words" in low and ":" in text:

                fields.setdefault(
                    "Tax Amount (in words)",
                    _trim_in_words(text.partition(":")[2].strip())
                )

        return fields

    def extract_tax_summary(
        self,
        footer_rows,
        page_width
    ):
        """
        Extract the HSN / tax summary sub-table from the footer:

            HSN/SAC | Taxable Value | Rate | Tax Amount | Total

        Values are bucketed into columns by their horizontal position
        (as a fraction of page_width). Handles the IGST layout; the
        CGST/SGST columns are left blank when not present.
        """

        summary = []

        if not footer_rows or page_width <= 0:
            return summary

        # Locate the tax-summary header row
        start = None

        for i, row in enumerate(footer_rows):

            text = " ".join(
                w["text"].lower()
                for w in row
            )

            if "hsn" in text and "taxable" in text:
                start = i
                break

        if start is None:
            return summary

        for row in footer_rows[start + 1:]:

            words = sorted(
                (w for w in row if w["text"].strip()),
                key=lambda w: w["x"]
            )

            if not words:
                continue

            texts = [w["text"].strip() for w in words]

            low = " ".join(texts).lower()

            has_number = any(
                re.search(r"\d", t)
                for t in texts
            )

            # Stop once we reach the "in words" / declaration lines
            if "in words" in low or "declaration" in low:
                break

            # Skip the secondary header line (Value / Rate / Amount ...)
            if not has_number:
                continue

            entry = {
                "HSN": texts[0],
                "Taxable": "",
                "IGST_Rate_%": "",
                "CGST_Rate_%": "",
                "SGST_Rate_%": "",
                "IGST_Amount": "",
                "CGST_Amount": "",
                "SGST_Amount": "",
                "Total": "",
            }

            for w in words[1:]:

                t = w["text"].strip()

                ratio = w["x"] / page_width

                if "%" in t:
                    entry["IGST_Rate_%"] = t

                elif 0.55 <= ratio < 0.72:
                    entry["Taxable"] = t

                elif 0.72 <= ratio < 0.80:
                    entry["IGST_Rate_%"] = t

                elif 0.80 <= ratio < 0.93:
                    entry["IGST_Amount"] = t

                elif ratio >= 0.93:
                    entry["Total"] = t

            summary.append(entry)

        return summary

    def extract_fields(
        self,
        pages
    ):

        HEADERS = [

            "Invoice No",
            "Invoice Date",
            "Dated",
            "Delivery Note",
            "Mode/Terms of Payment",
            "Reference No",
            "Other References",
            "Buyer's Order No",
            "Dispatch Doc No",
            "Delivery Note Date",
            "Dispatched through",
            "Destination",
            "Terms of Delivery",
            "GSTIN/UIN",
            "GSTIN",
            "State Name",
            "E-Mail",
            "EMAIL ID",
            "PH",
            "Phone",
            "Consignee (Ship to)",
            "Buyer (Bill to)",
            "Amount Chargeable (in words)",
            "Tax Amount (in words)",
            "Declaration"

        ]


        fields = {}


        header_lookup = {

            h.lower(): h

            for h in HEADERS

        }


        for page in pages:


            rows = page.get(
                "Rows",
                []
            )


            for index,row in enumerate(rows):


                row.sort(
                    key=lambda x:x["x"]
                )


                words = [

                    w["text"].strip()

                    for w in row

                ]


                row_text = " ".join(words)


                lower = row_text.lower()



                matched = None


                for header in HEADERS:


                    if header.lower() in lower:

                        matched = header

                        break



                if not matched:

                    continue



                # -----------------------------------
                # Remove header part
                # -----------------------------------

                pos = lower.find(
                    matched.lower()
                )


                after = row_text[

                    pos +

                    len(matched)

                :].strip(
                    " :-."
                )


                # -----------------------------------
                # Ignore table header
                # -----------------------------------

                if "description of goods" in lower:

                    continue



                value = after



                # -----------------------------------
                # If no value on same line,
                # take next rows
                # -----------------------------------

                if not value:


                    next_lines=[]


                    for nxt in range(
                        index+1,
                        min(
                            index+4,
                            len(rows)
                        )
                    ):


                        txt=" ".join(

                            w["text"]

                            for w in rows[nxt]

                        )


                        txt_lower=txt.lower()



                        # stop at next known header

                        if any(

                            h.lower() in txt_lower

                            for h in HEADERS

                        ):

                            break



                        next_lines.append(txt)



                    value="\n".join(
                        next_lines
                    )



                # -----------------------------------
                # Cleanup
                # -----------------------------------

                value=value.strip()



                if value:


                    fields[matched]=value



        return fields
    def normalize_result(
        self,
        result
    ):
        boxes = []

        if not result:
            return boxes

        try:

            # PaddleOCR 2.x format
            if isinstance(result, list):

                if not result[0]:
                    return boxes

                for item in result[0]:

                    try:

                        polygon = item[0]

                        text = item[1][0].strip()

                        confidence = float(
                            item[1][1]
                        )

                        if not text:
                            continue


                        xs = [
                            p[0]
                            for p in polygon
                        ]

                        ys = [
                            p[1]
                            for p in polygon
                        ]


                        left = min(xs)
                        right = max(xs)

                        top = min(ys)
                        bottom = max(ys)


                        width = right - left
                        height = bottom - top


                        center_x = left + (
                            width / 2
                        )

                        center_y = top + (
                            height / 2
                        )


                        boxes.append({

                            "text": text,

                            "confidence": confidence,

                            "box": polygon,


                            # Coordinates
                            "x": left,
                            "y": top,

                            "left": left,
                            "top": top,

                            "right": right,
                            "bottom": bottom,


                            # Size
                            "width": width,
                            "height": height,


                            # Center
                            "center_x": center_x,
                            "center_y": center_y

                        })


                    except Exception as ex:

                        print(
                            "Normalize item error:",
                            ex
                        )


            # PaddleOCR 3.x format
            else:

                data = result


                texts = data["rec_texts"]

                scores = data["rec_scores"]

                polys = data["rec_polys"]


                for polygon, text, confidence in zip(
                    polys,
                    texts,
                    scores
                ):


                    text = text.strip()


                    if not text:
                        continue


                    xs = [
                        p[0]
                        for p in polygon
                    ]

                    ys = [
                        p[1]
                        for p in polygon
                    ]


                    left = min(xs)
                    right = max(xs)

                    top = min(ys)
                    bottom = max(ys)


                    width = right - left
                    height = bottom - top


                    center_x = left + (
                        width / 2
                    )

                    center_y = top + (
                        height / 2
                    )


                    boxes.append({

                        "text": text,

                        "confidence": float(
                            confidence
                        ),

                        "box": polygon,


                        "x": left,
                        "y": top,

                        "left": left,
                        "top": top,

                        "right": right,
                        "bottom": bottom,


                        "width": width,
                        "height": height,


                        "center_x": center_x,
                        "center_y": center_y

                    })


        except Exception:

            traceback.print_exc()


        return boxes

    def find_table(
        self,
        rows
    ):

        TABLE_WORDS = {

            "description",

            "goods",

            "hsn",

            "sac",

            "qty",

            "quantity",

            "rate",

            "amount",

            "taxable",

            "igst",

            "cgst",

            "sgst",

            "disc"

        }

        table_start = -1

        table_end = -1

        # -------------------------------
        # Find Table Start
        # -------------------------------

        for index, row in enumerate(rows):

            text = " ".join(

                word["text"].lower()

                for word in row

            )

            matched = {
                keyword for keyword in TABLE_WORDS if keyword in text
            }

            if len(matched) >= 3:

                table_start = index

                break

            # Some layouts wrap the header onto two stacked lines (e.g.
            # "Unit" above "Price", "Net" above "Amount") - neither line
            # alone reaches the keyword threshold, but together they
            # clearly are the table header (the same multi-row header
            # detect_table_columns already merges further down). Only
            # tried once this row has already matched something, so two
            # unrelated lines elsewhere can't coincidentally combine -
            # and only when the NEXT row doesn't already qualify on its
            # own, so a genuine single-row header (e.g. a "SN | CGST |
            # SGST" sub-heading sitting just above the real, already-
            # sufficient header row) isn't backdated a row early, pulling
            # that extra line into the table and throwing off column
            # detection.
            if matched and index + 1 < len(rows):

                next_text = " ".join(
                    word["text"].lower() for word in rows[index + 1]
                )

                next_matched = {
                    keyword for keyword in TABLE_WORDS if keyword in next_text
                }

                if len(next_matched) < 3 and len(matched | next_matched) >= 3:

                    table_start = index

                    break

        # -------------------------------
        # Find Table End
        # -------------------------------

        if table_start == -1:

            return -1, -1

        FOOTER_WORDS = [

            "amount chargeable",

            "tax amount",

            "declaration",

            "bank details",

            # Some layouts label this section just "Bank:" rather than
            # "Bank Details" (e.g. "Bank: Kotak Mahindra Bank" as its own
            # row) - without this, "bank details" never matches and the
            # whole Bank/IFSC/A/C block gets scanned as more item rows.
            "bank:",

            "terms",

            "e.& o.e",

            "authorised",

            "authorized",

            # Generic amount-summary section headings used by layouts whose
            # own footer markers above never appear until much further down
            # (e.g. "Bank Details") - without these, everything between the
            # item table and that later marker (the amount-in-words line,
            # the Sub Total/Tax/Received/Balance block, a separate tax-rate
            # table) gets scanned as more table rows, and label fragments
            # from it end up glued onto the last real item's description.
            "invoice amount in words",

            "amount in words",

            "sub total",

        ]

        for index in range(

            table_start + 1,

            len(rows)

        ):

            text = " ".join(

                word["text"].lower()

                for word in rows[index]

            )

            found = False

            for keyword in FOOTER_WORDS:

                if keyword in text:

                    table_end = index

                    found = True

                    break

            if found:

                break

        if table_end == -1:

            table_end = len(rows)

        return (

            table_start,

            table_end

        )
    def extract_header(
        self,
        header_rows
    ):

        fields = {}

        current_key = None

        for row in header_rows:

            row.sort(
                key=lambda x: x["x"]
            )

            # Complete row text
            row_text = " ".join(
                word["text"]
                for word in row
            ).strip()

            if not row_text:
                continue

            # ----------------------------------
            # Find labels ending with :
            # ----------------------------------

            for index, word in enumerate(row):

                text = word["text"].strip()

                if text.endswith(":"):

                    key = text[:-1].strip()

                    value = " ".join(

                        w["text"]

                        for w in row[index + 1:]

                    ).strip()

                    fields[key] = value

                    current_key = key

            # ----------------------------------
            # Multi-line value
            # ----------------------------------

            if current_key:

                has_label = False

                for word in row:

                    if word["text"].endswith(":"):

                        has_label = True

                        break

                if not has_label:

                    first_x = row[0]["x"]

                    if first_x < 250:

                        fields[current_key] += (
                            "\n" + row_text
                        )

        return fields
            
    def extract_items(
            self,
            table_rows,
            columns=None
    ):

        items = []

        current_item = None

        # Some layouts wrap a long description onto the line BEFORE the row
        # carrying the serial number/qty/amount, instead of after it — that
        # lead-in text has nowhere to attach (no item has started yet) and
        # would otherwise be silently dropped. Buffered here and prepended
        # to the next item's Description once it actually starts.
        pending_lead_in = ""

        # Freight / courier etc. printed in the item table become their OWN
        # line — captured, but flagged as a charge so the SF part /
        # Navision-item checks skip them. Hoisted to function scope: also
        # needed by the "no leading serial" item-start fallback below, so a
        # charge line with a full HSN/SAC code (e.g. "996812") isn't
        # mistaken for a regular table item before it ever reaches the
        # dedicated charge-line handling further down.
        CHARGE_KW = ("freight", "frieght", "courier", "forwarding",
                     "shipping", "packing", "handling", "cartage",
                     "loading", "insurance")


        def _strip_currency(value):
            # A Rate/Amount token glued to its currency symbol ("₹ 14000.00",
            # "Rs. 250", "INR250") reads as text, not a number, unless the
            # symbol/prefix is stripped first.
            return re.sub(
                r"^\s*(₹|\$|€|£|rs\.?|inr)\s*",
                "",
                value.strip(),
                flags=re.IGNORECASE,
            )



        def clean_number(value):

            try:
                return float(
                    _strip_currency(value).replace(",", "")
                )
            except (ValueError, AttributeError):
                return None



        def is_number(value):

            value = _strip_currency(value).replace(",", "").strip()

            return re.match(
                r"^\d+(\.\d+)?$",
                value
            ) is not None



        def is_hsn(value):
            # GST HSN codes are validly 4, 6 or 8 digits depending on the
            # supplier's turnover slab — not always the full 6-8 digit form.
            return re.match(
                r"^\d{4,10}$",
                value.strip()
            ) is not None

        def is_hsn_or_sac(value):
            # Like is_hsn, but also accepts the short 4-digit SAC "heading"
            # form GST uses for services — always prefixed "99"
            # (e.g. "9968" = courier services). Used only where a row has
            # already been identified as a charge line (freight/courier/
            # etc.), not for deciding whether a row starts a new table item
            # — a stray "99xx" token elsewhere in a row is far more likely
            # to be a coincidental number than a real SAC code, so the
            # broader row-classification checks intentionally stay strict.
            v = value.strip()
            return is_hsn(v) or re.match(r"^99\d{2}$", v) is not None



        def is_unit(value):

            return value.lower().strip().strip(".,)") in [
                "no",
                "nos",
                "pcs",
                "pc",
                "kg",
                "kgs",
                "unit",
                "qty",
                "nos",
                "each",
                "ea",
                # A bare "days" (the other half of a Warranty column's "90
                # days" split from its own number by OCR) with no number
                # attached is warranty-period noise, not description text -
                # a genuine product description never uses the bare word on
                # its own. The number itself ("90") is already dropped
                # elsewhere as a stray non-column figure; only the leftover
                # word needs excluding here.
                "day",
                "days",
            ]

        def descriptive_text(wds):
            """Real description words from a row: unit words (No./Nos/
            Pcs/...) are always noise, and a number is noise ONLY when it
            sits under the Quantity/Rate/Amount column - a genuine spec
            number that happens to be part of the description (e.g. "90"
            in "90 Days Warranty") is printed in the Description column's
            own territory and must survive, while a Quantity/Rate/Amount
            figure that leaked onto this row (already harvested above)
            should not be echoed into the text too."""
            out = []
            for w in wds:
                t = w["text"].strip()
                if is_unit(t):
                    continue
                if is_number(t) and value_cols:
                    col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                    if col in ("Quantity", "Rate", "Amount"):
                        continue
                out.append((w, t))
            if not out:
                return ""
            # A number glued straight onto the next word with no space
            # (e.g. "1000Base-T") can land as two overlapping/touching OCR
            # boxes rather than one - joining every kept word with a plain
            # space would then insert a space that was never actually
            # there. Only omit it when the boxes actually touch or overlap
            # (gap <= 2); two genuinely separate words always have a real
            # gap between their boxes.
            result = out[0][1]
            for i in range(1, len(out)):
                prev_w = out[i - 1][0]
                w, t = out[i]
                gap = w["x"] - prev_w.get("right", prev_w["x"])
                result += t if gap <= 2 else " " + t
            return result.strip()



        if not table_rows:
            return []


        # Column x-positions used to align values. HSN/Quantity/Rate/Amount/
        # Discount receive values; SI/Description/Per/IGST/Total are included
        # only as partition anchors so a token nearest one of THEM (e.g. the
        # IGST or grand-total figure) is not misfiled into the item Amount.
        value_cols = {
            key: columns[key]
            for key in ("SI", "Description", "HSN", "Quantity", "Rate",
                        "Per", "Amount", "Discount", "IGST", "Total")
            if columns and key in columns
        }

        def number_with_unit(value):
            """Parse a number that OCR glued to a trailing unit, e.g.
            "3,800.00Pcs" -> 3800.0. Returns None when there is no leading
            number."""
            m = re.match(r"^([\d,]+(?:\.\d+)?)\s*[A-Za-z%.]+$", value.strip())
            return clean_number(m.group(1)) if m else None

        def leading_number(value):
            """Leading monetary number of a token, tolerating a second number
            glued on (OCR merges the taxable value with the GST %:
            "900.0018.00" -> 900.00). Falls back to a plain integer."""
            m = re.match(r"^(\d[\d,]*\.\d{2})", value.strip())
            if m:
                return clean_number(m.group(1))
            m = re.match(r"^(\d[\d,]*)(?!\S*[A-Za-z])", value.strip())
            return clean_number(m.group(1)) if m else None

        def row_has_values(vwords):
            """True if the row (minus its serial token) carries item data —
            an HSN code under the HSN column, or a number under the
            Quantity/Rate/Amount column. A row with only descriptive text is
            a wrapped-description continuation, not a new item.
            The HSN check is column-gated (not "any 4-10 digit token
            anywhere in the row") because a wrapped description can itself
            contain a bare number that happens to be HSN-shaped - e.g. a
            part number OCR split across two lines as "1000" + "Base-T"
            (from "1000Base-T...") lands the "1000" fragment under the
            Description column, not HSN; without this gate it would be
            mistaken for a real HSN code and the continuation row wrongly
            treated as a new item."""
            for w in vwords:
                t = w["text"].strip()
                if value_cols:
                    col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                    if col == "HSN" and is_hsn(t.replace(",", "")):
                        return True
                    if col in ("Quantity", "Rate", "Amount") and (
                        is_number(t) or number_with_unit(t) is not None
                    ):
                        return True
                elif is_hsn(t.replace(",", "")) or is_number(t):
                    return True
            return False

        def row_has_amount(vwords):
            """True if the row carries a real value under the Amount
            column - the one signal a genuine item line always has, that a
            stray continuation fragment (e.g. a quantity/unit leaking onto
            its own wrapped-description row) does not."""
            if not value_cols:
                return False
            for w in vwords:
                t = w["text"].strip()
                col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                if col == "Amount" and (is_number(t) or number_with_unit(t) is not None):
                    return True
            return False

        item_seq = 0


        for row in table_rows:


            # Sort by center, not left edge: a number immediately glued to
            # the next word with no space (e.g. "1000Base-T") can get OCR'd
            # as two overlapping boxes - a wide one for the trailing text
            # whose left edge is drawn a bit early, and a narrow one for the
            # leading digits nested inside it (e.g. digits box left=203,
            # right=243 vs text box left=180, right=880). Sorting by left
            # edge alone then puts the wide box first - splicing "1000" in
            # after "DGS-" instead of before "Base-T" - while the digits'
            # box center is still safely left of the wide box's center.
            row.sort(
                key=lambda x: x.get("center_x", x["x"])
            )


            words = [
                w for w in row
                if w["text"].strip()
            ]


            if not words:
                continue



            texts = [
                w["text"].strip()
                for w in words
            ]


            row_text = " ".join(texts)

            lower = row_text.lower()



            # Ignore table header

            if (
                "description" in lower
                and
                "amount" in lower
            ):
                continue



            # Ignore tax rows

            if any(x in lower for x in [
                "output igst",
                "input igst",
                "cgst",
                "sgst",
                "tax amount",
                "total"
            ]):

                continue



            first = texts[0]



            # ----------------------------------
            # Detect item start
            #
            # A real serial is a small standalone integer ("1", "2") or an
            # integer glued to descriptive text ("1CPU"). A formatted number
            # such as "17,900.00" is an amount / subtotal, NOT an item start.
            # ----------------------------------

            serial = None
            desc = ""
            serial_from_token = False

            serial_word_idx = 0

            std = re.fullmatch(r"(\d{1,3})[.)]?", first)
            if std:
                serial = std.group(1)
                serial_from_token = True
            else:
                glued = re.match(r"^(\d{1,3})[.)]?\s*([A-Za-z].*)$", first)
                if glued:
                    serial = glued.group(1)
                    desc = glued.group(2).strip()
                    serial_from_token = True

            # Fallback: the serial sits at the table's own detected SI/Sl No.
            # column position but isn't the row's first token — some layouts
            # put the description first, e.g. "Laptop Panel Cover | pcs | 1 |
            # 8473 | 1 | 1,800.00 | pcs | 1,800.00" (serial "1" is the 3rd
            # token). Without this, only the very first such row is kept —
            # every row after it has no recognisable item start, so it gets
            # folded into the first item's Description as a "continuation"
            # instead of starting its own item, silently merging several
            # distinct products into one garbled description.
            if serial is None and value_cols.get("SI") is not None:
                si_x = value_cols["SI"]
                for idx, w in enumerate(words):
                    t = w["text"].strip()
                    m = re.fullmatch(r"(\d{1,3})[.)]?", t)
                    if m and abs(w["x"] - si_x) < 150:
                        serial = m.group(1)
                        serial_word_idx = idx
                        serial_from_token = True
                        break

            # Fallback: a data row with no leading serial (scanned invoices
            # whose SI column is missing, or a layout with no SI column at
            # all). Recognise it as a real item either by an HSN code, or
            # (some layouts print no HSN on the item's own row at all - it's
            # on a separate row further down) by a genuine Amount value
            # sitting under that column - requiring HSN alone would miss the
            # item entirely and let it fall through into the next unrelated
            # row's description.
            # Synthesise a sequential serial. A charge line (freight/courier/
            # etc.) must NOT be swept in here — it has its own dedicated
            # handling further below that correctly sets Quantity/Rate/the
            # charge flag; this fallback would otherwise treat it as a
            # generic table item and lose all of that.
            # Also require a genuine, non-unit description word among the
            # row's tokens: a real item always names the product, while a
            # GST breakdown/tax-summary row or an unlabelled running-total
            # row ("QTY | 02 | Rs.4366.00") that slipped past
            # clean_table_rows has no description text of its own - at
            # most a bare unit word ("QTY"/"Nos") - and would otherwise be
            # mistaken for a second phantom item.
            # The Amount check (rather than any one of Quantity/Rate/Amount)
            # matters too: a genuine item line always states its line total,
            # while a wrapped-description continuation that merely has a
            # stray quantity/unit bleeding onto its own row ("Godown: Main
            # Location | 2.00 Nos") does not, and must stay folded into the
            # current item's description instead of being promoted into an
            # incomplete phantom item.
            # A rounding adjustment ("Rounded Off", "Less : Rounded Off (-)")
            # has real (non-unit) label text and a genuine Amount, so it
            # would otherwise pass every check above and get misfiled as a
            # purchased item worth a few paise/rupees - exclude it by name.
            # The HSN check is column-gated (not "any 4-10 digit token
            # anywhere in the row") for the same reason as row_has_values
            # below: a wrapped description can itself contain a bare number
            # that happens to be HSN-shaped - e.g. a part number OCR split
            # across two lines as "1000" + "Base-T..." (from
            # "1000Base-T...") lands the "1000" fragment under the
            # Description column, not HSN, and would otherwise start a
            # phantom new item out of a genuine continuation row.
            if (
                serial is None
                and value_cols
                and not any(k in lower for k in CHARGE_KW)
                and not re.search(r"round(?:ed)?[\s-]*off", lower)
                and (
                    any(
                        is_hsn(t.replace(",", ""))
                        and min(value_cols, key=lambda c: abs(w["x"] - value_cols[c])) == "HSN"
                        for w, t in zip(words, texts)
                    )
                    or row_has_amount(words)
                )
                and any(
                    re.search(r"[A-Za-z]{2,}", t) and not is_unit(t)
                    for t in texts
                )
            ):
                item_seq += 1
                serial = str(item_seq)


            if serial is not None:

                # A leading integer on a row that carries no item data is not
                # a new item — it is a wrapped description line (or an OCR
                # artefact such as "8GB" split into "8" + "GB"). Fold it into
                # the current item's description instead of starting a new one.
                cont_words = (
                    words[:serial_word_idx] + words[serial_word_idx + 1:]
                    if serial_from_token else words
                )
                if (
                    serial_from_token
                    and current_item is not None
                    and not row_has_values(cont_words)
                ):
                    cont = desc
                    for w in cont_words:
                        t = w["text"].strip()
                        if t and not is_unit(t) and not is_number(t):
                            cont += " " + t
                    cont = cont.strip()
                    # OCR often splits a size like "8GB" into a stray "8"
                    # (mistaken for a serial) + "GB..."; reattach the digit
                    # when the folded text starts with a size unit.
                    if cont and serial.isdigit() and re.match(
                        r"^(GB|TB|MB|KB)\b", cont, re.IGNORECASE
                    ):
                        cont = serial + cont
                    if cont:
                        current_item["Description"] += " " + cont
                    continue

                if serial_from_token and serial.isdigit():
                    item_seq = int(serial)

                if current_item:

                    items.append(
                        current_item
                    )



                current_item = {

                    "SI": serial,

                    "Description": (pending_lead_in + " " + desc).strip() if pending_lead_in else desc,

                    "HSN": None,

                    "Quantity": None,

                    "Rate": None,

                    "Amount": None,

                    # A freight/courier/shipping line printed WITH its own
                    # explicit serial number (e.g. "2 | Freight Charges |
                    # ... | 500.00") reaches here via the genuine-serial
                    # path above, bypassing the CHARGE_KW check further up
                    # that only guards the no-serial fallback - without
                    # tagging it here too, it's saved as a normal "Item"
                    # (Service First tries to match it as a real part, and
                    # it wrongly shows up as a selectable description on
                    # the Part Description Mapping screen).
                    "charge": (
                        any(k in lower for k in CHARGE_KW)
                        and not any(k in lower for k in ("total", "tax", "output"))
                    ),

                }
                pending_lead_in = ""


                numbers = []


                # When no serial token was consumed, the first token is real
                # data (e.g. the description), so include it.
                value_words = (
                    words[:serial_word_idx] + words[serial_word_idx + 1:]
                    if serial_from_token else words
                )

                for word in value_words:


                    txt = word["text"].strip()


                    if is_unit(txt):

                        continue


                    # ------------------------------------------
                    # Column-aware assignment (preferred):
                    # place each token under its nearest column.
                    # ------------------------------------------

                    if value_cols:

                        col = min(
                            value_cols,
                            key=lambda c: abs(word["x"] - value_cols[c])
                        )


                        # A 4+ digit token whose nearest column is Quantity
                        # is almost never a genuine quantity (real order
                        # quantities run 1-3 digits) - it's an HSN/SAC code
                        # that landed a few pixels closer to the Quantity
                        # header than its own, likely because the data row's
                        # column boundary drifted slightly from the header
                        # row's. Reroute it before the distance tie-break
                        # locks it into the wrong column.
                        if (
                            col == "Quantity"
                            and "HSN" in value_cols
                            and re.match(r"^\d{4,10}[A-Za-z]?$", txt)
                        ):
                            col = "HSN"


                        # HSN / SAC code (4, 6 or 8 digits, optional letter)
                        if col == "HSN" and re.match(
                            r"^\d{4,10}[A-Za-z]?$",
                            txt
                        ):
                            current_item["HSN"] = txt
                            continue


                        if col in ("Quantity", "Rate", "Amount") and is_number(txt):
                            current_item[col] = clean_number(txt)
                            continue


                        # A number OCR glued to its unit ("3,800.00Pcs") under
                        # a value column: recover the number, drop the unit.
                        if col in ("Quantity", "Rate", "Amount"):
                            nwu = number_with_unit(txt)
                            if nwu is not None:
                                current_item[col] = nwu
                                continue
                            # Two numbers glued (taxable value + GST %):
                            # "900.0018.00" -> 900.00.
                            ldn = leading_number(txt)
                            if ldn is not None:
                                current_item[col] = ldn
                                continue


                        # Numbers under other columns (e.g. discount) are
                        # ignored; free text becomes description.
                        if not is_number(txt):
                            current_item["Description"] += " " + txt

                        continue


                    # ------------------------------------------
                    # Fallback: no detected columns -> old heuristic
                    # ------------------------------------------

                    if is_hsn(txt):

                        current_item["HSN"] = txt

                        continue



                    if is_number(txt):

                        numbers.append({

                            "x":word["center_x"],

                            "value":clean_number(txt)

                        })

                        continue



                    # description continuation

                    current_item["Description"] += (
                        " " + txt
                    )



                # -----------------------------
                # Assign numeric values (fallback, by order)
                # -----------------------------

                numbers.sort(
                    key=lambda x:x["x"]
                )


                if not value_cols and len(numbers) >= 3:


                    current_item["Quantity"] = (
                        numbers[0]["value"]
                    )


                    current_item["Rate"] = (
                        numbers[1]["value"]
                    )


                    current_item["Amount"] = (
                        numbers[-1]["value"]
                    )


                elif not value_cols and len(numbers) == 2:


                    current_item["Rate"] = (
                        numbers[0]["value"]
                    )


                    current_item["Amount"] = (
                        numbers[1]["value"]
                    )



            else:


                # ----------------------------------
                # Charge line / description continuation
                # ----------------------------------

                # A bare physical-dimension spec ("18.5\"") has no letters
                # either, so it fails the isalpha() check below same as a
                # leaked tax/summary number does - but its trailing inch
                # mark makes it unambiguous (a plain number or a percent
                # figure like "18%" never has one), so it can safely count
                # as real content without reopening the regression a
                # broader "any non-numeric token" rule caused earlier.
                has_text = (
                    any(c.isalpha() for c in row_text)
                    or any(_SIZE_SPEC_RE.match(w["text"].strip()) for w in words)
                )

                # "continued"/"contd" is a multi-page invoice's own page-
                # footer marker ("continued ...", printed below the table on
                # every page but the last), and "this is a computer
                # generated invoice" is the standalone footer line every
                # page of this layout prints at the very bottom - both have
                # no leading serial and real letters, so without excluding
                # them here they silently glue onto whichever item happened
                # to be last on that page's table (e.g. "Hinges ... Part No
                # : 5H50S29037 continued" / "... This is a Computer
                # Generated Invoice").
                # "Rounded Off" (past tense) is the actual wording several
                # vendors print ("Dell Mouse MS116 Rounded Off", "...Less :
                # Rounded Off (-)0.40") - "round off"/"round-off"/"roundoff"
                # above don't substring-match it (the "ed" breaks it), so it
                # slipped through and glued onto the previous item exactly
                # like "continued" did.
                EXCLUDE_KW = ("output", "igst", "cgst", "sgst", "tax amount",
                              "total", "round off", "round-off", "roundoff",
                              "rounded off",
                              "rupees", "inr ", "grand total", "tax rate",
                              "taxable", "amount chargeable", "in words",
                              "declaration", "continued", "contd",
                              "computer generated invoice")

                is_charge = (has_text
                             and any(k in lower for k in CHARGE_KW)
                             and not any(k in lower for k in
                                         ("total", "tax", "output")))

                if is_charge:
                    def _val(t):
                        t = t.strip()
                        if is_number(t.replace(",", "")):
                            return clean_number(t)
                        v = number_with_unit(t)
                        return v if v is not None else leading_number(t)

                    amt = None
                    if value_cols and "Amount" in value_cols:
                        best = None
                        for w in words:
                            v = _val(w["text"])
                            if v is not None:
                                d = abs(w["x"] - value_cols["Amount"])
                                if best is None or d < best[0]:
                                    best = (d, v)
                        amt = best[1] if best else None
                    if amt is None:
                        nums = [_val(w["text"]) for w in words]
                        nums = [n for n in nums if n is not None]
                        amt = nums[-1] if nums else None

                    hsn = next((w["text"].strip() for w in words
                                if is_hsn_or_sac(w["text"].strip())), None)
                    label = " ".join(
                        w["text"].strip() for w in words
                        if _val(w["text"]) is None and not is_hsn_or_sac(w["text"].strip())
                    ).strip() or "Freight"

                    # Some layouts print the charge's OWN GST rate as a
                    # separate cell on this same row (e.g. "Freight Outward
                    # 9968 | 18 | % | 120.00" - a bare number immediately
                    # followed by "%", not the HSN and not the Amount). The
                    # number itself is a valid _val() match, so the label-
                    # building comprehension above already excludes it from
                    # the description - it just isn't captured anywhere
                    # else otherwise, silently discarding a real, explicitly
                    # stated freight tax rate. Read straight from row_text
                    # (before that exclusion) rather than the words list, so
                    # it doesn't matter whether "18" and "%" arrived as one
                    # token or two.
                    charge_rate_match = re.search(r"(\d{1,2}(?:\.\d+)?)\s*%", row_text)
                    charge_rate = (
                        float(charge_rate_match.group(1)) if charge_rate_match else None
                    )

                    # A bare charge-keyword row with no number of its own,
                    # immediately after an already-open charge line (e.g.
                    # "Freight Charges" on one row, then a lone "Shipping"
                    # wrapping onto the very next row with no amount/HSN of
                    # its own) is that same charge's label wrapping onto a
                    # second line, not a second charge - fold it into the
                    # open charge's own Description instead of closing it
                    # out and opening an empty duplicate charge line (which
                    # then fails mandatory-field validation with no
                    # Quantity/Rate of its own).
                    if amt is None and current_item and current_item.get("charge"):
                        current_item["Description"] = (
                            current_item["Description"] + " " + label
                        ).strip()
                        continue

                    if current_item:
                        items.append(current_item)
                    item_seq += 1
                    current_item = {
                        "SI": str(item_seq),
                        "Description": (pending_lead_in + " " + label).strip() if pending_lead_in else label,
                        "HSN": hsn,
                        "Quantity": 1, "Rate": amt, "Amount": amt,
                        "charge": True,
                        "ChargeRatePercent": charge_rate,
                    }
                    pending_lead_in = ""
                    continue

                if (current_item and has_text
                        and not any(k in lower for k in EXCLUDE_KW)
                        and not any(k in lower for k in CHARGE_KW)):
                    # A scanned/garbled image can split one logical item
                    # row across several OCR rows, with the Quantity/Rate/
                    # Amount figures landing on a row that has no
                    # recognisable serial of its own and so falls through
                    # to here as plain description continuation. Recover
                    # any of those columns this item is still missing
                    # before appending the row as description text -
                    # only a token tight against a value column's own
                    # x-position qualifies, so a part number or size that
                    # happens to be numeric but sits elsewhere on the row
                    # isn't mistaken for it.
                    if value_cols:
                        for w in words:
                            t = w["text"].strip()
                            if not is_number(t):
                                continue
                            col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                            if (
                                col in ("Quantity", "Rate", "Amount")
                                and current_item.get(col) is None
                                and abs(w["x"] - value_cols[col]) < 150
                            ):
                                current_item[col] = clean_number(t)
                    content = descriptive_text(words)
                    if content:
                        current_item["Description"] += " " + content
                elif (current_item is None and has_text
                        and not any(k in lower for k in EXCLUDE_KW)
                        and not any(k in lower for k in CHARGE_KW)):
                    content = descriptive_text(words)
                    if content:
                        pending_lead_in = (
                            pending_lead_in + " " + content
                        ).strip() if pending_lead_in else content



        if current_item:

            items.append(
                current_item
            )



        # Cleanup

        for seq, item in enumerate(items, start=1):

            item["Description"] = re.sub(
                r"\s+",
                " ",
                item["Description"]
            ).strip()

            # Renumber serials sequentially in row (invoice) order so a
            # mis-read serial ("18") cannot propagate into Line No. downstream
            # (Line_No = SI * 10000).
            item["SI"] = seq

            # A charge line (freight/courier/shipping/...) that reached
            # "Item" creation via its OWN printed serial number (e.g. "2 |
            # Freight Charges | 996532 | 200.00") only ever gets its flat
            # Amount filled from the row's own columns - unlike the
            # dedicated no-serial charge path further above, nothing here
            # states a per-unit Rate or a Quantity, so both stay None and
            # the line fails mandatory Quantity/Direct Unit Cost validation
            # even though the invoice states its amount perfectly clearly.
            # A flat charge is conceptually "1 unit costing the stated
            # amount" - default it the same way the dedicated path already
            # does, rather than leaving it incomplete.
            if item.get("charge") and item.get("Amount") is not None:
                if item.get("Quantity") is None:
                    item["Quantity"] = 1
                if item.get("Rate") is None:
                    item["Rate"] = item["Amount"]

        return items

    def detect_table_columns(self, table_rows):
        """
        Detect invoice table column positions dynamically.
        Returns pixel X positions of each column.
        """

        columns = {}

        if not table_rows:
            return columns

        # ------------------------------------------
        # Find Header Row
        # ------------------------------------------
        header_row = None

        HEADER_KEYWORDS = [
            "description",
            "goods",
            "hsn",
            "sac",
            "qty",
            "quantity",
            "rate",
            "amount",
            "disc",
            "discount",
            "per",
            "unit",
            "si",
            "sl",
            "sr",
            "no",
            # multi-row / tax-style headers (e.g. Sainath layout)
            "name",
            "product",
            "service",
            "particular",
            "taxable",
            "value",
            "igst",
            "cgst",
            "sgst",
            "total",
            "price",
        ]

        def _is_data_tok(t):
            t = t.replace(",", "")
            return bool(re.search(r"\d\.\d", t)) or bool(re.fullmatch(r"\d{3,}", t))

        # A column header can span several stacked rows — e.g.
        #   "Name of Product / Service | HSN / SAC | Taxable Value | IGST"
        #   "Qty | Rate | % | Total"
        #   "Amount"
        # Merge the leading label rows (header keywords, no data values) into
        # one combined header so every column label is seen. Single-row
        # headers (the common case) merge to just that one row.
        merged = []
        for row in table_rows:
            text = " ".join(w["text"].lower() for w in row)
            has_kw = any(k in text for k in HEADER_KEYWORDS)
            has_data = any(_is_data_tok(w["text"]) for w in row)
            if has_kw and not has_data:
                merged.extend(row)
            elif merged:
                break            # first data row -> header block ended
            # else: skip leading non-header rows until the header starts

        if merged:
            header_row = merged
        else:
            best_score = 0
            for row in table_rows:
                text = " ".join(w["text"].lower() for w in row)
                score = sum(1 for key in HEADER_KEYWORDS if key in text)
                if score > best_score:
                    best_score = score
                    header_row = row

        if header_row is None:
            return columns

        # ------------------------------------------
        # Sort left -> right
        # ------------------------------------------

        header_row = sorted(
            header_row,
            key=lambda w: w["x"]
        )

        # ------------------------------------------
        # Detect Columns
        # ------------------------------------------

        amount_locked = False   # "Taxable Value" wins the Amount column

        for word in header_row:

            txt = word["text"].lower().replace(".", "").strip()

            x = word["x"]

            if txt in ("si", "sl", "sr"):
                columns["SI"] = x

            elif txt == "no":
                columns.setdefault("SI", x)

            elif "description" in txt:
                columns["Description"] = x

            elif (txt in ("goods", "particulars", "particular")
                  or "name" in txt or "product" in txt):
                columns.setdefault("Description", x)

            elif "hsn" in txt:
                columns["HSN"] = x

            elif "sac" == txt:
                columns.setdefault("HSN", x)

            elif "qty" in txt or "quantity" in txt:
                columns["Quantity"] = x

            elif "rate" in txt or "price" in txt:
                columns["Rate"] = x

            elif txt == "per":
                columns["Per"] = x

            elif "disc" in txt:
                columns["Discount"] = x

            elif "taxable" in txt:
                # Pre-tax base amount; authoritative Amount column.
                columns["Amount"] = x
                amount_locked = True

            elif "igst" in txt or "cgst" in txt or "sgst" in txt:
                # Tax columns: kept only as partition anchors so their
                # amounts don't spill into the item Amount.
                columns.setdefault("IGST", x)

            elif txt == "total":
                columns.setdefault("Total", x)

            elif "amount" in txt:
                if not amount_locked:
                    columns["Amount"] = x

        # ------------------------------------------
        # Merge split headers
        # Example:
        # SI | No.
        # HSN | SAC
        # ------------------------------------------

        if "SI" not in columns:
            for w in header_row:
                if w["text"].lower().startswith("no"):
                    columns["SI"] = w["x"] - 40
                    break

        if "HSN" not in columns:
            for w in header_row:
                if "sac" in w["text"].lower():
                    columns["HSN"] = w["x"] - 40
                    break

        # ------------------------------------------
        # Sort columns by X position
        # ------------------------------------------

        columns = dict(
            sorted(
                columns.items(),
                key=lambda item: item[1]
            )
        )

        print("\nDetected Table Columns")
        print("-" * 40)

        for name, pos in columns.items():
            print(f"{name:<12}: {pos:.0f}")

        print("-" * 40)

        return columns
    def split_page_regions(
        self,
        rows
    ):

        left_rows = []
        right_rows = []
        table_rows = []
        footer_rows = []


        for row in rows:

            if not row:
                continue


            avg_x = sum(
                w["center_x"]
                for w in row
            ) / len(row)



            text = " ".join(
                w["text"].lower()
                for w in row
            )



            # -----------------------------
            # Table
            # -----------------------------

            if (
                "description" in text
                or
                "goods" in text
                or
                "hsn" in text
                or
                "quantity" in text
            ):

                table_rows.append(row)

                continue



            # -----------------------------
            # Footer
            # -----------------------------

            if (
                "amount chargeable" in text
                or
                "tax amount" in text
                or
                "declaration" in text
            ):

                footer_rows.append(row)

                continue



            # -----------------------------
            # Header split
            # -----------------------------

            if avg_x < 900:

                left_rows.append(row)

            else:

                right_rows.append(row)



        return {

            "Left": left_rows,

            "Right": right_rows,

            "Table": table_rows,

            "Footer": footer_rows

        }
    def detect_regions(
        self,
        rows
    ):

        all_words = [
            w
            for row in rows
            for w in row
        ]


        if not all_words:
            return {}


        page_width = max(
            w["right"]
            for w in all_words
        )


        middle = page_width / 2


        regions = {

            "LEFT": [],

            "RIGHT": [],

            "CENTER": []

        }


        for row in rows:


            row_center = sum(
                w["center_x"]
                for w in row
            ) / len(row)


            if row_center < middle * 0.8:

                regions["LEFT"].append(row)


            elif row_center > middle * 1.2:

                regions["RIGHT"].append(row)


            else:

                regions["CENTER"].append(row)



        return regions
    def clean_table_rows(self, table_rows):

        cleaned_rows = []
        # Still inside the table's (possibly multi-line) column-header
        # block — some layouts wrap it across 2+ short rows (e.g.
        # "Sl | Description of | ..." then "No. | Goods and Services").
        # Stays True until the first row that isn't dropped as a header,
        # so the structural fallback below only ever consumes a
        # contiguous run of header lines at the very start of the table,
        # never a genuine short/numberless line appearing later.
        in_header_block = True

        for row in table_rows:

            words_in_row = [w["text"].strip() for w in row if w["text"].strip()]
            text = " ".join(words_in_row)

            if not text:
                continue


            lower = text.lower()


            # Tax / total / round-off rows are dropped. Freight / courier /
            # forwarding etc. are KEPT — extract_items turns them into their
            # own (charge) line item (item 6).
            remove_words = [
                "output igst",
                "input igst",
                "igst",
                "cgst",
                "sgst",
                "tax amount",
                "total",
                "round off",
                "round-off",
                "roundoff",
            ]


            if any(x in lower for x in remove_words):
                continue


            # remove duplicate headers
            header_words = [
                "description",
                "hsn",
                "quantity",
                "rate",
                "amount"
            ]


            score = sum(
                1
                for x in header_words
                if x in lower
            )


            if score >= 3:
                continue

            # The table's own column-header row(s) (e.g. "No. | Goods and
            # Services") don't always match >=3 of the known header words
            # above — some layouts title their columns differently, or
            # wrap the header across multiple short lines. Structurally,
            # though, a header line is short and has no numeric content
            # at all (no SI no., HSN, qty, rate or amount) — a genuine
            # item/description row practically always carries at least
            # one number. Only applies while still inside the leading
            # header block, so a genuine short/numberless line appearing
            # later (e.g. a wrapped description) is never dropped this way.
            if (in_header_block and len(words_in_row) <= 4
                    and not any(re.search(r"\d", w) for w in words_in_row)):
                continue

            in_header_block = False
            cleaned_rows.append(row)


        return cleaned_rows
    
    def split_serial_description(self, rows):
        """
        Split OCR text like:
        1Adaptor
        2Laptop

        into:
        1   Adaptor
        2   Laptop
        """

        new_rows = []


        for row in rows:

            updated_row = []


            for word in row:

                text = word["text"].strip()


                match = re.match(
                    r"^(\d+)\s*([A-Za-z].+)$",
                    text
                )

                # A token that is wholly a measurement ("8GB", "1TB",
                # "2666MHz", "500GB") is NOT a serial glued to a description —
                # it is a size in the item description. Leave it intact so it
                # is not mis-split into a fake serial + "GB"/"TB".
                if match and re.fullmatch(
                    r"\d+\s*(GB|TB|MB|KB|GHZ|MHZ|HZ|MAH|WH|W|V)",
                    text,
                    re.IGNORECASE,
                ):
                    match = None


                if match:

                    serial = match.group(1)

                    desc = match.group(2)


                    # -----------------------------
                    # Serial box
                    # -----------------------------

                    serial_box = word.copy()

                    serial_box["text"] = serial

                    serial_box["right"] = (
                        serial_box["x"]
                        +
                        len(serial) * 10
                    )


                    serial_box["width"] = (
                        serial_box["right"]
                        -
                        serial_box["x"]
                    )


                    serial_box["center_x"] = (
                        serial_box["x"]
                        +
                        serial_box["width"] / 2
                    )



                    # -----------------------------
                    # Description box
                    # -----------------------------

                    desc_box = word.copy()

                    desc_box["text"] = desc


                    DESCRIPTION_START = 180

                    desc_box["x"] = DESCRIPTION_START
                    desc_box["left"] = DESCRIPTION_START

                    desc_box["left"] = desc_box["x"]


                    desc_box["width"] = (
                        word["right"]
                        -
                        desc_box["x"]
                    )


                    desc_box["center_x"] = (
                        desc_box["x"]
                        +
                        desc_box["width"] / 2
                    )


                    updated_row.append(
                        serial_box
                    )


                    updated_row.append(
                        desc_box
                    )


                else:

                    updated_row.append(
                        word
                    )


            updated_row.sort(
                key=lambda x:x["x"]
            )


            new_rows.append(
                updated_row
            )


        return new_rows
    def merge_table_header_rows(self, rows):

        merged = []
        i = 0

        while i < len(rows):

            current = rows[i]

            if i + 1 < len(rows):

                current_text = " ".join(w["text"] for w in current).lower()
                next_text = " ".join(w["text"] for w in rows[i + 1]).lower()

                if current_text.startswith("si") and next_text == "no.":

                    current.extend(rows[i + 1])
                    current.sort(key=lambda x: x["x"])

                    merged.append(current)
                    i += 2
                    continue

            merged.append(current)
            i += 1

        return merged