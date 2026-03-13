import time
import requests
from requests.auth import HTTPBasicAuth
from gpiozero import MatrixKeypad, OutputDevice
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522  # Adjust import if using a different MFRC522 library/fork

# =======================================
# CONFIGURATION FILES (all values come from /opt/*.txt)
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
    """Read a config file safely, return default on failure."""
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return default

# Load configuration at startup
ZONE_CODE      = read_config(CONFIG_FILES['zone'], default="UNKNOWN_ZONE")
SOURCE_DEVICE  = read_config(CONFIG_FILES['device'], default="team4-pi")
SOURCE_TEAM    = read_config(CONFIG_FILES['team'], default="Team4")
AUTH_ENDPOINT  = read_config(CONFIG_FILES['auth_endpoint'])
AUDIT_ENDPOINT = read_config(CONFIG_FILES['audit_endpoint'])
AUTH_USERNAME  = read_config(CONFIG_FILES['auth_username'])
AUTH_PASSWORD  = read_config(CONFIG_FILES['auth_password'])

# Basic validation / warnings
if not AUTH_ENDPOINT:
    print("CRITICAL: Auth endpoint missing (/opt/auth_endpoint.txt)")
if not AUTH_USERNAME or not AUTH_PASSWORD:
    print("WARNING: Basic Auth credentials missing — auth requests will likely fail")
if not AUDIT_ENDPOINT:
    print("WARNING: Audit endpoint missing — logging will be skipped")

# =======================================
# HARDWARE INITIALIZATION
# =======================================

# RFID reader (MFRC522 via SPI)
reader = MFRC522()

# Keypad - 4 rows × 3 columns (adjust GPIO pins to match your wiring!)
keypad = MatrixKeypad(
    row_pins=[21, 20, 16, 12],     # GPIO pins for rows
    col_pins=[26, 19, 13],         # GPIO pins for columns
    keys=[
        ['1', '2', '3'],
        ['4', '5', '6'],
        ['7', '8', '9'],
        ['*', '0', '#']
    ]
)

# LCD - 16×2 I2C (PCF8574 backpack at address 0x27 - change if different)
lcd = CharLCD(
    i2c_expander='PCF8574',
    address=0x27,
    port=1,
    cols=16,
    rows=2,
    dotsize=8,
    charmap='A00',
    auto_linebreaks=True
)

# Door unlock output (relay or MOSFET - active high assumed)
unlock_pin = 18  # ← CHANGE THIS to your actual GPIO pin
door = OutputDevice(unlock_pin, active_high=True, initial_value=False)

# =======================================
# MAIN APPLICATION LOOP
# =======================================
print("RFID + PIN Door Access started. Waiting for badge scan...")

while True:
    badge_uid = None

    # 1. Wait for RFID badge scan
    lcd.clear()
    lcd.write_string("Scan badge...")

    while not badge_uid:
        (status, TagType) = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            (status, uid) = reader.MFRC522_Anticoll()  # Some forks use MFRC522_SelectTagSN()
            if status == reader.MI_OK:
                badge_uid = ''.join(format(x, '02x') for x in uid).upper()
                print(f"Badge detected: {badge_uid}")
        time.sleep(0.1)

    # 2. Prompt for 4-digit PIN
    lcd.clear()
    lcd.write_string("Enter PIN:")

    pin_code = ""
    while len(pin_code) < 4:
        pressed_keys = keypad.keys_pressed
        for key in pressed_keys:
            if key.isdigit():
                pin_code += key
                # Show asterisk on second line
                lcd.cursor_pos = (1, len(pin_code) - 1)
                lcd.write_string('*')
                time.sleep(0.25)  # simple debounce
                break
        time.sleep(0.05)

    print(f"PIN entered (not logged): ****")

    # 3. Send authentication request (with Basic Auth)
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
        resp = requests.post(
            AUTH_ENDPOINT,
            json=auth_payload,
            auth=HTTPBasicAuth(AUTH_USERNAME, AUTH_PASSWORD),
            timeout=6
        )
        resp.raise_for_status()
        data = resp.json()

        access_granted = data.get("access_granted", False)
        denial_reason = data.get("denial_reason") or "Unknown reason"

    except requests.exceptions.RequestException as e:
        print(f"Authentication API error: {e}")
        lcd.clear()
        lcd.write_string("Server Error")
        time.sleep(4)
        continue

    # 4. Display result & control door
    lcd.clear()

    if access_granted:
        lcd.write_string("Access Granted")
        door.on()
        time.sleep(15)
        door.off()
    else:
        lcd.write_string("Denied:")
        lcd.cursor_pos = (1, 0)
        lcd.write_string(denial_reason[:16])  # truncate to fit display

    # 5. Send audit/log entry (WITHOUT PIN)
    if AUDIT_ENDPOINT:
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
            requests.post(AUDIT_ENDPOINT, json=audit_payload, timeout=5)
            print("Audit log sent")
        except requests.exceptions.RequestException as e:
            print(f"Audit log failed (non-critical): {e}")

    # Short cooldown before next scan
    time.sleep(3)
    lcd.clear()