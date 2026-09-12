# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and versions follow [Semantic Versioning](https://semver.org/spec/v2.0.0.html).
While the major version is 0, a minor bump may contain breaking changes.

## [Unreleased]

### Fixed

- The install instructions failed on a stock Debian or Ubuntu. Those
  distributions ship `python3` without `ensurepip`, so the documented
  `python3 -m venv .venv` aborts on an otherwise complete Python
  installation. The requirements now name the `venv` module explicitly
  and give the `apt install python3-venv` line that precedes it.
- The pre-commit hook and its installer told everyone to run
  `brew install gitleaks`, which is macOS-only advice in a fail-closed
  gate. Both now name the Debian and Ubuntu package alongside the
  Homebrew one, so a Linux contributor is not left with a blocked commit
  and an install command that does not exist on their machine.

### Changed

- Linux is tested rather than assumed. The roadmap said Linux "probably
  already works"; it now says so on the strength of a run rather than a
  reading of the source, and the section narrows to Windows, which
  remains untested.

### Notes

- No source changes were needed. The claim that the Python side carries
  no host platform assumptions held: every defect found on Ubuntu was in
  the documentation or the tooling around the code, not in the code.
  Verified on Ubuntu 24.04, Python 3.12.3, against paramiko 5.0.0 and
  Pillow 12.3.0 — both major versions ahead of the floors in
  `pyproject.toml`, and neither needed a change.
- The tablet's USB ethernet interface comes up on Linux with no
  configuration. The kernel binds it, the tablet's DHCP server addresses
  it, and `10.11.99.1` answers. This was the item the roadmap called out
  as least certain, and it needed nothing.
- Verified against a live Paper Pro on build `20260827113527`: key
  installation, the inspector's eleven checks, `status`, and a full write
  to `poweroff.png` including the read-write remount, the atomic rename,
  the hash verification and the revert to read-only. Backups captured on
  macOS were read back correctly on Linux, manifest hashes and all, so
  the on-device backup set travels between host platforms.

## [0.6.0] - 2026-09-09

### Added

- An application icon in the browser tab. Served from the package at
  `/favicon.png`, so the page still makes no request off the machine.
- `rephemeral ui --reload` restarts the server on source changes, and
  `rephemeral ui` now prints its version at startup. Reload is off by
  default: a restart triggered mid-write would drop the SSH session during
  a remount, leaving the tablet's root filesystem writable. The reloader
  ignores the static directory, whose files are re-read per request anyway.

### Notes

- The version line exists because routes are imported once at startup while
  the page HTML is read from disk per request. A server older than a route
  serves new markup against endpoints that do not exist, which presents as
  a broken feature rather than a stale process.

## [0.5.0] - 2026-09-09

A review pass, independently verified here against a live tablet. Two of
the defects it found were long-standing and mine.

### Added

- A test suite: 29 tests covering backup integrity, transfer safeguards,
  SSH cleanup, image conversion, config handling and web requests. They
  use simulated devices and never write to a connected tablet.
- A 32 MB cap on uploads, so an oversized file cannot be read into memory
  whole before anything checks it.

### Security

- The web UI now rejects cross-origin requests and unexpected Host
  headers. The old reasoning, that anything able to reach localhost
  already has a shell on the machine, overlooked the browser: a page you
  visit could issue requests to `127.0.0.1:8765` and the browser would
  send them. Covers `127.0.0.1` and `localhost`; local processes still
  have unauthenticated access, as before.
- `write_home_bytes` normalizes its path before checking the `/home/`
  prefix. Without that, `/home/../usr/share/remarkable/...` satisfied the
  check and escaped the partition the guard exists to confine writes to.
- New private keys are created with mode 600 rather than written and then
  chmodded, closing the window where the key existed readable.
- Backup manifests are validated on load: schema, firmware build, screen
  key format, hash format, and that each record's filename matches its
  key. A tampered manifest could otherwise direct reads and writes
  elsewhere. The firmware build string is validated too, since it forms
  part of both host and device backup paths.
- `remove_key` matches the key body as a field rather than by substring,
  and writes its temporary file with restricted permissions.
- The Host header check parses IPv6 literals. Starlette's
  TrustedHostMiddleware strips a port by splitting on the first colon,
  which turns `[::1]:8765` into `[` and matches nothing, so browsing to
  `http://[::1]:8765` returned 400 while the allowed-host list implied it
  was supported. The check reads bracketed literals to their closing
  bracket and rejects anything trailing it, so `[::1].evil.example` does
  not read as `[::1]`.

### Changed

- Backup manifests load from the tablet first and fall back to the host,
  rather than the reverse. The tablet is the shared copy, so another
  computer's updates are no longer masked by a stale local one.
- Applying verifies that recoverable stock exists before overwriting a
  screen, and records the replacement's hash before the write rather than
  after, so an interrupted write cannot leave the tool unable to
  recognize its own output.
- `capture_all` saves after each screen instead of once at the end, so an
  interruption keeps the captures already made.
- Grayscale output is now a single-channel PNG rather than gray values in
  three channels. Smaller, and the stock `remotewipe.png` is single
  channel too, so the device handles it.
- Web requests are serialized against the tablet and run off the event
  loop. Concurrent requests could previously interleave, and one
  finishing could remount the rootfs read-only while another was still
  writing to it.
- Batch CLI operations exit nonzero when any screen fails, instead of
  always reporting success.

### Fixed

- The inspector's USB subnet check compared only the first two octets:
  `"10.11.99".rsplit(".", 1)[0]` is `"10.11"`. It now compares properly
  as a network.
- Read-only remount verification parses `/proc/mounts` instead of
  grepping `mount` output, and a failed revert no longer clears the
  writable-depth counter, so cleanup retries it.
- The free-space check credited the existing file's size against the
  write. The temporary upload and the old file coexist until the rename,
  so the check could pass when the space was not there.
- SSH connections set explicit banner, auth and channel timeouts, close
  partially-opened sessions on failure, and close each command's channel
  rather than leaking one per command.
- Config values are escaped when written, so a path containing a quote or
  backslash no longer produces a broken file.
- Transparency is composited for non-palette images too, not only
  palette ones.
- 14px between the file chooser button and the filename beside it.
- README corrections: it still claimed the preview step showed the result
  before writing, which 0.3.0 removed; the clone URL was a placeholder;
  and it referenced a `[ui]` install extra that does not exist.

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

[Unreleased]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.6.0...HEAD
[0.6.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.5.0...v0.6.0
[0.5.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.4.0...v0.5.0
[0.4.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.3.1...v0.4.0
[0.3.1]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.3.0...v0.3.1
[0.3.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.2.0...v0.3.0
[0.2.0]: https://github.com/Bestalexmartin/rePhemeral/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/Bestalexmartin/rePhemeral/releases/tag/v0.1.0
