"""Opt-in real-loopback regressions for browser-lifetime fixture gating."""

import http.client
import io
import threading
import unittest
from urllib.parse import urlsplit
import wave

from tests.native_support import fixture_server


class FixtureStream(unittest.TestCase):
    def test_transfer_waits_until_explicit_release(self):
        gate = threading.Event()
        with fixture_server(gate) as url:
            target = urlsplit(url)
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
            connection.request('GET', target.path)
            response = connection.getresponse()
            first = response.read(4096)
            done = threading.Event()
            received = []

            def next_byte():
                received.append(response.read(1))
                done.set()

            reader = threading.Thread(target=next_byte)
            reader.start()
            try:
                self.assertFalse(done.wait(0.3), 'The fixture advanced before its release event')
                gate.set()
                self.assertTrue(done.wait(2), 'Released fixture did not continue')
                reader.join(timeout=2)
                body = first + received[0] + response.read()
                with wave.open(io.BytesIO(body)) as audio:
                    self.assertEqual(audio.getnframes(), 131072)
            finally:
                gate.set()
                reader.join(timeout=3)
                response.close()
                connection.close()

    def test_context_exit_releases_waiting_transfer(self):
        gate = threading.Event()
        with fixture_server(gate) as url:
            target = urlsplit(url)
            connection = http.client.HTTPConnection(target.hostname, target.port, timeout=3)
            connection.request('GET', target.path)
            response = connection.getresponse()
            first = response.read(4096)
        self.assertTrue(gate.is_set())
        body = first + response.read()
        self.assertEqual(len(body), 262188)
        response.close()
        connection.close()


if __name__ == '__main__':
    unittest.main(verbosity=2)
