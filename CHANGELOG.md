# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is 0, a minor bump may contain breaking changes.

## [Unreleased]

## [0.4.0] - 2026-09-09

### Added

- `rephemeral inspector`, a read-only diagnosis of every layer between
  your machine and the tablet: Python version, dependencies, config, SSH
  key and its permissions, whether the USB network interface came up,
  whether anything is listening, whether SSH authenticates, and then the
  firmware build, free space, screen presence and backup coverage.
- A roadmap in the README covering other reMarkable models, Windows and
  Linux support, and a standalone application.

### Notes

- The inspector exists because this tool spans four layers that fail
  differently, and "could not connect to 10.11.99.1" does not say which
  one broke. On a machine other than the one it was developed on, that is
  the only question worth answering.
- Checks stop at the first failure and mark the rest skipped, rather than
  reporting a cascade of consequences as though they were separate faults.
- It detects the USB interface by asking the OS which local address would
  route to the tablet, rather than shelling out to `ifconfig` or `ip`,
  neither of which exists everywhere. Nothing is written to the tablet.

## [0.3.1] - 2026-09-09

### Changed

- Header readouts reordered to Root free, Firmware, Tablet. Tablet holds
  the widest-swinging value, from a dash to a board name to "Not
  connected", so it sits at the outside edge where that movement disturbs
  nothing beside it.
- A little air between the two sentences of the rail's standing note, so
  the deliberate break does not read as an accidental word wrap.

### Fixed

- The "no tablet detected" message wrapped to three lines. It lives inside
  the tile grid, so it was being laid out as a grid item and squeezed into
  one column; it now spans them.
- A malformed reply from `/api/ping` was indistinguishable from a
  disconnected tablet, and reported one forever. It now says so in the
  browser console. The likely cause is a server process older than the
  endpoint, since uvicorn loads routes once at import while the page HTML
  is re-read per request.

## [0.3.0] - 2026-09-09

### Added

- The web UI detects a tablet being connected or disconnected on its own,
  and loads its screens without you pressing Refresh.
- `GET /api/ping`, a cheap TCP reachability probe backing that watcher.

### Notes

- The watcher probes every 2 seconds while nothing is attached and every 8
  while one is, and only a change in reachability triggers a full status
  read. A status read opens an SSH session and hashes all eleven screens,
  which is far too much to repeat on a poll.
- Reachable is not the same as usable: before Developer Mode is enabled a
  Paper Pro accepts the TCP connection and then resets it. The probe only
  prompts the full read, which reports a real error in that case.
- Probing is skipped while the browser tab is hidden and resumes when it
  is shown again.

## [0.2.0] - 2026-09-09

### Changed

- **Breaking: the `--greyscale` flag is now `--grayscale`.** The project
  standardized on American English throughout, which also renamed the
  `greyscale` parameter on `prepare()` and `Applier.apply()`, the
  `Screen.greyscale` field, and the `greyscale` multipart form field on
  `POST /api/screen/{key}/apply`.
- Rebuilt the web UI as a two-column layout: a scrolling library of screen
  tiles on the left, a fixed inspector rail on the right. Selecting a
  screen no longer means scrolling to reach the controls.
- Restyled from a dark, saturated palette to a light neutral one. An image
  tool's shell should not compete with the images in it, so the thumbnails
  are now the only saturated thing on screen.
- Replaced status vocabulary with plain words: Stock, Custom, No backup,
  Changed, Missing.
- Tiles lift on hover with a neutral glow and stay lifted with a warm one
  when selected, replacing a border-only selection state that was easy to
  miss.
- The page no longer scrolls. Only the screen library does, so the header,
  the controls and the result readout stay in view.

### Added

- Thumbnails are cached per screen, and only the screen that changed is
  refetched after an apply or restore. Previously every apply reloaded all
  eleven images over SFTP.
- The result readout is always present and holds its place, so a result
  never shifts the layout.
- Prose keeps at least three words on its last line, with a fallback that
  gives up rather than risk overflowing a narrow column.

### Removed

- The preview step, and with it `POST /api/screen/{key}/preview`.
  Conversion notes are now reported after the write instead, and the
  tile itself refreshes to show the result.
- The Google Fonts dependency. The UI uses system faces, so it makes no
  network requests. This tool talks to a tablet on a USB cable, on a
  machine that may have no network at all.

### Fixed

- Transparency was discarded rather than composited when converting an
  image, so transparent pixels kept whatever color the exporter left
  underneath. A logo that is more than half transparent could land on
  black without any error. All fit modes now composite onto the chosen
  background.
- An unknown screen key returned 500 instead of 404.
- SVG input reported "cannot identify image file". It now explains that
  Pillow reads raster formats and to export a PNG.
- Faint label text failed WCAG AA at 3.67:1 against white and now passes.

## [0.1.0] - 2026-09-09

First working version.

### Added

- Replace any of eleven system screens on a reMarkable Paper Pro over USB,
  from a CLI or a localhost web UI.
- Stock artwork is captured before a screen is first written, write-once
  per firmware build, to both the tablet's `/home` partition and the host.
  The tool records the hash of everything it writes and refuses to capture
  its own output as stock, which is how a naive version of this loses the
  originals.
- Restore per screen or all at once, verified against the recorded stock
  hash before writing.
- Drift reporting distinguishes stock, an image the tool applied, and
  something it does not recognize, such as art replaced by a firmware
  update.
- Image conversion to the panel's 1620x2160 (or 1012x1012 for the sleep
  carousel), with cover, contain and stretch fits, and PNG encoding that
  escalates through palette quantization to stay under a size cap.
- A fail-closed gitleaks pre-commit hook, since this repository is public
  and the two things that must never reach it are the device password and
  an SSH private key.

### Security

- The device root password is used once to install an SSH keypair and is
  never written to disk. The tool holds no cloud credentials and makes no
  network requests beyond the USB link.
- Every write remounts the root filesystem read-write, writes through a
  temporary file and rename, syncs, remounts read-only in a `finally`, and
  verifies the revert took. A tablet left with a writable rootfs is one
  unclean shutdown away from needing a reflash.
- Writes are refused above a 4 MB per-image cap or below an 8 MB free
  floor on `/`, which holds roughly 41 MB on a stock device.
- Symlinks are never followed. `restart-crashed.png` ships as a link to
  `rebooting.png`, and writing through it would silently overwrite the
  wrong screen.
- The host is confirmed to be a reMarkable before anything is remounted.

### Notes

- Confirmed on firmware build `20260827113527` that a written screen takes
  effect immediately: `xochitl` reads these PNGs when it needs to draw
  them rather than caching them at startup. No restart step is needed.

[Unreleased]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.4.0...HEAD
[0.4.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Bestalexmartin/rePhemeral/releases/tag/v0.1.0
