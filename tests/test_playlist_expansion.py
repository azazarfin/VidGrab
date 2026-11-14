"""Playlist metadata is finite, explicit and bounded before worker delivery."""

import importlib.util

from tests.support import OfflineTestCase


class PlaylistExpansionTests(OfflineTestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.playlists"),
                             "Playlist expansion needs a bounded metadata boundary")
        from backend.services import playlists
        return playlists

    def result(self, entries):
        return {"playlist": {"entries": entries}}

    def test_selection_is_positive_bounded_and_preserves_playlist_order(self):
        service = self.service()
        self.assertEqual(service.parse_selection("3,1-2,2"), (1, 2, 3))
        for selection in ("", "0", "-1", "3-1", "1:4", "1,", "4097", "1-129"):
            with self.subTest(selection=selection), self.assertRaises(ValueError):
                service.parse_selection(selection)

    def test_nested_unsafe_or_duplicate_children_are_rejected(self):
        service = self.service()
        for entries in (
            [{"position": 1, "url": "file:///tmp/media"}],
            [{"position": 1, "url": "https://user:secret@fixture.invalid/media"}],
            [{"position": True, "url": "https://fixture.invalid/media"}],
            [{"position": 1, "url": "https://fixture.invalid/media", "entries": []}],
            [{"position": 1, "url": "https://fixture.invalid/a"}, {"position": 1, "url": "https://fixture.invalid/b"}],
            [],
        ):
            with self.subTest(entries=entries), self.assertRaises(ValueError):
                service.validate_playlist_result(self.result(entries))

    def test_large_manifest_and_child_count_are_rejected(self):
        service = self.service()
        entries = [{"position": i, "url": f"https://fixture.invalid/{i}"} for i in range(1, 130)]
        with self.assertRaises(ValueError):
            service.validate_playlist_result(self.result(entries))
        entries = [{"position": i, "url": "https://fixture.invalid/" + "a" * 1900} for i in range(1, 40)]
        with self.assertRaises(ValueError):
            service.validate_playlist_result(self.result(entries))

    def test_raw_metadata_iteration_stops_at_requested_position(self):
        service = self.service()
        visited = []
        def entries():
            for index in range(1, 5000):
                visited.append(index)
                yield {"url": f"https://fixture.invalid/{index}", "title": str(index)}
        class Extractor:
            def extract_info(self, url, *, download, process):
                self.options = (download, process)
                return {"_type": "playlist", "entries": entries()}
        extractor = Extractor()
        result = service.extract_playlist(extractor, "https://fixture.invalid/list", "2,4", lambda: False)
        self.assertEqual(visited, [1, 2, 3, 4])
        self.assertEqual(extractor.options, (False, False))
        self.assertEqual([entry["position"] for entry in result["playlist"]["entries"]], [2, 4])

    def test_missing_selected_entry_fails_without_inventing_youtube_urls(self):
        service = self.service()
        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {"_type": "playlist", "entries": [{"id": "private"}]}
        with self.assertRaises(ValueError):
            service.extract_playlist(Extractor(), "https://fixture.invalid/list", "1", lambda: False)

    def test_worker_result_guard_accepts_metadata_but_rejects_mixed_media(self):
        from backend.engine.worker_owner import validate_worker_message
        result = self.result([{"position": 1, "url": "https://fixture.invalid/one"}])
        try:
            validate_worker_message({"type": "result", "data": result})
        except ValueError as error:
            self.fail(f"Owned workers must deliver bounded playlist metadata: {error}")
        with self.assertRaises(ValueError):
            validate_worker_message({"type": "result", "data": {**result, "filepath": "/tmp/unowned.mp3"}})

    def test_single_source_generic_entries_preserve_only_the_required_referer(self):
        service = self.service()
        class Extractor:
            def extract_info(self, *args, **kwargs):
                return {"_type": "playlist", "entries": [{"formats": [{"url": "https://fixture.invalid/one.wav",
                    "http_headers": {"Referer": "https://fixture.invalid/list", "Cookie": "secret"}}]}]}
        result = service.extract_playlist(Extractor(), "https://fixture.invalid/list", None, lambda: False)
        entry = result["playlist"]["entries"][0]
        self.assertEqual(entry["url"], "https://fixture.invalid/one.wav")
        self.assertEqual(entry.get("referer"), "https://fixture.invalid/list")
        self.assertNotIn("secret", str(result))
