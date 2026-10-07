import json
from pathlib import Path
from io import BytesIO
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from openpyxl import Workbook

from .services import (
    _create_ollama_plan,
    _ollama_request,
    create_deterministic_plan,
    create_plan,
    execute_plan,
    is_invalid_completeness_refusal,
    load_tables,
    profile_tables,
    validate_code,
)


class AnalystCoreTests(TestCase):
    def setUp(self):
        sample = Path(__file__).resolve().parent.parent / "sample_sales.csv"
        self.tables, self.sources = load_tables([{"name": "sample_sales.csv", "path": str(sample)}])

    def test_sample_total_is_564800(self):
        code = "df = dfs['sample_sales']\nresult = float(df['Sales'].sum())\nevidence = [{'rows': int(len(df))}]\nquality_notes = []"
        output = execute_plan(code, self.tables)
        self.assertEqual(output["result"], 564800.0)

    def test_scalar_aggregate_is_not_indexed_and_reports_safe_error(self):
        code = (
            "df = dfs['sample_sales']\n"
            "result = df['Sales'].sum()[0]\n"
            "evidence = []\n"
            "quality_notes = []"
        )

        with self.assertRaisesRegex(
            RuntimeError,
            r"Generated pandas analysis failed \((?:KeyError|IndexError)\)\.",
        ):
            execute_plan(code, self.tables)

    def test_profile_reports_shape(self):
        profile = profile_tables(self.tables, self.sources)["sample_sales"]
        self.assertEqual(profile["rows"], 12)
        self.assertIn("Sales", profile["columns"])

    def test_detects_invalid_small_sample_refusal(self):
        self.assertTrue(is_invalid_completeness_refusal({
            "status": "CANNOT_DETERMINE",
            "reason": "Only 12 rows are available and a larger dataset is required.",
        }))
        self.assertFalse(is_invalid_completeness_refusal({
            "status": "CANNOT_DETERMINE",
            "reason": "No cost or profit column is available.",
        }))

    def test_blocks_file_access(self):
        with self.assertRaises(ValueError):
            validate_code("result = open('secret')\nevidence = []\nquality_notes = []")

    def test_profit_is_refused_when_cost_data_is_missing(self):
        plan = create_plan(
            "What is the profit for each product?",
            profile_tables(self.tables, self.sources),
        )
        self.assertEqual(plan["status"], "CANNOT_DETERMINE")
        self.assertEqual(plan["code"], "")
        self.assertIn("no cost/profit data", plan["reason"])

    def test_deterministic_total_sales_plan_executes(self):
        plan = create_deterministic_plan(
            "What is the total sales?", profile_tables(self.tables, self.sources)
        )
        output = execute_plan(plan["code"], self.tables)
        self.assertEqual(output["result"], 564800.0)

    def test_deterministic_sales_by_region_plan_executes(self):
        plan = create_deterministic_plan(
            "What is the total sales by region?",
            profile_tables(self.tables, self.sources),
        )
        output = execute_plan(plan["code"], self.tables)
        self.assertEqual(output["result"]["South"], 232000.0)

    @override_settings(IS_VERCEL=True)
    def test_vercel_executes_validated_plan_in_process(self):
        code = (
            "df = dfs['sample_sales']\n"
            "result = float(df['Sales'].sum())\n"
            "evidence = [{'rows': int(len(df))}]\nquality_notes = []"
        )
        output = execute_plan(code, self.tables)
        self.assertEqual(output["result"], 564800.0)

    @override_settings(
        OLLAMA_URL="https://ollama.com",
        OLLAMA_API_KEY="test-cloud-key",
    )
    @patch("analyst.services.urllib.request.urlopen")
    def test_ollama_cloud_request_uses_bearer_key(self, mocked_urlopen):
        response = MagicMock()
        response.read.return_value = b'{"message":{"content":"ok"}}'
        mocked_urlopen.return_value.__enter__.return_value = response

        _ollama_request("/api/chat", {"model": "test"})

        request = mocked_urlopen.call_args.args[0]
        self.assertEqual(request.full_url, "https://ollama.com/api/chat")
        self.assertEqual(request.get_header("Authorization"), "Bearer test-cloud-key")

    @override_settings(OLLAMA_URL="https://ollama.com", OLLAMA_API_KEY="")
    def test_ollama_cloud_requires_api_key(self):
        with self.assertRaisesRegex(RuntimeError, "OLLAMA_API_KEY is missing"):
            _ollama_request("/api/chat", {"model": "test"})

    @override_settings(OLLAMA_URL="https://ollama.com", OLLAMA_MODEL="test")
    @patch("analyst.services._ollama_request")
    def test_ollama_cloud_accepts_json_from_thinking_field(self, request_mock):
        request_mock.return_value = {
            "message": {
                "content": "",
                "thinking": "Here is the plan:\n```json\n"
                '{"status":"CANNOT_DETERMINE","reason":"missing",'
                '"code":"","assumptions":[],"used_tables":[]}\n```',
            }
        }

        plan = _create_ollama_plan("question", {})

        self.assertEqual(plan["status"], "CANNOT_DETERMINE")

    @override_settings(OLLAMA_URL="https://ollama.com", OLLAMA_MODEL="test")
    @patch("analyst.services._ollama_request")
    def test_ollama_cloud_accepts_tool_call_arguments(self, request_mock):
        request_mock.return_value = {
            "message": {
                "tool_calls": [{"function": {"arguments": {
                    "status": "READY", "reason": "supported",
                    "code": "result = 1\nevidence = []\nquality_notes = []",
                    "assumptions": [], "used_tables": ["sales"],
                }}}]
            }
        }

        plan = _create_ollama_plan("question", {})

        self.assertEqual(plan["status"], "READY")

    @override_settings(OLLAMA_URL="https://ollama.com", OLLAMA_MODEL="test")
    @patch("analyst.services._ollama_request")
    def test_ollama_cloud_accepts_double_encoded_json(self, request_mock):
        inner = json.dumps({
            "status": "CANNOT_DETERMINE", "reason": "missing", "code": "",
            "assumptions": [], "used_tables": [],
        })
        request_mock.return_value = {"message": {"content": json.dumps(inner)}}

        plan = _create_ollama_plan("question", {})

        self.assertEqual(plan["status"], "CANNOT_DETERMINE")

    def test_uploaded_file_can_be_removed_from_workspace(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with patch(
                    "analyst.views.ollama_status",
                    return_value={"online": True, "ready": True, "model": "test"},
                ):
                    response = self.client.post(
                        reverse("upload"),
                        {
                            "datasets": SimpleUploadedFile(
                                "remove-me.csv",
                                b"region,sales\nNorth,10\n",
                                content_type="text/csv",
                            )
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    records = self.client.session["uploaded_files"]
                    self.assertEqual(len(records), 1)
                    stored_path = Path(records[0]["path"])
                    self.assertTrue(stored_path.is_file())

                    dashboard = self.client.get(reverse("dashboard"))
                    self.assertContains(dashboard, "Remove remove-me.csv")

                    response = self.client.post(reverse("delete_file", args=[0]))
                    self.assertEqual(response.status_code, 302)
                    self.assertFalse(stored_path.exists())
                    self.assertEqual(self.client.session["uploaded_files"], [])

    def test_upload_works_with_vercel_signed_cookie_sessions(self):
        with TemporaryDirectory() as media_root:
            with override_settings(
                MEDIA_ROOT=media_root,
                SESSION_ENGINE="django.contrib.sessions.backends.signed_cookies",
            ):
                response = self.client.post(
                    reverse("upload"),
                    {
                        "datasets": SimpleUploadedFile(
                            "vercel.csv",
                            b"region,sales\nNorth,10\n",
                            content_type="text/csv",
                        )
                    },
                )

                self.assertEqual(response.status_code, 302)
                session = self.client.session
                self.assertRegex(session["workspace_id"], r"^[0-9a-f]{32}$")
                self.assertEqual(len(session["uploaded_files"]), 1)
                self.assertTrue(Path(session["uploaded_files"][0]["path"]).is_file())

    def test_dashboard_ignores_expired_serverless_temp_path(self):
        session = self.client.session
        session["uploaded_files"] = [
            {"name": "expired.xlsx", "path": "/tmp/no-longer-present.xlsx", "size": 10}
        ]
        session.save()

        with patch(
            "analyst.views.ollama_status",
            return_value={"online": True, "ready": True, "model": "test"},
        ):
            response = self.client.get(reverse("dashboard"))

        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "Could not inspect a dataset")

    @override_settings(IS_VERCEL=True)
    def test_vercel_upload_profiles_in_the_same_request(self):
        with TemporaryDirectory() as media_root, override_settings(MEDIA_ROOT=media_root):
            with patch(
                "analyst.views.ollama_status",
                return_value={"online": True, "ready": True, "model": "test"},
            ):
                response = self.client.post(
                    reverse("upload"),
                    {
                        "datasets": SimpleUploadedFile(
                            "serverless.csv",
                            b"region,sales\nNorth,10\n",
                            content_type="text/csv",
                        )
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "serverless")
        self.assertContains(response, "1 rows")

    @override_settings(AI_PROVIDER="ollama")
    @patch("analyst.views.create_plan")
    def test_ask_can_analyze_file_sent_with_same_serverless_request(self, create_plan_mock):
        create_plan_mock.return_value = {
            "status": "CANNOT_DETERMINE",
            "reason": "test response",
            "code": "",
            "assumptions": [],
            "used_tables": [],
            "provider_label": "test",
        }
        with patch(
            "analyst.views.ollama_status",
            return_value={"online": True, "ready": True, "model": "test"},
        ):
            response = self.client.post(
                reverse("ask"),
                {
                    "question": "What are total sales?",
                    "datasets": SimpleUploadedFile(
                        "request.csv",
                        b"region,sales\nNorth,10\n",
                        content_type="text/csv",
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "test response")
        profile = create_plan_mock.call_args.args[1]
        self.assertEqual(profile["request"]["rows"], 1)

    @patch("analyst.views.create_plan")
    def test_ask_replans_once_after_generated_code_execution_error(self, create_plan_mock):
        create_plan_mock.side_effect = [
            {
                "status": "READY",
                "reason": "Total sales",
                "code": (
                    "df = dfs['request']\n"
                    "result = df['sales'].sum()[0]\n"
                    "evidence = []\n"
                    "quality_notes = []"
                ),
                "assumptions": [],
                "used_tables": ["request"],
            },
            {
                "status": "READY",
                "reason": "Total sales",
                "code": (
                    "df = dfs['request']\n"
                    "result = float(df['sales'].sum())\n"
                    "evidence = [{'total_sales': result}]\n"
                    "quality_notes = []"
                ),
                "assumptions": [],
                "used_tables": ["request"],
            },
        ]

        with patch(
            "analyst.views.ollama_status",
            return_value={"online": True, "ready": True, "model": "test"},
        ):
            response = self.client.post(
                reverse("ask"),
                {
                    "question": "What are total sales?",
                    "datasets": SimpleUploadedFile(
                        "request.csv",
                        b"region,sales\nNorth,10\nSouth,20\n",
                        content_type="text/csv",
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "30.0")
        self.assertEqual(create_plan_mock.call_count, 2)
        retry_call = create_plan_mock.call_args
        self.assertRegex(
            retry_call.kwargs["execution_error"],
            r"Generated pandas analysis failed \((?:KeyError|IndexError)\)\.",
        )

    @patch("analyst.views.create_plan")
    def test_missing_data_question_uses_profile_without_model_call(self, create_plan_mock):
        with patch(
            "analyst.views.ollama_status",
            return_value={"online": True, "ready": True, "model": "test"},
        ):
            response = self.client.post(
                reverse("ask"),
                {
                    "question": "missing data test",
                    "datasets": SimpleUploadedFile(
                        "missing.csv",
                        b"region,sales\nNorth,10\nSouth,\n",
                        content_type="text/csv",
                    ),
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "MISSING-DATA PROFILE")
        self.assertContains(response, "sales: 1")
        self.assertContains(response, "missing_count")
        create_plan_mock.assert_not_called()

    def test_xlsx_can_be_removed_after_dashboard_profiles_it(self):
        workbook = Workbook()
        sheet = workbook.active
        sheet.append(["region", "sales"])
        sheet.append(["North", 10])
        payload = BytesIO()
        workbook.save(payload)
        workbook.close()

        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                with patch(
                    "analyst.views.ollama_status",
                    return_value={"online": True, "ready": True, "model": "test"},
                ):
                    response = self.client.post(
                        reverse("upload"),
                        {
                            "datasets": SimpleUploadedFile(
                                "remove-me.xlsx",
                                payload.getvalue(),
                                content_type=(
                                    "application/vnd.openxmlformats-officedocument."
                                    "spreadsheetml.sheet"
                                ),
                            )
                        },
                    )
                    self.assertEqual(response.status_code, 302)
                    stored_path = Path(
                        self.client.session["uploaded_files"][0]["path"]
                    )

                    dashboard = self.client.get(reverse("dashboard"))
                    self.assertContains(dashboard, "Remove remove-me.xlsx")

                    response = self.client.post(reverse("delete_file", args=[0]))

                    self.assertEqual(response.status_code, 302)
                    self.assertFalse(stored_path.exists())
                    self.assertEqual(self.client.session["uploaded_files"], [])

    def test_delete_rejects_files_outside_current_session_upload_folder(self):
        with TemporaryDirectory() as media_root, TemporaryDirectory() as other_root:
            with override_settings(MEDIA_ROOT=media_root):
                session = self.client.session
                session["uploaded_files"] = [
                    {
                        "name": "private.csv",
                        "path": str(Path(other_root) / "private.csv"),
                        "size": 1,
                    }
                ]
                session.save()
                protected_file = Path(other_root) / "private.csv"
                protected_file.write_bytes(b"protected")

                response = self.client.post(reverse("delete_file", args=[0]))

                self.assertEqual(response.status_code, 302)
                self.assertTrue(protected_file.is_file())
                self.assertEqual(len(self.client.session["uploaded_files"]), 1)

    def test_delete_allows_upload_from_previous_session_folder(self):
        with TemporaryDirectory() as media_root:
            with override_settings(MEDIA_ROOT=media_root):
                session = self.client.session
                session["uploaded_files"] = []
                session.save()
                old_folder = Path(media_root) / "uploads" / "old-session-key"
                old_folder.mkdir(parents=True)
                old_path = old_folder / "previous-upload.csv"
                old_path.write_bytes(b"region,sales\nNorth,10\n")
                session = self.client.session
                session["uploaded_files"] = [
                    {
                        "name": "previous-upload.csv",
                        "path": str(old_path),
                        "size": old_path.stat().st_size,
                    }
                ]
                session.save()

                response = self.client.post(reverse("delete_file", args=[0]))

                self.assertEqual(response.status_code, 302)
                self.assertFalse(old_path.exists())
                self.assertEqual(self.client.session["uploaded_files"], [])
