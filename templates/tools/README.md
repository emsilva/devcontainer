# Generated user tools

These files belong to generated containers. The shared native-agent installer and
the canonical container do not install ccswap or Plaud.

The committed locks were generated with npm 11.19.1 and uv 0.12.22. From the
generator checkout, the exact commands below keep all working files and caches
under its ignored `tmp/`. npm uses separate empty user/global configuration
files; uv uses the system certificate store and disables Python downloads.

```bash
lock_root="$PWD/tmp/tools-batch-a/locks"
mkdir -p "$lock_root/plaud-cli" "$lock_root/npm-cache" "$lock_root/uv-cache" "$lock_root/tmp"
printf '' > "$lock_root/empty.npmrc"
printf '' > "$lock_root/empty-global.npmrc"
cp templates/tools/plaud-cli/package.json "$lock_root/plaud-cli/package.json"
printf 'ccswap==0.42.0\n' > "$lock_root/ccswap.in"
(
  cd "$lock_root/plaud-cli"
  TMPDIR="$lock_root/tmp" npm install --package-lock-only --ignore-scripts \
    --registry=https://registry.npmjs.org --audit=false --fund=false \
    --cache="$lock_root/npm-cache" --userconfig="$lock_root/empty.npmrc" \
    --globalconfig="$lock_root/empty-global.npmrc"
)
(
  cd "$lock_root"
  TMPDIR="$lock_root/tmp" UV_CACHE_DIR="$lock_root/uv-cache" UV_EXTRA_INDEX_URL='' UV_INDEX='' \
    uv --no-config --system-certs pip compile --universal --python-version 3.12 \
    --no-python-downloads --index-url https://pypi.org/simple --no-header \
    --output-file ccswap-constraints.txt ccswap.in
)
cp "$lock_root/plaud-cli/package-lock.json" templates/tools/plaud-cli/package-lock.json
cp "$lock_root/ccswap-constraints.txt" templates/tools/ccswap-constraints.txt
```

`install-tools.sh` installs ccswap with those constraints and stages Plaud with
`npm ci --ignore-scripts` before replacing its user-owned directory. The Plaud
completeness probe is `--help`, with `PLAUD_TELEMETRY_DISABLED=1` and
`PLAUD_NO_UPDATE_NOTIFIER=1`: reading 0.3.15's `dist/index.js` confirms help exits
without authentication or an update request. Its `version` subcommand checks the
registry, so it is not used as the probe. Plaud is skipped outside Node
`^18.17.0 || >=20.5.0`, the intersection of the locked dependencies' engine ranges.
Regenerating the lock means re-deriving this range from its `engines.node` fields.
