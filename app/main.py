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
from datetime import date,datetime, timedelta, timezone,time
from typing import Annotated, Literal, Optional
from decimal import Decimal, ROUND_HALF_UP  
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, field_validator, model_validator
from contextlib import asynccontextmanager
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError
from fastapi.exceptions import RequestValidationError

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
    return work_hours is not None and work_hours < 4.5                           # R3: 30-minute minimum                           

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
