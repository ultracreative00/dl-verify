# dl-verify

AAMVA Driver's License fraud-detection API — Sprint 1 & 2.

Parses the PDF417 barcode on the back of any US/CAN driver's licence, runs
all cross-validation checks against the card front (OCR), gates on image
quality, and returns a structured fraud-signal payload with a final
`PASS / REVIEW / REJECT` recommendation.

---

## Table of Contents

1. [Architecture Overview](#architecture-overview)
2. [Prerequisites](#prerequisites)
3. [System Dependencies](#system-dependencies)
4. [Clone & Environment Setup](#clone--environment-setup)
5. [Python Environment](#python-environment)
6. [Install Python Dependencies](#install-python-dependencies)
7. [PaddleOCR (Sprint 2 — optional)](#paddleocr-sprint-2--optional)
8. [Environment Variables](#environment-variables)
9. [Run the API Server](#run-the-api-server)
10. [API Usage](#api-usage)
11. [Run the Test Suite](#run-the-test-suite)
12. [Project Structure](#project-structure)
13. [Fraud Signals Reference](#fraud-signals-reference)
14. [Troubleshooting](#troubleshooting)

---

## Architecture Overview

```
POST /v1/verify
        │
        ▼
┌───────────────────┐
│  Image Quality    │  blur · glare · resolution · aspect-ratio gate
│  Gate             │
└────────┬──────────┘
         │  pass
         ▼
┌───────────────────┐     ┌──────────────────────────┐
│  PDF417 Barcode   │     │  Front OCR Extraction     │  (Sprint 2)
│  Decode + AAMVA   │     │  PaddleOCR / Textract     │
│  Parse            │     └────────────┬─────────────┘
└────────┬──────────┘                  │
         │                             │
         └──────────┬──────────────────┘
                    ▼
         ┌──────────────────────┐
         │  Cross-Validation    │  6 checks → named fraud signals
         │  Engine              │
         └──────────┬───────────┘
                    ▼
         ┌──────────────────────┐
         │  Risk Scorer         │  weighted rule engine → score 0-100
         └──────────┬───────────┘
                    ▼
         JSON response  { decision, score, signals, extracted_fields }
```

---

## Prerequisites

| Requirement | Minimum version | Notes |
|---|---|---|
| Ubuntu / Debian Linux | 20.04 LTS + | Tested on 22.04 and 24.04 |
| Python | 3.10 + | 3.11 recommended |
| pip | 23 + | comes with Python |
| git | any | for cloning |

---

## System Dependencies

Install the C libraries that the barcode and image packages need:

```bash
sudo apt update && sudo apt install -y \
    python3-dev \
    python3-pip \
    python3-venv \
    build-essential \
    libzbar0 \
    libzbar-dev \
    libgl1 \
    libglib2.0-0 \
    libsm6 \
    libxext6 \
    libxrender-dev \
    libgomp1 \
    curl \
    git
```

> **`libzbar0`** — required by `pyzbar` for PDF417 barcode decoding.  
> **`libgl1` / `libglib2.0-0`** — required by `opencv-python-headless`.

---

## Clone & Environment Setup

```bash
# 1. Clone the repository
git clone https://github.com/ultracreative00/dl-verify.git
cd dl-verify

# 2. Copy the example env file
cp .env.example .env
```

---

## Python Environment

Using a virtual environment is strongly recommended to avoid dependency
conflicts with system Python.

```bash
# Create the venv (Python 3.11 shown; use python3 if 3.11 is not default)
python3 -m venv .venv

# Activate it — you must do this every time you open a new terminal
source .venv/bin/activate

# Confirm the right Python is active
which python   # should print  .../dl-verify/.venv/bin/python
python --version
```

---

## Install Python Dependencies

```bash
# Upgrade pip first (avoids resolver bugs on older pip versions)
pip install --upgrade pip

# Install all runtime + test dependencies
pip install -r requirements.txt
```

Installation takes 1–3 minutes on a typical connection. The heaviest packages
are `scipy`, `opencv-python-headless`, and `boto3`.

---

## PaddleOCR (Sprint 2 — optional)

Sprint 2 adds front-of-card OCR. PaddleOCR requires a separate install step
because it ships large model weights and has its own CUDA/CPU build matrix.

**Skip this section if you only need Sprint 1 (barcode + quality gate).**

```bash
# CPU-only install (works on any Linux machine without a GPU)
pip install paddlepaddle==2.6.1 -f https://www.paddlepaddle.org.cn/whl/linux/mkl/avx/stable.html
pip install paddleocr==2.7.3

# Then set OCR_BACKEND in your .env:
# OCR_BACKEND=paddleocr
```

> **GPU users (CUDA 11.8):**  
> Replace `paddlepaddle==2.6.1` with `paddlepaddle-gpu==2.6.1.post118` and
> install from the GPU index. See
> [PaddlePaddle install docs](https://www.paddlepaddle.org.cn/en/install/quick).

On first run PaddleOCR downloads ~200 MB of detection/recognition model
weights to `~/.paddleocr/`. This only happens once.

---

## Environment Variables

Open `.env` and adjust values for your setup. All variables ship with safe
defaults for local development.

```dotenv
# ── App ──────────────────────────────────────────────────────────
APP_ENV=development
APP_PORT=8000
SECRET_KEY=change_me_in_production   # change before any non-local use

# ── Image Quality thresholds ─────────────────────────────────────
MAX_IMAGE_SIZE_MB=10
MIN_IMAGE_WIDTH=400
MIN_IMAGE_HEIGHT=250
BLUR_THRESHOLD_FAIL=30.0    # Laplacian variance — lower = harder reject
BLUR_THRESHOLD_WARN=80.0
GLARE_PIXEL_RATIO=0.08      # fraction of image allowed to be blown out

# ── OCR Backend: paddleocr | aws_textract ────────────────────────
OCR_BACKEND=paddleocr

# ── AWS (only needed when OCR_BACKEND=aws_textract) ──────────────
AWS_ACCESS_KEY_ID=
AWS_SECRET_ACCESS_KEY=
AWS_REGION=us-east-1

# ── Logging ──────────────────────────────────────────────────────
LOG_LEVEL=INFO
```

| Variable | Description |
|---|---|
| `APP_ENV` | `development` disables auth middleware and enables debug logging |
| `APP_PORT` | TCP port the Uvicorn server listens on |
| `SECRET_KEY` | Used to sign internal tokens — set a random 32+ char string in prod |
| `BLUR_THRESHOLD_FAIL` | Laplacian variance below this value → hard FAIL on quality gate |
| `BLUR_THRESHOLD_WARN` | Variance between WARN and FAIL threshold → soft warning |
| `OCR_BACKEND` | `paddleocr` (local, free) or `aws_textract` (needs AWS creds) |

---

## Run the API Server

```bash
# Make sure the venv is active
source .venv/bin/activate

# Start the server (reload flag enables hot-reload during development)
uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload
```

Expected output:

```
INFO:     Uvicorn running on http://0.0.0.0:8000 (Press CTRL+C to quit)
INFO:     Started reloader process [xxxxx] using WatchFiles
INFO:     Started server process [xxxxx]
INFO:     Waiting for application startup.
INFO:     Application startup complete.
```

Interactive API docs are available at:

- **Swagger UI** → http://localhost:8000/docs  
- **ReDoc** → http://localhost:8000/redoc  
- **Health check** → http://localhost:8000/health

---

## API Usage

### Verify a Driver's Licence

```bash
curl -X POST http://localhost:8000/v1/verify \
  -F "back_image=@/path/to/dl_back.jpg" \
  -F "front_image=@/path/to/dl_front.jpg"
```

> `front_image` is optional for Sprint 1 (barcode-only mode). Required in
> Sprint 2 for OCR cross-validation.

**Response shape:**

```json
{
  "session_id": "01J3X...",
  "decision": "PASS",
  "score": 87,
  "recommendation": "PASS",
  "processing_time_ms": 312,
  "image_quality": {
    "back": { "passed": true, "blur_score": 143.2, "glare_ratio": 0.01 },
    "front": { "passed": true, "blur_score": 198.7, "glare_ratio": 0.02 }
  },
  "parsed_fields": {
    "license_number": "D1234567",
    "first_name": "JOHN",
    "last_name": "DOE",
    "date_of_birth": "1990-06-15",
    "expiration_date": "2028-06-15",
    "issue_date": "2024-06-15",
    "address": "123 Main St, Los Angeles, CA 90001",
    "jurisdiction": "CA",
    "aamva_version": 9
  },
  "signals": {
    "barcode_readable": true,
    "syntax_valid": true,
    "dates_logical": true,
    "expiry_future": true,
    "issue_before_expiry": true,
    "dcf_entropy_ok": true,
    "jurisdiction_fields_present": true,
    "ocr_name_match": true,
    "ocr_dob_match": true,
    "ocr_dl_number_match": true
  },
  "flags": []
}
```

**Decision values:**

| Value | Meaning |
|---|---|
| `PASS` | Score ≥ 70, no hard-fail signals |
| `REVIEW` | Score 40–69, or one or more warn-level signals |
| `REJECT` | Score < 40, or any hard-fail signal triggered |

### Health Check

```bash
curl http://localhost:8000/health
# → {"status":"ok","version":"0.1.0"}
```

---

## Run the Test Suite

The test suite is fully synthetic — no live images or network access required.

```bash
# Activate the venv if not already active
source .venv/bin/activate

# Run all tests
pytest tests/ -v

# Run with coverage report
pytest tests/ -v --cov=app --cov-report=term-missing

# Run a single test file
pytest tests/test_parser.py -v
pytest tests/test_validators.py -v
pytest tests/test_quality.py -v
pytest tests/test_scorer.py -v
```

Expected output summary:

```
tests/test_parser.py        19 passed
tests/test_validators.py    36 passed
tests/test_quality.py       14 passed
tests/test_scorer.py        18 passed
========================= 87 passed in X.XXs =========================
```

---

## Project Structure

```
dl-verify/
├── app/
│   ├── main.py              # FastAPI app factory, lifespan, middleware
│   ├── api/
│   │   └── v1/
│   │       └── verify.py    # POST /v1/verify route handler
│   ├── core/
│   │   ├── parser.py        # PDF417 decode + AAMVA field extraction
│   │   ├── validators.py    # 6 cross-validation checks → ValidationResult
│   │   ├── quality.py       # Image quality gate (blur, glare, resolution)
│   │   ├── ocr.py           # PaddleOCR / Textract abstraction (Sprint 2)
│   │   └── scorer.py        # Weighted rule engine → FraudScore
│   ├── models/
│   │   └── schemas.py       # Pydantic request/response models
│   └── utils/
│       └── logging.py       # structlog configuration
├── config/
│   └── settings.py          # pydantic-settings env loader
├── tests/
│   ├── conftest.py
│   ├── test_parser.py       # 19 cases — barcode parsing
│   ├── test_validators.py   # 36 cases — all fraud signals
│   ├── test_quality.py      # 14 cases — image quality gate
│   └── test_scorer.py       # 18 cases — scoring + hard-fail contract
├── .env.example
├── .gitignore
├── pyproject.toml
├── requirements.txt
└── README.md
```

---

## Fraud Signals Reference

### Hard-Fail Signals (any one → automatic REJECT)

| Signal key | Trigger condition |
|---|---|
| `barcode_readable` = false | PDF417 could not be decoded at all |
| `syntax_valid` = false | AAMVA field lengths / character sets violated |
| `expiry_future` = false | `DBA` expiration date is in the past |
| `issue_before_expiry` = false | `DBD` issue date is after `DBA` expiration |
| `dob_after_issue` = true | Date of birth (`DBB`) is after issue date |
| `impossible_date` = true | Any date field is not a valid calendar date |

### Warn Signals (contribute to score reduction → may trigger REVIEW)

| Signal key | Trigger condition |
|---|---|
| `dcf_entropy_ok` = false | `DCF` Document Discriminator has near-zero entropy |
| `jurisdiction_fields_present` = false | State-specific `ZXX` fields absent for known jurisdictions |
| `issue_expiry_window_mismatch` = true | Issue→expiry span doesn't match the issuing state's policy |
| `ocr_name_match` = false | Barcode name ↔ OCR-extracted name differ (Sprint 2) |
| `ocr_dob_match` = false | Barcode DOB ↔ OCR-extracted DOB differ (Sprint 2) |
| `ocr_dl_number_match` = false | Barcode DL# ↔ OCR-extracted DL# differ (Sprint 2) |

---

## Troubleshooting

### `ImportError: libzbar.so.0: cannot open shared object file`

```bash
sudo apt install -y libzbar0
```

### `ImportError: libGL.so.1: cannot open shared object file`

```bash
sudo apt install -y libgl1
```

### `ModuleNotFoundError: No module named 'cv2'`

The venv is not active, or `opencv-python-headless` wasn't installed:

```bash
source .venv/bin/activate
pip install opencv-python-headless==4.10.0.82
```

### PaddleOCR model download hangs or fails

PaddleOCR downloads models from Baidu servers on first run. If the connection
is blocked, manually download and place them under `~/.paddleocr/`:

```bash
# Check what paddle is trying to download
python -c "from paddleocr import PaddleOCR; PaddleOCR(use_angle_cls=True, lang='en')"
```

### Port 8000 already in use

```bash
# Find and kill the process using port 8000
sudo lsof -i :8000
sudo kill -9 <PID>

# Or run on a different port
uvicorn app.main:app --port 8001 --reload
# Update APP_PORT=8001 in .env accordingly
```

### Tests fail with `ModuleNotFoundError: No module named 'app'`

Run pytest from the repo root with the venv active:

```bash
cd dl-verify
source .venv/bin/activate
pytest tests/ -v
```

---

> **Note:** This project performs forensic analysis of document images.
> It does **not** make live queries to DMV databases or AAMVA registries —
> no such API is available to the private sector. Decisions are based entirely
> on document forensics and barcode cross-validation.
