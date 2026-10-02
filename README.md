# Download Router

[![Download Latest Release](https://img.shields.io/badge/Download-Latest_Release-blue?style=for-the-badge)](https://github.com/abdelrhman1040/download-router/releases/latest)

## 1. Overview and Key Features

The "Download Router" tool displays a popup the moment a download begins, allowing you to choose the save destination. It features automatic routing and saving (Auto-save) without showing the popup, based on defined rules that depend on file type, source, name, or download URL, along with the ability to pin your favorite folders to the top of the list for quick access.

## 2. Installation Guide

The program is a "portable version" and does not require traditional installation.

1. Download the `Download-Router-Windows.zip` file from the [Releases page](https://github.com/abdelrhman1040/download-router/releases/latest).
2. Extract the downloaded file to any safe folder on your PC (such as `C:\DownloadRouter`).
   > **Note:** Do not separate the `download_router.exe` file from its accompanying `_internal` folder; they must remain in the same directory.
3. Install the extension in Google Chrome:
   - Open the extensions page: `chrome://extensions/`
     <img width="1920" height="1080" alt="Screenshot 2026-10-02 134141" src="https://github.com/user-attachments/assets/58fb218a-c667-447d-b74a-14d67fad3df2" />

   - Enable Developer mode.
     <img width="1920" height="1080" alt="Screenshot 2026-10-02 134209" src="https://github.com/user-attachments/assets/a6325096-3f24-4618-b65c-a19d147d247f" />

   - Click "Load unpacked" and select the folder containing the `manifest.json` and `background.js` files.
     <img width="1920" height="1080" alt="Screenshot 2026-10-02 134216" src="https://github.com/user-attachments/assets/17764038-ff55-414e-80d0-4f2339d5e0f8" />
     <img width="941" height="635" alt="Screenshot 2026-10-02 134251" src="https://github.com/user-attachments/assets/e7892dcd-42cd-4118-b21c-73274c758c05" />

4. Open the `download_router.exe` file once to start running in the background.
<img width="907" height="170" alt="image" src="https://github.com/user-attachments/assets/8dc11501-efc1-43e9-b40c-54ee2186402e" />

## 3. Run on Windows Startup (Optional)

1. Right-click on `download_router.exe` (inside your extracted folder) and select "Create shortcut".
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

## 6. Run from Source Code (For Developers)

If you prefer to run the program using the original Python script instead of the compiled executable:

1. Clone this repository to your local machine.
2. Make sure you have Python installed.
3. Run the main script via your terminal or command prompt:
   ```bash
   python download_router.py
