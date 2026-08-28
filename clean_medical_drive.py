"""
Medical Drive folder cleaner.

Sweeps the whole Google Drive "Medical" tree, reads each PDF/image's actual
content (text layer, or Claude vision for scanned docs), and works out the
real collection date, category and provider — then plans a rename/move to
bring the file in line with the naming convention:

    Medical/{year}/{category}/YYYY-MM-DD_{category}_{provider}_{type}.ext

This exists because ingest_labs_gdrive.py trusts the filename blindly:
parse_filename() silently falls back to date.today() on a bad date, and the
ingest only picks up files with "_labs_" in the name. A messy filename is
invisible to the ingest and/or gets stamped with the wrong date. Running
this first keeps the folder — and the timeline — clean.

Safety rules (do not relax these):
  - Never deletes anything. Ever.
  - Ambiguous dates (both day and month <= 12, so D/M vs M/D genuinely
    can't be told apart from the text alone) are quarantined, not guessed.
    A previous script guessed and got a month wrong — see rename_log
    history / commit messages for the case this is guarding against.
  - Anything read with low confidence, or that fails to read at all, is
    quarantined rather than filed on a guess.
  - Duplicates (identical content hash) are quarantined, keeping whichever
    copy is already correctly named/placed (or the oldest, if neither is).
  - Dry-run by default. Nothing changes on Drive unless --apply is passed.
    Every run (dry-run or apply) writes rename_log.csv describing what it
    did or would do.
  - Every run also (re)writes a master tracker workbook at tracker/master_tracker.xlsx
    — one tab per category plus a Resumen tab and a _REVISAR tab — as a standing
    inventory of everything in the Medical folder. This happens on dry runs too,
    since building the tracker never touches Drive.

Auth: same service-account pattern as ingest_labs_gdrive.py, via the
GDRIVE_CREDENTIALS secret. Unlike the ingest (readonly), this needs the
service account to have Editor access on the Medical folder, because it
renames and moves files.
"""
import os, io, sys, csv, re, json, base64, hashlib, argparse
from datetime import date, datetime, timezone
from collections import defaultdict

from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import anthropic
import pdfplumber
from pdf2image import convert_from_bytes
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill
from openpyxl.utils import get_column_letter

# ── Config ────────────────────────────────────────────────────────────────────
GDRIVE_FOLDER_ID = "1_pB5M_-xqWU-jNYykK83fhZiWc5ldrS2"  # Medical/
SCOPES = ["https://www.googleapis.com/auth/drive"]  # needs write (Editor on the folder)
MODEL = "claude-sonnet-4-20250514"

# Ordered (not a set) so the tracker's tabs come out in a stable, sensible order.
CATEGORIES_ORDERED = ["labs", "radiologia", "ultrasonidos", "inbody", "patologia"]
CATEGORIES = set(CATEGORIES_ORDERED)
KNOWN_PROVIDERS = {"quest-mx", "quest-usa", "labcorp", "hospital-angeles-lomas"}
REVISAR_FOLDER_NAME = "_REVISAR"
DEFAULT_TRACKER_PATH = os.path.join("tracker", "master_tracker.xlsx")

MIN_TEXT_LEN_FOR_TEXT_LAYER = 40  # below this, treat the PDF as scanned/image-only

DATE_LABELS_HINT = (
    "Fecha Toma Muestra, Muestra Tomada, Fecha de Toma, Fecha de Recoleccion, "
    "Collected, Date Collected, Specimen Collected"
)

CLASSIFY_INSTRUCTIONS = f"""You are helping organize a personal medical records archive (thyroid cancer
monitoring: labs, ultrasounds, radiology, pathology/biopsy reports, and InBody
body-composition scans). You will be shown a medical document (as text or as
an image). Identify the following and return ONLY a valid JSON object, no
markdown, no explanation:

{{
  "fecha_encontrada": true or false,
  "fecha_texto": "the date exactly as printed, digits and separators only, e.g. '17/04/2026' — do NOT reorder or reformat it, do NOT guess which number is the day vs month",
  "fecha_label": "which label it was next to, e.g. 'Fecha Toma Muestra'",
  "categoria": one of "labs", "radiologia", "ultrasonidos", "inbody", "patologia", or null if you cannot tell confidently,
  "categoria_confianza": "alta" or "baja",
  "proveedor": a short kebab-case slug for the lab/clinic/hospital that issued the document (use "quest-mx" for Quest Diagnostics Mexico, "quest-usa" for Quest Diagnostics USA, "labcorp" for LabCorp, "hospital-angeles-lomas" for Hospital Angeles Lomas; otherwise invent a short kebab-case slug from the letterhead, e.g. "clinica-san-jose"), or null if you truly cannot tell,
  "tipo": a short kebab-case slug (1-3 words) for what this document is, e.g. "tiroideo", "biometria", "general", "tiroglobulina", "perfil-lipidico", "ultrasonido-tiroides", "biopsia-tiroides", "torax", "otro",
  "notas": "anything odd worth a human's attention, or empty string"
}}

Rules for fecha_texto specifically:
- Find the SPECIMEN COLLECTION date, not the birth date, report-creation date,
  print date, or release date. Look for labels like: {DATE_LABELS_HINT}.
- Copy the date digits and separators EXACTLY as printed (e.g. "05/03/2026").
  Do not convert it, do not decide whether it's day-first or month-first —
  that decision is made deliberately outside this step, by a human-reviewed
  rule, because a past automated guess got the month wrong.
- If no such date is findable, set fecha_encontrada to false and fecha_texto to null.

Be conservative: if you are not confident about categoria or the document is
illegible/ambiguous, say so via categoria_confianza "baja" rather than guessing.
"""

# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds_info = json.loads(os.environ["GDRIVE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)


def download_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf.read()


def list_children(service, folder_id):
    """Non-recursive: immediate children of a folder (files + folders)."""
    items, page_token = [], None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed=false",
            fields="nextPageToken, files(id,name,mimeType,parents,createdTime)",
            pageToken=page_token,
        ).execute()
        items.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return items


def walk_tree(service, folder_id, path_parts, skip_folder_names=(REVISAR_FOLDER_NAME,)):
    """Yield (file_dict, path_parts, parent_folder_id) for every non-folder file
    under folder_id, recursively. Folders named in skip_folder_names (and their
    contents) are skipped entirely — used to leave _REVISAR alone."""
    for item in list_children(service, folder_id):
        if item["mimeType"] == "application/vnd.google-apps.folder":
            if item["name"] in skip_folder_names:
                continue
            yield from walk_tree(service, item["id"], path_parts + [item["name"]], skip_folder_names)
        else:
            yield item, path_parts, folder_id


def get_or_create_folder(service, parent_id, name, folder_cache, apply_changes):
    """folder_cache maps (parent_id, name) -> folder_id for folders we know exist.
    In dry-run mode, a folder that doesn't exist yet is represented by a
    synthetic id "PLANNED:{parent_id}:{name}" so planning can continue without
    touching Drive."""
    key = (parent_id, name)
    if key in folder_cache:
        return folder_cache[key]
    if not apply_changes:
        planned_id = f"PLANNED:{parent_id}:{name}"
        folder_cache[key] = planned_id
        return planned_id
    folder = service.files().create(
        body={"name": name, "mimeType": "application/vnd.google-apps.folder", "parents": [parent_id]},
        fields="id",
    ).execute()
    folder_cache[key] = folder["id"]
    return folder["id"]


def move_file(service, file_id, old_parent_id, new_parent_id, new_name):
    kwargs = {"fileId": file_id, "fields": "id,name,parents"}
    if new_name is not None:
        kwargs["body"] = {"name": new_name}
    if new_parent_id and new_parent_id != old_parent_id:
        kwargs["addParents"] = new_parent_id
        kwargs["removeParents"] = old_parent_id
    service.files().update(**kwargs).execute()


# ── Text / vision extraction ─────────────────────────────────────────────────
def extract_pdf_text(pdf_bytes):
    try:
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            text = ""
            for page in pdf.pages[:3]:  # header/specimen info is always on the first pages
                t = page.extract_text()
                if t:
                    text += t + "\n"
        return text.strip()
    except Exception as e:
        print(f"    pdfplumber error: {e}")
        return ""


def render_first_page_png(pdf_bytes):
    images = convert_from_bytes(pdf_bytes, dpi=150, first_page=1, last_page=1)
    buf = io.BytesIO()
    images[0].save(buf, format="PNG")
    return buf.getvalue()


def classify_from_text(text, client):
    if len(text) > 15000:
        text = text[:15000]
    msg = client.messages.create(
        model=MODEL, max_tokens=600,
        messages=[{"role": "user", "content": CLASSIFY_INSTRUCTIONS + "\n\nDocument text:\n" + text}],
    )
    return _parse_json_object(msg.content[0].text)


def classify_from_image(png_bytes, client):
    img_b64 = base64.standard_b64encode(png_bytes).decode("utf-8")
    msg = client.messages.create(
        model=MODEL, max_tokens=600,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": img_b64}},
            {"type": "text", "text": CLASSIFY_INSTRUCTIONS},
        ]}],
    )
    return _parse_json_object(msg.content[0].text)


def _parse_json_object(raw):
    raw = raw.strip()
    if "```" in raw:
        for p in raw.split("```"):
            p = p.strip().lstrip("json").strip()
            if p.startswith("{"):
                raw = p
                break
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end + 1]
    return json.loads(raw)


def classify_document(file_bytes, mime_type, client):
    """Returns (classification_dict_or_None, method, error_or_None)."""
    if mime_type == "application/pdf":
        text = extract_pdf_text(file_bytes)
        if len(text) >= MIN_TEXT_LEN_FOR_TEXT_LAYER:
            try:
                return classify_from_text(text, client), "text", None
            except Exception as e:
                return None, "text", str(e)
        try:
            png_bytes = render_first_page_png(file_bytes)
        except Exception as e:
            return None, "vision", f"pdf2image render failed: {e}"
        try:
            return classify_from_image(png_bytes, client), "vision", None
        except Exception as e:
            return None, "vision", str(e)
    elif mime_type in ("image/jpeg", "image/png", "image/jpg"):
        media_type = "image/png" if mime_type == "image/png" else "image/jpeg"
        try:
            img_b64 = base64.standard_b64encode(file_bytes).decode("utf-8")
            msg = client.messages.create(
                model=MODEL, max_tokens=600,
                messages=[{"role": "user", "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": media_type, "data": img_b64}},
                    {"type": "text", "text": CLASSIFY_INSTRUCTIONS},
                ]}],
            )
            return _parse_json_object(msg.content[0].text), "vision", None
        except Exception as e:
            return None, "vision", str(e)
    else:
        return None, "skip", f"unsupported mime type {mime_type}"


# ── Date ambiguity resolution (deterministic — never left to the model) ──────
DATE_TOKEN_RE = re.compile(r"(\d{1,2})[/\-.](\d{1,2})[/\-.](\d{4})")

def resolve_date(fecha_texto):
    """Returns (date_or_None, reason_or_None). Never guesses an ambiguous date."""
    if not fecha_texto:
        return None, "no_date_found"
    m = DATE_TOKEN_RE.search(fecha_texto)
    if not m:
        return None, "unparseable_date_text"
    a, b, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
    if a > 12 and b > 12:
        return None, "invalid_date_numbers"
    if a > 12 and b <= 12:
        day, month = a, b          # unambiguous D/M
    elif b > 12 and a <= 12:
        day, month = b, a          # unambiguous M/D
    else:
        return None, "ambiguous_day_month"  # both <=12 — cannot tell D/M from M/D
    try:
        return date(y, month, day), None
    except ValueError:
        return None, "invalid_calendar_date"


# ── Filename / slug helpers ───────────────────────────────────────────────────
def slugify(s):
    if not s:
        return "otro"
    s = s.strip().lower()
    s = re.sub(r"[^a-z0-9]+", "-", s)
    return s.strip("-") or "otro"


def build_target_name(fecha, categoria, proveedor, tipo, ext):
    return f"{fecha.isoformat()}_{slugify(categoria)}_{slugify(proveedor)}_{slugify(tipo)}{ext}"


# ── Master tracker (tracker/master_tracker.xlsx) ─────────────────────────────
TRACKER_COLUMNS = [
    ("File Name", lambda r: r.get("new_name") or r.get("original_name")),
    ("Date", lambda r: r.get("resolved_date", "")),
    ("Provider", lambda r: r.get("proveedor", "")),
    ("Type", lambda r: r.get("tipo", "")),
    ("Year", lambda r: r.get("target_year", "")),
    ("Current Path", lambda r: r.get("original_path", "")),
    ("Target Path", lambda r: r.get("new_path", "")),
    ("Status", lambda r: r.get("action", "")),
    ("Notes", lambda r: r.get("reason", "")),
    ("Drive Link", lambda r: f"https://drive.google.com/file/d/{r['file_id']}/view" if r.get("file_id") else ""),
]

HEADER_FILL = PatternFill(start_color="1F2937", end_color="1F2937", fill_type="solid")
HEADER_FONT = Font(color="FFFFFF", bold=True)


def _write_sheet(wb, title, records):
    ws = wb.create_sheet(title=title[:31])  # Excel sheet-name length limit
    ws.append([col_name for col_name, _ in TRACKER_COLUMNS])
    for cell in ws[1]:
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
    for r in sorted(records, key=lambda r: (r.get("resolved_date") or "", r.get("original_path", ""))):
        ws.append([getter(r) for _, getter in TRACKER_COLUMNS])
    ws.freeze_panes = "A2"
    for i, (col_name, _) in enumerate(TRACKER_COLUMNS, start=1):
        widest = max([len(col_name)] + [len(str(row[i - 1].value or "")) for row in ws.iter_rows(min_row=2)])
        ws.column_dimensions[get_column_letter(i)].width = min(max(widest + 2, 10), 60)
    return ws


def build_master_tracker(records, tracker_path, apply_changes):
    """(Re)writes the whole tracker workbook from this run's records — one tab per
    category, a Resumen (summary) tab, and a _REVISAR tab for anything quarantined,
    duplicate, errored, or ignored. Safe to call on a dry run: it only writes the
    .xlsx file, never touches Drive."""
    by_category = defaultdict(list)
    revisar = []
    for r in records:
        action = r.get("action")
        categoria = r.get("categoria")
        if action in ("quarantine", "duplicate", "error", "ignored") or not categoria or categoria not in CATEGORIES:
            revisar.append(r)
        else:
            by_category[categoria].append(r)

    wb = Workbook()
    wb.remove(wb.active)  # drop the default blank sheet

    summary = wb.create_sheet(title="Resumen")
    summary.append(["Master Tracker — Medical Drive folder"])
    summary["A1"].font = Font(bold=True, size=14)
    summary.append(["Last updated (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")])
    summary.append(["Mode", "apply (changes executed)" if apply_changes else "dry run (plan only, nothing changed on Drive)"])
    summary.append(["Total files scanned", len(records)])
    summary.append([])
    summary.append(["Category", "Files"])
    for cell in summary[summary.max_row]:
        cell.font = Font(bold=True)
    for cat in CATEGORIES_ORDERED:
        summary.append([cat, len(by_category.get(cat, []))])
    summary.append(["_REVISAR (needs manual review)", len(revisar)])
    for col, width in zip("AB", (34, 50)):
        summary.column_dimensions[col].width = width

    for cat in CATEGORIES_ORDERED:
        _write_sheet(wb, cat, by_category.get(cat, []))
    _write_sheet(wb, REVISAR_FOLDER_NAME, revisar)

    os.makedirs(os.path.dirname(tracker_path) or ".", exist_ok=True)
    wb.save(tracker_path)


# ── Main sweep ────────────────────────────────────────────────────────────────
def main():
    ap = argparse.ArgumentParser(description="Clean up the Medical Google Drive folder.")
    ap.add_argument("--apply", action="store_true", help="Actually rename/move/quarantine on Drive. Default is dry-run.")
    ap.add_argument("--year", default=None, help="Only sweep this year subfolder (e.g. 2024). Default: whole tree.")
    ap.add_argument("--log", default="rename_log.csv", help="Path to write the CSV log to.")
    ap.add_argument("--tracker", default=DEFAULT_TRACKER_PATH, help="Path to write the master tracker .xlsx to.")
    args = ap.parse_args()

    apply_changes = args.apply
    print(f"Mode: {'APPLY (changes will be made)' if apply_changes else 'DRY RUN (no changes will be made)'}")

    service = get_drive_service()
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])

    # 1. Locate Medical/_REVISAR (create only if applying and it's genuinely missing).
    top_children = list_children(service, GDRIVE_FOLDER_ID)
    revisar = next((f for f in top_children if f["name"] == REVISAR_FOLDER_NAME
                     and f["mimeType"] == "application/vnd.google-apps.folder"), None)
    folder_cache = {}
    if revisar:
        revisar_id = revisar["id"]
    elif apply_changes:
        revisar_id = get_or_create_folder(service, GDRIVE_FOLDER_ID, REVISAR_FOLDER_NAME, folder_cache, True)
    else:
        revisar_id = f"PLANNED:{GDRIVE_FOLDER_ID}:{REVISAR_FOLDER_NAME}"

    # Seed folder_cache with the real year/category folders we already saw.
    for f in top_children:
        if f["mimeType"] == "application/vnd.google-apps.folder" and f["name"] != REVISAR_FOLDER_NAME:
            folder_cache[(GDRIVE_FOLDER_ID, f["name"])] = f["id"]
            for sub in list_children(service, f["id"]):
                if sub["mimeType"] == "application/vnd.google-apps.folder":
                    folder_cache[(f["id"], sub["name"])] = sub["id"]

    # 2. Walk the tree and classify every file.
    scan_root = GDRIVE_FOLDER_ID
    scan_path_prefix = []
    if args.year:
        year_folder = folder_cache.get((GDRIVE_FOLDER_ID, args.year))
        if not year_folder or str(year_folder).startswith("PLANNED:"):
            print(f"Year folder '{args.year}' not found under Medical/ — nothing to do.")
            return
        scan_root, scan_path_prefix = year_folder, [args.year]

    records = []
    print("Scanning Medical/ tree...")
    for f, path_parts, parent_id in walk_tree(service, scan_root, scan_path_prefix):
        current_path = "/".join(path_parts + [f["name"]])
        print(f"  {current_path}")
        try:
            file_bytes = download_file(service, f["id"])
        except Exception as e:
            records.append(_error_record(f, path_parts, parent_id, f"download failed: {e}"))
            continue

        content_hash = hashlib.sha256(file_bytes).hexdigest()
        mime = f["mimeType"]
        if mime not in ("application/pdf", "image/jpeg", "image/png", "image/jpg"):
            records.append(_error_record(f, path_parts, parent_id, f"unsupported file type ({mime}) — left alone",
                                          content_hash=content_hash, action="ignored"))
            continue

        classification, method, err = classify_document(file_bytes, mime, client)
        record = {
            "file_id": f["id"], "parent_id": parent_id,
            "original_path": current_path, "original_name": f["name"],
            "created_time": f.get("createdTime", ""),
            "content_hash": content_hash, "extraction_method": method,
            "mime_type": mime,
        }

        if err or not classification:
            record.update(action="quarantine", reason=f"extraction failed ({method}): {err}",
                           new_path="", new_name="")
            records.append(record)
            continue

        fecha, date_reason = resolve_date(classification.get("fecha_texto"))
        categoria = classification.get("categoria")
        categoria_confianza = classification.get("categoria_confianza", "baja")
        proveedor = classification.get("proveedor") or "otro"
        tipo = classification.get("tipo") or "otro"
        record["classification"] = classification

        if fecha is None:
            record.update(action="quarantine", reason=f"date: {date_reason}", new_path="", new_name="")
            records.append(record)
            continue
        if not categoria or categoria not in CATEGORIES or categoria_confianza != "alta":
            record.update(action="quarantine",
                           reason=f"category not confident (categoria={categoria!r}, confianza={categoria_confianza!r})",
                           new_path="", new_name="")
            records.append(record)
            continue

        ext = os.path.splitext(f["name"])[1].lower() or (".pdf" if mime == "application/pdf" else ".jpg")
        target_name = build_target_name(fecha, categoria, proveedor, tipo, ext)
        target_year = str(fecha.year)
        record.update(
            resolved_date=fecha.isoformat(), categoria=categoria, proveedor=proveedor, tipo=tipo,
            target_year=target_year, target_name=target_name,
        )
        records.append(record)

    # 3. Duplicate detection across the whole scanned set.
    by_hash = defaultdict(list)
    for r in records:
        if r.get("action") in ("quarantine", "ignored"):
            continue
        by_hash[r["content_hash"]].append(r)

    for group in by_hash.values():
        if len(group) < 2:
            continue
        # Prefer whichever copy is already exactly correctly named+placed; else oldest.
        def already_correct(r):
            parts = r["original_path"].split("/")
            if len(parts) < 3:
                return False
            return (r["original_name"] == r["target_name"]
                    and parts[0] == r["target_year"]
                    and parts[1] == r["categoria"])
        keeper = next((r for r in group if already_correct(r)), None)
        if keeper is None:
            keeper = min(group, key=lambda r: r.get("created_time", ""))
        for r in group:
            if r is keeper:
                continue
            r["action"] = "duplicate"
            r["reason"] = f"duplicate of {keeper['original_path']} (identical content hash)"

    # 4. Decide final action for everything not already quarantined/duplicate/ignored.
    for r in records:
        if "action" in r:
            continue
        current_year = r["original_path"].split("/")[0] if "/" in r["original_path"] else ""
        current_category = r["original_path"].split("/")[1] if r["original_path"].count("/") >= 2 else ""
        if r["original_name"] == r["target_name"] and current_year == r["target_year"] and current_category == r["categoria"]:
            r["action"] = "no_change"
        elif current_year == r["target_year"] and current_category == r["categoria"]:
            r["action"] = "rename"
        else:
            r["action"] = "move"

    # 5. Apply (or just log) the plan.
    print("\nApplying plan..." if apply_changes else "\nPlan (dry run — nothing will change):")
    for r in records:
        action = r.get("action", "quarantine")
        if action == "no_change":
            r["new_path"], r["new_name"] = r["original_path"], r["original_name"]
        elif action in ("rename", "move"):
            dest_folder_id = get_or_create_folder(
                service,
                get_or_create_folder(service, GDRIVE_FOLDER_ID, r["target_year"], folder_cache, apply_changes),
                r["categoria"], folder_cache, apply_changes,
            )
            r["new_path"] = f"{r['target_year']}/{r['categoria']}/{r['target_name']}"
            r["new_name"] = r["target_name"]
            if apply_changes:
                try:
                    move_file(service, r["file_id"], r["parent_id"], dest_folder_id, r["target_name"])
                except Exception as e:
                    r["action"] = "error"
                    r["reason"] = f"Drive update failed: {e}"
        elif action in ("quarantine", "duplicate"):
            prefix = "DUP_" if action == "duplicate" else "REVISAR_"
            new_name = r["original_name"] if r["original_name"].startswith(prefix) else prefix + r["original_name"]
            r["new_path"] = f"{REVISAR_FOLDER_NAME}/{new_name}"
            r["new_name"] = new_name
            if apply_changes:
                try:
                    move_file(service, r["file_id"], r["parent_id"], revisar_id, new_name)
                except Exception as e:
                    r["action"] = "error"
                    r["reason"] = f"Drive update failed: {e}"
        print(f"  [{r.get('action')}] {r['original_path']} -> {r.get('new_path', '(unchanged)')}"
              + (f"  ({r['reason']})" if r.get("reason") else ""))

    # 6. Write the CSV log (always — dry run or apply).
    fieldnames = ["action", "original_path", "original_name", "new_path", "new_name",
                  "resolved_date", "categoria", "proveedor", "tipo",
                  "extraction_method", "content_hash", "reason", "file_id", "applied"]
    with open(args.log, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in records:
            row = dict(r)
            row["applied"] = apply_changes and r.get("action") not in ("no_change", "ignored", "error")
            w.writerow(row)

    # 7. (Re)build the master tracker workbook — one tab per category. Safe on a
    # dry run too; this only writes a local .xlsx, it never touches Drive.
    build_master_tracker(records, args.tracker, apply_changes)

    counts = defaultdict(int)
    for r in records:
        counts[r.get("action", "quarantine")] += 1
    print(f"\n{'='*50}")
    print(f"DONE — {len(records)} files scanned. " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    print(f"Log written to {args.log}")
    print(f"Master tracker written to {args.tracker}")
    if not apply_changes:
        print("This was a DRY RUN — nothing on Drive changed. Re-run with --apply to execute this plan.")


def _error_record(f, path_parts, parent_id, reason, content_hash="", action="quarantine"):
    return {
        "file_id": f["id"], "parent_id": parent_id,
        "original_path": "/".join(path_parts + [f["name"]]), "original_name": f["name"],
        "created_time": f.get("createdTime", ""), "content_hash": content_hash,
        "extraction_method": "", "mime_type": f.get("mimeType", ""),
        "action": action, "reason": reason, "new_path": "", "new_name": "",
    }


if __name__ == "__main__":
    main()
