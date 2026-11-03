import io
import json
import os
from unittest.mock import MagicMock, patch

from scripts import send_telegram_notification as sender


def test_stdin_sender_uses_shared_intent_and_requires_provider_ack(capsys):
    token = str(123456789) + ':' + 'A' * 35
    for ok in (True, False):
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = 200
        response.read.return_value = json.dumps({'ok': ok}).encode()
        with patch.dict(os.environ, {'TG_TOKEN': token, 'GLOBAL_TELEGRAM_CHAT_ID': 'synthetic-chat'}, clear=True), patch(
            'sys.stdin', io.StringIO('Synthetic workflow failure'),
        ), patch('urllib.request.urlopen', return_value=response) as send:
            assert sender.main() == (0 if ok else 1)
        request = send.call_args.args[0]
        assert request.get_method() == 'POST'
        assert json.loads(request.data)['text'] == 'Synthetic workflow failure'
    output = capsys.readouterr()
    assert token not in output.out + output.err
    assert 'synthetic-chat' not in output.out + output.err
