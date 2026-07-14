"""
Entry point for horizon-monitor.

Commands:
  python main.py tray              # system tray icon (production mode)
  python main.py monitor           # headless monitoring loop (terminal)
  python main.py monitor --dry-run # test poll loop without LLM
  python main.py query "..."       # one-shot question
  python main.py agent             # interactive REPL

Configuration is loaded from config.toml in the project root.
API keys are loaded from .env (copy from .env.example).
"""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

try:
    import tomllib  # Python 3.11+
except ModuleNotFoundError:  # pragma: no cover - older interpreters
    import tomli as tomllib

import click
from dotenv import load_dotenv


def app_dir() -> Path:
    """Where config.toml/.env live: beside the .exe when frozen, else the repo root."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).parent


def _bundled(name: str) -> Path:
    """A file packaged into the PyInstaller archive (or the source tree when not frozen)."""
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).parent))
    return base / name


APP_DIR = app_dir()
CONFIG_PATH = APP_DIR / "config.toml"

# Load .env from beside the exe (or the repo root in dev). Missing file is fine.
load_dotenv(APP_DIR / ".env")


def load_config() -> dict:
    if not CONFIG_PATH.exists():
        example = _bundled("config.example.toml")
        if example.exists():
            import shutil
            shutil.copy(example, CONFIG_PATH)
            print(
                f"Created {CONFIG_PATH} from the bundled template — edit it as needed.",
                flush=True,
            )
        else:
            raise click.ClickException(
                f"{CONFIG_PATH.name} not found — copy config.example.toml to config.toml "
                "and edit it"
            )
    with open(CONFIG_PATH, "rb") as f:
        return tomllib.load(f)


def require_api_key() -> str:
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        raise click.ClickException(
            "ANTHROPIC_API_KEY not set — copy .env.example to .env and fill it in"
        )
    return api_key


@click.group()
def cli():
    pass


@cli.command()
@click.option("--dry-run", is_flag=True, help="Poll without calling LLM or notifying")
def monitor(dry_run: bool):
    """Start the monitoring loop."""
    config = load_config()
    if not dry_run:
        require_api_key()  # dry-run never calls the LLM, so the key is optional there
    asyncio.run(_run_monitor(config, dry_run))


async def _run_monitor(config: dict, dry_run: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.poller import Poller
    from src.extractor import Extractor
    from src.rag import RAGPipeline
    from src.store import EventStore

    mcp_cfg = config["mcp"]
    poll_cfg = config["polling"]
    win_cfg = config["windows"]
    claude_cfg = config["claude"]
    user_cfg = config["user"]
    rag_cfg = config["rag"]

    api_key = os.environ.get("ANTHROPIC_API_KEY", "")

    extractor = Extractor(
        api_key=api_key,
        model=claude_cfg["vision_model"],
        user_display_name=user_cfg["display_name"],
    )

    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
    store.connect()

    # Optional retention: drop data older than [retention].retain_days on startup so a
    # long-running monitor doesn't accumulate forever. 0/absent = keep everything.
    retain_days = int(config.get("retention", {}).get("retain_days", 0) or 0)
    if retain_days > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=retain_days)).isoformat()
        removed = store.expire_older_than(cutoff)
        rag.delete_older_than(cutoff)
        if removed:
            print(f"retention: dropped {removed} event(s) older than {retain_days}d", flush=True)

    async with HorizonMCPClient(
        server_path=mcp_cfg["server_path"],
        command=mcp_cfg["command"],
    ) as client:
        poller = Poller(
            client=client,
            interval=poll_cfg["interval_seconds"],
            change_threshold=poll_cfg["change_threshold"],
            screen_index=poll_cfg["screen_index"],
            monitor_titles=win_cfg["monitor_titles"],
            focus_before_shot=poll_cfg["focus_before_shot"],
        )

        async def on_change(state, png: bytes) -> None:
            print(f"CHANGED  {state.screenshot_hash}  [{state.window_title}]", flush=True)
            events, is_locked = await extractor.extract(png, window_title=state.window_title)
            if is_locked:
                print("  [lock screen detected]", flush=True)
            for ev in events:
                tag = " @YOU" if ev.directed_at_user else ""
                ch = f" «{ev.channel}»" if ev.channel else ""
                t = f" {ev.chat_time}" if ev.chat_time else ""
                print(f"  [{ev.app}{ch}]{t} {ev.speaker}: {ev.message[:80]}{tag}", flush=True)
            if not events and not is_locked:
                print("  (no chat messages detected)", flush=True)
            elif events:
                new = store.ingest(events)   # dedup gate — only genuinely new messages
                if new:
                    rag.ingest(new)          # embed only the new ones
                    print(f"  stored +{len(new)} ({store.count()} total)", flush=True)

        print(f"Starting monitor (dry_run={dry_run}, interval={poll_cfg['interval_seconds']}s) — Ctrl+C to stop", flush=True)
        await poller.run(on_change=on_change, dry_run=dry_run)


@cli.command()
def tray():
    """Launch the classic pystray menu icon (legacy; prefer `app`)."""
    config = load_config()
    api_key = require_api_key()
    from src.tray import TrayApp
    TrayApp(config, api_key).run()


@cli.command()
def app():
    """Launch the desktop app: tray icon + tabbed window (PySide6)."""
    config = load_config()
    # Don't hard-require the key here — the window should open even without it so the
    # user can paste it into the Settings tab. Features that need it error gracefully.
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    from src.ui import run
    raise SystemExit(run(config, api_key))


def _parse_coords(s: str, n: int, what: str) -> tuple[int, ...]:
    """Parse a comma-separated integer tuple like '120,340' or '0,0,800,600'."""
    try:
        parts = tuple(int(p.strip()) for p in s.split(","))
    except ValueError:
        raise click.ClickException(f"{what} must be {n} comma-separated integers, got {s!r}")
    if len(parts) != n:
        raise click.ClickException(f"{what} must have {n} values, got {len(parts)}")
    return parts


@cli.command()
@click.argument(
    "action",
    type=click.Choice(
        [
            "unlock", "keep-alive", "launch", "activate", "taskbar", "run", "foreground",
            "reply", "read-file", "write-file", "open",
            "read-screen", "read-scroll", "paste",
        ]
    ),
)
@click.argument("value", required=False, default="")
@click.option(
    "--out", default="",
    help="read-*: write the captured text to this local file instead of stdout",
)
@click.option(
    "--save/--no-save", default=False,
    help="write-file/paste: press Ctrl+S in the remote after pasting",
)
@click.option(
    "--region", default="",
    help="read-screen/read-scroll: OCR only this 'x,y,w,h' region in ACTUAL screen "
         "pixels (not screenshot pixels — they differ under display scaling). Omit "
         "to OCR the full screen, which needs no coordinates.",
)
@click.option(
    "--at", "at", default="",
    help="paste: click 'x,y' to focus the target before pasting",
)
@click.option(
    "--double", is_flag=True,
    help="paste: double-click the --at point (some apps need two clicks to focus)",
)
@click.option(
    "--interval", type=float, default=None,
    help="keep-alive: seconds between nudges (default [control].keepalive_interval_seconds)",
)
@click.option(
    "--submit/--no-submit", default=True,
    help="unlock: press Enter after typing the password (--no-submit lets you verify "
         "the typed password before pressing Enter — avoids burning a login attempt)",
)
def remote(action: str, value: str, out: str, save: bool, region: str, at: str,
           double: bool, interval: float, submit: bool):
    """Drive the remote Horizon session (WRITE/READ actions; requires [control].enabled).

    Examples:
      python main.py remote foreground
      python main.py remote launch "Microsoft Teams"
      python main.py remote unlock                 # type the password and sign in
      python main.py remote unlock --no-submit     # type it but stop before Enter
      python main.py remote keep-alive             # nudge until Ctrl+C so it won't lock
      python main.py remote reply "on it, thanks"

    OCR read bridge (for VDIs that block clipboard copy-out — reads via screenshot+OCR):
      python main.py remote read-screen --out data/screen.txt
      python main.py remote read-scroll 960,500 --out data/pane.txt   # scroll-stitch a pane
      python main.py remote paste data/reply.txt --at 700,980 --double # focus + paste-in

    Clipboard code bridge (only where copy-out is allowed; read-file fails otherwise):
      python main.py remote open "src/app.py"
      python main.py remote read-file --out data/pull.txt
      python main.py remote write-file data/pull.txt --save
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Remote control disabled — set [control].enabled = true in config.toml"
        )
    asyncio.run(
        _run_remote(config, action, value, out, save, region, at, double, interval, submit)
    )


async def _run_remote(
    config: dict, action: str, value: str, out: str, save: bool,
    region: str = "", at: str = "", double: bool = False,
    interval: float | None = None, submit: bool = True,
) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.controller import RemoteController

    mcp_cfg = config["mcp"]
    ctl = config.get("control", {})
    region_box = _parse_coords(region, 4, "--region") if region else None

    def _emit(text: str, label: str) -> None:
        if out:
            out_path = Path(out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(text, encoding="utf-8")
            print(f"remote: {label} {len(text)} chars -> {out}", flush=True)
        else:
            print(text)

    async with HorizonMCPClient(mcp_cfg["server_path"], mcp_cfg["command"]) as client:
        c = RemoteController(
            client,
            focus_target=ctl.get("focus_target", "PVDI"),
            launch_wait=ctl.get("launch_wait_seconds", 1.5),
            clipboard_sync=ctl.get("clipboard_sync_seconds", 0.6),
            copy_timeout=ctl.get("copy_timeout_seconds", 6.0),
            screen=ctl.get("screen", 0),
        )
        print(f"remote: {action} {value!r}", flush=True)
        if action == "foreground":
            await c.bring_to_front()
        elif action == "unlock":
            password = os.environ.get("HORIZON_PASSWORD", "")
            if not password:
                raise click.ClickException("HORIZON_PASSWORD not set in .env")
            await c.unlock(password, submit=submit)
        elif action == "keep-alive":
            secs = interval if interval is not None else ctl.get(
                "keepalive_interval_seconds", 120.0
            )
            print(
                f"remote: keep-alive every {secs:g}s — Ctrl+C to stop", flush=True
            )
            try:
                while True:
                    await c.nudge()
                    await asyncio.sleep(secs)
            except (KeyboardInterrupt, asyncio.CancelledError):
                print("remote: keep-alive stopped", flush=True)
                return
        elif action in ("launch", "activate"):
            if not value:
                raise click.ClickException(f"'{action}' needs an app name")
            await c.launch_or_activate(value)
        elif action == "taskbar":
            if not value:
                raise click.ClickException("'taskbar' needs an app term to match")
            ok = await c.activate_taskbar([value])
            print(f"remote: taskbar {'ACTIVATED' if ok else 'NOT FOUND'}: {value!r}", flush=True)
        elif action == "run":
            if not value:
                raise click.ClickException("'run' needs a command")
            await c.run_command(value)
        elif action == "reply":
            if not value:
                raise click.ClickException("'reply' needs message text")
            await c.send_reply(value)
        elif action == "open":
            if not value:
                raise click.ClickException("'open' needs a file path")
            await c.open_file(value)
        elif action == "read-file":
            text = await c.copy_from_remote()
            _emit(text, "pulled")
        elif action == "write-file":
            if not value:
                raise click.ClickException("'write-file' needs a local file path")
            text = Path(value).read_text(encoding="utf-8")
            await c.paste_to_remote(text, replace_all=True, save=save)
            print(
                f"remote: pasted {len(text)} chars from {value}"
                f"{' and saved' if save else ''}",
                flush=True,
            )
        elif action == "read-screen":
            text = await c.read_screen(region=region_box)
            _emit(text, "ocr")
        elif action == "read-scroll":
            if not value:
                raise click.ClickException("'read-scroll' needs an 'x,y' scroll point")
            sx, sy = _parse_coords(value, 2, "read-scroll point")
            text, screens = await c.read_scrolling(sx, sy, region=region_box)
            _emit(text, f"ocr ({screens} screen(s))")
        elif action == "paste":
            if not value:
                raise click.ClickException("'paste' needs a local file path (text to paste)")
            text = Path(value).read_text(encoding="utf-8")
            ax = ay = None
            if at:
                ax, ay = _parse_coords(at, 2, "--at")
            await c.paste_at(text, x=ax, y=ay, double=double, save=save)
            print(
                f"remote: pasted {len(text)} chars from {value}"
                f"{f' at {ax},{ay}' if at else ''}{' and saved' if save else ''}",
                flush=True,
            )
        print("remote: done", flush=True)


@cli.command()
@click.argument("goal")
@click.option("--act", is_flag=True, help="Hands-on mode: perform actions (confirming each). "
                                          "Default is read-only advice.")
def assist(goal: str, act: bool):
    """Direct help — a screen-aware agent drives the remote VDI to do a task.

    \b
    python main.py assist "help me switch to master, do not stash my changes"
    python main.py assist "open the integration test file in VS Code" --act

    Advise (default) is read-only — it only tells you the steps. --act performs them,
    asking you to confirm every input action (y = do it, s = skip, n = stop).
    """
    config = load_config()
    api_key = require_api_key()
    from src.assistant import ComputerUseAgent

    agent = ComputerUseAgent(config, api_key)

    def confirm(desc: str) -> None:
        ans = input(f"\n  ACT — perform this? [{desc}]\n  (y)es / (s)kip / (n)o-stop: ").strip().lower()
        decision = {"y": "confirm", "yes": "confirm", "s": "skip", "skip": "skip"}.get(ans, "stop")
        agent.resolve_confirmation(decision)

    agent.on_text = lambda s: print(f"\nAssistant: {s}", flush=True)
    agent.on_action = confirm
    agent.on_result = lambda s: print(f"  · {s}", flush=True)
    agent.on_status = lambda s: print(f"  [{s}]", flush=True)
    agent.on_error = lambda e: print(f"  ERROR: {e}", flush=True)

    mode = "act" if act else "advise"
    print(f"assist ({mode}): {goal!r}\n", flush=True)
    agent.run_turn(goal, mode)
    print("\nassist: done", flush=True)


@cli.command()
@click.argument("chat_title")
@click.option("--months", type=int, default=None,
              help="Stop once messages older than this many months appear "
                   "(default [backfill].months).")
@click.option("--max-frames", type=int, default=None,
              help="Safety cap on total frames (default [backfill].max_frames).")
@click.option("--screen", type=int, default=None,
              help="VDI monitor the chat is on (default [backfill].screen).")
@click.option("--no-ingest", is_flag=True,
              help="Parse only; don't write to SQLite/ChromaDB (dry check).")
def backfill(chat_title: str, months: int | None, max_frames: int | None,
             screen: int | None, no_ingest: bool):
    """Import a whole Teams chat's back-history into the knowledge base.

    Unlike `monitor` (which sees only the current screen), this scrolls the named
    conversation newest->oldest, OCRs each frame (FREE — no Claude API), and
    ingests the history into SQLite + ChromaDB. Bring the chat to the front first.

    \b
    python main.py backfill "ResiDB Support"
    python main.py backfill "ResiDB Support" --months 3 --no-ingest
    """
    config = load_config()
    ctl = config.get("control", {})
    if not ctl.get("enabled", False):
        raise click.ClickException(
            "Backfill scrolls/drives the remote — set [control].enabled = true in config.toml"
        )
    asyncio.run(_run_backfill(config, chat_title, months, max_frames, screen, not no_ingest))


async def _run_backfill(config: dict, chat_title: str, months: int | None,
                        max_frames: int | None, screen: int | None, ingest: bool) -> None:
    from src.mcp_client import HorizonMCPClient
    from src.backfill import ChatBackfiller

    mcp_cfg = config["mcp"]
    async with HorizonMCPClient(mcp_cfg["server_path"], mcp_cfg["command"]) as client:
        bf = ChatBackfiller(config, on_log=lambda s: print(s, flush=True))
        print(f"backfill: {chat_title!r} (ingest={ingest}) — bring the chat to the front. "
              "Ctrl+C to stop", flush=True)
        result = await bf.run(
            client, chat_title, months=months, max_frames=max_frames,
            screen=screen, ingest=ingest,
        )
        print(f"backfill: {result.summary()}", flush=True)


@cli.command()
@click.argument("keyword")
@click.option("--limit", type=int, default=30, help="Max matches to show.")
@click.option("--channel", default="", help="Restrict to a channel (substring match).")
@click.option("--emails", is_flag=True,
              help="Also list every email address found in the matches.")
def search(keyword: str, limit: int, channel: str, emails: bool):
    """Exact keyword search over collected messages (FREE — no API, no LLM).

    The literal-lookup complement to `query` (which is semantic + Sonnet). Good for
    finding an exact string — an email address, a table name, "distro" — that vector
    search would only approximate.

    \b
    python main.py search distro
    python main.py search better.com --emails
    python main.py search email --channel "ResiDB Support"
    """
    import re as _re
    config = load_config()
    from src.store import EventStore

    store = EventStore(config["rag"].get("events_db", "./data/events.db"))
    store.connect()
    rows = store.search(keyword, limit=limit, channel=channel or None)
    if not rows:
        click.echo(f"No messages matching {keyword!r} (of {store.count()} stored).")
    for r in rows:
        when = r.get("chat_time") or (r.get("observed_at") or "")[:16]
        ch = f" «{r['channel']}»" if r.get("channel") else ""
        click.echo(f"[{when}]{ch} {r['speaker']}: {r['message'][:160]}")
    if emails:
        found: dict[str, tuple[str, str]] = {}
        for r in rows:
            for m in _re.findall(
                r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", r["message"]
            ):
                found.setdefault(m, (r["speaker"], r.get("chat_time", "")))
        if found:
            click.echo("\nEmail addresses found:")
            for addr, (sp, ct) in sorted(found.items()):
                click.echo(f"  {addr}  ({sp}, {ct})")
    store.close()


@cli.command()
@click.argument("question")
def query(question: str):
    """Ask a question about recorded conversations."""
    config = load_config()
    api_key = require_api_key()
    _run_query(config, api_key, question)


@cli.command()
def agent():
    """Start interactive query REPL."""
    config = load_config()
    api_key = require_api_key()
    _run_query(config, api_key, question=None)


@cli.command()
@click.option(
    "--older-than", type=int, default=None, metavar="DAYS",
    help="Delete only events older than DAYS days. Omit to purge everything.",
)
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
def purge(older_than: int | None, yes: bool):
    """Trim or wipe stored conversation data (SQLite + ChromaDB).

    \b
    python main.py purge                  # delete ALL stored events (asks to confirm)
    python main.py purge --older-than 30  # delete events older than 30 days
    """
    config = load_config()
    if older_than is not None and older_than < 0:
        raise click.ClickException("--older-than must be >= 0")

    if older_than is None:
        if not yes and not click.confirm(
            "This deletes ALL stored events from SQLite and ChromaDB. Continue?"
        ):
            raise click.ClickException("Aborted.")
    _run_purge(config, older_than)


def _run_purge(config: dict, older_than: int | None) -> int:
    """Apply retention/purge to both stores. Returns SQLite rows removed.

    Shared by the `purge` command and the monitor's startup auto-retention.
    """
    from src.rag import RAGPipeline
    from src.store import EventStore

    rag_cfg = config["rag"]

    store = EventStore(rag_cfg.get("events_db", "./data/events.db"))
    store.connect()
    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    if older_than is None:
        removed = store.purge_all()
        rag.purge_all()
        print(f"purge: removed all {removed} event(s) from SQLite + ChromaDB", flush=True)
    else:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than)).isoformat()
        removed = store.expire_older_than(cutoff)
        rag.delete_older_than(cutoff)
        print(f"purge: removed {removed} event(s) older than {older_than}d", flush=True)
    store.close()
    return removed


def _run_query(config: dict, api_key: str, question: str | None) -> None:
    from src.rag import RAGPipeline
    from src.agent import QueryAgent

    rag_cfg = config["rag"]
    claude_cfg = config["claude"]

    rag = RAGPipeline(
        db_path=rag_cfg["db_path"],
        collection_name=rag_cfg["collection_name"],
        embedding_provider=rag_cfg["embedding_provider"],
        voyage_api_key=os.environ.get("VOYAGE_API_KEY") or None,
        top_k=rag_cfg["top_k"],
    )
    rag.connect()

    agent_obj = QueryAgent(
        api_key=api_key,
        model=claude_cfg["query_model"],
        rag=rag,
        max_tokens=claude_cfg["max_tokens"],
    )

    if question:
        agent_obj.query(question)
    else:
        agent_obj.repl()


if __name__ == "__main__":
    # Double-clicking the packaged .exe (no args) launches the desktop app.
    if getattr(sys, "frozen", False) and len(sys.argv) == 1:
        sys.argv.append("app")
    cli()
