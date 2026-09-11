import copy
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from scripts import migrate_daily_accounting_state as migration
from tests.test_approved_accounting_rebase import ArchiveTransaction, setup_rebase
from tests.test_daily_accounting_migration import Ref, Snapshot, _evidence


NOW = datetime(2026, 9, 11, 3, 0, tzinfo=timezone.utc)


class AccountClient:
    def __init__(self, accounts):
        self.accounts = [copy.deepcopy(account) for account in accounts]
        self.calls = 0

    def get_account(self):
        value = self.accounts[min(self.calls, len(self.accounts) - 1)]
        self.calls += 1
        if isinstance(value, Exception):
            raise value
        return copy.deepcopy(value)

    def __getattr__(self, name):
        raise AssertionError(f"unexpected broker surface: {name}")


class CountingRef(Ref):
    def __init__(self, snapshot, *, on_get=None):
        super().__init__(snapshot)
        self.calls = 0
        self.on_get = on_get

    def get(self, **kwargs):
        self.calls += 1
        if self.on_get is not None:
            self.on_get(self)
        return super().get(**kwargs)


def _post_rebase_setup(monkeypatch, tmp_path, accounts):
    from application import rebased_recovery

    refs, archive_ref, old_ledger, old_control = setup_rebase(monkeypatch)
    old_control["source"]["original_evidence"]["account_scope_sha256"] = (
        migration.digest({"account_uid": "123"})
    )
    refs["control_ref"].snapshot.value = copy.deepcopy(old_control)
    monkeypatch.setattr(
        migration, "APPROVED_REBASE_CONTROL_SHA256", migration.digest(old_control)
    )
    proposal = migration.build_rebase_proposal(
        ledger=old_ledger,
        evidence=_evidence(history_counts={"earn_rewards": 1}),
        observed_at=NOW,
    )
    tx = ArchiveTransaction()
    migration._rebase_transaction(
        tx,
        refs=refs,
        archive_ref=archive_ref,
        ledger=old_ledger,
        control=old_control,
        ledger_update_time=refs["ledger_ref"].snapshot.update_time,
        proposed_fields=proposal["proposed_fields"],
        observed_at=NOW,
        started_at=NOW,
    )
    archive = tx.creates[0][1]
    current_ledger = {**old_ledger, **tx.writes[0][1]}
    monkeypatch.setattr(rebased_recovery, "APPROVED_OLD_LEDGER_SHA256", migration.digest(old_ledger))
    monkeypatch.setattr(rebased_recovery, "APPROVED_OLD_CONTROL_SHA256", migration.digest(old_control))
    monkeypatch.setattr(
        rebased_recovery,
        "APPROVED_OPENING_BALANCES_SHA256",
        migration.digest(proposal["proposed_fields"]["last_balance_snapshot"]),
    )

    ledger_ref = CountingRef(Snapshot(current_ledger))
    archive_ref = CountingRef(Snapshot(archive))
    ledger_ref.parent = SimpleNamespace(
        document=lambda name: archive_ref
        if name == migration.REBASE_ARCHIVE_DOCUMENT
        else (_ for _ in ()).throw(AssertionError("unexpected archive document"))
    )
    refs = {
        "ledger_ref": ledger_ref,
        "control_ref": CountingRef(Snapshot(old_control)),
        "owner_ref": CountingRef(Snapshot(None)),
    }
    client = AccountClient(accounts)
    expected = {"account_scope_sha256": migration.digest({"account_uid": "123"})}
    encrypted_path = tmp_path / "private-scope.cms"

    monkeypatch.setattr(migration, "PRIVATE_SCOPE_PREVIEW_PATH", encrypted_path)
    monkeypatch.setattr(migration, "require_runtime_context", lambda: None)
    monkeypatch.setattr(
        migration,
        "resolve_runtime_target_from_env",
        lambda **kwargs: SimpleNamespace(
            live_continuity=SimpleNamespace(state="RECONCILE_ONLY")
        ),
    )
    monkeypatch.setattr(migration, "_expected_digests", lambda: expected)
    monkeypatch.setattr(migration, "_refs", lambda: refs)
    monkeypatch.setattr(migration, "connect_client", lambda *args, **kwargs: client)
    monkeypatch.setattr(
        migration.os,
        "environ",
        {
            "GITHUB_SHA": "f" * 40,
            "BINANCE_API_KEY": "synthetic",
            "BINANCE_API_SECRET": "synthetic",
            "PROPOSAL_RECIPIENT_CERTIFICATE": "synthetic public certificate",
        },
    )
    return refs, archive_ref, client, encrypted_path


def _account(*, uid="123", unknown_free="2.50000000", unknown_locked="0.125"):
    return {
        "uid": uid,
        "balances": [
            {"asset": "USDT", "free": "500", "locked": "0"},
            {"asset": "BTC", "free": "0.1", "locked": "0"},
            {"asset": "ETH", "free": "2", "locked": "0"},
            {"asset": "BNB", "free": "0.25", "locked": "0"},
            {"asset": "DOGE", "free": unknown_free, "locked": unknown_locked},
            {"asset": "ZERO", "free": "0", "locked": "0"},
        ],
    }


def test_scope_preview_encrypts_only_stable_non_managed_nonzero_spot_rows(
    monkeypatch, tmp_path, capsys
):
    refs, archive_ref, client, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [_account(), _account()]
    )
    plaintext = []

    def encrypt(value, *, certificate):
        plaintext.append(copy.deepcopy(value))
        assert certificate == "synthetic public certificate"
        return b"encrypted-cms"

    monkeypatch.setattr(migration, "encrypt_rebase_proposal", encrypt)

    assert migration.main(["scope-preview"]) == 0
    output = capsys.readouterr().out
    public = json.loads(output)

    assert public == {
        "execution_authority_granted": False,
        "ledger_unchanged": True,
        "no_order": True,
        "stage": "private_spot_scope_preview",
        "status": "encrypted_scope_preview_ready",
        "write_performed": False,
    }
    assert client.calls == 2
    assert refs["ledger_ref"].calls == 2
    assert refs["control_ref"].calls == 2
    assert refs["owner_ref"].calls == 2
    assert archive_ref.calls == 2
    assert encrypted_path.read_bytes() == b"encrypted-cms"
    assert plaintext[0]["non_managed_nonzero_spot_assets"] == [
        {"asset": "DOGE", "free": "2.50000000", "locked": "0.125"}
    ]
    assert plaintext[0]["source_kind"] == "post_rebase"
    assert plaintext[0]["historical_difference_unresolved"] is True
    assert plaintext[0]["no_order"] is True
    assert plaintext[0]["write_performed"] is False
    assert "DOGE" not in output
    assert "2.50000000" not in output


def test_scope_preview_accepts_unicode_alphanumeric_assets_without_public_leak(
    monkeypatch, tmp_path, capsys
):
    account = _account()
    account["balances"].extend(
        [
            {"asset": "测试币", "free": "1.25", "locked": "0"},
            {"asset": "１２３４５６", "free": "0.5", "locked": "0.1"},
            {"asset": "零余额币", "free": "0", "locked": "0"},
        ]
    )
    _post_rebase_setup(monkeypatch, tmp_path, [account, account])
    plaintext = []

    def encrypt(value, *, certificate):
        plaintext.append(copy.deepcopy(value))
        return b"encrypted-cms"

    monkeypatch.setattr(migration, "encrypt_rebase_proposal", encrypt)

    assert migration.main(["scope-preview"]) == 0
    output = capsys.readouterr().out
    rows = plaintext[0]["non_managed_nonzero_spot_assets"]
    assert {row["asset"] for row in rows} == {"DOGE", "测试币", "１２３４５６"}
    assert "零余额币" not in {row["asset"] for row in rows}
    assert all(asset not in output for asset in ("测试币", "１２３４５６", "零余额币"))
    assert all(quantity not in output for quantity in ("1.25", "0.5", "0.1"))


@pytest.mark.parametrize("invalid_asset", ["BAD-ASSET", "BAD\x01ASSET", "资" * 21])
def test_scope_preview_rejects_punctuation_control_or_overlong_assets(
    monkeypatch, tmp_path, invalid_asset
):
    account = _account()
    account["balances"].append(
        {"asset": invalid_asset, "free": "0", "locked": "0"}
    )
    _, _, _, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [account, account]
    )

    with pytest.raises(migration.MigrationBlocked, match="private_scope_balance_invalid"):
        migration.run("scope-preview", now=NOW)
    assert not encrypted_path.exists()


def test_scope_preview_rejects_duplicate_unicode_asset(monkeypatch, tmp_path):
    account = _account()
    account["balances"].extend(
        [
            {"asset": "测试币", "free": "0", "locked": "0"},
            {"asset": "测试币", "free": "0", "locked": "0"},
        ]
    )
    _, _, _, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [account, account]
    )

    with pytest.raises(migration.MigrationBlocked, match="private_scope_balance_invalid"):
        migration.run("scope-preview", now=NOW)
    assert not encrypted_path.exists()


@pytest.mark.parametrize(
    "second,reason",
    [
        (_account(unknown_free="2.6"), "private_scope_snapshot_changed"),
        (_account(uid="456"), "account_scope_unverified"),
    ],
)
def test_scope_preview_rejects_changed_balance_or_identity(
    monkeypatch, tmp_path, second, reason
):
    _, _, client, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [_account(), second]
    )
    with pytest.raises(migration.MigrationBlocked, match=reason):
        migration.run("scope-preview", now=NOW)
    assert client.calls == 2
    assert not encrypted_path.exists()


@pytest.mark.parametrize("field,value", [("free", "-1"), ("locked", "NaN"), ("free", "Infinity")])
def test_scope_preview_rejects_negative_or_nonfinite_spot_components(
    monkeypatch, tmp_path, field, value
):
    account = _account()
    account["balances"][-2][field] = value
    _, _, _, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [account, account]
    )
    with pytest.raises(migration.MigrationBlocked, match="private_scope_balance_invalid"):
        migration.run("scope-preview", now=NOW)
    assert not encrypted_path.exists()


def test_scope_preview_rejects_archive_change_after_broker_reads(
    monkeypatch, tmp_path
):
    _, archive_ref, _, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [_account(), _account()]
    )

    def mutate_on_second_read(ref):
        if ref.calls == 2:
            ref.snapshot.value["private_unexpected_field"] = True

    archive_ref.on_get = mutate_on_second_read
    with pytest.raises(migration.MigrationBlocked, match="private_scope_source_changed"):
        migration.run("scope-preview", now=NOW)
    assert not encrypted_path.exists()


def test_scope_preview_rejects_environment_identity_not_approved_by_archive(
    monkeypatch, tmp_path
):
    account = _account(uid="999")
    _, _, client, encrypted_path = _post_rebase_setup(
        monkeypatch, tmp_path, [account, account]
    )
    monkeypatch.setattr(
        migration,
        "_expected_digests",
        lambda: {"account_scope_sha256": migration.digest({"account_uid": "999"})},
    )

    with pytest.raises(migration.MigrationBlocked, match="account_scope_unverified"):
        migration.run("scope-preview", now=NOW)
    assert client.calls == 0
    assert not encrypted_path.exists()


@pytest.mark.parametrize("failure", ["encryption", "broker"])
def test_scope_preview_failure_is_sanitized_and_never_writes_plaintext(
    monkeypatch, tmp_path, capsys, failure
):
    account = _account(unknown_free="987654.321")
    accounts = [account, account]
    if failure == "broker":
        accounts = [TimeoutError("987654.321-private provider payload")]
    _, _, _, encrypted_path = _post_rebase_setup(monkeypatch, tmp_path, accounts)
    encryption_calls = []
    if failure == "encryption":
        def fail_encryption(*args, **kwargs):
            encryption_calls.append(True)
            raise migration.MigrationBlocked("proposal_encryption_failed")

        monkeypatch.setattr(
            migration,
            "encrypt_rebase_proposal",
            fail_encryption,
        )

    assert migration.main(["scope-preview"]) == 2
    output = capsys.readouterr().out
    assert "987654.321" not in output
    assert json.loads(output)["reason_code"] == (
        "proposal_encryption_failed" if failure == "encryption" else "migration_blocked"
    )
    assert encryption_calls == ([True] if failure == "encryption" else [])
    assert not encrypted_path.exists()
