def _execute_first_archive(authed):
    authed.post("/api/scan-jobs", json={"as_of_date": "2026-11-16"})
    pending = authed.get(
        "/api/judgments", params={"status": "待执行"}
    ).json()["items"]
    archive = next(j for j in pending if j["decided_action"] == "归档")
    resp = authed.post(f"/api/judgments/{archive['id']}/execute")
    assert resp.status_code == 200
    return archive["id"], resp.json()["run"]


def test_partition_api_marks_address_complete(authed):
    jid, run = _execute_first_archive(authed)
    parts = authed.get("/api/partitions").json()["items"]
    target = next(p for p in parts if p["archive_uri"] == run["archive_uri"])
    assert target["storage_state"] == "已归档"
    assert target["archive_uri_missing"] is False
    assert target["address_complete"] is True
    assert target["snapshot"] is not None
    assert target["snapshot"]["row_count"] > 0
    assert target["snapshot"]["content_digest"]


def test_archive_snapshots_endpoint_lists_verifiable_entity(authed):
    _execute_first_archive(authed)
    snaps = authed.get("/api/archive-snapshots").json()["items"]
    assert snaps, "归档后应能查询实物快照，供下游放行核对实体"
    s = snaps[0]
    assert s["archive_uri"].startswith("archive://retain/")
    assert s["row_count"] > 0
    assert s["content_digest"]


def test_policy_api_exposes_archive_completeness_overview(authed):
    _execute_first_archive(authed)
    body = authed.get("/api/policies").json()
    overview = {o["domain_code"]: o for o in body["archive_overview"]}
    touched = next(o for o in overview.values() if o["archived_count"] > 0)
    assert touched["missing_uri_count"] == 0
    assert touched["snapshot_count"] >= 1


def test_reconcile_api_reports_zero_mismatch_when_addresses_complete(authed):
    _execute_first_archive(authed)
    report = authed.post(
        "/api/reconcile-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["mismatch_count"] == 0
    assert report["missing_archive_uri_count"] == 0
    assert all(
        m["mismatch_type"] != "missing_archive_uri" for m in report["mismatches"]
    )


def test_double_execute_api_second_call_conflicts(authed):
    jid, _ = _execute_first_archive(authed)
    again = authed.post(f"/api/judgments/{jid}/execute")
    assert again.status_code == 409
