# Developing and building

[← Back to iPhone USB Internet](../README.md)

## Project layout

| Path | Contents |
| --- | --- |
| `src/` | Windows application, UI, assets and phone relay templates. |
| `tests/` | Offline regressions and mocked UI checks. |
| `tools/` | Test, build, verification and source packaging commands. |
| `docs/` | Documentation and demonstration screenshots. |
| `licenses/` | Third-party source notices. |
| `vendor/` | Local pinned network binaries, ignored by Git. |
| `build/`, `dist/` | Generated work files and release output, ignored by Git. |

The project uses PolyForm Noncommercial 1.0.0. Read [LICENSE](../LICENSE),
[commercial-use terms](../COMMERCIAL_USE.txt) and
[trademark terms](../TRADEMARKS.txt) before redistributing a modified build.

## Run offline checks

Use **Python 3.12 on Windows**, create a virtual environment, and install
`requirements-build.txt`. From the repository root:

```powershell
python -m pip install -r requirements-build.txt
python -B tools/test.py
```

These tests use mocks and local fixtures. They do not connect a phone or
switch routing. The optional vendor parser test is skipped if its binary is absent.

UI checks require Node.js and Playwright:

```powershell
npm install
npx playwright install chromium
npm run test:ui
```

The UI tests mock the native bridge and block external requests. Screenshots
are written to `build/ui-tests/`. To use an existing compatible Chromium
browser, set `UI_TEST_BROWSER` to its executable path before running the test.

## Build the Windows installer

1. Place approved `tun2socks.exe` and x64 `wintun.dll` binaries in `vendor/`.
   Obtain them from [tun2socks](https://github.com/xjasonlyu/tun2socks) and
   [Wintun](https://www.wintun.net/). Their exact SHA-256 values are pinned in
   `src/backend.py`; the builder rejects other binaries.
2. Install **Inno Setup 6**.
3. Run:

   ```powershell
   python -B tools/build.py --installer
   python -B tools/verify.py
   python -B tools/package.py
   ```

Outputs include the app folder at `dist/iPhone USB Internet`, the installer
and companion checksum in `dist/installer/`, the allowlisted source ZIP and
`dist/SHA256SUMS.txt`. Building never installs the app or changes networking.

`tools/verify.py` compares compiled modules with the source and checks resources,
the complete file inventory, dependency hashes and ordinary launch privileges.
`tools/package.py` verifies the installer checksum and exports only the approved
source files. It excludes vendor binaries, build output and private data.

The app's `--package-check` option verifies bundled network dependency hashes
without pairing. `--self-test` additionally validates the current Windows
account's pairing. Neither starts the connection worker.

## Prepare a release

Follow the [release checklist](../RELEASE%20CHECKLIST.txt). Commit source files
and upload the reviewed installer, source ZIP and checksums as release assets.
Never distribute a personally paired app, receiver command, session folder or
installed phone relay containing a private key.

Keep the installer and source assets for each published version matched to its
release commit. Documentation on the main branch may be newer than a tagged
release; a documentation change alone does not require reinstalling the app.
