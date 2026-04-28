async function staleRunCallJson(url, method, payload) {
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

function staleRunAgentLabel(agentName) {
  if (agentName === "job_intake") return "Jobs Agent";
  if (agentName === "job_scoring") return "Scoring Agent";
  if (agentName === "resume_generation") return "Resume Agent";
  if (agentName === "job_apply") return "Apply Agent";
  return "Agent";
}

function staleRunFormatDateTime(value) {
  if (!value) return "an unknown time";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString();
}

function staleRunBuildRedirect(target, runId) {
  const url = new URL(target || "/", window.location.origin);
  if (runId) {
    url.searchParams.set("run", runId);
  }
  return `${url.pathname}${url.search}${url.hash}`;
}

let staleRunsQueue = [];
let staleRunActive = null;

function staleRunSetStatus(message, level = "info") {
  const box = document.getElementById("stale-run-modal-status");
  if (!box) return;
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

function staleRunSetButtonsDisabled(disabled) {
  const continueButton = document.getElementById("stale-run-continue-button");
  const cancelButton = document.getElementById("stale-run-cancel-button");
  if (continueButton) continueButton.disabled = disabled || !staleRunActive?.resumable;
  if (cancelButton) cancelButton.disabled = disabled;
}

function staleRunHideModal() {
  const modal = document.getElementById("stale-run-modal");
  if (modal) modal.hidden = true;
  staleRunActive = null;
  staleRunSetStatus("");
}

function staleRunRenderCurrent() {
  const modal = document.getElementById("stale-run-modal");
  const message = document.getElementById("stale-run-modal-message");
  const continueButton = document.getElementById("stale-run-continue-button");
  if (!modal || !message || !continueButton) return;

  if (!staleRunsQueue.length) {
    staleRunHideModal();
    return;
  }

  staleRunActive = staleRunsQueue[0];
  const agentLabel = staleRunAgentLabel(staleRunActive.agent_name);
  const started = staleRunFormatDateTime(staleRunActive.started_at);
  const resumableText = staleRunActive.resumable
    ? "Continue it or cancel it?"
    : "It cannot be continued. Cancel it?";
  message.textContent = `A previous ${agentLabel} run from ${started} did not finish. ${resumableText}`;
  continueButton.textContent = staleRunActive.resumable ? "Yes, Continue" : "Cannot Continue";
  modal.hidden = false;
  staleRunSetStatus("");
  staleRunSetButtonsDisabled(false);
}

async function staleRunLoadQueue() {
  const payload = await staleRunCallJson("/runs/stale", "GET");
  staleRunsQueue = Array.isArray(payload.runs) ? payload.runs : [];
  staleRunRenderCurrent();
}

async function staleRunContinueCurrent() {
  if (!staleRunActive || !staleRunActive.resumable) return;
  staleRunSetButtonsDisabled(true);
  staleRunSetStatus("Resuming stale run...", "info");
  try {
    const payload = await staleRunCallJson(`/runs/${staleRunActive.id}/resume`, "POST");
    window.location.assign(staleRunBuildRedirect(payload.redirect_to, payload.run?.id || staleRunActive.id));
  } catch (error) {
    staleRunSetStatus(error.message, "error");
    staleRunSetButtonsDisabled(false);
  }
}

async function staleRunCancelCurrent() {
  if (!staleRunActive) return;
  const cancelledRunId = staleRunActive.id;
  staleRunSetButtonsDisabled(true);
  staleRunSetStatus("Cancelling stale run...", "info");
  try {
    await staleRunCallJson(`/runs/${cancelledRunId}/cancel`, "POST");
    staleRunsQueue = staleRunsQueue.slice(1);
    staleRunRenderCurrent();
    if (!staleRunsQueue.length) {
      const url = new URL(window.location.href);
      if (url.searchParams.get("run") === cancelledRunId) {
        url.searchParams.delete("run");
        window.history.replaceState({}, "", `${url.pathname}${url.search}${url.hash}`);
      }
    }
  } catch (error) {
    staleRunSetStatus(error.message, "error");
    staleRunSetButtonsDisabled(false);
  }
}

window.addEventListener("DOMContentLoaded", async () => {
  const continueButton = document.getElementById("stale-run-continue-button");
  const cancelButton = document.getElementById("stale-run-cancel-button");
  if (!continueButton || !cancelButton) return;

  continueButton.addEventListener("click", () => {
    void staleRunContinueCurrent();
  });
  cancelButton.addEventListener("click", () => {
    void staleRunCancelCurrent();
  });

  try {
    await staleRunLoadQueue();
  } catch (error) {
    staleRunSetStatus(error.message, "error");
  }
});
