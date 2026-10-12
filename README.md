# Waddle

![Waddle logo](web/assets/waddle-horizontal-colour.png)

Waddle is a Windows desktop workspace for understanding how a website appears in ordinary Google Search AI Overviews. It records search evidence, shows which pages and domains are cited, inventories the owner’s website, and turns observed opportunities into human-reviewable page briefs and tasks.

## What Waddle does

### Monitor search visibility

Create a project for a website, define tracked search phrases, and collect Google Search AI Overview results for a selected country, language and device. Each completed check preserves the Overview summary, citation URLs, passage/reference mappings when supplied, provider diagnostics and a screenshot. Capture history makes changes visible over time; failed or incomplete checks remain clearly separate from usable evidence.

### Investigate citations and competitors

Explore cited pages, recurring cited domains, related questions, competitor domains and citation opportunities from saved evidence. Waddle distinguishes owned pages, competitors and other cited sources, keeps the evidence window explicit, and exports query or citation data as CSV. It does not treat a citation gap as proof of a ranking cause or a guarantee of future inclusion.

### Understand the website behind the evidence

The built-in crawler discovers same-domain pages through sitemaps and internal links, then records titles, descriptions, headings, readable content, word counts, canonicals, robots/snippet restrictions, links, dates and JSON-LD observations. Crawls can be paused, resumed and retried for blocked or unavailable pages. Public HTTP(S) boundaries, robots rules, redirects, response sizes and concurrency are guarded in the crawler; JavaScript is not executed and CAPTCHAs are not bypassed.

### Turn evidence into improvements

Plan queries against suggested or manually selected pages, compare an owned page with selected cited pages, and create a local AI review when the required evidence is available. Accepted recommendations can become editable briefs, writing or technical tasks, and deployment follow-ups. Reviews are suggestions grounded in saved evidence, not automatic website changes or predicted rankings.

### Keep work local and recoverable

The application runs on the local machine and stores its workspace in SQLite. Changes are saved immediately, automatic backups protect the workspace, and credentials are protected with Windows DPAPI. Local page reviews use Ollama; Waddle does not send review content to a cloud AI model.

## Tech stack

| Area | Technology |
| --- | --- |
| Application | Python 3.10+ using the standard library |
| Local web server | Python `http.server` with a threaded local server |
| UI | Plain HTML, CSS and JavaScript; no frontend build system |
| Persistence | SQLite via Python `sqlite3` |
| Website fetching | Python networking plus `curl_cffi==0.16.3` for browser-compatible HTTP fallback |
| Search collection | Bright Data SERP API, configured from Waddle Settings |
| Local AI | Ollama over loopback HTTP, optional |
| Launcher | Windows PowerShell (`Launch.ps1`) and CMD wrapper (`Launch.cmd`) |
| Fonts and brand assets | Plus Jakarta Sans, Waddle logo and mascot PNG assets |

There is no Node.js, npm, Docker, database server or compilation step required to run the released application.

## Requirements

### Required to launch the application

- Windows 10 or later
- Python 3.10 or later available as `python` or `python.exe`
- Permission to run a local application and open `http://127.0.0.1:8765/`
- Internet access only for the website and provider operations you choose to run

### Required for live Google AI Overview checks

- A Bright Data account
- An accessible Bright Data SERP zone
- The relevant Bright Data credentials entered in Waddle Settings

Without Bright Data access, the local interface, saved workspace, website crawler and existing evidence can still be used, but new provider-backed checks cannot complete.

### Optional for local page reviews

- [Ollama](https://ollama.com/) installed and running locally
- An installed Ollama text model with enough context for the selected evidence

Ollama is not needed for search collection, crawling, citation exploration or ordinary project setup.

## Install and run

1. Download or clone this repository.
2. Open PowerShell in the repository folder.
3. Create a virtual environment and install the pinned runtime dependency:

   ```powershell
   python -m venv .venv
   .venv\Scripts\python.exe -m pip install -r requirements.txt
   ```

4. Start Waddle by double-clicking `Launch.cmd`, or run:

   ```powershell
   .\Launch.ps1
   ```

The launcher prefers `.venv\Scripts\pythonw.exe` when present, falls back to Python on PATH, starts the local server in the background, and opens the browser. Waddle is available at `http://127.0.0.1:8765/`.

To run it without opening a browser, use:

```powershell
.venv\Scripts\python.exe app.py --no-browser
```

The server also accepts `--port PORT` and `--data-dir PATH` for development or isolated workspaces.

## First use

1. Create a project and enter the website domain.
2. Add search phrases one per line, or create the project and add them later.
3. Open **Settings** and configure/test the Bright Data connection.
4. Run one selected search first so you can confirm the connection and evidence format.
5. Start a website crawl from **Website Pages** when you want owned-page inventory.
6. Configure Ollama under **Settings → Local AI — Ollama** only if you want local page reviews.

Creating a project does not automatically run a search, crawl or AI generation. Provider requests and retrieval actions are explicit.

## Data, privacy and limits

The default workspace is stored at:

```text
%LOCALAPPDATA%\AIOverviewTool\workspace.sqlite3
```

Waddle keeps this directory name so existing workspaces remain available. Moving the repository does not move or delete the workspace. Backups are created automatically in the same workspace directory. Credentials use Windows DPAPI and cannot be decrypted by another Windows user.

Waddle collects only when you request a check or retrieval. Provider credentials and raw provider error bodies are not included in ordinary evidence records. Website fetching is limited to public HTTP(S) URLs and remains subject to the target website’s availability and access controls. A robots permission does not guarantee that a CDN will allow automated access.

This release tracks ordinary Google Search AI Overviews. Google AI Mode and other assistants are separate products and are not represented as equivalent evidence.

## Troubleshooting

**Python is not found** — install Python 3.10 or later, enable “Add Python to PATH” during installation, then reopen PowerShell.

**The browser does not open** — browse manually to `http://127.0.0.1:8765/`. If the port is busy, stop the other local service or run `python app.py --port 8766` and open port 8766 directly.

**The Bright Data test fails** — confirm the credentials, that the account has an accessible SERP zone, and that the machine can reach the provider. Waddle does not silently substitute another paid provider.

**A crawl reports blocked pages** — the site, CDN, robots policy or a CAPTCHA/security layer may have denied access. Waddle records that outcome instead of pretending the page was absent.

**Local AI reviews fail** — confirm Ollama is running on loopback, the selected model is installed, and the evidence fits the model’s available context. A failed review does not create partial recommendations.

## Development checks

The runtime distribution has no build step. From a checkout that also contains the development tests, use:

```powershell
.venv\Scripts\python.exe -m unittest discover -s tests -v
node --check web/app.js
node --check web/pages.js
node --check web/strategy.js
node --check web/improvements.js
```

The `tests/` directory and development documents are intentionally not required for an end user to install and run Waddle.

## Licence

No licence has been declared yet. Add an appropriate licence before redistributing Waddle.
