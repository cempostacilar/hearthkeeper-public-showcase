# AutoRestock Frontend (React + Vite)

This is a starter React frontend for the AutoRestock conversation + cart review experience.

## What it includes

- Chat-style check-in timeline
- Editable cart review cards
- Status controls for matched/substituted/unresolved items
- Lightweight quick-action parsing for demo commands

---

## ✅ Fast start (Windows CMD)

> Your previous errors (`ENOENT ... C:\\Users\\CemPo\\package.json`) happen when `npm` is run from the wrong folder.

Run exactly:

```bat
cd /d C:\path\to\PetProject\frontend
dir
npm install
npm run dev
```

Then open:

- `http://localhost:5173`

If `dir` output does **not** show `package.json`, you are still in the wrong directory.

---

## ✅ Fast start (PowerShell)

```powershell
Set-Location "C:\path\to\PetProject\frontend"
Get-ChildItem
npm install
npm run dev
```

Then open:

- `http://localhost:5173`

---

## Demo mode (no npm install)

If package installation is blocked, use demo mode:

```bash
cd frontend
python3 -m http.server 4173
```

Then open:

- `http://localhost:4173/demo.html`

---

## Troubleshooting

### `npm ERR! enoent Could not read package.json`
You are not in the frontend directory.

Fix:

```bat
cd /d C:\path\to\PetProject\frontend
dir
```

You must see:
- `package.json`
- `index.html`
- `src\`

### `cd frontend` says path not found
You are likely in `C:\Users\CemPo` instead of the project path.

Find the project first, then enter `frontend`:

```bat
cd /d C:\
dir
```

(Or navigate with File Explorer and copy the full folder path.)

---

## Build

```bash
npm run build
```

## Next integration step

Replace `src/sampleData.js` and quick actions in `src/App.jsx` with FastAPI endpoints:

- `POST /sessions`
- `GET /sessions/{id}`
- `POST /sessions/{id}/messages`
- `PATCH /list-items/{id}`
- `POST /sessions/{id}/build-carts`
