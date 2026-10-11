"""Settings (JSON) and the standalone email registry (SQLite), under DATA_DIR.

plab-register has no concept of a PornoLab "account" in its own storage --
once registration + activation succeeds, the finished credentials are handed
to pornolab-seeder via POST /api/accounts/import and this service forgets
about them. The only thing genuinely persisted here is the in-flight email
registry: inboxes created but not yet turned into a PornoLab account.
"""

import json
import os
import sqlite3
import threading
import time
from dataclasses import asdict, dataclass
from pathlib import Path

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETTINGS_PATH = DATA_DIR / "settings.json"


@dataclass
class Settings:
    # Which tool creates throwaway inboxes. "tuta" is the only one that's
    # actually phone-free end to end (see app/gmx.py's module docstring for
    # the web.de/gmx.net phone-gate dead end, and app/mail_ru.py for mail.ru's
    # VK-ID phone requirement).
    email_factory_provider: str = "tuta"
    gmx_provider: str = "web.de"
    sms_activate_api_key: str = ""
    mailru_domain: str = "mail.ru"

    pornolab_url: str = "https://pornolab.net"

    # Where pornolab-seeder lives, for the handoff call after a successful
    # registration (POST {seeder_url}/api/accounts/import).
    seeder_url: str = os.environ.get("SEEDER_URL", "http://pornolab-seeder:8000")


class SettingsStore:
    def __init__(self, path: Path | None = None):
        self.path = path or SETTINGS_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.settings = self._load()

    def _load(self) -> Settings:
        if self.path.exists():
            try:
                d = json.loads(self.path.read_text())
                return Settings(**{k: v for k, v in d.items() if k in Settings.__dataclass_fields__})
            except Exception:                                            # noqa: BLE001
                pass
        return Settings()

    def update(self, **kw) -> None:
        for k, v in kw.items():
            if k in Settings.__dataclass_fields__:
                setattr(self.settings, k, v)
        self.path.write_text(json.dumps(asdict(self.settings), indent=2))


SCHEMA = """
create table if not exists emails (
    id integer primary key autoincrement,
    provider text not null,
    address text not null,
    password text not null,
    created_at real,
    note text default ''
);
create table if not exists events (
    id integer primary key autoincrement,
    at real,
    level text,
    message text
);
"""


class DB:
    def __init__(self, path: Path | None = None):
        self.path = path or DATA_DIR / "register.db"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()

    def execute(self, sql: str, args=()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, args)
            self.conn.commit()
            return cur

    def rows(self, sql: str, args=()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.conn.execute(sql, args).fetchall()]

    # -- emails ----------------------------------------------------------- #
    def list_emails(self) -> list[dict]:
        return self.rows("select id, provider, address, created_at, note from emails order by id desc")

    def get_email(self, email_id: int) -> dict | None:
        rows = self.rows("select * from emails where id=?", (email_id,))
        return rows[0] if rows else None

    def create_email(self, *, provider: str, address: str, password: str, note: str = "") -> int:
        cur = self.execute(
            "insert into emails(provider, address, password, created_at, note) values (?,?,?,?,?)",
            (provider, address, password, time.time(), note),
        )
        return cur.lastrowid

    def delete_email(self, email_id: int) -> None:
        self.execute("delete from emails where id=?", (email_id,))

    # -- events ------------------------------------------------------------ #
    def event(self, level: str, message: str) -> None:
        self.execute("insert into events(at, level, message) values (?,?,?)",
                     (time.time(), level, message))

    def events(self, limit: int = 200) -> list[dict]:
        return self.rows("select * from events order by id desc limit ?", (limit,))
