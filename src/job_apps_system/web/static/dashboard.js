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

let dashboardSetupConfig = null;
let apiKeyModalResolver = null;
let activeApiKeyRequirement = null;

const AGENT_LLM_REQUIREMENTS = {
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

function buildNavigationTarget(href, params = {}) {
  const url = new URL(href, window.location.origin);
  Object.entries(params).forEach(([key, value]) => {
    if (value == null || value === "") {
      url.searchParams.delete(key);
      return;
    }
    url.searchParams.set(key, value);
  });
  return `${url.pathname}${url.search}${url.hash}`;
}

function setActionButtonsDisabled(disabled) {
  document.querySelectorAll(".dashboard-workflow-button[data-action]").forEach((button) => {
    button.disabled = disabled;
    button.closest(".dashboard-workflow-card")?.classList.toggle("is-disabled", disabled);
  });
}

async function queueAndNavigate(agentAction, href) {
  if (agentAction && !(await ensureAgentRequirements(agentAction))) {
    return;
  }

  setActionButtonsDisabled(true);
  try {
    if (agentAction === "job_intake") {
      const run = await callJson("/jobs/intake/start", "POST", {
        search_urls: [],
        max_jobs_per_search: null,
      });
      window.location.assign(buildNavigationTarget(href, { run: run.id || "" }));
      return;
    }
    if (agentAction === "job_scoring") {
      const run = await callJson("/scoring/start", "POST", { job_ids: [] });
      window.location.assign(buildNavigationTarget(href, { run: run.id || "" }));
      return;
    }
    window.location.assign(href);
  } catch (error) {
    setActionButtonsDisabled(false);
    window.alert(error.message);
  }
}

function bindWorkflowButtons() {
  document.querySelectorAll(".dashboard-workflow-button").forEach((button) => {
    button.addEventListener("click", async (event) => {
      event.preventDefault();
      event.stopPropagation();
      if (button.disabled) return;
      const agentAction = button.dataset.action || "";
      const href = button.dataset.href || "/";
      await queueAndNavigate(agentAction || null, href);
    });
  });

  document.querySelectorAll(".dashboard-workflow-card[data-action][data-href]").forEach((card) => {
    card.style.cursor = "pointer";
    card.addEventListener("click", async (event) => {
      if (event.target.closest("button, a, input, select, textarea")) return;
      if (card.classList.contains("is-disabled")) return;
      const agentAction = card.dataset.action || "";
      const href = card.dataset.href || "/";
      await queueAndNavigate(agentAction || null, href);
    });
  });
}

window.addEventListener("DOMContentLoaded", () => {
  hideApiKeyModal();
  bindWorkflowButtons();
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
});
