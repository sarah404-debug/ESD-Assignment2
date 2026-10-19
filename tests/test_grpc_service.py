"""Tests for the gRPC service (grpc_service/server.py).

Starts the real gRPC server in this process on a free port, with a throwaway
database, and calls it through the generated stub, like the gRPC client does.
Run from the project folder (needs grpcio, e.g. the venv):
    python -m unittest discover -s tests -v
"""
import os
import sys
import tempfile
import unittest
from concurrent import futures
from pathlib import Path
from unittest import mock

# Point the store at a temporary database BEFORE importing it.
_tmp = tempfile.TemporaryDirectory()
os.environ["QUEST_DB_PATH"] = os.path.join(_tmp.name, "test_grpc.db")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import grpc  # noqa: E402

from grpc_service.server import QuestService, RequestLoggingInterceptor  # noqa: E402
from quest_core import rules  # noqa: E402
from questpb import quest_pb2, quest_pb2_grpc  # noqa: E402


class GrpcServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = grpc.server(futures.ThreadPoolExecutor(max_workers=4),
                                 interceptors=[RequestLoggingInterceptor()])
        quest_pb2_grpc.add_QuestServiceServicer_to_server(QuestService(), cls.server)
        port = cls.server.add_insecure_port("127.0.0.1:0")  # 0 = any free port
        cls.server.start()
        cls.channel = grpc.insecure_channel(f"127.0.0.1:{port}")
        cls.stub = quest_pb2_grpc.QuestServiceStub(cls.channel)

    @classmethod
    def tearDownClass(cls):
        cls.channel.close()
        cls.server.stop(None)

    def resolve(self, instance_id, outcome, **kwargs):
        request = quest_pb2.ResolveQuestRequest(instance_id=instance_id, outcome=outcome)
        return self.stub.ResolveQuest.with_call(request, timeout=5, **kwargs)

    def assert_code(self, code, instance_id, outcome):
        with self.assertRaises(grpc.RpcError) as ctx:
            self.resolve(instance_id, outcome)
        self.assertEqual(ctx.exception.code(), code)

    def test_resolve_and_safe_repeat(self):
        quest, _ = rules.create_quest()
        response, _ = self.resolve(quest["instance_id"], quest_pb2.OUTCOME_COMPLETED)
        self.assertEqual(response.quest.status, "completed")
        self.assertTrue(response.changed)
        response, _ = self.resolve(quest["instance_id"], quest_pb2.OUTCOME_COMPLETED)
        self.assertFalse(response.changed)

    def test_change_is_visible_through_the_shared_rules(self):
        quest, _ = rules.create_quest()
        self.resolve(quest["instance_id"], quest_pb2.OUTCOME_ABANDONED)
        self.assertEqual(rules.get_quest(quest["instance_id"])["status"], "abandoned")

    def test_error_codes(self):
        quest, _ = rules.create_quest()
        qid = quest["instance_id"]
        self.assert_code(grpc.StatusCode.NOT_FOUND, "00000000-0000-0000-0000-000000000000",
                         quest_pb2.OUTCOME_COMPLETED)
        self.assert_code(grpc.StatusCode.INVALID_ARGUMENT, "abc", quest_pb2.OUTCOME_COMPLETED)
        self.assert_code(grpc.StatusCode.INVALID_ARGUMENT, qid, quest_pb2.OUTCOME_UNSPECIFIED)
        self.resolve(qid, quest_pb2.OUTCOME_SKIPPED)
        self.assert_code(grpc.StatusCode.FAILED_PRECONDITION, qid, quest_pb2.OUTCOME_COMPLETED)

    def test_request_id_from_caller_is_kept(self):
        quest, _ = rules.create_quest()
        _, call = self.resolve(quest["instance_id"], quest_pb2.OUTCOME_COMPLETED,
                               metadata=(("x-request-id", "test-id-123"),))
        self.assertEqual(dict(call.initial_metadata())["x-request-id"], "test-id-123")

    def test_invalid_request_id_is_replaced(self):
        quest, _ = rules.create_quest()
        _, call = self.resolve(quest["instance_id"], quest_pb2.OUTCOME_COMPLETED,
                               metadata=(("x-request-id", "bad id!"),))
        self.assertNotEqual(dict(call.initial_metadata())["x-request-id"], "bad id!")

    def test_unexpected_error_becomes_internal(self):
        quest, _ = rules.create_quest()
        with mock.patch.object(rules, "resolve_quest", side_effect=RuntimeError("boom")):
            self.assert_code(grpc.StatusCode.INTERNAL, quest["instance_id"], quest_pb2.OUTCOME_COMPLETED)


if __name__ == "__main__":
    unittest.main()
