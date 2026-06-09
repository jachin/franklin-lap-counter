use anyhow::{Context, Result};
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode},
    execute,
    terminal::{EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode},
};
use ratatui::{
    Terminal,
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, List, ListItem, Paragraph},
};
use redis::Commands;
use serde::{Deserialize, Serialize};
use serialport::SerialPort;
use std::collections::HashMap;
use std::io::{self, BufRead, BufReader};
use std::sync::Arc;
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};
use tokio::sync::Mutex;
use tracing::{error, info, warn};

// Redis configuration
// Authoritative channel/message reference: docs/redis-message-reference.md
const DEFAULT_REDIS_SOCKET_PATH: &str = "./redis.sock";
const REDIS_IN_CHANNEL: &str = "hardware:in";
const REDIS_OUT_CHANNEL: &str = "hardware:out";
const REDIS_EVENTS_CHANNEL: &str = "franklin:events";

// Message types
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum OutMessage {
    Lap {
        racer_id: u32,
        sensor_id: u32,
        race_time: f64,
        lap_number: u32,
        race_start_at: f64,
        lap_at: f64,
        #[serde(default)]
        simulated: bool,
    },
    Heartbeat {
        #[serde(default)]
        simulated: bool,
    },
    Status {
        message: String,
        #[serde(default)]
        simulated: bool,
    },
    Error {
        message: String,
        #[serde(default)]
        simulated: bool,
    },
    Debug {
        message: String,
        #[serde(default)]
        simulated: bool,
    },
    RaceControl {
        command: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_id: Option<String>,
        accepted: bool,
        #[serde(skip_serializing_if = "Option::is_none")]
        message: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        racer_id: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        penalty_seconds: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        lap_number: Option<u32>,
    },
    CountdownPhase {
        phase: String,
        at: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        source: Option<String>,
    },
    StartRace {
        at: f64,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        source: Option<String>,
        #[serde(default)]
        simulated: bool,
    },
    Raw {
        line: String,
    },
}

#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum InMessage {
    Command {
        command: String,
        #[serde(skip_serializing_if = "Option::is_none")]
        command_id: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        source: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        timestamp: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        racer_id: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        sensor_id: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        race_time: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        start_at: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        ready_at: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        set_at: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        go_at: Option<f64>,
        #[serde(skip_serializing_if = "Option::is_none")]
        penalty_seconds: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        reason: Option<String>,
        #[serde(skip_serializing_if = "Option::is_none")]
        lap_number: Option<u32>,
    },
}

// Application state
struct App {
    messages: Vec<String>,
    message_count: usize,
    simulation_mode: bool,
    verbose: bool,
    race_active: bool,
    race_start_time: Option<Instant>,
    race_start_epoch: Option<f64>,
    lap_counts: HashMap<u32, u32>,
    last_heartbeat: Option<Instant>,
    reset_requested: bool,
    should_quit: bool,
}

impl App {
    fn new(simulation_mode: bool, verbose: bool) -> Self {
        Self {
            messages: Vec::new(),
            message_count: 0,
            simulation_mode,
            verbose,
            race_active: false,
            race_start_time: None,
            race_start_epoch: None,
            lap_counts: HashMap::new(),
            last_heartbeat: None,
            reset_requested: false,
            should_quit: false,
        }
    }

    fn add_message(&mut self, msg: String) {
        self.message_count += 1;
        let formatted = format!("{:4} | {}", self.message_count, msg);
        self.messages.push(formatted);

        // Keep only the last 1000 messages to prevent memory issues
        if self.messages.len() > 1000 {
            self.messages.remove(0);
        }
    }

    fn format_out_message(&self, msg: &OutMessage) -> String {
        match msg {
            OutMessage::Lap {
                racer_id,
                sensor_id,
                race_time,
                lap_number,
                race_start_at,
                lap_at,
                simulated,
            } => format!(
                "[LAP{}] Racer {} - Sensor {} - lap={} rel={:.3}s start_at={:.3} lap_at={:.3}",
                if *simulated { " (SIM)" } else { "" },
                racer_id,
                sensor_id,
                lap_number,
                race_time,
                race_start_at,
                lap_at
            ),
            OutMessage::Heartbeat { simulated } => {
                if *simulated {
                    "[HEARTBEAT (SIM)] ♥".to_string()
                } else {
                    "[HEARTBEAT] ♥".to_string()
                }
            }
            OutMessage::Status { message, simulated } => format!(
                "[STATUS{}] {}",
                if *simulated { " (SIM)" } else { "" },
                message
            ),
            OutMessage::Error { message, simulated } => format!(
                "[ERROR{}] {}",
                if *simulated { " (SIM)" } else { "" },
                message
            ),
            OutMessage::Debug { message, simulated } => format!(
                "[DEBUG{}] {}",
                if *simulated { " (SIM)" } else { "" },
                message
            ),
            OutMessage::RaceControl {
                command,
                command_id,
                accepted,
                message,
                racer_id,
                penalty_seconds,
                reason,
                lap_number,
            } => {
                let status = if *accepted { "accepted" } else { "rejected" };
                let mut details: Vec<String> = Vec::new();
                if let Some(cid) = command_id {
                    details.push(format!("command_id={}", cid));
                }
                if let Some(detail) = message {
                    details.push(detail.clone());
                }
                if let Some(rid) = racer_id {
                    details.push(format!("racer_id={}", rid));
                }
                if let Some(penalty) = penalty_seconds {
                    details.push(format!("penalty_seconds={}", penalty));
                }
                if let Some(why) = reason {
                    details.push(format!("reason={}", why));
                }
                if let Some(lap_no) = lap_number {
                    details.push(format!("lap_number={}", lap_no));
                }

                if details.is_empty() {
                    format!("[RACE_CONTROL] {} {}", command, status)
                } else {
                    format!(
                        "[RACE_CONTROL] {} {} ({})",
                        command,
                        status,
                        details.join(", ")
                    )
                }
            }
            OutMessage::CountdownPhase {
                phase,
                at,
                command_id,
                source,
            } => {
                let mut details: Vec<String> = vec![format!("at={:.3}", at)];
                if let Some(cid) = command_id {
                    details.push(format!("command_id={}", cid));
                }
                if let Some(src) = source {
                    details.push(format!("source={}", src));
                }
                format!("[COUNTDOWN] {} ({})", phase, details.join(", "))
            }
            OutMessage::StartRace {
                at,
                command_id,
                source,
                simulated,
            } => {
                let mut details: Vec<String> = vec![format!("at={:.3}", at)];
                if let Some(cid) = command_id {
                    details.push(format!("command_id={}", cid));
                }
                if let Some(src) = source {
                    details.push(format!("source={}", src));
                }
                details.push(format!("simulated={}", simulated));
                format!("[START_RACE] {}", details.join(", "))
            }
            OutMessage::Raw { line } => format!("[RAW] {}", line),
        }
    }

    fn get_connection_status(&self) -> (String, Color) {
        if self.simulation_mode {
            ("Hardware: Simulated".to_string(), Color::Cyan)
        } else if let Some(last_hb) = self.last_heartbeat {
            let elapsed = last_hb.elapsed();
            if elapsed < Duration::from_secs(5) {
                ("Hardware: Connected".to_string(), Color::Green)
            } else {
                ("Hardware: Disconnected".to_string(), Color::Red)
            }
        } else {
            ("Hardware: Waiting...".to_string(), Color::Yellow)
        }
    }
}

// Hardware communication handler
struct HardwareComm {
    redis_client: redis::Client,
    serial_port_path: Option<String>,
    baudrate: u32,
}

impl HardwareComm {
    fn new(
        redis_socket_path: &str,
        serial_port_path: Option<String>,
        baudrate: u32,
    ) -> Result<Self> {
        // Convert relative path to absolute for Redis client
        let socket_path = if redis_socket_path.starts_with('/') {
            // Already absolute
            redis_socket_path.to_string()
        } else {
            // Make it absolute
            std::env::current_dir()?
                .join(redis_socket_path)
                .to_string_lossy()
                .to_string()
        };

        let redis_client = redis::Client::open(format!("unix://{}", socket_path))
            .context("Failed to create Redis client")?;

        Ok(Self {
            redis_client,
            serial_port_path,
            baudrate,
        })
    }

    fn open_serial_connection(&self) -> Result<Box<dyn SerialPort>> {
        let port_path = self
            .serial_port_path
            .as_ref()
            .context("No serial port specified")?;

        info!(
            "Opening serial connection to {} at {} baud",
            port_path, self.baudrate
        );

        let port = serialport::new(port_path, self.baudrate)
            .timeout(Duration::from_secs(1))
            .open()
            .context(format!("Failed to open serial port {}", port_path))?;

        info!("Successfully connected to hardware at {}", port_path);

        Ok(port)
    }

    fn send_reset_commands(&self, port: &mut Box<dyn SerialPort>) -> Result<()> {
        info!("Sending reset commands to hardware");

        // Wake up hardware
        let wake_cmd = b"\r\n";
        port.write_all(wake_cmd)?;
        info!("SERIAL TX: {:?} (wake-up CR/LF)", wake_cmd);
        std::thread::sleep(Duration::from_millis(100));

        // Reset commands from Python version
        let commands: Vec<&[u8]> = vec![
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a",
        ];

        for (idx, cmd) in commands.iter().enumerate() {
            port.write_all(cmd)?;
            info!("SERIAL TX: Reset command {}: {:?}", idx + 1, cmd);
        }

        Ok(())
    }

    fn parse_hardware_line(&self, line: &str) -> Option<OutMessage> {
        if line.starts_with("\x01#") && line.contains("xC249") {
            // Heartbeat
            Some(OutMessage::Heartbeat { simulated: false })
        } else if line.starts_with("\x01@") || line.starts_with("@") {
            // Lap message: \x01@\t<sensor_id>\t...\t<racer_id>\t<race_time>\t...
            // Try tab-separated first, then fall back to whitespace-separated
            let parts: Vec<&str> = if line.contains('\t') {
                line.split('\t').collect()
            } else {
                line.split_whitespace().collect()
            };

            if parts.len() >= 6 {
                match (parts.get(3), parts.get(1), parts.get(4)) {
                    (Some(racer_id_str), Some(sensor_id_str), Some(race_time_str)) => {
                        // Trim whitespace from each field before parsing
                        if let (Ok(racer_id), Ok(sensor_id), Ok(race_time)) = (
                            racer_id_str.trim().parse::<u32>(),
                            sensor_id_str.trim().parse::<u32>(),
                            race_time_str.trim().parse::<f64>(),
                        ) {
                            info!(
                                "Parsed lap: racer_id={}, sensor_id={}, race_time={}",
                                racer_id, sensor_id, race_time
                            );
                            return Some(OutMessage::Lap {
                                racer_id,
                                sensor_id,
                                race_time,
                                lap_number: 0,
                                race_start_at: 0.0,
                                lap_at: 0.0,
                                simulated: false,
                            });
                        } else {
                            warn!(
                                "Failed to parse lap values - racer:{:?} sensor:{:?} time:{:?}",
                                racer_id_str, sensor_id_str, race_time_str
                            );
                        }
                    }
                    _ => {
                        warn!("Could not extract lap fields from parts: {:?}", parts);
                    }
                }
            } else {
                warn!(
                    "Lap line has {} parts, need at least 6: {:?}",
                    parts.len(),
                    parts
                );
            }
            Some(OutMessage::Status {
                message: format!("Malformed lap line: {}", line),
                simulated: false,
            })
        } else if line.starts_with("\x01$") {
            // New message: \x01$\t<sensor_id>\t<raw_time>\t<flag1>\t<flag2>
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.len() >= 5 {
                // Extract sensor_id and time information
                if let Some(sensor_id_str) = parts.get(1) {
                    if let Ok(sensor_id) = sensor_id_str.trim().parse::<u32>() {
                        return Some(OutMessage::Status {
                            message: format!("Sensor {} signal received", sensor_id),
                            simulated: false,
                        });
                    }
                }
                // Fall back to status message instead of raw
                Some(OutMessage::Status {
                    message: "New message received from hardware".to_string(),
                    simulated: false,
                })
            } else {
                Some(OutMessage::Status {
                    message: format!("Malformed new_msg line: {}", line),
                    simulated: false,
                })
            }
        } else if line.contains("HELLO") || line.contains("RESET") {
            // Recognize common messages and turn them into status
            Some(OutMessage::Status {
                message: format!("Hardware message: {}", line.trim()),
                simulated: false,
            })
        } else if !line.is_empty() {
            // Only use Raw for debugging, not for normal operation
            Some(OutMessage::Debug {
                message: format!("Hardware data: {}", line.trim()),
                simulated: false,
            })
        } else {
            None
        }
    }

    fn send_message(&self, msg: &OutMessage) -> Result<()> {
        // Route messages to the appropriate Redis channel.
        let target_channel = match msg {
            OutMessage::Raw { .. } => None, // Don't publish raw messages to Redis
            OutMessage::RaceControl { .. } | OutMessage::CountdownPhase { .. } => {
                Some(REDIS_EVENTS_CHANNEL)
            }
            _ => Some(REDIS_OUT_CHANNEL),
        };

        if let Some(channel) = target_channel {
            let mut conn = self
                .redis_client
                .get_connection()
                .context("Failed to get Redis connection")?;

            let json = serde_json::to_string(msg).context("Failed to serialize message")?;

            conn.publish::<_, _, ()>(channel, json)
                .context("Failed to publish to Redis")?;
        }

        Ok(())
    }
}

// Background task to listen for messages from Redis
async fn redis_listener_task(hw: Arc<HardwareComm>, app: Arc<Mutex<App>>) -> Result<()> {
    // Run blocking Redis operations in a separate thread
    tokio::task::spawn_blocking(move || {
        let mut conn = match hw.redis_client.get_connection() {
            Ok(c) => c,
            Err(e) => {
                error!("Failed to get Redis connection for pubsub: {}", e);
                return;
            }
        };

        let mut pubsub = conn.as_pubsub();

        // Set read timeout so we don't block forever
        pubsub
            .set_read_timeout(Some(std::time::Duration::from_millis(100)))
            .ok();

        if let Err(e) = pubsub.subscribe(REDIS_OUT_CHANNEL) {
            error!("Failed to subscribe to Redis channel (hardware:out): {}", e);
            return;
        }

        if let Err(e) = pubsub.subscribe(REDIS_EVENTS_CHANNEL) {
            error!(
                "Failed to subscribe to Redis channel (franklin:events): {}",
                e
            );
            return;
        }

        loop {
            // Check if we should quit
            let rt = tokio::runtime::Handle::current();
            let should_quit = rt.block_on(async {
                let app = app.lock().await;
                app.should_quit
            });

            if should_quit {
                info!("Redis listener task exiting");
                break;
            }

            // Get message (will timeout after 100ms)
            let msg = match pubsub.get_message() {
                Ok(m) => m,
                Err(e) => {
                    // Timeout is expected, just continue
                    // Redis returns IoError for timeouts
                    if e.is_io_error() {
                        continue;
                    }
                    error!("Failed to get message from Redis: {}", e);
                    continue;
                }
            };

            let payload: String = match msg.get_payload() {
                Ok(p) => p,
                Err(e) => {
                    error!("Failed to get payload from message: {}", e);
                    continue;
                }
            };

            if let Ok(out_msg) = serde_json::from_str::<OutMessage>(&payload) {
                // Use a blocking runtime to lock the mutex
                let rt = tokio::runtime::Handle::current();
                rt.block_on(async {
                    let mut app = app.lock().await;

                    // Update last heartbeat time
                    if matches!(&out_msg, OutMessage::Heartbeat { .. }) {
                        app.last_heartbeat = Some(Instant::now());
                    }

                    // Skip RAW and HEARTBEAT messages unless verbose mode is enabled
                    let should_display = match &out_msg {
                        OutMessage::Raw { .. } | OutMessage::Heartbeat { .. } => app.verbose,
                        _ => true,
                    };
                    if should_display {
                        let formatted = app.format_out_message(&out_msg);
                        app.add_message(formatted);
                    }
                });
            }
        }
    })
    .await?;

    Ok(())
}

// Background task for simulation mode
async fn simulation_task(hw: Arc<HardwareComm>, app: Arc<Mutex<App>>) -> Result<()> {
    info!("Starting simulation task");

    let mut last_heartbeat = Instant::now();

    loop {
        tokio::time::sleep(Duration::from_millis(100)).await;

        // Send heartbeat every 2 seconds
        if last_heartbeat.elapsed() >= Duration::from_secs(2) {
            hw.send_message(&OutMessage::Heartbeat { simulated: true })?;
            last_heartbeat = Instant::now();
        }

        // Check if we should quit
        let should_quit = {
            let app = app.lock().await;
            app.should_quit
        };

        if should_quit {
            break;
        }
    }

    Ok(())
}

// Background task for hardware mode
async fn hardware_task(hw: Arc<HardwareComm>, app: Arc<Mutex<App>>) -> Result<()> {
    info!("Starting hardware task");

    // Run blocking serial operations in a separate thread
    tokio::task::spawn_blocking(move || {
        // Open serial port
        info!(
            "SERIAL: Opening connection to {:?} at {} baud",
            hw.serial_port_path, hw.baudrate
        );
        let mut port = match hw.open_serial_connection() {
            Ok(p) => {
                info!("SERIAL: Successfully opened port");
                p
            }
            Err(e) => {
                error!("SERIAL: Failed to open serial connection: {}", e);
                let _ = hw.send_message(&OutMessage::Status {
                    message: format!("Lap tracking hardware not found: {}", e),
                    simulated: false,
                });
                return;
            }
        };

        // Send initial status
        let status_msg = format!(
            "Hardware connected and initialized at {}",
            hw.serial_port_path
                .as_ref()
                .unwrap_or(&"unknown".to_string())
        );
        info!("SERIAL: Connection established, sending initial status");
        if let Err(e) = hw.send_message(&OutMessage::Status {
            message: status_msg,
            simulated: false,
        }) {
            error!("Failed to send status: {}", e);
        }

        // Send reset commands
        info!("SERIAL: Sending initial reset commands");
        if let Err(e) = hw.send_reset_commands(&mut port) {
            error!("SERIAL: Failed to send reset commands: {}", e);
            let _ = hw.send_message(&OutMessage::Status {
                message: format!("Error sending reset commands: {}", e),
                simulated: false,
            });
        } else {
            info!("SERIAL: Initial reset commands sent successfully");
        }

        // Create buffered reader for line reading
        let mut reader = BufReader::new(port);
        let mut last_heartbeat = Instant::now();

        loop {
            // Check if we should quit or reset
            let rt = tokio::runtime::Handle::current();
            let (should_quit, should_reset) = rt.block_on(async {
                let mut app = app.lock().await;
                let quit = app.should_quit;
                let reset = app.reset_requested;
                if reset {
                    app.reset_requested = false; // Clear the flag
                }
                (quit, reset)
            });

            if should_quit {
                info!("Hardware task exiting");
                break;
            }

            // Send reset commands if requested
            if should_reset {
                info!("SERIAL: Race reset requested, sending reset commands");
                if let Err(e) = hw.send_reset_commands(&mut reader.get_mut()) {
                    error!("SERIAL: Failed to send race reset commands: {}", e);
                    let _ = hw.send_message(&OutMessage::Status {
                        message: format!("Error sending reset commands: {}", e),
                        simulated: false,
                    });
                } else {
                    info!("SERIAL: Race reset commands sent successfully");
                    let _ = hw.send_message(&OutMessage::Status {
                        message: "Race reset commands sent".to_string(),
                        simulated: false,
                    });
                }
            }

            // Read line from serial (with timeout)
            let mut line_buf = String::new();
            match reader.read_line(&mut line_buf) {
                Ok(0) => {
                    // No data, continue
                    std::thread::sleep(Duration::from_millis(50));
                    continue;
                }
                Ok(n) => {
                    let line = line_buf.trim();

                    // Log raw received data
                    info!("SERIAL RX: {} bytes: {:?}", n, line_buf.as_bytes());
                    info!("SERIAL RX: Trimmed line: {:?}", line);

                    // Parse and send message
                    if let Some(mut msg) = hw.parse_hardware_line(line) {
                        // Update heartbeat time if we got a heartbeat
                        if matches!(msg, OutMessage::Heartbeat { .. }) {
                            last_heartbeat = Instant::now();
                        }

                        if let OutMessage::Lap {
                            racer_id,
                            race_time,
                            lap_number,
                            race_start_at,
                            lap_at,
                            ..
                        } = &mut msg
                        {
                            let rt = tokio::runtime::Handle::current();
                            let (computed_lap_number, computed_start_at, computed_lap_at) = rt
                                .block_on(async {
                                    let mut app = app.lock().await;
                                    next_lap_metadata(&mut app, *racer_id, *race_time)
                                });
                            *lap_number = computed_lap_number;
                            *race_start_at = computed_start_at;
                            *lap_at = computed_lap_at;
                        }

                        if let Err(e) = hw.send_message(&msg) {
                            error!("Failed to send message: {}", e);
                        }
                    }
                }
                Err(e) => {
                    // Check for timeout (expected)
                    if e.kind() == std::io::ErrorKind::TimedOut {
                        continue;
                    }
                    error!("Error reading from serial: {}", e);
                    let _ = hw.send_message(&OutMessage::Status {
                        message: format!("Error reading serial: {}", e),
                        simulated: false,
                    });
                }
            }

            // Check for heartbeat timeout (10 seconds)
            if last_heartbeat.elapsed() > Duration::from_secs(10) {
                warn!("Heartbeat lost");
                let _ = hw.send_message(&OutMessage::Status {
                    message: "Heartbeat lost".to_string(),
                    simulated: false,
                });
                last_heartbeat = Instant::now(); // Reset to avoid spam
            }

            std::thread::sleep(Duration::from_millis(50));
        }
    })
    .await?;

    Ok(())
}

// Handle user input
fn handle_input(app: &mut App, hw: &HardwareComm, key: KeyCode) -> Result<()> {
    match key {
        KeyCode::Char('q') | KeyCode::Char('Q') => {
            app.should_quit = true;
        }
        KeyCode::Char('s') | KeyCode::Char('S') if app.simulation_mode => {
            app.race_active = true;
            app.race_start_time = Some(Instant::now());
            app.race_start_epoch = Some(now_epoch_seconds());
            app.lap_counts.clear();
            info!("Simulation race started");
        }
        KeyCode::Char('p') | KeyCode::Char('P') if app.simulation_mode => {
            app.race_active = false;
            app.race_start_time = None;
            app.race_start_epoch = None;
            app.lap_counts.clear();
            info!("Simulation race stopped");
        }
        KeyCode::Char(c @ '1'..='4') if app.simulation_mode => {
            let racer_id = c.to_digit(10).unwrap();
            let race_time = if let Some(start_time) = app.race_start_time {
                start_time.elapsed().as_secs_f64()
            } else {
                0.0
            };
            let (lap_number, race_start_at, lap_at) = next_lap_metadata(app, racer_id, race_time);

            // In simulation mode, send lap message directly without going through Redis command channel
            hw.send_message(&OutMessage::Lap {
                racer_id,
                sensor_id: 1,
                race_time,
                lap_number,
                race_start_at,
                lap_at,
                simulated: true,
            })?;
            info!("Simulated lap for racer {} (lap #{})", racer_id, lap_number);
        }
        _ => {}
    }

    Ok(())
}

fn now_epoch_seconds() -> f64 {
    match SystemTime::now().duration_since(UNIX_EPOCH) {
        Ok(d) => d.as_secs_f64(),
        Err(_) => 0.0,
    }
}

fn next_lap_metadata(app: &mut App, racer_id: u32, race_time: f64) -> (u32, f64, f64) {
    let now = now_epoch_seconds();
    let start_epoch = app.race_start_epoch.unwrap_or(now - race_time);
    if app.race_start_epoch.is_none() {
        app.race_start_epoch = Some(start_epoch);
    }

    let lap_counter = app.lap_counts.entry(racer_id).or_insert(0);
    *lap_counter += 1;

    let lap_number = *lap_counter;
    let lap_at = start_epoch + race_time;

    (lap_number, start_epoch, lap_at)
}

// Background task to handle Redis commands (simulation mode)
async fn command_handler_task(hw: Arc<HardwareComm>, app: Arc<Mutex<App>>) -> Result<()> {
    // Run blocking Redis operations in a separate thread
    tokio::task::spawn_blocking(move || {
        let mut conn = match hw.redis_client.get_connection() {
            Ok(c) => c,
            Err(e) => {
                error!("Failed to get Redis connection for command handler: {}", e);
                return;
            }
        };

        let mut pubsub = conn.as_pubsub();

        // Set read timeout so we don't block forever
        pubsub
            .set_read_timeout(Some(std::time::Duration::from_millis(100)))
            .ok();

        if let Err(e) = pubsub.subscribe(REDIS_IN_CHANNEL) {
            error!(
                "Failed to subscribe to command channel (hardware:in): {}",
                e
            );
            return;
        }

        loop {
            // Check if we should quit
            let rt = tokio::runtime::Handle::current();
            let should_quit = rt.block_on(async {
                let app = app.lock().await;
                app.should_quit
            });

            if should_quit {
                info!("Command handler task exiting");
                break;
            }

            // Get message (will timeout after 100ms)
            let msg = match pubsub.get_message() {
                Ok(m) => m,
                Err(e) => {
                    // Timeout is expected, just continue
                    // Redis returns IoError for timeouts
                    if e.is_io_error() {
                        continue;
                    }
                    error!("Failed to get command message: {}", e);
                    continue;
                }
            };

            let payload: String = match msg.get_payload() {
                Ok(p) => p,
                Err(e) => {
                    error!("Failed to get command payload: {}", e);
                    continue;
                }
            };

            if let Ok(in_msg) = serde_json::from_str::<InMessage>(&payload) {
                match in_msg {
                    InMessage::Command {
                        command,
                        command_id,
                        source,
                        timestamp: _timestamp,
                        racer_id,
                        sensor_id,
                        race_time,
                        start_at,
                        ready_at,
                        set_at,
                        go_at,
                        penalty_seconds,
                        reason,
                        lap_number,
                    } => match command.as_str() {
                        "start_race" => {
                            // Get simulation mode from app state and initialize race epoch.
                            let now = now_epoch_seconds();
                            let start_epoch = start_at.or(go_at).unwrap_or(now);
                            let rt = tokio::runtime::Handle::current();
                            let is_simulation = rt.block_on(async {
                                let mut app = app.lock().await;
                                let sim_mode = app.simulation_mode;

                                // If in hardware mode, request reset
                                if !sim_mode {
                                    app.reset_requested = true;
                                }

                                app.race_active = true;
                                app.race_start_time = Some(Instant::now());
                                app.race_start_epoch = Some(start_epoch);
                                app.lap_counts.clear();

                                sim_mode
                            });
                            let ready_epoch = ready_at.unwrap_or(start_epoch - 2.0);
                            let set_epoch = set_at.unwrap_or(start_epoch - 1.0);
                            let go_epoch = go_at.unwrap_or(start_epoch);

                            let _ = hw.send_message(&OutMessage::CountdownPhase {
                                phase: "ready".to_string(),
                                at: ready_epoch,
                                command_id: command_id.clone(),
                                source: source.clone(),
                            });
                            let _ = hw.send_message(&OutMessage::CountdownPhase {
                                phase: "set".to_string(),
                                at: set_epoch,
                                command_id: command_id.clone(),
                                source: source.clone(),
                            });
                            let _ = hw.send_message(&OutMessage::CountdownPhase {
                                phase: "go".to_string(),
                                at: go_epoch,
                                command_id: command_id.clone(),
                                source: source.clone(),
                            });

                            let _ = hw.send_message(&OutMessage::StartRace {
                                at: start_epoch,
                                command_id: command_id.clone(),
                                source: source.clone(),
                                simulated: is_simulation,
                            });

                            let status_message = if is_simulation {
                                format!("Simulation race scheduled at {:.3}", start_epoch)
                            } else {
                                format!("Race scheduled at {:.3}", start_epoch)
                            };

                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "start_race".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some(status_message.clone()),
                                racer_id: None,
                                penalty_seconds: None,
                                reason: None,
                                lap_number: None,
                            });

                            info!("{}", status_message);
                        }
                        "end_race" => {
                            // Get simulation mode from app state
                            let rt = tokio::runtime::Handle::current();
                            let is_simulation = rt.block_on(async {
                                let mut app = app.lock().await;
                                app.race_active = false;
                                app.race_start_time = None;
                                app.race_start_epoch = None;
                                app.lap_counts.clear();
                                app.simulation_mode
                            });

                            let status_message = if is_simulation {
                                "Simulation race ended".to_string()
                            } else {
                                "Race ended".to_string()
                            };

                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "end_race".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some(status_message.clone()),
                                racer_id: None,
                                penalty_seconds: None,
                                reason: None,
                                lap_number: None,
                            });

                            info!("{}", status_message);
                        }
                        "reset_race" => {
                            // Request hardware reset commands in hardware mode.
                            let rt = tokio::runtime::Handle::current();
                            rt.block_on(async {
                                let mut app = app.lock().await;
                                app.reset_requested = true;
                                app.race_active = false;
                                app.race_start_time = None;
                                app.race_start_epoch = None;
                                app.lap_counts.clear();
                            });

                            let status_message = "Race reset requested".to_string();
                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "reset_race".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some(status_message.clone()),
                                racer_id: None,
                                penalty_seconds: None,
                                reason: None,
                                lap_number: None,
                            });
                            info!("{}", status_message);
                        }
                        "simulate_lap" => {
                            let rid = racer_id.unwrap_or(1);
                            let rel_race_time = race_time.unwrap_or(0.0);

                            let rt = tokio::runtime::Handle::current();
                            let (lap_number, race_start_at, lap_at) = rt.block_on(async {
                                let mut app = app.lock().await;
                                next_lap_metadata(&mut app, rid, rel_race_time)
                            });

                            if let Err(e) = hw.send_message(&OutMessage::Lap {
                                racer_id: rid,
                                sensor_id: sensor_id.unwrap_or(1),
                                race_time: rel_race_time,
                                lap_number,
                                race_start_at,
                                lap_at,
                                simulated: true,
                            }) {
                                error!("Failed to send lap message: {}", e);
                            }
                            info!("Simulated lap for racer {} (lap #{})", rid, lap_number);
                        }
                        "add_penalty" => {
                            let rid = match racer_id {
                                Some(id) => id,
                                None => {
                                    let _ = hw.send_message(&OutMessage::RaceControl {
                                        command: "add_penalty".to_string(),
                                        command_id: command_id.clone(),
                                        accepted: false,
                                        message: Some("Missing racer_id".to_string()),
                                        racer_id: None,
                                        penalty_seconds: None,
                                        reason: reason.clone(),
                                        lap_number: None,
                                    });
                                    continue;
                                }
                            };

                            let penalty = penalty_seconds.unwrap_or(5);
                            if penalty == 0 || penalty % 5 != 0 {
                                let _ = hw.send_message(&OutMessage::RaceControl {
                                    command: "add_penalty".to_string(),
                                    command_id: command_id.clone(),
                                    accepted: false,
                                    message: Some(
                                        "Penalty must be a positive 5-second increment".to_string(),
                                    ),
                                    racer_id: Some(rid),
                                    penalty_seconds: Some(penalty),
                                    reason: reason.clone(),
                                    lap_number: None,
                                });
                                continue;
                            }

                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "add_penalty".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some("Penalty accepted".to_string()),
                                racer_id: Some(rid),
                                penalty_seconds: Some(penalty),
                                reason: reason.clone(),
                                lap_number: None,
                            });
                            info!("Penalty accepted for racer {}: {}s", rid, penalty);
                        }
                        "remove_lap" => {
                            let rid = match racer_id {
                                Some(id) => id,
                                None => {
                                    let _ = hw.send_message(&OutMessage::RaceControl {
                                        command: "remove_lap".to_string(),
                                        command_id: command_id.clone(),
                                        accepted: false,
                                        message: Some("Missing racer_id".to_string()),
                                        racer_id: None,
                                        penalty_seconds: None,
                                        reason: reason.clone(),
                                        lap_number,
                                    });
                                    continue;
                                }
                            };

                            if let Some(lap_no) = lap_number {
                                if lap_no == 0 {
                                    let _ = hw.send_message(&OutMessage::RaceControl {
                                        command: "remove_lap".to_string(),
                                        command_id: command_id.clone(),
                                        accepted: false,
                                        message: Some("lap_number must be > 0".to_string()),
                                        racer_id: Some(rid),
                                        penalty_seconds: None,
                                        reason: reason.clone(),
                                        lap_number: Some(lap_no),
                                    });
                                    continue;
                                }
                            }

                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "remove_lap".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some("Lap removal accepted".to_string()),
                                racer_id: Some(rid),
                                penalty_seconds: None,
                                reason: reason.clone(),
                                lap_number,
                            });
                            info!(
                                "Lap removal accepted for racer {} lap {:?}",
                                rid, lap_number
                            );
                        }
                        "disqualify_racer" => {
                            let rid = match racer_id {
                                Some(id) => id,
                                None => {
                                    let _ = hw.send_message(&OutMessage::RaceControl {
                                        command: "disqualify_racer".to_string(),
                                        command_id: command_id.clone(),
                                        accepted: false,
                                        message: Some("Missing racer_id".to_string()),
                                        racer_id: None,
                                        penalty_seconds: None,
                                        reason: reason.clone(),
                                        lap_number: None,
                                    });
                                    continue;
                                }
                            };

                            let _ = hw.send_message(&OutMessage::RaceControl {
                                command: "disqualify_racer".to_string(),
                                command_id: command_id.clone(),
                                accepted: true,
                                message: Some("Racer disqualified".to_string()),
                                racer_id: Some(rid),
                                penalty_seconds: None,
                                reason: reason.clone(),
                                lap_number: None,
                            });
                            info!("Racer disqualified: {}", rid);
                        }
                        _ => {
                            error!("Unknown command: {}", command);
                        }
                    },
                }
            }
        }
    })
    .await?;

    Ok(())
}

// Render the TUI
fn render_ui(terminal: &mut Terminal<CrosstermBackend<io::Stdout>>, app: &App) -> Result<()> {
    terminal.draw(|f| {
        let chunks = Layout::default()
            .direction(Direction::Vertical)
            .constraints([
                Constraint::Length(3), // Header
                Constraint::Min(0),    // Messages
                Constraint::Length(4), // Status (now 4 lines: keys, message count, connection status)
            ])
            .split(f.area());

        // Header
        let mode_text = if app.simulation_mode {
            "SIMULATION MODE"
        } else {
            "HARDWARE MODE"
        };
        let header_text = format!("=== Hardware Comm Redis - {} ===", mode_text);
        let header = Paragraph::new(header_text)
            .style(
                Style::default()
                    .fg(Color::Cyan)
                    .add_modifier(Modifier::BOLD),
            )
            .block(Block::default().borders(Borders::BOTTOM));
        f.render_widget(header, chunks[0]);

        // Messages
        let available_width = chunks[1].width as usize;
        let messages: Vec<ListItem> = app
            .messages
            .iter()
            .rev()
            .take(chunks[1].height as usize - 2)
            .rev()
            .map(|m| {
                // Pad message to full width to clear any leftover text
                let padded = format!("{:width$}", m, width = available_width);
                ListItem::new(padded)
            })
            .collect();

        let messages_list = List::new(messages).block(Block::default().borders(Borders::NONE));
        f.render_widget(messages_list, chunks[1]);

        // Status bar
        let status_width = chunks[2].width as usize;
        let mut status_lines = vec![];

        if app.simulation_mode {
            let keys_text = "Keys: [S]tart race | Sto[P] race | [1-4] Simulate lap | [Q]uit";
            let padding = " ".repeat(status_width.saturating_sub(keys_text.len()));
            let padding_str = format!("uit{}", padding);
            status_lines.push(Line::from(vec![
                Span::raw("Keys: "),
                Span::styled("[S]", Style::default().fg(Color::Yellow)),
                Span::raw("tart race | Sto"),
                Span::styled("[P]", Style::default().fg(Color::Yellow)),
                Span::raw(" race | "),
                Span::styled("[1-4]", Style::default().fg(Color::Yellow)),
                Span::raw(" Simulate lap | "),
                Span::styled("[Q]", Style::default().fg(Color::Yellow)),
                Span::raw(padding_str),
            ]));
        } else {
            let keys_text = "Keys: [Q]uit";
            let padding = " ".repeat(status_width.saturating_sub(keys_text.len()));
            let padding_str = format!("uit{}", padding);
            status_lines.push(Line::from(vec![
                Span::raw("Keys: "),
                Span::styled("[Q]", Style::default().fg(Color::Yellow)),
                Span::raw(padding_str),
            ]));
        }

        let msg_count_text = format!("Messages received: {}", app.message_count);
        let padded_msg_count = format!("{:width$}", msg_count_text, width = status_width);
        status_lines.push(Line::from(padded_msg_count));

        // Add connection status line
        let (status_text, status_color) = app.get_connection_status();
        let padded_status = format!("{:width$}", status_text, width = status_width);
        status_lines.push(Line::from(Span::styled(
            padded_status,
            Style::default()
                .fg(status_color)
                .add_modifier(Modifier::BOLD),
        )));

        let status = Paragraph::new(status_lines).block(Block::default().borders(Borders::TOP));
        f.render_widget(status, chunks[2]);
    })?;

    Ok(())
}

fn get_default_serial_port() -> String {
    #[cfg(target_os = "macos")]
    {
        "/dev/tty.usbserial-AB0KLIK2".to_string()
    }
    #[cfg(target_os = "linux")]
    {
        "/dev/ttyUSB0".to_string()
    }
    #[cfg(not(any(target_os = "macos", target_os = "linux")))]
    {
        panic!("Unsupported OS for default serial port")
    }
}

#[tokio::main]
async fn main() -> Result<()> {
    // Initialize logging
    tracing_subscriber::fmt()
        .with_writer(std::fs::File::create("hardware_redis.log")?)
        .with_ansi(false)
        .init();

    info!("Starting franklin-hardware-monitor");

    // Parse command-line arguments
    let args: Vec<String> = std::env::args().collect();
    let simulation_mode = args.contains(&"--sim".to_string()) || args.contains(&"-s".to_string());
    let verbose = args.contains(&"--verbose".to_string()) || args.contains(&"-v".to_string());

    // Parse redis socket path (--redis-socket <path>)
    let redis_socket_path = if let Some(pos) = args.iter().position(|a| a == "--redis-socket") {
        args.get(pos + 1)
            .map(|s| s.as_str())
            .unwrap_or(DEFAULT_REDIS_SOCKET_PATH)
    } else {
        DEFAULT_REDIS_SOCKET_PATH
    };

    // Parse serial port (--serial-port <path>)
    let serial_port = if let Some(pos) = args.iter().position(|a| a == "--serial-port" || a == "-p")
    {
        args.get(pos + 1).map(|s| s.to_string())
    } else if !simulation_mode {
        // Default serial port based on OS
        Some(get_default_serial_port())
    } else {
        None
    };

    // Parse baudrate (--baudrate <rate>)
    let baudrate = if let Some(pos) = args.iter().position(|a| a == "--baudrate" || a == "-b") {
        args.get(pos + 1)
            .and_then(|s| s.parse::<u32>().ok())
            .unwrap_or(9600)
    } else {
        9600
    };

    // Create app state
    let app = Arc::new(Mutex::new(App::new(simulation_mode, verbose)));

    // Create hardware comm
    let hw = Arc::new(HardwareComm::new(redis_socket_path, serial_port, baudrate)?);

    // Test Redis connection
    match hw.redis_client.get_connection() {
        Ok(mut conn) => {
            redis::cmd("PING").query::<String>(&mut conn)?;
            info!("Redis connection successful");
        }
        Err(e) => {
            error!("Failed to connect to Redis: {}", e);
            eprintln!("Failed to connect to Redis: {}", e);
            eprintln!("Make sure Redis is running with: redis-server --unixsocket ./redis.sock");
            return Err(e.into());
        }
    }

    // Start background tasks
    let app_clone = app.clone();
    let hw_clone = hw.clone();
    tokio::spawn(async move {
        if let Err(e) = redis_listener_task(hw_clone, app_clone).await {
            error!("Redis listener task error: {}", e);
        }
    });

    if simulation_mode {
        let app_clone = app.clone();
        let hw_clone = hw.clone();
        tokio::spawn(async move {
            if let Err(e) = simulation_task(hw_clone, app_clone).await {
                error!("Simulation task error: {}", e);
            }
        });

        let app_clone = app.clone();
        let hw_clone = hw.clone();
        tokio::spawn(async move {
            if let Err(e) = command_handler_task(hw_clone, app_clone).await {
                error!("Command handler task error: {}", e);
            }
        });
    } else {
        // Hardware mode - spawn hardware task
        let app_clone = app.clone();
        let hw_clone = hw.clone();
        tokio::spawn(async move {
            if let Err(e) = hardware_task(hw_clone, app_clone).await {
                error!("Hardware task error: {}", e);
            }
        });

        // Also spawn command handler for external commands
        let app_clone = app.clone();
        let hw_clone = hw.clone();
        tokio::spawn(async move {
            if let Err(e) = command_handler_task(hw_clone, app_clone).await {
                error!("Command handler task error: {}", e);
            }
        });
    }

    // Setup terminal
    enable_raw_mode()?;
    let mut stdout = io::stdout();
    execute!(stdout, EnterAlternateScreen, EnableMouseCapture)?;
    let backend = CrosstermBackend::new(stdout);
    let mut terminal = Terminal::new(backend)?;

    // Main loop
    let result = run_app(&mut terminal, app.clone(), hw.clone()).await;

    // Restore terminal
    disable_raw_mode()?;
    execute!(
        terminal.backend_mut(),
        LeaveAlternateScreen,
        DisableMouseCapture
    )?;
    terminal.show_cursor()?;

    result
}

async fn run_app(
    terminal: &mut Terminal<CrosstermBackend<io::Stdout>>,
    app: Arc<Mutex<App>>,
    hw: Arc<HardwareComm>,
) -> Result<()> {
    loop {
        // Render
        {
            let app_lock = app.lock().await;
            render_ui(terminal, &app_lock)?;
        }

        // Handle input (non-blocking with timeout)
        if event::poll(Duration::from_millis(100))? {
            if let Event::Key(key) = event::read()? {
                let mut app_lock = app.lock().await;
                handle_input(&mut app_lock, &hw, key.code)?;

                if app_lock.should_quit {
                    break;
                }
            }
        }
    }

    Ok(())
}
