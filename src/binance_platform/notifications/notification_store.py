"""Generation-checked private notification state; never uses trading state."""

from __future__ import annotations

import json
import re


class NotificationStore:
    def __init__(self, prefix, *, client=None):
        if not isinstance(prefix, str) or not re.fullmatch(r"gs://[a-z0-9][a-z0-9._-]+/[^\s?#*]+", prefix):
            raise ValueError("notification_store_configuration_missing")
        if client is None:
            from google.cloud import storage
            client = storage.Client()
        self.client = client
        self.prefix = prefix.rstrip("/")

    def _blob(self, key):
        from google.cloud import storage
        if not re.fullmatch(r"[a-zA-Z0-9_./=-]+", key) or ".." in key:
            raise ValueError("notification_store_key_invalid")
        return storage.Blob.from_string(f"{self.prefix}/{key}", client=self.client)

    def load(self, key):
        from google.api_core.exceptions import NotFound
        blob = self._blob(key)
        try:
            blob.reload(retry=None, timeout=30)
        except NotFound:
            return None, 0
        generation = int(blob.generation)
        value = json.loads(blob.download_as_text(if_generation_match=generation, retry=None, timeout=30))
        if not isinstance(value, dict):
            raise ValueError("notification_marker_invalid")
        return value, generation

    def compare_and_swap(self, key, generation, value):
        from google.api_core.exceptions import PreconditionFailed
        blob = self._blob(key)
        try:
            blob.upload_from_string(json.dumps(value, sort_keys=True, allow_nan=False),
                content_type="application/json", if_generation_match=generation, retry=None, timeout=30)
        except PreconditionFailed:
            return False
        return True

    def put_once(self, key, value):
        """An identical replay is safe; conflicting revisions stay visible."""
        if self.compare_and_swap(key, 0, value):
            return True
        existing, _ = self.load(key)
        if existing != value:
            raise ValueError("notification_candidate_conflict")
        return True
