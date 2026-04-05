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

function formatAgentName(agentName) {
  if (agentName === "job_intake") return "Jobs Agent";
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

function setRunStatus(message, level = "info", steps = [], meta = "") {
  const box = document.getElementById("run-status");
  const heading = document.getElementById("run-status-heading");
  const detail = document.getElementById("run-status-message");
  const metaNode = document.getElementById("run-status-meta");
  const stepsNode = document.getElementById("run-status-steps");

  box.dataset.level = level;
  heading.textContent = message;
  detail.textContent = "";
  metaNode.textContent = meta;

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
  if (status === "failed") return "error";
  return "info";
}

function formatStatus(status) {
  if (!status) return "";
  return status.replaceAll("_", " ");
}

function renderRunStatus(run) {
  if (!run) {
    setRunStatus("Ready.", "info");
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
  );
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
          <td><span class="table-status-chip" data-status="${escapeHtml(run.status || "")}">${escapeHtml(formatStatus(run.status || ""))}</span></td>
          <td>${escapeHtml(run.summary || run.message || "")}</td>
        </tr>
      `,
    )
    .join("");
}

async function refreshRuns() {
  const data = await callJson("/runs/", "GET");
  const runs = data.runs || [];
  renderRuns(runs);

  if (!activeRunId && runs.length) {
    renderRunStatus(runs[0]);
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
    document.getElementById("run-intake-button").disabled = false;
  } catch (error) {
    activeRunId = null;
    clearRunPolling();
    document.getElementById("run-intake-button").disabled = false;
    setRunStatus(`Unable to poll run: ${error.message}`, "error");
  }
}

async function runIntake() {
  const button = document.getElementById("run-intake-button");
  button.disabled = true;
  clearRunPolling();
  setRunStatus("Queueing jobs agent…", "info");
  try {
    const run = await callJson("/jobs/intake/start", "POST", { search_urls: [], max_jobs_per_search: 100 });
    activeRunId = run.id;
    renderRunStatus(run);
    await refreshRuns();
    await pollRun(run.id);
  } catch (error) {
    setRunStatus(error.message, "error");
    button.disabled = false;
  }
}

async function syncEmJobs() {
  const button = document.getElementById("sync-em-jobs-button");
  button.disabled = true;
  setRunStatus("Syncing jobs sheet…", "info", [
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
    button.disabled = false;
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("run-intake-button").addEventListener("click", runIntake);
  document.getElementById("sync-em-jobs-button").addEventListener("click", syncEmJobs);
  document.getElementById("refresh-runs-button").addEventListener("click", refreshRuns);

  try {
    const runs = await refreshRuns();
    const activeRun = runs.find((run) => run.status === "queued" || run.status === "running");
    if (activeRun) {
      activeRunId = activeRun.id;
      renderRunStatus(activeRun);
      await pollRun(activeRun.id);
    } else if (runs.length) {
      renderRunStatus(runs[0]);
    }
  } catch (error) {
    setRunStatus(`Unable to load run history: ${error.message}`, "error");
  }
});
