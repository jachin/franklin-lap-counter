import serial
import time
import logging
import json
import threading
import redis
import platform

# Message format examples:
# From hardware process to GUI:
# {"type":"lap","racer_id":...,"lap_number":...,"lap_time":...}
# {"type":"heartbeat"}
# {"type":"status","message":...}

# From GUI to hardware process:
# {"type":"command","command":"start_race"}
# {"type":"command","command":"stop_race"}

REDIS_SOCKET_PATH = "./redis.sock"
REDIS_IN_CHANNEL = "hardware:in"
REDIS_OUT_CHANNEL = "hardware:out"


class HardwareCommRedis:
    def __init__(
        self,
        serial_port=None,
        baudrate=9600,
        redis_socket=REDIS_SOCKET_PATH,
        simulation_mode=False,
    ):
        # Setup logger for hardware process
        self.logger = logging.getLogger("hardware_comm_redis")
        if not self.logger.hasHandlers():
            handler = logging.FileHandler("hardware_redis.log", mode="a")
            formatter = logging.Formatter("%(asctime)s %(levelname)s:%(message)s")
            handler.setFormatter(formatter)
            self.logger.addHandler(handler)
            self.logger.setLevel(logging.DEBUG)
            self.logger.propagate = False

        self.serial_port = serial_port
        self.baudrate = baudrate
        self.simulation_mode = simulation_mode

        # Redis connection for publishing (main thread)
        self.redis = redis.Redis(unix_socket_path=redis_socket, decode_responses=True)
        # Redis connection for subscription (used in listener thread)
        self.redis_sub = redis.Redis(
            unix_socket_path=redis_socket, decode_responses=True
        )
        self.pubsub = self.redis_sub.pubsub(ignore_subscribe_messages=True)
        self.ser = None
        self.running = False

        self._command_queue = []  # Thread-safe list would be better for heavy use; for this, GIL is fine.

        # Simulation state
        self._sim_race_active = False
        self._sim_last_heartbeat = time.time()

        mode = "SIMULATION" if simulation_mode else "HARDWARE"
        self.logger.info(f"HardwareCommRedis __init__ in {mode} mode")
        for handler in self.logger.handlers:
            handler.flush()

    def open_connection(self):
        if self.simulation_mode:
            self.logger.info("Running in simulation mode - no serial connection")
            self.send_out({"type": "status", "message": "Running in simulation mode"})
            return

        self.logger.debug(
            f"Opening serial connection to {self.serial_port} at {self.baudrate} baud"
        )
        for handler in self.logger.handlers:
            handler.flush()

        try:
            self.ser = serial.Serial(self.serial_port, self.baudrate, timeout=1)
            self.logger.info(
                f"Successfully connected to hardware at {self.serial_port}"
            )

            # Wake up hardware and send reset commands immediately
            self.ser.write(b"\r\n")  # Send enter (CR/LF) to wake up hardware
            self.logger.debug("Sent wake-up CR/LF to hardware")
            time.sleep(0.1)  # Give hardware a moment to wake up

            self.send_out(
                {
                    "type": "status",
                    "message": f"Hardware connected and initialized at {self.serial_port}",
                }
            )
        except serial.SerialException as e:
            error_msg = f"Lap tracking hardware not found at {self.serial_port}"
            self.logger.error(f"Serial connection failed: {e}")
            self.send_out(
                {"type": "hardware_error", "message": error_msg, "detail": str(e)}
            )
            raise  # Re-raise to be caught by run() method

    def close_connection(self):
        if self.ser and self.ser.is_open:
            self.logger.debug("Closing serial connection")
            self.ser.close()

    def read_line(self):
        try:
            if self.ser:
                line = self.ser.readline()
                if line:
                    decoded_line = line.decode("utf-8").strip()
                    self.logger.debug(f"Read line from serial: {decoded_line}")
                    return decoded_line
        except Exception as e:
            error_msg = f"Error reading serial: {e}"
            self.logger.error(error_msg)
            self.send_out({"type": "status", "message": error_msg})
        return None

    def send_reset_command(self):
        self.logger.debug("Sending reset commands to hardware")
        commands = [
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a",
        ]
        try:
            for command in commands:
                if self.ser is not None:
                    self.ser.write(command)
                    self.logger.debug(f"Sent command: {command}")
                else:
                    msg = "Serial port not open"
                    self.logger.error(msg)
                    self.send_out({"type": "status", "message": msg})
        except Exception as e:
            err_msg = f"Error sending commands: {e}"
            self.logger.error(err_msg)
            self.send_out({"type": "status", "message": err_msg})

    def send_out(self, msg):
        try:
            self.redis.publish(REDIS_OUT_CHANNEL, json.dumps(msg))
        except Exception as e:
            self.logger.error(f"Failed to publish to Redis: {e}")

    def _handle_command(self, msg):
        """Handle a single command message."""
        self.logger.debug(f"Received command: {msg}")
        if msg.get("type") == "command":
            if msg.get("command") == "start_race":
                self.logger.info("Received start_race command - sending reset commands")
                if self.simulation_mode:
                    self._sim_race_active = True
                    self.send_out(
                        {"type": "status", "message": "Simulation race started"}
                    )
                    self.logger.info("Simulation race started")
                else:
                    self.send_reset_command()
                    self.send_out(
                        {"type": "status", "message": "Start race commands sent"}
                    )
                    self.logger.info("Start race commands sent")
            elif msg.get("command") == "stop_race":
                if self.simulation_mode:
                    self._sim_race_active = False
                    self.send_out(
                        {"type": "status", "message": "Simulation race stopped"}
                    )
                    self.logger.info("Simulation race stopped")
                else:
                    self.send_out(
                        {"type": "status", "message": "Stop race command received"}
                    )
                    self.logger.info("Stop race command received")
            elif msg.get("command") == "simulate_lap":
                # Simulate a lap event
                if self.simulation_mode:
                    racer_id = msg.get("racer_id", 1)
                    sensor_id = msg.get("sensor_id", 1)
                    race_time = msg.get("race_time", time.time())
                    lap_msg = {
                        "type": "lap",
                        "racer_id": racer_id,
                        "sensor_id": sensor_id,
                        "race_time": race_time,
                    }
                    self.send_out(lap_msg)
                    self.logger.info(f"Simulated lap: {lap_msg}")

    def _redis_command_listener(self):
        # This runs in a background thread, populating self._command_queue
        self.pubsub.subscribe(REDIS_IN_CHANNEL)
        for message in self.pubsub.listen():
            try:
                msg_data = message["data"]
                msg = json.loads(msg_data)
                self._command_queue.append(msg)
            except Exception as e:
                self.logger.error(f"Error parsing command from redis: {e}")

    def _handle_output_line(self, line, last_heartbeat_time):
        """Process a line of output from the hardware and return updated heartbeat time."""
        if not line:
            return last_heartbeat_time

        self.send_out({"type": "debug", "message": f"Read line: {line}"})
        self.logger.debug(f"Read line processed: {line}")

        if line.startswith("\x01#") and "xC249" in line:
            last_heartbeat_time = time.time()
            self.send_out({"type": "heartbeat"})
            self.logger.debug("Heartbeat detected")
        elif line.startswith("\x01@"):
            parts = line.split("\t")
            try:
                if len(parts) >= 6:
                    racer_id = int(parts[3])
                    sensor_id = int(parts[1])
                    race_time = float(parts[4])
                    lap_message = {
                        "type": "lap",
                        "racer_id": racer_id,
                        "sensor_id": sensor_id,
                        "race_time": race_time,
                    }
                    self.send_out(lap_message)
                    self.logger.debug(f"Lap message parsed and sent: {lap_message}")
                else:
                    msg = f"Malformed lap line: {line}"
                    self.send_out({"type": "status", "message": msg})
                    self.logger.error(msg)
            except Exception as e:
                err_msg = f"Error parsing lap line: {e} - {line}"
                self.send_out({"type": "status", "message": err_msg})
                self.logger.error(err_msg)
        elif line.startswith("\x01$"):
            parts = line.split("\t")
            try:
                if len(parts) >= 5:
                    sensor_id = int(parts[1])
                    raw_time_str = parts[2].replace(",", ".")
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
                    self.send_out(new_message)
                    self.logger.debug(f"New message parsed and sent: {new_message}")
                else:
                    msg = f"Malformed new_msg line: {line}"
                    self.send_out({"type": "status", "message": msg})
                    self.logger.error(msg)
            except Exception as e:
                err_msg = f"Error parsing new_msg line: {e} - {line}"
                self.send_out({"type": "status", "message": err_msg})
                self.logger.error(err_msg)
        else:
            self.send_out({"type": "raw", "line": line})
            self.logger.debug(f"Raw line sent: {line}")

        return last_heartbeat_time

    def _simulation_loop(self):
        """Simulation mode: send periodic heartbeats and respond to commands."""
        last_heartbeat_time = time.time()

        while self.running:
            # Handle any queued command messages from Redis
            while self._command_queue:
                cmd_msg = self._command_queue.pop(0)
                self._handle_command(cmd_msg)

            # Send periodic heartbeats in simulation mode
            if time.time() - last_heartbeat_time > 2:
                self.send_out({"type": "heartbeat"})
                last_heartbeat_time = time.time()
                self.logger.debug("Simulation heartbeat sent")

            time.sleep(0.1)

    def run(self):
        self.running = True
        self.logger.info("HardwareCommRedis started running")
        for handler in self.logger.handlers:
            handler.flush()

        # Test Redis connection
        try:
            self.redis.ping()
            self.logger.info("Redis connection successful")
            self.send_out({"type": "status", "message": "Redis connected"})
        except Exception as e:
            err_msg = f"Failed to connect to Redis: {e}"
            self.logger.error(err_msg)
            self.send_out({"type": "status", "message": err_msg})
            return

        # Start Redis subscription listener in background
        listener_thread = threading.Thread(
            target=self._redis_command_listener, daemon=True
        )
        listener_thread.start()

        try:
            if self.simulation_mode:
                self.logger.info("Starting simulation mode")
                self.open_connection()
                self._simulation_loop()
            else:
                self.logger.info("HardwareCommRedis starting run loop")
                try:
                    self.open_connection()
                except serial.SerialException:
                    # Hardware not found - send error and stop
                    self.logger.error("Cannot start: hardware not connected")
                    # Keep running to allow user to see the error and quit gracefully
                    while self.running:
                        time.sleep(0.1)
                    return

                # Hardware is now initialized, start monitoring
                last_heartbeat_time = time.time()
                while self.running:
                    # Handle any queued command messages from Redis
                    while self._command_queue:
                        cmd_msg = self._command_queue.pop(0)
                        self._handle_command(cmd_msg)

                    # Read and process lines from serial
                    line = self.read_line()
                    last_heartbeat_time = self._handle_output_line(
                        line, last_heartbeat_time
                    )

                    # Heartbeat timeout
                    if time.time() - last_heartbeat_time > 10:
                        msg = "Heartbeat lost"
                        self.send_out({"type": "status", "message": msg})
                        self.logger.warning(msg)

                    time.sleep(0.05)

        except Exception as e:
            err_msg = f"Unexpected error in hardware comm: {e}"
            self.send_out({"type": "error", "message": err_msg})
            self.logger.error(err_msg)
        finally:
            self.close_connection()


def get_default_serial_port():
    system = platform.system()
    if system == "Linux":
        return "/dev/ttyUSB0"
    elif system == "Darwin":  # Mac OS
        return "/dev/tty.usbserial-AB0KLIK2"
    else:
        raise RuntimeError(f"Unsupported OS: {system}")


def start_hardware_comm_redis():
    hw = HardwareCommRedis(get_default_serial_port(), 9600)
    hw.run()


# Example minimal usage test if run standalone: listens for Redis "hardware:in" commands and sends status to "hardware:out"
if __name__ == "__main__":
    import curses
    import sys

    # Check for simulation mode flag
    simulation_mode = "--sim" in sys.argv or "-s" in sys.argv

    def main(stdscr):
        # Connect redis for sending commands to hardware process
        redis_send = redis.Redis(
            unix_socket_path=REDIS_SOCKET_PATH, decode_responses=True
        )
        redis_recv = redis.Redis(
            unix_socket_path=REDIS_SOCKET_PATH, decode_responses=True
        )
        pubsub = redis_recv.pubsub()
        pubsub.subscribe(REDIS_OUT_CHANNEL)

        # Start hardware process in a thread
        def hw_thread_fn():
            if simulation_mode:
                hw = HardwareCommRedis(simulation_mode=True)
            else:
                hw = HardwareCommRedis(get_default_serial_port(), 9600)
            hw.run()

        hw_thread = threading.Thread(target=hw_thread_fn, daemon=True)
        hw_thread.start()

        # Configure curses
        curses.curs_set(0)  # Hide cursor
        stdscr.nodelay(True)  # Non-blocking getch
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        # Instruction lines at top
        mode_text = "SIMULATION MODE" if simulation_mode else "HARDWARE MODE"
        stdscr.addstr(0, 0, f"=== Hardware Comm Redis - {mode_text} ===")

        if simulation_mode:
            stdscr.addstr(
                1,
                0,
                "Keys: [S]tart race | Sto[P] race | [1-4] Simulate lap for racer | [Q]uit",
            )
        else:
            stdscr.addstr(1, 0, "Keys: [Q]uit")

        stdscr.hline(2, 0, curses.ACS_HLINE, width)

        # Create a window for output below the line, leaving 3 lines for status at bottom
        output_height = height - 6
        output_win = curses.newwin(output_height, width, 3, 0)
        output_win.scrollok(True)
        output_win.idlok(True)

        status_win = curses.newwin(3, width, height - 3, 0)
        status_win.nodelay(True)
        status_win.timeout(100)

        stdscr.refresh()
        output_win.refresh()
        status_win.refresh()

        # Track race start time for simulation
        race_start_time = None
        message_count = 0
        hardware_error_detected = False

        try:
            while True:
                # Handle hardware messages from Redis pubsub
                # Only process one message per iteration to allow keyboard checking
                message = pubsub.get_message(
                    timeout=0.01
                )  # Shorter timeout for responsiveness
                if message and message["type"] == "message":
                    try:
                        msg_obj = json.loads(message["data"])
                        message_count += 1

                        # Check for hardware error
                        if (
                            msg_obj.get("type") == "hardware_error"
                            and not hardware_error_detected
                        ):
                            hardware_error_detected = True
                            # Clear screen and show error (only once)
                            stdscr.clear()
                            output_win.clear()

                            # Display error message centered
                            error_msg = msg_obj.get("message", "Hardware not found")
                            stdscr.addstr(
                                0, 0, "=== Hardware Comm Redis - HARDWARE MODE ==="
                            )
                            stdscr.hline(1, 0, curses.ACS_HLINE, width)

                            # Center the error message
                            error_line = height // 2 - 2
                            stdscr.addstr(error_line, 0, " " * width)
                            stdscr.addstr(
                                error_line, (width - len(error_msg)) // 2, error_msg
                            )

                            help_msg = "Please connect the lap tracking hardware and restart this program."
                            stdscr.addstr(
                                error_line + 2,
                                (width - len(help_msg)) // 2,
                                help_msg,
                            )

                            quit_msg = "Press [Q] to quit"
                            stdscr.addstr(
                                error_line + 4,
                                (width - len(quit_msg)) // 2,
                                quit_msg,
                            )

                            stdscr.refresh()

                        # Skip all message processing if hardware error occurred
                        elif not hardware_error_detected:
                            # Format message based on type
                            if msg_obj.get("type") == "lap":
                                msg_str = f"[LAP] Racer {msg_obj.get('racer_id')} - Sensor {msg_obj.get('sensor_id')} - Time: {msg_obj.get('race_time'):.3f}s"
                            elif msg_obj.get("type") == "heartbeat":
                                msg_str = "[HEARTBEAT] ♥"
                            elif msg_obj.get("type") == "status":
                                msg_str = f"[STATUS] {msg_obj.get('message')}"
                            elif msg_obj.get("type") == "error":
                                msg_str = f"[ERROR] {msg_obj.get('message')}"
                            else:
                                msg_str = f"[{msg_obj.get('type', 'UNKNOWN').upper()}] {msg_obj}"

                            output_win.addstr(f"{message_count:4d} | {msg_str}\n")
                            output_win.refresh()
                    except Exception as e:
                        output_win.addstr(f"Malformed HW msg: {e}\n")
                        output_win.refresh()

                # Handle keyboard input
                c = stdscr.getch()
                if c != -1:
                    # Convert to lowercase for easier comparison
                    key_char = chr(c).lower() if 32 <= c <= 126 else None

                    # Always allow quit - handle both 'q' and uppercase 'Q'
                    if key_char == "q" or c == ord("Q") or c == ord("q"):
                        raise KeyboardInterrupt

                    # If hardware error, ignore all other commands
                    if hardware_error_detected:
                        continue

                    status_win.clear()
                    status_win.addstr(
                        0, 0, f"Last key: {chr(c) if 32 <= c <= 126 else c}    "
                    )

                    if (
                        simulation_mode and key_char == "s"
                    ):  # Start race (simulation only)
                        status_win.addstr(1, 0, "Sending START RACE command...")
                        cmd = {"type": "command", "command": "start_race"}
                        redis_send.publish(REDIS_IN_CHANNEL, json.dumps(cmd))
                        race_start_time = time.time()

                    elif (
                        simulation_mode and key_char == "p"
                    ):  # Stop race (simulation only)
                        status_win.addstr(1, 0, "Sending STOP RACE command...")
                        cmd = {"type": "command", "command": "stop_race"}
                        redis_send.publish(REDIS_IN_CHANNEL, json.dumps(cmd))
                        race_start_time = None

                    elif simulation_mode and key_char in ["1", "2", "3", "4"]:
                        # Simulate a lap for racer 1-4
                        racer_id = int(key_char)
                        current_time = time.time()
                        race_time = (
                            current_time - race_start_time
                            if race_start_time
                            else current_time
                        )

                        status_win.addstr(
                            1, 0, f"Simulating lap for racer {racer_id}..."
                        )
                        cmd = {
                            "type": "command",
                            "command": "simulate_lap",
                            "racer_id": racer_id,
                            "sensor_id": 1,
                            "race_time": race_time,
                        }
                        redis_send.publish(REDIS_IN_CHANNEL, json.dumps(cmd))

                    status_win.addstr(2, 0, f"Messages received: {message_count}")
                    status_win.refresh()

                curses.napms(100)

        except KeyboardInterrupt:
            pass

    curses.wrapper(main)
