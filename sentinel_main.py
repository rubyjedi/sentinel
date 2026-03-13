import time
from gpiozero import MatrixKeypad
from signal import pause
from RPLCD.i2c import CharLCD
from rpi_rc522 import MFRC522
import requests

# Initialize RFID reader (MFRC522 via SPI)
reader = MFRC522()

# Initialize Keypad (assuming 4x3 matrix, adjust pins as needed)
keypad = MatrixKeypad(
    row_pins=[21, 20, 16],  # Example GPIO pins for rows
    col_pins=[26, 19, 13, 6],  # Example GPIO pins for columns (4th for #/* if needed, but for 4x3 use 3 cols)
    keys=[['1', '2', '3'], ['4', '5', '6'], ['7', '8', '9'], ['*', '0', '#']]
)

# Initialize LCD (assuming I2C address 0x27, 16x2 display; adjust if different)
lcd = CharLCD(i2c_expander='PCF8574', address=0x27, port=1, cols=16, rows=2, dotsize=8)

# Placeholder API endpoint (replace with your actual URL)
API_ENDPOINT = "https://example.com/api/auth"

def read_file(file_path):
    with open(file_path, 'r') as f:
        return f.read().strip()

# Main loop
while True:
    # Wait for RFID
    badge_uid = None
    while not badge_uid:
        (status, TagType) = reader.MFRC522_Request(reader.PICC_REQIDL)
        if status == reader.MI_OK:
            (status, uid) = reader.MFRC522_SelectTagSN()
            if status == reader.MI_OK:
                badge_uid = ''.join(format(x, '02x') for x in uid)  # Convert to hex string
                print(f"RFID UID: {badge_uid}")  # For debugging

    # Display "Enter PIN:" on LCD
    lcd.clear()
    lcd.write_string("Enter PIN:")

    # Collect 4-digit PIN from keypad
    pin_code = ""
    while len(pin_code) < 4:
        key = keypad.wait_for_press(timeout=0.1)  # Non-blocking check
        if key and key.isdigit():  # Only accept digits 0-9
            pin_code += key
            # Optionally display * on LCD for security
            lcd.cursor_pos = (1, len(pin_code) - 1)
            lcd.write_string('*')
        time.sleep(0.1)  # Debounce

    print(f"PIN: {pin_code}")  # For debugging

    # Read values from files
    zone_code = read_file('/opt/zone.txt')
    source_device = read_file('/opt/device.txt')  # Should be "team4-pi"
    source_team = read_file('/opt/team.txt')  # Should be "Team4"

    # Prepare JSON payload
    payload = {
        "p_badge_uid": badge_uid,
        "p_pin_code": pin_code,
        "p_zone_code": zone_code,
        "p_source_device": source_device,
        "p_source_team": source_team
    }

    # Send POST request
    try:
        response = requests.post(API_ENDPOINT, json=payload)
        response.raise_for_status()  # Raise error if not 200-299
        json_response = response.json()
        print("API Response:", json_response)  # Process further as needed
        # TODO: Add further processing here, e.g., display on LCD based on response
    except requests.exceptions.RequestException as e:
        print(f"API Error: {e}")
        # Handle error, e.g., display "Error" on LCD

    # Clear LCD after operation
    lcd.clear()
    time.sleep(2)  # Short delay before next scan