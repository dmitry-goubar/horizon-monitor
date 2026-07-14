"""
Adaptive scroll-and-capture over any scrollable pane in the remote session.

This is the reusable core behind info-gathering from Teams, Symphony, Outlook and
VS Code: scroll a pane end to end, capturing each frame ONCE with as little overlap
as is safe. The naive fixed-step loop moved only a few lines per scroll (Teams'
WebView2 barely moves per wheel call and it's non-linear), so ~85% of every frame
was a re-capture of the last one. This tunes itself instead.

How it self-adjusts (a simple feedback loop):
  * After each scroll burst it MEASURES how far the content actually moved by matching
    anchor text lines (identical OCR text, unique in both frames) between the previous
    and current frame — the median y-shift is the pixel displacement.
  * A running px-per-pulse estimate (EMA) sizes the next burst to hit a target
    displacement of ~`target_fraction` of the pane height, leaving enough overlap to
    keep matching anchors — which is exactly what guarantees no lines are skipped.
  * If a burst overshoots so far that NO anchors match (overlap lost → a gap may have
    formed), it scrolls back to re-establish overlap and shrinks the step. Self-correcting.

App-agnostic: it yields raw OCR per frame; callers layer their own parser on top
(e.g. src/backfill.py parses Teams messages). The MCP client only needs
ocr/move_mouse/scroll — see src/mcp_client.py.
"""

from __future__ import annotations

import re
from typing import Any

# "Last, First" — a header line; a safe (text) surface to put the wheel over so the
# wheel isn't eaten by an embedded image/table or dead space.
_NAME = re.compile(r"^[A-Z][\w'’.\-]+,\s*[A-Z]")


def lines_sorted(ocr: Any) -> list[dict]:
    """OCR line boxes in reading order (top→bottom, then left→right).

    Accepts the raw JSON string an MCP `ocr` call returns, or an already-parsed dict.
    """
    import json
    data = ocr
    if isinstance(ocr, str):
        try:
            data = json.loads(ocr)
        except (json.JSONDecodeError, TypeError):
            return []
    lines = (data or {}).get("lines", [])
    return sorted(lines, key=lambda l: (l.get("y", 0), l.get("x", 0)))


def pick_scroll_target(lines: list[dict], pane: tuple[int, int, int, int],
                       fallback: tuple[int, int]) -> tuple[int, int]:
    """A point over real TEXT (a sender header / short left-aligned line) to wheel on.

    Scrolling over an embedded image/table or dead space does nothing (the wheel is
    eaten), so aim at a text line — present in almost every frame and always scrollable.
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


def measure_displacement(prev_lines: list[dict], cur_lines: list[dict],
                         min_len: int = 6) -> int | None:
    """Pixels the content moved between two frames, via OCR anchor matching.

    Uses only anchors that are UNIQUE in both frames (so repeated tokens like "214"
    can't mismatch) and takes the median y-shift (robust to a few bad reads). Returns
    None when no anchor is shared — i.e. the frames don't overlap, so the true shift
    is unknown and a gap may exist. Scrolling up yields a positive shift (old content
    slides down); scrolling down, negative.
    """
    def uniq_y(lines: list[dict]) -> dict[str, int]:
        seen: dict[str, int] = {}
        dup: set[str] = set()
        for l in lines:
            t = l.get("text", "").strip()
            if len(t) < min_len:
                continue
            if t in seen:
                dup.add(t)
            else:
                seen[t] = l.get("y", 0)
        for t in dup:
            seen.pop(t, None)
        return seen

    a, b = uniq_y(prev_lines), uniq_y(cur_lines)
    dys = sorted(b[t] - a[t] for t in a.keys() & b.keys())
    if not dys:
        return None
    return dys[len(dys) // 2]


class AdaptiveScroller:
    """Feedback-controlled scroll-and-capture of one scrollable pane.

    Typical use (see src/backfill.py):
        s = AdaptiveScroller(pane, screen)
        ocr, lines = await s.prime(client)          # capture the starting frame
        while ...:
            ...process lines...
            ocr, lines, disp = await s.advance(client, "up")
            if disp is not None and abs(disp) < s.stall_px:  # barely moved → top/lazy-load
                ...
    """

    def __init__(self, pane: tuple[int, int, int, int], screen: int, *,
                 base_amount: int = 15, target_fraction: float = 0.8,
                 init_pulses: int = 3, min_pulses: int = 1, max_pulses: int = 16,
                 ema_alpha: float = 0.4, pulse_wait_ms: int = 90, settle_ms: int = 550,
                 stall_px: int = 16, on_log=None) -> None:
        self.pane = tuple(pane)
        self.screen = screen
        self.base_amount = base_amount
        self.target_px = max(60, int(self.pane[3] * target_fraction))
        self.init_pulses = init_pulses
        self.min_pulses = min_pulses
        self.max_pulses = max_pulses
        self.ema_alpha = ema_alpha
        self.pulse_wait_ms = pulse_wait_ms
        self.settle_ms = settle_ms
        self.stall_px = stall_px
        self._log = on_log or (lambda _s: None)

        self.px_per_pulse: float | None = None   # learned scroll gain
        self.prev_lines: list[dict] = []
        self.last_pulses: int = init_pulses
        self.last_disp: int | None = None

    # ---- observability -----------------------------------------------------

    def new_ratio(self, disp: int | None) -> float:
        """Fraction of the pane that is NEW content after a move (1.0 = no overlap)."""
        if disp is None:
            return 1.0
        return min(1.0, abs(disp) / max(self.pane[3], 1))

    def planned_pulses(self) -> int:
        if self.px_per_pulse and self.px_per_pulse > 1:
            n = round(self.target_px / self.px_per_pulse)
            return max(self.min_pulses, min(self.max_pulses, int(n)))
        return self.init_pulses

    # ---- capture -----------------------------------------------------------

    async def _ocr(self, client) -> tuple[str, list[dict]]:
        px, py, pw, ph = self.pane
        raw = await client.ocr(x=px, y=py, width=pw, height=ph, screen=self.screen)
        return raw, lines_sorted(raw)

    async def prime(self, client) -> tuple[str, list[dict]]:
        """Capture the starting frame so the first advance() can measure against it."""
        raw, lines = await self._ocr(client)
        self.prev_lines = lines
        return raw, lines

    async def _burst(self, client, direction: str, pulses: int, lines: list[dict]) -> None:
        px, py, pw, ph = self.pane
        fallback = (px + pw // 2, py + 100)
        tx, ty = pick_scroll_target(lines or self.prev_lines, self.pane, fallback)
        await client.move_mouse(tx, ty, screen=self.screen)
        for _ in range(max(1, pulses)):
            await client.scroll(tx, ty, direction, amount=self.base_amount, screen=self.screen)
            await client.wait(self.pulse_wait_ms)
        await client.wait(self.settle_ms)

    async def advance(self, client, direction: str = "up") -> tuple[str, list[dict], int | None]:
        """One adaptive scroll burst + capture. Returns (raw_ocr, lines, displacement_px).

        displacement is None only if overlap was lost and couldn't be recovered.
        """
        pulses = self.planned_pulses()
        await self._burst(client, direction, pulses, self.prev_lines)
        raw, cur = await self._ocr(client)
        disp = measure_displacement(self.prev_lines, cur)

        if disp is None:
            # Overshot past the overlap — step back toward the previous frame until an
            # anchor reappears, so we never leave an unread gap.
            back = "down" if direction == "up" else "up"
            for _ in range(3):
                await self._burst(client, back, max(1, pulses // 2), cur)
                raw, cur = await self._ocr(client)
                disp = measure_displacement(self.prev_lines, cur)
                if disp is not None:
                    break
            self._adapt_overshoot(pulses)
        else:
            self._adapt(pulses, disp)

        self.last_pulses = pulses
        self.last_disp = disp
        self.prev_lines = cur
        return raw, cur, disp

    async def nudge_hard(self, client, direction: str = "up") -> tuple[str, list[dict], int | None]:
        """A deliberately large burst at the pane's top/bottom edge — used to shake a
        stall loose (Teams lazy-loads older messages when you hit the very top)."""
        px, py, pw, ph = self.pane
        edge_y = py + 60 if direction == "up" else py + ph - 60
        tx, _ = pick_scroll_target(self.prev_lines, self.pane, (px + pw // 2, edge_y))
        await client.move_mouse(tx, edge_y, screen=self.screen)
        for _ in range(self.max_pulses):
            await client.scroll(tx, edge_y, direction, amount=self.base_amount, screen=self.screen)
            await client.wait(self.pulse_wait_ms)
        await client.wait(self.settle_ms)
        raw, cur = await self._ocr(client)
        disp = measure_displacement(self.prev_lines, cur)
        self.prev_lines = cur
        self.last_disp = disp
        return raw, cur, disp

    # ---- controller --------------------------------------------------------

    def _adapt(self, pulses: int, disp: int) -> None:
        moved = abs(disp)
        if moved >= 20:                      # real movement → update the gain estimate
            ppp = moved / max(pulses, 1)
            self.px_per_pulse = (ppp if self.px_per_pulse is None
                                 else self.ema_alpha * ppp
                                 + (1 - self.ema_alpha) * self.px_per_pulse)
        # near-zero movement = a stall (top / lazy-load), NOT low gain — leave the
        # estimate alone so we don't wind pulses up forever against a wall.

    def _adapt_overshoot(self, pulses: int) -> None:
        # We moved at least a full pane in `pulses`; raise the gain estimate so the next
        # burst is smaller, and don't trust a stale low estimate.
        est = self.pane[3] / max(pulses, 1)
        self.px_per_pulse = est if self.px_per_pulse is None else max(self.px_per_pulse, est)
