/* auto-find-jobs.js — Find Jobs page toggle that drives the job_intake schedule. */

(function () {
  const AGENT_NAME = "job_intake";
  const ENDPOINT = `/schedule/agent/${AGENT_NAME}`;

  const toggle = document.getElementById("auto-find-jobs-toggle");
  const modal = document.getElementById("auto-find-jobs-modal");
  if (!toggle || !modal) return;

  const stage = modal.querySelector(".auto-find-jobs-stage");
  const confirmYes = document.getElementById("auto-find-jobs-confirm-yes");
  const confirmNo = document.getElementById("auto-find-jobs-confirm-no");
  const pickerCancel = document.getElementById("auto-find-jobs-picker-cancel");
  const pickerSave = document.getElementById("auto-find-jobs-picker-save");
  const disableCancel = document.getElementById("auto-find-jobs-disable-cancel");
  const disableConfirm = document.getElementById("auto-find-jobs-disable-confirm");
  const disableMessage = document.getElementById("auto-find-jobs-disable-message");
  const errorBox = document.getElementById("auto-find-jobs-error");
  const freqInputs = modal.querySelectorAll('input[name="auto-find-jobs-frequency"]');
  const weeklyBlock = modal.querySelector(".auto-find-jobs-weekly");
  const dayInputs = modal.querySelectorAll(".day-chip input");
  const weekIntervalSelect = document.getElementById("auto-find-jobs-week-interval");
  const hourInput = document.getElementById("auto-find-jobs-hour");
  const minuteInput = document.getElementById("auto-find-jobs-minute");
  const ampmButtons = modal.querySelectorAll(".ampm-option");

  let lastSavedConfig = null;

  async function fetchJson(method, payload) {
    const response = await fetch(ENDPOINT, {
      method,
      headers: { "Content-Type": "application/json" },
      body: payload ? JSON.stringify(payload) : undefined,
    });
    const text = await response.text();
    const data = text ? JSON.parse(text) : null;
    if (!response.ok) {
      const detail = data && typeof data === "object" ? data.detail : text;
      throw new Error(detail || `${response.status} ${response.statusText}`);
    }
    return data;
  }

  function syncStageHeight() {
    if (modal.hidden) return;
    const activePane = stage.querySelector(`[data-pane="${stage.dataset.step}"]`);
    if (!activePane) return;
    const h = activePane.offsetHeight;
    if (h > 0) stage.style.height = `${h}px`;
  }

  function scheduleSync() {
    requestAnimationFrame(() => requestAnimationFrame(syncStageHeight));
  }

  function setStage(step) {
    stage.dataset.step = step;
    scheduleSync();
  }

  function showModal(initialStep) {
    modal.hidden = false;
    stage.dataset.step = initialStep || "confirm";
    scheduleSync();
  }

  function hideModal() {
    modal.hidden = true;
    stage.dataset.step = "confirm";
    stage.style.height = "";
    errorBox.hidden = true;
    errorBox.textContent = "";
  }

  function applyFrequencyView() {
    const freq = Array.from(freqInputs).find((input) => input.checked)?.value || "daily";
    weeklyBlock.hidden = freq !== "weekly";
    scheduleSync();
  }

  if (typeof ResizeObserver !== "undefined") {
    const observer = new ResizeObserver(() => syncStageHeight());
    stage.querySelectorAll(".auto-find-jobs-pane").forEach((pane) => observer.observe(pane));
  }

  // Convert "HH:MM" 24-hour string ↔ {hour12, minute, ampm}.
  function from24Hour(value) {
    const match = /^(\d{1,2}):(\d{2})$/.exec(value || "");
    if (!match) return { hour12: 9, minute: 0, ampm: "AM" };
    let hour = Number(match[1]);
    const minute = Number(match[2]);
    const ampm = hour >= 12 ? "PM" : "AM";
    if (hour === 0) hour = 12;
    else if (hour > 12) hour -= 12;
    return { hour12: hour, minute, ampm };
  }

  function to24Hour(hour12, minute, ampm) {
    let h = Number(hour12) || 12;
    if (h < 1) h = 1;
    if (h > 12) h = 12;
    let m = Number(minute);
    if (!Number.isFinite(m) || m < 0) m = 0;
    if (m > 59) m = 59;
    let h24 = h % 12;
    if (ampm === "PM") h24 += 12;
    return `${String(h24).padStart(2, "0")}:${String(m).padStart(2, "0")}`;
  }

  const DAY_LABELS = { mon: "Mon", tue: "Tue", wed: "Wed", thu: "Thu", fri: "Fri", sat: "Sat", sun: "Sun" };

  function formatScheduleSummary(config) {
    if (!config) return "";
    const { hour12, minute, ampm } = from24Hour(config.run_at_local_time || "09:00");
    const time = `${hour12}:${String(minute).padStart(2, "0")} ${ampm}`;
    if (config.frequency !== "weekly") {
      return `every day at ${time}`;
    }
    const days = (config.days_of_week || []).map((d) => DAY_LABELS[d] || d);
    let dayText;
    if (days.length === 0) dayText = "no days selected";
    else if (days.length === 1) dayText = days[0];
    else if (days.length === 2) dayText = `${days[0]} and ${days[1]}`;
    else dayText = `${days.slice(0, -1).join(", ")}, and ${days[days.length - 1]}`;
    const interval = Number(config.week_interval) || 1;
    let cadence;
    if (interval === 1) cadence = "every week";
    else if (interval === 2) cadence = "every other week";
    else cadence = `every ${interval} weeks`;
    return `on ${dayText} ${cadence} at ${time}`;
  }

  function refreshDisableMessage() {
    const summary = formatScheduleSummary(lastSavedConfig);
    if (summary) {
      // Summary comes from a controlled vocabulary (literal frequency,
      // canned day labels, formatted hour/minute). Safe to use innerHTML so
      // the schedule fragment can pick up its own color.
      disableMessage.innerHTML =
        `AI Auto Find Jobs is currently scheduled to run ` +
        `<span class="schedule-summary-text">${summary}</span>` +
        `. Turning this off will stop the App from automatically finding new jobs in the background.`;
    } else {
      disableMessage.textContent = "Turning this off will stop the App from automatically finding new jobs in the background.";
    }
  }

  function setAmpm(value) {
    ampmButtons.forEach((btn) => {
      const isActive = btn.dataset.ampm === value;
      btn.classList.toggle("is-active", isActive);
      btn.setAttribute("aria-pressed", isActive ? "true" : "false");
    });
  }

  function getAmpm() {
    const active = Array.from(ampmButtons).find((btn) => btn.classList.contains("is-active"));
    return active ? active.dataset.ampm : "AM";
  }

  ampmButtons.forEach((btn) => {
    btn.addEventListener("click", () => setAmpm(btn.dataset.ampm));
  });

  // Two-digit padded display for the minute when it loses focus.
  minuteInput.addEventListener("blur", () => {
    const m = Number(minuteInput.value);
    if (Number.isFinite(m)) {
      minuteInput.value = String(Math.max(0, Math.min(59, m))).padStart(2, "0");
    }
  });
  hourInput.addEventListener("blur", () => {
    const h = Number(hourInput.value);
    if (Number.isFinite(h)) {
      hourInput.value = String(Math.max(1, Math.min(12, h)));
    }
  });

  function populatePicker(config) {
    const freq = config.frequency === "weekly" ? "weekly" : "daily";
    freqInputs.forEach((input) => { input.checked = input.value === freq; });
    const days = new Set(config.days_of_week || []);
    dayInputs.forEach((input) => { input.checked = days.has(input.dataset.day); });
    weekIntervalSelect.value = String(config.week_interval || 1);
    const { hour12, minute, ampm } = from24Hour(config.run_at_local_time || "09:00");
    hourInput.value = String(hour12);
    minuteInput.value = String(minute).padStart(2, "0");
    setAmpm(ampm);
    applyFrequencyView();
  }

  function readPicker() {
    const freq = Array.from(freqInputs).find((input) => input.checked)?.value || "daily";
    const days = Array.from(dayInputs).filter((input) => input.checked).map((input) => input.dataset.day);
    const weekInterval = Number(weekIntervalSelect.value) || 1;
    const time = to24Hour(hourInput.value, minuteInput.value, getAmpm());
    return {
      agent_name: AGENT_NAME,
      enabled: true,
      frequency: freq,
      days_of_week: freq === "weekly" ? days : (lastSavedConfig?.days_of_week || ["mon", "tue", "wed", "thu", "fri"]),
      week_interval: freq === "weekly" ? weekInterval : 1,
      run_at_local_time: time,
    };
  }

  function validatePayload(payload) {
    if (payload.frequency === "weekly" && payload.days_of_week.length === 0) {
      return "Pick at least one day of the week.";
    }
    if (!/^\d{2}:\d{2}$/.test(payload.run_at_local_time)) {
      return "Pick a valid time of day.";
    }
    return null;
  }

  async function loadInitialState() {
    try {
      const config = await fetchJson("GET");
      lastSavedConfig = config;
      toggle.checked = !!config.enabled;
      populatePicker(config);
    } catch (err) {
      console.error("Failed to load auto-find-jobs schedule:", err);
    }
  }

  toggle.addEventListener("change", () => {
    if (toggle.checked) {
      populatePicker(lastSavedConfig || {});
      showModal("confirm");
      // Stays on only if the user makes it through Save.
      toggle.checked = false;
    } else {
      // Confirm before turning off; show the user the schedule they're disabling.
      refreshDisableMessage();
      showModal("disable-confirm");
      // Snap back to on; we'll flip it after the user confirms in the modal.
      toggle.checked = true;
    }
  });

  confirmNo.addEventListener("click", () => {
    hideModal();
  });

  confirmYes.addEventListener("click", () => {
    setStage("picker");
  });

  pickerCancel.addEventListener("click", () => {
    hideModal();
  });

  disableCancel.addEventListener("click", () => {
    hideModal();
  });

  disableConfirm.addEventListener("click", async () => {
    try {
      const payload = {
        ...(lastSavedConfig || {
          agent_name: AGENT_NAME,
          frequency: "daily",
          days_of_week: ["mon", "tue", "wed", "thu", "fri"],
          week_interval: 1,
          run_at_local_time: "09:00",
        }),
        agent_name: AGENT_NAME,
        enabled: false,
      };
      const updated = await fetchJson("PUT", payload);
      const next = (updated.schedules || []).find((item) => item.agent_name === AGENT_NAME);
      if (next) {
        lastSavedConfig = next;
        toggle.checked = !!next.enabled;
      } else {
        toggle.checked = false;
      }
      hideModal();
    } catch (err) {
      console.error("Failed to disable auto-find-jobs:", err);
    }
  });

  freqInputs.forEach((input) => input.addEventListener("change", applyFrequencyView));

  pickerSave.addEventListener("click", async () => {
    errorBox.hidden = true;
    errorBox.textContent = "";
    const payload = readPicker();
    const validationError = validatePayload(payload);
    if (validationError) {
      errorBox.textContent = validationError;
      errorBox.hidden = false;
      return;
    }
    try {
      const updated = await fetchJson("PUT", payload);
      const next = (updated.schedules || []).find((item) => item.agent_name === AGENT_NAME);
      if (next) {
        lastSavedConfig = next;
        toggle.checked = !!next.enabled;
      } else {
        toggle.checked = true;
      }
      hideModal();
    } catch (err) {
      errorBox.textContent = err.message || "Failed to save schedule.";
      errorBox.hidden = false;
    }
  });

  modal.addEventListener("click", (event) => {
    if (event.target === modal) {
      hideModal();
    }
  });

  loadInitialState();
})();
