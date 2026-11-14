from io import BytesIO
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError

from infra.archive_mirror import mirror


@pytest.fixture
def event_setup(monkeypatch):
    monkeypatch.setenv("SOURCE_BUCKET", "source")
    monkeypatch.setenv("SOURCE_PREFIX", "archive/")
    monkeypatch.setenv("R2_BUCKET", "target")
    source = Mock(return_value=b'{"synthetic":true}')
    s3 = Mock()
    s3.get_object.side_effect = lambda **kwargs: {
        "Metadata": {"gcs-generation": "123"},
        "Body": BytesIO(source.return_value),
    }
    monkeypatch.setattr(mirror, "read_source", source)
    monkeypatch.setattr(mirror, "r2_client", lambda: s3)
    return source, s3


def publish(**attrs):
    attributes = {
        "eventType": "OBJECT_FINALIZE",
        "bucketId": "source",
        "objectId": "archive/test.json",
        "objectGeneration": "123",
    }
    return mirror.app.test_client().post("/", json={"message": {"attributes": attributes | attrs}})


def s3_error(status):
    return ClientError({"ResponseMetadata": {"HTTPStatusCode": status}}, "PutObject")


def test_creates_mirror_from_exact_generation(event_setup):
    source, client = event_setup
    assert publish().status_code == 204
    source.assert_called_once_with("source", "archive/test.json", "123")
    assert client.put_object.call_args.kwargs["IfNoneMatch"] == "*"
    assert client.put_object.call_args.kwargs["Body"] == source.return_value
    assert client.put_object.call_args.kwargs["Metadata"] == {"gcs-generation": "123"}


def test_same_generation_replay_is_acknowledged(event_setup, capsys):
    _, client = event_setup
    client.put_object.side_effect = s3_error(412)
    assert publish().status_code == 204
    assert '"result": "already_present"' in capsys.readouterr().out


@pytest.mark.parametrize("attrs", [{"bucketId": "other"}, {"objectId": "other/test.json"}, {"eventType": "OBJECT_DELETE"}])
def test_other_events_are_ignored(event_setup, attrs):
    source, _ = event_setup
    assert publish(**attrs).status_code == 204
    source.assert_not_called()


def test_failed_write_is_retried_by_the_trigger(event_setup):
    _, client = event_setup
    client.put_object.side_effect = [s3_error(503), {}]
    assert publish().status_code == 503
    assert publish().status_code == 204
    assert client.put_object.call_count == 2


def test_conflicting_existing_object_is_never_acked(event_setup, capsys):
    _, client = event_setup
    client.put_object.side_effect = s3_error(412)
    client.get_object.side_effect = None
    client.get_object.return_value = {
        "Metadata": {"gcs-generation": "123"},
        "Body": BytesIO(b'{"different":true}'),
    }
    assert publish().status_code == 503
    assert client.put_object.call_count == 1
    assert "ArchiveConflict" in capsys.readouterr().out


def test_source_generation_is_pinned(monkeypatch):
    client = Mock()
    monkeypatch.setattr(mirror, "storage_client", lambda: client)
    mirror.read_source("source", "archive/test.json", "123")
    blob = client.bucket.return_value.blob
    blob.assert_called_once_with("archive/test.json", generation=123)
    assert blob.return_value.download_as_bytes.call_args.kwargs["if_generation_match"] == 123
