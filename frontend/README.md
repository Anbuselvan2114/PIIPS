# PIIPS frontend (drop-in)

Three files to copy into your React app's `src/`:

- `api.js` — API client (config + processing endpoints)
- `Configuration.jsx` — save the local PDF folder path
- `Dashboard.jsx` — Start button, live progress bar (polling), results table

They use plain React + `fetch` and inline styles, so there are **no extra
dependencies**. Restyle with your existing UI library as you like.

## Wiring into your drawer menu

You already have the left drawer with a **Configuration** item. Just render
these two components for their routes. Example with your existing router:

```jsx
import Dashboard from "./Dashboard";
import Configuration from "./Configuration";

// inside your <Routes> (react-router v6)
<Route path="/" element={<Dashboard />} />
<Route path="/configuration" element={<Configuration />} />
```

Minimal standalone example (no router) if you need one:

```jsx
import { useState } from "react";
import Dashboard from "./Dashboard";
import Configuration from "./Configuration";

export default function App() {
  const [page, setPage] = useState("dashboard");
  const menu = [
    ["dashboard", "Dashboard"],
    ["configuration", "Configuration"],
  ];
  return (
    <div style={{ display: "flex", minHeight: "100vh" }}>
      <nav style={{ width: 220, background: "#0b1021", color: "#fff", padding: 16 }}>
        <h3 style={{ marginTop: 0 }}>PIIPS</h3>
        {menu.map(([key, label]) => (
          <div
            key={key}
            onClick={() => setPage(key)}
            style={{
              padding: "10px 12px",
              borderRadius: 6,
              cursor: "pointer",
              background: page === key ? "#0b7285" : "transparent",
            }}
          >
            {label}
          </div>
        ))}
      </nav>
      <main style={{ flex: 1 }}>
        {page === "dashboard" ? <Dashboard /> : <Configuration />}
      </main>
    </div>
  );
}
```

## API base URL

`api.js` uses a **relative** base by default, which is correct in production
(IIS serves the React build and reverse-proxies `/api/*` to FastAPI on
`127.0.0.1:8000`).

For local dev against the backend directly, set `VITE_API_BASE` (Vite) — e.g.
create `.env.local` with:

```
VITE_API_BASE=http://localhost:8000
```

(For Create React App, replace the `import.meta.env` line in `api.js` with your
`process.env.REACT_APP_API_BASE`.)

## Backend endpoints

| Method | Path                          | Purpose                                   |
|--------|-------------------------------|-------------------------------------------|
| GET    | `/api/config`                 | Current config (`pdf_folder`, `output_folder`) |
| POST   | `/api/config`                 | Save `{ "pdf_folder": "..." }` (validates the folder exists) |
| POST   | `/api/process/start`          | Start a job → `{ job_id }` (409 if one is already running) |
| GET    | `/api/process/status/{job_id}`| Live progress: `status`, `total`, `processed`, `percent`, `current_file` |
| GET    | `/api/process/result/{job_id}`| Full results incl. each PDF's extracted JSON |

Each processed PDF is also written as `<name>.json` into the configured
`output_folder` (defaults to `<app>/output`).
