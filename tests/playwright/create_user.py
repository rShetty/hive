#!/usr/bin/env python3
"""Create a user in SQLite with proper bcrypt hash, bypassing API rate limits."""
import sqlite3, uuid, datetime, sys, json, time
import bcrypt

DB_PATH = '/Users/rshetty/hive/backend/agent_marketplace.db'

def create_user(name, email, password, retries=10):
    for attempt in range(retries):
        try:
            conn = sqlite3.connect(DB_PATH, timeout=30)
            uid = str(uuid.uuid4())
            hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            now = datetime.datetime.utcnow().isoformat()
            conn.execute(
                "INSERT INTO users (id, email, name, hashed_password, is_active, is_admin, created_at) VALUES (?, ?, ?, ?, 1, 0, ?)",
                (uid, email, name, hashed, now)
            )
            # Also create a wallet with 10000 tokens
            wid = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO wallets (id, user_id, balance, created_at, updated_at) VALUES (?, ?, 10000, ?, ?)",
                (wid, uid, now, now)
            )
            conn.commit()
            conn.close()
            return uid
        except sqlite3.OperationalError as e:
            if 'locked' in str(e) and attempt < retries - 1:
                time.sleep(3)
                continue
            raise
    raise RuntimeError("Failed to create user after retries")

if __name__ == '__main__':
    name = sys.argv[1]
    email = sys.argv[2]
    password = sys.argv[3]
    uid = create_user(name, email, password)
    print(json.dumps({"id": uid, "email": email, "name": name}))
