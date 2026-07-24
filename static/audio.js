// biome-ignore lint/correctness/noUnusedVariables: used via <script> include from HTML pages
const FranklinAudio = (() => {
	let audioCtx = null;
	let bitcrusherNode = null;
	let bitcrusherGainNode = null;
	let pulseWave = null;
	let soundPatch = null;

	let soundEnabled = true;
	let soundStyle = localStorage.getItem("franklin_sound_style") || "classic";

	async function loadPatch() {
		try {
			const res = await fetch("/static/sounds.json", { cache: "no-cache" });
			if (res.ok) soundPatch = await res.json();
		} catch (_) {
			soundPatch = null;
		}
	}

	function initBitcrusher() {
		if (!audioCtx) return;
		if (bitcrusherNode) return;
		const shaper = audioCtx.createWaveShaper();
		const curve = new Float32Array(2048);
		for (let i = 0; i < 2048; i++) {
			const x = (i / 2047) * 2 - 1;
			curve[i] = Math.round(x * 8) / 8;
		}
		shaper.curve = curve;
		shaper.oversample = "2x";
		bitcrusherNode = shaper;
		bitcrusherGainNode = audioCtx.createGain();
		bitcrusherGainNode.gain.value = 3.0;
		bitcrusherNode.connect(bitcrusherGainNode);
		bitcrusherGainNode.connect(audioCtx.destination);
	}

	function initPulseWave() {
		if (!audioCtx) return;
		if (pulseWave) return;
		const real = new Float32Array([0, 0, 0, 0.5, 0.5, 0, 0, 0]);
		const imag = new Float32Array([0, 0, 0, 0, 0, 0, 0, 0]);
		pulseWave = audioCtx.createPeriodicWave(real, imag);
	}

	function beep(freq, durationMs) {
		if (!soundEnabled) return;
		try {
			if (!audioCtx)
				audioCtx = new (window.AudioContext || window.webkitAudioContext)();
			if (audioCtx.state === "suspended") {
				audioCtx.resume().catch(() => {});
				return;
			}
			const t = audioCtx.currentTime;
			const osc = audioCtx.createOscillator();
			const gain = audioCtx.createGain();
			osc.type = "sine";
			osc.frequency.value = freq;
			gain.gain.setValueAtTime(0.4, t);
			gain.gain.exponentialRampToValueAtTime(0.001, t + durationMs / 1000);
			osc.connect(gain);
			gain.connect(audioCtx.destination);
			osc.start(t);
			osc.stop(t + durationMs / 1000);
		} catch (_) {}
	}

	function playVoices(soundDef) {
		if (!soundEnabled || !audioCtx || audioCtx.state === "suspended") return;
		if (!soundDef) return;
		try {
			const t0 = audioCtx.currentTime;
			const bitcrush = !!soundDef.bitcrush;
			if (bitcrush) initBitcrusher();
			const destination =
				bitcrush && bitcrusherNode ? bitcrusherNode : audioCtx.destination;

			for (const v of soundDef.voices || []) {
				const start = t0 + (v.start || 0);
				const dur = v.dur || 0.2;
				const attack = v.attack || 0.01;
				const vol = v.vol != null ? v.vol : 0.3;
				const type = v.type || "sine";

				if (type === "noise") {
					const bufferSize = Math.ceil(audioCtx.sampleRate * dur);
					const buffer = audioCtx.createBuffer(
						1,
						bufferSize,
						audioCtx.sampleRate,
					);
					const data = buffer.getChannelData(0);
					for (let i = 0; i < bufferSize; i++) {
						data[i] = (Math.random() * 2 - 1) * (1 - i / bufferSize);
					}
					const source = audioCtx.createBufferSource();
					source.buffer = buffer;
					const gain = audioCtx.createGain();
					gain.gain.setValueAtTime(0, start);
					gain.gain.linearRampToValueAtTime(vol, start + attack);
					gain.gain.linearRampToValueAtTime(0, start + dur);
					source.connect(gain);
					gain.connect(destination);
					source.start(start);
					source.stop(start + dur);
					continue;
				}

				const osc = audioCtx.createOscillator();
				const gain = audioCtx.createGain();
				if (type === "pulse") {
					initPulseWave();
					osc.setPeriodicWave(pulseWave);
				} else {
					osc.type = type;
				}
				osc.frequency.setValueAtTime(v.freq || 440, start);
				if (v.freq_end) {
					osc.frequency.linearRampToValueAtTime(v.freq_end, start + dur);
				}
				gain.gain.setValueAtTime(0, start);
				gain.gain.linearRampToValueAtTime(vol, start + attack);
				gain.gain.linearRampToValueAtTime(0, start + dur);
				osc.connect(gain);
				gain.connect(destination);
				osc.start(start);
				osc.stop(start + dur);
			}
		} catch (_) {}
	}

	function dispatch(name) {
		if (!soundEnabled || !audioCtx || audioCtx.state === "suspended") return;
		try {
			const stylePatch = soundPatch?.[soundStyle];
			const soundDef = stylePatch?.[name];
			if (soundDef) {
				playVoices(soundDef);
				return;
			}
		} catch (_) {}
	}

	return {
		async init() {
			if (!audioCtx)
				audioCtx = new (window.AudioContext || window.webkitAudioContext)();
			await loadPatch();
			if (audioCtx.state === "suspended") await audioCtx.resume();
		},

		isReady() {
			return audioCtx !== null && audioCtx.state !== "suspended";
		},

		playReady1() {
			dispatch("ready1");
		},
		playReady2() {
			dispatch("ready2");
		},
		playReady() {
			dispatch("ready");
		},
		playSet() {
			dispatch("set");
		},
		playGo() {
			dispatch("go");
		},
		playFinish() {
			dispatch("finish");
		},

		beep(freq, durationMs) {
			beep(freq, durationMs);
		},

		setEnabled(enabled) {
			soundEnabled = enabled;
		},

		getEnabled() {
			return soundEnabled;
		},

		setStyle(style) {
			soundStyle = style;
			localStorage.setItem("franklin_sound_style", style);
		},

		getStyle() {
			return soundStyle;
		},
	};
})();
