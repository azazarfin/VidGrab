"""Sign and verify VidGrab v1 update manifests and bundle archives (pure Python RFC 8032 Ed25519)."""

import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import secrets
import sys
import tarfile

ROOT = Path(__file__).resolve().parents[1]
REQUIRED_NOTICES = ("FONTS_LICENSE.txt", "GPLv3.ffmpeg.txt", "LICENSE", "THIRD_PARTY.md")

# Curve25519 / Ed25519 constants per RFC 8032
_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_I = pow(2, (_P - 1) // 4, _P)
_BX = 15112221349535400772501151409588531511454012693041857206046113283949847762202
_BY = (4 * pow(5, _P - 2, _P)) % _P


def _ed_add(p, q):
    x1, y1, z1, t1 = p
    x2, y2, z2, t2 = q
    a = ((y1 - x1) * (y2 - x2)) % _P
    b = ((y1 + x1) * (y2 + x2)) % _P
    c = (2 * t1 * t2 * _D) % _P
    d = (2 * z1 * z2) % _P
    e = (b - a) % _P
    f = (d - c) % _P
    g = (d + c) % _P
    h = (b + a) % _P
    return ((e * f) % _P, (g * h) % _P, (f * g) % _P, (e * h) % _P)


def _ed_scalarmult(point, scalar):
    r = (0, 1, 1, 0)
    cur = point
    while scalar > 0:
        if scalar & 1:
            r = _ed_add(r, cur)
        cur = _ed_add(cur, cur)
        scalar >>= 1
    return r


def _ed_encode(point):
    x, y, z, _ = point
    zi = pow(z, _P - 2, _P)
    rx = (x * zi) % _P
    ry = (y * zi) % _P
    bits = ry | ((rx & 1) << 255)
    return bits.to_bytes(32, "little")


def _ed_decode(enc: bytes):
    if len(enc) != 32:
        raise ValueError("Encoded Ed25519 point must be 32 bytes")
    raw = int.from_bytes(enc, "little")
    y = raw & ((1 << 255) - 1)
    if y >= _P:
        raise ValueError("Non-canonical y coordinate")
    sign = (raw >> 255) & 1
    y2 = (y * y) % _P
    u = (y2 - 1) % _P
    v = (_D * y2 + 1) % _P
    x2 = (u * pow(v, _P - 2, _P)) % _P
    if x2 == 0:
        if sign:
            raise ValueError("Invalid sign bit for x=0")
        return (0, y, 1, 0)
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P != 0:
        x = (x * _I) % _P
    if (x * x - x2) % _P != 0:
        raise ValueError("Invalid Ed25519 point")
    if (x & 1) != sign:
        x = (_P - x) % _P
    return (x, y, 1, (x * y) % _P)


def _clamp(seed_hash_lo: bytes) -> int:
    b = bytearray(seed_hash_lo)
    b[0] &= 248
    b[31] &= 127
    b[31] |= 64
    return int.from_bytes(b, "little")


def _base_point():
    return (_BX, _BY, 1, (_BX * _BY) % _P)


def generate_keypair() -> tuple[str, str]:
    seed = secrets.token_bytes(32)
    return seed.hex(), public_key_from_seed(seed).hex()


def public_key_from_seed(seed: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 private seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    return _ed_encode(_ed_scalarmult(_base_point(), a))


def sign_ed25519(seed: bytes, message: bytes) -> bytes:
    if len(seed) != 32:
        raise ValueError("Ed25519 private seed must be 32 bytes")
    h = hashlib.sha512(seed).digest()
    a = _clamp(h[:32])
    prefix = h[32:]
    pk = _ed_encode(_ed_scalarmult(_base_point(), a))
    r = int.from_bytes(hashlib.sha512(prefix + message).digest(), "little") % _L
    r_bytes = _ed_encode(_ed_scalarmult(_base_point(), r))
    k = int.from_bytes(hashlib.sha512(r_bytes + pk + message).digest(), "little") % _L
    s = (r + k * a) % _L
    return r_bytes + s.to_bytes(32, "little")


def verify_ed25519(pk: bytes, message: bytes, signature: bytes) -> bool:
    if len(pk) != 32 or len(signature) != 64:
        return False
    r_bytes = signature[:32]
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    try:
        a_pt = _ed_decode(pk)
        r_pt = _ed_decode(r_bytes)
    except ValueError:
        return False
    k = int.from_bytes(hashlib.sha512(r_bytes + pk + message).digest(), "little") % _L
    lhs = _ed_scalarmult(_base_point(), s)
    rhs = _ed_add(r_pt, _ed_scalarmult(a_pt, k))
    return _ed_encode(lhs) == _ed_encode(rhs)


def compute_notices_digest(notices_dir: Path) -> str:
    lines = []
    for name in REQUIRED_NOTICES:
        p = notices_dir / name
        if p.is_symlink() or not p.is_file():
            raise ValueError(f"Missing or symlinked required notice file: {p}")
        digest = hashlib.sha256(p.read_bytes()).hexdigest()
        lines.append(f"{digest}  {name}\n")
    return hashlib.sha256("".join(lines).encode("utf-8")).hexdigest()


def _collect_engine_files(engine_dir: Path) -> tuple[Path, list[Path], str]:
    engine_dir = Path(engine_dir).resolve(strict=True)
    notices_dir = engine_dir / "_internal" / "notices"
    if not notices_dir.is_dir():
        notices_dir = engine_dir / "notices"
    notices_digest = compute_notices_digest(notices_dir)
    raw_files = sorted(
        (p for p in engine_dir.rglob("*") if not p.is_dir()),
        key=lambda p: p.relative_to(engine_dir).as_posix(),
    )
    for path in raw_files:
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"Refused symlink or non-regular file in engine bundle: {path}")
    return engine_dir, raw_files, notices_digest


def build_engine_tar_bundle(engine_dir: Path, out_archive: Path, *, compress: bool = False) -> tuple[str, int, str]:
    engine_dir, raw_files, notices_digest = _collect_engine_files(engine_dir)

    if out_archive.exists() or out_archive.is_symlink():
        raise ValueError("Output archive must be a fresh file path")
    out_archive.parent.mkdir(parents=True, exist_ok=True)

    mode = "w:gz" if compress else "w"
    fixed_mtime = 1795132800
    with tarfile.open(out_archive, mode) as tf:
        for path in raw_files:
            rel = Path("engine") / path.relative_to(engine_dir)
            info = tarfile.TarInfo(name=rel.as_posix())
            data = path.read_bytes()
            info.size = len(data)
            info.mtime = fixed_mtime
            info.uid = 0
            info.gid = 0
            info.uname = ""
            info.gname = ""
            executable = (path.stat().st_mode & 0o111) != 0 or path.name in ("vidgrab-engine", "ffmpeg", "ffprobe", "deno")
            info.mode = 0o755 if executable else 0o644
            tf.addfile(info, io.BytesIO(data))

    archive_bytes = out_archive.read_bytes()
    return hashlib.sha256(archive_bytes).hexdigest(), len(archive_bytes), notices_digest


def build_engine_zip_bundle(engine_dir: Path, out_archive: Path) -> tuple[str, int, str]:
    import zipfile

    engine_dir, raw_files, notices_digest = _collect_engine_files(engine_dir)

    if out_archive.exists() or out_archive.is_symlink():
        raise ValueError("Output archive must be a fresh file path")
    out_archive.parent.mkdir(parents=True, exist_ok=True)

    fixed_date_time = (2026, 11, 20, 0, 0, 0)
    with zipfile.ZipFile(out_archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for path in raw_files:
            rel = Path("engine") / path.relative_to(engine_dir)
            info = zipfile.ZipInfo(rel.as_posix(), date_time=fixed_date_time)
            executable = (path.stat().st_mode & 0o111) != 0 or path.name in (
                "vidgrab-engine", "vidgrab-engine.exe", "ffmpeg", "ffmpeg.exe",
                "ffprobe", "ffprobe.exe", "deno", "deno.exe",
            )
            info.external_attr = (0o100755 if executable else 0o100644) << 16
            zf.writestr(info, path.read_bytes())

    archive_bytes = out_archive.read_bytes()
    return hashlib.sha256(archive_bytes).hexdigest(), len(archive_bytes), notices_digest


def canonical_signing_bytes(manifest_without_sig: dict) -> bytes:
    cleaned = {k: v for k, v in manifest_without_sig.items() if k != "signature"}
    return json.dumps(cleaned, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def create_signed_manifest(
    *,
    version: str,
    channel: str,
    platform: str,
    url: str,
    archive_path: Path,
    notices_sha256: str,
    private_seed_hex: str,
    protocol_min: int = 1,
    protocol_max: int = 1,
    archive_format: str | None = None,
    notes: str | None = None,
) -> dict:
    raw = Path(archive_path).read_bytes()
    fmt = archive_format
    if fmt is None:
        if str(archive_path).endswith(".tar.gz") or str(archive_path).endswith(".tgz"):
            fmt = "tar.gz"
        elif str(archive_path).endswith(".zip"):
            fmt = "zip"
        else:
            fmt = "tar"
    payload = {
        "update_manifest_version": 1,
        "version": version,
        "channel": channel,
        "protocol_min": protocol_min,
        "protocol_max": protocol_max,
        "platform": platform,
        "url": url,
        "size": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "third_party_notices_sha256": notices_sha256,
        "format": fmt,
    }
    if notes is not None:
        payload["notes"] = notes
    seed = bytes.fromhex(private_seed_hex.strip())
    sig = sign_ed25519(seed, canonical_signing_bytes(payload))
    payload["signature"] = sig.hex()
    return payload


def verify_manifest_dict(manifest: dict, public_key_hex: str) -> bool:
    pk = bytes.fromhex(public_key_hex.strip())
    sig = bytes.fromhex(manifest["signature"])
    return verify_ed25519(pk, canonical_signing_bytes(manifest), sig)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("keygen", help="Print a fresh Ed25519 keypair JSON object (keep private key secret!)")

    p_pack = sub.add_parser("bundle", help="Pack a staged engine tree into a deterministic .tar/.tar.gz/.zip update bundle")
    p_pack.add_argument("--engine-dir", type=Path, required=True)
    p_pack.add_argument("--out", type=Path, required=True)
    p_pack.add_argument("--compress", action="store_true")
    p_pack.add_argument("--format", choices=("tar", "tar.gz", "zip"))

    p_sign = sub.add_parser("sign", help="Create and sign update_manifest_version=1 JSON")
    p_sign.add_argument("--version", required=True)
    p_sign.add_argument("--channel", default="stable")
    p_sign.add_argument("--platform", choices=("linux-x86_64", "windows-x64"), required=True)
    p_sign.add_argument("--url", required=True)
    p_sign.add_argument("--archive", type=Path, required=True)
    p_sign.add_argument("--notices-sha256", required=True)
    p_sign.add_argument("--protocol-min", type=int, default=1)
    p_sign.add_argument("--protocol-max", type=int, default=1)
    p_sign.add_argument("--notes")
    p_sign.add_argument("--key-env", default="VIDGRAB_UPDATE_SIGNING_PRIVATE_KEY")
    p_sign.add_argument("--out", type=Path, required=True)

    p_ver = sub.add_parser("verify", help="Verify an update manifest signature")
    p_ver.add_argument("--manifest", type=Path, required=True)
    p_ver.add_argument("--public-key-hex", required=True)

    args = parser.parse_args()
    if args.cmd == "keygen":
        priv, pub = generate_keypair()
        print(json.dumps({"private_key_hex": priv, "public_key_hex": pub}, indent=2))
    elif args.cmd == "bundle":
        fmt = args.format
        if fmt is None:
            if args.compress or str(args.out).endswith((".tar.gz", ".tgz")):
                fmt = "tar.gz"
            elif str(args.out).endswith(".zip"):
                fmt = "zip"
            else:
                fmt = "tar"
        if fmt == "zip":
            sha, size, notices = build_engine_zip_bundle(args.engine_dir, args.out)
        else:
            sha, size, notices = build_engine_tar_bundle(args.engine_dir, args.out, compress=(fmt == "tar.gz"))
        print(json.dumps({"archive": str(args.out), "format": fmt, "sha256": sha, "size": size, "third_party_notices_sha256": notices}, indent=2))
    elif args.cmd == "sign":
        priv = os.environ.get(args.key_env, "").strip()
        if not priv:
            parser.error(f"Signing private key env var {args.key_env} is empty")
        manifest = create_signed_manifest(
            version=args.version,
            channel=args.channel,
            platform=args.platform,
            url=args.url,
            archive_path=args.archive,
            notices_sha256=args.notices_sha256,
            private_seed_hex=priv,
            protocol_min=args.protocol_min,
            protocol_max=args.protocol_max,
            notes=args.notes,
        )
        args.out.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"manifest": str(args.out), "version": args.version, "sha256": manifest["sha256"]}, indent=2))
    elif args.cmd == "verify":
        manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
        ok = verify_manifest_dict(manifest, args.public_key_hex)
        if not ok:
            sys.exit("Signature verification failed")
        print("OK")


if __name__ == "__main__":
    main()
