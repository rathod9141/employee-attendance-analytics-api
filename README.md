# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee attendance records and analytics.

## Features

* List attendance records with filters and pagination.
* Generate monthly attendance summaries for individual employees.
* Summarize attendance metrics by department.
* Rank employees by late attendance.
* Generate department attendance trends over a date range.
* Inspect MongoDB query execution plans through the admin explain endpoint.

## Requirements

* Python 3.10 or newer
* MongoDB 6.0 or newer

## Setup

Create and activate a virtual environment, then install the dependencies.

```bash
python -m venv .venv
```

On Windows PowerShell:

```powershell
.venv\Scripts\Activate.ps1
```

Install dependencies:

```bash
pip install -r requirements.txt
```

Configure the MongoDB connection using the environment variables expected by the application. Do not commit credentials or `.env` files to this repository.

Load the sample data if your MongoDB configuration is ready:

```bash
python sample_seed.py
```

## Run the API

Start the application with:

```bash
uvicorn app.main:app --port 8000
```

If port 8000 is already in use on your development machine, use port 8001 for local testing.

## API documentation

After starting the server, open:

* Swagger UI: `http://127.0.0.1:8000/docs`
* Health check: `http://127.0.0.1:8000/health`

If running on port 8001, replace `8000` with `8001` in these URLs.

## Admin explain endpoint

The API provides an endpoint to inspect MongoDB execution statistics:

```text
/admin/explain/{endpoint}
```

Use Swagger UI to select an endpoint and supply its required query parameters.

## Project structure

```text
app/
  __init__.py
  main.py
requirements.txt
openapi.yaml
DATA_MODEL.md
DECISIONS.md
REVIEW.md
PROBLEM_STATEMENT.docx
sample_data/
sample_seed.py
```

## Design notes and review

See `DECISIONS.md` for implementation decisions and `REVIEW.md` for the solution review.

## Repository

Source code: https://github.com/rathod9141/employee-attendance-analytics-api
