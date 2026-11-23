"""Compare Tree-sitter Rust declarations with rust-analyzer's SCIP definitions.

A function, method, struct, enum, union, trait, type alias, module or
macro_rules macro is matched when rust-analyzer defines a global symbol of the
same name on the same line of the same file. Methods are also checked for their
owner: the SCIP symbol must name the implementing type (``impl#[Type]``) or the
trait (``Trait#``) that Tree-sitter scoped them by.

rust-analyzer indexes one configuration of each crate: items behind an inactive
``#[cfg]`` and files no crate includes have no SCIP definition, and items that
macros generate or that are written inside a macro invocation have no
Tree-sitter declaration. Each difference is classified by reading the parse
tree, so that every unexplained difference is listed.

Usage:
  rust-analyzer scip ROOT          # writes ROOT/index.scip
  rm -rf ROOT/target               # build output rust-analyzer leaves
  python tools/compare_rust_declarations_with_scip.py ROOT ROOT/index.scip
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re

import tree_sitter
import tree_sitter_rust

import deepthought

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# Descriptor endings of the declarations compared: functions and methods
# `name().`, types, traits and associated types `Name#`, modules `name/`,
# macros `name!`, and procedural macro functions `name:`.
_ENDINGS = ("().", "#", "/", "!", ":")
_KINDS = {
    "Function",
    "Method",
    "Struct",
    "Enum",
    "Union",
    "Trait",
    "TypeAlias",
    "Module",
    "Macro",
}
_DECLARING = rb"\b(fn|struct|enum|union|trait|type|mod|macro_rules!)\s+(r#)?"
_PARSER = tree_sitter.Parser(tree_sitter.Language(tree_sitter_rust.language()))


def _owners(descriptor):
    """Type and trait names a SCIP method descriptor places the method in."""
    names = set()
    for implemented in re.findall(r"impl#\[`?([^`\]]*)`?\]", descriptor):
        # `&'a mut path::Type<T>` -> Type
        bare = re.sub(
            r"<.*", "", re.sub(r"^&('\w+ )?(mut )?|^\*(const|mut) ", "", implemented)
        )
        names.add(bare.split("::")[-1])
    names.update(re.findall(r"(\w+)#\w+\(\)\.$", descriptor))
    return names


class _Source:
    """Parse trees of the compared files, for classifying differences."""

    def __init__(self, root):
        self.root = root
        self.files = {}

    def line(self, file, line):
        if file not in self.files:
            content = (self.root / file).read_bytes()
            self.files[file] = (_PARSER.parse(content), content.split(b"\n"))
        lines = self.files[file][1]
        return lines[line - 1] if line <= len(lines) else b""

    def node(self, file, line, name):
        """The innermost node at the first occurrence of ``name`` on the line."""
        found = re.search(
            rb"\b" + re.escape(name.encode()) + rb"\b", self.line(file, line)
        )
        point = (line - 1, found.start() if found else 0)
        return self.files[file][0].root_node.named_descendant_for_point_range(
            point, point
        )

    @staticmethod
    def cfg_gated(node):
        """Whether the node or an item around it carries a #[cfg] attribute,
        or its file an inner #![cfg]."""
        while node is not None:
            sibling = node.prev_named_sibling
            while sibling is not None and sibling.type in {
                "attribute_item",
                "line_comment",
                "block_comment",
            }:
                if sibling.type == "attribute_item" and sibling.text.startswith(
                    b"#[cfg"
                ):
                    return True
                sibling = sibling.prev_named_sibling
            if node.type == "source_file":
                return any(
                    c.type == "inner_attribute_item" and c.text.startswith(b"#![cfg")
                    for c in node.named_children
                )
            node = node.parent
        return False

    @staticmethod
    def in_macro(node):
        while node is not None:
            if node.type in {"macro_invocation", "token_tree"}:
                return True
            node = node.parent
        return False


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("index", type=Path)
    args = parser.parse_args()
    root = args.root.resolve()
    result = deepthought.analyze_repository(
        root, repository_id="comparison", languages=("rust",)
    )
    source = deepthought.ingest_scip(args.index, repository_id="comparison")
    scip = {}
    for d in source.definitions:
        if d.native_id.startswith("local "):
            continue
        descriptor = d.native_id.split(" ", 4)[-1]
        if not descriptor.endswith(_ENDINGS) or descriptor.endswith("crate/"):
            continue
        # The last descriptor names the declaration.
        names = _NAME.findall(descriptor.split("]")[-1])
        if not names:
            continue
        # A path through a symbolic link names the file the analysis reads.
        file = (root / d.file).resolve().relative_to(root).as_posix()
        entries = scip.setdefault((file, d.line, names[-1]), [])
        if descriptor not in entries:
            entries.append(descriptor)
    ours = {}
    for d in result.definitions:
        if d.kind in _KINDS:
            ours.setdefault((d.file, d.line, d.symbol.split(".")[-1]), []).append(d)
    matched = ours.keys() & scip.keys()
    owners = []
    for key in sorted(matched):
        for d in ours[key]:
            if d.kind != "Method":
                continue
            owner = d.symbol.split(".")[-2] if "." in d.symbol else None
            if owner not in {o for x in scip[key] for o in _owners(x)}:
                owners.append([d.file, d.line, d.symbol, scip[key]])
    indexed_files = {file for file, _, _ in scip}
    files = _Source(root)
    unmatched, examples = Counter(), []
    for key in sorted(ours.keys() - scip.keys()):
        file, line, name = key
        node = files.node(file, line, name)
        if (
            ours[key][0].kind == "Module"
            and node.parent.type == "mod_item"
            and node.parent.child_by_field_name("body") is None
        ):
            # `mod name;`: rust-analyzer defines the module at line 1 of its
            # file instead.
            reason = "file_module_declaration"
        elif files.cfg_gated(node):
            reason = "cfg_gated"
        elif file not in indexed_files:
            reason = "file_not_indexed"
        else:
            reason = "unexplained"
            examples.append([*key, ours[key][0].symbol])
        unmatched[reason] += 1
    scip_only, scip_examples = Counter(), []
    for key in sorted(scip.keys() - ours.keys()):
        file, line, name = key
        descriptor = scip[key][0]
        if re.search(r"#\w+#$", descriptor) and not descriptor.startswith("impl"):
            # `Enum#Variant#` (or a trait's associated type `Trait#Name#`,
            # which Tree-sitter declares on the same line when written).
            if not re.search(
                _DECLARING + re.escape(name.encode()), files.line(*key[:2])
            ):
                continue
        if descriptor.endswith("/") and line == 1:
            reason = "file_module"
        elif files.in_macro(files.node(file, line, name)):
            reason = "in_macro_invocation"
        elif not re.search(
            _DECLARING + re.escape(name.encode()) + rb"\b", files.line(file, line)
        ):
            reason = "macro_generated"
        else:
            reason = "unexplained"
            scip_examples.append([*key, descriptor])
        scip_only[reason] += 1
    print(
        json.dumps(
            {
                "declarations": sum(len(v) for v in ours.values()),
                "scip_declarations": len(scip),
                "matched": sum(len(ours[k]) for k in matched),
                # Methods of an impl for a slice, tuple, array or trait object
                # have no owner here; an impl through a type alias is named by
                # the alias as written, rust-analyzer by the aliased type.
                "owner_mismatches": dict(
                    sorted(
                        Counter(
                            "impl_for_unnamed_type"
                            if not any(
                                re.fullmatch(r"\w+", n)
                                for x in o[3]
                                for n in _owners(x)
                            )
                            else "impl_through_alias_or_other"
                            for o in owners
                        ).items()
                    )
                ),
                "owner_mismatch_named": [
                    o
                    for o in owners
                    if any(re.fullmatch(r"\w+", n) for x in o[3] for n in _owners(x))
                ][:20],
                "tree_sitter_only": dict(sorted(unmatched.items())),
                "tree_sitter_only_unexplained": examples[:20],
                "scip_only": dict(sorted(scip_only.items())),
                "scip_only_unexplained": scip_examples[:20],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
