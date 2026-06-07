# How to create the database (autostock.db)

The file **autostock.db** is created by a small program (script). That program needs **Python** to run. If you see *"Python was not found"*, Python is either not installed or not set up for use in the terminal.

---

## Step 1: Install Python (one-time)

1. Open your browser and go to: **https://www.python.org/downloads/**
2. Click the big yellow button **"Download Python 3.x.x"**.
3. When the download finishes, run the installer (double‑click the downloaded file).
4. **Important:** On the first screen of the installer, **check the box** that says:
   - **"Add python.exe to PATH"**  
   (It’s at the bottom. If you don’t check it, the commands below won’t work.)
5. Click **"Install Now"** and wait until it finishes. Then close the installer.
6. **Close any open Command Prompt or PowerShell windows** and open a **new** one (so it picks up Python).

---

## Step 2: Create the database

1. Open **Command Prompt** (search for **cmd** in the Windows search bar).
2. Type this and press Enter (use your own path if your project is somewhere else):
   ```
   cd C:\Users\CemPo\Projects\AutoRestock
   ```
3. Then type and press Enter:
   ```
   python db_setup.py
   ```
4. You should see: **"Database created at: ... autostock.db"**  
   and the file **autostock.db** will appear in your AutoRestock folder.

**Or:** after installing Python, you can double‑click **create_database.bat** in the AutoRestock folder instead of using the command line.

---

## If you still see "Python was not found"

- Windows might be opening the **Microsoft Store** when you type `python`. To use the Python you installed from python.org instead:
  1. Open **Settings** → **Apps** → **Advanced app settings** → **App execution aliases**.
  2. Turn **Off** the entries for **"python.exe"** and **"python3.exe"** (so they don’t open the Store).
  3. Open a **new** Command Prompt and try again.

- Or try this in Command Prompt (sometimes the installer registers it as `py`):
  ```
  py db_setup.py
  ```

---

After **autostock.db** appears in the folder, you can load your order history by running **import_order_history.py** (same way: `python import_order_history.py` in that folder).
