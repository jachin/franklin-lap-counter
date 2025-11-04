use anyhow::{Context, Result};
use crossterm::{
    event::{self, DisableMouseCapture, EnableMouseCapture, Event, KeyCode},
    execute,
    terminal::{disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen},
};
use ratatui::{
    backend::CrosstermBackend,
    layout::{Constraint, Direction, Layout},
    style::{Color, Modifier, Style},
    text::{Line, Span},
    widgets::{Block, Borders, List, ListItem, Paragraph},
    Terminal,
};
use redis::Commands;
use serde::{Deserialize, Serialize};
use serialport::SerialPort;
use std::io::{self, BufRead, BufReader};
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::sync::Mutex;
use tracing::{error, info, warn};

// Redis configuration
const DEFAULT_REDIS_SOCKET_PATH: &str = "./redis.sock";
const REDIS_IN_CHANNEL: &str = "hardware:in";
const REDIS_OUT_CHANNEL: &str = "hardware:out";

// Message types
#[derive(Debug, Clone, Serialize, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum OutMessage {
    Lap {
        racer_id: u32,
        sensor_id: u32,
        race_time: f64,
    },
    Heartbeat,
    Status {
        message: String,
    },
    Error {
        message: String,
    },
    Debug {
        message: String,
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
        racer_id: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        sensor_id: Option<u32>,
        #[serde(skip_serializing_if = "Option::is_none")]
        race_time: Option<f64>,
    },
}

// Application state
struct App {
    messages: Vec<String>,
    message_count: usize,
    simulation_mode: bool,
    race_active: bool,
    race_start_time: Option<Instant>,
    should_quit: bool,
}

impl App {
    fn new(simulation_mode: bool) -> Self {
        Self {
            messages: Vec::new(),
            message_count: 0,
            simulation_mode,
            race_active: false,
            race_start_time: None,
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
            } => format!(
                "[LAP] Racer {} - Sensor {} - Time: {:.3}s",
                racer_id, sensor_id, race_time
            ),
            OutMessage::Heartbeat => "[HEARTBEAT] ♥".to_string(),
            OutMessage::Status { message } => format!("[STATUS] {}", message),
            OutMessage::Error { message } => format!("[ERROR] {}", message),
            OutMessage::Debug { message } => format!("[DEBUG] {}", message),
            OutMessage::Raw { line } => format!("[RAW] {}", line),
        }
    }
}

// Hardware communication handler
struct HardwareComm {
    redis_client: redis::Client,
    simulation_mode: bool,
    serial_port_path: Option<String>,
    baudrate: u32,
}

impl HardwareComm {
    fn new(
        simulation_mode: bool,
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
            simulation_mode,
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
        port.write_all(b"\r\n")?;
        info!("Sent wake-up CR/LF to hardware");
        std::thread::sleep(Duration::from_millis(100));

        // Reset commands from Python version
        let commands: Vec<&[u8]> = vec![
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x30\x2c\x31\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x32\x34\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x39\x2c\x30\x2c\x0d\x0a",
            b"\x01\x3f\x2c\x32\x33\x32\x2c\x30\x2c\x31\x34\x2c\x31\x2c\x30\x0d\x0a",
        ];

        for cmd in commands {
            port.write_all(cmd)?;
            info!("Sent reset command: {:?}", cmd);
        }

        Ok(())
    }

    fn parse_hardware_line(&self, line: &str) -> Option<OutMessage> {
        if line.starts_with("\x01#") && line.contains("xC249") {
            // Heartbeat
            Some(OutMessage::Heartbeat)
        } else if line.starts_with("\x01@") {
            // Lap message: \x01@\t<sensor_id>\t...\t<racer_id>\t<race_time>\t...
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.len() >= 6 {
                match (parts.get(3), parts.get(1), parts.get(4)) {
                    (Some(racer_id_str), Some(sensor_id_str), Some(race_time_str)) => {
                        if let (Ok(racer_id), Ok(sensor_id), Ok(race_time)) = (
                            racer_id_str.parse::<u32>(),
                            sensor_id_str.parse::<u32>(),
                            race_time_str.parse::<f64>(),
                        ) {
                            return Some(OutMessage::Lap {
                                racer_id,
                                sensor_id,
                                race_time,
                            });
                        }
                    }
                    _ => {}
                }
            }
            Some(OutMessage::Status {
                message: format!("Malformed lap line: {}", line),
            })
        } else if line.starts_with("\x01$") {
            // New message: \x01$\t<sensor_id>\t<raw_time>\t<flag1>\t<flag2>
            let parts: Vec<&str> = line.split('\t').collect();
            if parts.len() >= 5 {
                // Just send as raw for now - we can add a NewMsg variant if needed
                Some(OutMessage::Raw {
                    line: line.to_string(),
                })
            } else {
                Some(OutMessage::Status {
                    message: format!("Malformed new_msg line: {}", line),
                })
            }
        } else if !line.is_empty() {
            Some(OutMessage::Raw {
                line: line.to_string(),
            })
        } else {
            None
        }
    }

    fn send_message(&self, msg: &OutMessage) -> Result<()> {
        let mut conn = self
            .redis_client
            .get_connection()
            .context("Failed to get Redis connection")?;

        let json = serde_json::to_string(msg).context("Failed to serialize message")?;

        conn.publish::<_, _, ()>(REDIS_OUT_CHANNEL, json)
            .context("Failed to publish to Redis")?;

        Ok(())
    }

    fn send_command(&self, cmd: &InMessage) -> Result<()> {
        let mut conn = self
            .redis_client
            .get_connection()
            .context("Failed to get Redis connection")?;

        let json = serde_json::to_string(cmd).context("Failed to serialize command")?;

        conn.publish::<_, _, ()>(REDIS_IN_CHANNEL, json)
            .context("Failed to publish command to Redis")?;

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
            error!("Failed to subscribe to Redis channel: {}", e);
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
                    let formatted = app.format_out_message(&out_msg);
                    app.add_message(formatted);
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

    // Send initial status
    hw.send_message(&OutMessage::Status {
        message: "Running in simulation mode".to_string(),
    })?;

    let mut last_heartbeat = Instant::now();

    loop {
        tokio::time::sleep(Duration::from_millis(100)).await;

        // Send heartbeat every 2 seconds
        if last_heartbeat.elapsed() >= Duration::from_secs(2) {
            hw.send_message(&OutMessage::Heartbeat)?;
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
        let mut port = match hw.open_serial_connection() {
            Ok(p) => p,
            Err(e) => {
                error!("Failed to open serial connection: {}", e);
                let _ = hw.send_message(&OutMessage::Status {
                    message: format!("Lap tracking hardware not found: {}", e),
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
        if let Err(e) = hw.send_message(&OutMessage::Status {
            message: status_msg,
        }) {
            error!("Failed to send status: {}", e);
        }

        // Send reset commands
        if let Err(e) = hw.send_reset_commands(&mut port) {
            error!("Failed to send reset commands: {}", e);
            let _ = hw.send_message(&OutMessage::Status {
                message: format!("Error sending reset commands: {}", e),
            });
        }

        // Create buffered reader for line reading
        let mut reader = BufReader::new(port);
        let mut last_heartbeat = Instant::now();

        loop {
            // Check if we should quit
            let rt = tokio::runtime::Handle::current();
            let should_quit = rt.block_on(async {
                let app = app.lock().await;
                app.should_quit
            });

            if should_quit {
                info!("Hardware task exiting");
                break;
            }

            // Read line from serial (with timeout)
            let mut line_buf = String::new();
            match reader.read_line(&mut line_buf) {
                Ok(0) => {
                    // No data, continue
                    std::thread::sleep(Duration::from_millis(50));
                    continue;
                }
                Ok(_) => {
                    let line = line_buf.trim();

                    // Parse and send message
                    if let Some(msg) = hw.parse_hardware_line(line) {
                        // Update heartbeat time if we got a heartbeat
                        if matches!(msg, OutMessage::Heartbeat) {
                            last_heartbeat = Instant::now();
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
                    });
                }
            }

            // Check for heartbeat timeout (10 seconds)
            if last_heartbeat.elapsed() > Duration::from_secs(10) {
                warn!("Heartbeat lost");
                let _ = hw.send_message(&OutMessage::Status {
                    message: "Heartbeat lost".to_string(),
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
            // In simulation mode, send message directly without going through Redis command channel
            hw.send_message(&OutMessage::Status {
                message: "Simulation race started".to_string(),
            })?;
            info!("Simulation race started");
        }
        KeyCode::Char('p') | KeyCode::Char('P') if app.simulation_mode => {
            app.race_active = false;
            app.race_start_time = None;
            // In simulation mode, send message directly without going through Redis command channel
            hw.send_message(&OutMessage::Status {
                message: "Simulation race stopped".to_string(),
            })?;
            info!("Simulation race stopped");
        }
        KeyCode::Char(c @ '1'..='4') if app.simulation_mode => {
            let racer_id = c.to_digit(10).unwrap();
            let race_time = if let Some(start_time) = app.race_start_time {
                start_time.elapsed().as_secs_f64()
            } else {
                0.0
            };

            // In simulation mode, send lap message directly without going through Redis command channel
            hw.send_message(&OutMessage::Lap {
                racer_id,
                sensor_id: 1,
                race_time,
            })?;
            info!("Simulated lap for racer {}", racer_id);
        }
        _ => {}
    }

    Ok(())
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
            error!("Failed to subscribe to command channel: {}", e);
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
                        racer_id,
                        sensor_id,
                        race_time,
                    } => match command.as_str() {
                        "start_race" => {
                            if let Err(e) = hw.send_message(&OutMessage::Status {
                                message: "Simulation race started".to_string(),
                            }) {
                                error!("Failed to send status: {}", e);
                            }
                            info!("Simulation race started");
                        }
                        "stop_race" => {
                            if let Err(e) = hw.send_message(&OutMessage::Status {
                                message: "Simulation race stopped".to_string(),
                            }) {
                                error!("Failed to send status: {}", e);
                            }
                            info!("Simulation race stopped");
                        }
                        "simulate_lap" => {
                            if let Err(e) = hw.send_message(&OutMessage::Lap {
                                racer_id: racer_id.unwrap_or(1),
                                sensor_id: sensor_id.unwrap_or(1),
                                race_time: race_time.unwrap_or(0.0),
                            }) {
                                error!("Failed to send lap message: {}", e);
                            }
                            info!("Simulated lap for racer {}", racer_id.unwrap_or(1));
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
                Constraint::Length(3), // Status
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
        let messages: Vec<ListItem> = app
            .messages
            .iter()
            .rev()
            .take(chunks[1].height as usize - 2)
            .rev()
            .map(|m| ListItem::new(m.clone()))
            .collect();

        let messages_list = List::new(messages).block(Block::default().borders(Borders::NONE));
        f.render_widget(messages_list, chunks[1]);

        // Status bar
        let mut status_lines = vec![];

        if app.simulation_mode {
            status_lines.push(Line::from(vec![
                Span::raw("Keys: "),
                Span::styled("[S]", Style::default().fg(Color::Yellow)),
                Span::raw("tart race | Sto"),
                Span::styled("[P]", Style::default().fg(Color::Yellow)),
                Span::raw(" race | "),
                Span::styled("[1-4]", Style::default().fg(Color::Yellow)),
                Span::raw(" Simulate lap | "),
                Span::styled("[Q]", Style::default().fg(Color::Yellow)),
                Span::raw("uit"),
            ]));
        } else {
            status_lines.push(Line::from(vec![
                Span::raw("Keys: "),
                Span::styled("[Q]", Style::default().fg(Color::Yellow)),
                Span::raw("uit"),
            ]));
        }

        status_lines.push(Line::from(format!(
            "Messages received: {}",
            app.message_count
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

    info!("Starting hardware-comm");

    // Parse command-line arguments
    let args: Vec<String> = std::env::args().collect();
    let simulation_mode = args.contains(&"--sim".to_string()) || args.contains(&"-s".to_string());

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
    let app = Arc::new(Mutex::new(App::new(simulation_mode)));

    // Create hardware comm
    let hw = Arc::new(HardwareComm::new(
        simulation_mode,
        redis_socket_path,
        serial_port,
        baudrate,
    )?);

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
