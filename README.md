# dl-verify

> Production-grade Driver's License fraud detection API — AAMVA PDF417 barcode parsing, cross-validation, image quality gate, and front OCR diff.

Built as a private-repo SaaS MVP targeting SMBs who need KYC-grade document verification without enterprise pricing.

---

## Architecture

```
POST /v1/verify
  ├── Image quality gate          (blur, glare, resolution, crop completeness)
  ├── PDF417 barcode extract      (ZXing/zbar → AAMVA field parser)
  ├── Barcode syntax conformance  (field lengths, date validity, format rules)
  ├── Temporal logic checks       (issue/expiry windows, age consistency)
  ├── Jurisdiction fingerprinting (state-specific ZXX field patterns)
  ├── DCF entropy analysis        (Document Discriminator pattern matching)
  ├── [Sprint 2] Front OCR        (PaddleOCR field extraction)
  └── [Sprint 2] Barcode↔OCR diff (name, DOB, DL#, address cross-check)
```

---

## Quickstart

```bash
git clone https://github.com/ultracreative00/dl-verify
cd dl-verify
cp .env.example .env
docker-compose up --build
```

API: `http://localhost:8000`  
Docs: `http://localhost:8000/docs`

---

## API

### `POST /v1/verify`

Accepts multipart/form-data.

| Field | Type | Required | Notes |
|---|---|---|---|
| `back_image` | File | ✅ | JPG/PNG — contains PDF417 barcode |
| `front_image` | File | Sprint 2 | JPG/PNG — OCR extraction |
| `session_id` | string | ❌ | Client idempotency key |

**Response Schema**

```json
{
  "session_id": "ver_01HX...",
  "recommendation": "PASS",
  "risk_score": 0.12,
  "processing_ms": 980,
  "quality_gate": {
    "passed": true,
    "score": 91,
    "detail": {}
  },
  "barcode_parsed_fields": {
    "DAQ": "D1234567",
    "DCS": "DOE",
    "DAC": "JANE",
    "DBB": "07151990",
    "DBA": "07152028",
    "DBD": "07152020",
    "DAG": "1234 ELM ST",
    "DAI": "SACRAMENTO",
    "DAJ": "CA",
    "DAK": "958140000",
    "DBC": "2",
    "DAU": "506",
    "DCF": "00/00/0000NNNAN",
    "DCG": "USA"
  },
  "cross_validation_signals": {
    "barcode_detected":        { "passed": true,  "score": 1.0, "detail": {} },
    "barcode_syntax":          { "passed": true,  "score": 0.95, "detail": {} },
    "temporal_logic":          { "passed": true,  "score": 1.0, "detail": {} },
    "expiry_window_policy":    { "passed": true,  "score": 1.0, "detail": {} },
    "jurisdiction_fields":     { "passed": true,  "score": 0.8, "detail": {} },
    "dcf_entropy":             { "passed": true,  "score": 0.9, "detail": {} },
    "age_field_consistency":   { "passed": true,  "score": 1.0, "detail": {} }
  },
  "ocr_fields": null,
  "ocr_barcode_diff": null
}
```

---

## Signal Reference

| Signal | Weight | Auto-Fail | Description |
|--------|--------|-----------|-------------|
| `barcode_detected` | Gate | ✅ | PDF417 present and decodable |
| `barcode_syntax` | High | Partial | Field lengths, date format, country code |
| `temporal_logic` | High | Partial | DBD < DBA, valid dates, no impossible calendar values |
| `expiry_window_policy` | Medium | No | Issue-to-expiry window matches state policy |
| `jurisdiction_fields` | Medium | No | ZXX state-specific fields valid |
| `dcf_entropy` | Medium | No | Document Discriminator pattern conformance |
| `age_field_consistency` | High | No | DDH under-18 date aligns with DBB |
| `ocr_barcode_diff` | High | No | OCR fields match barcode values (Sprint 2) |

---

## Project Structure

```
dl-verify/
├── app/
│   ├── api/            # FastAPI routes
│   ├── core/
│   │   ├── barcode/    # AAMVA PDF417 parse + cross-validation
│   │   ├── ocr/        # PaddleOCR front-image extraction (Sprint 2)
│   │   └── quality/    # Image quality gate
│   ├── models/         # Pydantic schemas
│   └── utils/          # Config, logging, helpers
├── config/
│   └── jurisdiction_policy.json
├── tests/
├── docker-compose.yml
└── Dockerfile
```

---

## Roadmap

- **Sprint 1** ✅ Barcode parse, all cross-validation checks, image quality gate
- **Sprint 2** ✅ Front OCR, barcode↔OCR diff
- **Sprint 3** — Face matching + passive liveness
- **Sprint 4** — Template geometry alignment (top 10 states)
- **Sprint 5** — Webhook delivery, API key auth, per-customer audit log
