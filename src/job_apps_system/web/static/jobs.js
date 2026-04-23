/* jobs.js — Jobs view with card layout (Applications) and table layout (All Jobs) */

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
const activeApplyRuns = new Map();
const activeResumeRuns = new Map();
const jobsPageConfig = window.jobsPageConfig || {};
const JOBS_LIST_ENDPOINT = jobsPageConfig.listEndpoint || "/jobs/list";
const SHOW_APPLICATION_COLUMNS = jobsPageConfig.showApplicationColumns !== false;
const USE_CARD_LAYOUT = jobsPageConfig.useCardLayout !== false;
const CARD_MOVE_DURATION_MS = 460;
let sortField = "created_time";
let sortDirection = "desc";
let applyPreviewOverlay = null;
let applyPreviewViewport = null;
let applyPreviewImage = null;

/* Column definitions: field → display properties */
const ALL_COLUMNS = [
  { field: "apply_action",    editable: false, type: "action" },
  { field: "resume_url",      editable: true,  type: "url" },
  { field: "posted_date",     editable: false, type: "date" },
  { field: "score",           editable: false, type: "score" },
  { field: "applied",         editable: false, type: "checkbox" },
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
  return !["apply_action", "resume_url"].includes(column.field);
});

/* ── Data loading ───────────────────────────────────────────────────── */

async function loadJobs() {
  try {
    const data = await callJson(JOBS_LIST_ENDPOINT, "GET");
    jobsData = data.jobs || [];
    renderView(filteredJobs());
  } catch (err) {
    if (USE_CARD_LAYOUT) {
      const list = document.getElementById("jobs-card-list");
      list.innerHTML = `<div class="empty-state"><p>Failed to load jobs: ${escapeHtml(err.message)}</p></div>`;
    } else {
      const tbody = document.getElementById("jobs-table-body");
      tbody.innerHTML = `<tr><td colspan="${VISIBLE_COLUMNS.length}" class="empty-state"><p>Failed to load jobs: ${escapeHtml(err.message)}</p></td></tr>`;
    }
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
    if (USE_CARD_LAYOUT && SHOW_APPLICATION_COLUMNS && left.applied !== right.applied) {
      return left.applied ? 1 : -1;
    }

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

/* ── Shared rendering helpers ──────────────────────────────────────── */

function renderView(jobs) {
  if (USE_CARD_LAYOUT) {
    renderCards(jobs);
  } else {
    renderTable(jobs);
  }
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatDateShort(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function scoreHtml(score) {
  if (score == null) return '<span class="score-badge score-none">\u2014</span>';
  const n = Number(score);
  let tier = "low";
  if (n >= 80) tier = "high";
  else if (n >= 50) tier = "mid";
  return `<span class="score-badge score-${tier}">${escapeHtml(score)}</span>`;
}

function urlCellHtml(url, field, jobId) {
  if (!url) {
    return `<span class="url-empty" data-field="${field}" data-job-id="${escapeHtml(jobId)}">\u2014</span>`;
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

/* ── Card rendering (Applications page) ────────────────────────────── */

function renderCards(jobs) {
  const list = document.getElementById("jobs-card-list");
  const countEl = document.getElementById("jobs-count");
  countEl.textContent = `${jobs.length} job${jobs.length !== 1 ? "s" : ""}`;
  const previousRects = captureCardRects(list);
  hideApplyPreview();

  if (!jobs.length) {
    list.innerHTML = `<div class="empty-state"><p>No jobs found.</p></div>`;
    return;
  }

  list.innerHTML = sortedJobs(jobs).map((job) => renderCard(job)).join("");
  animateCardReorder(list, previousRects);
}

function renderCard(job) {
  const id = escapeHtml(job.id);
  const postedLabel = formatDate(job.posted_date) || "\u2014";
  const createdLabel = formatDateShort(job.created_time) || "\u2014";

  // Line 1 — Header: score, company, title, posted timestamp
  const header = `
    <div class="job-card-row job-card-header">
      <div class="job-card-header-left">
        ${scoreHtml(job.score)}
        <span class="job-card-company" data-editable data-field="company_name" data-job-id="${id}">${escapeHtml(job.company_name || "")}</span>
        <span class="job-card-title" data-editable data-field="job_title" data-job-id="${id}">${escapeHtml(job.job_title || "")}</span>
      </div>
      <div class="job-card-header-right">
        <span class="job-card-posted-badge">Posted ${escapeHtml(postedLabel)}</span>
      </div>
    </div>`;

  // Line 2 — Description
  const desc = `
    <div class="job-card-row job-card-description" data-editable data-field="job_description" data-job-id="${id}" title="${escapeHtml(job.job_description || "")}">${escapeHtml(truncateText(job.job_description, 200))}</div>`;

  // Line 3 — Links
  const linkItem = (label, url, field) => {
    const content = url
      ? urlCellHtml(url, field, job.id)
      : `<span class="url-empty" data-field="${field}" data-job-id="${id}">\u2014</span>`;
    return `<span class="job-card-link-item" data-editable data-field="${field}" data-job-id="${id}"><span class="job-card-field-label">${label}:</span> ${content}</span>`;
  };

  const links = `
    <div class="job-card-row job-card-links">
      ${linkItem("Apply", job.apply_url, "apply_url")}
      ${linkItem("Posting", job.job_posting_url, "job_posting_url")}
      ${linkItem("Company", job.company_url, "company_url")}
    </div>`;

  // Line 4 — Actions + meta
  const actions = SHOW_APPLICATION_COLUMNS
    ? `<div class="job-card-actions">${applyActionHtml(job)}${resumeActionHtml(job)}</div>`
    : `<div class="job-card-actions"></div>`;
  const meta = `
    <div class="job-card-row job-card-meta">
      ${actions}
      <span class="job-card-created-meta">Created ${escapeHtml(createdLabel)}</span>
    </div>`;

  return `<div class="job-card" data-job-id="${id}"><div class="job-card-inner">${header}${desc}${links}${meta}</div></div>`;
}

/* ── Table rendering (All Jobs page) ───────────────────────────────── */

function renderTable(jobs) {
  const tbody = document.getElementById("jobs-table-body");
  const countEl = document.getElementById("jobs-count");
  countEl.textContent = `${jobs.length} job${jobs.length !== 1 ? "s" : ""}`;
  hideApplyPreview();

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
  if (column.field === "apply_action") {
    return `<td class="cell-apply-action">${applyActionHtml(job)}</td>`;
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

function applyActionHtml(job) {
  const previewClass = job.application_screenshot_url ? " has-preview" : "";
  const previewData = job.application_screenshot_url
    ? ` data-preview-url="${escapeHtml(job.application_screenshot_url)}"`
    : "";
  if (job.applied) {
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button applied" disabled data-job-id="${escapeHtml(job.id)}">Applied</button></div>`;
  }
  if (activeApplyRuns.has(String(job.id))) {
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button running" disabled data-job-id="${escapeHtml(job.id)}">Applying...</button></div>`;
  }
  if (activeApplyRuns.size > 0) {
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button blocked" disabled data-job-id="${escapeHtml(job.id)}">Wait</button></div>`;
  }
  if (!job.resume_url) {
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button blocked" disabled data-job-id="${escapeHtml(job.id)}">No Resume</button></div>`;
  }
  if (!job.apply_url) {
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button blocked" disabled data-job-id="${escapeHtml(job.id)}">No Apply URL</button></div>`;
  }
  const label = job.application_status === "failed" ? "Retry Apply" : "Apply";
  const title = job.application_error ? ` title="${escapeHtml(job.application_error)}"` : "";
  return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button" data-job-id="${escapeHtml(job.id)}"${title}>${label}</button></div>`;
}

function resumeActionHtml(job) {
  if (activeResumeRuns.has(String(job.id))) {
    return '<button type="button" class="resume-action-button running" disabled>Generating...</button>';
  }
  if (!job.resume_url && activeResumeRuns.size > 0) {
    return '<button type="button" class="resume-action-button blocked" disabled>Wait</button>';
  }
  if (!job.resume_url) {
    return `<button type="button" class="resume-action-button generate-resume-button" data-job-id="${escapeHtml(job.id)}">Generate Custom Resume</button>`;
  }
  return `<a href="${escapeHtml(job.resume_url)}" target="_blank" rel="noopener" class="resume-action-button">AI Resume</a>`;
}

function captureCardRects(list) {
  if (!list) return new Map();
  return new Map(
    Array.from(list.querySelectorAll(".job-card")).map((card) => [
      String(card.dataset.jobId || ""),
      card.getBoundingClientRect(),
    ]),
  );
}

function animateCardReorder(list, previousRects) {
  if (!list || !previousRects.size) return;

  const cards = Array.from(list.querySelectorAll(".job-card"));
  const movedCards = cards.filter((card) => {
    const previous = previousRects.get(String(card.dataset.jobId || ""));
    if (!previous) return false;

    const next = card.getBoundingClientRect();
    const deltaX = previous.left - next.left;
    const deltaY = previous.top - next.top;
    if (!deltaX && !deltaY) return false;

    card.classList.add("job-card-reordering");
    card.style.transition = "none";
    card.style.transform = `translate(${deltaX}px, ${deltaY}px)`;
    return true;
  });

  if (!movedCards.length) return;

  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      movedCards.forEach((card) => {
        card.style.transition = `transform ${CARD_MOVE_DURATION_MS}ms cubic-bezier(0.22, 1, 0.36, 1), box-shadow 220ms ease`;
        card.style.transform = "";
      });
    });
  });

  window.setTimeout(() => {
    movedCards.forEach((card) => {
      card.classList.remove("job-card-reordering");
      card.style.transition = "";
      card.style.transform = "";
    });
  }, CARD_MOVE_DURATION_MS + 80);
}

function ensureApplyPreviewOverlay() {
  if (applyPreviewOverlay && applyPreviewViewport && applyPreviewImage) return;

  applyPreviewOverlay = document.createElement("div");
  applyPreviewOverlay.className = "apply-preview-overlay";
  applyPreviewViewport = document.createElement("div");
  applyPreviewViewport.className = "apply-preview-viewport";
  applyPreviewImage = document.createElement("img");
  applyPreviewImage.className = "apply-preview-image";
  applyPreviewImage.alt = "Application screenshot";
  applyPreviewViewport.appendChild(applyPreviewImage);
  applyPreviewOverlay.appendChild(applyPreviewViewport);
  applyPreviewOverlay.addEventListener("click", (event) => {
    if (event.target === applyPreviewOverlay) {
      hideApplyPreview();
    }
  });
  applyPreviewViewport.addEventListener("mouseleave", hideApplyPreview);
  document.body.appendChild(applyPreviewOverlay);
}

function showApplyPreview(url) {
  if (!url) return;
  ensureApplyPreviewOverlay();
  if (applyPreviewImage.src !== url) {
    applyPreviewImage.src = url;
  }
  applyPreviewViewport.scrollTop = 0;
  applyPreviewOverlay.classList.add("visible");
}

function hideApplyPreview() {
  if (!applyPreviewOverlay) return;
  applyPreviewOverlay.classList.remove("visible");
}

function onWindowKeydown(event) {
  if (event.key === "Escape") {
    hideApplyPreview();
  }
}

async function startApplyForJob(jobId) {
  if (!jobId || activeApplyRuns.size > 0) return;
  activeApplyRuns.set(String(jobId), "");
  renderView(filteredJobs());

  try {
    const run = await callJson("/apply/start", "POST", { limit: 1, job_ids: [String(jobId)] });
    activeApplyRuns.set(String(jobId), String(run.id || ""));
    await pollApplyRun(String(jobId), String(run.id || ""));
  } catch (err) {
    activeApplyRuns.delete(String(jobId));
    renderView(filteredJobs());
    window.alert(`Failed to start Apply Agent: ${err.message}`);
  }
}

async function startResumeForJob(jobId) {
  if (!jobId || activeResumeRuns.size > 0) return;
  activeResumeRuns.set(String(jobId), "");
  renderView(filteredJobs());

  try {
    const run = await callJson("/resumes/generate/start", "POST", { limit: 1, job_ids: [String(jobId)] });
    activeResumeRuns.set(String(jobId), String(run.id || ""));
    await pollResumeRun(String(jobId), String(run.id || ""));
  } catch (err) {
    activeResumeRuns.delete(String(jobId));
    renderView(filteredJobs());
    window.alert(`Failed to start Resume Agent: ${err.message}`);
  }
}

async function pollResumeRun(jobId, runId) {
  try {
    while (true) {
      const run = await callJson(`/runs/${runId}`, "GET");
      if (!["queued", "running"].includes(run.status)) {
        activeResumeRuns.delete(String(jobId));
        await loadJobs();
        if (run.status === "failed") {
          window.alert(`Resume Agent failed: ${run.message || "Unknown error"}`);
        } else if (run.status === "cancelled") {
          window.alert(`Resume Agent cancelled: ${run.message || "Run cancelled"}`);
        }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  } catch (err) {
    activeResumeRuns.delete(String(jobId));
    renderView(filteredJobs());
    window.alert(`Failed to monitor Resume Agent: ${err.message}`);
  }
}

async function pollApplyRun(jobId, runId) {
  try {
    while (true) {
      const run = await callJson(`/runs/${runId}`, "GET");
      if (!["queued", "running"].includes(run.status)) {
        activeApplyRuns.delete(String(jobId));
        await loadJobs();
        if (run.status === "failed") {
          window.alert(`Apply Agent failed: ${run.message || "Unknown error"}`);
        } else if (run.status === "cancelled") {
          window.alert(`Apply Agent cancelled: ${run.message || "Run cancelled"}`);
        }
        return;
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  } catch (err) {
    activeApplyRuns.delete(String(jobId));
    renderView(filteredJobs());
    window.alert(`Failed to monitor Apply Agent: ${err.message}`);
  }
}

/* ── Inline editing ─────────────────────────────────────────────────── */

let activeEditor = null; // { el, field, jobId, originalValue, input, labelText }

function descTruncLen() {
  return USE_CARD_LAYOUT ? 200 : 80;
}

function startEdit(el) {
  if (activeEditor && activeEditor.el === el) return;
  if (activeEditor) commitEdit();

  const field = el.dataset.field;
  const jobId = el.dataset.jobId;
  const job = jobsData.find((j) => String(j.id) === String(jobId));
  if (!job) return;

  const originalValue = job[field] ?? "";

  // Save the label text before clearing (for card link items)
  const labelEl = el.querySelector(".job-card-field-label");
  const labelText = labelEl ? labelEl.textContent : "";

  el.classList.add("editing");

  const input = document.createElement("input");
  input.type = "text";
  input.className = "cell-editor";
  input.value = originalValue;
  el.innerHTML = "";
  el.appendChild(input);
  input.focus();
  input.select();

  activeEditor = { el, field, jobId, originalValue, input, labelText };

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
  setTimeout(() => {
    if (activeEditor) commitEdit();
  }, 0);
}

function commitEdit() {
  if (!activeEditor) return;
  const { el, field, jobId, originalValue, input, labelText } = activeEditor;
  const newValue = input.value;

  input.removeEventListener("keydown", onEditorKeydown);
  input.removeEventListener("blur", onEditorBlur);
  activeEditor = null;
  el.classList.remove("editing");

  // Update in-memory data
  const job = jobsData.find((j) => String(j.id) === String(jobId));
  if (job) job[field] = newValue;

  // Re-render the field content
  restoreFieldContent(el, field, jobId, newValue, labelText);

  // Show saved flash if changed
  if (newValue !== String(originalValue ?? "")) {
    el.classList.add("cell-dirty");
    flashSaved(el);
    void saveCell(jobId, field, newValue, originalValue, el, labelText);
  }
}

function cancelEdit() {
  if (!activeEditor) return;
  const { el, field, jobId, originalValue, input, labelText } = activeEditor;

  input.removeEventListener("keydown", onEditorKeydown);
  input.removeEventListener("blur", onEditorBlur);
  activeEditor = null;
  el.classList.remove("editing");

  restoreFieldContent(el, field, jobId, originalValue, labelText);
}

function restoreFieldContent(el, field, jobId, value, labelText) {
  const col = ALL_COLUMNS.find((c) => c.field === field);
  if (col && col.type === "url") {
    if (labelText) {
      el.innerHTML = `<span class="job-card-field-label">${escapeHtml(labelText)}</span> ` + urlCellHtml(value, field, jobId);
    } else {
      el.innerHTML = urlCellHtml(value, field, jobId);
    }
  } else if (col && col.type === "longtext") {
    el.textContent = truncateText(String(value ?? ""), descTruncLen());
    el.title = String(value ?? "");
  } else {
    el.textContent = String(value ?? "");
  }
}

function flashSaved(el) {
  el.classList.add("cell-saved");
  setTimeout(() => el.classList.remove("cell-saved"), 1200);
}

/* ── Tab navigation between editable cells ──────────────────────────── */

function getEditableCells() {
  const container = USE_CARD_LAYOUT ? "#jobs-card-list" : "#jobs-table-body";
  return Array.from(
    document.querySelectorAll(`${container} [data-editable]:not(.cell-checkbox)`),
  );
}

function focusNextEditable() {
  const cells = getEditableCells();
  if (!cells.length) return;
  const current = document.activeElement?.closest("[data-editable]");
  let idx = current ? cells.indexOf(current) : -1;
  idx = (idx + 1) % cells.length;
  startEdit(cells[idx]);
}

function focusPrevEditable() {
  const cells = getEditableCells();
  if (!cells.length) return;
  const current = document.activeElement?.closest("[data-editable]");
  let idx = current ? cells.indexOf(current) : 0;
  idx = (idx - 1 + cells.length) % cells.length;
  startEdit(cells[idx]);
}

/* ── Save ───────────────────────────────────────────────────────────── */

async function saveCell(jobId, field, value, originalValue, el, labelText) {
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

    if (el) {
      restoreFieldContent(el, field, jobId, originalValue, labelText);
    }

    window.alert(`Failed to save job change: ${err.message}`);
  }
}

/* ── Search ─────────────────────────────────────────────────────────── */

function onSearchInput(e) {
  searchTerm = e.target.value.trim();
  renderView(filteredJobs());
}

/* ── Sort controls ──────────────────────────────────────────────────── */

function onSortChange() {
  const select = document.getElementById("jobs-sort");
  sortField = select.value;
  renderView(filteredJobs());
}

function onSortDirToggle() {
  sortDirection = sortDirection === "asc" ? "desc" : "asc";
  const btn = document.getElementById("jobs-sort-dir");
  btn.textContent = sortDirection === "asc" ? "\u2191" : "\u2193";
  btn.title = sortDirection === "asc" ? "Ascending" : "Descending";
  renderView(filteredJobs());
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

  // Sync the sort dropdown
  const select = document.getElementById("jobs-sort");
  if (select) select.value = sortField;
  const btn = document.getElementById("jobs-sort-dir");
  if (btn) {
    btn.textContent = sortDirection === "asc" ? "\u2191" : "\u2193";
    btn.title = sortDirection === "asc" ? "Ascending" : "Descending";
  }

  renderView(filteredJobs());
}

/* ── Event delegation ───────────────────────────────────────────────── */

function onContainerClick(e) {
  const applyButton = e.target.closest(".apply-job-button");
  if (applyButton && !applyButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    void startApplyForJob(applyButton.dataset.jobId);
    return;
  }

  const resumeButton = e.target.closest(".generate-resume-button");
  if (resumeButton && !resumeButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    void startResumeForJob(resumeButton.dataset.jobId);
    return;
  }

  // Don't intercept clicks on links
  if (e.target.closest("a")) return;

  const editable = e.target.closest("[data-editable]:not(.cell-checkbox)");
  if (!editable) return;

  startEdit(editable);
}

function onContainerMouseOver(e) {
  const wrap = e.target.closest(".apply-action-wrap.has-preview");
  if (!wrap) return;
  showApplyPreview(wrap.dataset.previewUrl);
}

function onContainerMouseOut(e) {
  const wrap = e.target.closest(".apply-action-wrap.has-preview");
  if (!wrap) return;
  if (e.relatedTarget && wrap.contains(e.relatedTarget)) return;
  if (e.relatedTarget && applyPreviewOverlay && applyPreviewOverlay.contains(e.relatedTarget)) return;
  hideApplyPreview();
}

function initColumnResize() {
  const table = document.querySelector(".jobs-table");
  if (!table) return;
  const headers = table.querySelectorAll("thead th");

  headers.forEach((th) => {
    const existingHandle = th.querySelector(".col-resize-handle");
    if (existingHandle) existingHandle.remove();

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
  const searchInput = document.getElementById("jobs-search");
  const refreshBtn = document.getElementById("jobs-refresh");
  const sortSelect = document.getElementById("jobs-sort");
  const sortDirBtn = document.getElementById("jobs-sort-dir");

  searchInput.addEventListener("input", onSearchInput);
  refreshBtn.addEventListener("click", loadJobs);
  sortSelect.addEventListener("change", onSortChange);
  sortDirBtn.addEventListener("click", onSortDirToggle);

  // Set initial sort select value
  sortSelect.value = sortField;

  if (USE_CARD_LAYOUT) {
    const cardList = document.getElementById("jobs-card-list");
    cardList.addEventListener("click", onContainerClick);
    cardList.addEventListener("mouseover", onContainerMouseOver);
    cardList.addEventListener("mouseout", onContainerMouseOut);
  } else {
    const tbody = document.getElementById("jobs-table-body");
    const tableHead = document.querySelector(".jobs-table thead");
    tbody.addEventListener("click", onContainerClick);
    tbody.addEventListener("mouseover", onContainerMouseOver);
    tbody.addEventListener("mouseout", onContainerMouseOut);
    if (tableHead) tableHead.addEventListener("click", onHeaderClick);
    initColumnResize();
  }

  window.addEventListener("keydown", onWindowKeydown);
  loadJobs();
});
