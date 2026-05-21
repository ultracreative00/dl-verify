# DL Verify

> AAMVA Driver's License Fraud Detection — Sprint 1 & 2

[![Sprint](https://img.shields.io/badge/Sprint-1--2%20Complete-brightgreen)](#fraud-signals-reference)
[![Python](https://img.shields.io/badge/Python-3.10%2B-blue)](#prerequisites)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.110%2B-green)](#run-the-api-server)
[![UI](https://img.shields.io/badge/UI-Multi--step%20upload-blueviolet)](#using-the-ui)
[![License](https://img.shields.io/badge/License-MIT-gray)](#)

A forensic identity-verification API that parses the **PDF417 barcode** on the back of any US/CAN driver's license, cross-validates it against the front (OCR), gates on image quality, and returns a structured fraud-signal payload with a final `PASS / REVIEW / REJECT` recommendation — with no DMV database calls.

A built-in **multi-step browser UI** is served at `/` — upload front and back images of a DL and get a full per-signal breakdown instantly.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Pipeline Stages](#pipeline-stages)
3. [Prerequisites](#prerequisites)
4. [System Dependencies](#system-dependencies)
5. [Clone & Environment Setup](#clone--environment-setup)
6. [Python Environment](#python-environment)
7. [Install Python Dependencies](#install-python-dependencies)
8. [PaddleOCR (Sprint 2)](#paddleocr-sprint-2)
9. [Environment Variables](#environment-variables)
10. [Run the API Server](#run-the-api-server)
11. [Using the UI](#using-the-ui)
12. [API Usage](#api-usage)
13. [Run the Test Suite](#run-the-test-suite)
14. [Project Structure](#project-structure)
15. [Fraud Signals Reference](#fraud-signals-reference)
16. [Roadmap](#roadmap)
17. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
Browser UI  →  POST /api/v1/verify  (multipart: front + back images)
                       │
                       ▼
          ┌────────────────────────┐
          │  Image Quality Gate    │  blur · glare · resolution · aspect-ratio
          └───────────┬────────────┘
                      │ pass
                      ▼
     ┌────────────────────────────┐     ┌──────────────────────────────┐
     │  PDF417 Barcode Detect     │     │  Front OCR Extraction        │
     │  (zbar / ZXing fallback)   │     │  (PaddleOCR / Textract)      │
     └───────────┬────────────────┘     └──────────────┬───────────────┘
                 │                                      │
                 ▼                                      ▼
          ┌─────────────────────────────────────────────────────────┐
          │  AAMVA Parse  →  Cross-Validation  →  OCR↔Barcode Diff  │
          │  6 checks: syntax · dates · expiry window · jurisdiction │
          │             DCF entropy · age-derived fields             │
          └───────────────────────────┬─────────────────────────────┘
                                      │
                                      ▼
                          ┌───────────────────────┐
                          │  Risk Scorer           │
                          │  Weighted signal agg.  │
                          │  PASS / REVIEW / REJECT│
                          └───────────────────────┘
```

---

## Pipeline Stages

| # | Stage | What happens | Hard-fail on |
|---|-------|-------------|-------------|
| 1 | **Image Quality Gate** | Blur, glare, resolution, aspect-ratio checks on both images | Either image fails quality floor |
| 2 | **Barcode Detection** | PDF417 located and decoded from back image via zbar/ZXing | Barcode unreadable or absent |
| 3 | **AAMVA Parse** | Raw payload parsed into 30+ structured field dict | Malformed header / unrecognised version |
| 4 | **Cross-Validation** | 6 validators run in parallel (see [Fraud Signals Reference](#fraud-signals-reference)) | Any hard-fail validator fires |
| 5 | **OCR ↔ Barcode Diff** *(Sprint 2)* | Front OCR fields diffed against barcode values | Name / DOB / DL# mismatch |
| 6 | **Risk Scoring** | Weighted signal aggregation → `risk_score` 0–1 + recommendation | — |

---

## Prerequisites

- Python **3.10+**
- `pip` / `venv` or `conda`
- **zbar** system library (for barcode detection)
- **libGL** (OpenCV dependency on Linux)

---

## System Dependencies

### macOS
```bash
brew install zbar
```

### Ubuntu / Debian
```bash
sudo apt-get update
sudo apt-get install -y libzbar0 libzbar-dev libgl1
```

### Windows
Download the [ZBar Windows installer](https://sourceforge.net/projects/zbar/) and add it to `PATH`.

---

## Clone & Environment Setup

```bash
git clone https://github.com/ultracreative00/dl-verify.git
cd dl-verify
```

---

## Python Environment

```bash
python -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
```

---

## Install Python Dependencies

```bash
pip install -r requirements.txt
```

---

## PaddleOCR (Sprint 2)

Sprint 2 adds front-image OCR extraction to power the **OCR ↔ Barcode Diff** panel. PaddleOCR is an optional heavy dependency:

```bash
pip install paddlepaddle paddleocr
```

Alternatively, set `OCR_BACKEND=textract` in `.env` to route OCR through AWS Textract (requires AWS credentials).

If neither is configured, Sprint-1-only mode runs without OCR diff — the UI panel stays hidden until OCR signals are present.

---

## Environment Variables

Copy `.env.example` to `.env` and fill in values:

```bash
cp .env.example .env
```

| Variable | Default | Description |
|----------|---------|-------------|
| `DL_VERIFY_ENV` | `development` | `development` \| `production` |
| `DL_VERIFY_VERSION` | `0.1.0` | Semver string shown in UI badge |
| `ALLOWED_ORIGINS` | `*` (dev) | Comma-separated CORS origins |
| `OCR_BACKEND` | `paddleocr` | `paddleocr` \| `textract` \| `none` |
| `AWS_ACCESS_KEY_ID` | — | Required if `OCR_BACKEND=textract` |
| `AWS_SECRET_ACCESS_KEY` | — | Required if `OCR_BACKEND=textract` |
| `AWS_REGION` | `us-east-1` | AWS region for Textract |

---

## Run the API Server

```bash
# Development (hot-reload)
uvicorn app.main:app --reload

# Production
uvicorn app.main:app --host 0.0.0.0 --port 8000 --workers 4
```

Open **http://localhost:8000** — the upload UI loads automatically as the main page.

API docs: **http://localhost:8000/docs** | ReDoc: **http://localhost:8000/redoc**

---

## Using the UI

The browser UI at **`http://localhost:8000`** is the main app entry point — no separate dashboard or navigation needed.

It walks through **3 steps inline on a single page**:

### Step 1 — Upload
- Two drop-zones: **Front** (portrait side) and **Back** (barcode side)
- Drag-and-drop or click to browse — accepts JPEG · PNG · WebP up to 10 MB each
- Image previews appear immediately; a clear button removes either side
- The **Verify Document** button activates only once both images are loaded

### Step 2 — Analyse
- Hit **Verify Document** to submit both images to `POST /api/v1/verify`
- A live **6-stage pipeline tracker** animates in real time:
  `Upload → Quality → Barcode → Parse → Validate → Score`
- Each stage shows active (pulsing), done (✓), or failed (✗)

### Step 3 — Review Results
Results appear below the upload form on the same page:

| Panel | Contents |
|-------|----------|
| **Verdict card** | `PASS / REVIEW / REJECT` label + animated risk gauge (0–100%) |
| **Hard flags** | Red/amber chips for any forced-fail or forced-review signals |
| **Validation Checks** | All 6 checks with severity badge + score; click any row to expand raw sub-signals |
| **Extracted Fields** | All 16 AAMVA fields parsed from the barcode |
| **OCR ↔ Barcode Diff** | *(Sprint 2)* Auto-shown when OCR signals are present — field-by-field match/mismatch |
| **Pipeline Warnings** | Non-fatal notices from image quality or parse steps |
| **Raw JSON** | Full API response viewer with copy-to-clipboard |

Light/dark mode toggle is in the top-right corner.

---

## API Usage

### `POST /api/v1/verify`

**Request** — `multipart/form-data`

| Field | Type | Description |
|-------|------|-------------|
| `front` | image file | Front of the DL (JPEG/PNG/WebP, max 10 MB) |
| `back` | image file | Back of the DL — must contain PDF417 barcode |

**Example (curl)**
```bash
curl -X POST http://localhost:8000/api/v1/verify \
  -F "front=@/path/to/dl_front.jpg" \
  -F "back=@/path/to/dl_back.jpg"
```

**Response schema**
```jsonc
{
  "recommendation":          "PASS" | "REVIEW" | "REJECT",
  "risk_score":              0.0,          // 0.0 (clean) → 1.0 (fraud)
  "hard_fails":              [],           // signals that forced REJECT
  "hard_warns":              [],           // signals that raised floor to REVIEW
  "checks": [
    {
      "check":    "check_syntax_conformance",
      "severity": "pass" | "warn" | "fail",
      "score":    0.0,
      "signals":  {}                        // raw sub-signals
    }
  ],
  "extracted_fields": {
    "license_number":  "A1234567",
    "family_name":     "SMITH",
    "given_name":      "JOHN",
    "date_of_birth":   "01011990",
    "expiration_date": "01012028",
    "issue_date":      "01012020",
    "address_street":  "123 MAIN ST",
    "address_city":    "ANYTOWN",
    "address_state":   "CA",
    "address_postal":  "900010000",
    "jurisdiction":    "CA",
    "aamva_version":   8
    // ... + sex, height, country, middle_name
  },
  "barcode_detected":        true,
  "image_quality_passed":    true,
  "processing_ms":           312,
  "pipeline_stage_reached":  "risk_scoring",
  "warnings":                []
}
```

---

## Run the Test Suite

```bash
pytest tests/ -v
```

Test fixtures live in `tests/fixtures/`. Synthetic barcode payloads are used — no real PII.

---

## Project Structure

```
dl-verify/
├── app/
│   ├── main.py                  # FastAPI factory — CORS, routing, static UI served at /
│   ├── api/
│   │   └── routes/
│   │       └── verify.py        # POST /api/v1/verify — pipeline orchestration
│   ├── core/
│   │   ├── barcode/
│   │   │   ├── detector.py      # PDF417 detection (zbar + ZXing fallback)
│   │   │   ├── parser.py        # AAMVA 2020 field parser
│   │   │   ├── validators.py    # 6 cross-validation checks
│   │   │   └── exceptions.py    # BarcodeNotFoundError
│   │   ├── image/
│   │   │   └── quality.py       # Blur · glare · resolution · aspect-ratio gate
│   │   └── scoring/
│   │       └── scorer.py        # Weighted signal aggregation → risk score
│   ├── models/                  # Pydantic shared models (future)
│   └── utils/
│       └── logger.py            # Structured JSON logger
├── ui/
│   └── index.html               # Single-page upload UI — served at / (main app entry point)
├── tests/
├── config/
├── .env.example
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Fraud Signals Reference

### Sprint 1 — Barcode & Cross-Validation

| Check | Severity levels | What it catches |
|-------|-----------------|-----------------|
| `check_syntax_conformance` | pass / warn / fail | Field length violations, invalid chars, unparseable dates |
| `check_date_logic` | pass / warn / fail | `issue_date` after `expiry_date`, impossible calendar dates |
| `check_expiry_window` | pass / warn / fail | Issue→expiry span mismatches state policy (4yr / 8yr) |
| `check_jurisdiction_fields` | pass / warn / fail | Missing or malformed state-specific `ZXX` fields |
| `check_dcf_entropy` | pass / warn / fail | `DCF` Document Discriminator random-string patterns |
| `check_age_derived_fields` | pass / warn / fail | `DDH` (Under-18 Until) inconsistent with `DBB` (DOB) |

### Sprint 2 — OCR ↔ Barcode Diff

| Check | What it catches |
|-------|-----------------|
| `ocr_barcode_diff` | Name · DOB · DL# mismatch between front OCR and barcode |

### Hard-fail codes

| Code | Meaning |
|------|---------|
| `image_quality_failed` | One or both images failed the quality gate |
| `barcode_unreadable` | No PDF417 found or decode failed |
| `aamva_parse_failed` | Barcode found but AAMVA header malformed |

---

## Roadmap

- [x] **Sprint 1** — AAMVA PDF417 barcode parsing + 6 cross-validation checks + image quality gate
- [x] **Sprint 2** — Front OCR extraction (PaddleOCR / Textract) + barcode↔OCR diff panel in UI
- [ ] **Sprint 3** — Face matching (InsightFace / AWS Rekognition) + passive liveness detection
- [ ] **Sprint 4** — Template geometry alignment for top-10 US states
- [ ] **Sprint 5** — Webhook delivery, result storage, API key management per customer

---

## Troubleshooting

**`ImportError: libzbar.so.0: cannot open shared object file`**
```bash
sudo apt-get install -y libzbar0
```

**`cv2.error` or `libGL.so.1: cannot open shared object file`**
```bash
sudo apt-get install -y libgl1-mesa-glx
# or
pip install opencv-python-headless
```

**`BarcodeNotFoundError` on a real DL image**
- Ensure the back image is in focus and the barcode is fully visible.
- Try increasing image brightness or contrast before uploading.
- Verify the barcode is a PDF417 (not QR or Code 128).

**CORS error in browser**
- In development, `ALLOWED_ORIGINS=*` is the default.
- For production, set `ALLOWED_ORIGINS=https://yourdomain.com` in `.env`.

**Slow first request (PaddleOCR)**
- PaddleOCR downloads model weights on first run (~200 MB). Subsequent requests are fast.
- To skip OCR, set `OCR_BACKEND=none` in `.env`.

---

> **Forensic analysis only** — DL Verify does not query DMV databases, AAMVA PDPS, or any government registry. It detects forgery signals from the document itself.
