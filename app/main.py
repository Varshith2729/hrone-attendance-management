"""
Employee Attendance & Analytics API - Main Application

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)
"""
from contextlib import asynccontextmanager
import calendar
from datetime import datetime, date, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
from typing import Any, Dict, List, Optional

from bson import json_util
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, field_validator
from pymongo import MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError

load_dotenv()

# MongoDB setup
client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))

MIN_EPOCH_MS = 100000000000
MAX_EPOCH_MS = 4102444800000


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Create required startup indexes idempotently
    try:
        db.employees.create_index("emp_code", unique=True)
        db.employees.create_index("department")
        db.attendance_logs.create_index([("emp_code", 1), ("date", 1)], unique=True)
        db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
        db.attendance_logs.create_index([("emp_code", 1), ("date", -1)])
        db.attendance_logs.create_index([("status", 1), ("date", 1)])
    except Exception as e:
        print(f"Error creating indexes: {e}")
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)


# --------------------------------------------------------------------------- #
# Helper Functions & Business Rules (R1 - R8)
# --------------------------------------------------------------------------- #
def round_half_up(val: Optional[float], decimals: int = 2) -> Optional[float]:
    """R8: Standard numbers 2 decimals half-up, rates 4 decimals half-up."""
    if val is None:
        return None
    d = Decimal(str(val))
    q = Decimal("1") if decimals == 0 else Decimal("1e-" + str(decimals))
    return float(d.quantize(q, rounding=ROUND_HALF_UP))


def validate_epoch_ms(ms: Optional[int]) -> Optional[int]:
    """Validates integer epoch milliseconds within bounds."""
    if ms is None:
        return None
    if not isinstance(ms, int) or isinstance(ms, bool):
        raise HTTPException(status_code=422, detail="Epoch milliseconds must be an integer")
    if ms < MIN_EPOCH_MS or ms > MAX_EPOCH_MS:
        raise HTTPException(status_code=422, detail=f"Epoch milliseconds out of range [{MIN_EPOCH_MS}, {MAX_EPOCH_MS}]")
    return ms


def to_epoch_ms(val: Any) -> Optional[int]:
    """Converts a BSON date / Python datetime to integer epoch milliseconds in UTC."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, datetime):
        if val.tzinfo is None:
            val = val.replace(tzinfo=timezone.utc)
        return int(val.timestamp() * 1000)
    return None


def from_epoch_ms(ms: int) -> datetime:
    """Converts integer epoch milliseconds to UTC datetime truncated to whole seconds (R1)."""
    validate_epoch_ms(ms)
    seconds = ms / 1000.0
    dt = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return dt.replace(microsecond=0)


def get_ist_datetime(dt_utc: datetime) -> datetime:
    """Converts UTC datetime to IST timezone truncated to whole seconds."""
    if dt_utc.tzinfo is None:
        dt_utc = dt_utc.replace(tzinfo=timezone.utc)
    return dt_utc.astimezone(IST).replace(microsecond=0)


def parse_time_str(time_str: str) -> tuple[int, int]:
    """Parses 'HH:MM' string into (hour, minute)."""
    parts = time_str.split(":")
    return int(parts[0]), int(parts[1])


def is_overnight_shift(shift_start: str, shift_end: str) -> bool:
    """R1: shift_end <= shift_start means overnight shift."""
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)
    return (eh < sh) or (eh == sh and em <= sm)


def get_attendance_date(punch_in_ist: datetime, shift_start: str, shift_end: str) -> str:
    """
    R1: A record's date is the IST calendar date of its punch_in.
    Overnight shift exception: punch-in IST time earlier than shift_end belongs to the shift
    that started the previous calendar day.
    """
    sh, sm = parse_time_str(shift_start)
    eh, em = parse_time_str(shift_end)

    if is_overnight_shift(shift_start, shift_end):
        end_t = time(eh, em)
        if punch_in_ist.time() < end_t:
            return (punch_in_ist.date() - timedelta(days=1)).isoformat()

    return punch_in_ist.date().isoformat()


def compute_late_minutes(punch_in_ist: datetime, shift_start: str, attendance_date_str: str) -> int:
    """
    R2: Grace period 10 minutes (600 seconds).
    Late ONLY if punch_in is strictly > 10:00 after shift_start.
    late_minutes = floor whole minutes elapsed since shift_start.
    """
    yr, mo, dy = map(int, attendance_date_str.split("-"))
    sh, sm = parse_time_str(shift_start)
    shift_start_dt = datetime(yr, mo, dy, sh, sm, 0, tzinfo=IST)

    diff_seconds = (punch_in_ist - shift_start_dt).total_seconds()
    if diff_seconds > 600:
        return int(diff_seconds // 60)
    return 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: (punch_out - punch_in) in seconds / 3600, rounded to 2 decimals half-up."""
    diff_sec = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_sec / 3600.0, 2)


def compute_half_day(work_hours: Optional[float]) -> bool:
    """R5: half_day is true when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


def compute_overtime_minutes(punch_out_ist: datetime, shift_end: str, attendance_date_str: str, shift_start: str) -> int:
    """
    R3: Whole minutes (floor) from shift_end to punch_out, counted only if >= 30; otherwise 0.
    For overnight shifts, shift_end is on attendance_date + 1 day.
    """
    yr, mo, dy = map(int, attendance_date_str.split("-"))
    eh, em = parse_time_str(shift_end)

    shift_end_date = date(yr, mo, dy)
    if is_overnight_shift(shift_start, shift_end):
        shift_end_date += timedelta(days=1)

    shift_end_dt = datetime(shift_end_date.year, shift_end_date.month, shift_end_date.day, eh, em, 0, tzinfo=IST)
    diff_seconds = (punch_out_ist - shift_end_dt).total_seconds()
    diff_mins = int(diff_seconds // 60)
    return diff_mins if diff_mins >= 30 else 0


def count_working_days(joined_on_str: str, month_str: str) -> int:
    """R7: Counts Monday-Friday days in month, starting from max(joined_on, month_start)."""
    yr, mo = map(int, month_str.split("-"))
    month_start = date(yr, mo, 1)
    month_end = date(yr, mo, calendar.monthrange(yr, mo)[1])
    joined_date = date.fromisoformat(joined_on_str)

    start_date = max(month_start, joined_date)
    if start_date > month_end:
        return 0

    working_days = 0
    curr = start_date
    while curr <= month_end:
        if curr.weekday() < 5:  # Mon-Fri
            working_days += 1
        curr += timedelta(days=1)
    return working_days


def format_employee_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats employee document for API responses according to OpenAPI contract."""
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": to_epoch_ms(doc["created_at"])
    }


def format_history_changes_api(changes_db: Dict[str, Any]) -> Dict[str, Any]:
    """Converts DB history changes object to API format with epoch milliseconds for timestamps."""
    res = {}
    for key, val in changes_db.items():
        if key in ["punch_in", "punch_out"]:
            res[key] = {
                "from": to_epoch_ms(val.get("from")),
                "to": to_epoch_ms(val.get("to"))
            }
        else:
            res[key] = {
                "from": val.get("from"),
                "to": val.get("to")
            }
    return res


def format_history_entry_api(entry: Dict[str, Any]) -> Dict[str, Any]:
    """Formats one history entry for API response."""
    return {
        "at": to_epoch_ms(entry["at"]),
        "by": entry["by"],
        "reason": entry["reason"],
        "changes": format_history_changes_api(entry.get("changes", {}))
    }


def format_attendance_doc(doc: Dict[str, Any]) -> Dict[str, Any]:
    """Formats attendance record document for API response according to OpenAPI schema."""
    history = [format_history_entry_api(h) for h in doc.get("history", [])]
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": to_epoch_ms(doc.get("punch_in")),
        "punch_out": to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": history
    }


# --------------------------------------------------------------------------- #
# Pipelines Builders for Analytics & Explain
# --------------------------------------------------------------------------- #
def build_employee_monthly_pipeline(emp_code: str, month: str) -> List[Dict[str, Any]]:
    yr, mo = map(int, month.split("-"))
    num_days = calendar.monthrange(yr, mo)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {"$gte": start_str, "$lte": end_str}
            }
        },
        {
            "$project": {
                "status": 1,
                "late_minutes": 1,
                "overtime_minutes": 1,
                "half_day": 1,
                "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
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
                                    {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                                    {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {"$sum": "$late_minutes"},
                "total_overtime_minutes": {"$sum": "$overtime_minutes"}
            }
        }
    ]


def build_department_summary_pipeline(month: str, department: Optional[str] = None) -> List[Dict[str, Any]]:
    yr, mo = map(int, month.split("-"))
    num_days = calendar.monthrange(yr, mo)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    match_emp = {"joined_on": {"$lte": end_str}}
    if department:
        match_emp["department"] = department

    return [
        {"$match": match_emp},
        {
            "$lookup": {
                "from": "attendance_logs",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "logs"
            }
        },
        {"$unwind": {"path": "$logs", "preserveNullAndEmptyArrays": True}},
        {
            "$match": {
                "$or": [
                    {"logs": None},
                    {
                        "$and": [
                            {"logs.date": {"$gte": start_str}},
                            {"logs.date": {"$lte": end_str}}
                        ]
                    }
                ]
            }
        },
        {
            "$project": {
                "department": 1,
                "emp_code": 1,
                "status": "$logs.status",
                "work_hours": "$logs.work_hours",
                "late_minutes": "$logs.late_minutes",
                "overtime_minutes": "$logs.overtime_minutes",
                "half_day": "$logs.half_day",
                "day_of_week": {
                    "$cond": [
                        {"$ifNull": ["$logs.date", False]},
                        {"$dayOfWeek": {"$dateFromString": {"dateString": "$logs.date", "format": "%Y-%m-%d"}}},
                        0
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "employees": {"$addToSet": "$emp_code"},
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$gte": ["$day_of_week", 2]},
                                    {"$lte": ["$day_of_week", 6]}
                                ]
                            },
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "valid_work_hours": {
                    "$push": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                                    {"$ne": ["$work_hours", None]}
                                ]
                            },
                            "$work_hours",
                            "$$REMOVE"
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                    }
                }
            }
        },
        {
            "$project": {
                "department": "$_id",
                "headcount": {"$size": "$employees"},
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": [{"$size": "$valid_work_hours"}, 0]},
                        {"$avg": "$valid_work_hours"},
                        None
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1
            }
        },
        {"$sort": {"department": 1}}
    ]


def build_late_leaderboard_pipeline(month: str, limit: int = 10, department: Optional[str] = None) -> List[Dict[str, Any]]:
    yr, mo = map(int, month.split("-"))
    num_days = calendar.monthrange(yr, mo)[1]
    start_str = f"{month}-01"
    end_str = f"{month}-{num_days:02d}"

    pipeline = [
        {
            "$match": {
                "date": {"$gte": start_str, "$lte": end_str},
                "late_minutes": {"$gt": 0}
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"}
    ]

    if department:
        pipeline.append({"$match": {"emp.department": department}})

    pipeline.extend([
        {
            "$group": {
                "_id": "$emp_code",
                "emp_code": {"$first": "$emp_code"},
                "name": {"$first": "$emp.name"},
                "department": {"$first": "$emp.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1}
            }
        },
        {
            "$sort": {
                "total_late_minutes": -1,
                "emp_code": 1
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {"$rank": {}}
                }
            }
        },
        {
            "$match": {
                "rank": {"$lte": limit}
            }
        }
    ])

    return pipeline


def build_department_trend_pipeline(department: str, from_date: str, to_date: str) -> List[Dict[str, Any]]:
    f_dt = date.fromisoformat(from_date)
    t_dt = date.fromisoformat(to_date)

    f_date_obj = datetime.combine(f_dt, time.min, tzinfo=timezone.utc)
    t_date_obj = datetime.combine(t_dt + timedelta(days=1), time.min, tzinfo=timezone.utc)

    return [
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "emp"
            }
        },
        {"$unwind": "$emp"},
        {
            "$match": {
                "emp.department": department,
                "date": {"$gte": from_date, "$lte": to_date}
            }
        },
        {
            "$project": {
                "date": 1,
                "status": 1,
                "late_minutes": 1,
                "half_day": 1,
                "date_obj": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}}
            }
        },
        {
            "$group": {
                "_id": "$date_obj",
                "date_obj": {"$first": "$date_obj"},
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                            {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1.0]},
                            0.0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]
                    }
                }
            }
        },
        {
            "$densify": {
                "field": "date_obj",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [f_date_obj, t_date_obj]
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "pipeline": [
                    {"$match": {"department": department}}
                ],
                "as": "all_dept_emps"
            }
        },
        {
            "$project": {
                "date": {"$dateToString": {"date": "$date_obj", "format": "%Y-%m-%d"}},
                "date_obj": 1,
                "present_count": {"$ifNull": ["$present_count", 0.0]},
                "late_count": {"$ifNull": ["$late_count", 0]},
                "all_dept_emps": 1
            }
        },
        {
            "$project": {
                "date": 1,
                "date_obj": 1,
                "present_count": 1,
                "late_count": 1,
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$all_dept_emps",
                            "as": "e",
                            "cond": {"$lte": ["$$e.joined_on", "$date"]}
                        }
                    }
                }
            }
        },
        {
            "$project": {
                "date": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]}
                    ]
                },
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                {"$gte": [{"$dayOfWeek": "$date_obj"}, 2]},
                                {"$lte": [{"$dayOfWeek": "$date_obj"}, 6]},
                                {"$gt": ["$headcount", 0]}
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None
                    ]
                }
            }
        },
        {"$sort": {"date": 1}},
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0]
                        }
                    }
                }
            }
        }
    ]


# --------------------------------------------------------------------------- #
# Request Models
# --------------------------------------------------------------------------- #
class EmployeeCreateIn(BaseModel):
    emp_code: str = Field(..., pattern=r'^EMP\d{4,6}$')
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r'^[^@\s]+@[^@\s]+\.[^@\s]+$')
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field('09:30', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    shift_end: str = Field('18:30', pattern=r'^([01]\d|2[0-3]):[0-5]\d$')
    joined_on: str = Field(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = 'PRESENT'

    @field_validator('status')
    def validate_status(cls, v):
        if v not in ['PRESENT', 'WFH', 'ON_DUTY']:
            raise ValueError("status must be one of PRESENT, WFH, ON_DUTY")
        return v

    @field_validator('punched_at')
    def validate_punched_at(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None

    @field_validator('punched_at')
    def validate_punched_at(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


class RegularizeIn(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)

    @field_validator('status')
    def validate_status(cls, v):
        if v is not None and v not in ['PRESENT', 'ABSENT', 'LEAVE', 'WFH', 'ON_DUTY']:
            raise ValueError("Invalid status value")
        return v

    @field_validator('punch_in', 'punch_out')
    def validate_instants(cls, v):
        if v is not None:
            validate_epoch_ms(v)
        return v


# --------------------------------------------------------------------------- #
# Endpoints (Phase 1 & Phase 2)
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception as e:
        raise HTTPException(status_code=503, detail="MongoDB health ping failed")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreateIn):
    if body.shift_start == body.shift_end:
        raise HTTPException(status_code=422, detail="shift_start and shift_end must differ")

    doc = body.model_dump()
    created_at_dt = datetime.now(timezone.utc).replace(microsecond=0)
    doc["created_at"] = created_at_dt

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return format_employee_doc(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department:
        q["department"] = department

    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    cursor = db.employees.find(q, {"_id": 0}).sort("emp_code", 1).skip(skip).limit(page_size)
    items = [format_employee_doc(emp) for emp in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_in_utc = from_epoch_ms(body.punched_at)
    else:
        punch_in_utc = datetime.now(timezone.utc).replace(microsecond=0)

    punch_in_ist = get_ist_datetime(punch_in_utc)
    attendance_date = get_attendance_date(punch_in_ist, emp["shift_start"], emp["shift_end"])
    late_mins = compute_late_minutes(punch_in_ist, emp["shift_start"], attendance_date)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late_mins,
        "overtime_minutes": 0,
        "half_day": False,
        "history": []
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="already punched in for this date")

    return format_attendance_doc(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    if body.punched_at is not None:
        punch_out_utc = from_epoch_ms(body.punched_at)
    else:
        punch_out_utc = datetime.now(timezone.utc).replace(microsecond=0)

    punch_out_ist = get_ist_datetime(punch_out_utc)

    # Find the most recent record whose punch_in is <= punched_at and punch_out is null
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$lte": punch_out_utc},
            "punch_out": None
        },
        sort=[("punch_in", -1)]
    )

    if not record:
        raise HTTPException(status_code=404, detail="No punch-in record found for employee")

    punch_in_utc = record["punch_in"]
    if punch_in_utc.tzinfo is None:
        punch_in_utc = punch_in_utc.replace(tzinfo=timezone.utc)

    if punch_out_utc <= punch_in_utc:
        raise HTTPException(status_code=422, detail="punch_out must be after punch_in")

    if (punch_out_utc - punch_in_utc).total_seconds() > 86400:
        raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")

    work_hours = compute_work_hours(punch_in_utc, punch_out_utc)
    half_day = compute_half_day(work_hours)
    overtime_mins = compute_overtime_minutes(punch_out_ist, emp["shift_end"], record["date"], emp["shift_start"])

    # Atomic update to avoid punch-out race conditions
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "punch_out": None
        },
        {
            "$set": {
                "punch_out": punch_out_utc,
                "work_hours": work_hours,
                "overtime_minutes": overtime_mins,
                "half_day": half_day
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="record already punched out")

    return format_attendance_doc(updated_doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from and date_to and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from cannot be greater than date_to")

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

    skip = (page - 1) * page_size
    total = db.attendance_logs.count_documents(q)
    cursor = db.attendance_logs.find(q, {"_id": 0}).sort([("date", -1), ("emp_code", 1)]).skip(skip).limit(page_size)
    items = [format_attendance_doc(doc) for doc in cursor]

    return {
        "items": items,
        "total": total,
        "page": page,
        "page_size": page_size
    }


@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str,
    date: str,
    body: RegularizeIn
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(status_code=404, detail="Attendance record not found for that date")

    if body.status is None and body.punch_in is None and body.punch_out is None:
        raise HTTPException(status_code=422, detail="Request must specify at least one field to change")

    # Target new status & instants
    new_status = body.status if body.status is not None else record["status"]
    current_punch_in_ms = to_epoch_ms(record.get("punch_in"))
    current_punch_out_ms = to_epoch_ms(record.get("punch_out"))

    new_punch_in_ms = body.punch_in if body.punch_in is not None else current_punch_in_ms
    new_punch_out_ms = body.punch_out if body.punch_out is not None else current_punch_out_ms

    if new_status in ["ABSENT", "LEAVE"]:
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(status_code=422, detail="punch_in and punch_out cannot be supplied when status is ABSENT or LEAVE")
        new_punch_in_utc = None
        new_punch_out_utc = None
    else:
        # Presence status
        if new_punch_in_ms is None:
            raise HTTPException(status_code=422, detail="presence status requires punch_in")

        new_punch_in_utc = from_epoch_ms(new_punch_in_ms)
        new_punch_in_ist = get_ist_datetime(new_punch_in_utc)

        # Check punch_in stays on attendance date (R1)
        expected_date = get_attendance_date(new_punch_in_ist, emp["shift_start"], emp["shift_end"])
        if expected_date != date:
            raise HTTPException(status_code=422, detail=f"punch_in must belong to attendance date {date}")

        if new_punch_out_ms is not None:
            new_punch_out_utc = from_epoch_ms(new_punch_out_ms)
            if new_punch_out_utc <= new_punch_in_utc:
                raise HTTPException(status_code=422, detail="punch_out must be after punch_in")
            if (new_punch_out_utc - new_punch_in_utc).total_seconds() > 86400:
                raise HTTPException(status_code=422, detail="punch_out cannot be more than 24 hours after punch_in")
        else:
            new_punch_out_utc = None

    # Recompute derived fields for final state
    if new_status in ["ABSENT", "LEAVE"]:
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    else:
        new_late = compute_late_minutes(new_punch_in_ist, emp["shift_start"], date)
        if new_punch_out_utc is not None:
            new_punch_out_ist = get_ist_datetime(new_punch_out_utc)
            new_work_hours = compute_work_hours(new_punch_in_utc, new_punch_out_utc)
            new_half_day = compute_half_day(new_work_hours)
            new_overtime = compute_overtime_minutes(new_punch_out_ist, emp["shift_end"], date, emp["shift_start"])
        else:
            new_work_hours = None
            new_half_day = False
            new_overtime = 0

    # Build history diff
    old_state = {
        "status": record["status"],
        "punch_in": to_epoch_ms(record.get("punch_in")),
        "punch_out": to_epoch_ms(record.get("punch_out")),
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False)
    }

    new_state = {
        "status": new_status,
        "punch_in": to_epoch_ms(new_punch_in_utc),
        "punch_out": to_epoch_ms(new_punch_out_utc),
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day
    }

    changes_db = {}
    for key in ["status", "punch_in", "punch_out", "work_hours", "late_minutes", "overtime_minutes", "half_day"]:
        if old_state[key] != new_state[key]:
            if key in ["punch_in", "punch_out"]:
                changes_db[key] = {
                    "from": record.get(key),
                    "to": new_punch_in_utc if key == "punch_in" else new_punch_out_utc
                }
            else:
                changes_db[key] = {
                    "from": old_state[key],
                    "to": new_state[key]
                }

    if not changes_db:
        raise HTTPException(status_code=422, detail="Request resulted in no changes to the record")

    now_utc = datetime.now(timezone.utc).replace(microsecond=0)
    history_entry_db = {
        "at": now_utc,
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes_db
    }

    current_history = record.get("history", [])

    # Atomic update to avoid concurrent history overwrites
    updated_doc = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": current_history
        },
        {
            "$set": {
                "status": new_status,
                "punch_in": new_punch_in_utc,
                "punch_out": new_punch_out_utc,
                "work_hours": new_work_hours,
                "late_minutes": new_late,
                "overtime_minutes": new_overtime,
                "half_day": new_half_day
            },
            "$push": {
                "history": history_entry_db
            }
        },
        return_document=ReturnDocument.AFTER
    )

    if not updated_doc:
        raise HTTPException(status_code=409, detail="Concurrent edit conflict on attendance record")

    return format_attendance_doc(updated_doc)


# --------------------------------------------------------------------------- #
# Analytics Endpoints (Phase 3)
# --------------------------------------------------------------------------- #
@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$')
):
    emp = db.employees.find_one({"emp_code": emp_code})
    if not emp:
        raise HTTPException(status_code=404, detail="Employee not found")

    working_days = count_working_days(emp["joined_on"], month)
    pipeline = build_employee_monthly_pipeline(emp_code, month)
    res_list = list(db.attendance_logs.aggregate(pipeline))

    if res_list:
        data = res_list[0]
        present_days = data.get("present_days", 0.0)
        leave_days = int(data.get("leave_days", 0))
        late_count = int(data.get("late_count", 0))
        total_late_minutes = int(data.get("total_late_minutes", 0))
        total_overtime_minutes = int(data.get("total_overtime_minutes", 0))
    else:
        present_days = 0.0
        leave_days = 0
        late_count = 0
        total_late_minutes = 0
        total_overtime_minutes = 0

    if working_days == 0:
        attendance_pct = None
    else:
        raw_pct = (present_days / working_days) * 100.0
        attendance_pct = round_half_up(raw_pct, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late_minutes,
        "total_overtime_minutes": total_overtime_minutes,
        "attendance_pct": attendance_pct
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    department: Optional[str] = None
):
    pipeline = build_department_summary_pipeline(month, department)
    res_list = list(db.employees.aggregate(pipeline))

    items = []
    for doc in res_list:
        avg_wh = doc.get("avg_work_hours")
        items.append({
            "department": doc["department"],
            "headcount": int(doc["headcount"]),
            "present_days": float(doc.get("present_days", 0.0)),
            "avg_work_hours": round_half_up(avg_wh, 2) if avg_wh is not None else None,
            "late_count": int(doc.get("late_count", 0)),
            "total_late_minutes": int(doc.get("total_late_minutes", 0)),
            "leave_count": int(doc.get("leave_count", 0)),
            "on_duty_count": int(doc.get("on_duty_count", 0))
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r'^\d{4}-(0[1-9]|1[0-2])$'),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None
):
    pipeline = build_late_leaderboard_pipeline(month, limit, department)
    res_list = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in res_list:
        items.append({
            "rank": int(doc["rank"]),
            "emp_code": doc["emp_code"],
            "name": doc["name"],
            "department": doc["department"],
            "total_late_minutes": int(doc["total_late_minutes"]),
            "late_count": int(doc["late_count"])
        })

    return {
        "month": month,
        "items": items
    }


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from", pattern=r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$'),
    to_date: str = Query(..., alias="to", pattern=r'^\d{4}-(0[1-9]|1[0-2])-(0[1-9]|[12]\d|3[01])$')
):
    # Verify department exists
    if not db.employees.find_one({"department": department}):
        raise HTTPException(status_code=404, detail="Department not found")

    try:
        f_dt = date.fromisoformat(from_date)
        t_dt = date.fromisoformat(to_date)
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid date format")

    if f_dt > t_dt:
        raise HTTPException(status_code=422, detail="from date cannot be greater than to date")

    if (t_dt - f_dt).days > 92:
        raise HTTPException(status_code=422, detail="Date range cannot exceed 92 days")

    pipeline = build_department_trend_pipeline(department, from_date, to_date)
    res_list = list(db.attendance_logs.aggregate(pipeline))

    items = []
    for doc in res_list:
        rate = doc.get("attendance_rate")
        mavg = doc.get("moving_avg_7d")

        items.append({
            "date": doc["date"],
            "is_working_day": bool(doc["is_working_day"]),
            "headcount": int(doc["headcount"]),
            "present_count": float(doc.get("present_count", 0.0)),
            "late_count": int(doc.get("late_count", 0)),
            "attendance_rate": round_half_up(rate, 4) if rate is not None else None,
            "moving_avg_7d": round_half_up(mavg, 4) if mavg is not None else None
        })

    return {
        "department": department,
        "items": items
    }


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: str = Path(..., enum=["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: Optional[int] = 10,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = 1,
    page_size: int = 20
):
    if endpoint == "attendance_list":
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

        skip = (page - 1) * page_size
        explain_res = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": q,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size
            },
            verbosity="executionStats"
        )
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(status_code=422, detail="emp_code and month are required")
        pipeline = build_employee_monthly_pipeline(emp_code, month)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_department_summary_pipeline(month, department)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "employees",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "employees",
            "explain": explain_json
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(status_code=422, detail="month is required")
        pipeline = build_late_leaderboard_pipeline(month, limit or 10, department)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }

    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(status_code=422, detail="department, from, and to are required")
        pipeline = build_department_trend_pipeline(department, from_date, to_date)
        explain_res = db.command(
            "explain",
            {
                "aggregate": "attendance_logs",
                "pipeline": pipeline,
                "cursor": {}
            },
            verbosity="executionStats"
        )
        explain_json = json.loads(json_util.dumps(explain_res))
        return {
            "endpoint": endpoint,
            "collection": "attendance_logs",
            "explain": explain_json
        }

    raise HTTPException(status_code=422, detail="Unsupported endpoint")
