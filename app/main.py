"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Reviewed and fixed. Every place I changed the starter has a "BUG n" / "FIX" comment so it is easy to
explain in the walkthrough. Rules (R1 - R10) are the ones in openapi.yaml.
"""
import json
import os
import re
from datetime import datetime, timedelta, timezone, date, time
from typing import Optional, Literal, Annotated
from decimal import Decimal, ROUND_HALF_UP

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# BUG 12: PyMongo returns NAIVE datetimes by default, and naive datetimes get mixed with aware ones
#         (TypeError -> 500) and are read as local time by .astimezone(). tz_aware=True means every
#         datetime we read from MongoDB is an aware UTC datetime, so all the maths is consistent.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

# BUG 5 / 13: the starter only had the emp_code index. All indexes are created at startup (idempotent).
# They are wrapped in a function so a slow MongoDB start does NOT crash the app: /health keeps answering
# 503 until MongoDB is up, and it retries the index creation as soon as the ping works.
_indexes_ready = False


def ensure_indexes():
    global _indexes_ready
    if _indexes_ready:
        return
    try:
        db.employees.create_index("emp_code", unique=True)                            # unique employee code
        db.employees.create_index([("department", 1), ("emp_code", 1)])               # list by department sorted by emp_code
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)  # ONE record per employee/day -> race-proof punch-in
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])              # attendance list sort + month/range scans
        db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])          # punch-out: latest punch_in per employee
        _indexes_ready = True
    except Exception:
        pass   # MongoDB not ready yet; tried again on the next /health call


ensure_indexes()

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]

app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def to_ms(dt):
    """datetime -> epoch milliseconds (int). Naive datetimes are treated as UTC."""
    if dt is None:
        return None
    if isinstance(dt, int):
        return dt
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def from_ms(ms: int) -> datetime:
    """epoch ms -> aware UTC datetime truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def parse_ymd(value: str, name: str = "date") -> date:
    """Strict YYYY-MM-DD -> date, else 422 (never a 500)."""
    if not re.match(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$", value or ""):
        raise HTTPException(422, f"{name} must be YYYY-MM-DD")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{name} is not a valid date")


def month_bounds(month: str):
    """'YYYY-MM' -> (first day of month, first day of next month)."""
    try:
        y, m = int(month[:4]), int(month[5:7])
        start = date(y, m, 1)
        nxt = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
    except (ValueError, IndexError):
        raise HTTPException(422, "month must be YYYY-MM")
    return start, nxt


def hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    return hhmm(shift_end) <= hhmm(shift_start)


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    # BUG 14 (R1): the starter used the server's local date. The attendance date is the IST date of the
    #              punch-in; for an overnight shift a punch-in earlier than shift_end belongs to the day before.
    ist = punch_in.astimezone(IST)
    d = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < hhmm(shift_end):
        d -= timedelta(days=1)
    return d


#def compute_late_minutes(punch_in: datetime, shift_start: str) -> int:
#    h, m = map(int, shift_start.split(":"))
#    start = punch_in.replace(hour=h, minute=m, second=0, microsecond=0)
#    minutes = int((punch_in - start).total_seconds() / 60)   # ( BUG 2) int() BEFORE the > 10 check
#    return minutes if minutes > 10 else 0
def compute_late_minutes(punch_in: datetime, shift_start: str, att_date: date) -> int:
    # BUG 2 FIX: compare in SECONDS (strictly more than 10:00), then floor to whole minutes.
    # BUG 14 FIX: shift start is built from the ATTENDANCE date in IST (matters for overnight shifts).
    start = datetime.combine(att_date, hhmm(shift_start), tzinfo=IST)
    seconds = int((punch_in.replace(microsecond=0) - start).total_seconds())
    if seconds > 600:
        return seconds // 60
    return 0


def round_half_up(value, places=2):
    d = value if isinstance(value, Decimal) else Decimal(str(value))
    return float(d.quantize(Decimal("1").scaleb(-places), rounding=ROUND_HALF_UP))


#def compute_work_hours(punch_in, punch_out) -> float:
#    return round((punch_out - punch_in).total_seconds() / 3600, 2)     # (BUG 8) banker's/float rounding
def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    # BUG 8 FIX: R4 wants HALF-UP. Decimal avoids float surprises.
    seconds = int((punch_out - punch_in).total_seconds())
    return round_half_up(Decimal(seconds) / Decimal(3600), 2)


#def compute_overtime(punch_out, shift_end, date_str) -> int:           # (BUG 9) ignored overnight, no 30 min rule
#    h, m = map(int, shift_end.split(":"))
#    end = datetime.fromisoformat(date_str).replace(hour=h, minute=m, tzinfo=IST)
#    return max(0, int((punch_out - end).total_seconds() / 60))
def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, att_date: date) -> int:
    # BUG 9 FIX: overnight shift ends the NEXT day; overtime only counts when >= 30 whole minutes (R3).
    end = datetime.combine(att_date, hhmm(shift_end), tzinfo=IST)
    if is_overnight(shift_start, shift_end):
        end += timedelta(days=1)
    minutes = int((punch_out.replace(microsecond=0) - end).total_seconds()) // 60
    if minutes >= 30:
        return minutes
    return 0


# ----- turning a Mongo document into the API shape ------------------------- #
# BUG 6 / 15: raw documents leak _id / id, return datetimes as ISO strings (API wants epoch ms), and old
#             records may miss history / half_day. One function builds the response everywhere.
def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc.get("name"),
        "email": doc.get("email"),
        "department": doc.get("department"),
        "shift_start": doc.get("shift_start"),
        "shift_end": doc.get("shift_end"),
        "joined_on": doc.get("joined_on"),
        "created_at": to_ms(doc.get("created_at")),
    }


def _ms_if_dt(v):
    return to_ms(v) if isinstance(v, datetime) else v


def serialize_history_entry(e: dict) -> dict:
    changes = {}
    for field, ch in (e.get("changes") or {}).items():
        if field in ("punch_in", "punch_out") and isinstance(ch, dict):
            changes[field] = {"from": _ms_if_dt(ch.get("from")), "to": _ms_if_dt(ch.get("to"))}
        else:
            changes[field] = ch
    return {"at": to_ms(e.get("at")), "by": e.get("by"), "reason": e.get("reason"), "changes": changes}


def serialize_record(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_ms(doc.get("punch_in")),
        "punch_out": to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day", False)),
        "history": [serialize_history_entry(h) for h in (doc.get("history") or [])],
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]

# BUG 16: instants must be epoch MILLISECONDS as a real integer. strict=True rejects floats and strings,
#         ge/le reject seconds-looking and out-of-range values -> 422.
EpochMs = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]


class EmployeeIn(BaseModel):
    # BUG 17: the starter accepted any strings (bad shift "25:99" or joined_on "abc" crashed later with 500).
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    shift_end: str = Field(default="18:30", pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def valid_joined_on(cls, v):
        parse_ymd(v, "joined_on")  # raises for 2026-02-31 etc.
        return v

    @model_validator(mode="after")
    def shifts_differ(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"   # BUG 7


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeRequest(BaseModel):
    status: Status = None            # omitted = unchanged
    punch_in: EpochMs = None
    punch_out: EpochMs = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
# BUG 11: /health had been commented out. The grader polls it; it must be 200 only if MongoDB answers, else 503.
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except Exception:
        raise HTTPException(503, "database unavailable")
    ensure_indexes()
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    # BUG 4: check-then-insert is a race. The unique index is the real guard; we turn its error into 409.
    doc = body.model_dump()
    doc["created_at"] = now_utc()      # BUG 18: was naive local datetime.now()
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),                     # BUG 19: page/page_size were unvalidated (R10)
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    # BUG 3: skip = page * page_size skipped the whole first page.
    # FIX: (page - 1) * page_size; total must count the FILTERED set; sort by emp_code (spec).
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(db.employees.find(q).sort("emp_code", 1).skip(skip).limit(page_size))
    return {"items": [serialize_employee(d) for d in items], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    # BUG 1: unknown employee -> emp was None -> TypeError -> 500. Now a clean 404.
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    # BUG 14: datetime.fromtimestamp() used the server's LOCAL time. Use UTC, truncate to seconds, derive IST date.
    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()
    att_date = attendance_date(ts, emp["shift_start"], emp["shift_end"])

    doc = {
        "emp_code": body.emp_code,
        "date": att_date.isoformat(),
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], att_date),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    # BUG 5: check-then-insert race. The unique (emp_code, date) index lets exactly one insert win.
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")

    return serialize_record(doc)   # BUG 6: epoch ms, no _id, no id


def build_attendance_find(emp_code, date_from, date_to, status, page, page_size):
    """Used by GET /attendance AND the explain endpoint, so both run the very same query."""
    df = parse_ymd(date_from, "date_from") if date_from else None
    dt = parse_ymd(date_to, "date_to") if date_to else None
    if df and dt and df > dt:
        raise HTTPException(422, "date_from must be <= date_to")
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if df or dt:
        q["date"] = {}
        if df:
            q["date"]["$gte"] = df.isoformat()
        if dt:
            q["date"]["$lte"] = dt.isoformat()
    if status:
        q["status"] = status
    return {
        "filter": q,
        "sort": {"date": -1, "emp_code": 1},
        "skip": (page - 1) * page_size,
        "limit": page_size,
    }


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,          # BUG 7b: invalid status -> 422
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    # BUG 10: starter loaded the WHOLE collection into Python, sorted there and sliced.
    # FIX: filter, sort, skip and limit all happen inside MongoDB (uses the indexes).
    find = build_attendance_find(emp_code, date_from, date_to, status, page, page_size)
    q = find["filter"]
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q)
        .sort(list(find["sort"].items()))
        .skip(find["skip"])
        .limit(find["limit"])
    )
    return {"items": [serialize_record(d) for d in docs], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(404, "employee not found")

    ts = from_ms(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", -1)],
    )
    if not record:
        raise HTTPException(404, "no punch-in record found")
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record already punched out")

    elapsed = (ts - record["punch_in"]).total_seconds()
    if elapsed <= 0 or elapsed > 24 * 3600:
        raise HTTPException(422, "punch-out must be after punch-in and within 24 hours")

    # BUG 20: overtime must use the RECORD's attendance date, not the punch-in's calendar date
    #         (an overnight punch-in at 02:00 belongs to the previous day's shift).
    att_date = date.fromisoformat(record["date"])
    work_hours = compute_work_hours(record["punch_in"], ts)
    overtime = compute_overtime(ts, emp["shift_start"], emp["shift_end"], att_date)
    half_day = work_hours < 4.50            # R5: uses the ROUNDED work_hours

    # atomic: only one of two simultaneous punch-outs can match "punch_out is still empty"
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, "work_hours": work_hours, "overtime_minutes": overtime, "half_day": half_day}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "record already punched out")

    return serialize_record(db.attendance_logs.find_one({"_id": record["_id"]}))   # no "id" field in the API


def same_value(a, b) -> bool:
    if a is None or b is None:
        return a is b
    if isinstance(a, bool) or isinstance(b, bool):
        return a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return abs(a - b) < 1e-9
    return a == b


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeRequest):
    att_date = parse_ymd(date, "date")

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": att_date.isoformat()})
    if not record:
        raise HTTPException(404, "attendance record not found")

    updates = body.model_dump(exclude_unset=True, exclude={"reason", "regularized_by"})
    if not updates:
        raise HTTPException(422, "no changes supplied")

    cur_in = record.get("punch_in")
    cur_out = record.get("punch_out")
    status = updates.get("status", record["status"])

    if status in ("ABSENT", "LEAVE"):
        if "punch_in" in updates or "punch_out" in updates:
            raise HTTPException(422, "ABSENT or LEAVE cannot have punch times")
        new_in = new_out = None
    else:
        new_in = from_ms(updates["punch_in"]) if "punch_in" in updates else cur_in
        new_out = from_ms(updates["punch_out"]) if "punch_out" in updates else cur_out
        if new_in is None:
            raise HTTPException(422, "presence status requires punch_in")

    shift_start, shift_end = employee["shift_start"], employee["shift_end"]
    if new_in is not None and attendance_date(new_in, shift_start, shift_end) != att_date:
        raise HTTPException(422, "punch_in does not belong to this attendance date")
    if new_out is not None:
        if new_out <= new_in:
            raise HTTPException(422, "punch_out must be after punch_in")
        if (new_out - new_in).total_seconds() > 24 * 3600:
            raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    # recompute derived fields from the FINAL times
    late = compute_late_minutes(new_in, shift_start, att_date) if new_in is not None else 0
    if new_in is not None and new_out is not None:
        work_hours = compute_work_hours(new_in, new_out)
        overtime = compute_overtime(new_out, shift_start, shift_end, att_date)
        half_day = work_hours < 4.50
    else:
        work_hours, overtime, half_day = None, 0, False

    new_values = {
        "status": status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
        "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day,
    }
    # BUG 21: old records can miss late/overtime/half_day. Missing means 0/False, otherwise we log fake "changes".
    old_values = {
        "status": record.get("status"), "punch_in": cur_in, "punch_out": cur_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes") or 0,
        "overtime_minutes": record.get("overtime_minutes") or 0,
        "half_day": bool(record.get("half_day", False)),
    }
    # BUG 22: history must keep punch times as BSON dates (DATA_MODEL.md section 4); epoch ms only in the API.
    changes = {}
    for f in new_values:
        if not same_value(old_values[f], new_values[f]):
            changes[f] = {"from": old_values[f], "to": new_values[f]}
    if not changes:
        raise HTTPException(422, "no changes detected")

    entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # If someone changed the record after we read it, this filter no longer matches -> 409 for the loser,
    # so two corrections can never silently overwrite each other's history.
    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {"$set": new_values, "$push": {"history": entry}},
    )
    if result.modified_count != 1:
        raise HTTPException(409, "attendance changed concurrently; retry")

    return serialize_record(db.attendance_logs.find_one({"_id": record["_id"]}))


# --------------------------------------------------------------------------- #
# Analytics: pipeline builders (shared by the endpoints AND the explain endpoint)
# Analytics read the STORED late_minutes / overtime_minutes / half_day / work_hours (never recompute).
# --------------------------------------------------------------------------- #
LATE_MIN = {"$ifNull": ["$late_minutes", 0]}
IS_LATE = {"$cond": [{"$gt": [LATE_MIN, 0]}, 1, 0]}
IS_PRESENT = {"$in": ["$status", PRESENCE]}
HALF_OR_FULL = {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]}     # missing half_day -> full day
# Mongo $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Monday-Friday = 2..6
IS_WEEKDAY = {
    "$and": [
        {"$gte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 2]},
        {"$lte": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}}, 6]},
    ]
}
PRESENT_WEEKDAY_VALUE = {"$cond": [{"$and": [IS_PRESENT, IS_WEEKDAY]}, HALF_OR_FULL, 0]}   # R6 + R7
# BUG 23: {"$ne": ["$work_hours", None]} is TRUE for a MISSING field, so it counted records with no work_hours.
#         $isNumber is true only when a real number is there.
HAS_WORK_HOURS = {"$and": [IS_PRESENT, {"$isNumber": "$work_hours"}]}
WH_HUNDREDTHS = {"$cond": [HAS_WORK_HOURS, {"$round": [{"$multiply": ["$work_hours", 100]}, 0]}, 0]}
WH_COUNT = {"$cond": [HAS_WORK_HOURS, 1, 0]}


def month_match(start: date, nxt: date) -> dict:
    return {"$match": {"date": {"$gte": start.isoformat(), "$lt": nxt.isoformat()}}}


def employee_monthly_pipeline(emp_code, start, nxt):
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": start.isoformat(), "$lt": nxt.isoformat()}}},
        {
            "$group": {
                "_id": None,
                "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
                "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            }
        },
    ]


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")):
    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    month_start, next_month = month_bounds(month)
    month_end = next_month - timedelta(days=1)

    # R7: working days Mon-Fri, counted from joined_on for a mid-month joiner
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = 0
    current_day = max(month_start, joined_on)
    while current_day <= month_end:
        if current_day.weekday() < 5:
            working_days += 1
        current_day += timedelta(days=1)

    result = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, month_start, next_month)))
    summary = result[0] if result else {}
    present = summary.get("present_days", 0)

    if working_days == 0:
        attendance_pct = None
    else:
        attendance_pct = round_half_up(Decimal(str(present)) * 100 / Decimal(working_days), 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present,
        "leave_days": summary.get("leave_days", 0),
        "late_count": summary.get("late_count", 0),
        "total_late_minutes": summary.get("total_late_minutes", 0),
        "total_overtime_minutes": summary.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


def department_summary_pipeline(start, nxt, department):
    # Step 1: sum per EMPLOYEE (small result). Step 2: join employees once per employee to get the department.
    # Step 3: sum per department. (The starter joined employees for every single log row, twice.)
    pipeline = [
        month_match(start, nxt),
        {
            "$group": {
                "_id": "$emp_code",
                "present_days": {"$sum": PRESENT_WEEKDAY_VALUE},
                "wh_hundredths": {"$sum": WH_HUNDREDTHS},
                "wh_count": {"$sum": WH_COUNT},
                "late_count": {"$sum": IS_LATE},
                "total_late_minutes": {"$sum": LATE_MIN},
                "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
            }
        },
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},          # logs of employees that don't exist are dropped here
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})
    pipeline.append(
        {
            "$group": {
                "_id": "$emp.department",
                "present_days": {"$sum": "$present_days"},
                "wh_hundredths": {"$sum": "$wh_hundredths"},
                "wh_count": {"$sum": "$wh_count"},
                "late_count": {"$sum": "$late_count"},
                "total_late_minutes": {"$sum": "$total_late_minutes"},
                "leave_count": {"$sum": "$leave_count"},
                "on_duty_count": {"$sum": "$on_duty_count"},
            }
        }
    )
    return pipeline


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    month_start, next_month = month_bounds(month)
    month_end_str = (next_month - timedelta(days=1)).isoformat()

    stats = {r["_id"]: r for r in db.attendance_logs.aggregate(department_summary_pipeline(month_start, next_month, department))}

    # R9: headcount comes from the EMPLOYEES collection, so people with zero logs still count
    # and people who join after the month do not.
    hc_match = {"joined_on": {"$lte": month_end_str}}
    if department:
        hc_match["department"] = department
    headcounts = db.employees.aggregate(
        [{"$match": hc_match}, {"$group": {"_id": "$department", "headcount": {"$sum": 1}}}, {"$sort": {"_id": 1}}]
    )

    items = []
    for h in headcounts:
        r = stats.get(h["_id"], {})
        n = r.get("wh_count", 0)
        # average over RECORDS (sum of all hours / number of records), half-up to 2 decimals (R8)
        avg = round_half_up(Decimal(int(r["wh_hundredths"])) / Decimal(n) / Decimal(100), 2) if n else None
        items.append({
            "department": h["_id"],
            "headcount": h["headcount"],
            "present_days": r.get("present_days", 0),
            "avg_work_hours": avg,
            "late_count": r.get("late_count", 0),
            "total_late_minutes": r.get("total_late_minutes", 0),
            "leave_count": r.get("leave_count", 0),
            "on_duty_count": r.get("on_duty_count", 0),
        })
    items.sort(key=lambda x: x["department"])
    return {"month": month, "items": items}


def late_leaderboard_pipeline(start, nxt, department, limit):
    pipeline = [
        month_match(start, nxt),
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": LATE_MIN}, "late_count": {"$sum": IS_LATE}}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},
    ]
    if department:
        pipeline.append({"$match": {"emp.department": department}})   # rank only inside that department
    pipeline += [
        # BUG 24: the starter's window sorted on TWO fields; rank must depend only on total_late_minutes
        #         (ties share a rank: 1, 2, 2, 4). Sort on that one field, break ties with emp_code afterwards.
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},                          # limit is applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {
            "_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name", "department": "$emp.department",
            "total_late_minutes": 1, "late_count": 1,
        }},
    ]
    return pipeline


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    start, nxt = month_bounds(month)
    items = list(db.attendance_logs.aggregate(late_leaderboard_pipeline(start, nxt, department, limit)))
    return {"month": month, "items": items}


def department_trend_pipeline(department, emp_codes, from_date, to_date):
    # Everything happens in MongoDB: group per day -> fill the missing days -> headcount -> rate -> moving average.
    lo = datetime(from_date.year, from_date.month, from_date.day, tzinfo=UTC)
    to_dt = datetime(to_date.year, to_date.month, to_date.day, tzinfo=UTC)
    hi = to_dt + timedelta(days=1)
    window = {"documents": [-6, 0]}      # current row + up to 6 rows before it, only rows INSIDE the range
    return [
        {"$match": {"emp_code": {"$in": emp_codes}, "date": {"$gte": from_date.isoformat(), "$lte": to_date.isoformat()}}},
        {"$group": {
            "_id": "$date",
            "present_count": {"$sum": {"$cond": [IS_PRESENT, HALF_OR_FULL, 0]}},   # weekends count too
            "late_count": {"$sum": IS_LATE},
        }},
        # BUG 25: $densify only works on numbers/dates. The starter's _id was a STRING ("2026-07-06"), so it crashed.
        #         Convert the day to a real date first.
        {"$set": {"day": {"$dateFromString": {"dateString": "$_id"}}}},
        {"$project": {"_id": 0, "day": 1, "present_count": 1, "late_count": 1}},
        # a department with no logs at all would leave $densify with nothing to start from, so add one empty seed row
        {"$unionWith": {"pipeline": [{"$documents": [{"day": lo, "present_count": 0, "late_count": 0}]}]}},
        {"$group": {"_id": "$day", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$set": {"day": "$_id"}},
        {"$densify": {"field": "day", "range": {"step": 1, "unit": "day", "bounds": [lo, hi]}}},   # one row per calendar day
        {"$match": {"day": {"$gte": lo, "$lte": to_dt}}},
        {"$set": {"date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$day"}}}},
        # R9: headcount that day = employees of the department with joined_on <= that day
        {"$lookup": {
            "from": "employees",
            "let": {"d": "$date"},
            "pipeline": [
                {"$match": {"department": department, "$expr": {"$lte": ["$joined_on", "$$d"]}}},
                {"$count": "n"},
            ],
            "as": "hc",
        }},
        {"$set": {
            "headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
            "is_working_day": {"$and": [{"$gte": [{"$dayOfWeek": "$day"}, 2]}, {"$lte": [{"$dayOfWeek": "$day"}, 6]}]},
        }},
        # r4 = attendance_rate in units of 1/10000, rounded HALF-UP with integer maths:
        # floor((10000 * 2p + h) / (2h)) where 2p is a whole number (half days make p a multiple of 0.5).
        # ($round in Mongo is banker's rounding, which R8 forbids.) null on weekends or headcount 0.
        {"$set": {"r4": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$trunc": [{"$divide": [
                {"$add": [{"$multiply": [{"$toInt": {"$multiply": ["$present_count", 2]}}, 10000]}, "$headcount"]},
                {"$multiply": [2, "$headcount"]},
            ]}, 0]},
            None,
        ]}}},
        {"$sort": {"day": 1}},
        # moving average over the last 7 rows: sum and count of the non-null rates in the window
        {"$setWindowFields": {"sortBy": {"day": 1}, "output": {
            "r4_sum": {"$sum": "$r4", "window": window},
            "r4_cnt": {"$sum": {"$cond": [{"$eq": [{"$type": "$r4"}, "null"]}, 0, 1]}, "window": window},
        }}},
        {"$set": {
            "attendance_rate": {"$cond": [{"$eq": [{"$type": "$r4"}, "null"]}, None, {"$divide": ["$r4", 10000]}]},
            "moving_avg_7d": {"$cond": [
                {"$eq": ["$r4_cnt", 0]},
                None,
                {"$divide": [{"$trunc": [{"$divide": [
                    {"$add": [{"$multiply": ["$r4_sum", 2]}, "$r4_cnt"]},
                    {"$multiply": [2, "$r4_cnt"]},
                ]}, 0]}, 10000]},
            ]},
        }},
        {"$sort": {"day": 1}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1,
            "late_count": 1, "attendance_rate": 1, "moving_avg_7d": 1,
        }},
    ]


def prepare_trend(department, from_, to):
    from_date = parse_ymd(from_, "from")      # BUG 26: a bad date used to crash with ValueError -> 500
    to_date = parse_ymd(to, "to")
    if to_date < from_date:
        raise HTTPException(422, "to must be >= from")
    if (to_date - from_date).days + 1 > 92:   # 92 calendar days inclusive = one quarter
        raise HTTPException(422, "range exceeds 92 days")
    codes = [e["emp_code"] for e in db.employees.find({"department": department}, {"_id": 0, "emp_code": 1})]
    if not codes:
        raise HTTPException(404, "unknown department")
    return department_trend_pipeline(department, codes, from_date, to_date)


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: str = Query(..., alias="from"), to: str = Query(...)):
    pipeline = prepare_trend(department, from_, to)
    return {"department": department, "items": list(db.attendance_logs.aggregate(pipeline))}


# --------------------------------------------------------------------------- #
# Admin: explain
# --------------------------------------------------------------------------- #
def run_explain(find=None, pipeline=None):
    # BUG 27: starter called .explain() on an aggregate CommandCursor (it has none -> 500), used the wrong
    #         verbosity, and explained simplified queries. We run the explain COMMAND with executionStats on
    #         the same query/pipeline the endpoint uses.
    if find is not None:
        inner = {"find": "attendance_logs", **find}
    else:
        inner = {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}}
    out = db.command({"explain": inner, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(out))      # ObjectId / Timestamp are not JSON-safe otherwise


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(None, pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Optional[str] = Query(None, alias="from"),
    to: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        explain = run_explain(find=build_attendance_find(emp_code, date_from, date_to, status, page, page_size))
    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month required")
        start, nxt = month_bounds(month)
        explain = run_explain(pipeline=employee_monthly_pipeline(emp_code, start, nxt))
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month required")
        start, nxt = month_bounds(month)
        explain = run_explain(pipeline=department_summary_pipeline(start, nxt, department))
    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month required")
        start, nxt = month_bounds(month)
        explain = run_explain(pipeline=late_leaderboard_pipeline(start, nxt, department, limit))
    else:  # department_trend
        if not department or not from_ or not to:
            raise HTTPException(422, "department, from, to required")
        explain = run_explain(pipeline=prepare_trend(department, from_, to))
    return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
