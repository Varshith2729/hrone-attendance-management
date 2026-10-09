# REVIEW.md

List every defect you found in the starter `app/main.py` (helpers and endpoints). For each one:

| # | Where (function / line) | What is wrong | How you'd notice it (test, input, or symptom) | How you fixed it |
|---|---|---|---|---|
| 1 | `list_employees` / L93 | `skip = page * page_size` skips the first page of results entirely. | Requesting `page=1` with 20 items skips items 1–20 and returns item 21 onwards. | Corrected formula to `skip = (page - 1) * page_size`. |
| 2 | `list_employees` / L94 | `total = db.employees.count_documents({})` counts total collection size ignoring department filter. | Querying `/employees?department=Engineering` returns total count of all employees in DB. | Changed to `total = db.employees.count_documents(q)`. |
| 3 | `list_employees` / L95 | Missing `emp_code` ascending sort in query. | Returned items are in arbitrary DB insertion order rather than sorted by `emp_code`. | Added `.sort("emp_code", 1)` to MongoDB query. |
| 4 | `create_employee` & `punch_in` / L79, L104 | Concurrency race condition due to check-then-insert (`find_one` then `insert_one`) without DB indexes. | Simultaneous duplicate requests both pass `find_one` and insert duplicate documents. | Added startup unique database indexes (`employees.emp_code` and `attendance_logs.(emp_code, date)`). Caught `DuplicateKeyError` and returned HTTP 409. |
| 5 | `punch_in` / L102–103 | Naive UTC datetime creation and missing IST timezone / overnight shift date resolution (R1). | Punching in at 05:30 IST for an overnight shift (22:00–06:00) assigns today's date instead of yesterday's. | Implemented `get_ist_datetime` and `get_attendance_date` with whole-second truncation and overnight shift handling per R1. |
| 6 | `compute_late_minutes` / L35–37 | Naive time comparison and incorrect grace period check (`minutes > 10` instead of > 600s). | Punching in at 09:40:01 for a 09:30 shift returned 0 instead of 10 late minutes. | Used IST shift start datetime and checked `total_seconds() > 600` → `int(diff_seconds // 60)`. |
| 7 | `punch_in` & `list_attendance` / L119, L149 | Injects custom `"id"` string and pops `"_id"`. | Response JSON contains `"id": "..."` which violates the OpenAPI contract schema. | Removed `"id"` field creation and excluded `_id` in response formatting. |
| 8 | Endpoints returning dates / L121, L150 | Returns raw BSON/Python `datetime` objects instead of Epoch milliseconds at API boundary. | API response serialized datetimes as ISO strings or BSON objects instead of integer milliseconds. | Implemented `to_epoch_ms` to return UTC epoch milliseconds integers for all timestamps. |
| 9 | `list_attendance` / L144–149 | Loads entire database collection into Python memory (`list(find())`) and sorts in Python. | High memory usage and slow latency on 100k records. | Implemented DB-level sorting (`[("date", -1), ("emp_code", 1)]`), counting, and pagination (`skip`/`limit`). |
| 10 | `list_attendance` / L137–141 | Missing validation for invalid date range (`date_from > date_to`). | Requesting `date_from > date_to` returned an empty list instead of 422 Unprocessable Entity. | Added explicit validation check `if date_from > date_to: raise HTTPException(422)`. |
| 11 | `compute_work_hours` / L41 | Uses Python's standard `round()`, which applies Banker's Rounding (round half to even). | `2.505` rounded to `2.50` instead of `2.51` (violates half-up rounding rule R4 & R8). | Implemented `round_half_up` utility using `Decimal` with `ROUND_HALF_UP`. |
| 12 | `compute_overtime` / L46 | Does not account for overnight shifts where `shift_end` is on the next calendar day. | Punching out of an overnight shift produced negative or incorrect overtime minutes. | Handled `shift_end` on `attendance_date + 1 day` for overnight shifts (`shift_end <= shift_start`). |

## Examined Code Items Verified as Non-Defects

- **`FastAPI(title="Employee Attendance & Analytics API", version="2.0.0")`**: Version string and title match `openapi.yaml` contract exactly.
- **`load_dotenv()` at startup**: Allows local `.env` variables while allowing environment variable overrides.
- **Plain string representation for `date` ("YYYY-MM-DD") and shift times ("HH:MM")**: Correct per `DATA_MODEL.md` section 1 (calendar dates/times are not instants and must remain plain strings).
