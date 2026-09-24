
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


# A properly-focused scan of a text document has sharp glyph edges, which
# show up as a high-variance response to a Laplacian (edge-detection)
# filter; a shaky/blurry/out-of-focus capture smears those edges, dropping
# the variance sharply. Calibrated empirically against every already-
# working scanned page in the trained-format corpus (lowest observed:
# ~56, a short, sparse invoice with little text to produce edges from even
# in perfect focus) - set well below that with a safety margin, so this
# only catches a scan meaningfully worse than anything already processed
# successfully, not a merely sparse or simple one.
_BLUR_VARIANCE_MIN = 25.0


def _looks_like_blurry_page(image):
    """True when a rasterised page is too blurry/shaky to trust for
    extraction - see _BLUR_VARIANCE_MIN's calibration note above."""
    if image is None or image.size == 0:
        return False
    gray = cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)
    return cv2.Laplacian(gray, cv2.CV_64F).var() < _BLUR_VARIANCE_MIN


# "|" is a vertical-stroke glyph OCR genuinely confuses for "I" (read as
# "I" on one page's scan of the SAME invoice, correctly as "|" on
# another) - canonicalized away before comparing two pages' own "Invoice
# No." readings, so this specific noise never fragments one multi-page
# invoice into several phantom ones. NOT a general fuzzy/similarity match
# (deliberately) - two genuinely different, similarly-formatted invoice
# numbers (e.g. sequential "...541" vs "...542") must still compare as
# different, so only this exact narrow confusion is normalized, nothing
# broader (extend the translate table if/when another such pair turns up).
_INVOICE_NO_CANON = str.maketrans({"|": "I"})


def _invoice_no_differs(a, b):
    """True when two pages' own "Invoice No." readings are different
    invoices, not just a stray "|"/"I" OCR misread on the same one - see
    _INVOICE_NO_CANON's own note.
    Sample: _invoice_no_differs('26|1TN1L2501832', '26I1TN1L2501832') -> False
    Sample: _invoice_no_differs('D/2026-27/541', 'D/2026-27/542') -> True"""
    return a.upper().translate(_INVOICE_NO_CANON) != b.upper().translate(_INVOICE_NO_CANON)


def _is_near_duplicate(value, already):
    """True when `value` (one page's own field reading, e.g. a Seller
    Address line) already appears, punctuation/whitespace aside, SOMEWHERE
    within `already` (the group's accumulated multi-line value so far) -
    a genuine multi-page invoice's own repeated letterhead is rarely
    printed byte-identically page to page (e.g. "...Chennai," on one page,
    "...Chennai" - no trailing comma - on another), so the exact-substring
    check `value not in merged_fields[key]` alone lets that trivial
    difference through and needlessly duplicates the SAME line into the
    merged result.
    Sample: _is_near_duplicate('...Chennai', '...Chennai,') -> True"""
    norm = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
    value_n = norm(value)
    return bool(value_n) and value_n in norm(already)


# A physical-dimension spec with no unit word of its own (e.g. `18.5"` for a
# monitor's screen size) - digits then a bare inch/feet mark, nothing else.
_SIZE_SPEC_RE = re.compile(r'^\d+(\.\d+)?["\']$')


def _norm_gstin(value):
    """Uppercase, alphanumerics only — for comparing GSTIN values."""
    return re.sub(r"[^A-Z0-9]", "", (value or "").upper())


# "Serial Number:XXXX", "Serial No.: XXXX", "S/N: XXXX", "Sr. No: XXXX" - the
# value is one unspaced token (letters/digits/dashes); only stripped when it
# contains a digit, so a plain word after the label is never eaten.
_SERIAL_NO_RE = re.compile(
    r"(?i)\b(?:serial\s*(?:no\.?|number)|s\s*/\s*n|sr\.?\s*no\.?)\s*[:#\-]?\s*"
    r"(?P<val>[A-Za-z0-9][A-Za-z0-9\-]*)"
    # ...and any further serials of the same list ("S/N: A1B2C3 / D4E5F6, ..."):
    # unspaced UPPERCASE alphanumerics of 6+ chars that contain a digit.
    r"(?:[\s,/&]+(?-i:(?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{5,})(?![A-Za-z0-9\-]))*"
)


# Dell hardware serial in its hyphenated form, printed with no label at all.
_DELL_TAG_RE = re.compile(r"(?i)\bCN-[0-9A-Z]{6}(?:-[0-9A-Z]{3,5}){3,}\b")


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
        """Rasterise a single PDF page at the OCR DPI for a page that must
        be OCR'd (no usable text layer of its own).

        PyMuPDF (fitz) first, not poppler - a real, measured quality
        difference, not a style preference: the SAME PDF/page/DPI rendered
        via poppler (pdf2image's convert_from_path) then fed to PaddleOCR
        read a clearly-legible line as "ZAAAA P NAAA TAAAAAAAH IAAAAA
        NAAAAAAAA AAAAA AR" at confidence 0.55; the identical page
        rendered via fitz instead and fed to the exact same PaddleOCR call
        read it correctly ("22 SENAI THALAIVAR MALLIGAI HABIBULLAH ROAD T
        NAGAR") at confidence 0.99. fitz is already required for this
        method to be reached at all (see _page_inputs's own `fitz is not
        None` guard), so no extra fallback is needed here."""
        with fitz.open(pdf_path) as doc:
            if not (1 <= page_no <= doc.page_count):
                return None
            pix = doc[page_no - 1].get_pixmap(dpi=_OCR_DPI, alpha=False)
            arr = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
            return cv2.cvtColor(arr, cv2.COLOR_RGB2BGR)

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

            # Two boxes on the same physical line occupy distinct
            # (non-overlapping) x-ranges - that's what "side by side"
            # means. If the incoming box's x-range substantially overlaps
            # a box already in this row, they can't really be neighbours
            # on one line; they're stacked lines (e.g. a tightly-leaded
            # company-name/address letterhead) whose centers happened to
            # land within line_threshold of each other. Split them into
            # separate rows instead of concatenating unrelated lines.
            stacked = any(
                min(box["right"], other["right"]) - max(box["x"], other["x"])
                > 0.5 * min(box["width"], other["width"])
                for other in current_row
            )

            if (not stacked) and abs(

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
        # `allow_scanned=True` (config_store's per-type "allow_scanned_pdfs_
        # part"/"_service" toggles - see processor.py) skips that rejection:
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
            blurry_count = sum(1 for image in rasterised if _looks_like_blurry_page(image))
            if blurry_count * 2 > len(rasterised):
                raise ValueError(
                    "Document looks too blurry/shaky to read reliably "
                    "(low edge sharpness) — unsupported."
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
        # "Invoice No." and it CLEARLY differs from the current group's — a
        # page with no Invoice No. of its own never triggers a split, so
        # ordinary multi-page single invoices are unaffected. "Clearly"
        # (see _invoice_no_differs) rather than a bare string inequality:
        # the same physical invoice number can OCR slightly differently
        # page to page on a poor scan (e.g. "26|1TN1L2501832" on page 1,
        # "26I1TN1L2501832" on page 2 - one stray "|"/"I" swap), which
        # would otherwise fragment one genuine multi-page invoice into
        # several phantom ones.
        groups = []

        def _new_group():
            return {"text": [], "fields": [], "items": [], "tax": [], "invoice_no": "",
                     "page_start": None, "page_end": None}

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
                    and _invoice_no_differs(page_invoice_no, groups[-1]["invoice_no"])):
                groups.append(_new_group())

            group = groups[-1]
            if page_invoice_no and not group["invoice_no"]:
                group["invoice_no"] = page_invoice_no
            if group["page_start"] is None:
                group["page_start"] = page_no
            group["page_end"] = page_no


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

        # A party's own Name is always fully printed on the single page
        # that introduces it - never legitimately split/continued onto a
        # later page the way an address or terms block might be (see the
        # "Append multiline values" rule below, which exists for exactly
        # that legitimate case). Without this exception, an unrelated
        # later-page label that happens to read as a bare name - e.g. a
        # "Bank Details" footer's "Name : Indusand bank" line, picked up
        # by that page's own independent anchor-extraction pass via its
        # "first unclaimed line = party name" fallback (a fresh page
        # starts that pass with nothing claimed yet) - gets silently
        # concatenated onto the correct first-page value instead of being
        # ignored, corrupting the real Seller/Buyer/Consignee Name.
        #
        # "Invoice No." and "Vendor Code" are the same category of problem
        # for a different reason: a single fixed identifier, reprinted
        # identically on every page of one multi-page invoice, but OCR'd
        # SLIGHTLY differently page to page on a poor scan (the exact
        # "|"/"I" noise _invoice_no_differs already tolerates for the
        # page-grouping decision itself) - "value not in merged_fields[key]"
        # below only catches an EXACT non-match, so two such near-identical
        # readings still get wrongly concatenated into one garbled value
        # ("26|1TN1L2501832\n26I1TN1L2501832") instead of keeping just the
        # first page's own reading.
        _SINGLE_VALUE_FIELDS = {
            "Seller Name", "Buyer Name", "Consignee Name",
            "Invoice No.", "Vendor Code",
        }

        merged_fields = {}
        for fields in group["fields"]:
            for key, value in fields.items():
                if not value:
                    continue
                if key not in merged_fields:
                    merged_fields[key] = value
                elif key in _SINGLE_VALUE_FIELDS:
                    continue
                elif value not in merged_fields[key] and not _is_near_duplicate(
                    value, merged_fields[key]
                ):
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
            # 1-based, inclusive - which of the source PDF's own physical
            # pages this invoice's content actually came from. Downstream
            # (invoice_schema/database/app.py's PDF endpoint) uses this so
            # the PDF viewer/download shows this invoice's WHOLE span, not
            # just its first page, for a genuine multi-page invoice sharing
            # a file with other invoices (see PageStart/PageEnd's own use).
            "PageStart": group["page_start"],
            "PageEnd": group["page_end"],
        }

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

            # A genuinely simple, non-GST invoice table (e.g. a generic
            # small-business template billing a flat "DESCRIPTION |
            # AMOUNT" per line, no HSN/Qty/Rate/tax breakdown at all -
            # SHWETMANI ENTERPRISES's own layout) never reaches the 3-
            # keyword threshold above no matter how its header is split
            # across rows, since there's no third table-related word
            # anywhere on the page to find. "description" + "amount"
            # together, even alone, is still an unambiguous item-table
            # header - virtually no other part of a real invoice pairs
            # those two words on one row.
            if {"description", "amount"} <= matched:

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
            
    def extract_items(
            self,
            table_rows,
            columns=None
    ):

        items = []

        current_item = None

        # Some layouts wrap a long description onto the line BEFORE the row
        # carrying the serial number/qty/amount, instead of after it - most
        # visibly before the very FIRST item (no item has started yet, so
        # that lead-in text has nowhere to attach and would otherwise be
        # silently dropped) - buffered here and prepended to the next
        # item's Description once it actually starts. Also reused (see
        # last_peelable_addition below) for the exact same layout
        # recurring between LATER items, where the lead-in line gets
        # wrongly appended onto the PREVIOUS item first before anyone
        # notices it was actually the next item's own opening line.
        pending_lead_in = ""

        orphan_amount = None

        # A vendor whose numeric columns (HSN/Qty/Rate/Amount) sit
        # vertically CENTERED against a multi-line wrapped description
        # cell (common in HTML-table-rendered invoices) prints that row's
        # serial+values on a physical OCR row in the MIDDLE of the cell,
        # not its top line - e.g. a 3-line cell "DELL ADAPTER 65W C TYPE" /
        # "(Latitude 5420 Adaptor)+values" / "Serial No.: ...". The top
        # line ("DELL ADAPTER 65W C TYPE") has no serial of its own, so it
        # falls through as a plain continuation of the item running before
        # it started - by the time the row WITH the serial/values is seen,
        # that opening line has already been wrongly glued onto the
        # PREVIOUS item's Description. Tracks the single most recent
        # continuation line appended this way (None once anything else
        # happens to current_item, so only a line DIRECTLY, immediately
        # before a new item's own row is ever a candidate) so it can be
        # peeled back off the previous item and re-attached as the new
        # item's own lead-in instead, right when that new item starts (see
        # its own use below). Only a non-parenthetical line qualifies - a
        # "(...)" line is a clarifying suffix of whatever precedes it
        # (e.g. "(Epson M3170 Maintenance Box)" genuinely belongs to the
        # item before it, not the one after), never a new item's own
        # opening phrase.
        last_peelable_addition = None

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

        # "continued"/"contd" is a multi-page invoice's own page-footer
        # marker ("continued ...", printed below the table on every page
        # but the last), and "this is a computer generated invoice" is the
        # standalone footer line every page of this layout prints at the
        # very bottom - both have no leading serial and real letters, so
        # without excluding them they silently glue onto whichever item
        # happened to be last on that page's table (e.g. "Hinges ... Part
        # No : 5H50S29037 continued" / "... This is a Computer Generated
        # Invoice"). "Rounded Off" (past tense) is the actual wording
        # several vendors print ("Dell Mouse MS116 Rounded Off", "...Less :
        # Rounded Off (-)0.40") - "round off"/"round-off"/"roundoff" above
        # don't substring-match it (the "ed" breaks it), so it slipped
        # through and glued onto the previous item exactly like
        # "continued" did.
        # Hoisted to function scope (not just the continuation-line branch
        # below that originally needed it): also needed by the "no leading
        # serial" item-start fallback further down, so a genuine tax-
        # summary/footer row with a real Amount on it (e.g. "Taxable
        # Amount  ₹56,888.00") isn't mistaken for a whole new phantom line
        # item just because it has real words and a number that parses as
        # an amount.
        EXCLUDE_KW = ("output", "igst", "cgst", "sgst", "tax amount",
                      "total", "round off", "round-off", "roundoff",
                      "rounded off",
                      "rupees", "inr ", "grand total", "tax rate",
                      "taxable", "amount chargeable", "in words",
                      "declaration", "continued", "contd",
                      "computer generated invoice",
                      # A vendor that has no separate HSN/SAC table
                      # column instead prints "HSN/SAC-84733099" as
                      # its own line within the description cell
                      # itself (e.g. Shupla IT) - real printed text,
                      # but Service First's own catalog description
                      # for the part never includes this restatement,
                      # so keeping it in Description only prevents an
                      # otherwise-clean match. The digits themselves are
                      # still recovered separately - see the HSN-caption
                      # capture just above the charge-line handling below.
                      "hsn/sac",
                      # A vendor that repeats the invoice's own Buyer's
                      # Order No. as an extra line inside the item's
                      # description cell (e.g. "PO # : SPRPUR/2026/09/04 -
                      # 86476" printed right under the part name) - real
                      # printed text, but Service First's own catalog
                      # description for the part never includes it, so
                      # keeping it glued onto Description only prevents an
                      # otherwise-clean SF match (same rationale as
                      # "hsn/sac" above - the PO No. itself is already
                      # captured separately as Buyer's Order No.).
                      "po #",
                      # A bank-details footer block (account for payment,
                      # not part of the invoice's own line items) sitting
                      # directly below the table's "Total" row (e.g.
                      # Printer World's "Bank Name / Beneficiary / Account
                      # No / IFSC Code / Branch: SECUNDERABAD / Way Bill No
                      # | Net Amount 1,711.00") - without these, "Branch:
                      # SECUNDERABAD" glues onto the last real item's
                      # Description, and "Way Bill No | Net Amount
                      # 1,711.00" (real words + the invoice's own real
                      # Amount, no leading serial) is indistinguishable from
                      # a genuine item and becomes a phantom line with a
                      # blank HSN/Quantity - the same failure shape as the
                      # "Taxable Amount"/"Total" case above.
                      "way bill", "net amount", "bank name", "beneficiary",
                      "account no", "ifsc code", "branch",
                      # R Logic's own footer/summary lines: "TCS | 0.00" (Tax
                      # Collected at Source, a real GST line item on the
                      # invoice's own tax-summary block, not a purchased
                      # part) has no leading serial and a genuine trailing
                      # amount, so without this it's indistinguishable from
                      # a real item and becomes a phantom 5th line with
                      # blank HSN - the same failure shape as "Taxable
                      # Amount"/"Total" above. "Important Note :" and
                      # "Remarks : Based On Sales Orders ..." are boilerplate
                      # invoice notes printed right after the last charge
                      # line (Packing and Forwarding Expenses) with no
                      # amount of their own, so they'd otherwise fold into
                      # that charge's own Description via the "bare
                      # charge-keyword row wraps onto the next line" rule
                      # just above.
                      "tcs", "important note", "remarks",
                      # A simple small-business invoice's own summary/
                      # signature block below the item list (SHWETMANI
                      # ENTERPRISES: "Advance 0/-" / "Make all cheque
                      # payable to..." / "Customer Approval Signature:") -
                      # same failure shape as "Taxable Amount"/"Total"
                      # above: no leading serial, real words, sometimes a
                      # trailing amount, so without these it folds into the
                      # last genuine item's own Description instead of
                      # being recognized as page furniture. "Subtotal" and
                      # "Tax" alone aren't included here - both are already
                      # too generic/risky to blanket-exclude (a real item
                      # description could legitimately contain either).
                      "advance", "make all cheque", "customer approval signature",
                      "total due",
                      # A freelancer/service-call invoice's own case-detail
                      # block below the (single) real billed line - "Name",
                      # "Call Status-...", "Pan Card-...", "Aadhar Card-...",
                      "bank-", "a/c no", "ifsc-", "call slip enclosed",
                      # A small-business invoice's own payment/terms footer
                      # (BABA INFOTECH: "PAYMENT MODE / By Bank Ac. ... /
                      # Ifsc Code :- ... / AMOUNT IN WORD:- ... / NOTE:-
                      # ... / If you have any questions about this
                      # invoice") - all page furniture below the one real
                      # billed line, same failure shape as the bank block
                      # above, just worded differently.
                      "payment mode", "bank ac", "ifsc", "in word:",
                      "note:-", "if you have any questions",
                      "verified by", "seal and signature",
                      "system generated invoice", "pan card", "aadhar card",
                      "call status", "subject to", "jurisdiction",
                      "upi id")


        def _strip_currency(value):
            # A Rate/Amount token glued to its currency symbol ("₹ 14000.00",
            # "Rs. 250", "INR250") reads as text, not a number, unless the
            # symbol/prefix is stripped first. A trailing "/-" (or "/=") is
            # the same idea in reverse - a common Indian-invoice shorthand
            # for "only" ("8400/-" = "Rs. 8,400 only", e.g. SHWETMANI
            # ENTERPRISES's own simple per-line amounts) - without this,
            # is_number()/clean_number() reject the whole token as text,
            # so the row never registers as a genuine item start at all.
            value = re.sub(r"[/=]-?\s*$", "", value.strip())
            return re.sub(
                r"^\s*(₹|\$|€|£|rs\.?|inr)\s*",
                "",
                value,
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

        def _hsn_token_value(value):
            """The normalized (plain-digit) HSN/SAC value a table-cell
            token represents, or None if it isn't HSN-shaped at all. Some
            vendors (e.g. R Logic) print the code split by its own real
            chapter.heading.sub-heading structure - "84.73.3020" for what's
            really HSN 84733020 - rather than as one plain digit run;
            without this, is_hsn's own plain \\d{4,10} check never matches
            it, the HSN column stays blank, and the whole invoice fails the
            mandatory-field gate for it despite the PDF genuinely stating a
            code. Accepts a bare optional trailing letter the same as the
            plain form does (see is_hsn's own docstring)."""
            v = value.strip()
            m = re.match(r"^\d{4,10}[A-Za-z]?$", v)
            if m:
                return v
            if re.fullmatch(r"\d{2,4}(?:\.\d{1,4}){1,3}[A-Za-z]?", v):
                digits = re.sub(r"\.", "", v)
                letter = digits[-1] if digits[-1].isalpha() else ""
                num_part = digits[:-1] if letter else digits
                if 4 <= len(num_part) <= 10:
                    return num_part + letter
            return None



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
            Pcs/...) are always noise, a GST cell's combined amount+percent
            text (e.g. "Rs 45.00 (18%)") is always noise regardless of
            whether it happens to parse as a plain number, and a number is
            noise ONLY when it sits under the Quantity/Rate/Amount column -
            a genuine spec number that happens to be part of the
            description (e.g. "90" in "90 Days Warranty") is printed in the
            Description column's own territory and must survive, while a
            Quantity/Rate/Amount figure that leaked onto this row (already
            harvested above) should not be echoed into the text too."""
            out = []
            for w in wds:
                t = w["text"].strip()
                if is_unit(t):
                    continue
                if value_cols:
                    col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                    if col in ("GST", "IGST"):
                        continue
                    if is_number(t) and col in ("Quantity", "Rate", "Amount"):
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
                        "Per", "Amount", "Discount", "IGST", "GST", "Total")
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
                    if col == "HSN" and _hsn_token_value(t.replace(",", "")) is not None:
                        return True
                    if col in ("Quantity", "Rate", "Amount") and (
                        is_number(t) or number_with_unit(t) is not None
                    ):
                        return True
                elif _hsn_token_value(t.replace(",", "")) is not None or is_number(t):
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

        # A payment/terms footer block (BABA INFOTECH: "PAYMENT MODE / By
        # Bank Ac. ... / NOTE:- ..." wrapped over several lines, then a "For
        # <company>" signature line) sits below the last real item and
        # runs to the end of the table - its wrapped continuation lines
        # carry none of the footer keywords themselves, so once its
        # opening marker is seen every later no-serial row is footer text,
        # never more description for the item above it. A genuine new item
        # still starts through its own serial/values path, unaffected.
        FOOTER_START_KW = ("payment mode", "bank ac", "ifsc", "in word:",
                           "note:-", "if you have any questions")
        footer_started = False


        # Whether any later row opens with its own item serial ("1 Coconut
        # Pci Express Lan Card ... 750.00"). When one does, a numbers-only row
        # above it is a subtotal, never a values-first item (see below).
        serial_rows_exist = any(
            r and re.fullmatch(
                r"\d{1,3}[.)]?",
                sorted(r, key=lambda w: w.get("center_x", w["x"]))[0]["text"].strip(),
            )
            for r in table_rows[1:]
        )

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

            if any(k in lower for k in FOOTER_START_KW):
                footer_started = True

            # A skewed scan/photo can print the FIRST item's amount on a row
            # above the item's own row, sharing it with nothing but the
            # header's wrapped "No." word ("No. ... 900.00", then "1 Service
            # Charges Received 998713"). Nothing has started yet, so the lone
            # figure under the Amount column is buffered and given to the
            # first item if that item ends up with no amount of its own.
            if (current_item is None and not items and orphan_amount is None
                    and value_cols.get("Amount") is not None):
                nums_here = [
                    w for w in words
                    if is_number(w["text"].strip().replace(",", ""))
                    and abs(w["x"] - value_cols["Amount"]) < 150
                ]
                others = [w for w in words if w not in nums_here]
                if (len(nums_here) == 1 and clean_number(nums_here[0]["text"]) not in (None, 0)
                        and all(re.fullmatch(r"(?i)no\.?|s\.?\s*no\.?|sl\.?", w["text"].strip())
                                for w in others)):
                    orphan_amount = clean_number(nums_here[0]["text"])
                    continue



            # Values-first item (Shupla IT): the item's own "01 | QTY | 875.00 |
            # QTY | 875.00" quantity/rate/amount row is printed ABOVE its
            # description lines, with nothing but unit words on it. Nothing
            # has started yet, so open the item from those values; the
            # description rows that follow attach to it as continuation.
            if (current_item is None and not items and not footer_started
                    and not serial_rows_exist
                    and value_cols.get("Amount") is not None):
                by_col = {}
                other_text = []
                for w in words:
                    t = w["text"].strip()
                    if is_number(t.replace(",", "")):
                        col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                        if col in ("Quantity", "Rate", "Amount") and col not in by_col:
                            by_col[col] = clean_number(t)
                        else:
                            other_text.append(t)
                    elif not is_unit(t):
                        other_text.append(t)
                if ("Amount" in by_col and len(by_col) >= 2 and not other_text
                        and by_col["Amount"] and (by_col.get("Rate") or by_col.get("Quantity"))):
                    current_item = {
                        "SI": "1",
                        "Description": pending_lead_in,
                        "HSN": None,
                        "Quantity": by_col.get("Quantity"),
                        "Rate": by_col.get("Rate"),
                        "Amount": by_col["Amount"],
                        "charge": False,
                    }
                    pending_lead_in = ""
                    item_seq = 1
                    continue

            # "HSN/SAC-84716060" printed on its own line under an item's
            # description carries that item's HSN.
            if current_item is not None and not current_item.get("HSN"):
                hsn_m = re.search(r"hsn\s*/?\s*sac\s*[-:]?\s*(\d{4,8})\b", lower)
                if hsn_m:
                    current_item["HSN"] = hsn_m.group(1)
                    continue

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

                # A tax/total row between an item's wrapped description line
                # and the next item's own row (HP: "machinery and equip",
                # then CGST / SGST / "Total for Item:", then "000003 ...")
                # proves that wrapped line belongs to the item above, not to
                # the next one - it must not be peeled onto it as a lead-in.
                last_peelable_addition = None
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
                # Same measurement/warranty-period guard as split_serial_
                # description's own two checks (a size like "8GB ..." or a
                # validity period like "45 Days ...") - this token is real
                # description content, not a serial number starting a new
                # item. Without this, a wrapped continuation line beginning
                # this way (e.g. "45 DAYS WARRNATY", the tail of a multi-
                # line description) gets misread as "item start, serial
                # 45", then folded back into the current item's Description
                # anyway once row_has_values() finds nothing else on the
                # row - but by then the leading number itself is already
                # gone (only GB/TB/MB/KB had their own narrow reattachment
                # fix-up below; DAYS and the rest of the unit list never
                # did, and the reattachment only helps `cont`'s exact
                # prefix anyway, not text a caller further down builds).
                if glued and re.match(
                    r"^\d{1,3}[.)]?\s*(GB|TB|MB|KB|GHZ|MHZ|HZ|MAH|WH|W|V|DAYS?)\b",
                    first,
                    re.IGNORECASE,
                ):
                    glued = None
                # General case covering any OTHER part/SKU code that
                # happens to start with a digit (e.g. "4QL27-80101", a
                # genuine part number wrapped onto its own line within a
                # multi-line item description) - same reasoning as
                # split_serial_description's own general check: a real
                # English word (a genuine "1CPU"-style serial + item name)
                # always has a lowercase letter somewhere; a part/SKU code
                # never does. Checked against just the word immediately
                # after the digit, not the whole remainder, in case a
                # longer wrapped line genuinely continues into real prose.
                if glued:
                    remainder_words = glued.group(2).split()
                    first_word = remainder_words[0] if remainder_words else ""
                    # A plain unit abbreviation ("QTY", "PCS") is legitimate
                    # glued text even though it has no lowercase letter -
                    # only reject when it ALSO isn't a recognised unit, so a
                    # bare "<qty> <UNIT>" row (e.g. the invoice's own
                    # unlabelled grand-total line "03 QTY") still parses as
                    # serial="03"/desc="QTY" and can be folded downstream by
                    # the has_desc_text/row_has_values checks, instead of
                    # silently skipping serial detection altogether and
                    # falling through to a different code path that has no
                    # such fold-back safeguard.
                    # A vendor that prints its item names in ALL CAPS (e.g.
                    # Bestech: "1 HP PRO BOOK 440 G8 LAPTOP", "3 COURIER
                    # CHARGES") has no lowercase letter anywhere either, so
                    # the lowercase check alone would wrongly reject every
                    # one of its genuine items too - the real distinguishing
                    # signal for "this is actually a bare part/SKU code, not
                    # a description" is that a code is always ONE unspaced
                    # token ("4QL27-80101"), while a genuine description -
                    # caps or not - is almost always multiple words. Only
                    # reject when the remainder is a single word AND has no
                    # lowercase letter; two or more words survives either
                    # way.
                    if (
                        first_word
                        and not is_unit(first_word)
                        and not any(c.islower() for c in first_word)
                        and len(remainder_words) < 2
                    ):
                        glued = None
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
            # A tax-summary/footer row with its own real Amount - "Taxable
            # Amount  ₹56,888.00", "Total  ₹66,788.00" - has exactly the
            # same shape (real words, a genuine amount, no leading serial)
            # and was otherwise indistinguishable from a real item lacking
            # only its own Quantity/Rate, becoming a phantom item that then
            # wrongly routes the whole invoice to NEW TEMPLATE for
            # "missing" fields that were never a real line item's to begin
            # with. EXCLUDE_KW is the same footer/summary-keyword list the
            # continuation-line path below already uses for this exact
            # reason.
            # The HSN check is column-gated (not "any 4-10 digit token
            # anywhere in the row") for the same reason as row_has_values
            # below: a wrapped description can itself contain a bare number
            # that happens to be HSN-shaped - e.g. a part number OCR split
            # across two lines as "1000" + "Base-T..." (from
            # "1000Base-T...") lands the "1000" fragment under the
            # Description column, not HSN, and would otherwise start a
            # phantom new item out of a genuine continuation row.
            # An unlabelled CGST/SGST/IGST amount sitting on its own row
            # (e.g. a scanned page whose "CGST"/"SGST" text OCR'd into
            # nothing recognisable) has exactly the same shape the
            # fallback below looks for - a bare Amount-column value with
            # no serial - and would otherwise become a phantom second
            # item with fabricated Quantity/Rate once nothing else marks
            # it as tax data (see EXCLUDE_KW above, which can't help here
            # since the identifying word itself never made it into the
            # OCR text at all). Caught structurally instead: India's GST
            # rates are a fixed, small set of slabs - if this row's own
            # Amount-column value is (within a few paise, for rounding)
            # the CURRENTLY OPEN item's own Amount times one of the half-
            # rates (CGST/SGST split) or full rates (IGST) EXPECT to
            # apply, it's that item's own tax line, not a new item.
            tax_subline_amt = None
            if current_item and current_item.get("Amount") and value_cols:
                # Of the numbers bucketed under the Amount column, the one
                # sitting CLOSEST to that column's own x is the row's real
                # line amount - not merely the first one found: a row that
                # also carries a tax-amount cell left of it (Anakage's
                # "... 2,27,910.00 | 18% | 41,023.80 | 2,27,910.00", the
                # 41,023.80 being exactly 18% of the previous line) used to
                # have that tax cell read as THE amount, so a genuine second
                # item with its own full set of values was dismissed as a
                # tax line and folded into the item above it.
                best_gap = None
                for w in words:
                    t = w["text"].strip()
                    col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                    if col != "Amount":
                        continue
                    v = clean_number(t)
                    if v is None:
                        v = number_with_unit(t)
                    if v is None:
                        continue
                    gap = abs(w["x"] - value_cols["Amount"])
                    if best_gap is None or gap < best_gap:
                        best_gap = gap
                        tax_subline_amt = v
            looks_like_tax_subline = tax_subline_amt is not None and any(
                abs(tax_subline_amt - current_item["Amount"] * r / 100) < 0.5
                for r in (2.5, 5, 6, 9, 12, 14, 18, 28)
            )

            if (
                serial is None
                and value_cols
                and not looks_like_tax_subline
                and not any(k in lower for k in CHARGE_KW)
                and not any(k in lower for k in EXCLUDE_KW)
                and not re.search(r"round(?:ed)?[\s-]*off", lower)
                # "Total" is already in EXCLUDE_KW, but a scanned/photocopied
                # page can OCR-drop its trailing letter ("Tota") - e.g. this
                # exact row, "Nos | Tota | 1 | 2,600.00", is genuinely the
                # invoice's own Total row (quantity "1 Nos", amount
                # "2,600.00"), not a second line item. Word-boundary regex
                # (not a plain EXCLUDE_KW substring) specifically so this
                # doesn't also match "tota" buried inside an unrelated real
                # word (e.g. "Toyota").
                and not re.search(r"\btota\b", lower)
                and (
                    any(
                        _hsn_token_value(t.replace(",", "")) is not None
                        and min(value_cols, key=lambda c: abs(w["x"] - value_cols[c])) == "HSN"
                        for w, t in zip(words, texts)
                    )
                    or row_has_amount(words)
                )
                and any(
                    re.search(r"[A-Za-z]{2,}", t) and not is_unit(t)
                    for t in texts
                )
                # An item whose Qty/Rate/Amount are printed on the line
                # BELOW its description - a "Serial Number: CN00HPF3..." /
                # "S/N: ..." / "Part No: ..." detail line carrying the row's
                # own HSN and price cells (Channel Four) - hasn't had its
                # values yet, so that detail line completes it (recovered by
                # the continuation branch below) rather than starting a
                # phantom second item of its own.
                and not (
                    current_item is not None
                    and current_item.get("Amount") is None
                    and re.match(
                        r"^\W*(serial|s/?n\b|sr\.?\s*no|imei|part\s*(no|number)|model)",
                        lower.strip(),
                    )
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
                # A genuine new item needs BOTH a real value (row_has_values)
                # AND real descriptive text naming the product - a row with
                # values but no description at all is exactly the shape of
                # the invoice's own grand-total line (e.g. "03 QTY  ₹
                # 10,797.00", no label on that row - the total quantity and
                # total amount, structurally identical to a genuine item's
                # own leading serial + amount). Without this, that total
                # row's bare "03" reads as a real serial (row_has_values
                # sees a genuine Amount) and becomes a standalone phantom
                # item with a blank Description.
                desc_clean = desc.strip()
                has_desc_text = (
                    desc_clean and not is_unit(desc_clean) and not is_number(desc_clean)
                ) or any(
                    w["text"].strip() and not is_unit(w["text"].strip())
                    and not is_number(w["text"].strip())
                    for w in cont_words
                )
                # The NEXT sequential serial + a real multi-word product name
                # (>= 3 words), after an item that already has its values,
                # opens the next item even though this row carries no values
                # of its own: its Qty/Rate/Amount are printed on the detail
                # line BELOW it (Channel Four's "2 USB MOUSE DELL MS116" /
                # "Serial Number: ... 84716060 2 ₹254.24 ₹508.48"). A wrapped
                # description fragment that merely starts with a digit
                # ("2 GB DDR4") has too few name words to qualify.
                starts_next_item = bool(
                    serial_from_token
                    and current_item is not None
                    and current_item.get("Amount") is not None
                    and serial.isdigit()
                    and int(serial) == item_seq + 1
                    and not row_has_values(cont_words)
                    and len({
                        m.lower()
                        for m in re.findall(
                            r"[A-Za-z]{2,}",
                            (desc or "") + " " + " ".join(w["text"] for w in cont_words),
                        )
                        if not is_unit(m)
                    }) >= 3
                )
                if (
                    serial_from_token
                    and current_item is not None
                    and not starts_next_item
                    and (not row_has_values(cont_words) or not has_desc_text)
                ):
                    # A digit-led row can still be page furniture, not a
                    # genuine wrapped description - e.g. a freelancer/
                    # service-call invoice's own numbered case-detail block
                    # below the one real billed line ("2 Name Jagdish
                    # Chandra", "6 BANK-ICICI", "7 A/C NO-..."). Same
                    # EXCLUDE_KW list used for the "no leading serial" new-
                    # item gate above - it has no leading serial here only
                    # because a case/detail number happens to look like one.
                    if any(k in lower for k in EXCLUDE_KW):
                        last_peelable_addition = None
                        continue
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
                    last_peelable_addition = None
                    continue

                if serial_from_token and serial.isdigit():
                    item_seq = int(serial)

                # This new item's own opening line may already be sitting,
                # wrongly, at the end of the PREVIOUS item's Description -
                # see last_peelable_addition's own comment above. Peel it
                # back off before that previous item is closed out below,
                # and hand it to THIS item as its lead-in instead (same
                # mechanism pending_lead_in already uses before the very
                # first item).
                if (
                    current_item
                    and last_peelable_addition
                    and current_item["Description"].endswith(last_peelable_addition)
                ):
                    remainder = current_item["Description"][
                        : -len(last_peelable_addition)
                    ].rstrip()
                    # Some vendors print the item's HSN CATEGORY name on the
                    # row with the serial/values (e.g. "Laptop ACCESSORIES
                    # (850440)", the HSN group's generic label, not a real
                    # item name) and wrap the item's own specific
                    # description onto the line AFTER it instead of before -
                    # the mirror image of the layout this peel-back exists
                    # for. That shape is unmistakable: the remainder is
                    # nothing but "<label> (<this item's own HSN>)" - never
                    # a genuine standalone item name on its own - so the
                    # text peeling would remove is actually this item's OWN
                    # real description, not the next item's opening line.
                    # Leave it in place instead of peeling it away.
                    hsn = (current_item.get("HSN") or "").strip()
                    is_hsn_category_label = bool(hsn) and remainder.endswith(f"({hsn})")
                    if not is_hsn_category_label:
                        current_item["Description"] = remainder
                        pending_lead_in = (
                            last_peelable_addition + " " + pending_lead_in
                        ).strip() if pending_lead_in else last_peelable_addition
                last_peelable_addition = None

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

                if orphan_amount is not None and not items:
                    # The amount buffered above the first item's own row
                    # (see orphan_amount). Quantity/Rate are not printed on
                    # such a page, so it is one unit at that amount, like a
                    # flat charge line; anything the row itself carries
                    # overwrites these below.
                    current_item["Amount"] = orphan_amount
                    current_item["Quantity"] = 1
                    current_item["Rate"] = orphan_amount
                    orphan_amount = None


                numbers = []


                # When no serial token was consumed, the first token is real
                # data (e.g. the description), so include it.
                value_words = (
                    words[:serial_word_idx] + words[serial_word_idx + 1:]
                    if serial_from_token else words
                )

                # A grouped CGST/SGST/IGST tax breakdown (SAP Business One's
                # own GST-invoice shape - e.g. R-Logic) packs 3 Rate+Amount
                # sub-column pairs, plus a final Line Total, all past the
                # "IGST" partition anchor (see detect_table_columns) - 7
                # numeric tokens the nearest-column scheme can only ever
                # bucket as one indistinct "IGST" slot. Detected here (once
                # per row, not per token) by that exact token count, rather
                # than merely "an IGST column exists" - a simple format with
                # ONE bare IGST amount column (no CGST/SGST breakdown at
                # all) also sets that same "IGST" key, and must NOT go
                # through this positional remapping.
                gst3_tokens = []
                if value_cols.get("IGST") is not None:
                    gst3_tokens = sorted(
                        (w for w in value_words
                         if w["x"] >= value_cols["IGST"] - 40
                         and is_number(w["text"].strip())),
                        key=lambda w: w["x"],
                    )
                is_gst3_breakdown = len(gst3_tokens) == 7

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

                        # A tightly-kerned PDF text layer can glue an ENTIRE
                        # row's worth of numeric cell values into ONE OCR/
                        # PDF-text token, space-separated but with no
                        # distinguishable per-value x-position at all (e.g.
                        # an Amazon invoice: "3,117.77 1 3,117.77 18% IGST
                        # 561.20 3,678.97" as a single word) - nearest-
                        # column matching can only ever place the WHOLE
                        # blob under one column, discarding the rest.
                        # Detected here by sub-part count (more space-
                        # separated parts than this row has known value
                        # columns to receive) AND the first few parts each
                        # individually looking number-shaped - a genuine
                        # Description that merely starts with a numeric
                        # magnitude (e.g. "1.5 MTR HDMI CABLE BLACK") has
                        # only ONE leading number followed by ordinary
                        # words, not several - and gated on landing nearest
                        # a genuine VALUE column (never Description/SI/HSN),
                        # so real description text is never reachable here
                        # regardless of its own wording. Split positionally:
                        # the known value columns (sorted left-to-right -
                        # e.g. Rate/UnitPrice, Quantity, Amount) take the
                        # first that-many parts in the same order; any
                        # leftover middle parts (typically a GST rate + tax
                        # type + tax amount, ahead of a final Line Total
                        # this schema has no field for) are scanned for a
                        # percentage-shaped token to recover the item's own
                        # tax rate, the same way ChargeRatePercent does for
                        # a charge line's own rate cell.
                        if col in ("Rate", "Quantity", "Amount", "Total"):
                            blob_parts = txt.split()
                            ordered = sorted(
                                (c for c in ("Rate", "Quantity", "Amount") if c in value_cols),
                                key=lambda c: value_cols[c],
                            )
                            if (
                                len(blob_parts) >= 5
                                and len(blob_parts) > len(ordered)
                                and sum(1 for p in blob_parts[:3] if is_number(p)) >= 2
                            ):
                                # A header whose Quantity column wasn't
                                # detected (only Rate/Amount known) leaves
                                # the blob's own "price qty net" triple
                                # mis-bucketed (qty landing in Amount).
                                # When the first three parts are numbers
                                # with price x qty = net, they ARE that
                                # triple - use them as such.
                                triple = [
                                    clean_number(p) if is_number(p) else None
                                    for p in blob_parts[:3]
                                ]
                                if (
                                    "Quantity" not in ordered
                                    and all(v is not None for v in triple)
                                    and abs(triple[0] * triple[1] - triple[2]) <= 0.02 * max(triple[2], 1)
                                ):
                                    for c, v in zip(("Rate", "Quantity", "Amount"), triple):
                                        if current_item.get(c) is None:
                                            current_item[c] = v
                                    ordered = []
                                for i, c in enumerate(ordered):
                                    if current_item.get(c) is None and is_number(blob_parts[i]):
                                        current_item[c] = clean_number(blob_parts[i])
                                for p in blob_parts[len(ordered):-1]:
                                    m = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*%", p)
                                    if m:
                                        current_item["TaxRatePercent"] = float(m.group(1))
                                        break
                                continue

                        # In a confirmed 3-way CGST/SGST/IGST breakdown (see
                        # is_gst3_breakdown above), the generic "Amount"
                        # anchor actually landed on the breakdown's own
                        # CGST-Amount sub-label (nothing on the header row
                        # said "Taxable" to lock the real Amount column
                        # there instead - see detect_table_columns), so a
                        # token nearest it is really a CGST/SGST/IGST
                        # sub-value, not the item's Amount - decoded
                        # positionally below instead, so discard it the same
                        # as a genuine IGST-zone token here. The item's real
                        # (pre-tax) Amount is the "Total" column instead.
                        if is_gst3_breakdown:
                            if col == "Total":
                                col = "Amount"
                            elif col == "Amount":
                                col = "IGST"

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
                            and _hsn_token_value(txt) is not None
                        ):
                            col = "HSN"


                        # A narrow Quantity column's own values (typically a
                        # bare 1-3 digit integer, sometimes "N.00") are often
                        # right-aligned within that column while the header
                        # LABEL text ("Quantity") is wide and left-anchored -
                        # so the value itself can land physically closer to
                        # the NEXT column's anchor (Rate/Amount) by just a
                        # handful of pixels (e.g. Viyona's own "1" quantity:
                        # 128px from its true Quantity anchor vs 123px from
                        # Rate - Rate wins the naive nearest-distance check
                        # by 5px, so the quantity is misfiled as Rate, then
                        # silently overwritten once the row's real Rate
                        # value arrives right after it, leaving Quantity
                        # blank). Reroute a near-tie back to Quantity when
                        # it's still unclaimed and the token itself is
                        # quantity-shaped (a plain integer or simple
                        # decimal, not a currency-formatted Rate/Amount).
                        if (
                            col in ("Rate", "Amount")
                            and "Quantity" in value_cols
                            and current_item.get("Quantity") is None
                            and re.fullmatch(r"\d{1,3}(\.\d{1,2})?", txt)
                            and abs(word["x"] - value_cols["Quantity"])
                            <= abs(word["x"] - value_cols[col]) + 40
                        ):
                            col = "Quantity"


                        # HSN / SAC code (4, 6 or 8 digits, optional letter -
                        # or the same digits printed with dot separators
                        # following the code's own chapter.heading.sub-
                        # heading structure, e.g. "84.73.3020" for what's
                        # really HSN 84733020 - _hsn_token_value strips the
                        # dots so the saved value always matches the plain
                        # digit form used everywhere else (SF lookup, other
                        # vendors' own printed HSN, etc.)).
                        hsn_val = _hsn_token_value(txt) if col == "HSN" else None
                        if hsn_val is not None:
                            current_item["HSN"] = hsn_val
                            continue


                        # A bare percentage ("18%") sitting nearest one of
                        # these columns is the line's GST RATE cell, not a
                        # quantity/price/amount - left to be parsed as a
                        # number below it overwrote the real Rate with 18
                        # (Anakage: Rate 18.0 against a 227,910 amount)
                        # whenever the table has no dedicated GST column to
                        # claim it.
                        if col in ("Quantity", "Rate", "Amount"):
                            pm = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*%", txt)
                            if pm:
                                pv = clean_number(pm.group(1))
                                if (pv is not None and 0 < pv <= 40
                                        and "TaxRatePercent" not in current_item):
                                    current_item["TaxRatePercent"] = pv
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


                        # A GST cell's combined amount+percent text (e.g.
                        # "₹ 684.00 (18%)") never parses as a plain number
                        # as a whole, so without this it fell straight
                        # through to the generic "free text becomes
                        # description" fallback below - corrupting the
                        # item's real Description with leaked price/tax
                        # text (sometimes landing mid-description, ahead of
                        # a wrapped second line, since this token is
                        # processed on the row's first physical line).
                        # Discarded outright, same as a stray Quantity/
                        # Rate/Amount number under another column. Same
                        # treatment for a full CGST/SGST/IGST breakdown
                        # (its own repeated Rate/Amount sub-columns, e.g.
                        # Bestech's "0.00% - 0.00% - 18.00% 892.37") - those
                        # land nearest the IGST partition anchor (see
                        # detect_table_columns), never a real description.
                        if col in ("GST", "IGST"):
                            # Some layouts print a CLEAN rate alone in this
                            # column (e.g. Sureworks' own "18.0%", no
                            # amount glued in) rather than the combined
                            # amount+percent blob this column usually
                            # holds - captured only when it parses as a
                            # plain, plausible GST percentage on its own,
                            # so the messy combined case above still falls
                            # through to being discarded exactly as before.
                            gm = re.fullmatch(r"(\d{1,2}(?:\.\d+)?)\s*%", txt)
                            if gm and "TaxRatePercent" not in current_item:
                                v = clean_number(gm.group(1))
                                if v is not None and 0 < v <= 40:
                                    current_item["TaxRatePercent"] = v
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

                # Decode the 3-way CGST/SGST/IGST breakdown positionally
                # (see is_gst3_breakdown/gst3_tokens above): CGST Rate, CGST
                # Amount, SGST Rate, SGST Amount, IGST Rate, IGST Amount,
                # then Line Total - the effective tax rate is always the
                # sum of the 3 rate slots (even indices 0/2/4); only one of
                # CGST+SGST vs IGST is ever non-zero on a real invoice (an
                # intra-state vs inter-state transaction), so summing all
                # three is correct either way without needing to know which.
                if is_gst3_breakdown:
                    rates = [clean_number(gst3_tokens[i]["text"]) for i in (0, 2, 4)]
                    current_item["TaxRatePercent"] = sum(r for r in rates if r is not None)

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

                # "HSN/SAC-84733099" glued onto its own wrapped line (see
                # EXCLUDE_KW's "hsn/sac" entry above) is excluded from the
                # open item's Description on purpose, but the code itself is
                # real, PDF-stated data - a vendor with no dedicated HSN
                # table column has nowhere else to put it. Recover it onto
                # the still-open item so a genuine HSN value isn't lost
                # outright and mistaken for the template lacking one.
                if current_item is not None and "hsn/sac" in lower and not current_item.get("HSN"):
                    hsn_match = re.search(r"hsn\s*/\s*sac\D{0,3}(\d{4,8})", lower)
                    if hsn_match:
                        current_item["HSN"] = hsn_match.group(1)

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
                        last_peelable_addition = None
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
                    last_peelable_addition = None
                    continue

                if (current_item and has_text and not footer_started
                        and not any(k in lower for k in EXCLUDE_KW)
                        and not any(k in lower for k in CHARGE_KW)
                        and not re.search(r"\btota\b", lower)):
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
                    recovered_value = False
                    if value_cols:
                        for w in words:
                            t = w["text"].strip()
                            # The HSN/SAC cell can sit on the same detail
                            # line as the price cells ("Serial Number: ...
                            # 84716040 ₹508.47 ₹1,016.94") - recovered like
                            # the numeric columns are, only when this item
                            # still has none.
                            if not current_item.get("HSN") and current_item.get("Amount") is None:
                                hv = _hsn_token_value(t.replace(",", ""))
                                if (hv is not None and "HSN" in value_cols
                                        and min(value_cols, key=lambda c: abs(w["x"] - value_cols[c])) == "HSN"):
                                    current_item["HSN"] = hv
                                    continue
                            if not is_number(t):
                                continue
                            col = min(value_cols, key=lambda c: abs(w["x"] - value_cols[c]))
                            if (
                                col in ("Quantity", "Rate", "Amount")
                                and current_item.get(col) is None
                                and abs(w["x"] - value_cols[col]) < 150
                            ):
                                current_item[col] = clean_number(t)
                                recovered_value = True
                    content = descriptive_text(words)
                    if content:
                        current_item["Description"] += " " + content
                    # Only a plain, non-parenthetical, non-"label: value",
                    # multi-WORD text-only row is ever a candidate to be
                    # peeled back onto a LATER item as its own lead-in (see
                    # last_peelable_addition's comment near pending_lead_in
                    # above). One that also carried a real Quantity/Rate/
                    # Amount for THIS item, or a "(...)" clarifying suffix,
                    # definitely belongs here. So does a "Serial No.:"/
                    # "S/N:"/etc. reference line (e.g. Bestech's own "S/N:
                    # IN0HH6MPB8TFC66J039U", right below its Dell monitor,
                    # immediately followed by an unrelated "COURIER CHARGES"
                    # item) - a colon-introduced attribute line is always
                    # describing whatever item precedes it, never a new
                    # item's own opening name. Same for a bare single word
                    # (e.g. Bestech's own "cable", the tail of "HP PRO BOOK
                    # 440 G8 LAPTOP DISPLAY / cable" wrapping onto its own
                    # line, immediately followed by an unrelated "COURIER
                    # CHARGES" item) - a genuine new item's opening name, if
                    # it needed to wrap onto its own line at all, is almost
                    # always more than one word; a lone leftover word is far
                    # more likely the tail of whatever came before it.
                    last_peelable_addition = (
                        content
                        if (
                            content
                            and not recovered_value
                            and not content.startswith("(")
                            and ":" not in content
                            and len(content.split()) > 1
                            # a serial/ID line ("112540108037, Warranty") is
                            # never a next item's opening name: it needs two
                            # real words
                            and len(re.findall(r"[A-Za-z]{3,}", content)) >= 2
                        )
                        else None
                    )
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

        # A "next serial + name words" row that never received any value of
        # its own was really a wrapped description line whose leading digits
        # only looked like the next serial ("2 PORT SERAIL CARD", or the
        # amount's own tail "02KV9G Dell Latitude ..."): fold it back into
        # the item above instead of leaving an empty phantom line.
        folded = []
        for item in items:
            if (
                folded
                and item.get("Amount") is None
                and item.get("Quantity") is None
                and item.get("Rate") is None
                and not item.get("charge")
            ):
                folded[-1]["Description"] = (
                    folded[-1]["Description"] + " " + item["Description"]
                ).strip()
                continue
            folded.append(item)
        items = folded

        # Cleanup

        for seq, item in enumerate(items, start=1):

            # Device serial numbers ("Serial Number:CN05NT8RPRC00626041SA08",
            # "S/N: CN-0657PN-64180-...") are never part of the item's name:
            # drop each label + its value, keeping the product text.
            item["Description"] = _SERIAL_NO_RE.sub(
                lambda m: " " if re.search(r"\d", m.group("val")) else m.group(0),
                item["Description"],
            )
            # Unlabelled Dell service-tag serials ("CN-05NT8R-PRC00-627-08LI-A08").
            item["Description"] = _DELL_TAG_RE.sub(" ", item["Description"])

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
                # A scanned page with no visible gap between adjacent
                # headers can OCR-merge two labels into one token (e.g.
                # "HSN/SACQuantity", HSN and Quantity glued together with
                # nothing between them) - the elif chain only ever
                # classifies a token as ONE column, so "Quantity" would
                # otherwise never get an anchor at all, and its row values
                # have nowhere to attach for the rest of the document.
                # Same x as HSN (the true split point is unknown) is still
                # far better than no anchor at all; setdefault so a later,
                # separately-printed "Quantity" header (the normal case)
                # always wins over this approximation.
                if "qty" in txt or "quantity" in txt:
                    columns.setdefault("Quantity", x)

            elif "sac" == txt:
                columns.setdefault("HSN", x)

            elif "qty" in txt or "quantity" in txt:
                columns["Quantity"] = x

            elif "rate" in txt or "price" in txt:
                # A grouped CGST/SGST/IGST breakdown repeats its own "Rate"
                # sub-label once per tax group (e.g. Bestech: base Rate,
                # then CGST Rate, SGST Rate, IGST Rate, four occurrences
                # total) - only the FIRST (base, leftmost) one is the real
                # per-unit Rate column; setdefault so a later repeat can
                # never drag this anchor rightward onto a tax sub-column,
                # which would then misattribute every token in the row by
                # nearest-x distance (scrambling Quantity along with it -
                # see extract_items' value recovery, which reads off this
                # same anchor).
                columns.setdefault("Rate", x)

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

            elif "gst" in txt:
                # A compact single "GST" column combining amount + rate in
                # one cell (e.g. "Rs 45.00 (18%)"), distinct from the full
                # IGST/CGST/SGST breakdown above - kept only as a partition
                # anchor (see extract_items' value_cols/GST skip below) so
                # its glued amount+percent text doesn't leak into Quantity/
                # Rate/Amount or, worse, the item Description itself.
                columns.setdefault("GST", x)

            elif txt == "total":
                columns.setdefault("Total", x)

            elif "amount" in txt:
                # Same repeated-sub-column reasoning as "Rate" just above -
                # the first plain "Amount" wins and later repeats (CGST/
                # SGST/IGST's own Amount, then a final "Total Amount") are
                # ignored, UNLESS "Taxable" explicitly locked a different,
                # more authoritative Amount column already (still wins
                # unconditionally either way - amount_locked only gates
                # THIS generic branch, never the "taxable" branch above).
                if not amount_locked:
                    columns.setdefault("Amount", x)

        # A scanned page can OCR the "Amount" header into something
        # unrecognizable (e.g. "Aannnt") - no keyword above ever matches it,
        # so the column is silently never created and every value in it
        # (the line's own Amount) has nowhere to land and is dropped
        # entirely. Last-resort positional fallback: a standard GST-invoice
        # item table's rightmost header column is always Amount, so the
        # header row's own rightmost word - as long as it sits past every
        # column already found (never overrides a genuinely-matched one) -
        # is that column even when its text is unrecognizable.
        if "Amount" not in columns and header_row:
            rightmost = max(header_row, key=lambda w: w["x"])
            if not columns or rightmost["x"] > max(columns.values()):
                columns["Amount"] = rightmost["x"]

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
                # A marketplace-style item row prints its own "Tax Type"
                # cell on the SAME row as the product ("1 | Symbol Trigger
                # Assembly for MC9000 ... | ₹3,117.77 1 ₹3,117.77 18% IGST
                # ₹561.20 ₹3,678.97"): serial + a real multi-word product
                # name + the tax-type word. Only that shape is kept - a
                # genuine tax row never opens with a serial and a product
                # name, and never mentions "total"/"tax amount"/"round".
                only_tax_type_hit = not any(
                    x in lower for x in (
                        "output igst", "input igst", "tax amount", "total",
                        "round off", "round-off", "roundoff",
                    )
                )
                name_words = [
                    w for w in re.findall(r"[A-Za-z]{3,}", text)
                    if w.lower() not in ("igst", "cgst", "sgst")
                ]
                if not (
                    only_tax_type_hit
                    and re.fullmatch(r"\d{1,3}[.)]?", words_in_row[0])
                    and len(name_words) >= 3
                ):
                    continue


            # remove duplicate headers
            header_words = [
                "description",
                "hsn",
                "quantity",
                "qty",  # some templates abbreviate the column header itself
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
                # "2666MHz", "500GB") or a warranty/validity period ("45
                # Days", "90 DAYS") is NOT a serial glued to a description —
                # it is real content in the item description (a size spec,
                # or - per descriptive_text's own docstring - a genuine spec
                # number like "90" in "90 Days Warranty" that must survive).
                # Leave it intact so it is not mis-split into a fake serial +
                # "GB"/"TB"/"Days".
                if match and re.fullmatch(
                    r"\d+\s*(GB|TB|MB|KB|GHZ|MHZ|HZ|MAH|WH|W|V|DAYS?)",
                    text,
                    re.IGNORECASE,
                ):
                    match = None

                # Same reasoning, but for a token that is the size/period
                # PLUS more description text glued on with it as one run
                # ("8GB PC4 2R*8 3200 DESKTOP RAM", "45 DAYS WARRNATY" - a
                # born-digital PDF's whole item-name/continuation-line cell
                # read as a single word, no space detected before the next
                # word). The measurement/period still isn't a serial number
                # here either - only the immediately-following unit matters,
                # not whether anything else follows it.
                if match and re.match(
                    r"^\d+\s*(GB|TB|MB|KB|GHZ|MHZ|HZ|MAH|WH|W|V|DAYS?)\b",
                    text,
                    re.IGNORECASE,
                ):
                    match = None

                # General case covering any OTHER part/SKU code that
                # happens to start with a digit (e.g. "4QL27-80101" - a
                # genuine part number wrapped onto its own line within a
                # multi-line item description, unrelated to the specific
                # measurement/period vocabulary above) - rather than
                # listing every possible code shape, check the word
                # immediately after the digit for the one thing a real
                # English word (a genuine "1Adaptor"-style serial +
                # description) always has and a code never does: a
                # lowercase letter. "Adaptor" has one; "QL27-80101" does
                # not. Without this, the leading digit is silently torn
                # off and lost as a fake serial number.
                if match:
                    first_word = match.group(2).split(None, 1)[0] if match.group(2).split() else ""
                    # A plain unit abbreviation ("QTY", "PCS", "NOS", ...)
                    # has no lowercase letter either, but it is legitimate
                    # glued text (e.g. the invoice's own unlabelled
                    # grand-total row "03 QTY") - only reject as a part/SKU
                    # code when it ISN'T also a recognised unit, so this row
                    # still splits into serial "03" + "QTY" and can be
                    # folded back downstream instead of surviving as one
                    # unsplit token that item-detection then mistakes for a
                    # standalone new item with no description.
                    is_unit_word = first_word.lower().strip(".,)") in (
                        "no", "nos", "pcs", "pc", "kg", "kgs", "unit", "qty",
                        "each", "ea", "day", "days",
                    )
                    if first_word and not is_unit_word and not any(c.islower() for c in first_word):
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

                # A grouped CGST/SGST/IGST header prints its own "Rate"/
                # "Amount" sub-labels as a SECOND physical header row (e.g.
                # "SL. | Name... | CGST | SGST | IGST | Total Amount" then
                # "No. | Rate | Amount | Rate | Amount | Rate | Amount") -
                # the "si"+"no." check above only catches this when the
                # first row's own leading word literally starts "si"; this
                # vendor's header says "SL." instead, so that check misses
                # it. Caught more generally here: a row made up ENTIRELY of
                # repeated "No."/"Rate"/"Amount" tokens is unambiguously a
                # sub-header, never real item data - merged into the
                # header regardless of what the row before it says, so it
                # can never fall through as ordinary table content and get
                # glued onto item 1's own Description as a false lead-in
                # (see pending_lead_in above).
                next_words = next_text.split()
                if next_words and all(
                    w in ("no.", "no", "rate", "amount") for w in next_words
                ):
                    current.extend(rows[i + 1])
                    current.sort(key=lambda x: x["x"])

                    merged.append(current)
                    i += 2
                    continue

            merged.append(current)
            i += 1

        return merged