"""Redact request material at diagnostic sinks, preserving operational values."""
import re
import traceback

_HEADERS = re.compile(r"\b(?:authorization|proxy-authorization|cookies?|set-cookie|referer|user[-_]agent|"
                      r"(?:http[-_]|request[-_])?headers|auth[-_]ref|probe[-_]id|credential[-_]url)"
                      r"['\"]?\s*[:=]", re.IGNORECASE)
# Punctuation is legal inside URLs; consume the whole non-whitespace token.
_URLS = re.compile(r"\bhttps?(?:://|%3a%2f%2f)\S+", re.IGNORECASE)


def redact_diagnostic(value):
    text = str(value)
    if _HEADERS.search(text):
        return "Request details omitted from diagnostics"
    return _URLS.sub("[redacted URL]", text)


def redact_exception(error):
    return redact_diagnostic("".join(traceback.format_exception(error)))
