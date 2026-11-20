"""Focused persistence, migration, key and deletion regression tests."""

import os
import sys
import unittest
from unittest.mock import patch
from types import SimpleNamespace

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app import gemini_files, lifecycle, mapping_store
from app.crypto import (
    EncryptionConfigurationError,
    decrypt_text,
    encrypt_text,
    generate_key,
    is_encrypted,
    lookup_digest,
)
from app.db import Base
from app.key_management import CloudKMSKeyProvider, KeyManagementError
from app.models import Chat, ChatFile, ChatMessage, ConversationTurn, TokenEntry
from migrate_phase4_encryption import (
    migrate_chat_files,
    migrate_chat_titles,
    migrate_messages,
    migrate_token_entries,
)


class EncryptedPersistenceTests(unittest.TestCase):
    def setUp(self):
        os.environ["PRIVY_ENVIRONMENT"] = "development"
        os.environ["PRIVY_ENCRYPTION_KEY"] = generate_key()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        self.factory = sessionmaker(bind=self.engine, expire_on_commit=False)
        TokenEntry.__table__.create(self.engine)
        ChatFile.__table__.create(self.engine)
        Chat.__table__.create(self.engine)
        ChatMessage.__table__.create(self.engine)
        ConversationTurn.__table__.create(self.engine)
        self.original_factory = mapping_store.SessionLocal
        mapping_store.SessionLocal = self.factory

    def tearDown(self):
        mapping_store.SessionLocal = self.original_factory
        self.engine.dispose()
        os.environ.pop("PRIVY_ENCRYPTION_KEY", None)
        os.environ.pop("PRIVY_BLIND_INDEX_KEY", None)

    def test_mapping_is_encrypted_at_rest_and_readable_from_new_sessions(self):
        token = mapping_store.get_or_create_tokens(
            "chat-a",
            [("EMAIL", "alice@example.test")],
            {},
        )["alice@example.test"]
        with self.factory() as db:
            row = db.scalar(select(TokenEntry))
            self.assertNotIn("alice@example.test", row.original)
            self.assertNotIn("alice@example.test", row.value_norm)
            self.assertTrue(is_encrypted(row.original))
            self.assertEqual(row.value_norm, lookup_digest("chat-a", "alice@example.test"))

        self.assertEqual(mapping_store.get_known_values("chat-a"), {"alice@example.test": token})
        self.assertEqual(mapping_store.get_reverse_map("chat-a"), {token: "alice@example.test"})
        self.assertEqual(mapping_store.get_known_values("chat-b"), {})

    def test_conversation_turn_persists_encrypted_messages_and_replays_idempotently(self):
        with self.factory() as db:
            db.add(Chat(
                chat_id="turn-chat",
                user_id="user-a",
                title="title",
                created_at=1,
                updated_at=1,
            ))
            db.commit()

        started = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-1",
            request_fingerprint="a" * 64,
            question="private question",
            context_revision="b" * 64,
        )
        self.assertEqual(started["status"], "started")
        self.assertEqual(started["previous_messages"], [])

        with self.factory() as db:
            user_message = db.scalar(select(ChatMessage).where(ChatMessage.role == "user"))
            self.assertTrue(is_encrypted(user_message.content))
            self.assertNotIn("private question", user_message.content)

        mapping_store.finish_conversation_turn(
            "turn-chat",
            "request-1",
            "private answer",
            1,
            metadata={"provider": "groq"},
        )
        replay = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-1",
            request_fingerprint="a" * 64,
            question="private question",
            context_revision="b" * 64,
        )
        self.assertEqual(replay["status"], "completed")
        self.assertEqual(replay["assistant"]["content"], "private answer")
        self.assertEqual(replay["assistant"]["metadata"], {"provider": "groq"})

        conflict = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-1",
            request_fingerprint="c" * 64,
            question="different question",
            context_revision="b" * 64,
        )
        self.assertEqual(conflict["status"], "conflict")

    def test_interrupted_turn_can_resume_and_chat_turns_are_isolated(self):
        with self.factory() as db:
            db.add_all([
                Chat(
                    chat_id="turn-chat",
                    user_id="user-a",
                    title="title",
                    created_at=1,
                    updated_at=1,
                ),
                Chat(
                    chat_id="other-chat",
                    user_id="user-b",
                    title="title",
                    created_at=1,
                    updated_at=1,
                ),
            ])
            db.commit()

        mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-1",
            request_fingerprint="a" * 64,
            question="question",
            context_revision="b" * 64,
        )
        busy = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-2",
            request_fingerprint="c" * 64,
            question="second question",
            context_revision="b" * 64,
        )
        self.assertEqual(busy["status"], "busy")

        mapping_store.interrupt_conversation_turn("turn-chat", "request-1")
        resumed = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-1",
            request_fingerprint="a" * 64,
            question="question",
            context_revision="d" * 64,
        )
        self.assertEqual(resumed["status"], "resumed")
        self.assertEqual(resumed["previous_messages"], [])
        self.assertEqual(resumed["is_first_message"], True)

        with self.factory() as db:
            active_turn = db.scalar(
                select(ConversationTurn).where(
                    ConversationTurn.chat_id == "turn-chat",
                    ConversationTurn.request_id == "request-1",
                )
            )
            active_turn.updated_at = 0
            db.commit()
        recovered = mapping_store.begin_conversation_turn(
            chat_id="turn-chat",
            user_id="user-a",
            request_id="request-4",
            request_fingerprint="f" * 64,
            question="new question after abandoned request",
            context_revision="b" * 64,
        )
        self.assertEqual(recovered["status"], "started")

        with self.assertRaises(LookupError):
            mapping_store.begin_conversation_turn(
                chat_id="turn-chat",
                user_id="user-b",
                request_id="request-3",
                request_fingerprint="e" * 64,
                question="unauthorized",
                context_revision="b" * 64,
            )

    def test_file_fields_persist_as_ciphertext_and_decrypt_after_new_session(self):
        raw_csv = "Email\nalice@example.test\n"
        file_id = mapping_store.set_chat_file(
            chat_id="chat-a",
            filename="sensitive-report.csv",
            masked_csv=raw_csv,
            columns_json='[{"name":"Email"}]',
            row_count=1,
            truncated=False,
            privacy_policy_json='{"columns":{}}',
            workbook_json='{"schema_version":1,"worksheets":[]}',
        )
        with self.factory() as db:
            row = db.get(ChatFile, file_id)
            self.assertTrue(is_encrypted(row.filename))
            self.assertTrue(is_encrypted(row.masked_csv))
            self.assertTrue(is_encrypted(row.columns_json))
            self.assertTrue(is_encrypted(row.privacy_policy_json))
            self.assertTrue(is_encrypted(row.workbook_json))
            self.assertNotIn("sensitive-report.csv", row.filename)
            self.assertNotIn("alice@example.test", row.masked_csv)

        restored = mapping_store.get_chat_file("chat-a", file_id)
        self.assertEqual(restored["filename"], "sensitive-report.csv")
        self.assertEqual(restored["masked_csv"], raw_csv)
        self.assertEqual(restored["privacy_policy_json"], '{"columns":{}}')
        self.assertEqual(restored["workbook_json"], '{"schema_version":1,"worksheets":[]}')

    def test_chat_title_is_encrypted_at_rest_and_legacy_title_migrates(self):
        chat_id = mapping_store.create_chat("user-a", "Question about Alice@example.test")
        with self.factory() as db:
            row = db.get(Chat, chat_id)
            self.assertTrue(is_encrypted(row.title))
            self.assertNotIn("Alice@example.test", row.title)
        self.assertEqual(
            mapping_store.get_chat(chat_id, "user-a")["title"],
            "Question about Alice@example.test",
        )

        with self.factory() as db:
            db.add(
                Chat(
                    chat_id="legacy-chat",
                    user_id="user-a",
                    title="Legacy title",
                    created_at=1,
                    updated_at=1,
                )
            )
            db.commit()
            self.assertEqual(migrate_chat_titles(db, batch_size=1), (1, 0))
            self.assertEqual(migrate_chat_titles(db, batch_size=1), (0, 0))
            row = db.get(Chat, "legacy-chat")
            self.assertTrue(is_encrypted(row.title))
            self.assertEqual(decrypt_text(row.title, aad="chat-title:legacy-chat"), "Legacy title")

    def test_legacy_mapping_stays_readable_and_is_upgraded_on_lookup(self):
        with self.factory() as db:
            db.add(
                TokenEntry(
                    session_id="chat-a",
                    value_norm="alice@example.test",
                    token="[EMAIL_1]",
                    original="alice@example.test",
                    value_type="EMAIL",
                )
            )
            db.commit()
        result = mapping_store.get_or_create_tokens(
            "chat-a",
            [("EMAIL", "alice@example.test")],
            {},
        )
        self.assertEqual(result["alice@example.test"], "[EMAIL_1]")
        with self.factory() as db:
            row = db.scalar(select(TokenEntry))
            self.assertTrue(is_encrypted(row.original))
            self.assertEqual(row.value_norm, lookup_digest("chat-a", "alice@example.test"))

    def test_mapping_and_message_migrations_are_batched_and_idempotent(self):
        with self.factory() as db:
            db.add(
                TokenEntry(
                    session_id="chat-a",
                    value_norm="alice@example.test",
                    token="[EMAIL_1]",
                    original="alice@example.test",
                    value_type="EMAIL",
                )
            )
            db.add(
                ChatMessage(
                    chat_id="chat-a",
                    role="user",
                    content="Question containing Alice",
                    masked_count=0,
                    metadata_json="{}",
                    created_at=1,
                )
            )
            db.commit()
            self.assertEqual(migrate_token_entries(db, batch_size=1), (1, 0))
            self.assertEqual(migrate_token_entries(db, batch_size=1), (0, 0))
            self.assertEqual(migrate_messages(db, batch_size=1), (1, 0))
            self.assertEqual(migrate_messages(db, batch_size=1), (0, 0))
            token_row = db.scalar(select(TokenEntry))
            message_row = db.scalar(select(ChatMessage))
            self.assertEqual(
                decrypt_text(token_row.original, aad="token:chat-a:EMAIL"),
                "alice@example.test",
            )
            self.assertEqual(
                decrypt_text(message_row.content, aad="chat-message:chat-a:user"),
                "Question containing Alice",
            )

    def test_file_migration_is_idempotent_and_verifies_ciphertext(self):
        file_id = "file-a"
        with self.factory() as db:
            db.add(
                ChatFile(
                    file_id=file_id,
                    chat_id="chat-a",
                    filename="legacy.csv",
                    masked_csv="Email\nalice@example.test\n",
                    columns_json="[]",
                    privacy_policy_json=None,
                    row_count=1,
                    truncated=False,
                    masked_count=0,
                    created_at=1,
                    updated_at=1,
                )
            )
            db.commit()
            self.assertEqual(migrate_chat_files(db, batch_size=1), (1, 0))
            self.assertEqual(migrate_chat_files(db, batch_size=1), (0, 0))
            row = db.get(ChatFile, file_id)
            self.assertEqual(
                decrypt_text(row.masked_csv, aad=f"chat-file:{file_id}:masked_csv"),
                "Email\nalice@example.test\n",
            )

    def test_file_migration_keeps_failed_rows_legacy_and_retries(self):
        with self.factory() as db:
            for file_id, filename in (("file-a", "good.csv"), ("file-b", "v99:unsupported")):
                db.add(
                    ChatFile(
                        file_id=file_id,
                        chat_id="chat-a",
                        filename=filename,
                        masked_csv="data",
                        columns_json="[]",
                        privacy_policy_json=None,
                        row_count=0,
                        truncated=False,
                        masked_count=0,
                        created_at=1,
                        updated_at=1,
                    )
                )
            db.commit()
            self.assertEqual(migrate_chat_files(db, batch_size=1), (1, 1))
            failed_row = db.get(ChatFile, "file-b")
            self.assertEqual(failed_row.filename, "v99:unsupported")
            failed_row.filename = "retry.csv"
            db.commit()
            self.assertEqual(migrate_chat_files(db, batch_size=1), (1, 0))

    def test_kms_client_tracks_actual_crypto_key_version(self):
        provider = object.__new__(CloudKMSKeyProvider)
        provider.key_id = "projects/p/locations/l/keyRings/r/cryptoKeys/k"
        provider._client = SimpleNamespace(
            encrypt=lambda request: SimpleNamespace(
                ciphertext=b"wrapped",
                key_name=provider.key_id + "/cryptoKeyVersions/7",
            ),
            decrypt=lambda request: SimpleNamespace(plaintext=b"dek"),
        )
        wrapped, version = provider.wrap_dek(b"dek")
        self.assertEqual(wrapped, b"wrapped")
        self.assertTrue(version.endswith("/cryptoKeyVersions/7"))
        self.assertEqual(provider.unwrap_dek(wrapped, version), b"dek")

    def test_unsupported_version_and_tampering_fail_closed(self):
        with self.assertRaises(EncryptionConfigurationError):
            decrypt_text("v99:anything")
        ciphertext = encrypt_text("private", aad="record")
        parts = ciphertext.split(":")
        parts[-1] = ("A" if parts[-1][0] != "A" else "B") + parts[-1][1:]
        with self.assertRaises(EncryptionConfigurationError):
            decrypt_text(":".join(parts), aad="record")

    def test_wrong_local_key_fails_decryption(self):
        ciphertext = encrypt_text("private", aad="record")
        os.environ["PRIVY_ENCRYPTION_KEY"] = generate_key()
        with self.assertRaises(EncryptionConfigurationError):
            decrypt_text(ciphertext, aad="record")

    def test_blind_index_key_is_stable_and_validated_separately(self):
        os.environ["PRIVY_BLIND_INDEX_KEY"] = generate_key()
        os.environ["PRIVY_ENVIRONMENT"] = "production"
        first = lookup_digest("chat-a", "alice@example.test")
        self.assertEqual(first, lookup_digest("chat-a", "alice@example.test"))
        with patch.dict(os.environ, {"PRIVY_BLIND_INDEX_KEY": "invalid"}):
            with self.assertRaises(EncryptionConfigurationError):
                lookup_digest("chat-a", "alice@example.test")

    def test_production_missing_kms_configuration_does_not_fall_back(self):
        os.environ["PRIVY_ENVIRONMENT"] = "production"
        os.environ.pop("PRIVY_KMS_KEY_NAME", None)
        with patch.dict("sys.modules", {"google": None, "google.cloud": None}):
            with self.assertRaises(KeyManagementError):
                CloudKMSKeyProvider()
            with self.assertRaises(KeyManagementError):
                encrypt_text("private")


class DeletionSafetyTests(unittest.TestCase):
    def test_failed_gemini_cleanup_preserves_cache_for_retry(self):
        entries = {"file-a": {"file_name": "files/remote", "store_name": "stores/remote"}}
        with (
            patch.object(gemini_files, "_load_cache", return_value=entries),
            patch.object(gemini_files, "_delete_raw_file", return_value=False),
            patch.object(gemini_files, "_delete_store", return_value=True),
            patch.object(gemini_files, "_save_cache") as save_cache,
        ):
            self.assertFalse(gemini_files.delete_gemini_file_cache("file-a", "test-key"))
            self.assertIn("file-a", entries)
            save_cache.assert_not_called()

    def test_file_deletion_is_not_final_when_provider_cleanup_fails(self):
        with (
            patch.object(lifecycle.store, "get_chat_file", return_value={"file_id": "file-a"}),
            patch.object(lifecycle, "delete_gemini_file_cache", return_value=False),
            patch.object(lifecycle.store, "delete_chat_file") as local_delete,
        ):
            with self.assertRaises(lifecycle.ProviderCleanupPending):
                lifecycle.delete_file("chat-a", "file-a")
            local_delete.assert_not_called()

    def test_cross_user_chat_deletion_is_rejected(self):
        with (
            patch.object(lifecycle.store, "get_chat", return_value=None),
            patch.object(lifecycle.store, "delete_chat") as delete_chat,
        ):
            self.assertFalse(lifecycle.delete_chat("chat-a", "other-user"))
            delete_chat.assert_not_called()


if __name__ == "__main__":
    unittest.main()
