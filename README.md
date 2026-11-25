# SC AI

Standalone SC Control Center and compliance assessment workspace.

## Start / stop on Windows

Run from this folder in PowerShell:

```powershell
.\Start-SC.ps1
# http://127.0.0.1:5180
.\Stop-SC.ps1
```

- Web: SC-Project-main/ (Node.js 22.5+), port 5180.
- Assessment API: platform/ with its own platform/.venv/, port 8001.
- Logs and process IDs: .runtime/.
- SC database and files: SC-Project-main/data/.
- Assessment database: platform/backend/compliance.db.

The start script uses only the local Python environment. No other project needs to be running or installed.

## First-time installation on another machine

```powershell
python -m venv platform/.venv
.\platform\.venv\Scripts\python.exe -m pip install -r platform/requirements.lock.txt
npm.cmd --prefix SC-Project-main install
```

Local .env files are already configured on this machine. On another machine, use
SC-Project-main/.env.experiment.example and platform/.env.example as starting points;
set COMPLIANCE_API_KEY and APP_API_KEY to the same randomly generated value.
Never overwrite existing .env files during reinstallation.

## Configuration

- Web configuration: SC-Project-main/.env.
- Assessment and inference configuration: platform/.env.
- The generated initial admin password is SC_EXPERIMENTAL_ADMIN_PASSWORD in the web .env.
- The expired inference key was cleared. Set AI_API_KEY to a new key; update AI_BASE_URL if the inference URL changes. Restart SC after editing the configuration.
- APP_API_KEY / COMPLIANCE_API_KEY protect the internal API bridge; these are separate from the inference key.
- HTTP AI requires a running inference server; the website itself runs locally.
- Codex has a separate account home at platform/.runtime/codex-assessor. Sign in to this home before using Codex mode; no credentials or account state are shared with the test project.

Example dedicated Codex sign-in, in a separate PowerShell window:

```powershell
$env:CODEX_HOME = Join-Path (Get-Location) 'platform/.runtime/codex-assessor'
& 'C:/Users/PC-1/AppData/Local/Programs/OpenAI/Codex/bin/codex.exe' -c 'cli_auth_credentials_store="file"' login
```

## Compliance Assessment

Choose a project and import/select a checklist, then create its assessment.
Map questions or short answer notes to controls, review the proposals, apply selected answers to the draft,
and save before running the assessment. The workflow supports configured-provider/Codex assessment,
progress, cancellation and Excel export. Mapping previews do not save answers automatically.
Original test-project assessment records were not imported.

## Verification

```powershell
.\scripts\Test-SC.ps1
```
