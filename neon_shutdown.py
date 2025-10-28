import ctypes
import json
import logging
import os
import subprocess
import sys
import time
from datetime import date, datetime
from pathlib import Path

from PyQt5.QtCore import Qt, QThread, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QFont, QIcon, QPainter, QPixmap
from PyQt5.QtWidgets import (
    QAction,
    QApplication,
    QComboBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSystemTrayIcon,
    QVBoxLayout,
    QWidget,
    QSplashScreen,
)

# ----------------- App metadata & config -----------------
APP_TITLE = "Neon Auto Shutdown"
APP_VERSION = "2.0.0"
CONFIG_FILE = "neon_settings.json"
ICON_FILE = "auto_shutdown_icon.png"  # Place your icon in the same directory

DEFAULT_CONFIG = {
    "default_minutes": 30,
    "idle_minutes": 5,
    "theme": "neon",
    "pre_reminder_minutes": 5,
    "schedules": [{"time": "23:30", "days": ["Mon", "Tue", "Wed", "Thu", "Fri"]}],
    "points": 0,
    "streak": 0,
    "last_shutdown_date": None,
    "achievements": [],
}

# Neon StyleSheet
NEON_BLUE = "#00ccff"
DARK_BG = "#0a0f1a"
NEON_STYLE = f"""
QWidget {{
    background: {DARK_BG};
    color: {NEON_BLUE};
    font-family: Consolas, 'Roboto Mono', monospace;
}}
QPushButton {{
    background: {DARK_BG};
    color: {NEON_BLUE};
    border: 2px solid {NEON_BLUE};
    border-radius: 10px;
    padding: 8px;
    font-weight: bold;
    font-size: 15px;
}}
QPushButton:hover {{
    background: {NEON_BLUE};
    color: {DARK_BG};
}}
QLineEdit, QComboBox {{
    background: #141e2b;
    color: {NEON_BLUE};
    border: 1px solid {NEON_BLUE};
    border-radius: 8px;
    padding: 6px;
    font-size: 14px;
}}
QLabel#title {{
    font-size: 22px;
    font-weight: bold;
    color: {NEON_BLUE};
}}
"""

# Setup logging
log = logging.getLogger("neon_shutdown")
log.setLevel(logging.DEBUG)
ch = logging.StreamHandler()
ch.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
ch.setFormatter(formatter)
log.addHandler(ch)

def load_config() -> dict:
    """Load config JSON, merge with defaults, and return a safe dict."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError, OSError) as e:
            log.warning("Config load error: %s", e)
            return DEFAULT_CONFIG.copy()
        if not isinstance(data, dict):
            return DEFAULT_CONFIG.copy()
        merged = DEFAULT_CONFIG.copy()
        # Only accept known keys
        for k, v in data.items():
            if k in DEFAULT_CONFIG:
                merged[k] = v
        return merged
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict) -> None:
    """Persist config to disk as JSON."""
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        log.error("Config write error: %s", e)

CONFIG = load_config()

# ----------------- Background workers -----------------
class IdleMonitor(QThread):
    idle = pyqtSignal()
    def __init__(self, minutes: int = 5):
        super().__init__()
        self.minutes = max(1, int(minutes))
        self.running = True

    def get_idle_seconds(self) -> float:
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        try:
            if not ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii)):
                return 0.0
            millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
            return millis / 1000.0
        except Exception as e:
            log.debug("get_idle_seconds error: %s", e)
            return 0.0

    def run(self) -> None:
        while self.running:
            try:
                idle_sec = self.get_idle_seconds()
                if idle_sec >= self.minutes * 60:
                    log.debug("Idle detected: %s seconds", idle_sec)
                    self.idle.emit()
                    # wait a bit to avoid spamming
                    for _ in range(5):
                        if not self.running:
                            break
                        time.sleep(1)
                else:
                    time.sleep(1)
            except Exception as e:
                log.exception("IdleMonitor error: %s", e)
                time.sleep(2)

class ScheduleChecker(QThread):
    trigger = pyqtSignal(dict)
    def __init__(self, schedules):
        super().__init__()
        self.schedules = schedules or []
        self.running = True
        self._last_keys = set()

    def run(self) -> None:
        while self.running:
            try:
                now = datetime.now()
                t = now.strftime("%H:%M")
                d = now.strftime("%a")
                for rule in self.schedules:
                    time_str = rule.get("time")
                    days = rule.get("days", [])
                    if t == time_str and d in days:
                        key = f"{t}-{d}-{now.strftime('%Y%m%d%H%M')}"
                        if key not in self._last_keys:
                            self._last_keys.add(key)
                            self.trigger.emit(rule)
                time.sleep(5)
            except Exception as e:
                log.exception("ScheduleChecker error: %s", e)
                time.sleep(5)

# ----------------- Countdown overlay -----------------
class CountdownOverlay(QWidget):
    """Top-most translucent countdown widget."""
    def __init__(self, seconds: int):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(240, 120)
        self.remaining = max(0, int(seconds))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)

    def tick(self) -> None:
        self.remaining -= 1
        if self.remaining <= 0:
            self.close()
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor(NEON_BLUE)
        alpha = 220 + (25 if self.remaining % 2 == 0 else 0)
        color.setAlpha(alpha)
        painter.setPen(color)
        painter.setFont(QFont("Consolas", 34, QFont.Bold))
        mins = self.remaining // 60
        secs = self.remaining % 60
        painter.drawText(self.rect(), Qt.AlignCenter, f"{mins:02d}:{secs:02d}")

# ----------------- Main Application -----------------
class NeonShutdownApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.setWindowIcon(QIcon(ICON_FILE) if os.path.exists(ICON_FILE) else QIcon())
        self.setGeometry(300, 200, 600, 340)

        # State
        self.shutdown_timer = None
        self.pre_reminder_timer = None
        self.overlay = None
        self.points = int(CONFIG.get("points", 0))
        self.streak = int(CONFIG.get("streak", 0))
        self.achievements = set(CONFIG.get("achievements", []))
        self.last_shutdown_date = CONFIG.get("last_shutdown_date", None)

        # Initialize UI and tray
        self.init_ui()
        self.setStyleSheet(NEON_STYLE)
        self.init_tray()

        # Background threads
        self.idle_thread = IdleMonitor(CONFIG.get("idle_minutes", 5))
        self.idle_thread.idle.connect(self.idle_warning)
        self.idle_thread.start()

        self.schedule_thread = ScheduleChecker(CONFIG.get("schedules", []))
        self.schedule_thread.trigger.connect(self.schedule_from_rule)
        self.schedule_thread.start()

        self.set_status("Ready.")
        self.update_level_label()

    # ----------------- UI -----------------
    def init_ui(self):
        title = QLabel(APP_TITLE)
        title.setObjectName("title")
        title.setFont(QFont("Consolas", 20, QFont.Bold))

        self.status = QLabel("Ready.")
        self.level = QLabel("Points: 0 | Streak: 0")

        self.minutes_input = QLineEdit(str(CONFIG.get("default_minutes", 30)))
        self.minutes_input.setPlaceholderText("Minutes (1-360)")

        self.schedule_btn = QPushButton("Schedule Shutdown")
        self.schedule_btn.clicked.connect(self.schedule_shutdown)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.clicked.connect(self.cancel_shutdown)

        self.delay_btn = QPushButton("Delay 5m")
        self.delay_btn.clicked.connect(lambda: self.delay_shutdown(5))

        self.btn_shutdown = QPushButton("Shutdown Now")
        self.btn_shutdown.clicked.connect(self.shutdown_now)

        self.btn_restart = QPushButton("Restart")
        self.btn_restart.clicked.connect(self.restart_now)

        self.btn_sleep = QPushButton("Sleep")
        self.btn_sleep.clicked.connect(self.sleep_now)

        self.btn_lock = QPushButton("Lock")
        self.btn_lock.clicked.connect(self.lock_now)

        self.theme_select = QComboBox()
        self.theme_select.addItems(["neon", "light", "blue", "red"])
        self.theme_select.setCurrentText(CONFIG.get("theme", "neon"))
        self.theme_select.currentTextChanged.connect(self.change_theme)

        top = QHBoxLayout()
        top.addWidget(QLabel("Minutes:"))
        top.addWidget(self.minutes_input)
        top.addWidget(self.schedule_btn)

        row = QHBoxLayout()
        row.addWidget(self.cancel_btn)
        row.addWidget(self.delay_btn)

        power = QHBoxLayout()
        power.addWidget(self.btn_shutdown)
        power.addWidget(self.btn_restart)
        power.addWidget(self.btn_sleep)
        power.addWidget(self.btn_lock)

        layout = QVBoxLayout(self)
        layout.addWidget(title)
        layout.addLayout(top)
        layout.addLayout(row)
        layout.addLayout(power)

        theme_row = QHBoxLayout()
        theme_row.addWidget(QLabel("Theme:"))
        theme_row.addWidget(self.theme_select)
        layout.addLayout(theme_row)

        layout.addWidget(self.status)
        layout.addWidget(self.level)

    def apply_theme(self, mode: str):
        if mode == "light":
            self.setStyleSheet("QWidget { background:#f5f5f5; color:#111; font-family:Consolas; }")
        elif mode == "blue":
            self.setStyleSheet(
                "QWidget { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,"
                "stop:0 #0a0f1a, stop:1 #001f3f); color:#00ccff; font-family:Consolas; }"
            )
        elif mode == "red":
            self.setStyleSheet("QWidget { background:#1a0a0a; color:#ff4444; font-family:Consolas; }")
        else:
            self.setStyleSheet(NEON_STYLE)

    def change_theme(self, mode: str):
        CONFIG["theme"] = mode
        save_config(CONFIG)
        self.apply_theme(mode)

    def init_tray(self):
        icon = QIcon(ICON_FILE) if os.path.exists(ICON_FILE) else QIcon()
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip(f"{APP_TITLE} v{APP_VERSION}")
        menu = QMenu()

        act_about = QAction("About", self)
        act_about.triggered.connect(self.show_about)

        act_shutdown = QAction("Shutdown", self)
        act_shutdown.triggered.connect(self.shutdown_now)

        act_restart = QAction("Restart", self)
        act_restart.triggered.connect(self.restart_now)

        act_sleep = QAction("Sleep", self)
        act_sleep.triggered.connect(self.sleep_now)

        act_lock = QAction("Lock", self)
        act_lock.triggered.connect(self.lock_now)

        act_quit = QAction("Quit", self)
        act_quit.triggered.connect(self.close)

        menu.addAction(act_about)
        menu.addSeparator()
        for a in [act_shutdown, act_restart, act_sleep, act_lock]:
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.show()

    # ----------------- Helpers / Gamification -----------------
    def set_status(self, msg: str) -> None:
        self.status.setText(msg)

    def update_level_label(self) -> None:
        self.level.setText(f"Points: {self.points} | Streak: {self.streak}")

    def persist_gamification(self) -> None:
        CONFIG["points"] = self.points
        CONFIG["streak"] = self.streak
        CONFIG["achievements"] = sorted(list(self.achievements))
        CONFIG["last_shutdown_date"] = self.last_shutdown_date
        save_config(CONFIG)
        self.update_level_label()

    def unlock(self, badge: str) -> None:
        if badge not in self.achievements:
            self.achievements.add(badge)
            self.persist_gamification()
            QMessageBox.information(self, "Achievement Unlocked", f"{badge}")

    def _clear_overlay(self) -> None:
        if self.overlay:
            try:
                self.overlay.close()
            except Exception as e:
                log.debug("Overlay close error: %s", e)
            finally:
                self.overlay = None

    def _stop_timer(self, name: str) -> None:
        t = getattr(self, name, None)
        if t:
            try:
                t.stop()
            except Exception as e:
                log.debug("Timer '%s' stop error: %s", name, e)
            finally:
                setattr(self, name, None)

    def show_about(self) -> None:
        QMessageBox.information(
            self,
            "About",
            f"{APP_TITLE} v{APP_VERSION}\n"
            "A Neon-themed Shutdown Scheduler\n"
            "© 2025 Kwame Software\n"
            "All rights reserved.",
        )

    # ----------------- Scheduling / Reminders -----------------
    def schedule_shutdown(self) -> None:
        try:
            minutes = int(self.minutes_input.text().strip())
            if not (1 <= minutes <= 360):
                raise ValueError("Minutes must be 1-360")
        except ValueError:
            self.set_status("Invalid minutes")
            return

        seconds = minutes * 60
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()

        pre = int(CONFIG.get("pre_reminder_minutes", 5))
        if minutes > pre > 0:
            self.pre_reminder_timer = QTimer(self)
            self.pre_reminder_timer.setSingleShot(True)
            self.pre_reminder_timer.timeout.connect(
                lambda: self.tray.showMessage(
                    APP_TITLE, f"Shutdown in {pre} minutes. Save your work!", QSystemTrayIcon.Information
                )
            )
            self.pre_reminder_timer.start((minutes - pre) * 60 * 1000)

        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setSingleShot(True)
        self.shutdown_timer.timeout.connect(self.shutdown_now)
        self.shutdown_timer.start(seconds * 1000)

        self.overlay = CountdownOverlay(seconds)
        try:
            screen_geo = QApplication.primaryScreen().availableGeometry()
            x = max(0, min(self.x() + self.width() - 260, screen_geo.width() - 260))
            y = max(0, min(self.y() + 60, screen_geo.height() - 140))
            self.overlay.move(self.mapToGlobal(self.rect().topLeft()).x() + self.width() - 260, self.mapToGlobal(self.rect().topLeft()).y() + 60)
        except Exception as e:
            log.debug("Overlay move error: %s", e)
        self.overlay.show()

        self.set_status(f"Shutdown in {minutes} min")

    def schedule_from_rule(self, rule: dict) -> None:
        t = rule.get("time", "?")
        d = ", ".join(rule.get("days", []))
        ans = QMessageBox.question(
            self,
            APP_TITLE,
            (
                f"Scheduled rule matched: {t} on {d}.\n"
                f"Schedule shutdown for {CONFIG.get('default_minutes', 30)} minutes from now?"
            ),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.Yes,
        )
        if ans == QMessageBox.Yes:
            self.minutes_input.setText(str(CONFIG.get("default_minutes", 30)))
            self.schedule_shutdown()

    def idle_warning(self) -> None:
        ans = QMessageBox.question(
            self,
            APP_TITLE,
            f"You\u2019ve been idle for {CONFIG.get('idle_minutes', 5)} minutes.\nShut down now?",
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        )
        if ans == QMessageBox.Yes:
            self.shutdown_now()

    # ----------------- System actions & gamification -----------------
    @property
    def SHUTDOWN_EXE(self):
        return str(Path(os.path.expandvars(r"%SystemRoot%") ) / "System32" / "shutdown.exe")

    @property
    def RUNDLL32_EXE(self):
        return str(Path(os.path.expandvars(r"%SystemRoot%") ) / "System32" / "rundll32.exe")

    def _run_cmd(self, args, *, shell: bool = False, status_ok: str = None, status_err: str = None) -> bool:
        try:
            if isinstance(args, str):
                log.debug("Running shell command: %s", args)
            else:
                log.debug("Running command: %s", args)
            subprocess.run(args, check=True, shell=shell)
            if status_ok:
                self.set_status(status_ok)
            return True
        except (FileNotFoundError, subprocess.CalledProcessError, OSError) as e:
            if status_err:
                self.set_status(f"{status_err}: {e}")
            else:
                log.debug("Command error: %s", e)
            return False

    def _apply_shutdown_rewards(self, action: str = "shutdown") -> None:
        if action == "shutdown":
            self.points += 10
            today = date.today().isoformat()
            try:
                if self.last_shutdown_date:
                    last = datetime.strptime(self.last_shutdown_date, "%Y-%m-%d").date()
                    delta_days = (date.today() - last).days
                    if delta_days == 1:
                        self.streak += 1
                    elif delta_days == 0:
                        pass
                    else:
                        self.streak = 1
                else:
                    self.streak = 1
            except (ValueError, TypeError) as e:
                log.debug("Streak parse error: %s", e)
                self.streak = 1
            self.last_shutdown_date = today

            if self.points >= 100:
                self.unlock("Century Points: 100+")
            if self.streak >= 7:
                self.unlock("Weekly Warrior: 7-day streak")
            self.unlock("Power Down: First successful shutdown")

        elif action == "sleep":
            self.points += 5
            if self.points >= 100:
                self.unlock("Century Points: 100+")
            self.unlock("Power Saver: Slept instead of shutdown")

        self.persist_gamification()

    def cancel_shutdown(self) -> None:
        self._run_cmd([self.SHUTDOWN_EXE, "/a"], status_ok="Cancelled", status_err="Cancel failed")
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self.points = max(0, self.points - 5)
        self.streak = 0
        self.persist_gamification()
        self.set_status("Cancelled (-5 pts)")

    def delay_shutdown(self, minutes: int = 5) -> None:
        if not self.shutdown_timer:
            self.set_status("Nothing to delay.")
            return
        self.cancel_shutdown()
        self.minutes_input.setText(str(int(minutes)))
        self.schedule_shutdown()
        self.set_status(f"Delayed {minutes}m")

    def shutdown_now(self) -> None:
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self.set_status("Shutting down...")
        self._apply_shutdown_rewards("shutdown")
        self.tray.showMessage(APP_TITLE, "System will shutdown now!", QSystemTrayIcon.Warning)
        self._run_cmd([self.SHUTDOWN_EXE, "/s", "/t", "1"], status_err="Shutdown failed")

    def restart_now(self) -> None:
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self.points = max(0, self.points - 2)
        self.persist_gamification()
        self.set_status("Restarting...")
        self.tray.showMessage(APP_TITLE, "System will restart now!", QSystemTrayIcon.Warning)
        self._run_cmd([self.SHUTDOWN_EXE, "/r", "/t", "1"], status_err="Restart failed")

    def sleep_now(self) -> None:
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self._apply_shutdown_rewards("sleep")
        self.set_status("Sleeping...")
        self.tray.showMessage(APP_TITLE, "System entering sleep mode.", QSystemTrayIcon.Information)
        cmd = f'"{self.RUNDLL32_EXE}" powrprof.dll,SetSuspendState 0,1,0'
        self._run_cmd(cmd, shell=True, status_err="Sleep failed")

    def lock_now(self) -> None:
        self.set_status("Locking...")
        self.tray.showMessage(APP_TITLE, "System locked.", QSystemTrayIcon.Information)
        cmd = f'"{self.RUNDLL32_EXE}" user32.dll,LockWorkStation'
        self._run_cmd(cmd, shell=True, status_err="Lock failed")

    # ----------------- Cleanup -----------------
    def closeEvent(self, event):
        try:
            self.idle_thread.running = False
            self.idle_thread.wait(1000)
        except Exception as e:
            log.debug("Idle thread cleanup error: %s", e)

        try:
            self.schedule_thread.running = False
            self.schedule_thread.wait(1000)
        except Exception as e:
            log.debug("Schedule thread cleanup error: %s", e)

        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()

        save_config(CONFIG)
        event.accept()

# ----------------- Boot -----------------
def show_splash():
    if os.path.exists(ICON_FILE):
        pix = QPixmap(ICON_FILE)
        splash = QSplashScreen(pix.scaled(300, 300, Qt.KeepAspectRatio, Qt.SmoothTransformation), Qt.WindowStaysOnTopHint)
        splash.show()
        QTimer.singleShot(1200, splash.close)  # show splash for 1.2 seconds
        return splash
    return None

if __name__ == "__main__":
    QApplication.setAttribute(Qt.AA_EnableHighDpiScaling, True)
    app = QApplication(sys.argv)
    show_splash()
    window = NeonShutdownApp()
    window.show()
    sys.exit(app.exec_())