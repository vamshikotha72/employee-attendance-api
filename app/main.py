"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from datetime import date,datetime, timedelta, timezone,time
from typing import Literal, Optional
from decimal import Decimal, ROUND_HALF_UP  
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from contextlib import asynccontextmanager
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

def ensure_indexes() -> None:
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", DESCENDING)], unique=True, name="uniq_emp_date"
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


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    if db.employees.find_one({"emp_code": body.emp_code}):
        raise HTTPException(409, "emp_code already exists")
    doc = body.model_dump()
    doc["created_at"] = datetime.now()
    db.employees.insert_one(doc)
    doc.pop("_id", None)
    return doc


@app.get("/employees")
def list_employees(department: Optional[str] = None, page: int = 1, page_size: int = 20):
    q = {}
    if department:
        q["department"] = department
    skip = page * page_size
    total = db.employees.count_documents({})
    items = list(db.employees.find(q, {"_id": 0}).skip(skip).limit(page_size))
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = datetime.fromtimestamp(body.punched_at // 1000, tz=timezone.utc) if body.punched_at else datetime.now(timezone.utc).replace(microsecond=0)
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
        res = db.attendance_logs.insert_one(doc)      # the unique index decides, not a prior lookup
    except DuplicateKeyError:
        raise HTTPException(409, "already punched in for this date")
    res = db.attendance_logs.insert_one(doc)
    doc["id"] = str(res.inserted_id)
    doc.pop("_id", None)
    return doc


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = 1,
    page_size: int = 20,
):
    q = {}
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
    docs = list(db.attendance_logs.find(q))
    docs.sort(key=lambda d: d["date"], reverse=True)
    total = len(docs)
    page_docs = docs[(page - 1) * page_size : page * page_size]
    for d in page_docs:
        d["id"] = str(d.pop("_id"))
    return {"items": page_docs, "total": total, "page": page, "page_size": page_size}


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
