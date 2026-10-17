"""The plug-in helper process (``<app> --plugin-host``). Nothing in here uses Qt.

It reads one JSON request per line on stdin and answers with JSON lines on
stdout (``{"progress": [done, total]}`` lines first for long jobs). Plug-ins
may print to stdout themselves, so the replies go to a copy of the original
stdout and stdout itself is pointed at stderr before any plug-in is loaded.

Requests:
- probe {format, path}: load once, report name, vendor, channel counts.
- open {format, path, rate, state}: keep an instance for editing; returns
  {id, name, vendor, latency, params}.
- set {id, key, value} -> {display}; params {id}; state {id} -> {state};
  program {id, index}; close {id}.
- preview {chain, src, first, end, channels} -> {path} of a .npy holding
  the processed frames (original frames come from the file itself).
- process {chain, src, dst, channels, label}: write the processed copy with
  processing.process_file.
- quit.
"""

from __future__ import annotations

import base64
import json
import os
import sys
import tempfile
import traceback


def main() -> int:
    reply_fd = os.dup(1)
    os.dup2(2, 1)  # plug-ins that print go to stderr, not into the replies
    replies = os.fdopen(reply_fd, "w", buffering=1)
    board = os.environ.get("SFM_PEDALBOARD")
    if board and board not in sys.path:
        sys.path.insert(0, board)

    from . import plugins, processing  # after the stdout swap: imports may print too

    def send(message: dict):
        replies.write(json.dumps(message) + "\n")
        replies.flush()

    opened: dict[int, object] = {}
    next_id = 1
    for line in sys.stdin:
        if not line.strip():
            continue
        try:
            request = json.loads(line)
            cmd = request.get("cmd")
            if cmd == "quit":
                break
            if cmd == "probe":
                plugin = plugins.open_plugin(request["format"], request["path"])
                ins, outs = plugin.channels
                send({"name": plugin.name, "vendor": plugin.vendor, "inputs": ins, "outputs": outs})
                plugin.close()
            elif cmd == "open":
                plugin = plugins.open_plugin(request["format"], request["path"], int(request.get("rate", 48000)))
                if request.get("state"):
                    plugin.set_state(base64.b64decode(request["state"]))
                opened[next_id] = plugin
                send({"id": next_id, "name": plugin.name, "vendor": plugin.vendor, "latency": plugin.latency,
                      "params": plugin.parameters(),
                      "programs": plugin.programs() if hasattr(plugin, "programs") else []})
                next_id += 1
            elif cmd == "set":
                plugin = opened[request["id"]]
                plugin.set_parameter(request["key"], float(request["value"]))
                match = [p for p in plugin.parameters() if p["index"] == request["key"]]
                send({"display": match[0]["display"] if match else "", "label": match[0]["label"] if match else ""})
            elif cmd == "params":
                send({"params": opened[request["id"]].parameters()})
            elif cmd == "program":
                plugin = opened[request["id"]]
                plugin.set_program(int(request["index"]))
                send({"params": plugin.parameters()})
            elif cmd == "state":
                send({"state": base64.b64encode(opened[request["id"]].get_state()).decode("ascii")})
            elif cmd == "close":
                plugin = opened.pop(request["id"], None)
                if plugin is not None:
                    plugin.close()
                send({"ok": True})
            elif cmd == "preview":
                chain = [plugins.ChainEntry(**e) for e in request["chain"]]
                settings = processing.Settings(mode="plugins", channels=tuple(request["channels"]))
                info_rate = processing.read_frames(request["src"], 0, 1)[1]
                engine = plugins.ChainEngine(chain, info_rate)
                try:
                    _original, processed, _rate = processing.preview(request["src"], request["first"], request["end"],
                                                                     None, settings, engine)
                finally:
                    engine.close()
                handle, path = tempfile.mkstemp(prefix="sfm-preview-", suffix=".npy")
                os.close(handle)
                import numpy as np
                np.save(path, processed)
                send({"path": path})
            elif cmd == "process":
                chain = [plugins.ChainEntry(**e) for e in request["chain"]]
                settings = processing.Settings(mode="plugins", channels=tuple(request["channels"]),
                                               label=request.get("label", ""))
                rate = processing.read_frames(request["src"], 0, 1)[1]
                engine = plugins.ChainEngine(chain, rate)
                try:
                    processing.process_file(request["src"], request["dst"], None, settings, engine=engine,
                                            progress=lambda d, t: send({"progress": [d, t]}))
                finally:
                    engine.close()
                send({"ok": request["dst"]})
            else:
                send({"error": f"unknown request {cmd!r}"})
        except Exception as error:  # noqa: BLE001 - every failure is reported to the app
            traceback.print_exc()
            send({"error": str(error) or type(error).__name__})
    for plugin in opened.values():
        try:
            plugin.close()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
