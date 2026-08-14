import { useEffect, useRef, useState } from "react";
import {
  startProcessing, getStatus, getResult, getActiveJob,
  getBatches, batchDownloadUrl, getStatusCounts,
  getInvoicesByStatus, getInvoicesByBatch, setInvoiceExcluded,
  getInvoiceFieldCheck,
} from "./api";
import { DataTable, Modal, PdfModal } from "./components";

// Dark categorical palette — one fixed, distinct hue per tbl_status slot
// (indexed by status_id). Sized past the number of statuses so colours never
// wrap/repeat, and each status always keeps the same colour.
const STATUS_COLORS = [
  "#3987e5", "#008300", "#d55181", "#c98500",
  "#199e70", "#d95926", "#9085e9", "#e66767",
  "#8a8f98", "#4aa3c7", "#b07acc", "#c2b21a",
];

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
  const [statusCounts, setStatusCounts] = useState([]);
  const [modal, setModal] = useState(null);
  const [fieldModal, setFieldModal] = useState(null);
  const [pdfFile, setPdfFile] = useState(null);
  const pollRef = useRef(null);

  const loadBatches = () =>
    getBatches().then((r) => setBatches(r.batches || [])).catch(() => {});
  const loadStatusCounts = () =>
    getStatusCounts().then((r) => setStatusCounts(r.counts || [])).catch(() => {});

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
    return () => clearInterval(pollRef.current);
  }, []);

  const poll = (jobId) => {
    clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const s = await getStatus(jobId);
        setJob(s);
        if (s.status === "completed" || s.status === "failed") {
          clearInterval(pollRef.current);
          setRunning(false);
          const full = await getResult(jobId);
          setResults(full.results || []);
          loadBatches();
          loadStatusCounts();
          setJob(null);
          if (s.status === "failed") setError(s.error || "Processing failed");
        }
      } catch (e) { clearInterval(pollRef.current); setRunning(false); setError(e.message); }
    }, 800);
  };

  const onStart = async () => {
    setError(null); setResults([]); setJob(null);
    try { const { job_id } = await startProcessing(user?.user_id); setRunning(true); poll(job_id); }
    catch (e) { setError(e.message); }
  };

  const openStatusModal = async (slice) => {
    setModal({ kind: "status", title: `Invoices — ${slice.status}`, rows: [], loading: true });
    try {
      const r = await getInvoicesByStatus(slice.status_id);
      setModal({ kind: "status", title: `Invoices — ${slice.status} (${(r.invoices || []).length})`,
                 rows: r.invoices || [], loading: false });
    } catch (e) { setModal({ kind: "status", title: "Invoices", rows: [], loading: false, error: e.message }); }
  };

  const openBatchModal = async (batch, status, statusLabel) => {
    const titleBase = status ? `Batch — ${batch} — ${statusLabel}` : `Batch — ${batch}`;
    setModal({ kind: "batch", title: titleBase, rows: [], loading: true, batch });
    try {
      const r = await getInvoicesByBatch(batch);
      const rows = status
        ? (r.invoices || []).filter((row) => row.status === status)
        : (r.invoices || []);
      setModal({ kind: "batch", title: `${titleBase} (${rows.length})`,
                 rows, loading: false, batch });
    } catch (e) { setModal({ kind: "batch", title: "Batch", rows: [], loading: false, batch, error: e.message }); }
  };

  const openFieldCheck = async (row) => {
    if (!row.header_id) return;
    setFieldModal({ title: `Fields — ${row.invoice_no || row.file_name}`, loading: true });
    try {
      const r = await getInvoiceFieldCheck(row.header_id);
      setFieldModal({ title: `Fields — ${row.invoice_no || row.file_name}`, loading: false, data: r });
    } catch (e) {
      setFieldModal({ title: "Fields", loading: false, error: e.message });
    }
  };

  const toggleExclude = async (row) => {
    const exclude = !row.is_excluded;
    try {
      const r = await setInvoiceExcluded(row.header_id, exclude, user?.user_id);
      setModal((m) => m && ({
        ...m,
        rows: m.rows.map((x) => x.header_id === row.header_id
          ? { ...x, is_excluded: exclude, status: r.new_status || x.status } : x),
      }));
      loadStatusCounts(); loadBatches();
    } catch (e) { setError(e.message); }
  };

  const percent = job?.percent ?? 0;
  const stageLabel = job
    ? `${job.stage || "Processing"}${job.current_file ? ` — ${job.current_file}` : ""} …` : "";

  // clickable invoice-number cell (opens the PDF viewer)
  const invoiceCell = (row) => (
    row.file_name ? (
      <button className="btn-link" onClick={() => setPdfFile(row.file_name)}
              style={{ background: "none", border: "none", padding: 0, color: "var(--primary)",
                       cursor: "pointer", textDecoration: "underline", font: "inherit" }}>
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
        <span className={`badge ${r.missing ? "badge-warning" : "badge-success"}`}>
          {r.missing ? "Missing" : "OK"}
        </span>
      ) },
  ];

  // "developer" is a legacy alias for "super admin" (pre-rename sessions),
  // matching how App.jsx's ROLE_MENUS treats the two roles as equivalent.
  const isSuperAdmin = ["super admin", "developer"].includes(
    (user?.user_type || "").toLowerCase()
  );

  // The Fields drill-down button is for anyone who administers data
  // (Super Admin + Admin), unlike the INCOMPLETE DATA/NEW TEMPLATE label
  // swap below, which is specifically about hiding technical status names
  // from non-admin roles.
  const canViewFields = isSuperAdmin || (user?.user_type || "").toLowerCase() === "admin";

  // Non-super-admins see "INCOMPLETE DATA" relabeled as "NEW TEMPLATE"
  // everywhere it's displayed (chart, breakdown, invoice lists) — the
  // underlying status name/logic is unchanged, this is display-only.
  const displayStatus = (s) => (!isSuperAdmin && s === "INCOMPLETE DATA") ? "NEW TEMPLATE" : s;
  const displayStatusCounts = statusCounts.map((c) => ({ ...c, status: displayStatus(c.status) }));

  const invoiceColumns = [
    { key: "invoice_no", label: "Invoice No.", render: invoiceCell },
    { key: "file_name", label: "File" },
    { key: "vendor", label: "Vendor" },
    { key: "invoice_type", label: "Invoice Type" },
    { key: "status", label: "Status", render: (row) => displayStatus(row.status) },
    { key: "batch", label: "Batch" },
    ...(canViewFields ? [{ key: "_fields", label: "", sortable: false,
      render: (row) => row.header_id ? (
        <button className="btn btn-subtle btn-sm" onClick={() => openFieldCheck(row)}>
          Fields
        </button>
      ) : null }] : []),
  ];
  const batchModalColumns = [
    ...invoiceColumns.filter((c) => c.key !== "batch"),
    { key: "include", label: "Include / Exclude", sortable: false,
      render: (row) => (
        <label style={{ display: "inline-flex", alignItems: "center", gap: 8, cursor: "pointer" }}>
          <input type="checkbox" checked={!row.is_excluded} onChange={() => toggleExclude(row)} />
          <span className={`badge ${row.is_excluded ? "badge-warning" : "badge-success"}`}>
            {row.is_excluded ? "Excluded" : "Included"}
          </span>
        </label>
      ) },
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
    { status: "BUYER ORDER NO DOESN'T EXIST", label: "Buyer Order No Doesn't Exist" },
    { status: "EXCLUDED", label: "Excluded" },
    { status: "PENDING IN SF", label: "Pending in SF" },
    { status: "INCOMPLETE DATA", label: isSuperAdmin ? "Incomplete Data" : "New Template" },
    { status: "READY TO LOAD", label: "Ready to Load" },
    { status: "LOADED", label: "Loaded" },
    { status: "POSTED", label: "Posted" },
    { status: "COMPLETED", label: "Completed" },
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
  const canDownload = (row) => (row.exportable ?? 1) > 0 && (row["st:READY TO LOAD"] ?? 0) > 0;

  const batchColumns = [
    { key: "batch", label: "Batch Name" },
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
      ) : <span className="muted">—</span>) },
    { key: "_entryno", label: "Entry No", sortable: false,
      render: (row) => (canDownload(row) ? (
        <input type="number" placeholder="e.g. 7"
               style={{ width: 90, height: 30, padding: "0 8px" }}
               value={batchInputs[row.batch]?.entryNo ?? ""}
               onChange={(e) => setBatchInputs((s) => ({
                 ...s, [row.batch]: { ...s[row.batch], entryNo: e.target.value },
               }))} />
      ) : <span className="muted">—</span>) },
    { key: "_dl", label: "", sortable: false,
      render: (row) => (canDownload(row) ? (
        <a className="btn btn-primary btn-sm"
           href={batchDownloadUrl(row.batch, batchInputs[row.batch]?.docNo, batchInputs[row.batch]?.entryNo)}>
          Download
        </a>
      ) : (
        <span className="muted" title="No active / included invoices to export, or every invoice in this batch has already moved past Ready to Load">—</span>
      )) },
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
                {running ? "Processing…" : "▶  Start"}
              </button>
            </div>
            <p className="hint" style={{ margin: 0 }}>
              Reads PDFs from the Input folder, extracts each invoice, and saves a batch.
            </p>
            {running && job && (
              <div style={{ marginTop: 18 }}>
                <div className="progress-meta">
                  <span>{stageLabel}</span>
                  <span>{job.processed}/{job.total} · {percent}%</span>
                </div>
                <div className="progress"><div className="progress-bar" style={{ width: `${percent}%` }} /></div>
              </div>
            )}
            {error && <div className="alert alert-danger" style={{ marginTop: 12 }}>{error}</div>}
          </div>

          <div className="card">
            <div className="card-title-row">
              <h3>Batches</h3><div style={{ flex: 1 }} />
              <button className="btn btn-subtle btn-sm" onClick={loadBatches}>Refresh</button>
            </div>
            <DataTable columns={batchColumns} rows={batchRows} searchKeys={["batch", "created"]}
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
              <h3>Invoices by status</h3><div style={{ flex: 1 }} />
              <button className="btn btn-subtle btn-sm" onClick={loadStatusCounts}>Refresh</button>
            </div>
            <StatusPie data={displayStatusCounts} onSlice={openStatusModal} />
          </div>
          <div className="card">
            <h3>Status breakdown</h3>
            <StatusBars data={displayStatusCounts} onSlice={openStatusModal} />
          </div>
        </div>
      </div>

      {modal && (
        <Modal title={modal.title} onClose={() => setModal(null)}>
          {modal.error && <div className="alert alert-danger">{modal.error}</div>}
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
              <h3 style={{ marginTop: 0 }}>Purchase Header</h3>
              <DataTable columns={fieldCheckColumns}
                         rows={fieldModal.data.header.map((f) => ({ ...f, _key: f.field }))}
                         searchKeys={["field"]} pageSize={50} empty="No header data." />

              {fieldModal.data.lines.map((line, i) => (
                <div key={`line-${i}`}>
                  <h3>Purchase Line — {line.label}</h3>
                  <DataTable columns={fieldCheckColumns}
                             rows={line.fields.map((f) => ({ ...f, _key: f.field }))}
                             searchKeys={["field"]} pageSize={50} empty="No line data." />
                </div>
              ))}
              {!fieldModal.data.lines.length && (
                <p className="hint">No Purchase Line rows saved for this invoice.</p>
              )}

              {fieldModal.data.reservations.map((res, i) => (
                <div key={`res-${i}`}>
                  <h3>Reservation Entry — {res.label}</h3>
                  <DataTable columns={fieldCheckColumns}
                             rows={res.fields.map((f) => ({ ...f, _key: f.field }))}
                             searchKeys={["field"]} pageSize={50} empty="No reservation data." />
                </div>
              ))}
            </>
          )}
        </Modal>
      )}

      {pdfFile && <PdfModal file={pdfFile} onClose={() => setPdfFile(null)} />}
    </div>
  );
}
