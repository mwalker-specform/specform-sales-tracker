#!/usr/bin/env python3
"""
insert_quotes.py — Insert parsed Outlook email records into quotes.db
Usage: python insert_quotes.py sync_pending.json
Called automatically by the Outlook sync scheduled task.
"""
import sys, json, sqlite3, os, glob

def find_db():
    """Find quotes.db regardless of the current session mount path."""
    # Try sandbox mount paths first
    patterns = [
        '/sessions/*/mnt/RMAX Weekly Quotes/rmax_app/quotes.db',
        '/sessions/*/mnt/RMAX*/rmax_app/quotes.db',
    ]
    for pattern in patterns:
        matches = glob.glob(pattern)
        if matches:
            return matches[0]
    # Fallback: relative to this script
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'quotes.db')

def main():
    if len(sys.argv) < 2:
        print("Usage: python insert_quotes.py sync_pending.json")
        sys.exit(1)

    data_file = sys.argv[1]
    if not os.path.exists(data_file):
        print(f"ERROR: {data_file} not found")
        sys.exit(1)

    with open(data_file) as f:
        records = json.load(f)

    db_path = find_db()
    if not os.path.exists(db_path):
        print(f"ERROR: Database not found at {db_path}")
        sys.exit(1)

    con = sqlite3.connect(db_path)

    inserted = skipped = 0
    for r in records:
        # Dedup: same as migrate.py — skip if sent_to + subject + date_received already exists
        exists = con.execute(
            "SELECT id FROM quotes WHERE sent_to=? AND subject=? AND date_received=?",
            (r.get('sent_to'), r.get('subject'), r.get('date_received'))
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
            r.get('status'),
            r.get('date_received'),
            r.get('date_quoted'),
            r.get('sent_to'),
            r.get('subject'),
            r.get('job_name'),
            r.get('customer'),
            r.get('location'),
            r.get('product'),
            r.get('price'),
            r.get('quantities'),
            r.get('amount'),
            r.get('close_date'),
            r.get('est_freight'),
            r.get('lead_time'),
            r.get('notes'),
        ))
        inserted += 1

    con.commit()
    con.close()

    total = sqlite3.connect(db_path).execute("SELECT COUNT(*) FROM quotes").fetchone()[0]
    print(f"Done. Inserted: {inserted}  Skipped (duplicates): {skipped}  Total in DB: {total}")

if __name__ == '__main__':
    main()
