"""检查启动配置是否能按 runtime 的 model@runtime 契约选中唯一网关链。"""

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import yaml

SPEC = importlib.util.spec_from_file_location(
    "candidate_configure", Path(__file__).with_name("configure.py")
)
configure = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(configure)


class ModelConfigurationTests(unittest.TestCase):
    def setUp(self):
        scratch = Path(__file__).resolve().parents[3] / ".runtime/e2e/config-unit"
        scratch.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=scratch)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.fleet = self.root / "fleet"
        self.assets = self.root / "assets"
        self.state = self.root / "state"
        (self.fleet / "config/prompts").mkdir(parents=True)
        original = self.assets / "original-profiles"
        (original / "harness").mkdir(parents=True)
        (original / "routes.yaml").write_text(
            yaml.safe_dump(
                {
                    "routes": {
                        "deepseek-v4-pro@opencode/gw": {
                            "auth": "static",
                            "runtimes": ["opencode"],
                            "model": "gateway/deepseek-v4-pro",
                            "opencode_provider": {
                                "gateway": {"options": {"baseURL": "http://127.0.0.1:15722/v1"}}
                            },
                        }
                    },
                }
            )
        )
        (self.assets / "source-manifest.json").write_text(
            json.dumps(
                {
                    "fleet_commit": "1" * 40,
                    "runtime_commit": "2" * 40,
                }
            )
        )
        (self.assets / "python-packages.txt").write_text("配置回归测试\n")
        self.roles = ("goal", "impl", "cr", "fr", "scribe")
        for role in self.roles:
            (self.fleet / "config/prompts" / f"{role}.md").write_text("角色说明\n")
        self.version = mock.patch.object(configure, "version", return_value="配置测试")
        self.version.start()
        self.addCleanup(self.version.stop)

    def write_config(self, model, runtime="opencode"):
        (self.fleet / "config/codex.json").write_text(
            json.dumps(
                {"roles": {role: {"runtime": runtime, "model": model} for role in self.roles}}
            )
        )

    def test_source_chain_label_and_bare_model_both_select_single_gateway(self):
        for source_model in ("deepseek-v4-pro@opencode", "deepseek-v4-pro"):
            with self.subTest(source_model=source_model):
                self.write_config(source_model)
                path = configure.configure(self.state, self.fleet, self.assets)
                effective = json.loads(path.read_text())
                routes = yaml.safe_load(
                    (self.state / "config/runtime-profiles/routes.yaml").read_text()
                )
                manifest = json.loads((self.state / "candidate-manifest.json").read_text())
                self.assertEqual(set(routes["chains"]), {"deepseek-v4-pro@opencode"})
                self.assertEqual(set(routes["routes"]), {"deepseek-v4-pro@opencode/gw"})
                for role, settings in effective["roles"].items():
                    self.assertEqual(settings["model"], "deepseek-v4-pro")
                    self.assertTrue(
                        Path(settings["system_prompt_file"])
                        .read_text()
                        .endswith(configure.FINAL_RESPONSE_RULE)
                    )
                    self.assertEqual(
                        manifest["final_response_override"], configure.FINAL_RESPONSE_RULE
                    )
                    # 这是 runtime CLI 的调用边界；重复 @opencode 会在这里回归失败。
                    chain_key = settings["model"] + "@" + settings["runtime"]
                    self.assertEqual(
                        routes["chains"][chain_key]["routes"], ["deepseek-v4-pro@opencode/gw"]
                    )
                    self.assertEqual(
                        manifest["role_model_overrides"][role],
                        {
                            "original": source_model,
                            "effective": "deepseek-v4-pro",
                            "runtime": "opencode",
                            "resolved_chain": chain_key,
                        },
                    )

    def test_unapproved_model_or_native_runtime_is_rejected(self):
        for model, runtime in (("other-model", "opencode"), ("deepseek-v4-pro", "codex")):
            with self.subTest(model=model, runtime=runtime):
                self.write_config(model, runtime)
                with self.assertRaisesRegex(ValueError, "角色默认模型"):
                    configure.configure(self.state, self.fleet, self.assets)
