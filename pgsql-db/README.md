# Database Setup

## Step 1: Create Database
CREATE DATABASE timetable_db;

## Step 2: Run Schema
psql -U postgres -d timetable_db -f schema.sql

## Step 3: Seed Data
psql -U postgres -d timetable_db -f seed.sql

## Tables Included
- faculty
- course
- batch
- room
- timeslot
- timetable
- conflicts
- constraint_rules