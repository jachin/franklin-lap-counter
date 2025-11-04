package main

import (
	"bufio"
	"context"
	"fmt"
	"log"
	"net"
	"strconv"
	"strings"
	"time"
)

// RedisClient represents a Redis client connection
type RedisClient struct {
	socketPath string
	conn       net.Conn
	reader     *bufio.Reader
	subConn    net.Conn // Separate connection for pub/sub
	subReader  *bufio.Reader
	logger     *log.Logger
}

// NewRedisClient creates a new Redis client
func NewRedisClient(socketPath string, logger *log.Logger) (*RedisClient, error) {
	conn, err := net.Dial("unix", socketPath)
	if err != nil {
		return nil, fmt.Errorf("failed to connect to redis socket: %w", err)
	}

	return &RedisClient{
		socketPath: socketPath,
		conn:       conn,
		reader:     bufio.NewReader(conn),
		logger:     logger,
	}, nil
}

// Close closes the Redis connections
func (rc *RedisClient) Close() error {
	var err error
	if rc.conn != nil {
		err = rc.conn.Close()
	}
	if rc.subConn != nil {
		if subErr := rc.subConn.Close(); subErr != nil && err == nil {
			err = subErr
		}
	}
	return err
}

// sendCommand sends a Redis command using RESP protocol
func (rc *RedisClient) sendCommand(args ...string) error {
	// Build RESP array command
	cmd := fmt.Sprintf("*%d\r\n", len(args))
	for _, arg := range args {
		cmd += fmt.Sprintf("$%d\r\n%s\r\n", len(arg), arg)
	}

	_, err := rc.conn.Write([]byte(cmd))
	return err
}

// readResponse reads a RESP response from Redis (main connection)
func (rc *RedisClient) readResponse() (string, error) {
	return rc.readResponseFrom(rc.reader)
}

// readResponseFromSub reads a RESP response from the subscription connection
func (rc *RedisClient) readResponseFromSub() (string, error) {
	return rc.readResponseFrom(rc.subReader)
}

// readResponseFrom reads a RESP response from the given reader
func (rc *RedisClient) readResponseFrom(reader *bufio.Reader) (string, error) {
	firstByte, err := reader.ReadByte()
	if err != nil {
		return "", err
	}

	switch firstByte {
	case '+': // Simple string
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		return strings.TrimRight(line, "\r\n"), nil

	case '-': // Error
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		return "", fmt.Errorf("redis error: %s", strings.TrimRight(line, "\r\n"))

	case ':': // Integer
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		return strings.TrimRight(line, "\r\n"), nil

	case '$': // Bulk string
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		sizeStr := strings.TrimRight(line, "\r\n")
		size, err := strconv.Atoi(sizeStr)
		if err != nil {
			return "", fmt.Errorf("invalid bulk string size: %w", err)
		}

		if size == -1 {
			return "", nil // Null bulk string
		}

		data := make([]byte, size)
		_, err = reader.Read(data)
		if err != nil {
			return "", err
		}

		// Read trailing \r\n
		reader.ReadByte()
		reader.ReadByte()

		return string(data), nil

	case '*': // Array
		line, err := reader.ReadString('\n')
		if err != nil {
			return "", err
		}
		sizeStr := strings.TrimRight(line, "\r\n")
		size, err := strconv.Atoi(sizeStr)
		if err != nil {
			return "", fmt.Errorf("invalid array size: %w", err)
		}

		if size == -1 {
			return "", nil // Null array
		}

		// For simplicity, we'll just return empty string for arrays
		// The actual array parsing happens in getMessage for subscribe
		return "", nil

	default:
		return "", fmt.Errorf("unknown response type: %c", firstByte)
	}
}

// Ping sends a PING command to Redis
func (rc *RedisClient) Ping() error {
	if err := rc.sendCommand("PING"); err != nil {
		return err
	}
	_, err := rc.readResponse()
	return err
}

// Publish publishes a message to a Redis channel
func (rc *RedisClient) Publish(channel, message string) error {
	if err := rc.sendCommand("PUBLISH", channel, message); err != nil {
		return err
	}
	_, err := rc.readResponse()
	return err
}

// Subscribe subscribes to a Redis channel
func (rc *RedisClient) Subscribe(channel string) error {
	// Create a separate connection for pub/sub if not already created
	if rc.subConn == nil {
		conn, err := net.Dial("unix", rc.socketPath)
		if err != nil {
			return fmt.Errorf("failed to connect to redis for pub/sub: %w", err)
		}
		rc.subConn = conn
		rc.subReader = bufio.NewReader(conn)
	}

	// Send SUBSCRIBE command on the pub/sub connection
	cmd := fmt.Sprintf("*2\r\n$9\r\nSUBSCRIBE\r\n$%d\r\n%s\r\n", len(channel), channel)
	if _, err := rc.subConn.Write([]byte(cmd)); err != nil {
		return err
	}

	// Read subscription confirmation (array with 3 elements)
	// Format: *3\r\n$9\r\nsubscribe\r\n$<len>\r\n<channel>\r\n:<num>\r\n
	firstByte, err := rc.subReader.ReadByte()
	if err != nil {
		return err
	}
	if firstByte != '*' {
		// If it's a newline or other whitespace, try reading again
		if firstByte == '\r' || firstByte == '\n' {
			rc.logger.Printf("Skipping whitespace before subscribe response")
			firstByte, err = rc.subReader.ReadByte()
			if err != nil {
				return err
			}
		}
		if firstByte != '*' {
			return fmt.Errorf("expected array for subscribe response, got: %q (byte: %d)", firstByte, firstByte)
		}
	}

	// Read array size
	_, err = rc.subReader.ReadString('\n')
	if err != nil {
		return err
	}

	// Read the three parts of the subscribe confirmation
	// We need to read from subReader instead of using readResponse (which uses the main reader)
	for i := 0; i < 3; i++ {
		// Read bulk string or integer
		respType, err := rc.subReader.ReadByte()
		if err != nil {
			rc.logger.Printf("Warning reading subscribe confirmation part %d: %v", i, err)
			continue
		}

		if respType == '$' {
			// Bulk string - read size and content
			sizeLine, _ := rc.subReader.ReadString('\n')
			sizeStr := strings.TrimRight(sizeLine, "\r\n")
			size, _ := strconv.Atoi(sizeStr)
			if size > 0 {
				data := make([]byte, size)
				rc.subReader.Read(data)
				rc.subReader.ReadByte() // \r
				rc.subReader.ReadByte() // \n
			}
		} else if respType == ':' {
			// Integer
			rc.subReader.ReadString('\n')
		}
	}

	rc.logger.Printf("Subscribed to channel: %s", channel)
	return nil
}

// GetMessage gets a message from subscribed channels with timeout
func (rc *RedisClient) GetMessage(ctx context.Context, timeout time.Duration) (string, error) {
	if rc.subConn == nil {
		return "", fmt.Errorf("not subscribed to any channel")
	}

	// Set read deadline
	rc.subConn.SetReadDeadline(time.Now().Add(timeout))
	defer rc.subConn.SetReadDeadline(time.Time{}) // Clear deadline

	select {
	case <-ctx.Done():
		return "", ctx.Err()
	default:
	}

	// Try to read message array indicator
	firstByte, err := rc.subReader.ReadByte()
	if err != nil {
		if netErr, ok := err.(net.Error); ok && netErr.Timeout() {
			return "", nil // Timeout is not an error, just no message
		}
		return "", err
	}

	if firstByte != '*' {
		return "", fmt.Errorf("expected array for message, got: %c", firstByte)
	}

	// Read array size
	line, err := rc.subReader.ReadString('\n')
	if err != nil {
		return "", err
	}
	sizeStr := strings.TrimRight(line, "\r\n")
	size, err := strconv.Atoi(sizeStr)
	if err != nil {
		return "", fmt.Errorf("invalid array size: %w", err)
	}

	if size != 3 {
		// Not a message response, skip it
		for i := 0; i < size; i++ {
			rc.readResponse()
		}
		return "", nil
	}

	// Read message type (should be "message")
	messageType, err := rc.readResponseFromSub()
	if err != nil {
		return "", err
	}
	if messageType != "message" {
		// Not a message, skip remaining parts
		rc.readResponseFromSub()
		rc.readResponseFromSub()
		return "", nil
	}

	// Read channel name (we don't need it)
	_, err = rc.readResponseFromSub()
	if err != nil {
		return "", err
	}

	// Read actual message content
	content, err := rc.readResponseFromSub()
	if err != nil {
		return "", err
	}

	return content, nil
}
