# Load testing (k6)

`k6_assessment.js` drives the candidate flow the way a real browser does: log in, start,
camera-consent, answer MCQs, visit questions, poll state, send occasional events,
submit code, and report latencies. Each virtual user is a different candidate.

## Prepare (staging only, throwaway data)

```bash
DJANGO_DEBUG=1 CODE_RUNNER=local .venv/bin/python manage.py seed_demo --candidates 1000
```

`seed_demo` refuses to run unless `DJANGO_DEBUG=1`, because it creates accounts with a known password.
Note the printed exam ID.

## Stages (implementation plan, section 19)

| Stage | Virtual users | Command option |
| --- | --- | --- |
| Smoke | 10 | `-e VUS=10` |
| Early load | 50 | `-e VUS=50` |
| Intermediate | 200 | `-e VUS=200` |
| High load | 500 | `-e VUS=500` |
| Target | 1000 | `-e VUS=1000` |

```bash
k6 run -e BASE=https://staging.example.test -e EXAM_ID=1 -e VUS=10 -e CANDIDATES=1000 loadtest/k6_assessment.js
```

The burst test (1,000 users joining at once) is `-e VUS=1000` with `HOLD=30s`.

## What this does not cover

API load does not reproduce 1,000 real webcams running face detection. Test real browsers
separately on the target devices and networks.

## Acceptance thresholds in the script

- Failed HTTP requests under 1%.
- p95 latency under 800 ms for state and answer saves.
- p95 latency under 5 s for code submissions.
