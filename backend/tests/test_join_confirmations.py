import os
import sys
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

os.environ.setdefault("DATABASE_URL", "sqlite://")
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from app.db import Base
from app.mapping_store import (
    confirm_join_confirmation,
    consume_join_confirmation,
    create_join_confirmation,
)
from app.models import Chat


class JoinConfirmationStoreTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.sessions = sessionmaker(bind=self.engine, expire_on_commit=False)
        with self.sessions() as db:
            db.add(Chat(
                chat_id="chat-1", user_id="user-1", title="test",
                created_at=1.0, updated_at=1.0,
            ))
            db.commit()
        self.patch_sessions = patch("app.mapping_store.SessionLocal", self.sessions)
        self.patch_sessions.start()
        self.addCleanup(self.patch_sessions.stop)
        self.addCleanup(self.engine.dispose)
        self.candidate = {
            "left": {"file_id": "file-a", "worksheet_id": "sheet-1", "column_id": "c1"},
            "right": {"file_id": "file-b", "worksheet_id": "table-1", "column_id": "c2"},
            "source_identities": [],
        }

    def test_confirmation_is_owner_scoped_one_time_and_expires(self):
        confirmation_id = create_join_confirmation(
            chat_id="chat-1", user_id="user-1", candidate=self.candidate
        )
        self.assertFalse(confirm_join_confirmation(
            chat_id="chat-1", user_id="other-user", confirmation_id=confirmation_id
        ))
        self.assertTrue(confirm_join_confirmation(
            chat_id="chat-1", user_id="user-1", confirmation_id=confirmation_id
        ))
        self.assertFalse(confirm_join_confirmation(
            chat_id="chat-1", user_id="user-1", confirmation_id=confirmation_id
        ))
        self.assertEqual(consume_join_confirmation(
            chat_id="chat-1", user_id="user-1", confirmation_id=confirmation_id
        ), self.candidate)
        self.assertIsNone(consume_join_confirmation(
            chat_id="chat-1", user_id="user-1", confirmation_id=confirmation_id
        ))

        expired_id = create_join_confirmation(
            chat_id="chat-1", user_id="user-1", candidate=self.candidate
        )
        with patch("app.mapping_store.time.time", return_value=4_000_000_000):
            self.assertFalse(confirm_join_confirmation(
                chat_id="chat-1", user_id="user-1", confirmation_id=expired_id
            ))
            self.assertIsNone(consume_join_confirmation(
                chat_id="chat-1", user_id="user-1", confirmation_id=expired_id
            ))


if __name__ == "__main__":
    unittest.main()
