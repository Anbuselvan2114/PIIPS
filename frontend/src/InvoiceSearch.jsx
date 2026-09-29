import { useRef, useState } from "react";
import { searchInvoices } from "./api";
import InvoiceResultsPanel from "./InvoiceResultsPanel";

// Look up one or more invoices by Invoice No. and see, per invoice: its file
// name, vendor, which batch it's in and how that batch itself is
// progressing, its own current status, and a full who-did-what-when
// timeline (Buyer Order Entry, Load/Post/Complete, Part Description Mapping
// updates, ...).
export default function InvoiceSearch() {
  const [query, setQuery] = useState("");
  const [rows, setRows] = useState([]);
  const [searched, setSearched] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Type-ahead suggestions: the same search, run automatically (debounced)
  // as the user types, shown as a picklist below the box - picking one
  // fills the box and runs the full search right away. Only ever a
  // convenience on top of the explicit Search button, never a replacement
  // for it (Enter/Search still work with nothing picked).
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const debounceRef = useRef(null);

  const runSearch = async (qOverride) => {
    const q = (qOverride ?? query).trim();
    if (!q) return;
    setShowSuggestions(false);
    setLoading(true); setError(null);
    try {
      const r = await searchInvoices(q);
      setRows(r.invoices || []);
      setSearched(true);
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };

  const onQueryChange = (value) => {
    setQuery(value);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    const q = value.trim();
    if (q.length < 2) {
      setSuggestions([]); setShowSuggestions(false);
      return;
    }
    debounceRef.current = setTimeout(async () => {
      try {
        const r = await searchInvoices(q);
        setSuggestions((r.invoices || []).slice(0, 8));
        setShowSuggestions(true);
      } catch {
        // A suggestion-lookup failure is silent - the explicit Search
        // button/Enter still works and will surface any real error.
      }
    }, 300);
  };

  const pickSuggestion = (invoiceNo) => {
    setQuery(invoiceNo);
    setSuggestions([]); setShowSuggestions(false);
    runSearch(invoiceNo);
  };

  return (
    <div className="page">
      <div className="card">
        <div className="card-title-row">
          <h3>Invoice Search</h3>
        </div>
        <p className="hint" style={{ marginTop: 0 }}>
          Look up an invoice by Invoice No. (or part of it) to see its file,
          vendor, batch, current status, and full tracking history.
        </p>
        <div className="row" style={{ marginBottom: 14, position: "relative" }}>
          <div style={{ position: "relative", maxWidth: 320, width: "100%" }}>
            <input className="input" placeholder="Invoice No., e.g. AVS/26-27/00668" value={query}
                   style={{ width: "100%" }}
                   onChange={(e) => onQueryChange(e.target.value)}
                   onFocus={() => suggestions.length > 0 && setShowSuggestions(true)}
                   onBlur={() => setTimeout(() => setShowSuggestions(false), 150)}
                   onKeyDown={(e) => { if (e.key === "Enter") runSearch(); }} />
            {showSuggestions && suggestions.length > 0 && (
              <div style={{
                position: "absolute", top: "100%", left: 0, right: 0, zIndex: 20,
                background: "var(--surface)", border: "1px solid var(--border)",
                borderRadius: 6, marginTop: 2, maxHeight: 260, overflowY: "auto",
                boxShadow: "0 4px 12px rgba(0,0,0,.12)",
              }}>
                {suggestions.map((s) => (
                  <div key={s.header_id} onMouseDown={(e) => { e.preventDefault(); pickSuggestion(s.invoice_no); }}
                       style={{ padding: "8px 10px", fontSize: 13, cursor: "pointer",
                                borderBottom: "1px solid var(--border)" }}>
                    <div style={{ fontWeight: 600 }}>{s.invoice_no}</div>
                    <div className="muted" style={{ fontSize: 12 }}>{s.file_name} · {s.vendor}</div>
                  </div>
                ))}
              </div>
            )}
          </div>
          <button className="btn btn-primary" disabled={loading || !query.trim()} onClick={() => runSearch()}>
            {loading ? "Searching…" : "Search"}
          </button>
        </div>
        {error && <div className="alert alert-danger" style={{ marginBottom: 12 }}>{error}</div>}
        {searched && (
          <InvoiceResultsPanel rows={rows} hideSearch empty="No invoice matches that Invoice No." />
        )}
      </div>
    </div>
  );
}
