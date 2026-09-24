# Local Media Zettelkasten

Local Media Zettelkasten (LMZ) is a local-first desktop application for collecting, organizing, reviewing, and searching personal image and video libraries.

LMZ keeps media on the local machine, stores durable metadata in SQLite and Markdown, and provides a Tauri/Svelte desktop interface backed by a local FastAPI service.

Current version: `1.0.0`
Primary target: Windows desktop

## Highlights

- Import local files and folders through the desktop interface.
- Download media from supported URLs through `gallery-dl` and `yt-dlp`.
- Detect exact and similar duplicates before destructive decisions are made.
- Review uncertain matches before saving a candidate as a variant, deleting it, or replacing matching media.
- Organize libraries by artists, platforms, topics, tags, media type, and date.
- Search and browse vaults with filters, facets, grid/masonry views, and an inspector.
- Use optional local WD tagging with `SmilingWolf/wd-vit-tagger-v3`.
- Manage multiple workspaces and vaults, including audits, repairs, backups, exports, restores, and vault merges.
- Inspect startup, ingestion, review, and application logs from the UI.

LMZ also contains a browser-capture prototype. It is not part of the v1.0.0 release acceptance scope.

## Application Layout

| Area | Purpose |
| --- | --- |
| **Vault** | Browse, search, filter, inspect, and edit media. |
| **Ingestion** | Import local media or process online URL queues. |
| **Review** | Resolve possible duplicates and cleanup tasks. |
| **Stats** | Explore counts, artists, platforms, topics, and WD tags. |
| **Settings** | Manage configuration, workspaces, vaults, maintenance, and shortcuts. |
| **App Logs** | Inspect startup, active-vault, and raw console logs. |

## Requirements

- Python 3.13 or newer
- Node.js 20.19+ or 22.12+ and npm
- Rust and Cargo; Tauri currently requires Rust 1.77.2 or newer
- FFmpeg and FFprobe on `PATH`
- `fpcalc` for audio fingerprinting
- SQLite
- Windows WebView2 and the Microsoft C++ build tools for desktop development

The project installs `gallery-dl`, `yt-dlp`, and the required Python packages. Windows uses `python-magic-bin`; Linux and macOS use `python-magic`.

## Installation

From PowerShell in the repository root:

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[windows,tauri,dev]"

cd frontend
npm install
npm exec playwright install chromium
cd ..
```

The commands above use the selected global Python 3.13+ interpreter. On Linux or macOS, replace the Python extras with `.[unix,tauri,dev]` and install the platform-specific Tauri packages.

## Quick Start

Run the Python/media readiness report before launching:

```powershell
python tools\maintenance\lmz_readiness_check.py --non-interactive
```

This report does not replace checking Node.js/npm, Rust/Cargo, Windows WebView2, or the Microsoft C++ build tools required for the desktop build.

Start the development application:

```powershell
python dev.py
```

This starts the local FastAPI backend on `127.0.0.1:8000`, starts Vite, and opens the Tauri desktop window. Stop the development session with `Ctrl+C` or by closing the application.

## First Run

1. Select the built-in **Default** workspace, or create an external workspace.
2. Select or create a vault.
3. Review processing and WD-tagging options under **Settings > General**.
4. Check downloader credentials under **Settings > Maintenance > Auth**.
5. Add files under **Ingestion > Local**, or add URLs under **Ingestion > Online**.

WD tagging may download `SmilingWolf/wd-vit-tagger-v3` the first time it is used. Video similarity fingerprinting may separately fetch `clip-ViT-B-32` when it is not cached. Disabling WD tagging prevents only the WD model download.

## Core Workflows

### Local and online ingestion

Local files and folders can be added from **Ingestion > Local** or by dragging them into the application. LMZ validates media, computes fingerprints, stores accepted items, writes notes, creates thumbnails, and updates indexes.

Online URLs are processed from **Ingestion > Online** through Normal, Force, and Failed queues. LMZ currently routes Pixiv, Pinterest/`pin.it`, Instagram, X/Twitter, and YouTube/`youtu.be` to `gallery-dl` or `yt-dlp`. Success depends on downloader versions, source-site changes, and valid authentication where required.

### Duplicate review

Exact duplicates are rejected during ingestion. Similar or uncertain matches are sent to **Review**, where the original and candidate media can be compared before an action is chosen.

### Search and browsing

Search supports plain text and metadata filters such as:

```text
landscape
a:artist name
p:Pixiv
t:reference
#blue_hair
a:artist name; p:Pixiv; #portrait
```

Use `/` in the search bar to discover layout, sorting, media, authentication, and maintenance commands.

## Workspaces and Vaults

A **workspace** is an independent LMZ library root. A **vault** is a media collection inside that workspace.

The default paths are:

```text
%USERPROFILE%\.lmz\app\       application settings, credentials, and models
%USERPROFILE%\.lmz\default\   default workspace
```

External workspaces are created as an `lmz` directory beneath a selected parent folder. LMZ does not relocate their files automatically. Media assets, Markdown notes, databases, thumbnails, review data, queues, and logs are managed inside the selected workspace.

Workspace deletion has guarded modes: unregister only, delete generated data, or delete the entire managed workspace. Vault deletion is separate: only inactive vaults inside the workspace can be deleted, and non-empty vaults require confirmation. Independent backups are still recommended before destructive operations.

Useful environment overrides:

- `LMZ_DATA_ROOT` changes the application data root.
- `LMZ_CONFIG_PATH` selects the workspace configuration file at startup.
- `LMZ_AUTH_ROOT` changes the downloader credential directory.
- `LMZ_DISABLE_RELOAD=1` disables reload behavior for direct backend launches.

## Authentication

Downloader credentials are application-scoped, not workspace-scoped. By default they are stored under:

```text
%USERPROFILE%\.lmz\app\secrets\auth\
|-- x/cookies.txt
|-- instagram/cookies.txt
|-- pinterest/cookies.txt
|-- youtube/cookies.txt
`-- pixiv/refresh_token.txt or cookies.txt
```

Cookie files must use Netscape cookie-file format. Pixiv OAuth refresh tokens are preferred, with cookies available as a fallback.

See the [Downloader Authentication Guide](docs/auth_guide.md) for setup details. Credentials grant account access; never commit, share, screenshot, or include them in support logs.

## Development

Run the backend directly from the repository root:

```powershell
cd backend
python web_api.py
```

Run frontend checks and builds from the repository root:

```powershell
cd frontend
npm run check
npm run build
cd ..
```

Run backend tests from the repository root:

```powershell
python -m pytest tests\backend
```

Before running frontend Playwright tests or the sidecar build, make sure the global Python 3.13+ interpreter is the active `python` on `PATH`; those npm scripts call `python` internally.

Run frontend checks and tests from the repository root:

```powershell
cd frontend
npm run test:mock-vault
npm run test:playwright
cd ..
```

From the repository root, build the production desktop application:

```powershell
cd frontend
npm run build:sidecar
npm run tauri build
```

From the repository root, the direct pipeline entry point and installed CLI command remain available:

```powershell
python main.py
lmz
```

The second command is available after the editable global installation. For normal interactive use, prefer `python dev.py` and the desktop interface.

## Repository Map

```text
backend/                  FastAPI, ingestion, storage, maintenance, and downloaders
frontend/                 Svelte UI and Tauri desktop shell
tests/                    Backend and Playwright regression coverage
tools/maintenance/        Readiness, build, fixture, and maintenance utilities
tools/browser_extension/  Browser capture prototype
docs/                     User and developer guides
```

Runtime data, credentials, models, logs, backups, exports, and generated build artifacts live outside the source tree or are excluded from version control.

## Privacy and Data Safety

- LMZ has no required hosted library service; library files and databases stay local.
- Online ingestion sends requests to the source platform through the configured downloader.
- WD tagging may download `SmilingWolf/wd-vit-tagger-v3`; video similarity fingerprinting may separately fetch `clip-ViT-B-32` when it is not cached.
- Backups, exports, logs, and browser-capture caches may contain sensitive paths or metadata.
- Confirmation dialogs and path guards reduce accidental damage but do not replace independent backups.

**Development note:** This project was human-directed and developed with substantial AI coding assistance. Product direction, design decisions, code review, and release preparation were led by me.
