# DECISIONS.md

1. **Indexes.** I index common filters and sorts. Employees have a unique `emp_code` index, plus indexes for department listings and join-date filtering. Attendance uses unique `(emp_code, date)` to enforce one daily record per employee. Date/sort and status/date indexes support list and monthly queries. I skipped a standalone `late_minutes` index because queries narrow by month first.
2. **Punch-in race.** Punch-in inserts without checking first. The unique index decides the race; duplicate-key errors become HTTP 409. MongoDB enforces this even when requests arrive concurrently.
3. **Ties.** The leaderboard uses competition ranking, so tied employees share a rank and later ranks can skip. Filtering after ranking means a tie at the cutoff may return more rows than the requested limit.
4. **Headcount.** Department summary starts with employees joined by month end, then looks up that month's attendance. Preserving employees with no logs keeps headcount accurate. Average work hours are calculated across records, not employee averages.
5. **Scale.** Trend fills missing dates, counts weekend presence, and leaves weekend rates null. At higher volume, I would pre-aggregate daily/monthly totals instead of repeatedly scanning raw logs.
