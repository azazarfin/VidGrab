## Summary

<!-- Briefly describe what changed and why. -->

## Related Issues

<!-- e.g. Closes #123 -->

## Verification Checklist

- [ ] `.venv/bin/python -m packages.protocol.generate --check` passes (if protocol files were touched)
- [ ] `.venv/bin/python -B -m unittest discover -s tests -p "test_*.py"` passes
- [ ] `cargo test --locked -p vidgrab-supervisor -p vidgrab-native-host` passes
- [ ] `node --test tests/check_*.mjs` passes
- [ ] `npm --prefix desktop run build` passes
- [ ] Documentation updated where behavior, CLI flags, or setup changed

## Screenshots / Logs (if applicable)

<!-- Attach screenshots for UI changes or redacted diagnostics output. -->
