---
description: "Import retrieved ALM users into the GPT AD group, with a dry run and explicit confirmation before commit."
agent: "agent"
---
Import the retrieved ALM users into GPT (Global Provisioning Tool):

1. Read `out/alm_users.json`. If it is missing, empty, or contains no users, stop and tell
   the user to run `/retrieve-access-requests` first. Never invent or manually substitute
   user IDs.
2. Start the required Incognito debug Chrome session from the repository root:
   `.\scripts\start-gpt.ps1`
   Tell the user to complete the GPT Kerberos login in that Chrome window if prompted.
3. Run a dry run using exactly the USERID values from the input file. Set `USER_IDS` only
   in the current terminal process; do not write it to `.env`:
   `$users = (Get-Content out/alm_users.json -Raw | ConvertFrom-Json).users; $env:USER_IDS = (($users | ForEach-Object { $_.userid }) -join ','); .\.venv\Scripts\python.exe src\elm_gpt.py`
4. Report every `[gpt] staged USERID: OK|NOT FOUND` result and show the exact list that
   would be submitted to the configured GPT group. Ask for explicit confirmation before
   committing. Staging is not confirmation.
5. Only after the user explicitly confirms, run the commit with the IDs loaded again from
   the input file:
   `$users = (Get-Content out/alm_users.json -Raw | ConvertFrom-Json).users; $env:USER_IDS = (($users | ForEach-Object { $_.userid }) -join ','); .\.venv\Scripts\python.exe src\elm_gpt.py --commit`
6. Report the per-user staging results and the final `[gpt] COMMIT` response. If a user is
   `NOT FOUND`, report it and do not invent a replacement ID. If GPT authentication fails,
   remind the user to use the launched debug Chrome session and connect to the Chrysler
   intranet / VPN.

7. GPT queues the AD change asynchronously, so a successful submit means "GPT accepted the
   request", not "the user is in the group". The JazzUsers permission check (step 4 of the
   pipeline, or `python src/jts_permission.py <UID>`) is what confirms the access actually
   landed. Do not report group membership as confirmed on the strength of the submit alone.

GPT provisioning changes AD group membership. It does not import JTS contributors or
manually assign Jazz licenses.
