from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db.base import ValidationLevel, ValuationStatus
from app.db.models import ValidationResult, ValuationVersion

from .conftest import seed_pending_version


def test_review_acknowledge_and_publish_workflow(admin_client, app_and_engine) -> None:
    _, version_id = seed_pending_version(app_and_engine[1])

    queue = admin_client.get("/api/v1/reviews")
    acknowledged = admin_client.post(
        f"/api/v1/reviews/{version_id}/acknowledge",
        json={"allow_publish": True, "note": "已核对原始估值表"},
    )
    published = admin_client.post(
        f"/api/v1/valuations/{version_id}/publish",
        json={"reason": "复核通过"},
    )

    assert queue.status_code == 200
    assert queue.json()["meta"]["total"] == 1
    assert acknowledged.status_code == 200
    assert acknowledged.json()["data"]["status"] == "publishable"
    assert published.status_code == 200
    assert published.json()["data"]["version_id"] == version_id


def test_review_requires_reason_and_publish_handles_missing_version(
    admin_client,
) -> None:
    missing = admin_client.post(
        "/api/v1/valuations/999999/publish", json={"reason": "x"}
    )
    missing_review = admin_client.post(
        "/api/v1/reviews/999999/acknowledge",
        json={"allow_publish": True, "note": "x"},
    )

    assert missing.status_code == 409
    assert missing_review.status_code == 409


def test_manual_publish_ignores_validation_findings(
    admin_client, app_and_engine
) -> None:
    _, version_id = seed_pending_version(app_and_engine[1])
    with Session(app_and_engine[1]) as session:
        version = session.get(ValuationVersion, version_id)
        finding = session.scalar(
            select(ValidationResult).where(
                ValidationResult.valuation_version_id == version_id
            )
        )
        assert version is not None
        assert finding is not None
        version.status = ValuationStatus.PUBLISHABLE
        finding.level = ValidationLevel.WARNING
        session.commit()

    published = admin_client.post(
        f"/api/v1/valuations/{version_id}/publish",
        json={"reason": "业务已核对，接受该异常"},
    )

    assert published.status_code == 200
    assert published.json()["data"]["validation_ignored_count"] == 1

    review_rows = admin_client.get("/api/v1/reviews", params={"status": "published"})
    assert review_rows.status_code == 200
    row = review_rows.json()["data"][0]
    assert row["ignored_count"] == 1
    assert row["findings"][0]["ignored"] is True

    with Session(app_and_engine[1]) as session:
        finding = session.scalar(
            select(ValidationResult).where(
                ValidationResult.valuation_version_id == version_id
            )
        )
        assert finding is not None
        assert finding.ignored is True
        assert finding.ignored_reason == "业务已核对，接受该异常"


def test_batch_publish_processes_all_publishable_versions(
    admin_client, app_and_engine
) -> None:
    first = seed_pending_version(app_and_engine[1], fund_name="待复核产品一")[1]
    second = seed_pending_version(app_and_engine[1], fund_name="待复核产品二")[1]
    with Session(app_and_engine[1]) as session:
        for version_id in (first, second):
            version = session.get(ValuationVersion, version_id)
            assert version is not None
            version.status = ValuationStatus.PUBLISHABLE
        session.commit()

    response = admin_client.post(
        "/api/v1/reviews/batch-publish",
        json={"reason": "历史数据批量复核接受"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["requested"] == 2
    assert response.json()["data"]["published"] == 2
    assert response.json()["data"]["failed"] == []
