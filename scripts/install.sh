#!/usr/bin/env bash
#
# Everything between a clone and a running application, in one command.
#
#     ./scripts/install.sh
#
# Safe to re-run. It never overwrites a .env, never regenerates an encryption
# key that already exists, and never touches a database that is already
# migrated beyond creating it. Re-running after a git pull is how you upgrade.
#
# It does not install system packages. PostgreSQL and Tesseract are decisions
# about a machine, made with a package manager and a sudo password, and a
# script that reaches for either on your behalf is a script you have to read
# before trusting. This one tells you the command and stops.

set -euo pipefail

cd "$(dirname "$0")/.."

say()  { printf '\n\033[1m%s\033[0m\n' "$*"; }
ok()   { printf '  ✓ %s\n' "$*"; }
warn() { printf '  ! %s\n' "$*"; }
die()  { printf '\n\033[1;31m%s\033[0m\n\n' "$*" >&2; exit 1; }

# ---------------------------------------------------------------- system deps

say "Checking what this machine already has"

command -v python3 >/dev/null || die "python3 is not installed."

PYTHON_OK=$(python3 - <<'EOF'
import sys
print("yes" if sys.version_info >= (3, 11) else "no")
EOF
)
[ "$PYTHON_OK" = yes ] || die "Python 3.11 or newer is required; this is $(python3 -V)."
ok "$(python3 -V)"

if ! command -v pg_isready >/dev/null && ! command -v psql >/dev/null; then
    die "PostgreSQL is not installed.
  Arch:   sudo pacman -S postgresql
  Debian: sudo apt install postgresql
Then start it (systemctl start postgresql) and run this again."
fi
ok "PostgreSQL client found"

if command -v pg_isready >/dev/null && ! pg_isready -q; then
    die "PostgreSQL is installed but not accepting connections.
Start it with: sudo systemctl start postgresql"
fi

# Without it a scan is still stored, hashed and linked — it simply has no text
# layer, so it is searchable only by filename and no dates come off it. Worth
# a warning rather than a stop.
if command -v tesseract >/dev/null; then
    ok "Tesseract $(tesseract --version 2>&1 | head -1 | awk '{print $2}')"
else
    warn "Tesseract is not installed. Scanned documents will be stored but not read."
    warn "  Arch:   sudo pacman -S tesseract tesseract-data-eng"
    warn "  Debian: sudo apt install tesseract-ocr"
fi

# ---------------------------------------------------------------- the venv

say "Installing the application"

if [ ! -d .venv ]; then
    python3 -m venv .venv
    ok "Created .venv"
else
    ok "Using the existing .venv"
fi

# Quiet, because the interesting output of this script is at the end and forty
# lines of resolver chatter buries it.
.venv/bin/pip install --quiet --upgrade pip
.venv/bin/pip install --quiet -e '.[dev]'
ok "Dependencies installed"

# ---------------------------------------------------------------- config

say "Configuration"

if [ ! -f .env ]; then
    cp .env.example .env
    ok "Wrote .env from .env.example"
else
    ok ".env already exists, left alone"
fi

env_value() { sed -n "s/^$1=//p" .env | tail -1; }

# A key that appears by magic is a key nobody knows they have to back up, so
# this says so loudly. It is only ever written when the line is empty: a
# regenerated key would make every existing blob unreadable, permanently.
if [ -z "$(env_value BLOB_ENCRYPTION_KEY)" ]; then
    KEY=$(.venv/bin/python -c "from renewal.crypto import generate_key; print(generate_key())")
    if grep -q '^BLOB_ENCRYPTION_KEY=' .env; then
        sed -i "s|^BLOB_ENCRYPTION_KEY=.*|BLOB_ENCRYPTION_KEY=$KEY|" .env
    else
        printf '\nBLOB_ENCRYPTION_KEY=%s\n' "$KEY" >> .env
    fi
    warn "Generated BLOB_ENCRYPTION_KEY and wrote it to .env."
    warn "BACK IT UP somewhere your blob backups are not. Lose it and every"
    warn "document is unreadable, by anyone, permanently."
else
    ok "BLOB_ENCRYPTION_KEY is set"
fi

# ---------------------------------------------------------------- database

say "Database"

DB_URL=$(env_value DATABASE_URL)
DB_URL=${DB_URL:-postgresql+psycopg:///renewal}
DB_NAME=${DB_URL##*/}
DB_NAME=${DB_NAME%%\?*}

if psql -lqt 2>/dev/null | cut -d'|' -f1 | grep -qw "$DB_NAME"; then
    ok "Database '$DB_NAME' exists"
elif createdb "$DB_NAME" 2>/dev/null; then
    ok "Created database '$DB_NAME'"
else
    die "Could not create the database '$DB_NAME'.
Your PostgreSQL user probably cannot create databases. Either:
  sudo -u postgres createdb -O \"$USER\" $DB_NAME
or point DATABASE_URL in .env at a database that already exists."
fi

.venv/bin/alembic upgrade head >/dev/null
ok "Migrations are at head"

# ---------------------------------------------------------------- the model

say "Model provider"

PROVIDER=$(env_value PROVIDER)
PROVIDER=${PROVIDER:-anthropic}
case "$PROVIDER" in
    anthropic) KEY_VAR=ANTHROPIC_API_KEY ;;
    openai)    KEY_VAR=OPENAI_API_KEY ;;
    grok)      KEY_VAR=XAI_API_KEY ;;
    huggingface) KEY_VAR=HF_TOKEN ;;
    custom)    KEY_VAR=LLM_API_KEY ;;
    *)         KEY_VAR="" ;;
esac

MODEL_READY=yes
if [ -n "$KEY_VAR" ] && [ -z "$(env_value "$KEY_VAR")" ]; then
    MODEL_READY=no
    warn "PROVIDER=$PROVIDER but $KEY_VAR is empty in .env."
    warn "The application builds its model client at import, so it will not"
    warn "start until that is filled in."
else
    ok "PROVIDER=$PROVIDER"
fi

# ---------------------------------------------------------------- an account

say "Accounts"

ACCOUNTS=$(.venv/bin/python - <<'EOF'
from sqlalchemy import func, select
from sqlalchemy.orm import sessionmaker
from renewal.db import get_engine
from renewal.models import User
session = sessionmaker(bind=get_engine())()
print(session.scalar(select(func.count(User.id))))
EOF
)

if [ "$ACCOUNTS" -gt 0 ]; then
    ok "$ACCOUNTS account(s) already exist"
elif [ -t 0 ]; then
    # There is no signup route on purpose: the person with shell access is the
    # provisioning system. This is that person, here, now.
    printf '\n  No accounts yet. Make the first one.\n'
    printf '  Email address: '
    read -r EMAIL
    if [ -n "$EMAIL" ]; then
        .venv/bin/python -m scripts.add_user "$EMAIL"
    else
        warn "Skipped. Run: .venv/bin/python -m scripts.add_user you@agency.com"
    fi
else
    warn "No accounts, and nothing to prompt with."
    warn "Run: .venv/bin/python -m scripts.add_user you@agency.com"
fi

# ---------------------------------------------------------------- done

say "Ready"

if [ "$MODEL_READY" = no ]; then
    printf '  1. Put %s in .env — nothing starts without it.\n' "$KEY_VAR"
    printf '  2. Start it:  .venv/bin/uvicorn renewal.app:app --port 8000\n'
else
    printf '  Start it:  .venv/bin/uvicorn renewal.app:app --port 8000\n'
    printf '  Then open: http://127.0.0.1:8000\n'
fi

cat <<'EOF'

  Optional, and each one is a section in the README:
    Mail        the six IMAP_* values, so documents arrive on their own
    Summaries   the SMTP_* values, so the daily email can be sent
    An archive  .venv/bin/python -m scripts.bulk_import /path/to/the/archive
    A service   deploy/renewal.service, so it starts itself on boot

EOF
