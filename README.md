# vital-signal-reports

Automated personal biometric dashboard pipeline built on WHOOP wearable data. Two provider-facing HTML dashboards rebuild daily via GitHub Actions and are served live on GitHub Pages.

## What it does

- Ingests biometric lab data from Google Drive via the Google Drive API
- Pulls WHOOP metrics (recovery, strain, sleep) from Supabase
- Builds two tailored HTML dashboards in Spanish: one for a nutritionist, one for an oncologist
- Publishes both dashboards automatically every day via a GitHub Actions scheduled workflow
- No manual intervention required after initial setup

## Dashboards

| Dashboard | Audience | Language |
|---|---|---|
| `martinez_nutritionist_dashboard.html` | Nutriólogo (Javier) | Spanish |
| `martinez_oncologist_dashboard.html` | Oncóloga (Dra. Escobar) | Spanish |

Both dashboards are live at:
```
https://fernandomartinez-de.github.io/vital-signal-reports/
```

## Stack

| Layer | Tool |
|---|---|
| Wearable data | WHOOP |
| Data storage | Supabase (PostgreSQL) |
| Lab data source | Google Drive |
| Ingestion | Python (ingest_labs_gdrive.py) |
| Dashboard build | Python (build_dashboards.py) |
| Automation | GitHub Actions (daily cron schedule) |
| Hosting | GitHub Pages |

## Pipeline flow

```
WHOOP → Supabase
Google Drive (lab results) → ingest_labs_gdrive.py
                                      ↓
                            build_dashboards.py
                                      ↓
                         HTML dashboards (Spanish)
                                      ↓
                    GitHub Actions → GitHub Pages (daily)
```

## Automation

The GitHub Actions workflow (`.github/workflows/rebuild-dashboards.yml`) runs on a daily schedule. It rebuilds both dashboards from the latest data and deploys them to GitHub Pages automatically.

## Setup

1. Clone the repo
2. Install dependencies: `pip install -r requirements.txt`
3. Configure the following secrets in your GitHub repository settings:
   - `SUPABASE_URL`
   - `SUPABASE_KEY`
   - `GDRIVE_CREDENTIALS` (Google service account JSON)
4. Enable GitHub Pages on the `main` branch
5. The workflow handles all subsequent rebuilds automatically

## Notes

This is a personal health monitoring project. Dashboard content is in Spanish and tailored to specific provider workflows. Data is private and sourced exclusively from personal wearable and lab integrations.
