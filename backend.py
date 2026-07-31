#!/usr/bin/env python3
"""
RMAX Quote Tracker — FastAPI backend
Run: uvicorn backend:app --reload --port 8000
"""
import os, sqlite3, json, glob as _glob, shutil, tempfile, io, time
from collections import defaultdict
from datetime import datetime, timedelta
from contextlib import contextmanager
from typing import Optional

# ── Date parsing helper ───────────────────────────────────────────────────────
_DATE_FMTS = [
    '%Y-%m-%d',   # 2026-07-09
    '%m/%d/%Y',   # 7/9/2026
    '%m/%d/%y',   # 7/9/26
    '%b %d, %Y',  # Jul 13, 2026
    '%B %d, %Y',  # July 13, 2026
    '%b. %d, %Y', # Jul. 13, 2026
]

def parse_date(s: str):
    """Return datetime or None for any of our known date string formats."""
    if not s:
        return None
    s = s.strip()
    for fmt in _DATE_FMTS:
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            pass
    return None

def normalize_date(s: str) -> str:
    """Convert any supported date string to YYYY-MM-DD; return original if unparseable."""
    if not s:
        return s
    dt = parse_date(s)
    return dt.strftime('%Y-%m-%d') if dt else s

from fastapi import FastAPI, HTTPException, Query, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# DATA_DIR can be overridden via env var so the DB lives on a persistent Railway volume.
# In Railway: set DATA_DIR=/data and mount a volume at /data.
DATA_DIR = os.environ.get("DATA_DIR", BASE_DIR)
os.makedirs(DATA_DIR, exist_ok=True)
DB_PATH  = os.path.join(DATA_DIR, "quotes.db")
STATIC   = os.path.join(BASE_DIR, "static")

# ── DB helpers ────────────────────────────────────────────────────────────────
@contextmanager
def get_db():
    con = sqlite3.connect(DB_PATH)
    con.row_factory = sqlite3.Row
    try:
        yield con
        con.commit()
    finally:
        con.close()

def init_db():
    with get_db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS quotes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            status             TEXT,
            date_received      TEXT,
            date_quoted        TEXT,
            sent_to            TEXT,
            subject            TEXT,
            job_name           TEXT,
            customer           TEXT,
            location           TEXT,
            product            TEXT,
            price              TEXT,
            quantities         TEXT,
            amount             REAL,
            close_date         TEXT,
            est_freight        TEXT,
            lead_time          TEXT,
            notes              TEXT,
            add_to_salesforce  INTEGER DEFAULT 0,
            completed          INTEGER DEFAULT 0,
            created_at         TEXT DEFAULT (datetime('now')),
            updated_at         TEXT DEFAULT (datetime('now'))
        )""")
        # Add columns to existing databases that predate this schema
        for col, definition in [('add_to_salesforce', 'INTEGER DEFAULT 0'),
                                 ('completed',         'INTEGER DEFAULT 0'),
                                 ('region',            'TEXT'),
                                 ('deleted',           'INTEGER DEFAULT 0'),
                                 ('company_id',        'INTEGER')]:
            try:
                con.execute(f"ALTER TABLE quotes ADD COLUMN {col} {definition}")
            except Exception:
                pass  # Column already exists

# ── Models ────────────────────────────────────────────────────────────────────
STATUS_OPTIONS = ["Won", "Lost", "Not Awarded", "Duplicate", "Verbal", "Unlikely"]

class QuoteIn(BaseModel):
    status:             Optional[str] = None
    date_received:      Optional[str] = None
    date_quoted:        Optional[str] = None
    sent_to:            Optional[str] = None
    subject:            Optional[str] = None
    job_name:           Optional[str] = None
    customer:           Optional[str] = None
    location:           Optional[str] = None
    product:            Optional[str] = None
    price:              Optional[str] = None
    quantities:         Optional[str] = None
    amount:             Optional[float] = None
    close_date:         Optional[str] = None
    est_freight:        Optional[str] = None
    lead_time:          Optional[str] = None
    notes:              Optional[str] = None
    region:             Optional[str] = None
    add_to_salesforce:  Optional[int] = 0
    completed:          Optional[int] = 0
    company_id:         Optional[int] = None

# ── App ───────────────────────────────────────────────────────────────────────
app = FastAPI(title="RMAX Quote Tracker")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

@app.on_event("startup")
def startup():
    init_db()
    init_hydrotech_db()
    init_glassworks_db()
    init_contacts_db()
    migrate_dates()

def migrate_dates():
    """One-time migration: normalize any non-ISO date strings in existing rows."""
    date_cols = ['date_received', 'date_quoted', 'close_date']
    for table in ('quotes', 'hydrotech_quotes'):
        try:
            with get_db() as con:
                rows = con.execute(f"SELECT id, {', '.join(date_cols)} FROM {table}").fetchall()
                for row in rows:
                    updates = {}
                    for col in date_cols:
                        val = row[col]
                        if val and not val[:4].isdigit():  # already ISO if starts with 4 digits
                            normalized = normalize_date(val)
                            if normalized != val:
                                updates[col] = normalized
                    if updates:
                        sets = ', '.join(f"{c}=?" for c in updates)
                        con.execute(f"UPDATE {table} SET {sets} WHERE id=?",
                                    list(updates.values()) + [row['id']])
        except Exception:
            pass

# ── Quote endpoints ───────────────────────────────────────────────────────────
@app.get("/api/quotes")
def list_quotes(
    status:   Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search:   Optional[str] = Query(None),
):
    with get_db() as con:
        sql = "SELECT * FROM quotes WHERE (deleted IS NULL OR deleted=0)"
        params = []
        if status and status != "All":
            sql += " AND status = ?"
            params.append(status)
        if location and location != "All":
            sql += " AND location = ?"
            params.append(location)
        if search:
            sql += " AND (subject LIKE ? OR sent_to LIKE ? OR job_name LIKE ? OR customer LIKE ? OR location LIKE ?)"
            s = f"%{search}%"
            params += [s, s, s, s, s]
        sql += " ORDER BY date_received DESC"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

@app.get("/api/quotes/{quote_id}")
def get_quote(quote_id: int):
    with get_db() as con:
        row = con.execute("SELECT * FROM quotes WHERE id=? AND (deleted IS NULL OR deleted=0)", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Quote not found")
        return dict(row)

@app.post("/api/quotes", status_code=201)
def create_quote(q: QuoteIn):
    with get_db() as con:
        cur = con.execute("""
        INSERT INTO quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes,
            region,add_to_salesforce,completed,company_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id))
        return {"id": cur.lastrowid}

@app.put("/api/quotes/{quote_id}")
def update_quote(quote_id: int, q: QuoteIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Quote not found")
        con.execute("""
        UPDATE quotes SET status=?,date_received=?,date_quoted=?,sent_to=?,subject=?,
            job_name=?,customer=?,location=?,product=?,price=?,quantities=?,amount=?,
            close_date=?,est_freight=?,lead_time=?,notes=?,
            region=?,add_to_salesforce=?,completed=?,company_id=?,updated_at=datetime('now')
        WHERE id=?
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id,quote_id))
        return {"ok": True}

@app.delete("/api/quotes/{quote_id}")
def delete_quote(quote_id: int):
    with get_db() as con:
        # Soft delete — keeps row so sync won't re-import the same email
        con.execute("UPDATE quotes SET deleted=1 WHERE id=?", (quote_id,))
        return {"ok": True}

@app.get("/api/quotes-export")
def export_quotes():
    """Export all quotes to a formatted Excel file."""
    import openpyxl
    from openpyxl.styles import (Font, PatternFill, Alignment, Border, Side)
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse
    import io

    with get_db() as con:
        rows = con.execute("""
            SELECT id, status, date_received, date_quoted, sent_to, subject,
                   job_name, customer, location, product, price, quantities,
                   amount, close_date, est_freight, lead_time, notes,
                   add_to_salesforce, completed
            FROM quotes WHERE (deleted IS NULL OR deleted=0) ORDER BY date_received DESC
        """).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "RMAX Quotes"

    # ── Styles ────────────────────────────────────────────────────────────────
    header_font  = Font(name='Calibri', bold=True, color='FFFFFF', size=11)
    header_fill  = PatternFill('solid', fgColor='1F4E79')
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    border_side  = Side(style='thin', color='BFBFBF')
    cell_border  = Border(left=border_side, right=border_side,
                          top=border_side, bottom=border_side)
    zebra_fill   = PatternFill('solid', fgColor='EBF3FB')

    STATUS_FILLS = {
        'Won':      PatternFill('solid', fgColor='D9EAD3'),
        'Lost':     PatternFill('solid', fgColor='F4CCCC'),
        'Verbal':   PatternFill('solid', fgColor='FFF2CC'),
        'Pending':  PatternFill('solid', fgColor='CFE2F3'),
    }

    # ── Headers ───────────────────────────────────────────────────────────────
    headers = [
        ('ID',              8),
        ('Status',          12),
        ('Date Received',   14),
        ('Date Quoted',     14),
        ('Sent To',         22),
        ('Subject',         30),
        ('Job Name',        28),
        ('Customer',        22),
        ('Location',        18),
        ('Product',         16),
        ('Price',           12),
        ('Quantities',      14),
        ('Amount',          14),
        ('Close Date',      14),
        ('Est. Freight',    14),
        ('Lead Time',       14),
        ('Notes',           35),
        ('Salesforce',      12),
        ('Completed',       12),
    ]

    ws.row_dimensions[1].height = 30
    for col_idx, (label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font        = header_font
        cell.fill        = header_fill
        cell.alignment   = header_align
        cell.border      = cell_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width

    # ── Freeze header row ─────────────────────────────────────────────────────
    ws.freeze_panes = 'A2'

    # ── Data rows ─────────────────────────────────────────────────────────────
    for row_idx, q in enumerate(rows, start=2):
        status = q['status'] or ''
        row_fill = STATUS_FILLS.get(status, (zebra_fill if row_idx % 2 == 0 else None))

        values = [
            q['id'],
            q['status'],
            q['date_received'],
            q['date_quoted'],
            q['sent_to'],
            q['subject'],
            q['job_name'],
            q['customer'],
            q['location'],
            q['product'],
            q['price'],
            q['quantities'],
            q['amount'],
            q['close_date'],
            q['est_freight'],
            q['lead_time'],
            q['notes'],
            'Yes' if q['add_to_salesforce'] else '',
            'Yes' if q['completed'] else '',
        ]

        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = cell_border
            cell.alignment = Alignment(vertical='top', wrap_text=(col_idx in (6,7,17)))
            if row_fill:
                cell.fill = row_fill
            # Format amount as currency
            if col_idx == 13 and value is not None:
                cell.number_format = '$#,##0.00'
            # Center status, flags
            if col_idx in (1, 2, 18, 19):
                cell.alignment = Alignment(horizontal='center', vertical='top')

    # ── Auto-filter ───────────────────────────────────────────────────────────
    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"

    # ── Stream response ───────────────────────────────────────────────────────
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"RMAX_Quotes_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )

# ── Sync endpoint — pulls directly from Outlook "RMAX Quotes" folder ──────────
import re as _sync_re

def _strip_tags(html: str) -> str:
    """Minimal HTML→text for use before _strip_html is defined."""
    html = _sync_re.sub(r'<style[^>]*>.*?</style>', '', html, flags=_sync_re.DOTALL|_sync_re.IGNORECASE)
    html = _sync_re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_sync_re.DOTALL|_sync_re.IGNORECASE)
    html = _sync_re.sub(r'<br\s*/?>', '\n', html, flags=_sync_re.IGNORECASE)
    html = _sync_re.sub(r'</?(?:p|div|tr|td|li|h\d)[^>]*>', '\n', html, flags=_sync_re.IGNORECASE)
    html = _sync_re.sub(r'<[^>]+>', '', html)
    return (html.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('\r', ''))

def _parse_quote_email(subject: str, body_html: str, content_type: str) -> dict:
    """
    Parse a quote-request email whose body follows the standard template:
        Job Name: XYZ
        Customer: XYZ
        Location: XYZ
        Product: XYZ
        Price: XYZ
        Quantities: XYZ
        Estimated Freight: XYZ
        Lead time: XYZ
    """
    body = _strip_tags(body_html) if content_type == "html" else body_html
    body = _sync_re.sub(r'\n{3,}', '\n\n', body).strip()

    def _field(label: str) -> str:
        """Extract the value after 'Label:' on the same line."""
        m = _sync_re.search(
            rf'^\s*{_sync_re.escape(label)}\s*:\s*(.+)',
            body, _sync_re.IGNORECASE | _sync_re.MULTILINE
        )
        return m.group(1).strip() if m else ''

    fields = {}

    job      = _field('Job Name')
    customer = _field('Customer')
    location = _field('Location')
    product  = _field('Product')
    price    = _field('Price')
    qty      = _field('Quantities')
    freight  = _field('Estimated Freight')
    lead     = _field('Lead time') or _field('Lead Time')

    # Fallback: derive job name from subject if body label missing
    if not job and subject:
        job = _sync_re.sub(
            r'^(re:|fw:|fwd:|(?:rmax|hydrotech|glassworks)\s+quotes?\s*[-–:]\s*)+',
            '', subject, flags=_sync_re.IGNORECASE
        ).strip()

    if job:      fields['job_name']    = job
    if customer: fields['customer']    = customer
    if location: fields['location']    = location
    if product:  fields['product']     = product
    if price:    fields['price']       = price
    if qty:      fields['quantities']  = qty
    if freight:  fields['est_freight'] = freight
    if lead:     fields['lead_time']   = lead

    # Amount: price_per_sf × sf_per_bundle × bundles
    # Price field example:  "$1.100/sf through Q3, 2026"
    # Qty field example:    "48 pcs/bundle (1536 sf/bundle) – 52 bundles"
    try:
        price_sf_m   = _sync_re.search(r'\$\s*([\d,]+(?:\.\d+)?)\s*/\s*sf', price or '', _sync_re.IGNORECASE)
        sf_bundle_m  = _sync_re.search(r'([\d,]+)\s*sf\s*/\s*bundle', qty or '', _sync_re.IGNORECASE)
        bundles_m    = _sync_re.search(r'(\d[\d,]*)\s*bundles?\b', qty or '', _sync_re.IGNORECASE)
        if price_sf_m and sf_bundle_m and bundles_m:
            price_per_sf  = float(price_sf_m.group(1).replace(',', ''))
            sf_per_bundle = float(sf_bundle_m.group(1).replace(',', ''))
            num_bundles   = float(bundles_m.group(1).replace(',', ''))
            fields['amount'] = round(price_per_sf * sf_per_bundle * num_bundles, 2)
    except (AttributeError, ValueError):
        pass

    return fields


@app.post("/api/sync")
def sync_from_outlook():
    """
    Pull new emails from the 'RMAX Quotes' Outlook folder via Graph API,
    parse each email body to pre-populate quote fields, and insert any
    that are not already in the database.
    Falls back to sync_pending.json if Graph is not configured.
    """
    # ── Primary path: Graph API ───────────────────────────────────────────────
    if GRAPH_CLIENT_SECRET:
        try:
            import requests as _req
        except ImportError:
            return {"inserted": 0, "skipped": 0, "message": "requests package not installed"}
        try:
            token = _get_graph_token()
        except Exception as e:
            return {"inserted": 0, "skipped": 0, "message": f"Auth failed: {e}"}

        hdrs = {"Authorization": f"Bearer {token}"}

        # ── Find "RMAX Quotes" folder ─────────────────────────────────────────
        # List all top-level and Inbox child folders, match by name
        folder_id = None
        for list_url in [
            f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders?$select=id,displayName&$top=100",
            f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/Inbox/childFolders?$select=id,displayName&$top=100",
        ]:
            try:
                r = _req.get(list_url, headers=hdrs, timeout=20)
                if r.status_code == 200:
                    for f in r.json().get("value", []):
                        if (f.get("displayName") or "").strip().lower() == "rmax quotes":
                            folder_id = f["id"]
                            break
                if folder_id:
                    break
            except Exception:
                pass

        if not folder_id:
            return {"inserted": 0, "skipped": 0,
                    "message": "Could not find 'RMAX Quotes' folder in Outlook — check folder name"}

        # ── Fetch messages with body ──────────────────────────────────────────
        msgs = []
        next_url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                    f"/mailFolders/{folder_id}/messages"
                    f"?$select=from,toRecipients,subject,receivedDateTime,body&$top=50")
        while next_url and len(msgs) < 200:
            try:
                r = _req.get(next_url, headers=hdrs, timeout=30)
                if r.status_code != 200:
                    return {"inserted": 0, "skipped": 0,
                            "message": f"Graph API error {r.status_code}: {r.text[:200]}"}
                data = r.json()
                msgs.extend(data.get("value", []))
                next_url = data.get("@odata.nextLink")
            except Exception as e:
                return {"inserted": 0, "skipped": 0, "message": f"Fetch error: {e}"}

        # ── Insert / update quotes ────────────────────────────────────────────
        inserted = updated = skipped = 0
        with get_db() as con:
            for msg in msgs:
                # "Sent To" = first recipient (who the quote was sent to)
                to_recipients = msg.get("toRecipients", [])
                if to_recipients:
                    first_to = to_recipients[0].get("emailAddress", {})
                    sent_to  = (first_to.get("name") or first_to.get("address") or "").strip()
                else:
                    sent_to  = ""

                subject       = (msg.get("subject") or "").strip()
                received_raw  = msg.get("receivedDateTime", "")
                date_received = received_raw[:10] if received_raw else ""

                # Dedup check — if already exists, fix sent_to if it was wrong
                exists = con.execute(
                    "SELECT id, sent_to FROM quotes WHERE subject=? AND date_received=?",
                    (subject, date_received)
                ).fetchone()
                if exists:
                    # Only fill in sent_to if blank — never overwrite a user-edited value
                    if sent_to and not exists["sent_to"]:
                        con.execute("UPDATE quotes SET sent_to=? WHERE id=?",
                                    (sent_to, exists["id"]))
                        updated += 1
                    else:
                        skipped += 1
                    continue

                # Parse body for pre-populated fields
                body_content = msg.get("body", {}).get("content", "")
                content_type = msg.get("body", {}).get("contentType", "text")
                parsed = _parse_quote_email(subject, body_content, content_type)

                con.execute("""
                INSERT INTO quotes
                    (date_received, sent_to, subject, job_name, customer, location,
                     product, price, quantities, amount, est_freight, lead_time)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
                """, (
                    date_received, sent_to, subject,
                    parsed.get('job_name'), parsed.get('customer'), parsed.get('location'),
                    parsed.get('product'), parsed.get('price'), parsed.get('quantities'),
                    parsed.get('amount'), parsed.get('est_freight'), parsed.get('lead_time'),
                ))
                inserted += 1

        parts = []
        if inserted: parts.append(f"{inserted} new quote{'' if inserted==1 else 's'} imported")
        if updated:  parts.append(f"{updated} updated")
        msg_text = "✓ " + ", ".join(parts) if parts else "✓ Already up to date"
        return {"inserted": inserted, "skipped": skipped, "message": msg_text}

    # ── Fallback: sync_pending.json (legacy path) ─────────────────────────────
    import json as _json
    pending_path = os.path.join(BASE_DIR, "sync_pending.json")
    if not os.path.exists(pending_path):
        return {"inserted": 0, "skipped": 0, "message": "Graph API not configured and no pending quotes file"}

    with open(pending_path, "r", encoding="utf-8") as f:
        records = _json.load(f)

    inserted = skipped = 0
    with get_db() as con:
        for r in records:
            exists = con.execute(
                "SELECT id FROM quotes WHERE sent_to=? AND subject=? AND date_received=?",
                (r.get("sent_to"), r.get("subject"), r.get("date_received"))
            ).fetchone()
            if exists:
                skipped += 1
                continue
            con.execute("""
            INSERT INTO quotes
                (status, date_received, date_quoted, sent_to, subject, job_name,
                 customer, location, product, price, quantities, amount,
                 close_date, est_freight, lead_time, notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                r.get("status"), r.get("date_received"), r.get("date_quoted"),
                r.get("sent_to"), r.get("subject"), r.get("job_name"),
                r.get("customer"), r.get("location"), r.get("product"),
                r.get("price"), r.get("quantities"), r.get("amount"),
                r.get("close_date"), r.get("est_freight"), r.get("lead_time"),
                r.get("notes"),
            ))
            inserted += 1

    with open(pending_path, "w", encoding="utf-8") as f:
        _json.dump([], f)

    return {"inserted": inserted, "skipped": skipped,
            "message": f"Imported {inserted} new quote(s), {skipped} duplicate(s) skipped"}

# ── Sync debug endpoint ───────────────────────────────────────────────────────
@app.get("/api/sync-debug")
def sync_debug():
    """Diagnose Graph API connectivity and RMAX Quotes folder lookup."""
    if not GRAPH_CLIENT_SECRET:
        return {"error": "GRAPH_CLIENT_SECRET not set in Railway environment variables"}
    try:
        import requests as _req
    except ImportError:
        return {"error": "requests package not installed"}
    try:
        token = _get_graph_token()
    except Exception as e:
        return {"error": f"Auth failed: {e}"}

    hdrs = {"Authorization": f"Bearer {token}"}
    result = {"auth": "ok", "folders": [], "rmax_quotes_folder_id": None, "message_count": None}

    # List all top-level folders
    try:
        r = _req.get(f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders?$select=id,displayName&$top=100", headers=hdrs, timeout=20)
        result["folders_status"] = r.status_code
        result["folders_raw"] = r.text[:500]
        if r.status_code == 200:
            folders = r.json().get("value", [])
            result["folders"] = [f["displayName"] for f in folders]
            for f in folders:
                if (f.get("displayName") or "").strip().lower() == "rmax quotes":
                    result["rmax_quotes_folder_id"] = f["id"]
    except Exception as e:
        result["folder_list_error"] = str(e)

    # Also check Inbox children
    if not result["rmax_quotes_folder_id"]:
        try:
            r = _req.get(f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/Inbox/childFolders?$select=id,displayName&$top=100", headers=hdrs, timeout=20)
            result["inbox_children_status"] = r.status_code
            result["inbox_children_raw"] = r.text[:500]
            if r.status_code == 200:
                children = r.json().get("value", [])
                result["inbox_children"] = [f["displayName"] for f in children]
                for f in children:
                    if (f.get("displayName") or "").strip().lower() == "rmax quotes":
                        result["rmax_quotes_folder_id"] = f["id"]
        except Exception as e:
            result["inbox_children_error"] = str(e)

    # If found, count messages
    if result["rmax_quotes_folder_id"]:
        fid = result["rmax_quotes_folder_id"]
        try:
            r = _req.get(f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/{fid}/messages?$select=subject,receivedDateTime,from,toRecipients&$top=5", headers=hdrs, timeout=20)
            if r.status_code == 200:
                msgs = r.json().get("value", [])
                result["message_count"] = len(msgs)
                result["sample_emails"] = [{
                    "subject":   m.get("subject",""),
                    "date":      m.get("receivedDateTime","")[:10],
                    "from":      m.get("from",{}).get("emailAddress",{}).get("name",""),
                    "from_addr": m.get("from",{}).get("emailAddress",{}).get("address",""),
                    "to":        [r2.get("emailAddress",{}).get("name","") for r2 in m.get("toRecipients",[])],
                    "to_addr":   [r2.get("emailAddress",{}).get("address","") for r2 in m.get("toRecipients",[])],
                } for m in msgs[:3]]
        except Exception as e:
            result["message_fetch_error"] = str(e)

    return result

# ── Dashboard endpoint ────────────────────────────────────────────────────────
@app.get("/api/dashboard")
def dashboard():
    with get_db() as con:
        # KPI totals
        totals = con.execute("""
            SELECT
                COUNT(*) as total_quotes,
                COALESCE(SUM(amount),0) as total_amount,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won_amount,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal_amount,
                COUNT(CASE WHEN status='Won'    THEN 1 END) as won_count,
                COUNT(CASE WHEN status='Verbal' THEN 1 END) as verbal_count,
                COUNT(CASE WHEN status='Lost'   THEN 1 END) as lost_count,
                COALESCE(SUM(CASE WHEN status='Lost'   THEN amount ELSE 0 END),0) as lost_amount
            FROM quotes WHERE (deleted IS NULL OR deleted=0)
        """).fetchone()

        # By location
        by_loc = con.execute("""
            SELECT location,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal
            FROM quotes
            WHERE (deleted IS NULL OR deleted=0) AND location IS NOT NULL
            GROUP BY location
            ORDER BY total DESC
        """).fetchall()

        # By region
        by_reg = con.execute("""
            SELECT COALESCE(NULLIF(TRIM(region),''),'(No Region)') as region,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal
            FROM quotes
            WHERE (deleted IS NULL OR deleted=0)
            GROUP BY region
            ORDER BY total DESC
        """).fetchall()

        # By month — parse dates in Python to handle M/D/YYYY, "Jul 13, 2026", etc.
        cutoff = datetime.now().replace(day=1) - timedelta(days=335)  # ~11 months ago
        raw_quotes = con.execute(
            "SELECT date_received, status, amount FROM quotes WHERE (deleted IS NULL OR deleted=0) AND date_received IS NOT NULL AND date_received != ''"
        ).fetchall()
        month_acc = defaultdict(lambda: dict(total_quotes=0,total=0.0,won=0.0,verbal=0.0,lost=0.0,won_count=0,verbal_count=0,lost_count=0))
        for row in raw_quotes:
            dt = parse_date(row["date_received"])
            if not dt or dt < cutoff:
                continue
            ym = dt.strftime('%Y-%m')
            amt = row["amount"] or 0
            st  = row["status"] or ''
            month_acc[ym]['total_quotes'] += 1
            month_acc[ym]['total']        += amt
            if st == 'Won':    month_acc[ym]['won']    += amt; month_acc[ym]['won_count']    += 1
            if st == 'Verbal': month_acc[ym]['verbal'] += amt; month_acc[ym]['verbal_count'] += 1
            if st == 'Lost':   month_acc[ym]['lost']   += amt; month_acc[ym]['lost_count']   += 1
        by_month = [{'month': k, **v} for k, v in sorted(month_acc.items())]

        # By close month — for 12-Month Rolling Projected Sales chart
        raw_close = con.execute(
            "SELECT close_date, status, amount FROM quotes "
            "WHERE (deleted IS NULL OR deleted=0) AND close_date IS NOT NULL AND close_date != ''"
        ).fetchall()
        close_acc = defaultdict(lambda: dict(total=0.0, won=0.0, verbal=0.0, open=0.0))
        for row in raw_close:
            dt = parse_date(row["close_date"])
            if not dt:
                continue
            ym  = dt.strftime('%Y-%m')
            amt = float(row["amount"] or 0)
            st  = (row["status"] or '').strip()
            if st in ('Lost', 'Duplicate'):
                continue
            close_acc[ym]['total'] += amt
            if st == 'Won':
                close_acc[ym]['won']    += amt
            elif st == 'Verbal':
                close_acc[ym]['verbal'] += amt
            else:
                close_acc[ym]['open']   += amt
        by_close_month = [{'month': k, 'total': round(v['total'], 2), 'won': round(v['won'], 2),
                           'verbal': round(v['verbal'], 2), 'open': round(v['open'], 2)}
                          for k, v in sorted(close_acc.items())]

        # Status breakdown
        by_status = con.execute("""
            SELECT status, COUNT(*) as count, COALESCE(SUM(amount),0) as amount
            FROM quotes
            WHERE (deleted IS NULL OR deleted=0) AND status IS NOT NULL
            GROUP BY status
            ORDER BY amount DESC
        """).fetchall()

        # By region and month — for regional monthly trends chart
        raw_rq = con.execute(
            "SELECT date_received, region, amount, status FROM quotes WHERE (deleted IS NULL OR deleted=0) AND date_received IS NOT NULL AND date_received != ''"
        ).fetchall()
        rm_acc = {}
        for row in raw_rq:
            dt = parse_date(row["date_received"])
            if not dt:
                continue
            ym     = dt.strftime('%Y-%m')
            region = (row["region"] or '').strip() or '(No Region)'
            key    = (region, ym)
            if key not in rm_acc:
                rm_acc[key] = {'region': region, 'month': ym, 'total': 0.0, 'count': 0, 'won': 0.0, 'verbal': 0.0}
            amt = float(row["amount"] or 0)
            rm_acc[key]['total'] += amt
            rm_acc[key]['count'] += 1
            st = (row["status"] or '').strip()
            if st == 'Won':
                rm_acc[key]['won'] += amt
            elif st == 'Verbal':
                rm_acc[key]['verbal'] += amt
        by_region_month = [{'region': k[0], 'month': k[1], 'total': round(v['total'], 2), 'count': v['count'],
                            'won': round(v['won'], 2), 'verbal': round(v['verbal'], 2)}
                           for k, v in sorted(rm_acc.items())]

        # Distinct filter options
        locations = [r[0] for r in con.execute(
            "SELECT DISTINCT location FROM quotes WHERE (deleted IS NULL OR deleted=0) AND location IS NOT NULL ORDER BY location"
        ).fetchall()]

        return {
            "totals": dict(totals),
            "by_location": [dict(r) for r in by_loc],
            "by_region": [dict(r) for r in by_reg],
            "by_region_month": by_region_month,
            "by_month": [dict(r) for r in by_month],
            "by_close_month": by_close_month,
            "by_status": [dict(r) for r in by_status],
            "locations": locations,
        }

# ── Overall Sales endpoint (reads live from Excel) ───────────────────────────
SALES_XLSX = os.path.join(BASE_DIR, "..", "Grow 2026 - SPECFORM Sales Plan.xlsx")

# ── Microsoft Graph / OneDrive config ─────────────────────────────────────────
# Set these as environment variables in Railway (never hardcode secrets in code)
GRAPH_TENANT_ID     = os.environ.get("GRAPH_TENANT_ID",     "10e8b460-4e9b-4c2b-b0e1-e102303252e1")
GRAPH_CLIENT_ID     = os.environ.get("GRAPH_CLIENT_ID",     "79b3406d-c46a-4128-912d-4b51c71f43a4")
GRAPH_CLIENT_SECRET = os.environ.get("GRAPH_CLIENT_SECRET", "")
GRAPH_USER          = os.environ.get("GRAPH_USER",          "mwalker@specformbc.com")
GRAPH_FILE_PATH     = os.environ.get("GRAPH_FILE_PATH",     "Desktop/RMAX Weekly Quotes/Grow 2026 - SPECFORM Sales Plan.xlsx")

_token_cache: dict = {"token": None, "expires_at": 0.0}

def _get_graph_token() -> str:
    if _token_cache["token"] and time.time() < _token_cache["expires_at"] - 60:
        return _token_cache["token"]
    try:
        import msal
    except ImportError:
        raise HTTPException(500, "msal not installed — run: pip install msal")
    app = msal.ConfidentialClientApplication(
        GRAPH_CLIENT_ID,
        authority=f"https://login.microsoftonline.com/{GRAPH_TENANT_ID}",
        client_credential=GRAPH_CLIENT_SECRET,
    )
    result = app.acquire_token_for_client(scopes=["https://graph.microsoft.com/.default"])
    if "access_token" not in result:
        raise HTTPException(500, f"Graph auth failed: {result.get('error_description', result)}")
    _token_cache["token"]      = result["access_token"]
    _token_cache["expires_at"] = time.time() + result.get("expires_in", 3600)
    return _token_cache["token"]

def _load_sales_workbook(data_only: bool = True, read_only: bool = False):
    """
    Return an openpyxl Workbook for the sales Excel file.
    Uses Microsoft Graph API when credentials are set; falls back to local file.
    """
    try:
        import openpyxl
    except ImportError:
        raise HTTPException(500, "openpyxl not installed — run: pip install openpyxl")

    if GRAPH_CLIENT_SECRET:
        try:
            import requests as _req
        except ImportError:
            raise HTTPException(500, "requests not installed — run: pip install requests")
        token = _get_graph_token()
        url   = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                 f"/drive/root:/{GRAPH_FILE_PATH}:/content")
        resp  = _req.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=30)
        if resp.status_code != 200:
            raise HTTPException(resp.status_code,
                f"OneDrive download failed ({resp.status_code}): {resp.text[:300]}")
        return openpyxl.load_workbook(io.BytesIO(resp.content),
                                      data_only=data_only, read_only=read_only)

    # ── Local file fallback ────────────────────────────────────────────────────
    xlsx_path = os.path.abspath(SALES_XLSX)
    if not os.path.exists(xlsx_path):
        alt = os.path.abspath(os.path.join(os.getcwd(), "..", "Grow 2026 - SPECFORM Sales Plan.xlsx"))
        if os.path.exists(alt):
            xlsx_path = alt
        else:
            raise HTTPException(404, f"Excel file not found at: {xlsx_path}")
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix='.xlsx', delete=False) as tmp:
            tmp_path = tmp.name
        shutil.copy2(xlsx_path, tmp_path)
        return openpyxl.load_workbook(tmp_path, data_only=data_only, read_only=read_only)
    except PermissionError:
        raise HTTPException(503, "Excel file is locked — close it in Excel and try again")
    except Exception as e:
        raise HTTPException(500, f"Failed to open Excel file: {e}")
    finally:
        if tmp_path and os.path.exists(tmp_path):
            try: os.unlink(tmp_path)
            except: pass

MONTH_SHEETS = [
    "January","February","March","April","May","June",
    "July","August","September","October","November","December"
]
PRODUCTS = ["RMAX", "Hydrotech", "LAM", "Advanced Glassworks"]

def _norm_product(name: str) -> str:
    n = (name or "").strip().upper()
    if "HYDROTECH" in n or "HYDROTECH" in n: return "Hydrotech"
    if "LAM" in n:                            return "LAM"
    if "GLASS" in n:                          return "Advanced Glassworks"
    if "RMAX" in n:                           return "RMAX"
    return name.strip()

def _parse_sheet(ws):
    """Return dict with mtd_actual, mtd_plan, ytd_actual, annual_plan keyed by product."""
    # Discover product columns from the header row (col B = 'SALES')
    col_map = {}  # product_name → column index (0-based)
    for row in ws.iter_rows(max_row=4, values_only=True):
        if row[1] and str(row[1]).strip().upper() == "SALES":
            for i, cell in enumerate(row):
                if i > 1 and cell:
                    col_map[_norm_product(str(cell))] = i
            break

    result = {k: {p: 0 for p in PRODUCTS} for k in ("mtd_actual","mtd_plan","ytd_actual","annual_plan")}
    for row in ws.iter_rows(values_only=True):
        label = str(row[1] or "").strip()
        if not label: continue
        if "MTD" in label.upper() and "PROJECTED" in label.upper():
            key = "mtd_actual"
        elif "MTD" in label.upper() and "AVERAGE" in label.upper():
            key = "mtd_plan"
        elif "YTD" in label.upper() and "SALES" in label.upper():
            key = "ytd_actual"
        elif "PLAN" in label.upper() and "YEARLY" in label.upper():
            key = "annual_plan"
        else:
            continue
        for prod, idx in col_map.items():
            if prod in PRODUCTS:
                try:
                    val = row[idx]
                    result[key][prod] = float(val) if val is not None else 0
                except (TypeError, ValueError):
                    result[key][prod] = 0
    return result

@app.get("/api/sales")
def overall_sales():
    wb = _load_sales_workbook()
    try:
        months = []
        annual_plan = {p: 0 for p in PRODUCTS}

        for month in MONTH_SHEETS:
            if month not in wb.sheetnames:
                continue
            ws = wb[month]
            d  = _parse_sheet(ws)
            # Use the most recent annual plan found
            for p in PRODUCTS:
                if d["annual_plan"][p]:
                    annual_plan[p] = d["annual_plan"][p]

            mtd_total = sum(d["mtd_actual"].values())
            plan_total = sum(d["mtd_plan"].values())
            ytd_total  = sum(d["ytd_actual"].values())
            if mtd_total == 0 and plan_total == 0:
                continue  # skip months with no data at all

            months.append({
                "month":      month,
                "mtd_actual": d["mtd_actual"],
                "mtd_plan":   d["mtd_plan"],
                "ytd_actual": d["ytd_actual"],
                "mtd_total":  mtd_total,
                "plan_total": plan_total,
                "ytd_total":  ytd_total,
            })

        annual_total = sum(annual_plan.values())
        ytd_total_all = months[-1]["ytd_total"] if months else 0

        return {
            "months":       months,
            "annual_plan":  annual_plan,
            "annual_total": annual_total,
            "ytd_total":    ytd_total_all,
            "products":     PRODUCTS,
        }
    except Exception as e:
        raise HTTPException(500, f"Error parsing Excel data: {e}")

# ── Rep Sales endpoint ────────────────────────────────────────────────────────
REP_MONTH_SHEETS = [
    ("Jan",   "Jan per SBC Rep"),
    ("Feb",   "Feb per SBC Rep"),
    ("Mar",   "March per SBC Rep"),
    ("Apr",   "April per SBC Rep"),
    ("May",   "May per SBC Rep"),
    ("Jun",   "June per SBC Rep"),
]
REP_PRODUCTS = ["RMAX", "LAM", "American Hydrotech", "Advanced Glassworks"]

def _norm_rep_product(name: str) -> str:
    n = (name or "").strip().upper()
    if "RMAX"     in n:                          return "RMAX"
    if "LAM"      in n:                          return "LAM"
    if "HYDROTECH" in n or "AMERICAN" in n:      return "American Hydrotech"
    if "GLASS"    in n:                          return "Advanced Glassworks"
    return name.strip()

def _parse_rep_sheet(ws):
    """Parse a per-rep sheet. Returns mtd_plan, mtd_projected, and individual rep rows."""
    col_map = {}
    for row in ws.iter_rows(max_row=6, values_only=True):
        if row[1] and str(row[1]).strip().upper() == "SALES":
            for i, cell in enumerate(row):
                if i > 1 and cell:
                    col_map[_norm_rep_product(str(cell))] = i
            break
    if not col_map:
        return None

    def extract(row):
        out = {}
        for prod, idx in col_map.items():
            if prod in REP_PRODUCTS:
                try:
                    out[prod] = float(row[idx]) if idx < len(row) and row[idx] is not None else 0.0
                except (TypeError, ValueError):
                    out[prod] = 0.0
        return {p: out.get(p, 0.0) for p in REP_PRODUCTS}

    mtd_plan      = {p: 0.0 for p in REP_PRODUCTS}
    mtd_projected = {p: 0.0 for p in REP_PRODUCTS}
    reps = {}

    SKIP_PATTERNS = ["SALES", "GROW 2026", "PER SPECFORM", "PER SBC", "SALES PLAN", "YEARLY"]

    for row in ws.iter_rows(values_only=True):
        label = str(row[1] or "").strip()
        if not label:
            continue
        lu = label.upper()
        if any(p in lu for p in SKIP_PATTERNS):
            continue
        if "MTD" in lu and ("AVERAGE" in lu or "PLAN" in lu):
            mtd_plan = extract(row)
        elif "MTD" in lu and "PROJECTED" in lu:
            mtd_projected = extract(row)
        else:
            reps[label] = extract(row)

    return {"mtd_plan": mtd_plan, "mtd_projected": mtd_projected, "reps": reps}

@app.get("/api/rep-sales")
def rep_sales():
    wb = _load_sales_workbook()
    try:
        months = []
        for month_name, sheet_name in REP_MONTH_SHEETS:
            if sheet_name not in wb.sheetnames:
                continue
            d = _parse_rep_sheet(wb[sheet_name])
            if d:
                months.append({"month": month_name, **d})
        return {"months": months, "products": REP_PRODUCTS}
    except Exception as e:
        raise HTTPException(500, f"Error parsing rep data: {e}")

@app.get("/api/sales/sheets")
def list_sheets():
    """Return all sheet names in the Excel file — used to identify rep tabs."""
    try:
        wb = _load_sales_workbook(read_only=True)
        return {"sheets": list(wb.sheetnames)}
    except Exception as e:
        raise HTTPException(500, str(e))

# ── Hydrotech Quotes ─────────────────────────────────────────────────────────

def init_hydrotech_db():
    with get_db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS hydrotech_quotes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            status             TEXT,
            date_received      TEXT,
            date_quoted        TEXT,
            sent_to            TEXT,
            subject            TEXT,
            job_name           TEXT,
            customer           TEXT,
            location           TEXT,
            product            TEXT,
            price              TEXT,
            quantities         TEXT,
            amount             REAL,
            close_date         TEXT,
            est_freight        TEXT,
            lead_time          TEXT,
            notes              TEXT,
            add_to_salesforce  INTEGER DEFAULT 0,
            completed          INTEGER DEFAULT 0,
            created_at         TEXT DEFAULT (datetime('now')),
            updated_at         TEXT DEFAULT (datetime('now'))
        )""")
        for col, definition in [('add_to_salesforce', 'INTEGER DEFAULT 0'),
                                 ('completed',         'INTEGER DEFAULT 0'),
                                 ('region',            'TEXT'),
                                 ('deleted',           'INTEGER DEFAULT 0'),
                                 ('pdf_filename',      'TEXT'),
                                 ('company_id',        'INTEGER')]:
            try:
                con.execute(f"ALTER TABLE hydrotech_quotes ADD COLUMN {col} {definition}")
            except Exception:
                pass
        # Multi-PDF table
        con.execute("""
        CREATE TABLE IF NOT EXISTS hydrotech_quote_pdfs (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            quote_id     INTEGER NOT NULL,
            pdf_filename TEXT NOT NULL,
            uploaded_at  TEXT DEFAULT (datetime('now'))
        )""")
        # Migrate any existing single pdf_filename values into the new table
        existing = con.execute(
            "SELECT id, pdf_filename FROM hydrotech_quotes WHERE pdf_filename IS NOT NULL AND pdf_filename != ''"
        ).fetchall()
        for row in existing:
            dup = con.execute(
                "SELECT id FROM hydrotech_quote_pdfs WHERE quote_id=? AND pdf_filename=?",
                (row["id"], row["pdf_filename"])
            ).fetchone()
            if not dup:
                con.execute(
                    "INSERT INTO hydrotech_quote_pdfs (quote_id, pdf_filename) VALUES (?,?)",
                    (row["id"], row["pdf_filename"])
                )
    # Ensure PDF storage directory exists
    os.makedirs(os.path.join(DATA_DIR, "hydrotech_pdfs"), exist_ok=True)

@app.get("/api/hydrotech-quotes")
def list_hydrotech_quotes(
    status:   Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search:   Optional[str] = Query(None),
):
    with get_db() as con:
        sql = "SELECT * FROM hydrotech_quotes WHERE (deleted IS NULL OR deleted=0)"
        params = []
        if status and status != "All":
            sql += " AND status = ?"
            params.append(status)
        if location and location != "All":
            sql += " AND location = ?"
            params.append(location)
        if search:
            sql += " AND (subject LIKE ? OR sent_to LIKE ? OR job_name LIKE ? OR customer LIKE ? OR location LIKE ?)"
            s = f"%{search}%"
            params += [s, s, s, s, s]
        sql += " ORDER BY date_received DESC"
        rows = con.execute(sql, params).fetchall()
        quotes = [dict(r) for r in rows]
        if quotes:
            ids = [q["id"] for q in quotes]
            pdf_rows = con.execute(
                f"SELECT id, quote_id, pdf_filename FROM hydrotech_quote_pdfs WHERE quote_id IN ({','.join('?'*len(ids))}) ORDER BY uploaded_at",
                ids
            ).fetchall()
            pdf_map = {}
            for pr in pdf_rows:
                pdf_map.setdefault(pr["quote_id"], []).append({"id": pr["id"], "filename": pr["pdf_filename"]})
            for q in quotes:
                q["pdfs"] = pdf_map.get(q["id"], [])
        return quotes

@app.get("/api/hydrotech-quotes/{quote_id}")
def get_hydrotech_quote(quote_id: int):
    with get_db() as con:
        row = con.execute("SELECT * FROM hydrotech_quotes WHERE id=? AND (deleted IS NULL OR deleted=0)", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Hydrotech quote not found")
        q = dict(row)
        pdf_rows = con.execute(
            "SELECT id, pdf_filename FROM hydrotech_quote_pdfs WHERE quote_id=? ORDER BY uploaded_at", (quote_id,)
        ).fetchall()
        q["pdfs"] = [{"id": pr["id"], "filename": pr["pdf_filename"]} for pr in pdf_rows]
        return q

@app.post("/api/hydrotech-quotes", status_code=201)
def create_hydrotech_quote(q: QuoteIn):
    with get_db() as con:
        cur = con.execute("""
        INSERT INTO hydrotech_quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes,
            region,add_to_salesforce,completed,company_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (q.status,q.date_received,q.date_quoted,q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              q.close_date,q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id))
        return {"id": cur.lastrowid}

@app.put("/api/hydrotech-quotes/{quote_id}")
def update_hydrotech_quote(quote_id: int, q: QuoteIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM hydrotech_quotes WHERE id=?", (quote_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Hydrotech quote not found")
        con.execute("""
        UPDATE hydrotech_quotes SET status=?,date_received=?,date_quoted=?,sent_to=?,subject=?,
            job_name=?,customer=?,location=?,product=?,price=?,quantities=?,amount=?,
            close_date=?,est_freight=?,lead_time=?,notes=?,
            region=?,add_to_salesforce=?,completed=?,company_id=?,updated_at=datetime('now')
        WHERE id=?
        """, (q.status,q.date_received,q.date_quoted,q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              q.close_date,q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id,quote_id))
        return {"ok": True}

@app.delete("/api/hydrotech-quotes/{quote_id}")
def delete_hydrotech_quote(quote_id: int):
    with get_db() as con:
        con.execute("UPDATE hydrotech_quotes SET deleted=1 WHERE id=?", (quote_id,))
        return {"ok": True}

@app.post("/api/hydrotech-quotes/{quote_id}/upload-pdf")
async def upload_hydrotech_pdf(quote_id: int, file: UploadFile):
    """Manually attach a PDF to an existing Hydrotech quote (supports multiple)."""
    os.makedirs(os.path.join(DATA_DIR, "hydrotech_pdfs"), exist_ok=True)
    safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in file.filename)
    pdf_filename = f"{quote_id}_{safe_name}"
    pdf_path = os.path.join(DATA_DIR, "hydrotech_pdfs", pdf_filename)
    contents = await file.read()
    with open(pdf_path, "wb") as f:
        f.write(contents)
    with get_db() as con:
        con.execute("INSERT INTO hydrotech_quote_pdfs (quote_id, pdf_filename) VALUES (?,?)", (quote_id, pdf_filename))
    return {"ok": True, "pdf_filename": pdf_filename}

@app.delete("/api/hydrotech-pdfs/{pdf_id}")
def delete_hydrotech_pdf(pdf_id: int):
    """Delete a single PDF attachment from a Hydrotech quote."""
    with get_db() as con:
        row = con.execute("SELECT pdf_filename FROM hydrotech_quote_pdfs WHERE id=?", (pdf_id,)).fetchone()
        if not row:
            raise HTTPException(404, "PDF not found")
        pdf_path = os.path.join(DATA_DIR, "hydrotech_pdfs", row["pdf_filename"])
        if os.path.exists(pdf_path):
            os.remove(pdf_path)
        con.execute("DELETE FROM hydrotech_quote_pdfs WHERE id=?", (pdf_id,))
    return {"ok": True}

@app.get("/api/hydrotech-pdf/{filename}")
def serve_hydrotech_pdf(filename: str):
    """Serve a saved Hydrotech quote PDF attachment."""
    # Sanitize: no path traversal
    safe = os.path.basename(filename)
    pdf_path = os.path.join(DATA_DIR, "hydrotech_pdfs", safe)
    if not os.path.exists(pdf_path):
        raise HTTPException(404, "PDF not found")
    return FileResponse(pdf_path, media_type="application/pdf",
                        headers={"Content-Disposition": f'inline; filename="{safe}"'})

@app.get("/api/hydrotech-quotes-export")
def export_hydrotech_quotes():
    """Export all Hydrotech quotes to a formatted Excel file."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse

    with get_db() as con:
        rows = con.execute("""
            SELECT id, status, date_received, date_quoted, sent_to, subject,
                   job_name, customer, location, product, price, quantities,
                   amount, close_date, est_freight, lead_time, notes,
                   add_to_salesforce, completed
            FROM hydrotech_quotes ORDER BY date_received DESC
        """).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Hydrotech Quotes"

    header_font  = Font(name='Calibri', bold=True, color='FFFFFF', size=11)
    header_fill  = PatternFill('solid', fgColor='166534')   # dark green for Hydrotech
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    border_side  = Side(style='thin', color='BFBFBF')
    cell_border  = Border(left=border_side, right=border_side,
                          top=border_side, bottom=border_side)
    zebra_fill   = PatternFill('solid', fgColor='DCFCE7')
    STATUS_FILLS = {
        'Won':    PatternFill('solid', fgColor='D9EAD3'),
        'Lost':   PatternFill('solid', fgColor='F4CCCC'),
        'Verbal': PatternFill('solid', fgColor='FFF2CC'),
    }

    headers = [
        ('ID', 8), ('Status', 12), ('Date Received', 14), ('Date Quoted', 14),
        ('Sent To', 22), ('Subject', 30), ('Job Name', 28), ('Customer', 22),
        ('Location', 18), ('Product', 16), ('Price', 12), ('Quantities', 14),
        ('Amount', 14), ('Close Date', 14), ('Est. Freight', 14), ('Lead Time', 14),
        ('Notes', 35), ('Salesforce', 12), ('Completed', 12),
    ]
    ws.row_dimensions[1].height = 30
    for col_idx, (label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = cell_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = 'A2'

    for row_idx, q in enumerate(rows, start=2):
        status   = q['status'] or ''
        row_fill = STATUS_FILLS.get(status, (zebra_fill if row_idx % 2 == 0 else None))
        values = [
            q['id'], q['status'], q['date_received'], q['date_quoted'],
            q['sent_to'], q['subject'], q['job_name'], q['customer'],
            q['location'], q['product'], q['price'], q['quantities'],
            q['amount'], q['close_date'], q['est_freight'], q['lead_time'],
            q['notes'],
            'Yes' if q['add_to_salesforce'] else '',
            'Yes' if q['completed'] else '',
        ]
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = cell_border
            cell.alignment = Alignment(vertical='top', wrap_text=(col_idx in (6, 7, 17)))
            if row_fill:
                cell.fill = row_fill
            if col_idx == 13 and value is not None:
                cell.number_format = '$#,##0.00'
            if col_idx in (1, 2, 18, 19):
                cell.alignment = Alignment(horizontal='center', vertical='top')

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"Hydrotech_Quotes_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )

@app.post("/api/hydrotech-sync")
def hydrotech_sync():
    """
    Pull new emails from the 'Hydrotech Quotes' Outlook folder via Graph API,
    parse each email body to pre-populate quote fields, and insert any
    that are not already in the database.
    """
    if not GRAPH_CLIENT_SECRET:
        return {"inserted": 0, "skipped": 0, "message": "Graph API not configured"}

    try:
        import requests as _req
    except ImportError:
        return {"inserted": 0, "skipped": 0, "message": "requests package not installed"}

    try:
        token = _get_graph_token()
    except Exception as e:
        return {"inserted": 0, "skipped": 0, "message": f"Auth failed: {e}"}

    hdrs = {"Authorization": f"Bearer {token}"}

    # ── Find "Hydrotech Quotes" folder ───────────────────────────────────────
    folder_id = None
    for list_url in [
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders?$select=id,displayName&$top=100",
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/Inbox/childFolders?$select=id,displayName&$top=100",
    ]:
        try:
            r = _req.get(list_url, headers=hdrs, timeout=20)
            if r.status_code == 200:
                for f in r.json().get("value", []):
                    if (f.get("displayName") or "").strip().lower() == "hydrotech quotes":
                        folder_id = f["id"]
                        break
            if folder_id:
                break
        except Exception:
            pass

    if not folder_id:
        return {"inserted": 0, "skipped": 0,
                "message": "Could not find 'Hydrotech Quotes' folder in Outlook — check folder name"}

    # ── Fetch messages with body ──────────────────────────────────────────────
    msgs = []
    next_url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                f"/mailFolders/{folder_id}/messages"
                f"?$select=id,from,toRecipients,subject,receivedDateTime,body,hasAttachments&$top=50")
    while next_url and len(msgs) < 200:
        try:
            r = _req.get(next_url, headers=hdrs, timeout=30)
            if r.status_code != 200:
                return {"inserted": 0, "skipped": 0,
                        "message": f"Graph API error {r.status_code}: {r.text[:200]}"}
            data = r.json()
            msgs.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
        except Exception as e:
            return {"inserted": 0, "skipped": 0, "message": f"Fetch error: {e}"}

    # ── Insert / update quotes ────────────────────────────────────────────────
    inserted = updated = skipped = 0
    with get_db() as con:
        for msg in msgs:
            # For Hydrotech, the customer is the *sender* of the incoming quote request
            from_addr = msg.get("from", {}).get("emailAddress", {})
            sent_to   = (from_addr.get("address") or from_addr.get("name") or "").strip()

            subject       = (msg.get("subject") or "").strip()
            received_raw  = msg.get("receivedDateTime", "")
            date_received = received_raw[:10] if received_raw else ""

            exists = con.execute(
                "SELECT id, sent_to FROM hydrotech_quotes WHERE subject=? AND date_received=?",
                (subject, date_received)
            ).fetchone()
            if exists:
                # Only fill in sent_to if blank — never overwrite a user-edited value
                if sent_to and not exists["sent_to"]:
                    con.execute("UPDATE hydrotech_quotes SET sent_to=? WHERE id=?",
                                (sent_to, exists["id"]))
                    updated += 1
                else:
                    skipped += 1
                continue

            body_content = msg.get("body", {}).get("content", "")
            content_type = msg.get("body", {}).get("contentType", "text")
            parsed = _parse_quote_email(subject, body_content, content_type)

            cur = con.execute("""
            INSERT INTO hydrotech_quotes
                (date_received, sent_to, subject, job_name, customer, location,
                 product, price, quantities, amount, est_freight, lead_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_received, sent_to, subject,
                parsed.get('job_name'), parsed.get('customer'), parsed.get('location'),
                parsed.get('product'), parsed.get('price'), parsed.get('quantities'),
                parsed.get('amount'), parsed.get('est_freight'), parsed.get('lead_time'),
            ))
            new_id = cur.lastrowid
            inserted += 1

            # ── Save PDF attachment if present ────────────────────────────────
            if msg.get("hasAttachments") and msg.get("id"):
                try:
                    att_url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                               f"/messages/{msg['id']}/attachments"
                               f"?$select=name,contentType,contentBytes&$top=20")
                    att_r = _req.get(att_url, headers=hdrs, timeout=30)
                    if att_r.status_code == 200:
                        for att in att_r.json().get("value", []):
                            ct = (att.get("contentType") or "").lower()
                            name = (att.get("name") or "attachment.pdf")
                            if "pdf" in ct or name.lower().endswith(".pdf"):
                                import base64 as _b64
                                pdf_bytes = _b64.b64decode(att.get("contentBytes", ""))
                                # Sanitize filename and prefix with quote id
                                safe_name = "".join(c if c.isalnum() or c in "._- " else "_" for c in name)
                                pdf_filename = f"{new_id}_{safe_name}"
                                pdf_path = os.path.join(DATA_DIR, "hydrotech_pdfs", pdf_filename)
                                with open(pdf_path, "wb") as pf:
                                    pf.write(pdf_bytes)
                                con.execute("INSERT INTO hydrotech_quote_pdfs (quote_id, pdf_filename) VALUES (?,?)",
                                            (new_id, pdf_filename))
                                # continue loop to save ALL PDF attachments (not just first)
                except Exception:
                    pass  # don't fail the import if PDF save fails

    parts = []
    if inserted: parts.append(f"{inserted} new quote{'' if inserted==1 else 's'} imported")
    if updated:  parts.append(f"{updated} updated")
    msg_text = "✓ " + ", ".join(parts) if parts else "✓ Already up to date"
    return {"inserted": inserted, "skipped": skipped, "message": msg_text}

# ── Glassworks Quotes ────────────────────────────────────────────────────────

def init_glassworks_db():
    with get_db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS glassworks_quotes (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            status             TEXT,
            date_received      TEXT,
            date_quoted        TEXT,
            sent_to            TEXT,
            subject            TEXT,
            job_name           TEXT,
            customer           TEXT,
            location           TEXT,
            product            TEXT,
            price              TEXT,
            quantities         TEXT,
            amount             REAL,
            close_date         TEXT,
            est_freight        TEXT,
            lead_time          TEXT,
            notes              TEXT,
            region             TEXT,
            add_to_salesforce  INTEGER DEFAULT 0,
            completed          INTEGER DEFAULT 0,
            deleted            INTEGER DEFAULT 0,
            created_at         TEXT DEFAULT (datetime('now')),
            updated_at         TEXT DEFAULT (datetime('now'))
        )""")
        for col, definition in [('add_to_salesforce', 'INTEGER DEFAULT 0'),
                                 ('completed',         'INTEGER DEFAULT 0'),
                                 ('region',            'TEXT'),
                                 ('deleted',           'INTEGER DEFAULT 0'),
                                 ('company_id',        'INTEGER')]:
            try:
                con.execute(f"ALTER TABLE glassworks_quotes ADD COLUMN {col} {definition}")
            except Exception:
                pass

@app.get("/api/glassworks-quotes")
def list_glassworks_quotes(
    status:   Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search:   Optional[str] = Query(None),
):
    with get_db() as con:
        sql = "SELECT * FROM glassworks_quotes WHERE (deleted IS NULL OR deleted=0)"
        params = []
        if status and status != "All":
            sql += " AND status = ?"
            params.append(status)
        if location and location != "All":
            sql += " AND location = ?"
            params.append(location)
        if search:
            sql += " AND (subject LIKE ? OR sent_to LIKE ? OR job_name LIKE ? OR customer LIKE ? OR location LIKE ?)"
            s = f"%{search}%"
            params += [s, s, s, s, s]
        sql += " ORDER BY date_received DESC"
        rows = con.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

@app.get("/api/glassworks-quotes/{quote_id}")
def get_glassworks_quote(quote_id: int):
    with get_db() as con:
        row = con.execute("SELECT * FROM glassworks_quotes WHERE id=? AND (deleted IS NULL OR deleted=0)", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Glassworks quote not found")
        return dict(row)

@app.post("/api/glassworks-quotes", status_code=201)
def create_glassworks_quote(q: QuoteIn):
    with get_db() as con:
        cur = con.execute("""
        INSERT INTO glassworks_quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes,
            region,add_to_salesforce,completed,company_id)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id))
        return {"id": cur.lastrowid}

@app.put("/api/glassworks-quotes/{quote_id}")
def update_glassworks_quote(quote_id: int, q: QuoteIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM glassworks_quotes WHERE id=?", (quote_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Glassworks quote not found")
        con.execute("""
        UPDATE glassworks_quotes SET status=?,date_received=?,date_quoted=?,sent_to=?,subject=?,
            job_name=?,customer=?,location=?,product=?,price=?,quantities=?,amount=?,
            close_date=?,est_freight=?,lead_time=?,notes=?,
            region=?,add_to_salesforce=?,completed=?,company_id=?,updated_at=datetime('now')
        WHERE id=?
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.region,q.add_to_salesforce,q.completed,q.company_id,quote_id))
        return {"ok": True}

@app.delete("/api/glassworks-quotes/{quote_id}")
def delete_glassworks_quote(quote_id: int):
    with get_db() as con:
        con.execute("UPDATE glassworks_quotes SET deleted=1 WHERE id=?", (quote_id,))
        return {"ok": True}

@app.get("/api/glassworks-quotes-export")
def export_glassworks_quotes():
    """Export all Glassworks quotes to a formatted Excel file."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
    from openpyxl.utils import get_column_letter
    from fastapi.responses import StreamingResponse
    import io

    with get_db() as con:
        rows = con.execute("""
            SELECT id, status, date_received, date_quoted, sent_to, subject,
                   job_name, customer, location, product, price, quantities,
                   amount, close_date, est_freight, lead_time, notes,
                   add_to_salesforce, completed
            FROM glassworks_quotes WHERE (deleted IS NULL OR deleted=0) ORDER BY date_received DESC
        """).fetchall()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Glassworks Quotes"

    header_font  = Font(name='Calibri', bold=True, color='FFFFFF', size=11)
    header_fill  = PatternFill('solid', fgColor='0e7490')   # teal for Glassworks
    header_align = Alignment(horizontal='center', vertical='center', wrap_text=True)
    border_side  = Side(style='thin', color='BFBFBF')
    cell_border  = Border(left=border_side, right=border_side,
                          top=border_side, bottom=border_side)
    zebra_fill   = PatternFill('solid', fgColor='CFFAFE')
    STATUS_FILLS = {
        'Won':    PatternFill('solid', fgColor='D9EAD3'),
        'Lost':   PatternFill('solid', fgColor='F4CCCC'),
        'Verbal': PatternFill('solid', fgColor='FFF2CC'),
    }

    headers = [
        ('ID', 8), ('Status', 12), ('Date Received', 14), ('Date Quoted', 14),
        ('Sent To', 22), ('Subject', 30), ('Job Name', 28), ('Customer', 22),
        ('Location', 18), ('Product', 16), ('Price', 12), ('Quantities', 14),
        ('Amount', 14), ('Close Date', 14), ('Est. Freight', 14), ('Lead Time', 14),
        ('Notes', 35), ('Salesforce', 12), ('Completed', 12),
    ]
    ws.row_dimensions[1].height = 30
    for col_idx, (label, width) in enumerate(headers, start=1):
        cell = ws.cell(row=1, column=col_idx, value=label)
        cell.font      = header_font
        cell.fill      = header_fill
        cell.alignment = header_align
        cell.border    = cell_border
        ws.column_dimensions[get_column_letter(col_idx)].width = width
    ws.freeze_panes = 'A2'

    for row_idx, q in enumerate(rows, start=2):
        status   = q['status'] or ''
        row_fill = STATUS_FILLS.get(status, (zebra_fill if row_idx % 2 == 0 else None))
        values = [
            q['id'], q['status'], q['date_received'], q['date_quoted'],
            q['sent_to'], q['subject'], q['job_name'], q['customer'],
            q['location'], q['product'], q['price'], q['quantities'],
            q['amount'], q['close_date'], q['est_freight'], q['lead_time'],
            q['notes'],
            'Yes' if q['add_to_salesforce'] else '',
            'Yes' if q['completed'] else '',
        ]
        for col_idx, value in enumerate(values, start=1):
            cell = ws.cell(row=row_idx, column=col_idx, value=value)
            cell.border    = cell_border
            cell.alignment = Alignment(vertical='top', wrap_text=(col_idx in (6, 7, 17)))
            if row_fill:
                cell.fill = row_fill
            if col_idx == 13 and value is not None:
                cell.number_format = '$#,##0.00'
            if col_idx in (1, 2, 18, 19):
                cell.alignment = Alignment(horizontal='center', vertical='top')

    ws.auto_filter.ref = f"A1:{get_column_letter(len(headers))}1"
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    filename = f"Glassworks_Quotes_{datetime.now().strftime('%Y%m%d')}.xlsx"
    return StreamingResponse(
        buf,
        media_type='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        headers={'Content-Disposition': f'attachment; filename="{filename}"'}
    )

@app.post("/api/glassworks-sync")
def glassworks_sync():
    """Pull new emails from 'Glassworks Quotes' Outlook folder via Graph API."""
    if not GRAPH_CLIENT_SECRET:
        return {"inserted": 0, "skipped": 0, "message": "Graph API not configured"}

    try:
        import requests as _req
    except ImportError:
        return {"inserted": 0, "skipped": 0, "message": "requests package not installed"}

    try:
        token = _get_graph_token()
    except Exception as e:
        return {"inserted": 0, "skipped": 0, "message": f"Auth failed: {e}"}

    hdrs = {"Authorization": f"Bearer {token}"}

    folder_id = None
    for list_url in [
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders?$select=id,displayName&$top=100",
        f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/Inbox/childFolders?$select=id,displayName&$top=100",
    ]:
        try:
            r = _req.get(list_url, headers=hdrs, timeout=20)
            if r.status_code == 200:
                for f in r.json().get("value", []):
                    if (f.get("displayName") or "").strip().lower() == "glassworks quotes":
                        folder_id = f["id"]
                        break
            if folder_id:
                break
        except Exception:
            pass

    if not folder_id:
        return {"inserted": 0, "skipped": 0,
                "message": "Could not find 'Glassworks Quotes' folder in Outlook — check folder name"}

    msgs = []
    next_url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                f"/mailFolders/{folder_id}/messages"
                f"?$select=from,toRecipients,subject,receivedDateTime,body&$top=50")
    while next_url and len(msgs) < 200:
        try:
            r = _req.get(next_url, headers=hdrs, timeout=30)
            if r.status_code != 200:
                return {"inserted": 0, "skipped": 0,
                        "message": f"Graph API error {r.status_code}: {r.text[:200]}"}
            data = r.json()
            msgs.extend(data.get("value", []))
            next_url = data.get("@odata.nextLink")
        except Exception as e:
            return {"inserted": 0, "skipped": 0, "message": f"Fetch error: {e}"}

    inserted = updated = skipped = 0
    with get_db() as con:
        for msg in msgs:
            to_recipients = msg.get("toRecipients", [])
            if to_recipients:
                first_to = to_recipients[0].get("emailAddress", {})
                sent_to  = (first_to.get("name") or first_to.get("address") or "").strip()
            else:
                sent_to  = ""

            subject       = (msg.get("subject") or "").strip()
            received_raw  = msg.get("receivedDateTime", "")
            date_received = received_raw[:10] if received_raw else ""

            exists = con.execute(
                "SELECT id, sent_to FROM glassworks_quotes WHERE subject=? AND date_received=?",
                (subject, date_received)
            ).fetchone()
            if exists:
                # Only fill in sent_to if blank — never overwrite a user-edited value
                if sent_to and not exists["sent_to"]:
                    con.execute("UPDATE glassworks_quotes SET sent_to=? WHERE id=?",
                                (sent_to, exists["id"]))
                    updated += 1
                else:
                    skipped += 1
                continue

            body_content = msg.get("body", {}).get("content", "")
            content_type = msg.get("body", {}).get("contentType", "text")
            parsed = _parse_quote_email(subject, body_content, content_type)

            con.execute("""
            INSERT INTO glassworks_quotes
                (date_received, sent_to, subject, job_name, customer, location,
                 product, price, quantities, amount, est_freight, lead_time)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, (
                date_received, sent_to, subject,
                parsed.get('job_name'), parsed.get('customer'), parsed.get('location'),
                parsed.get('product'), parsed.get('price'), parsed.get('quantities'),
                parsed.get('amount'), parsed.get('est_freight'), parsed.get('lead_time'),
            ))
            inserted += 1

    parts = []
    if inserted: parts.append(f"{inserted} new quote{'' if inserted==1 else 's'} imported")
    if updated:  parts.append(f"{updated} updated")
    msg_text = "✓ " + ", ".join(parts) if parts else "✓ Already up to date"
    return {"inserted": inserted, "skipped": skipped, "message": msg_text}

@app.get("/api/hydrotech-dashboard")
def hydrotech_dashboard():
    with get_db() as con:
        totals = con.execute("""
            SELECT
                COUNT(*) as total_quotes,
                COALESCE(SUM(amount),0) as total_amount,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won_amount,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal_amount,
                COUNT(CASE WHEN status='Won'    THEN 1 END) as won_count,
                COUNT(CASE WHEN status='Verbal' THEN 1 END) as verbal_count,
                COUNT(CASE WHEN status='Lost'   THEN 1 END) as lost_count,
                COALESCE(SUM(CASE WHEN status='Lost'   THEN amount ELSE 0 END),0) as lost_amount
            FROM hydrotech_quotes
        """).fetchone()
        by_loc = con.execute("""
            SELECT location,
                COUNT(*) as count,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal
            FROM hydrotech_quotes WHERE location IS NOT NULL
            GROUP BY location ORDER BY total DESC
        """).fetchall()
        by_month = con.execute("""
            SELECT substr(date_received,1,7) as month,
                COUNT(*) as total_quotes,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal,
                COALESCE(SUM(CASE WHEN status='Lost'   THEN amount ELSE 0 END),0) as lost,
                COUNT(CASE WHEN status='Won'    THEN 1 END) as won_count,
                COUNT(CASE WHEN status='Verbal' THEN 1 END) as verbal_count,
                COUNT(CASE WHEN status='Lost'   THEN 1 END) as lost_count
            FROM hydrotech_quotes
            WHERE date_received IS NOT NULL AND date_received != ''
              AND substr(date_received,1,7) >= substr(date('now','-11 months'),1,7)
            GROUP BY month ORDER BY month
        """).fetchall()
        by_status = con.execute("""
            SELECT status, COUNT(*) as count, COALESCE(SUM(amount),0) as amount
            FROM hydrotech_quotes WHERE status IS NOT NULL
            GROUP BY status ORDER BY amount DESC
        """).fetchall()
        locations = [r[0] for r in con.execute(
            "SELECT DISTINCT location FROM hydrotech_quotes WHERE location IS NOT NULL ORDER BY location"
        ).fetchall()]
        raw_close_ht = con.execute(
            "SELECT close_date, status, amount FROM hydrotech_quotes "
            "WHERE (deleted IS NULL OR deleted=0) AND close_date IS NOT NULL AND close_date != ''"
        ).fetchall()
        from collections import defaultdict as _dd2
        close_acc_ht = _dd2(lambda: dict(total=0.0, won=0.0, verbal=0.0, open=0.0))
        for row in raw_close_ht:
            dt = parse_date(row["close_date"])
            if not dt: continue
            ym = dt.strftime('%Y-%m')
            amt = float(row["amount"] or 0)
            st = (row["status"] or '').strip()
            if st in ('Lost', 'Duplicate'): continue
            close_acc_ht[ym]['total'] += amt
            if st == 'Won':     close_acc_ht[ym]['won']    += amt
            elif st == 'Verbal': close_acc_ht[ym]['verbal'] += amt
            else:                close_acc_ht[ym]['open']   += amt
        by_close_month_ht = [{'month': k, 'total': round(v['total'], 2), 'won': round(v['won'], 2),
                               'verbal': round(v['verbal'], 2), 'open': round(v['open'], 2)}
                              for k, v in sorted(close_acc_ht.items())]
        return {
            "totals": dict(totals),
            "by_location": [dict(r) for r in by_loc],
            "by_month": [dict(r) for r in by_month],
            "by_status": [dict(r) for r in by_status],
            "locations": locations,
            "by_close_month": by_close_month_ht,
        }

@app.get("/api/glassworks-dashboard")
def glassworks_dashboard():
    with get_db() as con:
        totals = con.execute("""
            SELECT
                COUNT(*) as total_quotes,
                COALESCE(SUM(amount),0) as total_amount,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won_amount,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal_amount,
                COUNT(CASE WHEN status='Won'    THEN 1 END) as won_count,
                COUNT(CASE WHEN status='Verbal' THEN 1 END) as verbal_count,
                COUNT(CASE WHEN status='Lost'   THEN 1 END) as lost_count,
                COALESCE(SUM(CASE WHEN status='Lost'   THEN amount ELSE 0 END),0) as lost_amount
            FROM glassworks_quotes WHERE (deleted IS NULL OR deleted=0)
        """).fetchone()
        by_loc = con.execute("""
            SELECT location,
                COUNT(*) as count,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal
            FROM glassworks_quotes WHERE (deleted IS NULL OR deleted=0) AND location IS NOT NULL
            GROUP BY location ORDER BY total DESC
        """).fetchall()
        by_month = con.execute("""
            SELECT substr(date_received,1,7) as month,
                COUNT(*) as total_quotes,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal,
                COALESCE(SUM(CASE WHEN status='Lost'   THEN amount ELSE 0 END),0) as lost,
                COUNT(CASE WHEN status='Won'    THEN 1 END) as won_count,
                COUNT(CASE WHEN status='Verbal' THEN 1 END) as verbal_count,
                COUNT(CASE WHEN status='Lost'   THEN 1 END) as lost_count
            FROM glassworks_quotes
            WHERE (deleted IS NULL OR deleted=0) AND date_received IS NOT NULL AND date_received != ''
              AND substr(date_received,1,7) >= substr(date('now','-11 months'),1,7)
            GROUP BY month ORDER BY month
        """).fetchall()
        by_status = con.execute("""
            SELECT status, COUNT(*) as count, COALESCE(SUM(amount),0) as amount
            FROM glassworks_quotes WHERE (deleted IS NULL OR deleted=0) AND status IS NOT NULL
            GROUP BY status ORDER BY amount DESC
        """).fetchall()
        raw_close_gw = con.execute(
            "SELECT close_date, status, amount FROM glassworks_quotes "
            "WHERE (deleted IS NULL OR deleted=0) AND close_date IS NOT NULL AND close_date != ''"
        ).fetchall()
        from collections import defaultdict as _dd3
        close_acc_gw = _dd3(lambda: dict(total=0.0, won=0.0, verbal=0.0, open=0.0))
        for row in raw_close_gw:
            dt = parse_date(row["close_date"])
            if not dt: continue
            ym = dt.strftime('%Y-%m')
            amt = float(row["amount"] or 0)
            st = (row["status"] or '').strip()
            if st in ('Lost', 'Duplicate'): continue
            close_acc_gw[ym]['total'] += amt
            if st == 'Won':      close_acc_gw[ym]['won']    += amt
            elif st == 'Verbal': close_acc_gw[ym]['verbal'] += amt
            else:                close_acc_gw[ym]['open']   += amt
        by_close_month_gw = [{'month': k, 'total': round(v['total'], 2), 'won': round(v['won'], 2),
                               'verbal': round(v['verbal'], 2), 'open': round(v['open'], 2)}
                              for k, v in sorted(close_acc_gw.items())]
        return {
            "totals": dict(totals),
            "by_location": [dict(r) for r in by_loc],
            "by_month": [dict(r) for r in by_month],
            "by_status": [dict(r) for r in by_status],
            "by_close_month": by_close_month_gw,
        }

# ── Contacts ─────────────────────────────────────────────────────────────────

import re as _re

def _strip_html(html: str) -> str:
    html = _re.sub(r'<style[^>]*>.*?</style>', '', html, flags=_re.DOTALL|_re.IGNORECASE)
    html = _re.sub(r'<script[^>]*>.*?</script>', '', html, flags=_re.DOTALL|_re.IGNORECASE)
    html = _re.sub(r'<br\s*/?>', '\n', html, flags=_re.IGNORECASE)
    html = _re.sub(r'</?(?:p|div|tr|td|li|h\d)[^>]*>', '\n', html, flags=_re.IGNORECASE)
    html = _re.sub(r'<[^>]+>', '', html)
    return (html.replace('&nbsp;', ' ').replace('&amp;', '&')
                .replace('&lt;', '<').replace('&gt;', '>').replace('&#13;', '').replace('\r', ''))

def _extract_phone(text: str) -> str:
    m = _re.search(r'(\+?1?[-.\s]?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4})', text)
    return m.group(1).strip() if m else ''

def _extract_sig_text(body_text: str) -> str:
    """Return the likely email signature block (last few lines before any reply chain)."""
    sep_patterns = [
        r'^-{3,}[\s\S]*?original\s+message',
        r'^on\s+.{10,200}\s+wrote:',
        r'^from:\s*\S+@',
        r'^sent\s+from\s+my',
        r'^_{5,}',
    ]
    lines = body_text.split('\n')
    cutoff = len(lines)
    for i, line in enumerate(lines):
        for pat in sep_patterns:
            if _re.match(pat, line.strip(), _re.IGNORECASE):
                cutoff = i
                break
        if cutoff < len(lines):
            break
    non_empty = [l for l in lines[:cutoff] if l.strip()]
    if len(non_empty) <= 1:
        return ''
    return '\n'.join(non_empty[-8:])

def init_contacts_db():
    with get_db() as con:
        con.execute("""
        CREATE TABLE IF NOT EXISTS contacts (
            id              INTEGER PRIMARY KEY AUTOINCREMENT,
            name            TEXT,
            company         TEXT,
            phone           TEXT,
            email           TEXT UNIQUE,
            location        TEXT,
            product_line    TEXT,
            customer_type   TEXT,
            notes           TEXT,
            manually_edited INTEGER DEFAULT 0,
            created_at      TEXT DEFAULT (datetime('now')),
            updated_at      TEXT DEFAULT (datetime('now'))
        )""")
        # Add columns to existing tables that predate this change
        for col, defn in [
            ("manually_edited", "INTEGER DEFAULT 0"),
            ("customer_type", "TEXT"),
            ("region", "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE contacts ADD COLUMN {col} {defn}")
            except Exception:
                pass  # Column already exists
        con.execute("""
        CREATE TABLE IF NOT EXISTS companies (
            id                        INTEGER PRIMARY KEY AUTOINCREMENT,
            name                      TEXT NOT NULL,
            address                   TEXT,
            phone                     TEXT,
            website                   TEXT,
            region                    TEXT,
            notes                     TEXT,
            strong_market_partner     INTEGER DEFAULT 0,
            large_account_opportunity INTEGER DEFAULT 0,
            created_at                TEXT DEFAULT (datetime('now')),
            updated_at                TEXT DEFAULT (datetime('now'))
        )""")
        # Migrations for companies columns added after initial deploy
        for col, defn in [
            ("strong_market_partner",     "INTEGER DEFAULT 0"),
            ("large_account_opportunity", "INTEGER DEFAULT 0"),
            ("region",                    "TEXT"),
        ]:
            try:
                con.execute(f"ALTER TABLE companies ADD COLUMN {col} {defn}")
            except Exception:
                pass
        con.execute("""
        CREATE TABLE IF NOT EXISTS company_contacts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            company_id  INTEGER NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
            contact_id  INTEGER NOT NULL REFERENCES contacts(id) ON DELETE CASCADE,
            role        TEXT,
            UNIQUE(company_id, contact_id)
        )""")

class ContactIn(BaseModel):
    name:          Optional[str] = None
    company:       Optional[str] = None
    phone:         Optional[str] = None
    email:         Optional[str] = None
    location:      Optional[str] = None
    product_line:  Optional[str] = None
    customer_type: Optional[str] = None
    region:        Optional[str] = None
    notes:         Optional[str] = None

@app.get("/api/contacts")
def list_contacts(
    product_line: Optional[str] = Query(None),
    location:     Optional[str] = Query(None),
    search:       Optional[str] = Query(None),
):
    with get_db() as con:
        sql = "SELECT * FROM contacts WHERE 1=1"
        params = []
        if product_line and product_line != 'All':
            sql += " AND (product_line = ? OR product_line = 'Both')"
            params.append(product_line)
        if location and location != 'All':
            sql += " AND location = ?"
            params.append(location)
        if search:
            sql += " AND (name LIKE ? OR company LIKE ? OR email LIKE ? OR location LIKE ? OR phone LIKE ?)"
            s = f"%{search}%"
            params += [s, s, s, s, s]
        sql += " ORDER BY COALESCE(NULLIF(location,''),'zzz'), COALESCE(NULLIF(name,''),'zzz'), email"
        return [dict(r) for r in con.execute(sql, params).fetchall()]

@app.post("/api/contacts", status_code=201)
def create_contact(c: ContactIn):
    with get_db() as con:
        try:
            cur = con.execute("""
            INSERT INTO contacts (name,company,phone,email,location,product_line,customer_type,region,notes)
            VALUES (?,?,?,?,?,?,?,?,?)
            """, (c.name,c.company,c.phone,c.email,c.location,c.product_line,c.customer_type,c.region,c.notes))
            return {"id": cur.lastrowid}
        except Exception:
            raise HTTPException(409, "A contact with this email already exists")

@app.put("/api/contacts/{contact_id}")
def update_contact(contact_id: int, c: ContactIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM contacts WHERE id=?", (contact_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Contact not found")
        con.execute("""
        UPDATE contacts SET name=?,company=?,phone=?,email=?,location=?,product_line=?,
            customer_type=?,region=?,notes=?,manually_edited=1,updated_at=datetime('now')
        WHERE id=?
        """, (c.name,c.company,c.phone,c.email,c.location,c.product_line,c.customer_type,c.region,c.notes,contact_id))
        return {"ok": True}

@app.delete("/api/contacts/{contact_id}")
def delete_contact(contact_id: int):
    with get_db() as con:
        con.execute("DELETE FROM contacts WHERE id=?", (contact_id,))
        return {"ok": True}

_EMAIL_RE = _re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')

@app.post("/api/contacts/import-from-quotes")
def import_contacts_from_quotes():
    """Scan both quote tables and create contact entries for unique customers / sent_to emails."""
    from collections import defaultdict
    with get_db() as con:
        rmax_rows  = con.execute("SELECT DISTINCT sent_to, customer, location FROM quotes WHERE (deleted IS NULL OR deleted=0) AND sent_to IS NOT NULL AND sent_to != ''").fetchall()
        hydro_rows = con.execute("SELECT DISTINCT sent_to, customer, location FROM hydrotech_quotes WHERE sent_to IS NOT NULL AND sent_to != ''").fetchall()

        # Build a map keyed by email (validated) or company name (as fallback)
        contact_map = {}  # key -> {"email": str|None, "company": str, "location": str, "sources": set}

        def _add_row(row, source):
            raw    = (row["sent_to"] or "").strip()
            email  = raw.lower() if _EMAIL_RE.match(raw) else None
            if not email:
                return  # skip entries with no valid email address
            company = (row["customer"] or "").strip()
            location = (row["location"] or "").strip()
            key = email
            if key not in contact_map:
                contact_map[key] = {"email": email, "company": company, "location": location, "sources": set()}
            else:
                if email and not contact_map[key]["email"]:
                    contact_map[key]["email"] = email
                if company and not contact_map[key]["company"]:
                    contact_map[key]["company"] = company
                if location and not contact_map[key]["location"]:
                    contact_map[key]["location"] = location
            contact_map[key]["sources"].add(source)

        for row in rmax_rows:  _add_row(row, "RMAX")
        for row in hydro_rows: _add_row(row, "Hydrotech")

        inserted = updated = skipped = 0
        for key, info in contact_map.items():
            email        = info["email"]
            company      = info["company"]
            location     = info["location"]
            product_line = "Both" if len(info["sources"]) > 1 else list(info["sources"])[0]

            # Dedup by email only — same email = same person; different email = new contact
            existing = con.execute(
                "SELECT id, email, company, location, product_line, manually_edited FROM contacts WHERE email=?",
                (email,)
            ).fetchone() if email else None
            if existing:
                if existing["manually_edited"]:
                    skipped += 1
                    continue
                updates = {}
                if email    and not existing["email"]:    updates["email"]    = email
                if company  and not existing["company"]:  updates["company"]  = company
                if location and not existing["location"]: updates["location"] = location
                if product_line == "Both" and existing["product_line"] != "Both":
                    updates["product_line"] = "Both"
                if updates:
                    set_clause = ", ".join(f"{k}=?" for k in updates)
                    con.execute(f"UPDATE contacts SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                                list(updates.values()) + [existing["id"]])
                    updated += 1
                else:
                    skipped += 1
            else:
                con.execute(
                    "INSERT INTO contacts (email, company, location, product_line) VALUES (?,?,?,?)",
                    (email, company, location, product_line)
                )
                inserted += 1

        return {"inserted": inserted, "updated": updated, "skipped": skipped, "total": inserted + updated + skipped}

@app.get("/api/contacts/fetch-signature/{email:path}")
def fetch_contact_signature(email: str):
    """Search inbox for an email FROM this address and parse their signature for contact info."""
    if not GRAPH_CLIENT_SECRET:
        return {"found": False, "reason": "Graph API not configured"}
    try:
        import requests as _req
    except ImportError:
        return {"found": False, "reason": "requests package not installed"}
    try:
        token = _get_graph_token()
    except Exception as e:
        return {"found": False, "reason": str(e)}

    url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/messages"
           f"?$filter=from/emailAddress/address eq '{email}'"
           f"&$select=body,from,subject&$top=1"
           f"&$orderby=receivedDateTime desc")
    try:
        resp = _req.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=20)
    except Exception as e:
        return {"found": False, "reason": f"Network error: {e}"}
    if resp.status_code == 401:
        return {"found": False, "reason": "Unauthorized — verify Graph API Mail.Read permission"}
    if resp.status_code != 200:
        return {"found": False, "reason": f"Graph API returned {resp.status_code}"}

    msgs = resp.json().get("value", [])
    if not msgs:
        return {"found": False, "reason": "No emails found from this address in your inbox"}

    msg = msgs[0]
    body_html = msg.get("body", {}).get("content", "")
    content_type = msg.get("body", {}).get("contentType", "text")
    sender_name  = msg.get("from", {}).get("emailAddress", {}).get("name", "")

    body_text = _strip_html(body_html) if content_type == "html" else body_html
    body_text = _re.sub(r'\n{3,}', '\n\n', body_text).strip()

    sig_text = _extract_sig_text(body_text)
    phone    = _extract_phone(sig_text or body_text)

    return {
        "found": True,
        "name":           sender_name,
        "phone":          phone,
        "signature_text": sig_text,
    }

@app.post("/api/contacts/scan-outlook-folders")
def scan_outlook_folders(folders: Optional[str] = Query(None)):
    """
    Scan one or more Outlook mail folders for email signatures and bulk-populate contacts.
    folders = comma-separated folder names, defaults to 'RMAX Quotes,Hydrotech Quotes'
    """
    if not GRAPH_CLIENT_SECRET:
        return {"error": "Graph API not configured — add GRAPH_CLIENT_SECRET to Railway env vars"}
    try:
        import requests as _req
    except ImportError:
        return {"error": "requests package not installed"}
    try:
        token = _get_graph_token()
    except Exception as e:
        return {"error": str(e)}

    folder_names = [f.strip() for f in (folders or "RMAX Quotes,Hydrotech Quotes").split(",") if f.strip()]
    headers = {"Authorization": f"Bearer {token}"}

    def _find_folder_id(name: str) -> str | None:
        """Look for a mail folder by display name — top-level and inbox children."""
        for url in [
            f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders?$filter=displayName eq '{name}'&$select=id",
            f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}/mailFolders/Inbox/childFolders?$filter=displayName eq '{name}'&$select=id",
        ]:
            try:
                r = _req.get(url, headers=headers, timeout=20)
                if r.status_code == 200:
                    vals = r.json().get("value", [])
                    if vals:
                        return vals[0]["id"]
            except Exception:
                pass
        return None

    def _paginate_messages(url_start):
        """Fetch up to 1000 messages from a paginated Graph URL."""
        msgs = []
        next_url = url_start
        while next_url and len(msgs) < 1000:
            try:
                r = _req.get(next_url, headers=headers, timeout=30)
                if r.status_code != 200:
                    break
                data = r.json()
                msgs.extend(data.get("value", []))
                next_url = data.get("@odata.nextLink")
            except Exception:
                break
        return msgs

    results = []
    total_created = total_updated = total_skipped = 0

    with get_db() as con:
        for folder_name in folder_names:
            pl = "Hydrotech" if "hydrotech" in folder_name.lower() else "RMAX"

            # Collect messages from named folder (if it exists) + Sent Items filtered by subject
            msgs = []
            folder_id = _find_folder_id(folder_name)
            if folder_id:
                msgs = _paginate_messages(
                    f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                    f"/mailFolders/{folder_id}/messages"
                    f"?$select=from,body,subject,toRecipients&$top=100"
                )

            # Also scan Sent Items for emails with this folder name in the subject
            # (catches quotes that never got moved to the folder)
            subject_kw = folder_name.replace("'", "''")  # escape single quotes for OData
            sent_msgs = _paginate_messages(
                f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                f"/mailFolders/SentItems/messages"
                f"?$filter=contains(subject,'{subject_kw}')"
                f"&$select=from,body,subject,toRecipients&$top=100"
            )
            # Merge, deduplicating by message id
            seen_ids = {m.get("id") for m in msgs}
            for m in sent_msgs:
                if m.get("id") not in seen_ids:
                    msgs.append(m)
                    seen_ids.add(m.get("id"))

            if not msgs:
                results.append({"folder": folder_name, "error": "No messages found in folder or Sent Items"})
                continue

            created = updated = skipped = 0
            seen = set()

            for msg in msgs:
                # Quote was sent TO the customer — read toRecipients for their address
                recipients = msg.get("toRecipients", [])
                if not recipients:
                    continue
                contact_addr = recipients[0].get("emailAddress", {})
                email = (contact_addr.get("address") or "").strip().lower()
                name  = (contact_addr.get("name")    or "").strip()
                if not email or email in seen:
                    continue
                seen.add(email)

                body_html    = msg.get("body", {}).get("content", "")
                content_type = msg.get("body", {}).get("contentType", "text")
                body_text    = _strip_html(body_html) if content_type == "html" else body_html
                body_text    = _re.sub(r'\n{3,}', '\n\n', body_text).strip()
                sig_text     = _extract_sig_text(body_text)
                phone        = _extract_phone(sig_text or body_text)

                # Pull company/location from quotes tables
                qrow = (con.execute("SELECT customer, location FROM quotes WHERE LOWER(sent_to)=? AND customer!='' LIMIT 1", (email,)).fetchone()
                        or con.execute("SELECT customer, location FROM hydrotech_quotes WHERE LOWER(sent_to)=? AND customer!='' LIMIT 1", (email,)).fetchone())
                company  = (qrow["customer"] if qrow else "") or ""
                location = (qrow["location"] if qrow else "") or ""

                # Dedup by email only — same email = same person; different email = new contact
                existing = con.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
                if existing:
                    # Never touch a contact the user has manually edited
                    if existing["manually_edited"]:
                        skipped += 1
                        continue
                    updates = {}
                    if name     and not existing["name"]:     updates["name"]     = name
                    if phone    and not existing["phone"]:    updates["phone"]    = phone
                    if company  and not existing["company"]:  updates["company"]  = company
                    if location and not existing["location"]: updates["location"] = location
                    # Fill in email if matched by company name and email was blank
                    if email and not existing["email"]:       updates["email"]    = email
                    # Upgrade product_line to Both if contact now appears in a second line
                    if existing["product_line"] and existing["product_line"] != pl and existing["product_line"] != "Both":
                        updates["product_line"] = "Both"
                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        con.execute(
                            f"UPDATE contacts SET {set_clause}, updated_at=datetime('now') WHERE id=?",
                            list(updates.values()) + [existing["id"]]
                        )
                        updated += 1
                    else:
                        skipped += 1
                else:
                    con.execute(
                        "INSERT INTO contacts (name,email,phone,company,location,product_line) VALUES (?,?,?,?,?,?)",
                        (name, email, phone, company, location, pl)
                    )
                    created += 1

            total_created += created
            total_updated += updated
            total_skipped += skipped
            results.append({
                "folder":           folder_name,
                "messages_scanned": len(msgs),
                "unique_senders":   len(seen),
                "created":          created,
                "updated":          updated,
                "skipped":          skipped,
            })

    return {
        "folders":       results,
        "total_created": total_created,
        "total_updated": total_updated,
        "total_skipped": total_skipped,
    }

# ── Company Accounts ──────────────────────────────────────────────────────────
class CompanyIn(BaseModel):
    name:                      str
    address:                   Optional[str] = None
    phone:                     Optional[str] = None
    website:                   Optional[str] = None
    region:                    Optional[str] = None
    notes:                     Optional[str] = None
    strong_market_partner:     Optional[int] = 0
    large_account_opportunity: Optional[int] = 0

@app.get("/api/companies")
def list_companies(search: Optional[str] = Query(None), region: Optional[str] = Query(None)):
    with get_db() as con:
        sql = """
            SELECT c.*,
                   COUNT(DISTINCT cc.contact_id) as contact_count,
                   COALESCE((SELECT SUM(amount) FROM quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0) AS rmax_quoted,
                   COALESCE((SELECT SUM(amount) FROM hydrotech_quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0) AS hydrotech_quoted,
                   COALESCE((SELECT SUM(amount) FROM glassworks_quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0) AS glassworks_quoted,
                   COALESCE((SELECT SUM(amount) FROM quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0)
                 + COALESCE((SELECT SUM(amount) FROM hydrotech_quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0)
                 + COALESCE((SELECT SUM(amount) FROM glassworks_quotes
                             WHERE company_id=c.id AND (deleted IS NULL OR deleted=0)),0)
                   AS total_quoted
            FROM companies c
            LEFT JOIN company_contacts cc ON cc.company_id = c.id
            WHERE 1=1
        """
        params = []
        if search:
            sql += " AND (c.name LIKE ? OR c.phone LIKE ? OR c.address LIKE ?)"
            s = f"%{search}%"
            params += [s, s, s]
        if region and region != 'All':
            sql += " AND c.region = ?"
            params.append(region)
        sql += " GROUP BY c.id ORDER BY c.name COLLATE NOCASE"
        return [dict(r) for r in con.execute(sql, params).fetchall()]

@app.post("/api/companies", status_code=201)
def create_company(c: CompanyIn):
    with get_db() as con:
        cur = con.execute(
            "INSERT INTO companies (name,address,phone,website,region,notes,strong_market_partner,large_account_opportunity) VALUES (?,?,?,?,?,?,?,?)",
            (c.name, c.address, c.phone, c.website, c.region, c.notes, c.strong_market_partner or 0, c.large_account_opportunity or 0)
        )
        return {"id": cur.lastrowid}

@app.put("/api/companies/{company_id}")
def update_company(company_id: int, c: CompanyIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM companies WHERE id=?", (company_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Company not found")
        con.execute("""
            UPDATE companies SET name=?,address=?,phone=?,website=?,region=?,notes=?,
                strong_market_partner=?,large_account_opportunity=?,updated_at=datetime('now')
            WHERE id=?
        """, (c.name, c.address, c.phone, c.website, c.region, c.notes,
              c.strong_market_partner or 0, c.large_account_opportunity or 0, company_id))
        return {"ok": True}

@app.delete("/api/companies/{company_id}")
def delete_company(company_id: int):
    with get_db() as con:
        con.execute("DELETE FROM company_contacts WHERE company_id=?", (company_id,))
        con.execute("DELETE FROM companies WHERE id=?", (company_id,))
        return {"ok": True}

@app.post("/api/companies/import-from-quotes")
def import_companies_from_quotes():
    """Scan all quote tables and create Company Account entries from unique customer names,
    carrying over the most-used region from that customer's quotes."""
    from collections import defaultdict, Counter
    with get_db() as con:
        rows = []
        for table in ("quotes", "hydrotech_quotes", "glassworks_quotes"):
            try:
                rs = con.execute(
                    f"SELECT customer, region FROM {table} "
                    f"WHERE (deleted IS NULL OR deleted=0) "
                    f"AND customer IS NOT NULL AND TRIM(customer) != ''"
                ).fetchall()
                rows.extend(rs)
            except Exception:
                pass

        # Group all regions seen per customer name (case-insensitive key)
        customer_map = defaultdict(lambda: {"canonical": "", "regions": []})
        for row in rows:
            name = (row["customer"] or "").strip()
            region = (row["region"] or "").strip()
            if not name:
                continue
            key = name.lower()
            if not customer_map[key]["canonical"]:
                customer_map[key]["canonical"] = name
            if region:
                customer_map[key]["regions"].append(region)

        inserted = updated = skipped = 0
        for key, info in customer_map.items():
            name = info["canonical"]
            non_empty = info["regions"]
            best_region = Counter(non_empty).most_common(1)[0][0] if non_empty else None

            existing = con.execute(
                "SELECT id, region FROM companies WHERE LOWER(name) = ?", (key,)
            ).fetchone()

            if existing:
                if best_region and not existing["region"]:
                    con.execute(
                        "UPDATE companies SET region=?, updated_at=datetime('now') WHERE id=?",
                        (best_region, existing["id"])
                    )
                    updated += 1
                else:
                    skipped += 1
            else:
                con.execute(
                    "INSERT INTO companies (name, region) VALUES (?, ?)",
                    (name, best_region)
                )
                inserted += 1

        return {"inserted": inserted, "updated": updated, "skipped": skipped,
                "total": inserted + updated + skipped}

@app.get("/api/companies/{company_id}/contacts")
def list_company_contacts(company_id: int):
    with get_db() as con:
        rows = con.execute("""
            SELECT ct.*, cc.role, cc.id as link_id
            FROM contacts ct
            JOIN company_contacts cc ON cc.contact_id = ct.id
            WHERE cc.company_id = ?
            ORDER BY COALESCE(NULLIF(ct.name,''),'zzz')
        """, (company_id,)).fetchall()
        return [dict(r) for r in rows]

class CompanyContactIn(BaseModel):
    contact_id: int
    role:       Optional[str] = None

@app.post("/api/companies/{company_id}/contacts", status_code=201)
def add_company_contact(company_id: int, body: CompanyContactIn):
    with get_db() as con:
        existing = con.execute("SELECT id FROM companies WHERE id=?", (company_id,)).fetchone()
        if not existing:
            raise HTTPException(404, "Company not found")
        try:
            con.execute(
                "INSERT INTO company_contacts (company_id,contact_id,role) VALUES (?,?,?)",
                (company_id, body.contact_id, body.role)
            )
        except Exception:
            raise HTTPException(409, "Contact already linked to this company")
        return {"ok": True}

@app.delete("/api/companies/{company_id}/contacts/{contact_id}")
def remove_company_contact(company_id: int, contact_id: int):
    with get_db() as con:
        con.execute(
            "DELETE FROM company_contacts WHERE company_id=? AND contact_id=?",
            (company_id, contact_id)
        )
        return {"ok": True}

@app.put("/api/companies/{company_id}/contacts/{contact_id}")
def update_company_contact_role(company_id: int, contact_id: int, body: CompanyContactIn):
    with get_db() as con:
        con.execute(
            "UPDATE company_contacts SET role=? WHERE company_id=? AND contact_id=?",
            (body.role, company_id, contact_id)
        )
        return {"ok": True}

@app.get("/api/companies/{company_id}/quote-stats")
def company_quote_stats(company_id: int, period: str = Query("all")):
    """Sum amount quoted and amount won across all 3 quote tables for a company.
    period: 'all' | 'ytd' | 'YYYY-MM'
    """
    import datetime
    tables = [
        ("quotes",           "deleted"),
        ("hydrotech_quotes", "deleted"),
        ("glassworks_quotes","deleted"),
    ]
    # Build date clause
    now = datetime.date.today()
    date_clause = ""
    date_params: list = []
    if period == "ytd":
        date_clause = "AND date_received >= ?"
        date_params = [f"{now.year}-01-01"]
    elif len(period) == 7 and period[4] == "-":   # YYYY-MM
        date_clause = "AND date_received LIKE ?"
        date_params = [f"{period}%"]

    total_quoted = 0.0
    total_won    = 0.0
    quote_count  = 0
    won_count    = 0
    with get_db() as con:
        for table, del_col in tables:
            cols = {r[1] for r in con.execute(f"PRAGMA table_info({table})")}
            if "company_id" not in cols or "amount" not in cols:
                continue
            where_del = f"AND ({del_col} IS NULL OR {del_col}=0)" if del_col in cols else ""
            has_date  = "date_received" in cols
            d_clause  = date_clause if has_date else ""
            params    = [company_id] + (date_params if has_date else [])
            rows = con.execute(
                f"SELECT amount, status FROM {table} WHERE company_id=? {where_del} {d_clause}",
                params
            ).fetchall()
            for r in rows:
                amt = r["amount"] or 0.0
                total_quoted += amt
                quote_count  += 1
                if r["status"] == "Won":
                    total_won += amt
                    won_count += 1
    return {
        "total_quoted": total_quoted,
        "total_won":    total_won,
        "quote_count":  quote_count,
        "won_count":    won_count,
        "period":       period,
    }

# ── Serve frontend ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    return FileResponse(
        os.path.join(STATIC, "index.html"),
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
    )
