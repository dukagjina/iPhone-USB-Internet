# Installation help

[← Back to iPhone USB Internet](README.md)

## Before you install

| What you need | Details |
| --- | --- |
| Windows PC | Windows 10/11 x64 with [Microsoft Edge WebView2 Runtime](https://developer.microsoft.com/en-us/microsoft-edge/webview2/#download-section). |
| Apple device support | Install **Apple Devices** from the Microsoft Store. |
| iPhone | Install [iSH Shell](https://ish.app) and keep it visible while connected. |
| Cable | A USB data cable compatible with your iPhone and PC. |
| Permission | Connecting requires administrator approval from the same Windows account that owns the pairing. |

Version **1.2.0** is an experimental prerelease. The installer is **unsigned**,
so Windows or your browser may show an unknown-publisher or reputation warning;
managed PCs may block it. Download only from the
[official release](https://github.com/dukagjina/iPhone-USB-Internet/releases/tag/v1.2.0).
The release includes SHA-256 checksums for checking the downloaded files.

The public installer starts unpaired. It does not include anyone else's phone
credentials. Opening the app does not start a connection or change routing.

## Install on Windows

1. Download **iPhone-USB-Internet-Setup.exe** from the release above.
2. Install WebView2 and Apple Devices if they are missing.
3. Run the installer. It installs for your Windows account and offers a Desktop shortcut.
4. Open **iPhone USB Internet** from the Start menu or Desktop.

## First-time phone setup

### 1. Prepare and pair

1. Open **iSH Shell** on your iPhone. Type the following and press Return:

   ```sh
   apk add python3
   ```

2. Wait until installation finishes and `localhost:~#` appears again.
3. Connect exactly one iPhone to the PC with a USB data cable. Unlock it and tap **Trust** if asked.
4. In the Windows app, expand **Setup & maintenance** and choose **Pair connected iPhone**.

The app creates a random pairing key and stores it encrypted for your Windows
account. An existing pairing is preserved rather than silently replaced.

### 2. Send the phone relay

1. In **Setup & maintenance**, choose **Prepare receiver command**, then **Copy receiver command**.
2. Paste that command into iSH and press Return. It is specific to your pairing; do not share it.
3. Wait for **READY FOR PHONE UPDATE** on the iPhone.
4. In the Windows app, choose **Send update**. If the three-minute receiver timeout expires, run the receiver command again before sending.
5. When iSH says **Saved**, run:

   ```sh
   python3 /root/phone_network.py
   ```

6. Wait for **PHONE NETWORK READY**. Leave iSH visible and the phone unlocked.

The receiver verifies the expected payload checksum, installs the script
atomically and keeps the previous script as a backup. Sending the relay does
not connect the PC.

### 3. Connect

1. On the iPhone, turn **cellular on**, **Wi-Fi off** and **Personal Hotspot off**.
2. In the Windows app, choose **Check phone**.
3. Once the check passes, choose **Connect PC** and approve Windows permission yourself.

Connecting changes PC routing. Administrator approval must use the same
Windows account that created the pairing; elevation with a different account
is not supported.

## Connect next time

1. Plug in and unlock the same paired iPhone.
2. Open iSH and run `python3 /root/phone_network.py` if the relay is not already running.
3. Keep cellular on, Wi-Fi and Personal Hotspot off, and iSH visible with **PHONE NETWORK READY** showing.
4. Choose **Check phone**, then **Connect PC** in the Windows app.

Choose **Disconnect** in the app when finished. To update the phone relay,
disconnect first, stop the old relay in iSH with **Ctrl + C**, then repeat
[Send the phone relay](#2-send-the-phone-relay). The command used to start the
relay stays the same across updates.

## Troubleshooting

| What you see | What to check |
| --- | --- |
| Phone not found | Connect exactly one iPhone, unlock it, accept Trust if prompted, and confirm Apple Devices recognizes it. Check that the cable supports data. |
| Phone check fails | Keep iSH visible with **PHONE NETWORK READY**, confirm cellular works on the phone, and read the single error beside **Check phone**. |
| Receiver times out | Run the receiver command again and choose **Send update** while **READY FOR PHONE UPDATE** is showing. |
| Waiting or failed requests | Expand **Diagnostics**. A short queue can occur as tabs open. Failed and rejected requests are totals; check whether they keep rising while pages fail to load. |
| Connection drops | Keep the phone unlocked and iSH in the foreground. Check the cable and cellular signal before reconnecting manually. |

If the problem persists, [open an issue](https://github.com/dukagjina/iPhone-USB-Internet/issues/new).
Include the app version, Windows version, steps to reproduce and visible error.
Do not attach pairing files, receiver commands, your phone relay script or its backup.

## Connection behavior and limits

- iSH must remain visible and the phone unlocked. This is not a background iPhone service.
- This is not a VPN or kill switch. Other enabled connections can remain available as fallback. LAN traffic, interface-bound apps, specific routes and Windows multi-adapter DNS may use another connection.
- Some games and VPNs require separate compatibility testing. A successful phone health check does not prove that every app uses the phone connection.
- Phone loss detection and normal route cleanup are implemented. Forced crashes can skip cleanup; restart Windows if connectivity does not recover. Routes are temporary.
- The current build passed offline regression, UI and packaging checks. Fresh-account/device onboarding, live reconnect, route-restoration and uninstall qualification remain before a stable release.

**Educational Purposes Only.** Carrier billing and hotspot classification have
not been verified. Turning off Personal Hotspot does not establish how a
carrier classifies usage. The project does not promise unlimited data or
hotspot-cap avoidance. Follow your plan and applicable laws; see the
[educational-use notice](EDUCATIONAL%20USE.txt).

## Uninstall

Disconnect and close the app, then use **Windows Settings → Apps → Installed
apps → iPhone USB Internet → Uninstall**. The uninstaller refuses while the
app or connection is running.

Uninstall keeps local pairing and preferences for reinstallation. To remove
those too, disconnect and close all copies before deleting
`%LOCALAPPDATA%\iPhone USB Internet`. Remove the relay script and its backup
from iSH yourself if retiring the phone. Read the [privacy notice](PRIVACY.txt)
for details of local data and external connections.
