# Sourced by the image's start.sh (as jovyan) right before the server starts.

# On out-of-memory, kill the notebook kernel instead of the server.
/usr/local/bin/eg-oom-watchdog &

# The EnergyGuard kernel is read-only. A user kernelspec with the same name in
# the persistent ~/.local volume would shadow the system one, so remove it.
rm -rf "${HOME}/.local/share/jupyter/kernels/eg-default" 2>/dev/null || true
