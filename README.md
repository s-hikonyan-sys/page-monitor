# page-monitor

Automated listing monitor with a multi-phase AI pipeline (Gemini + Playwright).

## What it does

1. **Scrape** paginated listing pages (Playwright; site-specific parse config via secrets)
2. **Screen** new items with Gemini (batch JSON phases)
3. **Analyze** passed items (risk / effort estimates, delivery wording)
4. **Generate & revise** structured text outputs for qualified items
5. **Notify** via Slack at each stage; optional state persistence in the repo

Phases are driven entirely by environment secrets (prompts, field schemas, thresholds). Sensitive configuration lives in GitHub Secrets, not in this repository.

## Stack

- Python 3.11
- Playwright (Chromium)
- Google Gemini API (`google-genai`)
- GitHub Actions (scheduled + manual `workflow_dispatch`)

## Local setup

```bash
pip install .
python -m playwright install chromium
# Set env vars (see private/INDEX.md locally) then:
python monitor.py
```

## Manual workflow inputs

| Input | Purpose |
|-------|---------|
| `persist_state` | Commit `seen_ids.txt` / `api_state.json` after run |
| `ignore_seen_ids` | Treat all scraped IDs as new (full pipeline test; skips seed) |

## Repository layout

| Path | Description |
|------|-------------|
| `monitor.py` | Main pipeline |
| `.github/workflows/` | CI runner |
| `private/` | Local secret templates (gitignored) |

## License

See repository default license if present.
