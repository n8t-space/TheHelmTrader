# Versioning & Release Process

The Helm is versioned like a production app: semantic versioning, a stable
production channel, and a separate beta channel so unvalidated work never
auto-ships to the running bot.

## Channels

| Channel | Branch | Tag form | Who runs it |
|---|---|---|---|
| **Production** | `main` | `vX.Y.Z` | The NSSM-hosted bot. Its version-check + in-place updater track `origin/main`. |
| **Beta** | `beta` | `vX.Y.Z-beta.N` | Manual validation (Sim / Playback / a live session) before promotion. NOT pulled by the production updater. |

The bot's version-check (`Trade_Perf/dashboard/api/version.py`) compares `HEAD`
to `origin/main` by SHA. Anything merged to `main` is what the production
updater offers to pull -- so only **validated** releases land on `main`.

## Semantic versioning

`MAJOR.MINOR.PATCH`

- **MAJOR** -- breaking change to a runtime contract (signal schema, settings
  shape, NS<->bot API, a NinjaScript that must be re-applied/re-deployed).
- **MINOR** -- new feature, backward compatible.
- **PATCH** -- bug fix, backward compatible.

Pre-releases use `-beta.N` (e.g. `v1.1.0-beta.2`). A `-beta` tag is never a
production release; it is a candidate for the next `vX.Y.Z`.

## Flow

1. Cut work onto `beta`. Commit with conventional messages
   (`feat:` / `fix:` / `refactor:` / `docs:`).
2. Tag the cut `vX.Y.Z-beta.N`, push `beta` + the tag.
3. Validate (Sim / Playback / live). Fix forward on `beta`, bumping `-beta.N`.
4. **Promote:** once validated, merge `beta` -> `main` and tag the stable
   `vX.Y.Z`. The production bot then offers the update.

## VERSION file

Repo-root `VERSION` holds the current tree's version string. On `beta` it
carries the `-beta.N` suffix; on `main` it carries the clean `vX.Y.Z`. Keep it
in sync with the tag at each cut.
