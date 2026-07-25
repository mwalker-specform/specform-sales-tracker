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

from fastapi import FastAPI, HTTPException, Query
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
                                 ('completed',         'INTEGER DEFAULT 0')]:
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
    add_to_salesforce:  Optional[int] = 0
    completed:          Optional[int] = 0

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
        sql = "SELECT * FROM quotes WHERE 1=1"
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
        row = con.execute("SELECT * FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Quote not found")
        return dict(row)

@app.post("/api/quotes", status_code=201)
def create_quote(q: QuoteIn):
    with get_db() as con:
        cur = con.execute("""
        INSERT INTO quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes,
            add_to_salesforce,completed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.add_to_salesforce,q.completed))
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
            add_to_salesforce=?,completed=?,updated_at=datetime('now')
        WHERE id=?
        """, (q.status,normalize_date(q.date_received),normalize_date(q.date_quoted),
              q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              normalize_date(q.close_date),q.est_freight,q.lead_time,q.notes,
              q.add_to_salesforce,q.completed,quote_id))
        return {"ok": True}

@app.delete("/api/quotes/{quote_id}")
def delete_quote(quote_id: int):
    with get_db() as con:
        con.execute("DELETE FROM quotes WHERE id=?", (quote_id,))
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
            FROM quotes ORDER BY date_received DESC
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

# ── Sync endpoint (imports sync_pending.json written by the scheduled task) ───
@app.post("/api/sync")
def sync_from_outlook():
    """
    Read sync_pending.json (written by the Outlook scheduled task), insert any
    new quotes, then clear the file.  Returns counts so the UI can show feedback.
    """
    import json as _json
    pending_path = os.path.join(BASE_DIR, "sync_pending.json")
    if not os.path.exists(pending_path):
        return {"inserted": 0, "skipped": 0, "message": "No pending quotes"}

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

    # Clear the file after successful import
    with open(pending_path, "w", encoding="utf-8") as f:
        _json.dump([], f)

    return {"inserted": inserted, "skipped": skipped,
            "message": f"Imported {inserted} new quote(s), {skipped} duplicate(s) skipped"}

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
            FROM quotes
        """).fetchone()

        # By location
        by_loc = con.execute("""
            SELECT location,
                COALESCE(SUM(amount),0) as total,
                COALESCE(SUM(CASE WHEN status='Won'    THEN amount ELSE 0 END),0) as won,
                COALESCE(SUM(CASE WHEN status='Verbal' THEN amount ELSE 0 END),0) as verbal
            FROM quotes
            WHERE location IS NOT NULL
            GROUP BY location
            ORDER BY total DESC
        """).fetchall()

        # By month — parse dates in Python to handle M/D/YYYY, "Jul 13, 2026", etc.
        cutoff = datetime.now().replace(day=1) - timedelta(days=335)  # ~11 months ago
        raw_quotes = con.execute(
            "SELECT date_received, status, amount FROM quotes WHERE date_received IS NOT NULL AND date_received != ''"
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

        # Status breakdown
        by_status = con.execute("""
            SELECT status, COUNT(*) as count, COALESCE(SUM(amount),0) as amount
            FROM quotes
            WHERE status IS NOT NULL
            GROUP BY status
            ORDER BY amount DESC
        """).fetchall()

        # Distinct filter options
        locations = [r[0] for r in con.execute(
            "SELECT DISTINCT location FROM quotes WHERE location IS NOT NULL ORDER BY location"
        ).fetchall()]

        return {
            "totals": dict(totals),
            "by_location": [dict(r) for r in by_loc],
            "by_month": [dict(r) for r in by_month],
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
                                 ('completed',         'INTEGER DEFAULT 0')]:
            try:
                con.execute(f"ALTER TABLE hydrotech_quotes ADD COLUMN {col} {definition}")
            except Exception:
                pass

@app.get("/api/hydrotech-quotes")
def list_hydrotech_quotes(
    status:   Optional[str] = Query(None),
    location: Optional[str] = Query(None),
    search:   Optional[str] = Query(None),
):
    with get_db() as con:
        sql = "SELECT * FROM hydrotech_quotes WHERE 1=1"
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

@app.get("/api/hydrotech-quotes/{quote_id}")
def get_hydrotech_quote(quote_id: int):
    with get_db() as con:
        row = con.execute("SELECT * FROM hydrotech_quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "Hydrotech quote not found")
        return dict(row)

@app.post("/api/hydrotech-quotes", status_code=201)
def create_hydrotech_quote(q: QuoteIn):
    with get_db() as con:
        cur = con.execute("""
        INSERT INTO hydrotech_quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes,
            add_to_salesforce,completed)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (q.status,q.date_received,q.date_quoted,q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              q.close_date,q.est_freight,q.lead_time,q.notes,
              q.add_to_salesforce,q.completed))
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
            add_to_salesforce=?,completed=?,updated_at=datetime('now')
        WHERE id=?
        """, (q.status,q.date_received,q.date_quoted,q.sent_to,q.subject,q.job_name,
              q.customer,q.location,q.product,q.price,q.quantities,q.amount,
              q.close_date,q.est_freight,q.lead_time,q.notes,
              q.add_to_salesforce,q.completed,quote_id))
        return {"ok": True}

@app.delete("/api/hydrotech-quotes/{quote_id}")
def delete_hydrotech_quote(quote_id: int):
    with get_db() as con:
        con.execute("DELETE FROM hydrotech_quotes WHERE id=?", (quote_id,))
        return {"ok": True}

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
    """Read hydrotech_sync_pending.json and insert new Hydrotech quotes."""
    import json as _json
    pending_path = os.path.join(BASE_DIR, "hydrotech_sync_pending.json")
    if not os.path.exists(pending_path):
        return {"inserted": 0, "skipped": 0, "message": "No pending Hydrotech quotes"}

    with open(pending_path, "r", encoding="utf-8") as f:
        records = _json.load(f)

    inserted = skipped = 0
    with get_db() as con:
        for r in records:
            exists = con.execute(
                "SELECT id FROM hydrotech_quotes WHERE sent_to=? AND subject=? AND date_received=?",
                (r.get("sent_to"), r.get("subject"), r.get("date_received"))
            ).fetchone()
            if exists:
                skipped += 1
                continue
            con.execute("""
            INSERT INTO hydrotech_quotes
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
            "message": f"Imported {inserted} new Hydrotech quote(s), {skipped} duplicate(s) skipped"}

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
        return {
            "totals": dict(totals),
            "by_location": [dict(r) for r in by_loc],
            "by_month": [dict(r) for r in by_month],
            "by_status": [dict(r) for r in by_status],
            "locations": locations,
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

@app.post("/api/contacts/import-from-quotes")
def import_contacts_from_quotes():
    """Scan both quote tables and create contact entries for unique sent_to emails."""
    from collections import defaultdict
    with get_db() as con:
        rmax_rows  = con.execute("SELECT DISTINCT sent_to, customer, location FROM quotes WHERE sent_to IS NOT NULL AND sent_to != ''").fetchall()
        hydro_rows = con.execute("SELECT DISTINCT sent_to, customer, location FROM hydrotech_quotes WHERE sent_to IS NOT NULL AND sent_to != ''").fetchall()

        contact_map = defaultdict(lambda: {"company": "", "location": "", "sources": set()})
        for row in rmax_rows:
            e = (row["sent_to"] or "").strip().lower()
            if e:
                contact_map[e]["company"]  = contact_map[e]["company"]  or (row["customer"] or "")
                contact_map[e]["location"] = contact_map[e]["location"] or (row["location"] or "")
                contact_map[e]["sources"].add("RMAX")
        for row in hydro_rows:
            e = (row["sent_to"] or "").strip().lower()
            if e:
                contact_map[e]["company"]  = contact_map[e]["company"]  or (row["customer"] or "")
                contact_map[e]["location"] = contact_map[e]["location"] or (row["location"] or "")
                contact_map[e]["sources"].add("Hydrotech")

        inserted = skipped = 0
        for email, info in contact_map.items():
            product_line = "Both" if len(info["sources"]) > 1 else list(info["sources"])[0]
            existing = con.execute("SELECT id, product_line, manually_edited FROM contacts WHERE email=?", (email,)).fetchone()
            if existing:
                # Never touch a contact the user has manually edited
                if existing["manually_edited"]:
                    skipped += 1
                    continue
                # Upgrade to Both if now seen in both product lines
                if product_line == "Both" and existing["product_line"] != "Both":
                    con.execute("UPDATE contacts SET product_line='Both',updated_at=datetime('now') WHERE email=?", (email,))
                skipped += 1
            else:
                con.execute("""
                INSERT INTO contacts (email,company,location,product_line)
                VALUES (?,?,?,?)
                """, (email, info["company"], info["location"], product_line))
                inserted += 1

        return {"inserted": inserted, "skipped": skipped, "total": inserted + skipped}

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

    results = []
    total_created = total_updated = total_skipped = 0

    with get_db() as con:
        for folder_name in folder_names:
            folder_id = _find_folder_id(folder_name)
            if not folder_id:
                results.append({"folder": folder_name, "error": "Folder not found in mailbox"})
                continue

            pl = "Hydrotech" if "hydrotech" in folder_name.lower() else "RMAX"

            # Paginate through all messages (up to 1 000)
            msgs = []
            next_url = (f"https://graph.microsoft.com/v1.0/users/{GRAPH_USER}"
                        f"/mailFolders/{folder_id}/messages"
                        f"?$select=from,body,subject&$top=100")
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

            created = updated = skipped = 0
            seen = set()

            for msg in msgs:
                sender = msg.get("from", {}).get("emailAddress", {})
                email  = (sender.get("address") or "").strip().lower()
                name   = (sender.get("name")    or "").strip()
                if not email or email in seen:
                    continue
                seen.add(email)

                body_html    = msg.get("body", {}).get("content", "")
                content_type = msg.get("body", {}).get("contentType", "text")
                body_text    = _strip_html(body_html) if content_type == "html" else body_html
                body_text    = _re.sub(r'\n{3,}', '\n\n', body_text).strip()
                sig_text     = _extract_sig_text(body_text)
                phone        = _extract_phone(sig_text or body_text)

                existing = con.execute("SELECT * FROM contacts WHERE email=?", (email,)).fetchone()
                if existing:
                    # Never touch a contact the user has manually edited
                    if existing["manually_edited"]:
                        skipped += 1
                        continue
                    updates = {}
                    if name  and not existing["name"]:  updates["name"]  = name
                    if phone and not existing["phone"]: updates["phone"] = phone
                    # Upgrade product_line to Both if contact now appears in a second line
                    if existing["product_line"] and existing["product_line"] != pl and existing["product_line"] != "Both":
                        updates["product_line"] = "Both"
                    if updates:
                        set_clause = ", ".join(f"{k}=?" for k in updates)
                        con.execute(
                            f"UPDATE contacts SET {set_clause}, updated_at=datetime('now') WHERE email=?",
                            list(updates.values()) + [email]
                        )
                        updated += 1
                    else:
                        skipped += 1
                else:
                    con.execute(
                        "INSERT INTO contacts (name,email,phone,product_line) VALUES (?,?,?,?)",
                        (name, email, phone, pl)
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

# ── Serve frontend ────────────────────────────────────────────────────────────
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/{full_path:path}")
def serve_spa(full_path: str):
    return FileResponse(os.path.join(STATIC, "index.html"))
