#!/usr/bin/env python3
"""End-to-end smoke test (Linux, needs `pip install psutil`):
dashboard on a fake tty  ->  start Windows agent  ->  stop it.
Expect the frame sequence NAS ... Windows ... NAS and no render errors."""
import json, os, re, shutil, signal, socket, subprocess, sys, tempfile, time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANSI = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]|\x1b\][A-Za-z0-9]*")


def main():
    tmp = tempfile.mkdtemp()
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    tty = os.path.join(tmp, "tty.txt"); open(tty, "w").close()
    cfg = os.path.join(tmp, "config.json")
    json.dump({"tty": tty, "agent_timeout_sec": 4, "agent_port": port}, open(cfg, "w"))
    adir = os.path.join(tmp, "agent"); os.makedirs(adir)
    shutil.copy(os.path.join(ROOT, "windows-agent", "nasscreen_agent.py"), adir)
    json.dump({"nas_host": "127.0.0.1", "port": port, "lhm_url": ""}, open(os.path.join(adir, "agent.json"), "w"))

    env = dict(os.environ, NASSCREEN_CONFIG=cfg, NASSCREEN_SIZE="140x45")
    dash = subprocess.Popen([sys.executable, os.path.join(ROOT, "fnos", "app", "dashboard.py")], env=env)
    try:
        time.sleep(4)
        agent = subprocess.Popen([sys.executable, os.path.join(adir, "nasscreen_agent.py")])
        time.sleep(7)
        agent.terminate(); agent.wait(timeout=10)
        time.sleep(7)
    finally:
        dash.send_signal(signal.SIGTERM)
        try:
            dash.wait(timeout=10)
        except subprocess.TimeoutExpired:
            dash.kill()

    frames = open(tty, encoding="utf-8", errors="replace").read().split("\x1b[H")[1:]
    kinds = "".join("W" if "Windows " in ANSI.sub("", f).split("\n")[0] else "N" for f in frames)
    print(f"{len(frames)} frames: {kinds}")
    errs = sum("render error" in f for f in frames)
    switched = re.search(r"N+W+N+", kinds) is not None
    print("render errors:", errs, "| switched NAS->Windows->NAS:", switched)
    shutil.rmtree(tmp, ignore_errors=True)
    return 0 if errs == 0 and switched else 1


if __name__ == "__main__":
    sys.exit(main())
