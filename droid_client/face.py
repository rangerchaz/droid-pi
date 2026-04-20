"""Face tracker (Haar cascade, runs on Pi, no API cost)."""
import os

import cv2


class FaceTracker:
    def __init__(self):
        cascade_path = None
        try:
            p = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
            if os.path.exists(p):
                cascade_path = p
        except (AttributeError, TypeError):
            pass
        if not cascade_path:
            # Local copy next to script
            p = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'haarcascade_frontalface_default.xml')
            if os.path.exists(p):
                cascade_path = p
        if not cascade_path:
            # Working directory
            p = os.path.join(os.getcwd(), 'haarcascade_frontalface_default.xml')
            if os.path.exists(p):
                cascade_path = p
        if cascade_path:
            print(f'[FaceTracker] Cascade: {cascade_path}')
        else:
            print('[FaceTracker] WARNING: No cascade file found — face tracking disabled')
        self.cascade = cv2.CascadeClassifier(cascade_path or '')
        self.last_face = None  # (cx, cy, w, h) of last detected face
        self.frames_without_face = 0
        self._enabled = True
        print("[FaceTracker] Initialized")

    def detect(self, frame):
        """Detect largest face in frame. Returns (center_x, center_y, w, h) or None."""
        if not self._enabled:
            return None
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Histogram equalization — critical for low-light face detection
            gray = cv2.equalizeHist(gray)
            # Scale down for speed on Pi 3B
            small = cv2.resize(gray, (320, 240))
            scale_x = frame.shape[1] / 320
            scale_y = frame.shape[0] / 240
            faces = self.cascade.detectMultiScale(small, 1.1, 3, minSize=(20, 20))
            if self.frames_without_face % 30 == 0 and self.frames_without_face > 0:
                print(f'[FaceTracker] No face for {self.frames_without_face} frames')
            if len(faces) > 0:
                if self.frames_without_face > 5:
                    print(f'[FaceTracker] Found face! ({len(faces)} detected)')
                # Largest face
                x, y, w, h = max(faces, key=lambda f: f[2] * f[3])
                cx = int((x + w / 2) * scale_x)
                cy = int((y + h / 2) * scale_y)
                self.last_face = (cx, cy, int(w * scale_x), int(h * scale_y))
                self.frames_without_face = 0
                return self.last_face
            else:
                self.frames_without_face += 1
                return None
        except Exception:
            return None
