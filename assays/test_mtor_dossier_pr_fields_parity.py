from mtor.cli import _compact_dossier_payload


def test_dossier_payload_includes_pr_fields():
    task_result = {
        "branch_name": "b1",
        "pr_url": "https://x",
        "pr_number": 3,
        "pr_created": True,
        "pr_error": "",
        "success": True,
    }

    payload = _compact_dossier_payload(
        workflow_id="wf1",
        status_val="COMPLETED",
        task_result=task_result,
        review={},
        dossier=None,
    )

    assert payload["branch_name"] == "b1"
    assert payload["pr_url"] == "https://x"
    assert payload["pr_number"] == 3
    assert payload["pr_created"] is True
