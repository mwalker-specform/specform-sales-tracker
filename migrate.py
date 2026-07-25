#!/usr/bin/env python3
"""
migrate.py — Import quotes from RMAX_All_Quotes_CLEAN.xlsx into quotes.db
Run once: python migrate.py
Run again safely: existing rows are skipped (dedup by sent_to + subject + date_received)
"""
import os, sqlite3, openpyxl
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
XLSX = os.path.join(BASE_DIR, '..', 'RMAX_All_Quotes_CLEAN.xlsx')
DB   = os.path.join(BASE_DIR, 'quotes.db')

HEADERS = [
    'status','date_received','date_quoted','sent_to','subject','job_name',
    'customer','location','product','price','quantities','amount',
    'close_date','est_freight','lead_time','notes'
]

def parse_date(v):
    if not v: return None
    if isinstance(v, datetime): return v.strftime('%Y-%m-%d')
    for fmt in ('%Y-%m-%d','%m/%d/%Y','%m-%d-%Y','%b %d, %Y','%B %d, %Y'):
        try: return datetime.strptime(str(v).strip(), fmt).strftime('%Y-%m-%d')
        except: pass
    return str(v).strip()

def main():
    if not os.path.exists(XLSX):
        print(f'ERROR: {XLSX} not found. Run from inside rmax_app/ folder.')
        return

    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb.active

    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row

    # Create table if not exists (same schema as backend.py)
    con.execute("""
    CREATE TABLE IF NOT EXISTS quotes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        status        TEXT,
        date_received TEXT,
        date_quoted   TEXT,
        sent_to       TEXT,
        subject       TEXT,
        job_name      TEXT,
        customer      TEXT,
        location      TEXT,
        product       TEXT,
        price         TEXT,
        quantities    TEXT,
        amount        REAL,
        close_date    TEXT,
        est_freight   TEXT,
        lead_time     TEXT,
        notes         TEXT,
        created_at    TEXT DEFAULT (datetime('now')),
        updated_at    TEXT DEFAULT (datetime('now'))
    )""")
    con.commit()

    inserted = skipped = 0
    for row in ws.iter_rows(min_row=5, values_only=True):
        if all(v is None for v in row): continue
        if row[1] is None and row[3] is None: continue   # skip group headers

        r = (list(row)[:16] + [None]*16)[:16]
        data = {k: r[i] for i,k in enumerate(HEADERS)}

        # Normalise dates
        data['date_received'] = parse_date(data['date_received'])
        data['date_quoted']   = parse_date(data['date_quoted'])
        data['close_date']    = parse_date(data['close_date'])

        # Amount → float
        try:    data['amount'] = float(data['amount']) if data['amount'] is not None else None
        except: data['amount'] = None

        # Dedup check
        exists = con.execute("""
            SELECT id FROM quotes
            WHERE sent_to=? AND subject=? AND date_received=?
        """, (data['sent_to'], data['subject'], data['date_received'])).fetchone()

        if exists:
            skipped += 1
            continue

        con.execute("""
        INSERT INTO quotes (status,date_received,date_quoted,sent_to,subject,job_name,
            customer,location,product,price,quantities,amount,close_date,est_freight,lead_time,notes)
        VALUES (:status,:date_received,:date_quoted,:sent_to,:subject,:job_name,
            :customer,:location,:product,:price,:quantities,:amount,:close_date,
            :est_freight,:lead_time,:notes)
        """, data)
        inserted += 1

    con.commit()
    con.close()
    total = con.execute if False else sqlite3.connect(DB).execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    print(f'Done. Inserted: {inserted}  Skipped (already exist): {skipped}  Total in DB: {total}')

if __name__ == '__main__':
    main()
