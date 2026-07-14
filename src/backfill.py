"""
Backfill a whole Teams chat's back-history into the knowledge base.

The live monitor (poller.py + extractor.py) only sees the *current* screen. This
module imports a conversation's **history**: it scrolls a named Teams chat from
newest to oldest, OCRs each frame (Windows OCR — FREE, offline, no Claude API),
parses the messages, and ingests them into the same SQLite + ChromaDB stores.
Use it once per chat to seed the RAG DB, then let the monitor keep it current.

Why OCR instead of Claude vision here: a 6-month scroll is hundreds of frames;
Haiku on every one would cost real money. Windows OCR is free and good enough for
chat text, and MessageEvent.doc_id() collapses the heavy frame overlap on ingest.

Teams scroll technique (learned against WebView2 on a multi-monitor VDI):
  * MOVE the pointer over the pane before every wheel event — Teams drops wheel
    events when the cursor isn't already over the scroll surface;
  * aim the wheel at a sender-header TEXT line, never an embedded image/table or
    dead space — those eat the wheel and the scroll silently stalls;
  * a stall usually means Teams is lazy-loading older messages, not that we hit
    the top — wait longer and hit the top edge hard before believing we're done.

Entry points:
  ChatBackfiller(config).run(client, "ResiDB Support")   # async, drives the remote
  parse_frame(ocr_json, channel="...")                    # pure, unit-testable
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable

from .models import MessageEvent

# --------------------------------------------------------------------------- #
# OCR frame parsing — pure functions, unit-testable offline against saved OCR. #
# --------------------------------------------------------------------------- #

_MONTHS = {m.lower(): i for i, m in enumerate(
    ["", "January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"])}
_WEEKDAY = (r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday|"
            r"Today|Yesterday)")
_TIME = r"(?:\d{1,2}/\d{1,2}/\d{4}\s+)?(?:\d{1,2}:\d{2}\s*[AaPp][Mm])"
# A speaker header: "Fogarty, Patrick 7:27 AM", "Kevil, Brian Friday 6:02 PM",
# "Goubar, Dmitry(MSUSA-Consultant) 8:31 AM".
_HEADER = re.compile(
    r"^(?P<speaker>[A-Z][\w'’.\-]+,\s*[A-Z][\w'’.\-]+(?:\([^)]*\))?)\s+"
    r"(?:%s\s+)?(?P<time>%s)\s*$" % (_WEEKDAY, _TIME))
# A date separator between message runs (not part of any message).
_SEP = re.compile(
    r"^(?:%s|(?:%s)\s+\d{4}|[A-Za-z]+day,\s+\w+\s+\d{1,2},\s+\d{4})$"
    % (_WEEKDAY, "|".join(list(_MONTHS)[1:])), re.I)
_SEP_MONTH_YEAR = re.compile(r"^(%s)\s+(\d{4})$" % "|".join(list(_MONTHS)[1:]), re.I)
_SEP_FULL_DATE = re.compile(
    r"^[A-Za-z]+day,\s+([A-Za-z]+)\s+\d{1,2},\s+(\d{4})$", re.I)
# "Last, First" — start of a header line; a safe (text) surface to wheel over.
_NAME = re.compile(r"^[A-Z][\w'’.\-]+,\s*[A-Z]")

# UI chrome / hovercard / reaction noise that OCR sweeps up between real bubbles.
_ARTIFACTS = [
    re.compile(r"Press Ctrl\+F to find in this chat", re.I),
    re.compile(r"\bEdited\b"),
    re.compile(r"[åâ]?\s*O\s*99\b"),          # reaction pill counts
    re.compile(r"\bReply\b|\bForward\b|\bReact\b"),
]
_WS = re.compile(r"\s{2,}")


def _clean(msg: str) -> str:
    for rx in _ARTIFACTS:
        msg = rx.sub(" ", msg)
    return _WS.sub(" ", msg).strip()


def _as_dict(ocr: Any) -> dict:
    if isinstance(ocr, str):
        try:
            return json.loads(ocr)
        except (json.JSONDecodeError, TypeError):
            return {}
    return ocr or {}


def lines_sorted(ocr: Any) -> list[dict]:
    """OCR line boxes in reading order (top->bottom, then left->right)."""
    lines = _as_dict(ocr).get("lines", [])
    return sorted(lines, key=lambda l: (l.get("y", 0), l.get("x", 0)))


def frame_rows(ocr: Any) -> list[str]:
    """OCR lines clustered into visual rows (lines within ~8px of y join as one)."""
    rows: list[str] = []
    cur_y: int | None = None
    cur: list[str] = []
    for l in lines_sorted(ocr):
        text = l.get("text", "").strip()
        if not text:
            continue
        y = l.get("y", 0)
        if cur_y is None or abs(y - cur_y) <= 8:
            cur.append(text)
            cur_y = y if cur_y is None else cur_y
        else:
            rows.append("  ".join(cur))
            cur, cur_y = [text], y
    if cur:
        rows.append("  ".join(cur))
    return rows


def parse_frame(ocr: Any, channel: str, user_tokens: tuple[str, ...] = ()) -> list[MessageEvent]:
    """Parse one OCR frame into MessageEvents.

    Teams renders a "Last, First  <time>" header above each speaker's run of
    messages; everything between two headers (or a date separator) is that
    speaker's text. Overlapping frames re-emit the same message — that's fine,
    doc_id() collapses them on ingest.
    """
    events: list[MessageEvent] = []
    speaker: str | None = None
    chat_time = ""
    buf: list[str] = []

    def flush() -> None:
        nonlocal buf
        if speaker and buf:
            msg = _clean(" ".join(buf))
            if len(msg) >= 3 and re.search(r"[A-Za-z]", msg):
                events.append(MessageEvent(
                    speaker=speaker, message=msg[:2000], app="teams",
                    window_title=channel, channel=channel, chat_time=chat_time,
                    directed_at_user=any(u in msg.lower() for u in user_tokens),
                ))
        buf = []

    for row in frame_rows(ocr):
        m = _HEADER.match(row)
        if m:
            flush()
            speaker = re.sub(r"\s+", " ", m.group("speaker")).strip()
            chat_time = m.group("time").strip()
            continue
        if _SEP.match(row):
            flush()
            speaker = None
            continue
        if speaker:
            buf.append(row)
    flush()
    return events


def dedupe(events: list[MessageEvent]) -> list[MessageEvent]:
    """Collapse near-dupes from overlapping frames. OCR jitter changes
    message[:120] so doc_id alone misses them; key on speaker + alnum-normalized
    prefix, keeping the LONGEST (most complete) variant per key."""
    uniq: dict[str, MessageEvent] = {}
    for e in sorted(events, key=lambda e: len(e.message), reverse=True):
        norm = re.sub(r"[^a-z0-9]", "", e.message.lower())[:60]
        if norm:
            uniq.setdefault(f"{e.speaker}|{norm}", e)
    return list(uniq.values())


def _line_ym(text: str) -> tuple[int, int] | None:
    """(year, month) if this line is a Teams date separator, else None."""
    t = text.strip()
    m = _SEP_MONTH_YEAR.match(t)
    if m:
        return int(m.group(2)), _MONTHS[m.group(1).lower()]
    m = _SEP_FULL_DATE.match(t)
    if m and m.group(1).lower() in _MONTHS:
        return int(m.group(2)), _MONTHS[m.group(1).lower()]
    return None


def hit_cutoff(lines: list[dict], cutoff_ym: tuple[int, int]) -> tuple[int, int] | None:
    """The oldest separator on screen if it's older than cutoff_ym, else None."""
    for l in lines:
        ym = _line_ym(l.get("text", ""))
        if ym and ym < cutoff_ym:
            return ym
    return None


def months_ago_ym(months: int, now: datetime | None = None) -> tuple[int, int]:
    now = now or datetime.now(timezone.utc)
    y, m = now.year, now.month - months
    while m <= 0:
        m += 12
        y -= 1
    return y, m


def pick_scroll_target(lines: list[dict], pane: tuple[int, int, int, int],
                       fallback: tuple[int, int]) -> tuple[int, int]:
    """A point over real chat TEXT (a sender header) to put the wheel on.

    Scrolling over an embedded image/table or dead space does nothing (the wheel
    is eaten), so aim at a sender-name line — present in almost every frame and
    always scrollable. Fall back to the given point if nothing text-like shows.
    """
    px, py, pw, ph = pane
    cy = py + ph // 2

    def safe(l: dict) -> bool:
        t = l.get("text", "").strip()
        x, w = l.get("x", 0), l.get("width", 0)
        return (bool(_NAME.match(t)) or
                (x < px + 320 and w < 340 and re.search(r"[A-Za-z]", t)
                 and not re.search(r"\d{4,}", t)))

    cands = [l for l in lines if safe(l)]
    if not cands:
        return fallback
    best = min(cands, key=lambda l: abs(l.get("y", 0) - cy))
    return best.get("x", px) + min(best.get("width", 40), 140) // 2, best.get("y", cy)


# --------------------------------------------------------------------------- #
# Live backfiller — drives the remote via HorizonMCPClient.                    #
# --------------------------------------------------------------------------- #

@dataclass
class BackfillResult:
    frames: int = 0
    parsed: int = 0
    unique: int = 0
    stored: int = 0
    vectors: int = 0
    stopped: str = ""            # "cutoff" | "top" | "max_frames"
    times_seen: list[str] = field(default_factory=list)

    def summary(self) -> str:
        span = ""
        if self.times_seen:
            span = f", {self.times_seen[0]} … {self.times_seen[-1]}"
        return (f"{self.frames} frames -> {self.parsed} blocks -> {self.unique} "
                f"unique; stored +{self.stored} (vectors +{self.vectors}); "
                f"stopped: {self.stopped}{span}")


class ChatBackfiller:
    """Scroll a named Teams chat to its start and ingest the history (OCR-based)."""

    def __init__(self, config: dict, on_log: Callable[[str], None] | None = None) -> None:
        self._config = config
        self._log = on_log or (lambda _s: None)
        bf = config.get("backfill", {})
        ctl = config.get("control", {})
        self._focus_target = ctl.get("focus_target", "PVDI")
        self._screen = int(bf.get("screen", ctl.get("screen", 0)))
        self._pane = tuple(bf.get("pane", [690, 115, 1210, 800]))
        self._scroll_amount = int(bf.get("scroll_amount", 25))
        self._settle_ms = int(bf.get("settle_ms", 750))
        self._stall_wait_ms = int(bf.get("stall_wait_ms", 2600))
        self._stall_limit = int(bf.get("stall_limit", 8))
        self._max_frames = int(bf.get("max_frames", 700))
        self._months = int(bf.get("months", 6))
        self._display_name = str(config.get("user", {}).get("display_name", "")).strip()

    def _user_tokens(self) -> tuple[str, ...]:
        return tuple(t.lower() for t in self._display_name.split() if len(t) > 2)

    async def run(self, client, chat_title: str, *, months: int | None = None,
                  max_frames: int | None = None, screen: int | None = None,
                  ingest: bool = True,
                  should_stop: Callable[[], bool] | None = None) -> BackfillResult:
        px, py, pw, ph = self._pane
        screen = self._screen if screen is None else screen
        max_frames = self._max_frames if max_frames is None else max_frames
        cutoff_ym = months_ago_ym(self._months if months is None else months)
        fallback = (px + pw // 2, py + 100)
        tokens = self._user_tokens()

        # 0) make sure Horizon has OS focus so input reaches the remote
        await client.focus_window(self._focus_target)
        await client.wait(400)

        # 1) sanity: is the requested chat actually on screen?
        header = await client.ocr(x=max(px - 230, 0), y=70, width=pw, height=45,
                                  screen=screen)
        token = chat_title.split()[0]
        if token.lower() not in header.lower():
            self._log(f"WARN: '{token}' not in chat header — is '{chat_title}' open? "
                      f"Bring it to the front first.")

        # 2) jump to the newest message; re-pick a text target each step so an
        #    embedded image doesn't stall the descent.
        for _ in range(16):
            ocr = await client.ocr(x=px, y=py, width=pw, height=ph, screen=screen)
            tx, ty = pick_scroll_target(lines_sorted(ocr), self._pane, fallback)
            await client.move_mouse(tx, ty, screen=screen)
            await client.scroll(tx, ty, "down", amount=40, screen=screen)
            await client.wait(150)
        await client.wait(self._settle_ms)

        # 3) page upward, OCR + parse each frame
        result = BackfillResult()
        all_events: list[MessageEvent] = []
        prev_full: str | None = None
        stall = 0
        for i in range(max_frames):
            if should_stop and should_stop():
                result.stopped = "cancelled"
                break
            ocr = await client.ocr(x=px, y=py, width=pw, height=ph, screen=screen)
            lines = lines_sorted(ocr)
            full_sig = "\n".join(l.get("text", "") for l in lines)
            frame_events = parse_frame(ocr, channel=chat_title, user_tokens=tokens)
            all_events.extend(frame_events)
            result.frames += 1
            for l in lines:
                m = _HEADER.match(l.get("text", "").strip())
                if m:
                    result.times_seen.append(m.group("time").strip())
            self._log(f"frame {i:04d}: {len(lines)} lines, +{len(frame_events)} blocks")

            ym = hit_cutoff(lines, cutoff_ym)
            if ym:
                result.stopped = f"cutoff ({ym[0]}-{ym[1]:02d})"
                break

            if full_sig == prev_full:
                stall += 1
                # Usually Teams fetching older messages, not the real top: wait and
                # hit the top edge hard to trigger the lazy-load before giving up.
                await client.wait(self._stall_wait_ms)
                tx, _ = pick_scroll_target(lines, self._pane, fallback)
                await client.move_mouse(tx, py + 60, screen=screen)
                await client.scroll(tx, py + 60, "up", amount=60, screen=screen)
                await client.wait(self._stall_wait_ms)
                if stall >= self._stall_limit:
                    result.stopped = "top"
                    break
                prev_full = full_sig
                continue
            stall = 0
            prev_full = full_sig

            tx, ty = pick_scroll_target(lines, self._pane, fallback)
            await client.move_mouse(tx, ty, screen=screen)
            await client.scroll(tx, ty, "up", amount=self._scroll_amount, screen=screen)
            await client.wait(self._settle_ms)
        else:
            result.stopped = "max_frames"

        # 4) dedupe + ingest
        uniq = dedupe(all_events)
        result.parsed = len(all_events)
        result.unique = len(uniq)
        result.times_seen = list(dict.fromkeys(result.times_seen))
        if ingest and uniq:
            result.stored, result.vectors = self._ingest(uniq)
        return result

    def _ingest(self, events: list[MessageEvent]) -> tuple[int, int]:
        import os
        from .store import EventStore
        from .rag import RAGPipeline

        rag_cfg = self._config["rag"]
        store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
        store.connect()
        new_rows = store.ingest(events)
        rag = RAGPipeline(
            db_path=rag_cfg["db_path"],
            collection_name=rag_cfg["collection_name"],
            embedding_provider=rag_cfg["embedding_provider"],
            voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
            top_k=rag_cfg.get("top_k", 8),
        )
        rag.connect()
        vectors = rag.ingest(new_rows)
        store.close()
        return len(new_rows), vectors
