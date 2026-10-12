"""
Employee Attendance & Analytics API  (FastAPI + MongoDB)

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB   (real environment variables win over a local .env)

Layout of this single file:
  1. configuration and Mongo connection
  2. time / shift helpers  (R1 - R5)         <- pure functions, no database
  3. validation types and request models
  4. serializers (Mongo document -> API JSON)
  5. startup: indexes
  6. endpoints: health, employees, attendance, analytics, admin/explain
"""
import json
import logging
import os
import re
import calendar
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal, Optional

from bson import json_util
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse
from pydantic import AfterValidator, BaseModel, Field, StrictInt, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

# --------------------------------------------------------------------------- #
# 1. Configuration
# --------------------------------------------------------------------------- #
load_dotenv()  # does NOT override variables that are already in the environment

log = logging.getLogger("attendance")

# tz_aware=True: PyMongo hands back timezone-aware (UTC) datetimes, so we never
# mix naive and aware values.
client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,
    tzinfo=timezone.utc,
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]
employees = db["employees"]
logs = db["attendance_logs"]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]  # R6
ABSENCE = ["ABSENT", "LEAVE"]
GRACE_SECONDS = 10 * 60  # R2
MIN_OVERTIME_MINUTES = 30  # R3
HALF_DAY_HOURS = 4.50  # R5
MAX_SHIFT_SECONDS = 24 * 3600


@asynccontextmanager
async def lifespan(_app: FastAPI):
    try:
        ensure_indexes()
    except PyMongoError as exc:  # keep the process alive; /health will report 503
        log.error("could not create indexes at startup: %s", exc)
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


@app.exception_handler(PyMongoError)
async def mongo_error_handler(_req: Request, exc: PyMongoError):
    # Database trouble is "service unavailable", never an unexplained 500.
    return JSONResponse(status_code=503, content={"detail": "database unavailable"})


# --------------------------------------------------------------------------- #
# 2. Time and rule helpers (R1 - R5). Pure functions: easy to unit test.
# --------------------------------------------------------------------------- #
def ms_to_dt(ms: int) -> datetime:
    """Epoch milliseconds -> aware UTC datetime, truncated to whole seconds (R1)."""
    return EPOCH + timedelta(seconds=ms // 1000)


def dt_to_ms(dt: Optional[datetime]) -> Optional[int]:
    """Datetime (aware, or naive meaning UTC) -> epoch milliseconds."""
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return (dt - EPOCH) // timedelta(milliseconds=1)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _hm(hhmm: str) -> time:
    h, m = hhmm.split(":")
    return time(int(h), int(m))


def is_overnight(shift_start: str, shift_end: str) -> bool:
    # zero-padded HH:MM strings compare correctly as text
    return shift_end <= shift_start


def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> str:
    """R1: IST calendar date of the punch-in; for overnight shifts a punch-in
    earlier than shift_end belongs to the shift that started the previous day."""
    ist = punch_in.astimezone(IST)
    day = ist.date()
    if is_overnight(shift_start, shift_end) and ist.time() < _hm(shift_end):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str: str, shift_start: str, shift_end: str) -> tuple[datetime, datetime]:
    """Start and end instants (IST-aware) of the shift that belongs to `date_str`."""
    day = Date.fromisoformat(date_str)
    start = datetime.combine(day, _hm(shift_start), tzinfo=IST)
    end_day = day + timedelta(days=1) if is_overnight(shift_start, shift_end) else day
    end = datetime.combine(end_day, _hm(shift_end), tzinfo=IST)
    return start, end


def late_minutes_for(punch_in: datetime, shift_start_dt: datetime) -> int:
    """R2: late only if strictly more than 10:00 after shift start; then floor minutes since start."""
    secs = int((punch_in - shift_start_dt).total_seconds())
    return secs // 60 if secs > GRACE_SECONDS else 0


def overtime_minutes_for(punch_out: datetime, shift_end_dt: datetime) -> int:
    """R3: floor minutes after shift end, counted only if at least 30."""
    mins = int((punch_out - shift_end_dt).total_seconds()) // 60
    return mins if mins >= MIN_OVERTIME_MINUTES else 0


def round_half_up(value, places: int) -> float:
    """R4/R8: half-up rounding done with Decimal, not with float round()."""
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def work_hours_for(punch_in: datetime, punch_out: datetime) -> float:
    secs = int((punch_out - punch_in).total_seconds())
    return float((Decimal(secs) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def derive(status: str, punch_in, punch_out, emp: dict, date_str: str) -> dict:
    """All stored derived fields for one record, per the table in DATA_MODEL.md."""
    out = {"work_hours": None, "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    if status not in PRESENCE or punch_in is None:
        return out
    start_dt, end_dt = shift_bounds(date_str, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    out["late_minutes"] = late_minutes_for(punch_in, start_dt)
    if punch_out is not None:
        out["work_hours"] = work_hours_for(punch_in, punch_out)
        out["overtime_minutes"] = overtime_minutes_for(punch_out, end_dt)
        out["half_day"] = out["work_hours"] < HALF_DAY_HOURS
    return out


def month_bounds(month: str) -> tuple[Date, Date]:
    year, mon = int(month[:4]), int(month[5:7])
    return Date(year, mon, 1), Date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(first: Date, last: Date, joined_on: Date) -> int:
    """R7: Mon-Fri between max(first, joined_on) and last. Pure calendar arithmetic (<= 31 steps)."""
    day, total = max(first, joined_on), 0
    while day <= last:
        total += day.weekday() < 5
        day += timedelta(days=1)
    return total


# --------------------------------------------------------------------------- #
# 3. Validation types and request models
# --------------------------------------------------------------------------- #
_DATE_RE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")


def _check_date(value: str) -> str:
    if not _DATE_RE.match(value):
        raise ValueError("must be YYYY-MM-DD")
    Date.fromisoformat(value)  # rejects 2026-02-30 and friends
    return value


DateStr = Annotated[str, AfterValidator(_check_date)]
_MONTH_PATTERN = r"^[0-9]{4}-(0[1-9]|1[0-2])$"
MonthStr = Annotated[str, Field(pattern=_MONTH_PATTERN)]  # for models / plain use
MonthQuery = Annotated[str, Query(pattern=_MONTH_PATTERN)]  # required query param (constraint lives in Query)
OptMonthQuery = Annotated[Optional[str], Query(pattern=_MONTH_PATTERN)]
FromQuery = Annotated[DateStr, Query(alias="from")]
OptFromQuery = Annotated[Optional[DateStr], Query(alias="from")]
TimeStr = Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")]
# StrictInt: floats, strings and bools are rejected (422). Range rejects "seconds-looking" values.
EpochMs = Annotated[StrictInt, Field(ge=100_000_000_000, le=4_102_444_800_000)]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
Page = Annotated[int, Query(ge=1, le=1_000_000_000)]
PageSize = Annotated[int, Query(ge=1, le=100)]


class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: TimeStr = "09:30"
    shift_end: TimeStr = "18:30"
    joined_on: DateStr

    @model_validator(mode="after")
    def _shift_differs(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None
    status: PresenceStatus = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMs] = None


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMs] = None
    punch_out: Optional[EpochMs] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)
    # derived fields sent by a client are simply ignored (pydantic drops unknown keys)


# --------------------------------------------------------------------------- #
# 4. Serializers: Mongo document -> API JSON
# --------------------------------------------------------------------------- #
def employee_out(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": dt_to_ms(doc.get("created_at")),
    }


def _history_out(entry: dict) -> dict:
    changes = {}
    for key, change in (entry.get("changes") or {}).items():
        if key in ("punch_in", "punch_out"):  # nested BSON dates -> epoch ms (easy to forget)
            change = {"from": dt_to_ms(change.get("from")), "to": dt_to_ms(change.get("to"))}
        changes[key] = change
    return {"at": dt_to_ms(entry.get("at")), "by": entry.get("by"), "reason": entry.get("reason"), "changes": changes}


def record_out(doc: dict) -> dict:
    """Missing legacy fields default as DATA_MODEL.md says: late/overtime 0, half_day false, history []."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_ms(doc.get("punch_in")),
        "punch_out": dt_to_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes") or 0,
        "overtime_minutes": doc.get("overtime_minutes") or 0,
        "half_day": bool(doc.get("half_day")),
        "history": [_history_out(h) for h in (doc.get("history") or [])],
    }


def get_employee_or_404(emp_code: str) -> dict:
    emp = employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    return emp


# --------------------------------------------------------------------------- #
# 5. Indexes (created at startup, idempotent)
# --------------------------------------------------------------------------- #
def ensure_indexes() -> None:
    def ensure(collection, keys, name: str, unique: bool = False) -> None:
        key_pattern = tuple(keys)
        for index in collection.list_indexes():
            if tuple(index["key"].items()) != key_pattern:
                continue
            compatible = (
                index.get("unique", False) == unique
                and not index.get("sparse", False)
                and "partialFilterExpression" not in index
                and "collation" not in index
                and "expireAfterSeconds" not in index
            )
            if not compatible:
                raise RuntimeError(f"existing index for {key_pattern} has incompatible options")
            return
        collection.create_index(keys, unique=unique, name=name)

    # employees
    ensure(employees, [("emp_code", ASCENDING)], "uniq_emp_code", unique=True)
    ensure(employees, [("department", ASCENDING), ("emp_code", ASCENDING)], "dept_emp_code")
    ensure(employees, [("department", ASCENDING), ("joined_on", ASCENDING)], "dept_joined_on")
    ensure(employees, [("joined_on", ASCENDING)], "joined_on")
    # attendance_logs
    ensure(logs, [("emp_code", ASCENDING), ("date", ASCENDING)], "uniq_emp_date", unique=True)
    ensure(logs, [("date", DESCENDING), ("emp_code", ASCENDING)], "date_desc_emp")
    ensure(logs, [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)], "status_date_emp")
    ensure(logs, [("emp_code", ASCENDING), ("punch_in", DESCENDING)], "emp_punch_in")


# --------------------------------------------------------------------------- #
# 6a. Health
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


# --------------------------------------------------------------------------- #
# 6b. Employees
# --------------------------------------------------------------------------- #
@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()
    try:
        employees.insert_one(doc)  # uniqueness is enforced by the unique index, not by a prior find
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department else {}
    total = employees.count_documents(q)  # counts the FILTERED set
    cursor = employees.find(q).sort("emp_code", ASCENDING).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# 6c. Attendance
# --------------------------------------------------------------------------- #
@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()
    date_str = attendance_date(ts, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30"))
    doc = {
        "emp_code": body.emp_code,
        "date": date_str,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        **derive(body.status, ts, None, emp, date_str),
        "history": [],
    }
    try:
        logs.insert_one(doc)  # unique (emp_code, date) index decides the race: one 201, the rest 409
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return record_out(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = get_employee_or_404(body.emp_code)
    ts = ms_to_dt(body.punched_at) if body.punched_at is not None else now_utc()

    # most recent record whose punch_in is at or before the punch-out (works across midnight)
    rec = logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    pin = rec["punch_in"]
    if ts <= pin:
        raise HTTPException(422, "punched_at must be after punch_in")
    if (ts - pin).total_seconds() > MAX_SHIFT_SECONDS:
        raise HTTPException(422, "punched_at is more than 24 hours after punch_in")

    derived = derive(rec["status"], pin, ts, emp, rec["date"])
    # Atomic compare-and-set: only succeeds if the record is STILL open. Of two simultaneous
    # punch-outs exactly one matches this filter; the other gets None -> 409.
    updated = logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": ts, **derived}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return record_out(updated)


def build_attendance_query(emp_code, date_from, date_to, status) -> dict:
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, "date_from must not be after date_to")
    q: dict = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status:
        q["status"] = status
    return q


ATTENDANCE_SORT = [("date", DESCENDING), ("emp_code", ASCENDING)]


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    q = build_attendance_query(emp_code, date_from, date_to, status)
    total = logs.count_documents(q)
    cursor = logs.find(q).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [record_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


def _plan_regularization(rec: dict, emp: dict, body: RegularizeIn) -> tuple[dict, dict]:
    """Work out the new stored state and the `changes` object. Raises 422 for rule violations."""
    old_status = rec["status"]
    new_status = body.status or old_status

    if new_status in ABSENCE:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(422, "ABSENT/LEAVE clear the punch times; do not supply them")
        pin = pout = None
    else:
        pin = ms_to_dt(body.punch_in) if body.punch_in is not None else rec.get("punch_in")
        pout = ms_to_dt(body.punch_out) if body.punch_out is not None else rec.get("punch_out")
        if pin is None:
            raise HTTPException(422, "a presence status requires a punch_in")
        if attendance_date(pin, emp.get("shift_start", "09:30"), emp.get("shift_end", "18:30")) != rec["date"]:
            raise HTTPException(422, "punch_in must stay on the record's attendance date")
        if pout is not None:
            if pout <= pin:
                raise HTTPException(422, "punch_out must be after punch_in")
            if (pout - pin).total_seconds() > MAX_SHIFT_SECONDS:
                raise HTTPException(422, "punch_out must be within 24 hours of punch_in")

    new = {"status": new_status, "punch_in": pin, "punch_out": pout, **derive(new_status, pin, pout, emp, rec["date"])}
    old = {
        "status": old_status,
        "punch_in": rec.get("punch_in"),
        "punch_out": rec.get("punch_out"),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes") or 0,
        "overtime_minutes": rec.get("overtime_minutes") or 0,
        "half_day": bool(rec.get("half_day")),
    }
    if all(old[k] == new[k] for k in ("status", "punch_in", "punch_out")):
        raise HTTPException(422, "request changes nothing")
    changes = {k: {"from": old[k], "to": new[k]} for k in new if old[k] != new[k]}
    return new, changes


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: DateStr, body: RegularizeIn):
    emp = get_employee_or_404(emp_code)
    for _attempt in range(5):
        rec = logs.find_one({"emp_code": emp_code, "date": date})
        if rec is None:
            raise HTTPException(404, "no record for that date")
        new, changes = _plan_regularization(rec, emp, body)
        entry = {"at": now_utc(), "by": body.regularized_by, "reason": body.reason, "changes": changes}

        # Optimistic concurrency: the write only applies if the record still looks exactly as we
        # read it (same inputs, same history length). Otherwise someone else edited it meanwhile:
        # we re-read and re-plan instead of overwriting their history entry.
        guard = {"_id": rec["_id"], "status": rec["status"],
                 "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
        history = rec.get("history")
        if "history" not in rec or history is None:
            guard["history"] = None  # matches missing or null
            update = {"$set": {**new, "history": [entry]}}
        else:
            guard["history"] = {"$size": len(history)}
            update = {"$set": new, "$push": {"history": entry}}
        updated = logs.find_one_and_update(guard, update, return_document=ReturnDocument.AFTER)
        if updated is not None:
            return record_out(updated)
    raise HTTPException(409, "record was modified concurrently; please retry")


# --------------------------------------------------------------------------- #
# 6d. Analytics - pipeline builders (shared by the endpoints AND /admin/explain)
# --------------------------------------------------------------------------- #
_IS_PRESENT = {"$in": ["$status", PRESENCE]}
# $dayOfWeek: 1 = Sunday ... 7 = Saturday, so Mon-Fri = 2..6. `date` is a plain "YYYY-MM-DD" string.
_IS_WEEKDAY = {"$in": [{"$dayOfWeek": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}}, [2, 3, 4, 5, 6]]}
_WEIGHT = {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]}  # R5: half day = 0.5
_LATE = {"$ifNull": ["$late_minutes", 0]}
_PRESENT_DAYS = {"$cond": [{"$and": [_IS_PRESENT, _IS_WEEKDAY]}, _WEIGHT, 0]}  # R7: Mon-Fri only
_PRESENT_ANY_DAY = {"$cond": [_IS_PRESENT, _WEIGHT, 0]}  # trend counts weekends too
_IS_LATE = {"$cond": [{"$gt": [_LATE, 0]}, 1, 0]}


def half_up_expr(decimal_expr, places: int) -> dict:
    """Half-up rounding inside the pipeline. Mongo's $round is half-to-EVEN, so we use
    floor(x * 10^p + 0.5) / 10^p on Decimal128 values (exact, no binary-float noise)."""
    scale = 10 ** places
    return {"$divide": [{"$floor": {"$add": [{"$multiply": [decimal_expr, scale]}, Decimal128("0.5")]}}, scale]}


def monthly_pipeline(emp_code: str, month: str) -> list:
    first, last = month_bounds(month)
    return [
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _PRESENT_DAYS},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "total_late_minutes": {"$sum": _LATE},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


def department_summary_pipeline(month: str, department: Optional[str]) -> list:
    """Runs on `employees` so that people with ZERO logs still produce a row (R9)."""
    first, last = month_bounds(month)
    match: dict = {"joined_on": {"$lte": last.isoformat()}}
    if department:
        match["department"] = department
    per_employee = [  # sub-pipeline: one aggregated row per employee, using the emp_code index
        {"$match": {"$expr": {"$eq": ["$emp_code", "$$code"]},
                    "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": _PRESENT_DAYS},
            # sum + count (not per-employee averages) so the department mean is over RECORDS
            "wh_sum": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}, {"$toDecimal": "$work_hours"}, 0]}},
            "wh_cnt": {"$sum": {"$cond": [{"$and": [_IS_PRESENT, {"$isNumber": "$work_hours"}]}, 1, 0]}},
            "late_count": {"$sum": _IS_LATE},
            "late_total": {"$sum": _LATE},
            "leave": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "on_duty": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
        }},
    ]
    zero = lambda f: {"$sum": {"$ifNull": [f"$m.{f}", 0]}}  # noqa: E731
    return [
        {"$match": match},
        {"$lookup": {"from": "attendance_logs", "let": {"code": "$emp_code"}, "pipeline": per_employee, "as": "m"}},
        {"$unwind": {"path": "$m", "preserveNullAndEmptyArrays": True}},  # keeps employees with no logs
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": zero("present_days"), "wh_sum": zero("wh_sum"), "wh_cnt": zero("wh_cnt"),
            "late_count": zero("late_count"), "late_total": zero("late_total"),
            "leave": zero("leave"), "on_duty": zero("on_duty"),
        }},
        {"$sort": {"_id": 1}},
        {"$project": {
            "_id": 0, "department": "$_id", "headcount": 1, "present_days": 1,
            "avg_work_hours": {"$cond": [{"$gt": ["$wh_cnt", 0]},
                                         {"$toDouble": half_up_expr({"$divide": ["$wh_sum", "$wh_cnt"]}, 2)}, None]},
            "late_count": 1, "total_late_minutes": "$late_total", "leave_count": "$leave", "on_duty_count": "$on_duty",
        }},
    ]


def leaderboard_pipeline(month: str, limit: int, department: Optional[str]) -> list:
    first, last = month_bounds(month)
    pipeline: list = [
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}, "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": "$late_minutes"}, "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "e"}},
        {"$unwind": "$e"},  # logs whose employee does not exist are dropped here
    ]
    if department:
        pipeline.append({"$match": {"e.department": department}})  # rank INSIDE the department
    pipeline += [
        # $rank = standard competition ranking: 1, 2, 2, 4
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},  # limit applied AFTER ranking
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$e.name", "department": "$e.department",
                      "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline


def trend_inputs(department: str, start: str, end: str) -> tuple[list, list]:
    """Small pre-query on `employees`: codes in the department and counts of joiners per join date."""
    rows = list(employees.aggregate([
        {"$match": {"department": department}},
        {"$group": {"_id": "$joined_on", "n": {"$sum": 1}, "codes": {"$push": "$emp_code"}}},
    ]))
    if not rows:
        raise HTTPException(404, "unknown department")
    codes = [c for r in rows for c in r["codes"]]
    joined = [{"d": r["_id"], "n": r["n"]} for r in rows]
    return codes, joined


def validate_trend_range(start: str, end: str) -> None:
    days = (Date.fromisoformat(end) - Date.fromisoformat(start)).days + 1
    if days < 1:
        raise HTTPException(422, "'to' must not be before 'from'")
    if days > 92:
        raise HTTPException(422, "range must not exceed 92 days")


def trend_pipeline(department: str, start: str, end: str, codes: list, joined: list) -> list:
    lower = datetime.combine(Date.fromisoformat(start), time(), tzinfo=UTC)
    upper = datetime.combine(Date.fromisoformat(end), time(), tzinfo=UTC) + timedelta(days=1)  # exclusive
    return [
        {"$match": {"emp_code": {"$in": codes}, "date": {"$gte": start, "$lte": end}}},
        {"$group": {"_id": "$date", "present_count": {"$sum": _PRESENT_ANY_DAY}, "late_count": {"$sum": _IS_LATE}}},
        # Sentinel row for day 1, so $densify has something to anchor on even if the range has NO records.
        {"$unionWith": {"coll": "employees", "pipeline": [
            {"$match": {"department": department}}, {"$limit": 1},
            {"$replaceWith": {"_id": start, "present_count": 0, "late_count": 0}}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"}, "late_count": {"$sum": "$late_count"}}},
        {"$addFields": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [lower, upper]}}},  # one row per day
        {"$sort": {"d": 1}},
        {"$addFields": {
            "date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
            "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]},
            "present_count": {"$ifNull": ["$present_count", 0]},
            "late_count": {"$ifNull": ["$late_count", 0]},
        }},
        {"$addFields": {"headcount": {"$sum": {"$map": {
            "input": {"$filter": {"input": {"$literal": joined}, "as": "j", "cond": {"$lte": ["$$j.d", "$date"]}}},
            "as": "j", "in": "$$j.n"}}}}},
        {"$addFields": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up_expr({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            # $avg skips nulls; window = current row + up to 6 earlier rows inside the range
            "moving_avg_7d": {"$avg": "$attendance_rate", "window": {"documents": [-6, "current"]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$isNumber": "$attendance_rate"}, {"$toDouble": "$attendance_rate"}, None]},
            "moving_avg_7d": {"$cond": [{"$isNumber": "$moving_avg_7d"},
                                        {"$toDouble": half_up_expr("$moving_avg_7d", 4)}, None]},
        }},
    ]


# --------------------------------------------------------------------------- #
# 6e. Analytics endpoints
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: MonthQuery):
    emp = get_employee_or_404(emp_code)
    first, last = month_bounds(month)
    working_days = count_working_days(first, last, Date.fromisoformat(emp["joined_on"]))
    agg = next(logs.aggregate(monthly_pipeline(emp_code, month)), None) or {}
    present = float(agg.get("present_days", 0))
    pct = round_half_up(Decimal(str(present)) / Decimal(working_days) * 100, 2) if working_days else None
    return {
        "emp_code": emp_code, "month": month, "working_days": working_days,
        "present_days": present, "leave_days": agg.get("leave_days", 0),
        "late_count": agg.get("late_count", 0),
        "total_late_minutes": agg.get("total_late_minutes", 0),
        "total_overtime_minutes": agg.get("total_overtime_minutes", 0),
        "attendance_pct": pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(month: MonthQuery, department: Optional[str] = None):
    items = list(employees.aggregate(department_summary_pipeline(month, department)))
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: MonthQuery, limit: Annotated[int, Query(ge=1, le=50)] = 10, department: Optional[str] = None):
    return {"month": month, "items": list(logs.aggregate(leaderboard_pipeline(month, limit, department)))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: FromQuery, to: DateStr):
    validate_trend_range(from_, to)
    codes, joined = trend_inputs(department, from_, to)
    return {"department": department, "items": list(logs.aggregate(trend_pipeline(department, from_, to, codes, joined)))}


# --------------------------------------------------------------------------- #
# 6f. Explain: same builders as the real endpoints, so the plan shown is the plan used
# --------------------------------------------------------------------------- #
def _need(**params):
    missing = [k for k, v in params.items() if v is None]
    if missing:
        raise HTTPException(422, f"missing required query parameter(s): {', '.join(missing)}")


def _explain_aggregate(collection: str, pipeline: list) -> dict:
    return db.command({"explain": {"aggregate": collection, "pipeline": pipeline, "cursor": {}},
                       "verbosity": "executionStats"})


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: OptMonthQuery = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[DateStr] = None,
    date_to: Optional[DateStr] = None,
    status: Optional[Status] = None,
    from_: OptFromQuery = None,
    to: Optional[DateStr] = None,
    page: Page = 1,
    page_size: PageSize = 20,
):
    if endpoint == "attendance_list":
        q = build_attendance_query(emp_code, date_from, date_to, status)
        result = db.command({"explain": {"find": "attendance_logs", "filter": q,
                                         "sort": dict(ATTENDANCE_SORT),
                                         "skip": (page - 1) * page_size, "limit": page_size},
                             "verbosity": "executionStats"})
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        _need(emp_code=emp_code, month=month)
        result, collection = _explain_aggregate("attendance_logs", monthly_pipeline(emp_code, month)), "attendance_logs"
    elif endpoint == "department_summary":
        _need(month=month)
        result, collection = _explain_aggregate("employees", department_summary_pipeline(month, department)), "employees"
    elif endpoint == "late_leaderboard":
        _need(month=month)
        result = _explain_aggregate("attendance_logs", leaderboard_pipeline(month, limit, department))
        collection = "attendance_logs"
    else:  # department_trend
        _need(department=department, **{"from": from_}, to=to)
        validate_trend_range(from_, to)
        codes, joined = trend_inputs(department, from_, to)
        result = _explain_aggregate("attendance_logs", trend_pipeline(department, from_, to, codes, joined))
        collection = "attendance_logs"

    # explain output contains ObjectIds / dates / Int64: make it JSON-safe
    safe = json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))
    return {"endpoint": endpoint, "collection": collection, "explain": safe}
