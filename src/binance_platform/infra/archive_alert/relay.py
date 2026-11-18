"""Send authorized archive backlog incidents to the existing operations chat."""

import base64
import json
import os
import re
from pathlib import Path
from urllib.request import Request, urlopen

from flask import Flask, request

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024


def read_incident():
    envelope = request.get_json(silent=True)
    if not isinstance(envelope, dict):
        return None
    message = envelope.get("message")
    if not isinstance(message, dict) or not isinstance(message.get("data"), str):
        return None
    try:
        data = json.loads(base64.b64decode(message["data"], validate=True))
    except (ValueError, UnicodeError):
        return None
    incident = data.get("incident") if isinstance(data, dict) else None
    return incident if isinstance(incident, dict) else None


def send_alert(text):
    token = Path(os.environ["TELEGRAM_TOKEN_FILE"]).read_text().strip()
    chat_id = Path(os.environ["TELEGRAM_CHAT_FILE"]).read_text().strip()
    if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", token) or not re.fullmatch(r"-?\d+", chat_id):
        raise ValueError("Invalid Telegram configuration")
    payload = json.dumps({"chat_id": chat_id, "text": text}).encode()
    endpoint = Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(endpoint, timeout=10) as response:
        reply = json.load(response)
    if reply.get("ok") is not True or not isinstance(reply.get("result", {}).get("message_id"), int):
        raise ValueError("Telegram did not confirm delivery")
    return reply["result"]["message_id"]


@app.post("/")
def alert():
    incident = read_incident()
    if not incident or incident.get("state") != "open" or incident.get("renotify") is True:
        return "", 204

    resource = incident.get("resource")
    labels = resource.get("labels") if isinstance(resource, dict) else None
    policy = incident.get("policy_name")
    subscription = labels.get("subscription_id") if isinstance(labels, dict) else None
    if (policy, subscription) == (os.environ["PRODUCTION_POLICY"], os.environ["PRODUCTION_SUBSCRIPTION"]):
        test = False
    elif (
        os.getenv("TEST_POLICY") and os.getenv("TEST_SUBSCRIPTION")
        and (policy, subscription) == (os.getenv("TEST_POLICY"), os.getenv("TEST_SUBSCRIPTION"))
    ):
        test = True
    else:
        return "", 204

    incident_id = incident.get("incident_id")
    if not isinstance(incident_id, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,80}", incident_id):
        return "", 204
    text = (
        "【测试】Binance R2 归档积压告警。使用隔离订阅验证通知。"
        if test else
        "Binance R2 双写告警：最早未处理的归档消息已积压超过 30 分钟。请检查 Pub/Sub 订阅和归档服务日志。"
    )
    try:
        message_id = send_alert(text)
    except Exception as error:
        print(json.dumps({"severity": "WARNING", "event": "archive_alert_retry",
                          "incident_id": incident_id, "error": type(error).__name__}), flush=True)
        return "", 503
    print(json.dumps({"event": "archive_alert_delivered", "incident_id": incident_id,
                      "test": test, "telegram_message_id": message_id}), flush=True)
    return "", 204
