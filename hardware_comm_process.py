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

    def send_reset_command(self):
        # These are the commands from your prototype that perform a reset/start
        commands = [
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a',
            b'\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a',
        ]
        try:
            for command in commands:
                if self.ser is not None:
                    self.ser.write(command)
                else:
                    self.out_queue.put({"type": "status", "message": "Serial port not open"})
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
                            self.send_reset_command()
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
                    elif line.startswith("\x01$"):
                        # Parse new message lines starting with \x01$
                        parts = line.split("\t")
                        try:
                            # Defensive: basic sanity check on parts count
                            if len(parts) >= 5:
                                # Example contents are like:
                                # \x01$\t202\t941,14\t0\t1x
                                # Extract sensor_id, and custom fields, example parse with defensive fallback for comma decimal
                                sensor_id = int(parts[1])
                                raw_time_str = parts[2].replace(",", ".")  # comma decimal to dot decimal
                                raw_time = float(raw_time_str)
                                status_flag1 = parts[3]
                                status_flag2 = parts[4]
                                new_message = {
                                    "type": "new_msg",
                                    "sensor_id": sensor_id,
                                    "raw_time": raw_time,
                                    "flag1": status_flag1,
                                    "flag2": status_flag2,
                                }
                                self.out_queue.put(new_message)
                            else:
                                self.out_queue.put({"type": "status", "message": f"Malformed new_msg line: {line}"})
                        except Exception as e:
                            self.out_queue.put({"type": "status", "message": f"Error parsing new_msg line: {e} - {line}"})
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
    import curses

    def main(stdscr):
        # Initialize multiprocessing queues and start hardware process
        in_q = multiprocessing.Queue()
        out_q = multiprocessing.Queue()
        p = multiprocessing.Process(target=start_hardware_comm_process, args=(in_q, out_q))
        p.start()

        # Configure curses
        curses.curs_set(0)  # Hide cursor
        stdscr.nodelay(True)  # Non-blocking getch
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        # Instruction line at top
        stdscr.addstr(0, 0, "Press Ctrl+R to send reset command, Ctrl+Q to quit.")
        stdscr.hline(1, 0, curses.ACS_HLINE, width)

        # Create a window for output below the line, leaving 2 lines for debug and status at bottom
        output_height = height - 4
        output_win = curses.newwin(output_height, width, 2, 0)
        output_win.scrollok(True)  # Enable scrolling
        output_win.idlok(True)

        # Status window at bottom for debug/key info
        status_win = curses.newwin(2, width, height - 2, 0)
        status_win.nodelay(True)
        status_win.timeout(100)

        stdscr.refresh()
        output_win.refresh()
        status_win.refresh()

        try:
            while True:
                # Handle hardware messages
                while not out_q.empty():
                    msg = out_q.get()
                    output_win.addstr(f"Hardware message: {msg}\n")
                    output_win.refresh()

                # Handle keyboard input
                c = stdscr.getch()
                if c != -1:
                    status_win.clear()
                    status_win.addstr(0, 0, f"Debug: got char code {c}    ")
                    status_win.refresh()

                    if c == 18:  # Ctrl+R
                        status_win.addstr(1, 0, "Sending reset command to hardware...")
                        status_win.refresh()
                        in_q.put({"type": "command", "command": "start_race"})
                    elif c == 17:  # Ctrl+Q
                        raise KeyboardInterrupt

                curses.napms(100)

        except KeyboardInterrupt:
            pass
        finally:
            p.terminate()
            p.join()

    curses.wrapper(main)
