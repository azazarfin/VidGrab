"""Validate fresh forwarded cookies without persisting a browser credential."""

from urllib.parse import urlsplit

from backend.cookie_manager import encode_cookies


def validate_fresh_cookies(url, cookies, now):
    if not cookies:
        raise ValueError("Share fresh browser cookies to continue this download")
    encode_cookies(cookies)
    host = (urlsplit(url).hostname or "").lower()
    for cookie in cookies:
        domain = str(cookie.get("domain") or "").lstrip(".").lower()
        expiry = cookie.get("expirationDate")
        if not domain or not (host == domain or host.endswith("." + domain)):
            raise ValueError("Fresh cookies must belong to the download's browser site")
        if expiry is not None and expiry > 0 and expiry <= now:
            raise ValueError("Browser cookies have expired; share a fresh context")
