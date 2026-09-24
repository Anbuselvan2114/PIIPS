"""
Worker-process side of PIIPS's parallel extraction.

processor.py fans a run's files out to a small pool of worker processes so
several PDFs are extracted at the same time. PaddleOCR is CPU bound and not
thread-safe, so the parallelism is per PROCESS: each worker loads its own
OCREngine once (lazily, on its first file) and reuses it for every file it is
given. Everything here must stay importable without importing processor.py
(a spawned worker re-imports this module, nothing else of the app).
"""

import os

_engine = None
_progress_queue = None


def init_worker(ocr_threads, progress_queue=None):
    """Pool initializer: cap PaddleOCR's own thread count so N workers don't
    each grab every core (see ocr_engine.OCREngine.initialize), and remember
    the queue per-file progress is reported on.
    Sample: init_worker(2, queue)"""
    global _progress_queue
    os.environ["PIIPS_OCR_THREADS"] = str(max(1, int(ocr_threads)))
    _progress_queue = progress_queue


def extract_one(path, allow_scanned):
    """Extract one document in this worker. Returns ("ok", ocr_result) or
    ("err", message) - an exception is returned, not raised, so a bad file
    never poisons the pool and the message survives pickling intact.
    Sample: extract_one('D:/PIIPS/Input/PT/PART/Chennai/a.pdf', False)"""
    global _engine
    try:
        if _engine is None:
            from ocr_engine import OCREngine
            _engine = OCREngine()

        last = [-1]

        def report(fraction):
            pct = int(fraction * 100)
            if _progress_queue is not None and pct != last[0]:
                last[0] = pct
                _progress_queue.put((path, pct))

        return "ok", _engine.read_pdf(path, allow_scanned=allow_scanned, progress=report)
    except Exception as exc:  # noqa: BLE001 - reported back per file
        import traceback
        traceback.print_exc()
        return "err", str(exc)


def warm():
    """Load this worker's OCR engine ahead of its first real file.
    Sample: warm() -> pid"""
    global _engine
    if _engine is None:
        from ocr_engine import OCREngine
        _engine = OCREngine()
    return os.getpid()
