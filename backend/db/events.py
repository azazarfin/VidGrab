"""Read one consistent SQLite snapshot; event payloads contain only invalidations."""

from contextlib import asynccontextmanager

import aiosqlite

from backend.db.ownership import require_owner
from backend.db.settings import read_settings


@asynccontextmanager
async def read_transaction(runtime, owner):
    async with aiosqlite.connect(runtime.db_path.resolve().as_uri() + "?mode=ro", uri=True) as db:
        db.row_factory = aiosqlite.Row
        await db.execute("BEGIN")
        try:
            await require_owner(db, owner)
            yield db
        finally:
            await db.rollback()


async def event_cursor(db):
    async with db.execute("SELECT seq FROM sqlite_sequence WHERE name='protocol_events'") as cursor:
        row = await cursor.fetchone()
    return row[0] if row is not None else 0


async def download_page(db, limit=25, after=None):
    async with db.execute("SELECT COUNT(*), COALESCE(SUM(status NOT IN ('complete','failed','cancelled')),0) FROM downloads") as cursor:
        total, unfinished = await cursor.fetchone()
    async with db.execute("SELECT id,url,revision,title,status,progress,file_path,error_code,retry_at,parent_id "
                          "FROM downloads WHERE (? IS NULL OR id>?) ORDER BY id LIMIT ?", (after, after, limit + 1)) as cursor:
        rows = await cursor.fetchall()
    jobs = [{**dict(row), "title": row["title"][:512] if row["title"] else row["title"],
             "progress": row["progress"] or 0.0} for row in rows[:limit]]
    return {"jobs": jobs, "next_after": jobs[-1]["id"] if len(rows) > limit else None,
            "counts": {"total": total, "unfinished": unfinished}}


class EventRepository:
    def __init__(self, runtime, owner, validate_settings):
        self.runtime = runtime
        self.owner = owner
        self.validate_settings = validate_settings

    async def settings(self):
        async with read_transaction(self.runtime, self.owner) as db:
            revision, values = await read_settings(db)
            return {"revision": revision, "values": self.validate_settings(values).model_dump()}

    async def downloads(self, limit, after):
        async with read_transaction(self.runtime, self.owner) as db:
            return await download_page(db, limit, after)

    async def events(self, instance_id, cursor, limit):
        async with read_transaction(self.runtime, self.owner) as db:
            current = await event_cursor(db)
            async with db.execute("SELECT MIN(sequence) FROM protocol_events") as query:
                oldest = (await query.fetchone())[0]
            gap = (instance_id != self.owner.id or cursor > current
                   or (oldest is not None and cursor < oldest - 1))
            if gap:
                revision, values = await read_settings(db)
                snapshot = {**await download_page(db, min(limit, 25)), "instance_id": self.owner.id, "cursor": current,
                            "settings": {"revision": revision, "values": self.validate_settings(values).model_dump()}}
                return {"instance_id": self.owner.id, "cursor": current, "resnapshot": True,
                        "has_more": False, "snapshot": snapshot, "events": []}
            async with db.execute("SELECT sequence,topic,entity_id,revision,kind FROM protocol_events "
                                  "WHERE sequence>? ORDER BY sequence LIMIT ?", (cursor, limit + 1)) as query:
                rows = await query.fetchall()
            events = [dict(row) for row in rows[:limit]]
            return {"instance_id": self.owner.id, "cursor": events[-1]["sequence"] if events else current,
                    "resnapshot": False, "has_more": len(rows) > limit, "snapshot": None, "events": events}
