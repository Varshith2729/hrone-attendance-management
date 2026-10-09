# DECISIONS.md

1. **Indexes.** Created unique index `employees.emp_code` (enforces employee uniqueness and fast lookup) and compound unique index `attendance_logs.(emp_code, date)` (enforces 1 record per employee per day and prevents punch-in race conditions). Created `attendance_logs.(date DESC, emp_code ASC)` for fast list sorting, `attendance_logs.(emp_code, date DESC)` for employee monthly analytics, and `employees.department` for lookup joins. Considered and rejected an index on `punch_in` alone because queries address records by natural key `(emp_code, date)`.

2. **Punch-in race.** When two identical punch-in requests arrive simultaneously, both compute the exact same IST attendance date `date`. Both attempt `db.attendance_logs.insert_one(doc)`. The compound unique database index `(emp_code, date)` atomically allows exactly one request to succeed (returning 201 Created) and raises a `DuplicateKeyError` for the second. The exception handler catches this and returns 409 Conflict.

3. **Ties.** Competition ranking (`$rank` in `$setWindowFields`) assigns equal rank to tied `total_late_minutes`. Filtering `rank <= limit` applies *after* ranking, so if two employees tie at rank 10 with `limit=10`, both are returned. Tied rows are sorted deterministically by `total_late_minutes` DESC, then `emp_code` ASC.

4. **Headcount.** The department summary aggregation pipeline starts from the `employees` collection (filtered by `joined_on <= month_end`), performing a `$lookup` to `attendance_logs` followed by `$unwind` with `preserveNullAndEmptyArrays: True`. This ensures every active employee in the department is counted in `headcount`, even if they have zero attendance logs.

5. **100x Scaling.** If data scaled 100x, I would pre-aggregate daily department trends and monthly summaries using MongoDB Change Streams into pre-computed analytics collections (`department_daily_summary`, `employee_monthly_summary`), eliminating heavy real-time cross-collection `$lookup` and `$densify` aggregations.
