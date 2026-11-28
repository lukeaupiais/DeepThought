"""Where a call's value goes and which of the caller's parameters it passes
on (``CallSite.result_use`` and ``CallSite.forwards``), and how replies say
them."""

import unittest

from deepthought.result_text import _flow
from deepthought.syntax import extract, extract_call_sites, make_parser


def sites(language, suffix, source):
    content = source.encode()
    tree = make_parser(language, suffix).parse(content)
    definitions = extract(tree, content, "r", "x" + suffix)
    return {
        (s.line, s.callee): (s.result_use, s.forwards)
        for s in extract_call_sites(tree, content, "r", "x" + suffix, definitions)
    }


class GoValueFlowTests(unittest.TestCase):
    def test_result_use_and_forwarded_parameters(self):
        found = sites(
            "go",
            ".go",
            """package p
func (r *R) f(a, b int, c ...string) error {
	x.y = g(a)
	v, err := k(b)
	go w(a)
	defer z()
	func() { inner(a) }()
	s.m[i] = q(v)
	return h(b, c...)
}
func shadow(a int) { if t { a := 1; use(a) } }
""",
        )
        self.assertEqual(found[(3, "g")], ("stored", ("0:a",)))
        self.assertEqual(found[(4, "k")], ("assigned", ("0:b",)))
        self.assertEqual(found[(5, "w")], ("dropped", ("0:a",)))
        self.assertEqual(found[(6, "z")], ("dropped", None))
        # A function literal's call passes on none of the caller's own.
        self.assertEqual(found[(7, "inner")], ("dropped", None))
        self.assertEqual(found[(8, "q")], ("stored", None))
        self.assertEqual(found[(9, "h")], ("returned", ("0:b", "1:c")))
        # A name declared again in the body may not be the parameter.
        self.assertEqual(found[(11, "use")], ("dropped", None))


class ScriptValueFlowTests(unittest.TestCase):
    def test_result_use_and_forwarded_parameters(self):
        found = sites(
            "typescript",
            ".ts",
            """function f(a: number, c = 1, ...d) {
  this.x = g(a);
  const y = m2(a);
  m3(c);
  return await h(c, ...d);
}
const k = (q) => m(q);
""",
        )
        self.assertEqual(found[(2, "g")], ("stored", ("0:a",)))
        self.assertEqual(found[(3, "m2")], ("assigned", ("0:a",)))
        self.assertEqual(found[(4, "m3")], ("dropped", ("0:c",)))
        self.assertEqual(found[(5, "h")], ("returned", ("0:c", "1:d")))
        self.assertEqual(found[(7, "m")], ("returned", ("0:q",)))


class PythonValueFlowTests(unittest.TestCase):
    def test_result_use_and_forwarded_parameters(self):
        found = sites(
            "python",
            ".py",
            """def f(self, a, b: int, c=1, *d, e: int = 2, **kw):
    self.x = g(a)
    y = m(b)
    m2(c)
    lst.append(n(self))
    return h(b, key=e, *d)
""",
        )
        self.assertEqual(found[(2, "g")], ("stored", ("0:a",)))
        self.assertEqual(found[(3, "m")], ("assigned", ("0:b",)))
        self.assertEqual(found[(4, "m2")], ("dropped", ("0:c",)))
        # ``self`` is not a parameter a caller passes.
        self.assertEqual(found[(5, "n")], ("passed", None))
        self.assertEqual(found[(6, "h")], ("returned", ("0:b", "key=e", "1:d")))


class FlowTextTests(unittest.TestCase):
    def test_flow_in_words(self):
        self.assertEqual(
            _flow({"result_use": "returned", "forwards": ["0:a", "key=b"]}),
            "returns its result, passes on its parameters a, b",
        )
        self.assertEqual(_flow({"result_use": "assigned"}), "")
        self.assertEqual(
            _flow({"forwards": ["1:opts"]}), "passes on its parameter opts"
        )


if __name__ == "__main__":
    unittest.main()
