#!/usr/bin/env python3
"""Fund a user's wallet by email. Used by Playwright helpers."""
import sqlite3
import sys
import os

def grant(email, amount=10000):
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "..", "backend", "agent_marketplace.db"),
        os.path.join(os.path.dirname(__file__), "..", "..", "data", "agent_marketplace.db"),
        os.path.join(os.getcwd(), "backend", "agent_marketplace.db"),
        os.path.join(os.getcwd(), "agent_marketplace.db"),
        "agent_marketplace.db",
    ]
    db_path = next((p for p in candidates if os.path.exists(p)), None)
    if not db_path:
        print("DB not found", file=sys.stderr)
        return 1
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    row = cur.execute("SELECT id FROM users WHERE email=?", (email,)).fetchone()
    if not row:
        conn.close()
        print(f"User {email} not found", file=sys.stderr)
        return 1
    uid = row[0]
    cur.execute("UPDATE wallets SET balance = balance + ? WHERE user_id=?", (amount, uid))
    if cur.rowcount == 0:
        import uuid
        wid = str(uuid.uuid4())
        cur.execute("INSERT INTO wallets (id, user_id, balance) VALUES (?, ?, ?)", (wid, uid, amount))
    conn.commit()
    conn.close()
    print(f"Granted {amount} tokens to {email}")
    return 0

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: grant_tokens.py <email> [amount]", file=sys.stderr)
        sys.exit(1)
    amt = float(sys.argv[2]) if len(sys.argv) > 2 else 10000
    sys.exit(grant(sys.argv[1], amt))
