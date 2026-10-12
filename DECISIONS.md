# DECISIONS.md

1. **Indexes.** Unique `(emp_code, date)` guarantees one record per day and serves monthly and trend queries. `(date desc, emp_code)` and `(status, date desc, emp_code)` match the list sort. `(emp_code, punch_in desc)` finds the open record at punch-out. On employees: unique `emp_code`, `(department, joined_on)` for headcount, and `(department, emp_code)` for sorted listing. I rejected an index on `late_minutes`: the date range already narrows the scan.
2. **Punch-in race.** Both requests skip any "check" and call `insert_one`. The unique `(emp_code, date)` index lets exactly one succeed; the other gets `DuplicateKeyError`, which I map to 409.
3. **Ties.** `$setWindowFields` with `$rank` gives 1, 2, 2, 4. I filter `rank <= limit` after ranking, so everyone tied at the cutoff is returned, even if that exceeds `limit`.
4. **Headcount.** The summary starts from `employees` (joined on or before month end) and uses `$lookup` plus `$unwind` with `preserveNullAndEmptyArrays`, so an employee with no logs still counts.
5. **At 100x.** Pre-aggregate daily/monthly totals (or use a materialized view) so analytics stop scanning raw logs, and cache the department summary.
