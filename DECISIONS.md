# DECISIONS.md  (about 150-200 words in total - plain language, your own words)

1. **Indexes.** I create five at startup. `employees.emp_code` (unique) stops duplicate employees. `(emp_code, date)` (unique) on attendance stops duplicate punch-ins and serves the emp_code filter and the monthly report. `(date desc, emp_code)` serves the attendance list sort and the month scans. `(emp_code, punch_in desc)` finds the latest punch-in for punch-out. `employees (department, emp_code)` serves the department list. I rejected an index on `status`: it has only five values, so it would barely narrow anything.

2. **Punch-in race.** Both requests go straight to `insert_one`. The unique `(emp_code, date)` index lets MongoDB accept one. The other raises `DuplicateKeyError`, which I turn into 409. The winner gets 201.

3. **Ties.** I rank with `$rank` on `total_late_minutes` only, so ties share a rank (1, 2, 2, 4). Then I keep `rank <= limit`, so both tied employees are returned, even if that is more than `limit` rows. Rows are ordered by minutes, then emp_code.

4. **Headcount.** I count it from the `employees` collection, using `joined_on <= month end`, then merge it with the log totals. Zero-log employees still count. A department with headcount 0 is left out.

5. **100x data:** I would pre-compute the daily department numbers in a separate collection, so the trend just reads them.
