import { useEffect, useMemo, useState } from "react";
import { getLifecycleInvoices, advanceLifecycle, rejectInvoice } from "./api";
import { DataTable, PdfModal, Modal } from "./components";

// Load / Post / Complete lifecycle page (one component, parameterised by
// `stage`). Lists invoices at the stage's source status(es); the user selects
// invoices — individually or a whole batch via the batch filter — and advances
// them to the stage's target status in one action.
const STAGES = {
  // Load's real target status is PURCHASE INVOICE PENDING, not LOADED
  // directly - an invoice only reaches LOADED once its Purchase Invoice
  // No. is mapped (Purchase Invoice Mapping menu). The button stays
  // labelled "Mark as Loaded" since that's still what the user is doing
  // from their own point of view; the actual next-status label the app
  // shows after clicking comes from the API response (`r.to`), not this.
  load:     { title: "Load",     action: "Mark as Loaded",    to: "PURCHASE INVOICE PENDING" },
  post:     { title: "Post",     action: "Mark as Posted",    to: "POSTED" },
  complete: { title: "Complete", action: "Mark as Completed", to: "COMPLETED" },
};

export default function Lifecycle({ user, stage }) {
  const cfg = STAGES[stage] || STAGES.load;
  const canPostOrReject = ["accounts", "admin", "super admin"]
    .includes((user?.user_type || "").toLowerCase());
  // Posting and rejecting are Accounts/Admin/Super Admin only; Load and
  // Complete stay open to everyone.
  const canAct = stage !== "post" || canPostOrReject;
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [msg, setMsg] = useState(null);
  const [selected, setSelected] = useState({});   // header_id -> bool
  const [batchFilter, setBatchFilter] = useState("");
  const [advancing, setAdvancing] = useState(false);
  const [pdfFile, setPdfFile] = useState(null);
  const [rejectTarget, setRejectTarget] = useState(null); // row being rejected
  const [rejectRemark, setRejectRemark] = useState("");
  const [rejecting, setRejecting] = useState(false);
  const [rejectError, setRejectError] = useState(null);

  const load = () => {
    setLoading(true); setError(null); setSelected({});
    getLifecycleInvoices(stage)
      .then((r) => setRows(r.invoices || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  };

  useEffect(() => { load(); /* reload when the stage changes */ }, [stage]);

  const batches = useMemo(
    () => Array.from(new Set(rows.map((r) => r.batch).filter(Boolean))).sort(),
    [rows]);

  // Load has no batch filter - it only ever shows invoices that already
  // have a real Navision Document No. (minted at Excel-download time, see
  // database._assign_document_numbers), so there's nothing un-downloaded
  // to filter out by batch in the first place.
  const visible = useMemo(() => {
    if (stage === "load") return rows.filter((r) => r.navision_doc_no);
    return batchFilter ? rows.filter((r) => r.batch === batchFilter) : rows;
  }, [rows, batchFilter, stage]);

  const selectedIds = Object.keys(selected).filter((k) => selected[k]).map(Number);
  const allVisibleSelected = visible.length > 0 && visible.every((r) => selected[r.header_id]);

  const toggleAll = () => {
    const next = { ...selected };
    const target = !allVisibleSelected;
    visible.forEach((r) => { next[r.header_id] = target; });
    setSelected(next);
  };

  const advance = async () => {
    if (!selectedIds.length) { setError("Select at least one invoice."); return; }
    setAdvancing(true); setError(null); setMsg(null);
    try {
      const r = await advanceLifecycle(stage, selectedIds, user?.user_id);
      const dupes = r.duplicate_no || [];
      if (dupes.length) {
        const list = dupes.map((d) => d.no).join(", ");
        setError(
          `${dupes.length} invoice(s) blocked - Document No. already in use: ${list}. `
          + "Resolve the duplicate (re-download with a fresh number) before loading."
        );
      }
      if (r.count) setMsg(`${r.count} invoice(s) → ${r.to}.`);
      load();
    } catch (e) { setError(e.message); }
    finally { setAdvancing(false); }
  };

  const openReject = (row) => {
    setRejectTarget(row); setRejectRemark(""); setRejectError(null);
  };

  const submitReject = async () => {
    if (!rejectRemark.trim()) { setRejectError("A remark is required."); return; }
    setRejecting(true); setRejectError(null);
    try {
      await rejectInvoice(rejectTarget.header_id, rejectRemark.trim(), user?.user_id);
      setRejectTarget(null);
      setMsg(`${rejectTarget.invoice_no || rejectTarget.file_name} → REJECTED BY ACCOUNTS.`);
      load();
    } catch (e) { setRejectError(e.message); }
    finally { setRejecting(false); }
  };

  const invoiceCell = (row) => (
    row.file_name ? (
      <button className="btn-link" onClick={() => setPdfFile(row.file_name)}
              style={{ background: "none", border: "none", padding: 0, color: "var(--primary)",
                       cursor: "pointer", textDecoration: "underline", font: "inherit" }}>
        {row.invoice_no || row.file_name}
      </button>
    ) : (row.invoice_no || "—")
  );

  const columns = [
    { key: "_sel", sortable: false,
      label: (
        <input type="checkbox" checked={allVisibleSelected} onChange={toggleAll}
               title="Select all" />
      ),
      render: (row) => (
        <input type="checkbox" checked={!!selected[row.header_id]}
               onChange={() => setSelected((s) => ({ ...s, [row.header_id]: !s[row.header_id] }))} />
      ) },
    { key: "invoice_no", label: "Invoice No.", render: invoiceCell },
    ...((stage === "load" || stage === "post") ? [{
      key: "doc_date", label: "Document Date",
      render: (row) => row.doc_date || "—",
    }] : []),
    { key: "navision_doc_no", label: "Navision Document No.",
      render: (row) => row.navision_doc_no || "—" },
    ...((stage === "post" || stage === "complete") ? [{
      key: "purchase_invoice_no", label: "Purchase Invoice No",
      render: (row) => row.purchase_invoice_no || "—",
    }] : []),
    { key: "file_name", label: "File" },
    { key: "vendor", label: "Vendor" },
    { key: "status", label: "Status" },
    ...(stage === "load" ? [{ key: "batch", label: "Batch" }] : []),
    { key: "reject_remark", label: "Reject Remark",
      render: (row) => row.reject_remark || "—" },
    ...(stage === "post" && canPostOrReject ? [{
      key: "_reject", sortable: false, label: "",
      render: (row) => (
        <button className="btn btn-danger btn-sm" onClick={() => openReject(row)}>
          ✕ Reject
        </button>
      ),
    }] : []),
  ];

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>{cfg.title}</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={load}>Refresh</button>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          {stage === "load"
            ? "Only invoices with a Navision Document No. (already downloaded) are shown. Select invoices and mark as loaded."
            : `Select invoices and ${cfg.action.toLowerCase()}.`}
        </p>

        <div style={{ display: "flex", alignItems: "center", gap: 12, marginBottom: 12, flexWrap: "wrap" }}>
          {stage !== "load" && stage !== "post" && (
            <label className="muted" style={{ display: "flex", alignItems: "center", gap: 6, fontSize: 13 }}>
              Batch
              <select className="input" style={{ width: "auto" }} value={batchFilter}
                      onChange={(e) => setBatchFilter(e.target.value)}>
                <option value="">All batches</option>
                {batches.map((b) => <option key={b} value={b}>{b}</option>)}
              </select>
            </label>
          )}
          <div style={{ flex: 1 }} />
          <span className="muted" style={{ fontSize: 13 }}>{selectedIds.length} selected</span>
          {canAct ? (
            <button className="btn btn-primary" disabled={advancing || !selectedIds.length}
                    onClick={advance}>
              {advancing ? "Working…" : cfg.action}
            </button>
          ) : (
            <span className="muted" style={{ fontSize: 13 }}>Accounts, Admin, or Super Admin access required to post.</span>
          )}
        </div>

        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {msg && <div className="alert alert-success" style={{ marginBottom: 12 }}>{msg}</div>}

        {loading ? <div className="empty">Loading…</div> : (
          <DataTable columns={columns} rows={visible.map((r) => ({ ...r, _key: r.header_id }))}
                     searchKeys={["invoice_no", "navision_doc_no", "purchase_invoice_no", "file_name", "vendor", "batch", "status"]}
                     pageSizeOptions={[10, 20, 30, "all"]}
                     empty="No invoices are eligible for this step." />
        )}
      </div>
      {pdfFile && <PdfModal file={pdfFile} onClose={() => setPdfFile(null)} />}
      {rejectTarget && (
        <Modal title={`Reject ${rejectTarget.invoice_no || rejectTarget.file_name}`}
               onClose={() => (!rejecting && setRejectTarget(null))} width={480}>
          <p className="hint" style={{ marginTop: 0 }}>
            This invoice moves back to REJECTED BY ACCOUNTS and reappears on the Load page.
          </p>
          <div className="field">
            <label className="label">Remark</label>
            <textarea className="input" rows={4} value={rejectRemark}
                      onChange={(e) => setRejectRemark(e.target.value)}
                      placeholder="Why is this invoice being rejected?" autoFocus />
          </div>
          {rejectError && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{rejectError}</div>}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, marginTop: 12 }}>
            <button className="btn btn-subtle" disabled={rejecting}
                    onClick={() => setRejectTarget(null)}>Cancel</button>
            <button className="btn btn-danger" disabled={rejecting || !rejectRemark.trim()}
                    onClick={submitReject}>
              {rejecting ? "Rejecting…" : "Reject"}
            </button>
          </div>
        </Modal>
      )}
    </div>
  );
}
