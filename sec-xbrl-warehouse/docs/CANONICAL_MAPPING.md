# Canonical Mapping Policy

## Purpose

Canonical metrics make cross-company queries convenient, but they are interpretation, not source truth. Raw concepts and facts always remain queryable.

## Precedence

1. Approved filing-specific concept mapping.
2. Approved exact namespace, concept, and taxonomy-version mapping.
3. Approved namespace/concept mapping valid for the filing date.
4. Filing presentation/calculation evidence and configured candidates.
5. Unmapped review item.

## Prohibited shortcuts

- Mapping from a fuzzy English label alone.
- Ignoring dimensions when selecting consolidated versus segment facts.
- Treating all duration facts as interchangeable.
- Applying a sign flip without a versioned rule and explanation.
- Replacing an old decision in place.

## Review evidence

A review should show concept namespace/name, taxonomy version, labels/docs, unit, period, dimensions, statement role and line, calculation parents/children, comparable filers, and the proposed metric/sign rule.

## Reproducibility

Every canonical fact stores raw fact ID, mapping UUID/version, confidence, explanation, extraction revision, and normalized unit/value. Re-running with a historical mapping version must reproduce the prior result.
