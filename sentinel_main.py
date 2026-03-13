import time
import sys
import argparse
import requests
from requests.auth import HTTPBasicAuth
from gpiozero import DigitalOutputDevice, Button, OutputDevice
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522  # Adjust import if using different MFRC522 fork
import os

# =======================================
# COMMAND-LINE ARGUMENTS
# =======================================

parser = argparse.ArgumentParser(description="RFID + PIN Door Access Controller")
parser.add_argument('--badge', '-b', type=str, help="Simulate RFID badge UID (hex string)")
parser.add_argument('--pin', '-p', type=str, help="Simulate 4-digit PIN code")
args = parser.parse_args()

SIMULATE_MODE = args.badge is not None and args.pin is not None

if SIMULATE_MODE:
    if len(args.pin) != 4 or not args.pin.isdigit():
        print("Error: --pin must be exactly 4 digits")
        sys.exit(1)
    print(f"Simulation mode: Using badge={args.badge}, pin={args.pin}")
else:
    print("Interactive mode: Waiting for real badge scan and keypad input")

# =======================================
# CONFIGURATION FILES
# =======================================

CONFIG_FILES = {
    'zone':           '/opt/zone.txt',
    'device':         '/opt/device.txt',
    'team':           '/opt/team.txt',
    'auth_endpoint':  '/opt/auth_endpoint.txt',
    'audit_endpoint': '/opt/audit_endpoint.txt',
    'auth_username':  '/opt/auth_username.txt',
    'auth_password':  '/opt/auth_password.txt',
}

def read_config(file_path, default=None):
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return default

# Load config (except zone)
SOURCE_DEVICE  = read_config(CONFIG_FILES['device'], default="team4-pi")
SOURCE_TEAM    = read_config(CONFIG_FILES['team'], default="Team4")
AUTH_ENDPOINT  = read_config(CONFIG_FILES['auth_endpoint'])
AUDIT_ENDPOINT = read_config(CONFIG_FILES['audit_endpoint'])
AUTH_USERNAME  = read_config(CONFIG_FILES['auth_username'])
AUTH_PASSWORD  = read_config(CONFIG_FILES['auth_password'])

if not AUTH_ENDPOINT:
    print("CRITICAL: Auth endpoint not configured (/opt/auth_endpoint.txt)")
if not AUTH_USERNAME or not AUTH_PASSWORD:
    print("WARNING: Basic Auth credentials missing")

# =======================================
# ZONE SETUP (prompt if file missing)
# =======================================

ZONE_FILE = CONFIG_FILES['zone']
zone_options = {
    '1': 'EXEC',
    '2': 'HRFILE',
    '3': 'ITROOM',
    '4': 'LAB',
    '5': 'LOBBY'
}

if os.path.exists(ZONE_FILE):
    ZONE_CODE = read_config(ZONE_FILE)
    if not ZONE_CODE:
        ZONE_CODE = "UNKNOWN_ZONE"
    print(f"Zone loaded: {ZONE_CODE}")
else:
    print("Zone file not found — setting up via keypad...")
    ZONE_CODE = None
    selected_key = None

    if SIMULATE_MODE:
        while selected_key not in zone_options:
            selected_key = input("Enter zone number (1-5): ").strip()
            if selected_key in zone_options:
                ZONE_CODE = zone_options[selected_key]
            else:
                print("Invalid choice — must be 1,2,3,4, or 5")
    else:
        # We'll initialize hardware early for zone setup if needed
        pass  # Handled after hardware init

# =======================================
# HARDWARE INITIALIZATION
# =======================================

reader = None
lcd    = None
door   = None

# Keypad setup with gpiozero
ROW_PINS = [21, 20, 16, 12]      # Adjust to your wiring
COL_PINS = [26, 19, 13]          # 4x3 keypad
KEY_MAP = [
    ['1', '2', '3'],
    ['4', '5', '6'],
    ['7', '8', '9'],
    ['*', '0', '#']
]

rows = [DigitalOutputDevice(pin, active_high=True, initial_value=True) for pin in ROW_PINS]
cols = [Button(pin, pull_up=True, bounce_time=0.02) for pin in COL_PINS]

def read_key():
    """Scan keypad once and return pressed key or None"""
    for row_idx, row in enumerate(rows):
        # Activate current row (set low)
        for r in rows:
            r.value = (r == row)  # only current row low
        time.sleep(0.001)
        
        for col_idx, col in enumerate(cols):
            if col.is_pressed:
                # Restore rows
                for r in rows:
                    r.value = True
                return KEY_MAP[row_idx][col_idx]
    
    # Restore rows if no press
    for r in rows:
        r.value = True
    return None

# Initialize other hardware only in non-simulation mode
if not SIMULATE_MODE:
    # RFID
    try:
        reader = MFRC522()
        print("RFID reader initialized")
    except Exception as e:
        print(f"RFID init failed: {e}")

    # LCD
    try:
        lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1,
                      cols=16, rows=2, dotsize=8, charmap='A00', auto_linebreaks=True)
        print("LCD initialized")
    except Exception as e:
        print(f"LCD init failed: {e}")

    # Door relay
    try:
        door = OutputDevice(18, active_high=True, initial_value=False)  # ← your GPIO pin
        print("Door output initialized")
    except Exception as e:
        print(f"Door init failed: {e}")

# Now handle zone setup if needed (requires keypad & LCD)
if not os.path.exists(ZONE_FILE) and not SIMULATE_MODE:
    if lcd:
        lcd.clear()
        lcd.write_string("Set Zone: 1-5")
    print("Waiting for zone selection on keypad...")
    
    selected_key = None
    while selected_key not in zone_options:
        key = read_key()
        if key and key in zone_options:
            selected_key = key
            ZONE_CODE = zone_options[selected_key]
            if lcd:
                lcd.clear()
                lcd.write_string(f"Zone: {ZONE_CODE}")
                time.sleep(2)
        time.sleep(0.02)
    
    if ZONE_CODE:
        try:
            with open(ZONE_FILE, 'w') as f:
                f.write(ZONE_CODE)
            print(f"Zone saved: {ZONE_CODE}")
        except Exception as e:
            print(f"Could not save zone file: {e}")
    else:
        ZONE_CODE = "UNKNOWN_ZONE"
        print("Zone setup cancelled — using default")

# =======================================
# MAIN LOOP
# =======================================

def main_loop():
    while True:
        # 1. Get badge UID
        if SIMULATE_MODE:
            badge_uid = args.badge.upper()
            print(f"[SIM] Badge: {badge_uid}")
            if lcd:
                lcd.clear()
                lcd.write_string("Sim Badge OK")
                time.sleep(1)
        else:
            if not reader:
                print("No RFID reader available")
                break
            badge_uid = None
            if lcd:
                lcd.clear()
                lcd.write_string("Scan badge...")
            while not badge_uid:
                (status, _) = reader.MFRC522_Request(reader.PICC_REQIDL)
                if status == reader.MI_OK:
                    (_, uid) = reader.MFRC522_Anticoll()
                    if _ == reader.MI_OK:
                        badge_uid = ''.join(format(x, '02x') for x in uid).upper()
                        print(f"Badge detected: {badge_uid}")
                time.sleep(0.1)

        # 2. Get PIN
        if SIMULATE_MODE:
            pin_code = args.pin
            print(f"[SIM] PIN: ****")
            if lcd:
                lcd.clear()
                lcd.write_string("Sim PIN: ****")
                time.sleep(1.5)
        else:
            if lcd:
                lcd.clear()
                lcd.write_string("Enter PIN:")
            pin_code = ""
            last_key = None
            while len(pin_code) < 4:
                key = read_key()
                if key and key.isdigit() and key != last_key:
                    pin_code += key
                    if lcd:
                        lcd.cursor_pos = (1, len(pin_code) - 1)
                        lcd.write_string('*')
                    last_key = key
                    time.sleep(0.3)  # debounce
                elif not key:
                    last_key = None
                time.sleep(0.02)

        print(f"PIN entered (not logged): ****")

        # 3. Authentication API
        auth_payload = {
            "p_badge_uid": badge_uid,
            "p_pin_code": pin_code,
            "p_zone_code": ZONE_CODE,
            "p_source_device": SOURCE_DEVICE,
            "p_source_team": SOURCE_TEAM
        }

        granted = False
        reason = "Unknown"

        try:
            resp = requests.post(
                AUTH_ENDPOINT,
                json=auth_payload,
                auth=HTTPBasicAuth(AUTH_USERNAME, AUTH_PASSWORD),
                timeout=6
            )
            resp.raise_for_status()
            data = resp.json()
            granted = data.get("access_granted", False)
            reason = data.get("denial_reason") or "Unknown"
        except Exception as e:
            print(f"Auth API error: {e}")
            if lcd:
                lcd.clear()
                lcd.write_string("Server Error")
                time.sleep(4)
            continue

        # 4. Display result & control door
        if lcd:
            lcd.clear()
        if granted:
            print("Access GRANTED")
            if lcd:
                lcd.write_string("Access Granted")
            if door:
                door.on()
                time.sleep(15)
                door.off()
        else:
            print(f"Access DENIED: {reason}")
            if lcd:
                lcd.write_string("Denied:")
                lcd.cursor_pos = (1, 0)
                lcd.write_string(reason[:16])

        # 5. Audit log (no PIN)
        if AUDIT_ENDPOINT:
            audit_payload = {
                "badge_uid": badge_uid,
                "zone_code": ZONE_CODE,
                "source_device": SOURCE_DEVICE,
                "source_team": SOURCE_TEAM,
                "access_granted": granted,
                "denial_reason": reason if not granted else None,
                "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
            }
            try:
                requests.post(AUDIT_ENDPOINT, json=audit_payload, timeout=5)
                print("Audit log sent")
            except Exception as e:
                print(f"Audit failed (non-critical): {e}")

        time.sleep(3)
        if lcd:
            lcd.clear()

        if SIMULATE_MODE:
            print("Simulation complete.")
            break

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        # Cleanup
        for r in rows:
            r.close()
        for c in cols:
            c.close()
        if door:
            door.off()
            door.close()
        print("Hardware cleaned up")