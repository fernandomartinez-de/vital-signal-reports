# tracker/

Holds `master_tracker.xlsx` — a standing inventory of everything in the Google
Drive "Medical" folder, one tab per year (matching the Drive folder layout,
`{year}/{category}/...`), each row tagged with its category, plus a `Resumen`
summary tab and a `_REVISAR` tab listing anything quarantined, duplicated,
unreadable, or otherwise missing a resolved year.

This file is generated and committed automatically by
`clean_medical_drive.py` every time it runs — dry run or `--apply` — via the
`clean-medical-drive.yml` workflow. Don't hand-edit `master_tracker.xlsx`;
the next run overwrites it from scratch.

Every run also uploads/updates a loose copy of this same file directly in the
Medical Drive folder root, next to the year folders — so it's visible from
Drive itself without needing to go find it in this repo. That copy is purely
informational; the script never reads it back or treats it as a source of
truth.
