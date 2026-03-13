import time
import sys
import argparse
import requests
from requests.auth import HTTPBasicAuth
from gpiozero import MatrixKeypad, OutputDevice
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522  # Adjust import if using different MFRC522 fork

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

# Load config
ZONE_CODE      = read_config(CONFIG_FILES['zone'], default="UNKNOWN_ZONE")
SOURCE_DEVICE  = read_config(CONFIG_FILES['device'], default="team4-pi")
SOURCE_TEAM    = read_config(CONFIG_FILES['team'], default="Team4")
AUTH_ENDPOINT  = read_config(CONFIG_FILES['auth_endpoint'])
AUDIT_ENDPOINT = read_config(CONFIG_FILES['audit_endpoint'])
AUTH_USERNAME  = read_config(CONFIG_FILES['auth_username'])
AUTH_PASSWORD  = read_config(CONFIG_FILES['auth_password'])

if not AUTH_ENDPOINT:
    print("CRITICAL: Auth endpoint not configured")
if not AUTH_USERNAME or not AUTH_PASSWORD:
    print("WARNING: Basic Auth credentials missing")

# =======================================
# HARDWARE (only initialize if not in pure simulation)
# =======================================

reader = None
keypad = None
lcd    = None
door   = None

if not SIMULATE_MODE:
    # RFID
    try:
        reader = MFRC522()
    except Exception as e:
        print(f"RFID init failed: {e} → continuing without reader")

    # Keypad (4x3 example - adjust pins!)
    try:
        keypad = MatrixKeypad(
            row_pins=[21, 20, 16, 12],
            col_pins=[26, 19, 13],
            keys=[['1','2','3'], ['4','5','6'], ['7','8','9'], ['*','0','#']]
        )
    except Exception as e:
        print(f"Keypad init failed: {e}")

    # LCD
    try:
        lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1,
                      cols=16, rows=2, dotsize=8, charmap='A00', auto_linebreaks=True)
    except Exception as e:
        print(f"LCD init failed: {e}")

    # Door relay
    try:
        door = OutputDevice(18, active_high=True, initial_value=False)  # ← your pin
    except Exception as e:
        print(f"Door output init failed: {e}")

# =======================================
# MAIN LOGIC
# =======================================

def main_loop():
    while True:
        # 1. Get badge UID
        if SIMULATE_MODE:
            badge_uid = args.badge.upper()
            print(f"[SIM] Badge UID: {badge_uid}")
            if lcd: lcd.clear(); lcd.write_string("Sim: Badge OK")
        else:
            if not reader:
                print("No RFID reader → exiting")
                break
            badge_uid = None
            if lcd: lcd.clear(); lcd.write_string("Scan badge...")
            while not badge_uid:
                (status, _) = reader.MFRC522_Request(reader.PICC_REQIDL)
                if status == reader.MI_OK:
                    (_, uid) = reader.MFRC522_Anticoll()
                    if _ == reader.MI_OK:
                        badge_uid = ''.join(format(x, '02x') for x in uid).upper()
                        print(f"Badge: {badge_uid}")
                time.sleep(0.1)

        # 2. Get PIN
        if SIMULATE_MODE:
            pin_code = args.pin
            print(f"[SIM] PIN: {pin_code}")
            if lcd:
                lcd.clear()
                lcd.write_string("Sim PIN: ****")
                time.sleep(1.5)
        else:
            if not keypad:
                print("No keypad → exiting")
                break
            if lcd: lcd.clear(); lcd.write_string("Enter PIN:")
            pin_code = ""
            while len(pin_code) < 4:
                pressed = keypad.keys_pressed
                for key in pressed:
                    if key.isdigit():
                        pin_code += key
                        if lcd: lcd.cursor_pos = (1, len(pin_code)-1); lcd.write_string('*')
                        time.sleep(0.25)
                        break
                time.sleep(0.05)

        # 3. Auth API call
        auth_payload = {
            "p_badge_uid": badge_uid,
            "p_pin_code": pin_code,
            "p_zone_code": ZONE_CODE,
            "p_source_device": SOURCE_DEVICE,
            "p_source_team": SOURCE_TEAM
        }

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
            print(f"Auth failed: {e}")
            if lcd: lcd.clear(); lcd.write_string("Server Error"); time.sleep(4)
            continue

        # 4. Show result & unlock if granted
        if lcd: lcd.clear()
        if granted:
            print("Access GRANTED")
            if lcd: lcd.write_string("Access Granted")
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
                print("Audit sent")
            except Exception as e:
                print(f"Audit failed: {e}")

        time.sleep(3)
        if lcd: lcd.clear()

        if SIMULATE_MODE:
            print("Simulation complete.")
            break  # Exit after one run in sim mode

if __name__ == "__main__":
    try:
        main_loop()
    except KeyboardInterrupt:
        print("\nStopped by user")
    finally:
        # Cleanup (optional but good practice)
        if door: door.off()