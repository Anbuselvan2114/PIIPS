import { useState, useEffect } from "react";
import { Logo, AnnouncementBell } from "./components";
import Dashboard from "./Dashboard";
import Configuration from "./Configuration";
import DatabaseConfig from "./DatabaseConfig";
import ApiConfiguration from "./ApiConfiguration";
import Training from "./Training";
import InputFiles from "./InputFiles";
import CreateField from "./CreateField";
import Mapping from "./Mapping";
import Template from "./Template";
import UserManagement from "./UserManagement";
import BuyerOrderEntry from "./BuyerOrderEntry";
import Lifecycle from "./Lifecycle";
import Login from "./Login";
import ForgotPassword from "./ForgotPassword";
import ChangePassword from "./ChangePassword";
import MailSettings from "./MailSettings";
import Announcement from "./Announcement";
import Manuals from "./Manuals";
import Publish from "./Publish";
import { getConfig } from "./api";

// key, label, icon, nav group
const MENU = [
  ["dashboard", "Dashboard", "▤", "Main"],
  ["input", "File Explorer", "🗂", "Main"],
  ["manual", "Manual", "📖", "Main"],
  ["buyerorder", "Buyer Order Entry", "✎", "Review"],
  ["load", "Load", "📥", "Accounts"],
  ["post", "Post", "📮", "Accounts"],
  ["complete", "Complete", "✅", "Accounts"],
  ["configuration", "Folder Configuration", "⚙", "Setup"],
  ["dbconfig", "Database Configuration", "🗄", "Setup"],
  ["apiconfig", "API Configuration", "🔌", "Setup"],
  ["template", "Template", "🧩", "Setup"],
  ["createfield", "Create Field", "✚", "Mapping"],
  ["mapping", "Field Mapping", "🔗", "Mapping"],
  ["training", "Model Training", "🧠", "Admin"],
  ["users", "User Management", "👤", "Admin"],
  ["mailsettings", "Mail Server Setting", "✉", "Admin"],
  ["announcement", "Announcement", "📣", "Admin"],
  ["publish", "Publish", "🚀", "Admin"],
];

const ROLE_MENUS = {
  "super admin": MENU.map(([k]) => k),
  developer: MENU.map(([k]) => k),   // legacy alias (pre-rename sessions)
  admin: ["dashboard", "input", "manual", "buyerorder", "load", "post", "complete",
          "configuration", "apiconfig", "template", "createfield", "users"],
  // Users process invoices, fix Buyer Order Nos, and Load them.
  user: ["dashboard", "input", "manual", "buyerorder", "load"],
  // Accounts run the downstream Post / Complete steps.
  accounts: ["dashboard", "input", "manual", "post", "complete"],
  // Viewer sees every page (read-only) - every mutating action is blocked
  // server-side too (app.py's _require_not_viewer), this is just so
  // nothing is hidden from them.
  viewer: MENU.map(([k]) => k),
};

// Load / Post / Complete are one component parameterised by stage.
const Load = (p) => <Lifecycle {...p} stage="load" />;
const Post = (p) => <Lifecycle {...p} stage="post" />;
const Complete = (p) => <Lifecycle {...p} stage="complete" />;

const PAGES = {
  dashboard: Dashboard, configuration: Configuration, input: InputFiles,
  training: Training, createfield: CreateField, mapping: Mapping,
  template: Template, users: UserManagement, dbconfig: DatabaseConfig,
  apiconfig: ApiConfiguration, buyerorder: BuyerOrderEntry,
  load: Load, post: Post, complete: Complete, manual: Manuals,
  publish: Publish, mailsettings: MailSettings, announcement: Announcement,
};

const loadUser = () => {
  // A "Sign in to PIIPS" link from a welcome/password-reset email carries
  // ?signout=1 - it's often opened in the same browser an admin just used
  // to create the account, so any session already active here must be
  // cleared instead of silently showing that admin's own dashboard.
  const params = new URLSearchParams(window.location.search);
  if (params.get("signout") === "1") {
    localStorage.removeItem("piips_user");
    params.delete("signout");
    const rest = params.toString();
    window.history.replaceState({}, "", window.location.pathname + (rest ? `?${rest}` : ""));
    return null;
  }
  try { return JSON.parse(localStorage.getItem("piips_user")); } catch { return null; }
};

export default function App() {
  const [user, setUser] = useState(loadUser);
  const [authView, setAuthView] = useState("login");
  const [page, setPage] = useState("dashboard");
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("piips_collapsed") === "1");
  const [theme, setTheme] = useState(() => localStorage.getItem("piips_theme") || "standard");
  // null = still checking. Logging in needs a working DB, so a fresh
  // deploy with an empty/missing config.json would otherwise strand
  // everyone on a Login screen that can never succeed, with no way to
  // reach Database Configuration (that page is normally only reachable
  // AFTER logging in). Checked before rendering Login at all - see
  // /api/config's public db_configured flag and /api/db-config's matching
  // bootstrap exception (both skip the Super-Admin gate only while this
  // is false).
  const [dbConfigured, setDbConfigured] = useState(null);

  useEffect(() => {
    getConfig()
      .then((c) => setDbConfigured(!!c.db_configured))
      // If /api/config itself can't be reached, the backend is down for
      // everyone regardless - fail open to the normal Login screen rather
      // than get stuck showing nothing.
      .catch(() => setDbConfigured(true));
  }, []);

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("piips_theme", theme);
  }, [theme]);

  // Casual deterrents only, not real security - DevTools is a feature of
  // the user's own browser and cannot be blocked from the page they're
  // viewing (a different shortcut, the browser menu, or an external tool
  // always gets there). Anything that actually matters is enforced
  // server-side regardless of what's visible here.
  useEffect(() => {
    const blockContextMenu = (e) => e.preventDefault();
    const blockDevToolsKeys = (e) => {
      const key = e.key.toUpperCase();
      if (key === "F12") { e.preventDefault(); return; }
      if (e.ctrlKey && e.shiftKey && (key === "I" || key === "J" || key === "C")) { e.preventDefault(); return; }
      if (e.ctrlKey && key === "U") { e.preventDefault(); }
    };
    document.addEventListener("contextmenu", blockContextMenu);
    document.addEventListener("keydown", blockDevToolsKeys);
    return () => {
      document.removeEventListener("contextmenu", blockContextMenu);
      document.removeEventListener("keydown", blockDevToolsKeys);
    };
  }, []);

  const toggleCollapsed = () => {
    setCollapsed((c) => { localStorage.setItem("piips_collapsed", c ? "0" : "1"); return !c; });
  };

  if (dbConfigured === null) {
    return <div className="page"><div className="card">Loading…</div></div>;
  }

  if (!dbConfigured) {
    return <DatabaseConfig onSaved={() => setDbConfigured(true)} />;
  }

  if (!user) {
    return authView === "forgot"
      ? <ForgotPassword onDone={() => setAuthView("login")} />
      : <Login
          onSuccess={(u) => { localStorage.setItem("piips_user", JSON.stringify(u)); setUser(u); setPage("dashboard"); }}
          onForgot={() => setAuthView("forgot")}
        />;
  }

  const logout = () => { localStorage.removeItem("piips_user"); setUser(null); setAuthView("login"); };

  // A freshly-created account, or one that just went through Forgot
  // password, must set its own password before doing anything else.
  if (user.must_change_password) {
    return (
      <ChangePassword
        user={user}
        onLogout={logout}
        onDone={() => {
          const updated = { ...user, must_change_password: false };
          localStorage.setItem("piips_user", JSON.stringify(updated));
          setUser(updated);
        }}
      />
    );
  }

  const allowed = ROLE_MENUS[(user.user_type || "").toLowerCase()] || ROLE_MENUS.user;
  const menu = MENU.filter(([k]) => allowed.includes(k));
  const activePage = allowed.includes(page) ? page : menu[0][0];
  const Active = PAGES[activePage];
  const title = (MENU.find(([k]) => k === activePage) || [, "PIIPS"])[1];

  // group the menu items
  const groups = [];
  menu.forEach(([k, label, ico, grp]) => {
    let g = groups.find((x) => x.name === grp);
    if (!g) { g = { name: grp, items: [] }; groups.push(g); }
    g.items.push([k, label, ico]);
  });

  return (
    <div className="app">
      <nav className={`sidebar${collapsed ? " collapsed" : ""}`}>
        <div className="brand">
          <span className="brand-mark"><Logo size={34} /></span>
          <div className="brand-text">
            <div className="brand-name">PIIPS</div>
            <div className="brand-sub">Invoice Processing Suite</div>
          </div>
        </div>

        {groups.map((g) => (
          <div key={g.name}>
            <div className="nav-group-label">{g.name}</div>
            {g.items.map(([k, label, ico]) => (
              <div key={k} title={label}
                   className={`nav-item${activePage === k ? " active" : ""}`}
                   onClick={() => setPage(k)}>
                <span className="ico">{ico}</span><span className="nav-label">{label}</span>
              </div>
            ))}
          </div>
        ))}

        <div className="sidebar-footer">
          <div className="user-chip">
            <div className="avatar">{(user.username || "?").slice(0, 1).toUpperCase()}</div>
            <div className="user-meta">
              <div className="user-name">{user.username}</div>
              <div className="user-role">{user.user_type}</div>
            </div>
          </div>
          <div className="nav-item" onClick={logout} title="Logout" style={{ color: "#fca5a5" }}>
            <span className="ico">⏻</span><span className="nav-label">Logout</span>
          </div>
        </div>
      </nav>

      <div className="main">
        <header className="topbar">
          <button className="toggle" onClick={toggleCollapsed} title={collapsed ? "Expand menu" : "Collapse menu"}>☰</button>
          <h1>{title}</h1>
          <div style={{ flex: 1 }} />
          <AnnouncementBell />
          <label className="theme-select" title="Theme">
            <span className="ico">🎨</span>
            <select value={theme} onChange={(e) => setTheme(e.target.value)}>
              <option value="standard">Standard</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
              <option value="oceanblue">Ocean Blue</option>
            </select>
          </label>
        </header>
        <div className="content">{Active ? <Active user={user} /> : null}</div>
      </div>
    </div>
  );
}
