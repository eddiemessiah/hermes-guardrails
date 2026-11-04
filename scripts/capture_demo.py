"""Drive a real Hermes CLI session against scripts/scripted_llm.py and save screenshots.

Usage: HERMES_HOME=<test home> python scripts/capture_demo.py docs/
Needs: pexpect, pyte, rich, cairosvg (dev only; the plugin itself has no dependencies).
"""
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import cairosvg
import pexpect
import pyte
from rich.console import Console
from rich.text import Text

OUT = Path(sys.argv[1] if len(sys.argv) > 1 else "docs")
COLS, ROWS = 112, 42
env = dict(os.environ, TERM="xterm-256color", COLUMNS=str(COLS), LINES=str(ROWS))
COLORS = {"default": None, "black": "black", "red": "red", "green": "green", "brown": "yellow", "yellow": "yellow",
          "blue": "blue", "magenta": "magenta", "cyan": "cyan", "white": "white"}


def color(c):
    if c in COLORS:
        return COLORS[c]
    return f"#{c}" if isinstance(c, str) and len(c) == 6 else None


def render(lines_or_screen, name, title):
    con = Console(record=True, width=COLS, color_system="truecolor", file=open(os.devnull, "w"))
    if isinstance(lines_or_screen, pyte.Screen):
        scr = lines_or_screen
        for y in range(scr.lines):
            row, t = scr.buffer[y], Text()
            for x in range(scr.columns):
                ch = row[x]
                style = " ".join(filter(None, ["bold" if ch.bold else "", color(ch.fg) or "", f"on {color(ch.bg)}" if color(ch.bg) else ""]))
                t.append(ch.data, style=style or None)
            t.rstrip()
            con.print(t, soft_wrap=True)
    else:
        for line in lines_or_screen.splitlines():
            con.print(Text.from_ansi(line), soft_wrap=True)
    svg = con.export_svg(title=title)
    OUT.mkdir(parents=True, exist_ok=True)
    cairosvg.svg2png(bytestring=svg.encode(), write_to=str(OUT / f"{name}.png"), output_width=1600)
    print("wrote", OUT / f"{name}.png")


def session():
    screen, stream = pyte.Screen(COLS, ROWS), None
    stream = pyte.ByteStream(screen)
    c = pexpect.spawn("hermes chat -q \"Pay this month's hosting and the GPU hours\"", env=env, timeout=60, dimensions=(ROWS, COLS))

    def pump(t):
        try:
            c.expect(pexpect.TIMEOUT, timeout=t)
        finally:
            stream.feed(c.before or b"")
    for _ in range(40):
        pump(0.5)
        if "Allow once" in "\n".join(screen.display):
            break
    render(screen, "02-approval-pause", "Hermes: Guardrails asks before paying a new payee")
    c.send("\r")
    for _ in range(30):
        pump(0.5)
        if "nothing was charged twice" in "\n".join(screen.display):
            break
    pump(1)
    render(screen, "03-run-complete", "Hermes: overspend blocked, one payment, retry deduplicated")
    c.sendline("/exit")
    pump(3)
    c.terminate(force=True)


def cli(*args):
    return subprocess.run(["hermes", "guardrails", *args], env=env, capture_output=True, text=True).stdout


if __name__ == "__main__":
    home = Path(env["HERMES_HOME"])
    subprocess.run(["hermes", "gateway", "stop"], env=env, capture_output=True)
    db = home / "plugin-data" / "guardrails" / "guardrails.db"
    for p in db.parent.glob("guardrails.db*"):
        p.unlink()
    render(cli("status"), "01-status-armed", "hermes guardrails status")
    session()
    render("$ hermes guardrails audit\n" + cli("audit") + "\n$ hermes guardrails status\n" + cli("status"),
           "04-audit-log", "Hash-chained audit log after the run")
    con = sqlite3.connect(db)
    con.execute("UPDATE audit SET data=replace(data,'75.00','7.50') WHERE event='blocked'")
    con.execute("UPDATE audit SET data=replace(data,'7500','750') WHERE event='blocked'")
    con.commit()
    con.close()
    render("$ sqlite3 guardrails.db \"UPDATE audit SET data=... WHERE event='blocked'\"\n"
           "$ hermes guardrails verify\n" + cli("verify"), "05-tamper-detected", "Editing the log breaks the chain")
    subprocess.run(["hermes", "gateway", "stop"], env=env, capture_output=True)
