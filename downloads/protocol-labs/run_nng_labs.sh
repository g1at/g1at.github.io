#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
build_dir="${NNG_BUILD_DIR:-${script_dir}/build}"
nng_prefix="${NNG_PREFIX:-/usr}"

if [[ -d "${nng_prefix}/lib/x86_64-linux-gnu" ]]; then
  nng_libdir="${nng_prefix}/lib/x86_64-linux-gnu"
else
  nng_libdir="${nng_prefix}/lib"
fi

# This also lets the loader find libnng's TLS dependencies when NNG_PREFIX is
# a self-contained package tree rather than /usr.
export LD_LIBRARY_PATH="${nng_libdir}${LD_LIBRARY_PATH:+:${LD_LIBRARY_PATH}}"

mkdir -p "${build_dir}"

for source in nng_aio_context_lab.c nng_flow_lab.c nng_roles.c; do
  output="${build_dir}/${source%.c}"
  gcc -std=c11 -Wall -Wextra -Werror -O2 \
    -I"${nng_prefix}/include" "${script_dir}/${source}" \
    -L"${nng_libdir}" -Wl,-rpath,"${nng_libdir}" -lnng -o "${output}"
done

"${build_dir}/nng_aio_context_lab"
"${build_dir}/nng_flow_lab"
