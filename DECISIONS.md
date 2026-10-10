# DECISIONS.md

## 1. Indexes

I created a unique index on employee `emp_code` to prevent duplicate employee codes. I added employee indexes for department and joining-date queries. For attendance, I created a unique compound index on `emp_code` and `date` to prevent duplicate daily records, plus indexes for date-based sorting, employee history, and status/date filtering. I did not add a separate status-only index because the main status queries also use dates.

## 2. Punch-in race

If two identical punch-in requests arrive together, both may pass the initial check for an existing record. The unique attendance index on `emp_code` and `date` allows only one insert to succeed. The other insert fails with a duplicate-key error. This protects the data, although the error should be handled and returned as HTTP 409.

## 3. Ties

The leaderboard uses ranking based on total late minutes. If employees tie at the limit, employees sharing the qualifying rank can also be returned, so the result may contain more than the requested limit.

## 4. Headcount

The department summary starts with eligible employees and then looks up their attendance records. Employees with no logs remain in the results and are included in headcount.

## 5. Scaling up

For 100 times more data, I would benchmark query plans and response times on realistic data, then optimize indexes and aggregation pipelines based on the results.
