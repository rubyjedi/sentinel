import time
import requests
from gpiozero import MatrixKeypad, OutputDevice
from signal import pause
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522  # or from mfrc522 import MFRC522 if using different fork

# =======================================
# CONFIGURATION
# =======================================

# RFID reader
reader = MFRC522()

# Keypad (4x3 matrix example - adjust pins to match your wiring)
keypad = MatrixKeypad(
    row_pins=[21, 20, 16, 12],      # 4 rows
    col_pins=[26, 19, 13],          # 3 columns
    keys=[
        ['1', '2', '3'],
        ['4', '5', '6'],
        ['7', '8', '9'],
        ['*', '0', '#']
    ]
)

# LCD (I2C - common PCF8574 backpack at 0x27; change address if needed)
lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1,
              cols=16, rows=2, dotsize=8,
              charmap='A00', auto_linebreaks=True)

# Door unlock relay / signal pin (active high assumed; change pin and active_high if needed)
unlock_pin = 18  # Example GPIO pin connected to relay or door controller
door = OutputDevice(unlock_pin, active_high=True, initial_value=False)

# API endpoint (replace with real URL)
API_ENDPOINT = "https://your-server.com/api/verify"  # ← CHANGE THIS

def read_file(file_path):
    try:
        with open(file_path, 'r') as f:
            return f.read().strip()
    except Exception as e:
        print(f"Error reading {file_path}: {e}")
        return ""

# =======================================
# MAIN LOOP
# =======================================
while True:
    badge_uid = None

    # 1. Wait for RFID card
    lcd.clear()
    lcd.write_string("Scan badge...")
    
    while not badge_uid:
        (status, TagType) = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            (status, uid) = reader.MFRC522_Anticoll()  # or reader.MFRC522_SelectTagSN() in some forks
            if status == reader.MI_OK:
                badge_uid = ''.join(format(x, '02x') for x in uid).upper()
                print(f"Badge UID: {badge_uid}")
        time.sleep(0.1)

    # 2. Prompt for PIN
    lcd.clear()
    lcd.write_string("Enter PIN:")

    pin_code = ""
    while len(pin_code) < 4:
        keys = keypad.keys_pressed  # or use when_pressed callback if preferred
        for key in keys:
            if key.isdigit():
                pin_code += key
                # Show * on second line for privacy
                lcd.cursor_pos = (1, len(pin_code) - 1)
                lcd.write_string('*')
                time.sleep(0.2)  # simple debounce
                break
        time.sleep(0.05)

    print(f"PIN entered: {pin_code}")

    # 3. Read static values from files
    zone_code    = read_file('/opt/zone.txt')
    source_device = read_file('/opt/device.txt') or "team4-pi"
    source_team   = read_file('/opt/team.txt')   or "Team4"

    # 4. Build and send POST request
    payload = {
        "p_badge_uid": badge_uid,
        "p_pin_code": pin_code,
        "p_zone_code": zone_code,
        "p_source_device": source_device,
        "p_source_team": source_team
    }

    try:
        response = requests.post(API_ENDPOINT, json=payload, timeout=5)
        response.raise_for_status()
        data = response.json()

        access_granted = data.get("access_granted", False)
        denial_reason  = data.get("denial_reason")

        lcd.clear()

        if access_granted:
            lcd.write_string("Access Granted")
            door.on()
            time.sleep(15)          # Hold signal high for 15 seconds
            door.off()
        else:
            reason = denial_reason or "Unknown"
            lcd.write_string("Denied:")
            lcd.cursor_pos = (1, 0)
            # Truncate reason if too long for 16-char display
            lcd.write_string(reason[:16])

    except requests.exceptions.RequestException as e:
        print(f"API request failed: {e}")
        lcd.clear()
        lcd.write_string("Server Error")

    # Brief pause before allowing next scan
    time.sleep(3)
    lcd.clear()