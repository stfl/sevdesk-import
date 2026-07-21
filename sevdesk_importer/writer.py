"""Serialising bookings into the CSV the sevDesk import wizard reads.

Direction is expressed by which of the two amount columns is populated, the other
being empty. That requires "Gutschrift & Belastung einblenden" to be enabled in the
wizard, which is an import-time prerequisite rather than a code concern.
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable

from sevdesk_importer.formatting import Convention
from sevdesk_importer.model import Booking

COLUMNS = ("Name", "Verwendungszweck", "Buchungstag", "Gutschrift", "Belastung")


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
        credit = booking.gutschrift
        debit = booking.belastung
        writer.writerow(
            [
                booking.name,
                booking.purpose,
                convention.date(booking.booking_date),
                convention.amount(credit) if credit is not None else "",
                convention.amount(debit) if debit is not None else "",
            ]
        )
    return buffer.getvalue()
