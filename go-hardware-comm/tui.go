package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log"
	"os"
	"strings"
	"time"

	tea "github.com/charmbracelet/bubbletea"
	"github.com/charmbracelet/lipgloss"
)

// Styles
var (
	headerStyle = lipgloss.NewStyle().
			Bold(true).
			Foreground(lipgloss.Color("15")).
			Background(lipgloss.Color("62")).
			Padding(0, 1)

	instructionStyle = lipgloss.NewStyle().
				Foreground(lipgloss.Color("241"))

	separatorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("238"))

	statusStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("86"))

	errorStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("196")).
			Bold(true)

	lapStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("46")).
			Bold(true)

	heartbeatStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("201"))

	debugStyle = lipgloss.NewStyle().
			Foreground(lipgloss.Color("243"))
)

// TUI messages
type hardwareMsg struct {
	data Message
}

type tickMsg time.Time

type errMsg struct {
	err error
}

// TUI Model
type model struct {
	redis          *RedisClient
	simulationMode bool
	messages       []string
	messageCount   int
	width          int
	height         int
	hardwareError  bool
	errorMessage   string
	raceStartTime  time.Time
	quitting       bool
}

func initialModel(simulationMode bool) model {
	return model{
		simulationMode: simulationMode,
		messages:       make([]string, 0),
		messageCount:   0,
	}
}

func (m model) Init() tea.Cmd {
	return tea.Batch(
		listenForMessages(m.redis),
		tickCmd(),
	)
}

func (m model) Update(msg tea.Msg) (tea.Model, tea.Cmd) {
	switch msg := msg.(type) {
	case tea.KeyMsg:
		switch msg.String() {
		case "q", "Q", "ctrl+c":
			m.quitting = true
			return m, tea.Quit

		case "s", "S":
			if m.simulationMode {
				return m, sendCommand(m.redis, Command{
					Type:    "command",
					Command: "start_race",
				})
			}

		case "p", "P":
			if m.simulationMode {
				return m, sendCommand(m.redis, Command{
					Type:    "command",
					Command: "stop_race",
				})
			}

		case "1", "2", "3", "4":
			if m.simulationMode {
				racerID := int(msg.String()[0] - '0')
				raceTime := 0.0
				if !m.raceStartTime.IsZero() {
					raceTime = time.Since(m.raceStartTime).Seconds()
				}
				return m, sendCommand(m.redis, Command{
					Type:     "command",
					Command:  "simulate_lap",
					RacerID:  racerID,
					SensorID: 1,
					RaceTime: raceTime,
				})
			}
		}

	case tea.WindowSizeMsg:
		m.width = msg.Width
		m.height = msg.Height

	case hardwareMsg:
		m.messageCount++
		m.messages = append(m.messages, formatMessage(msg.data))

		// Keep only last 100 messages to avoid memory issues
		if len(m.messages) > 100 {
			m.messages = m.messages[len(m.messages)-100:]
		}

		// Check for hardware error
		if msg.data.Type == "hardware_error" && !m.hardwareError {
			m.hardwareError = true
			m.errorMessage = msg.data.Message
		}

		return m, listenForMessages(m.redis)

	case tickMsg:
		return m, tickCmd()

	case errMsg:
		// Handle error
		return m, nil
	}

	return m, nil
}

func (m model) View() string {
	if m.quitting {
		return ""
	}

	var b strings.Builder

	// Header
	modeText := "HARDWARE MODE"
	if m.simulationMode {
		modeText = "SIMULATION MODE"
	}
	b.WriteString(headerStyle.Render(fmt.Sprintf("=== Hardware Comm Redis - %s ===", modeText)))
	b.WriteString("\n")

	// Instructions
	if m.hardwareError {
		b.WriteString(errorStyle.Render("ERROR: " + m.errorMessage))
		b.WriteString("\n")
		b.WriteString(instructionStyle.Render("Please connect the lap tracking hardware and restart this program."))
		b.WriteString("\n")
		b.WriteString(instructionStyle.Render("Press [Q] to quit"))
		b.WriteString("\n")
	} else if m.simulationMode {
		b.WriteString(instructionStyle.Render("Keys: [S]tart race | Sto[P] race | [1-4] Simulate lap for racer | [Q]uit"))
		b.WriteString("\n")
	} else {
		b.WriteString(instructionStyle.Render("Keys: [Q]uit"))
		b.WriteString("\n")
	}

	// Separator
	if m.width > 0 {
		b.WriteString(separatorStyle.Render(strings.Repeat("─", m.width)))
	} else {
		b.WriteString(separatorStyle.Render(strings.Repeat("─", 80)))
	}
	b.WriteString("\n")

	// Messages
	if !m.hardwareError {
		// Calculate how many lines we can show
		headerLines := 4 // header + instructions + separator + status
		availableLines := m.height - headerLines
		if availableLines < 1 {
			availableLines = 20 // default
		}

		// Show most recent messages
		startIdx := 0
		if len(m.messages) > availableLines {
			startIdx = len(m.messages) - availableLines
		}

		for i := startIdx; i < len(m.messages); i++ {
			b.WriteString(fmt.Sprintf("%4d | %s\n", i+1, m.messages[i]))
		}
	}

	// Status bar
	b.WriteString("\n")
	b.WriteString(statusStyle.Render(fmt.Sprintf("Messages received: %d", m.messageCount)))

	return b.String()
}

// Commands

func listenForMessages(redis *RedisClient) tea.Cmd {
	return func() tea.Msg {
		// This will be called repeatedly
		// We need to read from Redis in a non-blocking way
		// For now, return immediately and let tickCmd handle polling
		return nil
	}
}

func tickCmd() tea.Cmd {
	return tea.Tick(100*time.Millisecond, func(t time.Time) tea.Msg {
		return tickMsg(t)
	})
}

func sendCommand(redis *RedisClient, cmd Command) tea.Cmd {
	return func() tea.Msg {
		data, err := json.Marshal(cmd)
		if err != nil {
			return errMsg{err}
		}

		err = redis.Publish(RedisInChannel, string(data))
		if err != nil {
			return errMsg{err}
		}

		return nil
	}
}

// Helpers

func formatMessage(msg Message) string {
	switch msg.Type {
	case "lap":
		return lapStyle.Render(fmt.Sprintf("[LAP] Racer %d - Sensor %d - Time: %.3fs",
			msg.RacerID, msg.SensorID, msg.RaceTime))

	case "heartbeat":
		return heartbeatStyle.Render("[HEARTBEAT] ♥")

	case "status":
		return statusStyle.Render(fmt.Sprintf("[STATUS] %s", msg.Message))

	case "error", "hardware_error":
		return errorStyle.Render(fmt.Sprintf("[ERROR] %s", msg.Message))

	case "debug":
		return debugStyle.Render(fmt.Sprintf("[DEBUG] %s", msg.Message))

	case "new_msg":
		return fmt.Sprintf("[NEW_MSG] Sensor %d - Time: %.3fs - Flags: %s, %s",
			msg.SensorID, msg.RawTime, msg.Flag1, msg.Flag2)

	case "raw":
		return debugStyle.Render(fmt.Sprintf("[RAW] %s", msg.Line))

	default:
		return fmt.Sprintf("[%s] %v", strings.ToUpper(msg.Type), msg)
	}
}

// RunTUI starts the TUI application
func RunTUI(simulationMode bool) error {
	// Create a logger for Redis client
	logger := log.New(os.Stderr, "[TUI-Redis] ", log.LstdFlags)

	// Create Redis client for TUI
	redis, err := NewRedisClient(RedisSocketPath, logger)
	if err != nil {
		return fmt.Errorf("failed to create redis client for TUI: %w", err)
	}
	defer redis.Close()

	// Subscribe to output channel
	if err := redis.Subscribe(RedisOutChannel); err != nil {
		return fmt.Errorf("failed to subscribe to redis: %w", err)
	}

	// Create model
	m := initialModel(simulationMode)
	m.redis = redis

	// Create program
	p := tea.NewProgram(m, tea.WithAltScreen())

	// Start message receiver in background
	ctx := context.Background()
	go func() {
		logger.Println("TUI message receiver started, listening for messages...")
		for {
			msg, err := redis.GetMessage(ctx, 100*time.Millisecond)
			if err != nil {
				continue
			}
			if msg == "" {
				continue
			}

			logger.Printf("TUI received message: %s", msg)

			var hwMsg Message
			if err := json.Unmarshal([]byte(msg), &hwMsg); err != nil {
				logger.Printf("TUI failed to unmarshal message: %v", err)
				continue
			}

			p.Send(hardwareMsg{data: hwMsg})
		}
	}()

	_, err = p.Run()
	return err
}
