#!/usr/bin/env bash
# In-container half of probes/runner/probe: confirm which address we are actually leaving from,
# then run the probe. One JSON line either way.
#
# This used to bring the tunnel up itself with openvpn and poll an address service until egress
# changed. gluetun owns the tunnel now, so what is left is the check -- and the check still
# matters: a probe that measures the host connection while labelling the row as a remote exit
# put a bogus row in a survey once already.
set -uo pipefail

fail() {
  printf '{"exit":"%s","error":"%s"}\n' "$EXIT_NAME" "$1"
  exit 0
}

if [ -z "${HOST_IP:-}" ]; then
  # Empty rather than unset, so `set -u` does not catch it. With no host address there is
  # nothing to compare against and every check below would pass vacuously.
  fail "HOST_IP is empty; cannot tell the tunnel apart from the host"
fi

# Asked of a third party rather than of gluetun. Its control server would need an auth config
# mounted -- current versions answer /v1/publicip/ip with 401 by default -- and it derives that
# address by querying an address service anyway, so this is the same source one hop closer, and
# it is what the outside world actually sees. One request, against a budget of roughly a hundred.
EXIT_IP=""
for _ in $(seq 1 20); do
  # -f so an HTTP error is a failure rather than an error page treated as an address.
  candidate=$(curl -sf --max-time 5 https://api.ipify.org || true)
  # Parsed, not pattern-matched. A glob that merely allows hex, dots and colons also accepts
  # ".", "0", "404", "cafe" and "999.999.999.999", every one of which then differs from HOST_IP
  # and so passes as an exit address. Python is already in this image, and IPv6 has to be
  # accepted here -- the two families are separate budgets, so rejecting one looks like a dead
  # tunnel rather than a working one.
  if [ -n "$candidate" ] && python -c 'import ipaddress,sys; ipaddress.ip_address(sys.argv[1])' "$candidate" 2>/dev/null; then
    EXIT_IP=$candidate
    break
  fi
  sleep 2
done

[ -n "$EXIT_IP" ] || fail "could not determine the egress address from inside the tunnel"
[ "$EXIT_IP" != "$HOST_IP" ] || fail "egress address equals the host address; the tunnel is not carrying traffic"

export EXIT_IP
exec python "/lib-src/probes/${PROBE}.py"
