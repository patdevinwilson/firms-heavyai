import json
import unittest
from pathlib import Path
from app.server import map_query
from infra.template import template
from scripts.deploy import validate_config

ROOT = Path(__file__).resolve().parents[1]


class DeploymentTests(unittest.TestCase):
    def config(self):
        config = json.loads((ROOT / "config.example.json").read_text())
        config["heavyai_image"] = "example/heavyai:v9.0.2"
        return config

    def test_valid_portable_config(self):
        validate_config(self.config())

    def test_source_build_defaults_need_no_image_credentials(self):
        config = json.loads((ROOT / "config.example.json").read_text())
        validate_config(config)
        self.assertTrue(config["build_from_source"])
        for field in ("heavydb_ref", "heavyiq_ref"):
            invalid = dict(config, **{field: "main"})
            with self.assertRaises(ValueError):
                validate_config(invalid)

    def test_unpinned_and_shell_images_rejected(self):
        for image in ("example:latest", "example;touch /tmp/pwn", ""):
            config = self.config()
            config["heavyai_image"] = image
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_archive_needs_checksum(self):
        config = self.config()
        config["heavyai_image_archive"] = "https://example.org/image.tar"
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_global_sources_cannot_repeat(self):
        config = self.config()
        config["sources"] *= 2
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_default_template_has_no_inbound_ports(self):
        result = template()
        self.assertEqual(result["Resources"]["SecurityGroup"]["Properties"]["SecurityGroupIngress"], [])
        self.assertEqual(result["Resources"]["Instance"]["Properties"]["MetadataOptions"]["HttpTokens"], "required")

    def test_existing_network_is_never_managed_by_stack(self):
        result = template(existing_network=True)
        resources = result["Resources"]
        for name in ("VPC", "Subnet", "InternetGateway", "RouteTable", "InternetRoute"):
            self.assertNotIn(name, resources)
        self.assertEqual(resources["SecurityGroup"]["Properties"]["VpcId"], {"Ref": "ExistingVpcId"})
        self.assertEqual(resources["SecurityGroup"]["Properties"]["SecurityGroupIngress"], [])
        self.assertNotIn("DependsOn", resources["Instance"])

    def test_existing_network_ids_must_be_paired(self):
        config = self.config()
        config["existing_vpc_id"] = "vpc-21f92c44"
        with self.assertRaises(ValueError):
            validate_config(config)
        config["existing_subnet_id"] = "subnet-8de1cba5"
        validate_config(config)

    def test_archive_and_secret_are_retained(self):
        result = template()
        for resource in ("Archive", "Secret"):
            self.assertEqual(result["Resources"][resource]["DeletionPolicy"], "Retain")

    def test_no_secret_values_in_userdata(self):
        result = template()
        text = result["Resources"]["Instance"]["Properties"]["UserData"]["Fn::Base64"]
        self.assertNotIn("firms_map_key", text)
        self.assertNotIn("heavyai_password", text)

    def test_map_filters_reject_injection(self):
        for params in ({"hours": ["24; DROP TABLE x"]}, {"satellite": ["N20' OR 1=1"]},
                       {"confidence": ["unsupported"]}, {"hours": ["99999"]}):
            with self.assertRaises(ValueError):
                map_query(params)

    def test_map_query_is_bounded_and_uses_public_view(self):
        sql = map_query({"hours": ["24"], "confidence": ["h"]})
        self.assertIn("FROM firms_observations", sql)
        self.assertIn("confidence='h'", sql)
        self.assertIn("-24", sql)
        self.assertNotIn(";", sql)


if __name__ == "__main__":
    unittest.main()
