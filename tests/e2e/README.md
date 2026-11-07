# Real browser verification

> Ручные браузерные сценарии (без ассертов pytest) переехали в
> `tools/dev/playwright/` — они полезны при отладке, но не являются тестами
> и не должны попадать в `pytest`. Здесь остаются только `test_*.py`.


Drivers use Playwright with the existing `/usr/bin/chromium`, headless, at
1440 × 1000. They click rendered controls and navigate normal pages; no injected
application state or authentication shortcuts. The second user gets a fresh
browser context without copied cookies. Intermediate artifacts, session storage
and screenshots live under `/tmp/ui-verify` (do not publish session files).

From `app`, start the web process:

```sh
APP_PORT=8091 PUBLIC_BASE_URL=http://127.0.0.1:8091 LLM_PROVIDER= LLM_API_KEY_ENV= PYTHONPATH=src .venv/bin/python main.py
```

Start a worker in another terminal, stopping that process after the suite:

```sh
PYTHONPATH=src .venv/bin/python -m change_analysis.worker.main
```

Run:

```sh
.venv/bin/python -u tools/dev/playwright/ui_suite.py
```

Install Playwright if missing (`.venv/bin/pip install playwright`). The drivers
use the already installed system Chromium; a Playwright browser download is
unnecessary. They assume the provided analyst accounts and the original finished
2002-pair run named «Полный прогон». The suite deliberately creates test records;
it does not delete or reset existing data. The run may take several minutes on
a busy database. Each driver can also be run separately after its predecessors.
`tools/dev/playwright/ui_final.py` is read-only and checks the artifacts created by previous drivers.

The final assertion currently reproduces a service bug: after a checks-only job,
`runs.revision` advances, while pivot facts keep their earlier revision. A failing
suite is expected for this regression; it is not an all-green acceptance claim.
See `docs/agent_reports/ui-verify.md` for observed results and exact limitations.
