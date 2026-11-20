import os
import shutil
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

root = Path(SPECPATH).parent
tools = Path(os.environ["VIDGRAB_COMPONENT_DIR"]).resolve()
notices_stage = Path(workpath) / "notices_stage"
notices_stage.mkdir(parents=True, exist_ok=True)
fonts_notice = notices_stage / "FONTS_LICENSE.txt"
shutil.copy2(root / "extension" / "fonts" / "LICENSE.txt", fonts_notice)

datas = [
    (str(tools / "manifest.json"), "tools"),
    (str(tools / "GPLv3.ffmpeg.txt"), "notices"),
    (str(root / "LICENSE"), "notices"),
    (str(root / "packaging" / "THIRD_PARTY.md"), "notices"),
    (str(fonts_notice), "notices"),
]
for package in ("yt-dlp", "yt-dlp-ejs"):
    datas += copy_metadata(package)
datas += collect_data_files("yt_dlp_ejs")
suffix = ".exe" if sys.platform == "win32" else ""
binaries = [(str(tools / (name + suffix)), "tools") for name in ("ffmpeg", "ffprobe", "deno")]

analysis = Analysis(
    [str(root / "packaging" / "engine_entry.py")],
    pathex=[str(root)], binaries=binaries, datas=datas,
    hiddenimports=collect_submodules("yt_dlp") + collect_submodules("yt_dlp_ejs"),
    hookspath=[], runtime_hooks=[], excludes=[], noarchive=False,
)
python = PYZ(analysis.pure)
engine = EXE(
    python, analysis.scripts, [], exclude_binaries=True, name="vidgrab-engine",
    debug=False, strip=False, upx=False, console=True,
)
collection = COLLECT(engine, analysis.binaries, analysis.datas, strip=False, upx=False, name="vidgrab-engine")
