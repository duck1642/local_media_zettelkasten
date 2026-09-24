# Contributing

Thanks for your interest in Local Media Zettelkasten (LMZ).

Contributions are welcome for bug fixes, documentation, tests, and focused improvements that fit the current local-first architecture.

## Before You Start

- Check existing issues before opening a new one.
- Open an issue before starting a large feature or architectural change.
- Keep changes focused and explain user-facing behavior clearly.
- Never commit personal media, vault data, credentials, cookies, tokens, sensitive logs, model files, or generated builds.

## Development Setup

From the repository root in PowerShell:

```powershell
python -m pip install --upgrade pip
python -m pip install -e ".[windows,tauri,dev]"

cd frontend
npm install
npm exec playwright install chromium
cd ..
```

The commands above use the selected global Python 3.13+ interpreter. On Linux or macOS, replace the Python extras with `.[unix,tauri,dev]` and install the platform-specific Tauri dependencies.

## Local Checks

Run the readiness report:

```powershell
python tools\maintenance\lmz_readiness_check.py --non-interactive
```

Run backend tests:

```powershell
python -m pytest tests\backend
```

Run frontend checks and tests:

```powershell
cd frontend
npm run check
npm run build
npm run test:mock-vault
npm run test:playwright
cd ..
```

Before running frontend Playwright tests or the sidecar build, make sure the global Python 3.13+ interpreter is the active `python` on `PATH`; those npm scripts call `python` internally.

## Issues and Pull Requests

- For bugs, include reproduction steps, expected behavior, actual behavior, and relevant environment details.
- Remove credentials and personal paths from logs before sharing them.
- Keep pull requests focused and describe what changed, why, and which checks were run.
- Update the documentation when user-facing behavior changes.
- Do not include unrelated formatting changes or generated files.
