package main

import (
	"flag"
	"log"
	"os"
	"os/signal"
	"syscall"
	"time"
)

const (
	RedisSocketPath = "./redis.sock"
	RedisInChannel  = "hardware:in"
	RedisOutChannel = "hardware:out"
	LogFilePath     = "hardware_redis.log"
)

func main() {
	// Command line flags
	simulationMode := flag.Bool("sim", false, "Run in simulation mode")
	flag.BoolVar(simulationMode, "s", false, "Run in simulation mode (shorthand)")
	useTUI := flag.Bool("tui", false, "Run with interactive TUI")
	flag.BoolVar(useTUI, "t", false, "Run with interactive TUI (shorthand)")
	flag.Parse()

	// Setup logging
	logFile, err := os.OpenFile(LogFilePath, os.O_APPEND|os.O_CREATE|os.O_WRONLY, 0644)
	if err != nil {
		log.Fatalf("Failed to open log file: %v", err)
	}
	defer logFile.Close()

	logger := log.New(logFile, "", log.LstdFlags)
	logger.Printf("Starting hardware communication in mode: %s (TUI: %v)", modeString(*simulationMode), *useTUI)

	// If TUI mode, run TUI and hardware comm in separate goroutines
	if *useTUI {
		// Get serial port
		var serialPort string
		if !*simulationMode {
			serialPort = getDefaultSerialPort()
		}

		// Create hardware communication instance
		hw, err := NewHardwareComm(HardwareConfig{
			SerialPort:     serialPort,
			Baudrate:       9600,
			RedisSocket:    RedisSocketPath,
			SimulationMode: *simulationMode,
			Logger:         logger,
		})
		if err != nil {
			logger.Fatalf("Failed to create hardware comm: %v", err)
		}
		defer hw.Close()

		// Start hardware comm in background
		go func() {
			if err := hw.Run(); err != nil {
				logger.Printf("Hardware comm error: %v", err)
			}
		}()

		// Give hardware comm time to start and connect to Redis
		logger.Println("Waiting for hardware comm to initialize...")
		time.Sleep(1 * time.Second)

		// Run TUI (blocks until quit)
		if err := RunTUI(*simulationMode); err != nil {
			logger.Fatalf("TUI error: %v", err)
		}

		// Stop hardware comm
		hw.Stop()
		return
	}

	// Non-TUI mode: run as daemon
	// Get serial port
	var serialPort string
	if !*simulationMode {
		serialPort = getDefaultSerialPort()
	}

	// Create hardware communication instance
	hw, err := NewHardwareComm(HardwareConfig{
		SerialPort:     serialPort,
		Baudrate:       9600,
		RedisSocket:    RedisSocketPath,
		SimulationMode: *simulationMode,
		Logger:         logger,
	})
	if err != nil {
		logger.Fatalf("Failed to create hardware comm: %v", err)
	}
	defer hw.Close()

	// Handle graceful shutdown
	sigChan := make(chan os.Signal, 1)
	signal.Notify(sigChan, os.Interrupt, syscall.SIGTERM)

	// Run in goroutine
	errChan := make(chan error, 1)
	go func() {
		errChan <- hw.Run()
	}()

	// Wait for signal or error
	select {
	case <-sigChan:
		logger.Println("Received shutdown signal")
		hw.Stop()
	case err := <-errChan:
		if err != nil {
			logger.Printf("Hardware comm error: %v", err)
		}
	}

	logger.Println("Hardware communication stopped")
}

func modeString(simulation bool) string {
	if simulation {
		return "SIMULATION"
	}
	return "HARDWARE"
}

func getDefaultSerialPort() string {
	switch os.Getenv("GOOS") {
	case "linux":
		return "/dev/ttyUSB0"
	case "darwin":
		return "/dev/tty.usbserial-AB0KLIK2"
	default:
		// Check actual runtime OS
		if _, err := os.Stat("/dev/ttyUSB0"); err == nil {
			return "/dev/ttyUSB0"
		}
		return "/dev/tty.usbserial-AB0KLIK2"
	}
}
