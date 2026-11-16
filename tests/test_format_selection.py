"""Expired format IDs retain witnessed quality/container intent in one owned extraction."""
import importlib.util

from tests.support import OfflineTestCase

OLD = {"format_id": "old", "ext": "mp4", "height": 720, "width": 1280,
       "vcodec": "avc1", "acodec": "none", "fps": 30.0, "resolution": "1280x720"}


class FormatSelectionTests(OfflineTestCase):
    def service(self):
        self.assertIsNotNone(importlib.util.find_spec("backend.services.format_selection"),
                             "Format selections need a bounded, context-scoped metadata witness")
        from backend.services import format_selection
        return format_selection

    def select(self, formats, *, expression="old"):
        module = self.service()
        store = module.ProbeWitnesses(lambda: 1000)
        identity = store.create("https://fixture.invalid/watch", None, [OLD], expires_at=1300)
        intent = store.resolve(identity, "https://fixture.invalid/watch", None, expression)
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "simulate": True, "cachedir": False, "format": expression}) as ydl:
            refresh = module.install_format_refresh(ydl, intent)
            info = {"id": "fixture", "title": "Fixture", "formats": [dict(item, url="https://fixture.invalid/" + item["format_id"] + ".mp4") for item in formats]}
            info = ydl.process_video_result(info, download=True)
            return info, refresh

    def test_changed_id_uses_same_height_width_container_and_frame_rate(self):
        info, refresh = self.select([{**OLD, "format_id": "new"}, {**OLD, "format_id": "lower", "height": 480},
                                    {**OLD, "format_id": "other-container", "ext": "webm"}, {**OLD, "format_id": "other-fps", "fps": 60.0}])
        self.assertEqual(info["format_id"], "new")
        self.assertEqual(refresh.count, 1)

    def test_an_unchanged_valid_format_does_not_use_the_refresh(self):
        info, refresh = self.select([OLD])
        self.assertEqual(info["format_id"], "old")
        self.assertEqual(refresh.count, 0)

    def test_refresh_preserves_the_separate_audio_selector(self):
        info, refresh = self.select([{**OLD, "format_id": "new"}, {"format_id": "audio", "ext": "m4a", "vcodec": "none", "acodec": "mp4a"}],
                                    expression="old+bestaudio/best")
        self.assertEqual([item["format_id"] for item in info["requested_formats"]], ["new", "audio"])
        self.assertEqual(refresh.count, 1)

    def test_no_equivalent_quality_fails_without_a_lower_quality_fallback(self):
        module = self.service()
        with self.assertRaises(module.FormatUnavailable):
            self.select([{**OLD, "format_id": "lower", "height": 480}])

    def test_missing_separate_audio_cannot_fall_back_to_a_silent_video(self):
        module = self.service()
        with self.assertRaises(module.FormatUnavailable):
            self.select([OLD], expression="old+bestaudio/best")

    def test_one_owned_extraction_cannot_repair_the_selection_twice(self):
        module = self.service()
        store = module.ProbeWitnesses(lambda: 1000)
        identity = store.create("https://fixture.invalid/watch", None, [OLD], expires_at=1300)
        intent = store.resolve(identity, "https://fixture.invalid/watch", None, "old")
        import yt_dlp
        with yt_dlp.YoutubeDL({"quiet": True, "simulate": True, "cachedir": False, "format": "old"}) as ydl:
            refresh = module.install_format_refresh(ydl, intent)
            selector = ydl.build_format_selector("old")
            self.assertEqual(ydl._select_formats([{**OLD, "format_id": "new"}], selector)[0]["format_id"], "new")
            with self.assertRaises(module.FormatUnavailable):
                ydl._select_formats([{**OLD, "format_id": "third"}], selector)
            self.assertEqual(refresh.count, 1)

    def test_metadata_witness_expires_is_bounded_and_never_returns_media_credentials(self):
        module = self.service()
        now = [1000]
        store = module.ProbeWitnesses(lambda: now[0])
        identity = store.create("https://fixture.invalid/watch?sig=literal", "context-one",
                                [{**OLD, "url": "https://cdn.fixture.invalid/file?secret=ignored"}], expires_at=1300)
        intent = store.resolve(identity, "https://fixture.invalid/watch?sig=literal", "context-one", "old")
        self.assertNotIn("ignored", str(intent))
        self.assertNotIn("context-one", str(intent))
        from backend.services.control_errors import ControlError
        with self.assertRaises(ControlError) as mismatch:
            store.resolve(identity, "https://fixture.invalid/watch?sig=changed", "context-one", "old")
        self.assertEqual(mismatch.exception.code, "PROBE_CONTEXT_SCOPE")
        now[0] = 1301
        with self.assertRaises(ControlError) as expired:
            store.resolve(identity, "https://fixture.invalid/watch?sig=literal", "context-one", "old")
        self.assertEqual(expired.exception.code, "PROBE_EXPIRED")
        for index in range(130):
            store.create("https://fixture.invalid/" + str(index), None, [OLD], expires_at=1600)
        self.assertLessEqual(len(store.records), 128)

    def test_unknown_quality_does_not_authorize_an_unwitnessed_best_fallback(self):
        module = self.service()
        from backend.services.control_errors import ControlError
        store = module.ProbeWitnesses(lambda: 1000)
        unknown = {"format_id": "unknown", "ext": "mp4", "resolution": "unavailable", "vcodec": "avc1", "acodec": "none"}
        identity = store.create("https://fixture.invalid/watch", None, [unknown], expires_at=1300)
        with self.assertRaises(ControlError) as rejected:
            store.resolve(identity, "https://fixture.invalid/watch", None, "unknown+bestaudio/best")
        self.assertEqual(rejected.exception.code, "FORMAT_UNAVAILABLE")
