const csrfToken = document.querySelector('meta[name="csrf-token"]').content;
const elements = Object.fromEntries([
  "status-card", "phase", "countdown", "phone-number", "activation-id", "elapsed",
  "last-error", "purchase", "cancel", "retry", "events", "connection", "toast"
].map(id => [id, document.getElementById(id)]));

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

function render(status) {
  const phase = status.phase || "idle";
  elements["status-card"].className = `card status-card state-${phase}`;
  elements.phase.textContent = labels[phase] || phase.replaceAll("_", " ");
  elements["phone-number"].textContent = status.phoneNumber || "No number acquired";
  elements["activation-id"].textContent = status.activationId || "None";
  elements.elapsed.textContent = duration(status.elapsedSeconds);
  elements.countdown.textContent = status.activationId
    ? `${duration(status.timeoutRemainingSeconds)} remaining`
    : "No active timeout";
  elements["last-error"].textContent = status.lastError || "";
  visible(elements["last-error"], Boolean(status.lastError));
  visible(elements.purchase, status.canPurchase);
  visible(elements.cancel, status.canCancel);
  visible(elements.retry, status.canRetry);
  elements.events.innerHTML = status.events.length
    ? status.events.map(event => `<li><time>${new Date(event.time).toLocaleTimeString()}</time><span class="${event.level}">${escapeHtml(event.message)}</span></li>`).join("")
    : '<li class="empty">No activity yet.</li>';
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
document.getElementById("refresh").addEventListener("click", refresh);
document.getElementById("logout").addEventListener("click", async () => {
  await postAction("/logout");
  window.location = "/login";
});

refresh();
window.setInterval(refresh, 2000);
