const DAY_OPTIONS = [
  { value: "mon", label: "Mon" },
  { value: "tue", label: "Tue" },
  { value: "wed", label: "Wed" },
  { value: "thu", label: "Thu" },
  { value: "fri", label: "Fri" },
  { value: "sat", label: "Sat" },
  { value: "sun", label: "Sun" },
];

let schedulerState = null;

async function callJson(url, method, payload) {
  const response = await fetch(url, {
    method,
    headers: payload ? { "Content-Type": "application/json" } : undefined,
    body: payload ? JSON.stringify(payload) : undefined,
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json") ? await response.json() : await response.text();
  if (!response.ok) {
    const detail = typeof data === "object" && data !== null ? data.detail : data;
    throw new Error(
      typeof detail === "object" && detail !== null
        ? detail.message || `${response.status} ${response.statusText}`
        : detail || `${response.status} ${response.statusText}`,
    );
  }
  return data;
}

function setStatus(message, level = "info") {
  const box = document.getElementById("schedule-output");
  box.hidden = !message;
  box.textContent = message || "";
  box.dataset.level = level;
}

function renderSummary(state) {
  const launchAgent = state.launch_agent || {};
  document.getElementById("schedule-launch-agent-status").textContent = launchAgent.status_message || "Scheduler background item status unavailable.";
  document.getElementById("schedule-launch-agent-path").textContent = launchAgent.plist_path || "";

  const helper = state.helper || {};
  document.getElementById("schedule-helper-status").textContent = helper.status_message || "Secret helper status unavailable.";
  const helperBits = [];
  if (helper.backend) helperBits.push(`Backend: ${helper.backend}`);
  if (helper.helper_version) helperBits.push(`Helper ${helper.helper_version}`);
  if (helper.last_error_code) helperBits.push(`Last error: ${helper.last_error_code}`);
  document.getElementById("schedule-helper-detail").textContent = helperBits.join(" • ");
}

function scheduleCardMarkup(schedule) {
  const checkedDays = new Set(schedule.days_of_week || []);
  const dayCheckboxes = DAY_OPTIONS.map((day) => `
    <label class="schedule-day">
      <input type="checkbox" data-day="${day.value}" ${checkedDays.has(day.value) ? "checked" : ""} />
      <span>${day.label}</span>
    </label>
  `).join("");
  const statusLine = schedule.last_run_status
    ? `${schedule.last_run_status}${schedule.last_run_message ? ` • ${schedule.last_run_message}` : ""}`
    : "No scheduled run recorded yet.";

  return `
    <article class="schedule-card" data-agent-card="${schedule.agent_name}">
      <h3>${schedule.display_name}</h3>
      <div class="schedule-meta">
        Next run: ${schedule.next_run_local || "Not scheduled"}
      </div>
      <label class="schedule-day">
        <input type="checkbox" data-field="enabled" ${schedule.enabled ? "checked" : ""} />
        <span>Enabled</span>
      </label>
      <label>
        Run At
        <input type="time" data-field="run_at_local_time" value="${schedule.run_at_local_time}" />
      </label>
      <div class="schedule-days">${dayCheckboxes}</div>
      <div class="schedule-meta">${statusLine}</div>
      <div class="schedule-actions">
        <button type="button" class="btn-sm" data-run-now="${schedule.agent_name}">Run Now</button>
      </div>
    </article>
  `;
}

function renderSchedules(state) {
  schedulerState = state;
  renderSummary(state);
  const grid = document.getElementById("schedule-grid");
  grid.innerHTML = (state.schedules || []).map(scheduleCardMarkup).join("");

  grid.querySelectorAll("[data-run-now]").forEach((button) => {
    button.addEventListener("click", async () => {
      button.disabled = true;
      try {
        const response = await callJson(`/schedule/api/run/${button.dataset.runNow}`, "POST");
        applyState(response.state);
        const agents = response.result.triggered_agents || [];
        setStatus(agents.length ? `Started ${agents.join(", ")}.` : "No agent run was started.", "success");
      } catch (error) {
        setStatus(error.message, "error");
      } finally {
        button.disabled = false;
      }
    });
  });
}

function applyState(state) {
  schedulerState = state;
  renderSchedules(state);
}

function collectSchedulesPayload() {
  const schedules = Array.from(document.querySelectorAll("[data-agent-card]")).map((card) => ({
    agent_name: card.dataset.agentCard,
    enabled: Boolean(card.querySelector('[data-field="enabled"]').checked),
    run_at_local_time: card.querySelector('[data-field="run_at_local_time"]').value || "09:00",
    days_of_week: Array.from(card.querySelectorAll("[data-day]:checked")).map((node) => node.dataset.day),
    last_triggered_slot: schedulerState?.schedules?.find((item) => item.agent_name === card.dataset.agentCard)?.last_triggered_slot || null,
    last_run_started_at: schedulerState?.schedules?.find((item) => item.agent_name === card.dataset.agentCard)?.last_run_started_at || null,
    last_run_finished_at: schedulerState?.schedules?.find((item) => item.agent_name === card.dataset.agentCard)?.last_run_finished_at || null,
    last_run_status: schedulerState?.schedules?.find((item) => item.agent_name === card.dataset.agentCard)?.last_run_status || null,
    last_run_message: schedulerState?.schedules?.find((item) => item.agent_name === card.dataset.agentCard)?.last_run_message || null,
  }));
  return { schedules };
}

async function loadState() {
  const state = await callJson("/schedule/api/config", "GET");
  applyState(state);
}

async function saveSchedules() {
  const state = await callJson("/schedule/api/config", "PUT", collectSchedulesPayload());
  applyState(state);
  setStatus("Schedule saved.", "success");
}

async function installScheduler() {
  const state = await callJson("/schedule/api/install", "POST");
  applyState(state);
  setStatus("Scheduler background item installed.", "success");
}

async function uninstallScheduler() {
  const state = await callJson("/schedule/api/uninstall", "POST");
  applyState(state);
  setStatus("Scheduler background item removed.", "success");
}

async function runDueChecks() {
  const response = await callJson("/schedule/api/tick", "POST");
  applyState(response.state);
  const triggered = response.result.triggered_agents || [];
  setStatus(
    triggered.length
      ? `Triggered: ${triggered.join(", ")}.`
      : "No schedules were due at the current minute.",
    triggered.length ? "success" : "info",
  );
}

window.addEventListener("DOMContentLoaded", async () => {
  document.getElementById("schedule-save").addEventListener("click", async () => {
    try {
      await saveSchedules();
    } catch (error) {
      setStatus(error.message, "error");
    }
  });
  document.getElementById("schedule-install").addEventListener("click", async () => {
    try {
      await installScheduler();
    } catch (error) {
      setStatus(error.message, "error");
    }
  });
  document.getElementById("schedule-uninstall").addEventListener("click", async () => {
    try {
      await uninstallScheduler();
    } catch (error) {
      setStatus(error.message, "error");
    }
  });
  document.getElementById("schedule-run-due").addEventListener("click", async () => {
    try {
      await runDueChecks();
    } catch (error) {
      setStatus(error.message, "error");
    }
  });

  try {
    await loadState();
  } catch (error) {
    setStatus(error.message, "error");
  }
});
