#!/usr/bin/env python3
"""
Servo control for droid pan/tilt head.
Uses PCA9685 over I2C if available, falls back to software PWM on GPIO.
"""

import time
import threading

# Try to import PCA9685 driver (preferred)
try:
    from adafruit_servokit import ServoKit
    HAS_PCA9685 = True
except ImportError:
    HAS_PCA9685 = False

# Fallback: direct GPIO PWM
try:
    import RPi.GPIO as GPIO
    HAS_GPIO = True
except ImportError:
    HAS_GPIO = False

# Servo channels / pins
PAN_CHANNEL = 0    # PCA9685 channel for pan (left/right)
TILT_CHANNEL = 1   # PCA9685 channel for tilt (up/down)
PAN_GPIO = 12      # GPIO pin fallback for pan
TILT_GPIO = 13     # GPIO pin fallback for tilt

# Angle limits
PAN_MIN = 0
PAN_MAX = 180
PAN_CENTER = 90
TILT_MIN = 0       # Level / slightly down
TILT_MAX = 45      # Looking up
TILT_CENTER = 10   # Slightly above level (natural resting gaze)

# Smoothing — degrees per step
STEP_SIZE = 2
STEP_DELAY = 0.015  # 15ms between steps = smooth motion


class ServoController:
    def __init__(self):
        self.pan = PAN_CENTER
        self.tilt = TILT_CENTER
        self.target_pan = PAN_CENTER
        self.target_tilt = TILT_CENTER
        self.lock = threading.Lock()
        self.last_move_time = 0  # For vision frame diff suppression
        self.enabled = False
        self.kit = None
        self.pan_pwm = None
        self.tilt_pwm = None

        if HAS_PCA9685:
            try:
                self.kit = ServoKit(channels=16)
                self.kit.servo[TILT_CHANNEL].set_pulse_width_range(400, 2500)
                # Center briefly then release — avoid sustained jitter on boot
                self.kit.servo[PAN_CHANNEL].angle = PAN_CENTER
                self.kit.servo[TILT_CHANNEL].angle = TILT_CENTER
                time.sleep(0.5)  # Let servos reach position
                self.kit._pca.channels[PAN_CHANNEL].duty_cycle = 0
                self.kit._pca.channels[TILT_CHANNEL].duty_cycle = 0
                self.enabled = True
                print("[Servo] PCA9685 initialized — pan/tilt ready (released)")
            except Exception as e:
                print(f"[Servo] PCA9685 failed: {e}")

        if not self.enabled and HAS_GPIO:
            try:
                GPIO.setmode(GPIO.BCM)
                GPIO.setwarnings(False)
                GPIO.setup(PAN_GPIO, GPIO.OUT)
                GPIO.setup(TILT_GPIO, GPIO.OUT)
                self.pan_pwm = GPIO.PWM(PAN_GPIO, 50)  # 50Hz
                self.tilt_pwm = GPIO.PWM(TILT_GPIO, 50)
                self.pan_pwm.start(self._angle_to_duty(PAN_CENTER))
                self.tilt_pwm.start(self._angle_to_duty(TILT_CENTER))
                self.enabled = True
                print("[Servo] GPIO PWM initialized — pan/tilt ready")
            except Exception as e:
                print(f"[Servo] GPIO PWM failed: {e}")

        if not self.enabled:
            print("[Servo] No servo hardware detected — head tracking disabled")

        # Start smooth movement thread
        if self.enabled:
            self._thread = threading.Thread(target=self._smooth_loop, daemon=True)
            self._thread.start()

    def _angle_to_duty(self, angle):
        """Convert angle (0-180) to duty cycle for SG90."""
        return 2.5 + (angle / 180.0) * 10.0

    def _set_angle(self, channel, angle):
        """Set a servo to a specific angle."""
        if self.kit:
            self.kit.servo[channel].angle = angle
        elif channel == 0 and self.pan_pwm:
            self.pan_pwm.ChangeDutyCycle(self._angle_to_duty(angle))
        elif channel == 1 and self.tilt_pwm:
            self.tilt_pwm.ChangeDutyCycle(self._angle_to_duty(angle))

    def _release_servo(self, channel):
        """Stop PWM signal to servo — prevents jitter when idle."""
        try:
            if self.kit:
                # Set raw duty cycle to 0 — completely kills the PWM signal
                self.kit._pca.channels[channel].duty_cycle = 0
        except Exception:
            pass

    def _smooth_loop(self):
        """Continuously move servos toward target angles."""
        idle_since = time.time()
        self._released = True  # Start released (init already centered + released)
        while True:
            moved = False
            with self.lock:
                pan_diff = abs(self.pan - self.target_pan)
                tilt_diff = abs(self.tilt - self.target_tilt)

                if pan_diff > 1.0 or tilt_diff > 1.0:
                    # Re-engage if released
                    if self._released:
                        self._released = False

                    # Pan
                    if pan_diff > 0.5:
                        if self.pan < self.target_pan:
                            self.pan = min(self.pan + STEP_SIZE, self.target_pan)
                        else:
                            self.pan = max(self.pan - STEP_SIZE, self.target_pan)
                        self._set_angle(PAN_CHANNEL, self.pan)

                    # Tilt
                    if tilt_diff > 0.5:
                        if self.tilt < self.target_tilt:
                            self.tilt = min(self.tilt + STEP_SIZE, self.target_tilt)
                        else:
                            self.tilt = max(self.tilt - STEP_SIZE, self.target_tilt)
                        self._set_angle(TILT_CHANNEL, self.tilt)

                    moved = True
                    idle_since = time.time()

            if moved:
                time.sleep(STEP_DELAY)
            else:
                # Release servos after 1s idle — stops jitter/buzzing
                if not self._released and time.time() - idle_since > 1.0:
                    self._release_servo(PAN_CHANNEL)
                    self._release_servo(TILT_CHANNEL)
                    self._released = True
                time.sleep(0.1)

    def look_at(self, pan, tilt):
        """Set target pan/tilt angles. Movement is smoothed."""
        with self.lock:
            new_pan = max(PAN_MIN, min(PAN_MAX, pan))
            new_tilt = max(TILT_MIN, min(TILT_MAX, tilt))
            # Track last movement time for vision frame diff suppression
            if abs(new_pan - self.pan) > 2 or abs(new_tilt - self.tilt) > 2:
                self.last_move_time = time.time()
            self.target_pan = new_pan
            self.target_tilt = new_tilt

    def center(self):
        """Return to center position."""
        self.look_at(PAN_CENTER, TILT_CENTER)

    def track_face(self, face_x, face_y, frame_width, frame_height):
        """
        Track a face by adjusting pan/tilt based on face position in frame.
        face_x, face_y = center of face bounding box
        frame_width, frame_height = camera resolution
        """
        if not self.enabled:
            return

        # Calculate offset from center (normalized -1 to 1)
        offset_x = (face_x - frame_width / 2) / (frame_width / 2)
        offset_y = (face_y - frame_height / 2) / (frame_height / 2)

        # Dead zone — don't move for small offsets
        if abs(offset_x) < 0.15 and abs(offset_y) < 0.15:
            return

        # Target angles based on face position
        pan_target = self.pan + (offset_x) * 25
        tilt_target = self.tilt + (-offset_y * 20)

        # Smooth interpolation — move 35% toward target each tick
        smooth = 0.35
        new_pan = self.pan + (pan_target - self.pan) * smooth
        new_tilt = self.tilt + (tilt_target - self.tilt) * smooth

        # Only send to servos if movement is significant (>2°)
        # Prevents constant micro-adjustments that cause jitter
        if abs(new_pan - self.pan) > 2.0 or abs(new_tilt - self.tilt) > 2.0:
            self.look_at(new_pan, new_tilt)

    def idle_glance(self):
        """Random subtle movement for idle behavior."""
        import random
        pan_offset = random.uniform(-15, 15)
        tilt_offset = random.uniform(-5, 5)
        self.look_at(PAN_CENTER + pan_offset, TILT_CENTER + tilt_offset)

    def close(self):
        """Clean up servo resources."""
        if self.kit:
            try:
                self.kit.servo[PAN_CHANNEL].angle = PAN_CENTER
                self.kit.servo[TILT_CHANNEL].angle = TILT_CENTER
            except:
                pass
        if self.pan_pwm:
            self.pan_pwm.stop()
        if self.tilt_pwm:
            self.tilt_pwm.stop()
        if HAS_GPIO and self.enabled:
            try:
                GPIO.cleanup()
            except:
                pass

    # === Emote Animations ===

    def nod(self, times=2, speed=0.15):
        """Nod yes — tilt up/down."""
        def _anim():
            for _ in range(times):
                self.look_at(self.pan, self.tilt - 25)
                time.sleep(speed)
                self.look_at(self.pan, self.tilt + 25)
                time.sleep(speed)
            self.look_at(self.pan, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def shake(self, times=2, speed=0.15):
        """Shake no — pan left/right."""
        def _anim():
            for _ in range(times):
                self.look_at(self.pan - 35, self.tilt)
                time.sleep(speed)
                self.look_at(self.pan + 35, self.tilt)
                time.sleep(speed)
            self.look_at(PAN_CENTER, self.tilt)
        threading.Thread(target=_anim, daemon=True).start()

    def tilt_curious(self):
        """Curious — pan offset + tilt, like 'huh?'"""
        def _anim():
            self.look_at(self.pan + 25, TILT_CENTER + 20)
            time.sleep(1.2)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def look_up(self):
        """Look up — thinking/pondering."""
        def _anim():
            self.look_at(self.pan - 15, TILT_CENTER + 35)
            time.sleep(1.5)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def look_down(self):
        """Look down — shy/sad/thoughtful."""
        def _anim():
            self.look_at(self.pan, TILT_CENTER - 20)
            time.sleep(1.0)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def perk_up(self):
        """Quick snap up then settle — excited/alert/surprised."""
        def _anim():
            self.look_at(self.pan, TILT_CENTER + 35)
            time.sleep(0.2)
            self.look_at(self.pan, TILT_CENTER + 10)
            time.sleep(0.5)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def scan(self):
        """Slow pan left to right — scanning/looking around."""
        def _anim():
            self.look_at(PAN_CENTER - 45, self.tilt + 10)
            time.sleep(0.6)
            self.look_at(PAN_CENTER, self.tilt - 10)
            time.sleep(0.4)
            self.look_at(PAN_CENTER + 45, self.tilt + 10)
            time.sleep(0.6)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def laugh_bounce(self):
        """Quick bounces — amused."""
        def _anim():
            for _ in range(4):
                self.look_at(self.pan, self.tilt + 20)
                time.sleep(0.1)
                self.look_at(self.pan, self.tilt - 10)
                time.sleep(0.1)
            self.look_at(PAN_CENTER, TILT_CENTER)
        threading.Thread(target=_anim, daemon=True).start()

    def emote(self, name):
        """Run a named emote animation."""
        emotes = {
            'nod': self.nod,
            'shake': self.shake,
            'curious': self.tilt_curious,
            'think': self.look_up,
            'shy': self.look_down,
            'sad': self.look_down,
            'excited': self.perk_up,
            'alert': self.perk_up,
            'scan': self.scan,
            'playful': self.tilt_curious,
            'laugh': self.laugh_bounce,
            'agree': self.nod,
            'disagree': self.shake,
            'surprised': self.perk_up,
        }
        fn = emotes.get(name)
        if fn:
            fn()
