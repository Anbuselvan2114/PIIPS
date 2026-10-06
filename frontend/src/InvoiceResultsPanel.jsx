import { useState } from "react";
import { getInvoiceHistory, getInvoiceDetails, invoicePdfUrl } from "./api";
import { DataTable, Modal, PdfModal } from "./components";

// The results table + its View/Details/History actions - shared by Invoice
// Search (server-filtered by query) and Completed Invoices (the full
// COMPLETED list). Owns its own error/modal state so a caller only has to
// hand it `rows`. `selectable` additionally turns on a Load-menu-style
// checkbox column plus a "Download Selected" action (Completed Invoices
// only - Invoice Search's own single-row lookup has no use for it).
// `showStatus` (default on) shows Batch Status/File Status - Completed
// Invoices turns it off since every row there is COMPLETED by definition,
// making both columns redundant. `fileNameField` picks which field the
// File Name column displays (still "file_name" by default) - Completed
// Invoices passes "archived_name" so the column shows the ALL_INVOICES
// archive copy's own name ("<Invoice No.>_<Vendor Name>.pdf") instead of
// the original upload name; View/Download always use row.file_name/page
// regardless, since that's the original file in its status folder, not
// this archive copy.
export default function InvoiceResultsPanel({ rows, hideSearch, empty, selectable, showStatus = true,
                                               fileNameField = "file_name" }) {
  const [error, setError] = useState(null);
  const [history, setHistory] = useState(null);   // {invoice_no, file_name, events} | null
  const [historyLoading, setHistoryLoading] = useState(null);   // header_id currently loading
  const [viewing, setViewing] = useState(null);   // {file, page, pageEnd} | null - open in PdfModal
  const [details, setDetails] = useState(null);   // {invoice_no, ...payload} | null
  const [detailsLoading, setDetailsLoading] = useState(null);   // header_id currently loading
  const [detailsTab, setDetailsTab] = useState("header");   // "header" | "line" | "reservation"
  const [selected, setSelected] = useState({});   // header_id -> bool
  const [downloading, setDownloading] = useState(false);

  const openHistory = async (row) => {
    setHistoryLoading(row.header_id);
    setError(null);
    try {
      const r = await getInvoiceHistory(row.header_id);
      setHistory({ invoice_no: row.invoice_no, file_name: row.file_name, events: r.events || [] });
    } catch (e) {
      setError(e.message);
    } finally {
      setHistoryLoading(null);
    }
  };

  const openDetails = async (row) => {
    setDetailsLoading(row.header_id);
    setError(null);
    try {
      const r = await getInvoiceDetails(row.header_id);
      setDetails({ invoice_no: row.invoice_no, file_name: row.file_name, ...r });
      setDetailsTab("header");
    } catch (e) {
      setError(e.message);
    } finally {
      setDetailsLoading(null);
    }
  };

  const keyedRows = (rows || []).map((r, i) => ({ ...r, _key: r.header_id ?? i }));

  const selectedIds = Object.keys(selected).filter((k) => selected[k]).map(Number);
  const allSelected = keyedRows.length > 0 && keyedRows.every((r) => selected[r.header_id]);

  const toggleAll = () => {
    const next = { ...selected };
    const target = !allSelected;
    keyedRows.forEach((r) => { next[r.header_id] = target; });
    setSelected(next);
  };

  // Multiple browser downloads triggered back-to-back can get silently
  // dropped/blocked as "too many at once" - a small stagger between each
  // <a download> click keeps every one of them a distinct user-gesture-
  // adjacent action instead.
  const downloadSelected = async () => {
    const picked = keyedRows.filter((r) => selected[r.header_id] && r.file_name);
    if (!picked.length) return;
    setDownloading(true);
    for (let i = 0; i < picked.length; i++) {
      const row = picked[i];
      const a = document.createElement("a");
      a.href = invoicePdfUrl(row.file_name, row.page, row.page_end);
      a.download = row.file_name;
      document.body.appendChild(a);
      a.click();
      a.remove();
      if (i < picked.length - 1) await new Promise((res) => setTimeout(res, 400));
    }
    setDownloading(false);
  };

  const columns = [
    ...(selectable ? [{
      key: "_sel", sortable: false,
      label: (
        <input type="checkbox" checked={allSelected} onChange={toggleAll} title="Select all" />
      ),
      render: (row) => (
        <input type="checkbox" checked={!!selected[row.header_id]}
               onChange={() => setSelected((s) => ({ ...s, [row.header_id]: !s[row.header_id] }))} />
      ),
    }] : []),
    { key: fileNameField, label: "File Name",
      render: (row) => (
        <button className="btn-link" style={{ background: "none", border: "none", padding: 0,
                                                color: "var(--primary)", cursor: "pointer",
                                                textDecoration: "none", font: "inherit",
                                                whiteSpace: "normal", textAlign: "left" }}
                onClick={() => setViewing({ file: row.file_name, page: row.page, pageEnd: row.page_end })}>
          {row[fileNameField]}
        </button>
      ) },
    { key: "invoice_no", label: "Invoice No." },
    { key: "vendor", label: "Vendor Name" },
    { key: "batch", label: "Batch" },
    ...(showStatus ? [
      { key: "batch_status", label: "Batch Status" },
      { key: "status", label: "File Status" },
    ] : []),
    { key: "_details", label: "", sortable: false,
      render: (row) => (row.invoice_type === "PART" ? (
        <button className="btn btn-subtle btn-sm" disabled={detailsLoading === row.header_id}
                onClick={() => openDetails(row)}>
          {detailsLoading === row.header_id ? "Loading…" : "Details"}
        </button>
      ) : null) },
    { key: "_history", label: "", sortable: false,
      render: (row) => (row.header_id > 0 ? (
        <button className="btn btn-subtle btn-sm" disabled={historyLoading === row.header_id}
                onClick={() => openHistory(row)}>
          {historyLoading === row.header_id ? "Loading…" : "History"}
        </button>
      ) : null) },
  ];

  return (
    <>
      {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
      {selectable && (
        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12 }}>
          <span className="muted" style={{ fontSize: 13 }}>{selectedIds.length} selected</span>
          <button className="btn btn-primary btn-sm" disabled={!selectedIds.length || downloading}
                  onClick={downloadSelected}>
            {downloading ? "Downloading…" : "Download Selected"}
          </button>
        </div>
      )}
      <DataTable columns={columns} rows={keyedRows}
                 hideSearch={hideSearch}
                 pageSizeOptions={[10, 20, 30, "all"]}
                 empty={empty || "No invoices."} />

      {history && (
        <Modal title={`History — ${history.invoice_no || history.file_name}`}
               onClose={() => setHistory(null)} width={720}>
          {history.events.length === 0 ? (
            <div className="empty">No tracking history recorded for this invoice yet.</div>
          ) : (
            <div className="timeline">
              {history.events.map((ev) => (
                <div key={ev.Id} className="timeline-item">
                  <div className="timeline-dot" />
                  <div className="timeline-content">
                    <div className="timeline-header">
                      <span className="timeline-action">{ev.Action || "Unknown"}</span>
                      <span className="timeline-time">{ev.EventDatetime || "Unknown"}</span>
                    </div>
                    <div className="timeline-who">
                      {ev.UserName || "Unknown"}
                      {ev.ToStatus ? (
                        <> · {ev.FromStatus ? `${ev.FromStatus} → ${ev.ToStatus}` : ev.ToStatus}</>
                      ) : null}
                    </div>
                    {ev.Detail && <div className="timeline-detail">{ev.Detail}</div>}
                  </div>
                </div>
              ))}
            </div>
          )}
        </Modal>
      )}

      {details && (() => {
        const hasReservations = (details.reservations || []).length > 0;
        const tabs = [
          { key: "header", label: "Purchase Header" },
          { key: "line", label: "Purchase Line" },
          ...(hasReservations ? [{ key: "reservation", label: "Reservation" }] : []),
        ];
        return (
          <Modal title={`Details — ${details.invoice_no || details.file_name}`}
                 onClose={() => setDetails(null)} width={820}>
            <div style={{ display: "flex", gap: 6, borderBottom: "1px solid var(--border)", marginBottom: 14 }}>
              {tabs.map((t) => (
                <button key={t.key}
                        className={`btn btn-sm ${detailsTab === t.key ? "btn-primary" : "btn-subtle"}`}
                        style={{ borderRadius: "6px 6px 0 0" }}
                        onClick={() => setDetailsTab(t.key)}>
                  {t.label}
                </button>
              ))}
            </div>
            {detailsTab === "header" && (
              <DetailFields title="Purchase Header" columns={details.header_columns} row={details.header} />
            )}
            {detailsTab === "line" && (details.lines || []).map((line, i) => (
              <DetailFields key={i} title={`Purchase Line ${i + 1}`}
                            columns={details.line_columns} row={line} />
            ))}
            {detailsTab === "reservation" && hasReservations && (details.reservations || []).map((res, i) => (
              <DetailFields key={i} title={`Reservation Entry ${i + 1}`}
                            columns={details.reservation_columns} row={res} />
            ))}
          </Modal>
        );
      })()}

      {viewing && (
        <PdfModal file={viewing.file} page={viewing.page} pageEnd={viewing.pageEnd}
                  onClose={() => setViewing(null)} />
      )}
    </>
  );
}

// Every configured column for one sheet/row, blank ones included (nothing
// hidden just because it's empty or optional) - a plain key:value grid, no
// who/when (that's History's job).
function DetailFields({ title, columns, row }) {
  if (!columns || columns.length === 0) return null;
  return (
    <div className="card" style={{ marginBottom: 12 }}>
      <h3 style={{ marginTop: 0 }}>{title}</h3>
      <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "6px 20px" }}>
        {columns.map((col) => (
          <div key={col} style={{ display: "flex", justifyContent: "space-between", gap: 12,
                                   borderBottom: "1px solid var(--border)", padding: "4px 0" }}>
            <span className="muted" style={{ fontSize: 13 }}>{col}</span>
            <span style={{ fontWeight: 500, textAlign: "right", wordBreak: "break-word" }}>
              {(row && row[col]) || <span className="muted">—</span>}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
