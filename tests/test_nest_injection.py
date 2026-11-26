"""Calls through NestJS-injected constructor properties, bound to the class a
class provider supplies (deepthought.ts_modules.injected_calls)."""

import asyncio
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest

from deepthought import ts_modules
from deepthought.analysis_tools.tree_sitter import TreeSitterTool
from deepthought.engine import analyze_repository

FILES = {
    "tsconfig.json": json.dumps(
        {
            "compilerOptions": {
                "module": "commonjs",
                "target": "ES2021",
                "experimentalDecorators": True,
            },
            "include": ["src/**/*.ts"],
        }
    ),
    "src/tokens.ts": (
        "export const IUserRepository = 'IUserRepository';\n"
        "export interface IUserRepository {\n"
        "  get(id: string): string;\n"
        "  save(id: string): void;\n"
        "}\n"
        "export abstract class Clock {\n"
        "  abstract now(): number;\n"
        "}\n"
        "export const ICache = 'ICache';\n"
        "export interface ICache {\n"
        "  read(): string;\n"
        "}\n"
    ),
    "src/base.repository.ts": (
        "export class BaseRepository {\n  save(id: string): void {}\n}\n"
    ),
    "src/user.repository.ts": (
        "import { IUserRepository } from './tokens';\n"
        "import { BaseRepository } from './base.repository';\n"
        "\n"
        "export class UserRepository extends BaseRepository implements IUserRepository {\n"
        "  get(id: string): string {\n"
        "    return id;\n"
        "  }\n"
        "}\n"
    ),
    "src/clock.ts": (
        "import { Clock } from './tokens';\n"
        "\n"
        "export class SystemClock extends Clock {\n"
        "  now(): number {\n"
        "    return Date.now();\n"
        "  }\n"
        "}\n"
    ),
    # Inject from another package is not NestJS's.
    "src/other.ts": "export function Inject(token: unknown): ParameterDecorator {\n"
    "  return () => {};\n"
    "}\n",
    "src/user.service.ts": (
        "import { Inject, Injectable } from '@nestjs/common';\n"
        "import { Inject as Elsewhere } from './other';\n"
        "import { Clock, ICache, IUserRepository } from './tokens';\n"
        "\n"
        "@Injectable()\n"
        "export class UserService {\n"
        "  constructor(\n"
        "    @Inject(IUserRepository) private repository: IUserRepository,\n"
        "    private readonly clock: Clock,\n"
        "    @Elsewhere(IUserRepository) private other: IUserRepository,\n"
        "    @Inject(ICache) private cache: ICache,\n"
        "    @Inject(IUserRepository) plain: IUserRepository,\n"
        "  ) {}\n"
        "\n"
        "  find(id: string): string {\n"
        "    this.clock.now();\n"
        "    this.repository.save(id);\n"
        "    this.other.get(id);\n"
        "    this.cache.read();\n"
        "    return this.repository.get(id);\n"
        "  }\n"
        "}\n"
    ),
    "src/app.module.ts": (
        "import { Module } from '@nestjs/common';\n"
        "import { Clock, ICache, IUserRepository } from './tokens';\n"
        "import { UserRepository } from './user.repository';\n"
        "import { SystemClock } from './clock';\n"
        "import { UserService } from './user.service';\n"
        "\n"
        "const repositories = [{ provide: IUserRepository, useClass: UserRepository }];\n"
        "\n"
        "@Module({\n"
        "  providers: [\n"
        "    UserService,\n"
        "    ...repositories,\n"
        "    { provide: Clock, useClass: SystemClock },\n"
        "    { provide: ICache, useValue: { read: () => '' } },\n"
        "  ],\n"
        "})\n"
        "export class AppModule {}\n"
    ),
}


@unittest.skipUnless(
    os.environ.get("DEEPTHOUGHT_SCIP_TYPESCRIPT") or shutil.which("scip-typescript"),
    "Install the pinned scip-typescript",
)
class NestInjectionTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        for name, text in FILES.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)

    def test_injected_properties_hold_the_provided_classes(self):
        result = ts_modules.with_compiler_modules(
            analyze_repository(self.root, repository_id="r"), self.root
        )
        symbols = {d.id: d.symbol for d in result.definitions}
        # The other package's Inject, a value provider and a parameter
        # that is no property give nothing.
        self.assertEqual(
            {
                (symbols[owner], name): [symbols[i] for i in provided]
                for (owner, name), provided in (
                    result.compiler_modules.injections.items()
                )
            },
            {
                ("UserService", "clock"): ["SystemClock"],
                ("UserService", "repository"): ["UserRepository"],
            },
        )
        sites = {s.id: s for s in result.call_sites}
        self.assertEqual(
            sorted(
                (sites[site].callee, sites[site].line, symbols[target])
                for site, targets in ts_modules.injected_calls(result).items()
                for target in targets
            ),
            [
                ("this.clock.now", 16, "SystemClock.now"),
                ("this.repository.get", 20, "UserRepository.get"),
                # Inherited through extends.
                ("this.repository.save", 17, "BaseRepository.save"),
            ],
        )

    def test_graph_edges_carry_the_rule_and_the_compiler(self):
        output = asyncio.run(
            TreeSitterTool(self.root).analyze("r", languages=["typescript"])
        )
        edges = sorted(
            (
                r.source_match["qualified_name"].split("::")[2],
                r.properties["callee"],
                r.target_match["qualified_name"].split("::")[2],
                r.properties["producer"],
                r.properties["evidence_type"],
            )
            for r in output.relationships
            if r.type == "CALLS"
            and r.properties.get("rule_id") == ts_modules.NEST_INJECTED_CALL
        )
        self.assertEqual(
            edges,
            [
                (
                    "UserService.find",
                    "this.clock.now",
                    "SystemClock.now",
                    ts_modules.TYPESCRIPT_TOOL,
                    "resolved",
                ),
                (
                    "UserService.find",
                    "this.repository.get",
                    "UserRepository.get",
                    ts_modules.TYPESCRIPT_TOOL,
                    "resolved",
                ),
                (
                    "UserService.find",
                    "this.repository.save",
                    "BaseRepository.save",
                    ts_modules.TYPESCRIPT_TOOL,
                    "resolved",
                ),
            ],
        )
        self.assertEqual(
            output.metadata.custom_metrics["nest_injected_calls"],
            {"sites": 3, "edges": 3},
        )


if __name__ == "__main__":
    unittest.main()
