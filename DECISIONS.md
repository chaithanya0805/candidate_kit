# DECISIONS.md

1. **Indexes.**
On `employees`: unique `{"emp_code": 1}` enforces business-key uniqueness; `{"department": 1, "joined_on": 1}` backs department filtering and period headcount.
On `attendance_logs`: unique `{"emp_code": 1, "date": 1}` prevents duplicate daily records; `{"date": -1, "emp_code": 1}` backs attendance pagination without in-memory sorting; `{"emp_code": 1, "punch_in": -1}` resolves punch-outs instantly; `{"date": 1, "late_minutes": 1, "emp_code": 1}` accelerates late leaderboard aggregations.
*Rejected:* an index on `punch_out` alone, because punch-out lookups are always scoped by `emp_code` and `punch_in`.

2. **Punch-in race.**
Both requests execute concurrently. Rather than check-then-insert, both attempt insertion. MongoDB's unique index on `(emp_code, date)` guarantees atomic serialization at the database layer: exactly one insert succeeds (201), while the other raises a `DuplicateKeyError`, which our exception handler intercepts to return HTTP 409.

3. **Ties.**
Using MongoDB `$setWindowFields` with `$rank`, tied employees share the same rank and subsequent ranks are skipped (standard competition ranking: 1, 2, 2, 4). The `$match` filters by `rank <= limit`. If ties occur at the threshold, all tied employees are returned (exceeding `limit` rows), ordered by `total_late_minutes` descending, then `emp_code` ascending.

4. **Headcount.**
The pipeline originates on the `employees` collection matching `joined_on <= month_end`. It computes headcount from employee documents directly, then performs a `$lookup` to join their monthly `attendance_logs`. Employees with zero logs still count toward department headcount.

5. **One thing you would change for 100x data.**
Introduce daily/monthly materialized rollups updated asynchronously at punch-out and regularization, preventing large multi-stage pipeline scans over tens of millions of raw attendance records during analytics requests.
