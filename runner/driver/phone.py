"""The driver's messages to the phone. The topic and the access token come from the config (topic_file, token_file),
never from the repo. The inbox is read with worker_task.poll_inbox."""
import urllib.request

import runtime as R
import worker_task as W

C = W.C


def post(title, body, priority="default", actions=""):
    """Sends one message to the phone and logs it. The driver's own messages are always sent: the worker's phone_titles
    filter applies only to the worker's alerts."""
    R.log(f"phone [{priority}] {title}: {body[:120]}")
    if not C.topic:
        return
    headers = {"Title": title, "Priority": priority}
    if actions:
        headers["Actions"] = actions
    request = urllib.request.Request(f"{C.ntfy_server}/{C.topic}", data=body[:500].encode("utf-8"),
                                     headers=W.ntfy_headers(headers), method="POST")
    try:
        urllib.request.urlopen(request, timeout=20).read()
    except Exception as error:
        R.log(f"phone send failed: {error}")


def button(label, body):
    """An ntfy action button that posts body to the inbox topic, which is how the phone answers the driver."""
    return f"http, {label}, {C.ntfy_server}/{C.inbox_topic}, method=POST, body={body}, clear=true"


def pause(state, title, body, priority="high"):
    """Pauses the driver and tells the phone why."""
    state["paused"] = True
    R.write_state(state)
    post(title, body, priority)
