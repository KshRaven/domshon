# TODO

Pending / deferred work. Nothing here is required for the current feature set.

## Planned

- [ ] **Android app** (deferred by request). The server API in `server/app.py` is already everything it
      needs: bearer device tokens, `/api/manifest`, `/api/files`, `/api/download`, WebSocket `/ws`.
      Suggested route: Kotlin client reusing the three-way sync algorithm of `client/sync.py`, with a
      foreground service for background sync.
- [ ] **User themes**: `theme.py` is the single source of design tokens for web and desktop. Add a theme
      folder (`themes/*.json`) loaded at start-up plus a picker in Settings.
- [ ] **Multiple users / per-user folders and per-user quotas**: today there is one admin account plus
      device tokens (`quota_mb` is global).
- [ ] **Per-device permissions** on tokens (read-only tokens, folder-restricted tokens); today
      `user_writes` is all-or-nothing for non-admin tokens.
- [ ] **Delta transfer** (block hashing) for large files; today whole files are transferred.
- [ ] **Trash / versions** on the server for deleted or overwritten files.
- [ ] **File-system watcher** for instant local change detection (currently interval polling).
- [ ] **Code signing** (Windows) and **signing + notarisation** (macOS); releases are unsigned.
- [ ] **macOS Intel build** and a boot-time LaunchDaemon (today: Apple Silicon `.app`, per-login LaunchAgent).
- [ ] **More tests**: `client/sync.py` conflict handling and the GUI (`tests/test_core.py` covers quotas,
      extension rules, path safety and auth on the server API).
- [ ] **LICENSE**: choose one before publishing the repository (a decision for the owner).

## Known gaps / not yet verified

- [ ] **Never run end to end.** Everything was written without execution; expect small fixes on first run,
      especially: `pypimgr.ps1`, `scripts/build.py` PyInstaller flags and icon generation, the release
      workflow (macOS and Linux ARM jobs are marked best effort), the Docker nginx profiles, and the
      Windows service on a clean machine. Run `pytest` first.
