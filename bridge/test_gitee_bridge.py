import base64
import hashlib
import json
import os
import unittest

from gitee_bridge import Config, OAuthStore, PairingManager, Policy, _pkce_s256


class BridgeUnitTests(unittest.TestCase):
    def config(self, **overrides):
        values = {
            "host": "127.0.0.1",
            "port": 48765,
            "upstream_url": "https://api.gitee.com/mcp",
            "public_url": "https://bridge.example.test",
            "gitee_token": "dummy",
            "allowed_repositories": frozenset({"yeuei/gpt---github---codex"}),
            "allowed_tools": frozenset(),
            "pairing_code": "ABCD2345",
        }
        values.update(overrides)
        return Config(**values)

    def test_pairing_is_single_use_and_case_insensitive(self):
        pairing = PairingManager("ABCD2345")
        self.assertTrue(pairing.consume("abcd2345"))
        self.assertFalse(pairing.consume("ABCD2345"))

    def test_pkce(self):
        verifier = "a" * 43
        expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        self.assertEqual(_pkce_s256(verifier), expected)

    def test_oauth_code_requires_pkce_and_is_one_time(self):
        store = OAuthStore()
        client = store.register("ChatGPT", ["https://chatgpt.example/callback"])
        verifier = "b" * 43
        code = store.issue_code(client["client_id"], client["redirect_uris"][0], _pkce_s256(verifier), "state")
        self.assertIsNotNone(store.redeem_code(code, client["client_id"], client["redirect_uris"][0], verifier))
        self.assertIsNone(store.redeem_code(code, client["client_id"], client["redirect_uris"][0], verifier))

    def test_policy_filters_tools_and_repositories(self):
        policy = Policy(self.config())
        payload = {"result": {"tools": [{"name": "get_pull_detail"}, {"name": "delete_repository"}]}}
        self.assertEqual(["get_pull_detail"], [tool["name"] for tool in policy.filter_tools(payload)["result"]["tools"]])
        self.assertTrue(policy.allows_arguments({"owner": "yeuei", "repo": "gpt---github---codex"}))
        self.assertFalse(policy.allows_arguments({"owner": "someone-else", "repo": "other"}))


if __name__ == "__main__":
    unittest.main()
