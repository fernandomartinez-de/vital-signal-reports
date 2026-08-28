# vital-signal-reports

Automated personal biometric dashboard pipeline. Two provider-facing HTML dashboards rebuild daily via GitHub Actions and are served live on GitHub Pages.

## How it fits together

Two independent GitHub repos feed one shared Supabase database. Blue = the other repo (`whoop-pipeline`), green = scripts in *this* repo, purple = the shared database.

```mermaid
flowchart LR
    W(["WHOOP wristband"]) --> WP["whoop-pipeline repo<br/>daily sync, 11am UTC"]
    WP --> DB[("Supabase")]

    U(["You + Mom upload files"]) --> GD[("Google Drive<br/>Medical folder")]
    GD <--> CL["clean_medical_drive.py<br/>manual, tidies filenames"]
    GD --> IN["ingest_labs_gdrive.py<br/>daily, extracts values"]
    IN --> DB

    DB --> BD["build_dashboards.py<br/>daily"]
    BD --> OUT(["Dashboards on GitHub Pages"])

    classDef repo fill:#dbeafe,stroke:#2563eb,color:#1e3a8a;
    classDef thisrepo fill:#dcfce7,stroke:#16a34a,color:#14532d;
    classDef db fill:#ede9fe,stroke:#7c3aed,color:#4c1d95;
    classDef ext fill:#f3f4f6,stroke:#6b7280,color:#111827;

    class WP repo;
    class CL,IN,BD thisrepo;
    class DB db;
    class W,U,OUT,GD ext;
```

**Two rows above, two jobs:**
- **Top row (WHOOP):** lives entirely in [`whoop-pipeline`](https://github.com/fernandomartinez-de/whoop-pipeline), a separate private repo. It syncs WHOOP → Supabase on its own daily schedule. Nothing here touches it.
- **Bottom row (labs/imaging):** you and your mom drop files into Drive. `clean_medical_drive.py` (run manually, dry-run by default) reads each file's actual content to fix its name and folder — it writes back into the same Drive folder, it doesn't hand files anywhere. `ingest_labs_gdrive.py` then runs automatically every night, trusting only correctly-named files, and inserts the extracted values into Supabase.

Once both rows have landed in Supabase, `build_dashboards.py` runs automatically every evening, rebuilds the two Spanish dashboards, and GitHub Pages serves the latest version to your doctors.

## Dashboards

| Dashboard | Audience | Language |
|---|---|---|
| `martinez_nutritionist_dashboard.html` | Nutriólogo (Javier) | Spanish |
| `martinez_oncologist_dashboard.html` | Oncóloga (Dra. Escobar) | Spanish |

Live at: `https://fernandomartinez-de.github.io/vital-signal-reports/`

## Automation (this repo)

| Workflow | Trigger | Does |
|---|---|---|
| `clean-medical-drive.yml` | Manual | Runs `clean_medical_drive.py`. Dry-run unless you check `apply`. Uploads `rename_log.csv` as an artifact. |
| `ingest-labs.yml` | Daily, 8am UTC | Runs `ingest_labs_gdrive.py`. |
| `rebuild-dashboards.yml` | Daily, 6pm UTC | Runs `build_dashboards.py`, commits the rebuilt dashboards. |

`whoop-pipeline` has its own workflow in its own repo.

## Stack

| Layer | Tool |
|---|---|
| Wearable sync | [`whoop-pipeline`](https://github.com/fernandomartinez-de/whoop-pipeline) — separate repo |
| Database | Supabase (PostgreSQL), shared by both repos |
| Lab/imaging source | Google Drive |
| Automation | GitHub Actions |
| Hosting | GitHub Pages |

## Setup

1. Clone the repo, `pip install -r requirements.txt`
2. Add repo secrets: `SUPABASE_DB_URL`, `ANTHROPIC_API_KEY`, `GDRIVE_CREDENTIALS` (service account JSON)
3. Give that service account **Editor** access on the Medical Drive folder — the cleaner needs to rename/move files
4. Enable GitHub Pages on `main`
5. Everything else runs automatically, except the cleaner, which you trigger manually

## Notes

Personal health monitoring project. Dashboard content is in Spanish, tailored to specific provider workflows. Data is private.
