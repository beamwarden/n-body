/**
 * @module alertsound
 * @description Web Audio API alarm tone for anomaly alerting.
 *
 * Exports:
 *   initAlertSound()         — call once on app init; creates AudioContext and
 *                              registers the autoplay-unlock click listener.
 *   triggerAlertSound()      — play the 3-beep rising alarm (debounced, 2s cooldown).
 *   setAlertSoundMuted(bool) — set mute state; muted = true silences triggerAlertSound.
 *
 * Autoplay policy: AudioContext is created in suspended state and resumed on the
 * first user click anywhere on document.body. In the demo flow the presenter always
 * clicks the globe before injecting a maneuver, so the context is unlocked in time.
 * If the alarm fires before any click, the tone is silently skipped — the visual
 * flash still fires, so the anomaly is not missed.
 */

/** @type {AudioContext|null} */
let _audioCtx = null;

/** @type {boolean} */
let _muted = false;

/** @type {number} Timestamp (ms) of the last alarm trigger, for debounce. */
let _lastTriggerTime_ms = 0;

/** @type {number} Minimum ms between alarm triggers. */
const _DEBOUNCE_MS = 2000;

/**
 * Initialize the audio alert system.
 *
 * Creates an AudioContext in suspended state and registers a one-time click
 * listener on document.body that resumes it. Must be called once during app
 * startup (e.g., from initApp()).
 *
 * @returns {void}
 */
export function initAlertSound() {
    try {
        _audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    } catch (err) {
        console.warn('[alertsound] AudioContext not available:', err);
        return;
    }

    // One-time click listener to satisfy browser autoplay policy.
    const _unlockAudio = () => {
        if (_audioCtx && _audioCtx.state === 'suspended') {
            _audioCtx.resume().catch((err) => {
                console.warn('[alertsound] AudioContext resume failed:', err);
            });
        }
        document.body.removeEventListener('click', _unlockAudio);
    };
    document.body.addEventListener('click', _unlockAudio);
}

/**
 * Set mute state for the alarm tone.
 *
 * When muted, triggerAlertSound() is a no-op. The visual flash is unaffected.
 *
 * @param {boolean} muted - true to mute, false to unmute.
 * @returns {void}
 */
export function setAlertSoundMuted(muted) {
    _muted = Boolean(muted);
}

/**
 * Trigger the 3-beep rising alarm tone.
 *
 * Plays three square-wave beeps at 660 Hz, 880 Hz, 1100 Hz. Each beep is
 * 120ms on with 60ms silence between beeps. Gain is 0.25 with an
 * exponentialRampToValueAtTime fade-out per beep for a clean release.
 *
 * Silently skips if:
 *   - Audio is muted
 *   - AudioContext was never created (initAlertSound not called)
 *   - AudioContext is still suspended (user has not clicked page yet)
 *   - Less than 2 seconds have passed since the last trigger (debounce)
 *
 * @returns {void}
 */
export function triggerAlertSound() {
    if (_muted) return;
    if (!_audioCtx) return;
    if (_audioCtx.state === 'suspended') return;

    const now_ms = Date.now();
    if (now_ms - _lastTriggerTime_ms < _DEBOUNCE_MS) return;
    _lastTriggerTime_ms = now_ms;

    const ctx = _audioCtx;
    const t0 = ctx.currentTime;

    // Beep parameters
    const FREQUENCIES_HZ = [660, 880, 1100];
    const BEEP_DURATION_S = 0.120;
    const GAP_DURATION_S = 0.060;
    const GAIN_PEAK = 0.25;
    // Tiny floor so exponentialRamp does not hit zero (which is invalid).
    const GAIN_FLOOR = 0.0001;

    FREQUENCIES_HZ.forEach((freq_hz, i) => {
        const beepStart_s = t0 + i * (BEEP_DURATION_S + GAP_DURATION_S);
        const beepEnd_s = beepStart_s + BEEP_DURATION_S;

        const osc = ctx.createOscillator();
        const gain = ctx.createGain();

        osc.type = 'square';
        osc.frequency.setValueAtTime(freq_hz, beepStart_s);

        // Gain envelope: ramp up at start, hold, ramp down at end.
        gain.gain.setValueAtTime(GAIN_FLOOR, beepStart_s);
        gain.gain.linearRampToValueAtTime(GAIN_PEAK, beepStart_s + 0.010);
        gain.gain.setValueAtTime(GAIN_PEAK, beepEnd_s - 0.030);
        gain.gain.exponentialRampToValueAtTime(GAIN_FLOOR, beepEnd_s);

        osc.connect(gain);
        gain.connect(ctx.destination);

        osc.start(beepStart_s);
        osc.stop(beepEnd_s);

        // Disconnect after the node finishes to allow GC.
        osc.onended = () => {
            try {
                osc.disconnect();
                gain.disconnect();
            } catch (_) {
                // Already disconnected — ignore.
            }
        };
    });
}
