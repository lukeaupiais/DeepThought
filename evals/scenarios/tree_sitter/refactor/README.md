# TreeSitterTool refactoring-navigation pilot

These public tasks evaluate the declaration tool through the production MCP and
Neo4j interface. Each case uses the whole selected production source corpus.
Private repositories and derived private material are excluded.

Independent reviewers authored the exact declaration targets and eight
source-backed behavioral criteria per task before seeing tool output or agent
answers. The actor receives the question and source manifest. The oracle,
prepared tool output, rubric, native logs and query journal stay outside its
filesystem namespace.

The Python AST and TypeScript compiler checks compare every extracted declaration
against independent parsers. The agent tasks separately ask for bounded source
locations and a compatibility review. Behavioral facts require reading source.
The declaration graph contains neither resolved calls nor runtime semantics.

## Reproduction

Install DeepThought with its Python, TypeScript, Neo4j and MCP extras. The native
runner also requires Linux Bubblewrap, Python, Node, ripgrep and an authenticated
Codex CLI distribution. Use the same pinned TypeScript compiler for both arms.
The recorded pilot used compiler 5.9.3 and native Codex 0.166.0.

Use existing public checkouts at the revisions in the scenario manifests. The
runner performs no target builds, imports, dependency installations or source
mutations. Provision a dedicated Neo4j database per case. Import while writable,
then enable database-enforced read-only operation before launching agents.

Register the trusted case grader and prepare the exact graph using the installed
SDK. Set the normal DEEPTHOUGHT_NEO4J_* variables in the evaluator environment.

```python
import json
from pathlib import Path
import runpy

from deepthought.evaluation.neo4j import prepare_neo4j

runpy.run_path("tools/run_tree_sitter_eval.py", run_name="pilot_registration")
prepared = prepare_neo4j(
    "evals/scenarios/tree_sitter/refactor/deepagents.scenario.json",
    "/path/to/public/deepagents",
)
Path("/outside/actor/prepared.private.json").write_text(json.dumps(prepared))
```

After freezing that database, pass its connection configuration in an
evaluator-owned environment file, one KEY=VALUE assignment per line. Protect it
with mode 0600. It must remain outside the actor namespace.

```sh
python tools/run_tree_sitter_eval.py run \
  --prepared /outside/actor/prepared.private.json \
  --env /outside/actor/connection.env \
  --output /outside/actor/new-runs \
  --native-root /path/to/native/codex/platform/distribution \
  --typescript /path/to/pinned/typescript/package
```

The native root must contain bin/codex. The TypeScript package must contain
lib/typescript.js. Only the actor answer directory is writable inside its
namespace. Source files and its own context are read-only. The native CLI gets
the existing auth file at its conventional isolated location to authenticate the
model. Database credentials and grading material are excluded. Network access is
needed for the model and evaluator-owned local MCP transport. This runner does
not enforce an outbound network allowlist.

Each case runs fresh baseline, treatment, treatment, baseline sessions using the
same model, reasoning effort, timeout and source tools. Treatments additionally
consult the production DeepThought MCP service. Both arms retain source search,
Python AST and the TypeScript compiler. No source or graph dump is injected into
the prompt. Sessions have no shared history. Provider prompt-cache behavior is
observed rather than controlled.

The runner retains native traces, full terminal usage, server logs and actual
GraphQueryService read receipts outside the actor namespace. It writes native
wall measurements before grading. Additional provider usage fields stay in the
native receipt while the portable framework receives its supported fields.
An incomplete native run stops the driver and retains its output. An inspected
completed record can be resumed with identical contexts using --resume.

## Scoring and interpretation

The registered refactor-navigation-v1 grader reuses the existing declarations-v1
exact scorer. It changes actor instructions to permit a behavioral review in the
limitations strings. Structural precision and recall therefore remain separate
from semantic judgment. Integration acceptance alone cannot establish answer
quality or tool usefulness.

Reviewers score shuffled answers without arm labels against the pre-run rubrics,
with explanations and source anchors for every deducted point. An answer may
still reveal its tools in its prose, so label blinding does not guarantee full
reviewer blinding. Preserve the answers and the mapping after review.

Report native input, cached input and output tokens separately. Cached input is
already part of input, and reasoning tokens are already part of output. Report
native actor wall time and extraction/import preparation separately. The actor
timer excludes database provisioning and MCP startup. Codex credit-equivalent
cost is a rate-card estimate from observed usage, rather than an actual invoice
or marginal subscription charge. Coordinator/reviewer usage and infrastructure
cost require separate accounting.

This is a repeated three-repository public pilot. It is not a hidden benchmark
or a claim about microservices, arbitrary languages, TSX, runtime architecture or
implemented-refactor correctness. Preserve negative outcomes and query failures.
