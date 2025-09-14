---

description: "Make clean, conventional commits with emojis (no leftover files)"
allowed-tools:
\[
"Bash(git add:*)",
"Bash(git status:*)",
"Bash(git commit:*)",
"Bash(git diff:*)",
"Bash(git log:\*)",
]
-

# Claude Command: Commit

Create well‑formatted, atomic commits using Conventional Commits + emoji.

## Usage

```
/commit
```

## Workflow

0. Ultrathink
1. **Clean tree first**: if there are unstaged/untracked files, either **stage & include now** or **add to `.gitignore`**. **No leftovers.**
2. Commit **only staged** files (if none, stop and instruct to stage or ignore).
3. Review `git diff --staged` for multiple logical changes; **suggest splitting** if mixed concerns.
4. Create commit with the format below.

## Commit Format

`<emoji> <type>: <description>`

**Types:** `feat` · `fix` · `docs` · `style` · `refactor` · `perf` · `test` · `chore`

**Emoji map:** ✨ feat · 🐛 fix · 📝 docs · 💄 style · ♻️ refactor · ⚡ perf · ✅ test · 🔧 chore
**Extras (optional):** 🚀 ci · 🔖 release · 💥 breaking · 🔥 remove · 🚧 wip

## Rules

* Imperative mood (“add”, not “added”).
* First line ≤ 72 chars.
* **Atomic commits**: one purpose per commit.
* **Split** when concerns/types/files are unrelated or the change is large.

## Guardrails
* **Never add Claude signature to commits.**
* **Never add Claude signature to commits.**
* **Never add Claude signature to commits.**
* **Never commit with any name or e-mail different than the defaults set.**
* **Never commit with any name or e-mail different than the defaults set.**
* **Never commit with any name or e-mail different than the defaults set.**
* **Never commit with Claude as Co-Author. **
* **Never commit with Claude as Co-Author. **
* **Never commit with Claude as Co-Author. **
* **Never leave leftover files**: they must be **committed** or **git‑ignored**.
