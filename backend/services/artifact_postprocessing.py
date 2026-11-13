"""Capture concrete move records, then final outputs after yt-dlp post-processing."""

from pathlib import Path

from yt_dlp.postprocessor.common import PostProcessor

from backend.runtime import OutputPathError
from backend.services.artifacts import append_journal, completed_artifacts, is_link, validate_output_path


class CaptureArtifactMovesPP(PostProcessor):
    def __init__(self, runtime, workspace):
        super().__init__()
        self.runtime = runtime
        self.workspace = workspace

    def run(self, info):
        source = info["filepath"]
        final_dir = Path(info.get("__finaldir", Path(source).parent))
        kinds = {value.get("filepath"): "subtitle" for value in (info.get("requested_subtitles") or {}).values()}
        kinds.update({value.get("filepath"): "thumbnail" for value in info.get("thumbnails") or []})
        moves = [{"source": source, "path": str(final_dir / Path(source).name), "kind": "media"}]
        file_moves = info.get("__files_to_move") or {}
        for old, new in list(file_moves.items()):
            if self.workspace is not None and new and Path(new).is_relative_to(self.workspace):
                # Explicit sidecar templates are staged; publish the recorded filename beside the media.
                new = str(final_dir / Path(new).name)
                file_moves[old] = new
            if old != source:
                moves.append({"source": old, "path": new or str(final_dir / Path(old).name), "kind": kinds.get(old, "auxiliary")})
        for move in moves:
            destination = validate_output_path(self.runtime, move["path"])
            if self.workspace is not None:
                staged = Path(move["source"])
                if not staged.resolve().is_relative_to(self.workspace) or is_link(staged):
                    raise OutputPathError("Output did not originate in this attempt's owned workspace")
                if destination.exists():
                    raise OutputPathError("Output already exists; choose a different filename before retrying")
        if self.workspace is not None:
            append_journal(self.runtime, self.workspace,
                           [{**move, "state": "partial", "size": Path(move["source"]).stat().st_size} for move in moves])
        info["__vidgrab_artifact_moves"] = moves
        return [], info


class RecordFinalArtifactsPP(PostProcessor):
    def __init__(self, runtime, records, workspace=None):
        super().__init__()
        self.runtime = runtime
        self.records = records
        self.workspace = workspace

    def run(self, info):
        moves = info.pop("__vidgrab_artifact_moves", [])
        records = [{"path": info["filepath"], "kind": "media"}]
        sources = {info["filepath"]: moves[0]["source"]} if moves else {}
        for move in moves:
            if move["source"] != move["path"] and Path(move["source"]).exists():
                raise OutputPathError("Owned output was not published to its final path")
            if move["kind"] != "media" and move["path"] != info["filepath"]:
                records.append({"path": move["path"], "kind": move["kind"]})
                sources[move["path"]] = move["source"]
        # This is the last after_move processor: extensions and sidecars are authoritative.
        completed = completed_artifacts(self.runtime, {"filepath": info["filepath"], "artifacts": records})
        if self.workspace is not None:
            append_journal(self.runtime, self.workspace,
                           [{**record, "source": sources[record["path"]]} for record in completed])
        self.records.extend(records)
        return [], info
