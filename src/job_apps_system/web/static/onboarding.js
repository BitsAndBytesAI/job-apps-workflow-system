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
    throw new Error(detail || `${response.status} ${response.statusText}`);
  }
  return data;
}

let onboardingState = window.onboardingConfig || { currentStep: "project", config: null };
let linkedInPollHandle = null;
let linkedInBrowserPid = null;
const STEP_ORDER = ["project", "resume", "job-sites", "models", "anymailfinder", "score-threshold", "google"];

function setGlobalStatus(message, level = "info") {
  const box = document.getElementById("wizard-global-status");
  box.hidden = !message;
  box.textContent = message || "";
  box.dataset.level = level;
}

function setLinkedInStatus(message, level = "info") {
  const box = document.getElementById("wizard-linkedin-status");
  if (!box) return;
  box.textContent = message;
  box.dataset.level = level;
}

function setGoogleStatus(message, level = "info") {
  const box = document.getElementById("wizard-google-status");
  if (!box) return;
  box.textContent = message;
  box.dataset.level = level;
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
  const backButton = document.getElementById("wizard-back");
  const continueButton = document.getElementById("wizard-continue");
  if (backButton) backButton.disabled = STEP_ORDER.indexOf(stepId) <= 0;
  if (continueButton) continueButton.textContent = stepId === "google" ? "Finish Setup" : "Continue";
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
  const resumeText = document.getElementById("wizard-resume-text");
  const siteLinkedIn = document.getElementById("wizard-site-linkedin");
  const openAiModel = document.getElementById("wizard-openai-model");
  const anthropicModel = document.getElementById("wizard-anthropic-model");
  const scoreThreshold = document.getElementById("wizard-score-threshold");

  if (jobRole) jobRole.value = config.app.job_role || "";
  if (resumeLink) resumeLink.value = config.project_resume.source_url || "";
  if (resumeText) resumeText.value = config.project_resume.extracted_text || "";
  if (siteLinkedIn) siteLinkedIn.checked = (config.app.selected_job_sites || []).includes("linkedin");
  if (openAiModel) openAiModel.value = config.models.openai_model || "";
  if (anthropicModel) anthropicModel.value = config.models.anthropic_model || "";
  if (scoreThreshold) scoreThreshold.value = config.app.score_threshold ?? 82;

  const linkedInButton = document.getElementById("wizard-linkedin-connect");
  if (linkedInButton) {
    linkedInButton.disabled = Boolean(config.linkedin.authenticated);
    linkedInButton.textContent = config.linkedin.authenticated ? "LinkedIn Connected" : "Connect LinkedIn";
  }

  setLinkedInStatus(
    config.linkedin.authenticated
      ? "LinkedIn is connected."
      : "LinkedIn is not connected yet.",
    config.linkedin.authenticated ? "success" : "info",
  );

  setGoogleStatus(
    config.google.connected
      ? "Google is connected."
      : "Connect Google to finish onboarding.",
    config.google.connected ? "success" : "info",
  );
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

  if (fileInput.files && fileInput.files.length > 0) {
    const formData = new FormData();
    formData.append("file", fileInput.files[0]);
    const response = await callJson("/onboarding/api/resume/upload", "POST", formData);
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
  const selected = [];
  if (document.getElementById("wizard-site-linkedin").checked) {
    selected.push("linkedin");
  }
  const response = await callJson("/onboarding/api/job-sites", "POST", { selected_job_sites: selected });
  showStep(response.current_step);
  await refreshState();
}

async function saveModelsStep() {
  const response = await callJson("/onboarding/api/models", "POST", {
    openai_model: document.getElementById("wizard-openai-model").value,
    anthropic_model: document.getElementById("wizard-anthropic-model").value,
    openai_api_key: document.getElementById("wizard-openai-key").value.trim(),
    anthropic_api_key: document.getElementById("wizard-anthropic-key").value.trim(),
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

async function finishGoogleStep() {
  const response = await callJson("/onboarding/api/google/complete", "POST");
  if (response.redirect_to) {
    window.location.href = response.redirect_to;
  }
}

function connectGoogle() {
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
    try {
      await handleContinue();
    } catch (error) {
      setGlobalStatus(error.message, "error");
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

  await refreshState();
});
