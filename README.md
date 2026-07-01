# aiohelvar
Asynchronous Python library to interact with Helvar Routers.

This library written to support the (work in progress) [Helvar HomeAssistant integration](https://github.com/tomplayford/homeassistant_helvar/). 

Features:
* Manages the async TCP comms well, keeps the connection alive and listens to broadcast messages
* Decodes the HelvarNet messages and translates things into Python objects that can easily be further translated into Home Assistant objects
* Discovers and retrieves Devices, Groups & Scenes and and all their properties, state and values.
* Keeps track of device states as scenes and devices change based on notifications from the router.
* Calls the more useful commands to control or read status from the above.

Very much a work in progress. Known TODOS:

* Multi-cluster / multi-router support - device discovery still targets only the
  router you connect to. The diagnostics *report* the other routers in the
  cluster (QUERY_ROUTERS, C:102); enumerating devices across them is still TODO
* Sensor support
* Support relative changes to scene levels update commands
* Full DALI-2 energy (C:252) / diagnostics (C:253) support - reply payloads are
  now parsed (see `dali2.py`; `-1`/`-2` map to unsupported/unavailable) and can
  be queried with `Router.query_dali2_energy()` / `query_dali2_diagnostics()`.
  The request wire-format is best-effort and should be confirmed on hardware
* Better test coverage

## Diagnostics & testing tools

Two read-only tools ship with the library to make it easy to test connectivity
and behaviour from a console (for example on the Raspberry Pi running Home
Assistant) **without touching device state and without any of your own data**.

### `diagnose` - read-only connection / version / capability check

```bash
python -m aiohelvar diagnose <router-host> [--port 50000] [--timeout 5] [--json]
```

It opens a connection, then runs a short sequence of read-only queries
(workgroup name, router & HelvarNet version, clusters, groups, device
discovery), each bounded by a timeout so it never hangs. It prints a report and
a verdict, and exits `0` if the router looks compatible or `1` otherwise (handy
for scripting). Example against an old firmware:

```
Device discovery    : ERROR   -> error 15: Invalid message command
Verdict: WARNING - Router reachable, but device discovery (query C:100) is not
supported by this firmware (error 15: Invalid message command). ...
```

Error code 15 ("Invalid message command") is the tell-tale sign that a router's
firmware does not implement a query - i.e. a firmware/capability limit rather
than a wiring or network fault. See `aiohelvar/error_codes.py` for the full
HelvarNet error-code table.

### `mock` - a fake router to test against

```bash
python -m aiohelvar mock [--profile modern|legacy] [--host 127.0.0.1] [--port 50000]
```

Starts an in-process HelvarNet server so you can exercise the diagnostics (or
Home Assistant) without real hardware:

* `modern` answers every query.
* `legacy` answers version/cluster queries but rejects device discovery,
  workgroup name and group enumeration with error 15 - reproducing the
  "it hangs / finds nothing" situation seen on older routers.

Flip a running mock between profiles from the console (it prints its PID on
start):

```bash
kill -HUP <pid>
```

These are also usable from your own async code:

```python
from aiohelvar.diagnostics import run_diagnostics
report = await run_diagnostics("192.0.2.10")
print(report.to_text())

from aiohelvar.mock_router import MockRouter, LEGACY
async with MockRouter(LEGACY, port=0) as mock:
    ...  # point a client at mock.host:mock.port
```

### Addressing & ports

The HelvarNet `@cluster.router` address is derived from the router's IP using
its *cluster mask* (Designer 5 Quick Start Guide §3.4). With the Helvar default
mask `255.255.255.0` and the usual `10.254.C.R` layout, cluster = 3rd octet and
router = 4th octet, which is what `Router` assumes by default. For other masks
or when the router is reached on an unrelated IP (e.g. via a bridge), pass
`cluster_id`/`router_id` with `use_specified_ids=True`. Note the HelvarNet
API/TCP port is `50000`; `60005` is the separate inter-router *cluster comms*
port.

### DALI-2 energy & diagnostics (newer routers only)

DALI-2 energy (C:252) and diagnostics (C:253) can be read per device on routers
that support them (e.g. the 950). On older routers (905/910/920) the query
raises `DALI2NotSupportedError` rather than returning bogus data:

```python
result = await router.query_dali2_energy(device_address)
print(result.value("ACTE"), result.status("APPP"))  # e.g. 1.234, 'unsupported'
```

The diagnostics also *report* the other routers in the cluster (QUERY_ROUTERS,
C:102), though device discovery itself still targets only the connected router.

## (Some of the) Known limitations 

### Lack of unique device IDs

I can't find a way to grab a unique ID for devices on the various Router busses. 

The DALI standard requires every device have a GTIN and a unique serial number. These appear in Helvar's router management software, but are not available on the 3rd party APIs. I've tried probing for undocumented commands with no luck. 

For now, I'm using the workgroup name + the helvar bus address as a unique address. This is *not* unique per physical device - it is, however, unique at any point in time. 

Open to better suggestions!

### Routers don't notify changes to individual devices.

We receive notifications when group scenes change, and since we know device levels for every scene, we can update devices levels without polling devices. 

However, we don't get notified when individual devices change their load. This shouldn't be an issue for most setups, as Helvar is scene oriented, and almost every happens that way. 

We also receive notifications when there are relative changes to scene levels, but we don't currently support those commands. 

If you're having trouble here, I suggest we implement a device polling option that can be enabled. 

### Router doesn't report decimal scene levels

If you set scene levels to a decimal, rather than an int. (e.g. 0.2 or 54.6). The only command available to retrieve scene levels only
returns the integer. 

The only time this is really a problem on dim scenes where a value of 0.25 would show light, but the command is reporting off. 

We get round this by polling all devices manually if we think they've been updated by a scene. Don't like it. 

### Colour changing loads.

I don't have a router that supports these as native DALI devices. So I have no idea how they appear :)

The HelvarNET docs don't mention how it's supported. 


## Requests to Helvar :)

* Please provide a command to retrieve a device's GTIN and / or serial number.
* Please provide a command to retrieve full decimal values of a scene table.

## Disclaimer

Halvar (TM) is a registered trademark of Helvar Ltd.

This software is not officially endorsed by Helvar Ltd in any way.

The authors of this software provide no support, guarantees, or warranty for its use, features, safety, or suitability for any task. We do not recommend you use it for anything at all, and we don't accept any liability for any damages that may result from its use.

This software is licensed under the Apache License 2.0. See the LICENCE file for more details. 

## Development

### Installing Test Dependencies

To run tests, you need to install the test dependencies:

```bash
pip install -r requirements-test.txt
```

### Running Tests

Run all tests:

```bash
python3 -m pytest
```


