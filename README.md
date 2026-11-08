# QR File Transfer

Send files between your **phone** and your **PC** over your own Wi-Fi. No cable, no cloud, no app to install on the phone.

You open the desktop app on the PC, scan the QR code it shows with your phone camera, type the PIN, and the phone's browser becomes a file-transfer page.

## What it does

- **Phone → PC:** pick photos, videos or any files on the phone. A popup on the PC asks where to save them.
- **PC → phone:** choose files (or drag and drop them from Finder) and download them on the phone.
- **Named folders:** type a folder name before sending and the files go into that folder. On the PC the folder is created where you choose to save it. For PC → phone, the phone gets a `.zip` with that name.
- **Chat:** a small chat between phone and PC (copy-friendly, keeps line breaks) for sending links or text.
- **Pairing:** a QR code plus a random 4-digit PIN, generated every time the app starts.
- Everything stays on your local network. Nothing is uploaded to the internet.

## How it works

```
 Phone browser  <---- Wi-Fi (HTTP) ---->  Desktop app
 (web page)                               ├─ small web server (server.py)
                                          └─ app window (app.py + desktop.html)
```

The desktop app starts a small web server on your PC and shows its address as a QR code. The phone opens that address and gets the transfer page (`index.html`). Files go straight between the two devices.

## Requirements

- Python 3.9 or newer
- PC and phone on the **same Wi-Fi network**
- Tested on macOS with Python 3.14. Windows and Linux are untested.

## Run it

1. Download the project and open a terminal in its folder.
2. Install the dependencies (once):

   ```bash
   pip3 install -r requirements.txt
   ```

3. Start the app:

   ```bash
   python3 app.py
   ```

4. A window opens with a QR code and a 4-digit PIN.
5. Scan the QR code with your phone camera and open the link.
6. Type the PIN on the phone and tap **Connect**.

## Using it

| I want to… | Do this |
|---|---|
| Send files from phone to PC | On the phone, open **Send to PC**, tap the box, pick files. On the PC a popup appears; click **Save to…** and choose a location. |
| Send into a named folder | Type a name in **Folder name (optional)** before picking files. On the PC click **Save folder…** and choose where to create it. |
| Send files from PC to phone | In the PC window, click **Choose files to send** or drag files in from Finder. On the phone, open **From PC** and tap **Download**. |
| Pair a second phone | Click **＋ Pair phone** in the PC window to show the QR code and PIN again. |
| Chat | Tap the chat button (bottom-right) on either side. On the PC, Enter sends and Shift+Enter adds a new line. |

Files that are received but not saved wait in `~/FileDrop`. When you next start the app, leftovers are moved to `~/FileDrop/Earlier sessions`.

## Build a double-click app (macOS)

```bash
pyinstaller --noconfirm --windowed --name FileDrop \
  --add-data "index.html:." --add-data "desktop.html:." app.py
```

The app appears at `dist/FileDrop.app`. It is unsigned, so the first time you open it, right-click it, choose **Open**, then **Open** again. Rebuild after every code change.

## Project files

| File | Purpose |
|---|---|
| `app.py` | Desktop window, save dialogs, drag and drop |
| `server.py` | Web server: pairing, uploads, downloads, zip, chat |
| `desktop.html` | The page shown inside the desktop window |
| `index.html` | The page the phone opens |
| `requirements.txt` | Python packages |

## Troubleshooting

- **Phone can't open the link:** make sure both devices are on the same Wi-Fi, and allow incoming connections if your firewall asks. A VPN on the PC can make the QR code point to the wrong address.
- **"Session ended" on the phone:** scan the QR code again and use the new PIN.
- **App won't open (macOS):** right-click the app and choose **Open**.

## Known limitations

- Traffic is plain HTTP on your local network, so use it only on Wi-Fi you trust.
- The 4-digit PIN is a convenience, not strong protection. Do not rely on it on a shared or public network.
- Received files you delete with ✕ are removed permanently, without confirmation.
- Built and tested on macOS only.
