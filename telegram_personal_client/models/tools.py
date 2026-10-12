def changed_values(record, vals):
    """The subset of vals that differs from the record's current values.

    Lets engine upserts skip writes (and bus notifications) when Telegram
    re-sends data Odoo already has. Empty values ('' / False / 0) are equal.
    """
    diff = {}
    for name, value in vals.items():
        current = record[name]
        if record._fields[name].type == 'many2one':
            current = current.id
        if (current or False) != (value or False):
            diff[name] = value
    return diff
