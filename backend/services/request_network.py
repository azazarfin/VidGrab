"""One pinned urllib wire boundary, including redirected requests and bodies."""

import threading
import time
import urllib.request
from http.client import IncompleteRead
from urllib.parse import urlsplit

from yt_dlp.downloader import get_suitable_downloader
from yt_dlp.networking._urllib import HTTPHandler

from backend.services.request_budget import RequestCoordinator
from backend.services.retry_policy import RequestFailure, backoff, retry_after


def constrain_network_options(options):
    options.update(retries=0, fragment_retries=0, extractor_retries=0, file_access_retries=0,
                   concurrent_fragment_downloads=1, socket_timeout=10, ignoreerrors=False,
                   external_downloader=None, external_downloader_args={}, hls_prefer_native=True,
                   enable_file_urls=False)


class BudgetedRequests:
    def __init__(self, coordinator, cancelled, *, clock=time.time, credential_url=None):
        self.coordinator, self.cancelled, self.clock = coordinator, cancelled, clock
        self.failure = None
        self.responses = {}
        self.lock = threading.RLock()
        self.credential_origin = self._origin(credential_url) if credential_url is not None else None

    @staticmethod
    def _origin(url):
        try:
            parsed = urlsplit(url)
            return parsed.scheme, parsed.hostname, parsed.port if parsed.port is not None else (443 if parsed.scheme == "https" else 80)
        except ValueError:
            return None

    def check(self):
        if self.cancelled():
            raise RequestFailure("REQUEST_REJECTED")
        if self.failure is not None:
            raise self.failure

    def open(self, send, request):
        self.check()
        if self.credential_origin is not None and self._origin(request.full_url) != self.credential_origin:
            # This runs after urllib's cookie processor and before each wire
            # request, including redirects. Cookie domains alone do not fence
            # ports, scheme downgrades or overly broad parent domains.
            request.remove_header("Cookie")
            request.remove_header("Referer")
        lease = None
        try:
            lease = self.coordinator.acquire(request.full_url, self.clock())
            response = send(request)
            self._watch(response, lease)
            status = response.status
            if status in (401, 403):
                response.close()
                raise RequestFailure("AUTH_REQUIRED")
            if status in (429, 500, 502, 503, 504):
                raw = response.headers.get("Retry-After")
                deadline = retry_after(raw, self.clock())
                response.close()
                if deadline is None and raw and raw.strip().isascii() and raw.strip().isdigit():
                    raise RequestFailure("REQUEST_REJECTED")
                deadline = deadline if deadline is not None else self.clock() + backoff(0)
                self.coordinator.cooldown(request.full_url, deadline, self.clock())
                raise RequestFailure("UPSTREAM_RATE_LIMITED" if status == 429 else "UPSTREAM_TRANSIENT", retry_at=deadline)
            if status >= 400:
                response.close()
                raise RequestFailure("UPSTREAM_PERMANENT")
            return response
        except RequestFailure as error:
            if lease is not None:
                self._release(lease)
            self.failure = error
            raise
        except (OSError, TimeoutError) as error:
            if lease is not None:
                self._release(lease)
            self.failure = RequestFailure("TRANSPORT_ERROR")
            raise self.failure from error
        except BaseException:
            if lease is not None:
                self._release(lease)
            raise

    def _release(self, lease):
        with self.lock:
            self.responses.pop(lease, None)
            self.coordinator.release(lease)

    def _watch(self, response, lease):
        with self.lock:
            self.responses[lease] = response
        released = False
        def release():
            nonlocal released
            with self.lock:
                if not released:
                    self._release(lease)
                    released = True
        close = response.close
        def close_response(*args, **kwargs):
            try:
                return close(*args, **kwargs)
            finally:
                release()
        response.close = close_response
        for name in ("read", "read1", "readinto"):
            original = getattr(response, name, None)
            if original is None:
                continue
            def read_response(*args, original=original, **kwargs):
                try:
                    result = original(*args, **kwargs)
                except (OSError, IncompleteRead) as error:
                    self.failure = RequestFailure("TRANSPORT_ERROR")
                    close_response()
                    raise self.failure from error
                except BaseException:
                    close_response()
                    raise
                if response.closed or (hasattr(response, "isclosed") and response.isclosed()):
                    release()
                return result
            setattr(response, name, read_response)

    def close(self):
        with self.lock:
            responses = list(self.responses.values())
        failure = None
        for response in responses:
            try:
                response.close()
            except Exception as error:
                failure = failure or error
        if failure is not None:
            raise failure


def close_generic_header_responses(ydl):
    """The pinned GenericIE does not reuse its initial body in HLS branches."""
    generic = ydl.get_info_extractor("Generic")
    pending = []
    request_page = generic._request_webpage
    extract_manifest = generic._extract_m3u8_formats_and_subtitles
    extract = generic._real_extract

    def close_pending():
        while pending:
            pending.pop().close()

    def request_webpage(*args, **kwargs):
        response = request_page(*args, **kwargs)
        if response:
            pending.append(response)
        return response

    def extract_m3u8(*args, **kwargs):
        close_pending()
        return extract_manifest(*args, **kwargs)

    def extract_generic(*args, **kwargs):
        try:
            return extract(*args, **kwargs)
        finally:
            close_pending()

    generic._request_webpage = request_webpage
    generic._extract_m3u8_formats_and_subtitles = extract_m3u8
    generic._real_extract = extract_generic


def install_request_budget(ydl, runtime, context, cancelled, *, clock=time.time, credential_url=None):
    gate = BudgetedRequests(RequestCoordinator(runtime, context), cancelled, clock=clock, credential_url=credential_url)
    director = ydl._request_director
    urllib_handler = director.handlers.get("Urllib")
    if urllib_handler is None:
        raise RequestFailure("REQUEST_REJECTED")
    for key in list(director.handlers):
        if key != "Urllib":
            director.handlers.pop(key).close()
    create = urllib_handler._create_instance
    def create_instance(*args, **kwargs):
        original = create(*args, **kwargs)
        opener = urllib.request.OpenerDirector()
        for handler in original.handlers:
            if isinstance(handler, (urllib.request.FTPHandler, urllib.request.DataHandler, urllib.request.FileHandler)):
                continue
            if isinstance(handler, HTTPHandler):
                for name in ("http_open", "https_open"):
                    method = getattr(handler, name, None)
                    if method is not None:
                        setattr(handler, name, lambda request, method=method: gate.open(method, request))
            opener.add_handler(handler)
        opener.addheaders = []
        return opener
    urllib_handler._create_instance = create_instance
    urllib_handler._SUPPORTED_URL_SCHEMES = ("http", "https")
    urlopen = ydl.urlopen
    def open_request(*args, **kwargs):
        gate.check()
        return urlopen(*args, **kwargs)
    ydl.urlopen = open_request
    download = ydl.dl
    def download_media(name, info, *args, **kwargs):
        downloader = get_suitable_downloader(info, ydl.params)
        if downloader is not None and downloader.__module__ == "yt_dlp.downloader.external":
            raise RequestFailure("REQUEST_REJECTED")
        return download(name, info, *args, **kwargs)
    ydl.dl = download_media
    close_generic_header_responses(ydl)
    return gate
