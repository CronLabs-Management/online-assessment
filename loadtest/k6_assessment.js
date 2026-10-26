// k6 load test for the candidate flow: login, start, camera consent, answer, visit, state polling,
// occasional proctoring events, coding submission, and section submit.
//
// Prerequisite (staging only, throwaway data):
//   DJANGO_DEBUG=1 CODE_RUNNER=local .venv/bin/python manage.py seed_demo --candidates 1000
//
// Run:
//   k6 run -e BASE=https://staging.example.test -e EXAM_ID=1 -e VUS=1000 -e CANDIDATES=1000 loadtest/k6_assessment.js
//
// Each virtual user is a different candidate (candidate0001 ... candidateNNNN), so no two
// sessions share an attempt. Staged targets follow the implementation plan (section 19).

import http from "k6/http";
import { check, sleep, fail } from "k6";
import { Trend } from "k6/metrics";

const BASE = __ENV.BASE || "http://127.0.0.1:8000";
const EXAM_ID = __ENV.EXAM_ID || "1";
const VUS = parseInt(__ENV.VUS || "10", 10);
const CANDIDATES = parseInt(__ENV.CANDIDATES || "1000", 10);
const PASSWORD = __ENV.PASSWORD || "demo-pass-12345";
const HOLD = __ENV.HOLD || "5m";
const ITERATIONS_PER_CANDIDATE = parseInt(__ENV.ITERATIONS || "12", 10);

const answerLatency = new Trend("answer_latency", true);
const stateLatency = new Trend("state_latency", true);
const codeLatency = new Trend("code_latency", true);

export const options = {
  scenarios: {
    candidates: {
      executor: "ramping-vus",
      startVUs: 0,
      stages: [
        { duration: "1m", target: VUS },
        { duration: HOLD, target: VUS },
        { duration: "30s", target: 0 },
      ],
      gracefulRampDown: "30s",
    },
  },
  thresholds: {
    http_req_failed: ["rate<0.01"],
    answer_latency: ["p(95)<800"],
    state_latency: ["p(95)<800"],
    code_latency: ["p(95)<5000"],
  },
};

function csrfFromCookies(url) {
  const jar = http.cookieJar().cookiesForURL(url);
  return (jar.csrftoken && jar.csrftoken[0]) || "";
}

function jsonHeaders(url) {
  return {
    headers: {
      "Content-Type": "application/json",
      "X-CSRFToken": csrfFromCookies(url),
      Referer: url,
    },
  };
}

function login(username) {
  const loginUrl = `${BASE}/accounts/login/`;
  const page = http.get(loginUrl);
  const match = page.body.match(/name="csrfmiddlewaretoken" value="([^"]+)"/);
  if (!match) fail("login page did not include a CSRF token");
  const res = http.post(
    loginUrl,
    { username, password: PASSWORD, csrfmiddlewaretoken: match[1] },
    { headers: { Referer: loginUrl } },
  );
  check(res, { "logged in": (r) => r.status === 200 && !r.url.includes("/accounts/login") });
  return res.url.includes("/accounts/login") ? null : res;
}

// Module-level variables are per virtual user in k6.
let vuAttemptId = null;
let vuAttemptUrl = null;
let vuIteration = 0;

export default function () {
  const candidateNumber = ((__VU - 1) % CANDIDATES) + 1;
  const username = `candidate${String(candidateNumber).padStart(4, "0")}`;

  // One session per virtual user: log in, start (or resume) the attempt, pass the camera check.
  vuIteration += 1;
  if (vuAttemptId === null) {
    if (!login(username)) fail(`login failed for ${username}`);
    const start = http.post(`${BASE}/exams/${EXAM_ID}/start/`, null, {
      headers: { "X-CSRFToken": csrfFromCookies(`${BASE}/`), Referer: `${BASE}/` },
      redirects: 5,
    });
    if (!start.url.includes("/attempts/")) fail(`could not start attempt for ${username}`);
    const attemptUrl = start.url;
    const attemptId = attemptUrl.match(/\/attempts\/(\d+)\//)[1];
    vuAttemptUrl = attemptUrl;
    vuAttemptId = attemptId;
    const readiness = http.post(
      `${BASE}/api/attempts/${attemptId}/readiness/`,
      JSON.stringify({ snapshot_consent: true }),
      jsonHeaders(attemptUrl),
    );
    check(readiness, { "camera check recorded": (r) => r.status === 200 || r.status === 409 });
  }

  const attemptId = vuAttemptId;
  const attemptUrl = vuAttemptUrl;
  if (!attemptId) return;

  for (let step = 0; step < ITERATIONS_PER_CANDIDATE; step++) {
    const stateRes = http.get(`${BASE}/api/attempts/${attemptId}/state/`);
    stateLatency.add(stateRes.timings.duration);
    check(stateRes, { "state ok": (r) => r.status === 200 });
    const state = stateRes.json();
    if (!state || state.status !== "in_progress") break;

    const questions = state.questions || [];
    if (state.current_section && state.current_section.kind === "mcq" && questions.length) {
      const question = questions[Math.floor(Math.random() * questions.length)];
      const option = question.options[Math.floor(Math.random() * question.options.length)];
      http.post(
        `${BASE}/api/attempts/${attemptId}/visits/`,
        JSON.stringify({ kind: "mcq", item_id: question.id }),
        jsonHeaders(attemptUrl),
      );
      const answer = http.put(
        `${BASE}/api/attempts/${attemptId}/answers/`,
        JSON.stringify({ question_id: question.id, option_id: option.id }),
        jsonHeaders(attemptUrl),
      );
      answerLatency.add(answer.timings.duration);
      check(answer, { "answer saved": (r) => r.status === 200 });
    }

    if (step % 5 === 4) {
      http.post(
        `${BASE}/api/attempts/${attemptId}/events/`,
        JSON.stringify({ kind: "tab_hidden", duration_ms: 1200, client_at: new Date().toISOString(), context: {} }),
        jsonHeaders(attemptUrl),
      );
    }

    if (state.current_section && state.current_section.kind === "coding" && state.problems && state.problems.length) {
      const problem = state.problems[0];
      const started = Date.now();
      const code = http.post(
        `${BASE}/api/attempts/${attemptId}/code-submissions/`,
        JSON.stringify({ problem_id: problem.id, language: "python", code: "print(input())\n" }),
        jsonHeaders(attemptUrl),
      );
      codeLatency.add(Date.now() - started);
      check(code, { "code accepted or queued": (r) => r.status === 200 || r.status === 429 });
      break;
    }
    sleep(1 + Math.random() * 2);
  }
}
