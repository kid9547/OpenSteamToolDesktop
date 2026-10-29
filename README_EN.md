<h1 align="center">OpenSteamTool-Desktop</h1>

<p align="center">
  A Steam game library management desktop tool powered by <a href="https://github.com/OpenSteam001/OpenSteamTool">OpenSteamTool</a><br>
  Windows 11 Fluent Design GUI for all-in-one game unlocking and management
</p>

<p align="center">
  <img src="https://img.shields.io/badge/platform-Windows%2010%20%7C%2011-blue?logo=windows" alt="Platform">
  <img src="https://img.shields.io/badge/Steam-Required-1b2838?logo=steam" alt="Steam Required">
  <img src="https://img.shields.io/badge/GUI-Fluent%20Design-0078D4?logo=microsoft" alt="GUI">
  <img src="https://img.shields.io/github/v/release/kid9547/OpenSteamToolDesktop?color=green" alt="Release">
  <img src="https://img.shields.io/github/downloads/kid9547/OpenSteamToolDesktop/total" alt="Downloads">
</p>

<p align="center">
  <a href="README.md">中文</a>
</p>

---

> ⚠️ **Windows 10 / Windows 11 64-bit only.** Steam client must be installed and logged in. macOS / Linux are not supported.

---

## Download

Go to the [Releases](https://github.com/kid9547/OpenSteamToolDesktop/releases) page to download the latest version.

---

## What's New in v1.2.3

- Fixed the blank/all-black content area on the **Component Status**, **Diagnostics Center**, **Lua Rules**, **Cloud Saves** and **AppID Lookup** pages (their content widget was 0×0).
- Added a GUI assembly regression test so these scroll pages always resize their content with the window.

See [CHANGELOG.md](CHANGELOG.md) for the full release history.

---

## What's New in v1.2.2

- **Diagnostics Center**: environment snapshot, log search by level/keyword, crash-dump scan and one-click redacted diagnostic zip export.
- **AppID Lookup & Update Check**: local `appmanifest` + `appinfo.vdf` lookup for build, depot-manifest completeness and Lua rule status.
- **Denuvo tickets**: both `AppTicket` and `ETicket` are now required and validated; fixed AppID detection when the embedded AppID sits at a 2-byte-aligned offset. The ticket extractor remains bundled in the release.
- All 1.2.1 features remain available: component status, manifest pinning, cloud-save manager and Lua rule manager.

---

## Table of Contents

- [Features](#features)
- [OpenSteamTool Core Capabilities](#opensteamtool-core-capabilities)
- [Dashboard Overview](#dashboard-overview)
- [Usage Guide](#usage-guide)
- [In-Library Result](#in-library-result)
- [Antivirus Notice](#antivirus-notice)
- [FAQ](#faq)
- [Developer Guide](#developer-guide)
- [Disclaimer](#disclaimer)
- [License](#license)
- [Reference Projects & Feature Provenance](#reference-projects--feature-provenance)

---

## Features

| Feature                   | Description                                                                                         |
| ------------------------- | --------------------------------------------------------------------------------------------------- |
| **Manifest & Lua Import** | **New**: Import via file/folder dialog or **drag-and-drop** (`.manifest` / `.lua` / `.zip` auto-extracted) with smart AppID detection |
| **Network 403 Fix**       | **New**: Auto-configures `opensteamtool.toml` & `manifest.lua` multi-source fallback resolver (wudrm/steamrun) to fix Access Denied 403 error |
| **Modern Frog Icon**      | **New**: Anti-aliased transparent frog avatar with full multi-res chain; fixes blank taskbar icon on Windows |
| **Injection Manager**     | One-click deploy/remove OpenSteamTool DLLs with triple-layer verification, plus quick Steam restart |
| **Game Search & Add**     | Search Steam games by AppID or English name; auto-fetch metadata and generate library config        |
| **Full DLC Unlock**       | Automatically detects all DLCs associated with a game and unlocks them in one click                 |
| **Depot Decryption Keys** | Automatically retrieves decryption keys for each encrypted Depot to enable normal downloads         |
| **Game Library Manager**  | Card-style browsing with sorting, search, and right-click actions (view/copy/remove)                |
| **Component Status**      | **New**: Verifies that all three kernel DLLs are actually loaded by Steam, not merely present on disk |
| **Manifest Pinning**      | **New**: Pin a depot to a specific manifest GID so Lua regeneration or auto-completion cannot overwrite it |
| **Cloud Save Manager**    | **New**: Scan, back up and restore Steam `userdata` cloud saves for multiple accounts                |
| **Lua Rule Manager**      | **New**: Scans both `config/lua` and `config/stplug-in`, with enable/disable, conflict detection and details |
| **Diagnostics Center**    | **New**: Environment snapshot, log search by level/keyword, crash-dump scan and redacted diagnostic zip export |
| **AppID Lookup & Update Check** | **New**: Local `appmanifest` + `appinfo.vdf` lookup for build, manifest completeness and Lua status |
| **Denuvo Tickets**        | **New**: Import, extract or paste `AppTicket` + `ETicket`, written to both registry and Lua; extractor bundled in the release |
| **Auto Update**           | Checks GitHub Releases on startup and guides you to the latest version                              |

---

## OpenSteamTool Core Capabilities

OpenSteamTool is a C++ engine that injects into the Steam client via DLL. This tool provides a graphical interface for it. Once injected, you gain:

| Capability               | Description                                                                    |
| ------------------------ | ------------------------------------------------------------------------------ |
| **Game Unlocking**       | Unlock games and all DLCs you don't own                                        |
| **Depot Decryption**     | Auto-inject depot decryption keys for normal file downloads                    |
| **Manifest Download**    | Auto-download depot manifests with version locking to prevent unwanted updates |
| **Access Token**         | Support for protected games/DLCs that require access tokens                    |
| **Stats & Achievements** | Enable Steam stats and achievements for unowned games                          |
| **Hot Reload**           | Lua config changes take effect instantly without restarting Steam              |

---

## Dashboard Overview

Automatically detects Steam installation status, injection status, and library overview on startup:

![Dashboard](assets/2026-06-03-13-38-03-image.png)

---

## Usage Guide

### Step 1: Inject into Steam

1. Go to the **Injection Manager** page
2. Click the **Inject Steam** button
3. Restart the Steam client to take effect

> If Steam is currently running, the app will prompt you to close it first.

![Injection Manager](assets/2026-06-03-13-38-34-image.png)

### Step 2: Search and Add Games

1. Go to the **Search & Add** page
2. Enter a game **AppID** (e.g., `730` = CS2) or **English game name**
3. Click **Add to Library** — the tool automatically fetches metadata, DLC lists, and decryption keys, then generates the config

![Search & Add](assets/2026-06-03-13-38-55-image.png)

### Step 3: Manage Your Game Library

1. Go to the **Game Library** page to view all added games
2. Sort by name/AppID or search to filter
3. Right-click a game card to: copy AppID, copy game name, view on Steam, or remove from library

![Game Library](assets/2026-06-03-13-39-06-image.png)

---

## In-Library Result

Once added, the game appears directly in your Steam library:

![In-Library](assets/2026-06-03-13-49-34-image.png)

---

## Antivirus Notice

If Windows Security flags the program as a threat (DLL injection may trigger false positives), follow these steps:

1. Open **Windows Security**

![Windows Security](assets/2026-06-03-14-16-28-image.png)

2. Go to **Virus & threat protection** → **Manage settings**

![Manage Settings](assets/2026-06-03-14-27-30-image.png)

3. Turn off **Real-time protection**. Windows Security is prone to false positives with DLL injection tools — disabling it prevents the program from being blocked.

![Real-time protection](assets/2026-06-03-14-27-05-image.png)

---

## FAQ

### Steam shows no change after injection?

Make sure you have **restarted Steam**. DLL injection only takes effect at Steam startup. Use the **Verify Injection** button on the Injection Manager page to check the status.

### Game shows "Content still encrypted" after adding?

This occurs when some Depots are missing decryption keys. The tool automatically fetches them — if the issue persists, check your network connection.

### How do I uninstall the injection?

Click **Remove Injection** on the Injection Manager page, then restart Steam. You can also manually delete the three DLL files from your Steam root directory.

---

## Developer Guide

This is an open-source project — contributions and debugging are welcome. Follow the steps below to set up a local development environment.

### Prerequisites

| Requirement | Version |
|-------------|---------|
| OS | Windows 10 / 11 (64-bit) |
| Python | **3.10+** (3.12 or 3.13 recommended) |
| Steam Client | Installed and logged in |
| Git | Any version |

### 1. Clone the Repository

```bash
git clone https://github.com/kid9547/OpenSteamToolDesktop.git
cd OpenSteamToolDesktop
```

### 2. Create a Virtual Environment

```bash
# Create .venv in the project root
python -m venv .venv

# Activate it
.venv\Scripts\activate
```

> The command prompt prefix will show `(.venv)` once activated.

### 3. Install Dependencies

```bash
pip install -r requirements.txt
```

Key dependencies:

| Package | Purpose |
|---------|---------|
| `PyQt6` | GUI framework |
| `PyQt6-Fluent-Widgets` | Windows 11 Fluent Design component library |
| `httpx` | HTTP client (API requests, cover downloads) |
| `requests` | Fallback HTTP client |
| `pytest` | Unit testing |

### 4. Run & Debug

```bash
# Launch the app
python main.py
```

On startup, the app automatically:
- Detects the Steam installation path (via registry)
- Checks and downloads the required DLL (if missing)
- Checks for app updates
- Loads the game library

**Debugging tips:**

- Logs: `~/.OpenSteamToolDesktop/logs/main.log`
- Config: `~/.OpenSteamToolDesktop/config.json`
- DLL cache: `~/.OpenSteamToolDesktop/DLL/`
- Lua configs: `~/.OpenSteamToolDesktop/config/lua/`

### 5. IDE Debugging

<details>
<summary><b>VS Code</b></summary>

1. Install the [Python extension](https://marketplace.visualstudio.com/items?itemName=ms-python.python)
2. Open the project root folder
3. Select interpreter: `Ctrl+Shift+P` → `Python: Select Interpreter` → choose the `.venv` Python
4. Press `F5` to debug, or add this to `launch.json`:

```json
{
    "name": "OpenSteamToolDesktop",
    "type": "python",
    "request": "launch",
    "program": "${workspaceFolder}/main.py",
    "console": "integratedTerminal",
    "justMyCode": false
}
```

</details>

<details>
<summary><b>PyCharm</b></summary>

1. `File → Open` the project root directory
2. `File → Settings → Project → Python Interpreter` → select `.venv\Scripts\python.exe`
3. Right-click `main.py` → `Debug 'main'` to set breakpoints and debug

</details>

### 6. Run Tests

```bash
# Run all tests
pytest

# Run a single test file
pytest tests/test_game_manager.py

# Verbose output
pytest -v
```

### 7. Project Structure

```
OpenSteamToolDesktop/
├── main.py                  # Application entry point
├── config.py                # Global config constants (URLs, colors, HTTP, etc.)
├── requirements.txt         # Runtime dependencies
├── build_exe.py             # Build script (PyInstaller / Nuitka)
├── OpenSteamToolDesktop.spec # PyInstaller spec
├── core/                    # Core business logic
│   ├── steam_bridge.py      #   Steam bridge (DLL management, injection)
│   ├── game_manager.py      #   Lua game config generation & management
│   ├── metadata_fetcher.py  #   Steam metadata fetching
│   ├── dll_manager.py       #   DLL download & version management
│   ├── dll_injector.py      #   DLL injection & verification
│   ├── version_checker.py   #   App version checking
│   └── ...
├── gui/                     # UI layer
│   ├── main_window.py       #   Main window
│   ├── home_page.py         #   Dashboard page
│   ├── search_page.py       #   Search & add page
│   ├── library_page.py      #   Game library page
│   ├── inject_page.py       #   Injection manager page
│   ├── accelerate_page.py   #   Network acceleration page
│   └── widgets/             #   Custom widgets
├── utils/                   # Utility modules
│   ├── http_client.py       #   Unified HTTP client
│   ├── async_worker.py      #   QThread async task wrapper
│   ├── logger.py            #   Logging module
│   └── ...
├── resources/               # Static resources
│   └── fallback_dlls/       #   Built-in fallback DLLs
├── assets/                  # Screenshots, icons
└── tests/                   # Unit tests
```

### 8. Build & Package

```bash
# Install build tool
pip install pyinstaller

# PyInstaller build (default, produces dist/ + Zip)
python build_exe.py

# Nuitka build (C++ compilation, stronger code protection)
python build_exe.py --nuitka

# Clean build artifacts only
python build_exe.py --clean

# Build without generating Zip
python build_exe.py --no-zip
```

Build output goes to `dist/`, and a `OpenSteamToolDesktop-{version}.zip` is generated in the project root.

---

## Disclaimer

This project is for **educational and research purposes only**. Please adhere to the following:

- This tool does not provide game download functionality
- Respect the work of game developers and support genuine purchases
- Any consequences arising from the use of this tool are borne solely by the user
- Commercial use of this tool is strictly prohibited

---

## License

This project is powered by the [OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool) engine.

---

## Reference Projects & Feature Provenance

Some features were inspired by the interaction and implementation ideas of the following open-source projects. **Reference does not mean direct copying**: except for the explicitly bundled kernel DLLs, the UI, validation logic and local file handling are implemented independently by this project.

| Feature | Reference project(s) | What was referenced / how this project differs |
| --- | --- | --- |
| **Component Status** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool), [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua) | Component install/load state and fallback-loader notes; this project uses Windows Toolhelp to inspect Steam process modules instead of `psutil` |
| **Diagnostics Center** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool) | Log filtering, crash dumps and diagnostic collection; this project adds an environment snapshot, config redaction and one-click diagnostic zip export |
| **Lua Rule Manager** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua), [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool) | Lua enable/disable, bulk import and per-game configuration; this project scans both `config/lua` and `config/stplug-in` and adds conflict detection |
| **Cloud Save Manager** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua) | Cloud-save backup/restore ideas; this project works directly on Steam `userdata` and does not require the CloudRedirect DLL |
| **Manifest Pinning** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua), [hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | “Pin to current/latest manifest” and `setManifestid`; this project stores pins in local `manifest_pins.json` and never overwrites existing entries during auto-completion |
| **Denuvo Tickets** | [huanyuejue/Fluent-Steam-Lua](https://github.com/huanyuejue/Fluent-Steam-Lua), [hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | Ticket extraction/import and `[denuvo] mode`; this project requires both `AppTicket` and `ETicket` and writes them to both the registry and Lua |
| **AppID Lookup & Update Check** | [G-Yoka/G-OpenSteamTool](https://github.com/G-Yoka/G-OpenSteamTool), [SteamDatabase/SteamAppInfo](https://github.com/SteamDatabase/SteamAppInfo), [SteamDatabase/ValveKeyValue](https://github.com/SteamDatabase/ValveKeyValue) | Local cache lookup and `appinfo.vdf` binary parsing; this project adds combined build, depot-manifest, Lua and pin checks |
| **Signature Tables / Data Channels** | [huanyuejue/steam-monitor](https://github.com/huanyuejue/steam-monitor), [hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | Zero-seed pattern/IPC resolution; this project ships pre-generated TOMLs for the current Steam build as a fallback when mirrors are unreachable |
| **Cloud Save Redirect** | [Selectively11/CloudRedirect](https://github.com/Selectively11/CloudRedirect) (archived), [hazjy/CloudRedirect](https://github.com/hazjy/CloudRedirect) | Only `[cloud]` configuration, toggle and DLL detection are provided; this project **does not bundle** `cloud_redirect.dll` |
| **Kernel DLLs** | [hazjy/ZSteamTool](https://github.com/hazjy/ZSteamTool) | The bundled kernel is ZSteamTool **v1.2.1**; its upstream is [OpenSteam001/OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool), with changes inspired by [BetterSteamTools](https://github.com/madoiscool/BetterSteamTools) |
| **Desktop Base** | [yong0512/OpenSteamToolDesktop](https://github.com/yong0512/OpenSteamToolDesktop) | This project is a secondary development based on that desktop application |

> Note: All features above have been verified for page rendering and local data reading against a real Steam installation. Write operations such as Lua toggling, cloud-save backup, manifest pinning and Denuvo ticket import keep original files or backups wherever possible. Real Denuvo authorization still requires an account that **actually owns the game**; see the FAQ above.
>
> The bundled ZSteamTool kernel is distributed under **GPL-3.0**; see its repository for source and license. All other reference projects remain copyright of their respective authors.

---

## Acknowledgments & Project Origin

- Forked and maintained by [kid9547](https://github.com/kid9547/OpenSteamToolDesktop) based on [yong0512/OpenSteamToolDesktop](https://github.com/yong0512/OpenSteamToolDesktop).
- Powered by [OpenSteamTool](https://github.com/OpenSteam001/OpenSteamTool).
- UI built with [QFluentWidgets](https://github.com/zhiyiYo/PyQt-Fluent-Widgets).

---

## Star History

[![Star History Chart](https://api.star-history.com/svg?repos=kid9547/OpenSteamToolDesktop&type=Date)](https://star-history.com/#kid9547/OpenSteamToolDesktop&Date)

---

<p align="center">
  <sub>Made with ❤️ for the Steam community</sub>
</p>
