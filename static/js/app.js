// ============ Family Financial Control – frontend logic ============
const API = ""; // same origin
let state = {
  token: localStorage.getItem("ff_token") || null,
  user: null,
  categories: [],
  trendChart: null,
  totalsChart: null,
  reportChart: null,
  currentPage: "dashboard",
  backup: null,
};

// ---------- helpers ----------
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function escapeHtml(value) {
  return String(value == null ? "" : value)
    .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
}

function showAlert(message, type = "success") {
  const area = $("#alert-area");
  area.innerHTML = `<div class="alert alert-${type} alert-dismissible fade show" role="alert">
    ${message}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>`;
  setTimeout(() => { area.innerHTML = ""; }, 6000);
}

function describeError(data, fallback) {
  const detail = data && data.detail;
  if (!detail) return fallback;
  if (typeof detail === "string") return detail;
  if (detail.message) {
    const extra = Array.isArray(detail.errors) && detail.errors.length
      ? " " + detail.errors.slice(0, 5).map(e => `(row ${e.row}: ${e.error})`).join(" ")
      : "";
    return detail.message + extra;
  }
  return JSON.stringify(detail);
}

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) headers["Authorization"] = "Bearer " + state.token;
  if (options.body && !(options.body instanceof FormData)) {
    headers["Content-Type"] = "application/json";
  }
  const res = await fetch(API + path, { ...options, headers });
  if (res.status === 401) { logout(); throw new Error("Session expired"); }
  const data = res.status === 204 ? null : await res.json().catch(() => null);
  if (!res.ok) throw new Error(describeError(data, "Request failed"));
  return data;
}

function fmtMoney(n) {
  return (Number(n) || 0).toLocaleString("en-US", { maximumFractionDigits: 0 });
}

function canEdit() {
  return state.user && ["master_admin", "admin", "editor"].includes(state.user.role);
}
function canDownload() {
  return state.user && ["master_admin", "admin", "editor", "downloader"].includes(state.user.role);
}
function canAdmin() {
  return state.user && ["master_admin", "admin"].includes(state.user.role);
}
function isMaster() { return state.user && state.user.role === "master_admin"; }

function backupFileName(ext) {
  const d = new Date();
  const p = (n) => String(n).padStart(2, "0");
  const stamp = `${d.getFullYear()}${p(d.getMonth() + 1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}`;
  return `family_finance_backup_${stamp}.${ext}`;
}

// ---------- auth ----------
async function login(username, password) {
  const data = await api("/api/auth/login", {
    method: "POST",
    body: JSON.stringify({ username, password }),
  });
  state.token = data.access_token;
  state.user = data.user;
  localStorage.setItem("ff_token", state.token);
  await bootstrapApp();
}

function logout() {
  state.token = null;
  state.user = null;
  state.backup = null;
  localStorage.removeItem("ff_token");
  $("#backup-banner").innerHTML = "";
  $("#main-view").classList.add("d-none");
  $("#auth-view").classList.remove("d-none");
}

async function bootstrapApp() {
  try {
    state.user = await api("/api/auth/me");
  } catch (e) {
    logout();
    return;
  }
  $("#auth-view").classList.add("d-none");
  $("#main-view").classList.remove("d-none");
  $("#current-user").textContent = `${state.user.username} (${state.user.role})`;
  $("#nav-admin").style.display = canAdmin() ? "" : "none";

  state.categories = await api("/api/categories");
  fillCategorySelectors();
  showPage("dashboard");
  if (canAdmin()) loadBackupStatus();
}

function fillCategorySelectors() {
  const opts = state.categories.map(c => `<option value="${c.id}">${escapeHtml(c.name)}</option>`).join("");
  $("#trx-category-input").innerHTML = opts;
  $("#trx-category").innerHTML = `<option value="">All</option>` + opts;
  $("#rep-categories").innerHTML = opts;
}

// ---------- navigation ----------
function showPage(page) {
  state.currentPage = page;
  $$(".page").forEach(el => el.classList.add("d-none"));
  const el = document.getElementById("page-" + page);
  if (el) el.classList.remove("d-none");
  $$(".navbar-nav .nav-link").forEach(a => a.classList.toggle("active", a.dataset.page === page));
  if (page === "dashboard") loadDashboard();
  if (page === "transactions") loadTransactions();
  if (page === "savings") loadSavings();
  if (page === "statements") loadStatements();
  if (page === "reports") runReport();
  if (page === "admin") { loadUsers(); loadBackupStatus(); }
}

function kpiCard(c) {
  return `
    <div class="col-6 col-md-4 col-xl">
      <div class="card kpi-card ${c.cls}"><div class="card-body">
        <div class="d-flex justify-content-between align-items-start">
          <div><div class="text-muted small">${c.label}</div><div class="kpi-value">${c.value}</div>
            ${c.sub ? `<div class="text-muted small">${c.sub}</div>` : ""}</div>
          <i class="bi ${c.icon} fs-4 text-muted"></i>
        </div>
      </div></div>
    </div>`;
}

function isoDate(d = new Date()) {
  const off = d.getTimezoneOffset();
  return new Date(d.getTime() - off * 60000).toISOString().slice(0, 10);
}

function monthLabel(month) {
  const [y, m] = String(month).split("-");
  const names = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
  return `${names[Number(m) - 1] || m} ${y}`;
}

// ---------- dashboard ----------
async function loadDashboard() {
  const data = await api("/api/dashboard");
  const cards = [
    { label: "Total income", value: fmtMoney(data.total_income), cls: "income", icon: "bi-arrow-down-circle" },
    { label: "Total expenses", value: fmtMoney(data.total_expenses), cls: "expense", icon: "bi-arrow-up-circle" },
    { label: "Savings balance", value: fmtMoney(data.total_savings), cls: "savings", icon: "bi-piggy-bank",
      sub: `this month ${fmtMoney(data.savings_this_month || 0)}` },
    { label: "Cash balance", value: fmtMoney(data.cash_balance || 0), cls: "balance", icon: "bi-wallet2" },
    { label: "Transactions", value: data.transaction_count, cls: "balance", icon: "bi-list-check" },
  ];
  $("#kpi-cards").innerHTML = cards.map(kpiCard).join("");

  const labels = data.trend.map(t => t.month);
  if (state.trendChart) state.trendChart.destroy();
  state.trendChart = new Chart($("#trend-chart"), {
    type: "line",
    data: {
      labels,
      datasets: [
        { label: "Income", data: data.trend.map(t => t.income), borderColor: "#198754", tension: .3 },
        { label: "Expenses", data: data.trend.map(t => t.expenses), borderColor: "#dc3545", tension: .3 },
      ],
    },
    options: { responsive: true, plugins: { legend: { position: "bottom" } } },
  });

  if (state.totalsChart) state.totalsChart.destroy();
  state.totalsChart = new Chart($("#totals-chart"), {
    type: "doughnut",
    data: {
      labels: ["Income", "Expenses", "Savings"],
      datasets: [{ data: [data.total_income, data.total_expenses, data.total_savings],
        backgroundColor: ["#198754", "#dc3545", "#0d6efd"] }],
    },
    options: { plugins: { legend: { position: "bottom" } } },
  });

  // annual summary by calendar year: income, expenses, surplus, savings
  const annual = data.annual || [];
  if (state.annualChart) state.annualChart.destroy();
  state.annualChart = new Chart($("#annual-chart"), {
    type: "bar",
    data: {
      labels: annual.map(a => String(a.year)),
      datasets: [
        { label: "Income",  data: annual.map(a => a.income),   backgroundColor: "#198754" },
        { label: "Expenses", data: annual.map(a => a.expenses), backgroundColor: "#dc3545" },
        { label: "Surplus",  data: annual.map(a => a.surplus),  backgroundColor: "#0d6efd" },
        { label: "Savings",  data: annual.map(a => a.savings),  backgroundColor: "#ffc107" },
      ],
    },
    options: {
      responsive: true,
      plugins: { legend: { position: "bottom" } },
      scales: { y: { beginAtZero: true, ticks: { callback: v => fmtMoney(v) } } },
    },
  });
}

// ---------- transactions ----------
async function loadTransactions() {
  const params = new URLSearchParams();
  const from = $("#trx-from").value; if (from) params.append("start_date", from);
  const to = $("#trx-to").value; if (to) params.append("end_date", to);
  const cat = $("#trx-category").value; if (cat) params.append("category_ids", cat);
  const type = $("#trx-type").value; if (type) params.append("types", type);
  params.append("limit", "2000");

  const trx = await api("/api/transactions?" + params.toString());
  const tbody = $("#trx-table tbody");
  tbody.innerHTML = trx.map(t => `
    <tr>
      <td>${t.date}</td>
      <td>${escapeHtml(t.type)}</td>
      <td>${t.category ? escapeHtml(t.category.name) : ""}</td>
      <td class="text-end ${t.type === "Income" ? "text-success" : "text-danger"}">${fmtMoney(t.amount)}</td>
      <td>${escapeHtml(t.description || "")}</td>
      <td class="text-end">
        ${canEdit() ? `<button class="btn btn-sm btn-outline-secondary" onclick="editTransaction(${t.id})"><i class="bi bi-pencil"></i></button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteTransaction(${t.id})"><i class="bi bi-trash"></i></button>` : ""}
      </td>
    </tr>`).join("");
}

function syncTrxSavingsUI() {
  const isSaving = $("#trx-type-input").value === "Savings";
  $("#trx-amount-label").textContent = isSaving ? "Saving amount" : "Amount";
  $("#trx-amount-hint").classList.toggle("d-none", !isSaving);
  $("#trx-member-wrap").classList.toggle("d-none", !isSaving);
  if (isSaving) ensureMembers().then(fillMemberSelects);
}

function openAddTransaction() {
  $("#trxModalTitle").textContent = "Add transaction";
  $("#trx-id").value = "";
  $("#trx-date").value = new Date().toISOString().slice(0, 10);
  $("#trx-type-input").value = "Expenses";
  $("#trx-amount").value = "";
  $("#trx-description").value = "";
  syncTrxSavingsUI();
  new bootstrap.Modal("#trxModal").show();
}

async function editTransaction(id) {
  const trx = await api("/api/transactions?limit=100000");
  const t = trx.find(x => x.id === id);
  if (!t) return;
  $("#trxModalTitle").textContent = "Edit transaction";
  $("#trx-id").value = t.id;
  $("#trx-date").value = t.date;
  $("#trx-type-input").value = t.type;
  $("#trx-category-input").value = t.category_id;
  $("#trx-amount").value = t.amount;
  $("#trx-description").value = t.description || "";
  syncTrxSavingsUI();
  if (t.member_id) { await ensureMembers(); fillMemberSelects(); $("#trx-member").value = String(t.member_id); }
  new bootstrap.Modal("#trxModal").show();
}

async function deleteTransaction(id) {
  if (!confirm("Delete this transaction?")) return;
  await api("/api/transactions/" + id, { method: "DELETE" });
  showAlert("Transaction deleted");
  loadTransactions();
}

async function saveTransaction(e) {
  e.preventDefault();
  const id = $("#trx-id").value;
  const type = $("#trx-type-input").value;
  const payload = {
    date: $("#trx-date").value,
    type: type,
    category_id: Number($("#trx-category-input").value),
    amount: Number($("#trx-amount").value),
    description: $("#trx-description").value,
  };
  if (type === "Savings" && $("#trx-member").value) payload.member_id = Number($("#trx-member").value);
  if (id) {
    await api("/api/transactions/" + id, { method: "PUT", body: JSON.stringify(payload) });
  } else {
    await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
  }
  bootstrap.Modal.getInstance($("#trxModal")).hide();
  showAlert(type === "Savings" ? "Saving saved" : "Transaction saved");
  loadTransactions();
}

// ---------- reports ----------
async function runReport() {
  const payload = {
    start_date: $("#rep-from").value || null,
    end_date: $("#rep-to").value || null,
    category_ids: Array.from($("#rep-categories").selectedOptions).map(o => Number(o.value)),
    types: null,
    group_by: $("#rep-group").value,
  };
  const data = await api("/api/reports/summary", { method: "POST", body: JSON.stringify(payload) });
  const rows = data.rows;
  $("#report-table tbody").innerHTML = rows.map(r => `
    <tr><td>${escapeHtml(r.key)}</td><td class="text-end text-success">${fmtMoney(r.income)}</td>
    <td class="text-end text-danger">${fmtMoney(r.expenses)}</td>
    <td class="text-end text-primary">${fmtMoney(r.savings)}</td>
    <td class="text-end">${fmtMoney(r.net)}</td></tr>
  `).join("") + `<tr class="fw-bold"><td>${escapeHtml(data.totals.key)}</td>
    <td class="text-end">${fmtMoney(data.totals.income)}</td>
    <td class="text-end">${fmtMoney(data.totals.expenses)}</td>
    <td class="text-end">${fmtMoney(data.totals.savings)}</td>
    <td class="text-end">${fmtMoney(data.totals.net)}</td></tr>`;

  const labels = rows.map(r => r.key);
  if (state.reportChart) state.reportChart.destroy();
  state.reportChart = new Chart($("#report-chart"), {
    type: "bar",
    data: {
      labels,
      datasets: [
        { label: "Income", data: rows.map(r => r.income), backgroundColor: "#198754" },
        { label: "Expenses", data: rows.map(r => r.expenses), backgroundColor: "#dc3545" },
        { label: "Savings", data: rows.map(r => r.savings), backgroundColor: "#0d6efd" },
      ],
    },
    options: { plugins: { legend: { position: "bottom" } }, scales: { x: { stacked: false } } },
  });
}

// ---------- admin: users ----------
async function loadUsers() {
  const users = await api("/api/users");
  $("#users-table tbody").innerHTML = users.map(u => `
    <tr>
      <td>${u.id}</td><td>${escapeHtml(u.username)}</td><td>${escapeHtml(u.email)}</td>
      <td>
        <select class="form-select form-select-sm" onchange="changeRole(${u.id}, this.value)" ${isMaster() ? "" : "disabled"}>
          ${["master_admin", "admin", "editor", "viewer", "downloader"].map(r =>
            `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
        </select>
      </td>
      <td><input type="checkbox" ${u.is_approved ? "checked" : ""} onchange="changeField(${u.id}, 'is_approved', this.checked)"></td>
      <td><input type="checkbox" ${u.is_active ? "checked" : ""} onchange="changeField(${u.id}, 'is_active', this.checked)"></td>
      <td class="text-nowrap">
        <button class="btn btn-sm btn-outline-primary" onclick="enableUser(${u.id})">Enable</button>
        <button class="btn btn-sm btn-outline-secondary" onclick="openPasswordReset(${u.id}, '${escapeHtml(u.username)}')">
          <i class="bi bi-key"></i> Password
        </button>
        ${isMaster() && u.role !== "master_admin" && u.id !== state.user.id ? `
          <button class="btn btn-sm btn-outline-danger" onclick="deleteUser(${u.id}, '${escapeHtml(u.username)}')">
            <i class="bi bi-trash"></i>
          </button>` : ""}
      </td>
    </tr>`).join("");
}

async function deleteUser(id, username) {
  if (!confirm(`Delete the account "${username}"? The person will no longer be able to sign in. This cannot be undone.`)) return;
  try {
    await api("/api/users/" + id, { method: "DELETE" });
    showAlert("Account deleted");
    loadUsers();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function changeRole(id, role) {
  await api("/api/users/" + id, { method: "PATCH", body: JSON.stringify({ role }) });
  showAlert("Role updated");
}
async function changeField(id, field, value) {
  await api("/api/users/" + id, { method: "PATCH", body: JSON.stringify({ [field]: value }) });
  showAlert("User updated");
}
async function enableUser(id) {
  await api("/api/users/" + id, { method: "PATCH", body: JSON.stringify({ is_active: true, is_approved: true }) });
  showAlert("User enabled");
  loadUsers();
}

function openAddUser() {
  $("#user-form").reset();
  $("#new-approved").checked = true;
  $("#new-active").checked = true;
  $("#new-password").value = randomPassword();
  $("#new-role").value = "viewer";
  const adminOption = $("#new-role-admin");
  if (adminOption) adminOption.style.display = isMaster() ? "" : "none";
  new bootstrap.Modal("#userModal").show();
}

function randomPassword() {
  const chars = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789";
  const bytes = new Uint32Array(10);
  crypto.getRandomValues(bytes);
  return Array.from(bytes, b => chars[b % chars.length]).join("");
}

async function saveUser(e) {
  e.preventDefault();
  const payload = {
    username: $("#new-username").value.trim(),
    email: $("#new-email").value.trim(),
    full_name: $("#new-fullname").value.trim(),
    password: $("#new-password").value,
    role: $("#new-role").value,
    is_approved: $("#new-approved").checked,
    is_active: $("#new-active").checked,
  };
  try {
    const user = await api("/api/users", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#userModal")).hide();
    showAlert(`Account created for <strong>${escapeHtml(user.username)}</strong> (${escapeHtml(user.role)}).
      Sign-in details: username <code>${escapeHtml(user.username)}</code> / password
      <code>${escapeHtml(payload.password)}</code> — share it with them privately.`);
    loadUsers();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

function openPasswordReset(id, username) {
  $("#pwd-user-id").value = id;
  $("#pwd-username").textContent = username;
  $("#pwd-new").value = randomPassword();
  new bootstrap.Modal("#pwdModal").show();
}

async function savePassword(e) {
  e.preventDefault();
  const id = $("#pwd-user-id").value;
  const pwd = $("#pwd-new").value;
  try {
    const user = await api(`/api/users/${id}/password`, {
      method: "POST",
      body: JSON.stringify({ new_password: pwd }),
    });
    bootstrap.Modal.getInstance($("#pwdModal")).hide();
    showAlert(`New password for <strong>${escapeHtml(user.username)}</strong>:
      <code>${escapeHtml(pwd)}</code> — share it with them privately.`);
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

// ---------- admin: backup & restore ----------
async function loadBackupStatus() {
  if (!canAdmin()) return;
  try {
    state.backup = await api("/api/admin/backup-status");
  } catch (e) {
    return;
  }
  renderBackupBanner(state.backup);
  renderBackupStatus(state.backup);
}

function renderBackupStatus(s) {
  const el = $("#backup-status-text");
  if (el) {
    el.textContent = s.last_backup_at
      ? `Last backup: ${s.days_since_last_backup} day(s) ago · every ${s.interval_days} days`
      : `No backup downloaded yet · every ${s.interval_days} days`;
  }
  const hist = $("#backup-history");
  if (hist && s.history && s.history.length) {
    hist.innerHTML = "<strong>Recent activity</strong><br>" + s.history.slice(0, 6).map(h => {
      const label = { export_excel: "Excel backup downloaded", export_csv: "CSV backup downloaded", import: "Data restored from file" }[h.kind] || h.kind;
      return `${escapeHtml(h.created_at.replace("T", " ").slice(0, 16))} — ${escapeHtml(label)}
        ${h.username ? "by " + escapeHtml(h.username) : ""}${h.row_count != null ? ` (${h.row_count} rows)` : ""}`;
    }).join("<br>");
  }
}

function renderBackupBanner(s) {
  const el = $("#backup-banner");
  if (!s) { el.innerHTML = ""; return; }
  let html = "";

  if (s.persistent_storage === false) {
    html += `<div class="alert alert-danger">
      <strong><i class="bi bi-exclamation-octagon"></i> Storage warning — data is not saved permanently.</strong>
      <div class="small mt-1">${escapeHtml(s.storage_note || "")}</div>
    </div>`;
  }

  if (s.due) {
    const detail = s.last_backup_at
      ? `The last backup was <strong>${s.days_since_last_backup} day(s)</strong> ago (reminder every ${s.interval_days} days).`
      : `No backup has been downloaded yet.`;
    html += `<div class="alert alert-warning d-flex flex-wrap align-items-center gap-2">
      <i class="bi bi-shield-exclamation fs-5"></i>
      <div class="flex-grow-1">
        <strong>Weekly backup reminder.</strong> ${detail}
        Download the Excel backup and keep it somewhere safe — it can be used to restore all data.
      </div>
      <button class="btn btn-sm btn-warning" onclick="downloadBackupNow()">
        <i class="bi bi-file-earmark-excel"></i> Download Excel backup
      </button>
    </div>`;
  }
  el.innerHTML = html;
}

async function downloadFile(path, filename) {
  const res = await fetch(API + path, { headers: { Authorization: "Bearer " + state.token } });
  if (!res.ok) { showAlert("Download failed", "danger"); return false; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
  return true;
}

async function downloadBackupNow() {
  const ok = await downloadFile("/api/export/excel", backupFileName("xlsx"));
  if (ok) {
    showAlert("Backup downloaded — your reminder is reset for another week.");
    loadBackupStatus();
  }
}

function updateImportButtonState() {
  const file = $("#import-file").files[0];
  $("#import-btn").disabled = !(file && $("#import-confirm").checked);
}

async function runImport() {
  const file = $("#import-file").files[0];
  const box = $("#import-result");
  if (!file) return;
  if (!$("#import-confirm").checked) {
    showAlert("Please tick the confirmation box first.", "warning");
    return;
  }
  const btn = $("#import-btn");
  btn.disabled = true;

  // Safety net: keep a copy of what is in the system right now.
  box.innerHTML = `<div class="text-muted small">Step 1/2 — saving a copy of the current data…</div>`;
  await downloadFile("/api/export/excel", "before_restore_" + backupFileName("xlsx"));

  box.innerHTML = `<div class="text-muted small">Step 2/2 — uploading <strong>${escapeHtml(file.name)}</strong> and replacing all data…</div>`;
  try {
    const form = new FormData();
    form.append("file", file);
    form.append("confirm", "REPLACE_ALL");
    const out = await api("/api/admin/import", { method: "POST", body: form });
    const warn = out.warnings && out.warnings.length
      ? `<div class="mt-2">${out.warnings.map(w => escapeHtml(w)).join("<br>")}</div>` : "";
    const skipped = out.skipped_rows && out.skipped_rows.length
      ? `<div class="mt-2 small">${out.skipped_rows.slice(0, 10).map(s => `row ${s.row}: ${escapeHtml(s.error)}`).join("<br>")}</div>` : "";
    box.innerHTML = `<div class="alert alert-success mb-0">
      <strong>Restore complete.</strong><br>
      ${out.imported} transaction(s) imported from sheet “${escapeHtml(out.sheet)}”<br>
      ${out.deleted} old transaction(s) removed ·
      ${out.categories_created} category(ies) created ·
      ${out.categories_updated} updated
      ${warn}${skipped}
    </div>`;
    showAlert("Data replaced from the backup file.");
    state.categories = await api("/api/categories");
    fillCategorySelectors();
    await loadBackupStatus();
    loadUsers();
  } catch (err) {
    box.innerHTML = `<div class="alert alert-danger mb-0">
      <strong>Nothing was changed.</strong><br>${escapeHtml(err.message)}</div>`;
  } finally {
    btn.disabled = false;
    updateImportButtonState();
  }
}

// ---------- savings ----------
async function ensureMembers() {
  if (state.members && state.members.length) return state.members;
  try {
    const s = await api("/api/savings/summary?months=1");
    state.members = s.members || [];
  } catch (err) {
    state.members = state.user ? [{ id: state.user.id, name: state.user.full_name || state.user.username }] : [];
  }
  return state.members;
}

function fillMemberSelects(members) {
  const opts = members.map(m => `<option value="${m.id}">${escapeHtml(m.name)}</option>`).join("");
  ["#sv-member", "#trx-member"].forEach(sel => {
    const el = $(sel);
    if (!el) return;
    el.innerHTML = opts;
    if (state.user && members.some(m => m.id === state.user.id)) el.value = String(state.user.id);
  });
}

async function loadSavings() {
  const data = await api("/api/savings/summary?months=60");
  state.savings = data;
  const members = data.members || [];
  state.members = members;
  fillMemberSelects(members);

  const t = data.totals || {};
  const closed = data.months.filter(m => m.closed);
  const sweepNet = (t.sweep_in || 0) - (t.sweep_out || 0);
  $("#savings-kpis").innerHTML = [
    { label: "Savings balance", value: fmtMoney(t.closing_balance),
      sub: `opening ${fmtMoney(t.opening_balance)}`, cls: "savings", icon: "bi-piggy-bank" },
    { label: "Transferred in", value: fmtMoney(t.savings_in),
      sub: t.savings_in_loan ? `of which loan-funded ${fmtMoney(t.savings_in_loan)}` : "",
      cls: "income", icon: "bi-arrow-down-circle" },
    { label: "Withdrawn", value: fmtMoney(t.withdrawal), cls: "expense", icon: "bi-arrow-up-circle" },
    { label: "Posted by the sweep", value: fmtMoney(sweepNet),
      sub: `${closed.length} closed months`, cls: "balance", icon: "bi-arrow-repeat" },
  ].map(kpiCard).join("");

  $("#run-offsets-btn").style.display = canAdmin() ? "" : "none";
  $("#add-saving-btn").style.display = canEdit() ? "" : "none";
  $("#add-transfer-btn-2").style.display = canEdit() ? "" : "none";

  const badge = m => m.closed
    ? (m.swept
      ? '<span class="badge bg-success-subtle text-success-emphasis">swept</span>'
      : '<span class="badge bg-secondary-subtle text-secondary-emphasis">no sweep</span>')
    : '<span class="badge bg-primary-subtle text-primary-emphasis">running</span>';

  $("#savings-table tbody").innerHTML = [...data.months].reverse().map(m => {
    const sweep = (m.sweep_in || 0) - (m.sweep_out || 0);
    return `
    <tr>
      <td class="text-nowrap">${monthLabel(m.month)} ${badge(m)}</td>
      <td class="text-end">${fmtMoney(m.income)}</td>
      <td class="text-end">${fmtMoney(m.expenses)}</td>
      <td class="text-end ${m.surplus < 0 ? "text-danger" : ""}">${fmtMoney(m.surplus)}</td>
      <td class="text-end">${fmtMoney(m.savings_in)}
        ${m.savings_in_loan ? `<div class="text-muted small">incl. loan ${fmtMoney(m.savings_in_loan)}</div>` : ""}</td>
      <td class="text-end ${m.withdrawal ? "text-danger" : ""}">${m.withdrawal ? fmtMoney(m.withdrawal) : "—"}</td>
      <td class="text-end text-muted">${sweep ? fmtMoney(sweep) : "—"}</td>
      <td class="text-end fw-semibold ${m.savings_net < 0 ? "text-danger" : ""}">${fmtMoney(m.savings_net)}</td>
      <td class="text-end">${fmtMoney(m.balance)}</td>
      <td class="text-end text-nowrap">
        ${canEdit() ? `<button class="btn btn-sm btn-outline-primary" onclick="openSaving('${m.month}')">Add</button>` : ""}
      </td>
    </tr>`;
  }).join("");

  $("#savings-members-table thead").innerHTML =
    `<tr><th>Month</th>${members.map(m => `<th class="text-end">${escapeHtml(m.name)}</th>`).join("")}<th class="text-end">Total</th></tr>`;
  const rows = [...data.months].reverse().map(m => {
    const cells = members.map(mm => `<td class="text-end">${fmtMoney((m.members || {})[String(mm.id)] || 0)}</td>`).join("");
    const total = members.reduce((sum, mm) => sum + ((m.members || {})[String(mm.id)] || 0), 0);
    return `<tr><td class="text-nowrap">${monthLabel(m.month)}</td>${cells}<td class="text-end fw-semibold">${fmtMoney(total)}</td></tr>`;
  });
  const totalsRow = `<tr class="table-light fw-semibold"><td>Total</td>` +
    members.map(mm => {
      const sum = data.months.reduce((s, m) => s + ((m.members || {})[String(mm.id)] || 0), 0);
      return `<td class="text-end">${fmtMoney(sum)}</td>`;
    }).join("") +
    `<td class="text-end">${fmtMoney((t.savings_in || 0) - (t.savings_in_loan || 0))}</td></tr>`;
  $("#savings-members-table tbody").innerHTML = rows.join("") + totalsRow;
}

function openSaving(month) {
  if (!canEdit()) return;
  const dateInput = $("#sv-date");
  if (month) {
    const [y, m] = month.split("-");
    const last = new Date(Number(y), Number(m), 0);
    dateInput.value = isoDate(last);
  } else {
    dateInput.value = isoDate();
  }
  $("#sv-amount").value = "";
  $("#sv-note").value = "";
  ensureMembers().then(fillMemberSelects);
  new bootstrap.Modal("#savingModal").show();
}

async function saveSaving(e) {
  e.preventDefault();
  const payload = {
    date: $("#sv-date").value,
    amount: Number($("#sv-amount").value),
    note: $("#sv-note").value || null,
  };
  if ($("#sv-member").value) payload.member_id = Number($("#sv-member").value);
  try {
    await api("/api/savings/entries", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#savingModal")).hide();
    showAlert("Saving recorded — the month-end sweep has been recalculated.");
    if (state.currentPage === "savings") loadSavings();
    if (state.currentPage === "dashboard") loadDashboard();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function runOffsets() {
  const btn = $("#run-offsets-btn");
  btn.disabled = true;
  try {
    const out = await api("/api/admin/offsets/run", { method: "POST" });
    showAlert(`Month-end sweep: ${out.created} created, ${out.updated} updated, ${out.removed} removed.`);
    await loadSavings();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  } finally {
    btn.disabled = false;
  }
}

// ---------- financial statements ----------
function showStatementTab(tab) {
  ["income", "balance", "accounts", "recon"].forEach(t => {
    const el = $("#stmt-" + t);
    if (el) el.classList.toggle("d-none", t !== tab);
  });
  $$("#statement-tabs .nav-link").forEach(a => a.classList.toggle("active", a.dataset.stmt === tab));
  if (tab === "balance") loadBalanceSheet();
  if (tab === "accounts") loadAccountsBook();
  if (tab === "recon") loadReconciliation();
}

async function loadStatements() {
  if (!$("#bs-asof").value) $("#bs-asof").value = isoDate();
  if (!$("#stmt-from").value && !$("#stmt-to").value) setQuickRange("year", false);
  await Promise.all([loadIncomeStatement(), loadBalanceSheet(), loadAccountsBook(), loadReconciliation()]);
}

function setQuickRange(range, reload = true) {
  const now = new Date();
  const first = new Date(now.getFullYear(), now.getMonth(), 1);
  const lastPrev = new Date(now.getFullYear(), now.getMonth(), 0);
  const firstPrev = new Date(lastPrev.getFullYear(), lastPrev.getMonth(), 1);
  if (range === "month") { $("#stmt-from").value = isoDate(first); $("#stmt-to").value = isoDate(now); }
  if (range === "lastmonth") { $("#stmt-from").value = isoDate(firstPrev); $("#stmt-to").value = isoDate(lastPrev); }
  if (range === "year") { $("#stmt-from").value = isoDate(new Date(now.getFullYear(), 0, 1)); $("#stmt-to").value = isoDate(now); }
  if (range === "all") { $("#stmt-from").value = ""; $("#stmt-to").value = ""; }
  if (reload) loadIncomeStatement();
}

function statementRow(label, amount, opts = {}) {
  const cls = opts.cls || "";
  const indent = opts.indent ? ' class="ps-4"' : "";
  return `<tr class="${cls}"><td${indent}>${escapeHtml(label)}</td><td class="text-end">${fmtMoney(amount)}</td></tr>`;
}

async function loadIncomeStatement() {
  const params = new URLSearchParams();
  if ($("#stmt-from").value) params.append("start", $("#stmt-from").value);
  if ($("#stmt-to").value) params.append("end", $("#stmt-to").value);
  const rep = await api("/api/statements/income?" + params.toString());
  $("#stmt-period").textContent = (rep.start || rep.end)
    ? `Period: ${rep.start || "beginning"} → ${rep.end || "today"}`
    : "All recorded months";

  const byGroup = {};
  rep.expense_lines.forEach(l => { (byGroup[l.group || "Uncategorised"] = byGroup[l.group || "Uncategorised"] || []).push(l); });

  let html = '<table class="table table-sm mb-0"><tbody>';
  html += '<tr class="table-light fw-semibold"><td>INCOME</td><td></td></tr>';
  html += rep.income_lines.map(l => statementRow(l.category, l.amount, { indent: true })).join("") ||
    '<tr><td class="ps-4 text-muted">none</td><td></td></tr>';
  html += statementRow("Total income", rep.income_total, { cls: "fw-semibold border-top" });

  html += '<tr class="table-light fw-semibold"><td>EXPENSES</td><td></td></tr>';
  Object.entries(byGroup).forEach(([group, lines]) => {
    const subtotal = (rep.expense_groups.find(g => g.group === group) || {}).amount ||
      lines.reduce((s, l) => s + l.amount, 0);
    html += statementRow(group, subtotal, { cls: "fw-semibold ps-3" });
    html += lines.map(l => statementRow(l.category, l.amount, { indent: true })).join("");
  });
  if (!rep.expense_lines.length) html += '<tr><td class="ps-4 text-muted">none</td><td></td></tr>';
  html += statementRow("Total expenses", rep.expense_total, { cls: "fw-semibold border-top" });

  html += statementRow("Surplus (income − expenses)", rep.surplus, { cls: "fw-bold" });
  const fin = rep.financing || {};
  html += '<tr class="table-light fw-semibold"><td>BELOW THE LINE — movements, not income</td><td></td></tr>';
  html += statementRow("Transferred into savings (from income)", fin.savings_in || 0, { indent: true });
  if (fin.savings_in_funded_by_loan) {
    html += statementRow("Transferred into savings from the loan", fin.savings_in_funded_by_loan,
      { indent: true, cls: "text-muted" });
  }
  html += statementRow("Withdrawn from savings", fin.withdrawals || 0, { indent: true });
  html += statementRow("Net movement in savings (from income)", fin.savings_net || 0, { cls: "fw-semibold" });
  html += statementRow("Borrowed", fin.loan_borrowed || 0, { indent: true });
  html += statementRow("Repaid", fin.loan_repaid || 0, { indent: true });
  html += statementRow("Net worth change (only income and expenses move it)", rep.surplus, { cls: "fw-bold border-top" });
  html += "</tbody></table>";
  $("#income-statement").innerHTML = html;

  $("#stmt-months-table tbody").innerHTML = [...rep.months].reverse().map(m => `
    <tr>
      <td class="text-nowrap">${monthLabel(m.month)}</td>
      <td class="text-end">${fmtMoney(m.income)}</td>
      <td class="text-end">${fmtMoney(m.expenses)}</td>
      <td class="text-end ${m.surplus < 0 ? "text-danger" : ""}">${fmtMoney(m.surplus)}</td>
      <td class="text-end">${fmtMoney(m.savings)}</td>
    </tr>`).join("");
}

async function loadBalanceSheet() {
  const asof = $("#bs-asof").value;
  const bs = await api("/api/statements/balance-sheet" + (asof ? "?as_of=" + asof : ""));
  $("#bs-note").textContent = bs.as_of ? `As of ${bs.as_of}` : "All recorded data";

  let html = '<div class="row g-3"><div class="col-lg-6">';
  html += '<div class="card mb-3"><div class="card-body"><h6>ASSETS</h6><table class="table table-sm mb-0"><tbody>';
  bs.accounts.forEach(a => {
    const label = `${a.name}${a.opening_date ? ` (from ${a.opening_date})` : ""}`;
    html += statementRow(label, a.balance, { cls: "fw-semibold" });
    html += statementRow(`opening ${fmtMoney(a.opening_balance)} + in ${fmtMoney(a.movements_in)} − out ${fmtMoney(a.movements_out)}`,
      a.balance, { indent: true, cls: "text-muted" });
  });
  html += statementRow("Money in accounts", bs.money_total, { cls: "fw-semibold border-top" });

  const byKind = {};
  bs.asset_items.forEach(i => { (byKind[i.kind] = byKind[i.kind] || []).push(i); });
  Object.entries(byKind).forEach(([kind, items]) => {
    html += statementRow(kind, items.reduce((s, i) => s + i.value, 0), { cls: "fw-semibold ps-3 text-capitalize" });
    html += items.map(i => statementRow(i.name + (i.acquired_on ? ` (${i.acquired_on})` : ""), i.value, { indent: true })).join("");
  });
  if (!bs.asset_items.length) {
    html += '<tr><td class="ps-4 text-muted">no property, vehicles or investments recorded yet</td><td></td></tr>';
  }
  html += statementRow("TOTAL ASSETS", bs.total_assets, { cls: "fw-bold table-light fs-6" });
  html += "</tbody></table></div></div></div>";

  html += '<div class="col-lg-6">';
  html += '<div class="card mb-3"><div class="card-body"><h6>LIABILITIES</h6><table class="table table-sm mb-0"><tbody>';
  if (bs.loans.length) {
    bs.loans.forEach(l => {
      html += statementRow(`${l.lender} (borrowed ${fmtMoney(l.borrowed)} − repaid ${fmtMoney(l.repaid)})`,
        l.outstanding, { indent: true });
    });
  }
  if (bs.liability_items.length) {
    bs.liability_items.forEach(i => {
      const extra = i.interest_rate ? ` — ${i.interest_rate}% p.a.` : "";
      html += statementRow(i.name + extra, i.outstanding, { indent: true });
    });
  }
  if (!bs.loans.length && !bs.liability_items.length) {
    html += '<tr><td class="ps-4 text-muted">nothing owed / nothing recorded</td><td></td></tr>';
  }
  html += statementRow("TOTAL LIABILITIES", bs.liabilities_total, { cls: "fw-bold table-light fs-6" });
  html += "</tbody></table></div></div>";

  const check = bs.reconciliation || {};
  const ok = Math.abs(check.difference || 0) < 1;
  html += `<div class="card"><div class="card-body">
      <div class="d-flex justify-content-between align-items-center">
        <div><h6 class="mb-0">NET WORTH</h6>
          <div class="text-muted small">Assets ${fmtMoney(bs.total_assets)} − liabilities ${fmtMoney(bs.liabilities_total)}</div></div>
        <span class="fs-4 fw-bold ${bs.net_worth < 0 ? "text-danger" : "text-success"}">${fmtMoney(bs.net_worth)}</span>
      </div>
      <hr />
      <div class="small ${ok ? "text-success" : "text-danger"}">
        <i class="bi ${ok ? "bi-check-circle" : "bi-exclamation-triangle"}"></i>
        Opening balances ${fmtMoney(check.opening_balances)} + lifetime surplus ${fmtMoney(check.lifetime_surplus)}
        = ${fmtMoney(check.expected_net_worth)} ${ok ? "— matches the balance sheet" : "— does NOT match " + fmtMoney(check.actual_net_worth)}
      </div>
      <div class="text-muted small mt-1">From the records: income ${fmtMoney(bs.from_activity.income)},
        expenses ${fmtMoney(bs.from_activity.expenses)}, into savings ${fmtMoney(bs.from_activity.savings_in)},
        out of savings ${fmtMoney(bs.from_activity.withdrawals)},
        borrowed ${fmtMoney(bs.from_activity.loan_borrowed)}, repaid ${fmtMoney(bs.from_activity.loan_repaid)}.</div>
    </div></div></div></div>`;
  $("#balance-sheet").innerHTML = html;
}

// ---------- accounts, loans, reconciliation ----------
async function loadAccountsBook() {
  const [accounts, loans, assets, liabilities] = await Promise.all([
    api("/api/accounts"), api("/api/loans"), api("/api/assets"), api("/api/liabilities"),
  ]);
  state.accounts = accounts;
  state.loans = loans;
  state.assets = assets;
  state.liabilities = liabilities;
  const editable = canEdit();
  ["#add-account-btn", "#add-transfer-btn", "#add-loan-btn", "#add-asset-btn", "#add-liability-btn"]
    .forEach(sel => { const el = $(sel); if (el) el.style.display = editable ? "" : "none"; });

  $("#account-table tbody").innerHTML = accounts.map(a => `
    <tr>
      <td>${escapeHtml(a.name)}</td>
      <td class="text-capitalize">${escapeHtml(a.kind)}</td>
      <td class="text-end">${fmtMoney(a.opening_balance)}</td>
      <td class="text-muted small">${a.opening_date || "—"}</td>
      <td class="text-end">${fmtMoney(a.movements_in)}</td>
      <td class="text-end">${fmtMoney(a.movements_out)}</td>
      <td class="text-end fw-semibold">${fmtMoney(a.balance)}</td>
      <td class="text-end text-nowrap">${editable ? `
        <button class="btn btn-sm btn-outline-secondary" onclick="openAccount(${a.id})">Edit</button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteAccount(${a.id})">Delete</button>` : ""}</td>
    </tr>`).join("") || '<tr><td colspan="8" class="text-muted">No accounts yet.</td></tr>';

  $("#loan-table tbody").innerHTML = loans.map(l => `
    <tr>
      <td>${escapeHtml(l.lender)}</td>
      <td class="text-end">${fmtMoney(l.borrowed)}</td>
      <td class="text-end">${fmtMoney(l.repaid)}</td>
      <td class="text-end fw-semibold ${l.outstanding > 0 ? "text-danger" : ""}">${fmtMoney(l.outstanding)}</td>
    </tr>`).join("") ||
    '<tr><td colspan="4" class="text-muted">No loans recorded — use “Record borrow / repay”.</td></tr>';

  $("#asset-table tbody").innerHTML = assets.length ? assets.map(a => `
    <tr><td>${escapeHtml(a.name)}</td><td class="text-capitalize">${escapeHtml(a.kind)}</td>
      <td class="text-end">${fmtMoney(a.value)}</td>
      <td class="text-end text-nowrap">${editable ? `
        <button class="btn btn-sm btn-outline-secondary" onclick="openAsset(${a.id})">Edit</button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteAsset(${a.id})">Delete</button>` : ""}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="text-muted">No assets recorded yet.</td></tr>';

  $("#liability-table tbody").innerHTML = liabilities.length ? liabilities.map(l => `
    <tr><td>${escapeHtml(l.name)}</td><td class="text-capitalize">${escapeHtml(l.kind)}</td>
      <td class="text-end">${fmtMoney(l.outstanding)}</td>
      <td class="text-end text-nowrap">${editable ? `
        <button class="btn btn-sm btn-outline-secondary" onclick="openLiability(${l.id})">Edit</button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteLiability(${l.id})">Delete</button>` : ""}</td>
    </tr>`).join("") : '<tr><td colspan="4" class="text-muted">No liabilities recorded yet.</td></tr>';

  fillAccountSelects(accounts);
}

function fillAccountSelects(accounts) {
  const savings = (accounts || []).filter(a => a.kind === "savings");
  const opts = savings.map(a => `<option value="${a.id}">${escapeHtml(a.name)}</option>`).join("");
  ["#tr-account"].forEach(sel => { const el = $(sel); if (el) el.innerHTML = opts; });
}

async function loadReconciliation() {
  const rec = await api("/api/reconciliation");
  $("#recon-identity").textContent = rec.identity;
  const t = rec.totals;
  $("#recon-kpis").innerHTML = [
    { label: "Income", value: fmtMoney(t.income), cls: "income", icon: "bi-arrow-down-circle" },
    { label: "Expenses", value: fmtMoney(t.expenses), cls: "expense", icon: "bi-arrow-up-circle" },
    { label: "Surplus", value: fmtMoney(t.surplus), cls: "balance", icon: "bi-equals" },
    { label: "Months needing attention", value: String(t.months_with_issues),
      cls: t.months_with_issues ? "expense" : "income", icon: "bi-clipboard-check" },
  ].map(kpiCard).join("");

  $("#recon-issues").innerHTML = rec.issues.length
    ? `<div class="alert alert-warning mb-0"><strong>${rec.issues.length} month(s) need a look:</strong>
        <ul class="mb-0 mt-1">${rec.issues.slice(0, 12).map(i =>
          `<li>${monthLabel(i.month)} — ${escapeHtml(i.issue)}</li>`).join("")}</ul>
        ${rec.issues.length > 12 ? `<div class="small">…and ${rec.issues.length - 12} more</div>` : ""}</div>`
    : '<div class="alert alert-success mb-0">Every month reconciles: each surplus was moved to savings and every deficit has a recorded source.</div>';

  $("#recon-table tbody").innerHTML = [...rec.rows].reverse().map(r => `
    <tr>
      <td class="text-nowrap">${monthLabel(r.month)}</td>
      <td class="text-end">${fmtMoney(r.income)}</td>
      <td class="text-end">${fmtMoney(r.expenses)}</td>
      <td class="text-end ${r.surplus < 0 ? "text-danger" : ""}">${fmtMoney(r.surplus)}</td>
      <td class="text-end">${fmtMoney(r.savings_in)}</td>
      <td class="text-end">${r.withdrawal ? `<span class="text-danger">${fmtMoney(r.withdrawal)}</span>` : "—"}</td>
      <td class="text-end">${r.loan_borrow ? fmtMoney(r.loan_borrow) : "—"}</td>
      <td class="text-end">${r.loan_repay ? fmtMoney(r.loan_repay) : "—"}</td>
      <td class="text-end ${r.cash_balance < 0 ? "text-danger fw-semibold" : ""}">${fmtMoney(r.cash_balance)}</td>
      <td>${r.issue
        ? `<span class="badge bg-warning-subtle text-warning-emphasis" title="${escapeHtml(r.issue)}">check</span>`
        : '<span class="badge bg-success-subtle text-success-emphasis">ok</span>'}</td>
    </tr>`).join("");
}

// ---------- accounts, transfers, loans ----------
async function ensureAccounts() {
  if (state.accounts && state.accounts.length) return state.accounts;
  try { state.accounts = await api("/api/accounts"); } catch (err) { state.accounts = []; }
  return state.accounts;
}

function openAccount(id) {
  const a = (state.accounts || []).find(x => x.id === id);
  $("#accountModalTitle").textContent = a ? "Edit account" : "Add account";
  $("#account-id").value = a ? a.id : "";
  $("#account-name").value = a ? a.name : "";
  $("#account-kind").value = a ? a.kind : "savings";
  $("#account-opening").value = a ? a.opening_balance : "";
  $("#account-opening-date").value = a && a.opening_date ? a.opening_date : "";
  $("#account-note").value = a && a.note ? a.note : "";
  $("#account-active").checked = a ? a.is_active : true;
  new bootstrap.Modal("#accountModal").show();
}

async function saveAccount(e) {
  e.preventDefault();
  const id = $("#account-id").value;
  const payload = {
    name: $("#account-name").value,
    kind: $("#account-kind").value,
    opening_balance: Number($("#account-opening").value || 0),
    opening_date: $("#account-opening-date").value || null,
    note: $("#account-note").value || null,
    is_active: $("#account-active").checked,
  };
  try {
    if (id) await api("/api/accounts/" + id, { method: "PUT", body: JSON.stringify(payload) });
    else await api("/api/accounts", { method: "POST", body: JSON.stringify(payload) });
    state.accounts = null;
    bootstrap.Modal.getInstance($("#accountModal")).hide();
    showAlert("Account saved — the balance sheet and statements now use it.");
    await loadAccountsBook();
    loadBalanceSheet();
    loadDashboard();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function deleteAccount(id) {
  if (!confirm("Remove this account? Only possible when nothing uses it.")) return;
  try {
    await api("/api/accounts/" + id, { method: "DELETE" });
    state.accounts = null;
    showAlert("Account removed");
    await loadAccountsBook();
    loadBalanceSheet();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function openTransfer(defaultDirection) {
  if (!canEdit()) return;
  await ensureAccounts();
  fillAccountSelects(state.accounts);
  $("#tr-dir").value = defaultDirection || "in";
  toggleTransferFunding();
  $("#tr-date").value = isoDate();
  $("#tr-amount").value = "";
  $("#tr-note").value = "";
  new bootstrap.Modal("#transferModal").show();
}

function toggleTransferFunding() {
  $("#tr-funded-wrap").classList.toggle("d-none", $("#tr-dir").value !== "in");
}

async function saveTransfer(e) {
  e.preventDefault();
  const payload = {
    date: $("#tr-date").value,
    amount: Number($("#tr-amount").value),
    direction: $("#tr-dir").value,
    note: $("#tr-note").value || null,
  };
  if ($("#tr-account").value) payload.account_id = Number($("#tr-account").value);
  if (payload.direction === "in") payload.funded_by = $("#tr-funded").value;
  try {
    await api("/api/transfers", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#transferModal")).hide();
    showAlert("Transfer saved — the month-end sweep has been recalculated.");
    state.accounts = null;
    if (state.currentPage === "savings") loadSavings();
    if (state.currentPage === "statements") { loadAccountsBook(); loadBalanceSheet(); }
    if (state.currentPage === "dashboard") loadDashboard();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function openLoan() {
  if (!canEdit()) return;
  $("#ln-dir").value = "borrow";
  $("#ln-lender").value = (state.loans && state.loans[0]) ? state.loans[0].lender : "";
  $("#ln-date").value = isoDate();
  $("#ln-amount").value = "";
  $("#ln-note").value = "";
  new bootstrap.Modal("#loanModal").show();
}

async function saveLoan(e) {
  e.preventDefault();
  const payload = {
    date: $("#ln-date").value,
    amount: Number($("#ln-amount").value),
    direction: $("#ln-dir").value,
    lender: $("#ln-lender").value,
    note: $("#ln-note").value || null,
  };
  try {
    await api("/api/loans", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#loanModal")).hide();
    showAlert(payload.direction === "borrow" ? "Borrowing recorded — cash up, debt up."
                                             : "Repayment recorded — cash down, debt down.");
    await loadAccountsBook();
    loadBalanceSheet();
    loadDashboard();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

// ---------- assets & liabilities ----------
// the accounts tab shows accounts, loans, assets and liabilities together
async function loadItems() {
  return loadAccountsBook();
}

function openAsset(id) {
  const a = (state.assets || []).find(x => x.id === id);
  $("#assetModalTitle").textContent = a ? "Edit asset" : "Add asset";
  $("#asset-id").value = a ? a.id : "";
  $("#asset-name").value = a ? a.name : "";
  $("#asset-kind").value = a ? a.kind : "property";
  $("#asset-value").value = a ? a.value : "";
  $("#asset-acquired").value = a && a.acquired_on ? a.acquired_on : "";
  $("#asset-note").value = a && a.note ? a.note : "";
  $("#asset-active").checked = a ? a.is_active : true;
  new bootstrap.Modal("#assetModal").show();
}

async function saveAsset(e) {
  e.preventDefault();
  const id = $("#asset-id").value;
  const payload = {
    name: $("#asset-name").value,
    kind: $("#asset-kind").value,
    value: Number($("#asset-value").value),
    acquired_on: $("#asset-acquired").value || null,
    note: $("#asset-note").value || null,
    is_active: $("#asset-active").checked,
  };
  try {
    if (id) await api("/api/assets/" + id, { method: "PUT", body: JSON.stringify(payload) });
    else await api("/api/assets", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#assetModal")).hide();
    showAlert("Asset saved");
    loadItems();
    loadBalanceSheet();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function deleteAsset(id) {
  if (!confirm("Remove this asset from the balance sheet?")) return;
  await api("/api/assets/" + id, { method: "DELETE" });
  showAlert("Asset removed");
  loadItems();
  loadBalanceSheet();
}

function openLiability(id) {
  const l = (state.liabilities || []).find(x => x.id === id);
  $("#liabilityModalTitle").textContent = l ? "Edit liability" : "Add liability";
  $("#liability-id").value = l ? l.id : "";
  $("#liability-name").value = l ? l.name : "";
  $("#liability-kind").value = l ? l.kind : "mortgage";
  $("#liability-outstanding").value = l ? l.outstanding : "";
  $("#liability-payment").value = l && l.monthly_payment != null ? l.monthly_payment : "";
  $("#liability-rate").value = l && l.interest_rate != null ? l.interest_rate : "";
  $("#liability-started").value = l && l.started_on ? l.started_on : "";
  $("#liability-note").value = l && l.note ? l.note : "";
  $("#liability-active").checked = l ? l.is_active : true;
  new bootstrap.Modal("#liabilityModal").show();
}

async function saveLiability(e) {
  e.preventDefault();
  const id = $("#liability-id").value;
  const payload = {
    name: $("#liability-name").value,
    kind: $("#liability-kind").value,
    outstanding: Number($("#liability-outstanding").value),
    monthly_payment: $("#liability-payment").value ? Number($("#liability-payment").value) : null,
    interest_rate: $("#liability-rate").value ? Number($("#liability-rate").value) : null,
    started_on: $("#liability-started").value || null,
    note: $("#liability-note").value || null,
    is_active: $("#liability-active").checked,
  };
  try {
    if (id) await api("/api/liabilities/" + id, { method: "PUT", body: JSON.stringify(payload) });
    else await api("/api/liabilities", { method: "POST", body: JSON.stringify(payload) });
    bootstrap.Modal.getInstance($("#liabilityModal")).hide();
    showAlert("Liability saved");
    loadItems();
    loadBalanceSheet();
  } catch (err) {
    showAlert(escapeHtml(err.message), "danger");
  }
}

async function deleteLiability(id) {
  if (!confirm("Remove this liability from the balance sheet?")) return;
  await api("/api/liabilities/" + id, { method: "DELETE" });
  showAlert("Liability removed");
  loadItems();
  loadBalanceSheet();
}

// ---------- wire up ----------
document.addEventListener("DOMContentLoaded", () => {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await login($("#login-username").value, $("#login-password").value); }
    catch (err) { showAlert(escapeHtml(err.message), "danger"); }
  });

  $("#logout-btn").addEventListener("click", logout);
  $$(".navbar-nav .nav-link").forEach(a => a.addEventListener("click", (e) => {
    e.preventDefault(); showPage(a.dataset.page);
  }));
  $("#add-trx-btn").addEventListener("click", openAddTransaction);
  $("#trx-form").addEventListener("submit", saveTransaction);
  $("#trx-type-input").addEventListener("change", syncTrxSavingsUI);
  $("#trx-filter-btn").addEventListener("click", loadTransactions);
  $("#rep-run-btn").addEventListener("click", runReport);
  $("#export-csv-btn").addEventListener("click", () => downloadFile("/api/export/csv", backupFileName("csv")));
  $("#export-xlsx-btn").addEventListener("click", () => downloadFile("/api/export/excel", backupFileName("xlsx")));

  // savings
  $("#quick-saving-btn").addEventListener("click", () => openSaving());
  $("#add-saving-btn").addEventListener("click", () => openSaving());
  $("#saving-form").addEventListener("submit", saveSaving);
  $("#run-offsets-btn").addEventListener("click", runOffsets);
  $("#add-transfer-btn").addEventListener("click", () => openTransfer("in"));
  $("#add-transfer-btn-2").addEventListener("click", () => openTransfer("in"));
  $("#transfer-form").addEventListener("submit", saveTransfer);
  $("#tr-dir").addEventListener("change", toggleTransferFunding);

  // accounts and loans
  $("#add-account-btn").addEventListener("click", () => openAccount());
  $("#account-form").addEventListener("submit", saveAccount);
  $("#add-loan-btn").addEventListener("click", openLoan);
  $("#loan-form").addEventListener("submit", saveLoan);

  // financial statements
  $$("#statement-tabs .nav-link").forEach(a => a.addEventListener("click", (e) => {
    e.preventDefault();
    showStatementTab(a.dataset.stmt);
  }));
  $$("#stmt-income [data-range]").forEach(btn => btn.addEventListener("click", () => setQuickRange(btn.dataset.range)));
  $("#stmt-run-btn").addEventListener("click", loadIncomeStatement);
  $("#bs-run-btn").addEventListener("click", loadBalanceSheet);
  $("#print-statement-btn").addEventListener("click", () => window.print());
  $("#add-asset-btn").addEventListener("click", () => openAsset());
  $("#asset-form").addEventListener("submit", saveAsset);
  $("#add-liability-btn").addEventListener("click", () => openLiability());
  $("#liability-form").addEventListener("submit", saveLiability);

  // admin: users
  $("#add-user-btn").addEventListener("click", openAddUser);
  $("#user-form").addEventListener("submit", saveUser);
  $("#gen-password-btn").addEventListener("click", () => { $("#new-password").value = randomPassword(); });
  $("#pwd-form").addEventListener("submit", savePassword);

  // admin: backup & restore
  $("#backup-download-btn").addEventListener("click", downloadBackupNow);
  $("#backup-csv-btn").addEventListener("click", async () => {
    if (await downloadFile("/api/export/csv", backupFileName("csv"))) {
      showAlert("CSV backup downloaded.");
      loadBackupStatus();
    }
  });
  $("#import-file").addEventListener("change", updateImportButtonState);
  $("#import-confirm").addEventListener("change", updateImportButtonState);
  $("#import-btn").addEventListener("click", runImport);

  // auto-login if token exists
  if (state.token) bootstrapApp(); else $("#auth-view").classList.remove("d-none");
});
