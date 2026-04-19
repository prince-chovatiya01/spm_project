"""
=============================================================================
  UNIVERSITY TIMETABLE GENERATION ALGORITHM
=============================================================================
  Pipeline:
    Step 1 — Input Parsing        : Read all tables from PostgreSQL
    Step 2 — Preprocessing        : Expand courses → sessions, tag eligibility
    Step 3 — Sorting              : Most-constrained-variable heuristic
    Step 4 — Preferred Slot Pass  : Fast-path for pinned preferences
    Step 5 — Backtracking Alloc   : Recursive CSP solver with MRV + LCV
    Step 6 — Forward Checking     : Prune future domains after each assign
    Step 7 — Optimisation Pass    : Pairwise swap local-search
    Step 8 — Conflict Reporting   : Write failures to DB + stdout

  Data structures:
    dict[int, DataClass]   — O(1) entity lookup
    set[int]               — O(1) occupancy / availability checks
    list[Session]          — ordered session queue for backtracking
    dict[int, list[int]]   — per-session live domain (forward-checking)
    list[tuple[int,int]]   — (timeslot_id, room_id) candidate pairs

  Time complexity:
    Worst-case : O(b^n)  b=branching factor per session, n=session count
    Practical  : Near-polynomial — MCV sorting + FC prune search space
                 drastically; real university schedules rarely exceed 3-4
                 backtrack levels.

  SQL mapping:
    READ  — SELECT * FROM faculty / course / batch / room / timeslot /
                         faculty_availability / constraint_rules
    WRITE — INSERT INTO timetable (course_id, faculty_id, batch_id,
                                   room_id, timeslot_id)
    LOG   — INSERT INTO conflicts (type, description)
    The DB UNIQUE constraints mirror the in-memory occupancy sets;
    both guard the same invariant at different layers.
=============================================================================
"""

# ── stdlib ────────────────────────────────────────────────────────────────
import sys
import json
import logging
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import time
from typing import Optional

# ── third-party ───────────────────────────────────────────────────────────
try:
    import psycopg2
    import psycopg2.extras
except ImportError:
    sys.exit("psycopg2 not found. Run:  pip install psycopg2-binary")

# ── logging ───────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)-8s]  %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("timetable")


# ===========================================================================
# DATABASE CONNECTION
# ===========================================================================

DB_CONFIG: dict = {
    "host":     "localhost",
    "port":     5432,
    "dbname":   "university_db",
    "user":     "postgres",
    "password": "your_password",     # ← update this
}


def get_connection() -> "psycopg2.connection":
    """Open and return a psycopg2 database connection."""
    return psycopg2.connect(**DB_CONFIG)


# ===========================================================================
# SECTION A — IN-MEMORY DATA CLASSES
# ===========================================================================

@dataclass
class Faculty:
    id:   int
    name: str
    # set of timeslot_ids where this faculty is available
    available_timeslot_ids: set = field(default_factory=set)


@dataclass
class Course:
    id:                 int
    name:               str
    lectures_per_week:  int
    is_lab:             bool
    # 1 tutorial/week auto-added when course has a lab component
    tutorials_per_week: int = 0


@dataclass
class Batch:
    id:   int
    name: str
    size: int


@dataclass
class Room:
    id:       int
    name:     str
    capacity: int
    is_lab:   bool


@dataclass
class Timeslot:
    id:         int
    day:        str    # 'Monday' … 'Friday'
    start_time: time
    end_time:   time
    slot_type:  str    # 'lecture' | 'lab' | 'tutorial'


@dataclass
class Session:
    """
    One concrete weekly meeting to be scheduled.
    Created during preprocessing; filled during allocation.
    """
    session_id:           int          # unique 0-based index
    course_id:            int
    batch_id:             int
    faculty_id:           int
    session_type:         str          # 'lecture' | 'lab' | 'tutorial'
    required_capacity:    int
    # filtered at preprocessing; shrink further during forward-checking
    eligible_room_ids:    list         # list[int]
    eligible_timeslot_ids: list        # list[int]
    preferred_timeslot_id: Optional[int] = None
    # set by the solver:
    assigned_timeslot_id: Optional[int] = None
    assigned_room_id:     Optional[int] = None


# ===========================================================================
# SECTION B — STEP 1: INPUT PARSING
# ===========================================================================

def parse_input(conn) -> dict:
    """
    Read every relevant table from PostgreSQL and return a dict of
    in-memory lookup maps.

    SQL executed (reads only):
        SELECT * FROM faculty
        SELECT * FROM faculty_availability
        SELECT * FROM course
        SELECT * FROM batch
        SELECT * FROM room
        SELECT * FROM timeslot
        SELECT * FROM constraint_rules          ← holds JSON mappings
    """
    cur = conn.cursor(cursor_factory=psycopg2.extras.DictCursor)

    # ── 1a. Faculty ───────────────────────────────────────────────────────
    cur.execute("SELECT id, name FROM faculty ORDER BY id;")
    faculty_map: dict[int, Faculty] = {
        r["id"]: Faculty(id=r["id"], name=r["name"])
        for r in cur.fetchall()
    }

    # ── 1b. Faculty availability ──────────────────────────────────────────
    # SQL: JOIN would work too; a simple loop is clearer here.
    cur.execute("SELECT faculty_id, timeslot_id FROM faculty_availability;")
    for row in cur.fetchall():
        if row["faculty_id"] in faculty_map:
            faculty_map[row["faculty_id"]].available_timeslot_ids.add(
                row["timeslot_id"]
            )

    # ── 1c. Courses ───────────────────────────────────────────────────────
    cur.execute(
        "SELECT id, name, lectures_per_week, is_lab FROM course ORDER BY id;"
    )
    course_map: dict[int, Course] = {}
    for r in cur.fetchall():
        course_map[r["id"]] = Course(
            id=r["id"],
            name=r["name"],
            lectures_per_week=r["lectures_per_week"],
            is_lab=bool(r["is_lab"]),
            tutorials_per_week=1 if r["is_lab"] else 0,
        )

    # ── 1d. Batches ───────────────────────────────────────────────────────
    cur.execute("SELECT id, name, size FROM batch ORDER BY id;")
    batch_map: dict[int, Batch] = {
        r["id"]: Batch(id=r["id"], name=r["name"], size=r["size"])
        for r in cur.fetchall()
    }

    # ── 1e. Rooms ─────────────────────────────────────────────────────────
    cur.execute(
        "SELECT id, name, capacity, is_lab FROM room ORDER BY id;"
    )
    room_map: dict[int, Room] = {
        r["id"]: Room(
            id=r["id"],
            name=r["name"],
            capacity=r["capacity"],
            is_lab=bool(r["is_lab"]),
        )
        for r in cur.fetchall()
    }

    # ── 1f. Timeslots ─────────────────────────────────────────────────────
    cur.execute(
        "SELECT id, day, start_time, end_time FROM timeslot ORDER BY id;"
    )
    timeslot_map: dict[int, Timeslot] = {}
    for r in cur.fetchall():
        st: time = r["start_time"]
        et: time = r["end_time"]
        timeslot_map[r["id"]] = Timeslot(
            id=r["id"],
            day=r["day"],
            start_time=st,
            end_time=et,
            slot_type=_classify_timeslot(r["day"], st, et),
        )

    # ── 1g. Constraint rules (JSON blobs stored as text) ──────────────────
    #
    # Because the schema has no explicit course-faculty or batch-course
    # junction tables, we store those mappings as JSON strings in
    # constraint_rules with these reserved names:
    #
    #   "course_faculty_map"  →  {"<course_id>": <faculty_id>, ...}
    #   "batch_course_map"    →  {"<batch_id>": [<course_id>, ...], ...}
    #
    # INSERT examples:
    #   INSERT INTO constraint_rules (name, value)
    #     VALUES ('course_faculty_map', '{"1":2,"2":1}');
    #   INSERT INTO constraint_rules (name, value)
    #     VALUES ('batch_course_map',   '{"1":[1,2],"2":[1,3]}');
    #
    cur.execute("SELECT name, value FROM constraint_rules;")
    rules: dict[str, str] = {
        r["name"]: r["value"] for r in cur.fetchall()
    }

    def _load_json_rule(key: str, default, transform) -> dict:
        raw = rules.get(key, "{}")
        try:
            return transform(json.loads(raw))
        except (json.JSONDecodeError, TypeError, ValueError) as exc:
            log.warning(
                "constraint_rules[%s] could not be parsed: %s  "
                "(default=%r used)",
                key, exc, default,
            )
            return default

    # {"course_id": faculty_id}
    course_faculty_map: dict[int, int] = _load_json_rule(
        "course_faculty_map",
        default={},
        transform=lambda d: {int(k): int(v) for k, v in d.items()},
    )

    # {"batch_id": [course_id, ...]}
    batch_course_map: dict[int, list[int]] = _load_json_rule(
        "batch_course_map",
        default={},
        transform=lambda d: {
            int(k): [int(c) for c in v] for k, v in d.items()
        },
    )

    cur.close()

    log.info(
        "Parsed from DB — faculty:%d  courses:%d  batches:%d  "
        "rooms:%d  timeslots:%d",
        len(faculty_map), len(course_map), len(batch_map),
        len(room_map), len(timeslot_map),
    )

    return {
        "faculty_map":        faculty_map,
        "course_map":         course_map,
        "batch_map":          batch_map,
        "room_map":           room_map,
        "timeslot_map":       timeslot_map,
        "course_faculty_map": course_faculty_map,
        "batch_course_map":   batch_course_map,
    }


def _classify_timeslot(day: str, start: time, end: time) -> str:
    """
    Map a raw DB timeslot row to one of: 'lecture' | 'lab' | 'tutorial'.

    Scheduling windows (spec):
      Lectures  : Mon–Fri  08:00–12:00
      Labs      : Mon–Fri  14:00–18:00  exactly 2-hour blocks
      Tutorials : Mon–Tue  14:00–15:00
    """
    LECTURE_DAYS  = {"Monday", "Tuesday", "Wednesday", "Thursday", "Friday"}
    TUTORIAL_DAYS = {"Monday", "Tuesday"}

    # Tutorial window is a strict subset of the lab window —
    # check tutorial FIRST to avoid misclassification.
    if (
        day in TUTORIAL_DAYS
        and start == time(14, 0)
        and end   == time(15, 0)
    ):
        return "tutorial"

    if (
        day in LECTURE_DAYS
        and start >= time(14, 0)
        and end   <= time(18, 0)
        and (end.hour - start.hour) == 2       # exactly 2 hours
    ):
        return "lab"

    if (
        day in LECTURE_DAYS
        and start >= time(8, 0)
        and end   <= time(12, 0)
    ):
        return "lecture"

    return "unknown"   # rows with unknown type are ignored during filtering


# ===========================================================================
# SECTION C — STEP 2: PREPROCESSING
# ===========================================================================

def preprocess(data: dict) -> list[Session]:
    """
    Expand every (batch, course) pair into concrete Session objects and
    tag each with:
      • session_type           — lecture / lab / tutorial
      • required_capacity      — batch.size
      • eligible_room_ids      — rooms with correct type & sufficient capacity
      • eligible_timeslot_ids  — slots of correct type where faculty is free

    Complexity: O(B × C × T × R)
      B = batches, C = courses/batch, T = timeslots, R = rooms
    """
    sessions: list[Session] = []
    sid = 0   # monotonically increasing session_id

    faculty_map        = data["faculty_map"]
    course_map         = data["course_map"]
    batch_map          = data["batch_map"]
    room_map           = data["room_map"]
    timeslot_map       = data["timeslot_map"]
    course_faculty_map = data["course_faculty_map"]
    batch_course_map   = data["batch_course_map"]

    # Pre-partition IDs by type — avoids repeated full scans
    lec_ts_ids = [
        t.id for t in timeslot_map.values() if t.slot_type == "lecture"
    ]
    lab_ts_ids = [
        t.id for t in timeslot_map.values() if t.slot_type == "lab"
    ]
    tut_ts_ids = [
        t.id for t in timeslot_map.values() if t.slot_type == "tutorial"
    ]

    lec_room_ids = [r.id for r in room_map.values() if not r.is_lab]
    lab_room_ids = [r.id for r in room_map.values() if r.is_lab]

    for batch_id, course_ids in batch_course_map.items():
        batch = batch_map.get(batch_id)
        if not batch:
            log.warning("Batch id=%d not found; skipping.", batch_id)
            continue

        for course_id in course_ids:
            course = course_map.get(course_id)
            if not course:
                log.warning(
                    "Course id=%d not found; skipping.", course_id
                )
                continue

            faculty_id = course_faculty_map.get(course_id)
            if faculty_id is None:
                log.warning(
                    "No faculty mapped to course '%s' (id=%d); skipping.",
                    course.name, course_id,
                )
                continue

            faculty = faculty_map.get(faculty_id)
            if not faculty:
                log.warning(
                    "Faculty id=%d not found; skipping.", faculty_id
                )
                continue

            avail = faculty.available_timeslot_ids

            # ── Lecture sessions ──────────────────────────────────────
            for _ in range(course.lectures_per_week):
                sessions.append(Session(
                    session_id=sid,
                    course_id=course_id,
                    batch_id=batch_id,
                    faculty_id=faculty_id,
                    session_type="lecture",
                    required_capacity=batch.size,
                    eligible_room_ids=_eligible_rooms(
                        lec_room_ids, room_map, batch.size, is_lab=False
                    ),
                    eligible_timeslot_ids=_eligible_timeslots(
                        lec_ts_ids, avail
                    ),
                ))
                sid += 1

            # ── Lab session ───────────────────────────────────────────
            if course.is_lab:
                sessions.append(Session(
                    session_id=sid,
                    course_id=course_id,
                    batch_id=batch_id,
                    faculty_id=faculty_id,
                    session_type="lab",
                    required_capacity=batch.size,
                    eligible_room_ids=_eligible_rooms(
                        lab_room_ids, room_map, batch.size, is_lab=True
                    ),
                    eligible_timeslot_ids=_eligible_timeslots(
                        lab_ts_ids, avail
                    ),
                ))
                sid += 1

            # ── Tutorial session ──────────────────────────────────────
            for _ in range(course.tutorials_per_week):
                sessions.append(Session(
                    session_id=sid,
                    course_id=course_id,
                    batch_id=batch_id,
                    faculty_id=faculty_id,
                    session_type="tutorial",
                    required_capacity=batch.size,
                    eligible_room_ids=_eligible_rooms(
                        lec_room_ids, room_map, batch.size, is_lab=False
                    ),
                    eligible_timeslot_ids=_eligible_timeslots(
                        tut_ts_ids, avail
                    ),
                ))
                sid += 1

    log.info(
        "Preprocessing complete — %d sessions generated "
        "(%d lecture, %d lab, %d tutorial).",
        len(sessions),
        sum(1 for s in sessions if s.session_type == "lecture"),
        sum(1 for s in sessions if s.session_type == "lab"),
        sum(1 for s in sessions if s.session_type == "tutorial"),
    )
    return sessions


def _eligible_rooms(
    candidate_ids: list[int],
    room_map:      dict[int, Room],
    required_cap:  int,
    is_lab:        bool,
) -> list[int]:
    """Rooms that match the required type and have sufficient capacity."""
    return [
        rid for rid in candidate_ids
        if room_map[rid].is_lab == is_lab
        and room_map[rid].capacity >= required_cap
    ]


def _eligible_timeslots(
    candidate_ids: list[int],
    faculty_avail: set[int],
) -> list[int]:
    """Timeslots where the faculty is available."""
    return [tid for tid in candidate_ids if tid in faculty_avail]


# ===========================================================================
# SECTION D — STEP 3: SORTING (Most-Constrained-Variable heuristic)
# ===========================================================================

def sort_sessions(
    sessions:    list[Session],
    faculty_map: dict[int, Faculty],
) -> list[Session]:
    """
    Sort in place so that the hardest-to-schedule sessions come first.

    Sort key (ascending — lowest = schedule first):
      [0] type_rank         : lab=0, tutorial=1, lecture=2
      [1] domain_size       : |timeslots| × |rooms|  (fewer options first)
      [2] neg_capacity      : –batch.size            (bigger batch first)
      [3] faculty_avail     : |available timeslots|  (busier faculty first)

    Rationale: scheduling the most constrained session first (MCV) minimises
    the depth of backtracking required when conflicts occur later.
    """
    TYPE_RANK = {"lab": 0, "tutorial": 1, "lecture": 2}

    def _key(s: Session) -> tuple:
        domain = len(s.eligible_timeslot_ids) * max(len(s.eligible_room_ids), 1)
        fav    = len(faculty_map[s.faculty_id].available_timeslot_ids) \
                 if s.faculty_id in faculty_map else 9_999
        return (
            TYPE_RANK.get(s.session_type, 3),
            domain,
            -s.required_capacity,
            fav,
        )

    sessions.sort(key=_key)
    log.info("Sessions sorted (MCV heuristic): lab→tutorial→lecture.")
    return sessions


# ===========================================================================
# SECTION E — OCCUPANCY STATE
# ===========================================================================

class OccupancyState:
    """
    Thread of truth for which resources are taken at each timeslot.

    Mirrors the three UNIQUE constraints in the timetable table:
        UNIQUE(batch_id,   timeslot_id)
        UNIQUE(faculty_id, timeslot_id)
        UNIQUE(room_id,    timeslot_id)

    All lookups are O(1) via Python sets.

    Also maintains per-entity ordered schedule lists for soft-constraint
    scoring (gap detection, back-to-back detection).
    """

    def __init__(self):
        # entity_id → set of occupied timeslot_ids
        self.batch_occupied:   dict[int, set[int]] = {}
        self.faculty_occupied: dict[int, set[int]] = {}
        self.room_occupied:    dict[int, set[int]] = {}
        # entity_id → sorted list of (day_idx, start_hour) for soft scoring
        self.batch_schedule:   dict[int, list[tuple]] = {}
        self.faculty_schedule: dict[int, list[tuple]] = {}

    # ── internal helpers ──────────────────────────────────────────────────
    def _set_of(self, d: dict, key: int) -> set:
        if key not in d:
            d[key] = set()
        return d[key]

    def _list_of(self, d: dict, key: int) -> list:
        if key not in d:
            d[key] = []
        return d[key]

    # ── hard-constraint checks ────────────────────────────────────────────
    def is_batch_free(self, batch_id: int, ts_id: int) -> bool:
        return ts_id not in self._set_of(self.batch_occupied, batch_id)

    def is_faculty_free(self, faculty_id: int, ts_id: int) -> bool:
        return ts_id not in self._set_of(self.faculty_occupied, faculty_id)

    def is_room_free(self, room_id: int, ts_id: int) -> bool:
        return ts_id not in self._set_of(self.room_occupied, room_id)

    def all_free(
        self, batch_id: int, faculty_id: int, room_id: int, ts_id: int
    ) -> bool:
        return (
            self.is_batch_free(batch_id, ts_id)
            and self.is_faculty_free(faculty_id, ts_id)
            and self.is_room_free(room_id, ts_id)
        )

    # ── state mutation ────────────────────────────────────────────────────
    def occupy(
        self,
        batch_id:   int,
        faculty_id: int,
        room_id:    int,
        ts_id:      int,
        ts:         Timeslot,
    ) -> None:
        self._set_of(self.batch_occupied,   batch_id).add(ts_id)
        self._set_of(self.faculty_occupied, faculty_id).add(ts_id)
        self._set_of(self.room_occupied,    room_id).add(ts_id)
        # for soft-constraint scoring
        slot_key = (_day_index(ts.day), ts.start_time.hour)
        self._list_of(self.batch_schedule,   batch_id).append(slot_key)
        self._list_of(self.faculty_schedule, faculty_id).append(slot_key)

    def release(
        self,
        batch_id:   int,
        faculty_id: int,
        room_id:    int,
        ts_id:      int,
        ts:         Timeslot,
    ) -> None:
        self._set_of(self.batch_occupied,   batch_id).discard(ts_id)
        self._set_of(self.faculty_occupied, faculty_id).discard(ts_id)
        self._set_of(self.room_occupied,    room_id).discard(ts_id)
        slot_key = (_day_index(ts.day), ts.start_time.hour)
        lst = self._list_of(self.batch_schedule, batch_id)
        if slot_key in lst:
            lst.remove(slot_key)
        lst2 = self._list_of(self.faculty_schedule, faculty_id)
        if slot_key in lst2:
            lst2.remove(slot_key)


_DAY_ORDER = {
    "Monday": 0, "Tuesday": 1, "Wednesday": 2,
    "Thursday": 3, "Friday": 4,
}

def _day_index(day: str) -> int:
    return _DAY_ORDER.get(day, 9)


# ===========================================================================
# SECTION F — STEP 4: PREFERRED SLOT ASSIGNMENT
# ===========================================================================

def assign_preferred_slots(
    sessions:     list[Session],
    state:        OccupancyState,
    timeslot_map: dict[int, Timeslot],
) -> None:
    """
    Fast-path: for sessions that have a preferred timeslot, attempt to
    assign them immediately without backtracking.

    Validates ALL hard constraints before committing:
        • batch free in that timeslot
        • faculty free in that timeslot
        • some eligible room free in that timeslot
        • preferred slot type matches session type
        • room capacity ≥ batch size  (already enforced by eligible_room_ids)

    Invalid preferences are silently dropped (logged at DEBUG level) so the
    backtracker handles them normally.

    SQL analogy:
        INSERT INTO timetable ... WHERE NOT EXISTS(conflicting row)
    """
    assigned_count = 0

    for s in sessions:
        if s.preferred_timeslot_id is None:
            continue
        if s.assigned_timeslot_id is not None:
            continue   # already scheduled

        pref_ts_id = s.preferred_timeslot_id
        ts         = timeslot_map.get(pref_ts_id)

        # ── validate type match ───────────────────────────────────────
        if ts is None or ts.slot_type != s.session_type:
            log.debug(
                "Session %d: preferred slot %d type mismatch; discarding.",
                s.session_id, pref_ts_id,
            )
            s.preferred_timeslot_id = None
            continue

        # ── validate faculty availability ─────────────────────────────
        if pref_ts_id not in s.eligible_timeslot_ids:
            log.debug(
                "Session %d: faculty unavailable in preferred slot %d.",
                s.session_id, pref_ts_id,
            )
            s.preferred_timeslot_id = None
            continue

        # ── batch + faculty conflict check ────────────────────────────
        if not state.is_batch_free(s.batch_id, pref_ts_id):
            log.debug(
                "Session %d: batch busy in preferred slot %d.", s.session_id, pref_ts_id
            )
            s.preferred_timeslot_id = None
            continue
        if not state.is_faculty_free(s.faculty_id, pref_ts_id):
            log.debug(
                "Session %d: faculty busy in preferred slot %d.", s.session_id, pref_ts_id
            )
            s.preferred_timeslot_id = None
            continue

        # ── find a free eligible room ─────────────────────────────────
        chosen_room_id = None
        for rid in s.eligible_room_ids:
            if state.is_room_free(rid, pref_ts_id):
                chosen_room_id = rid
                break

        if chosen_room_id is None:
            log.debug(
                "Session %d: no free room in preferred slot %d.",
                s.session_id, pref_ts_id,
            )
            s.preferred_timeslot_id = None
            continue

        # ── commit ────────────────────────────────────────────────────
        s.assigned_timeslot_id = pref_ts_id
        s.assigned_room_id     = chosen_room_id
        state.occupy(s.batch_id, s.faculty_id, chosen_room_id, pref_ts_id, ts)
        assigned_count += 1

    log.info(
        "Preferred slot pass: %d/%d sessions assigned.",
        assigned_count,
        sum(1 for s in sessions if s.preferred_timeslot_id is not None
            or s.assigned_timeslot_id is not None),
    )


# ===========================================================================
# SECTION G — SOFT-CONSTRAINT SCORING
# ===========================================================================

def _score_candidate(
    session:      Session,
    ts_id:        int,
    room_id:      int,
    ts:           Timeslot,
    state:        OccupancyState,
    timeslot_map: dict[int, Timeslot],
) -> int:
    """
    Compute a soft-constraint penalty for placing this session at (ts_id, room_id).

    Lower penalty = better candidate.

    Penalties (additive):
        +5  creates a gap in the batch's schedule for the day
        +4  gives the faculty a back-to-back session
        +3  increases the batch's idle time (gap between sessions)
        +2  room change relative to the batch's last room on the same day
    """
    penalty = 0
    day_idx   = _day_index(ts.day)
    new_hour  = ts.start_time.hour

    # ── batch gap (+5) and idle time (+3) ────────────────────────────────
    batch_slots = state.batch_schedule.get(session.batch_id, [])
    same_day    = sorted(
        [(d, h) for d, h in batch_slots if d == day_idx],
        key=lambda x: x[1],
    )
    if same_day:
        hours = [h for _, h in same_day] + [new_hour]
        hours.sort()
        for i in range(len(hours) - 1):
            gap = hours[i + 1] - hours[i]
            if gap > 2:
                penalty += 5   # schedule gap (free period in the middle)
            elif gap > 0:
                penalty += 3   # idle time increase

    # ── faculty back-to-back (+4) ─────────────────────────────────────────
    fac_slots    = state.faculty_schedule.get(session.faculty_id, [])
    fac_same_day = [h for d, h in fac_slots if d == day_idx]
    for fh in fac_same_day:
        ts_obj = None
        # find an existing assigned timeslot with this (day, hour) key
        for ts_candidate in timeslot_map.values():
            if (
                _day_index(ts_candidate.day) == day_idx
                and ts_candidate.start_time.hour == fh
            ):
                ts_obj = ts_candidate
                break
        if ts_obj:
            duration = ts_obj.end_time.hour - ts_obj.start_time.hour
            if abs(fh - new_hour) <= duration:
                penalty += 4

    return penalty


# ===========================================================================
# SECTION H — STEP 5 + 6: BACKTRACKING SOLVER WITH FORWARD CHECKING
# ===========================================================================

class TimetableSolver:
    """
    Recursive backtracking solver with:
      • MRV variable ordering (sessions pre-sorted before calling solve)
      • LCV value ordering  (candidates sorted by soft-constraint penalty)
      • Forward checking    (live domain maintained per session index)
    """

    def __init__(
        self,
        sessions:     list[Session],
        state:        OccupancyState,
        timeslot_map: dict[int, Timeslot],
        room_map:     dict[int, Room],
    ):
        self.sessions     = sessions
        self.state        = state
        self.timeslot_map = timeslot_map
        self.room_map     = room_map

        # live_domain[i] = list of (ts_id, room_id) pairs still available
        # for sessions[i].  Initialised from eligible_* lists.
        self.live_domain: dict[int, list[tuple[int, int]]] = {}
        for i, s in enumerate(sessions):
            if s.assigned_timeslot_id is None:    # skip already-assigned
                self.live_domain[i] = [
                    (ts_id, room_id)
                    for ts_id  in s.eligible_timeslot_ids
                    for room_id in s.eligible_room_ids
                ]

        self.failure_log: list[dict] = []   # for conflict reporting

    # ── public entry point ────────────────────────────────────────────────
    def solve(self) -> bool:
        """
        Return True if all sessions are successfully assigned.
        Mutates self.sessions[*].assigned_* fields in place.
        """
        unassigned_indices = [
            i for i, s in enumerate(self.sessions)
            if s.assigned_timeslot_id is None
        ]
        return self._assign(unassigned_indices, cursor=0)

    # ── recursive backtracker ─────────────────────────────────────────────
    def _assign(self, indices: list[int], cursor: int) -> bool:
        """
        Attempt to assign sessions[indices[cursor]].
        Returns True on global success, False to trigger backtrack.

        Step 5 of the algorithm pipeline.
        """
        if cursor == len(indices):
            return True     # ✓ all sessions scheduled

        idx     = indices[cursor]
        session = self.sessions[idx]

        # ── generate candidates from live domain ──────────────────────
        candidates = self._filter_candidates(idx, session)

        if not candidates:
            # Dead end — record and backtrack
            self._log_failure(session, reason="no_valid_candidate")
            return False

        # ── score and sort candidates (LCV) ──────────────────────────
        scored = sorted(
            (
                (
                    _score_candidate(
                        session, ts_id, room_id,
                        self.timeslot_map[ts_id],
                        self.state,
                        self.timeslot_map,
                    ),
                    ts_id,
                    room_id,
                )
                for ts_id, room_id in candidates
            ),
            key=lambda x: x[0],
        )

        for penalty, ts_id, room_id in scored:
            ts = self.timeslot_map[ts_id]

            # ── tentatively assign ────────────────────────────────────
            session.assigned_timeslot_id = ts_id
            session.assigned_room_id     = room_id
            self.state.occupy(
                session.batch_id, session.faculty_id, room_id, ts_id, ts
            )

            # ── Step 6: forward checking ──────────────────────────────
            pruned = self._forward_check(indices, cursor, ts_id, room_id)
            fc_ok  = pruned is not None   # None → a future session wiped out

            if fc_ok:
                if self._assign(indices, cursor + 1):
                    return True   # ✓ propagated success

            # ── undo (backtrack) ──────────────────────────────────────
            session.assigned_timeslot_id = None
            session.assigned_room_id     = None
            self.state.release(
                session.batch_id, session.faculty_id, room_id, ts_id, ts
            )
            if fc_ok and pruned:
                self._restore_domain(pruned)

        return False   # no candidate succeeded → propagate failure

    # ── candidate generation ──────────────────────────────────────────────
    def _filter_candidates(
        self, idx: int, session: Session
    ) -> list[tuple[int, int]]:
        """
        Apply hard constraints to the live domain for this session.

        Hard constraints checked:
          • batch   not occupied at ts_id   (UNIQUE batch_id, timeslot_id)
          • faculty not occupied at ts_id   (UNIQUE faculty_id, timeslot_id)
          • room    not occupied at ts_id   (UNIQUE room_id, timeslot_id)
          • ts.slot_type == session.session_type
          • room.capacity >= session.required_capacity  (pre-filtered but re-checked)
        """
        valid = []
        for ts_id, room_id in self.live_domain.get(idx, []):
            ts   = self.timeslot_map.get(ts_id)
            room = self.room_map.get(room_id)
            if ts is None or room is None:
                continue
            if ts.slot_type != session.session_type:
                continue
            if room.capacity < session.required_capacity:
                continue
            if not self.state.all_free(
                session.batch_id, session.faculty_id, room_id, ts_id
            ):
                continue
            valid.append((ts_id, room_id))
        return valid

    # ── forward checking (Step 6) ─────────────────────────────────────────
    def _forward_check(
        self,
        indices: list[int],
        cursor:  int,
        ts_id:   int,
        room_id: int,
    ) -> Optional[list[tuple[int, list]]]:
        """
        After assigning (ts_id, room_id), remove any (ts_id, *) or (*, room_id)
        pairs from future sessions' live domains.

        Returns:
          list of (future_idx, removed_pairs) for rollback   — on success
          None                                               — domain wipe-out detected

        Step 6 of the algorithm pipeline.
        """
        assigned_session = self.sessions[indices[cursor]]
        pruned: list[tuple[int, list]] = []

        for fi in range(cursor + 1, len(indices)):
            fidx    = indices[fi]
            fsess   = self.sessions[fidx]
            domain  = self.live_domain.get(fidx, [])
            removed = []

            new_domain = []
            for pair in domain:
                fts_id, froom_id = pair
                # same timeslot → conflict if same batch OR same faculty
                if fts_id == ts_id and (
                    fsess.batch_id   == assigned_session.batch_id
                    or fsess.faculty_id == assigned_session.faculty_id
                ):
                    removed.append(pair)
                    continue
                # same room + same timeslot → room conflict
                if froom_id == room_id and fts_id == ts_id:
                    removed.append(pair)
                    continue
                new_domain.append(pair)

            if not new_domain:
                # This future session has no options → backtrack immediately
                # Restore the entries we already pruned in this forward pass
                self.live_domain[fidx] = domain   # not yet replaced
                if pruned:
                    for pidx, prems in pruned:
                        self.live_domain[pidx].extend(prems)
                return None   # signal failure

            self.live_domain[fidx] = new_domain
            if removed:
                pruned.append((fidx, removed))

        return pruned   # success; carry for potential rollback

    def _restore_domain(self, pruned: list[tuple[int, list]]) -> None:
        """Re-add pairs removed by a failed forward-checking pass."""
        for fidx, removed_pairs in pruned:
            self.live_domain[fidx].extend(removed_pairs)

    # ── failure logging ───────────────────────────────────────────────────
    def _log_failure(self, session: Session, reason: str) -> None:
        self.failure_log.append({
            "session_id":   session.session_id,
            "course_id":    session.course_id,
            "batch_id":     session.batch_id,
            "faculty_id":   session.faculty_id,
            "session_type": session.session_type,
            "reason":       reason,
        })


# ===========================================================================
# SECTION I — STEP 7: FINAL OPTIMISATION (local search swap pass)
# ===========================================================================

def optimise(
    sessions:     list[Session],
    state:        OccupancyState,
    timeslot_map: dict[int, Timeslot],
    room_map:     dict[int, Room],
    max_passes:   int = 20,
) -> int:
    """
    Pairwise swap local search.

    For every pair (i, j) of assigned sessions, attempt to swap their
    (timeslot, room) assignments.  Accept the swap if it reduces the total
    soft-constraint penalty.  Repeat until no improvement or max_passes
    is reached.

    Complexity per pass: O(n²) — n = assigned sessions
    Returns total number of accepted swaps.
    """
    assigned = [s for s in sessions if s.assigned_timeslot_id is not None]
    total_swaps = 0

    for pass_no in range(max_passes):
        improved = False

        for i in range(len(assigned)):
            for j in range(i + 1, len(assigned)):
                si, sj = assigned[i], assigned[j]

                # Can only swap sessions of the same type
                # (so room type + timeslot type stay compatible)
                if si.session_type != sj.session_type:
                    continue

                old_ts_i, old_room_i = si.assigned_timeslot_id, si.assigned_room_id
                old_ts_j, old_room_j = sj.assigned_timeslot_id, sj.assigned_room_id

                # ── score before swap ─────────────────────────────────
                score_before = (
                    _score_candidate(
                        si, old_ts_i, old_room_i,
                        timeslot_map[old_ts_i], state, timeslot_map
                    )
                    + _score_candidate(
                        sj, old_ts_j, old_room_j,
                        timeslot_map[old_ts_j], state, timeslot_map
                    )
                )

                # ── check feasibility of swap ─────────────────────────
                # Release both, then check if each can take the other's slot
                ts_i = timeslot_map[old_ts_i]
                ts_j = timeslot_map[old_ts_j]

                state.release(si.batch_id, si.faculty_id, old_room_i, old_ts_i, ts_i)
                state.release(sj.batch_id, sj.faculty_id, old_room_j, old_ts_j, ts_j)

                # si takes sj's slot; sj takes si's slot
                swap_ok = (
                    room_map[old_room_j].capacity >= si.required_capacity
                    and room_map[old_room_i].capacity >= sj.required_capacity
                    and state.all_free(si.batch_id, si.faculty_id, old_room_j, old_ts_j)
                    and state.all_free(sj.batch_id, sj.faculty_id, old_room_i, old_ts_i)
                    and old_ts_j in si.eligible_timeslot_ids
                    and old_ts_i in sj.eligible_timeslot_ids
                )

                if swap_ok:
                    # ── score after swap ──────────────────────────────
                    # Temporarily occupy swapped positions for scoring
                    state.occupy(si.batch_id, si.faculty_id, old_room_j, old_ts_j, ts_j)
                    state.occupy(sj.batch_id, sj.faculty_id, old_room_i, old_ts_i, ts_i)

                    score_after = (
                        _score_candidate(
                            si, old_ts_j, old_room_j,
                            timeslot_map[old_ts_j], state, timeslot_map
                        )
                        + _score_candidate(
                            sj, old_ts_i, old_room_i,
                            timeslot_map[old_ts_i], state, timeslot_map
                        )
                    )

                    if score_after < score_before:
                        # Accept swap
                        si.assigned_timeslot_id = old_ts_j
                        si.assigned_room_id     = old_room_j
                        sj.assigned_timeslot_id = old_ts_i
                        sj.assigned_room_id     = old_room_i
                        total_swaps += 1
                        improved = True
                    else:
                        # Reject — undo
                        state.release(si.batch_id, si.faculty_id, old_room_j, old_ts_j, ts_j)
                        state.release(sj.batch_id, sj.faculty_id, old_room_i, old_ts_i, ts_i)
                        state.occupy(si.batch_id, si.faculty_id, old_room_i, old_ts_i, ts_i)
                        state.occupy(sj.batch_id, sj.faculty_id, old_room_j, old_ts_j, ts_j)
                else:
                    # Swap not feasible — restore original occupancy
                    state.occupy(si.batch_id, si.faculty_id, old_room_i, old_ts_i, ts_i)
                    state.occupy(sj.batch_id, sj.faculty_id, old_room_j, old_ts_j, ts_j)

        log.info(
            "Optimisation pass %d/%d — swaps so far: %d",
            pass_no + 1, max_passes, total_swaps,
        )
        if not improved:
            log.info("No improvement in pass %d — stopping early.", pass_no + 1)
            break

    return total_swaps


# ===========================================================================
# SECTION J — STEP 8: CONFLICT REPORTING
# ===========================================================================

def report_conflicts(
    sessions:     list[Session],
    failure_log:  list[dict],
    faculty_map:  dict[int, Faculty],
    course_map:   dict[int, Course],
    batch_map:    dict[int, Batch],
    conn,
) -> None:
    """
    For every unassigned session, determine the primary reason and:
      1. Print a human-readable summary to stdout.
      2. Insert a row into the conflicts table.

    Reason taxonomy:
      no_timeslots  — eligible_timeslot_ids is empty after preprocessing
      no_rooms      — eligible_room_ids is empty after preprocessing
      no_candidate  — all candidates eliminated by hard constraints at runtime
      solver_fail   — backtracker exhausted all possibilities

    SQL:
        INSERT INTO conflicts (type, description) VALUES (%s, %s)
    """
    unassigned = [s for s in sessions if s.assigned_timeslot_id is None]
    if not unassigned:
        log.info("✓ All sessions assigned successfully — no conflicts.")
        return

    cur = conn.cursor()

    for s in unassigned:
        # Determine primary failure reason
        if not s.eligible_timeslot_ids:
            reason = "no_timeslots"
            detail = (
                "Faculty has no availability in any valid "
                f"{s.session_type} timeslot."
            )
        elif not s.eligible_room_ids:
            reason = "no_rooms"
            detail = (
                f"No {s.session_type} room with capacity ≥ "
                f"{s.required_capacity} exists."
            )
        else:
            # Cross-reference solver failure log
            entry = next(
                (f for f in failure_log if f["session_id"] == s.session_id),
                None,
            )
            reason = entry["reason"] if entry else "solver_fail"
            detail = (
                "All candidate (timeslot, room) pairs violated at least "
                "one hard constraint at schedule time."
            )

        course_name  = course_map[s.course_id].name  if s.course_id  in course_map  else "?"
        batch_name   = batch_map[s.batch_id].name    if s.batch_id   in batch_map   else "?"
        faculty_name = faculty_map[s.faculty_id].name if s.faculty_id in faculty_map else "?"

        description = (
            f"Session {s.session_id} | type={s.session_type} | "
            f"course={course_name} | batch={batch_name} | "
            f"faculty={faculty_name} | reason={reason} | {detail}"
        )

        log.warning("CONFLICT — %s", description)

        cur.execute(
            "INSERT INTO conflicts (type, description) VALUES (%s, %s);",
            (reason, description),
        )

    conn.commit()
    cur.close()
    log.warning(
        "%d/%d sessions could NOT be assigned.",
        len(unassigned), len(sessions),
    )


# ===========================================================================
# SECTION K — DATABASE WRITE
# ===========================================================================

def write_timetable(sessions: list[Session], conn) -> int:
    """
    Persist all successfully assigned sessions to the timetable table.

    SQL:
        INSERT INTO timetable (course_id, faculty_id, batch_id, room_id, timeslot_id)
        VALUES (%s, %s, %s, %s, %s)
        ON CONFLICT DO NOTHING;   ← DB UNIQUE constraints as safety net

    Returns the count of rows inserted.
    """
    cur = conn.cursor()
    inserted = 0

    for s in sessions:
        if s.assigned_timeslot_id is None:
            continue
        try:
            cur.execute(
                """
                INSERT INTO timetable
                    (course_id, faculty_id, batch_id, room_id, timeslot_id)
                VALUES (%s, %s, %s, %s, %s)
                ON CONFLICT DO NOTHING;
                """,
                (
                    s.course_id,
                    s.faculty_id,
                    s.batch_id,
                    s.assigned_room_id,
                    s.assigned_timeslot_id,
                ),
            )
            inserted += cur.rowcount
        except Exception as exc:
            log.error(
                "Failed to insert session %d: %s", s.session_id, exc
            )
            conn.rollback()

    conn.commit()
    cur.close()
    log.info("Timetable written — %d rows inserted.", inserted)
    return inserted


# ===========================================================================
# SECTION L — MAIN ORCHESTRATOR
# ===========================================================================

def generate_timetable() -> None:
    """
    Full pipeline orchestrator — calls every step in order.
    """
    log.info("=" * 65)
    log.info("  UNIVERSITY TIMETABLE GENERATOR  —  starting")
    log.info("=" * 65)

    conn = get_connection()

    try:
        # ── Step 1: Input parsing ─────────────────────────────────────────
        log.info("── Step 1: Input Parsing")
        data = parse_input(conn)

        # ── Step 2: Preprocessing ─────────────────────────────────────────
        log.info("── Step 2: Preprocessing")
        sessions = preprocess(data)

        if not sessions:
            log.error("No sessions generated — check constraint_rules in DB.")
            return

        # ── Step 3: Sorting ───────────────────────────────────────────────
        log.info("── Step 3: Sorting (MCV heuristic)")
        sessions = sort_sessions(sessions, data["faculty_map"])

        # ── Shared occupancy state ────────────────────────────────────────
        state = OccupancyState()

        # ── Step 4: Preferred slot assignment ─────────────────────────────
        log.info("── Step 4: Preferred Slot Assignment")
        assign_preferred_slots(sessions, state, data["timeslot_map"])

        # ── Step 5 + 6: Backtracking solver with forward checking ─────────
        log.info("── Step 5+6: Backtracking Solver + Forward Checking")
        solver = TimetableSolver(
            sessions,
            state,
            data["timeslot_map"],
            data["room_map"],
        )
        success = solver.solve()

        if success:
            log.info("Solver completed — all sessions assigned.")
        else:
            log.warning(
                "Solver could not assign all sessions — "
                "see conflict report below."
            )

        # ── Step 7: Optimisation pass ─────────────────────────────────────
        log.info("── Step 7: Optimisation Pass (local search)")
        swaps = optimise(
            sessions, state, data["timeslot_map"], data["room_map"]
        )
        log.info("Optimisation complete — %d swaps accepted.", swaps)

        # ── Step 8: Conflict reporting ────────────────────────────────────
        log.info("── Step 8: Conflict Reporting")
        report_conflicts(
            sessions,
            solver.failure_log,
            data["faculty_map"],
            data["course_map"],
            data["batch_map"],
            conn,
        )

        # ── DB write ──────────────────────────────────────────────────────
        log.info("── Writing timetable to database")
        write_timetable(sessions, conn)

        # ── Summary ───────────────────────────────────────────────────────
        assigned   = sum(1 for s in sessions if s.assigned_timeslot_id)
        unassigned = len(sessions) - assigned
        log.info("=" * 65)
        log.info(
            "  DONE — assigned:%d  unassigned:%d  total:%d",
            assigned, unassigned, len(sessions),
        )
        log.info("=" * 65)

        # ── Pretty-print timetable for quick inspection ───────────────────
        _print_timetable(sessions, data)

    finally:
        conn.close()


# ===========================================================================
# SECTION M — PRETTY PRINTER (diagnostic output)
# ===========================================================================

def _print_timetable(sessions: list[Session], data: dict) -> None:
    """Print the generated timetable to stdout in a readable table format."""
    faculty_map  = data["faculty_map"]
    course_map   = data["course_map"]
    batch_map    = data["batch_map"]
    room_map     = data["room_map"]
    timeslot_map = data["timeslot_map"]

    col_w = [6, 22, 12, 14, 14, 18, 12]
    header = (
        f"{'SID':<{col_w[0]}}  "
        f"{'Course':<{col_w[1]}}  "
        f"{'Type':<{col_w[2]}}  "
        f"{'Batch':<{col_w[3]}}  "
        f"{'Faculty':<{col_w[4]}}  "
        f"{'Day + Time':<{col_w[5]}}  "
        f"{'Room':<{col_w[6]}}"
    )
    separator = "─" * len(header)

    print()
    print("  GENERATED TIMETABLE")
    print(separator)
    print(header)
    print(separator)

    # Sort output by day + time for readability
    def _sort_key(s: Session):
        if s.assigned_timeslot_id is None:
            return (99, 99)
        ts = timeslot_map[s.assigned_timeslot_id]
        return (_day_index(ts.day), ts.start_time.hour)

    for s in sorted(sessions, key=_sort_key):
        if s.assigned_timeslot_id is None:
            ts_str   = "UNASSIGNED"
            room_str = "—"
        else:
            ts       = timeslot_map[s.assigned_timeslot_id]
            ts_str   = f"{ts.day[:3]} {ts.start_time.strftime('%H:%M')}–{ts.end_time.strftime('%H:%M')}"
            room_str = room_map[s.assigned_room_id].name if s.assigned_room_id else "—"

        course_name  = course_map[s.course_id].name   if s.course_id  in course_map  else "?"
        batch_name   = batch_map[s.batch_id].name     if s.batch_id   in batch_map   else "?"
        faculty_name = faculty_map[s.faculty_id].name if s.faculty_id in faculty_map else "?"

        print(
            f"{s.session_id:<{col_w[0]}}  "
            f"{course_name:<{col_w[1]}}  "
            f"{s.session_type:<{col_w[2]}}  "
            f"{batch_name:<{col_w[3]}}  "
            f"{faculty_name:<{col_w[4]}}  "
            f"{ts_str:<{col_w[5]}}  "
            f"{room_str:<{col_w[6]}}"
        )

    print(separator)
    print()


# ===========================================================================
# ENTRY POINT
# ===========================================================================

if __name__ == "__main__":
    generate_timetable()
