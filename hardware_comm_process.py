import serial
import multiprocessing
import time

# Message format examples:
# From hardware process to GUI:
# {"type":"lap","racer_id":...,"lap_number":...,"lap_time":...}
# {"type":"heartbeat"}
# {"type":"status","message":...}

# From GUI to hardware process:
# {"type":"command","command":"start_race"}
# {"type":"command","command":"stop_race"}

class HardwareCommProcess:
    def __init__(self, serial_port, baudrate, in_queue, out_queue):
        self.serial_port = serial_port
        self.baudrate = baudrate
        self.in_queue: multiprocessing.Queue = in_queue
        self.out_queue: multiprocessing.Queue = out_queue
        self.ser = None
        self.running = False

    def open_connection(self):
        self.ser = serial.Serial(self.serial_port, self.baudrate, timeout=1)

    def close_connection(self):
        if self.ser and self.ser.is_open:
            self.ser.close()

    def read_line(self):
        try:
            if self.ser:
                line = self.ser.readline()
                if line:
                    return line.decode('utf-8').strip()
        except Exception as e:
            self.out_queue.put({"type": "status", "message": f"Error reading serial: {e}"})
        return None

    def send_command_bytes(self):
        # These are the commands from your prototype
        commands = [
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a',
        ]
        try:
            if self.ser:
                for command in commands:
                    self.ser.write(command)
        except Exception as e:
            self.out_queue.put({"type": "status", "message": f"Error sending commands: {e}"})

    def run(self):
        self.running = True
        try:
            self.open_connection()
            last_heartbeat_time = time.time()
            while self.running:
                # Check input queue for commands
                while not self.in_queue.empty():
                    msg = self.in_queue.get()
                    if msg.get("type") == "command":
                        if msg.get("command") == "start_race":
                            self.send_command_bytes()
                            self.out_queue.put({"type": "status", "message": "Start race commands sent"})
                        elif msg.get("command") == "stop_race":
                            # Implement if needed
                            self.out_queue.put({"type": "status", "message": "Stop race command received"})

                # Read lines from serial
                line = self.read_line()

                self.out_queue.put({"type": "debug", "message": "Read line: {}".format(line)})

                if line:
                    # Heartbeat example check
                    # The device prepends each line with \x01 (SOH - Start of Heading) control character,
                    # so we check for "\x01#" instead of just "#" to detect heartbeats accurately.
                    if line.startswith("\x01#") and "xC249" in line:
                        last_heartbeat_time = time.time()
                        self.out_queue.put({"type": "heartbeat"})
                    elif line.startswith("\x01@"):
                        # Parse lap signal line
                        parts = line.split("\t")
                        try:
                            # Defensive: ensure enough parts for indexes we use
                            if len(parts) >= 6:
                                # Example line parts indexes:
                                # parts[1]: racer_id (int)
                                # parts[2]: sensor_id (int)
                                # parts[4]: lap_time (float seconds)
                                racer_id = int(parts[3])
                                sensor_id = int(parts[1])
                                lap_time = float(parts[4])
                                lap_message = {
                                    "type": "lap",
                                    "racer_id": racer_id,
                                    "sensor_id": sensor_id,
                                    "lap_time": lap_time,
                                }
                                self.out_queue.put(lap_message)
                            else:
                                self.out_queue.put({"type": "status", "message": f"Malformed lap line: {line}"})
                        except Exception as e:
                            self.out_queue.put({"type": "status", "message": f"Error parsing lap line: {e} - {line}"})
                    else:
                        # You can parse other outputs here or send as raw
                        self.out_queue.put({"type": "raw", "line": line})

                # Heartbeat timeout
                if time.time() - last_heartbeat_time > 10:
                    self.out_queue.put({"type": "status", "message": "Heartbeat lost"})

                time.sleep(0.05)

        except Exception as e:
            self.out_queue.put({"type": "status", "message": f"Exception in hardware comm process: {e}"})
        finally:
            self.close_connection()

def start_hardware_comm_process(in_queue, out_queue):
    hw = HardwareCommProcess('/dev/ttyUSB0', 9600, in_queue, out_queue)
    hw.run()


# Example minimal usage test if run standalone
if __name__ == '__main__':
    in_q = multiprocessing.Queue()
    out_q = multiprocessing.Queue()
    p = multiprocessing.Process(target=start_hardware_comm_process, args=(in_q, out_q))
    p.start()

    try:
        while True:
            while not out_q.empty():
                msg = out_q.get()
                print(f"Received from hardware process: {msg}")
            time.sleep(0.1)
    except KeyboardInterrupt:
        print("Terminating")
        p.terminate()
        p.join()
