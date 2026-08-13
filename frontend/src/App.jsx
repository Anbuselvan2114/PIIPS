import { useState, useEffect } from "react";
import { Logo } from "./components";
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
import Manuals from "./Manuals";

// key, label, icon, nav group
const MENU = [
  ["dashboard", "Dashboard", "▤", "Main"],
  ["input", "File Explorer", "🗂", "Main"],
  ["manual", "Manual", "📖", "Main"],
  ["buyerorder", "Buyer Order Entry", "✎", "Review"],
  ["load", "Load", "📥", "Accounts"],
  ["post", "Post", "📤", "Accounts"],
  ["complete", "Complete", "✅", "Accounts"],
  ["configuration", "Folder Configuration", "⚙", "Setup"],
  ["dbconfig", "Database Configuration", "🗄", "Setup"],
  ["apiconfig", "API Configuration", "🔌", "Setup"],
  ["template", "Template", "🧩", "Setup"],
  ["createfield", "Create Field", "✚", "Mapping"],
  ["mapping", "Field Mapping", "🔗", "Mapping"],
  ["training", "Model Training", "🧠", "Admin"],
  ["users", "User Management", "👤", "Admin"],
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
};

const loadUser = () => {
  try { return JSON.parse(localStorage.getItem("piips_user")); } catch { return null; }
};

export default function App() {
  const [user, setUser] = useState(loadUser);
  const [authView, setAuthView] = useState("login");
  const [page, setPage] = useState("dashboard");
  const [collapsed, setCollapsed] = useState(() => localStorage.getItem("piips_collapsed") === "1");
  const [theme, setTheme] = useState(() => localStorage.getItem("piips_theme") || "standard");

  useEffect(() => {
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem("piips_theme", theme);
  }, [theme]);

  const toggleCollapsed = () => {
    setCollapsed((c) => { localStorage.setItem("piips_collapsed", c ? "0" : "1"); return !c; });
  };

  if (!user) {
    return authView === "forgot"
      ? <ForgotPassword onDone={() => setAuthView("login")} />
      : <Login
          onSuccess={(u) => { localStorage.setItem("piips_user", JSON.stringify(u)); setUser(u); setPage("dashboard"); }}
          onForgot={() => setAuthView("forgot")}
        />;
  }

  const logout = () => { localStorage.removeItem("piips_user"); setUser(null); setAuthView("login"); };

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
          <label className="theme-select" title="Theme">
            <span className="ico">🎨</span>
            <select value={theme} onChange={(e) => setTheme(e.target.value)}>
              <option value="standard">Standard</option>
              <option value="light">Light</option>
              <option value="dark">Dark</option>
              <option value="blue">Blue</option>
              <option value="darkblue">Dark Blue</option>
            </select>
          </label>
        </header>
        <div className="content">{Active ? <Active user={user} /> : null}</div>
      </div>
    </div>
  );
}
