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
let dashboardSetupConfig = null;
let apiKeyModalResolver = null;
let activeApiKeyRequirement = null;

const AGENT_LLM_REQUIREMENTS = {
  job_intake: {
    secretField: "anthropic_api_key",
    configuredField: "anthropic_api_key_configured",
    providerName: "Anthropic",
    message: "Find Jobs triggers scoring automatically, so Anthropic must be configured first.",
  },
  job_scoring: {
    secretField: "anthropic_api_key",
    configuredField: "anthropic_api_key_configured",
    providerName: "Anthropic",
    message: "Best Job Matches uses Anthropic to score jobs against your profile.",
  },
};

function buildSetupUpdatePayload(config, secretOverrides = {}) {
  return {
    google: config.google,
    linkedin: config.linkedin,
    models: config.models,
    app: config.app,
    secrets: {
      openai_api_key: null,
      anthropic_api_key: null,
      anymailfinder_api_key: null,
      ...secretOverrides,
    },
  };
}

async function getSetupConfig(force = false) {
  if (!dashboardSetupConfig || force) {
    dashboardSetupConfig = await callJson("/setup/api/config", "GET");
  }
  return dashboardSetupConfig;
}

function setApiKeyModalStatus(message, level = "info") {
  const box = document.getElementById("api-key-modal-status");
  if (!message) {
    box.hidden = true;
    box.textContent = "";
    box.dataset.level = "info";
    return;
  }
  box.hidden = false;
  box.textContent = message;
  box.dataset.level = level;
}

function hideApiKeyModal() {
  const modal = document.getElementById("api-key-modal");
  const input = document.getElementById("api-key-modal-input");
  modal.hidden = true;
  input.value = "";
  setApiKeyModalStatus("");
  activeApiKeyRequirement = null;
}

function resolveApiKeyModal(result) {
  if (apiKeyModalResolver) {
    apiKeyModalResolver(result);
    apiKeyModalResolver = null;
  }
}

function openApiKeyModal(requirement) {
  const modal = document.getElementById("api-key-modal");
  const title = document.getElementById("api-key-modal-title");
  const label = document.getElementById("api-key-modal-label");
  const message = document.getElementById("api-key-modal-message");
  const input = document.getElementById("api-key-modal-input");
  const saveButton = document.getElementById("api-key-modal-save");

  activeApiKeyRequirement = requirement;
  title.textContent = `${requirement.providerName} API Key Required`;
  label.textContent = `${requirement.providerName} API Key`;
  message.textContent = requirement.message;
  saveButton.disabled = false;
  modal.hidden = false;
  input.value = "";
  input.focus();
  setApiKeyModalStatus("");

  return new Promise((resolve) => {
    apiKeyModalResolver = resolve;
  });
}

async function saveRequiredApiKey(requirement, apiKey) {
  const saveButton = document.getElementById("api-key-modal-save");
  saveButton.disabled = true;
  setApiKeyModalStatus("Saving key...", "info");
  try {
    const config = await getSetupConfig();
    await callJson(
      "/setup/api/config",
      "PUT",
      buildSetupUpdatePayload(config, { [requirement.secretField]: apiKey }),
    );
    dashboardSetupConfig = null;
    const refreshedConfig = await getSetupConfig(true);
    if (!refreshedConfig.secrets?.[requirement.configuredField]) {
      throw new Error(`${requirement.providerName} key was not persisted.`);
    }
    hideApiKeyModal();
    resolveApiKeyModal(true);
    return true;
  } catch (error) {
    setApiKeyModalStatus(error.message, "error");
    saveButton.disabled = false;
    return false;
  }
}

async function ensureAgentRequirements(agentName) {
  const requirement = AGENT_LLM_REQUIREMENTS[agentName];
  if (!requirement) return true;

  const config = await getSetupConfig();
  if (config.secrets?.[requirement.configuredField]) {
    return true;
  }
  return openApiKeyModal(requirement);
}

function setRunStatusVisibility(visible) {
  const section = document.getElementById("live-status-section");
  if (section) section.hidden = !visible;
}

function setCancelButtonVisibility(visible) {
  const cancelButton = document.getElementById("cancel-run-button");
  cancelButton.hidden = !visible;
  if (!visible) {
    cancelButton.disabled = false;
    cancelButton.textContent = "Cancel Agent";
  }
}

function formatAgentName(agentName) {
  if (agentName === "job_intake") return "Jobs Agent";
  if (agentName === "job_scoring") return "Scoring Agent";
  if (agentName === "resume_generation") return "Resume Agent";
  if (agentName === "job_apply") return "Apply Agent";
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

function setRunStatus(message, level = "info", steps = [], meta = "") {
  const box = document.getElementById("run-status");
  const heading = document.getElementById("run-status-heading");
  const detail = document.getElementById("run-status-message");
  const metaNode = document.getElementById("run-status-meta");
  const stepsNode = document.getElementById("run-status-steps");
  const indicator = document.getElementById("run-status-indicator");

  box.dataset.level = level;
  heading.textContent = message;
  detail.textContent = "";
  metaNode.textContent = meta;
  indicator.hidden = level !== "info";
  setRunStatusVisibility(true);

  const sortedSteps = sortStepsForDisplay(steps);
  if (!sortedSteps.length) {
    stepsNode.innerHTML = `<li class="step-list-empty">Steps will appear here while an agent is running.</li>`;
    return;
  }

  stepsNode.innerHTML = sortedSteps
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

function renderRunStatus(run) {
  if (!run) {
    setRunStatusVisibility(false);
    setCancelButtonVisibility(false);
    setActionButtonsDisabled(false);
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

  const indicator = document.getElementById("run-status-indicator");
  const cancelButton = document.getElementById("cancel-run-button");
  const isActive = run.status === "queued" || run.status === "running";
  indicator.hidden = !isActive;
  setCancelButtonVisibility(isActive);
  cancelButton.disabled = Boolean(run.cancel_requested);
  cancelButton.textContent = run.cancel_requested ? "Stopping..." : "Cancel Agent";
  setRunStatusVisibility(isActive);
  setActionButtonsDisabled(isActive);
}

function findActiveRun(runs) {
  return runs.find((run) => run.status === "queued" || run.status === "running") || null;
}

async function fetchRuns() {
  const data = await callJson("/runs/", "GET");
  return data.runs || [];
}

async function pollRun(runId) {
  try {
    const run = await callJson(`/runs/${runId}`, "GET");
    renderRunStatus(run);

    if (run.status === "queued" || run.status === "running") {
      activePollTimer = window.setTimeout(() => pollRun(runId), 1000);
      return;
    }

    activeRunId = null;
    clearRunPolling();
    const runs = await fetchRuns();
    const nextActiveRun = findActiveRun(runs);
    if (nextActiveRun) {
      activeRunId = nextActiveRun.id;
      renderRunStatus(nextActiveRun);
      await pollRun(nextActiveRun.id);
      return;
    }

    setRunStatusVisibility(false);
    setCancelButtonVisibility(false);
    setActionButtonsDisabled(false);
  } catch (error) {
    activeRunId = null;
    clearRunPolling();
    setActionButtonsDisabled(false);
    setRunStatus(`Unable to poll run: ${error.message}`, "error");
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
  } catch (error) {
    cancelButton.disabled = false;
    cancelButton.textContent = "Cancel Agent";
    setRunStatus(`Unable to stop agent: ${error.message}`, "error");
  }
}

async function queueAndNavigate(agentAction, href) {
  if (agentAction && !(await ensureAgentRequirements(agentAction))) {
    setRunStatus(`${formatAgentName(agentAction)} requires an API key before you run it manually.`, "error");
    return;
  }

  setActionButtonsDisabled(true);
  try {
    if (agentAction === "job_intake") {
      await callJson("/jobs/intake/start", "POST", {
        search_urls: [],
        max_jobs_per_search: null,
      });
    } else if (agentAction === "job_scoring") {
      await callJson("/scoring/start", "POST", { job_ids: [] });
    }
    window.location.assign(href);
  } catch (error) {
    setRunStatus(error.message, "error");
    setActionButtonsDisabled(false);
  }
}

function setActionButtonsDisabled(disabled) {
  document.querySelectorAll(".dashboard-workflow-card[data-action]").forEach((card) => {
    const action = card.dataset.action || "";
    if (!action) return;
    card.classList.toggle("is-disabled", disabled);
    card.style.pointerEvents = disabled ? "none" : "";
  });
}

function bindWorkflowButtons() {
  document.querySelectorAll(".dashboard-workflow-card").forEach((card) => {
    card.style.cursor = "pointer";
    card.addEventListener("click", async () => {
      if (card.classList.contains("is-disabled")) return;
      const agentAction = card.dataset.action || "";
      const href = card.dataset.href || "/";
      await queueAndNavigate(agentAction || null, href);
    });
  });
}

window.addEventListener("DOMContentLoaded", async () => {
  hideApiKeyModal();
  bindWorkflowButtons();
  document.getElementById("cancel-run-button").addEventListener("click", cancelActiveRun);
  document.getElementById("api-key-modal-cancel").addEventListener("click", () => {
    hideApiKeyModal();
    resolveApiKeyModal(false);
  });
  document.getElementById("api-key-modal-save").addEventListener("click", async () => {
    const input = document.getElementById("api-key-modal-input");
    const value = input.value.trim();
    if (!value || !activeApiKeyRequirement) {
      setApiKeyModalStatus("Enter an API key.", "error");
      return;
    }
    await saveRequiredApiKey(activeApiKeyRequirement, value);
  });
  document.getElementById("api-key-modal-input").addEventListener("keydown", async (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      const input = document.getElementById("api-key-modal-input");
      const value = input.value.trim();
      if (!value || !activeApiKeyRequirement) {
        setApiKeyModalStatus("Enter an API key.", "error");
        return;
      }
      await saveRequiredApiKey(activeApiKeyRequirement, value);
    } else if (event.key === "Escape") {
      event.preventDefault();
      hideApiKeyModal();
      resolveApiKeyModal(false);
    }
  });

  try {
    const runs = await fetchRuns();
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
    setRunStatus(`Unable to load active run state: ${error.message}`, "error");
  }
});
