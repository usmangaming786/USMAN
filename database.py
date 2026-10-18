import sqlite3
import json
from datetime import datetime
from typing import List, Dict, Optional, Any


class Database:
    def __init__(self, db_path: str = "sms_bot.db"):
        self.db_path = db_path
        self._init_db()

    def _conn(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self):
        conn = self._conn()
        c = conn.cursor()

        c.execute('''CREATE TABLE IF NOT EXISTS templates (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            message TEXT NOT NULL,
            variables TEXT DEFAULT '[]',
            char_count INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS data_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            filename TEXT NOT NULL,
            columns TEXT DEFAULT '[]',
            total_rows INTEGER DEFAULT 0,
            valid_rows INTEGER DEFAULT 0,
            invalid_rows INTEGER DEFAULT 0,
            duplicate_rows INTEGER DEFAULT 0,
            data_json TEXT DEFAULT '[]',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS firebase_configs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            url TEXT NOT NULL,
            is_active INTEGER DEFAULT 0,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS campaigns (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            campaign_id TEXT UNIQUE NOT NULL,
            name TEXT DEFAULT '',
            template_id INTEGER,
            data_file_id INTEGER,
            firebase_id INTEGER,
            status TEXT DEFAULT 'PREPARING',
            total INTEGER DEFAULT 0,
            completed INTEGER DEFAULT 0,
            failed INTEGER DEFAULT 0,
            devices_used TEXT DEFAULT '[]',
            sim_slot INTEGER DEFAULT 0,
            delay REAL DEFAULT 2.0,
            started_at TEXT,
            completed_at TEXT,
            duration TEXT DEFAULT '',
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS campaign_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            campaign_id TEXT NOT NULL,
            target_id TEXT NOT NULL,
            mobile TEXT NOT NULL,
            variables_json TEXT DEFAULT '{}',
            message TEXT DEFAULT '',
            status TEXT DEFAULT 'PENDING',
            device_id TEXT DEFAULT '',
            processed_at TEXT DEFAULT '',
            error TEXT DEFAULT ''
        )''')

        c.execute('''CREATE TABLE IF NOT EXISTS user_settings (
            user_id INTEGER PRIMARY KEY,
            default_delay REAL DEFAULT 2.0,
            default_sim INTEGER DEFAULT 0
        )''')

        conn.commit()
        conn.close()

    # ========== TEMPLATES ==========
    def create_template(self, user_id: int, name: str, message: str, variables: list) -> int:
        conn = self._conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO templates (user_id, name, message, variables, char_count) VALUES (?,?,?,?,?)",
            (user_id, name, message, json.dumps(variables), len(message))
        )
        tid = c.lastrowid
        conn.commit()
        conn.close()
        return tid

    def get_templates(self, user_id: int) -> List[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM templates WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_template(self, tid: int) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM templates WHERE id=?", (tid,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def update_template(self, tid: int, **kwargs):
        conn = self._conn()
        sets = []
        vals = []
        for k, v in kwargs.items():
            if k == 'variables':
                v = json.dumps(v)
            sets.append(f"{k}=?")
            vals.append(v)
        if 'message' in kwargs:
            sets.append("char_count=?")
            vals.append(len(kwargs['message']))
        vals.append(tid)
        conn.execute(f"UPDATE templates SET {','.join(sets)} WHERE id=?", vals)
        conn.commit()
        conn.close()

    def delete_template(self, tid: int):
        conn = self._conn()
        conn.execute("DELETE FROM templates WHERE id=?", (tid,))
        conn.commit()
        conn.close()

    # ========== DATA FILES ==========
    def save_data_file(self, user_id: int, filename: str, columns: list,
                       total: int, valid: int, invalid: int, duplicate: int,
                       data: list) -> int:
        conn = self._conn()
        c = conn.cursor()
        c.execute(
            "INSERT INTO data_files (user_id,filename,columns,total_rows,valid_rows,invalid_rows,duplicate_rows,data_json) VALUES (?,?,?,?,?,?,?,?)",
            (user_id, filename, json.dumps(columns), total, valid, invalid, duplicate, json.dumps(data))
        )
        fid = c.lastrowid
        conn.commit()
        conn.close()
        return fid

    def get_data_files(self, user_id: int) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT id, user_id, filename, columns, total_rows, valid_rows, invalid_rows, duplicate_rows, created_at FROM data_files WHERE user_id=? ORDER BY id DESC",
            (user_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_data_file(self, fid: int) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM data_files WHERE id=?", (fid,)).fetchone()
        conn.close()
        if row:
            d = dict(row)
            d['data'] = json.loads(d.get('data_json', '[]'))
            d['columns'] = json.loads(d.get('columns', '[]'))
            return d
        return None

    def delete_data_file(self, fid: int):
        conn = self._conn()
        conn.execute("DELETE FROM data_files WHERE id=?", (fid,))
        conn.commit()
        conn.close()

    # ========== FIREBASE ==========
    def add_firebase(self, user_id: int, name: str, url: str) -> int:
        conn = self._conn()
        c = conn.cursor()
        c.execute("INSERT INTO firebase_configs (user_id,name,url) VALUES (?,?,?)", (user_id, name, url))
        fid = c.lastrowid
        # If first firebase for user, set as active
        count = conn.execute("SELECT COUNT(*) as cnt FROM firebase_configs WHERE user_id=?", (user_id,)).fetchone()['cnt']
        if count == 1:
            conn.execute("UPDATE firebase_configs SET is_active=1 WHERE id=?", (fid,))
        conn.commit()
        conn.close()
        return fid

    def get_firebase_configs(self, user_id: int) -> List[dict]:
        conn = self._conn()
        rows = conn.execute("SELECT * FROM firebase_configs WHERE user_id=? ORDER BY id DESC", (user_id,)).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_active_firebase(self, user_id: int) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM firebase_configs WHERE user_id=? AND is_active=1", (user_id,)).fetchone()
        conn.close()
        return dict(row) if row else None

    def set_active_firebase(self, user_id: int, fid: int):
        conn = self._conn()
        conn.execute("UPDATE firebase_configs SET is_active=0 WHERE user_id=?", (user_id,))
        conn.execute("UPDATE firebase_configs SET is_active=1 WHERE id=? AND user_id=?", (fid, user_id))
        conn.commit()
        conn.close()

    def delete_firebase(self, fid: int):
        conn = self._conn()
        conn.execute("DELETE FROM firebase_configs WHERE id=?", (fid,))
        conn.commit()
        conn.close()

    def get_firebase_by_id(self, fid: int) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM firebase_configs WHERE id=?", (fid,)).fetchone()
        conn.close()
        return dict(row) if row else None

    # ========== CAMPAIGNS ==========
    def create_campaign(self, user_id: int, campaign_id: str, name: str,
                        template_id: int, data_file_id: int, firebase_id: int,
                        total: int, devices: list, sim_slot: int, delay: float) -> int:
        conn = self._conn()
        c = conn.cursor()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        c.execute(
            """INSERT INTO campaigns 
            (user_id,campaign_id,name,template_id,data_file_id,firebase_id,status,total,devices_used,sim_slot,delay,started_at) 
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (user_id, campaign_id, name, template_id, data_file_id, firebase_id,
             'RUNNING', total, json.dumps(devices), sim_slot, delay, now)
        )
        cid = c.lastrowid
        conn.commit()
        conn.close()
        return cid

    def add_campaign_targets(self, campaign_id: str, targets: List[dict]):
        conn = self._conn()
        for t in targets:
            conn.execute(
                "INSERT INTO campaign_targets (campaign_id,target_id,mobile,variables_json,message,status) VALUES (?,?,?,?,?,?)",
                (campaign_id, t['target_id'], t['mobile'], json.dumps(t.get('variables', {})),
                 t['message'], 'PENDING')
            )
        conn.commit()
        conn.close()

    def get_pending_targets(self, campaign_id: str, limit: int = 50) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM campaign_targets WHERE campaign_id=? AND status='PENDING' LIMIT ?",
            (campaign_id, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def update_target_status(self, campaign_id: str, target_id: str,
                             status: str, device_id: str = '', error: str = ''):
        conn = self._conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        conn.execute(
            "UPDATE campaign_targets SET status=?, device_id=?, processed_at=?, error=? WHERE campaign_id=? AND target_id=?",
            (status, device_id, now, error, campaign_id, target_id)
        )
        conn.commit()
        conn.close()

    def update_campaign_counters(self, campaign_id: str):
        conn = self._conn()
        completed = conn.execute(
            "SELECT COUNT(*) as c FROM campaign_targets WHERE campaign_id=? AND status='COMPLETED'",
            (campaign_id,)
        ).fetchone()['c']
        failed = conn.execute(
            "SELECT COUNT(*) as c FROM campaign_targets WHERE campaign_id=? AND status='FAILED'",
            (campaign_id,)
        ).fetchone()['c']
        conn.execute(
            "UPDATE campaigns SET completed=?, failed=? WHERE campaign_id=?",
            (completed, failed, campaign_id)
        )
        conn.commit()
        conn.close()
        return completed, failed

    def update_campaign_status(self, campaign_id: str, status: str, duration: str = ''):
        conn = self._conn()
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        if status in ('COMPLETED', 'STOPPED'):
            conn.execute(
                "UPDATE campaigns SET status=?, completed_at=?, duration=? WHERE campaign_id=?",
                (status, now, duration, campaign_id)
            )
        else:
            conn.execute(
                "UPDATE campaigns SET status=? WHERE campaign_id=?",
                (status, campaign_id)
            )
        conn.commit()
        conn.close()

    def cancel_pending_targets(self, campaign_id: str):
        conn = self._conn()
        conn.execute(
            "UPDATE campaign_targets SET status='CANCELLED' WHERE campaign_id=? AND status='PENDING'",
            (campaign_id,)
        )
        conn.commit()
        conn.close()

    def get_campaign(self, campaign_id: str) -> Optional[dict]:
        conn = self._conn()
        row = conn.execute("SELECT * FROM campaigns WHERE campaign_id=?", (campaign_id,)).fetchone()
        conn.close()
        if row:
            d = dict(row)
            d['devices_used'] = json.loads(d.get('devices_used', '[]'))
            return d
        return None

    def get_campaigns(self, user_id: int, limit: int = 20) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM campaigns WHERE user_id=? ORDER BY id DESC LIMIT ?",
            (user_id, limit)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_campaign_targets_all(self, campaign_id: str) -> List[dict]:
        conn = self._conn()
        rows = conn.execute(
            "SELECT * FROM campaign_targets WHERE campaign_id=? ORDER BY id",
            (campaign_id,)
        ).fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def get_next_campaign_number(self, user_id: int) -> int:
        conn = self._conn()
        row = conn.execute(
            "SELECT COUNT(*) as cnt FROM campaigns WHERE user_id=?",
            (user_id,)
        ).fetchone()
        conn.close()
        return (row['cnt'] if row else 0) + 1

    # ========== USER SETTINGS ==========
    def get_user_settings(self, user_id: int) -> dict:
        conn = self._conn()
        row = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        conn.close()
        if row:
            return dict(row)
        return {'user_id': user_id, 'default_delay': 2.0, 'default_sim': 0}

    def save_user_settings(self, user_id: int, **kwargs):
        conn = self._conn()
        existing = conn.execute("SELECT * FROM user_settings WHERE user_id=?", (user_id,)).fetchone()
        if existing:
            sets = []
            vals = []
            for k, v in kwargs.items():
                sets.append(f"{k}=?")
                vals.append(v)
            vals.append(user_id)
            conn.execute(f"UPDATE user_settings SET {','.join(sets)} WHERE user_id=?", vals)
        else:
            delay = kwargs.get('default_delay', 2.0)
            sim = kwargs.get('default_sim', 0)
            conn.execute("INSERT INTO user_settings (user_id, default_delay, default_sim) VALUES (?,?,?)",
                         (user_id, delay, sim))
        conn.commit()
        conn.close()

    # ========== DASHBOARD STATS ==========
    def get_user_stats(self, user_id: int) -> dict:
        conn = self._conn()
        total_campaigns = conn.execute(
            "SELECT COUNT(*) as c FROM campaigns WHERE user_id=?", (user_id,)
        ).fetchone()['c']
        total_targets = conn.execute(
            "SELECT COALESCE(SUM(total),0) as c FROM campaigns WHERE user_id=?", (user_id,)
        ).fetchone()['c']
        total_completed = conn.execute(
            "SELECT COALESCE(SUM(completed),0) as c FROM campaigns WHERE user_id=?", (user_id,)
        ).fetchone()['c']
        total_failed = conn.execute(
            "SELECT COALESCE(SUM(failed),0) as c FROM campaigns WHERE user_id=?", (user_id,)
        ).fetchone()['c']
        conn.close()
        return {
            'total_campaigns': total_campaigns,
            'total_targets': total_targets,
            'total_completed': total_completed,
            'total_failed': total_failed,
        }
