import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union


@dataclass(frozen=True)
class Video:
    id: int
    owner_user_id: int
    file_id: str


class DB:
    def __init__(self, path: Union[str, Path]):
        self.path = str(path)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self._init()

    def close(self) -> None:
        self.conn.close()

    def _init(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL;")
        cur.execute("PRAGMA foreign_keys=ON;")

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
              user_id INTEGER PRIMARY KEY,
              username TEXT,
              has_video INTEGER NOT NULL DEFAULT 0,
              active_chat_user_id INTEGER,
              age INTEGER,
              gender TEXT,
              looking_for TEXT,
              about TEXT,
              profile_complete INTEGER NOT NULL DEFAULT 0,
              banned INTEGER NOT NULL DEFAULT 0,
              banned_notified INTEGER NOT NULL DEFAULT 0,
              created_at TEXT NOT NULL DEFAULT (datetime('now'))
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS videos (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              owner_user_id INTEGER NOT NULL UNIQUE,
              file_id TEXT NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY (owner_user_id) REFERENCES users(user_id) ON DELETE CASCADE
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS views (
              viewer_user_id INTEGER NOT NULL,
              video_id INTEGER NOT NULL,
              viewed_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (viewer_user_id, video_id),
              FOREIGN KEY (viewer_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
              FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS complaints (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              reporter_user_id INTEGER NOT NULL,
              video_id INTEGER NOT NULL,
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY (reporter_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
              FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
            );
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS ratings (
              rater_user_id INTEGER NOT NULL,
              video_id INTEGER NOT NULL,
              value INTEGER NOT NULL, -- 1=like, -1=dislike
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              PRIMARY KEY (rater_user_id, video_id),
              FOREIGN KEY (rater_user_id) REFERENCES users(user_id) ON DELETE CASCADE,
              FOREIGN KEY (video_id) REFERENCES videos(id) ON DELETE CASCADE
            );
            """
        )

        self.conn.commit()

        # Backward-compatible migrations for existing DBs.
        self._ensure_user_columns()

    def _ensure_user_columns(self) -> None:
        cur = self.conn.cursor()
        cur.execute("PRAGMA table_info(users);")
        cols = {row[1] for row in cur.fetchall()}

        def add(col_sql: str) -> None:
            cur.execute(f"ALTER TABLE users ADD COLUMN {col_sql};")

        if "age" not in cols:
            add("age INTEGER")
        if "username" not in cols:
            add("username TEXT")
        if "active_chat_user_id" not in cols:
            add("active_chat_user_id INTEGER")
        if "gender" not in cols:
            add("gender TEXT")
        if "looking_for" not in cols:
            add("looking_for TEXT")
        if "about" not in cols:
            add("about TEXT")
        if "profile_complete" not in cols:
            add("profile_complete INTEGER NOT NULL DEFAULT 0")
        if "banned" not in cols:
            add("banned INTEGER NOT NULL DEFAULT 0")
        if "banned_notified" not in cols:
            add("banned_notified INTEGER NOT NULL DEFAULT 0")

        self.conn.commit()

    def ensure_user(self, user_id: int, username: Optional[str] = None) -> None:
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO users(user_id, username) VALUES (?, ?);",
            (user_id, username),
        )
        if username is not None:
            cur.execute("UPDATE users SET username=? WHERE user_id=?;", (username, user_id))
        self.conn.commit()

    def get_username(self, user_id: int) -> Optional[str]:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT username FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        if not row:
            return None
        return row["username"]

    def get_active_chat_user(self, user_id: int) -> Optional[int]:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT active_chat_user_id FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        if not row or row["active_chat_user_id"] is None:
            return None
        return int(row["active_chat_user_id"])

    def start_chat(self, user_id: int, partner_user_id: int) -> None:
        self.ensure_user(user_id)
        self.ensure_user(partner_user_id)
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET active_chat_user_id=? WHERE user_id=?;", (partner_user_id, user_id))
        cur.execute("UPDATE users SET active_chat_user_id=? WHERE user_id=?;", (user_id, partner_user_id))
        self.conn.commit()

    def end_chat(self, user_id: int) -> Optional[int]:
        self.ensure_user(user_id)
        partner_user_id = self.get_active_chat_user(user_id)
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET active_chat_user_id=NULL WHERE user_id=?;", (user_id,))
        if partner_user_id is not None:
            cur.execute(
                "UPDATE users SET active_chat_user_id=NULL WHERE user_id=? AND active_chat_user_id=?;",
                (partner_user_id, user_id),
            )
        self.conn.commit()
        return partner_user_id

    def user_has_video(self, user_id: int) -> bool:
        cur = self.conn.cursor()
        cur.execute("SELECT has_video FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        return bool(row and row["has_video"])

    def profile_complete(self, user_id: int) -> bool:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT profile_complete FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        return bool(row and row["profile_complete"])

    def is_banned(self, user_id: int) -> bool:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT banned FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        return bool(row and row["banned"])

    def ban_user(self, user_id: int) -> None:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET banned=1, banned_notified=0 WHERE user_id=?;", (user_id,))
        self.conn.commit()

    def banned_notified(self, user_id: int) -> bool:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("SELECT banned_notified FROM users WHERE user_id=?;", (user_id,))
        row = cur.fetchone()
        return bool(row and row["banned_notified"])

    def set_banned_notified(self, user_id: int) -> None:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute("UPDATE users SET banned_notified=1 WHERE user_id=?;", (user_id,))
        self.conn.commit()

    def set_profile(
        self,
        user_id: int,
        age: int,
        gender: str,
        looking_for: str,
        about: str,
    ) -> None:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute(
            """
            UPDATE users
            SET age=?,
                gender=?,
                looking_for=?,
                about=?,
                profile_complete=1
            WHERE user_id=?;
            """,
            (age, gender, looking_for, about, user_id),
        )
        self.conn.commit()

    def get_profile(self, user_id: int) -> Optional[dict]:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT age, gender, looking_for, about, profile_complete FROM users WHERE user_id=?;",
            (user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return {
            "age": row["age"],
            "gender": row["gender"],
            "looking_for": row["looking_for"],
            "about": row["about"],
            "profile_complete": bool(row["profile_complete"]),
        }

    def set_user_video(self, user_id: int, file_id: str) -> None:
        self.ensure_user(user_id)
        cur = self.conn.cursor()
        # Keep one current video per owner.
        cur.execute("DELETE FROM videos WHERE owner_user_id=?;", (user_id,))
        cur.execute(
            "INSERT INTO videos(owner_user_id, file_id) VALUES (?, ?);",
            (user_id, file_id),
        )
        cur.execute("UPDATE users SET has_video=1 WHERE user_id=?;", (user_id,))
        self.conn.commit()

    def clear_user_video(self, owner_user_id: int) -> None:
        self.ensure_user(owner_user_id)
        cur = self.conn.cursor()
        cur.execute("DELETE FROM videos WHERE owner_user_id=?;", (owner_user_id,))
        cur.execute("UPDATE users SET has_video=0 WHERE user_id=?;", (owner_user_id,))
        self.conn.commit()

    def delete_video_by_id(self, video_id: int) -> Optional[int]:
        cur = self.conn.cursor()
        cur.execute("SELECT owner_user_id FROM videos WHERE id=?;", (video_id,))
        row = cur.fetchone()
        if not row:
            return None
        owner_user_id = int(row["owner_user_id"])
        cur.execute("DELETE FROM videos WHERE id=?;", (video_id,))
        # If owner has no other video rows (shouldn't), clear has_video.
        cur.execute("SELECT 1 FROM videos WHERE owner_user_id=? LIMIT 1;", (owner_user_id,))
        still_has = cur.fetchone() is not None
        cur.execute("UPDATE users SET has_video=? WHERE user_id=?;", (1 if still_has else 0, owner_user_id))
        self.conn.commit()
        return owner_user_id

    def get_user_video(self, owner_user_id: int) -> Optional[Video]:
        self.ensure_user(owner_user_id)
        cur = self.conn.cursor()
        cur.execute(
            "SELECT id, owner_user_id, file_id FROM videos WHERE owner_user_id=?;",
            (owner_user_id,),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Video(id=row["id"], owner_user_id=row["owner_user_id"], file_id=row["file_id"])

    def pick_next_video(self, viewer_user_id: int) -> Optional[Video]:
        self.ensure_user(viewer_user_id)
        cur = self.conn.cursor()
        cur.execute(
            """
            SELECT v.id, v.owner_user_id, v.file_id
            FROM videos v
            WHERE v.owner_user_id != ?
              AND NOT EXISTS (
                SELECT 1
                FROM views vw
                WHERE vw.viewer_user_id = ?
                  AND vw.video_id = v.id
              )
            ORDER BY RANDOM()
            LIMIT 1;
            """,
            (viewer_user_id, viewer_user_id),
        )
        row = cur.fetchone()
        if not row:
            return None
        return Video(id=row["id"], owner_user_id=row["owner_user_id"], file_id=row["file_id"])

    def mark_viewed(self, viewer_user_id: int, video_id: int) -> None:
        self.ensure_user(viewer_user_id)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT OR IGNORE INTO views(viewer_user_id, video_id) VALUES (?, ?);",
            (viewer_user_id, video_id),
        )
        self.conn.commit()

    def add_complaint(self, reporter_user_id: int, video_id: int) -> None:
        self.ensure_user(reporter_user_id)
        cur = self.conn.cursor()
        cur.execute(
            "INSERT INTO complaints(reporter_user_id, video_id) VALUES (?, ?);",
            (reporter_user_id, video_id),
        )
        self.conn.commit()

    def rate(self, rater_user_id: int, video_id: int, value: int) -> None:
        if value not in (1, -1):
            raise ValueError("rating value must be 1 or -1")
        self.ensure_user(rater_user_id)
        cur = self.conn.cursor()
        cur.execute(
            """
            INSERT INTO ratings(rater_user_id, video_id, value)
            VALUES (?, ?, ?)
            ON CONFLICT(rater_user_id, video_id) DO UPDATE SET
              value=excluded.value,
              created_at=datetime('now');
            """,
            (rater_user_id, video_id, value),
        )
        self.conn.commit()

    def get_video_likes(self, video_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM ratings WHERE video_id=? AND value=1;",
            (video_id,),
        )
        row = cur.fetchone()
        return int(row["c"] if row else 0)

    def get_video_dislikes(self, video_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute(
            "SELECT COUNT(*) AS c FROM ratings WHERE video_id=? AND value=-1;",
            (video_id,),
        )
        row = cur.fetchone()
        return int(row["c"] if row else 0)

    def get_video_views(self, video_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM views WHERE video_id=?;", (video_id,))
        row = cur.fetchone()
        return int(row["c"] if row else 0)

    def get_video_complaints(self, video_id: int) -> int:
        cur = self.conn.cursor()
        cur.execute("SELECT COUNT(*) AS c FROM complaints WHERE video_id=?;", (video_id,))
        row = cur.fetchone()
        return int(row["c"] if row else 0)
