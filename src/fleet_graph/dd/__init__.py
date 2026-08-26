"""fleet_graph.dd -- the dev-dispatch pipeline (P3).

**Where `contracts/` came from, and how it compares to what production runs.**

The bundle was copied from the dev-dispatch plugin's working checkout at
`ce58913`. Production dd is pinned to release `76c4003b`, which is a different
revision -- so the two were compared file by file rather than assumed equal:

- **10 of 11 files are byte-identical**, including every schema the pipeline
  validates against and both the lifecycle and artifact contracts. That is
  what matters: the machine this repo walks is the machine production walks.
- `attempt-context-capability.json` differs. The working checkout's manifest
  carries an extra `stages` section and therefore a different bundle digest.

The difference is harmless *here* because the two capability checks have
different jobs. `dd/capability.py` verifies this bundle against the manifest
shipped beside it -- self-consistent, and what a dispatch is checked against.
The plugin's own `verify_plugin_capability` verifies the *plugin release*
against digests that come from the operator's binding config, never from this
bundle. Pointing fleet-graph at the production-pinned plugin therefore checks
the pinned release, as it should.

It is still worth knowing, and `tests/test_contract_provenance.py` re-runs the
comparison whenever the plugin checkout is present. Same shape as the note in
`vendor/__init__.py`: production runs something other than main, and vendoring
from whatever was checked out is how a bundle quietly diverges.
"""
