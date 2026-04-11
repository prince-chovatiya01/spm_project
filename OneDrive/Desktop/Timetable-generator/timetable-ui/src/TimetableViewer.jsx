import React, { useEffect, useState } from "react";

export default function TimetableViewer() {
  const [data, setData] = useState([]);

  useEffect(() => {
    fetch("export.json")
      .then(res => res.json())
      .then(setData);
  }, []);

  const days = ["Mon", "Tue", "Wed", "Thu", "Fri"];
  const times = [8, 9, 10, 11, 12, 14, 15, 16];

  const batches = [...new Set(data.map(d => d.batch))];

  const getCell = (batch, day, time) => {
    const entry = data.find(
      d => d.batch === batch && d.day === day && d.time === time
    );
    return entry ? `${entry.course} (${entry.room})` : "";
  };

  return (
    <div style={{ padding: 20 }}>
      <h1>📅 Timetable</h1>

      {batches.map(batch => (
        <div key={batch} style={{ marginBottom: 40 }}>
          <h2>{batch}</h2>

          <table border="1" cellPadding="8">
            <thead>
              <tr>
                <th>Day / Time</th>
                {times.map(t => (
                  <th key={t}>{t}:00</th>
                ))}
              </tr>
            </thead>

            <tbody>
              {days.map(day => (
                <tr key={day}>
                  <td><b>{day}</b></td>

                  {times.map(t => (
                    <td key={t}>
                      {getCell(batch, day, t)}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      ))}
    </div>
  );
}