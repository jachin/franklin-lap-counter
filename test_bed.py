import serial
import sys
import tty
import termios
import threading

terminate_event = threading.Event()

# Open the serial port
ser = serial.Serial('/dev/ttyUSB0', baudrate=9600, timeout=1)

def get_char():
    # Obtain a character from stdin
    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    try:
        tty.setraw(fd)
        ch = sys.stdin.read(1)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
    return ch

def send_bytes():
    # Define the byte series you want to send
    byte_series_1 = b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a'
    ser.write(byte_series_1)

    byte_series_2 = b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a'
    ser.write(byte_series_2)

    byte_series_3 = b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a'
    ser.write(byte_series_3)

    byte_series_4 = b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a'
    ser.write(byte_series_4)

def key_listener():
    global terminate_event
    while True:
        ch = get_char()
        if ch == 'q':
            print("Exiting program.")
            terminate_event.set()  # This line sets the event, signaling termination
            return
        elif ch == 'r':
            send_bytes()

try:
    print("Press 'r' key to send bytes.")

    # Start key listener thread
    listener_thread = threading.Thread(target=key_listener, daemon=True)
    listener_thread.start()

    while not terminate_event.is_set():
        if ser.in_waiting > 0:
            line = ser.readline().decode('utf-8').strip()  # Read a line
            print('\rReceived:', line)



finally:
    ser.close()  # Don't forget to close the port
