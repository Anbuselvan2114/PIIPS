import { useEffect, useRef, useState } from "react";
import {
  startProcessing, getStatus, getResult, getActiveJob,
  getBatches, downloadBatchFile, exportBatchFile, getBatchHistory, getStatusCounts,
  getInvoicesByStatus, getInvoicesByBatch, setInvoiceExcluded,
  getInvoiceFieldCheck, markAsNewTemplate, revertNewTemplate,
} from "./api";
import { DataTable, Modal, PdfModal, RunBar, runPercent, isPreparing } from "./components";

// Dark categorical palette — one fixed, distinct hue per tbl_status slot
// (indexed by status_id). Sized past the number of statuses so colours never
// wrap/repeat, and each status always keeps the same colour.
const STATUS_COLORS = [
  "#3987e5", "#008300", "#d55181", "#c98500",
  "#199e70", "#d95926", "#9085e9", "#e66767",
  "#8a8f98", "#4aa3c7", "#b07acc", "#c2b21a",
];

// Document No. is the full minted string (e.g. "PIIPSPO-2627-000001") -
// only its trailing digit run is ever shown/edited (leading zeros
// stripped), same convention as the Document No. override box itself.
// A non-numeric string (no digit run) falls back to blank.
function docNoSeq(s) {
  const m = /(\d+)\s*$/.exec(s || "");
  return m ? String(parseInt(m[1], 10)) : "";
}

// "210" (one invoice) or "210-220" (several) - null/blank inputs collapse
// to "—", same as every other not-yet-available Dashboard cell.
function numberRange(first, last) {
  if (first == null && last == null) return "—";
  if (first === last || last == null) return String(first);
  if (first == null) return String(last);
  return `${first}-${last}`;
}

function _polar(cx, cy, r, a) {
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}

function StatusPie({ data, onSlice }) {
  data = data.filter((d) => (d.count || 0) > 0);   // pie shows only non-empty slices
  const total = data.reduce((s, d) => s + (d.count || 0), 0);
  if (!total) return <div className="empty">No processed invoices yet.</div>;

  const cx = 100, cy = 100, r = 92;
  let angle = -Math.PI / 2;
  const slices = data.map((d) => {
    const frac = d.count / total;
    const a0 = angle, a1 = angle + frac * 2 * Math.PI;
    angle = a1;
    const [x0, y0] = _polar(cx, cy, r, a0);
    const [x1, y1] = _polar(cx, cy, r, a1);
    const large = a1 - a0 > Math.PI ? 1 : 0;
    const color = STATUS_COLORS[((d.status_id || 1) - 1) % STATUS_COLORS.length] || "#888";
    const path = `M ${cx} ${cy} L ${x0} ${y0} A ${r} ${r} 0 ${large} 1 ${x1} ${y1} Z`;
    return { d, color, path, frac };
  });
  const single = slices.length === 1;

  return (
    <div>
      <svg width="100%" height="200" viewBox="0 0 200 200"
           role="img" aria-label="Purchase headers by status" style={{ display: "block" }}>
        {single ? (
          <circle cx={cx} cy={cy} r={r} fill={slices[0].color} style={{ cursor: "pointer" }}
                  onClick={() => onSlice(slices[0].d)}>
            <title>{slices[0].d.status}: {slices[0].d.count} (100%)</title>
          </circle>
        ) : slices.map((s, i) => (
          <path key={i} d={s.path} fill={s.color} style={{ cursor: "pointer" }}
                stroke="var(--surface, #1a1a19)" strokeWidth="2"
                onClick={() => onSlice(s.d)}>
            <title>{s.d.status}: {s.d.count} ({Math.round(s.frac * 100)}%)</title>
          </path>
        ))}
      </svg>
      <div style={{ marginTop: 12 }}>
        {slices.map((s, i) => (
          <div key={i} onClick={() => onSlice(s.d)}
               style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6, cursor: "pointer" }}>
            <span style={{ width: 12, height: 12, borderRadius: 3, background: s.color,
                           display: "inline-block", flex: "0 0 auto" }} />
            <span style={{ flex: 1 }}>{s.d.status}</span>
            <span style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{s.d.count}</span>
            <span className="muted">({Math.round(s.frac * 100)}%)</span>
          </div>
        ))}
      </div>
      <p className="hint" style={{ margin: "8px 0 0" }}>Click a status to see its invoices.</p>
    </div>
  );
}

// Horizontal bars of ALL statuses (0 included), shown under the pie.
function StatusBars({ data, onSlice }) {
  if (!data.length) return null;
  const max = Math.max(1, ...data.map((d) => d.count || 0));
  return (
    <div>
      {data.map((d, i) => {
        const color = STATUS_COLORS[((d.status_id || 1) - 1) % STATUS_COLORS.length] || "#888";
        const pct = Math.round(((d.count || 0) / max) * 100);
        return (
          <div key={i} onClick={() => onSlice(d)} style={{ cursor: "pointer", marginBottom: 10 }}>
            <div style={{ display: "flex", justifyContent: "space-between", fontSize: 13, marginBottom: 3 }}>
              <span>{d.status}</span>
              <span style={{ fontWeight: 600, fontVariantNumeric: "tabular-nums" }}>{d.count}</span>
            </div>
            <div style={{ height: 10, background: "rgba(128,128,128,.2)", borderRadius: 999 }}>
              <div style={{ width: `${pct}%`, height: "100%", background: color, borderRadius: 999 }} />
            </div>
          </div>
        );
      })}
    </div>
  );
}

export default function Dashboard({ user }) {
  const [job, setJob] = useState(null);
  const [running, setRunning] = useState(false);
  const [error, setError] = useState(null);
  const [results, setResults] = useState([]);
  const [batches, setBatches] = useState([]);
  const [batchInputs, setBatchInputs] = useState({});   // batch -> {docNo, entryNo}
  const [batchHistory, setBatchHistory] = useState(null);   // {batch, events} | null
  const [batchHistoryLoading, setBatchHistoryLoading] = useState(null);   // batch name currently loading
  const [batchError, setBatchError] = useState(null);
  const [downloadingBatch, setDownloadingBatch] = useState(null);
  const [exportingBatch, setExportingBatch] = useState(null);
  const isSuperAdmin = ["super admin", "developer"].includes((user?.user_type || "").toLowerCase());
  // Batch History is also offered to Viewer - a read-only role that's
  // otherwise locked out of every Super-Admin-only action on this page
  // (Export, Mark as New Template, Revert, ...), but History is itself
  // read-only too, so there's nothing for Viewer's own restrictions to
  // guard against here (see app.py's /api/batches/history, updated the
  // same way).
  const isViewer = (user?.user_type || "").toLowerCase() === "viewer";
  // A row's own batch_status, from the Batches table already loaded for
  // the Dashboard - a missing/unlisted batch defaults to "CREATED" (same
  // default used at the Batch Status column itself, see batchColumns).
  const batchStatusFor = (batchName) =>
    batches.find((b) => b.batch === batchName)?.batch_status || "CREATED";
  const [statusCounts, setStatusCounts] = useState([]);
  const [modal, setModal] = useState(null);
  const [fieldModal, setFieldModal] = useState(null);
  // Which of the Fields popup's three tabs is showing: "header" | "lines" | "reservations".
  const [fieldTab, setFieldTab] = useState("header");
  const [pdfFile, setPdfFile] = useState(null);
  const pollRef = useRef(null);
  const polledRef = useRef(null);   // job id whose progress this screen is following

  const loadBatches = () =>
    getBatches().then((r) => {
      const list = r.batches || [];
      setBatches(list);
      // Pre-fill each batch's Document No./Entry No. boxes with what was
      // last actually downloaded (see database.list_batches'
      // last_doc_no/last_entry_no) - only for a batch whose box is still
      // untouched, so this never clobbers something the user is mid-typing.
      // Both boxes are type="number" plain-integer OVERRIDE START values
      // (e.g. "10"), not the full stored value - Entry No. already is a
      // plain integer, but Document No. is the full minted string (e.g.
      // "PIIPSPO-2627-000001"); a non-numeric string silently renders as
      // blank in a number input, so its trailing digit run is pulled out
      // and leading zeros stripped (empty digit run - e.g. a custom/
      // template-less Document No. - falls back to blank, same as before).
      setBatchInputs((prev) => {
        const next = { ...prev };
        for (const b of list) {
          if (next[b.batch] || (!b.last_doc_no && !b.last_entry_no)) continue;
          next[b.batch] = { docNo: docNoSeq(b.last_doc_no), entryNo: b.last_entry_no || "" };
        }
        return next;
      });
    }).catch(() => {});
  const loadStatusCounts = () =>
    getStatusCounts().then((r) => setStatusCounts(r.counts || [])).catch(() => {});
  const statusTotal = statusCounts.reduce((s, d) => s + (d.count || 0), 0);

  useEffect(() => {
    (async () => {
      try {
        const j = await getActiveJob("process");
        if (j && (j.status === "running" || j.status === "pending")) {
          setJob(j); setRunning(true); poll(j.job_id);
        }
      } catch { /* none */ }
      loadBatches();
      loadStatusCounts();
    })();
    // Only one process run can exist at a time, and it may have been started
    // by ANY signed-in user: keep looking for one so everybody sees the same
    // live bar (and a disabled Start) without reloading the page.
    const watch = setInterval(async () => {
      try {
        const j = await getActiveJob("process", true);
        if (j && (j.status === "running" || j.status === "pending") && polledRef.current !== j.job_id) {
          setJob(j); setRunning(true); setError(null); poll(j.job_id);
        }
      } catch { /* server busy - try again next tick */ }
    }, 2500);
    return () => { clearInterval(pollRef.current); clearInterval(watch); };
  }, []);

  const poll = (jobId) => {
    clearInterval(pollRef.current);
    polledRef.current = jobId;
    pollRef.current = setInterval(async () => {
      try {
        const s = await getStatus(jobId, true);
        setJob(s);
        if (s.status === "completed" || s.status === "failed") {
          clearInterval(pollRef.current);
          polledRef.current = null;
          setRunning(false);
          const full = await getResult(jobId);
          setResults(full.results || []);
          loadBatches();
          loadStatusCounts();
          setJob(null);
          if (s.status === "failed") setError(s.error || "Processing failed");
        }
      } catch (e) { clearInterval(pollRef.current); polledRef.current = null; setRunning(false); setError(e.message); }
    }, 800);
  };

  const onStart = async () => {
    setError(null); setResults([]); setJob(null);
    // Show the bar at once (a "preparing" piece) instead of after the first
    // poll comes back.
    setRunning(true);
    setJob({ status: "running", stage: "Preparing (starting)", total: 1, processed: 0, file_total: 0,
             percent: 0, segments: [0], started_by_name: user?.username, started_by: user?.user_id });
    try { const { job_id } = await startProcessing(user?.user_id); poll(job_id); }
    catch (e) { setError(e.message); }
  };

  // "Invoices — DATA MISMATCH (18)" -> "Invoices — DATA MISMATCH (17)" -
  // keeps the popup's own title count in sync after Mark as New Template/
  // Revert drops a row out of it (see markNewTemplate/revertFromNewTemplate
  // below). Title unchanged if it has no "(N)" count to begin with.
  const dropRowFromModalTitle = (title) =>
    (title || "").replace(/\((\d+)\)\s*$/, (_, n) => `(${Math.max(0, Number(n) - 1)})`);

  const openStatusModal = async (slice) => {
    setModal({ kind: "status", title: `Invoices — ${slice.status}`, rows: [], loading: true, status: slice.status });
    try {
      const r = await getInvoicesByStatus(slice.status_id);
      setModal({ kind: "status", title: `Invoices — ${slice.status} (${(r.invoices || []).length})`,
                 rows: r.invoices || [], loading: false, status: slice.status });
    } catch (e) { setModal({ kind: "status", title: "Invoices", rows: [], loading: false, error: e.message, status: slice.status }); }
  };

  const openBatchModal = async (batch, status, statusLabel) => {
    const titleBase = status ? `Batch — ${batch} — ${statusLabel}` : `Batch — ${batch}`;
    setModal({ kind: "batch", title: titleBase, rows: [], loading: true, batch, status });
    try {
      const r = await getInvoicesByBatch(batch);
      const rows = status
        ? (r.invoices || []).filter((row) => row.status === status)
        : (r.invoices || []);
      setModal({ kind: "batch", title: `${titleBase} (${rows.length})`,
                 rows, loading: false, batch, status });
    } catch (e) { setModal({ kind: "batch", title: "Batch", rows: [], loading: false, batch, status, error: e.message }); }
  };

  const openFieldCheck = async (row) => {
    if (!row.header_id) return;
    const title = `Fields — ${row.invoice_no || row.file_name}${row.invoice_type ? ` (${row.invoice_type})` : ""}`;
    setFieldTab("header");
    // A SERVICE invoice never calls Service First, so it never has any
    // Reservation Entry rows - that tab isn't offered for it at all.
    const isService = (row.invoice_type || "").trim().toUpperCase() === "SERVICE";
    setFieldModal({ title, loading: true, isService });
    try {
      const r = await getInvoiceFieldCheck(row.header_id);
      setFieldModal({ title, loading: false, data: r, isService });
    } catch (e) {
      setFieldModal({ title: "Fields", loading: false, error: e.message });
    }
  };

  const toggleExclude = async (row) => {
    const exclude = !row.is_excluded;
    try {
      const r = await setInvoiceExcluded(row.header_id, exclude, user?.user_id);
      setModal((m) => m && ({
        ...m, error: null,
        rows: m.rows.map((x) => x.header_id === row.header_id
          ? { ...x, is_excluded: exclude, status: r.new_status || x.status } : x),
      }));
      loadStatusCounts(); loadBatches();
    } catch (e) {
      // This button lives inside the invoice-list modal - show the
      // failure there (modal.error, right where the user is looking),
      // not on the outer page's error banner, which sits behind the
      // modal overlay and would go unnoticed.
      setModal((m) => m && ({ ...m, error: e.message }));
    }
  };

  // Statuses the "Mark as New Template" escape hatch is offered from - kept
  // in sync by hand with database.py's _NEW_TEMPLATE_ELIGIBLE_STATUSES.
  const NEW_TEMPLATE_ELIGIBLE_STATUSES = ["BUYER ORDER NO DOESN'T EXIST", "DATA MISMATCH"];

  const [markingNewTemplate, setMarkingNewTemplate] = useState(null);  // header_id being marked
  const markNewTemplate = async (row) => {
    if (!window.confirm(
      `Mark "${row.invoice_no || row.file_name}" as New Template?\n\n` +
      `This moves it out of ${row.status || "its current status"} for good - its ` +
      "PDF goes to the New Template folder and a copy to New_Format for " +
      "training. Use Revert (on the New Template popup) to undo this."
    )) return;
    setMarkingNewTemplate(row.header_id);
    try {
      await markAsNewTemplate(row.header_id, user?.user_id);
      // This popup is scoped to ONE status - once marked, the row no
      // longer belongs on it at all (it's now New Template), so drop it
      // from the list rather than just relabeling its status in place.
      setModal((m) => m && ({
        ...m, error: null,
        title: dropRowFromModalTitle(m.title),
        rows: m.rows.filter((x) => x.header_id !== row.header_id),
      }));
      loadStatusCounts(); loadBatches();
    } catch (e) {
      setModal((m) => m && ({ ...m, error: e.message }));
    } finally {
      setMarkingNewTemplate(null);
    }
  };

  const [revertingNewTemplate, setRevertingNewTemplate] = useState(null);  // header_id being reverted
  const revertFromNewTemplate = async (row) => {
    if (!window.confirm(
      `Revert "${row.invoice_no || row.file_name}" from New Template back to its ` +
      "previous status?"
    )) return;
    setRevertingNewTemplate(row.header_id);
    try {
      await revertNewTemplate(row.header_id, user?.user_id);
      // This popup is scoped to New Template - once reverted, the row no
      // longer belongs on it at all, so drop it from the list rather than
      // just relabeling its status in place.
      setModal((m) => m && ({
        ...m, error: null,
        title: dropRowFromModalTitle(m.title),
        rows: m.rows.filter((x) => x.header_id !== row.header_id),
      }));
      loadStatusCounts(); loadBatches();
    } catch (e) {
      setModal((m) => m && ({ ...m, error: e.message }));
    } finally {
      setRevertingNewTemplate(null);
    }
  };

  const [includingAll, setIncludingAll] = useState(false);
  const includeAll = async () => {
    const targets = (modal?.rows || []).filter((r) => r.is_excluded && r.header_id);
    if (!targets.length) return;
    setIncludingAll(true);
    try {
      const results = await Promise.allSettled(
        targets.map((r) => setInvoiceExcluded(r.header_id, false, user?.user_id)));
      const byId = new Map(targets.map((r, i) => [r.header_id, results[i]]));
      const failed = results.filter((r) => r.status === "rejected");
      // Shown inside this same modal (not the outer page's error banner,
      // which sits behind the modal overlay and would go unnoticed) - the
      // first invoice's own reason (e.g. "its batch already has an invoice
      // Loaded...") is far more actionable than a bare failure count.
      const failureMsg = failed.length
        ? failed[0].reason?.message
          + (failed.length > 1 ? ` (and ${failed.length - 1} more failed the same way)` : "")
        : null;
      setModal((m) => m && ({
        ...m, error: failureMsg,
        rows: m.rows.map((x) => {
          const res = byId.get(x.header_id);
          return res && res.status === "fulfilled"
            ? { ...x, is_excluded: false, status: res.value.new_status || x.status }
            : x;
        }),
      }));
      loadStatusCounts(); loadBatches();
    } finally { setIncludingAll(false); }
  };

  const percent = runPercent(job);
  // Several files are extracted at once, so name the count - not one file.
  const stageLabel = !job ? "" : job.stage === "Extracting"
    ? `Extracting — ${Math.max(job.active_files || 0, 1)} ${(job.active_files || 0) > 1 ? "files at a time" : "file"} …`
    : `${job.stage || "Processing"}${job.current_file ? ` — ${job.current_file}` : ""} …`;

  // Long, unbroken values (file names, batch names, invoice/vendor text
  // with no spaces to wrap at) would otherwise force the whole table wider
  // than the modal, triggering .table-wrap's horizontal scroll - wrap them
  // at any character instead so every column stays on one screen.
  const wrapCellStyle = { whiteSpace: "normal", overflowWrap: "break-word", maxWidth: 220 };

  // clickable invoice-number cell (opens the PDF viewer)
  const invoiceCell = (row) => (
    row.file_name ? (
      <button className="btn-link" onClick={() => setPdfFile({ file: row.file_name, page: row.page_start ?? row.page, pageEnd: row.page_end })}
              style={{ background: "none", border: "none", padding: 0, color: "var(--primary)",
                       cursor: "pointer", textDecoration: "underline", font: "inherit",
                       ...wrapCellStyle }}>
        {row.invoice_no || row.file_name}
      </button>
    ) : (row.invoice_no || "—")
  );

  const fieldCheckColumns = [
    { key: "field", label: "Field" },
    { key: "value", label: "Current Value",
      render: (r) => r.missing ? <span className="muted">—</span> : r.value },
    { key: "source", label: "Source" },
    { key: "missing", label: "Status",
      render: (r) => (
        <div>
          <span className={`badge ${r.optional ? "badge-muted" : r.missing ? "badge-warning" : "badge-success"}`}>
            {r.optional ? "Optional" : r.missing ? "Missing" : "OK"}
          </span>
          {r.reason && (
            <div className="hint" style={{ marginTop: 3 }}>{r.reason}</div>
          )}
        </div>
      ) },
  ];

  const invoiceColumns = [
    { key: "invoice_no", label: "Invoice No.", render: invoiceCell },
    { key: "file_name", label: "File",
      render: (row) => <div style={wrapCellStyle}>{row.file_name ?? "—"}</div> },
    { key: "vendor", label: "Vendor",
      render: (row) => <div style={wrapCellStyle}>{row.vendor ?? "—"}</div> },
    { key: "invoice_type", label: "Invoice Type",
      render: (row) => <div style={wrapCellStyle}>{row.invoice_type ?? "—"}</div> },
    { key: "status", label: "Status",
      render: (row) => <div style={wrapCellStyle}>{row.status ?? "—"}</div> },
    { key: "batch", label: "Batch",
      render: (row) => <div style={wrapCellStyle}>{row.batch ?? "—"}</div> },
    { key: "_fields", label: "", sortable: false,
      render: (row) => row.header_id ? (
        <button className="btn btn-subtle btn-sm" onClick={() => openFieldCheck(row)}>
          Fields
        </button>
      ) : null },
    // Super Admin only, and only on the "Buyer Order No. Doesn't Exist" /
    // "Data Mismatch" status popups - a one-way escape hatch for a row
    // whose real problem is a bad/untrained format, not just a missing PO
    // or field (see database.mark_as_new_template). Not offered on the
    // batch-scoped modal (batchModalColumns below), just these status-wide
    // ones. Also only while the row's own batch is still CREATED - once a
    // batch starts moving (Downloaded/In Progress/...) this is no longer
    // offered, matching the server-side check in database.
    // mark_as_new_template/revert_new_template.
    ...(isSuperAdmin && modal?.kind === "status" && NEW_TEMPLATE_ELIGIBLE_STATUSES.includes(modal?.status) ? [
      { key: "_new_template", label: "", sortable: false,
        render: (row) => (row.header_id && batchStatusFor(row.batch) === "CREATED") ? (
          <button className="btn btn-subtle btn-sm" disabled={markingNewTemplate === row.header_id}
                  title="Move this invoice to New Template status - its PDF goes to the New_Format training folder"
                  onClick={() => markNewTemplate(row)}>
            {markingNewTemplate === row.header_id ? "Marking…" : "Mark as New Template"}
          </button>
        ) : null },
    ] : []),
    // Super Admin only, and only on the "New Template" status popup - undo
    // for the button above (see database.revert_new_template). A row that
    // reached New Template the ordinary way (unrecognized format at first
    // processing, never marked via this button) simply has no previous
    // status to go back to - clicking Revert on one shows that as an error
    // rather than the button being hidden/disabled (no cheap way to know
    // in advance without a dedicated column in this list). Same
    // CREATED-only restriction as Mark as New Template above.
    ...(isSuperAdmin && modal?.kind === "status" && modal?.status === "NEW TEMPLATE" ? [
      { key: "_revert_new_template", label: "", sortable: false,
        render: (row) => (row.header_id && batchStatusFor(row.batch) === "CREATED") ? (
          <button className="btn btn-subtle btn-sm" disabled={revertingNewTemplate === row.header_id}
                  title="Revert this invoice from New Template back to its previous status"
                  onClick={() => revertFromNewTemplate(row)}>
            {revertingNewTemplate === row.header_id ? "Reverting…" : "Revert"}
          </button>
        ) : null },
    ] : []),
  ];
  // Include/Exclude only makes sense once an invoice is actually staged
  // for export (READY TO LOAD), or to undo a previous exclude (EXCLUDED) —
  // for every other status it's not shown at all, rather than offered and
  // doing something confusing.
  const batchModalColumns = [
    ...invoiceColumns.filter((c) => c.key !== "batch"),
    ...(["READY TO LOAD", "EXCLUDED"].includes(modal?.status) ? [
      { key: "include", label: "Include / Exclude", sortable: false,
        render: (row) => (
          <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
            <input type="checkbox" checked={!row.is_excluded} onChange={() => toggleExclude(row)} />
            <span className={`badge ${row.is_excluded ? "badge-warning" : "badge-success"}`}>
              {row.is_excluded ? "Excluded" : "Included"}
            </span>
          </label>
        ) },
    ] : []),
  ];

  const resultRows = results.map((r, i) => {
    const failed = !!(r.reason || r.error) || (r.status && r.status !== "success");
    return {
      _key: `${r.file}-${i}`, file: r.file,
      outcome: failed ? "Failure" : "Success",
      reason: r.reason || r.error ||
        (r.status === "skipped" ? "Already processed" :
         r.status === "unknown" ? "Format not trained" : "—"),
      batch: r.batch || "—", _failed: failed,
    };
  });
  const resultColumns = [
    { key: "file", label: "File Name" },
    { key: "outcome", label: "Result",
      render: (row) => <span className={`badge ${row._failed ? "badge-warning" : "badge-success"}`}>{row.outcome}</span> },
    { key: "reason", label: "Reason" },
    { key: "batch", label: "Batch" },
  ];

  // Status count columns shown in the batches table (label -> tbl_status name).
  // Each value is read from the batch's per-status `counts` map.
  const BATCH_STATUS_COLS = [
    { status: "NEW TEMPLATE", label: "New Template" },
    { status: "DUPLICATE", label: "Duplicate" },
    { status: "BUYER ORDER NO DOESN'T EXIST", label: "Buyer Order No Doesn't Exist" },
    { status: "EXCLUDED", label: "Excluded" },
    { status: "PENDING IN SF", label: "Pending in SF" },
    { status: "DATA MISMATCH", label: "Data Mismatch" },
    { status: "READY TO LOAD", label: "Ready to Load" },
    { status: "LOADED", label: "Loaded" },
    { status: "REJECTED BY ACCOUNTS", label: "Rejected by Accounts" },
    { status: "POSTED", label: "Posted" },
    { status: "COMPLETED", label: "Completed" },
    { status: "MANUALLY UPDATED", label: "Manually Updated" },
  ];

  // Flatten the chosen status counts onto each row so the DataTable can
  // sort/search on them. Extracted = total tracker rows in the batch.
  const batchRows = batches.map((b) => {
    const row = { ...b, _key: b.batch, extracted: b.headers ?? 0 };
    BATCH_STATUS_COLS.forEach(({ status }) => {
      row[`st:${status}`] = (b.counts || {})[status] ?? 0;
    });
    return row;
  });

  // Document No / Entry No / Download only make sense while a batch still
  // has invoices sitting at READY TO LOAD — once everything has moved on
  // to Loaded/Posted/Completed there's nothing left to stage for NAV.
  // A batch locks the moment any invoice in it reaches Loaded/Excluded (or
  // later) - see database._batch_status_and_lock - but a locked batch can
  // still be downloaded as long as it has invoices left at Ready to Load
  // (a partial Load: only some of the batch was taken on to NAV so far) -
  // the download itself never re-touches or re-mints a Document No./Entry
  // No. for one already past Ready to Load, only the still-pending ones
  // (see app.py's download_batch). Batches must also clear in creation
  // order - blocked_by names any earlier, not-yet-cleared batch(es) this
  // one must wait on (see database.list_batches). Only one batch may be
  // Downloaded/In Progress at a time - see app.py's download_batch.
  const canDownload = (row) => (row.exportable ?? 1) > 0 && (row["st:READY TO LOAD"] ?? 0) > 0
    && !(row.blocked_by || []).length
    && !(row["st:BUYER ORDER NO DOESN'T EXIST"] ?? 0)
    && !(row["st:DATA MISMATCH"] ?? 0)
    && !batches.some((b) => b.batch !== row.batch
      && (b.batch_status === "DOWNLOADED" || b.batch_status === "IN PROGRESS"));

  // A plain link navigation can't show a clean error (a failed download,
  // e.g. a Document No. collision, would just dump raw JSON in the
  // browser) - fetch it instead so a rejection surfaces here as a normal
  // Dashboard alert, and only save the file on success.
  const doDownload = async (batch) => {
    setBatchError(null); setDownloadingBatch(batch);
    try {
      const { blob, filename } = await downloadBatchFile(
        batch, batchInputs[batch]?.docNo, batchInputs[batch]?.entryNo);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      // The backend just flipped this batch's status to Downloaded (and
      // minted Document Nos) - refresh so the table reflects that without
      // needing a manual page reload.
      loadBatches();
      loadStatusCounts();
    } catch (e) { setBatchError(e.message); }
    finally { setDownloadingBatch(null); }
  };

  // Super Admin only - who/when downloaded this batch, and the Document
  // No./Entry No. each download produced (see database.
  // get_batch_download_history - every download already logs its own
  // tbl_Audit_Event row, this just reads them back filtered to one batch).
  const openBatchHistory = async (batch) => {
    setBatchHistoryLoading(batch);
    try {
      const r = await getBatchHistory(batch, user?.user_id);
      setBatchHistory({ batch, events: r.events || [] });
    } catch (e) { setBatchError(e.message); }
    finally { setBatchHistoryLoading(null); }
  };

  // Super Admin only - exports the batch's EXISTING data exactly as
  // currently persisted (no minting, no locking, no marking downloaded -
  // see app.py's export_batch). A read-only snapshot for comparing against
  // an Excel someone says looks wrong, not a real download.
  const doExport = async (batch) => {
    setBatchError(null); setExportingBatch(batch);
    try {
      const { blob, filename } = await exportBatchFile(batch, user?.user_id);
      const url = URL.createObjectURL(blob);
      const a = document.createElement("a");
      a.href = url; a.download = filename;
      document.body.appendChild(a); a.click(); a.remove();
      URL.revokeObjectURL(url);
      // Nothing changed server-side - no refresh needed.
    } catch (e) { setBatchError(e.message); }
    finally { setExportingBatch(null); }
  };

  const batchColumns = [
    ...(isSuperAdmin ? [{
      key: "_export", label: "", sortable: false,
      render: (row) => (
        <button className="btn btn-subtle btn-sm" disabled={exportingBatch === row.batch}
                title="Download this batch's existing data as-is (no changes made)"
                onClick={() => doExport(row.batch)}>
          {exportingBatch === row.batch ? "…" : "⬇"}
        </button>
      ),
    }] : []),
    { key: "batch", label: "Batch Name" },
    { key: "batch_status", label: "Batch Status", render: (row) => row.batch_status || "CREATED" },
    { key: "extracted", label: "Extracted", render: (row) => row.extracted ?? 0 },
    ...BATCH_STATUS_COLS.map(({ status, label }) => ({
      key: `st:${status}`,
      label,
      render: (row) => {
        const count = row[`st:${status}`] ?? 0;
        return count > 0 ? (
          <button className="btn-link" title={`View ${label} invoices`}
                  onClick={() => openBatchModal(row.batch, status, label)}>{count}</button>
        ) : count;
      },
    })),
    { key: "_docno", label: "Document No", sortable: false,
      render: (row) => (canDownload(row) ? (
        <input type="number" placeholder="e.g. 10"
               style={{ width: 90, height: 30, padding: "0 8px" }}
               value={batchInputs[row.batch]?.docNo ?? ""}
               onChange={(e) => setBatchInputs((s) => ({
                 ...s, [row.batch]: { ...s[row.batch], docNo: e.target.value },
               }))} />
      ) : (
        <span className="muted" title={row.doc_no_first ? `${row.doc_no_first} – ${row.doc_no_last}` : undefined}>
          {numberRange(
            row.doc_no_first != null ? docNoSeq(row.doc_no_first) : null,
            row.doc_no_last != null ? docNoSeq(row.doc_no_last) : null,
          )}
        </span>
      )) },
    { key: "_entryno", label: "Entry No", sortable: false,
      render: (row) => (canDownload(row) ? (
        <input type="number" placeholder="e.g. 7"
               style={{ width: 90, height: 30, padding: "0 8px" }}
               value={batchInputs[row.batch]?.entryNo ?? ""}
               onChange={(e) => setBatchInputs((s) => ({
                 ...s, [row.batch]: { ...s[row.batch], entryNo: e.target.value },
               }))} />
      ) : <span className="muted">{numberRange(row.entry_no_first, row.entry_no_last)}</span>) },
    { key: "_dl", label: "", sortable: false,
      render: (row) => (canDownload(row) ? (
        <button className="btn btn-primary btn-sm" disabled={downloadingBatch === row.batch}
                onClick={() => doDownload(row.batch)}>
          {downloadingBatch === row.batch ? "Downloading…" : "Download"}
        </button>
      ) : (
        <span className="muted"
              title={(row.locked && !(row["st:READY TO LOAD"] ?? 0))
                ? "This batch has an invoice already Loaded, Excluded, Posted, Completed, or Rejected, and nothing left at Ready to Load — there's nothing left to download."
                : (row.blocked_by || []).length
                ? `Waiting on earlier batch(es) to be Loaded, Posted, or Completed first: ${row.blocked_by.join(", ")}`
                : (row["st:BUYER ORDER NO DOESN'T EXIST"] ?? 0)
                ? "Kindly fill in the Buyer Order No for every invoice in this batch before downloading."
                : (row["st:DATA MISMATCH"] ?? 0)
                ? "Kindly resolve the Data Mismatch invoice(s) in this batch before downloading."
                : "No active / included invoices to export, or every invoice in this batch has already moved past Ready to Load"}>—</span>
      )) },
    ...(isSuperAdmin || isViewer ? [{
      key: "_history", label: "", sortable: false,
      render: (row) => (
        <button className="btn btn-subtle btn-sm" disabled={batchHistoryLoading === row.batch}
                onClick={() => openBatchHistory(row.batch)}>
          {batchHistoryLoading === row.batch ? "Loading…" : "History"}
        </button>
      ),
    }] : []),
  ];

  return (
    <div className="page">
      <div style={{ display: "flex", gap: 16, alignItems: "flex-start", flexWrap: "wrap" }}>

        <div style={{ flex: "1 1 560px", minWidth: 0, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card">
            <div className="card-title-row">
              <h3>Process invoices</h3>
              <div style={{ flex: 1 }} />
              <button className="btn btn-primary btn-lg" onClick={onStart} disabled={running}>
                {running
                  ? (job?.started_by_name && job.started_by !== user?.user_id
                      ? `Processing… (started by ${job.started_by_name})` : "Processing…")
                  : "▶  Start"}
              </button>
            </div>
            <p className="hint" style={{ margin: 0 }}>
              Reads PDFs from the Input folder, extracts each invoice, and saves a batch.
            </p>
            {running && job && (
              <div style={{ marginTop: 18 }}>
                <div className="progress-meta">
                  <span>
                    {job.started_by_name ? `Started by ${job.started_by_name} · ` : ""}{stageLabel}
                  </span>
                  <span>
                    {job.file_total && !isPreparing(job) ? `${Math.min(job.processed, job.file_total)}/${job.file_total} files · ` : ""}
                    {percent}%
                  </span>
                </div>
                <RunBar job={job} />
              </div>
            )}
            {error && <div className="alert alert-danger" style={{ marginTop: 12 }}>{error}</div>}
          </div>

          <div className="card">
            <div className="card-title-row">
              <h3>Batches</h3><div style={{ flex: 1 }} />
              <button className="btn btn-subtle btn-sm" onClick={loadBatches}>Refresh</button>
            </div>
            {(() => {
              // Only one batch may be mid-flight (Downloaded/In Progress)
              // at a time - see app.py's download_batch - so name whichever
              // batch(es) are currently sitting in that state, not just the
              // ones an ordering check (blocked_by) would report.
              const active = batches
                .filter((b) => b.batch_status === "DOWNLOADED" || b.batch_status === "IN PROGRESS")
                .map((b) => b.batch);
              if (!active.length) return null;
              return (
                <div className="alert alert-info" style={{ marginBottom: 12 }}>
                  Batch {active.join(", ")} {active.length > 1 ? "are" : "is"} still Downloaded/In Progress — no other batch can be downloaded until {active.length > 1 ? "they are" : "it's"} fully Loaded, Posted, or Completed.
                </div>
              );
            })()}
            {batchError && (
              <div className="alert alert-danger" style={{ marginBottom: 12 }}>{batchError}</div>
            )}
            <DataTable columns={batchColumns} rows={batchRows} searchKeys={["batch", "created"]}
                       defaultSortKey="batch" defaultSortDir="desc"
                       empty="No batches yet. Run Start to create one." />
          </div>

          <div className="card">
            <div className="card-title-row">
              <h3>Run results</h3><div style={{ flex: 1 }} />
              <button className="btn btn-subtle btn-sm" disabled={!results.length}
                      onClick={() => setResults([])}>Clear</button>
            </div>
            <DataTable columns={resultColumns} rows={resultRows}
                       searchKeys={["file", "outcome", "reason", "batch"]}
                       empty="No run yet. Click Start to process invoices." />
          </div>
        </div>

        <div style={{ flex: "0 0 300px", maxWidth: 340, display: "flex", flexDirection: "column", gap: 16 }}>
          <div className="card">
            <div className="card-title-row">
              <h3>Invoices by status</h3>
              <span className="badge badge-success" style={{ marginLeft: 8 }}>
                {statusTotal} total
              </span>
              <div style={{ flex: 1 }} />
              <button className="btn btn-subtle btn-sm" onClick={loadStatusCounts}>Refresh</button>
            </div>
            <StatusPie data={statusCounts} onSlice={openStatusModal} />
          </div>
          <div className="card">
            <div className="card-title-row">
              <h3>Status breakdown</h3>
              <span className="badge badge-success" style={{ marginLeft: 8 }}>
                {statusTotal} total
              </span>
            </div>
            <StatusBars data={statusCounts} onSlice={openStatusModal} />
          </div>
        </div>
      </div>

      {modal && (
        <Modal title={modal.title} onClose={() => setModal(null)} width={1300}>
          {modal.error && <div className="alert alert-danger">{modal.error}</div>}
          {modal.kind === "batch" && modal.status === "EXCLUDED"
            && !modal.loading && modal.rows.some((r) => r.is_excluded) && (
            <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 12 }}>
              <button className="btn btn-subtle btn-sm" disabled={includingAll} onClick={includeAll}>
                {includingAll ? "Including…" : "Include All"}
              </button>
            </div>
          )}
          {modal.loading ? <div className="empty">Loading…</div> : (
            <DataTable
              columns={modal.kind === "batch" ? batchModalColumns : invoiceColumns}
              rows={modal.rows.map((r) => ({ ...r, _key: r.header_id ?? r.file_name }))}
              searchKeys={["invoice_no", "file_name", "vendor", "invoice_type", "status"]}
              empty="No invoices." />
          )}
        </Modal>
      )}

      {fieldModal && (
        <Modal title={fieldModal.title} onClose={() => setFieldModal(null)}>
          {fieldModal.error && <div className="alert alert-danger">{fieldModal.error}</div>}
          {fieldModal.loading ? <div className="empty">Loading…</div> : fieldModal.data && (
            <>
              <div className="pills">
                {[
                  ["header", "Purchase Header", null],
                  ["lines", "Purchase Line", fieldModal.data.lines.length],
                  ...(fieldModal.isService ? [] : [
                    ["reservations", "Reservation Entry", fieldModal.data.reservations.length],
                  ]),
                ].map(([key, label, count]) => (
                  <div key={key}
                       className={`pill${fieldTab === key ? " active" : ""}`}
                       onClick={() => setFieldTab(key)}>
                    {label}{count != null ? ` (${count})` : ""}
                  </div>
                ))}
              </div>

              {fieldTab === "header" && (
                <DataTable columns={fieldCheckColumns}
                           rows={fieldModal.data.header.map((f) => ({ ...f, _key: f.field }))}
                           searchKeys={["field"]} pageSize={50} empty="No header data." />
              )}

              {fieldTab === "lines" && (
                <>
                  {fieldModal.data.lines.map((line, i) => (
                    <div key={`line-${i}`}>
                      <h3 style={i === 0 ? { marginTop: 0 } : undefined}>Purchase Line — {line.label}</h3>
                      <DataTable columns={fieldCheckColumns}
                                 rows={line.fields.map((f) => ({ ...f, _key: f.field }))}
                                 searchKeys={["field"]} pageSize={50} empty="No line data." />
                    </div>
                  ))}
                  {!fieldModal.data.lines.length && (
                    <p className="hint">No Purchase Line rows saved for this invoice.</p>
                  )}
                </>
              )}

              {fieldTab === "reservations" && !fieldModal.isService && (
                <>
                  {fieldModal.data.reservations.map((res, i) => (
                    <div key={`res-${i}`}>
                      <h3 style={i === 0 ? { marginTop: 0 } : undefined}>Reservation Entry — {res.label}</h3>
                      <DataTable columns={fieldCheckColumns}
                                 rows={res.fields.map((f) => ({ ...f, _key: f.field }))}
                                 searchKeys={["field"]} pageSize={50} empty="No reservation data." />
                    </div>
                  ))}
                  {!fieldModal.data.reservations.length && (
                    <p className="hint">No Reservation Entry rows for this invoice.</p>
                  )}
                </>
              )}
            </>
          )}
        </Modal>
      )}

      {pdfFile && <PdfModal file={pdfFile.file} page={pdfFile.page} pageEnd={pdfFile.pageEnd} onClose={() => setPdfFile(null)} />}

      {batchHistory && (
        <Modal title={`Download History — ${batchHistory.batch}`} onClose={() => setBatchHistory(null)} width={640}>
          {batchHistory.events.length === 0 ? (
            <div className="empty">No downloads recorded yet.</div>
          ) : (
            <div className="timeline">
              {batchHistory.events.map((ev) => (
                <div key={ev.Id} className="timeline-item">
                  <div className="timeline-dot" />
                  <div className="timeline-content">
                    <div className="timeline-header">
                      <span className="timeline-time">{ev.EventDatetime}</span>
                    </div>
                    <div className="timeline-who">{ev.UserName || "Unknown"}</div>
                    {ev.Detail && <div className="timeline-detail">{ev.Detail}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}
    </div>
  );
}
