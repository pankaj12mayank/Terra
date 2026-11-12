# 🌍 Terra Image Studio

A lightweight, high-performance batch image pipeline and web studio built with FastAPI. Convert any list—travel places, products, blog titles, or social media posts—into focal center-cropped, high-resolution **WebP**, **PNG**, or **JPG** images with automated provider fallbacks, zero-registration guest sessions, and an interactive review grid.

---

## ✨ Features

- **Multipurpose Input:** Feed in countries, places, e-commerce products, blog titles, or social media topics via CSV upload, multiline paste, or single entry.
- **Aspect Ratio Presets:**
  - **Travel & Places:** `16:9` (1920 × 1080 px)
  - **Products & Catalogs:** `1:1` (1080 × 1080 px)
  - **Editorial & Blog:** `3:2` (1200 × 800 px)
  - **Social Posts:** `4:5` (1080 × 1350 px)
  - Smart center-cropping using Pillow (`LANCZOS`) eliminates distortion and black bars.
- **Smart Fallback Pipeline:** Searches **Pexels** first; automatically falls back to **Pixabay** if rate-limited or no hits found. Includes duplicate detection.
- **Interactive Review:** Inspect assets in a visual grid, swap alternative search candidates without re-downloading, retry failed items, or remove entries.
- **ZIP Export with Credits:** Download a single ZIP file containing all processed assets plus a structured `credits.txt` referencing photographer names, profile links, and commercial licenses.
- **Guest Sessions (No Registration):**
  - Random session ID stored in browser cookies.
  - All files strictly isolated in `sessions/{session_id}/`.
  - Sessions automatically expire and delete after 24 hours.
  - "Delete my data" button immediately wipes all files from disk.
  - Unguessable recovery link allows resuming work on any other device.
- **Admin Console:**
  - Passkey-protected dashboard (`admin123`).
  - Manage and test Pexels and Pixabay API keys with live latency checks.
  - Quota meters tracking Pexels (200/hr, 20,000/mo) and Pixabay (5,000/hr).
  - Queue monitor showing active jobs and statuses.
  - **Strict Privacy:** Admin never sees guest queries or image files.

---

## 🚀 Quick Start (Single Command)

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure API Keys (Optional)
Copy `.env.example` to `.env`:
```bash
# Windows
copy .env.example .env

# Linux / macOS
cp .env.example .env
```
Add your free API keys from [Pexels](https://www.pexels.com/api/) and/or [Pixabay](https://pixabay.com/api/docs/):
```env
PEXELS_API_KEY=your_key_here
PIXABAY_API_KEY=your_key_here
ADMIN_PASSKEY=admin123
```
*(You can also configure keys directly in the Admin Console UI)*

### 3. Start Studio
```bash
python server.py
```
That's it! The server starts on **`http://localhost:8000`** and automatically opens the studio in your browser.

---

## 💻 Standalone CLI (Optional)

If you want to run the original country image downloader from terminal:
```bash
python image_downloader.py
```
Dry run (audit without downloading):
```bash
python image_downloader.py --dry-run
```

---

## 📁 Project Structure

```
Image-Scripts/
├── server.py              # FastAPI app & single-command server runner
├── engine.py              # Image cropping, search fallbacks, quota & ZIP engine
├── session_manager.py     # Guest session isolation, recovery links & auto-purge
├── index.html             # Web Studio UI (Workspace, Admin Console, Showcase)
├── image_downloader.py    # Standalone CLI downloader
├── countries_places.csv   # 194 country landmarks dataset
├── country_images/        # Pre-cached country WebP assets
├── providers_config.json  # Provider settings & credentials
├── admin_config.json      # Admin passkey & guest policy defaults
├── requirements.txt       # Python dependencies
├── .env.example           # Credentials template
└── README.md              # Documentation
```

---

## 📡 REST API Reference

| Method | Endpoint | Description |
|---|---|---|
| `GET` | `/` | Web Studio UI |
| `GET` | `/api/session` | Get active session state & items |
| `POST` | `/api/session/settings` | Update preset, format, width, and quality |
| `POST` | `/api/session/items` | Replace entire queue (from CSV or paste) |
| `POST` | `/api/session/item/add` | Add single item |
| `DELETE` | `/api/session/item/{id}` | Remove item and delete image file |
| `DELETE` | `/api/session/items` | Clear entire list |
| `POST` | `/api/session/extend` | Extend session TTL +24h |
| `POST` | `/api/session/delete` | Immediate self-destruct of session data |
| `POST` | `/api/session/job/start` | Start batch download & crop pipeline |
| `GET` | `/api/session/job/status` | Real-time job progress polling |
| `POST` | `/api/session/item/swap` | Swap image to an alternative candidate |
| `POST` | `/api/session/item/retry` | Re-search and process failed item |
| `GET` | `/api/session/export-zip` | Download complete ZIP with `credits.txt` |
| `GET` | `/api/session/export-credits` | Download `credits.txt` directly |
| `POST` | `/api/admin/login` | Verify passkey and get admin token |
| `GET` | `/api/admin/overview` | Admin statistics and quota meters |
| `GET` | `/api/admin/providers` | View masked provider credentials |
| `POST` | `/api/admin/providers` | Save provider keys and states |
| `POST` | `/api/admin/providers/test` | Test provider connection & latency |
| `GET` | `/api/admin/policy` | View TTL and guest limits |
| `POST` | `/api/admin/policy` | Update policy settings |
| `GET` | `/api/admin/queue` | View privacy-safe queue monitor |
| `POST` | `/api/admin/purge` | Purge all expired sessions |
| `GET` | `/api/admin/audit` | View system audit log |

---

## 📄 License

Distributed under the **MIT License**.
Imagery sourced via the engine remains subject to [Pexels License](https://www.pexels.com/license/) and [Pixabay License](https://pixabay.com/service/license/).
