import json
import subprocess
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from fmjev.core import APIError, DecisionService, FMBackend, loads
from fmjev.server import handler_for


def request(kind="choice"):
    criteria = {"yes": "yes", "no": "no"} if kind == "choice" else ["low", "middle", "high"]
    question = {"type": kind, "instructions": "Judge this state."}
    if kind != "noul":
        question["criteria"] = criteria
    return {"state": "example", "questions": {"test": question}}


class FakeBackend:
    def __init__(self, raw):
        self.raw = raw
        self.prompts = []

    def generate(self, schema, instructions, prompt, remaining):
        self.prompts.append(json.loads(prompt))
        return self.raw


class DecisionTests(unittest.TestCase):
    def evaluate(self, raw, kind="choice"):
        return DecisionService(FakeBackend(raw)).evaluate(request(kind))["answers"]["test"]

    def test_choice_tie_and_entropy(self):
        answer = self.evaluate({"p0": 0.5, "p1": 0.5})
        self.assertEqual(answer["choice"], "yes")
        self.assertAlmostEqual(answer["confidence"], 0)
        self.assertEqual(self.evaluate({"p0": 0, "p1": 1})["confidence"], 1)

    def test_score_is_expected_value_not_argmax(self):
        answer = self.evaluate({"p0": 0, "p1": 0.7, "p2": 0.3}, "score")
        self.assertAlmostEqual(answer["score"], 1.3)
        self.assertEqual(answer["legend"], {"0": "low", "1": "middle", "2": "high"})

    def test_noul_has_no_confidence(self):
        self.assertEqual(self.evaluate({"noul": 0.25}, "noul"), {"type": "noul", "noul": 0.25})

    def test_rounding_is_normalized(self):
        answer = self.evaluate({"p0": 0.49, "p1": 0.5})
        self.assertAlmostEqual(sum(answer["probabilities"].values()), 1)

    def test_invalid_outputs_are_not_silently_repaired(self):
        for raw in ({"p0": 0, "p1": 0}, {"p0": 1, "p1": 1}, {"p0": True, "p1": 0},
                    {"p0": float("nan"), "p1": 0}, {"p0": -0.1, "p1": 1},
                    {"p0": 0.5}, {"p0": 0.5, "p1": 0.5, "extra": 0}):
            with self.subTest(raw=raw), self.assertRaises(APIError) as error:
                self.evaluate(raw)
            self.assertEqual(error.exception.status, 502)

    def test_all_inputs_validated_before_inference(self):
        backend = FakeBackend({"p0": 1, "p1": 0})
        body = request()
        body["questions"]["bad"] = {"type": "score", "instructions": "x", "criteria": ["one"]}
        with self.assertRaises(APIError):
            DecisionService(backend).evaluate(body)
        self.assertEqual(backend.prompts, [])

    def test_questions_are_isolated_and_ids_hidden(self):
        backend = FakeBackend({"noul": 0.5})
        body = request("noul")
        body["questions"]["second"] = {"type": "noul", "instructions": "Different question"}
        DecisionService(backend).evaluate(body)
        self.assertEqual(len(backend.prompts), 2)
        self.assertNotIn("Different question", json.dumps(backend.prompts[0]))
        self.assertNotIn("questions", backend.prompts[0])

    def test_busy_and_lock_released_on_failure(self):
        service = DecisionService(FakeBackend({}))
        service.lock.acquire()
        with self.assertRaises(APIError) as error:
            service.evaluate(request())
        self.assertEqual(error.exception.status, 429)
        service.lock.release()
        with self.assertRaises(APIError):
            service.evaluate(request())
        self.assertFalse(service.lock.locked())

    def test_model_name_not_impersonated(self):
        body = request()
        body["model"] = "jev-latest"
        with self.assertRaises(APIError):
            DecisionService().evaluate(body)

    def test_strict_json(self):
        for raw in ('{"a":1,"a":2}', '{"a":NaN}', '{"a":1e999}'):
            with self.assertRaises(ValueError):
                loads(raw)


class BackendTests(unittest.TestCase):
    @patch("fmjev.core.subprocess.run")
    def test_prompt_is_stdin_and_no_shell(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, '{"noul":0.4}', "")
        result = FMBackend().generate({}, "judge", "$(do not execute)", 10)
        self.assertEqual(result, {"noul": 0.4})
        self.assertEqual(run.call_args.kwargs["input"], "$(do not execute)")
        self.assertNotIn("shell", run.call_args.kwargs)
        self.assertEqual(run.call_args.kwargs["timeout"], 10)

    @patch("fmjev.core.subprocess.run")
    def test_timeout(self, run):
        run.side_effect = subprocess.TimeoutExpired("fm", 1)
        with self.assertRaises(APIError) as error:
            FMBackend().generate({}, "judge", "state", 1)
        self.assertEqual(error.exception.status, 504)

    @patch("fmjev.core.subprocess.run")
    def test_failure_does_not_echo_private_data(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, "", "private state")
        with self.assertRaises(APIError) as error:
            FMBackend().generate({}, "judge", "private state", 1)
        self.assertNotIn("private state", str(error.exception))


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(DecisionService(FakeBackend({"noul": 0.1}))))
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.url = f"http://127.0.0.1:{cls.server.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()

    def test_health_and_success(self):
        with urlopen(self.url + "/health") as response:
            self.assertEqual(json.load(response)["status"], "ok")
        req = Request(self.url + "/v1/systemone", data=json.dumps(request("noul")).encode(),
                      headers={"Content-Type": "application/json"})
        with urlopen(req) as response:
            self.assertEqual(json.load(response)["answers"]["test"]["noul"], 0.1)

    def test_invalid_json_is_400(self):
        req = Request(self.url + "/v1/systemone", data=b'{"state":NaN}',
                      headers={"Content-Type": "application/json"})
        with self.assertRaises(HTTPError) as error:
            urlopen(req)
        self.assertEqual(error.exception.code, 400)
        self.assertEqual(json.load(error.exception)["error"]["code"], "invalid_json")
        error.exception.close()


if __name__ == "__main__":
    unittest.main()
