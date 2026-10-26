// Candidate exam runner. The server is the source of truth for time, sections and scoring.
// This file only displays state, sends actions, and keeps a local outbox for offline work.
import { Proctor } from "/static/js/proctor.js";

const root = document.getElementById("attempt-root");
const attemptId = root.dataset.attemptId;
const cameraRequired = root.dataset.cameraRequired === "1";
const missingFaceSeconds = Number(root.dataset.missingFace || 5);
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

function showStatus(message, tone = "info") {
  const el = $("status");
  el.textContent = message;
  el.className = `text-sm ${tone === "error" ? "text-red-700" : tone === "ok" ? "text-green-700" : "text-slate-600"}`;
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
  content.innerHTML = `<div class="alert alert-bad" role="alert">
      <p class="font-semibold">This test is open in another browser or session.</p>
      <p class="mt-2 leading-relaxed">${escapeHtml(message || "")}</p>
      <p class="mt-2 leading-relaxed">Close the other window. If you cannot find it, ask the administrator to allow you to continue here. Your answers so far are saved.</p>
    </div>`;
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

function tick() {
  if (!state || !state.deadline) return;
  const overall = remainingFor(state.deadline);
  $("overall").textContent = fmt(overall);
  if (state.current_section) {
    const section = remainingFor(state.current_section.deadline);
    $("timer").textContent = fmt(section);
    // Warn in the last five minutes and alarm in the last minute. Colour is never the only cue: the text is the timer.
    $("timer").classList.toggle("text-amber-700", section > 60 && section <= 300);
    $("timer").classList.toggle("text-red-700", section > 0 && section <= 60);
    if (section <= 0) refresh();
  } else {
    $("timer").textContent = "--:--";
  }
  if (overall <= 0) refresh();
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
      window.location.href = "/accounts/login/";
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
    window.location.href = `/attempts/${attemptId}/result/`;
    return;
  }
  renderSections();
  $("sections-panel").classList.remove("hidden");

  if (state.readiness_required) {
    showReadiness();
    return;
  }
  hideReadiness();

  if (!state.current_section) {
    content.innerHTML = '<p class="text-slate-700">All sections are complete. Redirecting to your results…</p>';
    setTimeout(() => (window.location.href = `/attempts/${attemptId}/result/`), 1200);
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
    const label = s.status === "submitted" ? "Submitted" : s.status === "expired" ? "Time up" : s.status === "in_progress" ? "Current" : "Locked";
    return `<li class="flex justify-between text-sm"><span>${i + 1}. ${escapeHtml(s.title)}</span><span class="text-slate-500">${label}</span></li>`;
  });
  $("sections").innerHTML = items.join("");
}

// ---------------------------------------------------------------- camera readiness

function snapshotBox() {
  return $("snapshot-consent");
}

function snapshotConsent() {
  const box = snapshotBox();
  return Boolean(box && box.checked);
}

function showReadiness() {
  $("readiness").classList.remove("hidden");
  content.classList.add("bg-slate-100", "pointer-events-none");
  content.setAttribute("aria-disabled", "true");
  if (!cameraRequired) {
    // Camera not required by this test: confirm readiness immediately.
    api("POST", "readiness/", { snapshot_consent: snapshotConsent() })
      .then(refresh)
      .catch((err) => showStatus(err.message, "error"));
    return;
  }
  const box = snapshotBox();
  if (box && !box.checked) {
    // Consent is needed before the camera check, so the face check never runs unconsented.
    $("camera-status").textContent = "Tick the consent box above to start the camera check.";
    if (!box.dataset.wired) {
      box.dataset.wired = "1";
      box.addEventListener("change", () => {
        if (box.checked && state && state.readiness_required) {
          ensureProctor();
          proctor.beginReadinessCheck();
        }
      });
    }
    return;
  }
  ensureProctor();
  proctor.beginReadinessCheck();
}

function hideReadiness() {
  $("readiness").classList.add("hidden");
  content.classList.remove("bg-slate-100", "pointer-events-none");
  content.removeAttribute("aria-disabled");
}

function ensureProctor() {
  if (proctor) {
    if (!proctor.running) startProctor();
    return;
  }
  proctor = new Proctor({
    video: $("camera"),
    missingFaceSeconds,
    onEvent: sendEvent,
    onStatus: (message) => ($("camera-status").textContent = message),
    onReadyChange: async (ready) => {
      if (!ready) return;
      $("camera-status").textContent = "Camera check passed.";
      try {
        await api("POST", "readiness/", { snapshot_consent: snapshotConsent() });
        await refresh();
      } catch (err) {
        showStatus(err.message, "error");
      }
    },
  });
  startProctor();
}

function startProctor() {
  proctor.start().catch(() => {
    $("retry-camera").classList.remove("hidden");
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
    if (data.over_threshold && SNAPSHOT_KINDS.has(kind)) {
      await captureSnapshot(data.event_id);
    }
  } catch (err) {
    if (err.network) {
      // Events are small; keep them in the outbox so the server still receives them.
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
  try {
    await fetch(`/api/attempts/${attemptId}/snapshots/`, {
      method: "POST",
      credentials: "same-origin",
      headers: { "X-CSRFToken": csrfToken() },
      body: form,
    });
  } catch (_) {
    // Snapshot upload is best-effort; the event itself is already recorded.
  }
}

document.addEventListener("visibilitychange", () => {
  if (document.hidden) {
    hiddenSince = Date.now();
  } else if (hiddenSince !== null) {
    sendEvent("tab_hidden", Date.now() - hiddenSince, {});
    hiddenSince = null;
  }
});

window.addEventListener("blur", () => {
  if (!document.hidden) blurSince = Date.now();
});

window.addEventListener("focus", () => {
  if (blurSince !== null) {
    sendEvent("window_blur", Date.now() - blurSince, {});
    blurSince = null;
  }
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
  content.toggleAttribute("inert", needed);
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
        ? "border-slate-900 bg-slate-900 text-white"
        : answered
          ? "border-green-700 bg-green-50 text-green-900"
          : "border-slate-300 bg-white text-slate-700 hover:border-slate-500";
      const label = `Question ${i + 1}${answered ? ", answered" : ", not answered"}${current ? ", current" : ""}`;
      return `<button type="button" data-idx="${i}" aria-label="${label}" ${current ? 'aria-current="step"' : ""} class="num h-10 w-10 rounded-md border text-sm font-medium ${tone}">${i + 1}</button>`;
    })
    .join("");

  const options = question.options
    .map((option, optIdx) => {
      const checked = option.id === question.selected_option_id ? "checked" : "";
      const letter = String.fromCharCode(65 + optIdx);
      return `<label class="flex cursor-pointer items-start gap-4 rounded-xl border-2 border-slate-200 bg-white p-5 leading-relaxed hover:border-blue-400 hover:bg-blue-50/20 has-[:checked]:border-emerald-600 has-[:checked]:bg-emerald-50/30 transition-colors">
        <input type="radio" name="mcq-${question.id}" value="${option.id}" ${checked} class="mt-1 h-5 w-5 text-emerald-600">
        <div class="flex-1">
          <span class="mr-2 inline-block rounded bg-slate-100 px-2 py-0.5 text-xs font-bold text-slate-700">${letter}</span>
          <span class="text-lg font-medium text-slate-900">${escapeHtml(option.text)}</span>
        </div>
      </label>`;
    })
    .join("");

  content.innerHTML = `
    <div class="flex items-center justify-between border-b border-slate-100 pb-3">
      <span class="num text-base font-bold text-slate-500">Question ${mcqIndex + 1} of ${questions.length}</span>
      <span class="rounded bg-blue-50 px-2.5 py-0.5 text-xs font-bold text-blue-700">Multiple Choice</span>
    </div>
    <p class="mt-5 whitespace-pre-line text-2xl font-semibold leading-relaxed text-slate-900">${escapeHtml(question.text)}</p>
    <div class="mt-8 space-y-3.5">${options}</div>
    <div class="mt-10 flex items-center justify-between gap-4 pt-4 border-t border-slate-100">
      <button type="button" id="prev" class="btn btn-secondary px-6 py-2.5 text-base font-semibold" ${mcqIndex === 0 ? "disabled" : ""}>&larr; Previous question</button>
      <button type="button" id="next" class="btn btn-primary px-7 py-2.5 text-base font-bold" ${mcqIndex >= questions.length - 1 ? "disabled" : ""}>Next question &rarr;</button>
    </div>
    <nav class="mt-10 border-t border-slate-200 pt-6" aria-label="Questions">
      <p class="mb-3 text-sm font-semibold text-slate-500 uppercase tracking-wider">Question Navigator</p>
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
  $("next").addEventListener("click", () => moveMcq(mcqIndex + 1));
  content.querySelectorAll("nav button").forEach((button) => {
    button.addEventListener("click", () => moveMcq(Number(button.dataset.idx)));
  });

  openVisit("mcq", question.id);
}

function moveMcq(index) {
  const total = (state.questions || []).length;
  if (index < 0 || index >= total) return;
  mcqIndex = index;
  lastRenderKey = "";
  render();
}

// ---------------------------------------------------------------- coding

function loadMonaco() {
  if (monacoPromise) return monacoPromise;
  const base = "https://cdn.jsdelivr.net/npm/monaco-editor@0.52.0/min/vs";
  monacoPromise = new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `${base}/loader.js`;
    script.onload = () => {
      window.require.config({ paths: { vs: base } });
      window.require(["vs/editor/editor.main"], () => resolve(window.monaco), reject);
    };
    script.onerror = reject;
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

async function makeEditor(problem, host) {
  // The editor always reports the language the candidate chose, even when the editor library is
  // unavailable, so submissions carry the right language.
  let currentLanguage = problem.draft_language || problem.languages[0];
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
    const editor = monaco.editor.create(host, {
      value: problem.draft_code || "",
      language: monacoLanguageId(currentLanguage),
      minimap: { enabled: false },
      automaticLayout: true,
      fontSize: 16,
    });
    const sendDraft = () => {
      timer = null;
      onChange(editor.getValue(), currentLanguage);
    };
    editor.onDidChangeModelContent(() => {
      clearTimeout(timer);
      timer = setTimeout(sendDraft, 1200);
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
      flush: () => {
        if (timer) {
          clearTimeout(timer);
          sendDraft();
        }
      },
    };
  } catch (_) {
    // Fallback editor if the CDN is unreachable.
    const area = document.createElement("textarea");
    area.className = "h-96 w-full rounded border p-3 font-mono text-sm";
    area.spellcheck = false;
    area.setAttribute("aria-label", "Code editor");
    area.value = problem.draft_code || "";
    host.appendChild(area);
    const sendDraft = () => {
      timer = null;
      onChange(area.value, currentLanguage);
    };
    area.addEventListener("input", () => {
      clearTimeout(timer);
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
      flush: () => {
        if (timer) {
          clearTimeout(timer);
          sendDraft();
        }
      },
    };
  }
}

function hostFor(problem) {
  if (!editorHosts[problem.id]) {
    const host = document.createElement("div");
    host.className = "h-96 w-full overflow-hidden rounded border";
    editorHosts[problem.id] = host;
  }
  return editorHosts[problem.id];
}

function renderCoding() {
  const problems = state.problems || [];
  if (!problems.length) {
    content.innerHTML = '<p class="text-slate-600">No coding problems in this section.</p>';
    return;
  }
  if (!problems.find((p) => p.id === activeProblemId)) activeProblemId = problems[0].id;
  const problem = problems.find((p) => p.id === activeProblemId);
  const locked = Boolean(problem.locked);

  const tabs = problems
    .map((p, i) => {
      const current = p.id === problem.id;
      const badge = p.locked ? " ✓" : "";
      return `<button type="button" data-id="${p.id}" class="rounded-md border px-3 py-1.5 text-sm ${current ? "border-slate-900 bg-slate-900 text-white" : "border-slate-300 bg-white hover:bg-slate-100"}">Problem ${i + 1}: ${escapeHtml(p.title)}${badge}</button>`;
    })
    .join("");

  const examples = problem.public_cases
    .map((c, i) => `<div class="rounded-lg border border-slate-200 bg-white p-3 text-sm">
        <div class="mb-2 font-medium">Example ${i + 1}</div>
        <div class="grid gap-2 sm:grid-cols-2">
          <div><div class="text-xs uppercase tracking-wide text-slate-500">Input</div><pre class="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs">${escapeHtml(c.input)}</pre></div>
          <div><div class="text-xs uppercase tracking-wide text-slate-500">Expected output</div><pre class="mt-1 whitespace-pre-wrap rounded bg-slate-50 p-2 font-mono text-xs">${escapeHtml(c.expected)}</pre></div>
        </div>
      </div>`)
    .join("");

  const languages = problem.languages
    .map((lang) => {
      const activeLang = problem.draft_language || problem.languages[0];
      const isSelected = lang.toLowerCase() === (activeLang || "").toLowerCase();
      return `<option value="${escapeHtml(lang)}" ${isSelected ? "selected" : ""}>${escapeHtml(formatLanguageName(lang))}</option>`;
    })
    .join("");

  const lockBanner = locked
    ? `<div class="mb-4 rounded-lg border border-green-200 bg-green-50 px-4 py-3 text-sm text-green-900">
         <strong>Final answer submitted.</strong> This code is locked and cannot be changed.
       </div>`
    : "";

  content.innerHTML = `
    <div class="mb-5 flex flex-wrap gap-2">${tabs}</div>
    <h2 class="text-lg font-semibold">${escapeHtml(problem.title)}</h2>
    <div class="mt-2 whitespace-pre-line text-sm text-slate-800">${escapeHtml(problem.statement)}</div>
    ${problem.input_spec ? `<div class="mt-3 whitespace-pre-line text-sm"><span class="font-medium">Input:</span> ${escapeHtml(problem.input_spec)}</div>` : ""}
    ${problem.output_spec ? `<div class="mt-2 whitespace-pre-line text-sm"><span class="font-medium">Output:</span> ${escapeHtml(problem.output_spec)}</div>` : ""}
    ${problem.constraints ? `<div class="mt-2 whitespace-pre-line text-sm"><span class="font-medium">Constraints:</span> ${escapeHtml(problem.constraints)}</div>` : ""}
    <div class="mt-4 space-y-3">${examples}</div>

    <div class="mt-6">${lockBanner}</div>
    <div class="mb-3 flex flex-wrap items-center gap-3">
      <label class="text-sm">Language
        <select id="lang" class="ml-1 rounded-md border border-slate-300 px-2 py-1 text-sm" ${locked ? "disabled" : ""}>${languages}</select>
      </label>
      <button type="button" id="run-code" class="rounded-md border border-slate-300 px-4 py-2 text-sm font-medium hover:bg-slate-100 disabled:opacity-50" ${locked ? "disabled" : ""}>Run</button>
      <button type="button" id="submit-code" class="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white hover:bg-slate-700 disabled:bg-slate-300" ${locked ? "disabled" : ""}>Submit final answer</button>
    </div>
    <p class="mb-3 text-xs text-slate-500">Run checks your code against the example and hidden test cases and shows the results. Runs are not saved. Only your submitted code is graded.</p>
    <div id="editor-slot" class="mb-4"></div>
    <div id="code-result" class="text-sm"></div>`;

  content.querySelectorAll("button[data-id]").forEach((button) => {
    button.addEventListener("click", () => {
      activeProblemId = Number(button.dataset.id);
      lastRenderKey = "";
      render();
    });
  });

  // Mount the host in the page before creating the editor, so the editor can measure itself.
  $("editor-slot").appendChild(hostFor(problem));
  if (!editors[problem.id]) {
    editors[problem.id] = makeEditor(problem, editorHosts[problem.id]);
  }
  editors[problem.id].then((editor) => {
    if (locked) {
      editor.setReadOnly(true);
      renderSubmissionSummary(problem);
      return;
    }
    const langSelect = $("lang");
    const runButton = $("run-code");
    const submitButton = $("submit-code");
    if (!langSelect || !runButton || !submitButton) return;
    langSelect.addEventListener("change", (event) => editor.setLanguage(event.target.value));
    runButton.addEventListener("click", () => runCode(problem, editor));
    submitButton.addEventListener("click", () => submitCode(problem, editor));
  });

  renderSubmissionSummary(problem);
  openVisit("coding", problem.id);
}

function renderSubmissionSummary(problem) {
  const target = $("code-result");
  if (!target) return;
  if (!problem.locked) {
    target.textContent = "Not submitted yet.";
    return;
  }
  const sub = problem.latest_submission;
  if (sub && sub.status === "runner_error") {
    target.innerHTML = `<p class="text-slate-700">Your final answer is recorded. It could not be checked automatically, and the administrator will review it.</p>`;
    return;
  }
  target.innerHTML = `<p class="text-slate-700">Your final answer is recorded${sub ? ` (${escapeHtml(sub.language)})` : ""}. Results are not shown during the test.</p>`;
}

async function runCode(problem, editor) {
  const target = $("code-result");
  const button = $("run-code");
  if (button) button.disabled = true;
  target.textContent = "Running your code against the test cases…";
  try {
    const data = await api("POST", "code-runs/", {
      problem_id: problem.id,
      language: editor.getLanguage(),
      code: editor.getValue(),
    });
    target.innerHTML = renderRunResults(data);
  } catch (err) {
    target.textContent = err.network
      ? "Connection lost. Your code was not run. Try again when you are reconnected."
      : err.message;
  } finally {
    if ($("run-code")) $("run-code").disabled = false;
  }
}

function renderRunResults(data) {
  if (data.compile_output) {
    return `<div class="rounded-lg border border-red-200 bg-red-50 p-4">
        <div class="font-medium text-red-800">Your code did not compile</div>
        <pre class="mt-2 whitespace-pre-wrap font-mono text-xs text-red-900">${escapeHtml(data.compile_output)}</pre>
      </div>`;
  }
  const publicRows = (data.public || [])
    .map((r, i) => `<div class="rounded-lg border ${r.passed ? "border-green-200 bg-green-50" : "border-red-200 bg-red-50"} p-3">
        <div class="font-medium ${r.passed ? "text-green-800" : "text-red-800"}">Example ${i + 1}: ${r.passed ? "passed" : "did not pass"}</div>
        ${r.passed ? "" : `<div class="mt-2 grid gap-2 sm:grid-cols-2">
          <div><div class="text-xs uppercase tracking-wide text-slate-500">Your output</div><pre class="mt-1 whitespace-pre-wrap rounded bg-white p-2 font-mono text-xs">${escapeHtml(r.output || "(no output)")}</pre></div>
          <div><div class="text-xs uppercase tracking-wide text-slate-500">Expected</div><pre class="mt-1 whitespace-pre-wrap rounded bg-white p-2 font-mono text-xs">${escapeHtml(r.expected)}</pre></div>
        </div>
        ${r.error ? `<pre class="mt-2 whitespace-pre-wrap font-mono text-xs text-red-900">${escapeHtml(r.error)}</pre>` : ""}`}
      </div>`)
    .join("");
  const hidden = data.hidden || { passed: 0, total: 0 };
  const hiddenLine = hidden.total
    ? `<div class="rounded-lg border border-slate-200 bg-white p-3 font-medium">Hidden test cases passed: ${hidden.passed} of ${hidden.total}</div>`
    : "";
  return `<div class="space-y-3">${publicRows || '<p class="text-slate-600">No example cases for this problem.</p>'}${hiddenLine}</div>`;
}

async function submitCode(problem, editor) {
  const message = "Submit this as your final answer?\n\nAfter you submit, this code is locked and cannot be changed. Only your submitted code is graded.";
  if (!confirm(message)) return;
  const target = $("code-result");
  target.textContent = "Submitting your final answer…";
  try {
    await flushDrafts();
    await flushOutbox();
    const data = await api("POST", "code-submissions/", {
      problem_id: problem.id,
      language: editor.getLanguage(),
      code: editor.getValue(),
    });
    problem.latest_submission = data.submission;
    problem.locked = true;
    lastRenderKey = "";
    render();
    showStatus("Final answer submitted. This code is now locked.", "ok");
  } catch (err) {
    target.textContent = err.network
      ? "Connection lost. Your code was not submitted. Try again when you are reconnected."
      : err.message;
    if (err.status === 401) window.location.href = "/accounts/login/";
  }
}

// ---------------------------------------------------------------- section submit

async function submitSection() {
  if (!state || !state.current_section) return;
  if (!confirm("Submit this section now? You cannot return to it afterwards.")) return;
  try {
    await flushDrafts();
    await flushOutbox();
    await api("POST", "submit-section/", { section_id: state.current_section.id });
    lastRenderKey = "";
    activeVisitKey = null;
    await refresh();
  } catch (err) {
    showStatus(
      err.network
        ? "Connection lost. The section was not submitted. Try again when you are reconnected."
        : err.message,
      "error",
    );
  }
}

// ---------------------------------------------------------------- start

$("submit-section").addEventListener("click", submitSection);
$("retry-camera").addEventListener("click", () => {
  $("retry-camera").classList.add("hidden");
  if (proctor) startProctor();
});

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
  if ((e.ctrlKey || e.metaKey) && ["c", "u", "a"].includes(e.key.toLowerCase())) {
    if (!e.target.closest(".monaco-editor, textarea, input")) {
      e.preventDefault();
      if (e.key.toLowerCase() === "c") {
        showStatus("Copying test content is restricted.", "error");
      }
    }
  }
});

setupFullscreenGate();
setOffline(!navigator.onLine);
refresh();
refreshTimer = setInterval(refresh, 5000);
setInterval(() => {
  tick();
  updateFullscreenGate();
}, 1000);
setInterval(flushOutbox, 4000);
