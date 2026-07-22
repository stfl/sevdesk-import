"""Serialising bookings into the CSV the sevDesk import wizard reads.

Direction is carried by the sign of a single `Betrag` column: positive credits the
account, negative debits it. The wizard reads the sign directly, so no column is
ever empty and no wizard-side toggle is required to see the amounts.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from sevdesk_importer.formatting import Convention
from sevdesk_importer.model import Booking

COLUMNS = ("Name", "Verwendungszweck", "Buchungstag", "Betrag")


def render_csv(bookings: Iterable[Booking], convention: Convention) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.writer(
        buffer,
        delimiter=convention.delimiter,
        lineterminator="\r\n",
        quoting=csv.QUOTE_MINIMAL,
    )
    writer.writerow(COLUMNS)
    for booking in bookings:
        writer.writerow(
            [
                booking.name,
                booking.purpose,
                convention.date(booking.booking_date),
                convention.amount(booking.amount_eur),
            ]
        )
    return buffer.getvalue()
