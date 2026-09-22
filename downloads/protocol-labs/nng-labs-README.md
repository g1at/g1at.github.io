# NNG 1.7.2 labs

The examples accompanying the Chinese article are intentionally small but
check real failure paths as well as the happy path.

```bash
NNG_PREFIX=/usr bash run_nng_labs.sh
```

If NNG lives in a package tree, point `NNG_PREFIX` at the directory containing
`include/nng` and `lib`. `NNG_BUILD_DIR` can move build products elsewhere.

* `nng_aio_context_lab.c`: six REQ contexts, three asynchronous REP contexts,
  explicit receive/process/send/stop states, error ownership, timeout, and
  ordered shutdown. It binds only `tcp://127.0.0.1:18892` and also uses inproc.
* `nng_flow_lab.c`: PUSH backpressure, PUB before a SUB pipe exists, and binary
  SUB prefix matching.
* `nng_roles.c`: REQ listens while REP dials, demonstrating that endpoint role
  and protocol role are independent. Run its two modes in separate terminals,
  or use `bash nng-run-roles.sh ./build/nng_roles` after compiling.

The script uses `-Wall -Wextra -Werror`; it was verified with GCC 13.3.0 and
NNG 1.7.2 on Ubuntu 24.04.

For an additional ownership/lifetime check, run `nng-sanitize.sh` with the
same `NNG_PREFIX`; it compiles the AIO lab with AddressSanitizer and UBSan.
