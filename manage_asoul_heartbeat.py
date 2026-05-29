#!/usr/bin/env python3
"""
A-SOUL Support Process Manager
Checks live status and manages heartbeat processes with proper locking.
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Change to the asoul-support directory
asoul_support_dir = Path(__file__).parent.resolve()
os.chdir(asoul_support_dir)

# Check if .cookies.json exists
if not Path('.cookies.json').exists():
    print("ERROR: .cookies.json not found. Exiting.")
    sys.exit(1)

# Define members (same as in heartbeat.py)
MEMBERS = [
    {"name": "嘉然",   "uid": 672328094,         "room": 22637261},
    {"name": "贝拉",   "uid": 672353429,         "room": 22632424},
    {"name": "乃琳",   "uid": 672342685,         "room": 22625027},
    {"name": "心宜",   "uid": 3537115310721181,  "room": 30849777},
    {"name": "思诺",   "uid": 3537115310721781,  "room": 30858592},
]

# Function to get live status using the heartbeat script's --check-only --json
def get_live_status():
    cmd = [sys.executable, 'scripts/heartbeat.py', '--check-only', '--json']
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"ERROR: Failed to get live status: {result.stderr}")
        return {}
    try:
        data = json.loads(result.stdout)
        live_members = data.get('live', [])
        live_dict = {member['name']: member for member in live_members}
        return live_dict
    except json.JSONDecodeError as e:
        print(f"ERROR: Failed to parse JSON output: {e}")
        print(f"Output was: {result.stdout[:200]}")
        return {}

# Get current live status
live_status = get_live_status()
print(f"Current live members: {[m['name'] for m in live_status.values()]}")

# Lock directory
LOCK_DIR = Path("/tmp/asoul_heartbeat_locks")
LOCK_DIR.mkdir(exist_ok=True)

# For each member, check lock and start if needed
for member in MEMBERS:
    name = member['name']
    room_id = member['room']
    lock_file = LOCK_DIR / f"{room_id}.lock"
    
    # Check if lock file exists and if the process is still running
    lock_valid = False
    if lock_file.exists():
        try:
            with open(lock_file, 'r') as f:
                pid_str = f.read().strip()
            if pid_str:
                pid = int(pid_str)
                # Check if process exists
                os.kill(pid, 0)  # Does not kill, just checks existence
                lock_valid = True
                print(f"  {name}: Lock exists with PID {pid} (process running)")
            else:
                print(f"  {name}: Lock file exists but empty -> removing")
                lock_file.unlink()
        except (OSError, ValueError, ProcessLookupError):
            # Process does not exist or invalid pid -> remove lock file
            print(f"  {name}: Lock file exists but process not found (PID {pid_str if 'pid_str' in locals() else 'unknown'}) -> removing")
            lock_file.unlink(missing_ok=True)
    
    # If member is live and no valid lock, start the heartbeat script
    if name in live_status:
        if not lock_valid:
            print(f"  {name}: Starting heartbeat process...")
            # Start the heartbeat script in the background
            log_dir = Path('logs')
            log_dir.mkdir(exist_ok=True)
            log_file = log_dir / f"heartbeat_{name}_{int(time.time())}.log"
            cmd = [sys.executable, 'scripts/heartbeat.py', '--until-offline', '--members', name]
            with open(log_file, 'w') as f:
                proc = subprocess.Popen(cmd, stdout=f, stderr=subprocess.STDOUT)
            # Write the PID to the lock file
            with open(lock_file, 'w') as f:
                f.write(str(proc.pid))
            print(f"  {name}: Started heartbeat process with PID {proc.pid}, logging to {log_file}")
        else:
            print(f"  {name}: Heartbeat process already running (PID from lock file)")
    else:
        # Member is not live
        if lock_file.exists():
            print(f"  {name}: Member is not live, but lock file exists -> removing (process should have exited or will exit)")
            lock_file.unlink()
        else:
            print(f"  {name}: Member is not live, no lock file")

print("Done.")