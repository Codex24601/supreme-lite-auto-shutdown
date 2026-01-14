import os, sys, time, ctypes, json
from datetime import datetime, date
from PyQt5.QtCore import Qt, QTimer, QThread, pyqtSignal
from PyQt5.QtGui import QFont, QIcon, QPainter, QColor
from PyQt5.QtWidgets import (
    QApplication, QWidget, QLabel, QLineEdit, QPushButton, QVBoxLayout,
    QHBoxLayout, QMessageBox, QSystemTrayIcon, QMenu, QAction, QComboBox
)

APP_TITLE = "Supreme Lite Auto‑Shutdown"
APP_VERSION = "1.0.0"
CONFIG_FILE = "supreme_settings.json"

DEFAULT_CONFIG = {
    "default_minutes": 30,
    "idle_minutes": 5,
    "theme": "dark",
    "pre_reminder_minutes": 5,
    "schedules": [
        {"time": "23:30", "days": ["Mon","Tue","Wed","Thu","Fri"]}
    ],
    # gamification
    "points": 0,
    "streak": 0,
    "last_shutdown_date": None,
    "achievements": []
}

# ------------- Config I/O -------------
def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                for k, v in DEFAULT_CONFIG.items():
                    if k not in data:
                        data[k] = v
                return data
        except:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    try:
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except:
        pass

CONFIG = load_config()

# ------------- Idle detection (Windows) -------------
class IdleMonitor(QThread):
    idle = pyqtSignal()
    def __init__(self, minutes=5):
        super().__init__()
        self.minutes = max(1, int(minutes))
        self.running = True
    def get_idle_seconds(self):
        class LASTINPUTINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]
        lii = LASTINPUTINFO()
        lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
        ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
        millis = ctypes.windll.kernel32.GetTickCount() - lii.dwTime
        return millis / 1000.0
    def run(self):
        while self.running:
            try:
                if self.get_idle_seconds() >= self.minutes * 60:
                    self.idle.emit()
                    time.sleep(5)
                time.sleep(1)
            except:
                time.sleep(2)

# ------------- Countdown overlay -------------
class CountdownOverlay(QWidget):
    def __init__(self, seconds):
        super().__init__()
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.WindowStaysOnTopHint | Qt.Tool)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.resize(220, 110)
        self.remaining = max(0, int(seconds))
        self.timer = QTimer(self)
        self.timer.timeout.connect(self.tick)
        self.timer.start(1000)
    def tick(self):
        self.remaining -= 1
        if self.remaining <= 0:
            self.close()
        self.update()
    def paintEvent(self, e):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        color = QColor("#00ff66")
        alpha = 180 + (75 if self.remaining % 2 == 0 else 0)  # pulse effect
        color.setAlpha(alpha)
        painter.setPen(color)
        painter.setFont(QFont("Consolas", 28, QFont.Bold))
        mins = self.remaining // 60
        secs = self.remaining % 60
        painter.drawText(self.rect(), Qt.AlignCenter, f"{mins:02d}:{secs:02d}")

# ------------- Smart schedule checker -------------
class ScheduleChecker(QThread):
    trigger = pyqtSignal(dict)
    def __init__(self, schedules):
        super().__init__()
        self.schedules = schedules or []
        self.running = True
        self._last_key = None
    def run(self):
        while self.running:
            try:
                now = datetime.now()
                t = now.strftime("%H:%M")
                d = now.strftime("%a")
                for rule in self.schedules:
                    if t == rule.get("time") and d in rule.get("days", []):
                        key = f"{t}-{d}-{now.strftime('%Y%m%d%H%M')}"
                        if self._last_key != key:
                            self._last_key = key
                            self.trigger.emit(rule)
                        time.sleep(60)
                time.sleep(5)
            except:
                time.sleep(5)

# ------------- Main app -------------
class SupremeApp(QWidget):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(f"{APP_TITLE} v{APP_VERSION}")
        self.setWindowIcon(QIcon("power.png"))  # ensure power.png in same folder
        self.setGeometry(300, 200, 600, 330)

        # state
        self.shutdown_timer = None
        self.pre_reminder_timer = None
        self.overlay = None
        self.points = int(CONFIG.get("points", 0))
        self.streak = int(CONFIG.get("streak", 0))
        self.achievements = set(CONFIG.get("achievements", []))
        self.last_shutdown_date = CONFIG.get("last_shutdown_date", None)

        # UI + visuals
        self.init_ui()
        self.apply_theme(CONFIG.get("theme", "dark"))
        self.init_tray()

        # background threads
        self.idle_thread = IdleMonitor(CONFIG.get("idle_minutes", 5))
        self.idle_thread.idle.connect(self.idle_warning)
        self.idle_thread.start()

        self.schedule_thread = ScheduleChecker(CONFIG.get("schedules", []))
        self.schedule_thread.trigger.connect(self.schedule_from_rule)
        self.schedule_thread.start()

        self.set_status("Ready.")
        self.update_level_label()

    # ---------- UI / Theme ----------
    def init_ui(self):
        title = QLabel(f"{APP_TITLE}")
        title.setFont(QFont("Consolas", 14, QFont.Bold))

        self.status = QLabel("Ready.")
        self.level = QLabel("Points: 0 | Streak: 0")

        self.minutes_input = QLineEdit(str(CONFIG.get("default_minutes", 30)))
        self.schedule_btn = QPushButton("Schedule Shutdown"); self.schedule_btn.clicked.connect(self.schedule_shutdown)
        self.cancel_btn = QPushButton("Cancel"); self.cancel_btn.clicked.connect(self.cancel_shutdown)
        self.delay_btn = QPushButton("Delay 5m"); self.delay_btn.clicked.connect(lambda: self.delay_shutdown(5))

        self.btn_shutdown = QPushButton("Shutdown Now"); self.btn_shutdown.clicked.connect(self.shutdown_now)
        self.btn_restart = QPushButton("Restart"); self.btn_restart.clicked.connect(self.restart_now)
        self.btn_sleep = QPushButton("Sleep"); self.btn_sleep.clicked.connect(self.sleep_now)
        self.btn_lock = QPushButton("Lock"); self.btn_lock.clicked.connect(self.lock_now)

        self.theme_select = QComboBox()
        self.theme_select.addItems(["dark", "light", "blue", "red"])
        self.theme_select.setCurrentText(CONFIG.get("theme", "dark"))
        self.theme_select.currentTextChanged.connect(self.change_theme)

        # layouts
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

        # button style (visual polish)
        button_style = """
        QPushButton {
            background-color:#0f1a0f; color:#00ff66;
            border:2px solid #00ff66; border-radius:8px; padding:6px; font-weight:bold;
        }
        QPushButton:hover { background-color:#00ff66; color:#0a0f0a; }
        """
        for btn in [self.schedule_btn, self.cancel_btn, self.delay_btn,
                    self.btn_shutdown, self.btn_restart, self.btn_sleep, self.btn_lock]:
            btn.setStyleSheet(button_style)

    def apply_theme(self, mode):
        if mode == "light":
            self.setStyleSheet("QWidget { background:#f5f5f5; color:#111; font-family:Consolas; }")
        elif mode == "blue":
            self.setStyleSheet("QWidget { background:qlineargradient(x1:0,y1:0,x2:1,y2:1,stop:0 #0a0f1a, stop:1 #001f3f); color:#00ccff; font-family:Consolas; }")
        elif mode == "red":
            self.setStyleSheet("QWidget { background:#1a0a0a; color:#ff4444; font-family:Consolas; }")
        else:
            self.setStyleSheet("QWidget { background:#0a0f0a; color:#00ff66; font-family:Consolas; }")

    def change_theme(self, mode):
        CONFIG["theme"] = mode
        save_config(CONFIG)
        self.apply_theme(mode)

    def init_tray(self):
        icon = QIcon("power.png")
        self.tray = QSystemTrayIcon(icon)
        self.tray.setToolTip(f"{APP_TITLE} v{APP_VERSION}")
        menu = QMenu()

        act_about = QAction("About", self); act_about.triggered.connect(self.show_about)
        act_shutdown = QAction("Shutdown", self); act_shutdown.triggered.connect(self.shutdown_now)
        act_restart  = QAction("Restart", self);  act_restart.triggered.connect(self.restart_now)
        act_sleep    = QAction("Sleep", self);    act_sleep.triggered.connect(self.sleep_now)
        act_lock     = QAction("Lock", self);     act_lock.triggered.connect(self.lock_now)
        act_quit     = QAction("Quit", self);     act_quit.triggered.connect(self.close)

        menu.addAction(act_about)
        menu.addSeparator()
        for a in [act_shutdown, act_restart, act_sleep, act_lock]:
            menu.addAction(a)
        menu.addSeparator()
        menu.addAction(act_quit)

        self.tray.setContextMenu(menu)
        self.tray.show()

    # ---------- Helpers ----------
    def set_status(self, msg):
        self.status.setText(msg)

    def update_level_label(self):
        self.level.setText(f"Points: {self.points} | Streak: {self.streak}")

    def persist_gamification(self):
        CONFIG["points"] = self.points
        CONFIG["streak"] = self.streak
        CONFIG["achievements"] = sorted(list(self.achievements))
        CONFIG["last_shutdown_date"] = self.last_shutdown_date
        save_config(CONFIG)
        self.update_level_label()

    def unlock(self, badge):
        if badge not in self.achievements:
            self.achievements.add(badge)
            self.persist_gamification()
            QMessageBox.information(self, "Achievement Unlocked", f"{badge}")

    def _clear_overlay(self):
        if self.overlay:
            try: self.overlay.close()
            except: pass
            self.overlay = None

    def _stop_timer(self, name):
        t = getattr(self, name, None)
        if t:
            try: t.stop()
            except: pass
            setattr(self, name, None)

    def show_about(self):
        QMessageBox.information(
            self, "About",
            f"{APP_TITLE} v{APP_VERSION}\n"
            "Developed by Kwame\n"
            "© 2025 Kwame Software\n"
            "All rights reserved."
        )

    # ---------- Scheduling / Reminders ----------
    def schedule_shutdown(self):
        try:
            minutes = int(self.minutes_input.text().strip())
            if minutes <= 0:
                raise ValueError
        except:
            self.set_status("Invalid minutes")
            return

        seconds = minutes * 60
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()

        # pre-reminder popup
        pre = int(CONFIG.get("pre_reminder_minutes", 5))
        if minutes > pre and pre > 0:
            self.pre_reminder_timer = QTimer(self)
            self.pre_reminder_timer.setSingleShot(True)
            self.pre_reminder_timer.timeout.connect(
                lambda: QMessageBox.information(self, APP_TITLE, f"Shutdown in {pre} minutes. Save your work.")
            )
            self.pre_reminder_timer.start((minutes - pre) * 60 * 1000)

        # main shutdown timer
        self.shutdown_timer = QTimer(self)
        self.shutdown_timer.setSingleShot(True)
        self.shutdown_timer.timeout.connect(self.shutdown_now)
        self.shutdown_timer.start(seconds * 1000)

        # overlay
        self.overlay = CountdownOverlay(seconds)
        try:
            self.overlay.move(self.x() + self.width() - 240, self.y() + 60)
        except:
            pass
        self.overlay.show()

        self.set_status(f"Shutdown in {minutes} min")

    def schedule_from_rule(self, rule):
        t = rule.get("time", "?")
        d = ", ".join(rule.get("days", []))
        ans = QMessageBox.question(
            self, APP_TITLE,
            f"Scheduled rule matched: {t} on {d}.\nSchedule shutdown for {CONFIG.get('default_minutes',30)} minutes from now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
        )
        if ans == QMessageBox.Yes:
            self.minutes_input.setText(str(CONFIG.get("default_minutes", 30)))
            self.schedule_shutdown()

    def idle_warning(self):
        ans = QMessageBox.question(
            self, APP_TITLE,
            f"You’ve been idle for {CONFIG.get('idle_minutes', 5)} minutes.\nShut down now?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No
        )
        if ans == QMessageBox.Yes:
            self.shutdown_now()

    def cancel_shutdown(self):
        try: os.system("shutdown /a")
        except: pass
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        # gamification penalty
        self.points = max(0, self.points - 5)
        self.streak = 0
        self.persist_gamification()
        self.set_status("Cancelled (-5 pts)")

    def delay_shutdown(self, minutes=5):
        if not self.shutdown_timer:
            self.set_status("Nothing to delay.")
            return
        self.cancel_shutdown()
        self.minutes_input.setText(str(int(minutes)))
        self.schedule_shutdown()
        self.set_status(f"Delayed {minutes}m")

    # ---------- Power actions + Gamification ----------
    def _apply_shutdown_rewards(self, action="shutdown"):
        # points and streak handling for successful shutdown (or sleep reward)
        if action == "shutdown":
            self.points += 10
            today = date.today().isoformat()
            if self.last_shutdown_date is None:
                self.streak = 1
            else:
                try:
                    last = datetime.strptime(self.last_shutdown_date, "%Y-%m-%d").date()
                    if (date.today() - last).days == 1:
                        self.streak += 1
                    elif (date.today() - last).days == 0:
                        # same day shutdown: keep streak as is
                        pass
                    else:
                        self.streak = 1
                except:
                    self.streak = 1
            self.last_shutdown_date = today

            # simple achievements
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

    def shutdown_now(self):
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self.set_status("Shutting down...")
        self._apply_shutdown_rewards("shutdown")
        os.system("shutdown /s /t 1")

    def restart_now(self):
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        # small penalty to discourage unnecessary restarts
        self.points = max(0, self.points - 2)
        self.persist_gamification()
        self.set_status("Restarting...")
        os.system("shutdown /r /t 1")

    def sleep_now(self):
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        self._apply_shutdown_rewards("sleep")
        self.set_status("Sleeping...")
        os.system("rundll32.exe powrprof.dll,SetSuspendState 0,1,0")

    def lock_now(self):
        self.set_status("Locking...")
        os.system("rundll32.exe user32.dll,LockWorkStation")

    # ---------- Cleanup ----------
    def closeEvent(self, e):
        try:
            self.idle_thread.running = False
            self.idle_thread.wait(1000)
        except:
            pass
        try:
            self.schedule_thread.running = False
            self.schedule_thread.wait(1000)
        except:
            pass
        self._stop_timer("shutdown_timer")
        self._stop_timer("pre_reminder_timer")
        self._clear_overlay()
        save_config(CONFIG)
        e.accept()

# ------------- Boot -------------
if __name__ == "__main__":
    app = QApplication(sys.argv)
    w = SupremeApp()
    w.show()
    sys.exit(app.exec_())
