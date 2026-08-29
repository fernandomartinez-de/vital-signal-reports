# tracker/

Holds `master_tracker.xlsx` — a standing inventory of everything in the Google
Drive "Medical" folder, one tab per year (grouped by each document's real
resolved date, independent of whatever Drive folder it's actually sitting in
— the cleaner renames files but no longer moves them), each row tagged with
its category, plus a `Resumen` summary tab and a `Needs Attention` tab.

`clean_medical_drive.py` only renames files in place — folder placement
(which `{year}/{category}/` folder a file lives in) is a human job. If it
can't confidently work out a file's date and/or category, even after
checking which folder the file is currently sitting in, it leaves that file
completely untouched and lists it on `Needs Attention` instead, so whoever's
organizing the Drive folder can see it and fix the name/placement by hand —
the next run picks it up cleanly once that's done. The only thing this
script still moves automatically is a confirmed duplicate (identical
content), which goes into `Medical/_REVISAR` prefixed `DUP_`; that's the one
case still listed on `Needs Attention` after being moved rather than left in
place.

This file is generated and committed automatically by
`clean_medical_drive.py` every time it runs — dry run or `--apply` — via the
`clean-medical-drive.yml` workflow (nightly dry run at 7am UTC, plus manual
runs). Don't hand-edit `master_tracker.xlsx`; the next run overwrites it
from scratch.

Every run also uploads/updates a loose copy of this same file directly in the
Medical Drive folder root, next to the year folders — so it's visible from
Drive itself without needing to go find it in this repo. That copy is purely
informational; the script never reads it back or treats it as a source of
truth.
