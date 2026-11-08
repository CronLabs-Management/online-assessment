// Candidate exam runner. The server is the source of truth for time, sections and scoring.
// This file only displays state, sends actions, and keeps a local outbox for offline work.
import { Proctor } from "/static/js/proctor.js";

const root = document.getElementById("attempt-root");
const attemptId = root.dataset.attemptId;
const resultUrl = root.dataset.resultUrl || `/attempts/${attemptId}/result/`;
const cameraRequired = root.dataset.cameraRequired === "1";
const fullscreenRequired = root.dataset.fullscreenRequired === "1";
const missingFaceSeconds = Number(root.dataset.missingFace || 5);
const microphoneMonitoring = root.dataset.microphone === "1";
const SNAPSHOT_KINDS = new Set(["face_missing", "multiple_faces", "head_turn", "camera_lost"]);
const OUTBOX_KEY = `assessment-outbox-${attemptId}`;

const $ = (id) => document.getElementById(id);
const content = $("content");

let state = null;
let serverOffsetMs = 0;
let refreshing = false;
let flushPromise = null;
let mcqIndex = 0;
let activeProblemId = null;
let activeVisitKey = null;
let lastRenderKey = "";
let proctor = null;
let hiddenSince = null;
let blurSince = null;
const editors = {};
const editorHosts = {};
let monacoPromise = null;

// ---------------------------------------------------------------- helpers

function escapeHtml(value) {
  return String(value ?? "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function csrfToken() {
  const match = document.cookie.match(/(?:^|; )csrftoken=([^;]+)/);
  return match ? decodeURIComponent(match[1]) : "";
}

function fmt(seconds) {
  const s = Math.max(0, Math.floor(seconds));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const mmss = `${String(m).padStart(2, "0")}:${String(r).padStart(2, "0")}`;
  return h ? `${h}:${mmss}` : mmss;
}

function remainingFor(iso) {
  return (new Date(iso).getTime() - (Date.now() + serverOffsetMs)) / 1000;
}

let statusToastTimer = null;
function showStatus(message, tone = "info") {
  // Update aria-live status element if present
  const el = $("status");
  if (el) {
    el.textContent = message;
    el.className = `text-sm ${tone === "error" ? "text-red-700" : tone === "ok" ? "text-green-700" : "text-slate-600"}`;
  }

  // Floating Unmissable Status Toast (placed safely below sticky header at top: 84px)
  let toast = $("global-status-toast");
  if (!toast) {
    toast = document.createElement("div");
    toast.id = "global-status-toast";
    toast.className = "fixed left-1/2 -translate-x-1/2 z-[100000] px-5 py-3 rounded-2xl shadow-elevated border flex items-center gap-3 text-xs sm:text-sm font-extrabold transition-all duration-300 max-w-md w-[calc(100vw-2rem)] select-none pointer-events-auto";
    toast.style.top = "84px";
    document.body.appendChild(toast);
  }

  const icons = {
    ok: "✓",
    error: "✕",
    warn: "⚠️",
    info: "ℹ️"
  };

  const bgStyles = {
    ok: "background: #ECFDF5; border-color: #10B981; color: #065F46; box-shadow: 0 10px 25px -5px rgba(16, 185, 129, 0.3);",
    error: "background: #FEF2F2; border-color: #EF4444; color: #991B1B; box-shadow: 0 10px 25px -5px rgba(239, 68, 68, 0.3);",
    warn: "background: #FFFBEB; border-color: #F59E0B; color: #92400E; box-shadow: 0 10px 25px -5px rgba(245, 158, 11, 0.3);",
    info: "background: #EFF6FF; border-color: #3B82F6; color: #1E40AF; box-shadow: 0 10px 25px -5px rgba(59, 130, 246, 0.3);"
  };

  toast.style.cssText = `top: 84px; ${bgStyles[tone] || bgStyles.info} transform: translateX(-50%) translateY(0); opacity: 1; z-index: 100000;`;
  toast.innerHTML = `
    <span class="w-6 h-6 rounded-full flex items-center justify-center shrink-0 font-black text-xs ${tone === 'ok' ? 'bg-emerald-200 text-emerald-800' : tone === 'error' ? 'bg-rose-200 text-rose-800' : 'bg-amber-200 text-amber-800'}">${icons[tone] || icons.info}</span>
    <span class="flex-1">${escapeHtml(message)}</span>
    <button type="button" class="ml-2 text-current opacity-70 hover:opacity-100 font-black text-xs cursor-pointer p-1">✕</button>
  `;

  const closeBtn = toast.querySelector("button");
  if (closeBtn) {
    closeBtn.onclick = () => {
      toast.style.opacity = "0";
      toast.style.transform = "translateX(-50%) translateY(-10px)";
      setTimeout(() => toast.classList.add("hidden"), 300);
    };
  }

  toast.classList.remove("hidden");
  if (statusToastTimer) clearTimeout(statusToastTimer);
  statusToastTimer = setTimeout(() => {
    if (toast) {
      toast.style.opacity = "0";
      toast.style.transform = "translateX(-50%) translateY(-10px)";
      setTimeout(() => toast.classList.add("hidden"), 300);
    }
  }, 4500);
}

function setOffline(isOffline) {
  $("offline").classList.toggle("hidden", !isOffline);
}

async function api(method, path, body) {
  let response;
  try {
    response = await fetch(`/api/attempts/${attemptId}/${path}`, {
      method,
      credentials: "same-origin",
      headers: body
        ? { "Content-Type": "application/json", "X-CSRFToken": csrfToken() }
        : { "X-CSRFToken": csrfToken() },
      body: body ? JSON.stringify(body) : undefined,
    });
  } catch (_) {
    const err = new Error("Connection lost.");
    err.network = true;
    throw err;
  }
  let data = {};
  try {
    data = await response.json();
  } catch (_) {
    data = {};
  }
  if (!response.ok) {
    if (response.status === 401) {
      window.location.href = `/login/?next=${encodeURIComponent(window.location.pathname)}`;
    }
    const err = new Error(data.error || `Request failed (${response.status}).`);
    err.status = response.status;
    err.code = data.code;
    if (err.code === "session_mismatch") blockForSessionConflict(err.message);
    throw err;
  }
  return data;
}

// Once the test is open in another session, this page stops asking the server for anything and
// explains what to do. Polling stops so the refusal is not repeated every few seconds.
let sessionBlocked = false;
let refreshTimer = null;

function blockForSessionConflict(message) {
  if (sessionBlocked) return;
  sessionBlocked = true;
  clearInterval(refreshTimer);
  const conflictHtml = `<div class="p-6 rounded-2xl bg-rose-50 border border-rose-200 text-rose-900 shadow-sm" role="alert">
      <h3 class="text-base font-extrabold text-rose-950 flex items-center gap-2">
        <span class="text-lg">⚠️</span> This assessment is open in another browser or tab
      </h3>
      <p class="mt-2 text-xs leading-relaxed text-rose-800 font-semibold">${escapeHtml(message || "This test is open in another browser session.")}</p>
      <p class="mt-2 text-xs leading-relaxed text-slate-600">Please close the other browser window or tab. If you cannot find it, ask your administrator to allow you to resume here. All your answers and draft code so far are safely preserved.</p>
      <button type="button" onclick="window.location.reload()" class="btn btn-secondary btn-sm mt-4 text-xs font-bold">Retry This Session</button>
    </div>`;
  if ($("readiness")) {
    $("readiness").innerHTML = conflictHtml;
    $("readiness").classList.remove("hidden");
  }
  content.innerHTML = conflictHtml;
  content.classList.remove("hidden");
  $("status").textContent = "";
}

// ---------------------------------------------------------------- outbox (offline-safe saves)

function outboxLoad() {
  try {
    return JSON.parse(localStorage.getItem(OUTBOX_KEY) || "[]");
  } catch (_) {
    return [];
  }
}

function outboxSave(items) {
  try {
    localStorage.setItem(OUTBOX_KEY, JSON.stringify(items));
  } catch (_) {
    // Storage full or disabled: the server still holds everything it has received.
  }
}

function enqueue(item) {
  const entry = { ...item, id: `${Date.now()}-${Math.random().toString(36).slice(2)}` };
  const items = outboxLoad().filter((existing) => !(entry.key && existing.key === entry.key));
  items.push(entry);
  outboxSave(items);
  flushOutbox();
}

// Sends queued saves in order. Every caller that needs the outbox empty (for example before a
// section submit) awaits the same drain, so nothing queued earlier can reach the server after the submit.
function flushOutbox() {
  if (flushPromise) return flushPromise;
  const drain = async () => {
    const sent = new Set();
    while (true) {
      const next = outboxLoad().find((item) => !sent.has(item.id));
      if (!next) break;
      try {
        await api(next.method, next.path, next.body);
      } catch (err) {
        if (err.network) break;
        // A rule error (for example, a section already closed) cannot be retried.
        showStatus(err.message, "error");
      }
      sent.add(next.id);
      outboxSave(outboxLoad().filter((existing) => existing.id !== next.id));
    }
  };
  // Clear the shared promise only after it is stored. An empty outbox finishes synchronously, and
  // clearing inside the body would leave a settled promise in place and block every later flush.
  flushPromise = drain().finally(() => {
    flushPromise = null;
  });
  return flushPromise;
}

// Sends any code change still waiting for its debounce timer.
async function flushDrafts() {
  await Promise.all(
    Object.values(editors).map((pending) =>
      pending.then((editor) => editor.flush && editor.flush()).catch(() => {}),
    ),
  );
}

// ---------------------------------------------------------------- clock and state

let isAutoSubmitting = false;

async function handleAutoSubmit(reason) {
  if (isAutoSubmitting || !state || isFinal(state)) return;
  isAutoSubmitting = true;

  const msg = reason === "overall"
    ? "Exam time has expired. Submitting your test…"
    : "Section time has expired. Submitting this section…";
  showStatus(msg, "warn");

  try {
    // 2000ms safety timeout on draft and outbox flushes so pending code/events have time to flush
    await Promise.race([
      flushDrafts(),
      new Promise((resolve) => setTimeout(resolve, 2000)),
    ]);
    await Promise.race([
      flushOutbox(),
      new Promise((resolve) => setTimeout(resolve, 2000)),
    ]);
  } catch (err) {
    console.warn("Auto-submit flush warning:", err);
  }

  try {
    if (state.current_section) {
      await api("POST", "submit-section/", { section_id: state.current_section.id });
    }
  } catch (err) {
    // If backend already advanced or expired via time check, this is expected
    console.warn("Auto-submit section call:", err);
  }

  try {
    await refresh();
  } finally {
    isAutoSubmitting = false;
  }
}

function tick() {
  if (!state || !state.deadline) return;
  // Timer only runs once readiness check is complete and questions are displayed
  if (state.readiness_required) {
    $("overall").textContent = "--:--";
    $("timer").textContent = "--:--";
    return;
  }
  const overall = remainingFor(state.deadline);
  $("overall").textContent = fmt(overall);
  if (state.current_section) {
    const section = remainingFor(state.current_section.deadline);
    $("timer").textContent = fmt(section);
    // Warn in the last five minutes and alarm in the last minute. Colour is never the only cue: the text is the timer.
    $("timer").classList.toggle("text-amber-700", section > 60 && section <= 300);
    $("timer").classList.toggle("text-red-700", section > 0 && section <= 60);
    if (section <= 0 && !isAutoSubmitting) {
      handleAutoSubmit("section");
      return;
    }
  } else {
    $("timer").textContent = "--:--";
  }
  if (overall <= 0 && !isAutoSubmitting) {
    handleAutoSubmit("overall");
    return;
  }
}

async function refresh() {
  if (refreshing || sessionBlocked) return;
  refreshing = true;
  try {
    const next = await api("GET", "state/");
    state = next;
    serverOffsetMs = new Date(next.server_now).getTime() - Date.now();
    setOffline(false);
    flushOutbox();
    render();
  } catch (err) {
    if (err.status === 401) {
      window.location.href = `/login/?next=${encodeURIComponent(window.location.pathname)}`;
      return;
    }
    if (err.network) {
      setOffline(true);
    } else {
      showStatus(err.message, "error");
    }
  } finally {
    refreshing = false;
  }
}

function isFinal(s) {
  return ["submitted", "expired", "cancelled"].includes(s.status);
}

function render() {
  if (!state) return;
  if (isFinal(state)) {
    window.location.href = resultUrl;
    return;
  }
  renderSections();
  $("sections-panel").classList.remove("hidden");
  wireCameraControls();

  if (state.switch_count !== undefined) {
    localSwitchCount = Math.max(localSwitchCount, state.switch_count);
  }
  if (state.missing_count !== undefined) {
    localFaceMissingCount = Math.max(localFaceMissingCount, state.missing_count);
  }
  updateIntegrityUI();

  if (state.flagged_for_review && $("flag-warning-chip")) {
    $("flag-warning-chip").classList.remove("hidden");
  }

  if (state.readiness_required) {
    showReadiness();
    return;
  }
  hideReadiness();

  if (cameraRequired) {
    ensureProctor();
  }

  if (!state.current_section) {
    content.innerHTML = '<p class="text-slate-700">All sections are complete. Redirecting to your results…</p>';
    setTimeout(() => (window.location.href = resultUrl), 1200);
    return;
  }

  $("submit-section").classList.remove("hidden");
  $("section-title").textContent = state.current_section.title;

  const key = JSON.stringify([
    state.current_section.id,
    (state.questions || []).map((q) => [q.id, q.selected_option_id]),
    (state.problems || []).map((p) => [p.id, p.latest_submission ? p.latest_submission.version : 0]),
  ]);
  if (key !== lastRenderKey) {
    lastRenderKey = key;
    if (state.current_section.kind === "mcq") renderMcq();
    else renderCoding();
  }
  tick();
}

function renderSections() {
  const items = (state.sections || []).map((s, i) => {
    const isCurrent = s.status === "in_progress";
    const label = s.status === "submitted" ? "Submitted" : s.status === "expired" ? "Time up" : isCurrent ? "Active" : "Locked";
    const badgeClass = isCurrent
      ? "bg-blue-100 text-[#2447F5] border-blue-200"
      : s.status === "submitted"
      ? "bg-[#E3F5EC] text-[#0B6B43] border-emerald-200"
      : "bg-slate-100 text-slate-500 border-slate-200";
    return `<li class="flex items-center justify-between p-2.5 rounded-xl border ${isCurrent ? 'bg-blue-50/60 border-blue-200 shadow-xs' : 'bg-slate-50/70 border-slate-100'}">
      <span class="truncate ${isCurrent ? 'font-extrabold text-[#0F172A]' : 'font-semibold text-slate-700'}">${i + 1}. ${escapeHtml(s.title)}</span>
      <span class="text-[10px] uppercase font-extrabold px-2 py-0.5 rounded-md border ${badgeClass}">${label}</span>
    </li>`;
  });
  $("sections").innerHTML = items.join("");
}

// ---------------------------------------------------------------- camera readiness & diagnostics

function snapshotBox() {
  return $("snapshot-consent");
}

function snapshotConsent() {
  const box = snapshotBox();
  return Boolean(box && box.checked);
}

let localSwitchCount = 0;
let localFaceMissingCount = 0;
let lastFaceStatus = "ok";
let diagCamPassed = false;
let diagGeoPassed = false;
let diagNetPassed = false;
let diagFsPassed = false;
let latestLocation = null;
let latestNetwork = null;
let readinessInitialized = false;
let geoPendingTimer = null;

function updateIntegrityUI() {
  const switchLim = (state && state.tab_switch_threshold) || 3;

  // Tab switch stats in sidebar and camera widget
  const tabWidget = $("tab-switch-stat");
  if (tabWidget) {
    tabWidget.textContent = `${localSwitchCount} / ${switchLim}`;
    tabWidget.className = localSwitchCount > switchLim ? "font-mono font-bold text-rose-300" : localSwitchCount > 0 ? "font-mono font-bold text-amber-300" : "font-mono font-bold text-slate-200";
  }

  const tabSidebar = $("tab-switch-stat-sidebar");
  if (tabSidebar) {
    tabSidebar.textContent = `${localSwitchCount} / ${switchLim}`;
    if (localSwitchCount > switchLim) {
      tabSidebar.className = "font-mono font-bold text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-300";
    } else if (localSwitchCount > 0) {
      tabSidebar.className = "font-mono font-bold text-amber-700 bg-amber-50 px-2 py-0.5 rounded border border-amber-300";
    } else {
      tabSidebar.className = "font-mono font-bold text-slate-900 bg-slate-100 px-2 py-0.5 rounded border border-slate-200";
    }
  }

  // Face absence stats in sidebar and camera widget
  const faceWidget = $("cam-face-missing-stat");
  if (faceWidget) {
    faceWidget.textContent = String(localFaceMissingCount);
    faceWidget.className = localFaceMissingCount > 0 ? "font-mono font-bold text-rose-300" : "font-mono font-bold text-slate-200";
  }

  const faceSidebar = $("face-missing-stat-sidebar");
  if (faceSidebar) {
    faceSidebar.textContent = `${localFaceMissingCount} logged`;
    faceSidebar.className = localFaceMissingCount > 0
      ? "font-mono font-bold text-rose-700 bg-rose-50 px-2 py-0.5 rounded border border-rose-300"
      : "font-mono font-bold text-slate-900 bg-slate-100 px-2 py-0.5 rounded border border-slate-200";
  }

  if (localSwitchCount > switchLim && $("flag-warning-chip")) {
    $("flag-warning-chip").classList.remove("hidden");
  }
}

function evaluateReadinessGate() {
  const isFs = Boolean(document.fullscreenElement);
  diagFsPassed = !fullscreenRequired || isFs;
  const allPassed = diagCamPassed && diagGeoPassed && diagNetPassed && diagFsPassed;
  const startBtn = $("btn-confirm-readiness");
  const hint = $("diagnostic-gate-hint");
  const pill = $("diagnostic-overall-pill");

  if (startBtn) {
    startBtn.disabled = !allPassed;
    if (allPassed) {
      startBtn.classList.remove("opacity-50", "cursor-not-allowed");
      startBtn.innerHTML = `<span>Start Assessment ${isFs ? "(Full Screen Active)" : ""}</span><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"></path><path d="M13 6l6 6-6 6"></path></svg>`;
    } else {
      startBtn.classList.add("opacity-50", "cursor-not-allowed");
      startBtn.innerHTML = `<span>Complete Verification to Start</span><svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"><path d="M5 12h14"></path><path d="M13 6l6 6-6 6"></path></svg>`;
    }
  }

  if (pill) {
    if (allPassed) {
      pill.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-2xl bg-emerald-50 border border-emerald-200 text-emerald-900 text-xs font-bold shadow-xs";
      pill.innerHTML = `<span class="w-2 h-2 rounded-full bg-emerald-500"></span><span>✓ All Systems Verified</span>`;
    } else {
      pill.className = "flex items-center gap-2 px-3.5 py-1.5 rounded-2xl bg-amber-50 border border-amber-200 text-amber-900 text-xs font-bold shadow-xs";
      pill.innerHTML = `<span class="w-2 h-2 rounded-full bg-amber-500 animate-pulse"></span><span>Testing System Requirements…</span>`;
    }
  }

  if (hint) {
    if (allPassed) {
      hint.innerHTML = `<span class="text-emerald-600 font-bold">✓</span><span class="text-emerald-800 font-semibold">All diagnostics passed${isFs ? " and Full Screen is enabled" : ""}. You may begin the assessment.</span>`;
    } else {
      const missing = [];
      if (!diagCamPassed) missing.push("Camera");
      if (!diagGeoPassed) missing.push("Location");
      if (!diagNetPassed) missing.push("Internet");
      if (!diagFsPassed) missing.push("Full Screen");
      hint.innerHTML = `<span class="text-amber-500 font-bold">⚠️</span><span>Please complete: ${missing.join(", ")} to unlock assessment.</span>`;
    }
  }
}

function updateReadinessFullscreenUI() {
  const isFs = Boolean(document.fullscreenElement);
  diagFsPassed = !fullscreenRequired || isFs;
  const badge = $("diag-fs-badge");
  const detail = $("diag-fs-detail");
  const status = $("diag-fs-status");
  const btn = $("btn-toggle-diag-fullscreen");

  if (!fullscreenRequired) {
    if (badge) {
      badge.className = "diag-badge-verified";
      badge.textContent = "✓ Optional";
    }
    if (detail) detail.textContent = "Full screen mode is optional for this assessment.";
    if (status) status.textContent = isFs ? "Full screen active" : "Window mode";
    if (btn) {
      btn.className = "btn btn-secondary btn-sm py-1.5 px-3.5 text-xs font-bold text-slate-700 bg-slate-100 border border-slate-200";
      btn.textContent = isFs ? "✓ Fullscreen Active" : "Toggle Full Screen";
    }
  } else if (diagFsPassed) {
    if (badge) {
      badge.className = "diag-badge-verified";
      badge.textContent = "✓ Passed";
    }
    if (detail) detail.textContent = "Full screen mode enabled and verified.";
    if (status) status.textContent = "Full screen active";
    if (btn) {
      btn.className = "btn btn-secondary btn-sm py-1.5 px-3.5 text-xs font-bold text-emerald-700 bg-emerald-50 border border-emerald-200";
      btn.textContent = "✓ Fullscreen Active";
    }
  } else {
    if (badge) {
      badge.className = "diag-badge-action";
      badge.textContent = "Action Required";
    }
    if (detail) detail.textContent = "Enable full screen mode to prevent distractions.";
    if (status) status.textContent = "Window mode (Full screen required)";
    if (btn) {
      btn.className = "btn btn-primary btn-sm py-1.5 px-3.5 text-xs font-extrabold shadow-cta";
      btn.textContent = "Enable Full Screen";
    }
  }
  evaluateReadinessGate();
}

async function testReadinessPing() {
  if (diagNetPassed && latestNetwork) {
    evaluateReadinessGate();
    return;
  }
  const badge = $("diag-net-badge");
  const detail = $("diag-net-detail");
  const metrics = $("diag-net-metrics");
  const btnRetest = $("diag-retest-net");

  if (badge) {
    badge.className = "diag-badge-pending";
    badge.textContent = "Testing…";
  }
  if (btnRetest && !btnRetest.dataset.wired) {
    btnRetest.dataset.wired = "1";
    btnRetest.addEventListener("click", () => {
      diagNetPassed = false;
      latestNetwork = null;
      testReadinessPing();
    });
  }

  const controller = new AbortController();
  const timeoutId = setTimeout(() => controller.abort(), 3500);
  const t0 = performance.now();
  try {
    const res = await fetch(`/api/attempts/${attemptId}/state/`, {
      method: "GET",
      credentials: "same-origin",
      cache: "no-store",
      signal: controller.signal,
    });
    clearTimeout(timeoutId);
    const latency = Math.max(8, Math.round(performance.now() - t0));
    if (res.ok) {
      diagNetPassed = true;
      latestNetwork = {
        server_reachable: true,
        rtt: latency,
        timestamp: new Date().toISOString(),
      };
      if (badge) {
        badge.className = "diag-badge-verified";
        badge.textContent = "✓ Verified";
      }
      if (metrics) metrics.textContent = `${latency} ms`;
      if (detail) detail.textContent = `Server reachable • ${latency}ms latency.`;
    } else {
      diagNetPassed = false;
      latestNetwork = {
        server_reachable: false,
        status: res.status,
        timestamp: new Date().toISOString(),
      };
      if (badge) {
        badge.className = "diag-badge-action";
        badge.textContent = `HTTP ${res.status}`;
      }
      if (metrics) metrics.textContent = "Error";
      if (detail) detail.textContent = `Server responded with status ${res.status}. Assessment not reachable.`;
    }
  } catch (err) {
    clearTimeout(timeoutId);
    diagNetPassed = false;
    const isOnline = navigator.onLine;
    latestNetwork = {
      server_reachable: false,
      online: isOnline,
      error: err.name === "AbortError" ? "timeout" : "network_error",
      timestamp: new Date().toISOString(),
    };
    if (badge) {
      badge.className = "diag-badge-action";
      badge.textContent = isOnline ? "Unreachable" : "Offline";
    }
    if (metrics) metrics.textContent = isOnline ? "Timeout" : "Offline";
    if (detail) {
      detail.textContent = isOnline
        ? "Browser reports network available; application server not reachable."
        : "No internet connection detected.";
    }
  }
  evaluateReadinessGate();
}

function initReadinessGeo() {
  if (diagGeoPassed && latestLocation) {
    evaluateReadinessGate();
    return;
  }
  const badge = $("diag-geo-badge");
  const detail = $("diag-geo-detail");
  const metrics = $("diag-geo-coords") || $("diag-geo-metrics");
  const btnRetry = $("diag-retry-geo");

  if (badge) {
    badge.className = "diag-badge-pending";
    badge.textContent = "Detecting…";
  }
  if (btnRetry && !btnRetry.dataset.wired) {
    btnRetry.dataset.wired = "1";
    btnRetry.addEventListener("click", () => {
      diagGeoPassed = false;
      latestLocation = null;
      initReadinessGeo();
    });
  }

  if (geoPendingTimer) clearTimeout(geoPendingTimer);
  let resolved = false;

  const resolveFallback = (reason) => {
    if (resolved) return;
    resolved = true;
    if (geoPendingTimer) clearTimeout(geoPendingTimer);
    diagGeoPassed = true;
    latestLocation = {
      source: "unavailable",
      reason: reason || "timeout",
      timestamp: new Date().toISOString(),
    };
    if (badge) {
      badge.className = "inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-100 text-slate-700 border border-slate-200";
      badge.textContent = "Location Unavailable";
    }
    if (metrics) metrics.textContent = "N/A";
    if (detail) {
      detail.textContent = reason === "permission_denied"
        ? "Browser location access was denied. Recorded for audit."
        : "Location services unavailable. Recorded for audit.";
    }
    if (btnRetry) {
      btnRetry.textContent = "Retry";
      btnRetry.className = "text-xs font-semibold text-slate-500 hover:text-slate-800";
    }
    evaluateReadinessGate();
  };

  geoPendingTimer = setTimeout(() => {
    resolveFallback("timeout_fallback");
  }, 3500);

  if (!navigator.geolocation) {
    resolveFallback("unsupported");
    return;
  }

  try {
    navigator.geolocation.getCurrentPosition(
      (pos) => {
        if (resolved) return;
        resolved = true;
        if (geoPendingTimer) clearTimeout(geoPendingTimer);
        diagGeoPassed = true;
        latestLocation = {
          source: "gps",
          latitude: Number(pos.coords.latitude.toFixed(6)),
          longitude: Number(pos.coords.longitude.toFixed(6)),
          accuracy: Math.round(pos.coords.accuracy),
          timestamp: new Date().toISOString(),
        };
        if (badge) {
          badge.className = "diag-badge-verified";
          badge.textContent = "✓ GPS Locked";
        }
        if (metrics) metrics.textContent = `±${Math.round(pos.coords.accuracy)}m`;
        if (detail) detail.textContent = `Coordinates locked: ${pos.coords.latitude.toFixed(4)}°, ${pos.coords.longitude.toFixed(4)}°`;
        if (btnRetry) {
          btnRetry.textContent = "✓ Verified";
          btnRetry.className = "text-xs font-bold text-emerald-600";
        }
        evaluateReadinessGate();
      },
      (err) => {
        console.warn("Geolocation warning/denied:", err);
        resolveFallback(err.code === 1 ? "permission_denied" : "unavailable");
      },
      { enableHighAccuracy: false, timeout: 3000, maximumAge: 300000 }
    );
  } catch (err) {
    console.warn("Geolocation call error:", err);
    resolveFallback("exception");
  }
}

function wireReadinessButton() {
  const btn = $("btn-confirm-readiness");
  if (!btn || btn.dataset.wired) return;
  btn.dataset.wired = "1";
  btn.addEventListener("click", async () => {
    if (fullscreenRequired && !document.fullscreenElement) {
      try {
        await document.documentElement.requestFullscreen();
      } catch (_) {}
    }

    btn.disabled = true;
    btn.innerHTML = `<span>Unlocking Assessment…</span>`;
    try {
      const payload = {
        snapshot_consent: snapshotConsent(),
        location: latestLocation,
        network: latestNetwork,
        device: {
          screen: `${window.screen.width}x${window.screen.height}`,
          viewport: `${window.innerWidth}x${window.innerHeight}`,
          platform: navigator.platform,
          language: navigator.language,
          pixelRatio: window.devicePixelRatio || 1,
          colorDepth: window.screen.colorDepth || 24,
        },
      };
      await api("POST", "readiness/", payload);
      if (proctor) {
        proctor.confirmReady();
      }
      hideReadiness();
      await refresh();
    } catch (err) {
      showStatus(err.message, "error");
      btn.disabled = false;
      evaluateReadinessGate();
    }
  });
}

function wireRetryCamera() {
  const retryBtn = $("retry-camera");
  const overlay = $("camera-error-overlay");
  if (!retryBtn || retryBtn.dataset.wired) return;
  retryBtn.dataset.wired = "1";
  retryBtn.addEventListener("click", () => {
    if (overlay) overlay.classList.add("hidden");
    ensureProctor();
    if (proctor) {
      proctor.start().then(() => {
        if (overlay) overlay.classList.add("hidden");
      }).catch(() => {
        if (overlay) overlay.classList.remove("hidden");
      });
    }
  });
}

let proctorAlertTimer = null;

function showProctorPrompt({ title, body, limitText = "", countText = "", tone = "warn" }) {
  const alertEl = $("proctor-alert");
  if (!alertEl) return;
  const titleEl = $("proctor-alert-title");
  const bodyEl = $("proctor-alert-body");
  const limitEl = $("proctor-alert-limit");
  const countEl = $("proctor-alert-count");
  const iconEl = $("proctor-alert-icon");

  if (titleEl) titleEl.textContent = title;
  if (bodyEl) bodyEl.textContent = body;
  if (limitEl) {
    if (limitText) {
      limitEl.textContent = limitText;
      limitEl.classList.remove("hidden");
    } else {
      limitEl.classList.add("hidden");
    }
  }
  if (countEl) {
    countEl.textContent = countText || `Face Absences: ${localFaceMissingCount} | Tab Switches: ${localSwitchCount}`;
    countEl.classList.remove("hidden");
  }
  if (iconEl) iconEl.textContent = tone === "bad" ? "🚨" : tone === "good" ? "✓" : "⚠️";

  alertEl.className = tone === "bad" ? "bad" : tone === "good" ? "good" : "";
  alertEl.classList.remove("hidden");

  if (proctorAlertTimer) clearTimeout(proctorAlertTimer);
  const timeoutMs = tone === "good" ? 1800 : 7500;
  proctorAlertTimer = setTimeout(() => {
    alertEl.classList.add("hidden");
  }, timeoutMs);
}

function dismissProctorPrompt() {
  const alertEl = $("proctor-alert");
  if (alertEl) alertEl.classList.add("hidden");
  if (proctorAlertTimer) clearTimeout(proctorAlertTimer);
}

function wireCameraControls() {
  const dismissBtn = $("dismiss-proctor-alert");
  if (dismissBtn && !dismissBtn.dataset.wired) {
    dismissBtn.dataset.wired = "1";
    dismissBtn.addEventListener("click", dismissProctorPrompt);
  }
}

function handleFaceStatus(face) {
  const guide = $("camera-face-guide");

  if (face.status === "missing") {
    if (guide) {
      guide.className = "camera-face-guide absolute inset-0.5 rounded-lg border-2 border-rose-500/90 animate-pulse pointer-events-none transition-colors";
    }

    if (lastFaceStatus !== "missing") {
      lastFaceStatus = "missing";
      localFaceMissingCount += 1;
      updateIntegrityUI();
      showProctorPrompt({
        title: "No Face Detected",
        body: "Your face is not visible in the camera preview. Please look directly forward.",
        limitText: `Threshold: ${missingFaceSeconds}s continuous absence before audit flag.`,
        countText: `Face Absences: ${localFaceMissingCount} logged | Tab Switches: ${localSwitchCount}`,
        tone: "bad",
      });
    }
  } else if (face.status === "turned") {
    if (guide) {
      guide.className = "camera-face-guide absolute inset-0.5 rounded-lg border-2 border-amber-400/90 pointer-events-none transition-colors";
    }

    if (lastFaceStatus !== "turned") {
      lastFaceStatus = "turned";
      showProctorPrompt({
        title: "Face Not Fully Shown / Looking Away",
        body: "Your face is turned or partially out of the frame. Please look directly forward.",
        limitText: "Prolonged gaze away is recorded in the proctoring audit log.",
        countText: `Face Absences: ${localFaceMissingCount} logged | Tab Switches: ${localSwitchCount}`,
        tone: "warn",
      });
    }
  } else if (face.status === "multiple") {
    if (guide) {
      guide.className = "camera-face-guide absolute inset-0.5 rounded-lg border-2 border-rose-500/90 animate-pulse pointer-events-none transition-colors";
    }

    if (lastFaceStatus !== "multiple") {
      lastFaceStatus = "multiple";
      showProctorPrompt({
        title: "Multiple People Detected",
        body: "More than one face was detected in camera view. You must complete this assessment alone.",
        limitText: "Multiple faces immediately flag your assessment for review.",
        countText: `Face Absences: ${localFaceMissingCount} logged | Tab Switches: ${localSwitchCount}`,
        tone: "bad",
      });
    }
  } else if (face.status === "unavailable") {
    if (guide) {
      guide.className = "camera-face-guide absolute inset-0.5 rounded-lg border border-slate-300 pointer-events-none transition-colors";
    }
    const camStatus = $("camera-status");
    if (camStatus) camStatus.textContent = "Camera active; face detector unavailable";
    if (!diagCamPassed) {
      diagCamPassed = true;
      const camBadge = $("diag-cam-badge");
      if (camBadge) {
        camBadge.className = "inline-flex items-center gap-1 px-2 py-0.5 rounded text-[11px] font-semibold bg-slate-100 text-slate-700 border border-slate-200";
        camBadge.textContent = "Detector Unavailable";
      }
      const camDetail = $("diag-cam-detail");
      if (camDetail) camDetail.textContent = "Camera feed active; face detection unavailable in this environment.";
      evaluateReadinessGate();
    }
    if (lastFaceStatus === "missing" || lastFaceStatus === "turned" || lastFaceStatus === "multiple") {
      dismissProctorPrompt();
    }
    lastFaceStatus = "unavailable";
  } else if (face.status === "ok") {
    if (guide) {
      guide.className = "camera-face-guide absolute inset-0.5 rounded-lg border border-emerald-400/80 pointer-events-none transition-colors";
    }

    if (!diagCamPassed) {
      diagCamPassed = true;
      const camBadge = $("diag-cam-badge");
      if (camBadge) {
        camBadge.className = "diag-badge-verified";
        camBadge.textContent = "✓ Verified";
      }
      const camDetail = $("diag-cam-detail");
      if (camDetail) camDetail.textContent = "Camera active & face centered in guide.";
      evaluateReadinessGate();
    }

    if (lastFaceStatus === "missing" || lastFaceStatus === "turned" || lastFaceStatus === "multiple") {
      lastFaceStatus = "ok";
      showProctorPrompt({
        title: "Face Detected",
        body: "Your face is now properly aligned and centered in view.",
        countText: `Face Absences: ${localFaceMissingCount} logged | Tab Switches: ${localSwitchCount}`,
        tone: "good",
      });
    }
  }
}

function showReadiness() {
  $("attempt-root")?.classList.remove("is-coding-page");
  document.body.classList.remove("theme-dark");
  document.body.style.backgroundColor = "";
  $("header-submit-section")?.classList.add("hidden");
  $("section-title").textContent = "Candidate Verification & Pre-Assessment Setup";
  $("readiness").classList.remove("hidden");
  content.classList.add("hidden");
  wireReadinessButton();
  wireRetryCamera();
  wireCameraControls();

  const btnFs = $("btn-toggle-diag-fullscreen");
  if (btnFs && !btnFs.dataset.wired) {
    btnFs.dataset.wired = "1";
    btnFs.addEventListener("click", () => {
      if (!document.fullscreenElement) {
        document.documentElement.requestFullscreen().then(() => {
          updateReadinessFullscreenUI();
        }).catch((err) => {
          console.warn("Fullscreen request error:", err);
          showStatus("Please allow full screen in your browser to proceed.", "error");
        });
      } else {
        updateReadinessFullscreenUI();
      }
    });
  }

  const btnRetestNet = $("diag-retest-net");
  if (btnRetestNet && !btnRetestNet.dataset.wired) {
    btnRetestNet.dataset.wired = "1";
    btnRetestNet.addEventListener("click", () => {
      diagNetPassed = false;
      latestNetwork = null;
      testReadinessPing();
    });
  }

  const btnRetryGeo = $("diag-retry-geo");
  if (btnRetryGeo && !btnRetryGeo.dataset.wired) {
    btnRetryGeo.dataset.wired = "1";
    btnRetryGeo.addEventListener("click", () => {
      diagGeoPassed = false;
      latestLocation = null;
      initReadinessGeo();
    });
  }

  const btnRetryCam = $("diag-retry-cam");
  if (btnRetryCam && !btnRetryCam.dataset.wired) {
    btnRetryCam.dataset.wired = "1";
    btnRetryCam.addEventListener("click", () => {
      diagCamPassed = false;
      const camBadge = $("diag-cam-badge");
      if (camBadge) {
        camBadge.className = "diag-badge-pending";
        camBadge.textContent = "Testing…";
      }
      ensureProctor();
      if (proctor) {
        proctor.beginReadinessCheck();
        proctor.start();
      }
    });
  }

  document.removeEventListener("fullscreenchange", updateReadinessFullscreenUI);
  document.addEventListener("fullscreenchange", updateReadinessFullscreenUI);

  if (readinessInitialized) {
    updateReadinessFullscreenUI();
    evaluateReadinessGate();
    return;
  }
  readinessInitialized = true;

  if (cameraRequired) {
    ensureProctor();
    if (proctor) proctor.beginReadinessCheck();
  } else {
    diagCamPassed = true;
    const camBadge = $("diag-cam-badge");
    if (camBadge) {
      camBadge.className = "diag-badge-verified";
      camBadge.textContent = "✓ Optional";
    }
  }

  initReadinessGeo();
  testReadinessPing();
  updateReadinessFullscreenUI();
}

function hideReadiness() {
  readinessInitialized = false;
  if (geoPendingTimer) clearTimeout(geoPendingTimer);
  document.removeEventListener("fullscreenchange", updateReadinessFullscreenUI);
  $("readiness").classList.add("hidden");
  content.classList.remove("hidden", "bg-slate-50", "pointer-events-none");
  content.removeAttribute("aria-disabled");
}

function ensureProctor() {
  wireRetryCamera();
  wireCameraControls();
  if (proctor) {
    if (!proctor.running && !proctor.starting) startProctor();
    return;
  }
  proctor = new Proctor({
    video: $("camera"),
    missingFaceSeconds,
    microphone: microphoneMonitoring,
    onEvent: sendEvent,
    onStatus: (message) => {
      const el = $("camera-status");
      if (el) el.textContent = message;
    },
    onFaceStatus: handleFaceStatus,
    onReadyChange: (ready) => {
      if (!ready) return;
      const el = $("camera-status");
      if (el) el.textContent = "Camera active.";
      diagCamPassed = true;
      const camBadge = $("diag-cam-badge");
      if (camBadge) {
        camBadge.className = "diag-badge-verified";
        camBadge.textContent = "✓ Verified";
      }
      const camDetail = $("diag-cam-detail");
      if (camDetail) camDetail.textContent = "Camera active & face centered in guide oval.";
      evaluateReadinessGate();
    },
  });
  startProctor();
}

// A second monitor lets someone else read the questions or the candidate read notes. Chromium reports
// it through screen.isExtended; other browsers do not, so this is evidence when present, never proof.
let screensReported = false;
let screensWatched = false;
function watchScreens() {
  if (screensWatched) return;
  screensWatched = true;
  if (typeof window.screen === "undefined" || !("isExtended" in window.screen)) return;
  const check = () => {
    if (window.screen.isExtended && !screensReported) {
      screensReported = true;
      sendEvent("multiple_screens", 0, { width: window.screen.width, height: window.screen.height });
    } else if (!window.screen.isExtended) {
      screensReported = false;
    }
  };
  check();
  if (typeof window.screen.addEventListener === "function") window.screen.addEventListener("change", check);
}

function startProctor() {
  wireRetryCamera();
  const overlay = $("camera-error-overlay");
  const camBadge = $("diag-cam-badge");
  const camDetail = $("diag-cam-detail");

  if (camBadge && !diagCamPassed) {
    camBadge.className = "diag-badge-pending";
    camBadge.textContent = "Connecting…";
  }
  if (camDetail && !diagCamPassed) {
    camDetail.textContent = "Requesting webcam access. Please click 'Allow' on your browser pop-up…";
  }

  const watchdog = setTimeout(() => {
    if (!diagCamPassed && (!proctor || !proctor.stream || !proctor.stream.active)) {
      if (camBadge) {
        camBadge.className = "diag-badge-action";
        camBadge.textContent = "Action Required";
      }
      if (camDetail) {
        camDetail.textContent = "Camera permission prompt pending or blocked. Please allow camera in your browser address bar and click 'Retry Camera'.";
      }
      if (overlay) overlay.classList.remove("hidden");
      evaluateReadinessGate();
    }
  }, 8000);

  proctor.start().then(() => {
    clearTimeout(watchdog);
    if (overlay) overlay.classList.add("hidden");
    if (camDetail && !diagCamPassed) {
      camDetail.textContent = "Camera feed active. Verifying face alignment…";
    }
    // If stream is active, auto-verify camera within 3.5 seconds so candidate is never trapped on the hardware page
    setTimeout(() => {
      if (!diagCamPassed && proctor && proctor.stream && proctor.stream.active) {
        diagCamPassed = true;
        if (camBadge) {
          camBadge.className = "diag-badge-verified";
          camBadge.textContent = "✓ Verified";
        }
        if (camDetail) {
          camDetail.textContent = "Camera feed active & verified.";
        }
        evaluateReadinessGate();
      }
    }, 3500);
  }).catch((err) => {
    clearTimeout(watchdog);
    if (overlay) overlay.classList.remove("hidden");
    diagCamPassed = false;
    const reason = (err && err.name === "NotAllowedError")
      ? "Camera permission denied"
      : ((err && err.name === "NotFoundError") ? "No camera device found" : "Camera device unavailable or busy");
    if (camBadge) {
      camBadge.className = "diag-badge-action";
      camBadge.textContent = "Action Required";
    }
    if (camDetail) {
      camDetail.textContent = `${reason}. Allow camera in your browser address bar, then click 'Retry Camera'.`;
    }
    evaluateReadinessGate();
  });
}

// ---------------------------------------------------------------- events and snapshots

async function sendEvent(kind, durationMs, context = {}) {
  const body = {
    kind,
    duration_ms: Math.round(durationMs || 0),
    client_at: new Date().toISOString(),
    context,
  };
  try {
    const data = await api("POST", "events/", body);
    if (data.switch_count !== undefined) {
      localSwitchCount = Math.max(localSwitchCount, data.switch_count);
    }
    if (data.missing_count !== undefined) {
      localFaceMissingCount = Math.max(localFaceMissingCount, data.missing_count);
    }
    updateIntegrityUI();

    if (data.flagged && $("flag-warning-chip")) {
      $("flag-warning-chip").classList.remove("hidden");
    }

    if (kind === "camera_revoked" || kind === "camera_lost") {
      const overlay = $("camera-error-overlay");
      if (overlay) overlay.classList.remove("hidden");
      showProctorPrompt({
        title: "Camera Feed Disconnected",
        body: "Your camera connection was lost. Please check permissions and click Retry on the webcam preview.",
        tone: "bad",
      });
    } else if (kind === "multiple_screens") {
      showProctorPrompt({
        title: "Second screen detected",
        body: "Disconnect any extra monitors and use a single screen for this test. This has been recorded for the reviewer.",
        tone: "bad",
      });
    } else if (kind === "virtual_camera") {
      showProctorPrompt({
        title: "Software camera detected",
        body: "Please use your device's real webcam, not a virtual camera app. This has been recorded for the reviewer.",
        tone: "bad",
      });
    } else if (kind === "tab_hidden" || kind === "window_blur") {
      const switches = localSwitchCount || data.switch_count || 1;
      const limit = data.switch_limit || (state && state.tab_switch_threshold) || 3;
      showProctorPrompt({
        title: "Tab Switch / Window Blur Detected",
        body: "Leaving or minimizing the assessment screen is monitored.",
        limitText: `Threshold: ${limit} tab switches allowed before reviewer flag.`,
        countText: `Tab Switches: ${switches} / ${limit} | Face Absences: ${localFaceMissingCount}`,
        tone: switches > limit ? "bad" : "warn",
      });
    } else if (kind === "fullscreen_exit") {
      showProctorPrompt({
        title: "Fullscreen Exited",
        body: "This test requires fullscreen mode. Leaving fullscreen is recorded.",
        limitText: `Threshold: ${data.switch_limit || 3} window exits allowed.`,
        countText: `Tab Switches: ${localSwitchCount} | Face Absences: ${localFaceMissingCount}`,
        tone: "bad",
      });
    } else if (kind === "face_missing") {
      showProctorPrompt({
        title: "Face Absence Recorded",
        body: `Face was missing for ${Math.round(durationMs / 1000)}s.`,
        limitText: `Policy threshold: ${missingFaceSeconds}s.`,
        countText: `Face Absences: ${localFaceMissingCount} logged | Tab Switches: ${localSwitchCount}`,
        tone: "bad",
      });
    }

    if (data.over_threshold && SNAPSHOT_KINDS.has(kind)) {
      await captureSnapshot(data.event_id);
    }
  } catch (err) {
    if (err.network) {
      enqueue({ method: "POST", path: "events/", body, key: null });
    }
  }
}

async function captureSnapshot(eventId) {
  // Images are only taken with the candidate's consent, as recorded by the server.
  if (!state || !state.snapshot_consent) return;
  const video = $("camera");
  if (!video.videoWidth) return;
  const canvas = $("snapshot-canvas");
  canvas.width = 320;
  canvas.height = 240;
  canvas.getContext("2d").drawImage(video, 0, 0, 320, 240);
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/jpeg", 0.6));
  if (!blob) return;
  const form = new FormData();
  form.append("image", blob, "snapshot.jpg");
  form.append("event_id", String(eventId));
  form.append("client_at", new Date().toISOString());
  const upload = () =>
    fetch(`/api/attempts/${attemptId}/snapshots/`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken() },
      body: form,
    });
  try {
    const res = await upload();
    if (!res.ok) throw new Error(`Snapshot upload returned ${res.status}`);
  } catch (err) {
    // Retry once after 1s on transient failure
    try {
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await upload();
    } catch (retryErr) {
      console.warn("Telemetry snapshot upload failed after retry:", retryErr);
    }
  }
}

function shouldTrackTabSwitch() {
  // Track post test start, once questions are unlocked
  return Boolean(
    testIsLive() &&
    state &&
    !state.readiness_required &&
    state.current_section
  );
}

document.addEventListener("visibilitychange", () => {
  if (!shouldTrackTabSwitch()) {
    hiddenSince = null;
    return;
  }
  if (document.hidden) {
    hiddenSince = Date.now();
  } else if (hiddenSince !== null) {
    const dur = Date.now() - hiddenSince;
    hiddenSince = null;
    localSwitchCount += 1;
    updateIntegrityUI();
    sendEvent("tab_hidden", dur, {});
  }
});

window.addEventListener("blur", () => {
  if (!shouldTrackTabSwitch()) {
    blurSince = null;
    return;
  }
  if (!document.hidden) blurSince = Date.now();
});

window.addEventListener("focus", () => {
  if (!shouldTrackTabSwitch() || blurSince === null) {
    blurSince = null;
    return;
  }
  const dur = Date.now() - blurSince;
  blurSince = null;
  localSwitchCount += 1;
  updateIntegrityUI();
  sendEvent("window_blur", dur, {});
});

// Copy, cut and paste are recorded. A very large paste is flagged, since it is unlikely to be typed.
// Only the length is sent, never the text itself.
["copy", "cut", "paste"].forEach((type) => {
  document.addEventListener(type, (event) => {
    if (!state || isFinal(state)) return;
    const text = type === "paste" && event.clipboardData ? event.clipboardData.getData("text") : "";
    sendEvent("clipboard", 0, { action: type, chars: text.length });
  });
});

// Full-screen gate: while the test is live and the browser is not full screen, the test is covered
// until the candidate returns to full screen. Each exit is recorded.
function setupFullscreenGate() {
  if ($("attempt-root").dataset.fullscreenRequired !== "1") return;
  document.addEventListener("fullscreenchange", () => {
    if (!document.fullscreenElement && testIsLive()) sendEvent("fullscreen_exit", 0, {});
    updateFullscreenGate();
  });
  $("enter-fullscreen").addEventListener("click", () => {
    document.documentElement.requestFullscreen().then(updateFullscreenGate).catch(() => {
      showStatus("Your browser did not allow full screen. Try the button again.", "error");
    });
  });
}

function testIsLive() {
  return Boolean(state && state.current_section && !isFinal(state) && !state.readiness_required);
}

function updateFullscreenGate() {
  if ($("attempt-root").dataset.fullscreenRequired !== "1") return;
  const gate = $("fullscreen-gate");
  if (!gate) return;
  const needed = testIsLive() && !document.fullscreenElement;
  gate.classList.toggle("active", needed);
  gate.classList.toggle("hidden", !needed);
  gate.style.display = needed ? "flex" : "none";
}

window.addEventListener("online", () => {
  setOffline(false);
  refresh();
});

window.addEventListener("offline", () => setOffline(true));

// ---------------------------------------------------------------- visits

function openVisit(kind, itemId) {
  const key = `${kind}:${itemId}`;
  if (key === activeVisitKey || (state && state.readiness_required)) return;
  activeVisitKey = key;
  api("POST", "visits/", { kind, item_id: itemId }).catch((err) => {
    activeVisitKey = null;
    if (err.network) return;
    showStatus(err.message, "error");
  });
}

// ---------------------------------------------------------------- MCQ

function renderMcq() {
  $("attempt-root")?.classList.remove("is-coding-page");
  document.body.classList.remove("theme-dark");
  document.body.style.backgroundColor = "";
  $("header-submit-section")?.classList.add("hidden");
  content.classList.remove("is-coding-view");
  const layoutRoot = document.querySelector(".attempt-layout");
  if (layoutRoot) layoutRoot.classList.remove("is-coding");

  const questions = state.questions || [];
  if (!questions.length) {
    content.innerHTML = '<p class="text-slate-600">No questions in this section.</p>';
    return;
  }
  if (mcqIndex >= questions.length) mcqIndex = 0;
  const question = questions[mcqIndex];

  const nav = questions
    .map((q, i) => {
      const answered = q.selected_option_id !== null && q.selected_option_id !== undefined;
      const current = i === mcqIndex;
      const tone = current
        ? "border-[#2447F5] bg-[#2447F5] text-white shadow-xs font-bold"
        : answered
          ? "border-[#0B6B43] bg-[#E3F5EC] text-[#0B6B43] font-bold"
          : "border-slate-300 bg-white text-slate-700 hover:border-slate-500 font-bold";
      const label = `Question ${i + 1}${answered ? ", answered" : ", not answered"}${current ? ", current" : ""}`;
      return `<button type="button" data-idx="${i}" aria-label="${label}" ${current ? 'aria-current="step"' : ""} class="num h-10 w-10 rounded-xl border text-sm font-bold ${tone}">${i + 1}</button>`;
    })
    .join("");

  const options = question.options
    .map((option, optIdx) => {
      const checked = option.id === question.selected_option_id ? "checked" : "";
      const letter = String.fromCharCode(65 + optIdx);
      return `<label class="flex cursor-pointer items-start gap-4 rounded-2xl border-[1.5px] border-slate-200 bg-white p-5 leading-relaxed hover:border-blue-400 hover:bg-blue-50/20 has-[:checked]:border-[#2447F5] has-[:checked]:bg-[#EEF1FE]/50 has-[:checked]:shadow-xs transition-all">
        <input type="radio" name="mcq-${question.id}" value="${option.id}" ${checked} class="mt-1 h-5 w-5 accent-[#2447F5]">
        <div class="flex-1">
          <span class="mr-2 inline-block rounded-lg bg-slate-100 px-2.5 py-1 text-xs font-extrabold text-slate-800">${letter}</span>
          ${option.text ? `<span class="text-base sm:text-lg font-bold text-[#0F172A]">${escapeHtml(option.text)}</span>` : ""}
          ${option.image_url ? `<img src="${escapeHtml(option.image_url)}" alt="Option ${letter}" draggable="false" class="mt-3 block max-h-56 max-w-full rounded-lg border border-slate-200 bg-white select-none">` : ""}
        </div>
      </label>`;
    })
    .join("");

    const isLastQuestion = mcqIndex >= questions.length - 1;
    const actionButtonHtml = isLastQuestion
      ? `<button type="button" id="submit-section-mcq" class="btn btn-primary px-7 py-3 text-base font-extrabold btn-submit-section shadow-cta">Submit Section &rarr;</button>`
      : `<button type="button" id="next" class="btn btn-primary px-7 py-3 text-base font-extrabold shadow-cta">Next question &rarr;</button>`;

    content.innerHTML = `
    <div class="flex items-center justify-between border-b border-slate-100 pb-3">
      <span class="num text-base font-extrabold text-slate-500">Question ${mcqIndex + 1} of ${questions.length}</span>
      <span class="rounded-full bg-blue-50 px-3 py-1 text-xs font-extrabold text-[#2447F5] border border-blue-200">Multiple Choice</span>
    </div>
    <p class="mt-5 whitespace-pre-line text-xl sm:text-2xl font-bold leading-relaxed text-[#0F172A]">${escapeHtml(question.text)}</p>
    ${question.image_url ? `<img src="${escapeHtml(question.image_url)}" alt="Image for question ${mcqIndex + 1}" draggable="false" class="mt-5 block max-h-96 max-w-full rounded-xl border border-slate-200 bg-white select-none">` : ""}
    <div class="mt-8 space-y-3.5">${options}</div>
    <div class="mt-10 flex items-center justify-between gap-4 pt-5 border-t border-slate-100">
      <button type="button" id="prev" class="btn btn-secondary px-6 py-2.5 text-base font-bold" ${mcqIndex === 0 ? "disabled" : ""}>&larr; Previous question</button>
      ${actionButtonHtml}
    </div>
    <nav class="mt-10 border-t border-slate-200 pt-6" aria-label="Questions">
      <p class="mb-3 text-xs font-extrabold text-slate-400 uppercase tracking-wider">Question Navigator</p>
      <div class="flex flex-wrap gap-2.5">${nav}</div>
    </nav>`;

  content.querySelectorAll(`input[name="mcq-${question.id}"]`).forEach((input) => {
    input.addEventListener("change", () => {
      const optionId = Number(input.value);
      question.selected_option_id = optionId;
      enqueue({
        key: `answer:${question.id}`,
        method: "PUT",
        path: "answers/",
        body: { question_id: question.id, option_id: optionId },
      });
      showStatus("Answer recorded.", "ok");
    });
  });
  $("prev").addEventListener("click", () => moveMcq(mcqIndex - 1));
  const nextBtn = $("next");
  if (nextBtn) {
    nextBtn.addEventListener("click", () => moveMcq(mcqIndex + 1));
  }
  const submitBtn = $("submit-section-mcq");
  if (submitBtn) {
    submitBtn.addEventListener("click", submitSection);
  }
  content.querySelectorAll("nav button").forEach((button) => {
    button.addEventListener("click", () => moveMcq(Number(button.dataset.idx)));
  });

  openVisit("mcq", question.id);
}

let isNavigatingMcq = false;
function moveMcq(index) {
  const total = (state.questions || []).length;
  if (index < 0 || index >= total || isNavigatingMcq) return;
  isNavigatingMcq = true;
  mcqIndex = index;
  lastRenderKey = "";
  render();
  setTimeout(() => { isNavigatingMcq = false; }, 150);
}

// ---------------------------------------------------------------- coding

function loadMonaco() {
  if (monacoPromise) return monacoPromise;
  const base = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs";

  // Configure Monaco web worker proxy using blob data-URI to prevent cross-origin Worker security restrictions
  if (!window.MonacoEnvironment) {
    window.MonacoEnvironment = {
      getWorkerUrl: function (workerId, label) {
        return `data:text/javascript;charset=utf-8,${encodeURIComponent(`
          self.MonacoEnvironment = {
            baseUrl: 'https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/'
          };
          importScripts('https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs/base/worker/workerMain.js');
        `)}`;
      }
    };
  }

  monacoPromise = new Promise((resolve, reject) => {
    if (window.monaco && window.monaco.editor) {
      return resolve(window.monaco);
    }
    const timeout = setTimeout(() => {
      console.warn("Monaco editor load timed out after 3.5s, falling back to textarea editor");
      reject(new Error("Monaco load timeout"));
    }, 3500);

    const script = document.createElement("script");
    script.src = `${base}/loader.js`;
    script.onload = () => {
      try {
        window.require.config({ paths: { vs: base } });
        window.require(
          ["vs/editor/editor.main"],
          () => {
            clearTimeout(timeout);
            resolve(window.monaco);
          },
          (err) => {
            clearTimeout(timeout);
            console.warn("Monaco AMD require error, falling back to textarea:", err);
            reject(err);
          }
        );
      } catch (err) {
        clearTimeout(timeout);
        console.warn("Monaco configuration error, falling back to textarea:", err);
        reject(err);
      }
    };
    script.onerror = (err) => {
      clearTimeout(timeout);
      console.warn("Monaco loader.js failed to load from CDN, falling back to textarea:", err);
      reject(err);
    };
    document.head.appendChild(script);
  });
  return monacoPromise;
}

const MONACO_LANG_MAP = {
  python: "python",
  javascript: "javascript",
  java: "java",
  c: "c",
  cpp: "cpp",
  "c++": "cpp",
};

function monacoLanguageId(language) {
  return MONACO_LANG_MAP[(language || "").toLowerCase()] || "plaintext";
}

function formatLanguageName(language) {
  const map = {
    python: "Python",
    java: "Java",
    c: "C",
    cpp: "C++",
    "c++": "C++",
    javascript: "JavaScript",
  };
  return map[(language || "").toLowerCase()] || language;
}

let currentEditorTheme = (function() {
  try {
    return localStorage.getItem("editor-theme") || "light";
  } catch (_) {
    return "light";
  }
})();

function applyEditorTheme(theme) {
  currentEditorTheme = theme;
  try {
    localStorage.setItem("editor-theme", theme);
  } catch (_) {}

  // Update root container theme classes
  const attemptRoot = $("attempt-root");
  if (attemptRoot) {
    attemptRoot.classList.toggle("theme-light", theme === "light");
    attemptRoot.classList.toggle("theme-dark", theme === "dark");
  }
  const ideLayout = document.querySelector(".coding-ide-layout");
  if (ideLayout) {
    ideLayout.classList.toggle("theme-light", theme === "light");
    ideLayout.classList.toggle("theme-dark", theme === "dark");
  }
  if (attemptRoot && attemptRoot.classList.contains("is-coding-page")) {
    document.body.classList.toggle("theme-light", theme === "light");
    document.body.classList.toggle("theme-dark", theme === "dark");
    document.body.style.backgroundColor = theme === "dark" ? "#020617" : "#f4f6fb";
  }

  // Update theme toggle button in toolbar if present
  const toggleBtn = $("btn-theme-toggle");
  const toggleIcon = $("theme-toggle-icon");
  const toggleText = $("theme-toggle-text");
  if (toggleBtn && toggleIcon && toggleText) {
    if (theme === "light") {
      toggleBtn.className = "flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-bold border bg-slate-100 border-slate-300 text-slate-800 hover:bg-slate-200 transition-colors cursor-pointer select-none";
      toggleIcon.textContent = "🌙";
      toggleText.textContent = "Dark";
    } else {
      toggleBtn.className = "flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-bold border bg-slate-800 border-slate-700 text-slate-200 hover:bg-slate-700 transition-colors cursor-pointer select-none";
      toggleIcon.textContent = "☀️";
      toggleText.textContent = "Light";
    }
  }

  // Update Monaco theme globally if loaded
  if (window.monaco && window.monaco.editor && typeof window.monaco.editor.setTheme === "function") {
    window.monaco.editor.setTheme(theme === "light" ? "vs" : "vs-dark");
  }

  // Update editor slot container background
  const slot = $("editor-slot");
  if (slot) {
    slot.classList.toggle("theme-light", theme === "light");
    slot.classList.toggle("theme-dark", theme !== "light");
    slot.style.setProperty("background", theme === "light" ? "#ffffff" : "#1e1e1e", "important");
  }

  // Update fallback textarea and gutter if active
  const fallbackArea = document.querySelector(".fallback-textarea");
  const fallbackGutter = document.querySelector(".fallback-gutter");
  if (fallbackArea) {
    fallbackArea.style.background = theme === "light" ? "#ffffff" : "#1e1e1e";
    fallbackArea.style.color = theme === "light" ? "#0f172a" : "#d4d4d4";
  }
  if (fallbackGutter) {
    fallbackGutter.style.background = theme === "light" ? "#f8fafc" : "#181818";
    fallbackGutter.style.color = theme === "light" ? "#94a3b8" : "#6e7681";
    fallbackGutter.style.borderColor = theme === "light" ? "#e2e8f0" : "#334155";
  }
}

async function makeEditor(problem, host) {
  let currentLanguage = problem.draft_language || (problem.languages && problem.languages[0]) || "python";
  let timer = null;
  const onChange = (code, language) => {
    enqueue({
      key: `draft:${problem.id}`,
      method: "PUT",
      path: "drafts/",
      body: { problem_id: problem.id, language, code },
    });
  };

  try {
    const monaco = await loadMonaco();
    host.innerHTML = "";
    const isLocked = Boolean(problem.locked);
    const editor = monaco.editor.create(host, {
      value: problem.draft_code || "",
      language: monacoLanguageId(currentLanguage),
      theme: currentEditorTheme === "light" ? "vs" : "vs-dark",
      minimap: { enabled: false },
      automaticLayout: true,
      fontSize: 14,
      lineNumbers: "on",
      lineNumbersMinChars: 3,
      lineDecorationsWidth: 10,
      scrollBeyondLastLine: false,
      tabSize: 4,
      insertSpaces: true,
      detectIndentation: false,
      autoIndent: "full",
      formatOnType: true,
      formatOnPaste: true,
      autoClosingBrackets: "always",
      autoClosingQuotes: "always",
      autoClosingOvertype: "always",
      matchBrackets: "always",
      bracketPairColorization: { enabled: true },
      guides: {
        indentation: true,
        bracketPairs: true,
        highlightActiveIndentation: true,
      },
      folding: true,
      showFoldingControls: "always",
      readOnly: isLocked,
      domReadOnly: false,
      cursorBlinking: "smooth",
      cursorSmoothCaretAnimation: "on",
      renderLineHighlight: "all",
      lineHeight: 22,
      fontFamily: "'Fira Code', 'Cascadia Code', Consolas, 'Courier New', monospace",
      padding: { top: 12, bottom: 12 },
    });

    const sendDraft = () => {
      timer = null;
      onChange(editor.getValue(), currentLanguage);
      const pill = $("editor-autosave-indicator");
      if (pill) {
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span><span>Saved</span>`;
      }
    };

    editor.onDidChangeModelContent(() => {
      clearTimeout(timer);
      const pill = $("editor-autosave-indicator");
      if (pill) {
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span><span>Saving…</span>`;
      }
      timer = setTimeout(sendDraft, 1200);
    });

    // Proctoring: Track copy, cut and paste within the code editor container
    ["copy", "cut", "paste"].forEach((type) => {
      host.addEventListener(type, (event) => {
        if (!state || isFinal(state)) return;
        const text = type === "paste" && event.clipboardData ? event.clipboardData.getData("text") : "";
        sendEvent("clipboard", 0, { action: type, chars: text.length });
      }, true);
    });

    return {
      getValue: () => editor.getValue(),
      getLanguage: () => currentLanguage,
      setReadOnly: (on) => editor.updateOptions({ readOnly: on }),
      setLanguage: (lang) => {
        currentLanguage = lang;
        monaco.editor.setModelLanguage(editor.getModel(), monacoLanguageId(lang));
        clearTimeout(timer);
        sendDraft();
      },
      layout: () => {
        try {
          editor.layout();
        } catch (_) {}
      },
      focus: () => {
        try {
          editor.focus();
        } catch (_) {}
      },
      flush: () => {
        if (timer) {
          clearTimeout(timer);
          sendDraft();
        }
      },
      rawEditor: editor,
    };
  } catch (err) {
    console.warn("Monaco editor fallback engaged:", err);
    host.innerHTML = "";
    const isLocked = Boolean(problem.locked);
    
    // Fallback Editor with Line Numbers Gutter, Auto-Indentation & Bracket Closing
    const wrapper = document.createElement("div");
    wrapper.className = "fallback-editor-wrapper flex w-full h-full relative overflow-hidden";
    wrapper.style.cssText = "position: absolute; inset: 0; width: 100%; height: 100%; z-index: 20; display: flex; overflow: hidden;";

    const isLight = currentEditorTheme === "light";
    const bg = isLight ? "#ffffff" : "#1e1e1e";
    const fg = isLight ? "#0f172a" : "#d4d4d4";
    const gutterBg = isLight ? "#f8fafc" : "#181818";
    const gutterFg = isLight ? "#94a3b8" : "#6e7681";
    const gutterBorder = isLight ? "#e2e8f0" : "#334155";

    // Line number gutter
    const gutter = document.createElement("div");
    gutter.className = "fallback-gutter select-none text-right font-mono text-xs overflow-hidden shrink-0";
    gutter.style.cssText = `width: 48px; background: ${gutterBg}; color: ${gutterFg}; border-right: 1px solid ${gutterBorder}; padding: 14px 8px 14px 4px; line-height: 22px; font-family: 'Fira Code', Consolas, monospace; user-select: none; pointer-events: none;`;

    // Code input textarea
    const area = document.createElement("textarea");
    area.className = "fallback-textarea flex-1 h-full p-3.5 font-mono text-sm border-0 outline-none resize-none";
    area.style.cssText = `background: ${bg}; color: ${fg}; line-height: 22px; font-family: 'Fira Code', Consolas, monospace; padding: 14px; outline: none; border: none; resize: none; pointer-events: auto; cursor: text; white-space: pre; overflow-wrap: normal; overflow-x: auto; tab-size: 4;`;
    area.spellcheck = false;
    area.setAttribute("aria-label", "Code editor");
    area.value = problem.draft_code || "";
    if (isLocked) area.readOnly = true;

    // Helper: update line numbers in gutter
    const updateGutter = () => {
      const lineCount = (area.value.match(/\n/g) || []).length + 1;
      let lines = "";
      for (let i = 1; i <= lineCount; i++) {
        lines += `<div>${i}</div>`;
      }
      gutter.innerHTML = lines;
      gutter.scrollTop = area.scrollTop;
    };

    area.addEventListener("scroll", () => {
      gutter.scrollTop = area.scrollTop;
    });

    // Key handlers for Indentation & Bracket Auto-closing
    area.addEventListener("keydown", (e) => {
      if (isLocked) return;

      // 1. Tab / Shift+Tab handling
      if (e.key === "Tab") {
        e.preventDefault();
        const start = area.selectionStart;
        const end = area.selectionEnd;
        const val = area.value;

        if (e.shiftKey) {
          // Unindent line(s)
          const lineStart = val.lastIndexOf("\n", start - 1) + 1;
          const lineEnd = val.indexOf("\n", end);
          const block = val.substring(lineStart, lineEnd === -1 ? val.length : lineEnd);
          const unindented = block
            .split("\n")
            .map((l) => (l.startsWith("    ") ? l.slice(4) : l.startsWith("\t") ? l.slice(1) : l.replace(/^ {1,3}/, "")))
            .join("\n");
          area.value = val.substring(0, lineStart) + unindented + (lineEnd === -1 ? "" : val.substring(lineEnd));
          area.selectionStart = Math.max(lineStart, start - 4);
          area.selectionEnd = Math.max(lineStart, end - (block.length - unindented.length));
        } else {
          if (start !== end && val.substring(start, end).includes("\n")) {
            // Multi-line indent
            const lineStart = val.lastIndexOf("\n", start - 1) + 1;
            const lineEnd = val.indexOf("\n", end);
            const block = val.substring(lineStart, lineEnd === -1 ? val.length : lineEnd);
            const indented = block
              .split("\n")
              .map((l) => "    " + l)
              .join("\n");
            area.value = val.substring(0, lineStart) + indented + (lineEnd === -1 ? "" : val.substring(lineEnd));
            area.selectionStart = start + 4;
            area.selectionEnd = end + (indented.length - block.length);
          } else {
            // Single cursor insert 4 spaces
            area.value = val.substring(0, start) + "    " + val.substring(end);
            area.selectionStart = area.selectionEnd = start + 4;
          }
        }
        updateGutter();
        area.dispatchEvent(new Event("input"));
        return;
      }

      // 2. Enter key: Auto-indentation helper
      if (e.key === "Enter") {
        e.preventDefault();
        const start = area.selectionStart;
        const end = area.selectionEnd;
        const val = area.value;

        // Find current line text up to cursor
        const lineStart = val.lastIndexOf("\n", start - 1) + 1;
        const currentLineToCursor = val.substring(lineStart, start);
        const indentMatch = currentLineToCursor.match(/^(\s*)/);
        let baseIndent = indentMatch ? indentMatch[1] : "";

        // Check if line ends with colon or opening brace
        const trimmed = currentLineToCursor.trim();
        let extraIndent = "";
        if (trimmed.endsWith(":") || trimmed.endsWith("{") || trimmed.endsWith("(") || trimmed.endsWith("[")) {
          extraIndent = "    ";
        }

        const insertText = "\n" + baseIndent + extraIndent;
        area.value = val.substring(0, start) + insertText + val.substring(end);
        area.selectionStart = area.selectionEnd = start + insertText.length;
        updateGutter();
        area.dispatchEvent(new Event("input"));
        return;
      }

      // 3. Auto-closing brackets and quotes
      const pairs = {
        "(": ")",
        "[": "]",
        "{": "}",
        '"': '"',
        "'": "'",
        "`": "`",
      };

      if (pairs[e.key]) {
        const start = area.selectionStart;
        const end = area.selectionEnd;
        const val = area.value;
        const nextChar = val.charAt(start);

        // If typing closing quote/tick and already right in front of it, step over
        if ((e.key === '"' || e.key === "'" || e.key === "`") && start === end && nextChar === e.key) {
          e.preventDefault();
          area.selectionStart = area.selectionEnd = start + 1;
          return;
        }

        e.preventDefault();
        const openChar = e.key;
        const closeChar = pairs[e.key];
        const selected = val.substring(start, end);
        area.value = val.substring(0, start) + openChar + selected + closeChar + val.substring(end);
        area.selectionStart = start + 1;
        area.selectionEnd = end + 1;
        updateGutter();
        area.dispatchEvent(new Event("input"));
        return;
      }

      // 4. Step over closing brackets: ), ], }
      const closers = [")", "]", "}"];
      if (closers.includes(e.key)) {
        const start = area.selectionStart;
        const end = area.selectionEnd;
        if (start === end && area.value.charAt(start) === e.key) {
          e.preventDefault();
          area.selectionStart = area.selectionEnd = start + 1;
          return;
        }
      }

      // 5. Backspace between empty pairs () -> delete both
      if (e.key === "Backspace") {
        const start = area.selectionStart;
        const end = area.selectionEnd;
        if (start === end && start > 0) {
          const prev = area.value.charAt(start - 1);
          const next = area.value.charAt(start);
          if (
            (prev === "(" && next === ")") ||
            (prev === "[" && next === "]") ||
            (prev === "{" && next === "}") ||
            (prev === '"' && next === '"') ||
            (prev === "'" && next === "'")
          ) {
            e.preventDefault();
            area.value = area.value.substring(0, start - 1) + area.value.substring(start + 1);
            area.selectionStart = area.selectionEnd = start - 1;
            updateGutter();
            area.dispatchEvent(new Event("input"));
            return;
          }
        }
      }
    });

    wrapper.appendChild(gutter);
    wrapper.appendChild(area);
    host.appendChild(wrapper);
    updateGutter();

    const sendDraft = () => {
      timer = null;
      onChange(area.value, currentLanguage);
      const pill = $("editor-autosave-indicator");
      if (pill) {
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-emerald-400"></span><span>Saved</span>`;
      }
    };

    area.addEventListener("input", () => {
      clearTimeout(timer);
      updateGutter();
      const pill = $("editor-autosave-indicator");
      if (pill) {
        pill.innerHTML = `<span class="w-1.5 h-1.5 rounded-full bg-amber-400 animate-pulse"></span><span>Saving…</span>`;
      }
      timer = setTimeout(sendDraft, 1200);
    });

    return {
      getValue: () => area.value,
      getLanguage: () => currentLanguage,
      setReadOnly: (on) => {
        area.readOnly = on;
      },
      setLanguage: (lang) => {
        currentLanguage = lang;
        clearTimeout(timer);
        sendDraft();
      },
      layout: () => {
        updateGutter();
      },
      focus: () => {
        try {
          area.focus();
        } catch (_) {}
      },
      flush: () => {
        if (timer) {
          clearTimeout(timer);
          sendDraft();
        }
      },
      rawEditor: area,
    };
  }
}

function hostFor(problem) {
  if (!editorHosts[problem.id]) {
    const host = document.createElement("div");
    host.className = "w-full h-full overflow-hidden";
    host.style.cssText = "position: absolute; inset: 0; width: 100%; height: 100%; min-height: 0; z-index: 1;";
    editorHosts[problem.id] = host;
  }
  return editorHosts[problem.id];
}

// ---------------------------------------------------------------- in-app modal system (ensures full screen is NEVER dropped)

let inAppModalResolver = null;

function showInAppConfirm({
  title = "Confirmation Required",
  message = "Please confirm your action.",
  details = "",
  confirmText = "Confirm",
  cancelText = "Cancel",
  tone = "primary",
}) {
  const modalRoot = $("in-app-modal-root");
  if (!modalRoot) {
    return Promise.resolve(window.confirm(`${title}\n\n${message}`));
  }

  // Teleport modalRoot to document.body so it sits at the absolute top of the DOM stacking context,
  // completely above Monaco editor, sidebars, textareas, or headers.
  if (modalRoot.parentElement !== document.body) {
    document.body.appendChild(modalRoot);
  }
  modalRoot.style.zIndex = "2147483647";

  const titleEl = $("in-app-modal-title");
  const msgEl = $("in-app-modal-message");
  const detailsEl = $("in-app-modal-details");
  const iconEl = $("in-app-modal-icon");
  const iconWrap = $("in-app-modal-icon-wrap");
  const btnConfirm = $("in-app-modal-confirm");
  const btnCancel = $("in-app-modal-cancel");

  if (titleEl) titleEl.textContent = title;
  if (msgEl) msgEl.textContent = message;

  if (detailsEl) {
    if (details) {
      detailsEl.innerHTML = escapeHtml(details);
      detailsEl.classList.remove("hidden");
    } else {
      detailsEl.classList.add("hidden");
    }
  }

  if (iconEl && iconWrap) {
    if (tone === "danger" || tone === "bad") {
      iconEl.textContent = "⚠️";
      iconWrap.className = "w-12 h-12 rounded-2xl bg-rose-50 text-rose-600 flex items-center justify-center shrink-0 text-2xl font-bold shadow-xs";
    } else if (tone === "warn") {
      iconEl.textContent = "⚠️";
      iconWrap.className = "w-12 h-12 rounded-2xl bg-amber-50 text-amber-600 flex items-center justify-center shrink-0 text-2xl font-bold shadow-xs";
    } else {
      iconEl.textContent = "🚀";
      iconWrap.className = "w-12 h-12 rounded-2xl bg-blue-50 text-[#2447F5] flex items-center justify-center shrink-0 text-2xl font-bold shadow-xs";
    }
  }

  if (btnConfirm) {
    btnConfirm.textContent = confirmText;
    if (tone === "danger" || tone === "bad") {
      btnConfirm.className = "btn bg-rose-600 hover:bg-rose-700 text-white text-xs sm:text-sm font-extrabold py-2.5 px-6 shadow-cta";
    } else {
      btnConfirm.className = "btn btn-primary text-xs sm:text-sm font-extrabold py-2.5 px-6 shadow-cta";
    }
  }

  if (btnCancel) {
    btnCancel.textContent = cancelText;
  }

  modalRoot.classList.remove("hidden");

  return new Promise((resolve) => {
    inAppModalResolver = resolve;

    const cleanup = (result) => {
      modalRoot.classList.add("hidden");
      btnConfirm.removeEventListener("click", onConfirm);
      btnCancel.removeEventListener("click", onCancel);
      modalRoot.removeEventListener("click", onBackdrop);
      document.removeEventListener("keydown", onKeyDown);
      inAppModalResolver = null;
      resolve(result);
    };

    const onConfirm = () => cleanup(true);
    const onCancel = () => cleanup(false);
    const onBackdrop = (e) => {
      if (e.target === modalRoot) cleanup(false);
    };
    const onKeyDown = (e) => {
      if (e.key === "Escape") {
        e.preventDefault();
        cleanup(false);
      }
    };

    btnConfirm.addEventListener("click", onConfirm);
    btnCancel.addEventListener("click", onCancel);
    modalRoot.addEventListener("click", onBackdrop);
    document.addEventListener("keydown", onKeyDown);
  });
}

// ---------------------------------------------------------------- Dual-Pane HackerRank/LeetCode Coding IDE

let activeConsoleTab = "cases";
let activeProblemSubTab = "description";
let consoleExpanded = true;

function renderCoding() {
  const problems = state.problems || [];
  if (!problems.length) {
    content.innerHTML = '<div class="p-8 text-center text-slate-400">No coding problems in this section.</div>';
    return;
  }
  if (!problems.find((p) => p.id === activeProblemId)) activeProblemId = problems[0].id;
  const problem = problems.find((p) => p.id === activeProblemId);
  const locked = Boolean(problem.locked);

  $("attempt-root")?.classList.add("is-coding-page");
  content.classList.add("is-coding-view");
  content.removeAttribute("inert");
  const layoutRoot = document.querySelector(".attempt-layout");
  if (layoutRoot) layoutRoot.classList.add("is-coding");

  const headerSubmitBtn = $("header-submit-section");
  if (headerSubmitBtn) {
    headerSubmitBtn.classList.remove("hidden");
    headerSubmitBtn.onclick = submitSection;
  }

  const problemTabs = problems
    .map((p, i) => {
      const isCurrent = p.id === problem.id;
      return `<button type="button" data-id="${p.id}" class="problem-tab-btn flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-all cursor-pointer ${
        isCurrent ? "active" : ""
      }">
        <span>Problem ${i + 1}</span>
        ${p.locked ? '<span class="text-emerald-500 font-black">✓</span>' : ""}
      </button>`;
    })
    .join("");

  const examplesHtml = problem.public_cases
    .map((c, i) => `
      <div class="example-card rounded-xl p-3.5 space-y-2.5">
        <div class="flex items-center justify-between text-xs font-extrabold">
          <span>Example ${i + 1}</span>
          <button type="button" data-copy-input="${escapeHtml(c.input)}" class="btn-copy-input text-[11px] font-bold text-blue-600 hover:underline flex items-center gap-1 cursor-pointer">
            <svg class="w-3.5 h-3.5" fill="none" stroke="currentColor" viewBox="0 0 24 24"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M8 16H6a2 2 0 01-2-2V6a2 2 0 012-2h8a2 2 0 012 2v2m-6 12h8a2 2 0 002-2v-8a2 2 0 00-2-2h-8a2 2 0 00-2 2v8a2 2 0 002 2z"/></svg>
            <span>Copy Input</span>
          </button>
        </div>
        <div class="space-y-1.5">
          <div class="text-[10px] font-bold uppercase tracking-wider spec-box-label">Input</div>
          <pre class="p-2.5 font-mono text-xs rounded-lg overflow-x-auto whitespace-pre-wrap">${escapeHtml(c.input)}</pre>
        </div>
        <div class="space-y-1.5">
          <div class="text-[10px] font-bold uppercase tracking-wider spec-box-label">Expected Output</div>
          <pre class="p-2.5 font-mono text-xs text-emerald-600 rounded-lg overflow-x-auto whitespace-pre-wrap">${escapeHtml(c.expected)}</pre>
        </div>
      </div>
    `)
    .join("");

  const languages = (problem.languages || ["python", "javascript", "java", "cpp", "c"])
    .map((lang) => {
      const activeLang = problem.draft_language || (problem.languages && problem.languages[0]) || "python";
      const isSelected = lang.toLowerCase() === (activeLang || "").toLowerCase();
      return `<option value="${escapeHtml(lang)}" ${isSelected ? "selected" : ""}>${escapeHtml(formatLanguageName(lang))}</option>`;
    })
    .join("");

  const consoleCasesTabs = problem.public_cases
    .map((c, i) => `
      <button type="button" data-case-idx="${i}" class="case-chip-btn px-2.5 py-1 rounded-lg text-[11px] font-extrabold cursor-pointer ${i === 0 ? 'active' : ''}">
        Case ${i + 1}
      </button>
    `)
    .join("");

  const firstCase = problem.public_cases[0] || { input: "", expected: "" };

  content.innerHTML = `
    <!-- Top-level Coding IDE Container (HackerRank Edge-to-Edge Full-Page Layout) -->
    <div class="coding-ide-layout flex flex-col lg:flex-row w-full h-full overflow-hidden">

      <!-- LEFT PANE: Problem Description, Spec, Examples, Submissions (44% Width) -->
      <section class="problem-pane flex flex-col overflow-hidden" aria-label="Problem Description">
        
        <!-- Multi-problem selector tabs -->
        <div class="problem-pane-header flex items-center gap-2 px-3 py-2 overflow-x-auto shrink-0 select-none">
          <span class="text-[10px] font-extrabold uppercase tracking-wider opacity-70 px-1">Problems:</span>
          ${problemTabs}
        </div>

        <!-- Problem Title & Badges Bar -->
        <div class="problem-title-bar px-5 pt-3 pb-2.5 shrink-0">
          <div class="flex items-center justify-between gap-3">
            <h2 class="text-base sm:text-lg font-extrabold tracking-tight truncate">${escapeHtml(problem.title)}</h2>
            <div class="flex items-center gap-1.5 shrink-0">
              <span class="px-2.5 py-0.5 rounded-full text-[11px] font-extrabold bg-blue-500/15 text-blue-500 border border-blue-500/30">
                ${problem.marks || 10} pts
              </span>
              ${locked ? '<span class="px-2.5 py-0.5 rounded-full text-[11px] font-extrabold bg-emerald-500/15 text-emerald-500 border border-emerald-500/30 flex items-center gap-1">✓ Submitted</span>' : '<span class="px-2.5 py-0.5 rounded-full text-[11px] font-extrabold opacity-70 border border-current">In Progress</span>'}
            </div>
          </div>

          <!-- Problem Navigation Sub-tabs -->
          <div class="flex items-center gap-2 mt-2.5 text-xs font-bold -mb-2.5 pb-2 select-none">
            <button type="button" id="tab-btn-desc" class="subtab-btn ${activeProblemSubTab === 'description' ? 'active' : ''}">Description</button>
            <button type="button" id="tab-btn-examples" class="subtab-btn ${activeProblemSubTab === 'examples' ? 'active' : ''}">Examples (${problem.public_cases.length})</button>
            <button type="button" id="tab-btn-subs" class="subtab-btn ${activeProblemSubTab === 'submissions' ? 'active' : ''}">Submissions</button>
          </div>
        </div>

        <!-- Left Pane Scrollable Body -->
        <div class="problem-body flex-1 overflow-y-auto p-5 space-y-4 custom-scrollbar text-sm leading-relaxed">
          
          <!-- Subtab 1: Description -->
          <div id="subtab-content-desc" class="${activeProblemSubTab === 'description' ? '' : 'hidden'} space-y-4">
            <div class="whitespace-pre-line font-medium leading-relaxed text-sm">${escapeHtml(problem.statement)}</div>

            ${problem.input_spec ? `
              <div class="spec-box p-3.5 rounded-xl space-y-1">
                <span class="spec-box-label text-xs font-extrabold uppercase tracking-wider">Input Format</span>
                <p class="whitespace-pre-line text-xs sm:text-sm font-medium">${escapeHtml(problem.input_spec)}</p>
              </div>
            ` : ""}

            ${problem.output_spec ? `
              <div class="spec-box p-3.5 rounded-xl space-y-1">
                <span class="spec-box-label text-xs font-extrabold uppercase tracking-wider">Output Format</span>
                <p class="whitespace-pre-line text-xs sm:text-sm font-medium">${escapeHtml(problem.output_spec)}</p>
              </div>
            ` : ""}

            ${(problem.example_input || problem.example_output) ? `
              <div class="example-card rounded-xl p-3.5 space-y-2.5">
                <span class="spec-box-label text-xs font-extrabold uppercase tracking-wider">Example</span>
                <div class="space-y-1.5">
                  <div class="text-[10px] font-bold uppercase tracking-wider spec-box-label">Input</div>
                  <pre class="p-2.5 font-mono text-xs rounded-lg overflow-x-auto whitespace-pre-wrap">${escapeHtml(problem.example_input)}</pre>
                </div>
                <div class="space-y-1.5">
                  <div class="text-[10px] font-bold uppercase tracking-wider spec-box-label">Output</div>
                  <pre class="p-2.5 font-mono text-xs text-emerald-600 rounded-lg overflow-x-auto whitespace-pre-wrap">${escapeHtml(problem.example_output)}</pre>
                </div>
                ${problem.example_explanation ? `
                  <div class="space-y-1.5">
                    <div class="text-[10px] font-bold uppercase tracking-wider spec-box-label">Explanation</div>
                    <p class="whitespace-pre-line text-xs sm:text-sm font-medium">${escapeHtml(problem.example_explanation)}</p>
                  </div>
                ` : ""}
              </div>
            ` : ""}

            ${problem.constraints ? `
              <div class="constraints-box p-3.5 rounded-xl space-y-1">
                <span class="text-xs font-extrabold uppercase tracking-wider flex items-center gap-1 font-bold">
                  <span>⚡</span> Constraints
                </span>
                <pre class="whitespace-pre-line font-mono text-xs overflow-x-auto">${escapeHtml(problem.constraints)}</pre>
              </div>
            ` : ""}
          </div>

          <!-- Subtab 2: Examples -->
          <div id="subtab-content-examples" class="${activeProblemSubTab === 'examples' ? '' : 'hidden'} space-y-3.5">
            ${examplesHtml}
          </div>

          <!-- Subtab 3: Submissions -->
          <div id="subtab-content-subs" class="${activeProblemSubTab === 'submissions' ? '' : 'hidden'} space-y-3">
            ${locked ? `
              <div class="p-4 rounded-xl bg-emerald-500/10 border border-emerald-500/40 text-emerald-600 dark:text-emerald-300 space-y-1.5">
                <div class="flex items-center gap-2 font-extrabold text-sm text-emerald-600 dark:text-emerald-200">
                  <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
                  <span>Final Solution Submitted &amp; Locked</span>
                </div>
                <p class="text-xs font-medium">Submitted language: <strong>${escapeHtml(formatLanguageName(problem.latest_submission?.language || problem.draft_language))}</strong></p>
                <p class="text-[11px] font-mono opacity-80">${escapeHtml(problem.latest_submission?.submitted_at || 'Just now')}</p>
              </div>
            ` : `
              <div class="p-8 text-center opacity-70 space-y-2">
                <p class="font-extrabold text-sm">No Final Submission Yet</p>
                <p class="text-xs font-medium max-w-xs mx-auto">When your code passes sample test cases, click <strong>"Submit final answer"</strong> on the right pane to record and lock your solution.</p>
              </div>
            `}
          </div>

        </div>

      </section>

      <!-- RIGHT PANE: Monaco Editor + Interactive Console Drawer (56% Width) -->
      <section class="editor-pane flex-1 flex flex-col overflow-hidden" aria-label="Code Editor & Console">
        
        <!-- Editor Header Toolbar -->
        <div class="editor-toolbar flex flex-wrap items-center justify-between gap-3 px-4 py-2 shrink-0">
          <div class="flex items-center gap-3">
            <label class="flex items-center gap-2 text-xs font-bold">
              <span class="uppercase tracking-wider text-[10px] opacity-70">Language:</span>
              <select id="lang" class="rounded-lg px-2.5 py-1 text-xs font-bold focus:outline-none focus:border-blue-500 cursor-pointer" ${locked ? "disabled" : ""}>
                ${languages}
              </select>
            </label>
            <span id="editor-autosave-indicator" class="flex items-center gap-1.5 text-[11px] opacity-70 font-mono select-none">
              <span class="w-1.5 h-1.5 rounded-full ${locked ? 'bg-emerald-500' : 'bg-emerald-500'}"></span>
              <span>${locked ? 'Locked' : 'Saved'}</span>
            </span>
          </div>

          <div class="flex items-center gap-2.5">
            <!-- Theme Toggle Button (Light / Dark Mode) -->
            <button type="button" id="btn-theme-toggle" class="flex items-center gap-1.5 px-2.5 py-1 rounded-lg text-xs font-bold border transition-colors cursor-pointer select-none" title="Toggle Light/Dark Theme">
              <span id="theme-toggle-icon">${currentEditorTheme === 'light' ? '🌙' : '☀️'}</span>
              <span id="theme-toggle-text">${currentEditorTheme === 'light' ? 'Dark' : 'Light'}</span>
            </button>

            <button type="button" class="btn btn-primary px-3.5 py-1 text-xs font-black btn-submit-section shadow-cta flex items-center gap-1.5 cursor-pointer" title="Submit coding section and advance">
              <span>Submit Section</span>
              <span>&rarr;</span>
            </button>
          </div>
        </div>

        <!-- Locked Solution Banner (Unmissable visual indicator) -->
        ${locked ? `
          <div id="editor-locked-banner" class="flex items-center justify-between px-4 py-2 bg-emerald-500/10 border-b border-emerald-500/30 text-emerald-700 dark:text-emerald-300 text-xs font-bold shrink-0 select-none">
            <div class="flex items-center gap-2">
              <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
              <span>Final Solution Submitted &amp; Locked &bull; Read-Only</span>
            </div>
            <span class="text-[11px] opacity-75 font-mono">Code is permanently recorded</span>
          </div>
        ` : ""}

        <!-- Monaco Editor Slot -->
        <div id="editor-slot" class="flex-1 w-full overflow-hidden relative cursor-text"></div>

        <!-- Bottom Collapsible Console Drawer -->
        <div id="console-drawer" class="flex flex-col shrink-0 transition-all duration-200">
          
          <!-- Console Drawer Top Bar: Tabs & Action Buttons -->
          <div class="console-topbar flex items-center justify-between px-3 sm:px-4 py-2 select-none">
            <div class="flex items-center gap-2">
              <button type="button" id="console-tab-cases" class="console-tab-btn px-2.5 py-1 rounded-lg text-xs font-bold transition-colors cursor-pointer ${activeConsoleTab === 'cases' ? 'active' : ''}">
                Sample Cases
              </button>
              <button type="button" id="console-tab-result" class="console-tab-btn px-2.5 py-1 rounded-lg text-xs font-bold transition-colors cursor-pointer ${activeConsoleTab === 'result' ? 'active' : ''}">
                Test Result
              </button>
            </div>

            <div class="flex items-center gap-2">
              <!-- Run & Submit Buttons -->
              <button type="button" id="run-code" class="btn btn-secondary py-1.5 px-4 text-xs font-extrabold flex items-center gap-1.5 cursor-pointer disabled:opacity-50" ${locked ? "disabled" : ""}>
                <span>▶ Run Code</span>
              </button>

              <button type="button" id="submit-code" class="btn btn-primary py-1.5 px-5 text-xs font-black shadow-cta flex items-center gap-1.5 cursor-pointer disabled:opacity-50" ${locked ? "disabled" : ""}>
                <span>${locked ? "✓ Solution Submitted &amp; Locked" : "🚀 Submit Final Answer"}</span>
              </button>

              <!-- Collapse Toggle Button -->
              <button type="button" id="btn-toggle-console" class="p-1 rounded-lg opacity-70 hover:opacity-100 text-xs font-bold cursor-pointer" title="Toggle console height">
                ${consoleExpanded ? '⌄' : '⌃'}
              </button>
            </div>
          </div>

          <!-- Console Body Viewport -->
          <div id="console-body" class="console-body ${consoleExpanded ? 'h-40' : 'h-0'} overflow-y-auto px-4 py-3 custom-scrollbar text-xs transition-all duration-200">
            
            <!-- Tab View 1: Sample Cases -->
            <div id="console-view-cases" class="${activeConsoleTab === 'cases' ? '' : 'hidden'} space-y-2.5">
              <div class="flex items-center gap-2 mb-2">
                ${consoleCasesTabs}
              </div>
              <div id="console-active-case-detail" class="space-y-2">
                <div>
                  <div class="text-[10px] font-extrabold uppercase tracking-wider opacity-70 mb-1">Input:</div>
                  <pre class="console-pre p-2.5 rounded-lg font-mono text-xs overflow-x-auto whitespace-pre-wrap">${escapeHtml(firstCase.input)}</pre>
                </div>
                <div>
                  <div class="text-[10px] font-extrabold uppercase tracking-wider opacity-70 mb-1">Expected Output:</div>
                  <pre class="console-pre p-2.5 rounded-lg font-mono text-xs text-emerald-600 dark:text-emerald-400 overflow-x-auto whitespace-pre-wrap">${escapeHtml(firstCase.expected)}</pre>
                </div>
              </div>
            </div>

            <!-- Tab View 2: Test Result -->
            <div id="console-view-result" class="${activeConsoleTab === 'result' ? '' : 'hidden'}">
              <div id="code-result" class="text-xs opacity-80">
                Click <strong>"Run Code"</strong> to test your solution against example test cases.
              </div>
            </div>

          </div>

        </div>

      </section>

    </div>
  `;

  // Subtab switching
  const switchSubTab = (tab) => {
    activeProblemSubTab = tab;
    ["desc", "examples", "subs"].forEach((t) => {
      const btn = $(`tab-btn-${t}`);
      const pane = $(`subtab-content-${t}`);
      const isActive = (tab === "description" && t === "desc") || tab === t || (tab === "submissions" && t === "subs");
      if (btn) {
        btn.classList.toggle("active", isActive);
      }
      if (pane) pane.classList.toggle("hidden", !isActive);
    });
  };

  $("tab-btn-desc")?.addEventListener("click", () => switchSubTab("description"));
  $("tab-btn-examples")?.addEventListener("click", () => switchSubTab("examples"));
  $("tab-btn-subs")?.addEventListener("click", () => switchSubTab("submissions"));

  // Copy buttons
  content.querySelectorAll(".btn-copy-input").forEach((btn) => {
    btn.addEventListener("click", () => {
      const text = btn.dataset.copyInput;
      if (navigator.clipboard && navigator.clipboard.writeText) {
        navigator.clipboard.writeText(text).then(() => {
          const orig = btn.innerHTML;
          btn.innerHTML = `<span class="text-emerald-500 font-extrabold">✓ Copied</span>`;
          setTimeout(() => (btn.innerHTML = orig), 1500);
        }).catch(() => {});
      }
    });
  });

  // Console Tabs
  const switchConsoleTab = (tab) => {
    activeConsoleTab = tab;
    const tabCases = $("console-tab-cases");
    const tabResult = $("console-tab-result");
    const viewCases = $("console-view-cases");
    const viewResult = $("console-view-result");
    if (tabCases && tabResult) {
      tabCases.classList.toggle("active", tab === "cases");
      tabResult.classList.toggle("active", tab === "result");
    }
    if (viewCases && viewResult) {
      viewCases.classList.toggle("hidden", tab !== 'cases');
      viewResult.classList.toggle("hidden", tab !== 'result');
    }
  };

  $("console-tab-cases")?.addEventListener("click", () => switchConsoleTab("cases"));
  $("console-tab-result")?.addEventListener("click", () => switchConsoleTab("result"));

  // Console Drawer toggle
  const toggleBtn = $("btn-toggle-console");
  const consoleBody = $("console-body");
  if (toggleBtn && consoleBody) {
    toggleBtn.addEventListener("click", () => {
      consoleExpanded = !consoleExpanded;
      consoleBody.className = `console-body ${consoleExpanded ? 'h-40' : 'h-0'} overflow-y-auto px-4 py-3 custom-scrollbar text-xs transition-all duration-200`;
      toggleBtn.textContent = consoleExpanded ? '⌄' : '⌃';
      setTimeout(() => {
        if (editors[problem.id]) {
          editors[problem.id].then((editor) => {
            if (typeof editor.layout === "function") editor.layout();
          });
        }
      }, 220);
    });
  }

  // Case chips inside console
  content.querySelectorAll(".case-chip-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const idx = Number(btn.dataset.caseIdx);
      const targetCase = problem.public_cases[idx];
      if (!targetCase) return;
      content.querySelectorAll(".case-chip-btn").forEach((b) => {
        b.classList.remove("active");
      });
      btn.classList.add("active");
      const detail = $("console-active-case-detail");
      if (detail) {
        detail.innerHTML = `
          <div>
            <div class="text-[10px] font-extrabold uppercase tracking-wider opacity-70 mb-1">Input:</div>
            <pre class="console-pre p-2.5 rounded-lg font-mono text-xs overflow-x-auto whitespace-pre-wrap">${escapeHtml(targetCase.input)}</pre>
          </div>
          <div>
            <div class="text-[10px] font-extrabold uppercase tracking-wider opacity-70 mb-1">Expected Output:</div>
            <pre class="console-pre p-2.5 rounded-lg font-mono text-xs text-emerald-600 dark:text-emerald-400 overflow-x-auto whitespace-pre-wrap">${escapeHtml(targetCase.expected)}</pre>
          </div>
        `;
      }
    });
  });

  // Section submit
  content.querySelectorAll(".btn-submit-section").forEach((b) => {
    b.onclick = submitSection;
  });

  // Problem tabs
  content.querySelectorAll(".problem-tab-btn").forEach((button) => {
    button.addEventListener("click", () => {
      activeProblemId = Number(button.dataset.id);
      lastRenderKey = "";
      render();
    });
  });

  // Immediate button and dropdown wiring
  const langSelect = $("lang");
  const runButton = $("run-code");
  const submitButton = $("submit-code");

  if (langSelect) {
    langSelect.onchange = async (event) => {
      const editor = await editors[problem.id];
      if (editor) editor.setLanguage(event.target.value);
    };
  }
  if (runButton && !locked) {
    runButton.onclick = async () => {
      const editor = await editors[problem.id];
      if (editor) runCode(problem, editor);
    };
  }
  if (submitButton && !locked) {
    submitButton.onclick = async () => {
      const editor = await editors[problem.id];
      if (editor) submitCode(problem, editor);
    };
  }

  // Theme toggle wiring
  const btnTheme = $("btn-theme-toggle");
  if (btnTheme) {
    btnTheme.addEventListener("click", () => {
      const nextTheme = currentEditorTheme === "light" ? "dark" : "light";
      applyEditorTheme(nextTheme);
    });
  }

  // Ensure universal theme is applied to full page and elements
  applyEditorTheme(currentEditorTheme);

  // Mount the editor host inside #editor-slot
  const editorSlot = $("editor-slot");
  if (editorSlot) {
    editorSlot.innerHTML = "";
    editorSlot.style.background = currentEditorTheme === "light" ? "#ffffff" : "#1e1e1e";
    editorSlot.appendChild(hostFor(problem));
    editorSlot.onclick = async () => {
      if (editors[problem.id]) {
        const editor = await editors[problem.id];
        if (locked) {
          showStatus("This solution has been submitted and locked. You can no longer edit your code for this problem.", "warn");
        } else if (editor && typeof editor.focus === "function") {
          editor.focus();
        }
      }
    };
  }

  if (!editors[problem.id]) {
    editors[problem.id] = makeEditor(problem, editorHosts[problem.id]);
  }

  editors[problem.id].then((editor) => {
    if (locked) {
      editor.setReadOnly(true);
      renderSubmissionSummary(problem);
    } else {
      editor.setReadOnly(false);
    }

    // Force layout calculation and focus on multiple frames so typing is immediately available
    if (typeof editor.layout === "function") editor.layout();
    requestAnimationFrame(() => {
      if (typeof editor.layout === "function") editor.layout();
      if (!locked && typeof editor.focus === "function") {
        editor.focus();
      }
    });
    setTimeout(() => {
      if (typeof editor.layout === "function") editor.layout();
    }, 120);
    setTimeout(() => {
      if (typeof editor.layout === "function") editor.layout();
    }, 350);
  });

  renderSubmissionSummary(problem);
  openVisit("coding", problem.id);
}

function renderSubmissionSummary(problem) {
  const target = $("code-result");
  if (!target) return;
  if (!problem.locked) {
    target.innerHTML = `<span class="opacity-80">Click <strong>"Run Code"</strong> to test sample cases, or <strong>"Submit Final Answer"</strong> when ready.</span>`;
    return;
  }
  const sub = problem.latest_submission;
  if (sub && sub.status === "runner_error") {
    target.innerHTML = `<p class="text-amber-500 dark:text-amber-400 font-bold">Your final answer is recorded. It will be reviewed by the evaluator.</p>`;
    return;
  }
  target.innerHTML = `<p class="text-emerald-600 dark:text-emerald-400 font-bold">Final solution recorded${sub ? ` (${escapeHtml(sub.language)})` : ""}. Code is locked.</p>`;
}

async function runCode(problem, editor) {
  const target = $("code-result");
  const button = $("run-code");
  if (button) button.disabled = true;

  // Automatically switch console tab to Test Result and expand console
  activeConsoleTab = "result";
  consoleExpanded = true;
  const consoleBody = $("console-body");
  if (consoleBody) {
    consoleBody.className = "console-body h-40 overflow-y-auto px-4 py-3 custom-scrollbar text-xs transition-all duration-200";
  }
  const tabCases = $("console-tab-cases");
  const tabResult = $("console-tab-result");
  const viewCases = $("console-view-cases");
  const viewResult = $("console-view-result");
  if (tabCases && tabResult) {
    tabCases.classList.remove("active");
    tabResult.classList.add("active");
  }
  if (viewCases && viewResult) {
    viewCases.classList.add("hidden");
    viewResult.classList.remove("hidden");
  }

  if (target) target.innerHTML = `<div class="flex items-center gap-2 opacity-80 text-xs py-2"><span class="w-3.5 h-3.5 border-2 border-blue-500 border-t-transparent rounded-full animate-spin"></span><span>Running solution against sample test cases…</span></div>`;

  try {
    const data = await api("POST", "code-runs/", {
      problem_id: problem.id,
      language: editor.getLanguage(),
      code: editor.getValue(),
    });
    if (target) target.innerHTML = renderRunResults(data);
  } catch (err) {
    if (target) {
      target.textContent = err.network
        ? "Connection lost. Your code was not run. Try again when you are reconnected."
        : err.message;
    }
  } finally {
    if ($("run-code")) $("run-code").disabled = false;
  }
}

function renderRunResults(data) {
  if (data.compile_output) {
    return `
      <div class="run-result-box fail rounded-xl p-3.5 space-y-2 border">
        <div class="font-extrabold text-xs flex items-center gap-1.5">
          <span>❌</span>
          <span>Compilation / Syntax Error</span>
        </div>
        <pre class="run-result-pre whitespace-pre-wrap font-mono text-xs p-3 rounded-lg max-h-40 overflow-y-auto">${escapeHtml(data.compile_output)}</pre>
      </div>
    `;
  }

  const cases = data.public || [];
  const allPassed = cases.length > 0 && cases.every((c) => c.passed);

  const statusHeader = allPassed
    ? `<div class="run-result-box pass flex items-center gap-2 p-2.5 rounded-xl border font-extrabold text-xs mb-3">
         <span class="w-2 h-2 rounded-full bg-emerald-500"></span>
         <span>Accepted — All Sample Test Cases Passed!</span>
       </div>`
    : `<div class="run-result-box fail flex items-center gap-2 p-2.5 rounded-xl border font-extrabold text-xs mb-3">
         <span>⚠️</span>
         <span>Test Cases Failed — Check your output below</span>
       </div>`;

  const rows = cases
    .map((r, i) => `
      <div class="run-result-box ${r.passed ? 'pass' : 'fail'} rounded-xl border p-3 space-y-2">
        <div class="flex items-center justify-between text-xs font-extrabold">
          <span>Example Case ${i + 1}</span>
          <span class="text-[10px] uppercase font-black px-2 py-0.5 rounded ${r.passed ? 'bg-emerald-500/20 text-emerald-600 dark:text-emerald-300' : 'bg-rose-500/20 text-rose-600 dark:text-rose-300'}">
            ${r.passed ? 'Passed ✓' : 'Failed ✕'}
          </span>
        </div>
        ${!r.passed ? `
          <div class="grid gap-2 sm:grid-cols-2 text-xs">
            <div>
              <div class="text-[10px] font-bold uppercase tracking-wider opacity-70">Your Output</div>
              <pre class="run-result-pre mt-1 p-2 rounded font-mono text-xs overflow-x-auto whitespace-pre-wrap">${escapeHtml(r.output || "(no output)")}</pre>
            </div>
            <div>
              <div class="text-[10px] font-bold uppercase tracking-wider opacity-70">Expected Output</div>
              <pre class="run-result-pre mt-1 p-2 rounded font-mono text-xs overflow-x-auto whitespace-pre-wrap">${escapeHtml(r.expected)}</pre>
            </div>
          </div>
          ${r.error ? `<pre class="run-result-pre mt-1.5 p-2 rounded font-mono text-xs whitespace-pre-wrap text-amber-600 dark:text-amber-300">${escapeHtml(r.error)}</pre>` : ""}
        ` : ""}
      </div>
    `)
    .join("");

  return statusHeader + `<div class="space-y-2">${rows || '<p class="opacity-70 text-xs">No sample cases evaluated.</p>'}</div>`;
}

async function submitCode(problem, editor) {
  const confirmed = await showInAppConfirm({
    title: "Submit Final Solution?",
    message: "Are you sure you want to submit this code as your final answer for this problem?",
    details: "Once submitted, your code is permanently locked and cannot be edited. Your solution will be evaluated against all test cases for grading.",
    confirmText: "Submit & Lock Code",
    cancelText: "Keep Editing",
    tone: "primary",
  });
  if (!confirmed) return;

  const target = $("code-result");
  const button = $("submit-code");
  if (button) button.disabled = true;
  if (target) target.textContent = "Submitting your final answer…";

  try {
    await flushDrafts();
    await flushOutbox();
    const data = await api("POST", "code-submissions/", {
      problem_id: problem.id,
      language: editor.getLanguage(),
      code: editor.getValue(),
    });
    problem.latest_submission = data.submission;
    if (problem.latest_submission && data.status) {
      problem.latest_submission.status = data.status;
    }
    problem.locked = true;
    lastRenderKey = "";
    render();
    showStatus("Final answer submitted. This code is now locked.", "ok");
  } catch (err) {
    if (target) {
      target.textContent = err.network
        ? "Connection lost. Your code was not submitted. Try again when you are reconnected."
        : err.message;
    }
    if (err.status === 401) window.location.href = `/login/?next=${encodeURIComponent(window.location.pathname)}`;
  } finally {
    if (button && !problem.locked) button.disabled = false;
  }
}

// ---------------------------------------------------------------- section submit

async function submitSection() {
  if (!state || !state.current_section) return;

  const confirmed = await showInAppConfirm({
    title: "Submit Current Section?",
    message: "Are you sure you want to finish and submit this section now?",
    details: "You cannot return to any questions or coding problems in this section once submitted. Time for this section will stop and you will advance to the next section.",
    confirmText: "Submit Section",
    cancelText: "Review Section",
    tone: "warn",
  });
  if (!confirmed) return;

  const buttons = document.querySelectorAll("#submit-section, #header-submit-section, .btn-submit-section");
  buttons.forEach((btn) => {
    btn.disabled = true;
    btn.textContent = "Submitting section…";
  });

  try {
    buttons.forEach((btn) => {
      btn.textContent = "Saving pending changes…";
    });
    await flushDrafts();
    await flushOutbox();
    const remainingOutbox = outboxLoad();
    if (remainingOutbox.length > 0) {
      throw new Error("Pending answers could not be saved to the server. Please check your connection and try submitting again.");
    }
    buttons.forEach((btn) => {
      btn.textContent = "Submitting section…";
    });
    await api("POST", "submit-section/", { section_id: state.current_section.id });
    lastRenderKey = "";
    activeVisitKey = null;
    showStatus("Section submitted successfully.", "ok");
    await refresh();
  } catch (err) {
    buttons.forEach((btn) => {
      btn.disabled = false;
      btn.textContent = "Submit Section →";
    });
    showStatus(
      err.network
        ? "Connection lost. The section was not submitted. Try again when you are reconnected."
        : err.message,
      "error",
    );
  }
}

// ---------------------------------------------------------------- start

const retryBtn = $("retry-camera");
if (retryBtn) {
  retryBtn.addEventListener("click", () => {
    const overlay = $("camera-error-overlay");
    if (overlay) overlay.classList.add("hidden");
    if (proctor) startProctor();
  });
}

// ---------------------------------------------------------------- security: restrict copy & select
document.addEventListener("contextmenu", (e) => {
  if (!e.target.closest(".monaco-editor, textarea")) {
    e.preventDefault();
  }
});

document.addEventListener("copy", (e) => {
  if (!e.target.closest(".monaco-editor, textarea, input")) {
    e.preventDefault();
    showStatus("Copying test content is restricted.", "error");
  }
});

document.addEventListener("cut", (e) => {
  if (!e.target.closest(".monaco-editor, textarea, input")) {
    e.preventDefault();
  }
});

document.addEventListener("keydown", (e) => {
  if (
    e.key === "F12" ||
    ((e.ctrlKey || e.metaKey) && e.shiftKey && ["i", "j", "c"].includes(e.key.toLowerCase())) ||
    ((e.ctrlKey || e.metaKey) && ["u"].includes(e.key.toLowerCase()))
  ) {
    e.preventDefault();
    sendEvent("devtools_attempt", 0, { key: e.key });
    showStatus("Developer tools and page inspection are disabled during the test.", "error");
    return;
  }

  if ((e.ctrlKey || e.metaKey) && ["c", "u", "a"].includes(e.key.toLowerCase())) {
    if (!e.target.closest(".monaco-editor, textarea, input")) {
      e.preventDefault();
      if (e.key.toLowerCase() === "c") {
        showStatus("Copying test content is restricted.", "error");
      }
    }
  }
});

$("header-submit-section")?.addEventListener("click", submitSection);

window.addEventListener("resize", () => {
  if (activeProblemId && editors[activeProblemId]) {
    editors[activeProblemId].then((editor) => {
      if (typeof editor.layout === "function") editor.layout();
    });
  }
});

setupFullscreenGate();
setOffline(!navigator.onLine);
refresh();
watchScreens();
refreshTimer = setInterval(refresh, 5000);
setInterval(() => {
  tick();
  updateFullscreenGate();
}, 1000);
setInterval(flushOutbox, 4000);
