# Contributing

Thanks for contributing! Please follow the conventions below so that
automated tooling (release-please, changelog generation) keeps working.

## Conventional Commits

This repository uses [Conventional Commits](https://www.conventionalcommits.org/)
to drive automated releases and changelog generation via
[release-please](https://github.com/googleapis/release-please).

All commits (and squash-merge commit titles for PRs) MUST follow the format:

```
<type>(<optional-scope>): <description>

[optional body]

[optional footer(s)]
```

Common `<type>` values:

| Type       | Purpose                                                    | Triggers release? |
|------------|------------------------------------------------------------|-------------------|
| `feat`     | A new user-visible feature                                 | Yes (minor)       |
| `fix`      | A bug fix                                                  | Yes (patch)       |
| `perf`     | A performance improvement                                  | Yes (patch)       |
| `refactor` | Refactor that doesn't change behavior                      | No                |
| `docs`     | Documentation only                                         | No                |
| `test`     | Adding or correcting tests                                 | No                |
| `build`    | Build system / dependency changes                          | No                |
| `ci`       | CI configuration changes                                   | No                |
| `chore`    | Maintenance work without user impact                       | No                |
| `revert`   | Revert of a previous commit                                | Yes (patch)       |

Breaking changes are signaled by appending `!` after the type/scope
(e.g. `feat!: drop Python 3.8 support`) or by including a
`BREAKING CHANGE:` footer; both trigger a major release.

### How releases work

A GitHub Action (`.github/workflows/release-please.yml`) watches the default
branch. After each merge, it opens (or updates) a "release PR" that
aggregates Conventional Commits, bumps the version, and updates `CHANGELOG.md`.
Merging that release PR cuts a tagged GitHub Release with auto-generated
release notes.
