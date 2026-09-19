"""Property tests for tools/smoke-tool-calling.py (pure parts, no network).

The smoke decides which deployments enter the `tools` route allowlist
(fork/tools_route.py), so its verdict must be strict: a model that answers
with prose, calls another function, or returns arguments that are not a
JSON object with the city, must fail. Step two (the `role: tool` reply)
must be built from the model's own call id, and the run's verdict must be
"every attempt of every deployment passed" -- k/n < n is a failure.
"""
import json
import unittest

from hypothesis import given, settings
from hypothesis import strategies as st

from fork import tools_route
from tests._loader import load_script

smoke = load_script("tools/smoke-tool-calling.py")

TOOL = smoke.TOOL_NAME

city_st = st.text(min_size=1, max_size=30).filter(str.strip)
call_id_st = st.from_regex(r"\A[A-Za-z0-9_\-]{1,24}\Z")
json_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10))


def _tool_call(call_id: str, name: str, arguments: str) -> dict:
    return {"id": call_id, "type": "function",
            "function": {"name": name, "arguments": arguments}}


def _good_message(call_id: str, city: str, extra: dict) -> dict:
    args = dict(extra)
    args["city"] = city
    return {"role": "assistant", "content": None,
            "tool_calls": [_tool_call(call_id, TOOL, json.dumps(args, ensure_ascii=False))]}


good_message_st = st.builds(
    _good_message, call_id=call_id_st, city=city_st,
    extra=st.dictionaries(st.text(min_size=1, max_size=6).filter(lambda k: k != "city"),
                          json_scalars, max_size=3))


# ─── Request builder ────────────────────────────────────────────────────────

class TestRequestBuilder(unittest.TestCase):
    @given(st.text(min_size=1, max_size=40), city_st, st.text(min_size=1, max_size=12))
    def test_one_function_tool_auto_choice_uncached(self, model, city, nonce):
        req = smoke.build_request(model, city, nonce)
        self.assertEqual(req["model"], model)
        self.assertEqual(req["tool_choice"], "auto")
        self.assertEqual(len(req["tools"]), 1)
        fn = req["tools"][0]
        self.assertEqual(fn["type"], "function")
        self.assertEqual(fn["function"]["name"], TOOL)
        params = fn["function"]["parameters"]
        self.assertEqual(params["type"], "object")
        self.assertEqual(params["properties"]["city"]["type"], "string")
        self.assertIn("city", params["required"])
        self.assertEqual(req["cache"], {"no-cache": True})
        self.assertFalse(req.get("stream", False))
        json.loads(json.dumps(req))

    @given(st.text(min_size=1, max_size=40), city_st, st.text(min_size=1, max_size=12),
           st.booleans())
    def test_no_fallback_pins_the_request_to_the_addressed_deployment(self, model, city, nonce, flag):
        req = smoke.build_request(model, city, nonce, no_fallback=flag)
        self.assertEqual(req.get("fallbacks"), [] if flag else None)
        self.assertNotIn("fallbacks", smoke.build_request(model, city, nonce))
        # the follow-up inherits it: step two must not wander off either
        _, _, call = smoke.check_tool_call(_good_message("c1", city, {}))
        second = smoke.build_followup(req, _good_message("c1", city, {}), call)
        self.assertEqual(second.get("fallbacks"), req.get("fallbacks"))

    @given(st.text(min_size=1, max_size=40), city_st, st.text(min_size=1, max_size=12))
    def test_prompt_names_the_city_and_the_nonce(self, model, city, nonce):
        req = smoke.build_request(model, city, nonce)
        user = req["messages"][-1]
        self.assertEqual(user["role"], "user")
        self.assertIn(city, user["content"])
        self.assertIn(nonce, user["content"])

    @given(st.text(min_size=1, max_size=40), city_st,
           st.text(min_size=1, max_size=12), st.text(min_size=1, max_size=12))
    def test_distinct_nonces_give_distinct_prompts(self, model, city, a, b):
        if a == b:
            return
        self.assertNotEqual(smoke.build_request(model, city, a)["messages"],
                            smoke.build_request(model, city, b)["messages"])


# ─── Step one: the tool call ────────────────────────────────────────────────

class TestToolCallCheck(unittest.TestCase):
    @given(good_message_st)
    def test_accepts_a_well_formed_call(self, message):
        ok, reason, call = smoke.check_tool_call(message)
        self.assertTrue(ok, reason)
        self.assertEqual(call["function"]["name"], TOOL)
        self.assertEqual(call["id"], message["tool_calls"][0]["id"])

    @given(good_message_st)
    def test_accepts_the_call_after_a_json_round_trip(self, message):
        ok, reason, _ = smoke.check_tool_call(json.loads(json.dumps(message)))
        self.assertTrue(ok, reason)

    @given(st.text(max_size=60))
    def test_rejects_prose_without_tool_calls(self, content):
        # drop_params strips `tools` on a backend that cannot call them:
        # the answer is plain text. This is the case the route exists for.
        for message in ({"role": "assistant", "content": content},
                        {"role": "assistant", "content": content, "tool_calls": []},
                        {"role": "assistant", "content": content, "tool_calls": None}):
            ok, reason, _ = smoke.check_tool_call(message)
            self.assertFalse(ok)
            self.assertIn("tool_calls", reason)

    @given(good_message_st, st.text(min_size=1, max_size=20))
    def test_rejects_another_function(self, message, other):
        if other == TOOL:
            return
        message["tool_calls"][0]["function"]["name"] = other
        ok, reason, _ = smoke.check_tool_call(message)
        self.assertFalse(ok)
        self.assertIn(repr(other), reason)

    @given(good_message_st, st.text(max_size=20))
    def test_rejects_arguments_that_are_not_json(self, message, junk):
        try:
            json.loads(junk)
            return  # happens to be JSON; covered by the next test
        except ValueError:
            pass
        message["tool_calls"][0]["function"]["arguments"] = junk
        ok, reason, _ = smoke.check_tool_call(message)
        self.assertFalse(ok)
        self.assertIn("JSON", reason)

    @given(good_message_st, st.one_of(json_scalars, st.lists(json_scalars, max_size=2)))
    def test_rejects_json_that_is_not_an_object(self, message, value):
        message["tool_calls"][0]["function"]["arguments"] = json.dumps(value)
        ok, _, _ = smoke.check_tool_call(message)
        self.assertFalse(ok)

    @given(good_message_st, st.one_of(st.none(), st.integers(), st.just(""), st.just("  ")))
    def test_rejects_a_missing_or_empty_city(self, message, bad_city):
        args = json.loads(message["tool_calls"][0]["function"]["arguments"])
        if bad_city is None:
            del args["city"]
        else:
            args["city"] = bad_city
        message["tool_calls"][0]["function"]["arguments"] = json.dumps(args)
        ok, reason, _ = smoke.check_tool_call(message)
        self.assertFalse(ok)
        self.assertIn("city", reason)

    def test_arguments_given_as_an_object_are_accepted(self):
        # Some backends hand the object through instead of a JSON string.
        message = _good_message("c1", "Paris", {})
        message["tool_calls"][0]["function"]["arguments"] = {"city": "Paris"}
        ok, reason, _ = smoke.check_tool_call(message)
        self.assertTrue(ok, reason)


# ─── Step two: the tool reply and the final answer ──────────────────────────

class TestFollowUp(unittest.TestCase):
    @given(st.text(min_size=1, max_size=40), city_st, st.text(min_size=1, max_size=12),
           good_message_st)
    def test_second_request_replays_the_call_and_answers_it_by_id(self, model, city, nonce, message):
        first = smoke.build_request(model, city, nonce)
        _, _, call = smoke.check_tool_call(message)
        second = smoke.build_followup(first, message, call)
        self.assertEqual(second["model"], model)
        self.assertEqual(second["tools"], first["tools"])
        self.assertEqual(second["messages"][:len(first["messages"])], first["messages"])
        self.assertEqual(second["messages"][-2], message)
        reply = second["messages"][-1]
        self.assertEqual(reply["role"], "tool")
        self.assertEqual(reply["tool_call_id"], call["id"])
        self.assertEqual(json.loads(reply["content"])["city"],
                         json.loads(call["function"]["arguments"])["city"])
        json.loads(json.dumps(second))

    @given(st.text(min_size=1, max_size=40), city_st, st.text(min_size=1, max_size=12),
           good_message_st)
    def test_first_request_is_not_mutated(self, model, city, nonce, message):
        first = smoke.build_request(model, city, nonce)
        snapshot = json.dumps(first, sort_keys=True)
        _, _, call = smoke.check_tool_call(message)
        smoke.build_followup(first, message, call)
        self.assertEqual(json.dumps(first, sort_keys=True), snapshot)


class TestFinalAnswerCheck(unittest.TestCase):
    @given(st.text(min_size=1, max_size=60).filter(str.strip))
    def test_accepts_non_empty_text(self, text):
        ok, reason = smoke.check_final(
            {"role": "assistant", "content": text, "tool_calls": None})
        self.assertTrue(ok, reason)

    @given(st.sampled_from([None, "", "   "]))
    def test_rejects_empty_text(self, blank):
        ok, _ = smoke.check_final({"role": "assistant", "content": blank})
        self.assertFalse(ok)

    @given(good_message_st, st.text(max_size=20))
    def test_rejects_another_tool_call_instead_of_an_answer(self, message, content):
        message["content"] = content
        ok, reason = smoke.check_final(message)
        self.assertFalse(ok)
        self.assertIn("tool_calls", reason)


# ─── Deployments from /model/info ───────────────────────────────────────────

_entry_st = st.builds(
    lambda name, model, base, mode, mid: {
        "model_name": name,
        "litellm_params": {"model": model, **({"api_base": base} if base else {})},
        "model_info": {"id": mid, "mode": mode},
    },
    name=st.sampled_from(["alpha", "beta", "standard", "tools", "gamma"]),
    model=st.sampled_from(["gemini/x", "groq/y", "openai/z"]),
    base=st.sampled_from(["", "https://a/v1", "https://b/v1"]),
    mode=st.sampled_from(["chat", "chat", "embedding"]),
    mid=st.from_regex(r"\A[0-9a-f]{4}\Z"),
)


class TestDeploymentIndex(unittest.TestCase):
    @settings(max_examples=200)
    @given(st.lists(_entry_st, max_size=20))
    def test_one_id_per_backend_generated_routes_skipped_chat_only(self, info):
        picked = smoke.unique_deployments(info)
        keys = [k for k, _ in picked]
        self.assertEqual(len(keys), len(set(keys)))
        expected = []
        for e in info:
            if e["model_name"] in smoke.GENERATED_ROUTES or e["model_info"]["mode"] != "chat":
                continue
            p = e["litellm_params"]
            k = tools_route.verified_key(p["model"], p.get("api_base", ""))
            if k not in expected:
                expected.append(k)
        self.assertEqual(keys, expected)
        ids = {e["model_info"]["id"] for e in info}
        for _, mid in picked:
            self.assertIn(mid, ids)

    @given(st.lists(_entry_st, max_size=20))
    def test_index_maps_every_id_to_the_allowlist_key(self, info):
        index = smoke.deployment_index(info)
        last = {}  # a repeated id (never in a real proxy) resolves to its last entry
        for e in info:
            p = e["litellm_params"]
            last[e["model_info"]["id"]] = tools_route.verified_key(p["model"], p.get("api_base", ""))
        self.assertEqual(index, last)


# ─── Verdict ────────────────────────────────────────────────────────────────

attempt_st = st.builds(
    smoke.Attempt, ok=st.booleans(), reason=st.just(""), model_id=st.text(max_size=4),
    api_base=st.just(""), ms=st.just(0), served_model=st.just(""), group=st.just(""),
    deployment=st.sampled_from(["", "gemini/a", "gemini/b", "openai/c @ https://h/v1"]),
)


class TestVerdict(unittest.TestCase):
    @given(st.dictionaries(st.text(min_size=1, max_size=8),
                           st.lists(st.booleans(), min_size=1, max_size=5), min_size=1))
    def test_all_pass_iff_every_attempt_ok(self, table):
        results = {m: [smoke.Attempt(ok=o, reason="", model_id="", api_base="", ms=0) for o in oks]
                   for m, oks in table.items()}
        self.assertEqual(smoke.all_passed(results), all(all(v) for v in table.values()))

    def test_empty_run_is_a_failure(self):
        self.assertFalse(smoke.all_passed({}))
        self.assertFalse(smoke.all_passed({"m": []}))

    @given(st.dictionaries(st.text(min_size=1, max_size=6), st.lists(attempt_st, max_size=8)))
    def test_verified_means_every_attributed_attempt_passed(self, results):
        verified = smoke.verified_deployments(results)
        seen = {}
        for attempts in results.values():
            for a in attempts:
                if a.deployment:
                    seen.setdefault(a.deployment, []).append(a.ok)
        self.assertEqual(verified, {d for d, oks in seen.items() if all(oks)})
        self.assertNotIn("", verified)


class TestFallbackRejection(unittest.TestCase):
    """Addressed by deployment id, an answer from another backend (the
    catch-all `*` chain took over) must not count for the addressed one."""

    @given(attempt_st, st.text(max_size=4), st.text(max_size=4))
    def test_ok_survives_only_when_the_addressed_deployment_answered(self, a, expected, served):
        a.model_id = served
        was_ok = a.ok
        out = smoke.reject_fallback(expected, a)
        self.assertIs(out, a)
        self.assertEqual(out.ok, was_ok and (not expected or served == expected))
        if was_ok and not out.ok:
            self.assertIn("fallback", out.reason)


class TestAllowlistVocabulary(unittest.TestCase):
    def test_smoke_and_route_spell_the_key_the_same_way(self):
        for model in ["gemini/x", "openai/y"]:
            for base in ["", "https://h/v1"]:
                self.assertEqual(smoke.deployment_key(model, base),
                                 tools_route.verified_key(model, base))


if __name__ == "__main__":
    unittest.main()
