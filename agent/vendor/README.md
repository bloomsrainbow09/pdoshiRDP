# Vendored dependencies

## `telethon/` — pinned to `dab6bc4d` (`v1.42.0-16-gdab6bc4d`)

Copied verbatim from `LonamiWebs/Telethon`, branch `v1`. **Not installed from PyPI**, and
that is deliberate.

PyPI's `telethon==1.42.0` is 16 commits behind this, and those commits are load-bearing for
exactly this workload:

```
208ac301  Update to layer 222            MTProto layers 218 -> 222
d57be746  Catch-all unknown RPCError and treat them as transient during getDifference
295c7363  Fix InvalidBufferError on incomplete recv data
```

`getDifference` is the catch-up path `watcher.replay_gap()` rides on — the thing that makes
a 6-hour runner wipe lose no messages — and the buffer fix is reconnection robustness. A
watcher that reconnects four times a day needs both.

**Why vendored rather than `pip install git+https://…@dab6bc4d`:** the newest commit on
that branch is literally *"Migrate off GitHub"*. Pinning a URL on a repository upstream is
leaving is a dependency that works until it does not, on a machine nobody is watching.

**To update:** replace this directory from a checkout at the new commit and record the
commit here. Do not edit files in place — the whole point is that this is upstream, verbatim.

Licence: MIT, upstream. `__pycache__` stripped; nothing else changed.
