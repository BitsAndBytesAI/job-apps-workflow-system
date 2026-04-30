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
const activeContactLookups = new Map();
const jobsPageConfig = window.jobsPageConfig || {};
const JOBS_LIST_ENDPOINT = jobsPageConfig.listEndpoint || "/jobs/list";
const SHOW_APPLICATION_COLUMNS = jobsPageConfig.showApplicationColumns !== false;
const USE_CARD_LAYOUT = jobsPageConfig.useCardLayout !== false;
const CAN_RUN_INTAKE = jobsPageConfig.canRunIntake === true;
const CAN_RUN_SCORING = jobsPageConfig.canRunScoring === true;
const SHOW_CONTACT_ACTION = jobsPageConfig.showContactAction === true;
const ANYMAILFINDER_CONFIGURED = jobsPageConfig.anymailfinderConfigured === true;
const GMAIL_CONFIGURED = jobsPageConfig.gmailConfigured === true;
let autoFindContactsEnabled = jobsPageConfig.autoFindContactsEnabled === true;
let autoScoreEnabled = jobsPageConfig.autoScoreEnabled === true;
const AUTO_SCORE_PENDING_COUNT = Number.isFinite(Number(jobsPageConfig.autoScorePendingCount))
  ? Number(jobsPageConfig.autoScorePendingCount)
  : 0;
let autoGenerateResumesEnabled = jobsPageConfig.autoGenerateResumesEnabled === true;
const AUTO_GENERATE_RESUMES_PENDING_COUNT = Number.isFinite(Number(jobsPageConfig.autoGenerateResumesPendingCount))
  ? Number(jobsPageConfig.autoGenerateResumesPendingCount)
  : 0;
const THRESHOLD_SETTLING_MS = 120000;
let pendingAutoGenerateResumesEnabled = null;
let thresholdSettlingTimer = null;
let autoResumeRunInFlight = false;
let activePageRunAgent = "";
const USE_SCORE_THRESHOLD_FILTER = jobsPageConfig.useScoreThresholdFilter === true;
const PAGE_RUN_AGENT = jobsPageConfig.pageRunAgent || "";
const PAGE_RUN_LABEL = jobsPageConfig.pageRunLabel || "Agent Status";
const APPLICATION_JOB_ID = jobsPageConfig.applicationJobId || "";
const APPLICATION_AUTO_APPLY = jobsPageConfig.applicationAutoApply === true;
const APPLICATION_MANUAL_APPLY = jobsPageConfig.applicationManualApply === true;
const IS_APPLICATIONS_PAGE = window.location.pathname.startsWith("/applications/");
const IS_EMAILS_INTERVIEWS_PAGE = window.location.pathname.startsWith("/interviews/");
const CARD_MOVE_DURATION_MS = 460;
let sortField = jobsPageConfig.defaultSortField || "created_time";
let sortDirection = jobsPageConfig.defaultSortDirection || "desc";
let applyPreviewOverlay = null;
let applyPreviewViewport = null;
let applyPreviewImage = null;
let pendingApplyChoiceJobId = null;
let pendingManualOutcomeJobId = null;
let pendingAutoScoreEnabled = null;
let activePageRunId = null;
let pageRunStarting = false;
let thresholdPersistTimer = null;
let persistedScoreThreshold = Number.isFinite(Number(jobsPageConfig.scoreThreshold))
  ? Number(jobsPageConfig.scoreThreshold)
  : 0;
let currentScoreThreshold = persistedScoreThreshold;
const expandedJobDescriptions = new Set();
let lastBestMatchesActionLockState = null;

/* Column definitions: field → display properties */
const ALL_COLUMNS = [
  { field: "apply_action",    editable: false, type: "action" },
  { field: "resume_url",      editable: true,  type: "url" },
  { field: "id",              editable: false, type: "text" },
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
  if (SHOW_APPLICATION_COLUMNS) return column.field !== "id";
  return !["apply_action", "resume_url"].includes(column.field);
});

/* ── Data loading ───────────────────────────────────────────────────── */

async function loadJobs() {
  try {
    const data = await callJson(buildJobsListUrl(), "GET");
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

function buildJobsListUrl() {
  const url = new URL(JOBS_LIST_ENDPOINT, window.location.origin);
  if (USE_SCORE_THRESHOLD_FILTER) {
    url.searchParams.set("threshold", String(currentScoreThreshold));
  }
  return `${url.pathname}${url.search}`;
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

function areBestMatchesCardActionsBlocked() {
  return USE_SCORE_THRESHOLD_FILTER && (
    isPageRunBlockingBestMatchesCardActions() ||
    activeResumeRuns.size > 0 ||
    activeApplyRuns.size > 0
  );
}

function isPageRunBlockingBestMatchesCardActions() {
  if (!pageRunStarting && !activePageRunId) return false;
  return PAGE_RUN_AGENT !== "job_scoring";
}

function formatDate(value) {
  if (!value) return "";
  const date = parseDateValue(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric", year: "numeric" });
}

function formatDateShort(value) {
  if (!value) return "";
  const date = parseDateValue(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleDateString("en-US", { month: "short", day: "numeric" });
}

function parseDateValue(value) {
  const text = String(value).trim();
  const dateOnlyMatch = text.match(/^(\d{4})-(\d{2})-(\d{2})$/);
  if (dateOnlyMatch) {
    const [, year, month, day] = dateOnlyMatch;
    return new Date(Number(year), Number(month) - 1, Number(day));
  }
  return new Date(text);
}

function longtextCardHtml(value, expanded, jobId) {
  const text = String(value ?? "");
  const truncated = text.length > 330;
  const rendered = escapeHtml(expanded || !truncated ? text : truncateText(text, 330));
  const full = escapeHtml(text);
  // Inline toggle that sits immediately after the truncated text, so the
  // user sees the expand affordance right where the text gets cut off.
  const toggle = truncated
    ? `<button type="button" class="job-card-description-toggle${expanded ? " is-expanded" : ""}" data-description-toggle data-job-id="${escapeHtml(jobId)}" aria-expanded="${expanded ? "true" : "false"}" aria-label="${expanded ? "Collapse description" : "Expand description"}" title="${expanded ? "Collapse description" : "Expand description"}">${expanded ? "−" : "+"}</button>`
    : "";
  return {
    title: full,
    html: `<span class="job-card-description-text">${rendered}${toggle}</span>`,
  };
}

function scoreHtml(score, withLabel = false) {
  const labelPrefix = withLabel ? "Score " : "";
  if (score == null) return `<span class="score-badge score-none">${labelPrefix}\u2014</span>`;
  const n = Number(score);
  let tier = "low";
  if (n >= 800) tier = "high";
  else if (n >= 700) tier = "mid";
  return `<span class="score-badge score-${tier}">${labelPrefix}${escapeHtml(formatScoreDisplay(score))}</span>`;
}

function formatScoreDisplay(score) {
  const n = Number(score);
  if (!Number.isFinite(n)) return String(score ?? "");
  return `${(n / 10).toFixed(1)}%`;
}

function formatScoreThresholdDisplay(rawThreshold) {
  const n = Number(rawThreshold);
  if (!Number.isFinite(n)) return "";
  return (n / 10).toFixed(1);
}

function formatScoringMessage(message) {
  const text = String(message ?? "");
  return text.replace(/(=\s*)(\d{1,4})(\b)/g, (_, prefix, score, suffix) => `${prefix}${formatScoreDisplay(score)}${suffix}`);
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
  const captchaBlocked = isCaptchaBlocked(job);
  const showDescription = !SHOW_CONTACT_ACTION;
  const description = showDescription
    ? longtextCardHtml(
        job.job_description,
        expandedJobDescriptions.has(String(job.id)),
        job.id,
      )
    : null;

  // Line 1 — Header: company, title, posted timestamp
  const header = `
    <div class="job-card-row job-card-header">
      <div class="job-card-header-left">
        <span class="job-card-company" data-editable data-field="company_name" data-job-id="${id}">${escapeHtml(job.company_name || "")}</span>
        <span class="job-card-title" data-editable data-field="job_title" data-job-id="${id}">${escapeHtml(job.job_title || "")}</span>
      </div>
      <div class="job-card-header-right">
        ${scoreHtml(job.score, true)}
      </div>
    </div>`;

  // Line 2 — Description (moved up; score row removed)
  const desc = showDescription
    ? `
    <div class="job-card-row job-card-description" data-editable data-field="job_description" data-job-id="${id}">
      ${description.html}
    </div>`
    : "";

  // Line 4 — Links
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
  const contacts = SHOW_CONTACT_ACTION ? contactListHtml(job) : "";

  // Line 4 — Actions (left) + Posted timestamp (right)
  const actions = SHOW_APPLICATION_COLUMNS
    ? `<div class="job-card-actions">${cardActionsHtml(job)}</div>`
    : `<div class="job-card-actions"></div>`;
  const metaLabel = captchaBlocked ? "Manual apply on - Captcha" : `Posted ${postedLabel}`;
  const showAppliedOn = IS_APPLICATIONS_PAGE && job.applied && job.applied_at;
  const appliedOnHtml = showAppliedOn
    ? `<span class="job-card-applied-on">Applied on ${escapeHtml(formatDate(job.applied_at))}</span>`
    : "";
  const meta = `
    <div class="job-card-row job-card-meta">
      ${actions}
      <div class="job-card-meta-center">
        <span class="job-card-id">Job ID ${id}</span>
      </div>
      <div class="job-card-meta-right">
        ${appliedOnHtml}
        <span class="job-card-posted-badge">${escapeHtml(metaLabel)}</span>
      </div>
    </div>`;

  return `<div class="job-card" data-job-id="${id}"><div class="job-card-inner">${header}${desc}${links}${contacts}${meta}</div></div>`;
}

function cardActionsHtml(job) {
  if (SHOW_CONTACT_ACTION) {
    return contactActionHtml(job);
  }
  return `${applyActionHtml(job)}${resumeActionHtml(job)}`;
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
  if (column.field === "id") {
    return `<td data-field="id">${escapeHtml(job.id)}</td>`;
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
  // Hover-preview of the apply screenshot is reserved for the success state only.
  if (job.applied) {
    const previewClass = job.application_screenshot_url ? " has-preview" : "";
    const previewData = job.application_screenshot_url
      ? ` data-preview-url="${escapeHtml(job.application_screenshot_url)}"`
      : "";
    return `<div class="apply-action-wrap${previewClass}"${previewData}><button type="button" class="apply-job-button applied" disabled data-job-id="${escapeHtml(job.id)}"><span class="apply-check" aria-hidden="true">✓</span>Applied</button></div>`;
  }
  if (activeApplyRuns.has(String(job.id))) {
    return `<div class="apply-action-wrap"><button type="button" class="apply-job-button running" disabled data-job-id="${escapeHtml(job.id)}">Applying...</button></div>`;
  }
  if (activeApplyRuns.size > 0) {
    return `<div class="apply-action-wrap"><button type="button" class="apply-job-button blocked" disabled data-job-id="${escapeHtml(job.id)}">Wait</button></div>`;
  }
  const actionsBlocked = areBestMatchesCardActionsBlocked();
  if (isManualApplyOnly(job)) {
    const title = job.application_error ? ` title="${escapeHtml(job.application_error)}"` : "";
    return `<div class="apply-action-wrap"><button type="button" class="apply-job-button${actionsBlocked ? " blocked" : ""}" ${actionsBlocked ? "disabled" : ""} data-job-id="${escapeHtml(job.id)}" data-manual-only="true"${title}>Manual Apply</button></div>`;
  }
  if (!job.resume_url) {
    return "";
  }
  if (!job.apply_url) {
    return `<div class="apply-action-wrap"><button type="button" class="apply-job-button blocked" disabled data-job-id="${escapeHtml(job.id)}">No Apply URL</button></div>`;
  }
  const label = job.application_status === "failed" ? "Retry Apply for Job" : "Apply for Job";
  const title = job.application_error ? ` title="${escapeHtml(job.application_error)}"` : "";
  return `<div class="apply-action-wrap"><button type="button" class="apply-job-button${actionsBlocked ? " blocked" : ""}" ${actionsBlocked ? "disabled" : ""} data-job-id="${escapeHtml(job.id)}"${title}>${label}</button></div>`;
}

function resumeActionHtml(job) {
  if (activeResumeRuns.has(String(job.id))) {
    return '<button type="button" class="resume-action-button running" disabled>Generating...</button>';
  }
  const actionsBlocked = areBestMatchesCardActionsBlocked();
  if (!job.resume_url && activeResumeRuns.size > 0) {
    return '<button type="button" class="resume-action-button blocked" disabled>Wait</button>';
  }
  if (!job.resume_url) {
    return `<button type="button" class="resume-action-button generate-resume-button${actionsBlocked ? " blocked" : ""}" ${actionsBlocked ? "disabled" : ""} data-job-id="${escapeHtml(job.id)}">AI Generate Resume</button>`;
  }
  if (actionsBlocked) {
    return '<button type="button" class="resume-action-button blocked" disabled>View AI Resume</button>';
  }
  return `<a href="${escapeHtml(job.resume_url)}" target="_blank" rel="noopener" class="resume-action-button">View AI Resume</a>`;
}

function contactActionHtml(job) {
  if (!SHOW_CONTACT_ACTION) return "";
  const jobId = String(job.id || "");
  const hasContacts = Array.isArray(job.contacts) && job.contacts.length > 0;
  if (activeContactLookups.has(jobId)) {
    return `<button type="button" class="resume-action-button running contact-action-button" disabled data-job-id="${escapeHtml(jobId)}">Finding Contacts...</button>`;
  }
  if (!ANYMAILFINDER_CONFIGURED) {
    return `<button type="button" class="resume-action-button blocked contact-action-button" disabled data-job-id="${escapeHtml(jobId)}" title="Add your Anymailfinder API key in Setup first.">Find Job Contacts</button>`;
  }
  if (hasContacts) {
    // Once any contact for this job has been emailed, hide the Send
    // button entirely — the per-contact "Email Sent at …" links are the
    // record of completion. No need to offer another batch send.
    const anySent = job.contacts.some((c) => c && c.email_sent === true);
    if (anySent) return "";
    if (!GMAIL_CONFIGURED) {
      return `<button type="button" class="resume-action-button blocked email-contacts-button" disabled data-job-id="${escapeHtml(jobId)}" title="Connect Google in Setup before sending email.">Send Contacts Email</button>`;
    }
    return `<button type="button" class="resume-action-button email-contacts-button" data-job-id="${escapeHtml(jobId)}">Send Contacts Email</button>`;
  }
  return `<button type="button" class="resume-action-button contact-action-button" data-job-id="${escapeHtml(jobId)}">Find Job Contacts</button>`;
}

function formatSentAtLabel(iso) {
  if (!iso) return "Email Sent";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "Email Sent";
  const time = d.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
  const date = d.toLocaleDateString([], { month: "short", day: "numeric" });
  return `Email Sent at ${date} ${time}`;
}

function contactListHtml(job) {
  const contacts = Array.isArray(job.contacts) ? job.contacts : [];
  if (!contacts.length) return "";

  return `
    <div class="job-card-row job-contact-row">
      <div class="job-contact-list">
        ${contacts.map((contact) => contactItemHtml(job.id, contact)).join("")}
      </div>
    </div>`;
}

function contactItemHtml(jobId, contact) {
  const contactId = escapeHtml(contact.id || "");
  const resolved = contact.resolved === true || Boolean(contact.email);
  const fallbackName = contact.decision_maker_category_label
    ? `${contact.decision_maker_category_label} contact not found`
    : "No contact found";
  const name = escapeHtml(contact.person_name || fallbackName);
  // Unresolved contacts have no email \u2014 drop the em-dash placeholder entirely
  // so the meta row collapses cleanly instead of showing a stray dash.
  const email = contact.email
    ? `<a href="mailto:${escapeHtml(contact.email)}" class="job-contact-link">${escapeHtml(contact.email)}</a>`
    : "";
  const title = contact.position ? `<span class="job-contact-title">${escapeHtml(contact.position)}</span>` : "";
  const category = contact.decision_maker_category_label
    ? `<span class="job-contact-pill">${escapeHtml(contact.decision_maker_category_label)}</span>`
    : "";
  const linkedin = contact.linkedin
    ? `<a href="${escapeHtml(contact.linkedin)}" target="_blank" rel="noopener" class="job-contact-link">LinkedIn</a>`
    : "";
  const status = contact.email_status && contact.email_status !== "valid"
    ? `<span class="job-contact-status" data-status="${escapeHtml(contact.email_status)}">${escapeHtml(String(contact.email_status).replaceAll("_", " "))}</span>`
    : "";

  const emailSent = contact.email_sent === true;
  // Unresolved contacts can't be selected for outreach, so don't render a
  // checkbox at all (previously rendered as disabled, which read as broken).
  const checkboxOrCheck = emailSent
    ? `<span class="job-contact-sent-check" aria-label="Email sent">✓</span>`
    : (resolved
        ? `<label class="job-contact-checkbox">
            <input
              type="checkbox"
              class="job-contact-select"
              data-job-id="${escapeHtml(jobId)}"
              data-contact-id="${contactId}"
              ${contact.selected ? "checked" : ""}
            />
          </label>`
        : "");
  const sentLink = emailSent
    ? `<button type="button" class="job-contact-sent-link email-sent-link" data-job-id="${escapeHtml(jobId)}" data-contact-id="${contactId}">${escapeHtml(formatSentAtLabel(contact.email_sent_at))}</button>`
    : "";

  return `
    <div class="job-contact-item${resolved ? "" : " is-unresolved"}${emailSent ? " is-sent" : ""}">
      ${checkboxOrCheck}
      <span class="job-contact-body">
        <span class="job-contact-header">
          <span class="job-contact-name">${name}</span>
          ${category}
          ${status}
          ${sentLink}
        </span>
        <span class="job-contact-meta">
          ${title}
          ${email}
          ${linkedin}
        </span>
      </span>
    </div>`;
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
    hideApplyChoiceModal();
    hideManualOutcomeModal();
    hideAutoScoreModal();
  }
}

function setPageRunStatusVisibility(visible) {
  const section = document.getElementById("page-agent-status-section");
  if (section) section.hidden = !visible;
}

function setPageRunCancelButtonState(isVisible, cancelRequested = false) {
  const button = document.getElementById("cancel-page-agent-button");
  if (!button) return;
  button.hidden = !isVisible;
  button.disabled = cancelRequested;
  button.textContent = cancelRequested ? "Stopping..." : "Cancel Agent";
}

function stepStatusPriority(status) {
  if (status === "running" || status === "queued") return 0;
  if (status === "pending") return 1;
  if (status === "completed" || status === "succeeded" || status === "cancelled" || status === "failed") return 2;
  return 1;
}

function sortStepsForDisplay(steps) {
  return [...steps]
    .map((step, index) => ({ step, index }))
    .sort((left, right) => {
      const priorityDelta = stepStatusPriority(left.step.status || "pending") - stepStatusPriority(right.step.status || "pending");
      if (priorityDelta !== 0) return priorityDelta;
      return left.index - right.index;
    })
    .map(({ step }) => step);
}

function formatRunDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return date.toLocaleString();
}

function pageRunStatusLevel(status) {
  if (status === "failed") return "error";
  if (status === "succeeded") return "success";
  return "info";
}

function activePageRunDetail(agent) {
  const a = agent || activePageRunAgent || PAGE_RUN_AGENT;
  if (a === "job_intake") return "LinkedIn scraping is in progress.";
  if (a === "job_scoring") return "Job scoring is in progress.";
  if (a === "resume_generation") return "Generating tailored resumes for jobs above the threshold.";
  return "Agent execution is in progress.";
}

function activePageRunStepMessage(step) {
  const message =
    PAGE_RUN_AGENT === "job_scoring" &&
    step &&
    step.name === "Score jobs" &&
    typeof step.previous_message === "string" &&
    step.previous_message
      ? step.previous_message
      : step?.message || "";

  if (PAGE_RUN_AGENT === "job_scoring") {
    return formatScoringMessage(message);
  }

  return message;
}

function renderPageRunStatus(run) {
  // Accept either the page's default agent (PAGE_RUN_AGENT) or whatever
  // agent we've explicitly started/restored as the active page run. On Best
  // Matches, that lets resume_generation runs (from Auto Generate Resumes)
  // drive the same status block as job_scoring runs.
  const expectedAgent = activePageRunAgent || PAGE_RUN_AGENT;
  if (!run || run.agent_name !== expectedAgent) {
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    return;
  }

  const isActive = ["queued", "running"].includes(run.status);
  if (!isActive) {
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    return;
  }

  const box = document.getElementById("page-agent-status");
  const heading = document.getElementById("page-agent-status-heading");
  const detail = document.getElementById("page-agent-status-message");
  const metaNode = document.getElementById("page-agent-status-meta");
  const stepsNode = document.getElementById("page-agent-status-steps");
  const indicator = document.getElementById("page-agent-status-indicator");
  if (!box || !heading || !detail || !metaNode || !stepsNode || !indicator) return;

  const runAgent = run.agent_name;
  const runLabel = runLabelForAgent(runAgent);
  box.dataset.level = pageRunStatusLevel(run.status);
  heading.textContent = runAgent === "job_scoring"
    ? formatScoringMessage(run.message || `${runLabel} is running.`)
    : run.message || `${runLabel} is running.`;
  const metaParts = [];
  if (run.started_at) metaParts.push(`Started ${formatRunDateTime(run.started_at)}`);
  metaNode.textContent = metaParts.join(" · ");
  indicator.hidden = false;
  setPageRunStatusVisibility(true);
  setPageRunCancelButtonState(true, Boolean(run.cancel_requested));

  const steps = sortStepsForDisplay(run.steps || []);
  const detailMessage =
    runAgent === "job_scoring"
      ? ""
      : activePageRunDetail(runAgent);
  detail.hidden = !detailMessage;
  detail.textContent = detailMessage;
  if (!steps.length) {
    stepsNode.innerHTML = `<li class="step-list-empty">Steps will appear here while the agent is running.</li>`;
    return;
  }

  stepsNode.innerHTML = steps
    .map(
      (step) => `
        <li class="step-item" data-status="${escapeHtml(step.status || "pending")}">
          <div class="step-item-row">
            <span class="step-name">${escapeHtml(step.name || "Unnamed step")}</span>
            <span class="step-status-chip" data-status="${escapeHtml(step.status || "pending")}">
              ${escapeHtml(step.status || "pending")}
            </span>
          </div>
          <div class="step-message">${escapeHtml(activePageRunStepMessage(step))}</div>
        </li>
      `,
    )
    .join("");
}

function setPageRunParam(runId) {
  const url = new URL(window.location.href);
  if (runId) {
    url.searchParams.set("run", runId);
  } else {
    url.searchParams.delete("run");
  }
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function setAutoScoreToggleState(enabled) {
  const input = document.getElementById("auto-score-toggle");
  if (!input) return;
  input.checked = Boolean(enabled);
}

function setAutoScoreToggleDisabled(disabled) {
  const input = document.getElementById("auto-score-toggle");
  if (!input) return;
  const wrapper = input.closest(".schedule-toggle");
  input.disabled = Boolean(disabled);
  if (wrapper) wrapper.classList.toggle("is-disabled", Boolean(disabled));
}

function showAutoScoreModal(nextEnabled) {
  const modal = document.getElementById("auto-score-modal");
  const title = document.getElementById("auto-score-modal-title");
  const message = document.getElementById("auto-score-modal-message");
  const confirm = document.getElementById("auto-score-modal-confirm");
  if (!modal || !title || !message || !confirm) return;

  pendingAutoScoreEnabled = Boolean(nextEnabled);
  title.textContent = nextEnabled ? "Enable AI Auto Score?" : "Turn Off AI Auto Score?";
  message.textContent = nextEnabled
    ? "Turning this on will automatically trigger the Scoring Agent whenever unscored jobs are available on Best Job Matches."
    : "Turning this off will stop the Scoring Agent from automatically triggering.";
  confirm.textContent = nextEnabled ? "Enable Auto Score" : "Turn Off Auto Score";
  modal.hidden = false;
}

function hideAutoScoreModal() {
  const modal = document.getElementById("auto-score-modal");
  if (modal) modal.hidden = true;
  pendingAutoScoreEnabled = null;
  setAutoScoreToggleState(autoScoreEnabled);
}

// ===== Auto Generate Resumes =====

function setAutoGenerateResumesToggleState(enabled) {
  const input = document.getElementById("auto-generate-resumes-toggle");
  if (!input) return;
  input.checked = Boolean(enabled);
}

function setAutoGenerateResumesToggleDisabled(disabled) {
  const input = document.getElementById("auto-generate-resumes-toggle");
  if (!input) return;
  input.disabled = Boolean(disabled);
}

function showAutoGenerateResumesModal(nextEnabled) {
  const modal = document.getElementById("auto-generate-resumes-modal");
  const title = document.getElementById("auto-generate-resumes-modal-title");
  const message = document.getElementById("auto-generate-resumes-modal-message");
  const confirm = document.getElementById("auto-generate-resumes-modal-confirm");
  if (!modal || !title || !message || !confirm) return;

  pendingAutoGenerateResumesEnabled = Boolean(nextEnabled);
  title.textContent = nextEnabled ? "Auto Generate Resumes?" : "Turn Off Auto Generate Resumes?";
  message.textContent = nextEnabled
    ? "Tailored resumes will be generated automatically for jobs that clear the threshold."
    : "Stop generating resumes automatically after scoring?";
  confirm.textContent = nextEnabled ? "Enable" : "Turn Off";
  modal.hidden = false;
}

function hideAutoGenerateResumesModal() {
  const modal = document.getElementById("auto-generate-resumes-modal");
  if (modal) modal.hidden = true;
  pendingAutoGenerateResumesEnabled = null;
  setAutoGenerateResumesToggleState(autoGenerateResumesEnabled);
}

async function persistAutoGenerateResumesEnabled(enabled) {
  const response = await callJson("/jobs/auto-generate-resumes", "PUT", { enabled: Boolean(enabled) });
  autoGenerateResumesEnabled = Boolean(response.auto_generate_resumes_enabled);
  setAutoGenerateResumesToggleState(autoGenerateResumesEnabled);
  return autoGenerateResumesEnabled;
}

function eligibleResumeJobIds() {
  return jobsData
    .filter((job) => {
      if (!job) return false;
      if (job.resume_url) return false;
      if (job.applied) return false;
      if (job.application_status) return false;
      if (job.score == null) return false;
      if (Number(job.score) < currentScoreThreshold) return false;
      return true;
    })
    .map((job) => String(job.id));
}

function clearThresholdSettlingTimer() {
  if (thresholdSettlingTimer !== null) {
    window.clearTimeout(thresholdSettlingTimer);
    thresholdSettlingTimer = null;
  }
}

function armThresholdSettlingTimer() {
  if (!autoGenerateResumesEnabled || !CAN_RUN_SCORING) return;
  clearThresholdSettlingTimer();
  thresholdSettlingTimer = window.setTimeout(() => {
    thresholdSettlingTimer = null;
    void maybeStartAutoResume();
  }, THRESHOLD_SETTLING_MS);
}

function setPageRunStatusLabel(text) {
  const label = document.getElementById("page-agent-status-label");
  if (label) label.textContent = text || "";
}

function runLabelForAgent(agent) {
  if (agent === "resume_generation") return "Resume Agent";
  if (agent === "job_scoring") return "Scoring Agent";
  if (agent === "job_intake") return "Jobs Agent";
  return PAGE_RUN_LABEL || "Agent Status";
}

async function maybeStartAutoResume() {
  if (!autoGenerateResumesEnabled || !CAN_RUN_SCORING) return;
  if (activePageRunId || autoResumeRunInFlight || pageRunStarting) return;
  if (activeResumeRuns.size > 0) return;
  const jobIds = eligibleResumeJobIds();
  if (!jobIds.length) return;
  await startAutoResumeRun(jobIds);
}

async function startAutoResumeRun(jobIds) {
  if (!Array.isArray(jobIds) || !jobIds.length) return;
  if (activePageRunId || autoResumeRunInFlight || pageRunStarting) return;
  autoResumeRunInFlight = true;
  pageRunStarting = true;
  updatePageRunButtonState();
  try {
    const run = await callJson("/resumes/generate/start", "POST", { limit: jobIds.length, job_ids: jobIds });
    pageRunStarting = false;
    activePageRunId = String(run.id || "");
    activePageRunAgent = "resume_generation";
    setPageRunParam(activePageRunId);
    setPageRunStatusLabel(runLabelForAgent("resume_generation"));
    renderPageRunStatus(run);
    updatePageRunButtonState();
    await pollPageRun(activePageRunId);
  } catch (err) {
    pageRunStarting = false;
    autoResumeRunInFlight = false;
    activePageRunId = null;
    activePageRunAgent = "";
    setPageRunParam(null);
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    setPageRunStatusLabel(PAGE_RUN_LABEL);
    updatePageRunButtonState();
    window.alert(`Failed to start Resume Agent: ${err.message}`);
  }
}

let pendingAutoFindContactsEnabled = null;

function setAutoFindContactsToggleState(enabled) {
  const input = document.getElementById("auto-find-contacts-toggle");
  if (!input) return;
  input.checked = Boolean(enabled);
}

function showAutoFindContactsModal(nextEnabled) {
  const modal = document.getElementById("auto-find-contacts-modal");
  const title = document.getElementById("auto-find-contacts-modal-title");
  const message = document.getElementById("auto-find-contacts-modal-message");
  const confirm = document.getElementById("auto-find-contacts-modal-confirm");
  if (!modal || !title || !message || !confirm) return;

  pendingAutoFindContactsEnabled = Boolean(nextEnabled);
  title.textContent = nextEnabled ? "Enable Auto Find Contacts?" : "Turn Off Auto Find Contacts?";
  message.textContent = nextEnabled
    ? "Enabling this feature will automatically find and display contacts for each job after successfully applying for the job."
    : "Turning this off will stop the App from automatically finding contacts after successful applies.";
  confirm.textContent = nextEnabled ? "Enable Auto Find Contacts" : "Turn Off Auto Find Contacts";
  modal.hidden = false;
}

function hideAutoFindContactsModal() {
  const modal = document.getElementById("auto-find-contacts-modal");
  if (modal) modal.hidden = true;
  pendingAutoFindContactsEnabled = null;
  setAutoFindContactsToggleState(autoFindContactsEnabled);
}

// ===== Email Contacts flow =====

let pendingEmailJobId = null;
let pendingEmailContactIds = [];
let pendingEmailMode = null;
let emailSendInFlight = false;

function selectedContactsForJob(job) {
  const contacts = Array.isArray(job?.contacts) ? job.contacts : [];
  return contacts.filter((c) => c.selected && c.email && !c.email_sent);
}

function showEmailChoiceModal() {
  const modal = document.getElementById("email-choice-modal");
  if (modal) modal.hidden = false;
}

function hideEmailChoiceModal() {
  const modal = document.getElementById("email-choice-modal");
  if (modal) modal.hidden = true;
}

function showEmailEditModal({ subject, body, bccSelf, recipients, loading = false, contactCount = 0 }) {
  const modal = document.getElementById("email-edit-modal");
  const card = document.getElementById("email-edit-modal-card");
  const subjectInput = document.getElementById("email-edit-subject");
  const bodyInput = document.getElementById("email-edit-body");
  const bccInput = document.getElementById("email-edit-bcc");
  const recipientsLine = document.getElementById("email-edit-recipients");
  const helperLine = document.getElementById("email-edit-helper");
  const errorBox = document.getElementById("email-edit-error");
  const sendBtn = document.getElementById("email-edit-send");
  if (!modal || !card || !subjectInput || !bodyInput || !bccInput || !recipientsLine) return;
  subjectInput.value = subject || "";
  bodyInput.value = body || "";
  bccInput.checked = Boolean(bccSelf);
  recipientsLine.textContent = recipients || "";
  // Loading state: spinner overlay covers the card AND inputs are disabled
  // so the user cannot type into a body that's about to be replaced by AI.
  if (loading) {
    card.classList.add("is-loading");
    subjectInput.disabled = true;
    bodyInput.disabled = true;
    bccInput.disabled = true;
  } else {
    card.classList.remove("is-loading");
    subjectInput.disabled = false;
    bodyInput.disabled = false;
    bccInput.disabled = false;
  }
  // Helper text: when only one contact is selected we already substituted
  // their name/title server-side, so no placeholders appear in the body and
  // the placeholder hint is irrelevant. Only mention the resume link.
  if (helperLine) {
    if (contactCount === 1) {
      helperLine.innerHTML = "The resume link is appended automatically.";
    } else {
      helperLine.innerHTML =
        '<code>{name}</code> and <code>{title}</code> are filled in automatically with each contact’s name and title when the email is sent — you don’t need to edit them. The resume link is appended automatically.';
    }
  }
  if (errorBox) {
    errorBox.hidden = true;
    errorBox.textContent = "";
  }
  if (sendBtn) sendBtn.disabled = Boolean(loading);
  modal.hidden = false;
}

function hideEmailEditModal() {
  const modal = document.getElementById("email-edit-modal");
  if (modal) modal.hidden = true;
  pendingEmailJobId = null;
  pendingEmailContactIds = [];
  pendingEmailMode = null;
  emailSendInFlight = false;
}

function setEmailEditError(message) {
  const errorBox = document.getElementById("email-edit-error");
  const sendBtn = document.getElementById("email-edit-send");
  const card = document.getElementById("email-edit-modal-card");
  const subjectInput = document.getElementById("email-edit-subject");
  const bodyInput = document.getElementById("email-edit-body");
  const bccInput = document.getElementById("email-edit-bcc");
  if (errorBox) {
    errorBox.textContent = message || "";
    errorBox.hidden = !message;
  }
  if (card) card.classList.remove("is-loading");
  if (subjectInput) subjectInput.disabled = false;
  if (bodyInput) bodyInput.disabled = false;
  if (bccInput) bccInput.disabled = false;
  if (sendBtn) sendBtn.disabled = false;
}

function describeRecipients(contacts) {
  if (!contacts.length) return "No recipients selected.";
  const names = contacts.map((c) => c.person_name || c.email).filter(Boolean);
  const summary = names.length <= 3 ? names.join(", ") : `${names.slice(0, 3).join(", ")} and ${names.length - 3} more`;
  const noun = contacts.length === 1 ? "contact" : "contacts";
  return `Sending separately to ${contacts.length} ${noun}: ${summary}`;
}

async function startEmailFlowForJob(jobId) {
  const job = currentJobById(jobId);
  if (!job) return;
  if (!GMAIL_CONFIGURED) {
    window.alert("Connect Google in Setup before sending email.");
    return;
  }
  const selected = selectedContactsForJob(job);
  if (!selected.length) {
    window.alert("Select at least one contact with an email address before sending.");
    return;
  }
  pendingEmailJobId = String(jobId);
  pendingEmailContactIds = selected.map((c) => String(c.id));
  pendingEmailMode = null;
  showEmailChoiceModal();
}

async function continueEmailFlow(mode) {
  pendingEmailMode = mode;
  const jobId = pendingEmailJobId;
  const contactIds = [...pendingEmailContactIds];
  hideEmailChoiceModal();
  if (!jobId || !contactIds.length) return;
  const job = currentJobById(jobId);
  const contacts = (job?.contacts || []).filter((c) => contactIds.includes(String(c.id)));
  const recipientsLabel = describeRecipients(contacts);

  showEmailEditModal({
    subject: "",
    body: "",
    bccSelf: false,
    recipients: recipientsLabel,
    loading: mode === "ai",
    contactCount: contacts.length,
  });

  try {
    const response = await callJson(
      `/interviews/${encodeURIComponent(jobId)}/contacts/email/preview`,
      "POST",
      { mode, contact_ids: contactIds },
    );
    showEmailEditModal({
      subject: response.subject || "",
      body: response.body || "",
      bccSelf: Boolean(response.bcc_self),
      recipients: recipientsLabel,
      loading: false,
      contactCount: contacts.length,
    });
  } catch (err) {
    setEmailEditError(err.message || "Failed to load email content.");
  }
}

async function submitEmailSend() {
  if (emailSendInFlight) return;
  const jobId = pendingEmailJobId;
  const contactIds = [...pendingEmailContactIds];
  if (!jobId || !contactIds.length) {
    hideEmailEditModal();
    return;
  }
  const subject = document.getElementById("email-edit-subject").value.trim();
  const body = document.getElementById("email-edit-body").value.trim();
  const bccSelf = document.getElementById("email-edit-bcc").checked;
  if (!subject) {
    setEmailEditError("Subject is required.");
    return;
  }
  if (!body) {
    setEmailEditError("Body is required.");
    return;
  }

  emailSendInFlight = true;
  const sendBtn = document.getElementById("email-edit-send");
  if (sendBtn) sendBtn.disabled = true;
  setEmailEditError("");

  try {
    const response = await callJson(
      `/interviews/${encodeURIComponent(jobId)}/contacts/email/send`,
      "POST",
      { contact_ids: contactIds, subject, body, bcc_self: bccSelf },
    );
    const results = Array.isArray(response.results) ? response.results : [];
    applyEmailSendResults(jobId, results);
    const failed = results.filter((r) => !r.ok);
    hideEmailEditModal();
    if (failed.length) {
      const messages = failed
        .map((r) => `• ${r.contact_id}: ${r.error || "Unknown error"}`)
        .join("\n");
      window.alert(`Some emails failed to send:\n${messages}`);
    }
  } catch (err) {
    setEmailEditError(err.message || "Failed to send email.");
  } finally {
    emailSendInFlight = false;
    if (sendBtn) sendBtn.disabled = false;
  }
}

function applyEmailSendResults(jobId, results) {
  const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
  if (index === -1) return;
  const job = jobsData[index];
  const updatedContacts = (job.contacts || []).map((contact) => {
    const match = results.find((r) => r.ok && r.contact && String(r.contact.id) === String(contact.id));
    return match ? { ...contact, ...match.contact } : contact;
  });
  jobsData[index] = { ...job, contacts: updatedContacts };
  renderView(filteredJobs());
}

async function openSentEmailViewer(jobId, contactId) {
  try {
    const response = await callJson(
      `/interviews/${encodeURIComponent(jobId)}/contacts/${encodeURIComponent(contactId)}/email`,
      "GET",
    );
    const modal = document.getElementById("email-view-modal");
    const toEl = document.getElementById("email-view-to");
    const subjectEl = document.getElementById("email-view-subject");
    const sentAtEl = document.getElementById("email-view-sent-at");
    const bccEl = document.getElementById("email-view-bcc");
    const bccLabel = document.getElementById("email-view-bcc-label");
    const bodyEl = document.getElementById("email-view-body");
    if (!modal || !toEl || !subjectEl || !sentAtEl || !bodyEl) return;
    const recipientLabel = response.person_name
      ? `${response.person_name} <${response.to || ""}>`
      : response.to || "";
    toEl.textContent = recipientLabel;
    subjectEl.textContent = response.subject || "";
    sentAtEl.textContent = response.sent_at ? new Date(response.sent_at).toLocaleString() : "";
    if (response.bcc) {
      bccLabel.hidden = false;
      bccEl.hidden = false;
      bccEl.textContent = response.bcc;
    } else {
      bccLabel.hidden = true;
      bccEl.hidden = true;
      bccEl.textContent = "";
    }
    bodyEl.textContent = response.body || "";
    modal.hidden = false;
  } catch (err) {
    window.alert(`Failed to load sent email: ${err.message}`);
  }
}

function hideSentEmailViewer() {
  const modal = document.getElementById("email-view-modal");
  if (modal) modal.hidden = true;
}

function pageRunIdFromQuery() {
  const url = new URL(window.location.href);
  return url.searchParams.get("run");
}

function clearAutoApplyParam() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("auto_apply")) return;
  url.searchParams.delete("auto_apply");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

function clearManualApplyParam() {
  const url = new URL(window.location.href);
  if (!url.searchParams.has("manual_apply")) return;
  url.searchParams.delete("manual_apply");
  window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
}

async function restoreActivePageRun() {
  if (!PAGE_RUN_AGENT) return;
  // On the Best Matches page (CAN_RUN_SCORING) we also restore an active
  // resume_generation run since it may be the auto-generated-resumes flow
  // started in a prior session.
  const acceptableAgents = CAN_RUN_SCORING ? [PAGE_RUN_AGENT, "resume_generation"] : [PAGE_RUN_AGENT];
  const matches = (run) => acceptableAgents.includes(run.agent_name) && ["queued", "running"].includes(run.status) && !run.stale;

  const requestedRunId = pageRunIdFromQuery();
  if (requestedRunId) {
    try {
      const run = await callJson(`/runs/${requestedRunId}`, "GET");
      if (matches(run)) {
        activePageRunId = String(run.id);
        activePageRunAgent = run.agent_name;
        if (run.agent_name === "resume_generation") autoResumeRunInFlight = true;
        setPageRunStatusLabel(runLabelForAgent(run.agent_name));
        renderPageRunStatus(run);
        updatePageRunButtonState();
        void pollPageRun(activePageRunId);
        return;
      }
    } catch {
    }
    setPageRunParam(null);
  }

  try {
    const payload = await callJson("/runs/", "GET");
    const activeRun = (payload.runs || []).find(matches);
    if (!activeRun) {
      setPageRunStatusVisibility(false);
      setPageRunCancelButtonState(false);
      updatePageRunButtonState();
      return;
    }

    activePageRunId = String(activeRun.id || "");
    activePageRunAgent = activeRun.agent_name;
    if (activeRun.agent_name === "resume_generation") autoResumeRunInFlight = true;
    setPageRunParam(activePageRunId);
    setPageRunStatusLabel(runLabelForAgent(activeRun.agent_name));
    renderPageRunStatus(activeRun);
    updatePageRunButtonState();
    void pollPageRun(activePageRunId);
  } catch (err) {
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    updatePageRunButtonState();
    window.alert(`Failed to restore ${PAGE_RUN_LABEL}: ${err.message}`);
  }
}

async function startApplyForJob(jobId, mode = "ai") {
  if (!jobId || activeApplyRuns.size > 0 || areBestMatchesCardActionsBlocked()) return;
  activeApplyRuns.set(String(jobId), "");
  renderView(filteredJobs());

  try {
    const run = await callJson("/apply/start", "POST", { limit: 1, job_ids: [String(jobId)], mode });
    activeApplyRuns.set(String(jobId), String(run.id || ""));
    await pollApplyRun(String(jobId), String(run.id || ""));
  } catch (err) {
    activeApplyRuns.delete(String(jobId));
    renderView(filteredJobs());
    window.alert(`Failed to start Apply Agent: ${err.message}`);
  }
}

function currentJobById(jobId) {
  return jobsData.find((job) => String(job.id) === String(jobId)) || null;
}

function replaceJobContacts(jobId, contacts) {
  const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
  if (index < 0) return;
  jobsData[index] = {
    ...jobsData[index],
    contacts: Array.isArray(contacts) ? contacts : [],
  };
}

function replaceJobContact(jobId, contact) {
  const job = currentJobById(jobId);
  if (!job || !contact) return;
  const existing = Array.isArray(job.contacts) ? job.contacts : [];
  const nextContacts = existing.map((entry) =>
    String(entry.id) === String(contact.id) ? { ...entry, ...contact } : entry,
  );
  replaceJobContacts(jobId, nextContacts);
}

function isCaptchaBlocked(job) {
  if (!job) return false;
  const status = String(job.application_status || "").toLowerCase();
  if (status === "captcha") return true;
  const error = String(job.application_error || "").toLowerCase();
  return error.includes("captcha") || error.includes("hcaptcha") || error.includes("recaptcha");
}

function isManualApplyOnly(job) {
  if (!job) return false;
  const status = String(job.application_status || "").toLowerCase();
  return status === "manual_started" || status === "manual_closed" || isCaptchaBlocked(job);
}

async function moveJobToApplications(jobId, source) {
  if (!USE_SCORE_THRESHOLD_FILTER) {
    return currentJobById(jobId);
  }

  const response = await callJson(`/jobs/${jobId}/move-to-applications`, "POST", { source });
  const updatedJob = response.job || null;
  const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
  if (index >= 0 && updatedJob) {
    jobsData[index] = { ...jobsData[index], ...updatedJob };
  }
  return updatedJob;
}

function removeJobFromBestMatches(jobId, rerender = true) {
  if (!USE_SCORE_THRESHOLD_FILTER) return;
  const targetJobId = String(jobId || "");
  expandedJobDescriptions.delete(targetJobId);
  jobsData = jobsData.filter((job) => String(job.id) !== targetJobId);
  if (rerender) {
    renderView(filteredJobs());
  }
}

function showApplyChoiceModal(jobId) {
  const modal = document.getElementById("apply-choice-modal");
  if (!modal) {
    void startApplyForJob(jobId);
    return;
  }
  pendingApplyChoiceJobId = String(jobId || "");
  modal.hidden = false;
}

function hideApplyChoiceModal() {
  const modal = document.getElementById("apply-choice-modal");
  if (modal) modal.hidden = true;
  pendingApplyChoiceJobId = null;
}

async function startManualApply(jobId) {
  const job = currentJobById(jobId);
  const targetJobId = String(jobId || "");
  if (!job?.apply_url || !targetJobId) {
    window.alert("This job does not have an apply URL.");
    return;
  }
  if (IS_APPLICATIONS_PAGE) {
    void startApplyForJob(targetJobId, "manual");
    return;
  }
  if (USE_SCORE_THRESHOLD_FILTER) {
    const movePromise = moveJobToApplications(targetJobId, "manual");
    await flyCardToTab(targetJobId, '.app-tab[href="/applications/"]');
    try {
      await movePromise;
    } catch (err) {
      renderView(filteredJobs());
      window.alert(`Failed to move job to Applications: ${err.message}`);
      return;
    }
    removeJobFromBestMatches(targetJobId, false);
  }

  const url = new URL("/applications/", window.location.origin);
  url.searchParams.set("job_id", targetJobId);
  url.searchParams.set("manual_apply", "1");
  window.location.assign(`${url.pathname}${url.search}`);
}

async function startAiApply(jobId) {
  const targetJobId = String(jobId || "");
  if (!targetJobId) return;
  if (IS_APPLICATIONS_PAGE) {
    void startApplyForJob(targetJobId, "ai");
    return;
  }
  const url = new URL("/applications/", window.location.origin);
  url.searchParams.set("job_id", targetJobId);
  url.searchParams.set("auto_apply", "1");
  window.location.assign(`${url.pathname}${url.search}`);
}

// Animate the source job card flying up into a target top-nav tab, then
// resolve. Caller is expected to navigate or refresh after the promise
// settles. tabSelector is a CSS selector for the destination .app-tab.
function flyCardToTab(jobId, tabSelector) {
  return new Promise((resolve) => {
    const reduced = window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    if (reduced || !window.gsap) { resolve(); return; }

    const card = document.querySelector(`.job-card[data-job-id="${jobId}"]`);
    const target = document.querySelector(tabSelector);
    if (!card || !target) { resolve(); return; }

    const cardRect = card.getBoundingClientRect();
    const targetRect = target.getBoundingClientRect();

    // Clone so the in-flight card sits over a fixed layer; the original
    // collapses underneath so the list reflows and the gap closes up.
    const clone = card.cloneNode(true);
    Object.assign(clone.style, {
      position: "fixed",
      top: `${cardRect.top}px`,
      left: `${cardRect.left}px`,
      width: `${cardRect.width}px`,
      height: `${cardRect.height}px`,
      margin: "0",
      zIndex: "10000",
      pointerEvents: "none",
      transformOrigin: "center center",
      willChange: "transform, opacity",
    });
    document.body.appendChild(clone);

    // Hide the original immediately so the slot reads as empty as the clone
    // flies away, but keep the slot's height for a beat so the user sees the
    // gap before neighbours slide up.
    card.style.visibility = "hidden";
    card.style.overflow = "hidden";
    window.gsap.to(card, {
      height: 0,
      marginTop: 0,
      marginBottom: 0,
      paddingTop: 0,
      paddingBottom: 0,
      duration: 0.45,
      delay: 0.7,
      ease: "power2.inOut",
      onComplete: () => { card.remove(); },
    });

    const dx = (targetRect.left + targetRect.width / 2) - (cardRect.left + cardRect.width / 2);
    const dy = (targetRect.top + targetRect.height / 2) - (cardRect.top + cardRect.height / 2);

    const tl = window.gsap.timeline({
      onComplete: () => {
        clone.remove();
        // Brief pulse on the target tab to acknowledge the landing.
        window.gsap.fromTo(
          target,
          { scale: 1 },
          { scale: 1.08, duration: 0.16, yoyo: true, repeat: 1, ease: "power2.out", transformOrigin: "center center" }
        );
        setTimeout(resolve, 220);
      },
    });

    // power3.inOut: gentle takeoff, fast travel through the middle, slow
    // settle at the end. The slow end is critical — it lets the user
    // actually see the final shrink land inside the tab instead of the
    // card vanishing in a blur of acceleration. No separate opacity tween;
    // the scale does the disappearing.
    tl.to(clone, {
      x: dx,
      y: dy,
      scale: 0.05,
      borderRadius: "9999px",
      duration: 1.8,
      ease: "power3.inOut",
    }, 0);
  });
}

async function startResumeForJob(jobId) {
  if (!jobId || activeResumeRuns.size > 0 || areBestMatchesCardActionsBlocked()) return;
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

// Lightweight status helpers that drive the same DOM nodes as
// renderPageRunStatus, but for the Contact Finder flow which doesn't
// have a backing workflow_run record. The status label comes from
// pageRunLabel ("Contact Finder") set by the interviews route.
let contactFinderHideTimer = null;
const CONTACT_FINDER_HIDE_DELAY_MS = 4000;

function showContactFinderStatus(state, message) {
  // A new state update cancels any pending auto-hide so the running state
  // of the next lookup isn't dismissed mid-flight.
  if (contactFinderHideTimer !== null) {
    window.clearTimeout(contactFinderHideTimer);
    contactFinderHideTimer = null;
  }
  const section = document.getElementById("page-agent-status-section");
  const box = document.getElementById("page-agent-status");
  const heading = document.getElementById("page-agent-status-heading");
  const detail = document.getElementById("page-agent-status-message");
  const indicator = document.getElementById("page-agent-status-indicator");
  const stepsNode = document.getElementById("page-agent-status-steps");
  const cancelBtn = document.getElementById("cancel-page-agent-button");
  if (!section || !box || !heading || !detail || !indicator || !stepsNode) return;
  section.hidden = false;
  if (cancelBtn) cancelBtn.hidden = true;
  const level = state === "failed" ? "error" : state === "succeeded" ? "success" : "info";
  box.dataset.level = level;
  if (state === "running") {
    indicator.hidden = false;
    heading.textContent = "Looking up contacts...";
  } else if (state === "succeeded") {
    indicator.hidden = true;
    heading.textContent = "Contacts ready.";
  } else if (state === "failed") {
    indicator.hidden = true;
    heading.textContent = "Contact lookup failed.";
  }
  detail.textContent = message || "";
  stepsNode.innerHTML = "";

  // Auto-hide once the run has landed on a terminal state. The contact
  // list itself surfaces the result; the status block is just transient.
  if (state === "succeeded" || state === "failed") {
    contactFinderHideTimer = window.setTimeout(() => {
      contactFinderHideTimer = null;
      hideContactFinderStatus();
    }, CONTACT_FINDER_HIDE_DELAY_MS);
  }
}

function hideContactFinderStatus() {
  if (contactFinderHideTimer !== null) {
    window.clearTimeout(contactFinderHideTimer);
    contactFinderHideTimer = null;
  }
  const section = document.getElementById("page-agent-status-section");
  if (section) section.hidden = true;
}

async function findContactsForJob(jobId, options = {}) {
  const targetJobId = String(jobId || "");
  if (!targetJobId || !ANYMAILFINDER_CONFIGURED || activeContactLookups.has(targetJobId)) {
    return false;
  }
  // The Contact Finder status section only exists on the Emails/Interviews
  // page (gated by show_contact_action in the template). When called from
  // the auto-trigger on Applications, we silently drive the lookup.
  const showStatus = IS_EMAILS_INTERVIEWS_PAGE && options.showStatus !== false;

  activeContactLookups.set(targetJobId, true);
  renderView(filteredJobs());
  if (showStatus) {
    showContactFinderStatus("running", "Searching Anymailfinder for decision-maker contacts...");
  }
  try {
    const response = await callJson(`/interviews/${encodeURIComponent(targetJobId)}/contacts/find`, "POST");
    const contacts = Array.isArray(response.contacts) ? response.contacts : [];
    replaceJobContacts(targetJobId, contacts);
    if (showStatus) {
      const summary = contacts.length === 1
        ? "Found 1 contact."
        : `Found ${contacts.length} contact${contacts.length === 0 ? "s" : "s"}.`;
      showContactFinderStatus(contacts.length ? "succeeded" : "failed", contacts.length
        ? summary
        : "No contacts were found for this company.");
    }
    return contacts.length > 0;
  } catch (err) {
    if (showStatus) {
      showContactFinderStatus("failed", err.message || "Failed to find job contacts.");
    } else {
      console.error("Auto find contacts failed:", err);
    }
    return false;
  } finally {
    activeContactLookups.delete(targetJobId);
    renderView(filteredJobs());
  }
}

async function updateInterviewContactSelection(jobId, contactId, selected) {
  const targetJobId = String(jobId || "");
  const targetContactId = String(contactId || "");
  if (!targetJobId || !targetContactId) return;

  const job = currentJobById(targetJobId);
  if (!job || !Array.isArray(job.contacts)) return;
  const previousContact = job.contacts.find((entry) => String(entry.id) === targetContactId);
  if (!previousContact) return;

  previousContact.selected = Boolean(selected);
  renderView(filteredJobs());

  try {
    const response = await callJson(
      `/interviews/${encodeURIComponent(targetJobId)}/contacts/${encodeURIComponent(targetContactId)}`,
      "PATCH",
      { selected: Boolean(selected) },
    );
    if (response.contact) {
      replaceJobContact(targetJobId, response.contact);
    }
  } catch (err) {
    previousContact.selected = !Boolean(selected);
    renderView(filteredJobs());
    window.alert(`Failed to update job contact selection: ${err.message}`);
    return;
  }

  renderView(filteredJobs());
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
        if (run.status === "failed") {
          await loadJobs();
          window.alert(`Apply Agent failed: ${run.message || "Unknown error"}`);
        } else if (run.status === "cancelled") {
          await loadJobs();
          window.alert(`Apply Agent cancelled: ${run.message || "Run cancelled"}`);
        } else {
          const manualJobId = manualClosedRunJobId(run, jobId);
          if (manualJobId) {
            // Manual flow: still need user confirmation before the card moves.
            await loadJobs();
            showManualOutcomeModal(manualJobId);
          } else {
            // Successful AI apply: fly the card out into Emails/Interviews
            // before the list refresh removes it.
            const successId = appliedRunJobId(run, jobId);
            if (successId && IS_APPLICATIONS_PAGE) {
              await flyCardToTab(successId, '.app-tab[href="/interviews/"]');
            }
            await loadJobs();
            // Auto Find Contacts: if the user toggled this on, kick off
            // the contact lookup for this job in the background. Hits the
            // /interviews/{job_id}/contacts/find endpoint which is page-
            // agnostic, so we can fire it from here (Applications page).
            if (successId && autoFindContactsEnabled && ANYMAILFINDER_CONFIGURED) {
              void findContactsForJob(successId, { showStatus: false });
            }
          }
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

function updatePageRunButtonState() {
  const button = document.getElementById(CAN_RUN_INTAKE ? "find-jobs-button" : "score-jobs-button");
  const running = pageRunStarting || Boolean(activePageRunId);
  if (button) {
    button.disabled = running;
    if (CAN_RUN_INTAKE) {
      button.textContent = running ? "Finding Jobs..." : "Find New Jobs";
    } else if (CAN_RUN_SCORING) {
      button.textContent = running ? "Scoring..." : "Score Jobs";
    }
  }
  // Lock the AI Auto Score toggle while a page-run is active — toggling
  // mid-run would race the agent. Auto Generate Resumes is intentionally
  // NOT locked: it just sets a future preference, and the in-flight run
  // doesn't re-read the toggle, so disabling mid-run is safe (the current
  // run finishes; future scorings/threshold-changes won't auto-trigger).
  if (CAN_RUN_SCORING) {
    setAutoScoreToggleDisabled(running);
  }
  const actionsLocked = areBestMatchesCardActionsBlocked();
  if (lastBestMatchesActionLockState !== actionsLocked) {
    lastBestMatchesActionLockState = actionsLocked;
    if (USE_CARD_LAYOUT && USE_SCORE_THRESHOLD_FILTER && jobsData.length) {
      renderView(filteredJobs());
    }
  }
}

function pageRunStartEndpoint() {
  if (PAGE_RUN_AGENT === "job_intake") return "/jobs/intake/start";
  if (PAGE_RUN_AGENT === "job_scoring") return "/scoring/start";
  return "";
}

function pageRunStartPayload() {
  if (PAGE_RUN_AGENT === "job_intake") {
    return { search_urls: [], max_jobs_per_search: null };
  }
  if (PAGE_RUN_AGENT === "job_scoring") {
    return { job_ids: [] };
  }
  return {};
}

async function startPageRun() {
  if ((!CAN_RUN_INTAKE && !CAN_RUN_SCORING) || pageRunStarting || activePageRunId) return;
  pageRunStarting = true;
  updatePageRunButtonState();

  try {
    const run = await callJson(pageRunStartEndpoint(), "POST", pageRunStartPayload());
    pageRunStarting = false;
    activePageRunId = String(run.id || "");
    activePageRunAgent = PAGE_RUN_AGENT;
    setPageRunParam(activePageRunId);
    setPageRunStatusLabel(runLabelForAgent(PAGE_RUN_AGENT));
    renderPageRunStatus(run);
    updatePageRunButtonState();
    await pollPageRun(activePageRunId);
  } catch (err) {
    pageRunStarting = false;
    activePageRunId = null;
    activePageRunAgent = "";
    setPageRunParam(null);
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    updatePageRunButtonState();
    window.alert(`Failed to start ${PAGE_RUN_LABEL}: ${err.message}`);
  }
}

async function pollPageRun(runId) {
  const expectedAgent = activePageRunAgent;
  const expectedLabel = runLabelForAgent(expectedAgent);
  try {
    while (true) {
      const run = await callJson(`/runs/${runId}`, "GET");
      if (!["queued", "running"].includes(run.status) || run.agent_name !== expectedAgent) {
        const completedAgent = expectedAgent;
        activePageRunId = null;
        activePageRunAgent = "";
        autoResumeRunInFlight = false;
        setPageRunParam(null);
        setPageRunStatusVisibility(false);
        setPageRunCancelButtonState(false);
        setPageRunStatusLabel(PAGE_RUN_LABEL);
        updatePageRunButtonState();
        await loadJobs();
        if (run.status === "failed") {
          window.alert(`${expectedLabel} failed: ${run.message || "Unknown error"}`);
        } else if (run.status === "cancelled") {
          window.alert(`${expectedLabel} cancelled: ${run.message || "Run cancelled"}`);
        }
        // After scoring completes (Auto Score path), kick off Auto Generate
        // Resumes immediately for any job that now passes the threshold and
        // doesn't have a resume yet. The 120s settling debounce only applies
        // to threshold-adjustment-triggered fires.
        if (completedAgent === "job_scoring" && autoGenerateResumesEnabled) {
          void maybeStartAutoResume();
        }
        return;
      }
      renderPageRunStatus(run);
      // While the scoring agent is running, refresh the job list so newly
      // scored jobs that clear the threshold appear as cards in real time
      // instead of waiting for the run to finish.
      if (expectedAgent === "job_scoring" && run.status === "running") {
        try { await loadJobs(); } catch { /* keep polling */ }
      }
      await new Promise((resolve) => setTimeout(resolve, 3000));
    }
  } catch (err) {
    activePageRunId = null;
    activePageRunAgent = "";
    autoResumeRunInFlight = false;
    setPageRunParam(null);
    setPageRunStatusVisibility(false);
    setPageRunCancelButtonState(false);
    setPageRunStatusLabel(PAGE_RUN_LABEL);
    updatePageRunButtonState();
    window.alert(`Failed to monitor ${expectedLabel}: ${err.message}`);
  }
}

async function cancelActivePageRun() {
  if (!activePageRunId) return;
  try {
    const run = await callJson(`/runs/${activePageRunId}/cancel`, "POST");
    renderPageRunStatus(run);
  } catch (err) {
    window.alert(`Failed to stop ${PAGE_RUN_LABEL}: ${err.message}`);
  }
}

async function persistScoreThreshold(threshold) {
  const response = await callJson("/jobs/score-threshold", "PUT", { score_threshold: threshold });
  persistedScoreThreshold = Number(response.score_threshold);
  currentScoreThreshold = persistedScoreThreshold;
  const input = document.getElementById("score-threshold-input");
  if (input) input.value = formatScoreThresholdDisplay(persistedScoreThreshold);
}

async function persistAutoScoreEnabled(enabled) {
  const response = await callJson("/jobs/auto-score", "PUT", { enabled: Boolean(enabled) });
  autoScoreEnabled = Boolean(response.auto_score_enabled);
  setAutoScoreToggleState(autoScoreEnabled);
  return autoScoreEnabled;
}

async function persistAutoFindContactsEnabled(enabled) {
  const response = await callJson("/interviews/auto-find-contacts", "PUT", { enabled: Boolean(enabled) });
  autoFindContactsEnabled = Boolean(response.auto_find_contacts_enabled);
  setAutoFindContactsToggleState(autoFindContactsEnabled);
  return autoFindContactsEnabled;
}

async function setManualApplied(jobId, applied) {
  const response = await callJson(`/jobs/${jobId}`, "PATCH", { applied });
  const updatedJob = response.job || null;
  const index = jobsData.findIndex((job) => String(job.id) === String(jobId));
  if (index >= 0 && updatedJob) {
    jobsData[index] = { ...jobsData[index], ...updatedJob };
  }
  renderView(filteredJobs());
}

function manualClosedRunJobId(run, fallbackJobId) {
  const result = run?.result;
  if (!result || typeof result !== "object") {
    return "";
  }
  const appliedJobs = Array.isArray(result.applied_jobs) ? result.applied_jobs : [];
  const manualClosedJob = appliedJobs.find((item) => item && item.status === "manual_closed");
  if (manualClosedJob?.job_id) {
    return String(manualClosedJob.job_id);
  }
  if (manualClosedJob && fallbackJobId) {
    return String(fallbackJobId);
  }
  return "";
}

function appliedRunJobId(run, fallbackJobId) {
  const result = run?.result;
  if (!result || typeof result !== "object") {
    return "";
  }
  const appliedJobs = Array.isArray(result.applied_jobs) ? result.applied_jobs : [];
  const successJob = appliedJobs.find((item) => item && item.success === true);
  if (successJob?.job_id) {
    return String(successJob.job_id);
  }
  if (successJob && fallbackJobId) {
    return String(fallbackJobId);
  }
  return "";
}

function showManualOutcomeModal(jobId) {
  const modal = document.getElementById("manual-outcome-modal");
  const title = document.getElementById("manual-outcome-modal-title");
  const message = document.getElementById("manual-outcome-modal-message");
  if (!modal || !jobId) return;
  const job = jobsData.find((item) => String(item.id) === String(jobId));
  pendingManualOutcomeJobId = String(jobId);
  if (title) {
    title.textContent = "Application Competed Successfully?";
  }
  if (message) {
    if (job) {
      const role = job.job_title || "this job";
      const company = job.company_name || "this company";
      message.textContent = `Did you successfully apply to ${role} at ${company}`;
    } else {
      message.textContent = "Did you successfully apply to this job?";
    }
  }
  modal.hidden = false;
}

function hideManualOutcomeModal() {
  const modal = document.getElementById("manual-outcome-modal");
  if (modal) modal.hidden = true;
  pendingManualOutcomeJobId = null;
}

function normalizeScoreThreshold(value) {
  if (value == null || value === "") return null;
  const parsed = Number.parseFloat(String(value).trim());
  if (!Number.isFinite(parsed)) return null;
  return Math.max(0, Math.min(1000, Math.round(parsed * 10)));
}

function scheduleThresholdRefresh(threshold) {
  if (thresholdPersistTimer !== null) {
    window.clearTimeout(thresholdPersistTimer);
  }
  // Reset the 120s settling timer on every threshold edit. If the user is
  // still fiddling with the threshold, we want to give them a full 120s of
  // quiet before we believe the new threshold is the one they want and let
  // Auto Generate Resumes kick in for any newly-eligible jobs.
  clearThresholdSettlingTimer();
  thresholdPersistTimer = window.setTimeout(async () => {
    thresholdPersistTimer = null;
    try {
      await persistScoreThreshold(threshold);
      await loadJobs();
      armThresholdSettlingTimer();
    } catch (err) {
      window.alert(`Failed to save scoring threshold: ${err.message}`);
    }
  }, 250);
}

function onScoreThresholdInput(event) {
  const threshold = normalizeScoreThreshold(event.target.value);
  if (threshold == null) return;
  currentScoreThreshold = threshold;
  scheduleThresholdRefresh(threshold);
}

function onScoreThresholdBlur(event) {
  const threshold = normalizeScoreThreshold(event.target.value);
  if (threshold == null) {
    event.target.value = formatScoreThresholdDisplay(persistedScoreThreshold);
    currentScoreThreshold = persistedScoreThreshold;
    return;
  }
  const normalizedDisplay = formatScoreThresholdDisplay(threshold);
  if (normalizedDisplay !== event.target.value) {
    event.target.value = normalizedDisplay;
  }
  currentScoreThreshold = threshold;
  scheduleThresholdRefresh(threshold);
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
    if (USE_CARD_LAYOUT) {
      const description = longtextCardHtml(value, expandedJobDescriptions.has(String(jobId)), jobId);
      el.innerHTML = description.html;
      el.removeAttribute("title");
    } else {
      el.textContent = truncateText(String(value ?? ""), descTruncLen());
      el.title = String(value ?? "");
    }
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
  const descriptionToggle = e.target.closest("[data-description-toggle]");
  if (descriptionToggle) {
    e.preventDefault();
    e.stopPropagation();
    const jobId = String(descriptionToggle.dataset.jobId || "");
    if (expandedJobDescriptions.has(jobId)) {
      expandedJobDescriptions.delete(jobId);
    } else {
      expandedJobDescriptions.add(jobId);
    }
    renderView(filteredJobs());
    return;
  }

  const applyButton = e.target.closest(".apply-job-button");
  if (applyButton && !applyButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    if (applyButton.dataset.manualOnly === "true") {
      void startManualApply(applyButton.dataset.jobId);
      return;
    }
    showApplyChoiceModal(applyButton.dataset.jobId);
    return;
  }

  const resumeButton = e.target.closest(".generate-resume-button");
  if (resumeButton && !resumeButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    void startResumeForJob(resumeButton.dataset.jobId);
    return;
  }

  const contactButton = e.target.closest(".contact-action-button");
  if (contactButton && !contactButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    if (IS_EMAILS_INTERVIEWS_PAGE) {
      void findContactsForJob(contactButton.dataset.jobId);
      return;
    }
    return;
  }

  const emailButton = e.target.closest(".email-contacts-button");
  if (emailButton && !emailButton.disabled) {
    e.preventDefault();
    e.stopPropagation();
    void startEmailFlowForJob(emailButton.dataset.jobId);
    return;
  }

  const sentLink = e.target.closest(".email-sent-link");
  if (sentLink) {
    e.preventDefault();
    e.stopPropagation();
    void openSentEmailViewer(sentLink.dataset.jobId, sentLink.dataset.contactId);
    return;
  }

  // Don't intercept clicks on links
  if (e.target.closest("a")) return;

  const editable = e.target.closest("[data-editable]:not(.cell-checkbox)");
  if (!editable) return;

  startEdit(editable);
}

function onContainerChange(e) {
  const contactSelect = e.target.closest(".job-contact-select");
  if (!contactSelect) return;

  e.stopPropagation();
  void updateInterviewContactSelection(
    contactSelect.dataset.jobId,
    contactSelect.dataset.contactId,
    contactSelect.checked,
  );
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

window.addEventListener("DOMContentLoaded", async () => {
  const searchInput = document.getElementById("jobs-search");
  const findJobsButton = document.getElementById("find-jobs-button");
  const scoreJobsButton = document.getElementById("score-jobs-button");
  const scoreThresholdInput = document.getElementById("score-threshold-input");
  const autoScoreToggle = document.getElementById("auto-score-toggle");
  const autoScoreModal = document.getElementById("auto-score-modal");
  const autoScoreModalCancel = document.getElementById("auto-score-modal-cancel");
  const autoScoreModalConfirm = document.getElementById("auto-score-modal-confirm");
  const sortSelect = document.getElementById("jobs-sort");
  const sortDirBtn = document.getElementById("jobs-sort-dir");
  const cancelPageRunButton = document.getElementById("cancel-page-agent-button");
  const applyChoiceModal = document.getElementById("apply-choice-modal");
  const manualApplyButton = document.getElementById("manual-apply-button");
  const aiApplyButton = document.getElementById("ai-apply-button");
  const manualOutcomeModal = document.getElementById("manual-outcome-modal");
  const manualOutcomeYesButton = document.getElementById("manual-outcome-yes-button");
  const manualOutcomeNoButton = document.getElementById("manual-outcome-no-button");

  searchInput.addEventListener("input", onSearchInput);
  if (findJobsButton) {
    findJobsButton.addEventListener("click", () => {
      void startPageRun();
    });
  }
  if (scoreJobsButton) {
    scoreJobsButton.addEventListener("click", () => {
      void startPageRun();
    });
  }
  if (scoreThresholdInput) {
    scoreThresholdInput.value = formatScoreThresholdDisplay(persistedScoreThreshold);
    scoreThresholdInput.addEventListener("input", onScoreThresholdInput);
    scoreThresholdInput.addEventListener("blur", onScoreThresholdBlur);
  }
  if (autoScoreToggle) {
    setAutoScoreToggleState(autoScoreEnabled);
    autoScoreToggle.addEventListener("change", () => {
      const nextEnabled = autoScoreToggle.checked;
      setAutoScoreToggleState(autoScoreEnabled);
      showAutoScoreModal(nextEnabled);
    });
  }
  if (cancelPageRunButton) {
    cancelPageRunButton.addEventListener("click", () => {
      void cancelActivePageRun();
    });
  }
  if (autoScoreModal) {
    autoScoreModal.addEventListener("click", (event) => {
      if (event.target === autoScoreModal) {
        hideAutoScoreModal();
      }
    });
  }
  if (autoScoreModalCancel) {
    autoScoreModalCancel.addEventListener("click", () => {
      hideAutoScoreModal();
    });
  }
  if (autoScoreModalConfirm) {
    autoScoreModalConfirm.addEventListener("click", async () => {
      if (pendingAutoScoreEnabled == null) return;
      const nextEnabled = pendingAutoScoreEnabled;
      try {
        await persistAutoScoreEnabled(nextEnabled);
        hideAutoScoreModal();
        if (nextEnabled && CAN_RUN_SCORING && AUTO_SCORE_PENDING_COUNT > 0 && !activePageRunId && !pageRunStarting) {
          void startPageRun();
        }
      } catch (err) {
        hideAutoScoreModal();
        window.alert(`Failed to save AI Auto Score: ${err.message}`);
      }
    });
  }

  // Auto Generate Resumes toggle
  const autoGenerateResumesToggle = document.getElementById("auto-generate-resumes-toggle");
  const autoGenerateResumesModal = document.getElementById("auto-generate-resumes-modal");
  const autoGenerateResumesModalCancel = document.getElementById("auto-generate-resumes-modal-cancel");
  const autoGenerateResumesModalConfirm = document.getElementById("auto-generate-resumes-modal-confirm");
  if (autoGenerateResumesToggle) {
    setAutoGenerateResumesToggleState(autoGenerateResumesEnabled);
    autoGenerateResumesToggle.addEventListener("change", () => {
      const nextEnabled = autoGenerateResumesToggle.checked;
      setAutoGenerateResumesToggleState(autoGenerateResumesEnabled);
      showAutoGenerateResumesModal(nextEnabled);
    });
  }
  if (autoGenerateResumesModal) {
    autoGenerateResumesModal.addEventListener("click", (event) => {
      if (event.target === autoGenerateResumesModal) {
        hideAutoGenerateResumesModal();
      }
    });
  }
  if (autoGenerateResumesModalCancel) {
    autoGenerateResumesModalCancel.addEventListener("click", () => {
      hideAutoGenerateResumesModal();
    });
  }
  if (autoGenerateResumesModalConfirm) {
    autoGenerateResumesModalConfirm.addEventListener("click", async () => {
      if (pendingAutoGenerateResumesEnabled == null) return;
      const nextEnabled = pendingAutoGenerateResumesEnabled;
      try {
        await persistAutoGenerateResumesEnabled(nextEnabled);
        hideAutoGenerateResumesModal();
        // Q1=A: enabling kicks off resume generation immediately for any
        // currently visible eligible jobs (passing threshold, no resume,
        // not already applied). The modal IS the deliberate intent — no
        // 120s debounce here.
        if (nextEnabled) {
          const jobIds = eligibleResumeJobIds();
          if (jobIds.length) {
            void maybeStartAutoResume();
          } else {
            window.alert(
              "Auto Generate Resumes is on. No jobs need a resume right now — it will trigger automatically after the next scoring run or threshold change."
            );
          }
        } else {
          // Cancel any pending settling timer if user disabled.
          clearThresholdSettlingTimer();
        }
      } catch (err) {
        hideAutoGenerateResumesModal();
        window.alert(`Failed to save Auto Generate Resumes: ${err.message}`);
      }
    });
  }

  const autoFindContactsToggle = document.getElementById("auto-find-contacts-toggle");
  const autoFindContactsModal = document.getElementById("auto-find-contacts-modal");
  const autoFindContactsModalCancel = document.getElementById("auto-find-contacts-modal-cancel");
  const autoFindContactsModalConfirm = document.getElementById("auto-find-contacts-modal-confirm");
  if (autoFindContactsToggle) {
    setAutoFindContactsToggleState(autoFindContactsEnabled);
    autoFindContactsToggle.addEventListener("change", () => {
      const nextEnabled = autoFindContactsToggle.checked;
      // Snap visual state back to current saved value until the user
      // confirms via the modal.
      setAutoFindContactsToggleState(autoFindContactsEnabled);
      showAutoFindContactsModal(nextEnabled);
    });
  }
  if (autoFindContactsModal) {
    autoFindContactsModal.addEventListener("click", (event) => {
      if (event.target === autoFindContactsModal) {
        hideAutoFindContactsModal();
      }
    });
  }
  if (autoFindContactsModalCancel) {
    autoFindContactsModalCancel.addEventListener("click", () => {
      hideAutoFindContactsModal();
    });
  }
  if (autoFindContactsModalConfirm) {
    autoFindContactsModalConfirm.addEventListener("click", async () => {
      if (pendingAutoFindContactsEnabled == null) return;
      const nextEnabled = pendingAutoFindContactsEnabled;
      try {
        await persistAutoFindContactsEnabled(nextEnabled);
        hideAutoFindContactsModal();
      } catch (err) {
        hideAutoFindContactsModal();
        window.alert(`Failed to save Auto Find Contacts: ${err.message}`);
      }
    });
  }

  // Email contacts modals
  const emailChoiceModal = document.getElementById("email-choice-modal");
  const emailChoiceCancel = document.getElementById("email-choice-cancel");
  const emailChoiceManual = document.getElementById("email-choice-manual");
  const emailChoiceAi = document.getElementById("email-choice-ai");
  const emailEditModal = document.getElementById("email-edit-modal");
  const emailEditCancel = document.getElementById("email-edit-cancel");
  const emailEditSend = document.getElementById("email-edit-send");
  const emailViewModal = document.getElementById("email-view-modal");
  const emailViewClose = document.getElementById("email-view-close");
  if (emailChoiceModal) {
    emailChoiceModal.addEventListener("click", (event) => {
      if (event.target === emailChoiceModal) hideEmailChoiceModal();
    });
  }
  if (emailChoiceCancel) {
    emailChoiceCancel.addEventListener("click", () => {
      hideEmailChoiceModal();
      pendingEmailJobId = null;
      pendingEmailContactIds = [];
    });
  }
  if (emailChoiceManual) {
    emailChoiceManual.addEventListener("click", () => {
      void continueEmailFlow("manual");
    });
  }
  if (emailChoiceAi) {
    emailChoiceAi.addEventListener("click", () => {
      void continueEmailFlow("ai");
    });
  }
  if (emailEditModal) {
    emailEditModal.addEventListener("click", (event) => {
      if (event.target === emailEditModal) hideEmailEditModal();
    });
  }
  if (emailEditCancel) {
    emailEditCancel.addEventListener("click", () => hideEmailEditModal());
  }
  if (emailEditSend) {
    emailEditSend.addEventListener("click", () => {
      void submitEmailSend();
    });
  }
  if (emailViewModal) {
    emailViewModal.addEventListener("click", (event) => {
      if (event.target === emailViewModal) hideSentEmailViewer();
    });
  }
  if (emailViewClose) {
    emailViewClose.addEventListener("click", () => hideSentEmailViewer());
  }

  if (applyChoiceModal) {
    applyChoiceModal.addEventListener("click", (event) => {
      if (event.target === applyChoiceModal) {
        hideApplyChoiceModal();
      }
    });
  }
  if (manualOutcomeModal) {
    manualOutcomeModal.addEventListener("click", (event) => {
      if (event.target === manualOutcomeModal) {
        hideManualOutcomeModal();
      }
    });
  }
  if (manualApplyButton) {
    manualApplyButton.addEventListener("click", async () => {
      const jobId = pendingApplyChoiceJobId;
      hideApplyChoiceModal();
      if (jobId) await startManualApply(jobId);
    });
  }
  if (aiApplyButton) {
    aiApplyButton.addEventListener("click", async () => {
      const jobId = pendingApplyChoiceJobId;
      hideApplyChoiceModal();
      if (!jobId) return;
      if (USE_SCORE_THRESHOLD_FILTER) {
        const movePromise = moveJobToApplications(jobId, "ai");
        await flyCardToTab(jobId, '.app-tab[href="/applications/"]');
        try {
          await movePromise;
        } catch (err) {
          renderView(filteredJobs());
          window.alert(`Failed to move job to Applications: ${err.message}`);
          return;
        }
        removeJobFromBestMatches(jobId, false);
      }
      await startAiApply(jobId);
    });
  }
  if (manualOutcomeYesButton) {
    manualOutcomeYesButton.addEventListener("click", async () => {
      const jobId = pendingManualOutcomeJobId;
      hideManualOutcomeModal();
      if (!jobId) return;
      try {
        await setManualApplied(jobId, true);
        if (IS_APPLICATIONS_PAGE) {
          await flyCardToTab(jobId, '.app-tab[href="/interviews/"]');
          await loadJobs();
        }
        // Auto Find Contacts: same trigger as the AI auto-apply success
        // branch in pollApplyRun, fired here for the manual confirm path.
        if (autoFindContactsEnabled && ANYMAILFINDER_CONFIGURED) {
          void findContactsForJob(jobId, { showStatus: false });
        }
      } catch (err) {
        window.alert(`Failed to update manual apply status: ${err.message}`);
      }
    });
  }
  if (manualOutcomeNoButton) {
    manualOutcomeNoButton.addEventListener("click", () => {
      hideManualOutcomeModal();
    });
  }
  updatePageRunButtonState();
  sortSelect.addEventListener("change", onSortChange);
  sortDirBtn.addEventListener("click", onSortDirToggle);

  // Set initial sort select value
  sortSelect.value = sortField;

  if (USE_CARD_LAYOUT) {
    const cardList = document.getElementById("jobs-card-list");
    cardList.addEventListener("click", onContainerClick);
    cardList.addEventListener("change", onContainerChange);
    cardList.addEventListener("mouseover", onContainerMouseOver);
    cardList.addEventListener("mouseout", onContainerMouseOut);
  } else {
    const tbody = document.getElementById("jobs-table-body");
    const tableHead = document.querySelector(".jobs-table thead");
    tbody.addEventListener("click", onContainerClick);
    tbody.addEventListener("change", onContainerChange);
    tbody.addEventListener("mouseover", onContainerMouseOver);
    tbody.addEventListener("mouseout", onContainerMouseOut);
    if (tableHead) tableHead.addEventListener("click", onHeaderClick);
    initColumnResize();
  }

  window.addEventListener("keydown", onWindowKeydown);
  await Promise.all([loadJobs(), restoreActivePageRun()]);
  if (APPLICATION_MANUAL_APPLY && APPLICATION_JOB_ID && !currentJobById(APPLICATION_JOB_ID)?.applied) {
    clearManualApplyParam();
    void startApplyForJob(APPLICATION_JOB_ID, "manual");
  } else if (APPLICATION_AUTO_APPLY && APPLICATION_JOB_ID && !currentJobById(APPLICATION_JOB_ID)?.applied) {
    clearAutoApplyParam();
    void startApplyForJob(APPLICATION_JOB_ID, "ai");
  } else if (CAN_RUN_SCORING && autoScoreEnabled && AUTO_SCORE_PENDING_COUNT > 0 && !activePageRunId && !pageRunStarting) {
    void startPageRun();
  } else if (CAN_RUN_SCORING && autoGenerateResumesEnabled && AUTO_GENERATE_RESUMES_PENDING_COUNT > 0 && !activePageRunId && !pageRunStarting) {
    // Auto Generate Resumes was previously enabled and there are jobs that
    // already pass the threshold without a resume — pick up where we left off.
    void maybeStartAutoResume();
  }
});
