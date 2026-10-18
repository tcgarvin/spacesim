# Prose Style

Applies to code comments, docstrings, docs, commit messages, and the reports
agents write back to the user.

## The rule

Mannered prose substitutes metaphor and flourish for direct statement. Instead
of "a parameter worth varying", the mannered writer produces "a dial worth
turning". Instead of "this point still matters", they write "this point earns
its keep". The phrases exist to display the writer, not to convey the idea, and
readers can tell. Metaphors drag in connotations the writer did not choose and
cannot control. The fix is to say what you mean. When a literal phrase is
available, use it.

## Operational rules

These are the rules the `8ade4f6` rewrite of every doc and code comment used.

- Keep facts. Drop history, measurements that no longer hold, hedges, and
  parentheticals. State what is true now.
- Comments say why, not what. Delete a comment that restates the code.
- Test docstrings are one line.
- Verify every symbol, path, flag, and constant you cite by grepping for it
  before you keep the reference. Stale references found this way include CLI
  flags that no longer exist, deleted probe scripts, and renamed methods.
- Prefer a table of numbers to a paragraph about numbers.
- Lead a report with the verdict and the 2-5 load-bearing figures.

## Batch rewrites

When rewriting comments or docstrings across many files:

1. Build the old -> new pairs and assert each old string matches exactly once
   in its file before applying it.
2. Afterwards, parse each changed module before and after with `ast`, strip
   docstrings from both trees, and assert `ast.dump` is identical. Comments and
   docstrings are the only things a prose pass may change.
