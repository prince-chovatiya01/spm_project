-- =========================
-- FACULTY
-- =========================
INSERT INTO faculty (name) VALUES
('Dr. Mehta'),
('Prof. Sharma'),
('Dr. Patel'),
('Prof. Iyer'),
('Dr. Singh');

-- =========================
-- COURSES
-- =========================
INSERT INTO course (name, lectures_per_week, is_lab) VALUES
('Mathematics', 3, FALSE),
('Physics', 3, FALSE),
('Chemistry', 3, FALSE),
('Data Structures', 4, FALSE),
('Operating Systems', 4, FALSE),
('DBMS Lab', 2, TRUE),
('Physics Lab', 2, TRUE);

-- =========================
-- BATCHES
-- =========================
INSERT INTO batch (name, size) VALUES
('CSE-A', 60),
('CSE-B', 55),
('IT-A', 50);

-- =========================
-- ROOMS
-- =========================
INSERT INTO room (name, capacity, is_lab) VALUES
('Room-101', 60, FALSE),
('Room-102', 50, FALSE),
('Room-103', 70, FALSE),
('Lab-1', 40, TRUE),
('Lab-2', 35, TRUE);

-- =========================
-- TIMESLOTS (Mon-Fri, 5 per day)
-- =========================
INSERT INTO timeslot (day, start_time, end_time) VALUES
('Monday', '09:00', '10:00'),
('Monday', '10:00', '11:00'),
('Monday', '11:00', '12:00'),
('Monday', '13:00', '14:00'),
('Monday', '14:00', '15:00'),

('Tuesday', '09:00', '10:00'),
('Tuesday', '10:00', '11:00'),
('Tuesday', '11:00', '12:00'),
('Tuesday', '13:00', '14:00'),
('Tuesday', '14:00', '15:00'),

('Wednesday', '09:00', '10:00'),
('Wednesday', '10:00', '11:00'),
('Wednesday', '11:00', '12:00'),
('Wednesday', '13:00', '14:00'),
('Wednesday', '14:00', '15:00'),

('Thursday', '09:00', '10:00'),
('Thursday', '10:00', '11:00'),
('Thursday', '11:00', '12:00'),
('Thursday', '13:00', '14:00'),
('Thursday', '14:00', '15:00'),

('Friday', '09:00', '10:00'),
('Friday', '10:00', '11:00'),
('Friday', '11:00', '12:00'),
('Friday', '13:00', '14:00'),
('Friday', '14:00', '15:00');

-- =========================
-- FACULTY AVAILABILITY (sample mapping)
-- =========================
INSERT INTO faculty_availability (faculty_id, timeslot_id)
SELECT f.id, t.id
FROM faculty f, timeslot t
WHERE t.id % 2 = f.id % 2;  -- simple distribution for demo

-- =========================
-- CONSTRAINT RULES (optional)
-- =========================
INSERT INTO constraint_rules (name, value) VALUES
('max_lectures_per_day', '3'),
('no_back_to_back', 'true'),
('lab_priority', 'true');