import json

import pytest

pytestmark = pytest.mark.e2e


def test_chat_retries_same_message_after_lost_receipt(page, live_server):
    token = "a" * 43
    submissions = []
    auth_frames = []

    def socket(route):
        def receive(data):
            frame = json.loads(data)
            if "token" in frame:
                auth_frames.append(frame)
                return
            submissions.append(frame)
            if len(submissions) == 1:
                route.close(code=1011)
            else:
                route.send(json.dumps({"type": "receipt", "id": frame["id"]}))

        route.on_message(receive)

    page.route_web_socket("**/ws", socket)
    page.goto(f"{live_server}/#/activity")
    page.get_by_text("Send a chat message", exact=True).click()
    page.get_by_label("Panel session token").fill(token)
    page.get_by_label("Message", exact=True).fill("Numbered panel event 17")
    page.get_by_role("button", name="Send message").click()
    page.get_by_text("Message received.", exact=True).wait_for()
    assert submissions == [
        {"id": submissions[0]["id"], "text": "Numbered panel event 17"},
        {"id": submissions[0]["id"], "text": "Numbered panel event 17"},
    ]
    assert auth_frames == [{"token": token}, {"token": token}]
    assert token not in page.evaluate("JSON.stringify(localStorage)")
    assert token not in page.evaluate("JSON.stringify(sessionStorage)")


def test_chat_discards_pending_plaintext_when_the_session_ends(page, live_server):
    submissions = []

    def socket(route):
        def receive(data):
            frame = json.loads(data)
            if "id" in frame:
                submissions.append(frame)
                route.close(code=1008)

        route.on_message(receive)

    page.route_web_socket("**/ws", socket)
    page.goto(f"{live_server}/#/activity")
    page.get_by_text("Send a chat message", exact=True).click()
    token = page.get_by_label("Panel session token")
    message = page.get_by_label("Message", exact=True)
    token.fill("a" * 43)
    message.fill("Private message with an uncertain receipt")
    page.get_by_role("button", name="Send message").click()
    page.get_by_text(
        "The panel session ended. Delivery could not be confirmed.", exact=True
    ).wait_for()
    assert token.input_value() == ""
    assert message.input_value() == ""
    assert len(submissions) == 1
