# Proctored Assessment Platform

Django application for creating tests, approving candidates, running timed MCQ and coding
sections, recording proctoring evidence, and reviewing attempts.

## What is included

- **Test authoring (staff):** settings, instructions, timed sections, MCQ questions with exactly one
  correct option, coding problems with public and hidden test cases. A **pre-publish check** lists
  every problem that blocks publishing; a test cannot be published until those pass.
- **Candidate access (staff):** paste the mailing list (email, name, phone). Candidates sign in with
  their email and a random password that the platform emails to them. Staff can send or resend login
  details at any time (a new password replaces the old one), remove and restore access, and every
  action is audited. Candidates confirm their name and phone before they can start.
- **Coding:** Python and JavaScript. **Run** checks the code against the public and hidden cases on
  the server and shows the results (hidden cases as counts only); runs are not saved. **Submit** is the
  final answer: it is graded and the problem is locked.
- **Marks:** candidates never see marks, pass marks or case results. Marks are visible to staff only.
- **Settings (staff):** how long proctoring snapshots are kept (default 60 days, 1 to 365).
- **Attempt engine (server-side):** randomized MCQ order and question selection per candidate, fixed
  assignments, section locking, server-clock deadlines, capped pause credit for disconnections,
  autosave, per-visit question timing.
- **Proctoring:** camera readiness check, face-absence, multiple faces, head turns, tab and window
  focus events, consent-gated snapshots stored privately.
- **Review (staff and Reviewers group):** attempt review with MCQ answers, visit timing, code versions
  and results, proctoring timeline with snapshots, interview decisions. CSV export and audit log.
- **Operations:** management commands to expire attempts, grade queued submissions and purge expired
  evidence.

## Security controls

Candidate tests are bound to one browser session, and a second session is refused until staff allow a
resume. Tests can require full screen, copy and paste are logged by length only, and sign-in is
throttled after repeated failures. The limits of browser-side proctoring are listed in
`docs/EXAM_FLOW.md` (section 12).

## Local development

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
export DJANGO_DEBUG=1 CODE_RUNNER=local        # development only: see the runner warning below
.venv/bin/python manage.py migrate
.venv/bin/python manage.py create_admin --username admin --email you@example.com
.venv/bin/python manage.py seed_demo --candidates 5   # optional demo test and candidates
.venv/bin/python manage.py runserver
```

`seed_demo` creates a staff login (`admin`) and candidates whose usernames are their emails, for
example `candidate0001@example.test`. All of them use the password `demo-pass-12345`. The command
refuses to run unless `DJANGO_DEBUG` is on.

### Tests

```bash
DJANGO_DEBUG=1 CODE_RUNNER=local .venv/bin/python manage.py test --parallel 1
```

Lint and the JavaScript syntax check:

```bash
pip install -r requirements-dev.txt
ruff check .
for f in static/js/*.js; do cp "$f" /tmp/check.mjs && node --check /tmp/check.mjs; done
```

### Continuous integration

`.github/workflows/ci.yml` runs on every push to `main` and on every pull request:

- **lint**: `ruff check` and an ES-module syntax check of every script in `static/js/`.
- **test**: migrations must be up to date, the full test suite runs, and `manage.py check --deploy`
  runs with production-style environment values.
- **e2e**: migrates a fresh database, seeds demo data, starts the dev server and runs the Playwright
  candidate flow (`e2e/smoke_test.py`). The server log is uploaded if this job fails.

The load test in `loadtest/` is not part of CI. Run it by hand against a staging server.

### Browser smoke test

With the dev server running on port 8765 and demo data seeded:

```bash
python3 e2e/smoke_test.py --base http://127.0.0.1:8765 --exam-id <id printed by seed_demo>
```

Requires Playwright with Chromium. External CDN assets are stubbed, so this checks the flow and wiring,
not real face detection.

## Code runner

Candidate code must never run on the web server. Two modes:

| `CODE_RUNNER` | Use | Notes |
| --- | --- | --- |
| `judge0` | Production | Sends each case to a Judge0 CE API on a separate, isolated host. Needs `JUDGE0_URL`. |
| `local` | Development only | Runs code as a local process with CPU, memory and output limits. **Not a security boundary.** Never use it with untrusted candidates. |
| *(empty)* | Default | Uses Judge0 if `JUDGE0_URL` is set; otherwise submissions are marked as runner errors, never as wrong answers. |

Setting up Judge0 (when a separate server is available): run the official Judge0 CE Docker images
on a small VPS with Docker, with network access restricted to your Django server, and point
`JUDGE0_URL` at it. Follow the current Judge0 CE release documentation for the exact compose file
and version, since image tags change between releases.

Grading runs in-process after each final submission. For a queue, run `manage.py grade_queued`
periodically (for example, a cPanel cron job every minute).

## Deploying on cPanel (Setup Python App / Passenger)

1. In cPanel → **Setup Python App**, create an app with Python 3.13 (3.10+ works with Django 5.2)
   pointing at the repository folder. The app root must contain `passenger_wsgi.py`.
2. Enter the environment variables from `.env.example` in the app's environment panel. Set real
   values for `DJANGO_SECRET_KEY`, `DJANGO_ALLOWED_HOSTS`, `DJANGO_CSRF_TRUSTED_ORIGINS`, `SITE_URL`
   (the public address, used in login emails), the `EMAIL_*` values for your mail server, and the
   `DB_*` values from **MySQL Databases**. Leave `DJANGO_DEBUG` unset or `0`.
3. Create the private evidence folder **outside** `public_html` (for example
   `/home/<user>/assessment_private`) and set `PRIVATE_MEDIA_ROOT` to it.
4. Install dependencies in the app's virtual environment: `pip install -r requirements.txt`.
5. Run, from the app folder:
   ```bash
   python manage.py migrate
   python manage.py collectstatic --noinput
   python manage.py create_admin --username admin --email you@example.com
   python manage.py test_email your-email@example.com   # verify email is working
   ```
6. Restart the app (Setup Python App → Restart, or `touch tmp/restart.txt`).
7. Optional cron jobs (cPanel → Cron Jobs):
   - `* * * * *` `cd <app> && <venv>/bin/python manage.py expire_attempts ; <venv>/bin/python manage.py grade_queued`

     Run `expire_attempts` first: it finishes attempts whose candidates left before time ran out, so
     their last section's drafts are queued before grading runs. Use `;`, not `&&`, so grading still runs
     if the sweep fails.
   - `0 3 * * *` `cd <app> && <venv>/bin/python manage.py purge_expired_snapshots`

     Deletes snapshots older than the retention period set under **Settings** in the staff area.
8. Staff accounts: create the administrator with `createsuperuser` (or in the Django admin, with staff
   access). Put reviewers in the **Reviewers** group (create the group in admin first). Reviewers can
   open attempts and record interview decisions, but cannot edit tests or manage candidate access.
   Candidate accounts are created by staff when they import the mailing list; their username is their
   email address.

Notes for shared hosting:

- The rate limits and the code-runner cache are per process. Passenger may run several processes, so
  limits are approximate. For strict limits, move the cache to a shared backend.
- Candidate code must run on the separate Judge0 host, not on cPanel.

## Production checklist

- `DJANGO_DEBUG` is off and `DJANGO_SECRET_KEY` is set.
- `SITE_URL` is the public address and `EMAIL_*` points at a working mail server. Run
  `python manage.py test_email you@example.com` to verify email delivery before the test opens.
- `CODE_RUNNER=judge0` with a reachable `JUDGE0_URL` (or accept that scored coding is unavailable).
- `PRIVATE_MEDIA_ROOT` is outside the web root and backed up with the database.
- Run `python manage.py check --deploy` with the production environment variables set.
- Load tests run on staging before candidates use the platform (see `loadtest/README.md`).
