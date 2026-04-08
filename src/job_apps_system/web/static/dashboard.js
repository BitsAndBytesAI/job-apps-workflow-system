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

let activeRunId = null;
let activePollTimer = null;
const MANUAL_RESUME_RUN_LIMIT = 5;

function setRunStatusVisibility(visible) {
  const box = document.getElementById("run-status");
  box.hidden = !visible;
}

function setCancelButtonVisibility(visible) {
  const cancelButton = document.getElementById("cancel-run-button");
  cancelButton.hidden = !visible;
  if (!visible) {
    cancelButton.disabled = false;
    cancelButton.textContent = "Kill Agent";
  }
}

function formatAgentName(agentName) {
  if (agentName === "job_intake") return "Jobs Agent";
  if (agentName === "job_scoring") return "Scoring Agent";
  if (agentName === "resume_generation") return "Resume Agent";
  if (!agentName) return "Agent";
  return agentName
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function clearRunPolling() {
  if (activePollTimer !== null) {
    window.clearTimeout(activePollTimer);
    activePollTimer = null;
  }
}

function setRunStatus(message, level = "info", steps = [], meta = "", collapsed = false) {
  const box = document.getElementById("run-status");
  const heading = document.getElementById("run-status-heading");
  const detail = document.getElementById("run-status-message");
  const metaNode = document.getElementById("run-status-meta");
  const stepsNode = document.getElementById("run-status-steps");
  const indicator = document.getElementById("run-status-indicator");

  box.dataset.level = level;
  box.dataset.collapsed = collapsed ? "true" : "false";
  heading.textContent = message;
  detail.textContent = collapsed ? "" : "";
  metaNode.textContent = meta;
  indicator.hidden = true;
  setRunStatusVisibility(true);

  if (collapsed) {
    detail.hidden = true;
    stepsNode.hidden = true;
    stepsNode.innerHTML = `<li class="step-list-empty">Steps will appear here while an agent is running.</li>`;
    return;
  }

  detail.hidden = false;
  stepsNode.hidden = false;

  if (!steps.length) {
    stepsNode.innerHTML = `<li class="step-list-empty">Steps will appear here while an agent is running.</li>`;
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
          <div class="step-message">${escapeHtml(step.message || "")}</div>
        </li>
      `,
    )
    .join("");
}

function formatDate(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function statusLevel(status) {
  if (status === "succeeded") return "success";
  if (status === "cancelled") return "info";
  if (status === "failed") return "error";
  return "info";
}

function formatStatus(status) {
  if (!status) return "";
  return status.replaceAll("_", " ");
}

function renderRunStatus(run) {
  if (!run) {
    setRunStatusVisibility(false);
    setCancelButtonVisibility(false);
    return;
  }

  const metaParts = [];
  if (run.started_at) metaParts.push(`Started ${formatDate(run.started_at)}`);
  if (run.finished_at) metaParts.push(`Finished ${formatDate(run.finished_at)}`);

  setRunStatus(
    `${formatAgentName(run.agent_name)}: ${run.message || formatStatus(run.status)}`,
    statusLevel(run.status),
    run.steps || [],
    metaParts.join(" · "),
    run.status !== "queued" && run.status !== "running",
  );

  const indicator = document.getElementById("run-status-indicator");
  const cancelButton = document.getElementById("cancel-run-button");
  indicator.hidden = !(run.status === "queued" || run.status === "running");
  setCancelButtonVisibility(run.status === "queued" || run.status === "running");
  cancelButton.disabled = Boolean(run.cancel_requested);
  cancelButton.textContent = run.cancel_requested ? "Stopping..." : "Kill Agent";
  setRunStatusVisibility(run.status === "queued" || run.status === "running");
}

function renderRuns(runs) {
  const summary = document.getElementById("runs-summary");
  const tbody = document.getElementById("runs-table-body");
  summary.textContent = `${runs.length} recorded run(s).`;
  summary.dataset.level = "info";

  if (!runs.length) {
    tbody.innerHTML = `<tr><td colspan="5">No agent runs recorded yet.</td></tr>`;
    return;
  }

  tbody.innerHTML = runs
    .map(
      (run) => `
        <tr>
          <td>${escapeHtml(formatAgentName(run.agent_name || ""))}</td>
          <td>${escapeHtml(formatDate(run.started_at))}</td>
          <td>${escapeHtml(formatDate(run.finished_at))}</td>
          <td>
            <span class="run-status-wrap">
              <span class="table-status-chip" data-status="${escapeHtml(run.status || "")}">${escapeHtml(formatStatus(run.status || ""))}</span>
              <span class="run-indicator" ${run.status === "running" || run.status === "queued" ? "" : "hidden"} aria-hidden="true"></span>
            </span>
          </td>
          <td><div class="run-summary" title="${escapeHtml(run.summary || run.message || "")}">${escapeHtml(run.summary || run.message || "")}</div></td>
        </tr>
      `,
    )
    .join("");
}

function findActiveRun(runs) {
  return runs.find((run) => run.status === "queued" || run.status === "running") || null;
}

async function refreshRuns() {
  const data = await callJson("/runs/", "GET");
  const runs = data.runs || [];
  renderRuns(runs);

  if (!activeRunId) {
    const activeRun = findActiveRun(runs);
    if (activeRun) {
      renderRunStatus(activeRun);
    } else {
      setRunStatusVisibility(false);
      setCancelButtonVisibility(false);
    }
  }

  return runs;
}

async function pollRun(runId) {
  try {
    const run = await callJson(`/runs/${runId}`, "GET");
    renderRunStatus(run);
    await refreshRuns();

    if (run.status === "queued" || run.status === "running") {
      activePollTimer = window.setTimeout(() => pollRun(runId), 1000);
      return;
    }

    activeRunId = null;
    clearRunPolling();
    const runs = await refreshRuns();
    const nextActiveRun = findActiveRun(runs);
    if (nextActiveRun) {
      activeRunId = nextActiveRun.id;
      renderRunStatus(nextActiveRun);
      await pollRun(nextActiveRun.id);
      return;
    }
    setRunStatusVisibility(false);
    setCancelButtonVisibility(false);
    setAgentCardsDisabled(false);
  } catch (error) {
    activeRunId = null;
    clearRunPolling();
    setAgentCardsDisabled(false);
    setRunStatus(`Unable to poll run: ${error.message}`, "error");
  }
}

async function runIntake() {
  setAgentCardsDisabled(true);
  clearRunPolling();
  setRunStatus("Queueing jobs agent...", "info");
  try {
    const run = await callJson("/jobs/intake/start", "POST", { search_urls: [], max_jobs_per_search: 100 });
    activeRunId = run.id;
    renderRunStatus(run);
    await refreshRuns();
    await pollRun(run.id);
  } catch (error) {
    setRunStatus(error.message, "error");
    setAgentCardsDisabled(false);
  }
}

async function runScoring() {
  setAgentCardsDisabled(true);
  clearRunPolling();
  setRunStatus("Queueing scoring agent...", "info");
  try {
    const run = await callJson("/scoring/start", "POST", { job_ids: [] });
    activeRunId = run.id;
    renderRunStatus(run);
    await refreshRuns();
    await pollRun(run.id);
  } catch (error) {
    setRunStatus(error.message, "error");
    setAgentCardsDisabled(false);
  }
}

async function runResume() {
  setAgentCardsDisabled(true);
  clearRunPolling();
  setRunStatus(`Queueing resume agent (max ${MANUAL_RESUME_RUN_LIMIT})...`, "info");
  try {
    const run = await callJson("/resumes/generate/start", "POST", { limit: MANUAL_RESUME_RUN_LIMIT });
    activeRunId = run.id;
    renderRunStatus(run);
    await refreshRuns();
    await pollRun(run.id);
  } catch (error) {
    setRunStatus(error.message, "error");
    setAgentCardsDisabled(false);
  }
}

async function cancelActiveRun() {
  if (!activeRunId) return;
  const cancelButton = document.getElementById("cancel-run-button");
  cancelButton.disabled = true;
  cancelButton.textContent = "Stopping...";
  try {
    const run = await callJson(`/runs/${activeRunId}/cancel`, "POST");
    renderRunStatus(run);
    await refreshRuns();
  } catch (error) {
    cancelButton.disabled = false;
    cancelButton.textContent = "Kill Agent";
    setRunStatus(`Unable to stop agent: ${error.message}`, "error");
  }
}

async function syncEmJobs() {
  const card = document.getElementById("sync-em-jobs-button");
  card.setAttribute("aria-disabled", "true");
  setRunStatus("Syncing jobs sheet...", "info", [
    { name: "Sync jobs sheet", status: "running", message: "Reading the Google Sheet and updating the local DB." },
  ]);
  try {
    const result = await callJson("/jobs/sync", "POST");
    setRunStatus(
      `Sheet sync finished. Rows=${result.row_count}, created=${result.created}, updated=${result.updated}.`,
      "success",
      [
        {
          name: "Sync jobs sheet",
          status: "completed",
          message: `Rows=${result.row_count}, created=${result.created}, updated=${result.updated}.`,
        },
      ],
    );
    await refreshRuns();
  } catch (error) {
    setRunStatus(error.message, "error");
  } finally {
    card.setAttribute("aria-disabled", "false");
  }
}

const AGENT_CARD_IDS = ["run-intake-button", "run-scoring-button", "run-resume-button"];

function setAgentCardsDisabled(disabled) {
  AGENT_CARD_IDS.forEach((id) => {
    const el = document.getElementById(id);
    if (el) el.setAttribute("aria-disabled", String(disabled));
  });
}

function handleCardClick(card, handler) {
  card.addEventListener("click", () => {
    if (card.getAttribute("aria-disabled") === "true") return;
    handler();
  });
  card.addEventListener("keydown", (e) => {
    if ((e.key === "Enter" || e.key === " ") && card.getAttribute("aria-disabled") !== "true") {
      e.preventDefault();
      handler();
    }
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  handleCardClick(document.getElementById("run-intake-button"), runIntake);
  handleCardClick(document.getElementById("run-scoring-button"), runScoring);
  handleCardClick(document.getElementById("run-resume-button"), runResume);
  handleCardClick(document.getElementById("sync-em-jobs-button"), syncEmJobs);
  document.getElementById("cancel-run-button").addEventListener("click", cancelActiveRun);
  document.getElementById("refresh-runs-button").addEventListener("click", refreshRuns);

  try {
    const runs = await refreshRuns();
    const activeRun = findActiveRun(runs);
    if (activeRun) {
      activeRunId = activeRun.id;
      renderRunStatus(activeRun);
      await pollRun(activeRun.id);
    } else {
      setRunStatusVisibility(false);
      setCancelButtonVisibility(false);
    }
  } catch (error) {
    setRunStatus(`Unable to load run history: ${error.message}`, "error");
  }
});
