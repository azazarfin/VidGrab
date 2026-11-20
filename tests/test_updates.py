import hashlib
import http.server
import importlib.util
import io
import json
import os
from pathlib import Path
import tarfile
import tempfile
import threading
import unittest

from tests.support import OfflineTestCase

ROOT = Path(__file__).resolve().parents[1]


def load_release_manifest():
    path = ROOT / "packaging" / "release_manifest.py"
    spec = importlib.util.spec_from_file_location("vidgrab_release_manifest", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def make_fixture_engine(root: Path, version_tag: str, *, bad_self_check: bool = False) -> Path:
    engine_dir = root / f"engine-{version_tag}"
    notices = engine_dir / "_internal" / "notices"
    notices.mkdir(parents=True)
    for name in ("LICENSE", "THIRD_PARTY.md", "GPLv3.ffmpeg.txt", "FONTS_LICENSE.txt"):
        (notices / name).write_text(f"{name}-{version_tag}\n", encoding="utf-8")
    exe = engine_dir / ("vidgrab-engine.exe" if os.name == "nt" else "vidgrab-engine")
    if bad_self_check:
        exe.write_text("#!/bin/sh\necho bad-self-check >&2\nexit 1\n", encoding="utf-8")
    else:
        exe.write_text(
            "#!/bin/sh\nif [ \"$1\" = \"--self-check\" ]; then\n"
            "  echo '{\"spike_version\":1,\"components\":{\"ffmpeg\":\"fixture\",\"yt_dlp\":\"2026.11.20\"}}'\n"
            "  exit 0\nfi\nexit 0\n",
            encoding="utf-8",
        )
    if os.name != "nt":
        exe.chmod(0o755)
    return engine_dir


class UpdateManifestAndEngineHelperTests(OfflineTestCase):
    def test_python_ed25519_matches_rfc8032_vectors_and_roundtrips(self):
        rm = load_release_manifest()
        # RFC 8032 Section 7.1 TEST 1 seed -> public key & signature
        seed1 = bytes.fromhex("9d61b19deffd5a60ba844af492ec2cc44449c5697b326919703bac031cae7f60")
        expected_pk1 = bytes.fromhex("d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a")
        expected_sig1 = bytes.fromhex(
            "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e06522490155"
            "5fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"
        )
        self.assertEqual(rm.public_key_from_seed(seed1), expected_pk1)
        self.assertEqual(rm.sign_ed25519(seed1, b""), expected_sig1)
        self.assertTrue(rm.verify_ed25519(expected_pk1, b"", expected_sig1))
        self.assertFalse(rm.verify_ed25519(expected_pk1, b"tampered", expected_sig1))

    def test_bundle_sign_and_tamper_detection(self):
        rm = load_release_manifest()
        priv, pub = rm.generate_keypair()
        engine_dir = make_fixture_engine(self.root, "0.2.0")
        bundle_tar = self.root / "vidgrab-engine-0.2.0.tar"
        sha, size, notices_sha = rm.build_engine_tar_bundle(engine_dir, bundle_tar)
        manifest = rm.create_signed_manifest(
            version="0.2.0",
            channel="stable",
            platform="windows-x64" if os.name == "nt" else "linux-x86_64",
            url="https://updates.example.invalid/vidgrab-engine-0.2.0.tar",
            archive_path=bundle_tar,
            notices_sha256=notices_sha,
            private_seed_hex=priv,
            notes="Security and runtime bump.",
        )
        self.assertTrue(rm.verify_manifest_dict(manifest, pub))
        tampered = dict(manifest, sha256="0" * 64)
        self.assertFalse(rm.verify_manifest_dict(tampered, pub))
        _, other_pub = rm.generate_keypair()
        self.assertFalse(rm.verify_manifest_dict(manifest, other_pub))

    def test_unpack_update_rejects_symlinks_and_traversal_and_extracts_clean_tar(self):
        from backend.engine.__main__ import _unpack_update

        bad_tar = self.root / "bad-sym.tar"
        with tarfile.open(bad_tar, "w") as tf:
            info = tarfile.TarInfo("engine/escape-link")
            info.type = tarfile.SYMTYPE
            info.linkname = "/etc/passwd"
            tf.addfile(info)
        with self.assertRaisesRegex(ValueError, "link"):
            _unpack_update(bad_tar, self.root / "out-sym", "tar")

        trav_tar = self.root / "bad-trav.tar"
        with tarfile.open(trav_tar, "w") as tf:
            data = b"pwn"
            info = tarfile.TarInfo("../../pwn.txt")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        with self.assertRaisesRegex(ValueError, "traversal|unsafe"):
            _unpack_update(trav_tar, self.root / "out-trav", "tar")

        good_tar = self.root / "good.tar.gz"
        rm = load_release_manifest()
        engine_dir = make_fixture_engine(self.root, "0.2.1")
        rm.build_engine_tar_bundle(engine_dir, good_tar, compress=True)
        out_good = self.root / "out-good"
        _unpack_update(good_tar, out_good, "tar.gz")
        self.assertTrue((out_good / "engine" / "_internal" / "notices" / "LICENSE").is_file())

        good_zip = self.root / "good.zip"
        sha_zip, size_zip, notices_zip = rm.build_engine_zip_bundle(engine_dir, good_zip)
        self.assertGreater(size_zip, 0)
        self.assertEqual(len(sha_zip), 64)
        self.assertEqual(len(notices_zip), 64)
        out_zip = self.root / "out-zip"
        _unpack_update(good_zip, out_zip, "zip")
        self.assertTrue((out_zip / "engine" / "_internal" / "notices" / "LICENSE").is_file())


if __name__ == "__main__":
    unittest.main()
