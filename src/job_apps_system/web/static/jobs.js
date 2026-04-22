/* jobs.js — Spreadsheet-style jobs table with inline editing */

async function callJson(url, method, payload) {
  const response = await fetch(url, {
    method,
    headers: { "Content-Type": "application/json" },
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" && data !== null ? data.detail : data;
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return data;
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#39;");
}

/* ── State ──────────────────────────────────────────────────────────── */

let jobsData = [];
let searchTerm = "";
const jobsPageConfig = window.jobsPageConfig || {};
const JOBS_LIST_ENDPOINT = jobsPageConfig.listEndpoint || "/jobs/list";
const SHOW_APPLICATION_COLUMNS = jobsPageConfig.showApplicationColumns !== false;
let sortField = "created_time";
let sortDirection = "desc";

/* Column definitions: field → display properties */
const ALL_COLUMNS = [
  { field: "applied",         editable: true,  type: "checkbox" },
  { field: "resume_url",      editable: true,  type: "url" },
  { field: "posted_date",     editable: false, type: "date" },
  { field: "score",           editable: false, type: "score" },
  { field: "company_name",    editable: true,  type: "text" },
  { field: "job_title",       editable: true,  type: "text" },
  { field: "job_description", editable: true,  type: "longtext" },
  { field: "apply_url",       editable: true,  type: "url" },
  { field: "company_url",     editable: true,  type: "url" },
  { field: "job_posting_url", editable: true,  type: "url" },
  { field: "created_time",    editable: false, type: "date" },
];

const VISIBLE_COLUMNS = ALL_COLUMNS.filter((column) => {
  if (SHOW_APPLICATION_COLUMNS) return true;
  return !["applied", "resume_url"].includes(column.field);
});

/* ── Data loading ───────────────────────────────────────────────────── */

async function loadJobs() {
  try {
    const data = await callJson(JOBS_LIST_ENDPOINT, "GET");
    jobsData = data.jobs || [];
    renderTable(filteredJobs());
  } catch (err) {
    const tbody = document.getElementById("jobs-table-body");
    tbody.innerHTML = `<tr><td colspan="${VISIBLE_COLUMNS.length}" class="empty-state"><p>Failed to load jobs: ${escapeHtml(err.message)}</p></td></tr>`;
  }
}

function filteredJobs() {
  if (!searchTerm) return jobsData;
  const q = searchTerm.toLowerCase();
  return jobsData.filter(
    (j) =>
      (j.company_name || "").toLowerCase().includes(q) ||
      (j.job_title || "").toLowerCase().includes(q),
  );
}

function sortedJobs(jobs) {
  const column = ALL_COLUMNS.find((item) => item.field === sortField);
  if (!column) return [...jobs];

  const direction = sortDirection === "asc" ? 1 : -1;
  return [...jobs].sort((left, right) => {
    const leftValue = sortableValue(left, column);
    const rightValue = sortableValue(right, column);

    const leftMissing = leftValue == null || leftValue === "";
    const rightMissing = rightValue == null || rightValue === "";
    if (leftMissing && rightMissing) return compareTiebreak(left, right);
    if (leftMissing) return 1;
    if (rightMissing) return -1;

    let comparison = 0;
    if (typeof leftValue === "number" && typeof rightValue === "number") {
      comparison = leftValue - rightValue;
    } else {
      comparison = String(leftValue).localeCompare(String(rightValue), undefined, {
        numeric: true,
        sensitivity: "base",
      });
    }

    if (comparison === 0) return compareTiebreak(left, right);
    return comparison * direction;
  });
}

function sortableValue(job, column) {
  const value = job[column.field];
  if (value == null) return null;

  if (column.type === "checkbox") {
    return value ? 1 : 0;
  }
  if (column.type === "score") {
    const number = Number(value);
    return Number.isFinite(number) ? number : null;
  }
  if (column.type === "date") {
    const timestamp = Date.parse(value);
    return Number.isNaN(timestamp) ? String(value).toLowerCase() : timestamp;
  }

  return String(value).trim().toLowerCase();
}

function compareTiebreak(left, right) {
  return String(left.id || "").localeCompare(String(right.id || ""), undefined, {
    numeric: true,
    sensitivity: "base",
  });
}

/* ── Rendering ──────────────────────────────────────────────────────── */

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString();
}

function scoreHtml(score) {
  if (score == null) return '<span class="score-badge score-none">—</span>';
  const n = Number(score);
  let tier = "low";
  if (n >= 80) tier = "high";
  else if (n >= 50) tier = "mid";
  return `<span class="score-badge score-${tier}">${escapeHtml(score)}</span>`;
}

function urlCellHtml(url, field, jobId) {
  if (!url) {
    return `<span class="url-empty" data-field="${field}" data-job-id="${escapeHtml(jobId)}">—</span>`;
  }
  const isDriveUrl = isGoogleDriveUrl(url);
  const icon = isDriveUrl ? googleDriveIconHtml() : "";
  const className = isDriveUrl ? "url-link drive-url-link" : "url-link";
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="${className}" title="${escapeHtml(url)}">${icon}<span>${escapeHtml(truncateUrl(url))}</span></a>`;
}

function isGoogleDriveUrl(url) {
  try {
    const hostname = new URL(url).hostname.toLowerCase();
    return hostname === "drive.google.com" || hostname === "docs.google.com";
  } catch {
    return false;
  }
}

function googleDriveIconHtml() {
  return `
    <span class="drive-link-icon" aria-hidden="true">
      <svg width="15" height="15" viewBox="0 0 48 42" role="img" focusable="false" xmlns="http://www.w3.org/2000/svg">
        <path fill="#188038" d="M16 0h16l-8 15z" />
        <path fill="#0F9D58" d="M16 0 0 28h16l8-13z" />
        <path fill="#FBBC04" d="M32 0 48 28H32L24 15z" />
        <path fill="#4285F4" d="M0 28h16l8 14H8z" />
        <path fill="#1A73E8" d="M16 28h16l8 14H24z" />
        <path fill="#EA4335" d="M32 28h16l-8 14z" />
      </svg>
    </span>`;
}

function truncateText(text, max) {
  if (!text) return "";
  return text.length > max ? text.slice(0, max - 1) + "\u2026" : text;
}

function truncateUrl(url) {
  try {
    const u = new URL(url);
    const path = u.pathname === "/" ? "" : u.pathname;
    const display = u.hostname + path;
    return display.length > 35 ? display.slice(0, 32) + "..." : display;
  } catch {
    return url.length > 35 ? url.slice(0, 32) + "..." : url;
  }
}

function renderTable(jobs) {
  const tbody = document.getElementById("jobs-table-body");
  const countEl = document.getElementById("jobs-count");
  countEl.textContent = `${jobs.length} job${jobs.length !== 1 ? "s" : ""}`;

  if (!jobs.length) {
    tbody.innerHTML = `<tr><td colspan="${VISIBLE_COLUMNS.length}" class="empty-state"><p>No jobs found.</p></td></tr>`;
    return;
  }

  tbody.innerHTML = sortedJobs(jobs)
    .map((job) => {
      const cells = VISIBLE_COLUMNS.map((column) => renderCell(job, column)).join("");
      return `<tr data-job-id="${escapeHtml(job.id)}">${cells}</tr>`;
    })
    .join("");

  updateSortIndicators();
}

function renderCell(job, column) {
  if (column.field === "applied") {
    return `
      <td class="cell-checkbox" data-field="applied" data-editable>
        <input type="checkbox" class="applied-checkbox" ${job.applied ? "checked" : ""} data-job-id="${escapeHtml(job.id)}" />
      </td>`;
  }
  if (column.field === "resume_url") {
    return `<td data-field="resume_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.resume_url, "resume_url", job.id)}</td>`;
  }
  if (column.field === "posted_date") {
    return `<td data-field="posted_date">${escapeHtml(formatDate(job.posted_date))}</td>`;
  }
  if (column.field === "score") {
    return `<td data-field="score">${scoreHtml(job.score)}</td>`;
  }
  if (column.field === "company_name") {
    return `<td data-field="company_name" data-editable data-job-id="${escapeHtml(job.id)}">${escapeHtml(job.company_name || "")}</td>`;
  }
  if (column.field === "job_title") {
    return `<td data-field="job_title" data-editable data-job-id="${escapeHtml(job.id)}">${escapeHtml(job.job_title || "")}</td>`;
  }
  if (column.field === "job_description") {
    return `<td class="cell-longtext" data-field="job_description" data-editable data-job-id="${escapeHtml(job.id)}" title="${escapeHtml(job.job_description || "")}">${escapeHtml(truncateText(job.job_description, 80))}</td>`;
  }
  if (column.field === "apply_url") {
    return `<td data-field="apply_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.apply_url, "apply_url", job.id)}</td>`;
  }
  if (column.field === "company_url") {
    return `<td data-field="company_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.company_url, "company_url", job.id)}</td>`;
  }
  if (column.field === "job_posting_url") {
    return `<td data-field="job_posting_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.job_posting_url, "job_posting_url", job.id)}</td>`;
  }
  if (column.field === "created_time") {
    return `<td data-field="created_time">${escapeHtml(formatDate(job.created_time))}</td>`;
  }
  return "";
}

/* ── Inline editing ─────────────────────────────────────────────────── */

let activeEditor = null; // { td, field, jobId, originalValue, input }

function startEdit(td) {
  if (activeEditor && activeEditor.td === td) return;
  if (activeEditor) commitEdit();

  const field = td.dataset.field;
  const jobId = td.dataset.jobId;
  const job = jobsData.find((j) => String(j.id) === String(jobId));
  if (!job) return;

  const originalValue = job[field] ?? "";
  td.classList.add("editing");

  const input = document.createElement("input");
  input.type = "text";
  input.className = "cell-editor";
  input.value = originalValue;
  td.textContent = "";
  td.appendChild(input);
  input.focus();
  input.select();

  activeEditor = { td, field, jobId, originalValue, input };

  input.addEventListener("keydown", onEditorKeydown);
  input.addEventListener("blur", onEditorBlur);
}

function onEditorKeydown(e) {
  if (e.key === "Enter") {
    e.preventDefault();
    commitEdit();
  } else if (e.key === "Escape") {
    e.preventDefault();
    cancelEdit();
  } else if (e.key === "Tab") {
    e.preventDefault();
    commitEdit();
    if (e.shiftKey) {
      focusPrevEditable();
    } else {
      focusNextEditable();
    }
  }
}

function onEditorBlur() {
  // Defer so click-on-another-cell works before we tear down
  setTimeout(() => {
    if (activeEditor) commitEdit();
  }, 0);
}

function commitEdit() {
  if (!activeEditor) return;
  const { td, field, jobId, originalValue, input } = activeEditor;
  const newValue = input.value;

  input.removeEventListener("keydown", onEditorKeydown);
  input.removeEventListener("blur", onEditorBlur);
  activeEditor = null;
  td.classList.remove("editing");

  // Update in-memory data
  const job = jobsData.find((j) => String(j.id) === String(jobId));
  if (job) job[field] = newValue;

  // Re-render the cell
  const col = ALL_COLUMNS.find((c) => c.field === field);
  if (col && col.type === "url") {
    td.innerHTML = urlCellHtml(newValue, field, jobId);
  } else if (col && col.type === "longtext") {
    td.textContent = truncateText(newValue, 80);
    td.title = newValue;
  } else {
    td.textContent = newValue;
  }

  // Show saved flash if changed
  if (newValue !== String(originalValue ?? "")) {
    td.classList.add("cell-dirty");
    flashSaved(td);
    void saveCell(jobId, field, newValue, originalValue, td);
  }
}

function cancelEdit() {
  if (!activeEditor) return;
  const { td, field, jobId, originalValue, input } = activeEditor;

  input.removeEventListener("keydown", onEditorKeydown);
  input.removeEventListener("blur", onEditorBlur);
  activeEditor = null;
  td.classList.remove("editing");

  // Restore original value
  const col = ALL_COLUMNS.find((c) => c.field === field);
  if (col && col.type === "url") {
    td.innerHTML = urlCellHtml(originalValue, field, jobId);
  } else if (col && col.type === "longtext") {
    td.textContent = truncateText(String(originalValue ?? ""), 80);
    td.title = String(originalValue ?? "");
  } else {
    td.textContent = String(originalValue ?? "");
  }
}

function flashSaved(td) {
  td.classList.add("cell-saved");
  setTimeout(() => td.classList.remove("cell-saved"), 1200);
}

/* ── Checkbox toggle ────────────────────────────────────────────────── */

function handleCheckboxChange(e) {
  const checkbox = e.target;
  if (!checkbox.classList.contains("applied-checkbox")) return;
  const jobId = checkbox.dataset.jobId;
  const job = jobsData.find((j) => String(j.id) === String(jobId));
  const originalValue = Boolean(job?.applied);
  if (job) job.applied = checkbox.checked;

  const td = checkbox.closest("td");
  td.classList.add("cell-dirty");
  flashSaved(td);
  void saveCell(jobId, "applied", checkbox.checked, originalValue, td);
}

/* ── Tab navigation between editable cells ──────────────────────────── */

function getEditableCells() {
  return Array.from(
    document.querySelectorAll("#jobs-table-body td[data-editable]:not(.cell-checkbox)"),
  );
}

function focusNextEditable() {
  const cells = getEditableCells();
  if (!cells.length) return;
  const current = document.activeElement?.closest("td[data-editable]");
  let idx = current ? cells.indexOf(current) : -1;
  idx = (idx + 1) % cells.length;
  startEdit(cells[idx]);
}

function focusPrevEditable() {
  const cells = getEditableCells();
  if (!cells.length) return;
  const current = document.activeElement?.closest("td[data-editable]");
  let idx = current ? cells.indexOf(current) : 0;
  idx = (idx - 1 + cells.length) % cells.length;
  startEdit(cells[idx]);
}

/* ── Save stub ──────────────────────────────────────────────────────── */

async function saveCell(jobId, field, value, originalValue, td) {
  try {
    const response = await callJson(`/jobs/${jobId}`, "PATCH", { [field]: value });
    const updatedJob = response.job;
    const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
    if (index >= 0 && updatedJob) {
      jobsData[index] = { ...jobsData[index], ...updatedJob };
    }
  } catch (err) {
    const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
    if (index >= 0) {
      jobsData[index][field] = originalValue;
    }

    if (field === "applied") {
      const checkbox = td.querySelector(".applied-checkbox");
      if (checkbox) {
        checkbox.checked = Boolean(originalValue);
      }
    } else {
      const col = ALL_COLUMNS.find((column) => column.field === field);
      if (col && col.type === "url") {
        td.innerHTML = urlCellHtml(originalValue, field, jobId);
      } else if (col && col.type === "longtext") {
        td.textContent = truncateText(String(originalValue ?? ""), 80);
        td.title = String(originalValue ?? "");
      } else {
        td.textContent = String(originalValue ?? "");
      }
    }

    window.alert(`Failed to save job change: ${err.message}`);
  }
}

/* ── Search ─────────────────────────────────────────────────────────── */

function onSearchInput(e) {
  searchTerm = e.target.value.trim();
  renderTable(filteredJobs());
}

function updateSortIndicators() {
  const headers = document.querySelectorAll(".jobs-table thead th[data-sort-field]");
  headers.forEach((header) => {
    const field = header.dataset.sortField;
    if (field === sortField) {
      header.dataset.sortDirection = sortDirection;
      header.setAttribute("aria-sort", sortDirection === "asc" ? "ascending" : "descending");
      return;
    }
    delete header.dataset.sortDirection;
    header.setAttribute("aria-sort", "none");
  });
}

function onHeaderClick(e) {
  if (e.target.closest(".col-resize-handle")) return;
  const header = e.target.closest("th[data-sort-field]");
  if (!header) return;

  const field = header.dataset.sortField;
  if (!field) return;

  if (sortField === field) {
    sortDirection = sortDirection === "asc" ? "desc" : "asc";
  } else {
    sortField = field;
    sortDirection = ["created_time", "posted_date", "score"].includes(field) ? "desc" : "asc";
  }

  renderTable(filteredJobs());
}

/* ── Event delegation ───────────────────────────────────────────────── */

function onTableClick(e) {
  // Checkbox change is handled separately
  if (e.target.classList.contains("applied-checkbox")) return;

  // Don't intercept clicks on links
  if (e.target.tagName === "A") return;

  const td = e.target.closest("td[data-editable]:not(.cell-checkbox)");
  if (!td) return;

  startEdit(td);
}

function initColumnResize() {
  const table = document.querySelector(".jobs-table");
  if (!table) return;
  const headers = table.querySelectorAll("thead th");

  headers.forEach((th) => {
    const existingHandle = th.querySelector(".col-resize-handle");
    if (existingHandle) {
      existingHandle.remove();
    }

    const handle = document.createElement("div");
    handle.className = "col-resize-handle";
    th.appendChild(handle);

    let startX;
    let startWidth;

    function onMouseMove(e) {
      const delta = e.clientX - startX;
      const newWidth = Math.max(40, startWidth + delta);
      th.style.width = `${newWidth}px`;
    }

    function onMouseUp() {
      document.removeEventListener("mousemove", onMouseMove);
      document.removeEventListener("mouseup", onMouseUp);
      document.body.style.cursor = "";
      document.body.style.userSelect = "";
    }

    handle.addEventListener("mousedown", (e) => {
      e.preventDefault();
      startX = e.clientX;
      startWidth = th.getBoundingClientRect().width;
      document.body.style.cursor = "col-resize";
      document.body.style.userSelect = "none";
      document.addEventListener("mousemove", onMouseMove);
      document.addEventListener("mouseup", onMouseUp);
    });
  });
}

/* ── Init ───────────────────────────────────────────────────────────── */

window.addEventListener("DOMContentLoaded", () => {
  const tbody = document.getElementById("jobs-table-body");
  const searchInput = document.getElementById("jobs-search");
  const refreshBtn = document.getElementById("jobs-refresh");
  const tableHead = document.querySelector(".jobs-table thead");

  tbody.addEventListener("click", onTableClick);
  tbody.addEventListener("change", handleCheckboxChange);
  searchInput.addEventListener("input", onSearchInput);
  refreshBtn.addEventListener("click", loadJobs);
  tableHead.addEventListener("click", onHeaderClick);

  initColumnResize();
  loadJobs();
});
