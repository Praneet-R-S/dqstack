"""DQ-Stack store-operations application.

The forecasting engine (the rest of `dqstack`) answers "what will sell and how much
should we hold". This package is the store-operations product built around it, per
the product specification: companies, stores and people; an inventory ledger whose
stock on hand is always the sum of its movements; floor work (counts, deliveries,
transfers, wastage); sales in by upload or by checkout; suggested and purchase
orders; alerts and reports; and the nightly forecast run that reads from this
package's database.

Layout:
    config.py       settings from the environment (development, staging, production)
    db.py           engine, sessions, base class, company scoping
    models.py       every table in section 4 of the specification
    security.py     password hashing, sessions, CSRF, lockout
    permissions.py  the role matrix in section 2, enforced on the server
    audit.py        the append-only activity log
    ledger.py       stock movements and stock on hand
    formats.py      Indian money, date and time formats
    main.py         the FastAPI application
"""
