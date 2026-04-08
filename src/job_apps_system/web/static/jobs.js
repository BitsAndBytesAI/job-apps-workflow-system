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

/* Column definitions: field → display properties */
const COLUMNS = [
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

const EDITABLE_FIELDS = COLUMNS.filter((c) => c.editable && c.type !== "checkbox").map((c) => c.field);

/* ── Data loading ───────────────────────────────────────────────────── */

async function loadJobs() {
  try {
    const data = await callJson("/jobs/list", "GET");
    jobsData = data.jobs || [];
    renderTable(filteredJobs());
  } catch (err) {
    const tbody = document.getElementById("jobs-table-body");
    tbody.innerHTML = `<tr><td colspan="11" class="empty-state"><p>Failed to load jobs: ${escapeHtml(err.message)}</p></td></tr>`;
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
  return `<a href="${escapeHtml(url)}" target="_blank" rel="noopener" class="url-link" title="${escapeHtml(url)}">${escapeHtml(truncateUrl(url))}</a>`;
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
    tbody.innerHTML = `<tr><td colspan="11" class="empty-state"><p>No jobs found.</p></td></tr>`;
    return;
  }

  tbody.innerHTML = jobs
    .map(
      (job) => `
      <tr data-job-id="${escapeHtml(job.id)}">
        <td class="cell-checkbox" data-field="applied" data-editable>
          <input type="checkbox" class="applied-checkbox" ${job.applied ? "checked" : ""} data-job-id="${escapeHtml(job.id)}" />
        </td>
        <td data-field="resume_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.resume_url, "resume_url", job.id)}</td>
        <td data-field="posted_date">${escapeHtml(formatDate(job.posted_date))}</td>
        <td data-field="score">${scoreHtml(job.score)}</td>
        <td data-field="company_name" data-editable data-job-id="${escapeHtml(job.id)}">${escapeHtml(job.company_name || "")}</td>
        <td data-field="job_title" data-editable data-job-id="${escapeHtml(job.id)}">${escapeHtml(job.job_title || "")}</td>
        <td class="cell-longtext" data-field="job_description" data-editable data-job-id="${escapeHtml(job.id)}" title="${escapeHtml(job.job_description || "")}">${escapeHtml(truncateText(job.job_description, 80))}</td>
        <td data-field="apply_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.apply_url, "apply_url", job.id)}</td>
        <td data-field="company_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.company_url, "company_url", job.id)}</td>
        <td data-field="job_posting_url" data-editable data-job-id="${escapeHtml(job.id)}">${urlCellHtml(job.job_posting_url, "job_posting_url", job.id)}</td>
        <td data-field="created_time">${escapeHtml(formatDate(job.created_time))}</td>
      </tr>`,
    )
    .join("");
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
  const col = COLUMNS.find((c) => c.field === field);
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
  const col = COLUMNS.find((c) => c.field === field);
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
      const col = COLUMNS.find((column) => column.field === field);
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

/* ── Init ───────────────────────────────────────────────────────────── */

window.addEventListener("DOMContentLoaded", () => {
  const tbody = document.getElementById("jobs-table-body");
  const searchInput = document.getElementById("jobs-search");
  const refreshBtn = document.getElementById("jobs-refresh");

  tbody.addEventListener("click", onTableClick);
  tbody.addEventListener("change", handleCheckboxChange);
  searchInput.addEventListener("input", onSearchInput);
  refreshBtn.addEventListener("click", loadJobs);

  loadJobs();
});
