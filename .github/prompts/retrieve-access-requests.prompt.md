---
mode: alm-access-retrieval
description: 'Fetch ALM Access Request work items and the requested user IDs from the "ALM Access Request Pending ICT Action - Internal ALM" queue (read-only).'
---
Run the retrieval task only:

1. In the terminal at the repository root, run:
   `python src/alm_access_requests.py`
   (CID is read from `.env`; do NOT prompt for it. The password is sensitive - the
   script always prompts for it and the user types it directly into the terminal.)
2. If it reports an auth failure, remind the user they must be on the Chrysler intranet / VPN.
3. Report each returned work item with its fields (Type, ID, Summary, Access Type, Status,
   New Users, Existing User(s), Domain, Work Area(s), Roles, Approvals, Planned For,
   Created By/Date, Modified By/Date) and list the unique USERID tokens parsed from New Users.

Defaults to the "In Progress - ICT" (Pending ICT Action) state. To broaden, re-run with
`--all-open`, or target another state with `--state "<name>"`, or export with `--csv out.csv`.
