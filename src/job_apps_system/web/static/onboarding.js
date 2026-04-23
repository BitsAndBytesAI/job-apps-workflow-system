async function callJson(url, method, payload) {
  const response = await fetch(url, {
    method,
    headers: payload instanceof FormData ? undefined : { "Content-Type": "application/json" },
    body: payload ? (payload instanceof FormData ? payload : JSON.stringify(payload)) : undefined,
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

let onboardingState = window.onboardingConfig || { currentStep: "project", config: null };
let linkedInPollHandle = null;
let linkedInBrowserPid = null;
let wizardSubmitting = false;
const MASKED_SECRET_VALUE = "********";
const STEP_ORDER = ["project", "resume", "job-sites", "models", "anymailfinder", "score-threshold", "applicant", "google"];

function setGlobalStatus(message, level = "info") {
  const box = document.getElementById("wizard-global-status");
  const messageBox = document.getElementById("wizard-global-status-message");
  const googleDocActions = document.getElementById("wizard-google-doc-actions");
  box.hidden = !message;
  if (messageBox) messageBox.textContent = message || "";
  if (googleDocActions) googleDocActions.hidden = true;
  box.dataset.level = level;
}

function setGoogleDocAccessStatus() {
  const box = document.getElementById("wizard-global-status");
  const messageBox = document.getElementById("wizard-global-status-message");
  const googleDocActions = document.getElementById("wizard-google-doc-actions");
  box.hidden = false;
  box.dataset.level = "error";
  messageBox.textContent = "Please make your resume doc publicly accessible or click here to connect your Google Account.";
  googleDocActions.hidden = false;
}

function setLinkedInStatus(message, level = "info") {
  if (onboardingState.currentStep === "job-sites") {
    setGlobalStatus(message, level);
  }
}

function getContinueLabel(stepId, submitting = false) {
  if (submitting) return stepId === "google" ? "Finishing Setup..." : "Working...";
  return stepId === "google" ? "Finish Setup" : "Continue";
}

function updateWizardNavigation() {
  const backButton = document.getElementById("wizard-back");
  const continueButton = document.getElementById("wizard-continue");
  const stepIndex = STEP_ORDER.indexOf(onboardingState.currentStep);
  if (backButton) backButton.disabled = wizardSubmitting || stepIndex <= 0;
  if (continueButton) {
    continueButton.disabled = wizardSubmitting;
    continueButton.textContent = getContinueLabel(onboardingState.currentStep, wizardSubmitting);
  }
}

function setWizardSubmitting(isSubmitting) {
  wizardSubmitting = isSubmitting;
  updateWizardNavigation();
}

function applyMaskedSecretInput(inputId, configured) {
  const input = document.getElementById(inputId);
  if (!input) return;
  if (configured) {
    if (!input.value || input.value === MASKED_SECRET_VALUE || input.dataset.maskedSecret === "true") {
      input.value = MASKED_SECRET_VALUE;
      input.dataset.maskedSecret = "true";
    }
  } else if (input.dataset.maskedSecret === "true") {
    input.value = "";
    input.dataset.maskedSecret = "false";
  }
}

function getSecretInputValue(inputId) {
  const input = document.getElementById(inputId);
  if (!input) return "";
  const value = input.value.trim();
  if (input.dataset.maskedSecret === "true" && value === MASKED_SECRET_VALUE) {
    return "";
  }
  return value;
}

function setupMaskedSecretInput(inputId, isConfigured) {
  const input = document.getElementById(inputId);
  if (!input) return;
  input.addEventListener("focus", () => {
    if (input.dataset.maskedSecret === "true" && input.value === MASKED_SECRET_VALUE) {
      input.value = "";
      input.dataset.maskedSecret = "false";
    }
  });
  input.addEventListener("blur", () => {
    if (!input.value.trim() && isConfigured()) {
      input.value = MASKED_SECRET_VALUE;
      input.dataset.maskedSecret = "true";
    }
  });
}

function showStep(stepId) {
  onboardingState.currentStep = stepId;
  document.querySelectorAll("[data-step-panel]").forEach((panel) => {
    panel.hidden = panel.dataset.stepPanel !== stepId;
  });
  document.querySelectorAll("[data-step-id]").forEach((node) => {
    const nodeStepIndex = STEP_ORDER.indexOf(node.dataset.stepId);
    const currentStepIndex = STEP_ORDER.indexOf(stepId);
    if (nodeStepIndex < currentStepIndex) {
      node.dataset.state = "complete";
    } else if (node.dataset.stepId === stepId) {
      node.dataset.state = "active";
    } else {
      node.dataset.state = "upcoming";
    }
  });
  updateWizardNavigation();
}

function showGoogleDocModal() {
  const modal = document.getElementById("wizard-google-doc-modal");
  if (modal) modal.hidden = false;
}

function hideGoogleDocModal() {
  const modal = document.getElementById("wizard-google-doc-modal");
  if (modal) modal.hidden = true;
}

async function refreshState() {
  onboardingState.config = await callJson("/onboarding/api/state", "GET");
  showStep(onboardingState.config.onboarding.wizard_current_step);
  applyConfigToWizard();
}

function applyConfigToWizard() {
  const config = onboardingState.config;
  if (!config) return;

  const jobRole = document.getElementById("wizard-job-role");
  const resumeLink = document.getElementById("wizard-resume-link");
  const existingResume = document.getElementById("wizard-existing-resume");
  const linkedInSearchSection = document.getElementById("wizard-linkedin-search-section");
  const linkedInSearchUrl = document.getElementById("wizard-linkedin-search-url");
  const openAiModel = document.getElementById("wizard-openai-model");
  const anthropicModel = document.getElementById("wizard-anthropic-model");
  const scoreThreshold = document.getElementById("wizard-score-threshold");
  const applicant = config.applicant || {};

  if (jobRole) jobRole.value = config.app.job_role || "";
  if (resumeLink) resumeLink.value = config.project_resume.source_url || "";
  if (existingResume) {
    const resumeLabel = config.project_resume.original_file_name || config.project_resume.source_url || "";
    existingResume.hidden = !config.project_resume.extracted_text;
    existingResume.textContent = resumeLabel
      ? `Current base resume is already saved: ${resumeLabel}`
      : "Current base resume is already saved.";
  }
  if (linkedInSearchUrl) linkedInSearchUrl.value = (config.linkedin.search_urls || [])[0] || "";
  if (linkedInSearchSection) linkedInSearchSection.hidden = !config.linkedin.authenticated;
  if (openAiModel) openAiModel.value = config.models.openai_model || "";
  if (anthropicModel) anthropicModel.value = config.models.anthropic_model || "";
  applyMaskedSecretInput("wizard-openai-key", config.secrets.openai_api_key_configured);
  applyMaskedSecretInput("wizard-anthropic-key", config.secrets.anthropic_api_key_configured);
  if (scoreThreshold) scoreThreshold.value = config.app.score_threshold ?? 82;
  applyApplicantConfig(applicant);

  const linkedInButton = document.getElementById("wizard-linkedin-connect");
  if (linkedInButton) {
    linkedInButton.disabled = Boolean(config.linkedin.authenticated);
    linkedInButton.textContent = config.linkedin.authenticated ? "LinkedIn Connected" : "Connect LinkedIn";
  }

  const googleButton = document.getElementById("wizard-google-connect");
  if (googleButton) {
    googleButton.disabled = Boolean(config.google.connected);
    googleButton.textContent = config.google.connected ? "Google Connected" : "Connect Google";
  }

  if (onboardingState.currentStep === "google" && config.google.connected) {
    setGlobalStatus("Google connected successfully. Click Finish Setup to continue.", "success");
  }

  setLinkedInStatus(
    config.linkedin.authenticated
      ? "LinkedIn is connected."
      : "LinkedIn is not connected yet.",
    config.linkedin.authenticated ? "success" : "info",
  );

}

function applyApplicantConfig(applicant) {
  const fields = {
    "wizard-applicant-legal-name": "legal_name",
    "wizard-applicant-email": "email",
    "wizard-applicant-phone": "phone",
    "wizard-applicant-linkedin-url": "linkedin_url",
    "wizard-applicant-portfolio-url": "portfolio_url",
    "wizard-applicant-github-url": "github_url",
    "wizard-applicant-current-company": "current_company",
    "wizard-applicant-current-title": "current_title",
    "wizard-applicant-years-of-experience": "years_of_experience",
    "wizard-applicant-address-line-1": "address_line_1",
    "wizard-applicant-address-line-2": "address_line_2",
    "wizard-applicant-city": "city",
    "wizard-applicant-state": "state",
    "wizard-applicant-postal-code": "postal_code",
    "wizard-applicant-country": "country",
    "wizard-applicant-compensation-expectation": "compensation_expectation",
    "wizard-applicant-programming-languages-years": "programming_languages_years",
    "wizard-applicant-favorite-ai-tool": "favorite_ai_tool",
  };
  Object.entries(fields).forEach(([id, key]) => {
    const input = document.getElementById(id);
    if (input) input.value = applicant[key] || "";
  });
  const workAuthorized = document.getElementById("wizard-applicant-work-authorized-us");
  const requiresSponsorship = document.getElementById("wizard-applicant-requires-sponsorship");
  const smsConsent = document.getElementById("wizard-applicant-sms-consent");
  if (workAuthorized) workAuthorized.checked = applicant.work_authorized_us ?? true;
  if (requiresSponsorship) requiresSponsorship.checked = Boolean(applicant.requires_sponsorship);
  if (smsConsent) smsConsent.checked = Boolean(applicant.sms_consent);
}

async function goBack() {
  setGlobalStatus("");
  const response = await callJson("/onboarding/api/back", "POST");
  showStep(response.current_step);
  await refreshState();
}

async function saveProjectStep() {
  const jobRole = document.getElementById("wizard-job-role").value.trim();
  const response = await callJson("/onboarding/api/project", "POST", {
    job_role: jobRole,
  });
  showStep(response.current_step);
  await refreshState();
}

async function saveResumeStep() {
  const fileInput = document.getElementById("wizard-resume-file");
  const linkValue = document.getElementById("wizard-resume-link").value.trim();
  const existingResume = onboardingState.config?.project_resume || {};

  if (fileInput.files && fileInput.files.length > 0) {
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const response = await callJson("/onboarding/api/resume/upload", "POST", formData);
    showStep(response.current_step);
    await refreshState();
    return;
  }

  if (
    existingResume.extracted_text
    && (!linkValue || linkValue === (existingResume.source_url || "").trim())
  ) {
    const response = await callJson("/onboarding/api/resume/continue", "POST");
    showStep(response.current_step);
    await refreshState();
    return;
  }

  if (!linkValue) {
    throw new Error("Upload a .docx file or provide a resume link.");
  }

  const response = await callJson("/onboarding/api/resume/link", "POST", {
    source_url: linkValue,
  });
  showStep(response.current_step);
  await refreshState();
}

async function fetchLinkedInStatus() {
  const config = onboardingState.config;
  const response = await callJson("/setup/api/linkedin/auth/check", "POST", {
    google: config.google,
    linkedin: config.linkedin,
    models: config.models,
    onboarding: config.onboarding,
    project_resume: config.project_resume,
    app: config.app,
    secrets: { openai_api_key: null, anthropic_api_key: null, anymailfinder_api_key: null },
  });
  return response;
}

async function closeLinkedInBrowserIfNeeded() {
  if (!linkedInBrowserPid) return;
  try {
    await callJson("/setup/api/linkedin/browser/terminate", "POST", { pid: linkedInBrowserPid });
  } finally {
    linkedInBrowserPid = null;
  }
}

function stopLinkedInPolling() {
  if (linkedInPollHandle) {
    clearInterval(linkedInPollHandle);
    linkedInPollHandle = null;
  }
}

function startLinkedInPolling() {
  stopLinkedInPolling();
  linkedInPollHandle = setInterval(async () => {
    try {
      const response = await fetchLinkedInStatus();
      if (response.authenticated) {
        stopLinkedInPolling();
        await closeLinkedInBrowserIfNeeded();
        await refreshState();
      }
    } catch (error) {
      setLinkedInStatus(error.message, "error");
    }
  }, 3000);
}

async function connectLinkedIn() {
  const config = onboardingState.config;
  const auth = await fetchLinkedInStatus();
  if (auth.authenticated) {
    await refreshState();
    return;
  }
  const response = await callJson("/setup/api/linkedin/browser/launch", "POST", {
    google: config.google,
    linkedin: config.linkedin,
    models: config.models,
    onboarding: config.onboarding,
    project_resume: config.project_resume,
    app: config.app,
    secrets: { openai_api_key: null, anthropic_api_key: null, anymailfinder_api_key: null },
  });
  linkedInBrowserPid = response.pid ?? null;
  setLinkedInStatus(`${response.message} Sign in to LinkedIn in the opened browser.`, "info");
  startLinkedInPolling();
}

async function saveJobSitesStep() {
  const response = await callJson("/onboarding/api/job-sites", "POST", {
    selected_job_sites: ["linkedin"],
    search_url: document.getElementById("wizard-linkedin-search-url").value.trim(),
  });
  showStep(response.current_step);
  await refreshState();
}

async function saveModelsStep() {
  const response = await callJson("/onboarding/api/models", "POST", {
    openai_model: document.getElementById("wizard-openai-model").value,
    anthropic_model: document.getElementById("wizard-anthropic-model").value,
    openai_api_key: getSecretInputValue("wizard-openai-key"),
    anthropic_api_key: getSecretInputValue("wizard-anthropic-key"),
  });
  document.getElementById("wizard-openai-key").value = "";
  document.getElementById("wizard-anthropic-key").value = "";
  showStep(response.current_step);
  await refreshState();
}

async function saveAnymailfinderStep() {
  const response = await callJson("/onboarding/api/anymailfinder", "POST", {
    api_key: document.getElementById("wizard-anymailfinder-key").value.trim() || null,
  });
  document.getElementById("wizard-anymailfinder-key").value = "";
  showStep(response.current_step);
  await refreshState();
}

async function saveScoreThresholdStep() {
  const response = await callJson("/onboarding/api/score-threshold", "POST", {
    score_threshold: Number(document.getElementById("wizard-score-threshold").value || 82),
  });
  showStep(response.current_step);
  await refreshState();
}

function collectApplicantPayload() {
  return {
    legal_name: document.getElementById("wizard-applicant-legal-name").value.trim(),
    preferred_name: "",
    email: document.getElementById("wizard-applicant-email").value.trim(),
    phone: document.getElementById("wizard-applicant-phone").value.trim(),
    linkedin_url: document.getElementById("wizard-applicant-linkedin-url").value.trim(),
    portfolio_url: document.getElementById("wizard-applicant-portfolio-url").value.trim(),
    github_url: document.getElementById("wizard-applicant-github-url").value.trim(),
    current_company: document.getElementById("wizard-applicant-current-company").value.trim(),
    current_title: document.getElementById("wizard-applicant-current-title").value.trim(),
    years_of_experience: document.getElementById("wizard-applicant-years-of-experience").value.trim(),
    address_line_1: document.getElementById("wizard-applicant-address-line-1").value.trim(),
    address_line_2: document.getElementById("wizard-applicant-address-line-2").value.trim(),
    city: document.getElementById("wizard-applicant-city").value.trim(),
    state: document.getElementById("wizard-applicant-state").value.trim(),
    postal_code: document.getElementById("wizard-applicant-postal-code").value.trim(),
    country: document.getElementById("wizard-applicant-country").value.trim() || "United States",
    work_authorized_us: document.getElementById("wizard-applicant-work-authorized-us").checked,
    requires_sponsorship: document.getElementById("wizard-applicant-requires-sponsorship").checked,
    compensation_expectation: document.getElementById("wizard-applicant-compensation-expectation").value.trim(),
    programming_languages_years: document.getElementById("wizard-applicant-programming-languages-years").value.trim(),
    favorite_ai_tool: document.getElementById("wizard-applicant-favorite-ai-tool").value.trim(),
    favorite_ai_tool_usage: "",
    company_value_example: "",
    why_interested_guidance: "",
    additional_info_guidance: "",
    sms_consent: document.getElementById("wizard-applicant-sms-consent").checked,
    custom_answer_guidance: "",
  };
}

async function saveApplicantStep() {
  const response = await callJson("/onboarding/api/applicant", "POST", collectApplicantPayload());
  showStep(response.current_step);
  await refreshState();
}

async function finishGoogleStep() {
  const response = await callJson("/onboarding/api/google/complete", "POST");
  if (response.redirect_to) {
    window.location.href = response.redirect_to;
    return true;
  }
  return false;
}

function connectGoogle() {
  startGoogleOAuth();
}

function connectGoogleFromResumeStep() {
  sessionStorage.setItem("onboardingReturnToResume", "1");
  startGoogleOAuth();
}

async function startGoogleOAuth() {
  const status = await callJson("/setup/api/google/auth/status", "GET");
  if (!status.client_configured) {
    setGlobalStatus("Google OAuth client configuration is missing. Rebuild the app with a Google OAuth client configuration.", "error");
    return;
  }
  window.location.href = "/setup/api/google/auth/start";
}

async function handleContinue() {
  setGlobalStatus("");
  const step = onboardingState.currentStep;
  if (step === "project") return saveProjectStep();
  if (step === "resume") return saveResumeStep();
  if (step === "job-sites") return saveJobSitesStep();
  if (step === "models") return saveModelsStep();
  if (step === "anymailfinder") return saveAnymailfinderStep();
  if (step === "score-threshold") return saveScoreThresholdStep();
  if (step === "applicant") return saveApplicantStep();
  if (step === "google") return finishGoogleStep();
}

window.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("wizard-back").addEventListener("click", async () => {
    try {
      await goBack();
    } catch (error) {
      setGlobalStatus(error.message, "error");
    }
  });
  document.getElementById("wizard-continue").addEventListener("click", async () => {
    if (wizardSubmitting) return;
    setWizardSubmitting(true);
    try {
      const keepSubmitting = await handleContinue();
      if (!keepSubmitting) setWizardSubmitting(false);
    } catch (error) {
      setWizardSubmitting(false);
      if (error.code === "google_doc_access_required") {
        setGoogleDocAccessStatus();
      } else if (
        onboardingState.currentStep === "models"
        && (error.message || "").toLowerCase().includes("api key")
      ) {
        setGlobalStatus("AI Keys Required or Subscribe", "error");
      } else {
        setGlobalStatus(error.message, "error");
      }
    }
  });
  document.getElementById("wizard-linkedin-connect").addEventListener("click", async () => {
    try {
      await connectLinkedIn();
    } catch (error) {
      setLinkedInStatus(error.message, "error");
    }
  });
  document.getElementById("wizard-google-connect").addEventListener("click", connectGoogle);
  document.getElementById("wizard-google-doc-connect").addEventListener("click", connectGoogleFromResumeStep);
  document.getElementById("wizard-google-doc-info").addEventListener("click", showGoogleDocModal);
  document.getElementById("wizard-google-doc-modal-close").addEventListener("click", hideGoogleDocModal);
  document.getElementById("wizard-google-doc-modal").addEventListener("click", (event) => {
    if (event.target.id === "wizard-google-doc-modal") hideGoogleDocModal();
  });
  setupMaskedSecretInput("wizard-openai-key", () => Boolean(onboardingState.config?.secrets?.openai_api_key_configured));
  setupMaskedSecretInput("wizard-anthropic-key", () => Boolean(onboardingState.config?.secrets?.anthropic_api_key_configured));

  await refreshState();

  const params = new URLSearchParams(window.location.search);
  if (params.get("resume_google_connected") === "1" || sessionStorage.getItem("onboardingReturnToResume") === "1") {
    sessionStorage.removeItem("onboardingReturnToResume");
    showStep("resume");
    setGlobalStatus("Google Connected, please click Continue to extract your resume", "success");
  }
});
