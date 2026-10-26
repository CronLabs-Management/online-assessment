# How an assessment works, end to end

This describes the platform as it is built: who does what, how candidates get access, how a test
runs, how marks are calculated, what data is kept, and what happens in the edge cases.

---

## 1. Roles

| Role | How the account is made | Can do |
|---|---|---|
| Staff (administrator) | `createsuperuser`, or a staff user in `/admin/` | Create and publish tests, import candidates, send and resend login details, remove or restore access, see marks, review attempts, change settings, read the audit log |
| Reviewer | Member of the **Reviewers** group | Read attempts, view snapshots, record interview decisions. Cannot change tests, candidate access or settings. |
| Candidate | Created by staff when the mailing list is imported | Sign in, confirm details, take a published test they have access to, see their own confirmation. Never sees marks. |
| System | Cron jobs | Expire abandoned attempts, grade queued code, delete evidence past the retention period |

Staff cannot take tests. A reviewer who also sat a test cannot record a decision on their own attempt.

---

## 2. Creating and publishing a test (staff)

1. **Tests → New test.** Enter the title, instructions, opening and closing times, total duration and
   the proctoring options. A new test is a **draft**: candidates cannot see it.
2. **Add sections.** Each section is either multiple choice or coding, with its own time limit and
   order. A multiple-choice section draws a set number of questions per candidate. A coding section
   draws a set number of problems per candidate.
3. **Add questions.** Each question needs at least two options and exactly one correct option.
4. **Add coding problems.** Each problem needs a statement, at least one supported language
   (Python or JavaScript) and at least one test case with an expected output. Hidden test cases are
   used for grading and never shown to candidates.
5. **Pre-publish check.** The test page lists every requirement with a tick, a cross (must fix) or a
   warning (does not block publishing). The publish button stays disabled until every blocking item
   passes. The same rules are enforced again on the server when publishing.

The blocking items are: at least one section; each section's time fits inside the total duration;
closing time is after opening time and in the future; enough questions for each multiple-choice
section; enough coding problems for each coding section; every question has two or more options and
exactly one correct; every coding problem has a statement, a language and a test case with an
expected output.

Warnings (do not block): a coding problem with no hidden test case, and an empty candidate list.

**Unpublish** hides the test from candidates again. Changes to a published test should be avoided
while candidates are taking it.

---

## 3. Giving candidates access

Candidates are not self-registered. Staff hold the mailing list.

1. **Candidates → Add candidates.** Paste one candidate per line: `email, Full Name, phone`. Name and
   phone are optional, and commas or tabs both work, so a spreadsheet can be pasted directly. Each
   candidate is added as **approved**. Adding does not send any email.
   - An existing candidate on another test keeps their account and gets a new entry for this test.
   - A staff email address is skipped and reported.
   - Entering the same person twice does not duplicate them.
2. **Send login details.** On the Candidates page:
   - **Send to new candidates** emails everyone who has not received a password yet.
   - **Resend to everyone** gives everyone a new password, and the old ones stop working.
   - Each click handles at most 50 candidates, because password hashing is deliberately slow. The page
     reports how many are still waiting; press the button again for the next batch.
3. **Per candidate:** **Send login email** (or **Resend login email**) and **Remove access** (with an
   optional reason). Access can be **restored** later. Every action is audited.

### What the candidate receives

One email: "Your login for <test>". It contains the sign-in address, the email address they sign in
with, a random password, the link to the test, the test's opening and closing times, and short
instructions: keep the password private, the timer runs once the test is started, and a new password
replaces the old one each time it is resent.

The password is generated from letters and digits in the form `xxxx-xxxx-xxxx`. It is hashed in the
database and never shown again. Staff cannot read it back; they can only send a new one.

### Sign-in and resend

- Candidates sign in at the site's sign-in page with their **email** and the **password** from the
  email. Email matching ignores capitals.
- Candidates cannot change their password. If they lose the email, they ask the administrator, who
  presses **Resend login email**.
- A failed email does not change anything: the candidate keeps the password they already have. The
  page says the email could not be sent so staff can check the mail settings and try again.
- Access that has been removed does not allow a new test start, and a candidate who is mid-test is
  stopped at their next action.

---

## 4. The candidate's journey

| Step | What the candidate sees |
|---|---|
| Sign in | **My tests**: each test with its state and the next action |
| Confirm details | On the first visit, the candidate checks their name and phone. Starting a test is blocked until this is done. |
| Wait | "Opens soon" until the opening time |
| Start | **Start test** on the test page. The timer starts and cannot be paused. Readiness (camera and microphone) is checked first. |
| Work | Sections in order. Answers are saved as the candidate goes. |
| Submit a section | Asks for confirmation. A submitted section cannot be reopened. |
| Coding | **Run** checks the code and shows results. **Submit final answer** locks the problem. |
| Finish | "Your test is submitted." No marks or results are shown. |

The possible states on **My tests** are: Confirm your details, Opens soon, Open, In progress,
Submitted, Closed, and Access removed.

### Run and Submit (coding)

- **Run** compiles and runs the code against every example and hidden test case on the server. It shows
  each example's input, expected output and actual output, and for hidden cases only the count
  ("Hidden test cases passed: 2 of 3"). Runs are not saved and are not graded. Run is limited to 20
  per minute per attempt.
- **Submit final answer** asks for confirmation, then grades the code immediately. After submission the
  problem is **locked**: the editor becomes read-only and no further submissions or drafts are accepted
  for that problem. Submission is limited to 30 per minute per attempt.
- If a candidate has not submitted a problem when its section ends, the most recent unsubmitted draft
  is submitted automatically as the final answer.
- A problem whose grading fails repeatedly is flagged for staff review and scores zero until staff
  look at it. The candidate is told the answer is recorded and will be reviewed.

---

## 5. Marks

Marks are calculated on the server and shown only to staff (the attempt review page, the candidate
list and the CSV export).

- **Multiple choice:** the marks of each question the candidate answered with its correct option.
  Unanswered and wrong answers score zero.
- **Coding:** the marks of each problem's final submission, graded against all its test cases
  (public and hidden). Each problem can be submitted once, so there is one final submission per
  problem. The maximum is the sum of the problem marks.
- **Total:** MCQ plus coding, out of the maximum for the candidate's own question and problem draw.

There is **no pass mark**. The platform does not decide who passes. Staff read the marks and record
interview decisions, which are separate from the marks.

Monitoring signals (tab changes, face absence, multiple faces, head turns) inform review. They never
change a score or decide an outcome on their own.

---

## 6. Timing and disconnection

- The server clock is the only clock that counts. Each section has a deadline derived from when it
  starts, and never extends past the test's closing time.
- A disconnection shorter than the allowed grace period is not penalised. Longer gaps get pause credit,
  capped at the configured maximum pause, and never past the closing time.
- When a section's time runs out, the section ends and its unsubmitted drafts are submitted. When the
  last section ends, the attempt is recorded as **time expired**.
- An attempt the candidate abandoned is finished by the `expire_attempts` cron job once its deadline
  passes, so its drafts are graded.

---

## 7. Proctoring, security and retention

- The camera is checked before the test starts. Snapshots are taken only if the candidate has
  consented on the readiness screen.
- Events (visibility changes, face checks, connection changes) are recorded with server timestamps.
- Snapshots are stored outside the public web folder and are reviewed by staff.
- **Retention:** snapshots are kept for **60 days** by default. Staff can change this under **Settings**
  (1 to 365 days). The daily `purge_expired_snapshots` job deletes evidence older than the current
  setting and keeps a record in the audit log.

### Security controls

- **One browser session per test.** The first action a candidate takes binds the test to that browser
  session. Opening the same test from another browser or session is refused with a notice, and the
  attempt is flagged. Answers already saved are kept. Each refused session is recorded in the event
  log, at most once every five minutes.
- **Allowing a resume.** Only staff can let a candidate continue in a new browser. On the attempt
  review page, after confirming who the candidate is, staff press **Allow resume in this browser**.
  The action is recorded in the audit log. A password resend signs out existing sessions, so a
  candidate who resends mid-test needs a resume allowed before they can continue.
- **Full screen.** A test can require full screen (on by default for new tests; staff can turn it off
  per test). Whenever the candidate leaves full screen, the test is covered by a "Return to full
  screen" prompt. The timer keeps running. Each exit is recorded and counts toward the flag threshold
  shared with tab switches.
- **Copy and paste.** Copy, cut and paste actions are recorded with their length only. The text is
  never sent to the server. A paste of 200 characters or more is flagged.
- **Sign-in throttling.** After five wrong passwords for one email address within 15 minutes, sign-in
  for that address is refused until the window passes, even with the right password. One IP address
  is limited to 30 failures in the same window. A correct sign-in clears the email's count.
- **Session length.** Candidate sessions end after 12 hours. The session cookie is not readable by
  page scripts.
- **Run uses the same rules as Submit.** Run is refused after the section closes, for a locked
  problem, or from a session that is not the bound one. Run still stores nothing.

---

## 8. Emails

The platform sends one kind of email: the **login details** email (section 3). Nothing else is sent.
There are no verification links, invitations or decision emails. Each send is recorded in the audit
log. Email links are built from `SITE_URL`.

---

## 9. Edge cases

| Situation | What happens |
|---|---|
| Candidate on the list, test still a draft | Not visible to the candidate |
| Test unpublished mid-way | Candidates cannot see it. Attempts already started are not cut off by the page change; staff should avoid this. |
| Access removed while a test is running | Their next action is refused. Answers already saved remain. |
| Access removed, then restored | The candidate can continue where they left off, provided the test is still open. |
| Login email fails | The candidate keeps their current password. Staff are told to check the mail settings. |
| Candidate resends, old password still in use | The old password stops working immediately. |
| Candidate cannot find the email | Staff press **Resend login email**. |
| Candidate has not confirmed details | Starting is blocked until they do. |
| Bulk send with more than 50 waiting | Each click sends 50. Press again for the rest. |
| Same email imported twice | One entry, no duplicate |
| Browser closed mid-test | The timer keeps running on the server; the candidate can return until the section ends |
| Test closes before a candidate starts | Shown as **Closed** |
| Code runner unavailable | Submissions are marked as runner errors, not wrong answers, and staff can review them |
| Draft saved after a problem is submitted | Refused: the problem is locked |
| Candidate's browser session changes mid-test | The second session is refused and flagged. Staff allow the resume after checking identity. |
| Password resent mid-test | Existing sessions are signed out. Staff allow the resume so the candidate can continue. |
| Full screen left during a test | Prompt shown until full screen is re-entered. Exit recorded. |
| Too many wrong passwords | Sign-in for that email is refused for 15 minutes, then clears |
| Reviewer on their own attempt | Cannot record a decision |

---

## 10. Data kept

| Data | Where | Kept until |
|---|---|---|
| Candidate account (email, name, phone) | Database | Until deleted by staff |
| Passwords | Database, hashed only | Replaced on each resend |
| Answers, drafts, submissions, marks | Database | Retained with the attempt |
| Proctoring events | Database | Retained with the attempt |
| Snapshot images | Private folder outside the web root | 60 days by default (Settings) |
| Audit log | Database | Retained |
| Browser session link (hashed session key only) | Database | Until the attempt finishes or staff allow a resume |
| Clipboard activity (lengths only, never text) | Database | Retained with the attempt |

---

## 11. Operations

Environment: `SITE_URL` (public address), `EMAIL_*` (mail server), `CODE_RUNNER` and `JUDGE0_URL`
(code runner), and `PRIVATE_MEDIA_ROOT` (evidence folder outside the web root). See `.env.example`.

After deploying this change, run `python manage.py migrate` (migration `0002_exam_security`) and
restart the application.

Cron jobs:

- Every minute: `expire_attempts`, then `grade_queued`.
- Daily: `purge_expired_snapshots`.

Before candidates use the platform, staff should send a login email to themselves, sign in with it,
and check the test page's pre-publish checklist is fully ticked.

---

## 12. Known limits

- Proctoring runs in the candidate's browser. It is evidence to review, not an automatic decision.
- The face detection model is replaced by a stub in the automated tests, so the tests check the wiring,
  not real detection accuracy.
- The platform has not been load tested at the target size. Run `loadtest/` on a staging server first.
- Local development uses a non-isolated code runner. Use the Judge0 runner for real candidates.
- A web page cannot force full screen or stop a candidate leaving it, and it cannot see other windows
  or devices. Full-screen exits and tab switches are evidence for staff to review, not proof.
- Copy and paste are seen only inside the test page. A second camera, or a phone photographing the
  screen, is not detected.
- A candidate who uses a second device can continue there only after staff allow the resume.
- Sign-in throttling is kept in the server's cache. Each server process keeps its own count, so on
  hosts that run several processes the limit is weaker. Use a shared cache for a stricter limit.
- The full-screen requirement can be turned off per test. Turn it off only for tests that do not need it.
