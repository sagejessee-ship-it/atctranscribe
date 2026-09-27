# ADR-009: Exactly one architecture-family registry

Status: Accepted (2026-09-26)

## Context

v1 accidentally created competing definitions of "model family". Independence
between models (ensemble agreement) must be measured by architecture family,
never by raw model count.

## Decision

- The `architecture_family` table is the controlled vocabulary.
  `model.architecture_family` is a foreign key to it.
- Anything that needs model independence reads that column. No other family
  mapping may exist anywhere in the code.

## Consequences

`test_exactly_one_architecture_family_registry` fails if a second
family-bearing column appears. It also fails if the code assigns a
dict/list/set/tuple literal to a name containing `family`.
