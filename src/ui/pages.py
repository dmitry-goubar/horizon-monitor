"""Tab pages for the horizon-monitor window.

Phase 1 scope:
  - MonitorPage  : full alert controls laid out; "Test beep" works. Engine wiring lands
                   in a later phase (the toggle/terms are kept in memory for now).
  - CollectPage / PullPage / PushPage : styled previews of the planned feature.
  - AskPage      : fully functional Q&A over the RAG store (reuses QueryAgent streaming).

Every page is a self-contained QWidget so the shell can add "more to come" with no churn.
"""

from __future__ import annotations

import asyncio
import os
import re
import threading
from pathlib import Path

from PySide6.QtCore import Qt, QObject, QTimer, Signal
from PySide6.QtGui import QFont
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QFrame, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QListWidgetItem, QPlainTextEdit, QPushButton, QRadioButton, QScrollArea, QSpinBox,
    QVBoxLayout, QWidget,
)

from .theme import COLORS, STATUS_COLORS


# --------------------------------------------------------------------- helpers

def _card(title: str = "", subtitle: str = "") -> tuple[QFrame, QVBoxLayout]:
    """A rounded surface card; returns (frame, inner_layout) for adding content."""
    card = QFrame()
    card.setProperty("class", "card")
    lay = QVBoxLayout(card)
    lay.setContentsMargins(16, 14, 16, 16)
    lay.setSpacing(10)
    if title:
        t = QLabel(title)
        t.setObjectName("SectionTitle")
        lay.addWidget(t)
    if subtitle:
        s = QLabel(subtitle)
        s.setObjectName("Dim")
        s.setWordWrap(True)
        lay.addWidget(s)
    return card, lay


def _page_scaffold(title: str, subtitle: str) -> tuple[QWidget, QVBoxLayout]:
    """A scrollable page with a title/subtitle header; returns (page, body_layout)."""
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(24, 20, 24, 20)
    outer.setSpacing(14)

    head = QVBoxLayout()
    head.setSpacing(2)
    t = QLabel(title)
    t.setObjectName("PageTitle")
    s = QLabel(subtitle)
    s.setObjectName("PageSubtitle")
    head.addWidget(t)
    head.addWidget(s)
    outer.addLayout(head)

    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.Shape.NoFrame)
    inner = QWidget()
    body = QVBoxLayout(inner)
    body.setContentsMargins(0, 0, 0, 0)
    body.setSpacing(14)
    scroll.setWidget(inner)
    outer.addWidget(scroll, 1)
    return page, body


def _coming_soon(text: str) -> QLabel:
    lbl = QLabel("🛠  " + text)
    lbl.setObjectName("Dim")
    lbl.setWordWrap(True)
    return lbl


def _explain_exc(exc: BaseException) -> str:
    """Human-readable error message, unwrapping anyio/asyncio ExceptionGroups.

    The MCP stdio client runs tool calls inside a task group, so real failures surface
    as 'unhandled errors in a TaskGroup (N sub-exception)' — useless to the user. Dig out
    the underlying leaf exception(s) and report those instead.
    """
    group = getattr(exc, "exceptions", None)
    if group:  # BaseExceptionGroup / ExceptionGroup
        parts = [_explain_exc(e) for e in group]
        seen, uniq = set(), []
        for p in parts:
            if p not in seen:
                seen.add(p)
                uniq.append(p)
        return "; ".join(uniq)
    msg = str(exc).strip()
    return f"{type(exc).__name__}: {msg}" if msg else type(exc).__name__


# ----------------------------------------------------------------- Monitor tab

class MonitorPage(QWidget):
    """Watch for your name / keywords → local beep + (later) Telegram ping.

    Presence-only: alerts say *who* mentioned you, never the message text.
    """

    def __init__(self) -> None:
        super().__init__()
        page, body = _page_scaffold(
            "Monitor",
            "Get pinged the moment someone mentions you — so stepping away never looks "
            "like you went dark. No message content ever leaves this machine.",
        )

        # Engine controls — start/pause/stop the background monitor. This one loop also
        # feeds the Collect tab, so the controls live here but govern monitoring app-wide.
        ctl_card, ctl_lay = _card()
        ctl_row = QHBoxLayout()
        self.engine_pill = QLabel()
        self.engine_pill.setProperty("class", "pill")
        ctl_row.addWidget(self.engine_pill)
        ctl_row.addStretch(1)
        self.btn_start = QPushButton("Start")
        self.btn_start.setObjectName("Primary")
        self.btn_pause = QPushButton("Pause")
        self.btn_stop = QPushButton("Stop")
        for b in (self.btn_start, self.btn_pause, self.btn_stop):
            ctl_row.addWidget(b)
        ctl_lay.addLayout(ctl_row)
        hint = QLabel("Runs the background monitor — also powers the Collect tab's recording.")
        hint.setObjectName("Dim")
        hint.setWordWrap(True)
        ctl_lay.addWidget(hint)
        body.addWidget(ctl_card)
        self.set_engine_state("stopped")

        # On/off
        top, toplay = _card()
        row = QHBoxLayout()
        self.enabled = QCheckBox("Watch for mentions")
        self.enabled.setFont(QFont("Segoe UI", 11))
        row.addWidget(self.enabled)
        row.addStretch(1)
        self._state = QLabel("Off")
        self._state.setObjectName("Dim")
        row.addWidget(self._state)
        toplay.addLayout(row)
        self.enabled.toggled.connect(
            lambda on: self._state.setText("Armed — will alert on a match" if on else "Off")
        )
        body.addWidget(top)

        # Watch terms
        terms_card, terms_lay = _card(
            "Watch terms", "Your name and any keywords that count as 'about you'."
        )
        add_row = QHBoxLayout()
        self.term_input = QLineEdit()
        self.term_input.setPlaceholderText("e.g. Dmitry, my-project, the deploy…")
        add_btn = QPushButton("Add")
        add_row.addWidget(self.term_input, 1)
        add_row.addWidget(add_btn)
        terms_lay.addLayout(add_row)
        self.terms = QListWidget()
        self.terms.setMaximumHeight(130)
        terms_lay.addWidget(self.terms)
        rm_btn = QPushButton("Remove selected")
        terms_lay.addWidget(rm_btn, 0, Qt.AlignLeft)
        add_btn.clicked.connect(self._add_term)
        self.term_input.returnPressed.connect(self._add_term)
        rm_btn.clicked.connect(self._remove_term)
        body.addWidget(terms_card)

        # Notify channels
        notify_card, notify_lay = _card("How to alert me")
        beep_row = QHBoxLayout()
        self.beep = QCheckBox("Beep on this machine")
        self.beep.setChecked(True)
        test_beep = QPushButton("Test")
        test_beep.setMaximumWidth(70)
        test_beep.clicked.connect(self._test_beep)
        beep_row.addWidget(self.beep)
        beep_row.addStretch(1)
        beep_row.addWidget(test_beep)
        notify_lay.addLayout(beep_row)

        tg_row = QHBoxLayout()
        self.telegram = QCheckBox("Ping my phone via Telegram")
        cfg_btn = QPushButton("Configure…")
        cfg_btn.setMaximumWidth(110)
        cfg_btn.setToolTip("Set up the bot token / chat id and send a test ping.")
        cfg_btn.clicked.connect(self._configure_telegram)
        tg_row.addWidget(self.telegram)
        tg_row.addStretch(1)
        tg_row.addWidget(cfg_btn)
        notify_lay.addLayout(tg_row)
        notify_lay.addWidget(_coming_soon(
            "Telegram sends only ‘🔔 <Name> mentioned you’ — never the message body."
        ))
        body.addWidget(notify_card)

        # Webcam access watch (privacy). Your webcam is redirected into the remote session,
        # so the Horizon client opening it = the VDI/Teams/Webex using your camera.
        cam_card, cam_lay = _card(
            "Camera & microphone access",
            "Your camera and mic are redirected into the remote session, so a Teams/Webex "
            "call shows up as the Horizon client opening them. Get alerted + logged whenever "
            "that happens — so you'd notice any access you didn't start.",
        )
        cam_row = QHBoxLayout()
        self.webcam_watch = QCheckBox("Watch & alert on Horizon camera access")
        self.webcam_watch.setChecked(True)
        cam_row.addWidget(self.webcam_watch)
        cam_row.addStretch(1)
        self.btn_webcam_log = QPushButton("Open log")
        self.btn_webcam_log.setMaximumWidth(110)
        cam_row.addWidget(self.btn_webcam_log)
        cam_lay.addLayout(cam_row)
        self.mic_watch = QCheckBox("Watch & alert on Horizon microphone access")
        self.mic_watch.setChecked(True)
        cam_lay.addWidget(self.mic_watch)
        self.webcam_status = QLabel("—")
        self.webcam_status.setObjectName("Dim")
        self.webcam_status.setWordWrap(True)
        cam_lay.addWidget(self.webcam_status)
        body.addWidget(cam_card)

        # Cooldown
        cd_card, cd_lay = _card("Don't spam me")
        cd_row = QHBoxLayout()
        cd_row.addWidget(QLabel("Re-alert cooldown:"))
        self.cooldown = QSpinBox()
        self.cooldown.setRange(0, 120)
        self.cooldown.setValue(5)
        self.cooldown.setSuffix(" min")
        cd_row.addWidget(self.cooldown)
        cd_row.addStretch(1)
        cd_lay.addLayout(cd_row)
        body.addWidget(cd_card)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

    def set_engine_state(self, state: str) -> None:
        """Reflect the capture engine's state in the control bar (pill + button enablement)."""
        labels = {
            "running": "● Running", "paused": "⏸ Paused",
            "stopped": "○ Stopped", "starting": "◌ Starting…",
        }
        color = STATUS_COLORS.get(state, COLORS["text_dim"])
        self.engine_pill.setText(labels.get(state, state))
        self.engine_pill.setStyleSheet(
            f"color: {color}; border: 1px solid {COLORS['border']};"
            f" border-radius: 11px; padding: 3px 10px; background: {COLORS['surface2']};"
        )
        # While starting/running the engine is live, so Start is disabled and Stop works;
        # Pause only applies once fully running.
        self.btn_start.setEnabled(state not in ("running", "starting"))
        self.btn_pause.setEnabled(state == "running")
        self.btn_stop.setEnabled(state != "stopped")

    def _add_term(self) -> None:
        text = self.term_input.text().strip()
        if text:
            self.terms.addItem(text)
            self.term_input.clear()

    def _remove_term(self) -> None:
        for item in self.terms.selectedItems():
            self.terms.takeItem(self.terms.row(item))

    def _test_beep(self) -> None:
        try:
            import winsound
            winsound.Beep(880, 250)
        except Exception:
            from PySide6.QtWidgets import QApplication
            QApplication.beep()

    def _configure_telegram(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        from src.telegram import TelegramNotifier

        tn = TelegramNotifier(
            os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            os.environ.get("TELEGRAM_CHAT_ID", ""),
        )
        if not tn.configured:
            QMessageBox.information(
                self,
                "Telegram presence alerts",
                "Pings your phone with only '🔔 <Name> mentioned you' — never the message "
                "body.\n\nTo enable:\n"
                "1. In Telegram, message @BotFather → /newbot → copy the token.\n"
                "2. Message your new bot once, then open\n"
                "   https://api.telegram.org/bot<token>/getUpdates and copy your chat id.\n"
                "3. Add to .env:\n"
                "     TELEGRAM_BOT_TOKEN=...\n"
                "     TELEGRAM_CHAT_ID=...\n"
                "4. Restart the app, then Configure → Send test.",
            )
            return
        if QMessageBox.question(
            self, "Telegram", "Credentials found. Send a test ping now?"
        ) == QMessageBox.StandardButton.Yes:
            ok, detail = tn.send_test()
            (QMessageBox.information if ok else QMessageBox.warning)(
                self, "Telegram test", detail
            )


# ----------------------------------------------------------------- preview tabs

# Pretty app names for the channel picker, so e.g. "CHAT" shows where it came from.
_APP_LABELS = {
    "teams": "MS Teams", "symphony": "Symphony", "outlook": "Outlook",
    "unknown": "?",
}


class _CollectSignals(QObject):
    status = Signal(str)
    refreshed = Signal(dict, list)   # stats dict, [(channel, app), ...]


class CollectPage(QWidget):
    """Record selected channels into the knowledge base + manage what's stored.

    The channel picker / record toggle feed the engine's collect sink (the main window
    syncs them). Stats + purge talk straight to EventStore/RAGPipeline (same stores the
    engine writes); reads are instant via SQLite, purges run on a worker thread.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._store = None   # lazy EventStore (SQLite — fast, read on the UI thread)
        # Persisted channel selection (raw names). None = no saved prefs → default to all
        # checked; a set means restore exactly those, surviving Refresh re-populates.
        self._restore_channels: set[str] | None = None

        self._sig = _CollectSignals()
        self._sig.status.connect(self._set_status)
        self._sig.refreshed.connect(self._apply_refresh)

        page, body = _page_scaffold(
            "Collect",
            "Record conversations into a private, on-disk knowledge base you can question "
            "in the Ask tab. Choose which channels to keep, and prune what you don't.",
        )

        # Record on/off
        rec_card, rl = _card()
        rec_row = QHBoxLayout()
        self.record = QCheckBox("Record conversations to the knowledge base")
        self.record.setFont(QFont("Segoe UI", 11))
        self.record.setChecked(True)
        rec_row.addWidget(self.record)
        rec_row.addStretch(1)
        rl.addLayout(rec_row)
        body.addWidget(rec_card)

        # Channels
        ch_card, cl = _card(
            "Channels to collect",
            "Collect everything, or tick the channels to keep. They appear here as "
            "they're seen on screen — Refresh after new ones show up.",
        )
        self.collect_all = QCheckBox("Collect all channels")
        self.collect_all.setChecked(True)
        cl.addWidget(self.collect_all)
        self.channels = QListWidget()
        self.channels.setMaximumHeight(150)
        cl.addWidget(self.channels)
        refresh_btn = QPushButton("Refresh channels")
        cl.addWidget(refresh_btn, 0, Qt.AlignLeft)
        body.addWidget(ch_card)

        # Knowledge base stats + purge
        kb_card, kl = _card("Knowledge base")
        self.stats_label = QLabel("…")
        self.stats_label.setWordWrap(True)
        kl.addWidget(self.stats_label)

        older_row = QHBoxLayout()
        older_row.addWidget(QLabel("Delete events older than"))
        self.older_days = QSpinBox()
        self.older_days.setRange(1, 365)
        self.older_days.setValue(30)
        self.older_days.setSuffix(" days")
        older_row.addWidget(self.older_days)
        self.btn_older = QPushButton("Delete")
        older_row.addWidget(self.btn_older)
        older_row.addStretch(1)
        kl.addLayout(older_row)

        self.btn_purge = QPushButton("Purge everything…")
        kl.addWidget(self.btn_purge, 0, Qt.AlignLeft)
        body.addWidget(kb_card)

        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        body.addWidget(self.status)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        self.collect_all.toggled.connect(lambda on: self.channels.setEnabled(not on))
        refresh_btn.clicked.connect(self.refresh)
        self.btn_older.clicked.connect(self._delete_older)
        self.btn_purge.clicked.connect(self._purge_all)
        self.channels.itemChanged.connect(self._channel_toggled)
        self.channels.setEnabled(not self.collect_all.isChecked())
        self.refresh()

    def set_persisted(self, record: bool, collect_all: bool, channels: list | None) -> None:
        """Restore saved Collect prefs (called once at startup before signals are wired)."""
        self.record.setChecked(bool(record))
        self.collect_all.setChecked(bool(collect_all))
        self.channels.setEnabled(not collect_all)
        self._restore_channels = set(channels) if channels is not None else None
        self.refresh()

    def _channel_toggled(self, _item) -> None:
        # A user tick/untick becomes the selection to remember (fires only on real edits —
        # _apply_refresh blocks signals while it rebuilds the list).
        self._restore_channels = set(self.selected_channels())

    # ------------------------------------------------------------ helpers

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _store_conn(self):
        if self._store is None:
            from src.store import EventStore
            self._store = EventStore(
                self._config["rag"].get("events_db", "./data/events.db")
            )
            self._store.connect()
        return self._store

    def selected_channels(self) -> list[str]:
        out = []
        for i in range(self.channels.count()):
            it = self.channels.item(i)
            if it.checkState() == Qt.CheckState.Checked:
                # The raw channel name is stored in UserRole; the visible text also
                # carries the app label (e.g. "CHAT  ·  MS Teams").
                out.append(it.data(Qt.ItemDataRole.UserRole))
        return out

    def refresh(self) -> None:
        try:
            store = self._store_conn()
            self._apply_refresh(store.stats(), store.channels_with_app())
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Could not read the store: {exc}")

    def _apply_refresh(self, stats: dict, channels: list) -> None:
        total = stats.get("total", 0)
        first, last = stats.get("first"), stats.get("last")
        span = f"{first[:10]} → {last[:10]}" if first else "no events yet"
        top = ", ".join(f"{c} ({n})" for c, n in stats.get("channels", [])[:5]) or "—"
        self.stats_label.setText(
            f"{total} event(s) · {span}\nTop channels: {top}"
        )
        # Which channels stay checked: a restored/edited selection if we have one,
        # otherwise the live selection (default: all checked on a fresh start).
        keep = self._restore_channels
        if keep is None:
            keep = set(self.selected_channels())
            default_all = True
        else:
            default_all = False
        self.channels.blockSignals(True)
        self.channels.clear()
        for ch, app in channels:
            label = _APP_LABELS.get(app, app or "?")
            it = QListWidgetItem(f"{ch}    ·  {label}")
            it.setData(Qt.ItemDataRole.UserRole, ch)   # raw channel = the filter value
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            checked = (default_all and not keep) or ch in keep
            it.setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)
            self.channels.addItem(it)
        self.channels.blockSignals(False)

    # ------------------------------------------------------------ purge ops

    def _run_store_op(self, label: str, fn) -> None:
        """Run fn(store, rag) -> result-message on a worker thread, then refresh."""
        self._set_status(f"{label}…")

        def worker() -> None:
            try:
                import os
                from src.rag import RAGPipeline
                store = self._store_conn()
                rcfg = self._config["rag"]
                rag = RAGPipeline(
                    db_path=rcfg["db_path"],
                    collection_name=rcfg["collection_name"],
                    embedding_provider=rcfg["embedding_provider"],
                    voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
                    top_k=rcfg["top_k"],
                )
                rag.connect()
                msg = fn(store, rag)
                self._sig.refreshed.emit(store.stats(), store.channels_with_app())
                self._sig.status.emit(msg)
            except Exception as exc:  # noqa: BLE001
                self._sig.status.emit(f"{label} failed: {exc}")

        threading.Thread(target=worker, daemon=True).start()

    def _delete_older(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        days = self.older_days.value()
        if QMessageBox.question(
            self, "Delete old events",
            f"Delete every event older than {days} day(s) from both stores?",
        ) != QMessageBox.StandardButton.Yes:
            return

        def fn(store, rag) -> str:
            from datetime import datetime, timedelta, timezone
            cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
            removed = store.expire_older_than(cutoff)
            rag.delete_older_than(cutoff)
            return f"Deleted {removed} event(s) older than {days}d."

        self._run_store_op(f"Deleting events older than {days}d", fn)

    def _purge_all(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if QMessageBox.warning(
            self, "Purge everything",
            "Delete ALL stored events and embeddings? This cannot be undone.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        def fn(store, rag) -> str:
            removed = store.purge_all()
            rag.purge_all()
            return f"Purged everything ({removed} event(s) removed)."

        self._run_store_op("Purging the knowledge base", fn)


class _PullSignals(QObject):
    status = Signal(str)
    result = Signal(str, int)   # text, screens captured
    error = Signal(str)


class PullPage(QWidget):
    """Read a file out of the remote VS Code into a local viewer.

    Clipboard copy-OUT is blocked on this VDI (DLP), so the default channel is OCR:
    open the file, then scroll + OCR page-by-page and stitch the captures. OCR is LOSSY
    (confuses 1/l/I, drops indentation) — the result is flagged 'verify before trusting'.
    An optional 'lossless clipboard' path is offered for VDIs where copy-out is allowed.

    Drives the remote (scroll/type), so it's gated behind [control].enabled like Remote.
    """

    def __init__(self, config: dict, api_key: str = "", pulled: dict | None = None) -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._busy = False
        # Shared {remote_path: pulled_text} so the Push tab can diff against what was pulled.
        self._pulled = pulled if pulled is not None else {}
        self._pull_path = ""

        self._sig = _PullSignals()
        self._sig.status.connect(self._set_status)
        self._sig.result.connect(self._on_result)
        self._sig.error.connect(self._on_error)

        page, body = _page_scaffold(
            "Pull / read screen",
            "Read whatever is on the remote screen (email, a doc, VS Code…) into a local "
            "viewer via OCR — lossy, so verify before trusting. By default it just reads; "
            "it never types into the remote unless you ask it to open a file first.",
        )

        if not self._control_enabled:
            warn, _ = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to pull from the remote.",
            )
            body.addWidget(warn)

        # Pull controls
        ctl_card, cl = _card(
            "Read the remote screen",
            "Click Pull to read what's currently on screen, top-to-bottom. Nothing is "
            "typed into the remote.",
        )

        # Opt-in: open a VS Code file first. OFF by default — when on, it sends Ctrl+P and
        # TYPES the path, which only makes sense if VS Code is the focused remote app.
        self.open_first = QCheckBox("Open a file in VS Code first (sends Ctrl+P, then types)")
        self.open_first.setToolTip(
            "Leave OFF to just read the current screen. Turn ON only when the remote's "
            "focused app is VS Code — otherwise the keystrokes go into whatever app is "
            "focused (e.g. typing into an email)."
        )
        cl.addWidget(self.open_first)
        self.path = QLineEdit()
        self.path.setPlaceholderText("src/app.py  (only used when the box above is checked)")
        self.path.setEnabled(False)
        cl.addWidget(self.path)
        self.open_first.toggled.connect(self.path.setEnabled)

        opt_row = QHBoxLayout()
        opt_row.addWidget(QLabel("Max screens:"))
        self.max_screens = QSpinBox()
        self.max_screens.setRange(1, 60)
        self.max_screens.setValue(20)
        opt_row.addWidget(self.max_screens)
        opt_row.addSpacing(16)
        opt_row.addWidget(QLabel("Scroll step:"))
        self.scroll_step = QSpinBox()
        self.scroll_step.setRange(1, 40)
        self.scroll_step.setValue(15)
        self.scroll_step.setToolTip(
            "Mouse-wheel notches per scroll. Bigger = fewer passes to reach the bottom; "
            "too big can skip content. Overlap between passes is de-duplicated."
        )
        opt_row.addWidget(self.scroll_step)
        opt_row.addSpacing(16)
        self.lossless = QCheckBox("Try lossless clipboard copy first")
        self.lossless.setToolTip(
            "Only works where Horizon clipboard copy-out is enabled; falls back to OCR."
        )
        opt_row.addWidget(self.lossless)
        opt_row.addStretch(1)
        cl.addLayout(opt_row)

        self.btn_pull = QPushButton("Pull")
        self.btn_pull.setObjectName("Primary")
        self.btn_pull.clicked.connect(self._pull)
        cl.addWidget(self.btn_pull, 0, Qt.AlignLeft)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        cl.addWidget(self.status)
        body.addWidget(ctl_card)

        # Result
        res_card, rl = _card("Pulled text")
        self.warn = QLabel("⚠ OCR is lossy — verify before trusting, especially code.")
        self.warn.setObjectName("Dim")
        self.warn.setWordWrap(True)
        self.warn.setVisible(False)
        rl.addWidget(self.warn)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setFont(QFont("Consolas", 10))
        self.out.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.out.setMinimumHeight(220)
        rl.addWidget(self.out)
        btn_row = QHBoxLayout()
        self.btn_save = QPushButton("Save to file…")
        self.btn_save.setEnabled(False)
        self.btn_save.clicked.connect(self._save)
        self.btn_clear = QPushButton("Clear")
        self.btn_clear.clicked.connect(self._clear)
        btn_row.addWidget(self.btn_save)
        btn_row.addWidget(self.btn_clear)
        btn_row.addStretch(1)
        rl.addLayout(btn_row)
        body.addWidget(res_card, 1)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            self.btn_pull.setEnabled(False)
            self.path.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _controller(self, client):
        ctl = self._config.get("control", {})
        from src.controller import RemoteController
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
            screen=ctl.get("screen", 0),
        )

    def _pull(self) -> None:
        if self._busy or not self._control_enabled:
            return
        # Only open (and TYPE) a file path when the user explicitly opted in.
        path = self.path.text().strip() if self.open_first.isChecked() else ""
        self._pull_path = path
        max_screens = self.max_screens.value()
        scroll_step = self.scroll_step.value()
        lossless = self.lossless.isChecked()
        self._busy = True
        self.btn_pull.setEnabled(False)
        self.btn_save.setEnabled(False)
        threading.Thread(
            target=self._run, args=(path, max_screens, scroll_step, lossless), daemon=True
        ).start()

    def _run(self, path: str, max_screens: int, scroll_step: int, lossless: bool) -> None:
        async def go() -> tuple[str, int]:
            from src.mcp_client import HorizonMCPClient
            cfg = self._config
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = self._controller(client)
                if path:
                    self._sig.status.emit(f"Opening {path} in VS Code…")
                    await c.open_file(path)
                # Lossless clipboard copy first (works only if copy-out is enabled).
                if lossless:
                    self._sig.status.emit("Trying lossless clipboard copy…")
                    try:
                        return await c.copy_from_remote(), 0
                    except Exception as exc:  # noqa: BLE001 — fall back to OCR
                        self._sig.status.emit(f"Clipboard copy unavailable ({exc}); OCR…")
                self._sig.status.emit("Reading the screen via OCR (scroll-stitching)…")
                sx, sy = await c.screen_center()
                return await c.read_scrolling(
                    sx, sy, max_screens=max_screens, scroll_amount=scroll_step
                )

        try:
            import asyncio
            text, screens = asyncio.run(go())
            self._sig.result.emit(text, screens)
        except BaseException as exc:  # noqa: BLE001 — unwrap TaskGroup groups too
            self._sig.error.emit(_explain_exc(exc))

    def _on_result(self, text: str, screens: int) -> None:
        self._busy = False
        self.btn_pull.setEnabled(True)
        self.out.setPlainText(text)
        ocr = screens > 0
        self.warn.setVisible(ocr)
        self.btn_save.setEnabled(bool(text))
        if self._pull_path and text:
            self._pulled[self._pull_path] = text   # baseline for the Push tab's diff
        how = f"OCR · {screens} screen(s)" if ocr else "lossless clipboard"
        self._set_status(f"Pulled {len(text)} chars ({how}).")

    def _on_error(self, msg: str) -> None:
        self._busy = False
        self.btn_pull.setEnabled(True)
        self._set_status(f"Pull failed: {msg}")

    def _clear(self) -> None:
        """Reset the result area to start a fresh read."""
        if self._busy:
            return
        self.out.clear()
        self.warn.setVisible(False)
        self.btn_save.setEnabled(False)
        self._set_status("Cleared — ready for a new read.")

    def _save(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        text = self.out.toPlainText()
        if not text:
            return
        suggested = (self.path.text().strip().split("/")[-1] or "pulled.txt")
        fname, _ = QFileDialog.getSaveFileName(self, "Save pulled text", suggested)
        if not fname:
            return
        try:
            with open(fname, "w", encoding="utf-8") as fh:
                fh.write(text)
            self._set_status(f"Saved {len(text)} chars → {fname}")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Save failed: {exc}")


class _PushSignals(QObject):
    status = Signal(str)
    done = Signal(str)     # success message
    error = Signal(str)


class PushPage(QWidget):
    """Write a local/edited file back into the remote VS Code.

    The reliable write channel is paste (type_text fights autocomplete/auto-indent):
    open the target, select-all, paste the whole document, optionally save. Before any
    write you see a unified diff vs the last pulled version of that file and must Confirm,
    so a real file is never silently clobbered. Gated behind [control].enabled.
    """

    def __init__(self, config: dict, api_key: str = "", pulled: dict | None = None) -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._pulled = pulled if pulled is not None else {}
        self._busy = False

        self._sig = _PushSignals()
        self._sig.status.connect(self._set_status)
        self._sig.done.connect(self._on_done)
        self._sig.error.connect(self._on_error)

        page, body = _page_scaffold(
            "Push code",
            "Send a new or AI-edited file back into the remote VS Code. You review a diff "
            "and confirm before anything is overwritten.",
        )

        if not self._control_enabled:
            warn, _ = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to push to the remote.",
            )
            body.addWidget(warn)

        # What to push
        src_card, sl = _card(
            "What to push",
            "Load a local file or paste/type the content below — this exact text replaces "
            "the remote document.",
        )
        load_row = QHBoxLayout()
        self.local_path = QLineEdit()
        self.local_path.setPlaceholderText("local file to load (optional)…")
        load_btn = QPushButton("Load…")
        load_btn.clicked.connect(self._load)
        load_row.addWidget(self.local_path, 1)
        load_row.addWidget(load_btn)
        sl.addLayout(load_row)
        self.editor = QPlainTextEdit()
        self.editor.setFont(QFont("Consolas", 10))
        self.editor.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.editor.setMinimumHeight(150)
        self.editor.setPlaceholderText("Content to push…")
        sl.addWidget(self.editor)
        body.addWidget(src_card)

        # Where to push
        dst_card, dl = _card(
            "Where to push",
            "Remote path to open before pasting (blank = paste into the current editor).",
        )
        self.remote_path = QLineEdit()
        self.remote_path.setPlaceholderText("src/app.py")
        dl.addWidget(self.remote_path)
        self.save_after = QCheckBox("Save after paste (Ctrl+S)")
        self.save_after.setChecked(True)
        dl.addWidget(self.save_after)
        body.addWidget(dst_card)

        # Diff + push
        act_card, al = _card("Review & push")
        btn_row = QHBoxLayout()
        self.btn_diff = QPushButton("Preview diff")
        self.btn_diff.clicked.connect(self._preview_diff)
        self.btn_push = QPushButton("Push to remote")
        self.btn_push.setObjectName("Primary")
        self.btn_push.clicked.connect(self._push)
        btn_row.addWidget(self.btn_diff)
        btn_row.addWidget(self.btn_push)
        btn_row.addStretch(1)
        al.addLayout(btn_row)
        self.diff = QPlainTextEdit()
        self.diff.setReadOnly(True)
        self.diff.setFont(QFont("Consolas", 10))
        self.diff.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.diff.setMinimumHeight(150)
        al.addWidget(self.diff)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        al.addWidget(self.status)
        body.addWidget(act_card, 1)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            self.btn_push.setEnabled(False)

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _load(self) -> None:
        from PySide6.QtWidgets import QFileDialog
        fname, _ = QFileDialog.getOpenFileName(self, "Load file to push")
        if not fname:
            return
        try:
            with open(fname, encoding="utf-8") as fh:
                self.editor.setPlainText(fh.read())
            self.local_path.setText(fname)
            if not self.remote_path.text().strip():
                self.remote_path.setText(fname.replace("\\", "/").split("/")[-1])
            self._set_status(f"Loaded {fname}.")
        except Exception as exc:  # noqa: BLE001
            self._set_status(f"Load failed: {exc}")

    def _baseline(self) -> tuple[str, bool]:
        """Last-pulled text for the remote path, and whether one was found."""
        key = self.remote_path.text().strip()
        if key in self._pulled:
            return self._pulled[key], True
        return "", False

    def _diff_lines(self) -> tuple[list[str], int, int, bool]:
        import difflib
        base, found = self._baseline()
        new = self.editor.toPlainText()
        a = base.splitlines()
        b = new.splitlines()
        diff = list(difflib.unified_diff(a, b, "pulled", "to push", lineterm=""))
        added = sum(1 for d in diff if d.startswith("+") and not d.startswith("+++"))
        removed = sum(1 for d in diff if d.startswith("-") and not d.startswith("---"))
        return diff, added, removed, found

    def _preview_diff(self) -> None:
        diff, added, removed, found = self._diff_lines()
        if not self.editor.toPlainText():
            self._set_status("Nothing to push — the content is empty.")
            return
        base_note = "vs last pulled" if found else "no pulled baseline — all new"
        self.diff.setPlainText(
            "\n".join(diff) if diff else "(identical to the pulled version)"
        )
        self._set_status(f"+{added} / -{removed} lines ({base_note}).")

    def _push(self) -> None:
        from PySide6.QtWidgets import QMessageBox
        if self._busy or not self._control_enabled:
            return
        text = self.editor.toPlainText()
        if not text:
            self._set_status("Nothing to push — the content is empty.")
            return
        target = self.remote_path.text().strip()
        save = self.save_after.isChecked()
        _diff, added, removed, found = self._diff_lines()
        where = f"open '{target}' then " if target else "the current remote editor — "
        base_note = "" if found else "  (no pulled baseline to compare against)"
        if QMessageBox.warning(
            self, "Confirm push",
            f"{where}select-all and paste {len(text)} chars"
            f"{' then save' if save else ''}.\n\n"
            f"Changes vs pulled: +{added} / -{removed} lines.{base_note}\n\n"
            "This overwrites the remote document. Continue?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.Cancel,
            QMessageBox.StandardButton.Cancel,
        ) != QMessageBox.StandardButton.Yes:
            return

        self._busy = True
        self.btn_push.setEnabled(False)
        threading.Thread(
            target=self._run, args=(text, target, save), daemon=True
        ).start()

    def _run(self, text: str, target: str, save: bool) -> None:
        async def go() -> None:
            from src.mcp_client import HorizonMCPClient
            from src.controller import RemoteController
            cfg = self._config
            ctl = cfg.get("control", {})
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = RemoteController(
                    client,
                    focus_target=ctl.get("focus_target", "PVDI"),
                    launch_wait=ctl.get("launch_wait_seconds", 1.5),
                    clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
                    copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
                    screen=ctl.get("screen", 0),
                )
                if target:
                    self._sig.status.emit(f"Opening {target} in the remote…")
                    await c.open_file(target)
                self._sig.status.emit("Pasting the document into the remote…")
                await c.paste_to_remote(text, replace_all=True, save=save)

        try:
            import asyncio
            asyncio.run(go())
            self._sig.done.emit(
                f"Pushed {len(text)} chars{' and saved' if save else ''}."
            )
        except BaseException as exc:  # noqa: BLE001 — unwrap TaskGroup groups too
            self._sig.error.emit(_explain_exc(exc))

    def _on_done(self, msg: str) -> None:
        self._busy = False
        self.btn_push.setEnabled(True)
        self._set_status(msg)

    def _on_error(self, msg: str) -> None:
        self._busy = False
        self.btn_push.setEnabled(True)
        self._set_status(f"Push failed: {msg}")


# ---------------------------------------------------------------- Settings tab

def _toml_scalar(value) -> str:
    """Format a Python value as a TOML literal (scalars + string lists)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, list):
        inner = ", ".join(_toml_scalar(str(v)) for v in value)
        return f"[{inner}]"
    s = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{s}"'


def _update_toml(path: str, updates: dict[tuple[str, str], object]) -> None:
    """Surgically set `key = value` lines in-place, preserving comments/layout.

    `updates` maps (section, key) -> Python value. Only the matching line's value is
    rewritten (its trailing inline comment is kept). Missing keys are appended under
    their section header (added at end if the section itself is absent).
    """
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(updates)

    section = ""
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            section = stripped[1:-1]
            continue
        for (sec, key) in list(remaining):
            if sec == section and re.match(rf"^\s*{re.escape(key)}\s*=", line):
                hashidx = line.find("#")
                comment = "  " + line[hashidx:].rstrip("\n") if hashidx != -1 else ""
                indent = line[: len(line) - len(line.lstrip())]
                val = _toml_scalar(remaining.pop((sec, key)))
                lines[i] = f"{indent}{key} = {val}{comment}\n"

    # Append anything not found, grouped by section.
    if remaining:
        existing = {l.strip()[1:-1] for l in lines
                    if l.strip().startswith("[") and l.strip().endswith("]")}
        by_section: dict[str, list[str]] = {}
        for (sec, key), value in remaining.items():
            by_section.setdefault(sec, []).append(f"{key} = {_toml_scalar(value)}\n")
        tail = []
        for sec, kvs in by_section.items():
            if sec and sec not in existing:
                tail.append(f"\n[{sec}]\n")
            tail.extend(kvs)
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.extend(tail)

    p.write_text("".join(lines), encoding="utf-8")


def _update_env(path: str, updates: dict[str, str]) -> None:
    """Set KEY=value lines in a flat .env, updating existing keys or appending new ones."""
    p = Path(path)
    lines = p.read_text(encoding="utf-8").splitlines(keepends=True) if p.exists() else []
    seen = set()
    for i, line in enumerate(lines):
        m = re.match(r"^\s*([A-Za-z0-9_]+)\s*=", line)
        if m and m.group(1) in updates:
            lines[i] = f"{m.group(1)}={updates[m.group(1)]}\n"
            seen.add(m.group(1))
    for k, v in updates.items():
        if k not in seen:
            lines.append(f"{k}={v}\n")
    if lines and not lines[-1].endswith("\n"):
        lines[-1] += "\n"
    p.write_text("".join(lines), encoding="utf-8")


# .env keys surfaced in Settings: (env var, label).
_ENV_KEYS = [
    ("ANTHROPIC_API_KEY", "Anthropic API key"),
    ("VOYAGE_API_KEY", "Voyage API key (optional)"),
    ("HORIZON_PASSWORD", "Horizon unlock password"),
    ("TELEGRAM_BOT_TOKEN", "Telegram bot token"),
    ("TELEGRAM_CHAT_ID", "Telegram chat id"),
]


class SettingsPage(QWidget):
    """Edit config.toml settings and .env secrets in-app.

    config.toml edits are surgical (comments preserved). Secrets are write-only — the
    current value is never shown; a blank field keeps it, typing a value replaces it.
    Most changes take effect on the next restart (the config is read at startup).
    """

    def __init__(self, config: dict) -> None:
        super().__init__()
        self._config = config
        # config.toml / .env live beside the .exe when frozen, else the repo root —
        # the same location main.py reads them from.
        import sys
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path.cwd()
        self.CONFIG_PATH = str(base / "config.toml")
        self.ENV_PATH = str(base / ".env")

        page, body = _page_scaffold(
            "Settings",
            "Tune the monitor and manage credentials. Most changes apply after a restart.",
        )

        poll = config.get("polling", {})
        win = config.get("windows", {})
        ret = config.get("retention", {})
        user = config.get("user", {})
        notif = config.get("notifications", {})
        control = config.get("control", {})

        # Models & cost — which models we call and the running session estimate.
        from src.usage import TRACKER
        cost_card, cost_lay = _card(
            "Models & cost (this session)",
            "Which Claude models the app calls, and a rough running estimate of what this "
            "session has cost so far. Estimate only — verify against your Anthropic billing.",
        )
        cl = config.get("claude", {})
        self.vision_model = self._model_combo(
            cost_lay, "Monitor (vision):", cl.get("vision_model", "claude-haiku-4-5-20251001"))
        self.query_model = self._model_combo(
            cost_lay, "Ask (query):", cl.get("query_model", "claude-sonnet-4-6"))
        self.assist_model = self._model_combo(
            cost_lay, "Assist (computer-use):",
            config.get("assist", {}).get("model", "claude-opus-4-8"))
        self.usage_label = QLabel("No API calls yet this session.")
        self.usage_label.setWordWrap(True)
        cost_lay.addWidget(self.usage_label)
        body.addWidget(cost_card)
        self._usage_timer = QTimer(self)
        self._usage_timer.timeout.connect(self._refresh_usage)
        self._usage_timer.start(3000)   # cheap snapshot read; updates while the app runs
        self._refresh_usage()

        # Monitoring
        mon_card, ml = _card("Monitoring")
        self.interval = self._spin(ml, "Poll interval", poll.get("interval_seconds", 3),
                                   1, 120, " s")
        self.threshold = self._spin(ml, "Change threshold (0–64)",
                                    poll.get("change_threshold", 10), 0, 64, "")
        self.retain = self._spin(ml, "Keep data for (0 = forever)",
                                 ret.get("retain_days", 0), 0, 3650, " days")
        titles_row = QHBoxLayout()
        titles_row.addWidget(QLabel("Window titles:"))
        self.titles = QLineEdit(", ".join(win.get("monitor_titles", [])))
        self.titles.setPlaceholderText("PVDI, horizon-client")
        titles_row.addWidget(self.titles, 1)
        ml.addLayout(titles_row)
        body.addWidget(mon_card)

        # Behaviour
        beh_card, bl = _card("Behaviour")
        name_row = QHBoxLayout()
        name_row.addWidget(QLabel("Your display name:"))
        self.display_name = QLineEdit(user.get("display_name", ""))
        name_row.addWidget(self.display_name, 1)
        bl.addLayout(name_row)
        self.cooldown = self._spin(bl, "Re-alert cooldown",
                                   notif.get("cooldown_minutes", 5), 0, 240, " min")
        self.control_enabled = QCheckBox("Enable remote control (unlock, keep-awake, push)")
        self.control_enabled.setChecked(bool(control.get("enabled", False)))
        bl.addWidget(self.control_enabled)
        self.auto_start = QCheckBox(
            "Auto-start monitoring on launch (off = you press Start — avoids surprise cost)"
        )
        self.auto_start.setChecked(bool(config.get("capture", {}).get("auto_start", False)))
        bl.addWidget(self.auto_start)
        body.addWidget(beh_card)

        # Secrets
        sec_card, scl = _card(
            "Credentials (.env)",
            "Stored locally in .env, never displayed. Leave a field blank to keep the "
            "current value; type to replace it.",
        )
        self._env_edits: dict[str, QLineEdit] = {}
        for var, label in _ENV_KEYS:
            row = QHBoxLayout()
            tag = "set" if os.environ.get(var) else "not set"
            lbl = QLabel(f"{label}:")
            lbl.setMinimumWidth(200)
            edit = QLineEdit()
            edit.setEchoMode(QLineEdit.EchoMode.Password)
            edit.setPlaceholderText(f"({tag} — blank to keep)")
            self._env_edits[var] = edit
            row.addWidget(lbl)
            row.addWidget(edit, 1)
            scl.addLayout(row)
        body.addWidget(sec_card)

        # Save
        self.btn_save = QPushButton("Save settings")
        self.btn_save.setObjectName("Primary")
        self.btn_save.clicked.connect(self._save)
        body.addWidget(self.btn_save, 0, Qt.AlignLeft)
        self.status = QLabel("")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        body.addWidget(self.status)

        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

    def _refresh_usage(self) -> None:
        from src.usage import TRACKER
        snap = TRACKER.snapshot()
        if not snap["models"]:
            self.usage_label.setText("No API calls yet this session.")
            return
        parts = []
        for m in snap["models"]:
            name = m["model"].replace("claude-", "")
            tin = m["input"] + m["cache_read"] + m["cache_write"]
            cost = f"~${m['cost']:,.3f}" if m["priced"] else "price n/a"
            parts.append(
                f"{name}: {m['calls']} calls · {tin:,} in / {m['output']:,} out · {cost}"
            )
        parts.append(
            f"— Total: {snap['total_calls']} calls · {snap['total_tokens']:,} tokens "
            f"({snap['total_input']:,} in / {snap['total_output']:,} out) · "
            f"~${snap['total_cost']:,.3f} this session"
        )
        self.usage_label.setText("\n".join(parts))

    # Selectable models (cheapest → priciest), with their price shown in the dropdown.
    _MODELS = [
        ("claude-haiku-4-5-20251001", "Haiku 4.5"),
        ("claude-sonnet-4-6", "Sonnet 4.6"),
        ("claude-opus-4-8", "Opus 4.8"),
    ]

    def _model_combo(self, layout, label: str, current: str) -> QComboBox:
        from src.usage import TRACKER
        row = QHBoxLayout()
        lbl = QLabel(label)
        lbl.setMinimumWidth(170)
        combo = QComboBox()
        for mid, name in self._MODELS:
            r = TRACKER.rate(mid)
            price = f"  —  ${r[0]:g} / ${r[1]:g} per 1M" if r else ""
            combo.addItem(f"{name}{price}", mid)
        idx = combo.findData(current)
        if idx < 0:                                   # a model not in our list — keep it
            combo.addItem(f"{current} (custom)", current)
            idx = combo.count() - 1
        combo.setCurrentIndex(idx)
        row.addWidget(lbl)
        row.addWidget(combo, 1)
        layout.addLayout(row)
        return combo

    def _spin(self, layout, label, value, lo, hi, suffix) -> QSpinBox:
        row = QHBoxLayout()
        row.addWidget(QLabel(label + ":"))
        sp = QSpinBox()
        sp.setRange(lo, hi)
        sp.setValue(int(value))
        if suffix:
            sp.setSuffix(suffix)
        row.addWidget(sp)
        row.addStretch(1)
        layout.addLayout(row)
        return sp

    def _save(self) -> None:
        titles = [t.strip() for t in self.titles.text().split(",") if t.strip()]
        toml_updates = {
            ("polling", "interval_seconds"): self.interval.value(),
            ("polling", "change_threshold"): self.threshold.value(),
            ("retention", "retain_days"): self.retain.value(),
            ("windows", "monitor_titles"): titles,
            ("user", "display_name"): self.display_name.text().strip(),
            ("notifications", "cooldown_minutes"): self.cooldown.value(),
            ("control", "enabled"): self.control_enabled.isChecked(),
            ("capture", "auto_start"): self.auto_start.isChecked(),
            ("claude", "vision_model"): self.vision_model.currentData(),
            ("claude", "query_model"): self.query_model.currentData(),
            ("assist", "model"): self.assist_model.currentData(),
        }
        env_updates = {
            var: edit.text() for var, edit in self._env_edits.items() if edit.text()
        }
        try:
            _update_toml(self.CONFIG_PATH, toml_updates)
            if env_updates:
                _update_env(self.ENV_PATH, env_updates)
                for var, val in env_updates.items():
                    os.environ[var] = val   # apply secrets live; config needs a restart
                    self._env_edits[var].clear()
                    self._env_edits[var].setPlaceholderText("(set — blank to keep)")
            saved = "config.toml" + (" + .env" if env_updates else "")
            self.status.setText(f"Saved {saved}. Restart to apply config changes.")
        except Exception as exc:  # noqa: BLE001
            self.status.setText(f"Save failed: {exc}")


# --------------------------------------------------------------------- Ask tab

class _AskSignals(QObject):
    chunk = Signal(str)
    done = Signal()
    error = Signal(str)


class AskPage(QWidget):
    """Functional Q&A over the accumulated RAG store (Claude Sonnet, streamed)."""

    def __init__(self, config: dict, api_key: str) -> None:
        super().__init__()
        self._config = config
        self._api_key = api_key
        self._rag = None
        self._sig = _AskSignals()
        self._sig.chunk.connect(self._append)
        self._sig.done.connect(self._on_done)
        self._sig.error.connect(lambda e: (self._append(f"\n[Error: {e}]\n"), self._on_done()))

        page, body = _page_scaffold(
            "Ask",
            "Natural-language questions over everything collected so far.",
        )
        card, lay = _card()
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText("What did anyone say about the deployment this morning?")
        self.send = QPushButton("Ask")
        self.send.setObjectName("Primary")
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        lay.addLayout(row)
        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMinimumHeight(280)
        lay.addWidget(self.out)
        body.addWidget(card, 1)
        QVBoxLayout(self).addWidget(page)

        self.send.clicked.connect(self._ask)
        self.input.returnPressed.connect(self._ask)

    def _append(self, text: str) -> None:
        self.out.insertPlainText(text)
        self.out.ensureCursorVisible()

    def _on_done(self) -> None:
        self.send.setEnabled(True)
        self.input.setEnabled(True)
        self.input.setFocus()

    def _ask(self) -> None:
        q = self.input.text().strip()
        if not q or not self.send.isEnabled():
            return
        self.input.clear()
        self.out.setPlainText(f"Q: {q}\n\nA: ")
        self.send.setEnabled(False)
        self.input.setEnabled(False)
        threading.Thread(target=self._run, args=(q,), daemon=True).start()

    def _run(self, q: str) -> None:
        try:
            from src.rag import RAGPipeline
            from src.agent import QueryAgent
            if self._rag is None:
                cfg = self._config["rag"]
                import os
                self._rag = RAGPipeline(
                    db_path=cfg["db_path"],
                    collection_name=cfg["collection_name"],
                    embedding_provider=cfg["embedding_provider"],
                    voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
                    top_k=cfg["top_k"],
                )
                self._rag.connect()
            agent = QueryAgent(
                api_key=self._api_key,
                model=self._config["claude"]["query_model"],
                rag=self._rag,
                max_tokens=self._config["claude"]["max_tokens"],
            )
            agent.query(q, on_chunk=lambda t: self._sig.chunk.emit(t))
            self._sig.done.emit()
        except Exception as exc:  # noqa: BLE001 — surface any failure in the UI
            self._sig.error.emit(str(exc))


# ------------------------------------------------------------------ Assist tab

class _AssistSignals(QObject):
    """Marshal the agent's worker-thread callbacks onto the Qt UI thread."""
    text = Signal(str)
    action = Signal(str)
    result = Signal(str)
    status = Signal(str)
    finished = Signal()
    error = Signal(str)


class AssistPage(QWidget):
    """Direct help: describe a task, the agent looks at the remote screen and does it.

    Advise mode is read-only (suggests the steps); Act mode performs them. The "Confirm"
    selector controls Act's approval level — by default clicks/scrolls run automatically
    and only typing/keys are confirmed; "Every step" confirms all; "run freely" confirms
    nothing. Wraps src.assistant.ComputerUseAgent on a worker thread.
    """

    def __init__(self, config: dict, api_key: str) -> None:
        super().__init__()
        self._config = config
        self._api_key = api_key
        self._agent = None          # built lazily on first run
        self._running = False

        self._sig = _AssistSignals()
        self._sig.text.connect(lambda s: self._say("Assistant", s))
        self._sig.action.connect(self._on_action)
        self._sig.result.connect(lambda s: self._say("·", s))
        self._sig.status.connect(self._set_status)
        self._sig.finished.connect(self._on_finished)
        self._sig.error.connect(lambda e: (self._say("Error", e), self._on_finished()))

        # Direct (non-scrolling) layout so the input row stays pinned and visible — the
        # transcript scrolls internally instead of being pushed off-screen by a stretchy
        # transcript inside an outer scroll area.
        body = QVBoxLayout(self)
        body.setContentsMargins(24, 20, 24, 20)
        body.setSpacing(14)
        _title = QLabel("Assist")
        _title.setObjectName("PageTitle")
        _subtitle = QLabel(
            "Describe a task on the remote desktop in plain language. The agent looks at "
            "the screen and helps — read-only advice, or hands-on with your confirmation."
        )
        _subtitle.setObjectName("PageSubtitle")
        _subtitle.setWordWrap(True)
        body.addWidget(_title)
        body.addWidget(_subtitle)

        # Mode + status
        mode_card, mode_lay = _card()
        mode_row = QHBoxLayout()
        self.mode_advise = QRadioButton("Advise (read-only)")
        self.mode_act = QRadioButton("Act (hands-on)")
        self.mode_advise.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.mode_advise)
        grp.addButton(self.mode_act)
        mode_row.addWidget(self.mode_advise)
        mode_row.addWidget(self.mode_act)
        mode_row.addSpacing(10)
        mode_row.addWidget(QLabel("Confirm:"))
        self.act_approval = QComboBox()
        self.act_approval.addItem("Typing & risky steps", "guarded")
        self.act_approval.addItem("Every step", "every")
        self.act_approval.addItem("Nothing — run freely", "all")
        self.act_approval.setToolTip(
            "In Act mode, clicks/scrolls run automatically (their coordinates tell you "
            "nothing). This sets what still needs your OK. Either way, the assistant pauses "
            "to ask before destructive actions, and Stop always works."
        )
        self.act_approval.setEnabled(False)
        self.mode_act.toggled.connect(self.act_approval.setEnabled)
        mode_row.addWidget(self.act_approval)
        mode_row.addStretch(1)
        self.new_btn = QPushButton("New session")
        self.new_btn.setMaximumWidth(120)
        self.new_btn.clicked.connect(self._new_session)
        mode_row.addWidget(self.new_btn)
        mode_lay.addLayout(mode_row)
        self.status = QLabel("Ready.")
        self.status.setObjectName("Dim")
        mode_lay.addWidget(self.status)
        body.addWidget(mode_card)

        # Transcript
        chat_card, chat_lay = _card()
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setMinimumHeight(200)
        chat_lay.addWidget(self.transcript)

        # Confirmation bar (hidden until an action is proposed in Act mode)
        self.confirm_bar = QFrame()
        self.confirm_bar.setProperty("class", "card")
        cb = QHBoxLayout(self.confirm_bar)
        cb.setContentsMargins(12, 8, 12, 8)
        self.confirm_label = QLabel()
        self.confirm_label.setWordWrap(True)
        cb.addWidget(self.confirm_label, 1)
        self.btn_confirm = QPushButton("Confirm")
        self.btn_confirm.setObjectName("Primary")
        self.btn_skip = QPushButton("Skip")
        self.btn_stop_action = QPushButton("Stop")
        self.btn_confirm.clicked.connect(lambda: self._resolve("confirm"))
        self.btn_skip.clicked.connect(lambda: self._resolve("skip"))
        self.btn_stop_action.clicked.connect(lambda: self._resolve("stop"))
        for b in (self.btn_confirm, self.btn_skip, self.btn_stop_action):
            cb.addWidget(b)
        self.confirm_bar.setVisible(False)
        chat_lay.addWidget(self.confirm_bar)
        body.addWidget(chat_card, 1)

        # Input row
        in_card, in_lay = _card()
        row = QHBoxLayout()
        self.input = QLineEdit()
        self.input.setPlaceholderText(
            "e.g. Help me switch from this feature branch to master — do not stash my changes"
        )
        self.send = QPushButton("Send")
        self.send.setObjectName("Primary")
        self.stop = QPushButton("Stop")
        self.stop.setEnabled(False)
        row.addWidget(self.input, 1)
        row.addWidget(self.send)
        row.addWidget(self.stop)
        in_lay.addLayout(row)
        body.addWidget(in_card)

        self.send.clicked.connect(self._send)
        self.input.returnPressed.connect(self._send)
        self.stop.clicked.connect(self._stop_run)

    # ----------------------------------------------------------- transcript

    def _say(self, who: str, text: str) -> None:
        self.transcript.appendPlainText(f"{who}: {text}" if who else text)
        self.transcript.ensureCursorVisible()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    # ----------------------------------------------------------- run control

    def _ensure_agent(self):
        if self._agent is None:
            from src.assistant import ComputerUseAgent
            agent = ComputerUseAgent(self._config, self._api_key)
            agent.on_text = self._sig.text.emit
            agent.on_action = self._sig.action.emit
            agent.on_result = self._sig.result.emit
            agent.on_status = self._sig.status.emit
            agent.on_finished = self._sig.finished.emit
            agent.on_error = self._sig.error.emit
            self._agent = agent
        return self._agent

    def _send(self) -> None:
        goal = self.input.text().strip()
        if not goal or self._running:
            return
        mode = "act" if self.mode_act.isChecked() else "advise"
        approval = self.act_approval.currentData() or "guarded"
        self.input.clear()
        self._say("You", goal)
        self._set_status("Working…")
        self._running = True
        self.send.setEnabled(False)
        self.input.setEnabled(False)
        self.stop.setEnabled(True)
        agent = self._ensure_agent()
        threading.Thread(
            target=agent.run_turn, args=(goal, mode, approval), daemon=True
        ).start()

    def _stop_run(self) -> None:
        if self._agent is not None:
            self._agent.request_stop()
        self._set_status("Stopping…")

    def _on_finished(self) -> None:
        self._running = False
        self.confirm_bar.setVisible(False)
        self.send.setEnabled(True)
        self.input.setEnabled(True)
        self.stop.setEnabled(False)
        self._set_status("Ready.")
        self.input.setFocus()

    def _new_session(self) -> None:
        if self._running:
            return
        if self._agent is not None:
            self._agent.reset()
        self.transcript.clear()
        self._set_status("New session — ready.")

    # ----------------------------------------------------------- confirmation

    def _on_action(self, desc: str) -> None:
        self._say("▶ Proposes", desc)
        self.confirm_label.setText(f"Perform this action?  {desc}")
        self.confirm_bar.setVisible(True)
        self._set_status("Waiting for your confirmation…")

    def _resolve(self, decision: str) -> None:
        self.confirm_bar.setVisible(False)
        if self._agent is not None:
            self._agent.resolve_confirmation(decision)
        self._set_status("Working…" if decision != "stop" else "Stopping…")


# ------------------------------------------------------------------ Remote tab

class _RemoteSignals(QObject):
    """Marshal control-worker status onto the Qt UI thread."""
    status = Signal(str)
    keepalive = Signal(bool)   # True = running, False = stopped


class RemotePage(QWidget):
    """Drive the Horizon session directly: unlock, keep awake, focus, launch an app.

    These are the opt-in WRITE actions from RemoteController, gated behind
    [control].enabled. Each runs on a worker thread with its own MCP connection
    (mirrors the CLI `remote <action>` and the legacy tray), so the UI never blocks.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        # keep-awake worker handle (anti-idle nudges)
        self._ka_thread: threading.Thread | None = None
        self._ka_stop: threading.Event | None = None

        self._sig = _RemoteSignals()
        self._sig.status.connect(self._set_status)
        self._sig.keepalive.connect(self._reflect_keepalive)

        page, body = _page_scaffold(
            "Remote",
            "Reach into the Horizon session itself — unlock it, stop it locking, bring it "
            "forward, or open an app inside it. These type and click into a corporate "
            "desktop, so they only run when you trigger them.",
        )

        if not self._control_enabled:
            warn, wlay = _card(
                "Remote control is off",
                "Set [control].enabled = true in config.toml to enable these actions.",
            )
            body.addWidget(warn)

        # Status line (shared by every action)
        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)

        # --- Unlock
        unlock_card, ul = _card(
            "Unlock remote desktop",
            "Sends Ctrl+Alt+Del and types your password (HORIZON_PASSWORD in .env). "
            "Duo/MFA is approved on your phone — this only gets you to the prompt and signs in.",
        )
        self.type_only = QCheckBox("Type only — don't press Sign in (verify the entry first)")
        ul.addWidget(self.type_only)
        self.btn_unlock = QPushButton("Unlock remote desktop")
        self.btn_unlock.setObjectName("Primary")
        self.btn_unlock.clicked.connect(self._unlock)
        ul.addWidget(self.btn_unlock, 0, Qt.AlignLeft)
        body.addWidget(unlock_card)

        # --- Keep awake
        ka_interval = float(config.get("control", {}).get("keepalive_interval_seconds", 120))
        ka_card, kal = _card(
            "Keep awake",
            f"Nudges the cursor one pixel every {ka_interval:g}s so the session never idles "
            "into a lock. Each nudge briefly steals local focus — use it when you step away.",
        )
        ka_row = QHBoxLayout()
        self.btn_keepalive = QPushButton("Start")
        self.btn_keepalive.clicked.connect(self._toggle_keepalive)
        self.ka_state = QLabel("Off")
        self.ka_state.setObjectName("Dim")
        ka_row.addWidget(self.btn_keepalive)
        ka_row.addWidget(self.ka_state)
        ka_row.addStretch(1)
        kal.addLayout(ka_row)
        body.addWidget(ka_card)

        # --- Quick actions
        qa_card, qal = _card(
            "Quick actions",
            "Bring the Horizon window forward, or open/activate an app inside the remote "
            "via its Start menu (e.g. Microsoft Teams, Symphony).",
        )
        self.btn_front = QPushButton("Bring Horizon to front")
        self.btn_front.clicked.connect(
            lambda: self._run_control("bring to front", lambda c: c.bring_to_front())
        )
        qal.addWidget(self.btn_front, 0, Qt.AlignLeft)
        app_row = QHBoxLayout()
        self.app_input = QLineEdit()
        self.app_input.setPlaceholderText("App name as in the remote Start menu…")
        self.btn_launch = QPushButton("Launch / activate")
        self.btn_launch.clicked.connect(self._launch)
        self.app_input.returnPressed.connect(self._launch)
        app_row.addWidget(self.app_input, 1)
        app_row.addWidget(self.btn_launch)
        qal.addLayout(app_row)
        body.addWidget(qa_card)

        body.addWidget(self.status)
        body.addStretch(1)
        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            for b in (self.btn_unlock, self.btn_keepalive, self.btn_front, self.btn_launch):
                b.setEnabled(False)
            self.app_input.setEnabled(False)
            self.type_only.setEnabled(False)

    # ------------------------------------------------------------ helpers

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _controller(self, client):
        ctl = self._config.get("control", {})
        from src.controller import RemoteController
        return RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            screen=ctl.get("screen", 0),
        )

    def _run_control(self, label: str, factory) -> None:
        """Run one controller coroutine on a worker thread with a fresh MCP client.

        `factory` takes a RemoteController and returns the coroutine to await.
        """
        if not self._control_enabled:
            self._sig.status.emit("Remote control disabled — set [control].enabled=true.")
            return

        def worker() -> None:
            async def go() -> None:
                from src.mcp_client import HorizonMCPClient
                cfg = self._config
                async with HorizonMCPClient(
                    cfg["mcp"]["server_path"], cfg["mcp"]["command"]
                ) as client:
                    await factory(self._controller(client))

            self._sig.status.emit(f"{label}…")
            try:
                asyncio.run(go())
                self._sig.status.emit(f"{label} — done.")
            except BaseException as exc:  # noqa: BLE001 — unwrap TaskGroup groups too
                self._sig.status.emit(f"{label} failed: {_explain_exc(exc)}")

        threading.Thread(target=worker, daemon=True).start()

    # ------------------------------------------------------------ actions

    def _unlock(self) -> None:
        password = os.environ.get("HORIZON_PASSWORD", "")
        if not password:
            self._sig.status.emit("HORIZON_PASSWORD not set in .env — cannot unlock.")
            return
        submit = not self.type_only.isChecked()
        tail = " (signing in)" if submit else " (type only — press Sign in yourself)"
        self._run_control(
            "unlock" + tail, lambda c: c.unlock(password, submit=submit)
        )

    def _launch(self) -> None:
        app = self.app_input.text().strip()
        if not app:
            return
        self._run_control(
            f"launch/activate '{app}'", lambda c: c.launch_or_activate(app)
        )

    # ------------------------------------------------------------ keep-awake

    def _toggle_keepalive(self) -> None:
        if not self._control_enabled:
            self._sig.status.emit("Remote control disabled — set [control].enabled=true.")
            return
        if self._ka_thread:                       # running -> stop
            if self._ka_stop:
                self._ka_stop.set()
            self._ka_thread = None
            self._sig.keepalive.emit(False)
            self._sig.status.emit("Keep-awake: off.")
        else:                                     # stopped -> start
            self._ka_stop = threading.Event()
            self._ka_thread = threading.Thread(
                target=self._ka_worker, args=(self._ka_stop,), daemon=True
            )
            self._ka_thread.start()
            self._sig.keepalive.emit(True)
            self._sig.status.emit("Keep-awake: on.")

    def _reflect_keepalive(self, running: bool) -> None:
        self.btn_keepalive.setText("Stop" if running else "Start")
        self.ka_state.setText("On — nudging the remote" if running else "Off")

    def _ka_worker(self, stop: threading.Event) -> None:
        """Hold one MCP connection and nudge the remote until `stop` is set."""
        ctl = self._config.get("control", {})
        secs = float(ctl.get("keepalive_interval_seconds", 120.0))

        async def go() -> None:
            from src.mcp_client import HorizonMCPClient
            cfg = self._config
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                c = self._controller(client)
                while not stop.is_set():
                    try:
                        await c.nudge()
                    except Exception as exc:  # noqa: BLE001
                        self._sig.status.emit(f"Keep-awake nudge failed: {exc}")
                    stop.wait(secs)

        try:
            asyncio.run(go())
        except Exception as exc:  # noqa: BLE001
            self._sig.status.emit(f"Keep-awake stopped: {exc}")


# ---------------------------------------------------------------- Backfill tab

class _BackfillSignals(QObject):
    """Marshal ChatBackfiller worker-thread progress onto the Qt UI thread."""
    log = Signal(str)
    status = Signal(str)
    done = Signal(str)


class BackfillPage(QWidget):
    """Import a whole Teams chat's back-history into the knowledge base.

    Unlike the live monitor (which sees only the current screen), this scrolls a
    named conversation newest→oldest and OCRs each frame — free, offline, no Claude
    API cost — then ingests into the same SQLite + ChromaDB stores. Wraps
    src.backfill.ChatBackfiller on a worker thread with its own MCP connection
    (mirrors the CLI `backfill`), gated behind [control].enabled since it scrolls
    the remote.
    """

    def __init__(self, config: dict, api_key: str = "") -> None:
        super().__init__()
        self._config = config
        self._control_enabled = bool(config.get("control", {}).get("enabled", False))
        self._running = False
        self._stop = threading.Event()

        self._sig = _BackfillSignals()
        self._sig.log.connect(self._append)
        self._sig.status.connect(self._set_status)
        self._sig.done.connect(self._on_done)

        page, body = _page_scaffold(
            "Backfill",
            "Import a whole Teams chat's history into the knowledge base. It scrolls the "
            "named chat newest→oldest and reads each frame with free offline OCR — no "
            "Claude API cost. Bring the chat to the front in the remote first.",
        )

        if not self._control_enabled:
            warn, _ = _card(
                "Remote control is off",
                "Backfill scrolls the remote session — set [control].enabled = true in "
                "config.toml to enable it.",
            )
            body.addWidget(warn)

        bf = config.get("backfill", {})
        form, fl = _card(
            "Backfill a chat",
            "The chat title must match the name shown at the top of the conversation.",
        )
        row = QHBoxLayout()
        row.addWidget(QLabel("Chat title"))
        self.chat = QLineEdit()
        self.chat.setPlaceholderText("e.g. ResiDB Support")
        row.addWidget(self.chat, 1)
        row.addWidget(QLabel("Months back"))
        self.months = QSpinBox()
        self.months.setRange(1, 36)
        self.months.setValue(int(bf.get("months", 6)))
        row.addWidget(self.months)
        fl.addLayout(row)

        opt_row = QHBoxLayout()
        self.ingest = QCheckBox("Ingest into the knowledge base")
        self.ingest.setChecked(True)
        self.ingest.setToolTip("Uncheck for a dry run — scroll and parse but store nothing.")
        opt_row.addWidget(self.ingest)
        opt_row.addStretch(1)
        self.run_btn = QPushButton("Run backfill")
        self.run_btn.setObjectName("Primary")
        self.run_btn.clicked.connect(self._run)
        self.stop_btn = QPushButton("Stop")
        self.stop_btn.setEnabled(False)
        self.stop_btn.clicked.connect(self._request_stop)
        opt_row.addWidget(self.run_btn)
        opt_row.addWidget(self.stop_btn)
        fl.addLayout(opt_row)
        body.addWidget(form)

        self.status = QLabel("Ready." if self._control_enabled else "Disabled.")
        self.status.setObjectName("Dim")
        self.status.setWordWrap(True)
        body.addWidget(self.status)

        self.out = QPlainTextEdit()
        self.out.setReadOnly(True)
        self.out.setMinimumHeight(200)
        body.addWidget(self.out, 1)

        QVBoxLayout(self).addWidget(page)

        if not self._control_enabled:
            for w in (self.run_btn, self.chat, self.months, self.ingest):
                w.setEnabled(False)

    # -------------------------------------------------- ui-thread slots
    def _append(self, text: str) -> None:
        self.out.appendPlainText(text)
        self.out.ensureCursorVisible()

    def _set_status(self, text: str) -> None:
        self.status.setText(text)

    def _on_done(self, summary: str) -> None:
        self._running = False
        self.run_btn.setEnabled(True)
        self.stop_btn.setEnabled(False)
        self.chat.setEnabled(True)
        self._append(f"\n✔ {summary}")
        self._set_status("Done.")

    # -------------------------------------------------- actions
    def _request_stop(self) -> None:
        self._stop.set()
        self._set_status("Stopping after the current frame…")

    def _run(self) -> None:
        if self._running or not self._control_enabled:
            return
        title = self.chat.text().strip()
        if not title:
            self._set_status("Enter the chat title first.")
            return
        months = self.months.value()
        ingest = self.ingest.isChecked()
        self._stop.clear()
        self._running = True
        self.run_btn.setEnabled(False)
        self.stop_btn.setEnabled(True)
        self.out.clear()
        self._set_status(f"Backfilling {title!r} — bring the chat to the front…")
        threading.Thread(
            target=self._worker, args=(title, months, ingest), daemon=True
        ).start()

    def _worker(self, title: str, months: int, ingest: bool) -> None:
        async def go() -> None:
            from src.mcp_client import HorizonMCPClient
            from src.backfill import ChatBackfiller
            cfg = self._config
            async with HorizonMCPClient(
                cfg["mcp"]["server_path"], cfg["mcp"]["command"]
            ) as client:
                bf = ChatBackfiller(cfg, on_log=self._sig.log.emit)
                result = await bf.run(
                    client, title, months=months, ingest=ingest,
                    should_stop=self._stop.is_set,
                )
                self._sig.done.emit(result.summary())

        try:
            asyncio.run(go())
        except BaseException as exc:  # noqa: BLE001 — unwrap TaskGroup groups too
            self._sig.log.emit(f"ERROR: {_explain_exc(exc)}")
            self._sig.done.emit("failed")
