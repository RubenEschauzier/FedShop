#!/usr/bin/env bash
# Build the WatDiv generator used by FedShop.
#
# Boost >= 1.83 no longer pulls boost::filesystem::ifstream in through <boost/filesystem.hpp>,
# so src/model.cpp fails to compile. Force-including the header fixes this without patching
# the submodule, and is harmless on older Boost versions.
#
# The FedShop configs call generators/watdiv/bin/Release/watdiv directly, so `make install`
# (which copies to /usr/local/bin and needs root) is not required.
set -euo pipefail

cd "$(dirname "$0")/../generators/watdiv"
make rebuild CFLAGS="-std=c++17 -Wall -include boost/filesystem/fstream.hpp"
ls -l bin/Release/watdiv
