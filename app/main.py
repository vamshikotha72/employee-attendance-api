"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
import calendar
from datetime import date,datetime, timedelta, timezone,time
from typing import Annotated, Literal, Optional
from decimal import Decimal, ROUND_HALF_UP  
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from contextlib import asynccontextmanager
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError
from fastapi.exceptions import RequestValidationError
import json
from bson import json_util

load_dotenv()

client = MongoClient(
    os.getenv("MONGO_URI", "mongodb://localhost:27017"),
    tz_aware=True,                 # aware UTC datetimes, never naive
    serverSelectionTimeoutMS=5000,
)
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
Status = Literal["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"]

def ensure_indexes() -> None:
    # uniqueness is enforced by the database (defects 12 and 17)
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="uniq_emp_code")
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)], name="dept_emp_code"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="uniq_emp_date"
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)], name="date_emp"
    )
    db.attendance_logs.create_index(
        [("status", ASCENDING), ("date", DESCENDING), ("emp_code", ASCENDING)],
        name="status_date_emp",
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("punch_in", DESCENDING)], name="emp_punch_in"
    )
    db.employees.create_index(
        [("joined_on", ASCENDING), ("department", ASCENDING)], name="joined_dept"
    )


@asynccontextmanager
async def lifespan(_app: FastAPI):
    ensure_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)




# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def parse_hhmm(value: str):
    h, m = map(int, value.split(":"))
    return h, m




def shift_window(shift_date: date, shift_start: str, shift_end: str):
    """Aware IST (start, end) of the shift that began on shift_date.
    The end falls on the next calendar day for an overnight shift (R1)."""
    sh, sm = parse_hhmm(shift_start)
    eh, em = parse_hhmm(shift_end)
    start = datetime.combine(shift_date, time(sh, sm), tzinfo=IST)
    end = datetime.combine(shift_date, time(eh, em), tzinfo=IST)
    if (eh, em) <= (sh, sm):
        end += timedelta(days=1)
    return start, end

def attendance_date(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    """R1: the IST calendar date a punch-in belongs to."""
    local = punch_in.astimezone(IST)
    day = local.date()
    eh, em = parse_hhmm(shift_end)
    # overnight shift and punch-in earlier than shift end -> shift began the previous day
    if parse_hhmm(shift_end) <= parse_hhmm(shift_start) and \
            (local.hour, local.minute, local.second) < (eh, em, 0):
        day -= timedelta(days=1)
    return day


def compute_late_minutes(punch_in: datetime, shift_start: str, shift_end: str) -> int:
    if punch_in.tzinfo is None:                                      # PyMongo returns naive UTC
        punch_in = punch_in.replace(tzinfo=timezone.utc)
    shift_day = attendance_date(punch_in, shift_start, shift_end)
    start, _ = shift_window(shift_day, shift_start, shift_end)
    seconds = int((punch_in - start).total_seconds())               # compare seconds, not floored minutes
    return seconds // 60 if seconds > 600 else 0                     # R2: strictly more than 10:00


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int((punch_out - punch_in).total_seconds())
    hours = (Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(hours)                                              # R4: half-up, 2 decimals


def compute_overtime(punch_out: datetime, shift_start: str, shift_end: str, date_str: str) -> int:
    if punch_out.tzinfo is None:                                     # PyMongo returns naive UTC
        punch_out = punch_out.replace(tzinfo=timezone.utc)
    _, end = shift_window(date.fromisoformat(date_str), shift_start, shift_end)
    minutes = max(0, int((punch_out - end).total_seconds() / 60))   # floor, never negative
    return minutes if minutes >= 30 else 0                           # R3: 30-minute minimum


def compute_half_day(work_hours) -> bool:
    """R5: true when the ROUNDED work_hours is below 4.50. Call it after punch-out."""
    return work_hours is not None and work_hours < 4.5                                                    

EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)


def dt_to_ms(value: datetime) -> int:
    """Datetime -> epoch milliseconds (integer arithmetic). Naive values are UTC, as Mongo stores them."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (value - EPOCH) // timedelta(milliseconds=1)


def _ms_if_datetime(value):
    return dt_to_ms(value) if isinstance(value, datetime) else value


def history_out(entries) -> list:
    """History times (and punch times nested in `changes`) -> epoch milliseconds."""
    out = []
    for e in entries or []:
        changes = {
            field: {"from": _ms_if_datetime(v.get("from")), "to": _ms_if_datetime(v.get("to"))}
            for field, v in (e.get("changes") or {}).items()
        }
        out.append({"at": dt_to_ms(e["at"]), "by": e.get("by"), "reason": e.get("reason"), "changes": changes})
    return out


def attendance_out(doc: dict) -> dict:
    """Stored attendance document -> API record: no _id / id, instants as epoch milliseconds."""
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": _ms_if_datetime(doc.get("punch_in")),
        "punch_out": _ms_if_datetime(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out(doc.get("history")),
    }

def employee_out(doc: dict) -> dict:
    """Stored employee document -> API record (no _id; created_at as epoch milliseconds)."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": _ms_if_datetime(doc.get("created_at")),
    }
_DATE_RE = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
_HHMM = r"^([01][0-9]|2[0-3]):[0-5][0-9]$"   # [0-9], not \d: \d also matches non-ASCII digits


def parse_date_str(value: str) -> date:
    """Strict YYYY-MM-DD (date.fromisoformat alone also accepts e.g. '20260706')."""
    if not _DATE_RE.fullmatch(value):
        raise ValueError("date must be formatted YYYY-MM-DD")
    return date.fromisoformat(value)   # raises ValueError for impossible dates (2026-02-30)

def query_date(name: str, value: Optional[str]) -> Optional[date]:
    """Validate a date query parameter; a bad value becomes FastAPI's standard 422."""
    if value is None:
        return None
    try:
        return parse_date_str(value)
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", name), "msg": str(exc), "input": value}]
        )

def month_bounds(month: str) -> tuple:
    """'2026-07' -> (date(2026, 7, 1), date(2026, 7, 31))."""
    year, mon = int(month[:4]), int(month[5:7])
    if year < 1:
        raise ValueError("year must be at least 0001")      # datetime cannot represent year 0000
    return date(year, mon, 1), date(year, mon, calendar.monthrange(year, mon)[1])


def count_working_days(start: date, end: date) -> int:
    """R7: Monday-Friday days from start to end inclusive (no holiday calendar); 0 if start > end."""
    days = (end - start).days + 1
    if days <= 0:
        return 0
    full_weeks, extra = divmod(days, 7)
    first_weekday = start.weekday()                          # Monday = 0 ... Sunday = 6
    return full_weeks * 5 + sum(1 for i in range(extra) if (first_weekday + i) % 7 < 5)


def round_half_up(value, places: int) -> float:
    """R8: half-up rounding (not banker's, not float rounding), via Decimal."""
    return float(Decimal(str(value)).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP))


def employee_monthly_pipeline(emp_code: str, first: date, last: date) -> list:
    """Aggregation for one employee and one month. Reads the STORED derived values (never recomputes)."""
    # present_days only counts records on Mon-Fri (R7); weekend records still add to the other totals.
    weekdays = [
        (first + timedelta(days=i)).isoformat()
        for i in range((last - first).days + 1)
        if (first + timedelta(days=i)).weekday() < 5
    ]
    present_value = {
        "$cond": [
            {"$and": [{"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},    # R6
                      {"$in": ["$date", weekdays]}]},
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},   # R5: half day = 0.5
            0,
        ]
    }
    return [
        # served by the unique (emp_code, date) index: IXSCAN, no collection scan
        {"$match": {"emp_code": emp_code, "date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
        {"$group": {
            "_id": None,
            "present_days": {"$sum": present_value},
            "leave_days": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
            "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
        }},
    ]


PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]                     # R6


def department_summary_pipeline(first: date, last: date, department: Optional[str] = None) -> list:
    weekdays = [
        (first + timedelta(days=i)).isoformat()
        for i in range((last - first).days + 1)
        if (first + timedelta(days=i)).weekday() < 5
    ]
    is_present = {"$in": ["$status", PRESENCE_STATUSES]}
    present_value = {
        "$cond": [
            {"$and": [is_present, {"$in": ["$date", weekdays]}]},
            {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},
            0,
        ]
    }
    has_hours = {"$and": [is_present, {"$isNumber": "$work_hours"}]}
    late = {"$ifNull": ["$late_minutes", 0]}

    emp_match = {"joined_on": {"$lte": last.isoformat()}}              # R9
    if department is not None:
        emp_match["department"] = department

    return [
        {"$match": emp_match},
        {"$lookup": {
            "from": "attendance_logs",
            "localField": "emp_code",
            "foreignField": "emp_code",
            "pipeline": [
                {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
                {"$group": {
                    "_id": None,
                    "present_days": {"$sum": present_value},
                    "hours_sum": {"$sum": {"$cond": [has_hours, {"$toDecimal": "$work_hours"}, 0]}},
                    "hours_n": {"$sum": {"$cond": [has_hours, 1, 0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [late, 0]}, 1, 0]}},
                    "total_late_minutes": {"$sum": late},
                    "leave_count": {"$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}},
                    "on_duty_count": {"$sum": {"$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]}},
                }},
            ],
            "as": "logs",
        }},
        {"$set": {"s": {"$ifNull": [{"$first": "$logs"}, {}]}}},
        {"$group": {
            "_id": "$department",
            "headcount": {"$sum": 1},
            "present_days": {"$sum": {"$ifNull": ["$s.present_days", 0]}},
            "hours_sum": {"$sum": {"$ifNull": ["$s.hours_sum", 0]}},
            "hours_n": {"$sum": {"$ifNull": ["$s.hours_n", 0]}},
            "late_count": {"$sum": {"$ifNull": ["$s.late_count", 0]}},
            "total_late_minutes": {"$sum": {"$ifNull": ["$s.total_late_minutes", 0]}},
            "leave_count": {"$sum": {"$ifNull": ["$s.leave_count", 0]}},
            "on_duty_count": {"$sum": {"$ifNull": ["$s.on_duty_count", 0]}},
        }},
        {"$sort": {"_id": 1}},
    ]


def _to_decimal(value) -> Decimal:
    return value.to_decimal() if hasattr(value, "to_decimal") else Decimal(str(value))

def _month_range_or_422(month: str) -> tuple:
    """'YYYY-MM' -> (first_day, last_day); impossible values (e.g. year 0000) become a 422."""
    try:
        return month_bounds(month)
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "month"), "msg": str(exc), "input": month}]
        )


def late_leaderboard_pipeline(first: date, last: date, limit: int, department: Optional[str] = None) -> list:
    """Late-comers ranking. Reads the STORED late_minutes.
    $rank = standard competition ranking (1, 2, 2, 4); `limit` is applied AFTER ranking."""
    pipeline = [
        # served by the (date, emp_code) index: IXSCAN on the month's date range
        {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()},
                    "late_minutes": {"$gt": 0}}},
        {"$group": {"_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "emp"}},
        {"$unwind": "$emp"},                                   # logs of unknown employees are dropped
    ]
    if department is not None:
        pipeline.append({"$match": {"emp.department": department}})   # rank inside the department only
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},       # ties: emp_code ascending
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$emp.name",
                      "department": "$emp.department", "total_late_minutes": 1, "late_count": 1}},
    ]
    return pipeline

_PRESENT_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]               # R6
MAX_TREND_DAYS = 92


def _trend_inputs(department: str, from_: str, to: str):
    """Validate the range (422) and load the department's employees (404 if none)."""
    start, end = query_date("from", from_), query_date("to", to)
    if end < start:
        raise _422(("query", "to"), "to must not be before from", to)
    if (end - start).days + 1 > MAX_TREND_DAYS:
        raise _422(("query", "to"), f"range must not exceed {MAX_TREND_DAYS} days", to)
    emps = list(db.employees.find({"department": department}, {"_id": 0, "emp_code": 1, "joined_on": 1}))
    if not emps:
        raise HTTPException(404, "department not found")
    return start, end, [e["emp_code"] for e in emps], [e["joined_on"] for e in emps]


def department_trend_pipeline(codes: list, joins: list, start: date, end: date) -> list:
    """One row per calendar day from `start` to `end`: $group per day, $densify fills the gaps,
    $setWindowFields gives the 7-day moving average. Reads the STORED late_minutes / half_day.
    Rates are kept as integer units of 1/10000 so half-up rounding is exact."""
    start_dt = datetime(start.year, start.month, start.day, tzinfo=timezone.utc)
    end_excl = datetime(end.year, end.month, end.day, tzinfo=timezone.utc) + timedelta(days=1)
    zero = {"present_count": 0, "late_count": 0}
    return [
        # IXSCAN on (emp_code, date): the department's employees within the date range
        {"$match": {"emp_code": {"$in": codes},
                    "date": {"$gte": start.isoformat(), "$lte": end.isoformat()}}},
        {"$group": {"_id": "$date",
                    "present_count": {"$sum": {"$cond": [
                        {"$in": ["$status", _PRESENT_STATUSES]},
                        {"$cond": [{"$eq": [{"$ifNull": ["$half_day", False]}, True]}, 0.5, 1]},   # R5, R6
                        0]}},
                    "late_count": {"$sum": {"$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]}}}},
        # make sure both ends of the range exist (even with no records at all), then merge them in
        {"$unionWith": {"pipeline": [{"$documents": [
            {"_id": start.isoformat(), **zero}, {"_id": end.isoformat(), **zero}]}]}},
        {"$group": {"_id": "$_id", "present_count": {"$sum": "$present_count"},
                    "late_count": {"$sum": "$late_count"}}},
        {"$set": {"d": {"$dateFromString": {"dateString": "$_id", "format": "%Y-%m-%d"}}}},
        {"$densify": {"field": "d", "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_excl]}}},
        {"$set": {"date": {"$dateToString": {"date": "$d", "format": "%Y-%m-%d"}},
                  "present_count": {"$ifNull": ["$present_count", 0]},
                  "late_count": {"$ifNull": ["$late_count", 0]},
                  "is_working_day": {"$in": [{"$dayOfWeek": "$d"}, [2, 3, 4, 5, 6]]}}},    # Mon-Fri
        {"$set": {"headcount": {"$size": {"$filter": {                                      # R9
            "input": {"$literal": joins}, "as": "j", "cond": {"$lte": ["$$j", "$date"]}}}}}},
        # attendance_rate in 1/10000 units, half-up: floor((present*10000 + h/2) / h) in integers
        {"$set": {"rate_units": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            {"$floor": {"$divide": [
                {"$add": [{"$multiply": [{"$multiply": ["$present_count", 2]}, 10000]}, "$headcount"]},
                {"$multiply": ["$headcount", 2]}]}},
            None]}}},
        {"$set": {"has_rate": {"$cond": [{"$ne": ["$rate_units", None]}, 1, 0]}}},
        {"$setWindowFields": {"sortBy": {"d": 1}, "output": {
            "ru_sum": {"$sum": "$rate_units", "window": {"documents": [-6, 0]}},
            "ru_n": {"$sum": "$has_rate", "window": {"documents": [-6, 0]}}}}},
        {"$project": {
            "_id": 0, "date": 1, "is_working_day": 1, "headcount": 1, "present_count": 1, "late_count": 1,
            "attendance_rate": {"$cond": [{"$ne": ["$rate_units", None]}, {"$divide": ["$rate_units", 10000]}, None]},
            "moving_avg_7d": {"$cond": [{"$gt": ["$ru_n", 0]},
                {"$divide": [{"$floor": {"$divide": [
                    {"$add": [{"$multiply": ["$ru_sum", 2]}, "$ru_n"]}, {"$multiply": ["$ru_n", 2]}]}}, 10000]},
                None]}}},
        {"$sort": {"date": 1}},
    ]

def _explain(command_body: dict) -> dict:
    """Run explain (executionStats) and return it as plain JSON (BSON types made JSON-safe)."""
    result = db.command({"explain": command_body, "verbosity": "executionStats"})
    return json.loads(json_util.dumps(result, json_options=json_util.RELAXED_JSON_OPTIONS))

# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP[0-9]{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=_HHMM)
    shift_end: str = Field(default="18:30", pattern=_HHMM)
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def _valid_joined_on(cls, v: str) -> str:
        parse_date_str(v)
        return v

    @model_validator(mode="after")
    def _shift_times_differ(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


MIN_EPOCH_MS = 100_000_000_000     # below this a value looks like seconds -> 422
MAX_EPOCH_MS = 4_102_444_800_000   # 2100-01-01T00:00:00Z
EpochMillis = Annotated[int, Field(strict=True, ge=MIN_EPOCH_MS, le=MAX_EPOCH_MS)]


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")          # 200 only if MongoDB answers
    except PyMongoError:
        raise HTTPException(503, "database unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    now = datetime.now(timezone.utc)
    doc = body.model_dump()
    # MongoDB keeps milliseconds; truncate so the value we return equals the stored one.
    doc["created_at"] = now.replace(microsecond=now.microsecond // 1000 * 1000)
    try:
        db.employees.insert_one(doc)                 # the unique index decides, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_out(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,):
    q = {}
    if department:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)          # total honours the filter
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    return {"items": [employee_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc) if body.punched_at is not None else datetime.now(timezone.utc).replace(microsecond=0)
    d = attendance_date(ts, emp["shift_start"], emp["shift_end"]).isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(ts, emp["shift_start"], emp["shift_end"]),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)            # the unique index decides, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    return attendance_out(doc)
   
    
class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[EpochMillis] = None     # epoch ms; omitted -> now


def _body_422(field: str, msg: str, value=None) -> RequestValidationError:
    """422 in FastAPI's default body shape: {"detail": [ {...} ]} (as openapi.yaml ValidationError)."""
    return RequestValidationError(
        [{"type": "value_error", "loc": ("body", field), "msg": msg, "input": value}]
    )


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")

    # R1: whole seconds only.
    out_ts = (
        datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc)
        if body.punched_at is not None
        else datetime.now(timezone.utc).replace(microsecond=0)
    )

    # The employee's most recent record whose punch_in is at or before punched_at. Matching on
    # punch_in (not on the calendar date) is what makes this work across midnight.
    rec = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$ne": None, "$lte": out_ts}},
        sort=[("punch_in", DESCENDING)],
    )
    if rec is None:
        raise HTTPException(404, "no punch-in found")
    if rec.get("punch_out") is not None:
        raise HTTPException(409, "already punched out")

    punch_in_ts = rec["punch_in"]
    if punch_in_ts.tzinfo is None:
        punch_in_ts = punch_in_ts.replace(tzinfo=timezone.utc)
    punch_in_ts = punch_in_ts.replace(microsecond=0)
    if out_ts <= punch_in_ts:
        raise _body_422("punched_at", "punched_at must be after punch_in", body.punched_at)
    if out_ts - punch_in_ts > timedelta(hours=24):
        raise _body_422("punched_at", "punched_at must be within 24 hours of punch_in", body.punched_at)

    work_hours = compute_work_hours(punch_in_ts, out_ts)                                   # R4
    overtime = compute_overtime(out_ts, emp["shift_start"], emp["shift_end"], rec["date"])  # R3
    half_day = compute_half_day(work_hours)                                                # R5

    # Race guard: the filter only matches while the record is still open, so of several
    # simultaneous punch-outs exactly one update succeeds and the others get 409.
    updated = db.attendance_logs.find_one_and_update(
        {"_id": rec["_id"], "punch_out": None},
        {"$set": {"punch_out": out_ts, "work_hours": work_hours,
                  "overtime_minutes": overtime, "half_day": half_day}},
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(409, "already punched out")
    return attendance_out(updated)


class RegularizeIn(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def _check_body(self) -> "RegularizeIn":
        # The schema types are not nullable: an explicit null is invalid, an omitted field is "unchanged".
        for name in ("status", "punch_in", "punch_out"):
            if name in self.model_fields_set and getattr(self, name) is None:
                raise ValueError(f"{name} must not be null")
        # ABSENT / LEAVE clear both punch times, so supplying one with them is a contradiction.
        if self.status in ("ABSENT", "LEAVE") and (self.punch_in is not None or self.punch_out is not None):
            raise ValueError("status ABSENT/LEAVE clears both punch times: do not supply punch_in or punch_out")
        return self


def _aware(value: Optional[datetime]) -> Optional[datetime]:
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _422(loc: tuple, msg: str, value=None) -> RequestValidationError:
    return RequestValidationError([{"type": "value_error", "loc": loc, "msg": msg, "input": value}])


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(emp_code: str, date: str, body: RegularizeIn):
    # NB: the path parameter is called `date` (as in the contract), so the `date` class is not
    # usable inside this function; parse_date_str() is a module-level helper and is unaffected.
    try:
        parse_date_str(date)
    except ValueError as exc:
        raise _422(("path", "date"), str(exc), date)

    emp = db.employees.find_one({"emp_code": emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    rec = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if rec is None:
        raise HTTPException(404, "attendance record not found")

    old = {
        "status": rec["status"],
        "punch_in": _aware(rec.get("punch_in")),
        "punch_out": _aware(rec.get("punch_out")),
        "work_hours": rec.get("work_hours"),
        "late_minutes": rec.get("late_minutes", 0),
        "overtime_minutes": rec.get("overtime_minutes", 0),
        "half_day": rec.get("half_day", False),
    }

    # ---- apply the requested change (omitted fields stay as they are) ------------------------
    new_status = body.status if body.status is not None else old["status"]
    if new_status in ("ABSENT", "LEAVE"):
        new_in, new_out = None, None                       # clears both punch times
    else:
        new_in = (datetime.fromtimestamp(body.punch_in // 1000, tz=timezone.utc)    # R1: whole seconds
                  if body.punch_in is not None else old["punch_in"])
        new_out = (datetime.fromtimestamp(body.punch_out // 1000, tz=timezone.utc)
                   if body.punch_out is not None else old["punch_out"])
        if new_in is None:
            raise _422(("body", "punch_in"), "a presence status requires a punch_in")
        if body.punch_in is not None and \
                attendance_date(new_in, emp["shift_start"], emp["shift_end"]).isoformat() != date:
            raise _422(("body", "punch_in"), "punch_in must stay on the record's attendance date", body.punch_in)
        if (body.punch_in is not None or body.punch_out is not None) and new_out is not None:
            if new_out <= new_in:
                raise _422(("body", "punch_out"), "punch_out must be after punch_in", body.punch_out)
            if new_out - new_in > timedelta(hours=24):
                raise _422(("body", "punch_out"), "punch_out must be within 24 hours of punch_in", body.punch_out)

    # A request that changes nothing is invalid.
    if (new_status, new_in, new_out) == (old["status"], old["punch_in"], old["punch_out"]):
        raise _422(("body",), "the request does not change anything")

    # ---- recompute the derived fields from the FINAL state (R2-R5) ---------------------------
    if new_status in ("ABSENT", "LEAVE"):
        work_hours, late, overtime, half_day = None, 0, 0, False
    else:
        calc_in = new_in.replace(microsecond=0)
        late = compute_late_minutes(calc_in, emp["shift_start"], emp["shift_end"])
        if new_out is None:
            work_hours, overtime, half_day = None, 0, False
        else:
            calc_out = new_out.replace(microsecond=0)
            work_hours = compute_work_hours(calc_in, calc_out)
            overtime = compute_overtime(calc_out, emp["shift_start"], emp["shift_end"], date)
            half_day = compute_half_day(work_hours)
    new = {"status": new_status, "punch_in": new_in, "punch_out": new_out, "work_hours": work_hours,
           "late_minutes": late, "overtime_minutes": overtime, "half_day": half_day}

    # history: one key per field that ACTUALLY changed
    changes = {k: {"from": old[k], "to": new[k]} for k in new if new[k] != old[k]}
    now = datetime.now(timezone.utc)
    entry = {"at": now.replace(microsecond=now.microsecond // 1000 * 1000),
             "by": body.regularized_by, "reason": body.reason, "changes": changes}

    # ---- write: optimistic concurrency ----------------------------------------------------------
    # The update only applies if the record is still exactly as we read it (same status, same punch
    # times, same number of history entries). Of two simultaneous edits one wins; the loser matches
    # nothing and gets 409, so a history entry can never be silently lost or overwritten.
    n_hist = len(rec.get("history") or [])
    guard = {"_id": rec["_id"], "status": old["status"],
             "punch_in": rec.get("punch_in"), "punch_out": rec.get("punch_out")}
    if n_hist == 0:
        guard["$or"] = [{"history": {"$exists": False}}, {"history": {"$size": 0}}]
    else:
        guard["history"] = {"$size": n_hist}
    updated = db.attendance_logs.find_one_and_update(
        guard, {"$set": new, "$push": {"history": entry}}, return_document=ReturnDocument.AFTER
    )
    if updated is None:
        raise HTTPException(409, "the record was modified concurrently; please retry")
    return attendance_out(updated)

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    d_from = query_date("date_from", date_from)
    d_to = query_date("date_to", date_to)
    if d_from and d_to and d_from > d_to:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "date_from"),
              "msg": "date_from must not be after date_to", "input": date_from}]
        )
    q = {}
    if emp_code:
        q["emp_code"] = emp_code
    if d_from or d_to:
        q["date"] = {}
        if d_from:
            q["date"]["$gte"] = d_from.isoformat()
        if d_to:
            q["date"]["$lte"] = d_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])   # total order -> stable pages
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {"items": [attendance_out(d) for d in cursor], "total": total, "page": page, "page_size": page_size}


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")],     # required, YYYY-MM (else 422)
):
    try:
        first, last = month_bounds(month)
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "month"), "msg": str(exc), "input": month}]
        )
    emp = db.employees.find_one({"emp_code": emp_code}, {"joined_on": 1})
    if emp is None:
        raise HTTPException(404, "employee not found")

    rows = list(db.attendance_logs.aggregate(employee_monthly_pipeline(emp_code, first, last)))
    agg = rows[0] if rows else {}                      # no records in the month -> all zeros

    present_days = float(agg.get("present_days", 0))
    # R7: working days (Mon-Fri) are counted from joined_on for someone who joined mid-month.
    working_days = count_working_days(max(first, date.fromisoformat(emp["joined_on"])), last)
    attendance_pct = (
        None if working_days == 0
        else round_half_up(Decimal(str(present_days)) / Decimal(working_days) * 100, 2)   # R8
    )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days, 2),
        "leave_days": int(agg.get("leave_days", 0)),
        "late_count": int(agg.get("late_count", 0)),
        "total_late_minutes": int(agg.get("total_late_minutes", 0)),
        "total_overtime_minutes": int(agg.get("total_overtime_minutes", 0)),
        "attendance_pct": attendance_pct,
    }

@app.get("/analytics/departments/summary")
def department_summary(
    month: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")],
    department: Optional[str] = None,
):
    try:
        first, last = month_bounds(month)
    except ValueError as exc:
        raise RequestValidationError(
            [{"type": "value_error", "loc": ("query", "month"), "msg": str(exc), "input": month}]
        )

    items = []
    for row in db.employees.aggregate(department_summary_pipeline(first, last, department)):
        n = int(row["hours_n"])
        avg_hours = None if n == 0 else round_half_up(_to_decimal(row["hours_sum"]) / Decimal(n), 2)
        items.append({
            "department": row["_id"],
            "headcount": int(row["headcount"]),
            "present_days": round_half_up(_to_decimal(row["present_days"]), 2),
            "avg_work_hours": avg_hours,
            "late_count": int(row["late_count"]),
            "total_late_minutes": int(row["total_late_minutes"]),
            "leave_count": int(row["leave_count"]),
            "on_duty_count": int(row["on_duty_count"]),
        })
    return {"month": month, "items": items}

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: Annotated[str, Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")],
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    department: Optional[str] = None,
):
    first, last = _month_range_or_422(month)
    items = [
        {"rank": int(r["rank"]), "emp_code": r["emp_code"], "name": r["name"],
         "department": r["department"], "total_late_minutes": int(r["total_late_minutes"]),
         "late_count": int(r["late_count"])}
        for r in db.attendance_logs.aggregate(late_leaderboard_pipeline(first, last, limit, department))
    ]
    return {"month": month, "items": items}

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_: Annotated[str, Query(alias="from")],
    to: Annotated[str, Query()],
):
    start, end, codes, joins = _trend_inputs(department, from_, to)
    items = [
        {"date": r["date"], "is_working_day": bool(r["is_working_day"]), "headcount": int(r["headcount"]),
         "present_count": float(r["present_count"]), "late_count": int(r["late_count"]),
         "attendance_rate": r["attendance_rate"], "moving_avg_7d": r["moving_avg_7d"]}
        for r in db.attendance_logs.aggregate(department_trend_pipeline(codes, joins, start, end))
    ]
    return {"department": department, "items": items}

@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal["attendance_list", "employee_monthly", "department_summary",
                      "late_leaderboard", "department_trend"],
    emp_code: Optional[str] = None,
    month: Annotated[Optional[str], Query(pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$")] = None,
    department: Optional[str] = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[Status] = None,
    from_: Annotated[Optional[str], Query(alias="from")] = None,
    to: Optional[str] = None,
    page: Annotated[int, Query(ge=1, le=1_000_000_000)] = 1,
    page_size: Annotated[int, Query(ge=1, le=100)] = 20,
):
    def need(name: str, value):
        if value is None:
            raise _422(("query", name), f"{name} is required for {endpoint}")

    if endpoint == "attendance_list":
        # built exactly like GET /attendance
        d_from, d_to = query_date("date_from", date_from), query_date("date_to", date_to)
        if d_from and d_to and d_from > d_to:
            raise _422(("query", "date_from"), "date_from must not be after date_to", date_from)
        q = {}
        if emp_code:
            q["emp_code"] = emp_code
        if d_from or d_to:
            q["date"] = {}
            if d_from:
                q["date"]["$gte"] = d_from.isoformat()
            if d_to:
                q["date"]["$lte"] = d_to.isoformat()
        if status:
            q["status"] = status
        collection = "attendance_logs"
        body = {"find": collection, "filter": q, "sort": {"date": -1, "emp_code": 1},
                "skip": (page - 1) * page_size, "limit": page_size}

    elif endpoint == "employee_monthly":
        need("emp_code", emp_code)
        need("month", month)
        first, last = _month_range_or_422(month)
        collection = "attendance_logs"
        body = {"aggregate": collection, "pipeline": employee_monthly_pipeline(emp_code, first, last), "cursor": {}}

    elif endpoint == "department_summary":
        need("month", month)
        first, last = _month_range_or_422(month)
        collection = "employees"
        body = {"aggregate": collection, "pipeline": department_summary_pipeline(first, last, department),
                "cursor": {}}

    elif endpoint == "late_leaderboard":
        need("month", month)
        first, last = _month_range_or_422(month)
        collection = "attendance_logs"
        body = {"aggregate": collection, "pipeline": late_leaderboard_pipeline(first, last, limit, department),
                "cursor": {}}

    else:  # department_trend
        need("department", department)
        need("from", from_)
        need("to", to)
        start, end, codes, joins = _trend_inputs(department, from_, to)
        collection = "attendance_logs"
        body = {"aggregate": collection, "pipeline": department_trend_pipeline(codes, joins, start, end),
                "cursor": {}}

    return {"endpoint": endpoint, "collection": collection, "explain": _explain(body)}

# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
