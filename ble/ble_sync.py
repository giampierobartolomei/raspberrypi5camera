#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""
BLE‑triggered synchronized capture with large buffering and constant frame rate.

This script merges two prior approaches: the classic three‑thread pipeline used
to avoid dropping frames under disk stalls, and the constant frame‑rate (CFR)
resampler used to guarantee a strictly constant 30 fps timeline.  The goal is
to start recording immediately when the BLE client sends ``rec`` and to stop
immediately on ``stp``, writing a video at a constant 30 fps and then
converting it to 20 fps while preserving the duration.  Unlike earlier
versions, this Raspberry Pi script no longer writes a per‑frame CSV log:
the mobile app already produces its own log, and only the video is saved.
All long‑running
writes are decoupled via a large frame queue so the capture loop can run at
maximum priority without blocking on I/O.

Key features:

* **Instant start/stop** – the start time is taken from the arrival time of
  the ``rec`` command on this device.  Frames delivered before that moment
  are discarded to avoid including stale data sitting in the camera buffer.
* **Constant 30 fps output** – frames are duplicated or skipped to map the
  asynchronous delivery from the camera onto a strict 30 fps timeline.  At
  stop the last frame is repeated as padding so the video duration matches
  the real elapsed time.
* **Large queue** – the frame queue (``write_q``) is dimensioned to hold
  30 seconds of data.  This absorbs temporary disk slowdowns without
  dropping frames or blocking the capture thread.
* **No per‑frame CSV or trigger file** – the Raspberry Pi does not log
  individual frames to CSV and does not write a separate trigger file.
  The mobile app provides its own CSV and trigger information; the Pi
  only saves the video.
* **Post‑processing** – after the recording stops the source video is
  verified to be CFR ~30 fps using ffprobe, then automatically converted to
  20 fps via ffmpeg with ``-vsync cfr``.  The output duration is checked to
  match the source within ±1 frame at 20 fps.  Any mismatch raises an
  exception so problems are immediately apparent.

Dependencies (on Raspberry Pi OS):

  sudo apt‑get install -y python3-opencv python3-dbus python3-gi gir1.2-glib-2.0 v4l-utils ffmpeg

Run:

  sudo python3 ble_sync_combined.py

"""

import os
import sys
import time
import signal
import queue
import threading
import ctypes
import subprocess
import json
# import csv as _csv  # CSV logging removed; type left as Optional[object]
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import cv2
try:
    from gpiozero import LED
except Exception:
    LED = None

import dbus
import dbus.service
import dbus.mainloop.glib
from gi.repository import GLib


# ======================== CONFIG ========================

VIDEO_DEVICE = "/dev/video0"
WIDTH = 800
HEIGHT = 600
FPS = 30  # CFR timeline target (hardware delivers ~30 fps)
FOURCC_IN = "MJPG"
FOURCC_OUT = "MJPG"

# Queue size – sized for 30 s of data to absorb disk stalls.
WRITE_Q_MAXLEN = FPS * 30  # 900 frames at 30 fps
# Note: we no longer create a separate CSV queue.  The mobile app produces
# its own CSV log, so the Raspberry Pi does not write per‑frame CSV.

OUTPUT_DIR = "./recordings"

FRAME_INTERVAL_NS = int(1e9 / FPS)  # 33,333,333 ns per frame at 30 fps
# Consider a gap >80 ms as a hiccup worth logging.  It does not change the
# resampling but gives visibility into camera/driver stalls.
HICCUP_THRESHOLD_NS = int(1e9 * 0.080)

# Strict verification: ffprobe must exist and report CFR 30 on the source
STRICT_FPS_VERIFY = True
ENABLE_LEDS = True

# BLE UUIDs (match mobile app and firmware)
SERVICE_UUID = "19B10010-E8F2-537E-4F6C-D104768A1214"
CHAR_UUID = "19b10012-e8f2-537e-4f6c-d104768a1214"

# DBus / BlueZ constants
BLUEZ_SERVICE_NAME = "org.bluez"
GATT_MANAGER_IFACE = "org.bluez.GattManager1"
DBUS_OM_IFACE = "org.freedesktop.DBus.ObjectManager"
DBUS_PROP_IFACE = "org.freedesktop.DBus.Properties"
GATT_SERVICE_IFACE = "org.bluez.GattService1"
GATT_CHRC_IFACE = "org.bluez.GattCharacteristic1"
LE_ADVERTISING_MANAGER_IFACE = "org.bluez.LEAdvertisingManager1"
LE_ADVERTISEMENT_IFACE = "org.bluez.LEAdvertisement1"

# ========================================================


# ======================== LEDS (optional) ========================

LED_SERVICE_READY_PIN = 20
LED_BT_CONNECTED_PIN = 16
LED_RECORDING_PIN = 12


class _NullLED:
    def on(self) -> None:
        pass

    def off(self) -> None:
        pass

    def close(self) -> None:
        pass


def _make_led(pin: int):
    if not ENABLE_LEDS or LED is None:
        return _NullLED()
    try:
        return LED(pin)
    except Exception as e:
        print(f"[WARN] LED init failed on GPIO{pin}: {e}")
        return _NullLED()


LED_SERVICE_READY = _make_led(LED_SERVICE_READY_PIN)
LED_BT_CONNECTED = _make_led(LED_BT_CONNECTED_PIN)
LED_RECORDING = _make_led(LED_RECORDING_PIN)

LED_SERVICE_READY.off()
LED_BT_CONNECTED.off()
LED_RECORDING.off()

_last_gatt_activity = 0.0


def mark_gatt_activity() -> None:
    global _last_gatt_activity
    _last_gatt_activity = time.monotonic()
    LED_BT_CONNECTED.on()


def scan_any_connected(bus) -> bool:
    """Return True if any org.bluez.Device1 has Connected=True."""
    try:
        om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
        objects = om.GetManagedObjects()
        for _path, ifaces in objects.items():
            dev = ifaces.get("org.bluez.Device1")
            if dev and bool(dev.get("Connected", False)):
                return True
    except Exception:
        return False
    return False


def update_bt_led(bus) -> bool:
    """Periodic truth check for the BT LED with short GATT grace window."""
    alive = scan_any_connected(bus)
    if not alive and (time.monotonic() - _last_gatt_activity) < 2.0:
        alive = True
    if alive:
        LED_BT_CONNECTED.on()
    else:
        LED_BT_CONNECTED.off()
    return True


def on_properties_changed(interface, changed, invalidated, path=None, **kwargs):
    """Track connect/disconnect to update BT LED quickly."""
    if interface == "org.bluez.Device1" and "Connected" in changed:
        connected = bool(changed["Connected"])
        print(f"[BLE] Device at {path} connected={connected}")
        if connected:
            LED_BT_CONNECTED.on()


def ensure_dir(path: str) -> None:
    """Ensure directory exists."""
    os.makedirs(path, exist_ok=True)


def now_tag() -> str:
    """Return a timestamp string for filenames."""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def which(cmd: str) -> Optional[str]:
    """Find full path of an executable if present in $PATH."""
    for p in os.environ.get("PATH", "").split(os.pathsep):
        c = os.path.join(p, cmd)
        if os.path.isfile(c) and os.access(c, os.X_OK):
            return c
    return None


def set_thread_max_priority() -> None:
    """Raise the current thread's priority to maximum if possible."""
    try:
        # Nice can range from -20 (highest priority) to +19 (lowest).  The
        # expression ensures we always set to -20 regardless of current nice.
        os.nice(-20 - os.nice(0))
        return
    except Exception:
        pass
    # Try SCHED_FIFO if adjusting nice fails.  Not all systems permit this.
    try:
        param = ctypes.c_int(1)
        ctypes.cdll.LoadLibrary("libc.so.6").sched_setscheduler(0, 1, ctypes.byref(param))
    except Exception:
        pass


def v4l2_set_parm_30() -> None:
    """Attempt to lock the UVC camera to 30 fps via v4l2-ctl."""
    v4l2 = which("v4l2-ctl")
    if not v4l2:
        print("[WARN] v4l2-ctl not found. Install v4l-utils for tighter FPS locking.")
        return
    try:
        subprocess.run([
            v4l2, "-d", VIDEO_DEVICE, "--set-parm", str(FPS)
        ], check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    except Exception as e:
        print(f"[WARN] v4l2-ctl --set-parm failed: {e}")


def _ffprobe_video(path: str) -> dict:
    """Return ffprobe info for the first video stream in the given file."""
    ffprobe = which("ffprobe")
    if not ffprobe:
        raise RuntimeError("ffprobe not found. Install ffmpeg (ffprobe is required for strict FPS verification).")
    cmd = [
        ffprobe,
        "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate,nb_frames,duration,width,height,codec_name,pix_fmt",
        "-of", "json",
        path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffprobe failed: {p.stderr.strip()}")
    try:
        data = json.loads(p.stdout)
        return data["streams"][0]
    except Exception as e:
        raise RuntimeError(f"Failed to parse ffprobe output: {e}")


def _rate_to_float(rate: str) -> float:
    """Convert a rational or decimal string rate to float."""
    if not rate or rate == "0/0":
        return 0.0
    if "/" in rate:
        a, b = rate.split("/", 1)
        try:
            return float(a) / float(b)
        except Exception:
            return 0.0
    try:
        return float(rate)
    except Exception:
        return 0.0


def verify_source_cfr_30(path: str, tol_fps: float = 0.05) -> None:
    """Raise if the video at ``path`` is not CFR ~30 fps (within tolerance)."""
    info = _ffprobe_video(path)
    r = _rate_to_float(info.get("r_frame_rate", "0/0"))
    a = _rate_to_float(info.get("avg_frame_rate", "0/0"))
    if r <= 0 or a <= 0:
        raise RuntimeError(f"Invalid FPS values: r={info.get('r_frame_rate')} avg={info.get('avg_frame_rate')}")
    if abs(r - FPS) > tol_fps or abs(a - FPS) > tol_fps:
        raise RuntimeError(f"Source FPS mismatch: r={r:.4f} avg={a:.4f} expected={FPS}")
    if abs(r - a) > 0.01:
        raise RuntimeError(f"Source video appears VFR: r={r:.4f} avg={a:.4f}")


def convert_30_to_20_keep_duration(src_path: str, dst_path: str) -> None:
    """Convert a 30 fps video to 20 fps MP4/H.264 while preserving duration."""
    ffmpeg = which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("ffmpeg not found. Install ffmpeg for post‑processing.")
    verify_source_cfr_30(src_path)
    cmd = [
        ffmpeg,
        "-y",
        "-v", "error",
        "-i", src_path,
        "-vf", "fps=20",
        "-vsync", "cfr",
        "-an",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "18",
        "-pix_fmt", "yuv420p",
        dst_path,
    ]
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"ffmpeg conversion failed: {p.stderr.strip()}")
    # Verify output FPS
    out = _ffprobe_video(dst_path)
    r = _rate_to_float(out.get("r_frame_rate", "0/0"))
    a = _rate_to_float(out.get("avg_frame_rate", "0/0"))
    if abs(r - 20.0) > 0.05 or abs(a - 20.0) > 0.05:
        raise RuntimeError(f"Converted FPS mismatch: r={r:.4f} avg={a:.4f} expected=20")
    # Verify duration
    in_info = _ffprobe_video(src_path)
    dur_in = float(in_info.get("duration") or 0.0)
    dur_out = float(out.get("duration") or 0.0)
    # Accept ±1 frame at 20 fps (0.05 s) difference
    if dur_in > 0 and dur_out > 0:
        if abs(dur_in - dur_out) > 0.06:
            raise RuntimeError(f"Duration changed: input={dur_in:.3f}s output={dur_out:.3f}s")


@dataclass
class FrameItem:
    """Container for a captured frame and its timestamps."""
    idx: int
    frame: any
    capture_unix_ns: int
    capture_mono_ns: int
    is_hiccup: int


# CsvRow and CSV logging have been removed.  The Raspberry Pi no longer
# produces a per‑frame CSV log; only the video is saved.  A trigger file
# is also no longer generated, since the mobile app logs trigger times.


class CameraRecorder:
    """
    Orchestrates capture, resampling and recording using two worker threads:

      * capture – reads from the camera at max priority and enqueues frames
      * writer  – resamples frames to a CFR 30 fps timeline and writes the video

    The Raspberry Pi does not produce a per‑frame CSV log or a trigger file.
    Only the video is saved; the mobile app is responsible for logging
    trigger times and frame‑level data.  Recording can be started and
    stopped multiple times without restarting the threads.  Between
    sessions the frame queue is drained and counters are reset.
    """

    def __init__(self) -> None:
        ensure_dir(OUTPUT_DIR)

        # Camera and threads
        self.cap: Optional[cv2.VideoCapture] = None
        self.capture_thread: Optional[threading.Thread] = None
        self.writer_thread: Optional[threading.Thread] = None
        # No separate CSV thread; CSV logging has been removed.

        # Coordination
        self.stop_event = threading.Event()
        self.recording_event = threading.Event()
        self.outputs_ready = threading.Event()
        self.flush_done = threading.Event()
        self.lock = threading.Lock()

        # Queues
        self.write_q: "queue.Queue[Optional[FrameItem]]" = queue.Queue(maxsize=WRITE_Q_MAXLEN)
        # No CSV queue; the Raspberry Pi does not log frames to CSV.

        # Counters / stats
        self.frame_counter = 0       # incremental index of captured frames
        self.hiccup_count = 0
        self.dup_inserted = 0
        self.early_skipped = 0
        self.queue_drops = 0

        # Timing anchors per session
        self.rec_start_unix_ns: Optional[int] = None
        self.rec_start_mono_ns: Optional[int] = None
        self.arm_mono_ns: Optional[int] = None
        self.stop_unix_ns: Optional[int] = None
        self.stop_mono_ns: Optional[int] = None

        # CFR state
        self.out_frame_idx = -1
        self.last_frame = None

        # Output files
        self.session_base: Optional[str] = None
        self.video_path: Optional[str] = None
        # No csv_path or trigger_path attributes are defined since we no longer
        # generate per‑frame CSV logs or trigger files.
        self.video_writer: Optional[cv2.VideoWriter] = None
        # CSV file handles are not used.  They remain None.
        self.csv_fh: Optional[object] = None
        # We no longer log per‑frame CSV, so csv_writer remains None and
        # is typed as a generic optional object to avoid referencing the
        # removed csv module.
        self.csv_writer: Optional[object] = None

    # ---------------- Camera init ----------------

    def open_camera_standby(self) -> None:
        """Open the camera device and start the worker threads."""
        v4l2_set_parm_30()
        self.cap = cv2.VideoCapture(VIDEO_DEVICE, cv2.CAP_V4L2)
        if not self.cap.isOpened():
            raise RuntimeError(f"Cannot open camera device {VIDEO_DEVICE}")
        self.cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*FOURCC_IN))
        self.cap.set(cv2.CAP_PROP_FRAME_WIDTH, WIDTH)
        self.cap.set(cv2.CAP_PROP_FRAME_HEIGHT, HEIGHT)
        self.cap.set(cv2.CAP_PROP_FPS, FPS)
        # Reduce internal buffering if supported
        try:
            self.cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
        except Exception:
            pass
        actual_w = int(self.cap.get(cv2.CAP_PROP_FRAME_WIDTH))
        actual_h = int(self.cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
        actual_fps = float(self.cap.get(cv2.CAP_PROP_FPS))
        fourcc = int(self.cap.get(cv2.CAP_PROP_FOURCC))
        fourcc_str = "".join([chr((fourcc >> 8 * i) & 0xFF) for i in range(4)])
        print(f"[CAM] {VIDEO_DEVICE}: {actual_w}x{actual_h} @ {actual_fps:.2f}fps reported, FOURCC={fourcc_str}")
        print(f"[CAM] CFR output forced to {FPS}fps (duplicates/skips to compensate jitter)")
        # Start threads
        self.capture_thread = threading.Thread(target=self._capture_loop, daemon=True, name="capture")
        self.writer_thread = threading.Thread(target=self._writer_loop, daemon=True, name="writer")
        # No CSV thread is created because we do not write per‑frame CSV.
        self.capture_thread.start()
        self.writer_thread.start()

    # ---------------- Capture loop ----------------

    def _capture_loop(self) -> None:
        """Read frames from the camera and enqueue them when recording."""
        set_thread_max_priority()
        prev_mono = None
        while not self.stop_event.is_set():
            # Timestamp before blocking read
            t_unix = time.time_ns()
            t_mono = time.monotonic_ns()
            ok, frame = self.cap.read() if self.cap is not None else (False, None)
            if not ok or frame is None:
                time.sleep(0.002)
                continue
            # Detect hiccups
            is_hiccup = 0
            if prev_mono is not None:
                gap = t_mono - prev_mono
                if gap > HICCUP_THRESHOLD_NS:
                    is_hiccup = 1
                    with self.lock:
                        self.hiccup_count += 1
                    print(f"[HICCUP] gap={gap/1e6:.1f}ms  total={self.hiccup_count}")
            prev_mono = t_mono
            # Assign index
            with self.lock:
                idx = self.frame_counter
                self.frame_counter += 1
            # Only queue frames when actively recording
            if not self.recording_event.is_set():
                continue
            # Skip frames that were in the buffer before the arm time
            arm = self.arm_mono_ns
            if arm is not None and t_mono < arm:
                continue
            item = FrameItem(
                idx=idx,
                frame=frame,
                capture_unix_ns=t_unix,
                capture_mono_ns=t_mono,
                is_hiccup=is_hiccup,
            )
            try:
                self.write_q.put_nowait(item)
            except queue.Full:
                with self.lock:
                    self.queue_drops += 1
                print(f"[DROP] write_q full. total_drops={self.queue_drops}")

    # ---------------- Output helpers ----------------

    def _open_outputs(self) -> None:
        """Create output files for a new recording session."""
        base = f"{now_tag()}_svpro"
        self.session_base = base
        self.video_path = os.path.join(OUTPUT_DIR, f"{base}.avi")
        # Video writer at constant FPS
        fourcc = cv2.VideoWriter_fourcc(*FOURCC_OUT)
        self.video_writer = cv2.VideoWriter(self.video_path, fourcc, FPS, (WIDTH, HEIGHT))
        if not self.video_writer.isOpened():
            raise RuntimeError("VideoWriter failed to open")
        # No CSV file is opened; csv_fh and csv_writer remain None
        self.csv_fh = None
        self.csv_writer = None
        # No trigger file is created.  Only the video path is printed.
        print(f"[REC] Video:   {self.video_path}")

    def _close_outputs(self) -> None:
        """Close the output files if open."""
        if self.video_writer is not None:
            self.video_writer.release()
            self.video_writer = None
        if self.csv_fh is not None:
            self.csv_fh.flush()
            self.csv_fh.close()
            self.csv_fh = None
            self.csv_writer = None

    def _write_frame(self,
                     out_idx: int,
                     frame) -> None:
        """
        Write a single frame to the output video.  CSV logging is disabled,
        so only the frame is written.  If the video writer is not initialised
        the call is a no‑op.
        """
        if self.video_writer is None:
            return
        self.video_writer.write(frame)

    def _process_source_item_cfr(self, item: FrameItem) -> None:
        """
        Map a captured frame to the CFR timeline.  Duplicate or skip frames
        as needed and write them to the video.  CSV logging is disabled,
        so only the video output is produced.
        """
        # Ensure we have a start time
        if self.rec_start_mono_ns is None:
            return
        # Compute elapsed time since start
        delta_ns = item.capture_mono_ns - self.rec_start_mono_ns
        if delta_ns < 0:
            return
        expected_out = int(delta_ns // FRAME_INTERVAL_NS)
        # Fill any missing indices with duplicates of the last frame
        while self.out_frame_idx < expected_out - 1:
            self.out_frame_idx += 1
            dup_frame = self.last_frame if self.last_frame is not None else item.frame
            with self.lock:
                self.dup_inserted += 1
            self._write_frame(self.out_frame_idx, dup_frame)
        # If this frame maps to an index we've already produced, skip it
        if expected_out <= self.out_frame_idx:
            with self.lock:
                self.early_skipped += 1
            return
        # Write the real frame
        self.out_frame_idx = expected_out
        self.last_frame = item.frame
        self._write_frame(self.out_frame_idx, item.frame)

    def _pad_to_stop_time(self) -> None:
        """
        After stop_recording() is called, pad the video with duplicates of the
        last frame so that the total number of output frames matches the
        session's duration at 30 fps.
        """
        if self.rec_start_mono_ns is None or self.stop_mono_ns is None:
            return
        if self.last_frame is None:
            return
        dur_ns = self.stop_mono_ns - self.rec_start_mono_ns
        if dur_ns < 0:
            return
        target_total_frames = max(1, int(round((dur_ns / 1e9) * FPS)))
        target_last_idx = target_total_frames - 1
        while self.out_frame_idx < target_last_idx:
            self.out_frame_idx += 1
            with self.lock:
                self.dup_inserted += 1
            self._write_frame(self.out_frame_idx, self.last_frame)

    # ---------------- Worker threads ----------------

    def _writer_loop(self) -> None:
        """
        Consume frames from ``write_q``, resample them to a CFR 30 fps
        timeline and write them to the video file.  A sentinel value
        (``None``) signals the end of a session and triggers padding up to
        the stop time.
        """
        while not self.stop_event.is_set():
            if not self.recording_event.is_set() or not self.outputs_ready.is_set():
                time.sleep(0.002)
                continue
            try:
                item = self.write_q.get(timeout=0.2)
            except queue.Empty:
                continue
            if item is None:
                # End of session – pad and signal flush done
                try:
                    self._pad_to_stop_time()
                except Exception as e:
                    print(f"[ERR] padding failed: {e}")
                self.flush_done.set()
                continue
            # Normal frame
            try:
                self._process_source_item_cfr(item)
            except Exception as e:
                print(f"[ERR] writer item failed: {e}")

    # No _csv_loop method is defined because we no longer write per‑frame CSV.

    # ---------------- Recording control ----------------

    def start_recording(self, recv_unix_ns: int, recv_mono_ns: int) -> None:
        """Begin a recording session immediately."""
        with self.lock:
            if self.recording_event.is_set():
                return
            # Reset stats
            self.hiccup_count = 0
            self.dup_inserted = 0
            self.early_skipped = 0
            self.queue_drops = 0
            # Set timing anchors
            self.rec_start_unix_ns = recv_unix_ns
            self.rec_start_mono_ns = recv_mono_ns
            self.arm_mono_ns = recv_mono_ns
            self.stop_unix_ns = None
            self.stop_mono_ns = None
            # Reset CFR state
            self.out_frame_idx = -1
            self.last_frame = None
            self.flush_done.clear()
        # Drain the frame queue before starting
        self._drain_write_q()
        # Open outputs
        self._open_outputs()
        # Signal threads that outputs are ready
        self.outputs_ready.set()
        # Start recording
        self.recording_event.set()
        LED_RECORDING.on()
        print(f"[BLE->REC] START recv_unix_ns={recv_unix_ns} recv_mono_ns={recv_mono_ns}")

    def stop_recording(self) -> None:
        """End the current recording session."""
        with self.lock:
            if not self.recording_event.is_set():
                return
            # Mark stop times
            self.stop_unix_ns = time.time_ns()
            self.stop_mono_ns = time.monotonic_ns()
        # Stop capturing frames for this session
        self.recording_event.clear()
        # Push sentinel to writer queue to trigger padding and flush
        try:
            self.write_q.put_nowait(None)
        except queue.Full:
            # Make room for sentinel if needed
            try:
                _ = self.write_q.get_nowait()
            except queue.Empty:
                pass
            try:
                self.write_q.put_nowait(None)
            except Exception:
                pass
        # Wait for writer to finish padding
        self.flush_done.wait(timeout=5.0)
        # Close outputs and clear ready flag
        self._close_outputs()
        self.outputs_ready.clear()
        LED_RECORDING.off()
        print(f"[BLE->REC] STOP hiccups={self.hiccup_count} inserted={self.dup_inserted} skipped={self.early_skipped} drops={self.queue_drops}")
        # Verify source and convert to 20 fps
        if self.video_path and STRICT_FPS_VERIFY:
            try:
                verify_source_cfr_30(self.video_path)
                print("[VERIFY] Source video CFR 30fps OK (ffprobe)")
            except Exception as e:
                print(f"[ERR] Source FPS verification FAILED: {e}")
                return
        try:
            if self.video_path and self.session_base:
                dst = os.path.join(OUTPUT_DIR, f"{self.session_base}_20fps.mp4")
                print("[POST] Converting 30 -> 20 fps (duration preserved)")
                convert_30_to_20_keep_duration(self.video_path, dst)
                print(f"[POST] Saved: {dst}")
        except Exception as e:
            print(f"[ERR] Post-processing failed: {e}")

    def _drain_write_q(self) -> None:
        """Remove all items from the write queue without blocking."""
        try:
            while True:
                _ = self.write_q.get_nowait()
        except queue.Empty:
            pass

    # CSV drain helpers are removed since we no longer log frames to CSV.

    def shutdown(self) -> None:
        """Stop all threads and release resources."""
        self.stop_event.set()
        # Cancel any ongoing recording
        self.recording_event.clear()
        LED_RECORDING.off()
        # Ensure writer sees sentinel
        try:
            self.write_q.put_nowait(None)
        except Exception:
            pass
        # Wait for threads to exit gracefully
        for t in (self.capture_thread, self.writer_thread):
            if t is not None and t.is_alive():
                t.join(timeout=2.0)
        # Close outputs
        self._close_outputs()
        # Release camera
        if self.cap is not None:
            try:
                self.cap.release()
            except Exception:
                pass
            self.cap = None


# ======================== BLE GATT SERVER ========================

class InvalidArgsException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.freedesktop.DBus.Error.InvalidArgs"


class NotSupportedException(dbus.exceptions.DBusException):
    _dbus_error_name = "org.bluez.Error.NotSupported"


class Application(dbus.service.Object):
    """BlueZ application container for our GATT services."""
    def __init__(self, bus, recorder: CameraRecorder):
        self.path = "/org/bluez/example/app"
        self.services = []
        self.bus = bus
        self.recorder = recorder
        super().__init__(bus, self.path)
        self.add_service(CameraService(bus, 0, recorder))

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def add_service(self, s) -> None:
        self.services.append(s)

    @dbus.service.method(DBUS_OM_IFACE, out_signature="a{oa{sa{sv}}}")
    def GetManagedObjects(self):
        r = {}
        for s in self.services:
            r[s.get_path()] = s.get_properties()
            for c in s.characteristics:
                r[c.get_path()] = c.get_properties()
        return r


class Service(dbus.service.Object):
    """Simple GATT service wrapper."""
    def __init__(self, bus, index, uuid, primary):
        self.path = f"/org/bluez/example/service{index}"
        self.bus = bus
        self.uuid = uuid
        self.primary = primary
        self.characteristics = []
        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            GATT_SERVICE_IFACE: {
                "UUID": self.uuid,
                "Primary": self.primary,
                "Characteristics": dbus.Array([c.get_path() for c in self.characteristics], signature="o"),
            }
        }

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def add_characteristic(self, c) -> None:
        self.characteristics.append(c)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        p = self.get_properties().get(interface, {})
        if prop not in p:
            raise InvalidArgsException()
        return p[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})


class Characteristic(dbus.service.Object):
    """Basic GATT characteristic wrapper."""
    def __init__(self, bus, index, uuid, flags, service):
        self.path = service.get_path() + f"/char{index}"
        self.bus = bus
        self.uuid = uuid
        self.flags = flags
        self.service = service
        super().__init__(bus, self.path)

    def get_properties(self):
        return {
            GATT_CHRC_IFACE: {
                "Service": self.service.get_path(),
                "UUID": self.uuid,
                "Flags": dbus.Array(self.flags, signature="s"),
            }
        }

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        p = self.get_properties().get(interface, {})
        if prop not in p:
            raise InvalidArgsException()
        return p[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="a{sv}", out_signature="ay")
    def ReadValue(self, options):
        raise NotSupportedException()

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        raise NotSupportedException()


class CameraService(Service):
    """Service exposing the command characteristic for start/stop."""
    def __init__(self, bus, index, recorder: CameraRecorder):
        super().__init__(bus, index, SERVICE_UUID, True)
        self.add_characteristic(CommandCharacteristic(bus, 0, recorder, self))


class CommandCharacteristic(Characteristic):
    """Characteristic handling BLE write commands to control recording."""
    def __init__(self, bus, index, recorder: CameraRecorder, service):
        super().__init__(bus, index, CHAR_UUID, ["write", "write-without-response"], service)
        self.recorder = recorder

    @dbus.service.method(GATT_CHRC_IFACE, in_signature="aya{sv}")
    def WriteValue(self, value, options):
        """Start or stop recording based on incoming BLE payload."""
        try:
            payload = bytes(value).decode("utf-8", errors="ignore").strip()
        except Exception:
            payload = ""
        mark_gatt_activity()
        recv_unix = time.time_ns()
        recv_mono = time.monotonic_ns()
        low = payload.lower()
        if low.startswith("rec"):
            # Start recording immediately; ignore any trailing timestamp
            self.recorder.start_recording(recv_unix, recv_mono)
        elif low.startswith("stp"):
            self.recorder.stop_recording()
        else:
            print(f"[BLE] Unknown payload: {payload!r}")


class Advertisement(dbus.service.Object):
    """Simple LE advertisement exposing the service UUID and name."""
    def __init__(self, index, advertising_type, bus):
        self.path = f"/org/bluez/example/advertisement{index}"
        self.ad_type = advertising_type
        self.service_uuids = [SERVICE_UUID]
        self.local_name = "CameraModule" #Change in CameraModule2 if you want a second camera glasses device
        self.include_tx_power = True
        super().__init__(bus, self.path)

    def get_path(self) -> dbus.ObjectPath:
        return dbus.ObjectPath(self.path)

    def get_properties(self):
        return {
            LE_ADVERTISEMENT_IFACE: {
                "Type": self.ad_type,
                "ServiceUUIDs": dbus.Array(self.service_uuids, signature="s"),
                "LocalName": dbus.String(self.local_name),
                "IncludeTxPower": dbus.Boolean(self.include_tx_power),
            }
        }

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="ss", out_signature="v")
    def Get(self, interface, prop):
        p = self.get_properties().get(interface, {})
        if prop not in p:
            raise InvalidArgsException()
        return p[prop]

    @dbus.service.method(DBUS_PROP_IFACE, in_signature="s", out_signature="a{sv}")
    def GetAll(self, interface):
        return self.get_properties().get(interface, {})

    @dbus.service.method(LE_ADVERTISEMENT_IFACE, in_signature="", out_signature="")
    def Release(self):
        pass


# ======================== MAIN ========================

def find_adapter(bus) -> str:
    """Return the D‑Bus object path of the first adapter supporting advertising and GATT."""
    om = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, "/"), DBUS_OM_IFACE)
    for path, ifaces in om.GetManagedObjects().items():
        if LE_ADVERTISING_MANAGER_IFACE in ifaces and GATT_MANAGER_IFACE in ifaces:
            return path
    raise RuntimeError("No BLE adapter found. Is bluetoothd running?")


def register_app_and_advertisement(bus, mainloop, recorder: CameraRecorder):
    adapter_path = find_adapter(bus)
    app = Application(bus, recorder)
    gatt_mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), GATT_MANAGER_IFACE)
    gatt_mgr.RegisterApplication(
        app.get_path(), {},
        reply_handler=lambda: print("[BLE] GATT registered."),
        error_handler=lambda e: (print(f"[BLE] GATT error: {e}"), mainloop.quit()),
    )
    adv = Advertisement(0, "peripheral", bus)
    adv_mgr = dbus.Interface(bus.get_object(BLUEZ_SERVICE_NAME, adapter_path), LE_ADVERTISING_MANAGER_IFACE)
    adv_mgr.RegisterAdvertisement(
        adv.get_path(), {},
        reply_handler=lambda: print("[BLE] Advertisement registered."),
        error_handler=lambda e: (print(f"[BLE] Adv error: {e}"), mainloop.quit()),
    )
    return app, adv


def main() -> None:
    ensure_dir(OUTPUT_DIR)
    # Check prerequisites for strict verification
    if STRICT_FPS_VERIFY and not which("ffprobe"):
        print("[FATAL] STRICT_FPS_VERIFY enabled but ffprobe missing. Install ffmpeg.")
        sys.exit(2)
    recorder = CameraRecorder()
    recorder.open_camera_standby()
    dbus.mainloop.glib.DBusGMainLoop(set_as_default=True)
    def handle_sig(_sig, _frm):
        print("\n[SYS] Shutting down...")
        try:
            recorder.stop_recording()
        except Exception:
            pass
        recorder.shutdown()
        try:
            mainloop.quit()
        except Exception:
            pass
    signal.signal(signal.SIGINT, handle_sig)
    signal.signal(signal.SIGTERM, handle_sig)
    bus = dbus.SystemBus()
    bus.add_signal_receiver(
        on_properties_changed,
        dbus_interface=DBUS_PROP_IFACE,
        signal_name="PropertiesChanged",
        path_keyword="path",
    )
    global mainloop
    mainloop = GLib.MainLoop()
    try:
        register_app_and_advertisement(bus, mainloop, recorder)
    except Exception as e:
        print(f"[BLE] Setup failed: {e}")
        recorder.shutdown()
        sys.exit(1)
    GLib.timeout_add_seconds(1, lambda: update_bt_led(bus))
    LED_SERVICE_READY.on()
    print("[SYS] Ready. Camera warm. Waiting for BLE 'rec' / 'stp'...")
    try:
        mainloop.run()
    finally:
        LED_SERVICE_READY.off()
        LED_BT_CONNECTED.off()
        LED_RECORDING.off()
        for led in (LED_SERVICE_READY, LED_BT_CONNECTED, LED_RECORDING):
            try:
                led.close()
            except Exception:
                pass


if __name__ == "__main__":
    main()
