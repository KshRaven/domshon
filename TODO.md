# TODO

Pending / deferred work. Nothing here is required for the current feature set.

## Planned

- [ ] **Native Android sync client** (Kotlin, foreground service reusing the three-way algorithm of
      `client/sync.py`). The shipped Android app is a WebView shell around the web app (files, editor,
      admin panel and device monitor all work); only background folder sync is missing.
- [ ] **Theme picker** UI. Done: `theme.json` in the config folder (or `DS_THEME_FILE`) overrides colour tokens
      for web, desktop and terminal app. Missing: a picker in Settings and bundled `themes/*.json`.
- [ ] **Multiple users / per-user folders and per-user quotas**: today there is one admin account plus
      device tokens (`quota_mb` is global).
- [ ] **Folder-restricted tokens**. Done: read-only tokens (`--read-only`, web checkbox). Missing: tokens limited
      to one folder.
- [ ] **Delta transfer** (block hashing) for large files; today whole files are transferred.
- [ ] **Trash / versions** on the server for deleted or overwritten files (needs the manifest and sync to ignore
      the trash folder).
- [ ] **File-system watcher** for instant local change detection (currently interval polling; needs `watchdog`).
- [ ] **Code signing** (Windows) and **signing + notarisation** (macOS); releases are unsigned.
- [ ] **macOS Intel build** and a boot-time LaunchDaemon (today: Apple Silicon `.app`, per-login LaunchAgent).
- [ ] **More tests**: `client/sync.py` conflict handling and the GUI. Covered now: quotas, extension rules, path
      safety, server auth, device monitor, session presence, read-only tokens, terminal app helpers/frame width.
- [ ] **Terminal app extras**: mouse support, a live sync-profile editor, per-token read-only toggle in the menu.
- [ ] **LICENSE**: choose one before publishing the repository (a decision for the owner).

## Done recently

- Device monitoring (connected devices + last connected device) on the server, web admin panel (also inside the
  Android app), desktop app, terminal app and CLI (`domshon server devices`, `domshon admin devices`).
- Interactive terminal app (`domshon` / `ds` / `domshon tui`) with paged menus, wrap-around scrolling, Esc to stop
  processes; plain commands unchanged (`domshon --help`, `domshon help CMD`).
- Esc also stops `server serve`, `daemon` and `sync watch` in plain CLI mode.
- Console output never crashes on legacy Windows code pages.

## Known gaps / not yet verified

- [ ] **Never run end to end.** Everything was written without execution; expect small fixes on first run,
      especially: `pypimgr.ps1`, `scripts/build.py` PyInstaller flags and icon generation, the release
      workflow (macOS and Linux ARM jobs are marked best effort), the Docker nginx profiles, and the
      Windows service on a clean machine. Run `pytest` first.
