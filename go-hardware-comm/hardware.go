package main

import (
	"bufio"
	"context"
	"encoding/json"
	"fmt"
	"log"
	"strconv"
	"strings"
	"sync"
	"time"
)

// HardwareConfig contains configuration for hardware communication
type HardwareConfig struct {
	SerialPort     string
	Baudrate       int
	RedisSocket    string
	SimulationMode bool
	Logger         *log.Logger
}

// HardwareComm manages hardware communication
type HardwareComm struct {
	config           HardwareConfig
	redis            *RedisClient
	serial           *SerialPort
	running          bool
	runningMutex     sync.RWMutex
	ctx              context.Context
	cancel           context.CancelFunc
	commandQueue     chan Command
	simRaceActive    bool
	simLastHeartbeat time.Time
}

// Command represents a command from Redis
type Command struct {
	Type     string  `json:"type"`
	Command  string  `json:"command"`
	RacerID  int     `json:"racer_id,omitempty"`
	SensorID int     `json:"sensor_id,omitempty"`
	RaceTime float64 `json:"race_time,omitempty"`
}

// Message represents a message to send to Redis
type Message struct {
	Type     string  `json:"type"`
	Message  string  `json:"message,omitempty"`
	RacerID  int     `json:"racer_id,omitempty"`
	SensorID int     `json:"sensor_id,omitempty"`
	RaceTime float64 `json:"race_time,omitempty"`
	RawTime  float64 `json:"raw_time,omitempty"`
	Flag1    string  `json:"flag1,omitempty"`
	Flag2    string  `json:"flag2,omitempty"`
	Line     string  `json:"line,omitempty"`
	Detail   string  `json:"detail,omitempty"`
}

// NewHardwareComm creates a new hardware communication instance
func NewHardwareComm(config HardwareConfig) (*HardwareComm, error) {
	ctx, cancel := context.WithCancel(context.Background())

	// Create Redis client
	redis, err := NewRedisClient(config.RedisSocket, config.Logger)
	if err != nil {
		cancel()
		return nil, fmt.Errorf("failed to create redis client: %w", err)
	}

	hw := &HardwareComm{
		config:           config,
		redis:            redis,
		ctx:              ctx,
		cancel:           cancel,
		commandQueue:     make(chan Command, 100),
		simLastHeartbeat: time.Now(),
	}

	mode := "SIMULATION"
	if !config.SimulationMode {
		mode = "HARDWARE"
	}
	config.Logger.Printf("HardwareComm initialized in %s mode", mode)

	return hw, nil
}

// Close closes all resources
func (hw *HardwareComm) Close() error {
	hw.cancel()
	if hw.serial != nil {
		hw.serial.Close()
	}
	if hw.redis != nil {
		hw.redis.Close()
	}
	close(hw.commandQueue)
	return nil
}

// Stop stops the hardware communication
func (hw *HardwareComm) Stop() {
	hw.runningMutex.Lock()
	hw.running = false
	hw.runningMutex.Unlock()
	hw.cancel()
}

// IsRunning returns whether hardware communication is running
func (hw *HardwareComm) IsRunning() bool {
	hw.runningMutex.RLock()
	defer hw.runningMutex.RUnlock()
	return hw.running
}

// sendOut sends a message to Redis
func (hw *HardwareComm) sendOut(msg Message) error {
	data, err := json.Marshal(msg)
	if err != nil {
		return fmt.Errorf("failed to marshal message: %w", err)
	}

	return hw.redis.Publish(RedisOutChannel, string(data))
}

// openConnection opens the serial port connection
func (hw *HardwareComm) openConnection() error {
	if hw.config.SimulationMode {
		hw.config.Logger.Println("Running in simulation mode - no serial connection")
		return hw.sendOut(Message{
			Type:    "status",
			Message: "Running in simulation mode",
		})
	}

	hw.config.Logger.Printf("Opening serial connection to %s at %d baud", hw.config.SerialPort, hw.config.Baudrate)

	serial, err := NewSerialPort(hw.config.SerialPort, hw.config.Baudrate)
	if err != nil {
		errorMsg := fmt.Sprintf("Lap tracking hardware not found at %s", hw.config.SerialPort)
		hw.config.Logger.Printf("Serial connection failed: %v", err)
		hw.sendOut(Message{
			Type:    "hardware_error",
			Message: errorMsg,
			Detail:  err.Error(),
		})
		return err
	}

	hw.serial = serial
	hw.config.Logger.Printf("Successfully connected to hardware at %s", hw.config.SerialPort)

	// Wake up hardware
	hw.serial.Write([]byte("\r\n"))
	hw.config.Logger.Println("Sent wake-up CR/LF to hardware")
	time.Sleep(100 * time.Millisecond)

	return hw.sendOut(Message{
		Type:    "status",
		Message: fmt.Sprintf("Hardware connected and initialized at %s", hw.config.SerialPort),
	})
}

// sendResetCommand sends reset commands to hardware
func (hw *HardwareComm) sendResetCommand() error {
	hw.config.Logger.Println("Sending reset commands to hardware")

	commands := [][]byte{
		{0x01, 0x3f, 0x2c, 0x32, 0x33, 0x32, 0x2c, 0x30, 0x2c, 0x31, 0x34, 0x2c, 0x30, 0x2c, 0x31, 0x2c, 0x0d, 0x0a},
		{0x01, 0x3f, 0x2c, 0x32, 0x33, 0x32, 0x2c, 0x30, 0x2c, 0x32, 0x34, 0x2c, 0x30, 0x2c, 0x0d, 0x0a},
		{0x01, 0x3f, 0x2c, 0x32, 0x33, 0x32, 0x2c, 0x30, 0x2c, 0x39, 0x2c, 0x30, 0x2c, 0x0d, 0x0a},
		{0x01, 0x3f, 0x2c, 0x32, 0x33, 0x32, 0x2c, 0x30, 0x2c, 0x31, 0x34, 0x2c, 0x31, 0x2c, 0x30, 0x0d, 0x0a},
	}

	if hw.serial == nil {
		msg := "Serial port not open"
		hw.config.Logger.Println(msg)
		return hw.sendOut(Message{Type: "status", Message: msg})
	}

	for _, cmd := range commands {
		if _, err := hw.serial.Write(cmd); err != nil {
			errMsg := fmt.Sprintf("Error sending command: %v", err)
			hw.config.Logger.Println(errMsg)
			return hw.sendOut(Message{Type: "status", Message: errMsg})
		}
		hw.config.Logger.Println("Sent command")
	}

	return nil
}

// handleCommand processes a command from Redis
func (hw *HardwareComm) handleCommand(cmd Command) error {
	hw.config.Logger.Printf("Received command: %v", cmd)

	if cmd.Type != "command" {
		return nil
	}

	switch cmd.Command {
	case "start_race":
		hw.config.Logger.Println("Received start_race command - sending reset commands")
		if hw.config.SimulationMode {
			hw.simRaceActive = true
			hw.sendOut(Message{Type: "status", Message: "Simulation race started"})
			hw.config.Logger.Println("Simulation race started")
		} else {
			if err := hw.sendResetCommand(); err != nil {
				return err
			}
			hw.sendOut(Message{Type: "status", Message: "Start race commands sent"})
			hw.config.Logger.Println("Start race commands sent")
		}

	case "stop_race":
		if hw.config.SimulationMode {
			hw.simRaceActive = false
			hw.sendOut(Message{Type: "status", Message: "Simulation race stopped"})
			hw.config.Logger.Println("Simulation race stopped")
		} else {
			hw.sendOut(Message{Type: "status", Message: "Stop race command received"})
			hw.config.Logger.Println("Stop race command received")
		}

	case "simulate_lap":
		if hw.config.SimulationMode {
			hw.sendOut(Message{
				Type:     "lap",
				RacerID:  cmd.RacerID,
				SensorID: cmd.SensorID,
				RaceTime: cmd.RaceTime,
			})
			hw.config.Logger.Printf("Simulated lap: racer=%d sensor=%d time=%.3f", cmd.RacerID, cmd.SensorID, cmd.RaceTime)
		}
	}

	return nil
}

// handleOutputLine processes a line from the hardware
func (hw *HardwareComm) handleOutputLine(line string, lastHeartbeat *time.Time) error {
	hw.sendOut(Message{Type: "debug", Message: fmt.Sprintf("Read line: %s", line)})
	hw.config.Logger.Printf("Read line processed: %s", line)

	if strings.HasPrefix(line, "\x01#") && strings.Contains(line, "xC249") {
		*lastHeartbeat = time.Now()
		hw.sendOut(Message{Type: "heartbeat"})
		hw.config.Logger.Println("Heartbeat detected")
	} else if strings.HasPrefix(line, "\x01@") {
		// Parse lap message
		parts := strings.Split(line, "\t")
		if len(parts) >= 6 {
			racerID, err1 := strconv.Atoi(parts[3])
			sensorID, err2 := strconv.Atoi(parts[1])
			raceTime, err3 := strconv.ParseFloat(parts[4], 64)

			if err1 == nil && err2 == nil && err3 == nil {
				hw.sendOut(Message{
					Type:     "lap",
					RacerID:  racerID,
					SensorID: sensorID,
					RaceTime: raceTime,
				})
				hw.config.Logger.Printf("Lap message parsed and sent: racer=%d sensor=%d time=%.3f", racerID, sensorID, raceTime)
			} else {
				msg := fmt.Sprintf("Error parsing lap line: %s", line)
				hw.sendOut(Message{Type: "status", Message: msg})
				hw.config.Logger.Println(msg)
			}
		} else {
			msg := fmt.Sprintf("Malformed lap line: %s", line)
			hw.sendOut(Message{Type: "status", Message: msg})
			hw.config.Logger.Println(msg)
		}
	} else if strings.HasPrefix(line, "\x01$") {
		// Parse new_msg message
		parts := strings.Split(line, "\t")
		if len(parts) >= 5 {
			sensorID, err1 := strconv.Atoi(parts[1])
			rawTimeStr := strings.Replace(parts[2], ",", ".", -1)
			rawTime, err2 := strconv.ParseFloat(rawTimeStr, 64)

			if err1 == nil && err2 == nil {
				hw.sendOut(Message{
					Type:     "new_msg",
					SensorID: sensorID,
					RawTime:  rawTime,
					Flag1:    parts[3],
					Flag2:    parts[4],
				})
				hw.config.Logger.Printf("New message parsed and sent: sensor=%d time=%.3f", sensorID, rawTime)
			} else {
				msg := fmt.Sprintf("Error parsing new_msg line: %s", line)
				hw.sendOut(Message{Type: "status", Message: msg})
				hw.config.Logger.Println(msg)
			}
		} else {
			msg := fmt.Sprintf("Malformed new_msg line: %s", line)
			hw.sendOut(Message{Type: "status", Message: msg})
			hw.config.Logger.Println(msg)
		}
	} else {
		hw.sendOut(Message{Type: "raw", Line: line})
		hw.config.Logger.Printf("Raw line sent: %s", line)
	}

	return nil
}

// simulationLoop runs the simulation mode loop
func (hw *HardwareComm) simulationLoop() error {
	lastHeartbeat := time.Now()
	ticker := time.NewTicker(100 * time.Millisecond)
	defer ticker.Stop()

	for hw.IsRunning() {
		select {
		case <-hw.ctx.Done():
			return nil
		case cmd := <-hw.commandQueue:
			hw.handleCommand(cmd)
		case <-ticker.C:
			// Send periodic heartbeats
			if time.Since(lastHeartbeat) > 2*time.Second {
				hw.sendOut(Message{Type: "heartbeat"})
				lastHeartbeat = time.Now()
				hw.config.Logger.Println("Simulation heartbeat sent")
			}
		}
	}

	return nil
}

// Run starts the hardware communication loop
func (hw *HardwareComm) Run() error {
	hw.runningMutex.Lock()
	hw.running = true
	hw.runningMutex.Unlock()

	hw.config.Logger.Println("HardwareComm started running")

	// Test Redis connection
	if err := hw.redis.Ping(); err != nil {
		errMsg := fmt.Sprintf("Failed to connect to Redis: %v", err)
		hw.config.Logger.Println(errMsg)
		hw.sendOut(Message{Type: "status", Message: errMsg})
		return err
	}

	hw.config.Logger.Println("Redis connection successful")
	hw.sendOut(Message{Type: "status", Message: "Redis connected"})

	// Start Redis listener
	go hw.redisListener()

	// Open connection
	if err := hw.openConnection(); err != nil && !hw.config.SimulationMode {
		hw.config.Logger.Printf("Cannot start: hardware not connected - %v", err)
		// Keep running to allow graceful shutdown
		<-hw.ctx.Done()
		return err
	}

	// Run main loop
	if hw.config.SimulationMode {
		hw.config.Logger.Println("Starting simulation mode")
		return hw.simulationLoop()
	}

	// Hardware mode
	hw.config.Logger.Println("Starting hardware mode")
	return hw.hardwareLoop()
}

// hardwareLoop runs the hardware mode loop
func (hw *HardwareComm) hardwareLoop() error {
	lastHeartbeat := time.Now()
	ticker := time.NewTicker(50 * time.Millisecond)
	defer ticker.Stop()

	scanner := bufio.NewScanner(hw.serial)

	for hw.IsRunning() {
		select {
		case <-hw.ctx.Done():
			return nil
		case cmd := <-hw.commandQueue:
			hw.handleCommand(cmd)
		case <-ticker.C:
			// Read from serial (non-blocking with timeout)
			if scanner.Scan() {
				line := scanner.Text()
				hw.handleOutputLine(line, &lastHeartbeat)
			}

			// Check heartbeat timeout
			if time.Since(lastHeartbeat) > 10*time.Second {
				msg := "Heartbeat lost"
				hw.sendOut(Message{Type: "status", Message: msg})
				hw.config.Logger.Println(msg)
				lastHeartbeat = time.Now() // Reset to avoid spam
			}
		}
	}

	if err := scanner.Err(); err != nil {
		hw.config.Logger.Printf("Scanner error: %v", err)
	}

	return nil
}

// redisListener listens for commands from Redis
func (hw *HardwareComm) redisListener() {
	hw.config.Logger.Println("Starting Redis listener")

	if err := hw.redis.Subscribe(RedisInChannel); err != nil {
		hw.config.Logger.Printf("Failed to subscribe to Redis: %v", err)
		return
	}

	for hw.IsRunning() {
		select {
		case <-hw.ctx.Done():
			return
		default:
			msg, err := hw.redis.GetMessage(hw.ctx, 100*time.Millisecond)
			if err != nil {
				continue
			}
			if msg == "" {
				continue
			}

			var cmd Command
			if err := json.Unmarshal([]byte(msg), &cmd); err != nil {
				hw.config.Logger.Printf("Error parsing command from redis: %v", err)
				continue
			}

			select {
			case hw.commandQueue <- cmd:
			case <-hw.ctx.Done():
				return
			}
		}
	}
}
