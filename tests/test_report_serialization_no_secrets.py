import json
import os
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch

from notifications.intent_transport import send_intent
from quant_platform_kit.common.runtime_reports import build_runtime_report_base


def test_report_serialization_has_no_secrets():
    token = str(123456789) + ':' + 'A' * 35
    chat_id = 'synthetic-private-chat'
    for acknowledged in (True, False):
        transport = Mock()
        transport.post.return_value = SimpleNamespace(status_code=200, json=lambda: {'ok': acknowledged})
        with patch.dict(os.environ, {'TELEGRAM_TOKEN': token}, clear=True):
            receipt = send_intent('Synthetic cycle status', chat_id=chat_id, token=token,
                                  requests_module=transport)
        report = build_runtime_report_base(
            platform='BinancePlatform', deploy_target='synthetic', service_name='synthetic',
            strategy_profile='synthetic', run_id='synthetic', run_source='test',
            summary={'notification_receipt': receipt},
        )
        serialized = json.dumps(report)
        assert receipt['ok'] is acknowledged
        assert transport.post.call_count == 1
        assert token not in serialized
        assert chat_id not in serialized
        assert re.search(r'\d{6,12}:A[\w-]{34}', serialized) is None

        def check(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in {'token', 'secret', 'password', 'api_key'}:
                        assert not item
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        check(json.loads(serialized))


def test_provider_exception_cannot_enter_report():
    token = str(123456789) + ':' + 'A' * 35
    transport = Mock()
    transport.post.side_effect = RuntimeError(token)
    with patch.dict(os.environ, {'TELEGRAM_TOKEN': token}, clear=True):
        receipt = send_intent('Synthetic cycle status', chat_id='synthetic-private-chat',
                              token=token, requests_module=transport)
    assert not receipt['ok']
    assert token not in json.dumps(receipt)


def test_execution_report_and_side_effect_records_exclude_delivery_credentials():
    from live_services import send_tg_msg
    from runtime_support import ExecutionRuntime, build_execution_report, runtime_notify

    token = str(123456789) + ':' + 'A' * 35
    chat_id = 'synthetic-private-chat'
    for acknowledged in (True, False):
        response = SimpleNamespace(status_code=200, json=lambda: {'ok': acknowledged})
        runtime = ExecutionRuntime(
            dry_run=False, run_id='synthetic-report', tg_token=token, tg_chat_id=chat_id,
            notifier=lambda **kwargs: send_tg_msg(kwargs['token'], kwargs['chat_id'], kwargs['text']),
        )
        with patch.dict(os.environ, {'TG_TOKEN': token}, clear=True), patch(
            'live_services.requests.post', return_value=response,
        ):
            report = build_execution_report(runtime)
            assert runtime_notify(runtime, report, 'Synthetic runtime status') is acknowledged
        records = {'report': report, 'side_effects': runtime.side_effect_log}
        serialized = json.dumps(records)
        assert token not in serialized
        assert chat_id not in serialized
        assert re.search(r'\d{6,12}:A[\w-]{34}', serialized) is None

        def check(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key.lower() in {'token', 'secret', 'password', 'api_key'}:
                        assert not item
                    check(item)
            elif isinstance(value, list):
                for item in value:
                    check(item)
        check(records)
