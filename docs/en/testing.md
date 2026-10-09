# Testing

Backend: ONE pytest process (~800 tests); external boundaries are
mocked (MAX test-fake, local Bot API mock, injected GitHub HTTP,
tmp_path FS). Frontend: vitest, tsc, production build. Playwright
projects desktop-1440x900 / mobile-375x812 — run SEQUENTIALLY if
they share Xray port 10833. Pinned regressions: echo suppression
without visible markers, pair-code listener readiness,
cancellation-safe Xray reap, update SSRF/SHA rules, boot
orchestration, wheel VERSION.
