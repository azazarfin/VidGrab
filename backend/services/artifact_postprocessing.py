"""Capture concrete move records, then final outputs after yt-dlp post-processing."""

from pathlib import Path

from yt_dlp.postprocessor.common import PostProcessor

from backend.runtime import OutputPathError
from backend.services.artifacts import completed_artifacts, validate_output_path
from backend.services.publication import plan_publications, publish_artifact, verify_published


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
        if self.workspace is not None:
            planned = plan_publications(self.runtime, self.workspace, moves)
            moves = [publish_artifact(self.runtime, self.workspace, move) for move in planned]
            # The built-in mover now sees only the final media path. Its ordinary
            # shutil.move path could replace a destination created concurrently.
            info["filepath"] = moves[0]["path"]
            info["__files_to_move"] = {}
        else:
            for move in moves:
                validate_output_path(self.runtime, move["path"])
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
        for move in moves:
            if self.workspace is not None:
                verify_published(self.runtime, move)
            elif move["source"] != move["path"] and Path(move["source"]).exists():
                raise OutputPathError("Owned output was not published to its final path")
            if move["kind"] != "media" and move["path"] != info["filepath"]:
                records.append({"path": move["path"], "kind": move["kind"]})
        # This is the last after_move processor: extensions and sidecars are authoritative.
        completed_artifacts(self.runtime, {"filepath": info["filepath"], "artifacts": records})
        self.records.extend(records)
        return [], info
