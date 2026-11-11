from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal


def overview(connection):
    rows = [dict(record) for record in connection.execute('SELECT day, label, COUNT(*) AS scenes FROM classifications GROUP BY day, label ORDER BY day, label')]
    return rows
