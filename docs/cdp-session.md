# Export a session through CDP

Use this fallback when the browser opened by `crawl session` is blocked or cannot complete login. You will start Chrome or Edge manually, log in, and connect `crawl session` to that browser through CDP (Chrome DevTools Protocol) to export its state.

The external browser must be started with remote debugging enabled. Follow the steps below before running the export command. Keep it open until the terminal prints `saved ...`.

This does not bypass platform restrictions. If the manually opened browser is also blocked, you must resolve access before exporting a usable login.

## What gets saved

There are two separate locations:

| Location | Contents | Used by |
|---|---|---|
| Browser profile directory passed to `--user-data-dir` | Cookies, login state, preferences and browser cache | Chrome or Edge |
| JSON file passed to `crawl session` | The selected platform's cookies and localStorage | `crawl run --session` |

`crawl session` does not look for the browser profile on disk. It connects to the running browser at `--cdp-url` and asks that browser for its current state.

The browser profile and exported JSON both persist after the browser closes. A successful export confirms that state was saved, not that the platform accepted the login. Log in and check the page yourself before exporting.

## Requirements

- Chrome or Edge installed on the machine where you will log in.
- The project installed with its `browser` extra in the environment running `crawl session`.
- A visible desktop for the browser window.
- A separate browser profile directory. Do not point this command at your normal Chrome profile. Recent Chrome versions require a non-default directory for remote debugging ([Chrome documentation](https://developer.chrome.com/blog/remote-debugging-port)).

This guide uses Reddit. For Instagram, open `https://www.instagram.com/`, use `--feed instagram`, and choose an Instagram session filename.

## Windows browser with WSL

You will use three windows:

| Window | Purpose |
|---|---|
| Windows PowerShell | Start Chrome with its debugging port enabled |
| Chrome | Log in to Reddit manually |
| WSL terminal | Connect to Chrome and save the session |

### 1. Start Chrome from Windows PowerShell

Open **Windows PowerShell**, not the WSL terminal, and run:

```powershell
& "$env:ProgramFiles\Google\Chrome\Application\chrome.exe" `
  --remote-debugging-port=9222 `
  "--user-data-dir=$env:LOCALAPPDATA\crawl-browser" `
  "https://www.reddit.com/"
```

If Chrome is installed elsewhere, replace the executable path with its actual location. To use Edge with its own profile instead:

```powershell
& "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe" `
  --remote-debugging-port=9222 `
  "--user-data-dir=$env:LOCALAPPDATA\crawl-edge" `
  "https://www.reddit.com/"
```

Run only one of these commands. Both examples use port `9222`.

Chrome stores this separate profile under:

```text
C:\Users\<Windows username>\AppData\Local\crawl-browser
```

It may initially look like a fresh browser because it does not use your normal profile. Reusing this same directory on later launches preserves its saved state. If an instance using this directory is already running without remote debugging, close that instance before restarting it with the command above.

### 2. Log in in the browser

In the browser window opened by the command:

1. Log in to Reddit.
2. Complete any verification requested by Reddit.
3. Confirm that you can access Reddit and that the page shows your account.
4. Leave this browser window open.

If Reddit shows a block or rate-limit page, do not assume a session export will resolve it. The tool cannot complete the login for you or remove an IP restriction.

### 3. Check connectivity from WSL

In your **WSL terminal**, run:

```bash
curl --max-time 5 http://127.0.0.1:9222/json/version
```

A working endpoint returns JSON containing fields such as `Browser` and `webSocketDebuggerUrl`. This confirms connectivity, not authentication.

If the connection fails, follow [WSL cannot connect](#wsl-cannot-connect) before continuing.

### 4. Run the export from WSL

In the WSL terminal:

```bash
cd ~/crawl-me-maybe
source .venv/bin/activate

crawl session ./reddit-session.json \
  --feed reddit \
  --cdp-url http://127.0.0.1:9222
```

If the file already exists and you want to replace it, add `--force`:

```bash
crawl session ./reddit-session.json \
  --feed reddit \
  --cdp-url http://127.0.0.1:9222 \
  --force
```

The tool reuses an existing Reddit tab in the browser's default context, or opens one if necessary. It does not import a separate incognito context.

### 5. Press Enter in the WSL terminal

When the terminal shows:

```text
Connected to your browser for reddit
Log in in that browser, then press Enter here to save the session.
The browser and its tabs will remain open.
```

Return to **that WSL terminal and press Enter once**. Do not press Enter in the browser address bar. Do not close Chrome to signal completion.

Wait until the terminal prints:

```text
saved reddit-session.json (... cookies from ...)
```

The export is now complete. You can close the browser window.

The file is saved relative to the terminal's working directory. With the commands above, it is `~/crawl-me-maybe/reddit-session.json` inside WSL, not in Windows `LocalAppData`.

### 6. Use the saved session

```bash
crawl run "Toronto events this weekend" \
  --platform reddit \
  --session ./reddit-session.json
```

You can also add `--session ./reddit-session.json` to a run using explicit seeds. The original Chrome window does not need to remain open once export has finished.

## Use several exported sessions

Export each platform into its own file, then pass the files together:

```bash
crawl run "<prompt>" --platform instagram,reddit \
  --session ./ig-session.json ./reddit-session.json
```

The crawler merges the state in memory for both search and page fetching. It does not create a merged file or modify the originals. Later files override duplicate cookies and localStorage entries. A missing or invalid file is an error, not an instruction to skip that session.

## Native Linux

Start Chrome in a desktop terminal:

```bash
google-chrome --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.local/share/crawl-browser" \
  https://www.reddit.com/
```

The executable may be named `google-chrome-stable` or `chromium` on your system. Log in, leave the browser open, and run the same `crawl session ... --cdp-url http://127.0.0.1:9222` command from another terminal. Press Enter in the export terminal and wait for `saved ...` before closing the browser.

## Troubleshooting

### WSL cannot connect

First check the endpoint in **Windows PowerShell**:

```powershell
Invoke-RestMethod http://127.0.0.1:9222/json/version
```

If Windows also cannot connect, check that the browser is still running and was launched with both `--remote-debugging-port=9222` and the separate `--user-data-dir`.

If Windows succeeds but WSL fails, the debugging endpoint is working but the Windows and WSL loopback networks are separate. On Windows 11 22H2 or newer, WSL mirrored networking supports access to Windows services through `127.0.0.1` ([Microsoft documentation](https://learn.microsoft.com/windows/wsl/networking)).

In Windows PowerShell, open:

```powershell
notepad "$env:USERPROFILE\.wslconfig"
```

Add this configuration. If a `[wsl2]` section already exists, add or update the setting inside it rather than creating a duplicate section. Preserve other settings.

```ini
[wsl2]
networkingMode=mirrored
```

Save all work in WSL first. The next command stops every running WSL distribution and its processes. Run it in **Windows PowerShell**:

```powershell
wsl --shutdown
```

Open WSL again and retry the `curl` connectivity check. Keep the Windows browser running. If mirrored networking is unavailable on your machine, run the export command in a Windows Python environment with the project and its browser extra installed instead.

### “The browser was closed before its session could be read”

The browser was closed before export completed. Restart it with the same profile directory, check that you are still logged in, and run the export again. This time press Enter in the export terminal while the browser remains open. Wait for `saved ...` before closing it.

### “Already exists”

Choose another output filename or add `--force` to replace the existing file.

### “No cookies were found for this session”

The export did not find cookies for the selected platform. Check `--feed`, confirm that you logged in in the browser started with the debugging port, and use a regular tab in that browser's default context.

### Login times out

The default wait is 600 seconds. Rerun with a longer wait if needed, for example `--timeout 1200`. The browser stays open in CDP mode even when the wait times out.

### Reddit still blocks the crawler

Saved login state does not guarantee access. Reddit may restrict the crawler's IP or browser environment even when manual browsing works. `--ignore-robots` does not bypass these restrictions.

## Files and cleanup

- The external browser remains open after export. Close it yourself when finished.
- The profile directory is not automatically deleted. Keep it to reuse the login, or delete it after closing the browser. Deleting it does not delete the exported JSON.
- The JSON remains until you delete or replace it. Sessions can expire or be revoked, so export again when necessary.
- Keep the debugging port local. Treat the JSON as a login credential; do not share it or commit it to Git.
