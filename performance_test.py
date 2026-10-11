
from datetime import date, timedelta
from pymongo import MongoClient, ASCENDING, DESCENDING

MONGO_URI = "mongodb://localhost:27017"
DB_NAME = "attendance_perf_test"

TOTAL_EMPLOYEES = 1000
DAYS_PER_EMPLOYEE = 100
TOTAL_RECORDS = TOTAL_EMPLOYEES * DAYS_PER_EMPLOYEE

client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
db = client[DB_NAME]


def main():
    try:
        client.admin.command("ping")
        print(f"Connected to MongoDB. Test database: {DB_NAME}")
        print(f"Preparing {TOTAL_RECORDS:,} attendance records...")

        # Only clear the separate performance-test database.
        db.employees.delete_many({})
        db.attendance_logs.delete_many({})

        departments = ["Engineering", "Marketing", "Sales", "HR"]
        employees = []

        for i in range(1, TOTAL_EMPLOYEES + 1):
            employees.append({
                "emp_code": f"PERF{i:04d}",
                "name": f"Performance Employee {i}",
                "department": departments[i % len(departments)],
                "joined_on": "2025-01-01",
                "shift_start": "09:30",
                "shift_end": "18:30",
            })

        db.employees.insert_many(employees)

        start_date = date(2026, 1, 1)
        batch = []

        for i in range(1, TOTAL_EMPLOYEES + 1):
            emp_code = f"PERF{i:04d}"

            for day_number in range(DAYS_PER_EMPLOYEE):
                attendance_date = (
                    start_date + timedelta(days=day_number)
                ).isoformat()

                batch.append({
                    "emp_code": emp_code,
                    "date": attendance_date,
                    "status": "PRESENT",
                    "punch_in": None,
                    "punch_out": None,
                    "work_hours": 8.0,
                    "late_minutes": 0,
                    "overtime_minutes": 0,
                    "half_day": False,
                })

                if len(batch) >= 5000:
                    db.attendance_logs.insert_many(batch)
                    batch.clear()

        if batch:
            db.attendance_logs.insert_many(batch)

        print("Creating indexes...")

        db.employees.create_index(
            [("emp_code", ASCENDING)],
            unique=True,
            name="uq_employee_code",
        )
        db.employees.create_index(
            [("department", ASCENDING), ("emp_code", ASCENDING)],
            name="ix_employee_department_code",
        )
        db.employees.create_index(
            [("joined_on", ASCENDING), ("department", ASCENDING)],
            name="ix_employee_joined_department",
        )

        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", ASCENDING)],
            unique=True,
            name="uq_attendance_employee_date",
        )
        db.attendance_logs.create_index(
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            name="ix_attendance_date_employee",
        )
        db.attendance_logs.create_index(
            [("emp_code", ASCENDING), ("date", DESCENDING)],
            name="ix_attendance_employee_date",
        )
        db.attendance_logs.create_index(
            [("status", ASCENDING), ("date", ASCENDING)],
            name="ix_attendance_status_date",
        )

        print("\nVerifying record counts...")
        print("Employees:", db.employees.count_documents({}))
        print("Attendance records:", db.attendance_logs.count_documents({}))

        tests = [
            (
                "Employee attendance by date",
                {
                    "emp_code": "PERF0001",
                    "date": {
                        "$gte": "2026-01-01",
                        "$lte": "2026-04-10",
                    },
                },
            ),
            (
                "Attendance by date range",
                {
                    "date": {
                        "$gte": "2026-02-01",
                        "$lte": "2026-02-28",
                    },
                },
            ),
            (
                "Attendance by status and date",
                {
                    "status": "PRESENT",
                    "date": {
                        "$gte": "2026-02-01",
                        "$lte": "2026-02-28",
                    },
                },
            ),
        ]

        def has_collscan(node):
            if isinstance(node, dict):
                if node.get("stage") == "COLLSCAN":
                    return True
                return any(has_collscan(v) for v in node.values())
            if isinstance(node, list):
                return any(has_collscan(v) for v in node)
            return False

        print("\nExplain-plan results:")

        for name, query_filter in tests:
            plan = db.command(
                "explain",
                {
                    "find": "attendance_logs",
                    "filter": query_filter,
                },
                verbosity="executionStats",
            )

            stats = plan.get("executionStats", {})
            winning_plan = plan.get("queryPlanner", {}).get(
                "winningPlan", {}
            )

            print(f"\n{name}")
            print("Winning plan:", winning_plan)
            print("Documents examined:", stats.get("totalDocsExamined"))
            print("Keys examined:", stats.get("totalKeysExamined"))
            print("COLLSCAN detected:", has_collscan(winning_plan))

        print("\nPerformance test completed.")

    finally:
        client.close()


if __name__ == "__main__":
    main()