<img width="1377" height="910" alt="image" src="https://github.com/user-attachments/assets/8bebd658-1690-49eb-8a51-e50e69833c43" />

# TodoTimerTxt

A lightweight, keyboard-driven task manager built around the simplicity of the `todo.txt` format.

This project is a **spiritual successor** to https://github.com/benrhughes/todotxt.net, reimagined in Python with a focus on simplicity and task timing.

---

## Features

- Plain-text `todo.txt` storage
- Built-in task timing
    - Start and stop timers on tasks
    - Automatically records time spent directly into the `todo.txt` line
- Keyboard-based workflow
- Fast task creation and editing
- Dynamic priority adjustment
- Archive completed tasks to a configurable `archive.txt`
- Support for tags and metadata
- Live reload and save
- Python codebase

---

## File Format

Tasks remain compatible with the standard `todo.txt` format, with optional metadata extensions for time tracking.

The app may add fields like these to a task line:

- `created:YYYY-MM-DD-HH-MM-SS`
- `started:YYYY-MM-DD-HH-MM-SS`
- `lastworked:YYYY-MM-DD-HH-MM-SS`
- `spent:HH:MM:SS`

Example:

    (A) 2026-04-23 Write README +project created:2026-04-23-14-30-00 started:2026-04-23-15-10-12 lastworked:2026-04-23-15-42-08 spent:00:31:56

Field meanings:

- `created` is when the task was created
- `started` is when the current timer was started
- `lastworked` is when work was last stopped or recorded
- `spent` is the total accumulated time spent on the task

## Running

    python app.py

Or, for a windowed version without a console:

    pythonw app.pyw

## Archiving

Use the File menu to open or create an `archive.txt` file. Press `Ctrl+Shift+A`
to move all completed tasks from the current `todo.txt` into that archive file.

---

## Inspiration

- todo.txt by Gina Trapani
- https://github.com/benrhughes/todotxt.net

---

## License

This project is licensed under the Apache License 2.0. See the LICENSE file for details.
