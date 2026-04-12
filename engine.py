from db import *

conflicts = []

# ---------------- EXPAND LECTURES ----------------
def expand_lectures(courses):
    lectures = []

    for c in courses:
        course_id = c[0]
        lectures_count = c[2]
        is_lab = c[3]
        faculty_id = c[4]

        # ✅ LECTURES
        for _ in range(lectures_count):
            lectures.append({
                "course_id": course_id,
                "faculty_id": faculty_id,
                "type": "lecture",
                "priority": lectures_count
            })

        # ✅ ONLY ONE LAB
        if is_lab:
            lectures.append({
                "course_id": course_id,
                "faculty_id": faculty_id,
                "type": "lab",
                "priority": 10
            })

        # ✅ ONLY ONE TUTORIAL (for theory courses)
        if not is_lab and lectures_count >= 2:
            lectures.append({
                "course_id": course_id,
                "faculty_id": faculty_id,
                "type": "tutorial",
                "priority": 5
            })

    lectures.sort(key=lambda x: -x["priority"])
    return lectures


# ---------------- SLOT VALIDATION ----------------
def valid_slot(course, timeslot):
    slot_type = timeslot[4]

    if course["type"] == "lecture" and slot_type != "lecture":
        return False

    if course["type"] == "tutorial" and slot_type != "tutorial":
        return False

    if course["type"] == "lab" and slot_type != "lab":
        return False

    return True

def already_has_lecture_same_day(course_id, batch_id, timeslot_id):
    from db import cursor

    cursor.execute("""
        SELECT ts.day
        FROM timetable t
        JOIN timeslot ts ON t.timeslot_id = ts.id
        WHERE t.course_id=%s AND t.batch_id=%s
    """, (course_id, batch_id))

    rows = cursor.fetchall()

    # get current slot day
    cursor.execute("SELECT day FROM timeslot WHERE id=%s", (timeslot_id,))
    current_day = cursor.fetchone()[0]

    for r in rows:
        if r[0] == current_day:
            return True

    return False

# ---------------- BACK TO BACK ----------------
def is_back_to_back(faculty_id, ts_id):
    sched = get_faculty_schedule(faculty_id)
    return (ts_id - 1 in sched) or (ts_id + 1 in sched)


# ---------------- SCORE ----------------
def score(batch_id, faculty_id, ts_id):
    s = 0

    # soft constraint
    if is_back_to_back(faculty_id, ts_id):
        s += 5

    if ts_id not in get_batch_schedule(batch_id):
        s += 2

    return s


# ---------------- CHECK SPECIAL SESSION ----------------
def already_assigned_special(course_id, batch_id, lecture_type):
    from db import cursor

    cursor.execute("""
        SELECT ts.slot_type
        FROM timetable t
        JOIN timeslot ts ON t.timeslot_id = ts.id
        WHERE t.course_id=%s AND t.batch_id=%s
    """, (course_id, batch_id))

    rows = cursor.fetchall()

    for r in rows:
        if lecture_type == "lab" and r[0] == "lab":
            return True
        if lecture_type == "tutorial" and r[0] == "tutorial":
            return True

    return False


# ---------------- MAIN ----------------
def generate_timetable():
    courses, rooms, timeslots = get_data()
    cb = get_course_batch()

    lectures = expand_lectures(courses)

    if assign_rec(lectures, 0, cb, rooms, timeslots):
        print("✅ Timetable generated")
        print_table()
        export_json()
    else:
        print("❌ Failed")
        print_conflicts()


# ---------------- BACKTRACK ----------------
def assign_rec(lectures, i, cb, rooms, timeslots):

    if i == len(lectures):
        return True

    lec = lectures[i]
    cid = lec["course_id"]
    fid = lec["faculty_id"]

    batches = list(set([b for c, b in cb if c == cid]))

    for b in batches:

        # ❗ restrict only ONE lab/tutorial per course per batch
        if lec["type"] in ["lab", "tutorial"]:
            if already_assigned_special(cid, b, lec["type"]):
                continue

        candidates = []

        for ts in timeslots:
            ts_id = ts[0]
            # ❗ only one lecture per day per course
            if lec["type"] == "lecture":
                if already_has_lecture_same_day(cid, b, ts_id):
                    continue
            if not valid_slot(lec, ts):
                continue

            for room in rooms:
                rid = room[0]
                is_lab_room = room[3]

                reasons = []

                if lec["type"] == "lab" and not is_lab_room:
                    reasons.append("Wrong room (lab needed)")

                if lec["type"] != "lab" and is_lab_room:
                    reasons.append("Wrong room (lecture needed)")

                if not is_faculty_available(fid, ts_id):
                    reasons.append("Faculty unavailable")

                if not is_faculty_free(fid, ts_id):
                    reasons.append("Faculty busy")

                if not is_room_free(rid, ts_id):
                    reasons.append("Room busy")

                if not is_batch_free(b, ts_id):
                    reasons.append("Batch clash")

                if reasons:
                    conflicts.append((cid, b, ts_id, reasons))
                    continue

                candidates.append((score(b, fid, ts_id), ts_id, rid))

        # 🔥 prune
        candidates.sort()
        candidates = candidates[:3]

        for _, ts_id, rid in candidates:
            assign(cid, fid, b, rid, ts_id)

            if assign_rec(lectures, i + 1, cb, rooms, timeslots):
                return True

            remove_last()

    return False


# ---------------- TABLE PRINT ----------------
def print_table():
    from db import cursor

    cursor.execute("""
        SELECT b.name, ts.day, ts.start_time, ts.slot_type, c.name
        FROM timetable t
        JOIN batch b ON t.batch_id=b.id
        JOIN timeslot ts ON t.timeslot_id=ts.id
        JOIN course c ON t.course_id=c.id
        ORDER BY b.name, ts.day, ts.start_time
    """)

    rows = cursor.fetchall()

    from collections import defaultdict
    grid = defaultdict(dict)

    for b, d, t, stype, c in rows:
        label = c

        # mark soft violation
        if is_back_to_back:
            label += ""

        grid[b][(d, t)] = label

    days = ["Mon", "Tue", "Wed", "Thu", "Fri"]
    hours = [8, 9, 10, 11, 12, 14, 15, 16]

    for b in grid:
        print(f"\n📚 {b}")
        print("=" * 90)

        for d in days:
            row = []
            for h in hours:
                val = grid[b].get((d, h), "----")
                row.append(val[:10])
            print(d, "|", " | ".join(row))


# ---------------- CONFLICT ----------------
def print_conflicts():
    print("\n⚠️ Conflicts (Top 20):")
    for c in conflicts[:20]:
        print(f"Course {c[0]}, Batch {c[1]}, Slot {c[2]} → {c[3]}")