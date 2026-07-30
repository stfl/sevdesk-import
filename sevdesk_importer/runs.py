"""The run directory: where a statement and the CSV it produced are kept together.

One import is one directory, `<root>/<provider>/<until>/`, holding the export that
was read as `in.csv` and the sevDesk CSV that came out of it as `out.csv`. Naming it
after the last day actually booked makes the tree a ledger of what has been imported,
and reading the newest entry back is what lets a run pick up where the last one
stopped without anyone remembering a date.

Two rules keep that ledger honest. A directory is never written over: the same
statement always derives the same upper bound, so an existing directory means this
period has already been imported and the run is refused. And the export is moved
rather than copied, so a download cannot be fed in twice.
"""

from __future__ import annotations

import shutil
from datetime import date, timedelta
from pathlib import Path

#: The symlink, beside the dated directories, naming the most recent one.
LATEST = "latest"

IN_NAME = "in.csv"
OUT_NAME = "out.csv"


class RunError(Exception):
    """The run directory could not be used as the ledger it is meant to be."""


class RunAlreadyRecorded(RunError):
    """A directory for this upper bound exists. Refusing to write over an import."""


class UnreadableResumePoint(RunError):
    """The `latest` pointer exists but does not name a day."""


def provider_root(root: Path, provider: str) -> Path:
    return root / provider


def resume_since(root: Path, provider: str) -> date | None:
    """The lower bound implied by the last run: the day after the day it reached.

    `None` when this provider has never been imported, which means no lower bound.
    """
    link = provider_root(root, provider) / LATEST
    if not link.is_symlink():
        return None

    named = Path(link.readlink()).name
    try:
        return date.fromisoformat(named) + timedelta(days=1)
    except ValueError as error:
        raise UnreadableResumePoint(
            f"{link} points at {named!r}, which is not a YYYY-MM-DD day. Refusing to guess "
            "where the last import stopped — repoint it, or pass --since explicitly."
        ) from error


def commit(*, root: Path, provider: str, until: date, statement: Path, csv_text: str) -> Path:
    """Record one import, and return the path of the CSV it wrote.

    The directory is claimed before anything is written and the export is moved last,
    so a refused run leaves both the ledger and the export exactly as it found them.
    """
    target = provider_root(root, provider) / until.isoformat()
    if target.exists():
        raise RunAlreadyRecorded(
            f"{target} already exists, so bookings up to {until.isoformat()} have been "
            "imported already. Refusing to write over them."
        )

    target.mkdir(parents=True)
    written = target / OUT_NAME
    written.write_text(csv_text, encoding="utf-8", newline="")
    shutil.move(str(statement), str(target / IN_NAME))
    _point_latest(root, provider, until)
    return written


def _point_latest(root: Path, provider: str, until: date) -> None:
    """Move the `latest` pointer onto the directory just written.

    The link is relative and replaced atomically, so the tree can be moved whole and
    an interrupted run never leaves the pointer missing.
    """
    link = provider_root(root, provider) / LATEST
    staged = link.with_name(f"{LATEST}.new")
    staged.unlink(missing_ok=True)
    staged.symlink_to(until.isoformat(), target_is_directory=True)
    staged.replace(link)
