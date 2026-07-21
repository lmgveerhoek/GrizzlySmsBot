const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const elements = Object.fromEntries([
  "status-card", "phase", "countdown", "phone-number", "activation-id", "elapsed",
  "last-error", "purchase", "cancel", "retry", "events", "connection", "toast",
  "phone-copy-controls", "copy-full", "copy-national", "sms-card", "sms-message",
  "copy-sms", "theme-toggle"
].map(id => [id, document.getElementById(id)]));
let copyValues = {full: "", national: "", sms: ""};
let eventsSignature = "";

const labels = {
  idle: "Idle", acquired: "Number acquired", ready_pending: "Confirming readiness",
  waiting_for_sms: "Waiting for SMS", resend_required: "Resend required",
  cancellation_pending: "Cancelling", code_notification_pending: "Delivering code",
  code_delivered: "Completing", completed: "Completed", cancelled: "Cancelled", failed: "Failed"
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
  elements["status-card"].className = `card status-card state-${phase}`;
  setText(elements.phase, labels[phase] || phase.replaceAll("_", " "));
  setText(elements["phone-number"], status.phoneNumber || "No number acquired");
  setText(elements["activation-id"], status.activationId || "None");
  setText(elements.elapsed, duration(status.elapsedSeconds));
  setText(elements.countdown, status.activationId
    ? `${duration(status.timeoutRemainingSeconds)} remaining`
    : "No active timeout");
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
  visible(elements.cancel, status.canCancel);
  visible(elements.retry, status.canRetry);
  const nextEventsSignature = JSON.stringify(status.events);
  if (eventsSignature !== nextEventsSignature) {
    eventsSignature = nextEventsSignature;
    elements.events.innerHTML = status.events.length
      ? status.events.map(event => `<li><time>${new Date(event.time).toLocaleTimeString()}</time><span class="${event.level}">${escapeHtml(event.message)}</span></li>`).join("")
      : '<li class="empty">No activity yet.</li>';
  }
}

function escapeHtml(value) {
  const node = document.createElement("span");
  node.textContent = value;
  return node.innerHTML;
}

async function refresh() {
  try {
    const response = await fetch("/api/status", {headers: {"Accept": "application/json"}});
    if (response.status === 401) { window.location = "/login"; return; }
    if (!response.ok) throw new Error("Status request failed");
    render(await response.json());
    elements.connection.textContent = "Connected";
    elements.connection.classList.add("online");
  } catch (error) {
    elements.connection.textContent = "Disconnected";
    elements.connection.classList.remove("online");
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
    await refresh();
  } catch (error) {
    showToast(error.message);
  } finally {
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
elements.cancel.addEventListener("click", () => confirmAction(
  "Cancel this number?", "The bot will keep retrying until Grizzly confirms cancellation.", "Cancel number",
  () => postAction("/api/actions/cancel", {confirm: true})
));
elements.retry.addEventListener("click", () => postAction("/api/actions/retry"));
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
refresh();
window.setInterval(refresh, 2000);
