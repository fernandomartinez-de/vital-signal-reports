# tracker/

Holds `master_tracker.xlsx` — a standing inventory of everything in the Google
Drive "Medical" folder, one tab per year (grouped by each document's real
resolved date, independent of whatever Drive folder it's actually sitting in
— the cleaner renames files but no longer moves confidently-resolved ones),
each row tagged with its category, plus a `Resumen` summary tab and a
`_REVISAR` tab.

`clean_medical_drive.py` only renames confidently-resolved files in place —
folder placement (which `{year}/{category}/` folder a file lives in) is a
human job, and the script trusts a file's current folder as a fallback
category signal when the document content itself is unclear. If it can't
confidently work out a file's date and/or category, even after that folder
fallback, it moves the file into `Medical/_REVISAR` (prefixed `REVISAR_`) for
a human to sort out by hand and check the folder daily — files inside
`_REVISAR` are skipped on every subsequent scan, so moving one back out is
what puts it back in play. Confirmed duplicates (identical content) go into
`Medical/_REVISAR` too, prefixed `DUP_` instead.

This file is generated and committed automatically by
`clean_medical_drive.py` every time it runs — dry run or `--apply` — via the
`clean-medical-drive.yml` workflow, which now runs fully unattended every
night at 7am UTC WITH `--apply` (no manual click needed), plus manual runs
that default to a dry run unless "apply" is checked. Don't hand-edit
`master_tracker.xlsx`; the next run overwrites it from scratch.

Every run also uploads/updates a loose copy of this same file directly in the
Medical Drive folder root, next to the year folders — so it's visible from
Drive itself without needing to go find it in this repo. That copy is purely
informational; the script never reads it back or treats it as a source of
truth.
