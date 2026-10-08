"""A terminal chat with the running Conversation API (`make serve`), for demos.

    cd orchestrator && uv run python scripts/chat.py [--url http://127.0.0.1:8000] [--locale hi-IN]

Each reply prints its state, text, sources and numbered quick replies. Type a number to send that
quick reply, `f` to fill the consent form while one is open, `/erase` to delete the session and
`/quit` to leave; anything else is sent as text. The terminal is its only output: it logs nothing.
"""

import argparse
import uuid
from collections.abc import Callable
from typing import Any

import httpx

Ask = Callable[[str], str]


def consent_payload(form: dict[str, Any], ask: Ask) -> dict[str, Any]:
    """CONSENT_SUBMIT's payload (graph/states/s0.ConsentSubmit) from y/n answers to the form."""

    def yes(label: str) -> bool:
        return ask(f"  {label} [y/n] ").strip().lower().startswith("y")

    purposes = {}
    for p in form["purposes"]:
        purposes[p["id"]] = yes(p["label"] + (" (required)" if p["required"] else ""))
    return {
        "purposes": purposes,
        "age_18_plus": yes(form["adult"]["label"]),
        "notice_version": form["notice_version"],
        "notice_sha256": form["notice_sha256"],
    }


def show(body: dict[str, Any]) -> None:
    message = body["message"]
    print(f"\n[{body['state']}] {message['text']}")
    for source in message["sources"]:
        print(f"  source: {source['title']}, {source['section']}")
    for i, reply in enumerate(message["quick_replies"], 1):
        print(f"  {i}. {reply['label']}")
    if message.get("form"):
        print(f"  f. {message['form']['submit']}")


def chat(client: httpx.Client, locale: str, ask: Ask = input) -> None:
    created = client.post("/v1/sessions", json={"channel": "web", "locale": locale})
    created.raise_for_status()
    session = created.json()
    path = f"/v1/sessions/{session['session_id']}"
    auth = {"Authorization": f"Bearer {session['session_token']}"}
    message: dict[str, Any] = {"quick_replies": [], "form": None}
    request: dict[str, Any] = {"action": {"type": "START", "payload": {}}}
    while True:
        key = {"Idempotency-Key": str(uuid.uuid4())}
        response = client.post(f"{path}/turns", json=request, headers=auth | key)
        if response.status_code == 200:
            show(response.json())
            message = response.json()["message"]
        else:  # problem+json: say which, keep the last reply's choices
            problem = response.json() if "json" in response.headers.get("content-type", "") else {}
            print(f"  ({response.status_code} {problem.get('code', '')}: try again)")
        replies, form = message["quick_replies"], message.get("form")
        while True:
            line = ask("> ").strip()
            if line == "/quit":
                return
            if line == "/erase":
                erased = client.delete(path, headers=auth | {"Idempotency-Key": str(uuid.uuid4())})
                if erased.status_code == 200:
                    show(erased.json())
                return
            if line.isdigit() and 1 <= int(line) <= len(replies):
                request = {"action": replies[int(line) - 1]["action"]}
            elif line == "f" and form:
                submit = consent_payload(form, ask)
                request = {"action": {"type": "CONSENT_SUBMIT", "payload": submit}}
            elif line:
                request = {"text": line}
            else:
                continue
            break


def main() -> None:
    parser = argparse.ArgumentParser(description="A terminal chat with the Conversation API.")
    parser.add_argument("--url", default="http://127.0.0.1:8000")
    parser.add_argument("--locale", choices=["en-IN", "hi-IN"], default="en-IN")
    args = parser.parse_args()
    with httpx.Client(base_url=args.url, timeout=60) as client:
        try:
            chat(client, args.locale)
        except httpx.ConnectError:
            print(f"Nothing answers at {args.url}: start the API with `make serve`.")
        except (EOFError, KeyboardInterrupt):
            print()


if __name__ == "__main__":
    main()
