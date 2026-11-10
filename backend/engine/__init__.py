"""Import-safe, authenticated boundary for the P02 packaged lifecycle spike."""

def create_engine_app(*args, **kwargs):
    # Worker entrypoints must import without loading the ASGI application.
    from .application import create_engine_app as factory
    return factory(*args, **kwargs)

__all__ = ["create_engine_app"]
