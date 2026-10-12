# Employee Attendance & Analytics API

FastAPI + MongoDB. The application entry point is `app/main.py`.

## Run Locally (PowerShell)
```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Set `MONGO_URI` to your Atlas connection string when using Atlas. The API creates its indexes at startup; environment variables take precedence over values in `.env`.

## Local Fixtures
`employees.json` and `attendance_logs.json` are local Extended JSON fixtures. They are not loaded automatically, and the kit's `sample_seed.py` / `sample_data/` files were not present in this workspace. Use a disposable database when loading fixtures.

## Verification (12 Oct 2026)
Tested on local MongoDB 8.3.8. `GET /health`, `/docs`, all four July analytics requests, and all five `/admin/explain` requests returned 200. Every explain output contained `IXSCAN` and no `COLLSCAN`.

The fixture-backed July results were:
* EMP0001: 23 working days, 2 present days, 8.70% attendance.
* Engineering: headcount 2, 3 present days, 9.04 average work hours.
* Sales: headcount 2, 1.5 weighted present days, 35 late minutes, 1 leave day.
* Support: headcount 2, 2 present days, 8.75 average work hours.
* Late leaderboard: EMP0003 (Neha Iyer), 35 minutes.
* Engineering trend: 31 daily rows; headcount increases from 1 to 2 on 13 July. Non-zero attendance rates are 100% on 6 and 7 July and 50% on 14 July; weekend rates are null.

Additional probes passed: a no-log employee remained in department headcount; a mid-month joiner had 12 October working days and 0% with no logs; two 35-minute-late weekend records tied at rank 1 even with limit 1; an overnight shift produced a 7.75-hour record and 45 overtime minutes; ten simultaneous identical punch-ins returned one 201 and nine 409 responses. Probe records were removed afterward.

The pre-existing `attendance_db` contained records not present in the checked-in fixtures, so fixture analytics were tested in a temporary database. The app also initially failed index startup when it encountered equivalent indexes under MongoDB-generated names; index setup was made tolerant of equivalent existing definitions, and startup now completes.
