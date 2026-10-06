import { useEffect, useState } from "react";
import { getCompletedInvoices } from "./api";
import InvoiceResultsPanel from "./InvoiceResultsPanel";

// Every invoice that's finished the whole lifecycle (Load -> Post ->
// Complete) - exactly the ones whose PDF also got archived into
// <Folder Path>/ALL_INVOICES along the way (see
// config_store.copy_pdf_to_all_invoices). Reads from the database, always
// up to date, not a folder listing.
export default function CompletedInvoices() {
  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    getCompletedInvoices()
      .then((r) => setRows(r.invoices || []))
      .catch((e) => setError(e.message))
      .finally(() => setLoading(false));
  }, []);

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Completed Invoices</h3>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Every invoice that has finished the full Load → Post → Complete
          lifecycle.
        </p>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {loading ? (
          <div className="card">Loading…</div>
        ) : (
          <InvoiceResultsPanel rows={rows} empty="No completed invoices yet." selectable showStatus={false}
                               fileNameField="archived_name" />
        )}
      </div>
    </div>
  );
}
