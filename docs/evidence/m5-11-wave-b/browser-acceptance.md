# M5.11 Wave B browser acceptance evidence

Local-only verification on the isolated Compose stack using anonymous placeholder tokens (`EXAMPLE_ADMIN_TOKEN` / `EXAMPLE_WORKER_TOKEN`). No real credentials or production data were used.

## AO Browser

- Opened `http://localhost:8890/` with `ao preview` / `ao browser`, logged in, opened the Adapter, execution history, and log tabs.
- `ao browser errors --json`: no page errors.
- `ao browser console --json`: only the existing informational i18next message; no application error.

## Playwright live log matrix

Each case used a separate anonymous Task Adapter fixture so the Worker could
keep the Execution active while the browser paused and resumed. The 1440x820
zh-CN case also expanded the AI panel before maximizing the log pane; no AI
request or real provider credential was used.

| locale | viewport | DPR | pause | new count | resume | 2000-line browser window | maximize/Escape | AI + maximize | overflow | failed/unknown |
|---|---:|---:|---|---|---|---|---|---|---|---:|
| zh-CN | 1280x720 | 1 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| zh-CN | 1440x820 | 1.25 | True | True | True | True | True | True / True | document/body=False/False | 0/0 |
| zh-CN | 1680x900 | 1 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| zh-CN | 1920x1080 | 1.25 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| en | 1280x720 | 1 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| en | 1440x820 | 1.25 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| en | 1680x900 | 1 | True | True | True | True | True | — | document/body=False/False | 0/0 |
| en | 1920x1080 | 1.25 | True | True | True | True | True | — | document/body=False/False | 0/0 |

Screenshots: `live-final-{zh-CN,en}-{1280,1440,1680,1920}.png`.

## Playwright history matrix

| locale | viewport | DPR | search | match count | no follow controls | copy/download | maximize/Escape | overflow | failed/unknown |
|---|---:|---:|---|---|---|---|---|---|---:|
| zh-CN | 1280x720 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| zh-CN | 1440x900 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| zh-CN | 1680x900 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| zh-CN | 1920x900 | 1.25 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| en | 1280x720 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| en | 1440x900 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| en | 1680x900 | 1 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |
| en | 1920x900 | 1.25 | True | True | True | True / `execution-2.log` | True / True | document/body=False/False | 0/0 |

Screenshots: `history-{zh-CN,en}-{1280,1440,1680,1920}.png`.

## Server-saved truncation contract

Execution `1` was opened in history, not the live browser window: `warningVisible=True`, `contentCharacters=1048544`, `headPresent=True`, `markerPresent=True`, `tailPresent=True`. The saved content remains accessible at the server-defined cap and the UI labels the permanent server truncation separately from the 2000-line browser render window. See [`history-truncation-report.json`](history-truncation-report.json).

## Raw reports

- [`live-report.json`](live-report.json) and [`live-matrix-report.json`](live-matrix-report.json)
- [`browser-report.json`](browser-report.json)
- [`history-truncation-report.json`](history-truncation-report.json)

All Playwright cases reported `failedRequests=[]`, `pageErrors=[]`, and `unknownPathnames=[]`; the only console entries were informational i18next notices.
