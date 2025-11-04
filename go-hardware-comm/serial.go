package main

import (
	"fmt"
	"io"
	"os"

	"golang.org/x/sys/unix"
)

// SerialPort represents a serial port connection
type SerialPort struct {
	file *os.File
	fd   int
}

// NewSerialPort opens a serial port with the specified parameters
func NewSerialPort(port string, baudrate int) (*SerialPort, error) {
	// Open the serial port
	file, err := os.OpenFile(port, unix.O_RDWR|unix.O_NOCTTY|unix.O_NONBLOCK, 0)
	if err != nil {
		return nil, fmt.Errorf("failed to open serial port: %w", err)
	}

	fd := int(file.Fd())

	// Get current terminal settings
	termios, err := unix.IoctlGetTermios(fd, unix.TIOCGETA)
	if err != nil {
		file.Close()
		return nil, fmt.Errorf("failed to get termios: %w", err)
	}

	// Set baud rate
	speed := baudToSpeed(baudrate)
	termios.Ispeed = speed
	termios.Ospeed = speed

	// Configure 8N1 (8 data bits, no parity, 1 stop bit)
	termios.Cflag &^= unix.CSIZE  // Clear size bits
	termios.Cflag |= unix.CS8     // 8 data bits
	termios.Cflag &^= unix.PARENB // No parity
	termios.Cflag &^= unix.CSTOPB // 1 stop bit
	termios.Cflag |= unix.CREAD | unix.CLOCAL

	// Disable flow control
	termios.Iflag &^= unix.IXON | unix.IXOFF

	// Raw mode
	termios.Lflag &^= unix.ICANON | unix.ECHO | unix.ECHOE | unix.ISIG
	termios.Oflag &^= unix.OPOST

	// Set read timeout (deciseconds)
	termios.Cc[unix.VMIN] = 0   // Minimum number of characters
	termios.Cc[unix.VTIME] = 10 // Timeout in deciseconds (1 second)

	// Apply settings
	if err := unix.IoctlSetTermios(fd, unix.TIOCSETA, termios); err != nil {
		file.Close()
		return nil, fmt.Errorf("failed to set termios: %w", err)
	}

	// Set non-blocking mode for reads
	if err := unix.SetNonblock(fd, false); err != nil {
		file.Close()
		return nil, fmt.Errorf("failed to set blocking mode: %w", err)
	}

	return &SerialPort{
		file: file,
		fd:   fd,
	}, nil
}

// Read reads data from the serial port
func (sp *SerialPort) Read(p []byte) (n int, err error) {
	return sp.file.Read(p)
}

// Write writes data to the serial port
func (sp *SerialPort) Write(p []byte) (n int, err error) {
	return sp.file.Write(p)
}

// Close closes the serial port
func (sp *SerialPort) Close() error {
	if sp.file != nil {
		return sp.file.Close()
	}
	return nil
}

// Implement io.Reader interface for bufio.Scanner
var _ io.Reader = (*SerialPort)(nil)

// baudToSpeed converts a baudrate integer to a speed constant
func baudToSpeed(baudrate int) uint64 {
	switch baudrate {
	case 9600:
		return unix.B9600
	case 19200:
		return unix.B19200
	case 38400:
		return unix.B38400
	case 57600:
		return unix.B57600
	case 115200:
		return unix.B115200
	default:
		return unix.B9600
	}
}
