import { useEffect, useMemo, useState } from "react";
import { invoicePdfUrl } from "./api";

// PIIPS logo — a self-contained SVG emblem (teal card + invoice + processed
// check). Colours are baked in so it reads on any theme's sidebar/login.
export function Logo({ size = 40 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 64 64" fill="none"
         xmlns="http://www.w3.org/2000/svg" role="img" aria-label="PIIPS">
      <defs>
        <linearGradient id="piips-g" x1="0" y1="0" x2="64" y2="64" gradientUnits="userSpaceOnUse">
          <stop stopColor="#22d3ee" /><stop offset="1" stopColor="#0e7490" />
        </linearGradient>
      </defs>
      <rect x="4" y="4" width="56" height="56" rx="15" fill="url(#piips-g)" />
      <rect x="18" y="13" width="23" height="31" rx="3" fill="#ffffff" />
      <rect x="22" y="19" width="15" height="2.6" rx="1.3" fill="#0e7490" />
      <rect x="22" y="25" width="15" height="2.6" rx="1.3" fill="#9aa8bd" />
      <rect x="22" y="31" width="9" height="2.6" rx="1.3" fill="#9aa8bd" />
      <circle cx="43" cy="43" r="10" fill="#16a34a" stroke="#ffffff" strokeWidth="2.5" />
      <path d="M38.4 43.2l3 3 5.4-6" stroke="#ffffff" strokeWidth="2.6"
            strokeLinecap="round" strokeLinejoin="round" fill="none" />
    </svg>
  );
}

// Generic table: search box, click-to-sort headers, 10-row pagination.
// columns: [{ key, label, render?(row), sortable? }]
export function DataTable({ columns, rows, searchKeys, pageSize = 10,
                            pageSizeOptions, empty, actions }) {
  const [query, setQuery] = useState("");
  const [sortKey, setSortKey] = useState(null);
  const [sortDir, setSortDir] = useState("asc");
  const [page, setPage] = useState(0);
  // When pageSizeOptions is given, the user can change rows-per-page (incl.
  // "All"); otherwise the fixed `pageSize` prop is used.
  const [size, setSize] = useState(pageSize);
  const effectiveSize = size === "all" ? Math.max(rows.length, 1) : size;

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase();
    if (!q) return rows;
    const keys = searchKeys || columns.map((c) => c.key);
    return rows.filter((row) =>
      keys.some((k) => String(row[k] ?? "").toLowerCase().includes(q))
    );
  }, [rows, query, searchKeys, columns]);

  const sorted = useMemo(() => {
    if (!sortKey) return filtered;
    const arr = [...filtered];
    arr.sort((a, b) => {
      const av = a[sortKey], bv = b[sortKey];
      const an = Number(av), bn = Number(bv);
      let cmp;
      if (!isNaN(an) && !isNaN(bn) && av !== "" && bv !== "") cmp = an - bn;
      else cmp = String(av ?? "").localeCompare(String(bv ?? ""));
      return sortDir === "asc" ? cmp : -cmp;
    });
    return arr;
  }, [filtered, sortKey, sortDir]);

  const pageCount = Math.max(1, Math.ceil(sorted.length / effectiveSize));
  const cur = Math.min(page, pageCount - 1);
  const view = sorted.slice(cur * effectiveSize, cur * effectiveSize + effectiveSize);

  const toggleSort = (key) => {
    if (sortKey === key) setSortDir((d) => (d === "asc" ? "desc" : "asc"));
    else { setSortKey(key); setSortDir("asc"); }
  };

  useEffect(() => { setPage(0); }, [query, rows, size]);

  const searchRow = (
    <div style={{ marginBottom: 10, display: "flex", alignItems: "center", gap: 8 }}>
      <input className="input" placeholder="Search…" value={query}
             onChange={(e) => setQuery(e.target.value)} style={{ maxWidth: 260 }} />
      {actions}
    </div>
  );

  if (!rows.length) {
    return (
      <div>
        {actions && searchRow}
        <div className="empty">{empty || "No rows."}</div>
      </div>
    );
  }

  return (
    <div>
      {searchRow}
      <div className="table-wrap">
        <table className="tbl">
          <thead>
            <tr>
              {columns.map((c) => (
                <th key={c.key}
                    onClick={c.sortable === false ? undefined : () => toggleSort(c.key)}
                    style={{ cursor: c.sortable === false ? "default" : "pointer", userSelect: "none" }}>
                  {c.label}{sortKey === c.key ? (sortDir === "asc" ? " ▲" : " ▼") : ""}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {view.map((row, i) => (
              <tr key={row._key ?? i}>
                {columns.map((c) => (
                  <td key={c.key}>{c.render ? c.render(row) : (row[c.key] ?? "—")}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 12, marginTop: 10 }}>
        <span className="muted" style={{ fontSize: 13 }}>
          {sorted.length} row{sorted.length === 1 ? "" : "s"}
          {query ? ` (filtered from ${rows.length})` : ""}
        </span>
        {pageSizeOptions && (
          <label className="muted" style={{ fontSize: 13, display: "flex", alignItems: "center", gap: 6 }}>
            Rows
            <select className="input" style={{ width: "auto", padding: "2px 6px" }}
                    value={size}
                    onChange={(e) => setSize(e.target.value === "all" ? "all" : Number(e.target.value))}>
              {pageSizeOptions.map((o) => (
                <option key={o} value={o}>{o === "all" ? "All" : o}</option>
              ))}
            </select>
          </label>
        )}
        <div style={{ flex: 1 }} />
        <button className="btn btn-subtle btn-sm" disabled={cur <= 0}
                onClick={() => setPage(cur - 1)}>‹ Prev</button>
        <span className="muted" style={{ fontSize: 13 }}>Page {cur + 1} / {pageCount}</span>
        <button className="btn btn-subtle btn-sm" disabled={cur >= pageCount - 1}
                onClick={() => setPage(cur + 1)}>Next ›</button>
      </div>
    </div>
  );
}

export function Modal({ title, onClose, children, width = 1000 }) {
  return (
    <div onClick={onClose}
         style={{ position: "fixed", inset: 0, background: "rgba(0,0,0,.5)", zIndex: 1000,
                  display: "flex", alignItems: "flex-start", justifyContent: "center",
                  padding: "48px 16px", overflow: "auto" }}>
      <div onClick={(e) => e.stopPropagation()} className="card"
           style={{ width: `min(${width}px, 100%)`, maxWidth: width, margin: 0 }}>
        <div className="card-title-row">
          <h3>{title}</h3>
          <div style={{ flex: 1 }} />
          <button className="btn btn-subtle btn-sm" onClick={onClose}>✕ Close</button>
        </div>
        {children}
      </div>
    </div>
  );
}

// PDF/image viewer pop-up (opened by clicking an invoice number).
export function PdfModal({ file, onClose }) {
  return (
    <Modal title={file} onClose={onClose} width={1100}>
      <div style={{ height: "75vh" }}>
        <iframe title={file} src={invoicePdfUrl(file)}
                style={{ width: "100%", height: "100%", border: "none", borderRadius: 8 }} />
      </div>
    </Modal>
  );
}
