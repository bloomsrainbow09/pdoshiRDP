"""Where the bytes live.

Media must NOT go in the database and must NOT stay on the runner: the EC2 has an
8 GB disk and a runner is wiped every 6 hours. content.media therefore stores only
a pointer — (storage, storage_ref) — and the bytes go to object storage.

Backends, in preference order:
  gdrive    the existing OAuth uploader (bin/gdrive-upload.sh) — 5 TB already paid for
  supabase  Supabase Storage, S3-compatible, same credentials as the database
  local     development only; lost on a runner wipe

Not implemented yet. Ingest currently writes storage='local' when --media is used.
"""
