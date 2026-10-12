
import os
import re
import calendar
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator
from pymongo import MongoClient, ASCENDING, DESCENDING, ReturnDocument
from pymongo.errors import DuplicateKeyError
from pymongo.operations import IndexModel

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=3000)
db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
NON_PRESENCE_STATUSES = {"ABSENT", "LEAVE"}
ALL_STATUSES = PRESENCE_STATUSES | NON_PRESENCE_STATUSES

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ---------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def as_utc(value):
    """Ensure a datetime is timezone-aware and expressed in UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def epoch_ms(value):
    if value is None:
        return None
    return int(as_utc(value).timestamp() * 1000)


def parse_ms(value):
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(
            status_code=422,
            detail="Timestamp must be an integer in epoch milliseconds",
        )

    if value < 100000000000 or value > 4102444800000:
        raise HTTPException(
            status_code=422,
            detail="Timestamp is outside the allowed range",
        )

    try:
        timestamp_seconds = value // 1000
        return datetime.fromtimestamp(timestamp_seconds, UTC)
    except (ValueError, OverflowError, OSError):
        raise HTTPException(status_code=422, detail="Invalid timestamp")


def parse_date(value):
    try:
        result = date.fromisoformat(value)
        if result.isoformat() != value:
            raise ValueError
        return result
    except (ValueError, TypeError):
        raise HTTPException(
            status_code=422,
            detail="Date must use YYYY-MM-DD",
        )


def parse_month(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}", value):
        raise HTTPException(status_code=422, detail="Month must use YYYY-MM")
    try:
        return date.fromisoformat(value + "-01")
    except ValueError:
        raise HTTPException(status_code=422, detail="Invalid month")


def validate_shift(value):
    if not isinstance(value, str) or not re.fullmatch(
        r"(?:[01]\d|2[0-3]):[0-5]\d", value
    ):
        raise ValueError("Shift time must use HH:MM")
    return value


def rounded(value, places=2):
    quantum = Decimal("1").scaleb(-places)
    return float(
        Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP)
    )


def safe_employee(doc):
    result = {key: value for key, value in doc.items() if key != "_id"}
    if isinstance(result.get("created_at"), datetime):
        result["created_at"] = epoch_ms(result["created_at"])
    return result


def safe_attendance(doc):
    result = {key: value for key, value in doc.items() if key != "_id"}

    for key in ("punch_in", "punch_out"):
        if isinstance(result.get(key), datetime):
            result[key] = epoch_ms(result[key])

    result.setdefault("history", [])
    result.setdefault("half_day", False)
    result.setdefault("work_hours", None)
    result.setdefault("late_minutes", 0)
    result.setdefault("overtime_minutes", 0)

    # Copy history entries so the database-returned objects aren't modified.
    history = []
    for item in result["history"]:
        item_copy = dict(item)
        if isinstance(item_copy.get("at"), datetime):
            item_copy["at"] = epoch_ms(item_copy["at"])

        changes_copy = {}
        for field, change in item_copy.get("changes", {}).items():
            change_copy = dict(change)
            for side in ("before", "after"):
                if isinstance(change_copy.get(side), datetime):
                    change_copy[side] = epoch_ms(change_copy[side])
            changes_copy[field] = change_copy

        if "changes" in item_copy:
            item_copy["changes"] = changes_copy
        history.append(item_copy)

    result["history"] = history
    return result


def shift_start_datetime(day, shift_start):
    hour, minute = map(int, shift_start.split(":"))
    return datetime.combine(day, time(hour, minute), tzinfo=IST)


def shift_end_datetime(day, shift_start, shift_end):
    hour, minute = map(int, shift_end.split(":"))
    end_day = day + timedelta(days=1) if shift_end <= shift_start else day
    return datetime.combine(end_day, time(hour, minute), tzinfo=IST)


def attendance_shift_date(local_dt, employee):
    day = local_dt.date()
    if (
        employee["shift_end"] <= employee["shift_start"]
        and local_dt.strftime("%H:%M") < employee["shift_end"]
    ):
        day -= timedelta(days=1)
    return day.isoformat()


def compute_late_minutes(punch_in, shift_start, shift_end, attendance_day):
    local = as_utc(punch_in).astimezone(IST)
    day = date.fromisoformat(attendance_day)

    # For an overnight shift, use the previous day's start
    # when the attendance date belongs to that overnight shift.
    start = shift_start_datetime(day, shift_start)

    elapsed_seconds = (local - start).total_seconds()

    if elapsed_seconds <= 10 * 60:
        return 0

    return int(elapsed_seconds // 60)

def compute_work_hours(punch_in, punch_out):
    seconds = (as_utc(punch_out) - as_utc(punch_in)).total_seconds()
    return rounded(seconds / 3600, 2)


def compute_overtime(punch_out, shift_start, shift_end, day):
    start_day = date.fromisoformat(day)
    shift_end_at = shift_end_datetime(start_day, shift_start, shift_end)
    minutes = int((as_utc(punch_out) - shift_end_at).total_seconds() // 60)
    return minutes if minutes >= 30 else 0


def recompute_attendance(doc, employee):
    pin = as_utc(doc.get("punch_in"))
    pout = as_utc(doc.get("punch_out"))

    if doc.get("status") in NON_PRESENCE_STATUSES:
        doc["punch_in"] = None
        doc["punch_out"] = None
        doc["work_hours"] = None
        doc["late_minutes"] = 0
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
        return doc

    if pin is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    local_pin = pin.astimezone(IST)
    if attendance_shift_date(local_pin, employee) != doc["date"]:
        raise HTTPException(
            status_code=422,
            detail="Punch-in does not belong to this attendance date",
        )

    doc["punch_in"] = pin
    doc["punch_out"] = pout
    doc["late_minutes"] = compute_late_minutes(
    pin,
    employee["shift_start"],
    employee["shift_end"],
    doc["date"],
)

    if pout is None:
        doc["work_hours"] = None
        doc["overtime_minutes"] = 0
        doc["half_day"] = False
    else:
        if pout <= pin:
            raise HTTPException(
                status_code=422,
                detail="Punch-out must be after punch-in",
            )
        if pout > pin + timedelta(hours=24):
            raise HTTPException(
                status_code=422,
                detail="Punch-out cannot exceed 24 hours after punch-in",
            )

        hours = compute_work_hours(pin, pout)
        doc["work_hours"] = hours
        doc["overtime_minutes"] = compute_overtime(
            pout,
            employee["shift_start"],
            employee["shift_end"],
            doc["date"],
        )
        doc["half_day"] = hours < 4.50

    return doc


def ensure_indexes():
    db.employees.create_indexes([
        IndexModel(
            [("emp_code", ASCENDING)],
            unique=True,
            name="uq_employee_code",
        ),
        IndexModel(
            [("department", ASCENDING), ("emp_code", ASCENDING)],
            name="ix_employee_department_code",
        ),
        IndexModel(
            [("joined_on", ASCENDING), ("department", ASCENDING)],
            name="ix_employee_joined_department",
        ),
    ])

    db.attendance_logs.create_indexes([
        IndexModel(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uq_attendance_employee_date",
        ),
        IndexModel(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_date_employee",
        ),
        IndexModel(
            [("emp_code", ASCENDING), ("date", DESCENDING)],
            name="ix_attendance_employee_date",
        ),
        IndexModel(
            [("status", ASCENDING), ("date", ASCENDING)],
            name="ix_attendance_status_date",
        ),
    ])


@app.on_event("startup")
def startup():
    ensure_indexes()


# ---------------------------------------------------------------------
# Request models
# ---------------------------------------------------------------------

class EmployeeIn(BaseModel):
    model_config = ConfigDict(str_strip_whitespace=True)

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(min_length=3, max_length=120)
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = "09:30"
    shift_end: str = "18:30"
    joined_on: str

    @field_validator("email")
    @classmethod
    def validate_email(cls, value):
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", value):
            raise ValueError("Invalid email address")
        return value

    @field_validator("shift_start", "shift_end")
    @classmethod
    def validate_shift_time(cls, value):
        return validate_shift(value)

    @field_validator("joined_on")
    @classmethod
    def validate_join_date(cls, value):
        parse_date(value)
        return value

    @field_validator("shift_end")
    @classmethod
    def different_shift_times(cls, value, info):
        if info.data.get("shift_start") == value:
            raise ValueError("Shift start and end must differ")
        return value


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None
    status: str = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[int] = None


class AttendancePatch(BaseModel):
    status: Optional[str] = None
    punch_in: Optional[int] = None
    punch_out: Optional[int] = None
    reason: str = Field(min_length=1, max_length=500)
    regularized_by: str = Field(min_length=1, max_length=100)
    
# ---------------------------------------------------------------------
# Health and employee endpoints
# ---------------------------------------------------------------------

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(status_code=503, detail="MongoDB unavailable")


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(status_code=409, detail="emp_code already exists")

    return safe_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)
    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [safe_employee(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ---------------------------------------------------------------------
# Attendance endpoints
# ---------------------------------------------------------------------

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid punch-in status")

    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = (
        parse_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    local = punched_at.astimezone(IST)
    attendance_date = attendance_shift_date(local, employee)

    doc = {
        "emp_code": body.emp_code,
        "date": attendance_date,
        "status": body.status,
        "punch_in": punched_at,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
    punched_at,
    employee["shift_start"],
    employee["shift_end"],
    attendance_date,
),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            status_code=409,
            detail="Attendance already exists for this date",
        )

    return safe_attendance(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    punched_at = (
        parse_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    # Find the latest punch-in that occurred before the punch-out time.
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not record:
        raise HTTPException(
            status_code=404,
            detail="No punch-in found before this timestamp",
        )

    if record.get("punch_out") is not None:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    # MongoDB may return naive datetimes; treat them as UTC.
    punch_in_time = as_utc(record["punch_in"])

    if punched_at <= punch_in_time:
        raise HTTPException(
            status_code=422,
            detail="Punch-out must be after punch-in",
        )

    if punched_at > punch_in_time + timedelta(hours=24):
        raise HTTPException(
            status_code=422,
            detail="Punch-out cannot exceed 24 hours after punch-in",
        )

    work_hours = compute_work_hours(punch_in_time, punched_at)
    overtime = compute_overtime(
        punched_at,
        employee["shift_start"],
        employee["shift_end"],
        record["date"],
    )

    updated = db.attendance_logs.find_one_and_update(
        {"_id": record["_id"], "punch_out": None},
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime,
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance already punched out",
        )

    return safe_attendance(updated)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from:
        parse_date(date_from)
    if date_to:
        parse_date(date_to)

    if date_from and date_to and date_from > date_to:
        raise HTTPException(
            status_code=422,
            detail="date_from must not be after date_to",
        )

    if status and status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    query = {}

    if emp_code:
        query["emp_code"] = emp_code
    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}
        if date_from:
            query["date"]["$gte"] = date_from
        if date_to:
            query["date"]["$lte"] = date_to

    total = db.attendance_logs.count_documents(query)

    cursor = (
        db.attendance_logs.find(query)
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [safe_attendance(item) for item in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.patch("/attendance/{emp_code}/{attendance_date}")
def patch_attendance(
    emp_code: str,
    attendance_date: str,
    body: AttendancePatch,
):
    parse_date(attendance_date)

    record = db.attendance_logs.find_one({
        "emp_code": emp_code,
        "date": attendance_date,
    })

    if not record:
        raise HTTPException(
            status_code=404,
            detail="Attendance record not found",
        )

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    updates = body.model_dump(exclude_unset=True)
    reason = updates.pop("reason")
    regularized_by = updates.pop("regularized_by")

    if not updates:
        raise HTTPException(
            status_code=422,
            detail="No changes supplied",
        )

    new_doc = dict(record)

    for key in ("status", "punch_in", "punch_out"):
        if key in updates:
            value = updates[key]
            if key in ("punch_in", "punch_out") and value is not None:
                value = parse_ms(value)
            new_doc[key] = value

    status = new_doc.get("status")
    if status not in ALL_STATUSES:
        raise HTTPException(status_code=422, detail="Invalid status")

    if status in NON_PRESENCE_STATUSES:
        if (
            ("punch_in" in updates and updates["punch_in"] is not None)
            or ("punch_out" in updates and updates["punch_out"] is not None)
        ):
            raise HTTPException(
                status_code=422,
                detail="ABSENT and LEAVE cannot have punch times",
            )

        new_doc["punch_in"] = None
        new_doc["punch_out"] = None

    elif new_doc.get("punch_in") is None:
        raise HTTPException(
            status_code=422,
            detail="Presence status requires punch_in",
        )

    new_doc = recompute_attendance(new_doc, employee)

    changes = {}

    tracked_fields = (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
    )

    for key in tracked_fields:
        before = record.get(key)
        after = new_doc.get(key)

        before_compare = before
        after_compare = after

        if key in ("punch_in", "punch_out"):
            before_compare = as_utc(before)
            after_compare = as_utc(after)

        if before_compare != after_compare:
            changes[key] = {
                "from": before,
                "to": after,
            }
        
    if not changes:
        raise HTTPException(
            status_code=422,
            detail="Request does not change the record",
        )

    history_item = {
        "at": now_utc(),
        "by": regularized_by,
        "reason": reason,
        "changes": changes,
    }

    old_history = record.get("history", [])
    new_history = old_history + [history_item]

    # Use the original values as a concurrency check.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "history": old_history,
            "status": record["status"],
            "punch_in": record.get("punch_in"),
            "punch_out": record.get("punch_out"),
        },
        {
            "$set": {
                "status": new_doc["status"],
                "punch_in": new_doc.get("punch_in"),
                "punch_out": new_doc.get("punch_out"),
                "work_hours": new_doc.get("work_hours"),
                "late_minutes": new_doc.get("late_minutes", 0),
                "overtime_minutes": new_doc.get("overtime_minutes", 0),
                "half_day": new_doc.get("half_day", False),
                "history": new_history,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if updated is None:
        raise HTTPException(
            status_code=409,
            detail="Attendance was modified concurrently",
        )

    return safe_attendance(updated)

# ---------------------------------------------------------------------
# Monthly employee analytics
# ---------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: str):
    month_start = parse_month(month)
    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    )

    employee = db.employees.find_one({"emp_code": emp_code})
    if not employee:
        raise HTTPException(status_code=404, detail="Employee not found")

    joined = parse_date(employee["joined_on"])
    first_day = max(month_start, joined)

    # Count weekdays from the employee's joining date through month-end.
    working_days = 0
    current = first_day
    while current <= month_end:
        if current.weekday() < 5:
            working_days += 1
        current += timedelta(days=1)

    # A full presence day counts as 1, a half day as 0.5.
    # Weekend presence does not contribute to present_days.
    pipeline = [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end.isoformat(),
                },
            }
        },
        {
            "$set": {
                "_parsed_date": {
                    "$dateFromString": {"dateString": "$date"}
                }
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
                                    {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                    {
                                        "$not": [
                                            {
                                                "$in": [
                                                    {"$dayOfWeek": "$_parsed_date"},
                                                    [1, 7],
                                                ]
                                            }
                                        ]
                                    },
                                ]
                            },
                            {
                                "$cond": [
                                    {"$eq": ["$half_day", True]},
                                    0.5,
                                    1,
                                ]
                            },
                            0,
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
                "total_overtime_minutes": {"$sum": "$overtime_minutes"},
            }
        },
    ]

    result = list(db.attendance_logs.aggregate(pipeline))
    values = result[0] if result else {}

    present_days = values.get("present_days", 0)
    attendance_pct = (
        rounded(present_days / working_days * 100, 4)
        if working_days
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": values.get("leave_days", 0),
        "late_count": values.get("late_count", 0),
        "total_late_minutes": values.get("total_late_minutes", 0),
        "total_overtime_minutes": values.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


# ---------------------------------------------------------------------
# Department monthly summary
# ---------------------------------------------------------------------

@app.get("/analytics/departments/summary")
def department_summary(
    month: str,
    department: Optional[str] = None,
):
    month_start = parse_month(month)
    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    ).isoformat()

    employee_match = {"joined_on": {"$lte": month_end}}
    if department:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "emp_codes": {"$push": "$emp_code"},
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"codes": "$emp_codes"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$codes"]},
                                    {"$gte": ["$date", month_start.isoformat()]},
                                    {"$lte": ["$date", month_end]},
                                ]
                            }
                        }
                    },
                    {
                        "$set": {
                            "_parsed_date": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                       
{
    "$in": [
        "$status",
        list(PRESENCE_STATUSES),
    ]
},
                                        {
                                            "$cond": [
                                                {"$eq": ["$half_day", True]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_total": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        list(PRESENCE_STATUSES),
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        list(PRESENCE_STATUSES),
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$gt": ["$late_minutes", 0]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {"$sum": "$late_minutes"},
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "attendance",
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "attendance": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$attendance", 0]},
                        {
                            "present_days": 0,
                            "work_hours_total": 0,
                            "work_hours_count": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "leave_count": 0,
                            "on_duty_count": 0,
                        },
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]

    rows = list(db.employees.aggregate(pipeline))
    output = []

    for row in rows:
        attendance = row["attendance"]
        count = attendance.get("work_hours_count", 0)

        output.append(
            {
                "department": row["department"],
                "headcount": row["headcount"],
                "present_days": attendance.get("present_days", 0),
                "avg_work_hours": (
                    rounded(attendance["work_hours_total"] / count, 2)
                    if count
                    else None
                ),
                "late_count": attendance.get("late_count", 0),
                "total_late_minutes": attendance.get("total_late_minutes", 0),
                "leave_count": attendance.get("leave_count", 0),
                "on_duty_count": attendance.get("on_duty_count", 0),
            }
        )

    return {"month": month, "items": output}


# ---------------------------------------------------------------------
# Late leaderboard
# ---------------------------------------------------------------------

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str,
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    month_start = parse_month(month)
    month_end = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    ).isoformat()

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": month_start.isoformat(),
                    "$lte": month_end,
                },
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
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                    "rank": 1,
                }
            },
        ]
    )

    return {
        "month": month,
        "items": list(db.attendance_logs.aggregate(pipeline)),
    }

# ---------------------------------------------------------------------
# Department daily attendance trend
# ---------------------------------------------------------------------


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    start_date = parse_date(from_date)
    end_date = parse_date(to_date)

    if end_date < start_date:
        raise HTTPException(
            status_code=422,
            detail="'to' must be on or after 'from'",
        )

    if (end_date - start_date).days + 1 > 92:
        raise HTTPException(
            status_code=422,
            detail="Date range cannot exceed 92 days",
        )

    if not db.employees.find_one({"department": department}):
        raise HTTPException(
            status_code=404,
            detail="Department not found",
        )

    # Create a date spine and calculate the trend in MongoDB.
    
    # Generate the date spine in MongoDB.
    pipeline = [
        {
            "$documents": [
                {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat(),
                }
            ]
        },
        {
            "$project": {
                "_id": 0,
                "dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": "$start"
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": "$end"
                                                    }
                                                },
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "i",
                        "in": {
                            "date": {
                                "$dateToString": {
                                    "format": "%Y-%m-%d",
                                    "date": {
                                        "$dateAdd": {
                                            "startDate": {
                                                "$dateFromString": {
                                                    "dateString": "$start"
                                                }
                                            },
                                            "unit": "day",
                                            "amount": "$$i",
                                        }
                                    },
                                }
                            }
                        },
                    }
                },
            }
        },
        {"$unwind": "$dates"},
        {"$replaceRoot": {"newRoot": "$dates"}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$department", department]},
                                    {"$lte": ["$joined_on", "$$day"]},
                                ]
                            }
                        }
                    },
                    {"$project": {"_id": 0, "emp_code": 1}},
                ],
                "as": "employees",
            }
        },
        {"$set": {"headcount": {"$size": "$employees"}}},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "day": "$date",
                    "codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    },
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$date", "$$day"]},
                                    {"$in": ["$emp_code", "$$codes"]},
                                ]
                            }
                        }
                    }
                ],
                "as": "logs",
            }
        },
        {
            "$set": {
                "is_working_day": {
                    "$not": [
                        {
                            "$in": [
                                {
                                    "$dayOfWeek": {
                                        "$dateFromString": {
                                            "dateString": "$date"
                                        }
                                    }
                                },
                                [1, 7],
                            ]
                        }
                    ]
                },
                "present_count": {
                    "$sum": {
                        "$map": {
                            "input": {
                                "$filter": {
                                    "input": "$logs",
                                    "as": "log",
                                    "cond": {
                                        "$in": [
                                            "$$log.status",
                                            list(PRESENCE_STATUSES),
                                        ]
                                    },
                                }
                            },
                            "as": "log",
                            "in": {
                                "$cond": [
                                    {
                                        "$eq": [
                                            {
                                                "$ifNull": [
                                                    "$$log.half_day",
                                                    False,
                                                ]
                                            },
                                            True,
                                        ]
                                    },
                                    0.5,
                                    1,
                                ]
                            },
                        }
                    }
                },
                "late_count": {
                    "$size": {
                        "$filter": {
                            "input": "$logs",
                            "as": "log",
                            "cond": {
                                "$gt": [
                                    {"$ifNull": ["$$log.late_minutes", 0]},
                                    0,
                                ]
                            },
                        }
                    }
                },
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
                        {
                            "$divide": [
    {
        "$floor": {
            "$add": [
                {
                    "$multiply": [
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        10000,
                    ]
                },
                0.5,
            ]
        }
    },
    10000,
]
                        },
                        None,
                    ]
                },
                "_trend_date": {
                    "$dateFromString": {"dateString": "$date"}
                },
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"_trend_date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {
                            "documents": [-6, 0],
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": {
    "$cond": [
        {"$ne": ["$moving_avg_7d", None]},
        {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": ["$moving_avg_7d", 10000]},
                            0.5,
                        ]
                    }
                },
                10000,
            ]
        },
        None,
    ]
},
            }
        },
        {"$sort": {"date": 1}},
    ]

    items = list(db.aggregate(pipeline))

    return {
        "department": department,
        "items": items,
    }


@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str,
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        query = {}

        if emp_code:
            query["emp_code"] = emp_code
        if status:
            if status not in ALL_STATUSES:
                raise HTTPException(status_code=422, detail="Invalid status")
            query["status"] = status

        if date_from:
            parse_date(date_from)
        if date_to:
            parse_date(date_to)
        if date_from and date_to and date_from > date_to:
            raise HTTPException(
                status_code=422,
                detail="date_from must not be after date_to",
            )

        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = date_from
            if date_to:
                query["date"]["$lte"] = date_to

        collection = "attendance_logs"
        command = {
            "find": collection,
            "filter": query,
            "sort": {"date": -1, "emp_code": 1},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(
                status_code=422,
                detail="emp_code and month are required",
            )

        month_start = parse_month(month)
        month_end = date(
            month_start.year,
            month_start.month,
            calendar.monthrange(month_start.year, month_start.month)[1],
        ).isoformat()

        collection = "attendance_logs"
        pipeline = [
            {
                "$match": {
                    "emp_code": emp_code,
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end,
                    },
                }
            },
            {
                "$set": {
                    "_parsed_date": {
                        "$dateFromString": {"dateString": "$date"}
                    }
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
                                        {"$in": ["$status", list(PRESENCE_STATUSES)]},
                                        {
                                            "$not": [
                                                {
                                                    "$in": [
                                                        {"$dayOfWeek": "$_parsed_date"},
                                                        [1, 7],
                                                    ]
                                                }
                                            ]
                                        },
                                    ]
                                },
                                {"$cond": [{"$eq": ["$half_day", True]}, 0.5, 1]},
                                0,
                            ]
                        }
                    },
                    "leave_days": {
                        "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                    },
                    "late_count": {
                        "$sum": {"$cond": [{"$gt": ["$late_minutes", 0]}, 1, 0]}
                    },
                    "total_late_minutes": {"$sum": "$late_minutes"},
                    "total_overtime_minutes": {"$sum": "$overtime_minutes"},
                }
            },
        ]
        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        month_start = parse_month(month)
        month_end = date(
            month_start.year,
            month_start.month,
            calendar.monthrange(month_start.year, month_start.month)[1],
        ).isoformat()

        collection = "attendance_logs"
        pipeline = [
            {
                "$match": {
                    "date": {
                        "$gte": month_start.isoformat(),
                        "$lte": month_end,
                    },
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
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]

        if department:
            pipeline.append(
                {"$match": {"employee.department": department}}
            )

        pipeline.extend([
            {
                "$setWindowFields": {
                    "sortBy": {"total_late_minutes": -1},
                    "output": {"rank": {"$rank": {}}},
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
            {"$sort": {"total_late_minutes": -1, "_id": 1}},
            {
                "$project": {
                    "_id": 0,
                    "emp_code": "$_id",
                    "name": "$employee.name",
                    "department": "$employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                    "rank": 1,
                }
            },
        ])

        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    
    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(
                status_code=422,
                detail="month is required",
            )

        month_start = parse_month(month)
        month_end = date(
            month_start.year,
            month_start.month,
            calendar.monthrange(month_start.year, month_start.month)[1],
        ).isoformat()

        employee_match = {"joined_on": {"$lte": month_end}}
        if department:
            employee_match["department"] = department

        collection = "employees"
        pipeline = [
            {"$match": employee_match},
            {
                "$group": {
                    "_id": "$department",
                    "headcount": {"$sum": 1},
                    "emp_codes": {"$push": "$emp_code"},
                }
            },
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {"codes": "$emp_codes"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$in": ["$emp_code", "$$codes"]},
                                        {"$gte": ["$date", month_start.isoformat()]},
                                        {"$lte": ["$date", month_end]},
                                    ]
                                }
                            }
                        },
                        {
                            "$group": {
                                "_id": None,
                                "present_days": {
                                    "$sum": {
                                        "$cond": [
                                            {
                                                "$in": [
                                                    "$status",
                                                    list(PRESENCE_STATUSES),
                                                ]
                                            },
                                            {
                                                "$cond": [
                                                    {"$eq": ["$half_day", True]},
                                                    0.5,
                                                    1,
                                                ]
                                            },
                                            0,
                                        ]
                                    }
                                },
                                "late_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$gt": ["$late_minutes", 0]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "total_late_minutes": {
                                    "$sum": "$late_minutes"
                                },
                                "leave_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "LEAVE"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "on_duty_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$eq": ["$status", "ON_DUTY"]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                                "total_work_hours": {"$sum": "$work_hours"},
                                "work_hours_count": {
                                    "$sum": {
                                        "$cond": [
                                            {"$ne": ["$work_hours", None]},
                                            1,
                                            0,
                                        ]
                                    }
                                },
                            }
                        },
                    ],
                    "as": "attendance_summary",
                }
            },
            {
                "$set": {
                    "attendance_summary": {
                        "$ifNull": [
                            {"$arrayElemAt": ["$attendance_summary", 0]},
                            {
                                "present_days": 0,
                                "late_count": 0,
                                "total_late_minutes": 0,
                                "leave_count": 0,
                                "on_duty_count": 0,
                                "total_work_hours": 0,
                                "work_hours_count": 0,
                            },
                        ]
                    }
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "department": "$_id",
                    "headcount": 1,
                    "present_days": "$attendance_summary.present_days",
                    "late_count": "$attendance_summary.late_count",
                    "total_late_minutes": "$attendance_summary.total_late_minutes",
                    "leave_count": "$attendance_summary.leave_count",
                    "on_duty_count": "$attendance_summary.on_duty_count",
                    "avg_work_hours": {
                        "$cond": [
                            {"$gt": ["$attendance_summary.work_hours_count", 0]},
                            {
                                "$divide": [
                                    "$attendance_summary.total_work_hours",
                                    "$attendance_summary.work_hours_count",
                                ]
                            },
                            None,
                        ]
                    },
                }
            },
        ]

        command = {
            "aggregate": collection,
            "pipeline": pipeline,
            "cursor": {},
        }

    
    elif endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(
                status_code=422,
                detail="department, from, and to are required",
            )

        start_date = parse_date(from_date)
        end_date = parse_date(to_date)

        if end_date < start_date:
            raise HTTPException(
                status_code=422,
                detail="'to' must be on or after 'from'",
            )

        if (end_date - start_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail="Date range cannot exceed 92 days",
            )

        if not db.employees.find_one({"department": department}):
            raise HTTPException(
                status_code=404,
                detail="Department not found",
            )

        
        collection = "database"
        pipeline = [
            {
                "$documents": [
                    {
                        "start": start_date.isoformat(),
                        "end": end_date.isoformat(),
                    }
                ]
            },
            {
                "$project": {
                    "_id": 0,
                    "dates": {
                        "$map": {
                            "input": {
                                "$range": [
                                    0,
                                    {
                                        "$add": [
                                            {
                                                "$dateDiff": {
                                                    "startDate": {
                                                        "$dateFromString": {
                                                            "dateString": "$start"
                                                        }
                                                    },
                                                    "endDate": {
                                                        "$dateFromString": {
                                                            "dateString": "$end"
                                                        }
                                                    },
                                                    "unit": "day",
                                                }
                                            },
                                            1,
                                        ]
                                    },
                                ]
                            },
                            "as": "i",
                            "in": {
                                "date": {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": {
                                            "$dateAdd": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": "$start"
                                                    }
                                                },
                                                "unit": "day",
                                                "amount": "$$i",
                                            }
                                        },
                                    }
                                }
                            },
                        }
                    },
                }
            },
            {"$unwind": "$dates"},
            {"$replaceRoot": {"newRoot": "$dates"}},
            {
                "$lookup": {
                    "from": "employees",
                    "let": {"day": "$date"},
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$department", department]},
                                        {"$lte": ["$joined_on", "$$day"]},
                                    ]
                                }
                            }
                        },
                        {"$project": {"_id": 0, "emp_code": 1}},
                    ],
                    "as": "employees",
                }
            },
            {"$set": {"headcount": {"$size": "$employees"}}},
            {
                "$lookup": {
                    "from": "attendance_logs",
                    "let": {
                        "day": "$date",
                        "codes": {
                            "$map": {
                                "input": "$employees",
                                "as": "employee",
                                "in": "$$employee.emp_code",
                            }
                        },
                    },
                    "pipeline": [
                        {
                            "$match": {
                                "$expr": {
                                    "$and": [
                                        {"$eq": ["$date", "$$day"]},
                                        {"$in": ["$emp_code", "$$codes"]},
                                    ]
                                }
                            }
                        }
                    ],
                    "as": "logs",
                }
            },
            {
                "$set": {
                    "is_working_day": {
                        "$not": [
                            {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    [1, 7],
                                ]
                            }
                        ]
                    },
                    "present_count": {
                        "$sum": {
                            "$map": {
                                "input": {
                                    "$filter": {
                                        "input": "$logs",
                                        "as": "log",
                                        "cond": {
                                            "$in": [
                                                "$$log.status",
                                                list(PRESENCE_STATUSES),
                                            ]
                                        },
                                    }
                                },
                                "as": "log",
                                "in": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                {
                                                    "$ifNull": [
                                                        "$$log.half_day",
                                                        False,
                                                    ]
                                                },
                                                True,
                                            ]
                                        },
                                        0.5,
                                        1,
                                    ]
                                },
                            }
                        }
                    },
                    "late_count": {
                        "$size": {
                            "$filter": {
                                "input": "$logs",
                                "as": "log",
                                "cond": {
                                    "$gt": [
                                        {"$ifNull": ["$$log.late_minutes", 0]},
                                        0,
                                    ]
                                },
                            }
                        }
                    },
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
                            {
                                "$divide": [
    {
        "$floor": {
            "$add": [
                {
                    "$multiply": [
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
                        10000,
                    ]
                },
                0.5,
            ]
        }
    },
    10000,
]
                            },
                            None,
                        ]
                    },
                    "_trend_date": {
                        "$dateFromString": {"dateString": "$date"}
                    },
                }
            },
            {
                "$setWindowFields": {
                    "sortBy": {"_trend_date": 1},
                    "output": {
                        "moving_avg_7d": {
                            "$avg": "$attendance_rate",
                            "window": {"documents": [-6, 0]},
                        }
                    },
                }
            },
            {
                "$project": {
                    "_id": 0,
                    "date": 1,
                    "is_working_day": 1,
                    "headcount": 1,
                    "present_count": 1,
                    "late_count": 1,
                    "attendance_rate": 1,
                    
"moving_avg_7d": {
    "$cond": [
        {"$ne": ["$moving_avg_7d", None]},
        {
            "$divide": [
                {
                    "$floor": {
                        "$add": [
                            {"$multiply": ["$moving_avg_7d", 10000]},
                            0.5,
                        ]
                    }
                },
                10000,
            ]
        },
        None,
    ]
},
                }
            },
            {"$sort": {"date": 1}},
        ]

        command = {
            "aggregate": 1,
            "pipeline": pipeline,
            "cursor": {},
        }

    else:
        raise HTTPException(
            status_code=400,
            detail=(
                "Unsupported endpoint. Use attendance_list, employee_monthly, "
                "department_summary, late_leaderboard, or department_trend."
            ),
        )

    explanation = db.command(
        "explain",
        command,
        verbosity="executionStats",
    )

    return {
        "endpoint": endpoint,
        "collection": collection,
        "explain": explanation,
    }