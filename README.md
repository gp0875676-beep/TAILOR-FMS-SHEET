# FMS Telegram Alert System

Production-grade automation: Telegram Excel upload → validate → normalize →
snapshot-diff → business/deadline rules → data-quality anomalies → deduped
Telegram alerts → persistent DB. Built for `data tailor.xlsx` (1505 rows, 24
columns) and tested end-to-end against the real workbook.

## Confirmed from real data (see chat for full derivation)

- `TAILOR HRS`, `FINISHING HRS`, `DELIVERY HOURS` = `complete − deadline`.
  Negative = finished early. Positive = SLA breach.
- `PACKING HRS` = `PACKING COMPLETE − QC DEADLINE` (QC DEADLINE doubles as the
  packing-stage deadline; there's no separate QC-complete field).
- `SEND TO AGENCY HRS` = `RECEIVED BY TAILOR − SEND TO AGENCY` — a stage
  *duration*, structurally different from the other four HRS columns.
- Primary identity = `RFID` (98.5% unique). 7 RFIDs repeat across the 1505
  rows; inspection showed these are near-duplicate row entries (same item
  logged twice ~1 min apart) — flagged as `DQ_003`, not silently deduped.
  `alteration slip NO` is NOT unique on its own (one slip covers multiple
  garments — 550 duplicate slip numbers is expected/normal).

## Quick start (local)

```bash
cp .env.example .env      # fill in TELEGRAM_BOT_TOKEN, AUTHORIZED_CHAT_IDS, AUTHORIZED_USER_IDS
pip install -r requirements.txt
python -m app.main
```

Send the bot your `.xlsx`/`.xlsm` file from an authorized chat/user. It replies
with a validation + processing summary, then sends any actionable alerts.

## Architecture

```
Telegram Upload
      │
      ▼
excel_parser + validator   (per-row validation, doesn't crash on bad rows)
      │
      ▼
snapshot_manager           (diff vs previous state: new/updated/completed/removed)
      │
      ▼
rule_engine                (deadline math per config/rules.yaml)
anomaly_detector           (separate DQ_* channel, never touches source data)
      │
      ▼
alert_engine                (fingerprint = record_id+rule_id+alert_stage, dedups against DB)
      │
      ▼
message_renderer  →  Telegram (unless DRY_RUN=true)
      │
      ▼
PostgreSQL/SQLite (uploads, record_snapshots, alert_history, anomalies)
```

A single Render **Web Service** in `render.yaml` (`fms-telegram-bot`) runs
both the Telegram bot's polling loop AND a tiny health endpoint bound to
`$PORT` — the health server runs in a background thread, the bot polling
loop runs in the main thread. This is deliberate: Render's free tier only
gives free hours to Web Services (Background Workers need a paid plan), so
everything runs as one Web Service instead of a worker + separate web app.

It's backed by a PostgreSQL database (`fms-db` in `render.yaml`), which is
what survives restarts — the uploaded Excel file itself is only ever kept in
`TEMP_DIR` and deleted right after processing (see Section 12 of the spec:
Render's local disk is not assumed permanent).

## Rule inventory

**Confirmed with you (16-Aug-2026), tested against the real workbook:**

| Rule | What it does |
|---|---|
| RULE_001 | Slip → Tailor receipt within 1 hour (excludes plain sarees). 5-min pre-deadline + overdue alert. |
| RULE_002 | Tailor stage completion deadline. 20-min warning → 10-min MOST_URGENT → overdue. Same for Normal/Urgent. |
| RULE_007 | Finishing stage completion deadline. 15-min warning → deadline-reached → 15-min-after escalation (3 alerts). |
| RULE_009 | Delivery Date vs Delivered to Customer. 4-hour reminder + overdue. |
| RULE_011 | Packing completion vs Delivery Date (lead-time). 24h + 4h reminders, no overdue (by design). |
| RULE_012 | Tailor completed but Tailor Date was never filled in — flagged as a business alert, not just data quality. |
| *(Condition 7)* | Stopped Items Report — sent after every upload, lists every piece past its deadline (Slip No + Stage only), auto-chunked to respect Telegram's 4096-char limit. |

**Still on defaults (not yet confirmed by you)** — safe to use, but give me the exact numbers whenever you're ready:

| Rule | Current default behavior |
|---|---|
| RULE_003–006 | Process-pending flags for Agency/Finishing/Packing/Delivery (informational only, not wired into alerting yet) |
| RULE_008 | QC/Packing deadline — generic global tiers (24h/12h/6h Normal, 12h/3h/30m Urgent) |
| RULE_010 | Urgent escalation — informational placeholder |

See `config/rules.yaml` for every threshold, editable without touching code.

## What's deliberately NOT built (scope calls, not oversights)

- Multi-provider Telegram failover / alert replay/backtest — not requested
  in the acceptance test (Section 55); straightforward to add as a module
  under `app/` if needed.
- Daily/periodic digest scheduler — the `/summary`-after-upload flow is
  implemented; a cron-style periodic push needs APScheduler or Render Cron
  wired into `main.py`, intentionally left out to keep the worker simple.
- Section 35 (optional time-based monitoring between uploads) — config flags
  exist (`ENABLE_TIME_BASED_MONITORING`) but the actual scheduler loop isn't
  wired in yet, since it changes the deployment shape (needs a loop or Render
  Cron Job hitting the DB independently of Telegram updates). Flag it if you
  want this built out.

## Tests

```bash
pytest tests/ -v
```

Covers stage-detection, deadline-threshold math (including the
Normal-vs-Urgent tier difference), and the "no alert on completed record"
guard. The pipeline was also run end-to-end against the real
`data_tailor.xlsx` (1505 rows) during development: 1498 unique records
recognized (7 RFID duplicates correctly skipped), 410 initial alerts,
re-uploading the identical file produced zero reprocessing, and simulating
one delivery completion on a second upload correctly produced
`completed_records: 1` with the other 409 pending alerts suppressed as
duplicates — matching the Section 55 acceptance scenario.
