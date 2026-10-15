# Employee Attendance & Analytics API

A FastAPI + MongoDB service for employee punch-in / punch-out, corrections (regularization) and attendance analytics. All code is in `app/main.py`.

## Run it

Requires Python 3.10+ and MongoDB **6.0 or newer** (local Docker or Atlas M0 both work).

```bash
python -m venv .venv
.venv\Scripts\activate            # Windows  (Mac/Linux: source .venv/bin/activate)
pip install -r requirements.txt
```

Create a `.env` file next to `requirements.txt` (do not commit it), or set these as environment variables. Real environment variables win over `.env`:

```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Start MongoDB (for example `docker run -p 27017:27017 mongo:7`), then:

```bash
python sample_seed.py             # optional: loads the nine sample documents
uvicorn app.main:app --port 8000 --reload
```

Open `http://localhost:8000/docs` to try every endpoint in the browser. Indexes are created automatically when the app starts. If MongoDB is slow to come up, `/health` returns 503 and the indexes are created on the first successful check.

## Endpoints

| Area | Endpoint |
|---|---|
| System | `GET /health` (200 only if MongoDB answers a ping, otherwise 503) |
| Employees | `POST /employees`, `GET /employees` |
| Attendance | `POST /attendance/punch-in`, `POST /attendance/punch-out`, `GET /attendance`, `PATCH /attendance/{emp_code}/{date}` |
| Analytics | `GET /analytics/employees/{emp_code}/monthly`, `GET /analytics/departments/summary`, `GET /analytics/leaderboard/late`, `GET /analytics/departments/{department}/trend` |
| Admin | `GET /admin/explain/{endpoint}` (MongoDB `executionStats` for each endpoint's main query) |

The full contract and business rules (R1 - R10) are in `openapi.yaml`.

## How it is built

- **Times:** instants are epoch milliseconds in the API and aware UTC datetimes in MongoDB. Shift times and attendance dates are IST.
- **Safety under concurrency:** unique indexes on `emp_code` and on `(emp_code, date)` decide duplicates. Punch-out only updates a record whose `punch_out` is still empty, and a correction only applies if the record has not changed since it was read.
- **Analytics:** done as MongoDB aggregation pipelines, using the stored `late_minutes`, `overtime_minutes`, `half_day` and `work_hours`. The leaderboard and the trend moving average use window functions, and the trend fills missing days with `$densify`.
- **Rounding:** half-up everywhere (`Decimal` in Python, integer maths inside the trend pipeline).

## Other files

- `REVIEW.md`: defects found in the starter, and how each one was fixed.
- `DECISIONS.md`: the main design choices.
- `DATA_MODEL.md`: what is stored in MongoDB.
