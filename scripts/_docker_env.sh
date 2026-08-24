# Shared helper: make the docker CLI reachable. Sourced (not executed) by
# launch_local.sh and scheduled_job.sh.
#
# Docker Desktop normally symlinks its CLI into /usr/local/bin, but on this
# machine Docker.app lives in a custom folder ("/Applications/Python
# related/"), so the CLI is only on PATH in interactive shells. launchd jobs
# and Finder-launched apps get a minimal PATH and would not find it.
#
# If Docker.app ever moves, add its Contents/Resources/bin to this list.
if ! command -v docker >/dev/null 2>&1; then
    for _docker_dir in \
        /usr/local/bin \
        /opt/homebrew/bin \
        "/Applications/Python related/Docker.app/Contents/Resources/bin" \
        "/Applications/Docker.app/Contents/Resources/bin"; do
        if [ -x "$_docker_dir/docker" ]; then
            PATH="$_docker_dir:$PATH"
            export PATH
            break
        fi
    done
    unset _docker_dir
fi
