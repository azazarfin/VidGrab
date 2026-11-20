# Security Policy

## Supported Versions

| Version | Platforms | Status |
| --- | --- | --- |
| `0.1.x` | Linux (`x86_64` `.rpm`, `.deb`, `AppImage`), Windows (`x64` NSIS) | Supported |
| Legacy standalone `uvicorn` backend (`installer/`) | Linux, Windows | Deprecated — migrate via [docs/MIGRATION.md](docs/MIGRATION.md) |

---

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security vulnerabilities.

Instead, report vulnerabilities privately through [**GitHub Security Advisories**](https://github.com/azazarfin/VidGrab/security/advisories/new) on `azazarfin/VidGrab`.

Include as much of the following as possible:

- Affected component (`desktop/supervisor`, `native-host`, `backend`, `extension`, or packaging/installer)
- VidGrab version, OS, and browser version
- Steps to reproduce or minimal proof of concept
- Impact assessment and any suggested mitigation

Please scrub personal cookies, authorization headers, and local tokens before sharing logs (VidGrab's **Diagnostics → Copy redacted diagnostics** button automatically redacts sensitive fields).

---

## Security Architecture Summary

- **Local-only IPC:** Browser extensions communicate with `vidgrab-native-host` over OS-verified Native Messaging (`com.vidgrab.companion`) with explicit extension ID allowlists; paired open loopback ports are disabled.
- **Scoped Volatile Credentials:** Request observation (`webRequest` / `webNavigation`) is strictly optional and user-granted. Cookies are written only to permission-restricted temporary jars tied to a single download attempt and removed after completion or failure.
- **Signed Updates & Rollback:** Engine update manifests are verified in pure Rust using RFC 8032 **Ed25519** signatures against the pinned release public key (`6ee71cdd58c6f7bd787f9383eb0db61ed3157e0290ed1ed30370ab41b121561e`), alongside SHA-256 archive and third-party notice digests.
- **Path & Filesystem Safety:** Output paths, backups, legacy config imports, diagnostics exports, and update archive extractions enforce traversal, symlink (`O_NOFOLLOW`), and Windows reparse-point (`FILE_FLAG_OPEN_REPARSE_POINT`) guards.
- **No Arbitrary CLI Injection:** Arbitrary `custom_ytdlp_args` and runtime `pip` self-updates are disabled in the packaged companion engine.
