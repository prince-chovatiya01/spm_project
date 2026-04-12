-- =========================
-- DROP (for clean reset)
-- =========================
DROP TABLE IF EXISTS timetable CASCADE;
DROP TABLE IF EXISTS conflicts CASCADE;
DROP TABLE IF EXISTS constraint_rules CASCADE;
DROP TABLE IF EXISTS faculty CASCADE;
DROP TABLE IF EXISTS course CASCADE;
DROP TABLE IF EXISTS batch CASCADE;
DROP TABLE IF EXISTS room CASCADE;
DROP TABLE IF EXISTS timeslot CASCADE;

-- =========================
-- FACULTY
-- =========================
CREATE TABLE faculty (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL
);

-- =========================
-- COURSE
-- =========================
CREATE TABLE course (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100) NOT NULL,
    lectures_per_week INT NOT NULL,
    is_lab BOOLEAN DEFAULT FALSE
);

-- =========================
-- BATCH
-- =========================
CREATE TABLE batch (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50) NOT NULL,
    size INT NOT NULL
);

-- =========================
-- ROOM
-- =========================
CREATE TABLE room (
    id SERIAL PRIMARY KEY,
    name VARCHAR(50),
    capacity INT NOT NULL,
    is_lab BOOLEAN DEFAULT FALSE
);

-- =========================
-- TIMESLOT
-- =========================
CREATE TABLE timeslot (
    id SERIAL PRIMARY KEY,
    day VARCHAR(20),
    start_time TIME,
    end_time TIME
);

-- =========================
-- CONSTRAINT RULES
-- =========================
CREATE TABLE constraint_rules (
    id SERIAL PRIMARY KEY,
    name VARCHAR(100),
    value TEXT
);

-- =========================
-- TIMETABLE
-- =========================
CREATE TABLE timetable (
    id SERIAL PRIMARY KEY,
    course_id INT REFERENCES course(id),
    faculty_id INT REFERENCES faculty(id),
    batch_id INT REFERENCES batch(id),
    room_id INT REFERENCES room(id),
    timeslot_id INT REFERENCES timeslot(id)
);

-- =========================
-- CONFLICTS
-- =========================
CREATE TABLE conflicts (
    id SERIAL PRIMARY KEY,
    type VARCHAR(100),
    description TEXT
);