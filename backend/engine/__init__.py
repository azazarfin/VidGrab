"""Import-safe, authenticated boundary for the P02 packaged lifecycle spike."""

from .application import create_engine_app

__all__ = ["create_engine_app"]
