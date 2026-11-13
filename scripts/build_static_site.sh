#!/bin/sh
# The static website now builds from inside site/ (site/tools/build_static.sh) into
# site/public/, so a static host pointed at the site folder finds no requirements.txt
# and installs nothing. Kept so the old command still works.
exec sh "$(dirname "$0")/../site/tools/build_static.sh" "$@"
