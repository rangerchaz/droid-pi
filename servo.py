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
TILT_MIN = 30      # Don't look straight down
TILT_MAX = 150     # Don't look straight up
TILT_CENTER = 90

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
        self.enabled = False
        self.kit = None
        self.pan_pwm = None
        self.tilt_pwm = None

        if HAS_PCA9685:
            try:
                self.kit = ServoKit(channels=16)
                self.kit.servo[PAN_CHANNEL].angle = PAN_CENTER
                self.kit.servo[TILT_CHANNEL].angle = TILT_CENTER
                self.enabled = True
                print("[Servo] PCA9685 initialized — pan/tilt ready")
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

    def _smooth_loop(self):
        """Continuously move servos toward target angles."""
        while True:
            moved = False
            with self.lock:
                # Pan
                if abs(self.pan - self.target_pan) > 0.5:
                    if self.pan < self.target_pan:
                        self.pan = min(self.pan + STEP_SIZE, self.target_pan)
                    else:
                        self.pan = max(self.pan - STEP_SIZE, self.target_pan)
                    self._set_angle(PAN_CHANNEL, self.pan)
                    moved = True

                # Tilt
                if abs(self.tilt - self.target_tilt) > 0.5:
                    if self.tilt < self.target_tilt:
                        self.tilt = min(self.tilt + STEP_SIZE, self.target_tilt)
                    else:
                        self.tilt = max(self.tilt - STEP_SIZE, self.target_tilt)
                    self._set_angle(TILT_CHANNEL, self.tilt)
                    moved = True

            time.sleep(STEP_DELAY if moved else 0.05)

    def look_at(self, pan, tilt):
        """Set target pan/tilt angles. Movement is smoothed."""
        with self.lock:
            self.target_pan = max(PAN_MIN, min(PAN_MAX, pan))
            self.target_tilt = max(TILT_MIN, min(TILT_MAX, tilt))

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
        if abs(offset_x) < 0.1 and abs(offset_y) < 0.1:
            return

        # Adjust angles (invert X because camera is mirrored)
        pan_adjust = -offset_x * 15   # Max 15 degrees per frame
        tilt_adjust = offset_y * 10   # Max 10 degrees per frame

        new_pan = self.pan + pan_adjust
        new_tilt = self.tilt + tilt_adjust

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
