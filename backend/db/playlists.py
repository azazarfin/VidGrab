"""Owner-fenced atomic child creation and derived playlist parent state."""

import json
from uuid import uuid4
from fastapi import HTTPException

from backend.db.enqueue import insert_download
from backend.db.ownership import require_owner, require_recoverable, transaction
from backend.services.job_state import RUNNABLE, TERMINAL
from backend.services.playlists import PlaylistError, parse_selection, validate_playlist_result


async def sync_parent(db, parent_id, now):
    async with db.execute("SELECT * FROM downloads WHERE id=? AND job_kind='playlist'", (parent_id,)) as cursor:
        parent = await cursor.fetchone()
    if parent is None:
        return
    async with db.execute("SELECT status FROM downloads WHERE parent_id=?", (parent_id,)) as cursor:
        states = [row[0] for row in await cursor.fetchall()]
    if not states:
        return
    finished = sum(state in TERMINAL for state in states)
    status = "waiting_for_children"
    if parent["status"] == "cancelling":
        status = "cancelled" if finished == len(states) else "cancelling"
    elif finished == len(states):
        status = "failed" if "failed" in states else "cancelled" if "cancelled" in states else "complete"
    error = "One or more playlist items failed" if status == "failed" else None
    await db.execute("""UPDATE downloads SET status=?, progress=?, speed=0, eta=NULL, file_path=NULL,
        completed_at=?, error_message=? WHERE id=?""",
                     (status, finished * 100 / len(states), now if status in TERMINAL else None, error, parent_id))


async def sync_parents(db, now):
    async with db.execute("SELECT DISTINCT parent_id FROM downloads WHERE parent_id IS NOT NULL") as cursor:
        parents = [row[0] for row in await cursor.fetchall()]
    for parent in parents:
        await sync_parent(db, parent, now)
    return parents


class PlaylistStore:
    def __init__(self, runtime, owner):
        self.runtime = runtime
        self.owner = owner

    async def delete_history(self, job_id=None):
        removed = []
        async with transaction(self.runtime) as db:
            await require_owner(db, self.owner)
            if job_id is not None:
                async with db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)) as cursor:
                    row = await cursor.fetchone()
                if row is None:
                    raise HTTPException(404, "History item not found")
                if row["parent_id"] is not None:
                    raise HTTPException(409, "Remove playlist history through its parent")
                if row["status"] not in TERMINAL:
                    raise HTTPException(400, "Cannot delete an active download from history")
                roots = [job_id]
            else:
                async with db.execute("SELECT id FROM downloads WHERE parent_id IS NULL AND status IN ('complete', 'failed', 'cancelled')") as cursor:
                    roots = [row[0] for row in await cursor.fetchall()]
            for root in roots:
                async with db.execute("SELECT id, status FROM downloads WHERE id=? OR parent_id=?", (root, root)) as cursor:
                    group = await cursor.fetchall()
                async with db.execute("""SELECT 1 FROM downloads d JOIN download_attempts a ON a.download_id=d.id
                    JOIN attempt_cookie_files c ON c.attempt_id=a.id WHERE d.id=? OR d.parent_id=? LIMIT 1""", (root, root)) as cursor:
                    retained = await cursor.fetchone()
                if retained or any(row["status"] not in TERMINAL for row in group):
                    if job_id is not None:
                        raise HTTPException(409, "Playlist activity or retained cookie ownership prevents history removal")
                    continue
                await db.execute("DELETE FROM downloads WHERE parent_id=?", (root,))
                await db.execute("DELETE FROM downloads WHERE id=?", (root,))
                removed.extend(row["id"] for row in group)
        return removed

    async def expand(self, job_id, attempt_id, result, now):
        entries = validate_playlist_result(result)["playlist"]["entries"]
        async with transaction(self.runtime) as db:
            await require_owner(db, self.owner)
            async with db.execute("SELECT * FROM downloads WHERE id=?", (job_id,)) as cursor:
                parent = await cursor.fetchone()
            if parent is None or parent["status"] not in RUNNABLE or parent["current_attempt_id"] != attempt_id:
                return None
            if parent["job_kind"] != "playlist" or parent["parent_id"] is not None:
                raise PlaylistError("Only a root playlist attempt can expand children")
            async with db.execute("SELECT a.*, o.identity AS runtime_identity FROM download_attempts a JOIN runtime_owners o ON o.id=a.owner_id WHERE a.id=? AND a.download_id=? AND a.owner_id=? AND a.status='running'", (attempt_id, job_id, self.owner.id)) as cursor:
                attempt = await cursor.fetchone()
            if attempt is None:
                return None
            require_recoverable(attempt, self.owner.id)
            async with db.execute("SELECT 1 FROM attempt_cookie_files WHERE attempt_id=?", (attempt_id,)) as cursor:
                if await cursor.fetchone():
                    raise PlaylistError("Playlist cookie ownership must be cleaned before expansion")
            async with db.execute("SELECT 1 FROM downloads WHERE parent_id=?", (job_id,)) as cursor:
                if await cursor.fetchone():
                    raise PlaylistError("Playlist children were already committed")
            options = json.loads(parent["download_options"] or "{}")
            selected = parse_selection(options.get("playlist_items"))
            if selected is not None and tuple(entry["position"] for entry in entries) != selected:
                raise PlaylistError("Worker playlist positions differ from the committed selection")
            options.update(is_playlist=False, playlist_items=None)
            children = []
            for entry in entries:
                child_options = {**options, "duration": entry["duration"]}
                if entry["referer"]:
                    extra = dict(options.get("extra_args") or {})
                    if extra.get("http_headers") is not None and not isinstance(extra["http_headers"], dict):
                        raise PlaylistError("Invalid playlist request headers")
                    headers = dict(extra.get("http_headers") or {})
                    headers.setdefault("Referer", entry["referer"])
                    child_options["extra_args"] = {**extra, "http_headers": headers}
                child = {"id": str(uuid4()), "url": entry["url"], "title": entry["title"],
                         "thumbnail_url": entry["thumbnail_url"], "status": "queued", "progress": 0.0,
                         "speed": 0.0, "created_at": now, "format_selected": parent["format_selected"],
                         "download_options": json.dumps(child_options), "scheduled_at": None,
                         "auth_required": parent["auth_required"], "job_kind": "media",
                         "parent_id": job_id, "playlist_index": entry["position"]}
                await insert_download(db, child)
                children.append(child)
            await db.execute("UPDATE download_attempts SET status='complete', finished_at=?, error_message=NULL WHERE id=?", (now, attempt_id))
            await db.execute("""UPDATE downloads SET status='waiting_for_children', current_attempt_id=NULL,
                progress=0, speed=0, eta=NULL, file_path=NULL, completed_at=NULL, error_message=NULL WHERE id=?""", (job_id,))
        return children
