// Every menu key/label/icon/nav-group in the app, in sidebar order. The
// single source of truth for both App.jsx (which menus exist, in what
// order/group) and RoleMenuAccess.jsx (the Screen Access matrix, which
// needs this same list without importing App.jsx itself - keeping it here
// avoids a circular import between the two).
export const MENU = [
  ["dashboard", "Dashboard", "▤", "Main"],
  ["input", "File Explorer", "🗂", "Main"],
  ["manual", "Manual", "📖", "Main"],
  ["invoicesearch", "Invoice Search", "🔎", "Main"],
  ["buyerorder", "Buyer Order Entry", "✎", "Review & Update"],
  ["partdescupdate", "Part Description Mapping", "📝", "Review & Update"],
  ["load", "Load", "📥", "Review & Update"],
  ["post", "Post", "📮", "Accounts"],
  ["complete", "Complete", "✅", "Accounts"],
  ["vendorcode", "Vendor Code Entry", "🏷", "Super Admin"],
  ["completedinvoices", "Completed Invoices", "🗄", "Super Admin"],
  ["reports", "Reports", "📊", "Super Admin"],
  ["configuration", "Folder Configuration", "⚙", "Configuration"],
  ["dbconfig", "Database Configuration", "🗄", "Configuration"],
  ["apiconfig", "API Configuration", "🔌", "Configuration"],
  ["template", "Template", "🧩", "Configuration"],
  ["mailsettings", "Mail Server Configuration", "✉", "Configuration"],
  ["createfield", "Create Field", "✚", "Mapping"],
  ["mapping", "Field Mapping", "🔗", "Mapping"],
  ["users", "User Management", "👤", "Access Control"],
  ["rolemenus", "Screen Access", "🔐", "Access Control"],
  ["announcement", "Announcement", "📣", "Others"],
  ["publish", "Publish", "🚀", "Others"],
  ["training", "Model Training", "🧠", "Others"],
];

// Menu keys that are never configurable on the Screen Access matrix - a
// Super Admin/Developer always sees every menu (App.jsx enforces this
// regardless of tbl_RoleMenu's contents), so offering rows for those would
// be meaningless. "rolemenus" itself is excluded too: it must never be
// grantable to a lower-privileged role, since it controls who can reach
// every other screen (including itself). "reports" is Super Admin only by
// the same rule, same reasoning as rolemenus - a report can run an
// arbitrary stored procedure against live data, not something to offer a
// lower-privileged role even optionally. "vendorcode" (NAV Vendor Code
// Entry) was already never granted to any configurable role by default
// (see DEFAULT_ROLE_MENUS in App.jsx) - excluded here too so a Super Admin
// can no longer even opt a role into it from this matrix. "completedinvoices"
// was previously granted to every configurable role by default - now
// restricted to Super Admin only too (see DEFAULT_ROLE_MENUS/
// _ROLE_MENU_DEFAULTS, both updated to drop it for every role).
export const ROLE_MENU_EXCLUDED_KEYS = ["rolemenus", "reports", "vendorcode", "completedinvoices"];

// Menu keys hidden from the sidebar via CSS (App.jsx applies "nav-item-
// hidden" to these) without touching anything else - the menu still
// exists in MENU/PAGES/ROLE_MENU_EXCLUDED_KEYS, its route still works if
// navigated to directly, and its backend endpoints are untouched. To
// un-hide later, just remove the key from this list.
export const HIDDEN_MENU_KEYS = ["vendorcode"];

// The four roles a Super Admin can configure. Order shown on the matrix.
export const CONFIGURABLE_ROLES = [
  { key: "admin", label: "Admin" },
  { key: "user", label: "User" },
  { key: "accounts", label: "Accounts" },
  { key: "viewer", label: "Viewer" },
];
