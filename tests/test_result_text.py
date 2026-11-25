"""The text form of raw Cypher results: sentences from the graph's own
vocabulary, nothing lost, only numbers merged."""

import ast
from pathlib import Path
import re
import unittest

from deepthought import result_text
from deepthought.storage.graph.schema import RELATIONSHIP_SENTENCES

SOURCE = Path(__file__).resolve().parents[1] / "src" / "deepthought"


def symbol(eid, name, path, line, labels=("Function", "Symbol")):
    return {
        "element_id": eid,
        "labels": sorted(labels),
        "properties": {"name": name, "file_path": path, "start_line": line},
    }


def file_node(eid, path):
    return {
        "element_id": eid,
        "labels": ["File"],
        "properties": {"path": path, "file_path": path, "language": "go"},
    }


def link(kind, source, target, **properties):
    return {
        "element_id": source["element_id"] + kind + target["element_id"],
        "edge_id": None,
        "type": kind,
        "source": source["element_id"],
        "target": target["element_id"],
        "properties": {"evidence_type": "resolved", **properties},
    }


def path(*steps):
    """path(a, line, b, line, c): nodes with the CALLS line between each."""
    nodes = list(steps[0::2])
    links = [
        link("CALLS", nodes[i], nodes[i + 1], line=steps[2 * i + 1])
        for i in range(len(nodes) - 1)
    ]
    return {"nodes": nodes, "relationships": links}


TARGET = symbol("t", "Command.InheritedFlags", "command.go", 1744)
HAS = symbol("a", "Command.HasInheritedFlags", "command.go", 1816)
WRITE = symbol("b", "writeFlags", "bash_completions.go", 551)
GEN = symbol("c", "gen", "bash_completions.go", 652)
TEST = symbol("d", "TestNormPassedOnInherited", "command_test.go", 1823)
TEST_FILE = file_node("e", "command_test.go")
KNOWN = {n["element_id"]: n for n in (TARGET, HAS, WRITE, GEN, TEST, TEST_FILE)}


def render(result, **options):
    """Render with the items the service would look up for links."""
    options.setdefault("nodes", KNOWN)
    return result_text.render(result, **options)


def response(items, truncated=False):
    return _response(items, truncated)


def _response(items, truncated):
    return {
        "status": "succeeded",
        "items": items,
        "returned": len(items),
        "truncated": truncated,
    }


class ResultTextTests(unittest.TestCase):
    def test_callers_read_as_sentences_about_one_target(self):
        rows = [
            {"r": link("CALLS", HAS, TARGET, line=1817)},
            {"r": link("CALLS", WRITE, TARGET, line=580)},
            {"r": link("CALLS", TEST, TARGET, line=1839)},
            {"r": link("CALLS", TEST, TARGET, line=1844)},
        ]
        text = render(response(rows))
        self.assertEqual(
            text.splitlines(),
            [
                "4 results (all of them).",
                "Each of the following calls Command.InheritedFlags (command.go:1744):",
                "- Command.HasInheritedFlags (command.go:1816) at line 1817",
                "- writeFlags (bash_completions.go:551) at line 580",
                "In tests:",
                "- TestNormPassedOnInherited (command_test.go:1823, test) at lines 1839, 1844",
            ],
        )

    def test_one_link_is_a_whole_sentence(self):
        text = render(response([{"r": link("CALLS", HAS, TARGET, line=1817)}]))
        self.assertIn(
            "- Command.HasInheritedFlags (command.go:1816) calls "
            "Command.InheritedFlags (command.go:1744) at line 1817",
            text,
        )

    def test_link_ends_the_query_left_out_are_named_from_the_lookup(self):
        rows = [{"r": link("CALLS", HAS, TARGET, line=1817)}]
        text = render(response(rows), nodes={"a": HAS, "t": TARGET})
        self.assertIn("Command.HasInheritedFlags (command.go:1816) calls", text)
        missing = render(response(rows), nodes={})
        self.assertIn("(item not returned)", missing)

    def test_chains_read_as_one_sentence_and_name_the_shared_end_it(self):
        rows = [
            {"steps": 1, "chain": path(WRITE, 580, TARGET)},
            {"steps": 2, "chain": path(GEN, 675, WRITE, 580, TARGET)},
            {"steps": 2, "chain": path(TEST_FILE, 12, WRITE, 580, TARGET)},
        ]
        lines = render(response(rows)).splitlines()
        self.assertEqual(
            lines[1],
            'Each chain below ends at Command.InheritedFlags (command.go:1744) ("it"):',
        )
        self.assertIn("steps 1:", lines)
        self.assertIn(
            "  - writeFlags (bash_completions.go:551) calls it at line 580", lines
        )
        self.assertIn(
            "  - gen (bash_completions.go:652) calls writeFlags "
            "(bash_completions.go:551) at line 675, "
            "which calls it at line 580",
            lines,
        )
        self.assertIn(
            "  - command_test.go (file, test) calls writeFlags "
            "(bash_completions.go:551) at line 12, "
            "which calls it at line 580",
            lines,
        )

    def test_every_item_in_a_chain_gives_its_location_once(self):
        # A hop's line is in the calling item's file, so every item is placed
        # on first mention; same-named items are told apart the same way.
        bun = symbol("x", "toSSG", "src/adapter/bun/ssg.ts", 27)
        helper = symbol("y", "toSSG", "src/helper/ssg/ssg.ts", 375)
        fetch = symbol("z", "fetchRoutesContent", "src/helper/ssg/ssg.ts", 209)
        text = render(response([{"p": path(bun, 28, helper, 438, fetch)}]))
        self.assertIn(
            "toSSG (src/adapter/bun/ssg.ts:27) calls toSSG (src/helper/ssg/ssg.ts:375) "
            "at line 28, which calls fetchRoutesContent (src/helper/ssg/ssg.ts:209) "
            "at line 438",
            text,
        )
        loop = render(response([{"p": path(bun, 28, helper, 380, bun)}]))
        self.assertIn(
            "- toSSG (src/adapter/bun/ssg.ts:27) calls toSSG (src/helper/ssg/ssg.ts:375) "
            "at line 28, which calls toSSG at line 380",
            loop,
        )

    def test_kinds_other_than_functions_and_methods_are_named(self):
        cls = symbol("k", "Context", "src/context.ts", 293)
        cls["properties"]["kind"] = "Class"
        alias = symbol("m", "Handler", "src/types.ts", 12)
        alias["properties"]["kind"] = "TypeAlias"
        method = symbol("n", "Context.json", "src/context.ts", 700)
        method["properties"]["kind"] = "Method"
        text = render(response([{"n": cls}, {"n": alias}, {"n": method}]))
        self.assertIn("- Context (class, src/context.ts:293)", text)
        self.assertIn("- Handler (type alias, src/types.ts:12)", text)
        self.assertIn("- Context.json (src/context.ts:700)", text)

    def test_a_column_null_in_every_result_is_said_once(self):
        rows = [{"n": TARGET, "signature": None}, {"n": HAS, "signature": None}]
        self.assertIn("Same for every result: signature none.", render(response(rows)))
        one = render(response([{"n": TARGET, "signature": None}]))
        self.assertIn("signature none", one)
        mixed = render(
            response([{"n": TARGET, "signature": "()"}, {"n": HAS, "signature": None}])
        )
        self.assertNotIn("none", mixed)

    def test_only_numbers_merge_never_names_or_files(self):
        bun = symbol("x", "toSSG", "src/adapter/bun/ssg.ts", 27)
        deno = symbol("y", "toSSG", "src/adapter/deno/ssg.ts", 27)
        rows = [
            {"r": link("CALLS", bun, TARGET, line=28)},
            {"r": link("CALLS", deno, TARGET, line=28)},
        ]
        text = render(response(rows), nodes={**KNOWN, "x": bun, "y": deno})
        self.assertIn("- toSSG (src/adapter/bun/ssg.ts:27) at line 28", text)
        self.assertIn("- toSSG (src/adapter/deno/ssg.ts:27) at line 28", text)

    def test_plain_values_keep_their_labels_shared_values_said_once(self):
        rows = [
            {"file": "a.go", "line": 3, "status": "linked"},
            {"file": "a.go", "line": 9, "status": "linked"},
            {"file": "a.go", "line": 12, "status": "linked"},
            {"file": "b.go", "line": 4, "status": "linked"},
        ]
        self.assertEqual(
            render(response(rows)).splitlines(),
            [
                "4 results (all of them).",
                "Same for every result: status linked.",
                "file a.go:",
                "  - line 3, 9, 12",
                "file b.go:",
                "  - line 4",
            ],
        )

    def test_a_cut_result_says_so(self):
        text = render(response([{"n": 1}], truncated=True), limit=1)
        self.assertTrue(
            text.startswith(
                "1 result, cut at the limit: more exist. Narrow the "
                "query or raise max_elements above 1."
            )
        )
        self.assertEqual(render(response([])), "No results.")

    def test_coverage_line_names_families_and_languages_in_the_result(self):
        coverage = [
            {"family": "CALLS", "protocol": None, "language": "go", "status": "linked"},
            {
                "family": "CALLS",
                "protocol": None,
                "language": "python",
                "status": "linked",
            },
            {
                "family": "REQUESTS",
                "protocol": "http",
                "language": "go",
                "status": "not_linked",
                "reason": "parser_rules",
            },
        ]
        rows = [{"r": link("CALLS", HAS, TARGET, line=1817)}]
        text = render(
            response(rows), coverage=coverage, query="MATCH ()-[r:CALLS]->() RETURN r"
        )
        self.assertTrue(
            text.endswith("Coverage: CALLS in go: complete, compiler-checked.")
        )
        text = render(
            response([{"file": "main.go"}]),
            coverage=coverage,
            query="MATCH (c)-[:REQUESTS]->(g) RETURN c.file_path AS file",
        )
        self.assertTrue(
            text.endswith(
                "Coverage: REQUESTS (http) in go: may be incomplete (parser_rules), "
                "check the source."
            )
        )
        # Asking about coverage itself gets no coverage footer.
        self.assertNotIn(
            "Coverage:",
            render(
                response(rows),
                coverage=coverage,
                query="MATCH (e:EdgeCoverage) RETURN e",
            ),
        )

    def test_unlinked_calls_and_evidence_short_of_resolved(self):
        unlinked = {
            "element_id": "u",
            "labels": ["UnresolvedCall"],
            "properties": {
                "callee": "buf.WriteString",
                "file_path": "doc/rest_docs.go",
                "line": 35,
                "status": "unresolved",
                "reason": "scip_external",
            },
        }
        text = render(
            response([{"h": link("HAS_UNRESOLVED_CALL", WRITE, unlinked, line=35)}]),
            nodes={**KNOWN, "u": unlinked},
        )
        self.assertIn(
            "writeFlags (bash_completions.go:551) makes the unlinked call buf.WriteString "
            "(doc/rest_docs.go:35, unresolved, scip_external)",
            text,
        )
        guessed = link("CALLS", HAS, TARGET, line=3)
        guessed["properties"]["evidence_type"] = "name_matched"
        self.assertIn("[name matched] at line 3", render(response([{"r": guessed}])))

    def test_nothing_is_lost(self):
        rows = [
            {"steps": 2, "chain": path(GEN, 675, WRITE, 580, TARGET), "n": 7},
            {"steps": 1, "chain": path(TEST, 1839, TARGET), "n": 8},
            {"steps": 1, "chain": path(TEST, 1844, TARGET), "n": 8},
        ]
        text = render(response(rows))
        for value in [
            "675",
            "580",
            "1839",
            "1844",
            "7",
            "8",
            "gen",
            "writeFlags",
            "command_test.go:1823",
            "bash_completions.go:652",
            "command.go:1744",
        ]:
            self.assertIn(value, text)

    def test_every_relationship_type_the_engine_writes_has_a_sentence(self):
        written = set()
        for file in SOURCE.rglob("*.py"):
            tree = ast.parse(file.read_text())
            helpers = {"RelationshipData"}
            # Local helpers such as link(kind, ...) pass their first argument on.
            for function in ast.walk(tree):
                if isinstance(function, ast.FunctionDef) and function.args.args:
                    first = function.args.args[0].arg
                    for call in ast.walk(function):
                        if (
                            isinstance(call, ast.Call)
                            and getattr(call.func, "id", None) == "RelationshipData"
                            and call.args
                            and isinstance(call.args[0], ast.Name)
                            and call.args[0].id == first
                        ):
                            helpers.add(function.name)
            for call in ast.walk(tree):
                name = getattr(call, "func", None)
                name = getattr(name, "id", getattr(name, "attr", None))
                if isinstance(call, ast.Call) and name in helpers and call.args:
                    first = call.args[0]
                    if isinstance(first, ast.Constant) and isinstance(first.value, str):
                        written.add(first.value)
        self.assertGreaterEqual(len(written), 20)
        self.assertEqual(sorted(written - set(RELATIONSHIP_SENTENCES)), [])
        for kind, verb in RELATIONSHIP_SENTENCES.items():
            self.assertRegex(kind, r"^[A-Z][A-Z_]+$")
            self.assertTrue(
                verb and verb == verb.strip() and not re.search(r"[{}]", verb)
            )

    def test_an_unknown_relationship_type_still_reads(self):
        text = render(response([{"r": link("NEW_KIND", HAS, TARGET)}]))
        self.assertIn(
            "Command.HasInheritedFlags (command.go:1816) new_kind Command.InheritedFlags",
            text,
        )

    def test_contract_clients_and_handlers_read_as_requests(self):
        contract = {
            "element_id": "g",
            "labels": ["Contract", "GrpcMethod"],
            "properties": {
                "name": "hipstershop.ProductCatalogService/GetProduct",
                "file_path": "protos/demo.proto",
                "line": 73,
            },
        }
        client = symbol(
            "k", "checkoutService.prepOrderItems", "src/checkoutservice/main.go", 339
        )
        handler = symbol(
            "h",
            "productCatalog.GetProduct",
            "src/productcatalogservice/product_catalog.go",
            47,
        )
        known = {**KNOWN, "g": contract, "k": client, "h": handler}
        rows = [
            {"link": link("REQUESTS", client, contract, line=344)},
            {"link": link("HANDLES", handler, contract)},
        ]
        lines = render(response(rows), nodes=known).splitlines()
        self.assertEqual(
            lines[1:],
            [
                "- checkoutService.prepOrderItems (src/checkoutservice/main.go:339) sends a "
                "request to hipstershop.ProductCatalogService/GetProduct (protos/demo.proto:73) "
                "at line 344",
                "- productCatalog.GetProduct (src/productcatalogservice/product_catalog.go:47) "
                "handles hipstershop.ProductCatalogService/GetProduct (protos/demo.proto:73)",
            ],
        )


if __name__ == "__main__":
    unittest.main()
