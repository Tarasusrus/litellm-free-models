"""Property tests for tools/smoke-json-schema.py (pure parts, no network).

The smoke tool reports which deployments really honour a strict
`response_format=json_schema` through the proxy: one that silently drops
it (the proxy runs with `drop_params: true`) must fail here. So the
validator and the request builder are checked against generated data, not
a handful of hand-written examples.
"""
import json
import re
import unittest
from datetime import date

from hypothesis import given, settings
from hypothesis import strategies as st

from tests._loader import load_script

smoke = load_script("tools/smoke-json-schema.py")

SCHEMA = smoke.VACANCY_SCHEMA


# ─── Strategies derived from the schema ─────────────────────────────────────

def _instance_from_schema(schema: dict) -> st.SearchStrategy:
    """Builds a strategy that only yields instances valid under `schema`
    (for the JSON-Schema subset the tool understands)."""
    if "anyOf" in schema:
        return st.one_of(*[_instance_from_schema(s) for s in schema["anyOf"]])
    if "enum" in schema:
        return st.sampled_from(schema["enum"])
    t = schema["type"]
    if t == "null":
        return st.none()
    if t == "boolean":
        return st.booleans()
    if t == "integer":
        return st.integers(min_value=-(10**9), max_value=10**9)
    if t == "number":
        return st.floats(allow_nan=False, allow_infinity=False)
    if t == "string":
        return st.text(max_size=40)
    if t == "array":
        return st.lists(_instance_from_schema(schema["items"]), max_size=6)
    if t == "object":
        props = schema.get("properties", {})
        return st.fixed_dictionaries({k: _instance_from_schema(v) for k, v in props.items()})
    raise AssertionError(f"unsupported schema node: {schema}")


valid_instances = _instance_from_schema(SCHEMA)
json_scalars = st.one_of(st.none(), st.booleans(), st.integers(), st.text(max_size=10))


class TestSchemaShape(unittest.TestCase):
    """The schema is 'strict' in the OpenAI sense: everything required,
    nothing extra. Otherwise a provider could return {} and still pass."""

    def test_strict_object(self):
        self.assertEqual(SCHEMA["type"], "object")
        self.assertIs(SCHEMA["additionalProperties"], False)
        self.assertEqual(sorted(SCHEMA["required"]), sorted(SCHEMA["properties"]))
        self.assertGreaterEqual(len(SCHEMA["properties"]), 4)

    def test_nested_objects_are_strict_too(self):
        def walk(node):
            if node.get("type") == "object":
                self.assertIs(node.get("additionalProperties"), False)
                self.assertEqual(sorted(node["required"]), sorted(node["properties"]))
                for child in node["properties"].values():
                    walk(child)
            for child in node.get("anyOf", []):
                walk(child)
            if "items" in node:
                walk(node["items"])
        walk(SCHEMA)


class TestValidator(unittest.TestCase):
    @given(valid_instances)
    def test_accepts_every_schema_conforming_instance(self, inst):
        self.assertEqual(smoke.validate(inst, SCHEMA), [])

    @given(valid_instances, st.sampled_from(sorted(SCHEMA["required"])))
    def test_rejects_missing_required_key(self, inst, key):
        broken = dict(inst)
        del broken[key]
        errors = smoke.validate(broken, SCHEMA)
        self.assertTrue(any(key in e and "required" in e for e in errors), errors)

    @given(valid_instances, st.text(min_size=1, max_size=12), json_scalars)
    def test_rejects_extra_key(self, inst, extra_key, value):
        if extra_key in SCHEMA["properties"]:
            return
        broken = dict(inst)
        broken[extra_key] = value
        errors = smoke.validate(broken, SCHEMA)
        self.assertTrue(any("additional" in e for e in errors), errors)

    @given(valid_instances, st.sampled_from(sorted(SCHEMA["properties"])), json_scalars)
    def test_rejects_wrong_type(self, inst, key, wrong):
        # only keep replacements that really violate the property's schema
        if smoke.validate(wrong, SCHEMA["properties"][key]) == []:
            return
        broken = dict(inst)
        broken[key] = wrong
        self.assertNotEqual(smoke.validate(broken, SCHEMA), [])

    @given(st.one_of(json_scalars, st.lists(json_scalars, max_size=3)))
    def test_rejects_non_objects_at_top_level(self, inst):
        self.assertNotEqual(smoke.validate(inst, SCHEMA), [])

    def test_bool_is_not_integer(self):
        # Python's bool subclasses int; JSON Schema keeps them apart.
        self.assertNotEqual(smoke.validate(True, {"type": "integer"}), [])
        self.assertEqual(smoke.validate(3, {"type": "integer"}), [])

    @given(valid_instances)
    def test_survives_json_round_trip(self, inst):
        self.assertEqual(smoke.validate(json.loads(json.dumps(inst)), SCHEMA), [])


class TestVacancyPrompts(unittest.TestCase):
    """The RU vacancy text is what the client (job-hunt) will send: short,
    Cyrillic, and unique per attempt so the response cache cannot answer."""

    @given(st.integers(min_value=0, max_value=10**6), st.text(min_size=1, max_size=16))
    def test_prompt_is_short_cyrillic_and_carries_the_nonce(self, i, nonce):
        text = smoke.build_vacancy(i, nonce)
        self.assertLessEqual(len(text), smoke.MAX_VACANCY_CHARS)
        self.assertLessEqual(smoke.MAX_VACANCY_CHARS, 300)
        self.assertIn(nonce, text)
        cyrillic = len(re.findall(r"[А-Яа-яЁё]", text))
        latin = len(re.findall(r"[A-Za-z]", text.replace(nonce, "")))
        self.assertGreater(cyrillic, latin)

    @given(st.integers(min_value=0, max_value=10**6),
           st.text(min_size=1, max_size=16), st.text(min_size=1, max_size=16))
    def test_distinct_nonces_give_distinct_prompts(self, i, a, b):
        if a == b:
            return
        self.assertNotEqual(smoke.build_vacancy(i, a), smoke.build_vacancy(i, b))


class TestRequestBuilder(unittest.TestCase):
    @given(st.text(min_size=1, max_size=40), st.text(min_size=1, max_size=200))
    def test_request_is_strict_json_schema_and_uncached(self, model, text):
        req = smoke.build_request(model, text)
        self.assertEqual(req["model"], model)
        rf = req["response_format"]
        self.assertEqual(rf["type"], "json_schema")
        self.assertIs(rf["json_schema"]["strict"], True)
        self.assertEqual(rf["json_schema"]["schema"], SCHEMA)
        self.assertEqual(req["cache"], {"no-cache": True})
        self.assertEqual(req["messages"][-1], {"role": "user", "content": text})
        # must be plain JSON (no Python-only types slipped in)
        json.loads(json.dumps(req))

    @given(st.text(min_size=1, max_size=40))
    def test_stream_is_never_requested(self, model):
        # a streamed answer has no single JSON body to validate
        self.assertFalse(smoke.build_request(model, "x").get("stream", False))


class TestVerdict(unittest.TestCase):
    """Exit status is the only thing a Makefile/CI sees: k/n < n must fail."""

    @given(st.lists(st.booleans(), min_size=1, max_size=20))
    def test_all_pass_iff_every_attempt_ok(self, oks):
        results = [smoke.Attempt(ok=o, reason="" if o else "boom", model_id="", api_base="", ms=0)
                   for o in oks]
        self.assertEqual(smoke.all_passed({"m": results}), all(oks))

    @given(st.dictionaries(st.text(min_size=1, max_size=8),
                           st.lists(st.booleans(), min_size=1, max_size=5), min_size=1))
    def test_one_bad_model_fails_the_run(self, table):
        results = {m: [smoke.Attempt(ok=o, reason="", model_id="", api_base="", ms=0) for o in oks]
                   for m, oks in table.items()}
        self.assertEqual(smoke.all_passed(results), all(all(v) for v in table.values()))

    def test_empty_run_is_a_failure(self):
        self.assertFalse(smoke.all_passed({}))
        self.assertFalse(smoke.all_passed({"m": []}))


attempt_st = st.builds(
    smoke.Attempt,
    ok=st.booleans(), reason=st.just(""), model_id=st.text(max_size=4),
    api_base=st.just(""), ms=st.just(0), served_model=st.just(""), group=st.just(""),
    deployment=st.sampled_from(["", "gemini/a", "gemini/b", "openai/c"]),
)


class TestVerifiedDeployments(unittest.TestCase):
    """A deployment earns the allowlist only if nothing attributed to it failed."""

    @given(st.dictionaries(st.text(min_size=1, max_size=6), st.lists(attempt_st, max_size=8)))
    def test_no_failed_attempt_behind_a_verified_deployment(self, results):
        verified = smoke.verified_deployments(results)
        self.assertNotIn("", verified)
        for attempts in results.values():
            for a in attempts:
                if a.deployment in verified:
                    self.assertTrue(a.ok)
        # and every verified deployment really answered at least once
        seen_ok = {a.deployment for at in results.values() for a in at if a.ok and a.deployment}
        self.assertTrue(verified <= seen_ok)

    @given(st.dictionaries(st.text(min_size=1, max_size=6), st.lists(attempt_st, max_size=8)))
    def test_every_clean_deployment_is_verified(self, results):
        verified = smoke.verified_deployments(results)
        flat = [a for at in results.values() for a in at if a.deployment]
        for dep in {a.deployment for a in flat}:
            if all(a.ok for a in flat if a.deployment == dep):
                self.assertIn(dep, verified)


info_entry = st.fixed_dictionaries({
    "model_name": st.sampled_from(["m1", "m2", "embedding-x", "standard"]),
    "litellm_params": st.fixed_dictionaries({
        "model": st.sampled_from(["gemini/a", "openai/b"]),
        "api_base": st.sampled_from(["", "https://a/v1", "https://b/v1"]),
    }),
    "model_info": st.fixed_dictionaries({
        "id": st.text(alphabet="0123456789abcdef", min_size=4, max_size=6),
        "mode": st.sampled_from(["chat", "embedding", "audio_transcription"]),
    }),
})


# one /model/info entry per deployment id, as the proxy reports it
info_list = st.lists(info_entry, max_size=12, unique_by=lambda e: e["model_info"]["id"])


class TestModelInfoParsing(unittest.TestCase):
    @given(info_list)
    def test_chat_names_are_chat_only_ordered_and_unique(self, info):
        names = smoke.chat_model_names(info)
        self.assertEqual(len(names), len(set(names)))
        chat = [e["model_name"] for e in info if e["model_info"]["mode"] == "chat"]
        self.assertEqual(set(names), set(chat))
        # first occurrence order is kept
        self.assertEqual(names, [n for n in dict.fromkeys(chat)])

    @given(info_list)
    def test_index_maps_every_id_to_its_deployment(self, info):
        index = smoke.deployment_index(info)
        for e in info:
            self.assertIn(e["litellm_params"]["model"], index[e["model_info"]["id"]])

    @given(info_list)
    def test_same_model_on_another_host_is_another_deployment(self, info):
        # `openai/<model>` is served by several providers (LLM7, NVIDIA,
        # OVHcloud, ...); the key must tell them apart by api_base.
        index = smoke.deployment_index(info)
        for a in info:
            for b in info:
                pa, pb = a["litellm_params"], b["litellm_params"]
                ka, kb = index[a["model_info"]["id"]], index[b["model_info"]["id"]]
                if pa["model"] == pb["model"] and pa["api_base"] == pb["api_base"]:
                    self.assertEqual(ka, kb)
                else:
                    self.assertNotEqual(ka, kb)

    @given(st.sampled_from(["gemini/a", "openai/b"]), st.sampled_from(["", "https://a/v1"]))
    def test_deployment_key_round_trips(self, model, api_base):
        key = smoke.deployment_key(model, api_base)
        self.assertEqual(smoke.split_deployment_key(key), (model, api_base))

    def test_missing_fields_are_skipped(self):
        self.assertEqual(smoke.deployment_index([{"model_info": {}, "litellm_params": {}}]), {})
        self.assertEqual(smoke.chat_model_names([{"model_info": {}}]), [])


class TestRejectFallback(unittest.TestCase):
    @given(st.text(min_size=1, max_size=6), st.text(alphabet="ab-", max_size=6), st.booleans())
    def test_only_foreign_group_flips_an_ok_answer(self, model, group, ok):
        a = smoke.Attempt(ok=ok, reason="", model_id="x", api_base="", ms=0, group=group)
        out = smoke.reject_fallback(model, a)
        expected = ok and not (group and group != model)
        self.assertEqual(out.ok, expected)
        if ok and not out.ok:
            self.assertIn(group, out.reason)


class TestVerifiedAllowlist(unittest.TestCase):
    """JSON_SCHEMA_VERIFIED is what config invariants trust; keep it well-formed."""

    def test_non_empty_with_iso_dates(self):
        self.assertTrue(smoke.JSON_SCHEMA_VERIFIED)
        for key, day in smoke.JSON_SCHEMA_VERIFIED.items():
            model, _ = smoke.split_deployment_key(key)
            self.assertRegex(model, r"^[a-z0-9_-]+/.+", key)
            date.fromisoformat(day)

    def test_openai_compatible_entries_name_their_host(self):
        # A bare `openai/<model>` names no provider: the same id exists on
        # LLM7, NVIDIA and OVHcloud with very different behaviour.
        for key in smoke.JSON_SCHEMA_VERIFIED:
            model, api_base = smoke.split_deployment_key(key)
            if model.startswith("openai/"):
                self.assertTrue(api_base, f"{key}: api_base missing")

    def test_dates_are_not_in_the_future(self):
        for day in smoke.JSON_SCHEMA_VERIFIED.values():
            self.assertLessEqual(date.fromisoformat(day), date.today())


settings.register_profile("ci", max_examples=200)
settings.load_profile("ci")

if __name__ == "__main__":
    unittest.main()
