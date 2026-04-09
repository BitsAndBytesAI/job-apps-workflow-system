function splitSearchUrls(rawValue) {
  return rawValue
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
}

function formDataToPayload(form) {
  return {
    google: {
      resources: {
        job_emails_sent_sheet: form["google.resources.job_emails_sent_sheet"].value,
        interview_emails_sheet: form["google.resources.interview_emails_sheet"].value,
        base_resume_doc: form["google.resources.base_resume_doc"].value,
      },
    },
    linkedin: {
      browser_profile_path: form["linkedin.browser_profile_path"].value,
      search_urls: splitSearchUrls(form["linkedin.search_urls"].value),
    },
    models: {
      openai_model: form["models.openai_model"].value,
      anthropic_model: form["models.anthropic_model"].value,
    },
    app: {
      project_id: form["app.project_id"].value,
      job_role: form["app.job_role"].value,
      schedule_minutes: Number(form["app.schedule_minutes"].value || 25),
      score_threshold: Number(form["app.score_threshold"].value || 82),
      dry_run: form["app.dry_run"].checked,
      send_enabled: form["app.send_enabled"].checked,
      send_bcc: form["app.send_bcc"].value,
    },
    secrets: {
      openai_api_key: form["secrets.openai_api_key"].value || null,
      anthropic_api_key: form["secrets.anthropic_api_key"].value || null,
      anymailfinder_api_key: form["secrets.anymailfinder_api_key"].value || null,
    },
  };
}

function populateForm(config) {
  const form = document.getElementById("setup-form");
  form["google.resources.job_emails_sent_sheet"].value = config.google.resources.job_emails_sent_sheet || "";
  form["google.resources.interview_emails_sheet"].value = config.google.resources.interview_emails_sheet || "";
  form["google.resources.base_resume_doc"].value = config.google.resources.base_resume_doc || "";
  form["linkedin.browser_profile_path"].value = config.linkedin.browser_profile_path || "";
  form["linkedin.search_urls"].value = (config.linkedin.search_urls || []).join("\n");
  form["models.openai_model"].value = config.models.openai_model || "";
  form["models.anthropic_model"].value = config.models.anthropic_model || "";
  form["app.project_id"].value = config.app.project_id || "";
  form["app.job_role"].value = config.app.job_role || "";
  document.getElementById("project-name-display").textContent = config.app.project_id || "—";
  document.getElementById("job-role-display").textContent = config.app.job_role || "—";
  form["app.schedule_minutes"].value = config.app.schedule_minutes ?? 25;
  form["app.score_threshold"].value = config.app.score_threshold ?? 82;
  form["app.dry_run"].checked = Boolean(config.app.dry_run);
  form["app.send_enabled"].checked = Boolean(config.app.send_enabled);
  form["app.send_bcc"].value = config.app.send_bcc || "";

  applyStoredFieldValidations(config.field_validations || {});
  showSecretConfiguredStatus("secrets.openai_api_key", config.secrets.openai_api_key_configured);
  showSecretConfiguredStatus("secrets.anthropic_api_key", config.secrets.anthropic_api_key_configured);
  showSecretConfiguredStatus("secrets.anymailfinder_api_key", config.secrets.anymailfinder_api_key_configured);
}

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

function setGlobalStatus(message, level = "info") {
  const output = document.getElementById("setup-output");
  output.hidden = false;
  output.textContent = message;
  output.dataset.level = level;
}

function clearGlobalStatus() {
  const output = document.getElementById("setup-output");
  output.hidden = true;
  output.textContent = "";
  output.dataset.level = "";
}

function setFieldStatus(fieldName, message, level = "info") {
  const status = document.querySelector(`.field-status[data-field-name="${fieldName}"]`);
  const field = document.querySelector(`[name="${fieldName}"]`);
  if (!status) return;
  status.hidden = false;
  status.textContent = message;
  status.dataset.level = level;
  if (field) {
    field.classList.remove("validated-success", "validated-error", "validated-info");
    field.classList.add(`validated-${level}`);
  }
}

function clearFieldStatus(fieldName) {
  const status = document.querySelector(`.field-status[data-field-name="${fieldName}"]`);
  const field = document.querySelector(`[name="${fieldName}"]`);
  if (!status) return;
  status.hidden = true;
  status.textContent = "";
  status.dataset.level = "";
  if (field) {
    field.classList.remove("validated-success", "validated-error", "validated-info");
  }
}

function showSecretConfiguredStatus(fieldName, configured) {
  if (configured) {
    setFieldStatus(fieldName, "Key already configured.", "info");
  } else {
    clearFieldStatus(fieldName);
  }
}

function setFieldBusy(fieldName, isBusy) {
  const button = document.querySelector(`.inline-validate-button[data-field-name="${fieldName}"]`);
  if (!button) return;
  button.disabled = isBusy;
  button.textContent = isBusy ? "Validating..." : "Validate";
}

async function loadConfig() {
  const config = await callJson("/setup/api/config", "GET");
  populateForm(config);
  await loadGoogleStatus();
  await loadLinkedInStatus();
  await autoValidateSavedGoogleResources();
}

async function loadGoogleStatus() {
  const status = await callJson("/setup/api/google/auth/status", "GET");
  document.getElementById("google-status").textContent =
    `Google connected=${status.connected}, clientConfigured=${status.client_configured}, redirectUri=${status.redirect_uri}`;
}

function setLinkedInStatus(message, level = "info") {
  const box = document.getElementById("linkedin-status");
  box.textContent = message;
  box.dataset.level = level;
}

let linkedInSessionPollHandle = null;
let linkedInBrowserPid = null;

function setLinkedInButtonState(authenticated) {
  const button = document.getElementById("linkedin-connect-button");
  if (!button) return;
  button.disabled = Boolean(authenticated);
  button.textContent = authenticated ? "LinkedIn Connected" : "Connect LinkedIn";
}

async function validateField(fieldName) {
  const form = document.getElementById("setup-form");
  const payload = {
    field_name: fieldName,
    payload: formDataToPayload(form),
  };

  setFieldBusy(fieldName, true);
  clearGlobalStatus();
  try {
    const response = await callJson("/setup/api/field-validate", "POST", payload);
    setFieldStatus(fieldName, response.message, response.level || "info");
  } catch (error) {
    setFieldStatus(fieldName, error.message, "error");
  } finally {
    setFieldBusy(fieldName, false);
  }
}

function applyStoredFieldValidations(validations) {
  Object.entries(validations).forEach(([fieldName, validation]) => {
    if (!document.querySelector(`[name="${fieldName}"]`)) {
      return;
    }
    setFieldStatus(fieldName, validation.message, validation.level || "info");
  });
}

async function autoValidateSavedGoogleResources() {
  const googleFields = Array.from(document.querySelectorAll('[name^="google.resources."]'));
  for (const field of googleFields) {
    if (!field.value.trim()) {
      continue;
    }
    await validateField(field.name);
  }
}

async function saveConfig(event) {
  event.preventDefault();
  const form = document.getElementById("setup-form");
  const payload = formDataToPayload(form);

  try {
    const response = await callJson("/setup/api/config", "PUT", payload);
    populateForm(response);
    form["secrets.openai_api_key"].value = "";
    form["secrets.anthropic_api_key"].value = "";
    form["secrets.anymailfinder_api_key"].value = "";
    setGlobalStatus("Configuration saved.", "success");
    await loadGoogleStatus();
  } catch (error) {
    setGlobalStatus(error.message, "error");
  }
}

async function connectLinkedIn() {
  const form = document.getElementById("setup-form");
  const payload = formDataToPayload(form);
  try {
    const auth = await callJson("/setup/api/linkedin/auth/check", "POST", payload);
    if (auth.authenticated) {
      stopLinkedInSessionPolling();
      linkedInBrowserPid = null;
      setLinkedInStatus(
        `LinkedIn already connected. Cookies=${auth.cookie_count} Profile=${auth.profile_path}`,
        "success",
      );
      setLinkedInButtonState(true);
      return;
    }

    const response = await callJson("/setup/api/linkedin/browser/launch", "POST", payload);
    linkedInBrowserPid = response.pid ?? null;
    setLinkedInStatus(
      `${response.message} Sign in to LinkedIn in that browser. Session status will update automatically. PID=${response.pid ?? "n/a"} Profile=${response.profile_path}`,
      "success",
    );
    startLinkedInSessionPolling();
  } catch (error) {
    setLinkedInStatus(error.message, "error");
  }
}

async function fetchLinkedInAuthStatus(updateStatus = true) {
  const form = document.getElementById("setup-form");
  const payload = formDataToPayload(form);
  const response = await callJson("/setup/api/linkedin/auth/check", "POST", payload);
  if (updateStatus) {
    setLinkedInStatus(
      `${response.message} Cookies=${response.cookie_count} Profile=${response.profile_path}`,
      response.authenticated ? "success" : "error",
    );
  }
  setLinkedInButtonState(response.authenticated);
  return response;
}

async function loadLinkedInStatus() {
  try {
    await fetchLinkedInAuthStatus(true);
  } catch (error) {
    setLinkedInStatus(error.message, "error");
    setLinkedInButtonState(false);
  }
}

async function closeLinkedInBrowserIfNeeded() {
  if (!linkedInBrowserPid) {
    return false;
  }
  try {
    const response = await callJson("/setup/api/linkedin/browser/terminate", "POST", { pid: linkedInBrowserPid });
    linkedInBrowserPid = null;
    return Boolean(response.ok);
  } catch (error) {
    setLinkedInStatus(error.message, "error");
    return false;
  }
}

function stopLinkedInSessionPolling() {
  if (linkedInSessionPollHandle) {
    clearInterval(linkedInSessionPollHandle);
    linkedInSessionPollHandle = null;
  }
}

function startLinkedInSessionPolling() {
  stopLinkedInSessionPolling();
  let attempts = 0;
  linkedInSessionPollHandle = setInterval(async () => {
    attempts += 1;
    try {
      const response = await fetchLinkedInAuthStatus(true);
      if (response.authenticated) {
        stopLinkedInSessionPolling();
        const closed = await closeLinkedInBrowserIfNeeded();
        if (closed) {
          setLinkedInStatus(
            `${response.message} Closed the LinkedIn browser automatically. Cookies=${response.cookie_count} Profile=${response.profile_path}`,
            "success",
          );
        }
        return;
      }
      if (attempts >= 120) {
        stopLinkedInSessionPolling();
      }
    } catch (error) {
      setLinkedInStatus(error.message, "error");
      if (attempts >= 5) {
        stopLinkedInSessionPolling();
      }
    }
  }, 3000);
}

function enhanceFieldRows() {
  const fields = document.querySelectorAll("#setup-form input[name], #setup-form textarea[name], #setup-form select[name]");
  fields.forEach((field) => {
    if (field.type === "checkbox") return;
    const label = field.closest("label");
    if (!label || label.dataset.enhanced === "true") {
      return;
    }

    const labelText = Array.from(label.childNodes)
      .filter((node) => node.nodeType === Node.TEXT_NODE)
      .map((node) => node.textContent.trim())
      .join(" ")
      .trim();

    label.dataset.enhanced = "true";
    label.classList.add("setup-field");
    label.textContent = "";

    const title = document.createElement("span");
    title.className = "field-label";
    title.textContent = labelText;

    const controls = document.createElement("div");
    controls.className = "field-controls";

    const button = document.createElement("button");
    button.type = "button";
    button.className = "inline-validate-button";
    button.dataset.fieldName = field.name;
    button.textContent = "Validate";
    button.addEventListener("click", () => validateField(field.name));

    const status = document.createElement("div");
    status.className = "field-status";
    status.dataset.fieldName = field.name;
    status.hidden = true;

    field.addEventListener("input", () => clearFieldStatus(field.name));
    field.addEventListener("change", () => clearFieldStatus(field.name));

    controls.appendChild(field);
    controls.appendChild(button);

    label.appendChild(title);
    label.appendChild(controls);
    label.appendChild(status);
  });
}

window.addEventListener("DOMContentLoaded", () => {
  enhanceFieldRows();
  document.getElementById("setup-form").addEventListener("submit", saveConfig);
  document.getElementById("google-connect-button").addEventListener("click", () => {
    window.location.href = "/setup/api/google/auth/start";
  });
  document.getElementById("linkedin-connect-button").addEventListener("click", connectLinkedIn);
  loadConfig();

  // Font size controls (uses cookie for WKWebView compatibility)
  const DEFAULT_FONT_SIZE = 15;
  const MIN_FONT_SIZE = 12;
  const MAX_FONT_SIZE = 22;

  function getCurrentFontSize() {
    const m = document.cookie.match(/(?:^|; )app-font-size=(\d+)/);
    return m ? parseInt(m[1], 10) : DEFAULT_FONT_SIZE;
  }

  function setFontSize(size) {
    size = Math.max(MIN_FONT_SIZE, Math.min(MAX_FONT_SIZE, size));
    document.documentElement.style.fontSize = size + 'px';
    document.cookie = 'app-font-size=' + size + '; path=/; max-age=31536000; SameSite=Lax';
    document.getElementById('font-size-display').textContent = size + 'px';
    document.getElementById('font-size-down').disabled = size <= MIN_FONT_SIZE;
    document.getElementById('font-size-up').disabled = size >= MAX_FONT_SIZE;
  }

  setFontSize(getCurrentFontSize());

  document.getElementById('font-size-up').addEventListener('click', () => setFontSize(getCurrentFontSize() + 1));
  document.getElementById('font-size-down').addEventListener('click', () => setFontSize(getCurrentFontSize() - 1));
  document.getElementById('font-size-reset').addEventListener('click', () => setFontSize(DEFAULT_FONT_SIZE));
});
