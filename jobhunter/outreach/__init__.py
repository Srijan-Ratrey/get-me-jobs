"""Drafting and sending job applications.

Three modules, split along the line where the risk sits:

- `drafter`  builds one message from one (job, contact) pair, or refuses to.
- `policy`   decides whether a message may be sent at all. Every gate lives here.
- `sender`   talks to Gmail. Knows nothing about eligibility.

The split matters because `policy` is the only thing standing between an
unattended cron job and a spam operation, and it should be readable and
testable on its own. See docs/compliance.md.
"""

from __future__ import annotations
