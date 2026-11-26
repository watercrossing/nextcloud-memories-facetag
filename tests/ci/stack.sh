#!/usr/bin/env bash
# Bring up a throwaway Nextcloud + PostgreSQL with pinned Memories and Recognize releases and this
# app from the checkout, and load the test photo set into it. Used by CI and for local runs.
#
#   tests/ci/stack.sh up         # Nextcloud, Memories, Recognize, this app; faces on (tfjs-node)
#   tests/ci/stack.sh dataset    # photos into the test user, index them, run Recognize, name its clusters
#   tests/ci/stack.sh test       # the server-side tests (pytest; extra args go to pytest)
#   tests/ci/stack.sh app        # re-install this app from the checkout into the running stack
#   tests/ci/stack.sh all        # up + dataset + test
#   tests/ci/stack.sh occ ARGS…  # occ in the stack, as www-data
#   tests/ci/stack.sh psql SQL   # a query against its database
#   tests/ci/stack.sh logs DIR   # copy nextcloud.log and container logs into DIR
#   tests/ci/stack.sh down       # remove containers and volumes
#
# Versions (defaults: the newest pinned in tests/ci/versions.json):
#   NEXTCLOUD_VERSION=34  MEMORIES_VERSION=11.1.4  RECOGNIZE_VERSION=12.0.3
# Other knobs: COMPOSE_PROJECT_NAME (facetag-ci); FACETAG_CACHE (.cache in the repo: app tarballs,
#   Recognize's native runtime, photos); FACETAG_NETWORK (an existing docker network to run on
#   instead of the project's own).
#
# The containers need no network access: everything is downloaded on the host into the cache.
set -euo pipefail

HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
export COMPOSE_PROJECT_NAME=${COMPOSE_PROJECT_NAME:-facetag-ci}
export FACETAG_CACHE=${FACETAG_CACHE:-$ROOT/.cache}
mkdir -p "$FACETAG_CACHE"
FACETAG_CACHE=$(cd "$FACETAG_CACHE" && pwd)

latest() { python3 -c "import json,sys; d=json.load(open('$HERE/versions.json')); print(d['apps']['$1'][d['nextcloud'][-1]][0]['version'])"; }
export NEXTCLOUD_VERSION=${NEXTCLOUD_VERSION:-$(python3 -c "import json; print(json.load(open('$HERE/versions.json'))['nextcloud'][-1])")}
MEMORIES_VERSION=${MEMORIES_VERSION:-$(latest memories)}
RECOGNIZE_VERSION=${RECOGNIZE_VERSION:-$(latest recognize)}
NC_MAJOR=${NEXTCLOUD_VERSION%%.*}

TEST_USER=alice
APPS=/var/www/html/custom_apps

COMPOSE_FILES=(-f "$HERE/compose.yml")
if [ -n "${FACETAG_NETWORK:-}" ]; then COMPOSE_FILES+=(-f "$HERE/compose.network.yml"); fi
compose() { docker compose "${COMPOSE_FILES[@]}" "$@"; }
occ() { compose exec -T -u www-data nextcloud php occ "$@"; }
psql_() { compose exec -T db psql -U nextcloud -d nextcloud -v ON_ERROR_STOP=1 -qtA "$@"; }
step() { printf '\n\033[1m== %s\033[0m (%s)\n' "$*" "$(date -u +%T)"; }

# The app from the checkout, the files ./deploy installs
copy_app() {
	compose exec -T -u www-data nextcloud sh -c "rm -rf $APPS/memories_facetag && mkdir $APPS/memories_facetag && cd /facetag-src && tar -cf - appinfo lib js css bin | tar -C $APPS/memories_facetag -xf -"
}

# Re-install the app into a running stack after changing it; occ upgrade if its version changed
reinstall_app() {
	copy_app
	local want have
	want=$(sed -n 's:.*<version>\(.*\)</version>.*:\1:p' "$ROOT/appinfo/info.xml")
	have=$(occ config:app:get memories_facetag installed_version)
	if [ "$want" != "$have" ]; then occ upgrade -n; fi
	occ app:list --enabled | grep memories_facetag
}

up() {
	step "App tarballs: Memories $MEMORIES_VERSION, Recognize $RECOGNIZE_VERSION"
	python3 "$HERE/versions.py" fetch memories "$MEMORIES_VERSION" "$FACETAG_CACHE/apps" "$NC_MAJOR"
	python3 "$HERE/versions.py" fetch recognize "$RECOGNIZE_VERSION" "$FACETAG_CACHE/apps" "$NC_MAJOR"

	step "Nextcloud $NEXTCLOUD_VERSION + PostgreSQL"
	# Nobody logs in as admin or as the test user (the tests use an app password), so random ones
	NC_ADMIN_PASSWORD=$(head -c 18 /dev/urandom | base64 | tr -d '/+=') compose up -d --quiet-pull
	for _ in $(seq 1 300); do
		if occ status --output=json 2>/dev/null | grep -q '"installed":true'; then break; fi
		sleep 2
	done
	occ status

	step "Install Memories, Recognize and memories_facetag"
	for app in memories:"$MEMORIES_VERSION" recognize:"$RECOGNIZE_VERSION"; do
		compose exec -T -u www-data nextcloud sh -c "rm -rf $APPS/${app%%:*} && tar -xzf /ci-cache/apps/${app%%:*}-${app#*:}.tar.gz -C $APPS"
	done
	copy_app

	occ app:enable memories

	# Recognize's install step would download node, CPU and GPU libtensorflow and ffmpeg (~650 MB)
	# on every run. Instead: the node binary goes in first (so the step keeps it), the step's
	# libtensorflow download is pointed at a dead address so it fails at once (a warning, not an
	# error), and then the CPU libtensorflow and tfjs-node binding the release declares are put
	# where the step would have put them — all from the cache. See recognize_runtime.py.
	local rt node libtensorflow binding napi node_dir
	rt=$(python3 "$HERE/recognize_runtime.py" "$FACETAG_CACHE/apps/recognize-$RECOGNIZE_VERSION.tar.gz" "$FACETAG_CACHE")
	eval "$rt"
	compose exec -T -u www-data nextcloud sh -euc "
		cd $APPS/recognize
		tar -xzf /ci-cache/$node -C /tmp $node_dir/bin/node
		mkdir -p bin && mv /tmp/$node_dir/bin/node bin/node && chmod 755 bin/node"
	occ config:app:set recognize node_binary --lazy --value="$APPS/recognize/bin/node"
	occ config:app:set recognize tensorflow.purejs --lazy --value=false
	compose exec -T -u www-data -e TFJS_NODE_CDN_STORAGE=http://127.0.0.1:9/ nextcloud php occ app:enable recognize
	compose exec -T -u www-data nextcloud sh -euc "
		cd $APPS/recognize/node_modules/@tensorflow/tfjs-node
		rm -rf deps/lib && mkdir -p deps lib
		tar -xzf /ci-cache/$libtensorflow -C deps
		tar -xzf /ci-cache/$binding -C lib
		test -f lib/napi-v$napi/tfjs_binding.node"
	occ config:app:set recognize nice_binary --lazy --value=/usr/bin/nice
	occ app:enable memories_facetag
	occ app:list --output=json | python3 -c '
import json, sys
apps = json.load(sys.stdin)["enabled"]
for a in ("memories", "recognize", "memories_facetag"):
    print(a.ljust(20), apps.get(a, "NOT ENABLED"))
    if a not in apps: sys.exit(1)'

	step "Configure: cron mode (nothing runs behind the tests' back), Recognize faces only on tfjs-node"
	occ background:cron
	for m in faces imagenet landmarks movinet musicnn; do
		occ config:app:set recognize "$m.enabled" --value="$([ $m = faces ] && echo true || echo false)"
	done
	occ config:app:set recognize tensorflow.gpu --value=false
	echo "node_binary: $(occ config:app:get recognize node_binary)"
	compose exec -T -u www-data nextcloud sh -c 'cd custom_apps/recognize && bin/node -e "const tf = require(\"@tensorflow/tfjs-node\"); console.log(\"tfjs-node loads, backend\", tf.getBackend())"'

	step "Test user $TEST_USER"
	compose exec -T -u www-data -e OC_PASS="$(head -c 18 /dev/urandom | base64)" nextcloud php occ user:add --password-from-env "$TEST_USER" >/dev/null || true
	occ user:info "$TEST_USER" --output=json >/dev/null
}

dataset() {
	step "Photo set"
	uv run --no-project --with pillow python "$ROOT/tests/data/fetch.py" "$FACETAG_CACHE/photos"
	compose exec -T -u www-data nextcloud sh -c "rm -rf data/$TEST_USER/files/Photos && mkdir -p data/$TEST_USER/files/Photos && cp -r /ci-cache/photos/. data/$TEST_USER/files/Photos/"
	occ files:scan "$TEST_USER" -q
	step "Memories index"
	occ memories:index --user="$TEST_USER"
	# classify prints every descriptor; keep it in a file (stack.sh logs copies it out)
	step "Recognize: classify (faces)"
	compose exec -T -u www-data nextcloud sh -c 'php occ recognize:classify >/tmp/recognize-classify.log 2>&1; rc=$?; grep -E "^Face results|No files left|rror" /tmp/recognize-classify.log | tail -3; exit $rc'
	step "Recognize: cluster faces"
	occ recognize:cluster-faces
	queued=$(psql_ -c "SELECT count(*) FROM oc_recognize_queue_faces")
	echo "Recognize's face queue: $queued files left"
	[ "$queued" = 0 ]
	step "Name Recognize's clusters from the ground truth"
	python3 "$ROOT/tests/ci/name_clusters.py"
}

run_tests() {
	step "Server-side tests"
	uv run --no-project --with pytest --with requests python -m pytest "$ROOT/tests/ci/server" -v --tb=short -p no:cacheprovider "$@"
}

logs() {
	local dir=$1
	mkdir -p "$dir"
	compose exec -T nextcloud sh -c 'cat /var/www/html/data/nextcloud.log 2>/dev/null' >"$dir/nextcloud.log" || true
	compose logs --no-color >"$dir/containers.log" 2>&1 || true
	compose exec -T nextcloud cat /tmp/recognize-classify.log >"$dir/recognize-classify.log" 2>/dev/null || true
	occ app:list >"$dir/apps.txt" 2>&1 || true
}

case ${1:-} in
up) up ;;
app) reinstall_app ;;
dataset) dataset ;;
test) shift; run_tests "$@" ;;
all) up; dataset; run_tests ;;
occ) shift; occ "$@" ;;
psql) shift; psql_ -c "$*" ;;
logs) logs "${2:?directory}" ;;
down) compose down -v --remove-orphans ;;
env)
	# for the Python tests and helper scripts
	echo "COMPOSE_PROJECT_NAME=$COMPOSE_PROJECT_NAME TEST_USER=$TEST_USER"
	;;
*) sed -n '2,20p' "$0"; exit 1 ;;
esac
