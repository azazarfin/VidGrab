"""Development/legacy ASGI adapter; importing it never initializes runtime state."""

from backend.app import create_app

app = create_app()
