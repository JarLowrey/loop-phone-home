"""The driver's phone: messages to and from the ntfy topic. The topic name and the access token are read from the
files the config names (topic_file, token_file), never from the repo. Every call to ntfy carries the token when there is
one. Commands are read from the topic plus inbox_topic_suffix (the same topic when it is empty)."""
import json
import urllib.request

import loop_common as C

TOPIC_FILE = C.resolve(C.CFG.topic_file, C.CFG.config_path.parent)
TOKEN_FILE = C.resolve(C.CFG.token_file, C.CFG.config_path.parent)
SEEN = C.PROJECT / C.CFG.inbox_seen_file
SERVER = C.CFG.ntfy_server.rstrip("/")
# the driver sets this to its own log; a message is logged before it is sent
log = print


def read_word(path):
    return "".join(path.read_text(encoding="utf-8").split()) if path.exists() else ""


def topic():
    return read_word(TOPIC_FILE)


def ntfy_headers(headers=None):
    """The headers of a call to ntfy, with the access token when token_file holds one."""
    headers = dict(headers or {})
    token = read_word(TOKEN_FILE)
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def post(title, body, priority="default", actions=""):
    """One message to the phone. The driver's messages carry a title, which is how it skips its own replies."""
    name = topic()
    log(f"phone [{priority}] {title}: {body[:120]}")
    if not name:
        return
    headers = {"Title": title, "Priority": priority}
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(f"{SERVER}/{name}", data=body[:500].encode("utf-8"),
                                     headers=ntfy_headers(headers), method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception as error:
        log(f"phone send failed: {error}")


def button(label, body):
    """An ntfy action button that posts body to the topic, which is how the phone answers the driver."""
    return f"http, {label}, {SERVER}/{topic()}{C.CFG.inbox_topic_suffix}, method=POST, body={body}, clear=true"


def send_file(title, filename, data):
    """One phone message whose body is the file, so the phone shows it as an attachment. True when it was sent."""
    name = topic()
    log(f"phone [file] {title}: {filename}, {len(data)} bytes")
    if not name:
        return True
    request = urllib.request.Request(f"{SERVER}/{name}", data=data, method="PUT",
                                     headers=ntfy_headers({"Title": title, "Filename": filename}))
    try:
        urllib.request.urlopen(request, timeout=120).read()
        return True
    except Exception as error:
        log(f"phone file send failed: {error}")
        return False


def mark_seen(message_id):
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    with open(SEEN, "a", encoding="utf-8") as seen:
        seen.write(message_id + "\n")


def poll_messages(window):
    """(id, text) for each phone message in the last window (such as 1h) that has not been seen. Messages with a title
    are the driver's own, so they are marked seen and skipped."""
    name = topic()
    if not name:
        return []
    request = urllib.request.Request(f"{SERVER}/{name}{C.CFG.inbox_topic_suffix}/json?poll=1&since={window}",
                                     headers=ntfy_headers())
    try:
        body = urllib.request.urlopen(request, timeout=20).read()
    except Exception:
        return []
    seen = set(SEEN.read_text(encoding="utf-8").split()) if SEEN.exists() else set()
    messages = []
    for line in body.decode("utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("event") != "message" or event.get("id") in seen:
            continue
        if event.get("title"):
            mark_seen(event.get("id", ""))
            continue
        messages.append((event.get("id", ""), str(event.get("message", ""))))
    return messages
