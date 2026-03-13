import time
import requests
from gpiozero import MatrixKeypad, OutputDevice
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522  # Adjust import if using a different MFRC522 fork

# =======================================
# CONFIGURATION FILES
# =======================================

CONFIG_FILES = {
    'zone':         '/opt/zone.txt',
    'device':       '/opt/device.txt',
    'team':         '/opt/team.txt',
    'auth_endpoint': '/opt/auth_endpoint.txt',
    'audit_endpoint': '/opt/audit_endpoint.txt',
}

def read_config(file_path, default=None):
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return default

# Load all config values once at startup
ZONE_CODE      = read_config(CONFIG_FILES['zone'], default="UNKNOWN_ZONE")
SOURCE_DEVICE  = read_config(CONFIG_FILES['device'], default="team4-pi")
SOURCE_TEAM    = read_config(CONFIG_FILES['team'], default="Team4")
AUTH_ENDPOINT  = read_config(CONFIG_FILES['auth_endpoint'])
AUDIT_ENDPOINT = read_config(CONFIG_FILES['audit_endpoint'])

if not AUTH_ENDPOINT or not AUDIT_ENDPOINT:
    print("CRITICAL: One or both API endpoints are not configured!")
    print("Please create and populate:")
    print("  /opt/auth_endpoint.txt")
    print("  /opt/audit_endpoint.txt")
    # You could sys.exit(1) here in production

# =======================================
# HARDWARE SETUP
# =======================================

# RFID reader
reader = MFRC522()

# Keypad (4x3 matrix - adjust pins to YOUR wiring!)
keypad = MatrixKeypad(
    row_pins=[21, 20, 16, 12],     # 4 rows
    col_pins=[26, 19, 13],         # 3 columns
    keys=[
        ['1', '2', '3'],
        ['4', '5', '6'],
        ['7', '8', '9'],
        ['*', '0', '#']
    ]
)

# LCD (16x2 I2C at 0x27 - change if needed)
lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1,
              cols=16, rows=2, dotsize=8,
              charmap='A00', auto_linebreaks=True)

# Door unlock relay/signal (active high; set active_high=False if needed)
unlock_pin = 18
door = OutputDevice(unlock_pin, active_high=True, initial_value=False)

# =======================================
# MAIN LOOP
# =======================================
while True:
    badge_uid = None

    # 1. Wait for RFID badge
    lcd.clear()
    lcd.write_string("Scan badge...")
    
    while not badge_uid:
        (status, TagType) = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            (status, uid) = reader.MFRC522_Anticoll()  # Some libs use MFRC522_SelectTagSN()
            if status == reader.MI_OK:
                badge_uid = ''.join(format(x, '02x') for x in uid).upper()
                print(f"Badge UID: {badge_uid}")
        time.sleep(0.1)

    # 2. Prompt for PIN
    lcd.clear()
    lcd.write_string("Enter PIN:")

    pin_code = ""
    while len(pin_code) < 4:
        pressed = keypad.keys_pressed
        for key in pressed:
            if key.isdigit():
                pin_code += key
                lcd.cursor_pos = (1, len(pin_code) - 1)
                lcd.write_string('*')
                time.sleep(0.25)  # debounce
                break
        time.sleep(0.05)

    print(f"PIN entered (not logged): ****")

    # 3. First API: Authentication
    auth_payload = {
        "p_badge_uid": badge_uid,
        "p_pin_code": pin_code,
        "p_zone_code": ZONE_CODE,
        "p_source_device": SOURCE_DEVICE,
        "p_source_team": SOURCE_TEAM
    }

    access_granted = False
    denial_reason = None

    try:
        resp_auth = requests.post(AUTH_ENDPOINT, json=auth_payload, timeout=6)
        resp_auth.raise_for_status()
        data = resp_auth.json()

        access_granted = data.get("access_granted", False)
        denial_reason  = data.get("denial_reason") or "Unknown"

    except requests.exceptions.RequestException as e:
        print(f"Auth API failed: {e}")
        lcd.clear()
        lcd.write_string("Server Error")
        time.sleep(4)
        continue

    # 4. Show result on LCD
    lcd.clear()

    if access_granted:
        lcd.write_string("Access Granted")
        door.on()
        time.sleep(15)
        door.off()
    else:
        lcd.write_string("Denied:")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(denial_reason[:16])  # truncate if too long

    # 5. Second API: Audit/Log attempt (NO PIN)
    audit_payload = {
        "badge_uid": badge_uid,
        "zone_code": ZONE_CODE,
        "source_device": SOURCE_DEVICE,
        "source_team": SOURCE_TEAM,
        "access_granted": access_granted,
        "denial_reason": denial_reason if not access_granted else None,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S")
    }

    try:
        resp_audit = requests.post(AUDIT_ENDPOINT, json=audit_payload, timeout=5)
        resp_audit.raise_for_status()
        print("Audit log sent successfully")
    except requests.exceptions.RequestException as e:
        print(f"Audit API failed (non-critical): {e}")

    # Reset for next attempt
    time.sleep(3)
    lcd.clear()