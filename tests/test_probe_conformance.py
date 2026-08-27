"""R3 conformance, capability row 1 (r3-design §3): probing belongs to agent-run.

Gateway liveness is agent-runtime's question to answer; the scheduler only
interprets the answer (ignite / back off / park). So the scheduler package must
not talk to a gateway protocol face directly: no httpx client, no endpoint
literals. This guard is what keeps the capability boundary from quietly
regressing after step 3 deletes the old prober.

Scope note: executors/text_node.py also talks to the gateway directly (an L1
text primitive that deliberately does not pay runtime cost). Its exemption is
a separate adjudication (r3-design §3 row 4) and it is outside scheduler/, so
this guard does not reach it either way.
"""

from __future__ import annotations

import inspect
from pathlib import Path

import fleet_graph.scheduler
from fleet_graph.scheduler.probe import CliGatewayProber

SCHEDULER_DIR = Path(fleet_graph.scheduler.__file__).resolve().parent

#: The direct-HTTP GatewayProber still lives in probe.py until R3 step 3
#: deletes it; this exemption is removed in the same commit. Nothing else in
#: the package may join this list.
LEGACY_EXEMPT_UNTIL_STEP_3 = {"probe.py"}

#: What "talking to a gateway face directly" looks like in this codebase.
BANNED_FRAGMENTS = ("httpx", "/v1/chat/completions", "/v1/responses")


class TestSchedulerPackageStaysOffTheGatewayFace:
    def test_no_scheduler_module_talks_to_the_gateway_directly(self) -> None:
        offenders: list[str] = []
        for path in sorted(SCHEDULER_DIR.glob("*.py")):
            if path.name in LEGACY_EXEMPT_UNTIL_STEP_3:
                continue
            text = path.read_text(encoding="utf-8")
            for fragment in BANNED_FRAGMENTS:
                if fragment in text:
                    offenders.append(f"{path.name}: {fragment!r}")
        assert offenders == []

    def test_the_cli_prober_path_itself_is_gateway_free(self) -> None:
        """probe.py is exempted only for the legacy prober's sake. The new
        path must not hide behind that exemption, so it is checked directly."""
        source = inspect.getsource(CliGatewayProber)
        for fragment in BANNED_FRAGMENTS:
            assert fragment not in source

    def test_the_exemption_list_names_only_the_legacy_file(self) -> None:
        assert {"probe.py"} == LEGACY_EXEMPT_UNTIL_STEP_3
