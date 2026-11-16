#!/usr/bin/env bash
# Build the WatDiv generator used by FedShop.
#
# Boost >= 1.83 no longer pulls boost::filesystem::ifstream in through <boost/filesystem.hpp>,
# so src/model.cpp fails to compile. Force-including the header fixes this without patching
# the submodule, and is harmless on older Boost versions.
#
# watdiv-seed.patch makes runs reproducible: WatDiv seeds itself from the clock and ends open date
# ranges at today's date, unless WATDIV_SEED and WATDIV_TODAY are set (fedshop/discovery/catalog.py
# sets them). It is applied once; a checkout that already has it is left as it is.
#
# The FedShop configs call generators/watdiv/bin/Release/watdiv directly, so `make install`
# (which copies to /usr/local/bin and needs root) is not required.
set -euo pipefail

patch="$(cd "$(dirname "$0")" && pwd)/watdiv-seed.patch"
cd "$(dirname "$0")/../generators/watdiv"
if ! git apply --reverse --check "$patch" 2>/dev/null; then
    git apply "$patch"
fi
make rebuild CFLAGS="-std=c++17 -Wall -include boost/filesystem/fstream.hpp"
ls -l bin/Release/watdiv
