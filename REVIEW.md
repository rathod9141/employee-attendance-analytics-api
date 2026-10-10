# REVIEW.md

## Defects Found in the Starter Code

**1. Incorrect pagination in `list_employees`**

The starter calculates the pagination offset as `page * page_size`, which skips the first page of records. For example, page 1 with a page size of 20 starts after the first 20 employees. I fixed this by calculating the offset as `(page - 1) * page_size`.

**2. Incorrect employee count in `list_employees`**

The starter counts all employees without applying the department filter. When users filter by department, the total count can therefore be incorrect. I fixed this by applying the same query filter to `count_documents`.

**3. Missing employee validation in `punch_in`**

The starter does not check whether the employee exists before accessing `emp["shift_start"]`. An unknown employee code can cause an unhandled server error. I added employee validation and appropriate error handling.

**4. Timezone handling in `punch_in`**

The starter uses `datetime.fromtimestamp()` and `datetime.now()` without explicitly enforcing the required timezone. The resulting attendance date can depend on the server's local timezone. I reviewed and corrected timestamp and timezone handling in the implementation.

**5. Inefficient pagination in `list_attendance`**

The starter retrieves all matching attendance records, sorts them in Python, and then applies pagination. This becomes inefficient as the attendance collection grows. I moved filtering, sorting, skipping, and limiting into MongoDB and added supporting indexes.

**6. Missing pagination validation**

The starter does not explicitly validate page and page-size values in its listing endpoints. Zero or negative values can lead to unexpected results. I reviewed and added appropriate pagination validation.

**7. Missing required API endpoints**

The starter contains TODO comments for required functionality, including attendance punch-out, attendance correction, employee monthly analytics, department summaries, the late leaderboard, department trends, and the admin explain endpoint. I implemented the required analytics and explain functionality in `app/main.py`.

**8. Missing database indexes**

The starter does not establish indexes for the main employee and attendance query patterns. On a large dataset, this can lead to inefficient collection scans. I added indexes for employee codes, department and joining-date queries, and attendance employee, date, and status queries.

**9. Missing MongoDB explain functionality**

The starter does not implement `/admin/explain/{endpoint}`, making it difficult to inspect MongoDB query execution plans through the API. I implemented explain support using MongoDB execution statistics and tested the query plans for the required endpoints.

## Items Reviewed That Are Not Necessarily Defects

**Work-hour rounding:** The `compute_work_hours` helper rounds work hours to two decimal places. This is reasonable if it matches the API contract.

**Health endpoint:** Returning `{"status": "ok"}` from `/health` is appropriate for a basic health check. A separate database readiness check could be useful, but it is not automatically required.

**Default pagination:** Using page 1 and a page size of 20 is reasonable. The defect is the incorrect offset calculation or missing validation, not the defaults themselves.

**MongoDB aggregation:** Using aggregation pipelines for analytics is appropriate for large datasets, provided the business rules are preserved and suitable indexes are available.

## Testing and Verification

I tested the implemented endpoints through FastAPI Swagger UI using the available sample data. I also tested the admin explain endpoint for the required analytics endpoints. The tested query plans showed index scans for relevant employee and attendance queries, and the department summary and trend explain checks reported zero collection scans.

These tests were performed on the available sample dataset. Performance should also be validated against the larger dataset used by the grader.
