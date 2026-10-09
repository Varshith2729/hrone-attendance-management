# HROne Employee Attendance & Analytics API

A FastAPI and MongoDB backend service for managing employee records, attendance punch-in/out, manual corrections (regularizations), and high-performance attendance analytics.

## Features Implemented
- **Employee CRUD (`/employees`):** Create and list employees with unique employee codes, shift schedules, and department filtering.
- **Attendance Punch-In & Punch-Out (`/attendance/punch-in`, `/attendance/punch-out`):**
  - IST timezone (UTC+05:30) date calculation.
  - Overnight shift handling (R1: shifts ending on next calendar day).
  - Grace period & late minutes calculation (R2).
  - Work hours calculation with half-up rounding (R4, R8).
  - Overtime calculation for shifts exceeding 30 minutes past shift end (R3).
  - Half-day detection (R5).
  - Concurrency-safe punch-in (unique index) and atomic punch-out.
- **Attendance Listing & Regularization (`/attendance`, `/attendance/{emp_code}/{date}`):**
  - Database-level pagination, sorting (`date DESC, emp_code ASC`), and filtering.
  - Manual corrections with audit trail (`history` diff logging).
- **Analytics & Aggregations (`/analytics`):**
  - **Employee Monthly Summary:** Working days, present days, leave days, late count, total late/overtime minutes, attendance percentage.
  - **Department Summary:** Cross-collection aggregation including 0-log employees (R9 headcount), present days, average work hours, late/leave/on-duty counts.
  - **Late Leaderboard:** Standard competition ranking (1, 2, 2, 4) using `$rank` in `$setWindowFields`.
  - **Department Trend:** Daily time-series with `$densify` gap filling and 7-day moving average (`$setWindowFields`).
- **MongoDB Admin Explain (`/admin/explain/{endpoint}`):** Returns raw `executionStats` for endpoint queries/pipelines.

---

## Prerequisites
- **Python:** Version 3.10+ (Tested on Python 3.13)
- **MongoDB:** Version 6.0 or 7.0 (Local MongoDB or MongoDB Atlas M0 Free Tier)

---

## Environment Setup & Run Instructions

### 1. PowerShell Virtual Environment Setup & Dependencies
```powershell
# Create virtual environment
python -m venv .venv

# Activate virtual environment (Windows PowerShell)
.\.venv\Scripts\Activate.ps1

# Install requirements
pip install -r requirements.txt
```

### 2. Environment Variables (.env Configuration)
Create a `.env` file in the project root:
```env
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

### 3. Load Sample Data
Ensure MongoDB is running, then run:
```powershell
python sample_seed.py
```

### 4. Start the Application
```powershell
uvicorn app.main:app --port 8000 --reload
```

### 5. Interactive API Documentation (Swagger UI)
Open your browser and navigate to:
[http://localhost:8000/docs](http://localhost:8000/docs)

---

## Running Tests

Run unit and pipeline test scripts using Python:
```powershell
python -m unittest discover -s scratch
```
Or execute specific test suites:
```powershell
python scratch/test_phase1.py
python scratch/test_phase2.py
python scratch/test_phase3_pipelines.py
```

---

## API Overview & Example Requests

### 1. Create Employee (`POST /employees`)
```json
POST /employees
{
  "emp_code": "EMP0001",
  "name": "Asha Rao",
  "email": "asha@example.com",
  "department": "Engineering",
  "shift_start": "09:30",
  "shift_end": "18:30",
  "joined_on": "2026-01-05"
}
```

### 2. Punch In (`POST /attendance/punch-in`)
```json
POST /attendance/punch-in
{
  "emp_code": "EMP0001",
  "punched_at": 1783312500000,
  "status": "PRESENT"
}
```

### 3. Department Summary (`GET /analytics/departments/summary`)
```http
GET /analytics/departments/summary?month=2026-07
```

### 4. Late Leaderboard (`GET /analytics/leaderboard/late`)
```http
GET /analytics/leaderboard/late?month=2026-07&limit=10
```

---

## Known Limitations & Integration Notes
- **Local MongoDB Server:** A running MongoDB 6.0+ instance (local server or Atlas URI) is required for live endpoint execution.
- **Explain Verification:** Explain output execution plans return IXSCAN when database indexes created at startup are present.
