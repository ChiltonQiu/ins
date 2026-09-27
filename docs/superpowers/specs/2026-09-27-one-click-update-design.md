# One-Click Update — Design

Date: 2026-09-27
Status: decided without a review round, at the user's instruction ("just write
the implementation plan … and bundle it into a new release"); open to revision
Scope: a Windows install finds out a newer release exists and installs it
from a button, without anybody touching a file.

## Context

Nothing in the application knows its own version or that others exist.
Updating today means downloading the next `.exe` and running it by hand, and a
tarball install has a trap in it: unpacked into a new folder, it makes a new,
empty database beside the old one.

The repository is public and every release is a GitHub release carrying
`Renewal-X.Y.Z-setup.exe` and `SHA256SUMS`. The `.exe` installs into whatever
folder `/D=` names, and re-running it over an existing install is safe:
`bootstrap.ps1` reuses the Python, the database and `.env`, reinstalls the
dependencies and migrates to head. CI proves that on every push by installing
twice.

## Decisions

| Question | Decision |
|---|---|
| Where it shows | The setup page gets a fourth section, **Updates**: "Version 0.3.0 · up to date ✓", or "0.3.1 is available — what's new · **Update now**". When one is available, the setup banner line mentions it too. |
| How often it asks | Once at start and every 12 hours, on a background thread. A page never waits on GitHub. |
| What it asks | `GET https://api.github.com/repos/ChiltonQiu/ins/releases/latest`, 10-second timeout. Any failure means "couldn't check", shown quietly, never an error page. |
| Off switch | `UPDATE_CHECK=false` in `.env`. It is the one thing this application does that tells a third party the office's IP address. `PRIVACY.md` says so. |
| Comparing versions | Numeric `X.Y.Z` from the tag (`v0.3.1`) against `importlib.metadata.version("renewal")`. A tag that does not parse is ignored, not treated as newer. |
| Where one-click works | Windows only (`sys.platform == "win32"`) with `scripts/update.ps1` present. Everywhere else the section offers the release link instead of a button. |
| What the button does | Downloads the `.exe` asset into `runtime/downloads/`, checks its SHA-256 against the release's `SHA256SUMS`, then starts `scripts/update.ps1` detached and answers with an "Updating…" page that waits for the application to come back. |
| What `update.ps1` does | 1. Backs up the private database with `pg_dump -Fc` into `runtime\backups\before-update-<timestamp>.dump` (skipped, with a log line, on a system PostgreSQL). 2. Stops the application by PID and waits for it to exit. 3. Runs the installer with `/S /D=<this folder>` and waits for the installer alone. 4. Starts the application again with `start.ps1 -NoBrowser`, whether or not the install succeeded, so a failed update leaves the old version running instead of nothing. All of it goes to `runtime\update.log`. |
| Who can press it | Anyone signed in, the same as `/setup`. |

### What the checksum does and does not do

`SHA256SUMS` comes from the same release as the `.exe`, so matching proves the
download arrived intact. It does not prove who published it: anybody who can
publish a release on the repository can publish both files. That is the same
trust somebody places in the `.exe` the first time they double-click it, and
the spec says so rather than calling it signature verification.

### Why a detached script and not the application itself

On Windows a running process cannot replace the files it is running from, and
the installer reinstalls the virtualenv's packages. So the application's last
act is to hand off to a process that outlives it. `update.ps1` is read into
memory when it starts, so the installer overwriting it mid-run is harmless.

## Testing

- **Unit:** version parsing and comparison; the release lookup against a fake
  transport (found, no `.exe` asset, HTTP error, timeout, malformed JSON); the
  checksum refusing a corrupted download; the route refusing on non-Windows and
  answering the updating page on a faked Windows with a fake spawner.
- **CI (Windows):** after the silent `.exe` install and start, run
  `update.ps1` against the same `.exe`. The application must come back up,
  `runtime\update.log` must show the installer's exit code, and a `.dump`
  must exist in `runtime\backups\`.

CI cannot publish a newer release to update to. Installing the same version
over itself exercises everything except the version comparison, which the
unit tests cover.
