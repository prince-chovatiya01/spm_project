# Timetable Generator Backend

## Setup

1. Install dependencies:
pip install -r requirements.txt

2. Setup PostgreSQL:
- Create DB: timetable_db
- Run SQL scripts provided

3. Run project:
python main.py

---

## Structure

- db.py → Database connection + queries
- engine.py → Scheduling algorithm (CSP + backtracking)
- main.py → Entry point

---

## Data Contract

Function used by engine:

get_all_data() → returns:
- courses
- rooms
- timeslots
- course_batch
- faculty_availability

---

## Constraints Implemented

- No faculty clash
- No batch clash
- No room clash
- Lab vs lecture room constraint
- Faculty availability
- Max 1 lab/tutorial per week
- Max 1 lecture per day
- Soft constraint: avoid back-to-back

---

## For Ayush (Engine)

Implement:
generate_timetable(data)

Return:
timetable, conflicts

---

## For Kathan (API)

Build APIs:

POST /generate-timetable
GET /timetable/batch/{id}
GET /timetable/faculty/{id}
GET /timetable/room/{id}
GET /conflicts

---

## Notes

- DB is source of truth
- Engine must not directly modify DB
- Use provided helper functions