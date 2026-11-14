"""Mirror finalized GCS archives to R2 without overwriting existing objects."""

import json
import os
from functools import lru_cache

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError
from flask import Flask, request
from google.cloud import storage

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
MAX_ARCHIVE_BYTES = 1024 * 1024


class ArchiveConflict(Exception):
    """A previously archived object differs from the source generation."""


@lru_cache
def storage_client():
    return storage.Client()


@lru_cache
def r2_client():
    with open(os.environ["R2_CREDENTIALS_FILE"], encoding="utf-8") as stream:
        credentials = json.load(stream)
    return boto3.client(
        "s3",
        endpoint_url=f"https://{os.environ['R2_ACCOUNT_ID']}.r2.cloudflarestorage.com",
        region_name="auto",
        aws_access_key_id=credentials["access_key_id"],
        aws_secret_access_key=credentials["secret_access_key"],
        config=Config(
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            retries={"total_max_attempts": 1},
            connect_timeout=5,
            read_timeout=8,
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def read_source(bucket, key, generation):
    blob = storage_client().bucket(bucket).blob(key, generation=int(generation))
    return blob.download_as_bytes(
        if_generation_match=int(generation),
        start=0,
        end=MAX_ARCHIVE_BYTES,
        raw_download=True,
        retry=None,
        timeout=8,
        checksum=None,
    )


def mirror_archive(client, bucket, key, generation, source_bytes):
    if not 0 < len(source_bytes) <= MAX_ARCHIVE_BYTES:
        raise ValueError("Archive outside supported size")

    identity = {"Bucket": bucket, "Key": key}
    try:
        client.put_object(
            **identity,
            Body=source_bytes,
            ContentType="application/json",
            Metadata={"gcs-generation": generation},
            IfNoneMatch="*",
        )
        result = "created"
    except ClientError as error:
        if error.response["ResponseMetadata"]["HTTPStatusCode"] != 412:
            raise
        result = "already_present"

    stored = client.get_object(**identity)
    with stored["Body"] as stream:
        if stored["Metadata"].get("gcs-generation") != generation or stream.read(len(source_bytes) + 1) != source_bytes:
            raise ArchiveConflict
    return result


def log(event, severity="INFO", **fields):
    print(json.dumps({"severity": severity, "event": event, **fields}), flush=True)


@app.post("/")
def handle_event():
    envelope = request.get_json(silent=True)
    message = envelope.get("message") if isinstance(envelope, dict) else None
    attrs = message.get("attributes") if isinstance(message, dict) else None
    if not isinstance(attrs, dict):
        return "", 400

    bucket = attrs.get("bucketId")
    key = attrs.get("objectId", "")
    generation = attrs.get("objectGeneration", "")
    if (
        attrs.get("eventType") != "OBJECT_FINALIZE"
        or bucket != os.environ["SOURCE_BUCKET"]
        or not isinstance(key, str)
        or not key.startswith(os.environ["SOURCE_PREFIX"])
        or not key.endswith(".json")
    ):
        return "", 204
    if not isinstance(generation, str) or not generation.isascii() or not generation.isdecimal():
        return "", 400

    try:
        content = read_source(bucket, key, generation)
        result = mirror_archive(r2_client(), os.environ["R2_BUCKET"], key, generation, content)
    except Exception as error:
        log("archive_mirror_retry", "WARNING", generation=generation, error=type(error).__name__)
        return "", 503
    log("archive_mirror_verified", generation=generation, result=result, bytes=len(content))
    return "", 204
