const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const elements = Object.fromEntries([
  "status-card", "phase", "countdown", "phone-number", "activation-id", "elapsed",
  "last-error", "purchase", "stop-search", "cancel", "retry", "events", "connection", "toast",
  "phone-copy-controls", "copy-full", "copy-national", "sms-card", "sms-message",
  "copy-sms", "theme-toggle", "summary-attempts", "summary-successes",
  "summary-unsuccessful", "summary-cost", "history-rows", "auto-retry-toggle",
  "auto-retry-label", "auto-retry-description", "search-state", "search-summary",
  "search-service", "search-country", "search-max-price", "search-provider-ids"
].map(id => [id, document.getElementById(id)]));

let copyValues = {full: "", national: "", sms: ""};
let eventsSignature = "";
let historySignature = "";
let lastPhase = null;
let lastAutoRetrySignal = null;
let audioContext = null;
let refreshSequence = 0;
let appliedRefreshSequence = 0;
let refreshController = null;

function getAudioContext() {
  if (!audioContext) {
    audioContext = new (window.AudioContext || window.webkitAudioContext)();
  }
  return audioContext;
}

async function playRetrySound() {
  const ctx = getAudioContext();
  await ctx.resume();
  if (ctx.state !== "running") throw new Error("Audio context is suspended");
  await new Promise((resolve) => {
    const oscillator = ctx.createOscillator();
    const gainNode = ctx.createGain();
    oscillator.connect(gainNode);
    gainNode.connect(ctx.destination);
    oscillator.frequency.setValueAtTime(520, ctx.currentTime);
    oscillator.frequency.exponentialRampToValueAtTime(360, ctx.currentTime + 0.5);
    oscillator.type = "sine";
    gainNode.gain.setValueAtTime(0.3, ctx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.5);
    oscillator.onended = resolve;
    oscillator.start(ctx.currentTime);
    oscillator.stop(ctx.currentTime + 0.5);
  });
}

function playSuccessSound() {
  try {
    const ctx = getAudioContext();
    const oscillator = ctx.createOscillator();
    const gainNode = ctx.createGain();
    oscillator.connect(gainNode);
    gainNode.connect(ctx.destination);
    oscillator.frequency.value = 880;
    oscillator.type = 'sine';
    gainNode.gain.setValueAtTime(0.3, ctx.currentTime);
    gainNode.gain.exponentialRampToValueAtTime(0.01, ctx.currentTime + 0.3);
    oscillator.start(ctx.currentTime);
    oscillator.stop(ctx.currentTime + 0.3);
  } catch (error) {
    console.error('Failed to play success sound:', error);
  }
}

const labels = {
  idle: "Idle", acquired: "Number acquired", ready_pending: "Confirming readiness",
  waiting_for_sms: "Waiting for SMS", resend_required: "Resend required",
  cancellation_pending: "Cancelling", code_notification_pending: "Delivering code",
  code_delivered: "Completing", completed: "Completed", cancelled: "Cancelled", failed: "Failed",
  searching: "Searching"
};

function duration(seconds) {
  const safe = Math.max(0, Number(seconds || 0));
  const minutes = Math.floor(safe / 60);
  const remainder = safe % 60;
  return minutes ? `${minutes}m ${remainder}s` : `${remainder}s`;
}

function visible(element, show) { element.classList.toggle("hidden", !show); }
function setText(element, value) {
  if (element.textContent !== value) element.textContent = value;
}

function render(status) {
  const phase = status.phase || "idle";

  // Sounds are driven by explicit server state so retry audio plays before purchase.
  if (lastPhase !== null) {
    if (phase === "completed" && lastPhase !== "completed") {
      playSuccessSound();
    }
  }
  lastPhase = phase;
  if (
    status.autoRetryStage === "announcing"
    && (lastAutoRetrySignal === null || status.autoRetrySignal > lastAutoRetrySignal)
  ) {
    playRetryAndAcknowledge(status.autoRetrySignal);
  }
  lastAutoRetrySignal = status.autoRetrySignal || 0;

  elements["status-card"].className = `card status-card state-${phase}`;
  setText(elements.phase, labels[phase] || phase.replaceAll("_", " "));
  setText(elements["phone-number"], status.phoneNumber || "No number acquired");
  setText(elements["activation-id"], status.activationId || "None");
  setText(elements.elapsed, duration(status.elapsedSeconds));
  setText(elements.countdown, status.activationId
    ? `${duration(status.timeoutRemainingSeconds)} remaining`
    : status.isPollingForNumber ? "Checking availability" : "No active timeout");
  setText(elements["last-error"], status.lastError || "");
  visible(elements["last-error"], Boolean(status.lastError));
  copyValues = {
    full: status.phoneNumberCopy || status.phoneNumber || "",
    national: status.phoneNumberNational || "",
    sms: status.smsMessage || ""
  };
  visible(elements["phone-copy-controls"], Boolean(copyValues.full));
  visible(elements["copy-national"], Boolean(copyValues.national));
  visible(elements["sms-card"], Boolean(copyValues.sms));
  setText(elements["sms-message"], copyValues.sms);
  visible(elements.purchase, status.canPurchase);
  visible(elements["stop-search"], status.canStopSearch);
  visible(elements.cancel, status.canCancel);
  visible(elements.retry, status.canRetry);
  const requestsSent = Number(status.acquisitionRequests || 0);
  const noNumberResponses = Number(status.noNumberResponses || 0);
  const searching = Boolean(status.isPollingForNumber);
  setText(elements["search-state"], searching ? "Searching Grizzly" : "Not searching");
  elements["search-state"].classList.toggle("searching", searching);
  setText(
    elements["search-summary"],
    requestsSent
      ? `${requestsSent} request${requestsSent === 1 ? "" : "s"} sent, ${noNumberResponses} unavailable`
      : "No requests sent"
  );
  setText(elements["search-service"], status.service || "-");
  setText(elements["search-country"], status.country || "-");
  setText(elements["search-max-price"], status.maxPrice || "-");
  setText(elements["search-provider-ids"], status.providerIds || "Any provider");

  // Sync auto-retry toggle state
  if (elements["auto-retry-toggle"].checked !== status.autoRetryEnabled) {
    elements["auto-retry-toggle"].checked = status.autoRetryEnabled;
  }
  setText(elements["auto-retry-label"], status.autoRetryEnabled ? "On" : "Off");
  const timeoutMinutes = Math.round((status.autoRetryTimeoutSeconds || 180) / 60);
  const delaySeconds = status.autoRetryDelaySeconds || 5;
  const soundLeadSeconds = status.autoRetrySoundLeadSeconds || 3;
  let automationDescription = `Cancel after ${timeoutMinutes} minutes without an SMS, wait ${delaySeconds} seconds, then try another number.`;
  if (status.autoRetryEnabled && status.canPurchase) {
    automationDescription = "Enabled for your next purchase. Click Get another number to start.";
  } else if (status.autoRetryStage === "delay") {
    automationDescription = `Waiting ${delaySeconds} seconds before the retry alert.`;
  } else if (status.autoRetryStage === "announcing") {
    automationDescription = `Retry alert sent. Next purchase starts in ${soundLeadSeconds} seconds.`;
  }
  setText(
    elements["auto-retry-description"],
    automationDescription
  );

  const nextEventsSignature = JSON.stringify(status.events);
  if (eventsSignature !== nextEventsSignature) {
    eventsSignature = nextEventsSignature;
    elements.events.innerHTML = status.events.length
      ? status.events.map(event => `<li><time>${new Date(event.time).toLocaleTimeString()}</time><span class="${event.level}">${escapeHtml(event.message)}</span></li>`).join("")
      : '<li class="empty">No activity yet.</li>';
  }
  renderHistory(status.history || [], status.historySummary || {});
}

function renderHistory(history, summary) {
  setText(elements["summary-attempts"], String(summary.attempts || 0));
  setText(elements["summary-successes"], String(summary.codesReceived || 0));
  setText(elements["summary-unsuccessful"], String(summary.unsuccessful || 0));
  const totals = Object.entries(summary.grossPurchaseValues || {});
  setText(
    elements["summary-cost"],
    totals.length ? totals.map(([currency, value]) => `${value} ${currency === "unknown" ? "" : currency}`.trim()).join(" / ") : "0"
  );
  const nextSignature = JSON.stringify(history);
  if (historySignature === nextSignature) return;
  historySignature = nextSignature;
  elements["history-rows"].innerHTML = history.length
    ? history.map(entry => `<tr>
        <td>${escapeHtml(new Date(entry.acquiredAt).toLocaleString())}</td>
        <td>${escapeHtml(entry.phoneNumber)}</td>
        <td>${escapeHtml(entry.cost)} ${escapeHtml(entry.currency || "")}</td>
        <td>${escapeHtml(entry.providerFilter || "Any")}</td>
        <td><span class="outcome ${escapeHtml(entry.phase)}">${escapeHtml(entry.phase.replaceAll("_", " "))}</span></td>
        <td>${entry.codeReceived ? "Received" : "No"}</td>
      </tr>`).join("")
    : '<tr><td colspan="6" class="empty-cell">No tracked purchases yet.</td></tr>';
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

async function refresh(force = false) {
  if (refreshController && !force) return;
  if (refreshController) refreshController.abort();
  const controller = new AbortController();
  refreshController = controller;
  const sequence = ++refreshSequence;
  try {
    const response = await fetch("/api/status", {
      headers: {"Accept": "application/json"},
      signal: controller.signal
    });
    if (response.status === 401) { window.location = "/login"; return; }
    if (!response.ok) throw new Error("Status request failed");
    const status = await response.json();
    if (sequence < appliedRefreshSequence) return;
    appliedRefreshSequence = sequence;
    render(status);
    elements.connection.textContent = "Connected";
    elements.connection.classList.add("online");
  } catch (error) {
    if (error.name === "AbortError") return;
    if (sequence < appliedRefreshSequence) return;
    appliedRefreshSequence = sequence;
    elements.connection.textContent = "Disconnected";
    elements.connection.classList.remove("online");
  } finally {
    if (refreshController === controller) refreshController = null;
  }
}

async function acknowledgeRetrySound(signal) {
  try {
    await fetch("/api/actions/ack-retry-sound", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify({signal})
    });
  } catch (error) {
    // The server has a timeout fallback when the browser cannot acknowledge.
  }
}

async function playRetryAndAcknowledge(signal) {
  try {
    const claim = await fetch("/api/actions/claim-retry-sound", {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify({signal})
    });
    if (!claim.ok) return;
    await playRetrySound();
    await acknowledgeRetrySound(signal);
  } catch (error) {
    console.error("Retry sound could not be played:", error);
  }
}

async function postAction(path, body = {}) {
  document.querySelectorAll("button").forEach(button => button.disabled = true);
  try {
    const response = await fetch(path, {
      method: "POST",
      headers: {"Content-Type": "application/json", "X-CSRF-Token": csrfToken},
      body: JSON.stringify(body)
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || "Action failed");
    showToast(payload.message || "Action accepted");
  } catch (error) {
    showToast(error.message);
  } finally {
    await refresh(true);
    document.querySelectorAll("button").forEach(button => button.disabled = false);
  }
}

function showToast(message) {
  elements.toast.textContent = message;
  visible(elements.toast, true);
  window.setTimeout(() => visible(elements.toast, false), 3500);
}

async function copyToClipboard(value, label) {
  if (!value) return;
  try {
    await navigator.clipboard.writeText(value);
  } catch (error) {
    const input = document.createElement("textarea");
    input.value = value;
    input.style.position = "fixed";
    input.style.opacity = "0";
    document.body.appendChild(input);
    input.select();
    document.execCommand("copy");
    input.remove();
  }
  showToast(`${label} copied`);
}

function applyTheme(theme) {
  document.documentElement.dataset.theme = theme;
  localStorage.setItem("grizzly-theme", theme);
  setText(elements["theme-toggle"], theme === "dark" ? "Light mode" : "Dark mode");
}

function confirmAction(title, message, confirmLabel, callback) {
  const dialog = document.getElementById("confirm-dialog");
  document.getElementById("dialog-title").textContent = title;
  document.getElementById("dialog-message").textContent = message;
  document.getElementById("dialog-confirm").textContent = confirmLabel;
  dialog.addEventListener("close", () => { if (dialog.returnValue === "confirm") callback(); }, {once: true});
  dialog.showModal();
}

elements.purchase.addEventListener("click", () => confirmAction(
  "Purchase a number?", "This starts one paid Grizzly activation.", "Get number",
  () => postAction("/api/actions/purchase", {confirm: true})
));
elements["stop-search"].addEventListener("click", () =>
  postAction("/api/actions/stop-search")
);
elements.cancel.addEventListener("click", () => confirmAction(
  "Cancel this number?", "The bot will keep retrying until Grizzly confirms cancellation.", "Cancel number",
  () => postAction("/api/actions/cancel", {confirm: true})
));
elements.retry.addEventListener("click", () => postAction("/api/actions/retry"));
elements["auto-retry-toggle"].addEventListener("change", async () => {
  const enabled = elements["auto-retry-toggle"].checked;
  elements["auto-retry-toggle"].disabled = true;
  try { await getAudioContext().resume(); } catch (error) { /* Audio remains optional. */ }
  await postAction("/api/actions/auto-retry", {enabled});
  elements["auto-retry-toggle"].disabled = false;
});
elements["copy-full"].addEventListener("click", () => copyToClipboard(copyValues.full, "International number"));
elements["copy-national"].addEventListener("click", () => copyToClipboard(copyValues.national, "National number"));
elements["copy-sms"].addEventListener("click", () => copyToClipboard(copyValues.sms, "Verification code"));
elements["theme-toggle"].addEventListener("click", () => applyTheme(
  document.documentElement.dataset.theme === "dark" ? "light" : "dark"
));
document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("logout").addEventListener("click", async () => {
  await postAction("/logout");
  window.location = "/login";
});

applyTheme(document.documentElement.dataset.theme || "dark");
document.addEventListener("pointerdown", () => {
  try { getAudioContext().resume(); } catch (error) { /* Audio remains optional. */ }
}, {once: true});
refresh();
window.setInterval(refresh, 2000);
