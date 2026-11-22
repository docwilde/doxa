import unittest
from unittest.mock import MagicMock, patch

import pilot_dispatch as dispatch


class PilotTransportTests(unittest.TestCase):
    def connection(self, payload, *, status=200, declared=None):
        connection = MagicMock()
        response = connection.getresponse.return_value.__enter__.return_value
        response.getheader.return_value = "identity"
        response.read.return_value = payload
        response.length = declared
        response.status = status
        return connection, response

    def test_exact_request_and_raw_redirect_response(self):
        connection, response = self.connection(b"redirect", status=307, declared=8)
        body = b'{ "token": "example" }\n'
        self.assertEqual(
            dispatch.post_exact(connection, "/wire", body, key="fixture", response_limit=64),
            (307, b"redirect"),
        )
        args, kwargs = connection.request.call_args
        self.assertEqual(args, ("POST", "/wire"))
        self.assertEqual(kwargs["body"], body)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer fixture")
        self.assertEqual(connection.request.call_count, 1)
        response.read.assert_called_once_with(65)
        connection.close.assert_called_once()

    def test_complete_json_in_truncated_response_is_refused(self):
        response_body = b'{"usage":{"total_tokens":42}}'
        connection, _ = self.connection(response_body, declared=len(response_body) + 1)
        with self.assertRaisesRegex(OSError, "declared length"):
            dispatch.post_exact(connection, "/wire", b"{}", key="fixture", response_limit=64)
        connection.close.assert_called_once()

    def test_advertised_response_limit_refuses_before_read(self):
        connection, response = self.connection(b"", declared=65)
        with self.assertRaisesRegex(OSError, "bound"):
            dispatch.post_exact(connection, "/wire", b"{}", key="fixture", response_limit=64)
        response.read.assert_not_called()
        connection.close.assert_called_once()

    def test_live_transports_use_pinned_hosts_and_require_both_routed_credentials(self):
        scope = ("easy-config", dispatch.JEV_ARM, 1)
        with patch.dict("os.environ", {"DEEPSEEK_API_KEY": "fixture-repair"}, clear=True), \
             patch.object(dispatch, "run_call") as run:
            with self.assertRaisesRegex(ValueError, "TYPESAFE_API_KEY"):
                dispatch.dispatch_call(None, None, None, scope, "{}")
            run.assert_not_called()
        with patch.dict("os.environ", {
            "DEEPSEEK_API_KEY": "fixture-repair",
            "TYPESAFE_API_KEY": "fixture-decision",
        }, clear=True), patch.object(dispatch, "run_call") as run, \
             patch.object(dispatch.http.client, "HTTPSConnection") as connect, \
             patch.object(dispatch, "post_exact") as post:
            def admitted(_adapter, _state, _ledger, actual_scope, _request, decision, repair):
                self.assertEqual(actual_scope, scope)
                decision(b'{"step":"decision"}')
                repair(b'{"step":"repair"}')
            run.side_effect = admitted
            dispatch.dispatch_call(None, None, None, scope, "{}")
            self.assertEqual([args[0] for args, _ in connect.call_args_list],
                             ["api.typesafe.ai", "api.deepseek.com"])
            self.assertEqual([args[1:] for args, _ in post.call_args_list],
                             [("/v1/systemone", b'{"step":"decision"}'),
                              ("/chat/completions", b'{"step":"repair"}')])
            self.assertEqual([kwargs["key"] for _, kwargs in post.call_args_list],
                             ["fixture-decision", "fixture-repair"])


if __name__ == "__main__":
    unittest.main()
