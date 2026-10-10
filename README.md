# Employee Attendance & Analytics API

FastAPI and MongoDB backend for the HROne Employee Attendance & Analytics System.

## Requirements
- Python 3.11+
- MongoDB 6.0+

## Setup & Running

1. **Create and activate a virtual environment:**
   ```bash
   python -m venv .venv
   # Windows:
   .venv\Scripts\activate
   # Linux/macOS:
   source .venv/bin/activate
   ```

2. **Install dependencies:**
   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment:**
   Set `MONGO_URI` and `MONGO_DB` via environment variables or a `.env` file (environment variables override `.env`):
   ```bash
   export MONGO_URI="mongodb://localhost:27017"
   export MONGO_DB="attendance_db"
   ```

4. **Seed Sample Data (Optional):**
   ```bash
   python sample_seed.py
   ```

5. **Start Application:**
   ```bash
   uvicorn app.main:app --port 8000
   ```
   All database indexes are created idempotently on startup.
   The readiness probe is available at `GET /health`.

## Implemented Endpoints

- **System:**
  - `GET /health` (Readiness probe verifying MongoDB connectivity; 200 or 503)
- **Employees:**
  - `POST /employees` (Create employee with unique `emp_code` enforcement; 201, 409, 422)
  - `GET /employees` (List employees sorted by `emp_code` ascending with pagination and department filter)
- **Attendance:**
  - `POST /attendance/punch-in` (Record punch-in with R1 IST/overnight date rules and R2 late calculation; race-safe)
  - `POST /attendance/punch-out` (Close open punch-in, calculate R4 work hours, R3 overtime, R5 half-day; atomic update)
  - `GET /attendance` (Paginated listing sorted by `date` desc, `emp_code` asc; fast at scale)
  - `PATCH /attendance/{emp_code}/{date}` (Manual regularization with immutable audit trail and optimistic concurrency)
- **Analytics (MongoDB Aggregation Pipelines):**
  - `GET /analytics/employees/{emp_code}/monthly` (Employee monthly attendance summary with R7 working days)
  - `GET /analytics/departments/summary` (Cross-collection monthly department breakdown including zero-log headcount)
  - `GET /analytics/leaderboard/late` (Monthly late leaderboard using standard competition ranking with ties)
  - `GET /analytics/departments/{department}/trend` (Daily series with `$densify` gap-filling and 7-day moving average)
- **Admin:**
  - `GET /admin/explain/{endpoint}` (MongoDB executionStats explain output for endpoint queries and aggregations)

## Submission Files
- `app/main.py`: Complete application in a single file
- `requirements.txt`: Dependencies
- `REVIEW.md`: Comprehensive review of defects found in the starter code and their fixes
- `DECISIONS.md`: Architectural decisions and analysis
- `README.md`: System documentation
