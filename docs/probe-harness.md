# Running the probes across VPN exits

`probes/` measures how Google actually behaves. Throttling and the consent wall are both keyed to
the egress address, so the measurements that mean anything are the ones that hold everything
constant except the exit. This is the machinery for that.

Credentials never enter this repository. Everything secret lives in `$GNEWS_LAB_DIR`
(default `~/.gnews-lab`), along with the result rows.

```sh
export GNEWS_LAB_DIR=~/.gnews-lab
docker build -f probes/runner/Dockerfile -t gnews-probe .

probes/runner/probe latvia walled SERVER_HOSTNAMES=node-lv-01.protonvpn.net
probes/runner/parallel walled
probes/runner/parallel budget LIMIT=60
```

## Why gluetun

The tunnel is [gluetun](https://github.com/qdm12/gluetun)'s job, not ours. It is a VPN client in a
container supporting ProtonVPN over both OpenVPN and WireGuard, with a kill switch, and other
containers join its network namespace with `--network=container:...`.

Adopting it deleted the parts of this harness most likely to be wrong:

| was | now |
|---|---|
| one `.ovpn` per exit, generated from a 1.8 MB API dump by a Proton-specific script | one line per exit in `exits.tsv`, naming a country or hostname |
| `openvpn` installed in the probe image, brought up by our own script | gluetun, which also has a kill switch we did not write |
| a poll loop deciding whether egress had moved | gluetun is unhealthy until egress works, so nothing can leave before then |
| `--cap-add=NET_ADMIN` and `/dev/net/tun` on the container running our code | only on gluetun's |

What we keep is a check that we are not measuring this laptop, because a probe that did exactly
that while labelling the row as a remote exit put a bogus row in a survey once.
`probes/entrypoint.sh` asks a third-party address service from inside the tunnel and refuses to
run if the answer is missing, unparseable as an address, or equal to the host's.

It does **not** check that the address belongs to the exit you asked for. A wrong hostname in
`exits.tsv` yields rows labelled `latvia` from a Norwegian address, and the label is what
analysis groups by. The address is in every row, so this is recoverable after the fact — but only
if you look. Related: with a bare `SERVER_COUNTRIES`, gluetun re-picks a server whenever it
reconnects, and its own healthcheck drives reconnection, so the address recorded at startup can
go stale part-way through a long probe.

## Credentials

**OpenVPN — what the existing setup already has.** Sign in to `account.protonvpn.com`, go to
**Account → OpenVPN / IKEv2 username**, and put the username on line 1 of
`$GNEWS_LAB_DIR/auth.txt` and the password on line 2. These are VPN-scoped credentials, not your
Proton account login. This is the default (`VPN_TYPE=openvpn`).

**WireGuard — one key for every server.** Sign in to `account.protonvpn.com` with your **Proton
account**, then **Downloads → WireGuard configuration**: name it, pick a platform and options,
pick any server, **Create**, **Download**. The `.conf` is standard WireGuard; copy the
`PrivateKey` value out of its `[Interface]` block into `$GNEWS_LAB_DIR/wireguard.key` and run with
`VPN_TYPE=wireguard`. The same key authenticates every Proton server, so one config is enough —
gluetun chooses the server, and the `Endpoint` and `PublicKey` in the file are not used.

The OpenVPN credentials **cannot** produce a WireGuard key, so the dashboard step above is manual
by design. Reported reason, which I have not verified against the API myself: Proton's certificate
endpoint requires a full account session scope, granted to an account login rather than to
VPN-scoped credentials ([gluetun's Proton
notes](https://github.com/qdm12/gluetun-wiki/blob/main/setup/providers/protonvpn.md), [Proton's
own WireGuard page](https://protonvpn.com/support/wireguard-configurations)). Likewise "one key
for every server" is gluetun's documented behaviour rather than something measured here; what is
certain is that gluetun supplies the endpoint and server public key from its own list, so those
fields in the downloaded `.conf` go unused.

Note also that the old lab README described downloading WireGuard configs while the harness ran
OpenVPN throughout; following it produced files nothing could read.

## Exits

`$GNEWS_LAB_DIR/exits.tsv` is a label, a tab, and the gluetun selection. Start from
`probes/runner/exits.tsv.example`. To see what is available:

```sh
docker run --rm --entrypoint /gluetun-entrypoint qmcgaw/gluetun format-servers -protonvpn
```

Pin `SERVER_HOSTNAMES` when you want the same exit twice. A bare `SERVER_COUNTRIES` wanders
between runs, and since the throttle is per address, a wandering exit quietly changes what is
being measured. Every row records the address either way.

Nicaragua is absent from gluetun's bundled Proton list, so one of the nine exits used before this
change has no equivalent. gluetun can refresh that list at runtime if it matters.

## The trap worth remembering

`parallel` holds wall-clock constant. It does **not** give each probe its own budget. Two
budget-spending probes on one exit in one window measure the second against the remains of the
first: 8 of 11 arms once refused at request 1 for exactly this reason — the figure is recorded in
[probes/README.md](../probes/README.md), which is also where the two confounded runs it took to
notice are described. Split the exits, or rest them.

## What the image does and does not contain

`probes/runner/Dockerfile` installs the library's dependencies and then uninstalls the library, so
`import googlenewsdecoder` can only be satisfied by the working tree mounted at `/lib-src`. That
is deliberate. The previous image baked in a copy of the source, needing a sync-and-rebuild step
before every run, and that step is what drifted -- it carried `decoderv1` through `decoderv4` for
months after they were deleted, so the lab measured code that no longer existed. A mount cannot
drift, and a missing mount is an `ImportError` rather than a silent fall-through to a stale copy.

The consequence is that the image only needs rebuilding when dependencies change. It also means
the lab no longer exercises `pip install`; CI's `TEST_INSTALLED_PACKAGE=1` job covers that against
the built artifact, so there is no need to test it twice.
