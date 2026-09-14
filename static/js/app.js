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
};

// ---------- helpers ----------
function $(sel) { return document.querySelector(sel); }
function $$(sel) { return document.querySelectorAll(sel); }

function showAlert(message, type = "success") {
  const area = $("#alert-area");
  area.innerHTML = `<div class="alert alert-${type} alert-dismissible fade show" role="alert">
    ${message}<button type="button" class="btn-close" data-bs-dismiss="alert"></button></div>`;
  setTimeout(() => { area.innerHTML = ""; }, 5000);
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
  if (!res.ok) {
    const detail = data && data.detail ? data.detail : "Request failed";
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
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
  localStorage.removeItem("ff_token");
  $("#main-view").classList.add("d-none");
  $("#auth-view").classList.remove("d-none");
}

async function bootstrapApp() {
  // verify token and load me
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
}

function fillCategorySelectors() {
  const opts = state.categories.map(c => `<option value="${c.id}">${c.name}</option>`).join("");
  $("#trx-category-input").innerHTML = opts;
  $("#trx-category").innerHTML = `<option value="">All</option>` + opts;
  const multi = state.categories.map(c => `<option value="${c.id}">${c.name}</option>`).join("");
  $("#rep-categories").innerHTML = multi;
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
  if (page === "reports") runReport();
  if (page === "admin") loadUsers();
}

// ---------- dashboard ----------
async function loadDashboard() {
  const data = await api("/api/dashboard");
  const cards = [
    { label: "Total income", value: fmtMoney(data.total_income), cls: "income", icon: "bi-arrow-down-circle" },
    { label: "Total expenses", value: fmtMoney(data.total_expenses), cls: "expense", icon: "bi-arrow-up-circle" },
    { label: "Savings", value: fmtMoney(data.total_savings), cls: "savings", icon: "bi-piggy-bank" },
    { label: "Balance", value: fmtMoney(data.balance), cls: "balance", icon: "bi-wallet2" },
    { label: "Transactions", value: data.transaction_count, cls: "balance", icon: "bi-list-check" },
  ];
  $("#kpi-cards").innerHTML = cards.map(c => `
    <div class="col-6 col-md-4 col-xl">
      <div class="card kpi-card ${c.cls}"><div class="card-body">
        <div class="d-flex justify-content-between align-items-start">
          <div><div class="text-muted small">${c.label}</div><div class="kpi-value">${c.value}</div></div>
          <i class="bi ${c.icon} fs-4 text-muted"></i>
        </div>
      </div></div>
    </div>`).join("");

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
      <td>${t.type}</td>
      <td>${t.category ? t.category.name : ""}</td>
      <td class="text-end ${t.type === "Income" ? "text-success" : "text-danger"}">${fmtMoney(t.amount)}</td>
      <td>${t.description || ""}</td>
      <td class="text-end">
        ${canEdit() ? `<button class="btn btn-sm btn-outline-secondary" onclick="editTransaction(${t.id})"><i class="bi bi-pencil"></i></button>
        <button class="btn btn-sm btn-outline-danger" onclick="deleteTransaction(${t.id})"><i class="bi bi-trash"></i></button>` : ""}
      </td>
    </tr>`).join("");
}

function openAddTransaction() {
  $("#trxModalTitle").textContent = "Add transaction";
  $("#trx-id").value = "";
  $("#trx-date").value = new Date().toISOString().slice(0, 10);
  $("#trx-type-input").value = "Expenses";
  $("#trx-amount").value = "";
  $("#trx-description").value = "";
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
  const payload = {
    date: $("#trx-date").value,
    type: $("#trx-type-input").value,
    category_id: Number($("#trx-category-input").value),
    amount: Number($("#trx-amount").value),
    description: $("#trx-description").value,
  };
  if (id) {
    await api("/api/transactions/" + id, { method: "PUT", body: JSON.stringify(payload) });
  } else {
    await api("/api/transactions", { method: "POST", body: JSON.stringify(payload) });
  }
  bootstrap.Modal.getInstance($("#trxModal")).hide();
  showAlert("Transaction saved");
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
    <tr><td>${r.key}</td><td class="text-end text-success">${fmtMoney(r.income)}</td>
    <td class="text-end text-danger">${fmtMoney(r.expenses)}</td>
    <td class="text-end text-primary">${fmtMoney(r.savings)}</td>
    <td class="text-end">${fmtMoney(r.net)}</td></tr>
  `).join("") + `<tr class="fw-bold"><td>${data.totals.key}</td>
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

// ---------- admin ----------
async function loadUsers() {
  const users = await api("/api/users");
  $("#users-table tbody").innerHTML = users.map(u => `
    <tr>
      <td>${u.id}</td><td>${u.username}</td><td>${u.email}</td>
      <td>
        <select class="form-select form-select-sm" onchange="changeRole(${u.id}, this.value)" ${isMaster() ? "" : "disabled"}>
          ${["master_admin","admin","editor","viewer","downloader"].map(r =>
            `<option value="${r}" ${u.role === r ? "selected" : ""}>${r}</option>`).join("")}
        </select>
      </td>
      <td><input type="checkbox" ${u.is_approved ? "checked" : ""} onchange="changeField(${u.id}, 'is_approved', this.checked)"></td>
      <td><input type="checkbox" ${u.is_active ? "checked" : ""} onchange="changeField(${u.id}, 'is_active', this.checked)"></td>
      <td>${canAdmin() ? `<button class="btn btn-sm btn-outline-primary" onclick="enableUser(${u.id})">Enable</button>` : ""}</td>
    </tr>`).join("");
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

// ---------- exports ----------
async function downloadFile(path, filename) {
  const res = await fetch(API + path, { headers: { Authorization: "Bearer " + state.token } });
  if (!res.ok) { showAlert("Export failed", "danger"); return; }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

// ---------- wire up ----------
document.addEventListener("DOMContentLoaded", () => {
  $("#login-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try { await login($("#login-username").value, $("#login-password").value); }
    catch (err) { showAlert(err.message, "danger"); }
  });

  $("#register-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    try {
      await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
          username: $("#reg-username").value,
          email: $("#reg-email").value,
          full_name: $("#reg-fullname").value,
          password: $("#reg-password").value,
        }),
      });
      showAlert("Registration successful. Please wait for the master admin to approve your account.");
      $("#register-form").reset();
    } catch (err) { showAlert(err.message, "danger"); }
  });

  $("#logout-btn").addEventListener("click", logout);
  $$(".navbar-nav .nav-link").forEach(a => a.addEventListener("click", (e) => {
    e.preventDefault(); showPage(a.dataset.page);
  }));
  $("#add-trx-btn").addEventListener("click", openAddTransaction);
  $("#trx-form").addEventListener("submit", saveTransaction);
  $("#trx-filter-btn").addEventListener("click", loadTransactions);
  $("#rep-run-btn").addEventListener("click", runReport);
  $("#export-csv-btn").addEventListener("click", () => downloadFile("/api/export/csv", "transactions.csv"));
  $("#export-xlsx-btn").addEventListener("click", () => downloadFile("/api/export/excel", "transactions.xlsx"));

  // auto-login if token exists
  if (state.token) bootstrapApp(); else $("#auth-view").classList.remove("d-none");
});
