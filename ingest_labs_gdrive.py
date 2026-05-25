"""
Lab PDF auto-ingestion pipeline.
Scans Google Drive Medical folder for new PDFs, extracts lab values, inserts into Supabase.
"""
import os, json, re, io, sys
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
MODEL = "claude-sonnet-4-6"

EXTRACT_PROMPT = """You are a medical lab result parser. Extract ALL numeric lab values from the text below.

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

INSERT_SQL = """
INSERT INTO lab_results (fecha, año, archivo, proveedor, panel, marcador, valor, unidad, ref_min, ref_max, flag, estimulada, conversion_aplicada, revision_requerida)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, NULL, false)
ON CONFLICT DO NOTHING
"""

# ── Google Drive ──────────────────────────────────────────────────────────────
def get_drive_service():
    creds_json = os.environ["GDRIVE_CREDENTIALS"]
    creds_info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(creds_info, scopes=SCOPES)
    return build("drive", "v3", credentials=creds)

def list_pdfs_recursive(service, folder_id):
    """List all PDFs in folder and subfolders recursively."""
    pdfs = []
    # Get subfolders
    folders = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/vnd.google-apps.folder' and trashed=false",
        fields="files(id,name)"
    ).execute().get("files", [])
    
    for folder in folders:
        pdfs.extend(list_pdfs_recursive(service, folder["id"]))
    
    # Get PDFs in this folder
    files = service.files().list(
        q=f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false",
        fields="files(id,name,createdTime)"
    ).execute().get("files", [])
    
    pdfs.extend(files)
    return pdfs

def download_pdf(service, file_id):
    """Download PDF to memory."""
    request = service.files().get_media(fileId=file_id)
    buf = io.BytesIO()
    downloader = MediaIoBaseDownload(buf, request)
    done = False
    while not done:
        _, done = downloader.next_chunk()
    buf.seek(0)
    return buf

# ── Already processed ─────────────────────────────────────────────────────────
def get_processed_files(conn):
    cur = conn.cursor()
    cur.execute("SELECT DISTINCT archivo FROM lab_results WHERE archivo IS NOT NULL")
    result = {row[0] for row in cur.fetchall()}
    cur.close()
    return result

# ── Extract date and proveedor from filename ──────────────────────────────────
def parse_filename(filename):
    """Parse standardized filename like 2026-04-17_labs_quest-mx_biometria.pdf"""
    base = filename.replace(".pdf", "")
    parts = base.split("_")
    
    fecha_str = parts[0] if parts else "2000-01-01"
    try:
        fecha = date.fromisoformat(fecha_str)
        año = fecha.year
    except:
        fecha = date.today()
        año = fecha.year
    
    # Infer proveedor
    name_lower = filename.lower()
    if "labcorp" in name_lower:
        proveedor = "Labcorp NY"
    elif "quest-usa" in name_lower or "quest_usa" in name_lower:
        proveedor = "Quest USA"
    elif "quest-mx" in name_lower or "quest_mx" in name_lower:
        proveedor = "Quest MX"
    elif "angeles-lomas" in name_lower or "hospital" in name_lower:
        proveedor = "Hospital Ángeles Lomas"
    else:
        proveedor = "Desconocido"
    
    # Check if stimulated
    estimulada = "thyrogen" in name_lower or "thryrogen" in name_lower
    
    return fecha, año, proveedor, estimulada

# ── Text extraction ───────────────────────────────────────────────────────────
def extract_text(pdf_bytes):
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

# ── Claude parsing ────────────────────────────────────────────────────────────
def parse_with_claude(text, client):
    if len(text) > 15000:
        text = text[:15000]
    
    msg = client.messages.create(
        model=MODEL,
        max_tokens=3000,
        messages=[{"role": "user", "content": EXTRACT_PROMPT + text}]
    )
    raw = msg.content[0].text.strip()
    
    # Strip markdown
    if "```" in raw:
        parts = raw.split("```")
        for p in parts:
            p = p.strip()
            if p.startswith("json"): p = p[4:]
            p = p.strip()
            if p.startswith("["): 
                raw = p
                break
    
    start = raw.find("[")
    end = raw.rfind("]")
    if start != -1 and end != -1:
        raw = raw[start:end+1]
    
    return json.loads(raw.strip())

# ── Insert ────────────────────────────────────────────────────────────────────
def insert_rows(conn, fecha, año, proveedor, archivo, rows, estimulada):
    inserted = 0
    cur = conn.cursor()
    for r in rows:
        try:
            cur.execute(INSERT_SQL, (
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

# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    print("Connecting to Supabase...")
    conn = psycopg2.connect(os.environ["SUPABASE_DB_URL"])
    
    print("Connecting to Google Drive...")
    service = get_drive_service()
    
    print("Getting already-processed files...")
    processed = get_processed_files(conn)
    print(f"  {len(processed)} files already in Supabase")
    
    print("Scanning Google Drive Medical folder...")
    all_pdfs = list_pdfs_recursive(service, GDRIVE_FOLDER_ID)
    print(f"  {len(all_pdfs)} PDFs found in Drive")
    
    # Filter to only lab PDFs not yet processed
    new_pdfs = [
        f for f in all_pdfs
        if f["name"] not in processed
        and "_labs_" in f["name"]  # only lab files, not imaging
    ]
    print(f"  {len(new_pdfs)} new lab PDFs to process")
    
    if not new_pdfs:
        print("Nothing new. Done.")
        conn.close()
        return
    
    client = anthropic.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    total_inserted = 0
    flagged = []
    
    for pdf in new_pdfs:
        filename = pdf["name"]
        print(f"\n{'='*50}")
        print(f"Processing: {filename}")
        
        fecha, año, proveedor, estimulada = parse_filename(filename)
        print(f"  Date: {fecha} | Provider: {proveedor} | Stimulated: {estimulada}")
        
        # Download
        print("  Downloading...")
        try:
            pdf_bytes = download_pdf(service, pdf["id"])
        except Exception as e:
            print(f"  Download failed: {e}")
            continue
        
        # Extract text
        print("  Extracting text...")
        text = extract_text(pdf_bytes)
        if not text:
            print("  No text extracted — skipping (scanned PDF)")
            continue
        print(f"  {len(text)} chars")
        
        # Parse
        print("  Parsing with Claude...")
        try:
            rows = parse_with_claude(text, client)
        except Exception as e:
            print(f"  Parse error: {e}")
            continue
        print(f"  {len(rows)} markers found")
        
        # Insert
        inserted = insert_rows(conn, fecha, año, proveedor, filename, rows, estimulada)
        print(f"  Inserted: {inserted}")
        total_inserted += inserted
        
        # Collect flags
        for r in rows:
            if r.get("flag") in ("H", "L"):
                flagged.append(f"  {fecha} {r.get('marcador')} = {r.get('valor')} {r.get('unidad')} [{r.get('flag')}]")
    
    conn.close()
    print(f"\n{'='*50}")
    print(f"DONE — Total inserted: {total_inserted}")
    if flagged:
        print(f"Flagged values (H/L):")
        for f in flagged:
            print(f)

if __name__ == "__main__":
    main()
