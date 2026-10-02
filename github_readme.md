# Download Router

## 1. Overview and Key Features

The "Download Router" tool displays a popup the moment a download begins, allowing you to choose the save destination. It features automatic routing and saving (Auto-save) without showing the popup, based on defined rules that depend on file type, source, name, or download URL, along with the ability to pin your favorite folders to the top of the list for quick access.

## 2. Installation Guide

The program is a "portable version" and does not require traditional installation.

1. Move the `download_router.exe` file to any safe folder on your PC (such as `C:\DownloadRouter`).
2. Install the extension in Google Chrome:
   - Open the extensions page: `chrome://extensions/`
   - Enable Developer mode.
   - Click "Load unpacked" and select the folder containing the `manifest.json` and `background.js` files.
3. Open the `download_router.exe` file once to start running in the background.

## 3. Run on Windows Startup (Optional)

1. Right-click on `download_router.exe` and select "Create shortcut".
2. Press `Win + R` on your keyboard to open the Run menu.
3. Type `shell:startup` and press Enter to open the Startup folder.
4. Move the shortcut to this folder. (The program will automatically run silently in the background with every reboot).

## 4. How to Open the Program and Settings

- To open settings: Just double-click on the `download_router.exe` file (or its shortcut).
- The system will detect that the program is already running and will immediately open the settings window to manage your shortcuts.

## 5. Practical Example

Goal: Automatically save any PDF file downloaded from `github.com` into the "Programming Docs" folder without any popup appearing.

1. Open the program settings and click "Add shortcut".
2. In the "Name" field: type "GitHub Docs".
3. In the "Folder" field: click "Browse" and choose the "Programming Docs" folder on your PC.
4. Enable the option "Auto-save (don't ask)".
5. In the "Conditions" section, add two conditions:
   - Field: "File type", and type in value: `.pdf`
   - Click "Add condition", choose Field: "Site", Operator: "contains", and type in value: `github.com`
6. Click "Save". Now any file matching these conditions will be moved to its folder automatically.