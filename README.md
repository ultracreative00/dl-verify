# dl-verify

> AAMVA Driver's License fraud-detection API — Sprint 1 & 2

Parses the PDF417 barcode on the back of any US/CAN driver's license, runs cross-validation checks against the card front (OCR), gates on image quality, and returns a structured fraud-signal payload with a final `PASS / REVIEW / REJECT` recommendation.

A built-in browser UI is served at `/` — upload the front and back of a DL and get a full per-signal breakdown instantly.

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
10. [Using the UI](#using-the-ui)
11. [API Usage](#api-usage)
12. [Run the Test Suite](#run-the-test-suite)
13. [Project Structure](#project-structure)
14. [Fraud Signals Reference](#fraud-signals-reference)
15. [Troubleshooting](#troubleshooting)

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
     │  a