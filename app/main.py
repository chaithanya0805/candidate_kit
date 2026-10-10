"""
Employee Attendance & Analytics API

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (read from environment, local .env is fallback)
"""
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum
import json
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query, status
from pydantic import BaseModel, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, tz_aware=True, serverSelectionTimeoutMS=2000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))


def init_indexes():
    """Idempotently creates all required indexes at application startup."""
    try:
        client.admin.command("ping")
    except Exception:
        return

    try:
        # employees indexes
        db.employees.create_index([("emp_code", ASCENDING)], unique=True)
        db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)])

        # attendance_logs indexes
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)], unique=True
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)]
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("punch_in", DESCENDING)]
        )
        db.attendance_logs.create_index(
            [("date", ASCENDING), ("late_minutes", ASCENDING), ("emp_code", ASCENDING)]
        )
    except Exception:
        pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_indexes()
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up_2(val: Optional[float]) -> Optional[float]:
    """R8: Rounding to 2 decimal places half-up."""
    if val is None:
        return None
    d = Decimal(str(val)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    return float(d)


def round_half_up_4(val: Optional[float]) -> Optional[float]:
    """R8: Rounding to 4 decimal places half-up (rates)."""
    if val is None:
        return None
    d = Decimal(str(val)).quantize(Decimal("0.0001"), rounding=ROUND_HALF_UP)
    return float(d)


def epoch_ms_to_dt(ms: int) -> datetime:
    """R1: Before any calculation, truncate to whole seconds."""
    truncated_ms = (ms // 1000) * 1000
    return datetime.fromtimestamp(truncated_ms / 1000, tz=timezone.utc)


def dt_to_epoch_ms(dt: Optional[datetime]) -> Optional[int]:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


def get_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: Attendance date is IST calendar date of punch_in.
    Overnight shifts: if punch-in in IST is strictly earlier than shift_end,
    it belongs to the shift that started the previous day.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh, em) <= (sh, sm)
    if is_overnight:
        punch_in_time = (punch_in_ist.hour, punch_in_ist.minute, punch_in_ist.second)
        if punch_in_time < (eh, em, 0):
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()
    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes. Late only if strictly more than 10:00 after shift_start.
    When late, late_minutes = whole minutes (floor) elapsed since shift_start. Otherwise 0.
    """
    d = date.fromisoformat(attendance_date_str)
    sh, sm = map(int, shift_start.split(":"))
    shift_start_dt = datetime(d.year, d.month, d.day, sh, sm, 0, tzinfo=IST)

    punch_in_ist = punch_in_utc.astimezone(IST)
    diff_sec = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_sec > 600:
        return int(diff_sec // 60)
    return 0


def compute_overtime(
    punch_out_utc: datetime, shift_end: str, attendance_date_str: str, shift_start: str
) -> int:
    """
    R3: Overtime = whole minutes (floor) between shift_end and punch_out,
    counted only when >= 30; otherwise 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    d = date.fromisoformat(attendance_date_str)
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    if (eh, em) <= (sh, sm):
        shift_end_date = d + timedelta(days=1)
    else:
        shift_end_date = d

    shift_end_dt = datetime(
        shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST
    )
    punch_out_ist = punch_out_utc.astimezone(IST)
    diff_sec = (punch_out_ist - shift_end_dt).total_seconds()
    if diff_sec >= 1800:
        return int(diff_sec // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """
    R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up.
    """
    diff_sec = Decimal(str((punch_out_utc - punch_in_utc).total_seconds()))
    hours = diff_sec / Decimal("3600")
    return float(hours.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: Half day when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def count_working_days(month_str: str, joined_on_str: str) -> int:
    """
    R7: Mon-Fri working days in month, starting from joined_on for mid-month joiners.
    """
    year, m = map(int, month_str.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = date(year, m, 1)
    month_end = date(year, m, last_day)
    joined_date = date.fromisoformat(joined_on_str)

    if joined_date > month_end:
        return 0

    start_date = max(month_start, joined_date)
    count = 0
    cur = start_date
    while cur <= month_end:
        if cur.weekday() < 5:  # Mon-Fri
            count += 1
        cur += timedelta(days=1)
    return count


def serialize_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    created_at = doc.get("created_at")
    if isinstance(created_at, datetime):
        created_at_ms = dt_to_epoch_ms(created_at)
    else:
        created_at_ms = created_at

    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": created_at_ms,
    }


def serialize_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    history_out = []
    for h in doc.get("history", []) or []:
        changes_out = {}
        for k, v in (h.get("changes") or {}).items():
            f_val = v.get("from")
            t_val = v.get("to")
            if k in ("punch_in", "punch_out"):
                if isinstance(f_val, datetime):
                    f_val = dt_to_epoch_ms(f_val)
                if isinstance(t_val, datetime):
                    t_val = dt_to_epoch_ms(t_val)
            changes_out[k] = {"from": f_val, "to": t_val}
        history_out.append(
            {
                "at": dt_to_epoch_ms(h["at"]) if isinstance(h.get("at"), datetime) else h.get("at"),
                "by": h.get("by"),
                "reason": h.get("reason"),
                "changes": changes_out,
            }
        )

    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": dt_to_epoch_ms(doc.get("punch_in")),
        "punch_out": dt_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history_out,
    }


def serialize_bson_for_json(obj: Any) -> Any:
    return json.loads(json_util.dumps(obj))


# --------------------------------------------------------------------------- #
# Schemas
# --------------------------------------------------------------------------- #
class Status(str, Enum):
    PRESENT = "PRESENT"
    ABSENT = "ABSENT"
    LEAVE = "LEAVE"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class PresenceStatus(str, Enum):
    PRESENT = "PRESENT"
    WFH = "WFH"
    ON_DUTY = "ON_DUTY"


class EmployeeCreate(BaseModel):
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @model_validator(mode="after")
    def validate_rules(self):
        try:
            date.fromisoformat(self.joined_on)
        except Exception:
            raise ValueError("joined_on must be YYYY-MM-DD")
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class Employee(BaseModel):
    emp_code: str
    name: str
    email: str
    department: str
    shift_start: str
    shift_end: str
    joined_on: str
    created_at: int


class EmployeePage(BaseModel):
    items: List[Employee]
    total: int
    page: int
    page_size: int


class PunchInRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000, strict=True
    )
    status: PresenceStatus = PresenceStatus.PRESENT


class PunchOutRequest(BaseModel):
    emp_code: str
    punched_at: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000, strict=True
    )


class RegularizeRequest(BaseModel):
    status: Optional[Status] = None
    punch_in: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000, strict=True
    )
    punch_out: Optional[int] = Field(
        default=None, ge=100000000000, le=4102444800000, strict=True
    )
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


class HistoryEntry(BaseModel):
    at: int
    by: str
    reason: str
    changes: Dict[str, Dict[str, Any]]


class AttendanceRecord(BaseModel):
    emp_code: str
    date: str
    status: Status
    punch_in: Optional[int]
    punch_out: Optional[int]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: List[HistoryEntry]


class AttendancePage(BaseModel):
    items: List[AttendanceRecord]
    total: int
    page: int
    page_size: int


class EmployeeMonthly(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummary(BaseModel):
    month: str
    items: List[DepartmentSummaryItem]


class LeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class Leaderboard(BaseModel):
    month: str
    items: List[LeaderboardItem]


class TrendItem(BaseModel):
    date: str
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class Trend(BaseModel):
    department: str
    items: List[TrendItem]


# --------------------------------------------------------------------------- #
# Endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Database unreachable: {str(e)}",
        )


@app.post("/employees", status_code=status.HTTP_201_CREATED, response_model=Employee)
def create_employee(body: EmployeeCreate):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(timezone.utc)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")
    except PyMongoError as e:
        raise HTTPException(status_code=500, detail=str(e))

    return serialize_employee_doc(doc)


@app.get("/employees", response_model=EmployeePage)
def list_employees(
    department: Optional[str] = Query(None),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q: Dict[str, Any] = {}
    if department is not None:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q).sort("emp_code", ASCENDING).skip(skip).limit(page_size)
    items = [serialize_employee_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-in",
    status_code=status.HTTP_201_CREATED,
    response_model=AttendanceRecord,
)
def punch_in(body: PunchInRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = epoch_ms_to_dt(body.punched_at)
    else:
        # Truncate server current time to whole seconds
        now = datetime.now(timezone.utc)
        punch_in_utc = now.replace(microsecond=0)

    att_date = get_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])
    late_min = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status.value,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_min,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409, detail="Already punched in for this attendance date"
        )

    return serialize_attendance_doc(doc)


@app.post(
    "/attendance/punch-out",
    status_code=status.HTTP_200_OK,
    response_model=AttendanceRecord,
)
def punch_out(body: PunchOutRequest):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = epoch_ms_to_dt(body.punched_at)
    else:
        now = datetime.now(timezone.utc)
        punch_out_utc = now.replace(microsecond=0)

    # Find the most recent record whose punch_in <= punched_at
    latest_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )

    if not latest_record:
        raise HTTPException(
            status_code=404, detail="No punch-in found for this employee at or before punched_at"
        )

    if latest_record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="Record is already punched out")

    punch_in_utc = latest_record["punch_in"]
    if punch_out_utc <= punch_in_utc or punch_out_utc > punch_in_utc + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="punched_at must be strictly after punch_in and within 24 hours of it",
        )

    work_hrs = compute_work_hours(punch_in_utc, punch_out_utc)
    ot_min = compute_overtime(
        punch_out_utc, emp["shift_end"], latest_record["date"], emp["shift_start"]
    )
    half_d = compute_half_day(work_hrs)

    updated_record = db.attendance_logs.find_one_and_update(
        {"_id": latest_record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hrs,
                "overtime_minutes": ot_min,
                "half_day": half_d,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(status_code=409, detail="Record was punched out concurrently")

    return serialize_attendance_doc(updated_record)


@app.get("/attendance", response_model=AttendancePage)
def list_attendance(
    emp_code: Optional[str] = Query(None),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Status] = Query(None, alias="status"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if isinstance(date_from, str) and isinstance(date_to, str):
        if date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

    q: Dict[str, Any] = {}
    if emp_code:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from
        if date_to:
            q["date"]["$lte"] = date_to
    if status_filter:
        q["status"] = status_filter.value

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = (
        db.attendance_logs.find(q)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [serialize_attendance_doc(d) for d in cursor]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.patch(
    "/attendance/{emp_code}/{date}",
    status_code=status.HTTP_200_OK,
    response_model=AttendanceRecord,
)
def regularize_attendance(
    emp_code: str = Path(...),
    date_param: str = Path(..., alias="date", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    body: RegularizeRequest = ...,
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    existing_record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date_param})
    if not existing_record:
        raise HTTPException(
            status_code=404, detail="Attendance record not found for this date"
        )

    # A request that changes nothing is 422
    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="No changes supplied in regularize request")

    # Setting ABSENT or LEAVE clears both punch times, supplying punch_in or punch_out with it is 422
    new_status_str = body.status.value if body.status is not None else existing_record["status"]
    if new_status_str in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                status_code=422,
                detail=f"Supplying punch times with status {new_status_str} is invalid",
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_dt(body.punch_in)
        else:
            new_punch_in = existing_record.get("punch_in")

        if new_punch_in is None:
            raise HTTPException(
                status_code=422,
                detail="Presence status requires a punch_in instant (supplied or stored)",
            )

        # punch_in must stay on the record's attendance date (R1)
        calc_date = get_attendance_date(
            new_punch_in, emp["shift_start"], emp["shift_end"]
        )
        if calc_date != date_param:
            raise HTTPException(
                status_code=422,
                detail=f"punch_in yields attendance date {calc_date}, which does not match record date {date_param}",
            )

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_dt(body.punch_out)
        else:
            new_punch_out = existing_record.get("punch_out")

        if new_punch_out is not None:
            if new_punch_out <= new_punch_in or new_punch_out > new_punch_in + timedelta(hours=24):
                raise HTTPException(
                    status_code=422,
                    detail="punch_out must be strictly after punch_in and within 24 hours of it",
                )

    # Derive fields from final times
    if new_status_str in ("ABSENT", "LEAVE"):
        new_work_hours = None
        new_late_minutes = 0
        new_overtime_minutes = 0
        new_half_day = False
    else:
        new_late_minutes = compute_late_minutes(new_punch_in, emp["shift_start"], date_param)
        if new_punch_out is not None:
            new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
            new_overtime_minutes = compute_overtime(
                new_punch_out, emp["shift_end"], date_param, emp["shift_start"]
            )
            new_half_day = compute_half_day(new_work_hours)
        else:
            new_work_hours = None
            new_overtime_minutes = 0
            new_half_day = False

    # Compute changes
    changes: Dict[str, Dict[str, Any]] = {}
    fields_to_check = [
        ("status", existing_record.get("status"), new_status_str),
        ("punch_in", existing_record.get("punch_in"), new_punch_in),
        ("punch_out", existing_record.get("punch_out"), new_punch_out),
        ("work_hours", existing_record.get("work_hours"), new_work_hours),
        ("late_minutes", existing_record.get("late_minutes", 0), new_late_minutes),
        ("overtime_minutes", existing_record.get("overtime_minutes", 0), new_overtime_minutes),
        ("half_day", existing_record.get("half_day", False), new_half_day),
    ]

    for fname, old_v, new_v in fields_to_check:
        if old_v != new_v:
            changes[fname] = {"from": old_v, "to": new_v}

    if not changes:
        raise HTTPException(
            status_code=422, detail="Request resulted in no changes to the attendance record"
        )

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: history length must match existing
    current_history_len = len(existing_record.get("history", []))
    update_filter = {
        "_id": existing_record["_id"],
        "history": {"$size": current_history_len},
    }

    updated_record = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": {
                "status": new_status_str,
                "punch_in": new_punch_in,
                "punch_out": new_punch_out,
                "work_hours": new_work_hours,
                "late_minutes": new_late_minutes,
                "overtime_minutes": new_overtime_minutes,
                "half_day": new_half_day,
            },
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated_record:
        raise HTTPException(
            status_code=409, detail="Concurrent regularization conflict; please retry"
        )

    return serialize_attendance_doc(updated_record)


@app.get("/analytics/employees/{emp_code}/monthly", response_model=EmployeeMonthly)
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    working_days = count_working_days(month, emp["joined_on"])

    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": month_start, "$lte": month_end},
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {
                                        "$in": [
                                            {
                                                "$dayOfWeek": {
                                                    "$dateFromString": {"dateString": "$date"}
                                                }
                                            },
                                            [2, 3, 4, 5, 6],
                                        ]
                                    },
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    agg_res = list(db.attendance_logs.aggregate(pipeline))
    if agg_res:
        row = agg_res[0]
        present_days = float(row.get("present_days", 0.0))
        leave_days = int(row.get("leave_days", 0))
        late_count = int(row.get("late_count", 0))
        total_late = int(row.get("total_late_minutes", 0))
        total_ot = int(row.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_ot = 0

    if working_days > 0:
        att_pct = round_half_up_2((present_days / working_days) * 100.0)
    else:
        att_pct = None

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_ot,
        "attendance_pct": att_pct,
    }


@app.get("/analytics/departments/summary", response_model=DepartmentSummary)
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
    if department is not None:
        match_stage["department"] = department

    pipeline = [
        {"$match": match_stage},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"emp": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$emp"]},
                                    {"$gte": ["$date", month_start]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "logs_list": {"$push": "$logs"},
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": 1,
                "all_logs": {
                    "$reduce": {
                        "input": "$logs_list",
                        "initialValue": [],
                        "in": {"$concatArrays": ["$$value", "$$this"]},
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {
                                        "$and": [
                                            {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                            {
                                                "$in": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$$l.date"
                                                            }
                                                        }
                                                    },
                                                    [2, 3, 4, 5, 6],
                                                ]
                                            },
                                        ]
                                    },
                                    {"$cond": [{"$eq": ["$$l.half_day", True]}, 0.5, 1.0]},
                                    0.0,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {
                                "$cond": [
                                    {"$gt": [{"$ifNull": ["$$l.late_minutes", 0]}, 0]},
                                    1,
                                    0,
                                ]
                            },
                        }
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {"$ifNull": ["$$l.late_minutes", 0]},
                        }
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "LEAVE"]}, 1, 0]},
                        }
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$map": {
                            "input": "$all_logs",
                            "as": "l",
                            "in": {"$cond": [{"$eq": ["$$l.status", "ON_DUTY"]}, 1, 0]},
                        }
                    }
                },
                "valid_work_hours": {
                    "$filter": {
                        "input": "$all_logs",
                        "as": "l",
                        "cond": {
                            "$and": [
                                {"$in": ["$$l.status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                {"$ne": ["$$l.work_hours", None]},
                            ]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "department": 1,
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours.work_hours"},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": ASCENDING}},
    ]

    items = []
    for doc in db.employees.aggregate(pipeline):
        items.append(
            {
                "department": doc["department"],
                "headcount": doc["headcount"],
                "present_days": float(doc["present_days"]),
                "avg_work_hours": round_half_up_2(doc.get("avg_work_hours")),
                "late_count": int(doc["late_count"]),
                "total_late_minutes": int(doc["total_late_minutes"]),
                "leave_count": int(doc["leave_count"]),
                "on_duty_count": int(doc["on_duty_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", response_model=Leaderboard)
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = Query(None),
):
    year, m = map(int, month.split("-"))
    _, last_day = calendar.monthrange(year, m)
    month_start = f"{month}-01"
    month_end = f"{month}-{last_day:02d}"

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "date": {"$gte": month_start, "$lte": month_end},
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "emp",
            }
        },
        {"$match": {"emp": {"$ne": []}}},
        {"$unwind": "$emp"},
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend(
        [
            {
                "$project": {
                    "emp_code": "$_id",
                    "name": "$emp.name",
                    "department": "$emp.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": DESCENDING, "emp_code": ASCENDING}},
        ]
    )

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        items.append(
            {
                "rank": doc["rank"],
                "emp_code": doc["emp_code"],
                "name": doc["name"],
                "department": doc["department"],
                "total_late_minutes": int(doc["total_late_minutes"]),
                "late_count": int(doc["late_count"]),
            }
        )

    return {"month": month, "items": items}


@app.get("/analytics/departments/{department}/trend", response_model=Trend)
def department_trend(
    department: str = Path(...),
    from_date: str = Query(..., alias="from", pattern=r"^\d{4}-\d{2}-\d{2}$"),
    to_date: str = Query(..., alias="to", pattern=r"^\d{4}-\d{2}-\d{2}$"),
):
    try:
        from_d = date.fromisoformat(from_date)
        to_d = date.fromisoformat(to_date)
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if to_d < from_d:
        raise HTTPException(status_code=422, detail="to must be >= from")

    num_days = (to_d - from_d).days + 1
    if num_days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    # 404 for unknown department
    dept_exists = db.employees.find_one({"department": department})
    if not dept_exists:
        raise HTTPException(status_code=404, detail="Department not found")

    dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
    emp_codes = [e["emp_code"] for e in dept_emps]

    from_dt = datetime.combine(from_d, time.min, tzinfo=timezone.utc)
    to_dt_plus_1 = datetime.combine(to_d + timedelta(days=1), time.min, tzinfo=timezone.utc)

    pipeline: List[Dict[str, Any]] = [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {"$gte": from_date, "$lte": to_date},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}, 1, 0]
                    }
                },
            }
        },
        # Union with a single document to guarantee densify has input even if no attendance logs exist
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"department": department}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": {"$literal": from_date},
                            "present_count": {"$literal": 0.0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ],
            }
        },
        {
            "$group": {
                "_id": "$_id",
                "present_count": {"$max": "$present_count"},
                "late_count": {"$max": "$late_count"},
            }
        },
        {
            "$set": {
                "date_val": {"$dateFromString": {"dateString": "$_id"}},
            }
        },
        {
            "$densify": {
                "field": "date_val",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [from_dt, to_dt_plus_1],
                },
            }
        },
        {
            "$set": {
                "date": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date_val"}},
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date_val"}, [2, 3, 4, 5, 6]]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"d_str": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$d_str"]},
                                ]
                            }
                        }
                    },
                    {"$count": "hc"},
                ],
                "as": "hc_info",
            }
        },
        {
            "$set": {
                "headcount": {"$ifNull": [{"$first": "$hc_info.hc"}, 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date_val": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, "current"],
                        },
                    }
                },
            }
        },
        {"$sort": {"date_val": 1}},
    ]

    items = []
    for doc in db.attendance_logs.aggregate(pipeline):
        items.append(
            {
                "date": doc["date"],
                "is_working_day": bool(doc["is_working_day"]),
                "headcount": int(doc["headcount"]),
                "present_count": float(round_half_up_2(doc.get("present_count"))),
                "late_count": int(doc.get("late_count", 0)),
                "attendance_rate": round_half_up_4(doc.get("attendance_rate")),
                "moving_avg_7d": round_half_up_4(doc.get("moving_avg_7d")),
            }
        )

    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = Query(None),
    month: Optional[str] = Query(None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = Query(None),
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = Query(None),
    date_to: Optional[str] = Query(None),
    status_filter: Optional[Status] = Query(None, alias="status"),
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if isinstance(date_from, str) and isinstance(date_to, str) and date_from > date_to:
            raise HTTPException(status_code=422, detail="date_from must be <= date_to")

        q: Dict[str, Any] = {}
        if isinstance(emp_code, str):
            q["emp_code"] = emp_code
        if isinstance(date_from, str) or isinstance(date_to, str):
            q["date"] = {}
            if isinstance(date_from, str):
                q["date"]["$gte"] = date_from
            if isinstance(date_to, str):
                q["date"]["$lte"] = date_to
        if isinstance(status_filter, Status):
            q["status"] = status_filter.value

        skip = (page - 1) * page_size
        find_cmd = {
            "find": "attendance_logs",
            "filter": q,
            "sort": {"date": -1, "emp_code": 1},
            "skip": skip,
            "limit": page_size,
        }
        explain_res = db.command("explain", find_cmd, verbosity="executionStats")
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": serialize_bson_for_json(explain_res),
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422, detail="emp_code and month are required for employee_monthly"
            )

        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {"$gte": month_start, "$lte": month_end},
                }
            },
            {
                "$group": {
                    "_id": None,
                    "present_days": {
                        "$sum": {
                            "$cond": [
                                {
                                    "$and": [
                                        {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                        {
                                            "$in": [
                                                {
                                                    "$dayOfWeek": {
                                                        "$dateFromString": {"dateString": "$date"}
                                                    }
                                                },
                                                [2, 3, 4, 5, 6],
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": serialize_bson_for_json(explain_res),
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for department_summary"
            )

        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        match_stage: Dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            match_stage["department"] = department

        pipeline = [
            {"$match": match_stage},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"emp": "$emp_code"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$emp_code", "$$emp"]},
                                        {"$gte": ["$date", month_start]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "employees", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": serialize_bson_for_json(explain_res),
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422, detail="month is required for late_leaderboard"
            )

        year, m = map(int, month.split("-"))
        _, last_day = calendar.monthrange(year, m)
        month_start = f"{month}-01"
        month_end = f"{month}-{last_day:02d}"

        pipeline = [
            {
                "$match": {
                    "date": {"$gte": month_start, "$lte": month_end},
                    "late_minutes": {"$gt": 0},
                }
            },
            {
                "$group": {
                    "_id": "$emp_code",
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "late_count": {"$sum": 1},
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": serialize_bson_for_json(explain_res),
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required for department_trend",
            )
        try:
            from_d = date.fromisoformat(from_date)
            to_d = date.fromisoformat(to_date)
        except Exception:
            raise HTTPException(status_code=422, detail="Invalid date format")

        if to_d < from_d:
            raise HTTPException(status_code=422, detail="to must be >= from")
        if (to_d - from_d).days + 1 > 92:
            raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

        dept_emps = list(db.employees.find({"department": department}, {"emp_code": 1}))
        emp_codes = [e["emp_code"] for e in dept_emps]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": emp_codes},
                    "date": {"$gte": from_date, "$lte": to_date},
                }
            },
            {
                "$group": {
                    "_id": "$date",
                    "present_count": {
                        "$sum": {
                            "$cond": [
                                {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                1.0,
                                0.0,
                            ]
                        }
                    },
                }
            },
        ]
        explain_res = db.command(
            "explain",
            {"aggregate": "attendance_logs", "pipeline": pipeline, "cursor": {}},
            verbosity="executionStats",
        )
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": serialize_bson_for_json(explain_res),
        }

    else:
        raise HTTPException(status_code=422, detail=f"Unknown endpoint: {endpoint}")
