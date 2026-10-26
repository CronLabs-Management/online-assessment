"""
Browser smoke test for the candidate flow.

Run against a local server seeded with demo data:

    DJANGO_DEBUG=1 CODE_RUNNER=local .venv/bin/python manage.py seed_demo
    DJANGO_DEBUG=1 CODE_RUNNER=local .venv/bin/python manage.py runserver 127.0.0.1:8765 --noreload
    .venv/bin/python e2e/smoke_test.py --base http://127.0.0.1:8765 --exam-id 1

External CDN assets are stubbed so the test runs offline. The face detector is a fixed stub,
so this verifies the flow, consent gate and UI wiring, not real face detection.
Requires Playwright with Chromium (set CHROME_PATH if Chromium is elsewhere).
"""

import argparse
import json
import os
import re
import sys

from playwright.sync_api import sync_playwright

PASSWORD = "demo-pass-12345"

MEDIAPIPE_STUB = """
const landmarks = Array.from({ length: 478 }, () => ({ x: 0.5, y: 0.5, z: 0 }));
export const FilesetResolver = { forVisionTasks: async () => ({}) };
export const FaceLandmarker = {
  createFromOptions: async () => ({ detectForVideo: () => ({ faceLandmarks: [landmarks] }) }),
};
"""

HIDDEN_INPUTS = ["100 200", "7 -7", "1 3 5 7 9"]

# Correct Python solutions for the seeded problems, keyed by title.
SOLUTIONS = {
    "Sum two integers": "a, b = map(int, input().split())\nprint(a + b)\n",
    "Reverse a line": "print(input()[::-1])\n",
    "Count even numbers": "nums = input().split()\nprint(sum(1 for n in nums if int(n) % 2 == 0))\n",
}


def check(condition, message):
    status = "PASS" if condition else "FAIL"
    print(f"[{status}] {message}")
    if not condition:
        raise SystemExit(1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default="http://127.0.0.1:8765")
    parser.add_argument("--exam-id", required=True)
    parser.add_argument("--candidate", default="candidate0001@example.test")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    # CHROME_PATH is optional. When unset, Playwright uses the Chromium it installed
    # ("python -m playwright install chromium"), which is what CI and developer machines use.
    chrome = os.environ.get("CHROME_PATH") or None

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=chrome,
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        )
        context = browser.new_context(permissions=["camera"])

        def stub_external(route):
            url = route.request.url
            if "tasks-vision" in url and url.endswith("+esm"):
                route.fulfill(status=200, content_type="application/javascript", body=MEDIAPIPE_STUB)
            else:
                route.fulfill(status=404, body="offline test: external asset not available")

        context.route(re.compile(r"https://(cdn\.jsdelivr\.net|unpkg\.com|cdn\.tailwindcss\.com|storage\.googleapis\.com)/.*"), stub_external)

        page = context.new_page()
        page_errors = []
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        page.on("dialog", lambda dialog: dialog.accept())

        page.goto(f"{base}/accounts/login/")
        page.fill("input[name=username]", args.candidate)
        page.fill("input[name=password]", PASSWORD)
        page.locator("main form button[type=submit], main form button.btn-primary, main form button").first.click()
        page.wait_for_load_state()
        check("/accounts/login" not in page.url, "candidate can log in")

        page.goto(f"{base}/exams/{args.exam_id}/")
        page.locator(f"form[action*='/exams/{args.exam_id}/start/'] button").click()
        page.wait_for_url(re.compile(r".*/attempts/\d+/$"), timeout=20000)
        attempt_id = re.search(r"/attempts/(\d+)/", page.url).group(1)
        check(True, f"attempt {attempt_id} started")

        page.wait_for_selector("#readiness:not(.hidden)", timeout=20000)
        check(page.locator("#snapshot-consent").is_visible(), "snapshot consent box is shown before camera check")
        page.wait_for_function(
            "() => document.getElementById('camera-status').textContent.includes('Tick the consent')",
            timeout=20000,
        )
        check(True, "camera check waits for consent")
        page.check("#snapshot-consent")
        page.wait_for_function(
            "() => document.getElementById('readiness').classList.contains('hidden')", timeout=60000
        )
        check(True, "camera check passes after consent")

        page.wait_for_selector("#content input[type=radio]", timeout=20000)
        check(page.locator("#section-title").inner_text().startswith("Multiple choice"), "MCQ section is active")

        page.locator("#content input[type=radio]").first.check()
        page.wait_for_timeout(1500)
        page.reload()
        page.wait_for_selector("#content input[type=radio]", timeout=20000)
        check(page.locator("#content input[type=radio]:checked").count() == 1, "answer is saved and restored after reload")

        state_resp = page.request.get(f"{base}/api/attempts/{attempt_id}/state/")
        check(state_resp.status == 200, "state endpoint responds")
        state_text = state_resp.text()
        for hidden in HIDDEN_INPUTS:
            check(hidden not in state_text, f"hidden test input {hidden!r} is not sent to the candidate")
        state = state_resp.json()
        check(state.get("snapshot_consent") is True, "server recorded snapshot consent")

        csrf = next(c["value"] for c in context.cookies() if c["name"] == "csrftoken")
        headers = {"X-CSRFToken": csrf, "Content-Type": "application/json"}

        mcq_section = next(s for s in state["sections"] if s.get("kind") == "mcq" or "mcq" in json.dumps(s))
        submit = page.request.post(
            f"{base}/api/attempts/{attempt_id}/submit-section/",
            data=json.dumps({"section_id": mcq_section["id"]}),
            headers=headers,
        )
        check(submit.status == 200, "MCQ section submits")

        page.reload()
        page.wait_for_selector("#section-title", timeout=20000)
        page.wait_for_function(
            "() => document.getElementById('section-title').textContent.toLowerCase().includes('coding')",
            timeout=20000,
        )
        check(True, "coding section unlocks after MCQ submission")

        state = page.request.get(f"{base}/api/attempts/{attempt_id}/state/").json()
        problems = state.get("problems", [])
        check(len(problems) >= 1, f"{len(problems)} coding problem(s) assigned to the candidate")
        problem = problems[0]
        code = SOLUTIONS[problem["title"]]
        run_resp = page.request.post(
            f"{base}/api/attempts/{attempt_id}/code-runs/",
            data=json.dumps({"problem_id": problem["id"], "language": "python", "code": code}),
            headers=headers,
        )
        check(run_resp.status == 200, "Run is accepted")
        run = run_resp.json()
        check(run.get("public") and all(r["passed"] for r in run["public"]), f"Run passes every example for '{problem['title']}'")
        check(set(run.get("hidden", {})) == {"passed", "total"}, "Run reports hidden results as counts only")

        code_resp = page.request.post(
            f"{base}/api/attempts/{attempt_id}/code-submissions/",
            data=json.dumps({"problem_id": problem["id"], "language": "python", "code": code}),
            headers=headers,
        )
        check(code_resp.status == 200, "Submit is accepted")
        summary = code_resp.json().get("submission", {})
        check(summary.get("final") is True, "submission is final")
        check("public_results" not in summary and "hidden_passed" not in summary, "candidate summary carries no results or marks")
        locked = page.request.put(
            f"{base}/api/attempts/{attempt_id}/drafts/",
            data=json.dumps({"problem_id": problem["id"], "language": "python", "code": "print(0)"}),
            headers=headers,
        )
        check(locked.status >= 400 and locked.json().get("code") == "locked", "edits are refused after submit")

        page_load_errors = [e for e in page_errors if "offline" not in e.lower()]
        for error in page_load_errors[:6]:
            print("      page error:", error[:300])
        check(not page_load_errors, f"no uncaught JavaScript errors ({len(page_errors)} seen, offline stubs excluded)")
        browser.close()
    print("SMOKE TEST PASSED")


if __name__ == "__main__":
    sys.exit(main())
