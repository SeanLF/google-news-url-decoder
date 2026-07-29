#!/usr/bin/env bash
# In-container half of ./probe: bring up the tunnel, confirm egress actually moved, run the probe.
set -uo pipefail

openvpn --config /configs/only.ovpn --auth-user-pass /auth.txt --auth-nocache \
        --daemon --log /tmp/openvpn.log

if [ -z "${HOST_IP:-}" ]; then
  # Empty rather than unset, so `set -u` does not catch it. Without a host address there is
  # nothing to compare against, and every check below would pass vacuously.
  printf '{"exit":"%s","error":"HOST_IP is empty; cannot tell the tunnel apart from the host"}\n' "$EXIT_NAME"
  exit 0
fi

EXIT_IP=""
for _ in $(seq 1 25); do
  sleep 2
  # -f so an HTTP error is a failure rather than an error page treated as an address, and a
  # shape check because a captive portal answers 200 with prose. The old test was "non-empty
  # and different from the host", which any of those satisfy -- and being handed a body that
  # is not an address is exactly how a bogus row got into a survey once already.
  candidate=$(curl -sf --max-time 5 https://api.ipify.org || true)
  # Hex and colons allowed as well as dots: an IPv6 exit is a legitimate answer here, and the
  # two are separate budgets, so silently rejecting one would look like a dead tunnel.
  case "$candidate" in
    *[!0-9.:a-fA-F]*|"") continue ;;
  esac
  if [ "$candidate" != "$HOST_IP" ]; then
    EXIT_IP=$candidate
    break
  fi
done

if [ -z "$EXIT_IP" ]; then
  printf '{"exit":"%s","error":"tunnel never came up or egress did not change","log":"%s"}\n' \
    "$EXIT_NAME" "$(tail -1 /tmp/openvpn.log 2>/dev/null | tr -d '"')"
  exit 0
fi

export EXIT_IP
exec python "/probes/${PROBE}.py"
