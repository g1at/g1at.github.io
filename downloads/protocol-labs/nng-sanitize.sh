#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="${NNG_BUILD_DIR:-${script_dir}/build-sanitize}"
nng_prefix="${NNG_PREFIX:-/usr}"

if [[ -d "${nng_prefix}/lib/x86_64-linux-gnu" ]]; then
  nng_libdir="${nng_prefix}/lib/x86_64-linux-gnu"
else
  nng_libdir="${nng_prefix}/lib"
fi

mkdir -p "${build_dir}"
gcc -std=c11 -Wall -Wextra -Werror -O1 -g \
  -fsanitize=address,undefined -fno-omit-frame-pointer \
  -I"${nng_prefix}/include" "${script_dir}/nng_aio_context_lab.c" \
  -L"${nng_libdir}" -Wl,-rpath,"${nng_libdir}" -lnng \
  -o "${build_dir}/nng_aio_context_lab-asan"

export LD_LIBRARY_PATH="${nng_libdir}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"
export ASAN_OPTIONS="detect_leaks=1:halt_on_error=1"
export UBSAN_OPTIONS="halt_on_error=1:print_stacktrace=1"
timeout 20s "${build_dir}/nng_aio_context_lab-asan"
