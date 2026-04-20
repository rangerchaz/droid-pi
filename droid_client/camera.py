"""USB camera capture wrapper."""
import cv2

from .config import JPEG_QUALITY


class Camera:
    def __init__(self, index=0):
        self.index = index
        self.cap = None
        self.enabled = False
        self._open_retries = 0
        self._open()

    def _open(self):
        """Try to open camera. Don't crash if it fails — retry later."""
        try:
            # Auto-detect by trying configured index, then scanning
            for idx in [self.index, 0, 1, 2]:
                cap = cv2.VideoCapture(idx)
                if cap.isOpened():
                    self.cap = cap
                    if idx != self.index:
                        print(f"[Camera] Found camera at index {idx} (configured: {self.index})")
                        self.index = idx
                    break
                cap.release()
            else:
                print("[Camera] WARNING: Cannot open any camera — will retry")
                self.cap = None
                self.enabled = False
                return False
            if not self.cap.isOpened():
                print(f"[Camera] WARNING: Cannot open camera {self.index} — will retry")
                self.cap = None
                self.enabled = False
                return False
            self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            self.enabled = True
            self._open_retries = 0
            print(f"[Camera] Opened camera {self.index}")
            return True
        except Exception as e:
            print(f"[Camera] ERROR opening camera: {e}")
            self.cap = None
            self.enabled = False
            return False

    def disable(self):
        self.enabled = False
        if self.cap and self.cap.isOpened():
            self.cap.release()
            print("[Camera] Released — light off")

    def enable(self):
        if self.cap and self.cap.isOpened():
            self.enabled = True
            print("[Camera] Already open")
            return
        self._open()

    def capture_frame(self):
        """Return raw frame (for motion detection) and JPEG bytes."""
        if not self.enabled:
            return None, None
        ret, frame = self.cap.read()
        if not ret:
            return None, None
        _, jpeg = cv2.imencode('.jpg', frame, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
        return frame, jpeg.tobytes()

    def close(self):
        if self.cap:
            self.cap.release()
