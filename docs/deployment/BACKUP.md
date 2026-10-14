# Backups and moving to another machine

Everything runs on the Windows RTX 5080 PC. Day-to-day backup commands are in
the runbook, under
[Keeping the data safe](../runbook.md#keeping-the-data-safe). This page says
what matters and how to take a full export.

| category | what | how |
| --- | --- | --- |
| **Critical, non-re-creatable** | human corrections and span annotations (`annotation_*`), dataset manifests (`training_dataset*`), model pedigree and qualification, adjudication results and their spend, and the rows linking labels to evidence (segments, results, agreement) | a database dump (`pg_dump -Fc`), and copies of it off this PC |
| Expensive but re-creatable | ASR outputs (in the dump), raw CrispASR artifacts (`%USERPROFILE%\aerochorus-data\artifacts`), agreement (recomputable) | the dump covers results; the full export below adds the artifacts |
| Re-creatable | model files (pinned by sha256; `aerochorus worker models pull`), the CrispASR and vLLM images (pinned by digest/version), caches | not backed up |
| Not AeroChorus's | collector source audio (`\\192.168.68.84\bwi`) | never copied, never written |

## Full export

```powershell
powershell -ExecutionPolicy Bypass -File deploy\migrate\export-windows.ps1
```

This writes `%USERPROFILE%\aerochorus-data\migration\<stamp>\` with:
- `aerochorus.dump`: `pg_dump -Fc`;
- `counts.tsv`: row counts and content fingerprints of the critical tables
  (`deploy/migrate/counts.sql`);
- `alembic_version.txt`;
- the raw artifacts (`artifacts-jesseepc.tgz`) and the ATCO2 clips
  (`atco2_fixed.tgz`);
- `SHA256SUMS`.

Stop the worker and any adjudication runner first, so no newer rows are left
behind. Copy the folder off this PC. Never put it on the collector's share.

## Restore, or move to another machine

1. Start an empty stack there at the same code revision with
   `docker compose up -d`. Migrations bring an empty database to head.
2. Restore and check, as in the runbook's scratch-restore check:

   ```powershell
   docker compose cp aerochorus.dump postgres:/tmp/restore.dump
   docker compose exec -T postgres pg_restore -U aerochorus -d aerochorus --clean --if-exists /tmp/restore.dump
   Get-Content deploy\migrate\counts.sql | docker compose exec -T postgres psql -U aerochorus -d aerochorus -At -F "`t" -f -
   ```

   The counts and fingerprints must match `counts.tsv`. Restore only into a
   database you mean to replace.
3. Unpack `artifacts-jesseepc.tgz` and `atco2_fixed.tgz`. Map them in that
   machine's `worker.toml` (`artifact_mounts`, `[sources.atco2_fixed]`).

Identity survives a move, because nothing ties it to this machine:
- A segment is `(logical source key, relative path)`. The mount path is
  configuration in `worker.toml` only.
- Raw artifacts are `artifact://<store>/…`. This PC's store is `jesseepc`; map
  it on the new machine with `artifact_mounts`, and give new results a new
  store name.
- A hardware profile (e.g. `windows_blackwell_16gb`) is derived from the
  hardware, never the hostname. Qualification is per (model, profile), so a
  new GPU class qualifies again.
