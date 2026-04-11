import psycopg2
import json

conn = psycopg2.connect(
    dbname="timetable_db",
    user="postgres",
    password="123",
    host="localhost",
    port="5432"
)

cursor = conn.cursor()

# ---------------- CLEAR ----------------
def clear_timetable():
    cursor.execute("DELETE FROM timetable")
    conn.commit()

# ---------------- FETCH ----------------
def get_data():
    cursor.execute("SELECT * FROM course")
    courses = cursor.fetchall()

    cursor.execute("SELECT * FROM room")
    rooms = cursor.fetchall()

    cursor.execute("SELECT * FROM timeslot")
    timeslots = cursor.fetchall()

    return courses, rooms, timeslots

def get_course_batch():
    cursor.execute("SELECT course_id, batch_id FROM course_batch")
    return cursor.fetchall()

def get_preferred_slots(course_id):
    cursor.execute("SELECT timeslot_id FROM preferred_slot WHERE course_id=%s", (course_id,))
    return [row[0] for row in cursor.fetchall()]

# ---------------- AVAILABILITY ----------------
def is_faculty_available(faculty_id, timeslot_id):
    cursor.execute("""
        SELECT 1 FROM faculty_availability
        WHERE faculty_id=%s AND timeslot_id=%s
    """, (faculty_id, timeslot_id))
    return cursor.fetchone() is not None

# ---------------- CONSTRAINTS ----------------
def is_faculty_free(faculty_id, timeslot_id):
    cursor.execute("SELECT 1 FROM timetable WHERE faculty_id=%s AND timeslot_id=%s", (faculty_id, timeslot_id))
    return cursor.fetchone() is None

def is_room_free(room_id, timeslot_id):
    cursor.execute("SELECT 1 FROM timetable WHERE room_id=%s AND timeslot_id=%s", (room_id, timeslot_id))
    return cursor.fetchone() is None

def is_batch_free(batch_id, timeslot_id):
    cursor.execute("SELECT 1 FROM timetable WHERE batch_id=%s AND timeslot_id=%s", (batch_id, timeslot_id))
    return cursor.fetchone() is None

# ---------------- SCHEDULE ----------------
def get_batch_schedule(batch_id):
    cursor.execute("SELECT timeslot_id FROM timetable WHERE batch_id=%s", (batch_id,))
    return [row[0] for row in cursor.fetchall()]

def get_faculty_schedule(faculty_id):
    cursor.execute("SELECT timeslot_id FROM timetable WHERE faculty_id=%s", (faculty_id,))
    return [row[0] for row in cursor.fetchall()]

# ---------------- INSERT ----------------
def assign(course_id, faculty_id, batch_id, room_id, timeslot_id):
    cursor.execute("""
        INSERT INTO timetable (course_id, faculty_id, batch_id, room_id, timeslot_id)
        VALUES (%s, %s, %s, %s, %s)
    """, (course_id, faculty_id, batch_id, room_id, timeslot_id))
    conn.commit()

def remove_last():
    cursor.execute("""
        DELETE FROM timetable WHERE id = (
            SELECT id FROM timetable ORDER BY id DESC LIMIT 1
        )
    """)
    conn.commit()

# ---------------- EXPORT ----------------
def export_json():
    cursor.execute("""
        SELECT b.name, ts.day, ts.start_time, c.name, r.name
        FROM timetable t
        JOIN batch b ON t.batch_id = b.id
        JOIN timeslot ts ON t.timeslot_id = ts.id
        JOIN course c ON t.course_id = c.id
        JOIN room r ON t.room_id = r.id
        ORDER BY b.name, ts.day, ts.start_time
    """)

    rows = cursor.fetchall()

    data = []
    for r in rows:
        data.append({
            "batch": r[0],
            "day": r[1],
            "time": r[2],
            "course": r[3],
            "room": r[4]
        })

    with open("export.json", "w") as f:
        json.dump(data, f, indent=4)

    print("📦 JSON exported")