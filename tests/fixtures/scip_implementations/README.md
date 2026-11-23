# Implementation relationships control

`a.ts` covers a class that extends a class and implements an interface which
extends another interface, a class that implements a class, an abstract base,
member overrides, an object literal typed by an interface and a type alias. The
index was written by `deepthought index-typescript` (scip-typescript 0.6.0) from
the root `/fixtures/scip_implementations`.

The index records `extends` and `implements` alike as `is_implementation`,
including indirect supertypes (`Dog` -> `Animal` through `Pet`), and records
overriding members. It records nothing for `interface Pet extends Animal`, the
object literal or the alias, and no type-definition relationships.
