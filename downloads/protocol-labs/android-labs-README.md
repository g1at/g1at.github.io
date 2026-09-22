# Android boundary fixtures

`android_boundary_lab.py` is a host-side, standard-library fixture for the
article 《Android APP常见安全漏洞》.  It is not an APK and does not claim to
exercise Android framework behavior.

Run the deterministic demonstrations:

```bash
python android_boundary_lab.py --demo
```

Run all assertions:

```bash
python android_boundary_lab.py --test -v
```

The four groups cover:

1. exact HTTPS origin checks, userinfo/port/confusable host rejection, and
   per-hop redirect validation;
2. an in-memory SQLite analogue of unsafe concatenation, bound values,
   identifier allowlists, and row ownership;
3. real in-memory ZIP files with traversal, backslash, symbolic-link,
   overwrite, count, size, and compression-ratio guards;
4. an HMAC-authenticated request model whose method, path, timestamp, nonce,
   and body digest are bound, plus a server-side nonce replay set.

The fixture deliberately uses a hard-coded test key.  Production Android code
must use its actual authentication design and, where local keys are needed,
appropriate Android Keystore policy.  The replay set here is in-memory; a real
service needs an atomic, expiring store shared by all workers.
