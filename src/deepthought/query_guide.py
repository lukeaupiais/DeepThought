"""How to ask the graph the questions it answers better than text search.

``GUIDE`` is the queryGraph tool's description: what the graph holds, when to
use it instead of the shell, and worked queries for the relationship questions
behind "what does this change touch". It is general on purpose: the queries
take any declaration by file and line, and nothing in them names a project.
``RECIPES`` holds the same queries by name, so tests can run each one through
the read-only guard and against a real graph.
"""

USERS_OF_A_TYPE = """\
MATCH (t:Symbol {file_path: $file}) WHERE t.start_line <= $line <= t.end_line
WITH t ORDER BY t.end_line - t.start_line LIMIT 1
MATCH (m:Symbol {file_path: t.file_path}) WHERE m = t OR m.name STARTS WITH t.name + "."
MATCH (u)-[r:USES_TYPE|SIGNATURE_USES|READS_FIELD|WRITES_FIELD|CALLS|REFERENCES_FUNCTION]->(m)
WITH u, type(r) AS how, split(m.name, ".")[-1] AS what, m
WITH u,
  collect(DISTINCT CASE WHEN how IN ["READS_FIELD", "WRITES_FIELD"] THEN m END) AS fields,
  [w IN collect(DISTINCT CASE how WHEN "READS_FIELD" THEN what END) WHERE w IS NOT NULL] AS reads,
  [w IN collect(DISTINCT CASE how WHEN "WRITES_FIELD" THEN what END) WHERE w IS NOT NULL] AS writes,
  [w IN collect(DISTINCT CASE WHEN how IN ["CALLS", "REFERENCES_FUNCTION"] THEN what END) WHERE w IS NOT NULL] AS calls
WITH u, size(fields) AS fields_touched, reads, writes, calls
ORDER BY fields_touched DESC, coalesce(u.file_path, u.path), u.start_line
WITH collect({u: u, n: fields_touched, r: reads, w: writes, c: calls}) AS found
UNWIND found[0..$top] AS x
RETURN size(found) AS users_in_total, x.u AS user, x.n AS fields_touched,
  x.w AS writes, x.r AS reads, x.c AS calls"""

CALLERS = """\
MATCH (s:Symbol {file_path: $file}) WHERE s.start_line <= $line <= s.end_line
WITH s ORDER BY s.end_line - s.start_line LIMIT 1
MATCH p = (d)-[:CALLS|DISPATCHES_TO|REFERENCES_FUNCTION*1..4]->(s) WHERE d <> s
WITH d, min(length(p)) AS calls_away
ORDER BY calls_away, coalesce(d.file_path, d.path), d.start_line
WITH collect({n: calls_away, d: d}) AS found
UNWIND found[0..$top] AS x
RETURN size(found) AS callers_in_total, x.n AS calls_away, x.d AS caller"""

BETWEEN = """\
MATCH (a:Symbol {file_path: $from_file}) WHERE a.start_line <= $from_line <= a.end_line
WITH a ORDER BY a.end_line - a.start_line LIMIT 1
MATCH (b:Symbol {file_path: $to_file}) WHERE b.start_line <= $to_line <= b.end_line
WITH a, b ORDER BY b.end_line - b.start_line LIMIT 1
OPTIONAL MATCH p = allShortestPaths((a)-[:CALLS|DISPATCHES_TO|REFERENCES_FUNCTION*..10]->(b))
WITH a, b, collect(p) AS down
OPTIONAL MATCH q = allShortestPaths((b)-[:CALLS|DISPATCHES_TO|REFERENCES_FUNCTION*..10]->(a))
WITH down, collect(q) AS up
WITH (down + up)[0..20] AS chains
UNWIND chains AS c
UNWIND range(1, length(c) - 1) AS i
WITH nodes(c)[i] AS n, i
RETURN min(i) AS step, n AS on_the_way ORDER BY step, coalesce(n.file_path, n.path), n.start_line"""

SHARED_DATA = """\
MATCH (a:Symbol {file_path: $from_file}) WHERE a.start_line <= $from_line <= a.end_line
WITH a ORDER BY a.end_line - a.start_line LIMIT 1
MATCH (b:Symbol {file_path: $to_file}) WHERE b.start_line <= $to_line <= b.end_line
WITH a, b ORDER BY b.end_line - b.start_line LIMIT 1
OPTIONAL MATCH (w)-[:CALLS*1..3]->(a)
WITH a, b, collect(DISTINCT w) AS ws
OPTIONAL MATCH (b)-[:CALLS*1..2]->(r)
WITH a, b, ws, collect(DISTINCT r) AS rs
WITH ws + [a] AS writers, rs + [b] AS readers
UNWIND writers AS w
MATCH (w)-[:WRITES_FIELD]->(f:Field)<-[:READS_FIELD]-(r) WHERE r IN readers
WITH f, collect(DISTINCT w) AS written_by, collect(DISTINCT r) AS read_by
RETURN f AS field, written_by, read_by ORDER BY f.file_path, f.start_line LIMIT 30"""

IMPLEMENTATIONS = """\
MATCH (s:Symbol {file_path: $file}) WHERE s.start_line <= $line <= s.end_line
WITH s ORDER BY s.end_line - s.start_line LIMIT 1
OPTIONAL MATCH (i:Symbol)-[:DISPATCHES_TO]->(s)
WITH s, collect(i) + [s] AS methods
UNWIND methods AS m
MATCH (m)-[d:DISPATCHES_TO]->(impl)
RETURN d AS implementation ORDER BY impl.file_path, impl.start_line"""

TESTS = """\
MATCH (s:Symbol {file_path: $file}) WHERE s.start_line <= $line <= s.end_line
WITH s ORDER BY s.end_line - s.start_line LIMIT 1
MATCH p = (t)-[:CALLS|DISPATCHES_TO|REFERENCES_FUNCTION*1..4]->(s)
WHERE coalesce(t.file_path, t.path) =~ $test_path_pattern
WITH t, min(length(p)) AS calls_away
ORDER BY calls_away, coalesce(t.file_path, t.path), t.start_line
WITH collect({n: calls_away, t: t}) AS found
UNWIND found[0..$top] AS x
RETURN size(found) AS tests_in_total, x.n AS calls_away, x.t AS test"""

USES = """\
MATCH (s:Symbol {file_path: $file}) WHERE s.start_line <= $line <= s.end_line
WITH s ORDER BY s.end_line - s.start_line LIMIT 1
MATCH (s)-[r:CALLS|REFERENCES_FUNCTION|USES_TYPE|READS_FIELD|WRITES_FIELD|REQUESTS]->(x)
WITH type(r) AS how, collect(DISTINCT x) AS found
RETURN how, size(found) AS in_total, found[0..$top] AS what ORDER BY how"""

RECIPES = {
    "users_of_a_type": USERS_OF_A_TYPE,
    "callers": CALLERS,
    "between": BETWEEN,
    "shared_data": SHARED_DATA,
    "implementations": IMPLEMENTATIONS,
    "tests": TESTS,
    "uses": USES,
}

GUIDE = f"""\
Run one bounded, read-only Cypher query on the DeepThought graph of this \
repository at its analyzed commit. Pass values as $parameters; at most \
max_elements (up to 100) results come back.

What the graph is for. It answers relationship questions that text search \
answers badly or not at all: who uses this declaration, in every way (calls, \
function values, type names, field reads and writes, calls through \
interfaces); what lies on the call chains between two places; everything up \
the callers to an entry point or a test. Read code with the shell (rg, sed). \
Do not fetch source text through the graph (s.source_code): it is the file \
you can read anyway, and it fills the reply.

Working out what a change touches:
1. Find where the change starts with the shell.
2. For each declaration whose shape or behaviour changes, ask the graph who \
depends on it, with the query for its kind below. Go more than one step: a \
caller that must pass a new argument or handle a new result has callers too.
- A type, struct, class or one of its fields changes (a field added, its type \
changed): users of the type (query 1). Users that touch most of its fields handle the \
value as a whole (build, copy, compare, hash, serialize, print, convert it); \
they come first and usually change with it.
- A function's parameters, result or behaviour change: its callers, up to four \
calls away, nearest first (query 2).
- New information must get from where it is produced to where it is used: \
the declarations on the call chains between the two, and the fields that \
pass data between them (queries 3 and 4). Each declaration that passes the information along \
changes, and so does each type that carries it (then ask for that type's \
users, query 1).
- The change calls an interface method, or adds one: every implementation, \
test doubles and mocks included (query 5).
- Tests: the tests that reach a declaration (query 6); what their shared helpers call (query 7) \
shows the test setup the change may need.
3. Decide each candidate by reading its use line, not the whole file, and \
say why it changes.

Keep replies small: return declarations and links, not source; give $top \
(e.g. 30) to the queries that take it, which also return the total, so the \
rest is a count. A cut reply says so; narrow it rather than reading the \
rest by hand.

What the graph holds:
- (:Symbol {{name, file_path, start_line, end_line, kind}}) is a declaration; \
members are "Owner.member", fields too (kind "Field"). Pick one by file and a \
line inside it, as the queries do: names repeat.
- (caller)-[:CALLS {{line}}]->(callee). The caller is the declaration whose \
body holds the call, or the (:File {{path}}) for code outside any.
- (interface method)-[:DISPATCHES_TO]->(implementation).
- (user)-[:REFERENCES_FUNCTION {{line, sites}}]->(function): named as a value \
(handler, callback, table entry). -[:USES_TYPE]-> and -[:SIGNATURE_USES]->: \
a type named in a body or in a signature. -[:READS_FIELD]-> and \
-[:WRITES_FIELD]-> a field Symbol. One link per user and target, at its \
first line, with the number of sites.
- evidence_type "resolved" means a compiler index or type checker linked it; \
"name_matched" or "pattern_matched" means a name rule did: check it in source.
- (caller)-[:HAS_UNRESOLVED_CALL]->(:UnresolvedCall {{name, file_path, line, \
reason}}) is a call nothing resolved.
- (:Contract {{name}}) is a gRPC method or an HTTP route; \
(caller)-[:REQUESTS]-> it and (handler)-[:HANDLES]-> it.
- (:File)-[:IMPORTS]->(:File); (:EdgeCoverage {{family, language, status}}) \
says how completely each family of links was found.
- $test_path_pattern is always available: a regular expression for test file \
paths (coalesce(n.file_path, n.path) =~ $test_path_pattern).

How replies read (format "text"): a declaration is "Name (file:line)"; a link \
"A calls B at line N"; a chain "A calls B at line 4, which calls C at line 9". \
Values repeated in every row are said once at the top, and results in test \
files come last. The first line says whether the limit cut the result. \
"Coverage:" says whether the link families in the result are complete and \
compiler-checked (then do not search for more) or may be incomplete (then \
check the source). A "Text check" line says whether a text search for the \
declaration would find more than the reply; if it lists lines, read only those.

Queries ($file and $line: any line inside the declaration):
1. Users of a type or class and its fields and methods, those touching the \
most fields first (anchored on one field, the users of that field):
{USERS_OF_A_TYPE}

2. Callers, direct and up to four calls away (through interface methods and \
function values too), nearest first:
{CALLERS}

3. Declarations on the shortest call chains between two declarations (either \
one may call the other), in call order:
{BETWEEN}

4. Fields written by one declaration or its callers (up to three calls away) \
and read by another or what it calls (up to two away): the data that passes \
between them, when no call chain joins them or to see what a chain carries:
{SHARED_DATA}

5. Implementations of an interface method (anchored on an implementation: \
its siblings):
{IMPLEMENTATIONS}

6. Tests that reach a declaration, nearest first:
{TESTS}

7. What a declaration calls, names and reads or writes, by kind of use:
{USES}
"""
