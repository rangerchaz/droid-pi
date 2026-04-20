"""Motion centroid tracker via frame differencing."""
import cv2


class MotionTracker:
    """Track motion centroid via frame differencing — works in any lighting."""

    def __init__(self):
        self.prev_gray = None
        self.last_centroid = None
        self.frames_without_motion = 0

    def detect(self, frame):
        """Returns (center_x, center_y) of motion, or None."""
        try:
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            gray = cv2.GaussianBlur(gray, (21, 21), 0)
            small = cv2.resize(gray, (160, 120))

            if self.prev_gray is None:
                self.prev_gray = small
                return None

            diff = cv2.absdiff(self.prev_gray, small)
            self.prev_gray = small

            _, thresh = cv2.threshold(diff, 25, 255, cv2.THRESH_BINARY)
            contours, _ = cv2.findContours(thresh, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

            if not contours:
                self.frames_without_motion += 1
                return None

            # Filter small noise (< 3% of frame area)
            min_area = (160 * 120) * 0.03
            big_contours = [c for c in contours if cv2.contourArea(c) > min_area]
            if not big_contours:
                self.frames_without_motion += 1
                return None

            largest = max(big_contours, key=cv2.contourArea)
            M = cv2.moments(largest)
            if M['m00'] == 0:
                return None

            scale_x = frame.shape[1] / 160
            scale_y = frame.shape[0] / 120
            cx = int((M['m10'] / M['m00']) * scale_x)
            cy = int((M['m01'] / M['m00']) * scale_y)

            self.last_centroid = (cx, cy)
            self.frames_without_motion = 0
            return (cx, cy)
        except Exception:
            return None
