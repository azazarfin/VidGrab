import socket
from unittest.mock import patch

from tests.support import OfflineTestCase


class NetworkGuardTests(OfflineTestCase):
    def test_loopback_guard_works_when_the_platform_has_no_unix_socket_family(self):
        with socket.socket(socket.AF_INET) as listener, socket.socket(socket.AF_INET) as client:
            listener.bind(("127.0.0.1", 0))
            listener.listen(1)
            # Windows Python uses TCP for asyncio's internal socket pair.
            with patch.object(socket, "AF_UNIX", create=True):
                del socket.AF_UNIX
                try:
                    client.connect(listener.getsockname())
                except AttributeError as error:
                    self.fail(f"The guard assumes an unavailable socket family: {error}")
                finally:
                    # mock removes newly created attributes on platforms lacking it.
                    socket.AF_UNIX = None
            peer, _ = listener.accept()
            peer.close()
