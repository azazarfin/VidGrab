import asyncio
import logging
import sys
import yt_dlp
import httpx

logger = logging.getLogger("vidgrab.updater")

async def check_ytdlp_version() -> dict:
    """Checks the current local version of yt-dlp against the latest PyPI release."""
    try:
        current_version = yt_dlp.version.__version__
    except Exception as e:
        logger.error(f"Failed to check local yt-dlp version: {e}")
        current_version = "unknown"

    latest_version = None
    update_available = False

    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            res = await client.get("https://pypi.org/pypi/yt-dlp/json")
            if res.status_code == 200:
                data = res.json()
                latest_version = data.get("info", {}).get("version", "")
        
        if latest_version and current_version != "unknown":
            try:
                from packaging.version import parse as parse_version
                update_available = parse_version(latest_version) > parse_version(current_version)
            except Exception:
                # Fallback to direct inequality if packaging is not installed or parsing fails
                update_available = latest_version != current_version
    except Exception as e:
        logger.error(f"Failed to fetch latest yt-dlp version from PyPI: {e}")

    return {
        "current": current_version,
        "latest": latest_version,
        "update_available": update_available
    }

async def update_ytdlp() -> dict:
    """Runs pip install --upgrade yt-dlp in a subprocess to update the package."""
    logger.info("Starting yt-dlp update...")
    try:
        # Use the current Python executable to run pip
        process = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "--upgrade", "yt-dlp",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await process.communicate()
        
        if process.returncode == 0:
            import importlib
            importlib.reload(yt_dlp)  # Attempt to reload the module
            if hasattr(yt_dlp, "version"):
                importlib.reload(yt_dlp.version)
            
            try:
                new_version = yt_dlp.version.__version__
            except Exception:
                new_version = "unknown"

            logger.info(f"yt-dlp updated successfully to version {new_version}")
            return {
                "success": True,
                "message": f"Successfully updated yt-dlp to version {new_version}",
                "version": new_version
            }
        else:
            error_msg = stderr.decode().strip()
            logger.error(f"Failed to update yt-dlp: {error_msg}")
            return {
                "success": False,
                "message": f"Failed to update yt-dlp: {error_msg}"
            }
    except Exception as e:
        logger.error(f"Exception during yt-dlp update: {e}")
        return {
            "success": False,
            "message": f"Exception during update: {str(e)}"
        }
