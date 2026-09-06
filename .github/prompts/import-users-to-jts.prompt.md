---
mode: jts-user-import
description: 'Import the ALM users stored in out/alm_users.json into the Jazz Team Server (JTS) user registry. Dry run first, then commit after confirmation.'
---
Import the retrieved ALM users into JTS:

1. Confirm the input exists by reading `out/alm_users.json`. If it is missing or empty,
   stop and tell the user to run the `alm-access-retrieval` agent first (that agent writes
   the file).
2. Dry run (default) in the terminal at the repository root:
   `python src/jts_import_users.py`
   (CID/JTS_SERVER are read from `.env`; do NOT prompt for the CID. No password is asked
   for during a dry run and nothing is written.)
3. Show the user the exact list of users (USERID, name, email) that WOULD be created, and
   ask for explicit confirmation before committing.
4. After the user confirms, commit:
   `python src/jts_import_users.py --commit`
   (The script prompts for the password; the user types it directly into the terminal.)
   Report the per-user `[OK]` (imported) / `[UNARCH]` (already existed but was archived, now
   reactivated) / `[SKIP]` (already an active JTS user or not in LDAP) / `[FAIL]` results and
   the final created/unarchived/skipped/failed summary.
5. Archived-user handling: every user that already exists in JTS is checked for the
   application-side `archived` flag. If archived, the commit run automatically unarchives
   (reactivates) them via the JTS contributor REST API, since an archived account cannot use
   its access. Pass `--no-unarchive` to only report archived users instead of reactivating.
6. If it reports an auth failure, remind the user they must be on the Chrysler intranet / VPN.
   If the run exits 3, some users failed and some succeeded - report both groups. If it exits
   4, every user failed; do not describe that as a partial success.
7. Do NOT assign licenses or roles here - the Jazz User license is assigned automatically by
   JTS on import, and roles are assigned when the GPT provisioning script is run.

Override the input with `--users-in <path>` or the server with `--server <url>`. To validate
before importing everyone, add `--limit 1` (or `--limit N`) to the commit run.
