/**
 * Shared state machine for Franklin Lap Counter web applications.
 * Consolidates snapshot data and countdown events into a single logical UI state.
 */
// biome-ignore lint/correctness/noUnusedVariables: used via <script> include from HTML pages
class FranklinRaceStateMachine {
	constructor(options = {}) {
		this.onStateChange = options.onStateChange || (() => {});
		this.onSoundTrigger = options.onSoundTrigger || (() => {});

		this.reset();
	}

	reset() {
		this.state = "not_started";
		this.snapshot = null;
		this.countdownActive = false;
		this.currentPhase = null;
		this.countdownReadyAtEpoch = null;
		this.countdownReadyAtMonotonic = null;
		this.timers = [];
		this.lastAppliedSnapshotSeq = -1;
	}

	/**
	 * Monotonic ordering of start phases to prevent UI regressions.
	 */
	static PHASE_ORDER = {
		ready1: 1,
		ready2: 2,
		set: 3,
		go: 4,
		running: 5,
		winner_declared: 5,
		paused: 5,
		finished: 5,
	};

	/**
	 * Handle an authoritative race snapshot.
	 */
	handleSnapshot(snapshot) {
		if (!snapshot) return;

		// Skip stale snapshots
		if (
			snapshot.snapshot_seq !== undefined &&
			snapshot.snapshot_seq <= this.lastAppliedSnapshotSeq
		) {
			return;
		}
		this.lastAppliedSnapshotSeq = snapshot.snapshot_seq;

		const previousState = this.state;
		const previousSnapshotState = this.snapshot ? this.snapshot.state : null;
		this.snapshot = snapshot;

		// If a countdown is active, we only let the snapshot override if it's
		// a "going" state (running/winner_declared) or if the race was reset/finished.
		const snapshotState = snapshot.state || "not_started";

		if (this.countdownActive) {
			if (snapshotState === "running" || snapshotState === "winner_declared") {
				// Snapshot confirms race started; we can stand down the countdown preview.
				this.stopCountdown();
				this.state = snapshotState;
			} else if (snapshotState === "not_started") {
				// Keep the countdown running; it's authoritative for the UI until 'go'.
			} else {
				// Aborted or finished mid-countdown
				this.stopCountdown();
				this.state = snapshotState;
			}
		} else {
			this.state = snapshotState;
		}

		// Sound triggers for transitions
		if (snapshotState === "finished" && previousSnapshotState !== "finished") {
			this.onSoundTrigger("finish");
		}

		if (this.state !== previousState) {
			this.onStateChange(this.state);
		}
	}

	/**
	 * Handle a countdown_phase event.
	 */
	handleCountdownPhase(msg) {
		const phase = (msg.phase || "").toLowerCase();
		const atEpoch = msg.at;

		// Clobber protection: if race is already going, ignore countdown events.
		if (
			this.snapshot &&
			(this.snapshot.state === "running" ||
				this.snapshot.state === "winner_declared")
		) {
			return;
		}

		// Monotonic check
		const newLevel = FranklinRaceStateMachine.PHASE_ORDER[phase] || 0;
		const currentLevel =
			FranklinRaceStateMachine.PHASE_ORDER[this.currentPhase] || 0;
		if (newLevel < currentLevel && this.countdownActive) {
			return;
		}

		const now = Date.now();
		let delayMs = 0;

		// Anchor-based scheduling (mirroring GUI/TUI for sync)
		if (phase === "ready1" || phase === "ready") {
			this.countdownReadyAtEpoch = atEpoch;
			this.countdownReadyAtMonotonic = now;
			delayMs = 0;
			this.countdownActive = true;
		} else if (this.countdownReadyAtEpoch && this.countdownReadyAtMonotonic) {
			const totalMs = (atEpoch - this.countdownReadyAtEpoch) * 1000;
			const elapsedMs = now - this.countdownReadyAtMonotonic;
			delayMs = Math.max(0, totalMs - elapsedMs);
		} else {
			delayMs = Math.max(0, atEpoch * 1000 - now);
		}

		const timer = setTimeout(() => {
			// Re-verify monotonic level inside timeout
			const applyLevel = FranklinRaceStateMachine.PHASE_ORDER[phase] || 0;
			const currentApplyLevel =
				FranklinRaceStateMachine.PHASE_ORDER[this.currentPhase] || 0;
			if (applyLevel < currentApplyLevel && this.countdownActive) return;

			this.countdownActive = true;
			this.currentPhase = phase;
			this.state = phase === "ready" ? "ready1" : phase;

			// Audio triggers
			if (phase === "ready" || phase === "ready1")
				this.onSoundTrigger("ready1");
			else if (phase === "ready2") this.onSoundTrigger("ready2");
			else if (phase === "set") this.onSoundTrigger("set");
			else if (phase === "go") {
				this.onSoundTrigger("go");
				// Go is special; it stays active until the next snapshot confirms "running"
			}

			this.onStateChange(this.state);
		}, delayMs);

		this.timers.push(timer);
	}

	stopCountdown() {
		for (const t of this.timers) clearTimeout(t);
		this.timers = [];
		this.countdownActive = false;
		this.currentPhase = null;
		this.countdownReadyAtEpoch = null;
		this.countdownReadyAtMonotonic = null;
	}

	/**
	 * Returns the 4-light pattern for the current state.
	 * Classes: "red", "yellow", "green", "off"
	 */
	getLightPattern() {
		switch (this.state) {
			case "ready1":
				return ["red", "off", "off", "off"];
			case "ready2":
				return ["red", "red", "off", "off"];
			case "set":
				return ["red", "red", "yellow", "off"];
			case "go":
			case "running":
			case "winner_declared":
				return ["red", "red", "yellow", "green"];
			case "paused":
				return ["yellow", "yellow", "yellow", "yellow"];
			case "not_started":
				return ["red", "red", "red", "red"];
			default:
				return ["off", "off", "off", "off"];
		}
	}

	getStatusText() {
		switch (this.state) {
			case "ready1":
			case "ready2":
				return "Ready!";
			case "set":
				return "Set...";
			case "go":
				return "Go!";
			case "running":
				return "Race Running";
			case "paused":
				return "Race Paused";
			case "winner_declared":
				return "Winner Declared!";
			case "finished":
				return "Race Finished";
			case "not_started":
				return "Ready to start";
			default:
				return "Waiting";
		}
	}

	getButtonStates() {
		const snapshotState = this.snapshot ? this.snapshot.state : "not_started";
		const isNotStarted = snapshotState === "not_started";
		const isFinished = snapshotState === "finished";
		const isRunning =
			snapshotState === "running" ||
			snapshotState === "winner_declared" ||
			snapshotState === "paused";
		const inProgress = !isNotStarted && !isFinished;

		return {
			canStart: !isRunning,
			canEnd: inProgress,
			canReset: !isNotStarted,
			canPause: inProgress && snapshotState !== "paused",
			canResume: snapshotState === "paused",
			canManage: inProgress,
		};
	}
}
