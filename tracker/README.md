# tracker/

Holds `master_tracker.xlsx` — a standing inventory of everything in the Google
Drive "Medical" folder, one tab per category (`labs`, `radiologia`,
`ultrasonidos`, `inbody`, `patologia`), plus a `Resumen` summary tab and a
`_REVISAR` tab listing anything quarantined, duplicated, or unreadable.

This file is generated and committed automatically by
`clean_medical_drive.py` every time it runs — dry run or `--apply` — via the
`clean-medical-drive.yml` workflow. Don't hand-edit `master_tracker.xlsx`;
the next run overwrites it from scratch.
