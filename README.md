# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for managing employee attendance records and generating attendance analytics.

## Features

* List attendance records with filters and pagination.
* Generate monthly attendance summaries for individual employees.
* Summarize attendance metrics by department.
* Rank employees by late attendance.
* Generate department attendance trends over a date range.
* Inspect MongoDB query execution plans through the admin explain endpoint.
* Record employee punch-in and punch-out times.
* Support attendance corrections with an audit history.

## Requirements

* Python 3.10 or newer
* MongoDB 6.0 or newer
* Git (optional, for cloning the repository)

## Setup

### 1. Get the project

Clone the repository:

```bash
git clone https://github.com/rathod9141/employee-attendance-analytics-api.git
cd employee-attendance-analytics-api
```

Alternatively, download the repository and open its folder in your terminal.

### 2. Create a virtual environment

```bash
python -m venv .venv
```

Activate it on Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

### 3. Install dependencies

```bash
pip install -r requirements.txt
```

### 4. Configure MongoDB

Make sure MongoDB is running before starting the API.

The application supports these environment variables:

* `MONGO_URI`: MongoDB connection URL. Defaults to `mongodb://localhost:27017`.
* `MONGO_DB`: Database name. Defaults to `attendance_db`.

For a local MongoDB instance using the default settings, no additional environment variables are required.

For a remote database, configure the appropriate values before starting the application. Keep credentials private and never commit secrets or `.env` files to the repository.

### 5. Load sample data

If MongoDB is running and configured, load the provided sample data:

```bash
python sample_seed.py
```

Run this command when you want to populate the database with the sample employees and attendance records.

## Run the API

Start the application:

```bash
uvicorn app.main:app --port 8000
```

If port 8000 is already in use, use port 8001:

```bash
uvicorn app.main:app --port 8001
```

## API Documentation

Once the server is running, open these URLs in your browser.

For port 8000:

* Swagger UI: http://127.0.0.1:8000/docs
* Health check: http://127.0.0.1:8000/health

If you are using port 8001, replace `8000` with `8001` in the URLs.

Swagger UI lets you explore endpoints, provide query parameters, and test API requests.

## Analytics Endpoints

The API includes the following analytics endpoints:

* `GET /attendance` — list attendance records with filtering and pagination.
* `GET /analytics/employees/{emp_code}/monthly` — retrieve monthly attendance metrics for an employee.
* `GET /analytics/departments/summary` — retrieve department attendance summaries.
* `GET /analytics/leaderboard/late` — rank employees by late attendance.
* `GET /analytics/departments/{department}/trend` — retrieve daily attendance trends for a department.

Use Swagger UI to view each endpoint's required parameters and response format.

## Admin Explain Endpoint

The API provides an endpoint for inspecting MongoDB query execution statistics:

```text
/admin/explain/{endpoint}
```

Use Swagger UI to select an endpoint and provide its required query parameters. The response includes MongoDB execution statistics that help inspect query performance and index usage.

## Project Structure

```text
employee-attendance-analytics-api/
├── app/
│   ├── __init__.py
│   └── main.py
├── sample_data/
│   ├── attendance_logs.json
│   └── employees.json
├── DATA_MODEL.md
├── DECISIONS.md
├── PROBLEM_STATEMENT.docx
├── README.md
├── REVIEW.md
├── openapi.yaml
├── requirements.txt
└── sample_seed.py
```

## Performance Testing

Run the performance test with MongoDB running and the project virtual environment activated:

```bash
python performance_test.py
```

The script creates a separate `attendance_perf_test` database with 1,000 employees and 100,000 attendance records. It creates indexes and checks query execution plans for collection scans.

The script clears the employees and attendance_logs collections in `attendance_perf_test` before inserting test data. It does not clear the application's default `attendance_db` database.

## Design Notes and Review

* `DATA_MODEL.md` documents the MongoDB data model.
* `DECISIONS.md` explains key implementation decisions.
* `REVIEW.md` describes defects reviewed in the starter code and the testing performed.
* `openapi.yaml` documents the API contract.
* `PROBLEM_STATEMENT.docx` contains the assignment requirements.

## Repository

Source code: https://github.com/rathod9141/employee-attendance-analytics-api
