"""Mutable runtime state shared across modules.

Always import the module (`from droid_client import state`) and access
attributes (`state.is_speaking = True`). Do NOT use `from state import x`
— that creates a local copy and assignments won't propagate.
"""
import time

running = True
is_speaking = False
sleep_state = 'awake'  # 'awake' | 'sleeping'
boot_time = time.time()  # don't auto-sleep for first 120s after boot
last_motion_time = time.time()
noise_start_time = None
prev_frame_gray = None
