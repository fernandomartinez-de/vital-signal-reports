"""
Lab PDF + InBody image auto-ingestion pipeline.
Scans Google Drive Medical folder for new PDFs and InBody images,
extracts values, inserts into Supabase.
"""
import os, json, re, io, sys, base64
import psycopg2, psycopg2.extras
from datetime import date, datetime
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import anthropic
import pdfplumber

# ── Config ────────────────────────────────────────────────────────────────────
GDRIVE_FOLDER_ID = "1_pB5M_-xqWU-jNYykK83fhZiWc5ldrS2"
SCOPES = ["https://www.googleapis.com/auth/drive.readonly"]
MODEL = "claude-sonnet-4-20250514"

LAB_EXTRACT_PROMPT = """You are a medical lab result parser. Extract ALL numeric lab values from the text below.

Return ONLY a valid JSON array. No markdown, no explanation. Start with [ and end with ].

Each element:
{"marcador":"standardized Spanish name","panel":"tiroideo|glucemico|vitaminas|lipidico|hemograma|otro","valor":0.0,"unidad":"unit","ref_min":0.0,"ref_max":0.0,"flag":"H|L|normal"}

Rules:
- valor must be a number, never a string
- ref_min and ref_max must be numbers or null
- flag must be exactly "H", "L", or "normal"
- Skip qualitative results (NEGATIVO, AMARILLO, AUSENTES, etc.)
- Skip calculated ratios and indices

Lab text:
"""

INBODY_EXTRACT_PROMPT = """You are an InBody bioimpedance analysis parser. Extract all numeric values from this InBody result image.

Return ONLY a valid JSON object. No markdown, no explanation.

Required fields (use null if not found):
{
  "fecha": "YYYY-MM-DD",
  "peso": 0.0,
  "mme": 0.0,
  "masa_grasa": 0.0,
  "pgc": 0.0,
  "mlg": 0.0,
  "agua": 0.0,
  "tmb": 0,
  "score": 0,
  "angulo_fase": 0.0,
  "grasa_visceral": 0,
  "rel_cintura_cadera": 0.0,
  "imc": 0.0,
  "peso_ideal": 0.0,
  "control_peso": 0.0,
  "control_grasa": 0.0,
  "control_musculo": 0.0,
  "dispositivo": "InBody270S"
}

Extract the date from the image (Fecha / Hora de la prueba field).
grasa_visceral should be the level number (e.g. 5), not a range.
control_peso/grasa/musculo are the target adjustment values (negative = reduce, positive = increase).
"""

LAB_INSERT_SQL = """
INSERT INTO lab_results (fecha, año, archivo, proveedor, panel, marcador, valor, unidad, ref_min, ref_max, flag, estimulada, conversion_aplicada, revision_requerida)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, false)
ON CONFLICT DO NOTHING
"""

INBODY_INSERT_SQL = """
INSERT INTO inbody_results (fecha, archivo, peso, mme, masa_grasa, pgc, mlg, agua, tmb, score, angulo_fase, grasa_visceral, rel_cintura_cadera, imc, peso_ideal, control_peso, control_grasa, control_musculo, dispositivo, proveedor)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
ON CONFLICT (fecha, archivo) DO NOTHING
"""

# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds_info = json.loads(os.environ["GDRIVE_CREDENTIALS"])
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

def list_files_recursive(service, folder_id, mime_types=None):
    """List all files in folder and subfolders recursively."""
    files = []
    folders = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)"
    ).execute().get("files", [])
    for folder in folders:
        files.extend(list_files_recursive(service, folder["id"], mime_types))
    
    mime_filter = ""
    if mime_types:
        conditions = " or ".join(f"mimeType='{m}'" for m in mime_types)
        mime_filter = f" and ({conditions})"
    
    found = service.files().list(
        q=f"'{folder_id}' in parents{mime_filter} and trashed=false",
        fields="files(id,name,mimeType,parents)"
    ).execute().get("files", [])
    files.extend(found)
    return files

def get_folder_path(service, file_id):
    """Get the folder name containing this file."""
    file = service.files().get(fileId=file_id, fields="parents").execute()
    parents = file.get("parents", [])
    if not parents:
        return ""
    parent = service.files().get(fileId=parents[0], fields="name").execute()
    return parent.get("name", "").lower()

def download_file(service, file_id):
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

# ── Already processed ─────────────────────────────────────────────────────────
def get_processed_labs(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT archivo FROM lab_results WHERE archivo IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result

def get_processed_inbody(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT archivo FROM inbody_results WHERE archivo IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result

# ── Filename parsing ──────────────────────────────────────────────────────────
def parse_filename(filename):
    base = filename.replace(".pdf", "").replace(".jpg", "").replace(".jpeg", "").replace(".png", "")
    parts = base.split("_")
    try:
        fecha = date.fromisoformat(parts[0])
        año = fecha.year
    except:
        fecha = date.today()
        año = fecha.year
    
    name_lower = filename.lower()
    if "labcorp" in name_lower:
        proveedor = "Labcorp NY"
    elif "quest-usa" in name_lower or "quest_usa" in name_lower:
        proveedor = "Quest USA"
    elif "quest-mx" in name_lower or "quest_mx" in name_lower:
        proveedor = "Quest MX"
    elif "angeles" in name_lower or "hospital" in name_lower:
        proveedor = "Hospital Ángeles Lomas"
    elif "inbody" in name_lower:
        proveedor = "MNC Javier Luna Moran"
    else:
        proveedor = "Desconocido"
    
    estimulada = "thyrogen" in name_lower or "thryrogen" in name_lower
    return fecha, año, proveedor, estimulada

# ── Lab extraction ────────────────────────────────────────────────────────────
def extract_text_from_pdf(pdf_bytes):
    try:
        with pdfplumber.open(pdf_bytes) as pdf:
            text = ""
            for page in pdf.pages:
                t = page.extract_text()
                if t:
                    text += t + "\n"
        return text.strip()
    except Exception as e:
        print(f"    pdfplumber error: {e}")
        return ""

def parse_labs_with_claude(text, client):
    if len(text) > 15000:
        text = text[:15000]
    msg = client.messages.create(
        model=MODEL, max_tokens=3000,
        messages=[{"role": "user", "content": LAB_EXTRACT_PROMPT + text}]
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        for p in raw.split("```"):
            p = p.strip().lstrip("json").strip()
            if p.startswith("["): raw = p; break
    start, end = raw.find("["), raw.rfind("]")
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    return json.loads(raw.strip())

# ── InBody extraction ─────────────────────────────────────────────────────────
def parse_inbody_with_claude(image_bytes, mime_type, client):
    img_b64 = base64.standard_b64encode(image_bytes.read()).decode("utf-8")
    msg = client.messages.create(
        model=MODEL, max_tokens=1000,
        messages=[{"role": "user", "content": [
            {"type": "image", "source": {"type": "base64", "media_type": mime_type, "data": img_b64}},
            {"type": "text", "text": INBODY_EXTRACT_PROMPT}
        ]}]
    )
    raw = msg.content[0].text.strip()
    if "```" in raw:
        for p in raw.split("```"):
            p = p.strip().lstrip("json").strip()
            if p.startswith("{"): raw = p; break
    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    return json.loads(raw.strip())

# ── Insert ────────────────────────────────────────────────────────────────────
def insert_labs(conn, fecha, año, proveedor, archivo, rows, estimulada):
    inserted = 0
    cur = conn.cursor()
    for r in rows:
        try:
            cur.execute(LAB_INSERT_SQL, (
                fecha, año, archivo, proveedor,
                r.get("panel", "otro"),
                str(r.get("marcador", ""))[:100],
                float(r.get("valor", 0)),
                r.get("unidad"),
                r.get("ref_min"),
                r.get("ref_max"),
                r.get("flag", ""),
                estimulada,
            ))
            if cur.rowcount > 0:
                inserted += 1
        except Exception as e:
            print(f"    insert error {r.get('marcador')}: {e}")
    conn.commit()
    cur.close()
    return inserted

def insert_inbody(conn, data, archivo, proveedor):
    cur = conn.cursor()
    try:
        # Parse fecha from data or use today
        try:
            fecha = date.fromisoformat(data.get("fecha", date.today().isoformat()))
        except:
            fecha = date.today()
        
        cur.execute(INBODY_INSERT_SQL, (
            fecha, archivo,
            data.get("peso"), data.get("mme"), data.get("masa_grasa"),
            data.get("pgc"), data.get("mlg"), data.get("agua"),
            data.get("tmb"), data.get("score"), data.get("angulo_fase"),
            data.get("grasa_visceral"), data.get("rel_cintura_cadera"),
            data.get("imc"), data.get("peso_ideal"),
            data.get("control_peso"), data.get("control_grasa"),
            data.get("control_musculo"),
            data.get("dispositivo", "InBody270S"),
            proveedor,
        ))
        conn.commit()
        inserted = cur.rowcount
    except Exception as e:
        print(f"    InBody insert error: {e}")
        conn.rollback()
        inserted = 0
    cur.close()
    return inserted

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Connecting to Supabase...")
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    
    print("Connecting to Google Drive...")
    service = get_drive_service()
    
    processed_labs = get_processed_labs(conn)
    processed_inbody = get_processed_inbody(conn)
    print(f"  {len(processed_labs)} lab files already processed")
    print(f"  {len(processed_inbody)} InBody files already processed")
    
    print("Scanning Google Drive Medical folder...")
    all_files = list_files_recursive(service, GDRIVE_FOLDER_ID, [
        "application/pdf",
        "image/jpeg",
        "image/png",
        "image/jpg",
    ])
    print(f"  {len(all_files)} files found")
    
    # Split into labs and InBody
    new_labs = [
        f for f in all_files
        if f["mimeType"] == "application/pdf"
        and "_labs_" in f["name"]
        and f["name"] not in processed_labs
    ]
    new_inbody = [
        f for f in all_files
        if f["mimeType"] in ("image/jpeg", "image/png", "image/jpg")
        and "inbody" in f["name"].lower()
        and f["name"] not in processed_inbody
    ]
    
    print(f"  {len(new_labs)} new lab PDFs to process")
    print(f"  {len(new_inbody)} new InBody images to process")
    
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    total_lab_rows = 0
    total_inbody = 0
    
    # ── Process labs ──────────────────────────────────────────────────────────
    for pdf in new_labs:
        filename = pdf["name"]
        print(f"\nLab: {filename}")
        fecha, año, proveedor, estimulada = parse_filename(filename)
        print(f"  {fecha} | {proveedor} | estimulada={estimulada}")
        try:
            pdf_bytes = download_file(service, pdf["id"])
        except Exception as e:
            print(f"  Download failed: {e}"); continue
        text = extract_text_from_pdf(pdf_bytes)
        if not text:
            print(f"  No text extracted — skipping"); continue
        try:
            rows = parse_labs_with_claude(text, client)
        except Exception as e:
            print(f"  Parse error: {e}"); continue
        inserted = insert_labs(conn, fecha, año, proveedor, filename, rows, estimulada)
        print(f"  {len(rows)} markers found, {inserted} inserted")
        total_lab_rows += inserted
    
    # ── Process InBody ────────────────────────────────────────────────────────
    for img in new_inbody:
        filename = img["name"]
        print(f"\nInBody: {filename}")
        _, _, proveedor, _ = parse_filename(filename)
        mime = img["mimeType"]
        if mime == "image/jpg":
            mime = "image/jpeg"
        try:
            img_bytes = download_file(service, img["id"])
        except Exception as e:
            print(f"  Download failed: {e}"); continue
        try:
            data = parse_inbody_with_claude(img_bytes, mime, client)
        except Exception as e:
            print(f"  Parse error: {e}"); continue
        print(f"  Extracted: peso={data.get('peso')} score={data.get('score')}")
        inserted = insert_inbody(conn, data, filename, proveedor)
        print(f"  {'Inserted' if inserted else 'Already exists or error'}")
        total_inbody += inserted
    
    conn.close()
    print(f"\n{'='*50}")
    print(f"DONE — Lab rows inserted: {total_lab_rows} | InBody records inserted: {total_inbody}")

if __name__ == "__main__":
    main()
