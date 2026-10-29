"""Run with: python -m unittest discover -s .github/tests -p 'test_slides*.py'."""
import importlib.util
import http.server
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import urllib.parse
import urllib.request

SCRIPT = Path(os.environ.get("SLIDES_HELPER", Path(__file__).parents[1] / "scripts/slides-production.py"))
spec = importlib.util.spec_from_file_location("slides_production", SCRIPT)
pipeline = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pipeline)

BASE = "https://products.aspose.com/slides/"


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def changed(self, *rows):
        path = self.root / "changed.tsv"
        path.write_text("".join(f"{url}\t{size}\n" for url, size in rows), encoding="utf-8")
        return path

    def test_equal_duplicates_collapse_before_sampling(self):
        rows = pipeline.read_expectations([self.changed((BASE, 42), (BASE, 42))])
        self.assertEqual(rows, {BASE: 42})

    def test_first_row_must_not_hide_conflicting_generated_lengths(self):
        with self.assertRaisesRegex(ValueError, "conflict"):
            pipeline.read_expectations([self.changed((BASE, 42), (BASE, 43))])

    def test_scope_rejects_a_sibling_family(self):
        with self.assertRaises(ValueError):
            pipeline.read_expectations([self.changed(("https://products.aspose.com/words/", 42))])

    def test_scope_rejects_encoded_parent_and_query(self):
        for url in (BASE + "%2e%2e/words/", BASE + "net/?x=1", BASE + "../words/"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                pipeline.read_expectations([self.changed((url, 42))])

    def test_sha_gate_rejects_equal_length_english_differences(self):
        a = self.root / "a"; b = self.root / "b"
        a.mkdir(); b.mkdir()
        (a / "index.html").write_bytes(b"one")
        (b / "index.html").write_bytes(b"two")
        manifests = []
        for root, lang in ((a, "de"), (b, "fr")):
            path = self.root / f"{lang}.json"
            pipeline.snapshot(root, lang, ["en", "de", "fr"], "a" * 40, path)
            manifests.append(path)
        with self.assertRaisesRegex(ValueError, "index.html"):
            pipeline.compare(manifests, ["de", "fr"], "a" * 40)

    def test_sha_gate_includes_markdown_shared_assets_llms_and_sitemaps(self):
        for filename in ("net/index.md", "css/style.css", "js/main.js", "llms.txt", "sitemap.xml", "en/sitemap.xml"):
            with self.subTest(filename=filename):
                paths = []
                for language, contents in (("de", b"aaa"), ("fr", b"bbb")):
                    root = self.root / filename.replace("/", "-") / language
                    target = root / filename
                    target.parent.mkdir(parents=True)
                    target.write_bytes(contents)
                    path = root.parent / f"{language}.json"
                    pipeline.snapshot(root, language, ["en", "de", "fr"], "a" * 40, path)
                    paths.append(path)
                with self.assertRaises(ValueError):
                    pipeline.compare(paths, ["de", "fr"], "a" * 40)

    def test_locale_outputs_can_differ_but_all_expected_jobs_are_required(self):
        paths = []
        for lang in ("de", "fr"):
            root = self.root / lang
            (root / lang).mkdir(parents=True)
            (root / "index.html").write_bytes(b"English")
            (root / lang / "index.html").write_bytes(lang.encode())
            path = self.root / f"{lang}.json"
            pipeline.snapshot(root, lang, ["en", "de", "fr"], "a" * 40, path)
            paths.append(path)
        pipeline.compare(paths, ["de", "fr"], "a" * 40)
        with self.assertRaises(ValueError):
            pipeline.compare(paths[:1], ["de", "fr"], "a" * 40)

    def test_plan_shards_contain_each_url_once_and_canaries_have_expectations(self):
        changed = self.root / "changed"; changed.mkdir()
        (changed / "de.tsv").write_text(f"{BASE}net/\t42\n{BASE}llms.txt\t7\n{BASE}net/\t42\n")
        out = self.root / "plan"
        self.assertEqual(pipeline.plan(changed, out, 1), 2)
        self.assertEqual(sorted(path.read_text().strip() for path in (out / "shards").glob("*.txt")), [BASE + "llms.txt", BASE + "net/"])
        self.assertEqual((out / "canaries.txt").read_text().splitlines(), [BASE + "llms.txt", BASE + "net/"])

    def test_plan_cli_emits_extensionless_shard_matrix(self):
        changed = self.root / "changed"; changed.mkdir()
        (changed / "de.tsv").write_text(f"{BASE}net/\t42\n{BASE}llms.txt\t7\n")
        github_output = self.root / "github-output"
        result = subprocess.run([sys.executable, str(SCRIPT), "plan", "--changed", str(changed), "--output", str(self.root / "plan"), "--shard-size", "1", "--github-output", str(github_output)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(github_output.read_text().strip(), 'shards=["000", "001"]')

    def test_real_http_status_length_and_redirect_failures_are_retained(self):
        requests = []
        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self):
                requests.append(self.path)
                status = 302 if self.path.endswith("redirect/") else 404 if self.path.endswith("missing/") else 200
                self.send_response(status)
                if status == 302:
                    self.send_header("Location", "/slides/good/")
                if not self.path.endswith("no-length/"):
                    self.send_header("Content-Length", "6" if self.path.endswith("truncated/") else "5" if self.path.endswith("wrong-length/") else "4")
                self.end_headers()
                self.wfile.write(b"body")
            def log_message(self, *args):
                pass
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True); thread.start()
        self.addCleanup(server.server_close); self.addCleanup(server.shutdown)
        real_opener = urllib.request.build_opener(pipeline.NoRedirect())
        class LocalTransport:
            def open(self, request, timeout):
                target = f"http://127.0.0.1:{server.server_port}" + urllib.parse.urlsplit(request.full_url).path
                return real_opener.open(urllib.request.Request(target, headers=dict(request.header_items())), timeout=timeout)
        with mock.patch.object(pipeline.urllib.request, "build_opener", return_value=LocalTransport()):
            self.assertEqual(pipeline.edge(BASE + "good/")[:2], (200, 4))
            truncated = BASE + "truncated/"
            truncated_report = self.root / "truncated-report"
            self.assertFalse(pipeline.verify({truncated: 6}, [truncated], truncated_report))
            self.assertIn("\t4\tFAIL\t", (truncated_report / "observations.tsv").read_text())
            self.assertIn("body_length_differs_from_header", (truncated_report / "observations.tsv").read_text())
            for suffix, code, length in (("redirect/", 302, 4), ("missing/", 404, 4), ("no-length/", 200, 4), ("wrong-length/", 200, 4)):
                with self.subTest(suffix=suffix):
                    url = BASE + suffix
                    self.assertEqual(pipeline.edge(url)[:2], (code, length))
                    report = self.root / suffix
                    self.assertFalse(pipeline.verify({url: 4}, [url], report))
                    self.assertIn(f"{url}\t{code}\t4\t", (report / "observations.tsv").read_text())
            self.assertEqual(requests.count("/slides/good/"), 1)

    def test_retry_keeps_failed_observation_then_records_recovery(self):
        current = {"length": 41}
        def retry(urls):
            self.assertEqual(urls, [BASE])
            current["length"] = 42
        report = self.root / "retry"
        self.assertTrue(pipeline.verify({BASE: 42}, [BASE], report, probe=lambda u: (200, current["length"], "", ""), retry=retry))
        rows = (report / "observations.tsv").read_text().splitlines()
        self.assertIn("\t41\tFAIL\t", rows[1])
        self.assertIn("\t42\tPASS\t", rows[2])

    def test_probe_exception_retains_failure_report(self):
        def broken(url):
            raise OSError("offline")
        report = self.root / "offline"
        self.assertFalse(pipeline.verify({BASE: 42}, [BASE], report, probe=broken))
        self.assertIn("\t0\t42\t\tFAIL\t", (report / "observations.tsv").read_text())
        self.assertTrue((report / "result.json").is_file())

    def test_nonowner_uploads_only_its_locale_and_owner_owns_shared_files(self):
        workflow = Path(os.environ.get("SLIDES_WORKFLOW", Path(__file__).parents[1] / "workflows/production-slides.yml")).read_text()
        marker = "      - name: Sync to Ceph S3 Bucket (additive - no --delete across languages)"
        block = workflow.split(marker, 1)[1].split("      - name:", 1)[0]
        script = block.split("        run: |\n", 1)[1]
        script = "\n".join(line[10:] if line.startswith("          ") else "" for line in script.splitlines())
        bash = shutil.which("bash")
        if os.name == "nt":
            bash = str(Path(os.environ["ProgramFiles"]) / "Git/bin/bash.exe")
        for language, wanted_source, wanted_target in (
            ("fr", "./public/fr/", "s3://products-aspose-com/slides/fr/"),
            ("de", "./public/", "s3://products-aspose-com/slides/"),
        ):
            with self.subTest(language=language):
                (self.root / "public" / language).mkdir(parents=True, exist_ok=True)
                # The fake records the real external command boundary without credentials.
                prefix = 'aws() { printf "S3_SOURCE=%s\\nS3_TARGET=%s\\n" "$3" "$4"; }; export AWS_ENDPOINT_URL=https://s3.invalid\n'
                result = subprocess.run([bash, "-c", prefix + script.replace("${{ matrix.language }}", language)], cwd=self.root, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn("S3_SOURCE=" + wanted_source + "\n", result.stdout)
                self.assertIn("S3_TARGET=" + wanted_target + "\n", result.stdout)

    def test_verifier_retains_per_url_diagnostics_on_fatal_canary(self):
        expectations = {BASE: 42, BASE + "net/": 7}
        observations = {BASE: (200, 41, "HIT", "edge"), BASE + "net/": (200, 7, "MISS", "edge")}
        report = self.root / "report"
        result = pipeline.verify(expectations, [BASE], report, probe=observations.__getitem__, sample_size=50)
        self.assertFalse(result)
        self.assertEqual((report / "watch.txt").read_text().splitlines(), sorted(expectations))
        rows = (report / "observations.tsv").read_text().splitlines()
        self.assertIn(BASE + "\t200\t42\t41\tFAIL", rows[1])
        self.assertIn(BASE + "net/\t200\t7\t7\tPASS", rows[2])
        self.assertEqual(json.loads((report / "result.json").read_text())["failed_canaries"], [BASE])

    def test_non200_same_length_and_missing_length_are_real_failures(self):
        for code, length in ((404, 42), (302, 42), (200, None)):
            with self.subTest(code=code, length=length):
                self.assertFalse(pipeline.verify({BASE: 42}, [BASE], self.root / f"r{code}-{length}", probe=lambda u: (code, length, "", "")))

    def test_correct_singleton_is_accepted_and_sample_is_unique(self):
        calls = []
        def probe(url):
            calls.append(url)
            return 200, 42, "HIT", "edge"
        self.assertTrue(pipeline.verify({BASE: 42}, [BASE], self.root / "good", probe=probe))
        self.assertEqual(calls, [BASE])


if __name__ == "__main__":
    unittest.main()
