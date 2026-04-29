function splitSearchUrls(rawValue) {
  return rawValue
    .split("\n")
    .map((value) => value.trim())
    .filter(Boolean);
}

let currentSetupConfig = null;

function formatThresholdPercent(rawValue) {
  const value = Number(rawValue);
  if (!Number.isFinite(value)) return "82.0";
  return (value / 10).toFixed(1);
}

function parseThresholdPercent(value, fallback = 820) {
  const parsed = Number.parseFloat(String(value ?? "").trim());
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(0, Math.min(1000, Math.round(parsed * 10)));
}

function formDataToPayload(form) {
  const googleResources = currentSetupConfig?.google?.resources || {};
  const appConfig = currentSetupConfig?.app || {};
  return {
    onboarding: {
      wizard_completed: form["onboarding.wizard_completed"].value === "true",
      wizard_current_step: form["onboarding.wizard_current_step"].value || "project",
    },
    google: {
      resources: {
        job_emails_sent_sheet: googleResources.job_emails_sent_sheet || null,
        interview_emails_sheet: googleResources.interview_emails_sheet || null,
        base_resume_doc: googleResources.base_resume_doc || null,
      },
    },
    linkedin: {
      browser_profile_path: form["linkedin.browser_profile_path"].value || currentSetupConfig?.linkedin?.browser_profile_path || "browser-profiles/linkedin-firefox",
      search_urls: splitSearchUrls(form["linkedin.search_urls"].value),
    },
    models: {
      openai_model: form["models.openai_model"].value,
      anthropic_model: form["models.anthropic_model"].value,
    },
    project_resume: {
      source_type: form["project_resume.source_type"].value || null,
      source_url: form["project_resume.source_url"].value || null,
      original_file_name: form["project_resume.original_file_name"].value || null,
      original_file_path: form["project_resume.original_file_path"].value || null,
      extracted_text: form["project_resume.extracted_text"].value || null,
    },
    applicant: {
      legal_name: form["applicant.legal_name"].value,
      preferred_name: form["applicant.preferred_name"].value,
      email: form["applicant.email"].value,
      phone: form["applicant.phone"].value,
      linkedin_url: form["applicant.linkedin_url"].value,
      portfolio_url: form["applicant.portfolio_url"].value,
      github_url: form["applicant.github_url"].value,
      current_company: form["applicant.current_company"].value,
      current_title: form["applicant.current_title"].value,
      years_of_experience: form["applicant.years_of_experience"].value,
      address_line_1: form["applicant.address_line_1"].value,
      address_line_2: form["applicant.address_line_2"].value,
      city: form["applicant.city"].value,
      state: form["applicant.state"].value,
      postal_code: form["applicant.postal_code"].value,
      country: form["applicant.country"].value,
      work_authorized_us: form["applicant.work_authorized_us"].checked,
      requires_sponsorship: form["applicant.requires_sponsorship"].checked,
      compensation_expectation: form["applicant.compensation_expectation"].value,
      programming_languages_years: form["applicant.programming_languages_years"].value,
      favorite_ai_tool: form["applicant.favorite_ai_tool"].value,
      favorite_ai_tool_usage: form["applicant.favorite_ai_tool_usage"].value,
      company_value_example: form["applicant.company_value_example"].value,
      why_interested_guidance: form["applicant.why_interested_guidance"].value,
      additional_info_guidance: form["applicant.additional_info_guidance"].value,
      sms_consent: form["applicant.sms_consent"].checked,
      custom_answer_guidance: form["applicant.custom_answer_guidance"].value,
    },
    app: {
      project_name: form["app.project_name"].value,
      project_id: form["app.project_name"].value || form["app.job_role"].value || form["app.project_id"].value,
      job_role: form["app.job_role"].value,
      selected_job_sites: form["app.selected_job_sites.linkedin"].checked ? ["linkedin"] : [],
      schedule_minutes: Number(form["app.schedule_minutes"]?.value || appConfig.schedule_minutes || 25),
      max_jobs_per_run: Number(form["app.max_jobs_per_run"]?.value || appConfig.max_jobs_per_run || 10),
      score_threshold: parseThresholdPercent(form["app.score_threshold"].value, appConfig.score_threshold || 820),
      hide_jobs_below_score_threshold: form["app.hide_jobs_below_score_threshold"].checked,
      dry_run: form["app.dry_run"].checked,
      send_enabled: Boolean(appConfig.send_enabled),
      send_bcc: appConfig.send_bcc || null,
      apply_default_limit: Number(form["app.apply_default_limit"]?.value || appConfig.apply_default_limit || 1),
      apply_headless: form["app.apply_headless"]?.checked ?? Boolean(appConfig.apply_headless),
      apply_auto_submit: form["app.apply_auto_submit"]?.checked ?? (appConfig.apply_auto_submit ?? true),
      apply_debug_retain_success_logs: form["app.apply_debug_retain_success_logs"]?.checked ?? Boolean(appConfig.apply_debug_retain_success_logs),
      apply_choice_behavior: (form.querySelector("input[name='app.apply_choice_behavior']:checked")?.value)
        ?? appConfig.apply_choice_behavior
        ?? "always_ai",
      intake_title_blocklist: (form["app.intake_title_blocklist"]?.value || "")
        .split("\n")
        .map((line) => line.trim())
        .filter((line) => line.length > 0),
    },
    email_templates: {
      last_subject: form["email_templates.last_subject"]?.value || "",
      last_body: form["email_templates.last_body"]?.value || "",
      bcc_self: form["email_templates.bcc_self"]?.checked ?? false,
    },
    secrets: {
      openai_api_key: form["secrets.openai_api_key"].value || null,
      anthropic_api_key: form["secrets.anthropic_api_key"].value || null,
      anymailfinder_api_key: form["secrets.anymailfinder_api_key"].value || null,
    },
  };
}

function populateForm(config) {
  currentSetupConfig = config;
  const form = document.getElementById("setup-form");
  form["linkedin.browser_profile_path"].value = config.linkedin.browser_profile_path || "";
  form["linkedin.search_urls"].value = (config.linkedin.search_urls || []).join("\n");
  form["models.openai_model"].value = config.models.openai_model || "";
  form["models.anthropic_model"].value = config.models.anthropic_model || "";
  form["onboarding.wizard_completed"].value = String(Boolean(config.onboarding.wizard_completed));
  form["onboarding.wizard_current_step"].value = config.onboarding.wizard_current_step || "project";
  form["project_resume.source_type"].value = config.project_resume.source_type || "";
  form["project_resume.source_url"].value = config.project_resume.source_url || "";
  form["project_resume.original_file_name"].value = config.project_resume.original_file_name || "";
  form["project_resume.original_file_path"].value = config.project_resume.original_file_path || "";
  form["project_resume.extracted_text"].value = config.project_resume.extracted_text || "";
  const emailTemplates = config.email_templates || {};
  if (form["email_templates.last_subject"]) {
    form["email_templates.last_subject"].value = emailTemplates.last_subject || "";
  }
  if (form["email_templates.last_body"]) {
    form["email_templates.last_body"].value = emailTemplates.last_body || "";
  }
  if (form["email_templates.bcc_self"]) {
    form["email_templates.bcc_self"].checked = Boolean(emailTemplates.bcc_self);
  }
  populateApplicantForm(form, config.applicant || {});
  form["app.project_name"].value = config.app.project_name || "";
  form["app.project_id"].value = config.app.project_id || "";
  form["app.job_role"].value = config.app.job_role || "";
  form["app.selected_job_sites.linkedin"].checked = (config.app.selected_job_sites || []).includes("linkedin");
  document.getElementById("wizard-completed-display").textContent = config.onboarding.wizard_completed ? "Yes" : "No";
  document.getElementById("wizard-step-display").textContent = config.onboarding.wizard_current_step || "—";
  if (form["app.schedule_minutes"]) form["app.schedule_minutes"].value = config.app.schedule_minutes ?? 25;
  if (form["app.max_jobs_per_run"]) form["app.max_jobs_per_run"].value = config.app.max_jobs_per_run ?? 10;
  form["app.score_threshold"].value = formatThresholdPercent(config.app.score_threshold ?? 820);
  form["app.hide_jobs_below_score_threshold"].checked = config.app.hide_jobs_below_score_threshold ?? true;
  form["app.dry_run"].checked = Boolean(config.app.dry_run);
  if (form["app.apply_default_limit"]) form["app.apply_default_limit"].value = config.app.apply_default_limit ?? 1;
  if (form["app.apply_headless"]) form["app.apply_headless"].checked = Boolean(config.app.apply_headless);
  if (form["app.apply_auto_submit"]) form["app.apply_auto_submit"].checked = config.app.apply_auto_submit ?? true;
  if (form["app.apply_debug_retain_success_logs"]) form["app.apply_debug_retain_success_logs"].checked = Boolean(config.app.apply_debug_retain_success_logs);
  const applyChoice = config.app.apply_choice_behavior || "always_ai";
  const applyChoiceInput = form.querySelector(`input[name='app.apply_choice_behavior'][value='${applyChoice}']`);
  if (applyChoiceInput) applyChoiceInput.checked = true;
  if (form["app.intake_title_blocklist"]) {
    const list = Array.isArray(config.app.intake_title_blocklist)
      ? config.app.intake_title_blocklist
      : [];
    form["app.intake_title_blocklist"].value = list.join("\n");
  }

  applyStoredFieldValidations(config.field_validations || {});
  renderHelperStatus(config.secrets.helper || {});
  showSecretConfiguredStatus("secrets.openai_api_key", config.secrets.openai_api_key);
  showSecretConfiguredStatus("secrets.anthropic_api_key", config.secrets.anthropic_api_key);
  showSecretConfiguredStatus("secrets.anymailfinder_api_key", config.secrets.anymailfinder_api_key);
}

function populateApplicantForm(form, applicant) {
  const fields = [
    "legal_name",
    "preferred_name",
    "email",
    "phone",
    "linkedin_url",
    "portfolio_url",
    "github_url",
    "current_company",
    "current_title",
    "years_of_experience",
    "address_line_1",
    "address_line_2",
    "city",
    "state",
    "postal_code",
    "country",
    "compensation_expectation",
    "programming_languages_years",
    "favorite_ai_tool",
    "favorite_ai_tool_usage",
    "company_value_example",
    "why_interested_guidance",
    "additional_info_guidance",
    "custom_answer_guidance",
  ];
  fields.forEach((field) => {
    const input = form[`applicant.${field}`];
    if (input) input.value = applicant[field] || "";
  });
  if (form["applicant.work_authorized_us"]) form["applicant.work_authorized_us"].checked = applicant.work_authorized_us ?? true;
  if (form["applicant.requires_sponsorship"]) form["applicant.requires_sponsorship"].checked = Boolean(applicant.requires_sponsorship);
  if (form["applicant.sms_consent"]) form["applicant.sms_consent"].checked = Boolean(applicant.sms_consent);
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
    const error = new Error(
      typeof detail === "object" && detail !== null
        ? detail.message || `${response.status} ${response.statusText}`
        : detail || `${response.status} ${response.statusText}`,
    );
    if (typeof detail === "object" && detail !== null) {
      error.code = detail.code;
      error.detail = detail;
    }
    throw error;
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

function renderHelperStatus(helper) {
  const box = document.getElementById("setup-secret-helper-status");
  if (!box) return;
  box.textContent = helper?.status_message || "Secret helper status unavailable.";
  box.dataset.level = helper?.healthy ? "success" : "error";
}

function showSecretConfiguredStatus(fieldName, status) {
  if (status?.configured) {
    setFieldStatus(fieldName, status.status_message || "Key stored and ready.", "success");
  } else {
    clearFieldStatus(fieldName);
    if (status?.status_code && status.status_code !== "missing_secret") {
      setFieldStatus(fieldName, status.status_message || "Secret status unavailable.", "error");
    }
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
}

async function loadGoogleStatus() {
  const status = await callJson("/setup/api/google/auth/status", "GET");
  const box = document.getElementById("google-status");
  if (status.connected) {
    box.textContent = "Google connected.";
    box.dataset.level = "success";
    setGoogleButtonState(true);
    return;
  }
  if (status.client_configured) {
    box.textContent = "Connect Google to enable Docs and Drive.";
    box.dataset.level = "info";
    setGoogleButtonState(false);
    return;
  }
  box.textContent = "Google client configuration is missing.";
  box.dataset.level = "error";
  setGoogleButtonState(false, false);
}

function setGoogleButtonState(connected, clientConfigured = true) {
  const button = document.getElementById("google-connect-button");
  if (!button) return;
  button.disabled = Boolean(connected) || !clientConfigured;
  button.textContent = connected ? "Google Connected" : "Connect Google";
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
    await loadLinkedInStatus();
  } catch (error) {
    setGlobalStatus(error.message, "error");
  }
}

async function rerunSetupWizard() {
  const button = document.getElementById("rerun-setup-wizard-button");
  clearGlobalStatus();
  if (button) {
    button.disabled = true;
    button.textContent = "Opening Wizard...";
  }
  try {
    const response = await callJson("/setup/api/onboarding/restart", "POST");
    window.location.href = response.redirect_to || "/onboarding/";
  } catch (error) {
    if (button) {
      button.disabled = false;
      button.textContent = "Run Setup Wizard Again";
    }
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

function fieldNeedsValidateButton(fieldName) {
  void fieldName;
  return false;
}

function fieldNeedsStatus(fieldName) {
  return (
    fieldNeedsValidateButton(fieldName) ||
    fieldName.startsWith("secrets.")
  );
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

    const showStatus = fieldNeedsStatus(field.name);
    if (showStatus) {
      field.addEventListener("input", () => clearFieldStatus(field.name));
      field.addEventListener("change", () => clearFieldStatus(field.name));
    }

    controls.appendChild(field);
    if (fieldNeedsValidateButton(field.name)) {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "inline-validate-button";
      button.dataset.fieldName = field.name;
      button.textContent = "Validate";
      button.addEventListener("click", () => validateField(field.name));
      controls.appendChild(button);
    }

    label.appendChild(title);
    label.appendChild(controls);
    if (showStatus) {
      const status = document.createElement("div");
      status.className = "field-status";
      status.dataset.fieldName = field.name;
      status.hidden = true;
      label.appendChild(status);
    }
  });
}

window.addEventListener("DOMContentLoaded", () => {
  enhanceFieldRows();
  document.getElementById("setup-form").addEventListener("submit", saveConfig);
  document.getElementById("rerun-setup-wizard-button").addEventListener("click", rerunSetupWizard);
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
